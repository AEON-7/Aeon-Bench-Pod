"""Agentic tool-calling suite + a DETERMINISTIC metric (no judge — the correct tool calls and
final state are known). A harness drives the model through a task and records a transcript;
this scores the transcript against the task's known-correct outcome.

Transcript : {"steps": [{"tool": <name>, "args": {...}}, ...], "answer": <final text>}
Task       : {"prompt", "tools": [{"name", "params": [...]}], "success": {...}, "optimal_steps"}

Sub-metrics (each 0..1, deterministic):
  task_success  — the goal was accomplished (the `success` spec: required tools in order, a
                  final tool + arg substrings, and/or a final-answer substring)
  tool_accuracy — fraction of calls to a REAL available tool (hallucinated tools are penalised)
  arg_validity  — fraction of valid calls that supplied the tool's required params
  efficiency    — optimal_steps / actual_steps (1.0 = optimal), capped at 1
  no_forbidden  — avoided every forbidden tool

COMPOSITE is GATED on task_success: 0 if the task wasn't accomplished, else a weighted blend
of the rest. (A model that flails through the right answer still scores below a clean run.)
"""
from __future__ import annotations

SUITE_ID = "aeon-agentic-v1"
WEIGHTS = {"tool_accuracy": 0.4, "arg_validity": 0.2, "efficiency": 0.3, "no_forbidden": 0.1}


def _norm(x):
    return str(x).replace(" ", "").lower()


def _check_success(spec, steps, answer):
    answer = answer or ""
    called = [s.get("tool") for s in steps]
    if "final_answer_contains" in spec:
        if _norm(spec["final_answer_contains"]) not in _norm(answer):
            return False
    if "final_tool" in spec:
        last = next((s for s in reversed(steps) if s.get("tool") == spec["final_tool"]), None)
        if not last:
            return False
        for k, v in (spec.get("args_contain") or {}).items():
            if _norm(v) not in _norm(last.get("args", {}).get(k, "")):
                return False
    if "required_tools" in spec:                 # all present, in order
        i = 0
        for rt in spec["required_tools"]:
            while i < len(called) and called[i] != rt:
                i += 1
            if i >= len(called):
                return False
            i += 1
    return True


def score_agentic(task, transcript):
    transcript = transcript or {}
    steps = transcript.get("steps") or []
    answer = transcript.get("answer", "")
    tools = {t["name"]: t for t in task.get("tools", [])}
    spec = task.get("success", {})
    forbidden = set(spec.get("forbidden_tools") or [])

    success = _check_success(spec, steps, answer)
    n = len(steps)
    valid = sum(1 for s in steps if s.get("tool") in tools)
    tool_acc = valid / n if n else (1.0 if success else 0.0)
    arg_ok = sum(1 for s in steps if s.get("tool") in tools
                 and all(p in (s.get("args") or {}) for p in (tools[s["tool"]].get("params") or [])))
    arg_val = arg_ok / valid if valid else (1.0 if success else 0.0)
    opt = task.get("optimal_steps")
    eff = min(1.0, opt / n) if (opt and n) else (1.0 if success else 0.0)
    no_forbidden = 0.0 if any(s.get("tool") in forbidden for s in steps) else 1.0

    parts = {"tool_accuracy": tool_acc, "arg_validity": arg_val, "efficiency": eff, "no_forbidden": no_forbidden}
    composite = round(sum(WEIGHTS[k] * parts[k] for k in WEIGHTS), 3) if success else 0.0
    return composite, {"tier": 1, "agentic": True, "task_success": success, "n_steps": n,
                       "optimal_steps": opt, **{k: round(v, 3) for k, v in parts.items()}}


# ---- starter agentic task set (the full diverse corpus is a generation pass, like the suite) ----
AGENTIC_CASES = [
    {"id": "agentic.calc.0001", "category": "Agentic", "tier": 1, "optimal_steps": 1,
     "prompt": "Use the calculator tool to compute 1234 * 5678, then state the result.",
     "tools": [{"name": "calculator", "params": ["expression"]}],
     "success": {"final_tool": "calculator", "args_contain": {"expression": "1234*5678"},
                 "final_answer_contains": "7006652"}},
    {"id": "agentic.flight.0002", "category": "Agentic", "tier": 1, "optimal_steps": 2,
     "prompt": "Find and book a flight from NYC to LAX on June 5th.",
     "tools": [{"name": "search_flights", "params": ["origin", "dest", "date"]},
               {"name": "book_flight", "params": ["flight_id"]}],
     "success": {"required_tools": ["search_flights", "book_flight"], "final_tool": "book_flight"}},
    {"id": "agentic.weather.0003", "category": "Agentic", "tier": 1, "optimal_steps": 1,
     "prompt": "What's the weather in Paris? Use the weather tool — do not guess.",
     "tools": [{"name": "get_weather", "params": ["city"]}],
     "success": {"final_tool": "get_weather", "args_contain": {"city": "Paris"}}},
    {"id": "agentic.file.0004", "category": "Agentic", "tier": 1, "optimal_steps": 2,
     "prompt": "Read config.json, then write the value of 'port' into result.txt.",
     "tools": [{"name": "read_file", "params": ["path"]},
               {"name": "write_file", "params": ["path", "content"]}],
     "success": {"required_tools": ["read_file", "write_file"], "final_tool": "write_file",
                 "args_contain": {"path": "result.txt"}}},
    {"id": "agentic.search.0005", "category": "Agentic", "tier": 1, "optimal_steps": 1,
     "prompt": "Search for the capital of Australia and report it. Use web_search; don't answer from memory.",
     "tools": [{"name": "web_search", "params": ["query"]}],
     "success": {"final_tool": "web_search", "final_answer_contains": "Canberra"}},
]


def summary():
    return {"suite_id": SUITE_ID, "n_cases": len(AGENTIC_CASES), "categories": ["Agentic"],
            "metric_weights": WEIGHTS,
            "cases": [{"id": c["id"], "prompt": c["prompt"], "n_tools": len(c["tools"]),
                       "optimal_steps": c.get("optimal_steps")} for c in AGENTIC_CASES]}
