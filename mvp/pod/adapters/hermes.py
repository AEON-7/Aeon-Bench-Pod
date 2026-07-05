"""HermesAdapter — drives the `aeon-harness-hermes` container image (NousResearch/hermes-agent
inside python:3.11-slim, `TERMINAL_ENV=local` baked in so terminal/file tools execute INSIDE
the container — i.e. in the mounted /work — not docker-in-docker; built on the DGX, arm64).

Invocation (one one-shot container per task; the pod code runs ON the DGX):

    docker run --rm --network host -v <workdir>:/work -w /work aeon-harness-hermes \
        --query=<prompt> --base_url=<model_base_url> --api_key=sk-local --model=<alias> \
        --max_turns=8 --save_sample [--disabled_toolsets=<csv>]

(the image ENTRYPOINT is `python /app/run_agent.py`, so the argv after the image is the flag
list). `--save_sample` writes `sample_<uuid>.json` into the cwd (/work == workdir): a ShareGPT
transcript {"conversations": [{"from": "system|human|gpt|tool", "value": ...}, ...]} with tool
calls embedded in gpt turns as

    <tool_call>
    {"name": ..., "arguments": {...}}
    </tool_call>

and the final answer = the last gpt turn WITHOUT a tool_call. `parse_output(raw)` is a pure
function over that sample-file text (unit-tested against canned samples).

`AEON_HERMES_DISABLED_TOOLSETS` (csv) optionally disables toolsets; default none — the agent
needs its terminal/file tools for the environment-execution suite.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
import uuid

from .base import Adapter, AdapterError, run_argv, safe_name, strip_reasoning

IMAGE = os.environ.get("AEON_HERMES_IMAGE", "aeon-harness-hermes")
_API_KEY = "sk-local"
_MAX_TURNS = int(os.environ.get("AEON_HERMES_MAX_TURNS", "8"))

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def parse_output(raw: str) -> dict:
    """Pure parser: Hermes ShareGPT sample JSON text -> {"answer": str, "steps": [...]}.

    * steps: every `<tool_call>{json}</tool_call>` block inside gpt turns, in order —
      {"tool": name, "arguments"->"args"} parsed defensively (bad JSON -> {"_raw": ...});
    * answer: the LAST gpt turn containing NO tool_call block; if every gpt turn called a
      tool, fall back to the last gpt turn with the tool_call blocks stripped.
    """
    steps: list[dict] = []
    answer = ""
    fallback = ""
    try:
        obj = json.loads(raw or "")
    except Exception:
        return {"answer": "", "steps": []}
    convs = obj.get("conversations") if isinstance(obj, dict) else obj
    if not isinstance(convs, list):
        return {"answer": "", "steps": []}

    for turn in convs:
        if not isinstance(turn, dict) or turn.get("from") != "gpt":
            continue
        value = turn.get("value") or ""
        if not isinstance(value, str):
            value = str(value)
        blocks = _TOOL_CALL_RE.findall(value)
        for blk in blocks:
            try:
                call = json.loads(blk)
            except Exception:
                steps.append({"tool": "_unparsed", "args": {"_raw": blk[:400]}})
                continue
            name = call.get("name") if isinstance(call, dict) else None
            args = call.get("arguments") if isinstance(call, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {}
            if name:
                steps.append({"tool": str(name), "args": args})
        if blocks:
            stripped = strip_reasoning(_TOOL_CALL_RE.sub("", value))
            if stripped:
                fallback = stripped
        else:
            cleaned = strip_reasoning(value)   # drop leaked <think>…</think> (see base.strip_reasoning)
            if cleaned:
                answer = cleaned
    return {"answer": answer or fallback, "steps": steps}


class HermesAdapter(Adapter):
    name = "hermes"
    IMAGE = IMAGE

    def __init__(self):
        self._run_dir: str | None = None
        self._alias: str | None = None
        self._cfg_path: str | None = None
        self._disabled = os.environ.get("AEON_HERMES_DISABLED_TOOLSETS", "").strip()

    def _ensure_cfg(self) -> str:
        """Hermes REFUSES models whose reported context window is <64K (its tool-calling
        minimum). Our bench serves cap max-model-len at 32K purely for GB10 memory — the
        models' true windows are >=256K — so per Hermes' own guidance ("if your server
        reports a window smaller than the model's true window, set model.context_length")
        we mount a config declaring 65536: the smallest value that passes the gate, so a
        runaway transcript still fails honestly at the server's real 32K cap."""
        if self._cfg_path and os.path.isfile(self._cfg_path):
            return self._cfg_path
        d = self._run_dir or tempfile.mkdtemp(prefix="aeon_hermes_cfg_")
        p = os.path.join(d, "hermes-config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("model:\n  context_length: 65536\n")
        self._cfg_path = p
        return p

    # ---- v2 contract ------------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """Fresh per-model-run scratch dir (Hermes itself keeps no host-side config; every
        task container is `--rm` so agent state can never leak between models)."""
        d = os.path.join(run_root, f"hermes-{safe_name(served_alias)}")
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        self._run_dir, self._alias = d, served_alias
        return d

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 240) -> dict:
        if workdir is None:
            raise AdapterError("HermesAdapter (v2) requires a task workdir")

        before = set(glob.glob(os.path.join(workdir, "sample_*.json")))
        # Hermes resolves its file tools against TERMINAL_CWD (its single source-of-truth for
        # the agent working dir); without it, write_file falls back to "/" and the outputs are
        # lost. AND: the image ships a baked /work dir, so writes to /work land in the container
        # overlay and do NOT always propagate back through the bind mount — so we run a NAMED
        # (not --rm) container and `docker cp` /work back out afterwards, which captures the
        # agent's file outcomes and the sample reliably regardless of mount propagation.
        cname = f"aeon_hermes_{safe_name(served_alias)}_{uuid.uuid4().hex[:10]}"
        argv = [
            "docker", "run", "--name", cname, "--network", "host",
            "-e", "TERMINAL_CWD=/work",
            "-v", f"{self._ensure_cfg()}:/root/.hermes/config.yaml:ro",
            "-v", f"{workdir}:/work", "-w", "/work",
            self.IMAGE,
            f"--query={task.get('prompt', '')}",
            f"--base_url={model_base_url}",
            f"--api_key={_API_KEY}",
            f"--model={served_alias}",
            f"--max_turns={_MAX_TURNS}",
            "--save_sample",
        ]
        if self._disabled:
            argv.append(f"--disabled_toolsets={self._disabled}")

        try:
            out, err, rc, dur = run_argv(argv, timeout)
            # Pull everything the agent wrote in /work back into the host workdir so outcome
            # scoring sees it (harmless re-copy of the seed files; adds result.txt, sample, …).
            run_argv(["docker", "cp", f"{cname}:/work/.", workdir], 60)
        finally:
            run_argv(["docker", "rm", "-f", cname], 30)

        samples = [p for p in glob.glob(os.path.join(workdir, "sample_*.json"))
                   if p not in before]
        raw_sample = ""
        if samples:
            newest = max(samples, key=os.path.getmtime)
            try:
                with open(newest, encoding="utf-8", errors="replace") as f:
                    raw_sample = f.read()
            except OSError:
                raw_sample = ""
            # remove the transcript artifact so it never pollutes file scoring
            for p in samples:
                try:
                    os.remove(p)
                except OSError:
                    pass

        if raw_sample:
            parsed = parse_output(raw_sample)
            raw = raw_sample
        else:
            if rc != 0:
                raise AdapterError(f"hermes exited {rc} with no sample file; "
                                   f"stderr(tail): {err[-1600:]}")
            parsed = {"answer": "", "steps": []}
            raw = out
        return {"answer": parsed["answer"], "steps": parsed["steps"],
                "raw": raw, "duration_s": dur}

    def cleanup_run(self) -> None:
        if self._run_dir:
            shutil.rmtree(self._run_dir, ignore_errors=True)
        self._run_dir = self._alias = None
