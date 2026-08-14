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

from .base import (Adapter, AdapterError, ensure_image, run_argv, run_container_io,
                   safe_name, strip_reasoning)

# Published multi-arch by the pod repo's harness-images workflow, so ANY pod can pull it.
# It used to default to the bare local name, which only existed on a rig that had built it
# by hand — every other pod failed `docker create` and scored 0 on every agentic task.
# Now the pod BUILDS it locally on first use (see `ensure_image`): no third-party image is
# redistributed, and the operator's copy comes straight from upstream.
# Override with the env var to use a locally-built image instead.
IMAGE = os.environ.get("AEON_HERMES_IMAGE", "aeon-harness-hermes:latest")
_API_KEY = "sk-local"
_MAX_TURNS = int(os.environ.get("AEON_HERMES_MAX_TURNS", "8"))
# A ceiling on ONE TURN's output, not a budget to maximise.
#
# The custom provider defaults this to 65536, and on 2026-08-14 a god task truncated at exactly
# that: the model emitted 65k tokens in a single reply. At the 20-40 tok/s these serves sustain
# that is 27-55 MINUTES — longer than the whole 1800s task budget, spent on one turn, after which
# the task dies with nothing written. Bounding it costs that one turn and leaves the agent its
# other seven. 16384 tokens is roughly a 60KB file, ample for these artifacts.
#
# Hermes needs no help with the INPUT side: it reads the real window off the endpoint
# (observed "Context limit: 262,144 tokens (compress at 75% = 196,608)") and auto-compacts. Only
# the output ceiling is ours to set.
_MAX_OUTPUT_TOKENS = int(os.environ.get("AEON_HERMES_MAX_TOKENS", "16384"))

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
        """Floor the declared window so Hermes accepts the model, and cap one turn's output.

        `context_length: 65536` is a FLOOR, not a description. Hermes refuses any model reporting
        under 64K (its tool-calling minimum), and this guarantees the gate is cleared even by a
        serve that under-reports. It is not how Hermes actually sizes the run: measured 2026-08-14
        it reads the endpoint itself and logged "Context limit: 262,144 tokens (compress at 75% =
        196,608)" against a 262144 serve — so the input side is auto-detected and auto-compacted,
        and this line only matters when the endpoint says something smaller.

        `max_tokens` is the half we do have to set — see _MAX_OUTPUT_TOKENS. Left at the custom
        provider's 65536 default, one reply can outlast the entire task budget."""
        if self._cfg_path and os.path.isfile(self._cfg_path):
            return self._cfg_path
        d = self._run_dir or tempfile.mkdtemp(prefix="aeon_hermes_cfg_")
        p = os.path.join(d, "hermes-config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write("model:\n  context_length: 65536\n  max_tokens: %d\n" % _MAX_OUTPUT_TOKENS)
        self._cfg_path = p
        return p

    # ---- v2 contract ------------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """Fresh per-model-run scratch dir (Hermes itself keeps no host-side config; every
        task container is `--rm` so agent state can never leak between models)."""
        # build the harness image here if this machine doesn't have it yet - one loud
        # failure with install instructions beats 0 on every task.
        ensure_image(self.IMAGE, "hermes")
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
        # lost. File I/O is docker-cp BOTH ways (run_container_io): bind mounts break when the
        # pod itself is containerized — the daemon resolves pod-local paths on the HOST and
        # silently mounts an EMPTY dir (this dropped every task's seed files).
        args = [
            f"--query={task.get('prompt', '')}",
            f"--base_url={model_base_url}",
            f"--api_key={_API_KEY}",
            f"--model={served_alias}",
            f"--max_turns={_MAX_TURNS}",
            "--save_sample",
        ]
        if self._disabled:
            args.append(f"--disabled_toolsets={self._disabled}")
        from .. import harness_stream          # local: keeps pod.adapters free of a package cycle
        _obs = harness_stream.observer("hermes", task.get("id"))
        try:
            out, err, rc, dur = run_container_io(
                self.IMAGE, args,
                seed=[(workdir, "/work")],
                seed_optional=[(self._ensure_cfg(), "/root/.hermes/config.yaml")],
                collect=[("/work/.", workdir)],
                timeout=timeout, name_hint=f"hermes_{served_alias}",
                env={"TERMINAL_CWD": "/work"}, workdir="/work", on_line=_obs)
        finally:
            # Also closes on the timeout path, so a task that dies at the budget ends its tile
            # instead of leaving it live forever.
            _obs.close()

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
