"""Local self-test for the real harness adapters + the aeon-agentic-v2 suite.

Runs GREEN with NO GPU, NO docker, NO network:
  (a) each adapter's pure `parse_output` against CANNED sample outputs (all 3 real formats,
      incl. tool-call cases);
  (b) agentic_v2: scripted perfect execution scores 1.0 for EVERY task; sabotaged < 1.0;
      partial credit is a fraction; evidence rows enumerate each criterion;
  (c) run_agentic_v2 end-to-end with the MockAdapter (simulates a perfect agent writing
      files into the task workdirs) — every task scored 1.0, correct row shape.

Run:  python "C:/Users/Albert/AEON Bench/mvp/test_adapters_local.py"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import agentic_v2                                    # noqa: E402
from pod.adapters import hermes, mock, openclaw, opencode      # noqa: E402
from pod import adapters, run_harness2                         # noqa: E402

FAILED = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:
        FAILED.append(name)
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=4)


# ==============================================================================================
# (a) parsers vs canned harness outputs
# ==============================================================================================

_OPENCODE_NDJSON = "\n".join([
    json.dumps({"type": "step_start", "part": {"id": "s1"}}),
    # tool event, opencode part shape (state.input), streamed twice (pending -> completed)
    json.dumps({"type": "tool", "part": {"id": "call_1", "tool": "write",
                                         "state": {"status": "pending", "input": {}}}}),
    json.dumps({"type": "tool", "part": {"id": "call_1", "tool": "write",
                                         "state": {"status": "completed",
                                                   "input": {"filePath": "result.txt",
                                                             "content": "400"}}}}),
    # tool event, alternate shape (name + input at part level)
    json.dumps({"type": "tool_use", "part": {"name": "bash",
                                             "input": {"command": "cat result.txt"}}}),
    'this line is not JSON and must be skipped',
    # streamed text snapshots: second extends first -> must collapse to the longest
    json.dumps({"type": "text", "part": {"text": "The result is"}}),
    json.dumps({"type": "text", "part": {"text": "The result is 400."}}),
    json.dumps({"type": "step_finish", "part": {"reason": "stop",
                                                "tokens": {"output": 12}}}),
])


def test_opencode_parser():
    out = opencode.parse_output(_OPENCODE_NDJSON)
    assert out["answer"] == "The result is 400.", out
    assert len(out["steps"]) == 2, out["steps"]
    assert out["steps"][0] == {"tool": "write",
                               "args": {"filePath": "result.txt", "content": "400"}}, out
    assert out["steps"][1] == {"tool": "bash", "args": {"command": "cat result.txt"}}, out
    # degenerate inputs never crash
    assert opencode.parse_output("") == {"answer": "", "steps": []}
    assert opencode.parse_output("garbage\n{bad json") == {"answer": "", "steps": []}


_HERMES_SAMPLE = json.dumps({"conversations": [
    {"from": "system", "value": "You are Hermes."},
    {"from": "human", "value": "Compute 17*23+9 and write it to result.txt."},
    {"from": "gpt", "value": "I'll compute and write the file.\n<tool_call>\n"
                             '{"name": "terminal", "arguments": {"command": '
                             '"echo 400 > result.txt"}}\n</tool_call>'},
    {"from": "tool", "value": "exit 0"},
    {"from": "gpt", "value": "<tool_call>\n{\"name\": \"terminal\", \"arguments\": "
                             "{\"command\": \"cat result.txt\"}}\n</tool_call>"},
    {"from": "tool", "value": "400"},
    {"from": "gpt", "value": "Done — the result is 400 and it is saved in result.txt."},
]})


def test_hermes_parser():
    out = hermes.parse_output(_HERMES_SAMPLE)
    assert out["answer"] == "Done — the result is 400 and it is saved in result.txt.", out
    assert len(out["steps"]) == 2, out["steps"]
    assert out["steps"][0] == {"tool": "terminal",
                               "args": {"command": "echo 400 > result.txt"}}, out
    assert out["steps"][1]["args"]["command"] == "cat result.txt"
    # all-tool-call conversation -> falls back to last gpt turn stripped of tool_call blocks
    only_tools = json.dumps({"conversations": [
        {"from": "gpt", "value": "Working on it.\n<tool_call>\n"
                                 '{"name": "x", "arguments": {}}\n</tool_call>'}]})
    out2 = hermes.parse_output(only_tools)
    assert out2["answer"] == "Working on it." and out2["steps"] == [{"tool": "x", "args": {}}]
    assert hermes.parse_output("not json") == {"answer": "", "steps": []}
    # reasoning models leak <think> into the Hermes transcript (empty gemma block observed on
    # v3: '<think>\n</think>\nThe journey takes 105 minutes.') — it must NOT reach the answer
    leak = json.dumps({"conversations": [
        {"from": "gpt", "value": "<think>\n</think>\nThe journey takes 105 minutes."}]})
    assert hermes.parse_output(leak)["answer"] == "The journey takes 105 minutes."
    nonempty = json.dumps({"conversations": [
        {"from": "gpt", "value": "<think>\nlet me reason step by step...\n</think>\n\nFinal: 42"}]})
    assert hermes.parse_output(nonempty)["answer"] == "Final: 42"


def test_strip_reasoning():
    from pod.adapters.base import strip_reasoning
    assert strip_reasoning("<think>\n</think>\nHello") == "Hello"
    assert strip_reasoning("a <think>x</think> b") == "a  b"           # mid-string block
    assert strip_reasoning("<THINK>caps</THINK>\nyes") == "yes"        # case-insensitive
    assert strip_reasoning("<think>trace cut off with no close") == "" # leading dangling trace
    assert strip_reasoning("plain answer") == "plain answer"           # no-op
    assert strip_reasoning("") == "" and strip_reasoning(None) is None


_OPENCLAW_STDOUT = json.dumps({
    "status": "ok",
    "result": {"payloads": [{"type": "text", "text": "The answer is 400."},
                            {"type": "text", "text": "result.txt has been written."}]},
})


def test_openclaw_parser():
    out = openclaw.parse_output(_OPENCLAW_STDOUT)
    assert out["answer"] == "The answer is 400.\nresult.txt has been written.", out
    assert out["steps"] == []                       # no tool trace on stdout, by design
    # JSON on one line of noisy stdout
    noisy = "booting...\n" + _OPENCLAW_STDOUT + "\n"
    # (whole-blob parse fails -> line scan finds it)
    out2 = openclaw.parse_output("log line\n" + json.dumps(
        {"result": {"payloads": [{"text": "hi"}]}}))
    assert out2["answer"] == "hi", out2
    assert openclaw.parse_output(noisy)["answer"].startswith("The answer is 400.")
    assert openclaw.parse_output("") == {"answer": "", "steps": []}
    assert openclaw.parse_output("{\"result\": {}}") == {"answer": "", "steps": []}


# ==============================================================================================
# (b) agentic_v2 scoring
# ==============================================================================================

def test_v2_perfect_and_sabotage():
    agentic_v2.self_check()                         # perfect==1.0 AND sabotage<1.0, every task
    assert len(agentic_v2.CASES) >= 10
    assert agentic_v2.SUITE_ID == "aeon-agentic-v2.1"
    ids = agentic_v2.CASE_IDS
    assert len(set(ids)) == len(ids)


def test_v2_partial_credit_and_evidence():
    task = next(c for c in agentic_v2.CASES if c["id"] == "av2-01-compute-write")
    with tempfile.TemporaryDirectory() as wd:
        agentic_v2.populate_workdir(task, wd)
        # only the answer criterion met, file missing -> partial in (0, 1)
        score, ev = agentic_v2.score_agentic_v2(task, wd, "the value is 400")
        assert 0 < score < 1, (score, ev)
        assert len(ev) == 2 and {e["ok"] for e in ev} == {True, False}, ev
        assert all({"criterion", "ok", "detail"} <= set(e) for e in ev)
    # wrong file CONTENT also fails cleanly
    with tempfile.TemporaryDirectory() as wd:
        with open(os.path.join(wd, "result.txt"), "w") as f:
            f.write("399")
        score, ev = agentic_v2.score_agentic_v2(task, wd, "no idea")
        assert score == 0.0, (score, ev)


def test_v2_equals_normalisation():
    task = next(c for c in agentic_v2.CASES if c["id"] == "av2-04-log-count")
    with tempfile.TemporaryDirectory() as wd:
        with open(os.path.join(wd, "count.txt"), "w", newline="") as f:
            f.write("7\r\n")                        # CRLF + trailing newline still equals "7"
        score, ev = agentic_v2.score_agentic_v2(task, wd, "count is 7")
        assert score == 1.0, ev
    with tempfile.TemporaryDirectory() as wd:
        with open(os.path.join(wd, "count.txt"), "w") as f:
            f.write("17")                           # equals (not contains): 17 != 7
        score, ev = agentic_v2.score_agentic_v2(task, wd, "count is 7")
        assert score < 1.0, ev


# ==============================================================================================
# (c) run_agentic_v2 end-to-end with the MockAdapter
# ==============================================================================================

def test_run_agentic_v2_mock():
    seen = []
    rows = run_harness2.run_agentic_v2(
        "mock", "http://127.0.0.1:8000/v1", "test-model",
        concurrency=3, timeout=60,
        progress_cb=lambda cid, score, status: seen.append((cid, score, status)))
    assert len(rows) == len(agentic_v2.CASES)
    assert [r["case_id"] for r in rows] == agentic_v2.CASE_IDS      # CASES order preserved
    for r in rows:
        assert r["status"] == "scored", r
        assert r["score"] == 1.0, (r["case_id"], r["evidence"])
        assert r["category"] == "Agentic" and r["tier"] == 0
        assert r["suite_id"] == "aeon-agentic-v2.1"
        assert r["harness"] == "mock" and r["harness_version"] == "mock-0"
        assert isinstance(r["speed"]["e2e_s"], float)
        assert isinstance(r["evidence"], list) and r["evidence"]
        t = json.loads(r["raw_output"])                              # valid transcript JSON
        assert "answer" in t and "steps" in t
    assert len(seen) == len(rows)


def test_run_agentic_v2_harness_error_isolated():
    """A broken adapter task never aborts the batch -> harness_error rows, score 0."""
    class Boom(mock.MockAdapter):
        def run_task(self, task, *a, **k):
            if task["id"].endswith("01-compute-write"):
                raise RuntimeError("container exploded")
            return super().run_task(task, *a, **k)
    adapters.ADAPTERS["_boom"] = Boom
    try:
        rows = run_harness2.run_agentic_v2("_boom", "http://x/v1", "m", concurrency=2)
    finally:
        del adapters.ADAPTERS["_boom"]
        run_harness2._discover_cache.pop("_boom", None)
    bad = [r for r in rows if r["status"] == "harness_error"]
    good = [r for r in rows if r["status"] == "scored"]
    assert len(bad) == 1 and bad[0]["score"] == 0.0 and bad[0]["case_id"] == "av2-01-compute-write"
    assert len(good) == len(rows) - 1 and all(r["score"] == 1.0 for r in good)


def test_registry_and_argv_shape():
    """Registry intact; v2 adapters expose the contract + exact docker argv building blocks."""
    for hid in ("hermes", "openclaw", "opencode", "mock"):
        a = adapters.get(hid)
        assert hasattr(a, "prepare_run") and hasattr(a, "cleanup_run") and hasattr(a, "run_task")
    # prepare_run creates fresh per-model config/state and cleanup_run removes it
    with tempfile.TemporaryDirectory() as root:
        oc = adapters.get("opencode")
        d = oc.prepare_run("http://127.0.0.1:8000/v1", "aeon-x", root)
        cfg = json.load(open(os.path.join(d, "opencode.json"), encoding="utf-8"))
        assert cfg["model"] == "dgx/aeon-x"
        assert cfg["provider"]["dgx"]["options"]["baseURL"] == "http://127.0.0.1:8000/v1"
        assert cfg["provider"]["dgx"]["models"]["aeon-x"]["tool_call"] is True
        oc.cleanup_run()
        assert not os.path.isdir(d)

        cl = adapters.get("openclaw")
        d2 = cl.prepare_run("http://127.0.0.1:8000/v1", "aeon-x", root)
        ccfg = json.load(open(os.path.join(d2, "openclaw.json"), encoding="utf-8"))
        assert ccfg["models"]["providers"]["dgx"]["api"] == "openai-completions"
        assert ccfg["agents"]["defaults"]["model"]["primary"] == "dgx/aeon-x"
        cl.cleanup_run()
        assert not os.path.isdir(d2)


def main():
    print("== (a) pure parsers vs canned harness outputs ==")
    check("opencode.parse_output (NDJSON, tools+streamed text)", test_opencode_parser)
    check("hermes.parse_output (ShareGPT, <tool_call> blocks)", test_hermes_parser)
    check("openclaw.parse_output (result.payloads[].text)", test_openclaw_parser)
    check("strip_reasoning drops leaked <think> from harness answers", test_strip_reasoning)

    print("== (b) aeon-agentic-v2 scoring ==")
    check("perfect execution == 1.0 / sabotage < 1.0 (all tasks)", test_v2_perfect_and_sabotage)
    check("partial credit + evidence rows", test_v2_partial_credit_and_evidence)
    check("equals normalisation (CRLF, strict value)", test_v2_equals_normalisation)

    print("== (c) run_agentic_v2 end-to-end (MockAdapter) ==")
    check("mock perfect agent -> all rows scored 1.0", test_run_agentic_v2_mock)
    check("per-task harness_error isolated", test_run_agentic_v2_harness_error_isolated)
    check("registry + fresh config prepare/cleanup", test_registry_and_argv_shape)

    if FAILED:
        print(f"\nRESULT: FAIL ({len(FAILED)} failing: {FAILED})")
        raise SystemExit(1)
    print("\nRESULT: PASS (all local adapter/suite tests green)")


if __name__ == "__main__":
    main()
