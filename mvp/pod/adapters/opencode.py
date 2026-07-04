"""OpenCodeAdapter — drives the PROVEN `aeon-harness-opencode` container image (sst/opencode
CLI inside node:24-slim + ripgrep; built on the DGX, arm64).

Invocation (one one-shot container per task; the pod code runs ON the DGX):

    docker run --rm --network host -v <workdir>:/work -w /work aeon-harness-opencode \
        run --format json --auto -m dgx/<alias> "<prompt>"

`--auto` auto-approves permissions (required non-interactive); `--network host` lets the
container reach the served model on 127.0.0.1:8000. The workdir must contain `opencode.json`
configuring the custom OpenAI-compatible provider:

    {"$schema": "https://opencode.ai/config.json",
     "provider": {"dgx": {"npm": "@ai-sdk/openai-compatible", "name": "DGX",
                          "options": {"baseURL": <model_base_url>, "apiKey": "sk-local"},
                          "models": {"<alias>": {"name": "<alias>", "tool_call": true}}}},
     "model": "dgx/<alias>"}

Output = NDJSON events on stdout:
    {"type": "text", "part": {"text": ...}}           -> answer text (may stream snapshots)
    {"type": "tool"/"tool_use"/..., "part": {...}}    -> tool call (part carries tool+input)
    {"type": "step_finish", ...}                      -> step boundary

`parse_output(raw_stdout)` is a pure function (unit-tested against canned samples) and parses
DEFENSIVELY: any event/part carrying a tool/name field is collected as a step; streamed text
snapshots are deduplicated; malformed lines are skipped.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from .base import Adapter, AdapterError, run_argv, safe_name

IMAGE = os.environ.get("AEON_OPENCODE_IMAGE", "aeon-harness-opencode")
_PROVIDER_ID = "dgx"
_API_KEY = "sk-local"
_TOOLISH_TYPES = ("tool", "tool_use", "tool_call", "tool-invocation", "tool.execute")


def build_config(model_base_url: str, served_alias: str) -> dict:
    """The exact opencode.json this model-run uses (verified schema — see module docstring)."""
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            _PROVIDER_ID: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "DGX",
                "options": {"baseURL": model_base_url, "apiKey": _API_KEY},
                "models": {served_alias: {"name": served_alias, "tool_call": True}},
            }
        },
        "model": f"{_PROVIDER_ID}/{served_alias}",
    }


def parse_output(raw_stdout: str) -> dict:
    """Pure parser: OpenCode NDJSON stdout -> {"answer": str, "steps": [{"tool","args"}]}.

    Defensive by design:
      * text: consecutive streamed snapshots (each a prefix/extension of the last) collapse
        to the longest; distinct texts join with newlines;
      * tools: any event whose type mentions 'tool' OR whose part carries a tool/name field
        becomes a step; args come from part.input / part.args / part.arguments /
        part.state.input; repeated state updates for the same part id keep the richest args;
      * non-JSON lines and unknown event shapes are skipped, never fatal.
    """
    texts: list[str] = []
    steps_by_id: dict = {}          # id -> {"tool", "args"}  (preserves insertion order)
    anon = 0

    for line in (raw_stdout or "").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        etype = str(ev.get("type") or "")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}

        # ---- answer text -------------------------------------------------------------
        if etype == "text" or (part.get("type") == "text"):
            t = part.get("text") or ev.get("text")
            if isinstance(t, str) and t:
                if texts and (t.startswith(texts[-1]) or texts[-1].startswith(t)):
                    if len(t) > len(texts[-1]):     # streamed snapshot grew — keep longest
                        texts[-1] = t
                else:
                    texts.append(t)
            continue

        # ---- tool calls --------------------------------------------------------------
        toolish = any(tt in etype for tt in _TOOLISH_TYPES) if etype else False
        src = part or ev
        name = src.get("tool") or src.get("name")
        if isinstance(name, dict):
            name = name.get("name")
        if not name and isinstance(src.get("function"), dict):
            name = src["function"].get("name")
        if not (toolish or name):
            continue
        if not name or not isinstance(name, str):
            continue
        args = src.get("input") or src.get("args") or src.get("arguments")
        if not args and isinstance(src.get("state"), dict):
            args = src["state"].get("input")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {}
        pid = src.get("id") or src.get("callID") or src.get("call_id")
        if pid is None:
            anon += 1
            pid = f"_anon{anon}"
        prev = steps_by_id.get(pid)
        if prev is None or len(json.dumps(args)) >= len(json.dumps(prev["args"])):
            steps_by_id[pid] = {"tool": name, "args": args}

    answer = "\n".join(t.strip() for t in texts if t.strip()).strip()
    return {"answer": answer, "steps": list(steps_by_id.values())}


class OpenCodeAdapter(Adapter):
    name = "opencode"
    IMAGE = IMAGE

    def __init__(self):
        self._run_dir: str | None = None
        self._own_run_dir = False
        self._model: str | None = None
        self._config_path: str | None = None

    # ---- v2 contract ------------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """Fresh per-model-run config dir under `run_root` (never reused across models).
        Container state is inherently fresh: every task is a one-shot `docker run --rm`."""
        d = os.path.join(run_root, f"opencode-{safe_name(served_alias)}")
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        self._run_dir, self._own_run_dir = d, False
        self._model = f"{_PROVIDER_ID}/{served_alias}"
        self._config_path = os.path.join(d, "opencode.json")
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(build_config(model_base_url, served_alias), f, indent=2)
        return d

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 240) -> dict:
        if workdir is None:
            raise AdapterError("OpenCodeAdapter (v2) requires a task workdir")
        if self._config_path is None:
            # auto-prepare into a private temp root so a bare run_task still works
            root = tempfile.mkdtemp(prefix="aeon_opencode_run_")
            self.prepare_run(model_base_url, served_alias, root)
            self._own_run_dir = True

        # the CLI reads opencode.json from its cwd (/work) — drop the model-run config in
        shutil.copyfile(self._config_path, os.path.join(workdir, "opencode.json"))

        argv = [
            "docker", "run", "--rm", "--network", "host",
            "-v", f"{workdir}:/work", "-w", "/work",
            self.IMAGE,
            "run", "--format", "json", "--auto",
            "-m", self._model or f"{_PROVIDER_ID}/{served_alias}",
            task.get("prompt", ""),
        ]
        out, err, rc, dur = run_argv(argv, timeout)
        parsed = parse_output(out)
        if rc != 0 and not parsed["answer"] and not parsed["steps"]:
            raise AdapterError(f"opencode exited {rc} with no parseable output; "
                               f"stderr: {err[:400]}")
        return {"answer": parsed["answer"], "steps": parsed["steps"],
                "raw": out, "duration_s": dur}

    def cleanup_run(self) -> None:
        if self._run_dir:
            root = os.path.dirname(self._run_dir) if self._own_run_dir else None
            shutil.rmtree(self._run_dir, ignore_errors=True)
            if root:
                shutil.rmtree(root, ignore_errors=True)
        self._run_dir = self._config_path = self._model = None
        self._own_run_dir = False
