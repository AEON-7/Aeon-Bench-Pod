"""`Adapter` — the common interface every per-harness adapter implements.

An adapter is the (small) amount of harness-specific code that turns an AEON agentic task into
a scored transcript. The contract is one method:

    run_task(task, model_base_url, served_alias, *, timeout=300) -> {"steps": [...], "answer": ...}

where `task` is an entry of `aeon.agentic.AGENTIC_CASES`:

    {"prompt": str,
     "tools": [{"name": str, "params": [str, ...]}, ...],
     "success": {...},               # the deterministic success spec (scored, not given to the model)
     "optimal_steps": int}

and the returned transcript is exactly what `aeon.agentic.score_agentic(task, transcript)` expects:

    {"steps": [{"tool": <name>, "args": {<param>: <value>, ...}}, ...],
     "answer": <final assistant text>}

Each concrete adapter MUST:
  1. configure the harness to use the OpenAI-compatible endpoint `model_base_url` and the served
     model name `served_alias` (this is the model under test — the pod serves it),
  2. present `task["tools"]` to the harness (use `_tools_to_openai` to get the standard
     function-calling schema, or translate further to the harness's own tool format),
  3. run `task["prompt"]` to completion within `timeout` seconds,
  4. parse the harness's tool-call log / output into the `{steps, answer}` transcript.

NOTE: the tools are *presented* to the model but, for the deterministic agentic suite, they do not
need to be backed by real implementations — scoring only inspects which tools were called, with
what args, and the final answer. A harness that requires executable tools should register no-op /
stub handlers (each adapter notes how). The success spec is never shown to the model.
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from .. import harnesses


class AdapterError(RuntimeError):
    """Raised when an adapter cannot run a task (harness missing, bad output, timeout, ...)."""


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(s: str) -> str:
    """Filesystem/volume-safe token for aliases like 'dgx/aeon-ultimate:latest'."""
    return _SAFE_RE.sub("_", str(s)).strip("_") or "model"


def run_argv(argv: list[str], timeout: float, cwd: str | None = None):
    """Run one subprocess (typically `docker run --rm ...`) and capture everything.

    Returns `(stdout, stderr, returncode, duration_s)`. Raises AdapterError on launch
    failure or timeout — the caller (run_harness2) turns that into a per-task
    `harness_error` without aborting the batch.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except FileNotFoundError as e:
        raise AdapterError(f"cannot launch {argv[0]!r}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError(f"{argv[0]} timed out after {timeout}s "
                           f"(cmd: {' '.join(argv[:8])}...)") from e
    return proc.stdout or "", proc.stderr or "", proc.returncode, time.monotonic() - t0


class Adapter:
    #: harness id — must match a key in `pod.harnesses.HARNESSES`. Overridden by subclasses.
    name: str = ""

    # ---- version / disclosure -------------------------------------------------------------

    def version(self, pin: str | None = None) -> str | None:
        """The exact harness build in use, for disclosure. Delegates to
        `pod.harnesses.resolve_version` (explicit pin wins, else queries the installed CLI,
        else None = unknown)."""
        return harnesses.resolve_version(self.name, pin)

    def disclose(self, pin: str | None = None) -> dict:
        """{harness, harness_name, harness_repo, harness_version} record for the report."""
        return harnesses.disclose(self.name, pin)

    # ---- tool schema helpers --------------------------------------------------------------

    @staticmethod
    def _tools_to_openai(tools: list[dict] | None) -> list[dict]:
        """Convert the AEON tool spec -> OpenAI function-calling `tools` array.

        AEON tool:  {"name": "calculator", "params": ["expression"]}
        OpenAI tool:{"type": "function",
                     "function": {"name": "calculator", "description": ...,
                                  "parameters": {"type": "object",
                                                 "properties": {"expression": {"type": "string"}},
                                                 "required": ["expression"]}}}

        All params are typed as free-form strings (the AEON spec carries names only) and are all
        marked required — the suite's `arg_validity` sub-metric rewards supplying every declared
        param. This schema is what Hermes / OpenClaw / OpenCode are handed (each may translate it
        further to its own tool format)."""
        out = []
        for t in tools or []:
            params = list(t.get("params") or [])
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or f"AEON agentic tool '{t['name']}'.",
                    "parameters": {
                        "type": "object",
                        "properties": {p: {"type": "string"} for p in params},
                        "required": params,
                    },
                },
            })
        return out

    @staticmethod
    def _tool_names(tools: list[dict] | None) -> list[str]:
        return [t["name"] for t in (tools or [])]

    # ---- the run contract -----------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """v2 contract: create a FRESH config/state dir for this model-run under `run_root`
        (no state reuse between models). Default: no-op (adapter needs no prepared state)."""
        return None

    def cleanup_run(self) -> None:
        """v2 contract: tear down whatever `prepare_run` created. Default: no-op."""
        return None

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 300) -> dict:
        """Drive the harness through one task.

        v2 (environment-execution) contract — `workdir` given: launch ONE one-shot
        `docker run --rm --network host` with `workdir` mounted at /work (and -w /work) so
        the agent's file operations land in `workdir` for scoring; return
        `{"answer": str, "steps": [{"tool", "args"}, ...], "raw": str, "duration_s": float}`.

        v1 (synthetic-tools) legacy contract — `workdir` None: return
        `{"steps": [...], "answer": ...}`.

        Subclasses MUST override."""
        raise NotImplementedError(f"{type(self).__name__}.run_task is not implemented")

    # ---- shared parser surface ------------------------------------------------------------

    @staticmethod
    def _empty_transcript(answer: str = "") -> dict:
        return {"steps": [], "answer": answer}

    @staticmethod
    def _normalize_transcript(obj: Any) -> dict:
        """Coerce a loosely-shaped harness result into the strict transcript shape.

        Accepts any object that already looks like `{"steps": [...], "answer": ...}` and
        normalises each step to `{"tool": str, "args": dict}` — tolerating common aliases
        (`tool`/`name`/`function`, `args`/`arguments`/`parameters`/`input`). Unknown shapes
        degrade to an empty step list rather than crashing the run."""
        if not isinstance(obj, dict):
            return Adapter._empty_transcript()
        raw_steps = obj.get("steps") or obj.get("tool_calls") or []
        steps = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            tool = s.get("tool") or s.get("name") or s.get("function")
            if isinstance(tool, dict):                     # {"function": {"name": ...}}
                tool = tool.get("name")
            args = (s.get("args") or s.get("arguments")
                    or s.get("parameters") or s.get("input") or {})
            if isinstance(args, str):                      # JSON-encoded arguments string
                import json
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": args}
            if tool:
                steps.append({"tool": tool, "args": args})
        answer = obj.get("answer")
        if answer is None:
            answer = obj.get("output") or obj.get("final") or obj.get("content") or ""
        return {"steps": steps, "answer": answer if isinstance(answer, str) else str(answer)}
