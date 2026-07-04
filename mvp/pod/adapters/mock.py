"""`MockAdapter` — a harness-free adapter that returns a CORRECT transcript for any task.

It reads the task's deterministic `success` spec and emits the minimal step sequence + final
answer that satisfies it, so the whole pod pipeline (run -> score -> disclose -> submit) can be
exercised end-to-end with no GPU host and no real harness installed. It is also the oracle the
verification test scores against (every task must come out `task_success=True`).

This is NOT a model: it does not reason. It mechanically constructs a passing transcript from the
spec the same way `aeon.agentic._check_success` reads it:

  required_tools  -> emit one step per required tool, in order
  final_tool      -> ensure that tool is the LAST step (append/move it)
  args_contain    -> put those exact arg substrings on the final tool's step
  final_answer_contains -> put that substring in the answer

Because it targets the optimal step count, it also scores well on efficiency (it's a clean run).
"""
from __future__ import annotations

import json
import time

from .base import Adapter


class MockAdapter(Adapter):
    name = "mock"

    def version(self, pin: str | None = None) -> str | None:
        # The mock isn't a real harness; report a stable synthetic version so disclosure has
        # something deterministic (and never accidentally claims a real harness build).
        return pin or "mock-0"

    def disclose(self, pin: str | None = None) -> dict:
        return {"harness": "mock", "harness_name": "Mock Adapter (no harness)",
                "harness_repo": None, "harness_version": self.version(pin)}

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 300) -> dict:
        # ---- v2 (environment-execution) contract: simulate a PERFECT agent -------------
        # If a workdir is given and the task is an aeon-agentic-v2 case, apply the task's
        # scripted perfect execution (write the expected files, return the expected answer).
        if workdir is not None and ("_expected" in task or "setup_files" in task):
            from aeon import agentic_v2                      # mvp/ is on sys.path for pod code
            t0 = time.monotonic()
            answer = agentic_v2.apply_perfect(task, workdir)
            steps = [{"tool": "write", "args": {"path": rel}}
                     for rel in ((task.get("_expected") or {}).get("files") or {})]
            return {"answer": answer, "steps": steps,
                    "raw": json.dumps({"mock": True, "case": task.get("id"),
                                       "writes": [s["args"]["path"] for s in steps]}),
                    "duration_s": time.monotonic() - t0}

        # ---- v1 (synthetic-tools) contract ----------------------------------------------
        spec = task.get("success", {}) or {}
        tools = task.get("tools", []) or []
        param_map = {t["name"]: list(t.get("params") or []) for t in tools}
        first_tool = tools[0]["name"] if tools else None

        required = list(spec.get("required_tools") or [])
        final_tool = spec.get("final_tool")
        args_contain = dict(spec.get("args_contain") or {})

        # 1) Build the ordered list of tool calls.
        order: list[str] = list(required)
        if final_tool:
            # The final tool must be the LAST call. Remove an earlier occurrence so we don't
            # add an extra (efficiency) step, then append it at the end.
            if final_tool in order:
                order.remove(final_tool)
            order.append(final_tool)
        if not order:
            # No tool constraints (e.g. answer-only success) — call the first available tool so
            # the transcript still demonstrates tool use, if any tool exists.
            if first_tool:
                order = [first_tool]

        steps = []
        for tool in order:
            args = {p: self._stub_value(p) for p in param_map.get(tool, [])}
            steps.append({"tool": tool, "args": args})

        # 2) Apply args_contain to the FINAL matching tool step (that's what scoring checks).
        if args_contain and steps:
            target_name = final_tool or steps[-1]["tool"]
            tgt = next((s for s in reversed(steps) if s["tool"] == target_name), steps[-1])
            for k, v in args_contain.items():
                tgt["args"][k] = v

        # 3) Final answer: include the required substring (and echo any args_contain values so
        #    answer-only specs that reference them still pass).
        answer = spec.get("final_answer_contains") or "Task completed."
        return {"steps": steps, "answer": str(answer)}

    @staticmethod
    def _stub_value(param: str) -> str:
        """A plausible non-empty placeholder for a param with no spec'd value (keeps
        arg_validity at 1.0 — scoring only checks the param key is present and non-missing)."""
        return f"<{param}>"
