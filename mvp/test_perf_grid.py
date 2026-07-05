"""Self-test for pod/perf_grid.py + targets.py input-token capture (TASK D).

Runs GREEN locally with zero GPU: fake in-process targets, canned SSE streams,
sleep-based harness runners.

    python test_perf_grid.py
"""
from __future__ import annotations

import json
import os
import sys
import time

_MVP = os.path.dirname(os.path.abspath(__file__))
for p in (_MVP, os.path.join(_MVP, "pod")):
    if p not in sys.path:
        sys.path.insert(0, p)

import perf_grid  # noqa: E402
from perf_grid import (PROMPTS, CATEGORIES, SUITE_ID, _agg,  # noqa: E402
                       run_direct_grid, run_harness_timing, to_results)
from aeon.targets import OpenAITarget, TargetError  # noqa: E402


# ------------------------------------------------------------- fake targets

class FakeTarget:
    """Deterministic per-prompt latencies (thread-order independent)."""

    def __init__(self, base_url, model, api_key=None):
        self.base_url, self.model, self.api_key = base_url, model, api_key

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        prompt = messages[0]["content"]
        ttft = 50.0 + (len(prompt) % 100)
        return {"text": "ok", "ttft_ms": ttft, "decode_tps": 40.0,
                "e2e_ms": ttft + 100.0, "output_tokens": 64,
                "input_tokens": len(prompt) // 4, "input_tokens_estimated": False,
                "streamed": True}


class FlakyTarget(FakeTarget):
    """Raises TargetError on the PONG instruction prompt."""

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        if "PONG" in messages[0]["content"]:
            raise TargetError("boom: simulated endpoint failure")
        return super().chat(messages, temperature=temperature, max_tokens=max_tokens)


# ------------------------------------------------------------------- tests

def test_prompt_sets():
    assert CATEGORIES == ["Math", "Reasoning", "Coding", "Prose", "Instruction"]
    for cat in CATEGORIES:
        ps = PROMPTS[cat]
        assert len(ps) == 4, cat
        longest = max(len(p) for p in ps)
        # one long prompt per category, ~1500 tokens (chars//4 heuristic)
        assert 1000 <= longest // 4 <= 2600, (cat, longest // 4)
        assert min(len(p) for p in ps) < 300, cat  # mixed lengths
    # deterministic across builds
    assert PROMPTS["Math"][3] == perf_grid._long_math()


def test_agg_hand_check():
    reqs = [
        {"category": "Math", "ttft_ms": 100.0, "decode_tps": 10.0, "prefill_tps": 1000.0,
         "e2e_ms": 500.0, "output_tokens": 50, "input_tokens": 100},
        {"category": "Math", "ttft_ms": 200.0, "decode_tps": 20.0, "prefill_tps": 2000.0,
         "e2e_ms": 600.0, "output_tokens": 50, "input_tokens": 100},
        {"category": "Math", "ttft_ms": 300.0, "decode_tps": 30.0, "prefill_tps": 3000.0,
         "e2e_ms": 700.0, "output_tokens": 50, "input_tokens": 100},
        {"category": "Math", "ttft_ms": 400.0, "decode_tps": 40.0, "prefill_tps": 4000.0,
         "e2e_ms": 800.0, "output_tokens": 50, "input_tokens": 100},
    ]
    a = _agg(reqs, wall_clock_s=10.0, n_errors=1)
    assert a["n"] == 4 and a["n_errors"] == 1
    assert a["ttft_ms_mean"] == 250.0
    assert a["ttft_ms_p50"] == 250.0                    # 200 + (300-200)*0.5
    assert a["ttft_ms_p95"] == 385.0                    # 300 + (400-300)*0.85
    assert a["decode_tps_mean"] == 25.0
    assert a["prefill_tps_mean"] == 2500.0
    assert a["e2e_ms_mean"] == 650.0
    assert a["output_tokens_total"] == 200
    assert a["input_tokens_total"] == 400
    assert a["agg_decode_tps"] == 20.0                  # 200 tok / 10 s


def test_direct_grid_shape_and_math():
    calls = []
    grid = run_direct_grid("http://fake:8000/v1", "fake-model",
                           target_factory=FakeTarget,
                           progress_cb=lambda c, done, total: calls.append((c, done, total)))
    assert grid["kind"] == "direct" and grid["suite_id"] == SUITE_ID
    assert grid["isolation"] == "per_category"
    assert sorted(grid["levels"]) == [1, 4, 8, 16, 32]          # 5 levels
    # per-category ISOLATION: each cell runs max(4, conc) tasks of ITS category only
    for conc, lv in grid["levels"].items():
        n_cell = max(4, conc)
        assert sorted(lv["categories"]) == sorted(CATEGORIES)   # 5 cats
        assert lv["overall"]["n"] == 5 * n_cell and lv["errors"] == []
        assert lv["wall_clock_s"] > 0
        for cat in CATEGORIES:
            assert lv["categories"][cat]["n"] == n_cell
            assert lv["categories"][cat]["cell_wall_s"] >= 0   # fake target can round to 0.000
    assert len(calls) == sum(5 * max(4, c) for c in (1, 4, 8, 16, 32))
    # hand-verifiable cell: Math @ c=1 against the deterministic fake (prompts are BUSTED)
    busted = [perf_grid._bust(p, i) for i, p in enumerate(PROMPTS["Math"])]
    cell = grid["levels"][1]["categories"]["Math"]
    exp_ttfts = [50.0 + (len(p) % 100) for p in busted]
    assert cell["ttft_ms_mean"] == round(sum(exp_ttfts) / 4, 2)
    assert cell["decode_tps_mean"] == 40.0
    assert cell["output_tokens_total"] == 4 * 64
    exp_prefill = [round((len(p) // 4) / ((50.0 + (len(p) % 100)) / 1000.0), 2)
                   for p in busted]
    assert cell["prefill_tps_mean"] == round(sum(exp_prefill) / 4, 2)
    assert cell["input_tokens_total"] == sum(len(p) // 4 for p in busted)
    # aggregate decode tok/s uses the level wall clock (grid stores wall rounded
    # to 3 decimals; the fake finishes in ~ms, so compare with tolerance)
    wall = grid["levels"][1]["wall_clock_s"]      # = sum of the 5 sequential cell walls
    agg = grid["levels"][1]["overall"]["agg_decode_tps"]
    expect = 20 * 64 / wall
    assert agg > 0 and abs(agg - expect) / expect < 0.5, (agg, expect)
    return grid


def test_direct_grid_error_continue():
    grid = run_direct_grid("http://fake:8000/v1", "fake-model",
                           conc_levels=(1, 4), target_factory=FlakyTarget)
    for conc in (1, 4):
        lv = grid["levels"][conc]
        assert len(lv["errors"]) == 1
        assert "boom" in lv["errors"][0]["error"]
        assert lv["overall"]["n"] == 19 and lv["overall"]["n_errors"] == 1
        assert lv["categories"]["Instruction"]["n"] == 3
        assert lv["categories"]["Instruction"]["n_errors"] == 1
        assert lv["categories"]["Math"]["n"] == 4          # other cells unaffected


def test_to_results_direct(grid):
    rows = to_results(grid)
    assert len(rows) == 5 * (5 + 1)                        # 5 levels x (5 cats + overall)
    ids = {r["case_id"] for r in rows}
    assert "perf.direct.math.c1" in ids and "perf.direct.overall.c32" in ids
    assert "perf.direct.instruction.c16" in ids
    for r in rows:
        assert set(r) == {"case_id", "category", "tier", "status", "score",
                          "raw_output", "evidence", "speed"}
        assert r["category"] == "Performance" and r["tier"] == 0
        assert r["status"] == "perf" and r["score"] is None and r["raw_output"] == ""
        assert "agg_decode_tps" in r["evidence"] and "wall_clock_s" in r["evidence"]
        assert "tpot_ms_mean" in r["evidence"]             # TPOT captured per cell
        assert "ttft_ms" in r["speed"] and "decode_tps" in r["speed"] and "tpot_ms" in r["speed"]
        json.dumps(r)                                      # JSON-serializable


def test_harness_timing():
    timing = run_harness_timing("opencode", "http://fake:8000/v1", "fake-model",
                                conc_levels=(1, 2), n_tasks=4, timeout=30,
                                runner=lambda prompt: time.sleep(0.05))
    n_cats = len(perf_grid.CATEGORIES)
    for conc in (1, 2):
        lv = timing["levels"][conc]
        # n_tasks floors at max(requested, conc, n_categories) so every prompt TYPE
        # is timed at every level (per-category harness perf cells)
        assert lv["failures"] == 0 and lv["n_tasks"] == max(4, conc, n_cats)
        assert 0.04 <= lv["mean_task_s"] <= 0.5
        assert lv["p95_task_s"] >= lv["mean_task_s"] * 0.9
        assert lv["tasks_per_min"] > 0
        if conc == 1:
            # clean sequential pass: every category gets a timed cell
            assert set(lv["categories"]) == set(perf_grid.CATEGORIES)
            for cell in lv["categories"].values():
                assert cell["n"] >= 1 and cell["mean_task_s"] > 0
        else:
            # mixed pool at conc>1: per-category cells would be contaminated — none emitted
            assert lv["categories"] == {}
    # c=2 should finish the sleeps roughly twice as fast as c=1
    assert timing["levels"][2]["wall_clock_s"] < timing["levels"][1]["wall_clock_s"]
    assert timing["levels"][2]["tasks_per_min"] > timing["levels"][1]["tasks_per_min"]

    def flaky(prompt):
        if prompt == perf_grid.HARNESS_PROMPTS[0]:
            raise RuntimeError("harness task blew up")
        time.sleep(0.01)

    t2 = run_harness_timing("hermes", "http://fake:8000/v1", "fake-model",
                            conc_levels=(1,), n_tasks=4, runner=flaky)
    assert t2["levels"][1]["failures"] == 1

    rows = to_results(timing)
    ids = [r["case_id"] for r in rows]
    # overall row per level + per-category rows from the clean c1 pass only
    assert "perf.harness.opencode.c1" in ids and "perf.harness.opencode.c2" in ids
    assert "perf.harness.opencode.math.c1" in ids
    assert "perf.harness.opencode.instruction.c2" not in ids
    assert len(ids) == 2 + n_cats
    assert all(r["status"] == "perf" and r["category"] == "Performance" for r in rows)
    overall = next(r for r in rows if r["case_id"] == "perf.harness.opencode.c1")
    assert "tasks_per_min" in overall["evidence"]
    catrow = next(r for r in rows if r["case_id"] == "perf.harness.opencode.math.c1")
    assert catrow["evidence"]["scope"] == "Math" and catrow["evidence"]["harness"] == "opencode"

    try:
        run_harness_timing("x", "u", "a")                  # no runner -> ValueError
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# --------------------------------------- targets.py input-token capture

class FakeStreamResp:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.lines)


def _sse(obj):
    return ("data: " + json.dumps(obj) + "\n").encode()


def test_targets_stream_usage_capture():
    t = OpenAITarget("http://fake:8000/v1", "m")
    lines = [
        _sse({"choices": [{"delta": {"content": "Hel"}}]}),
        _sse({"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}),
        _sse({"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 2}}),
        b"data: [DONE]\n",
    ]
    t._post = lambda payload, stream: FakeStreamResp(lines)
    res = t.chat([{"role": "user", "content": "hi there"}], temperature=0.0, max_tokens=16)
    assert res["text"] == "Hello" and res["streamed"] is True
    assert res["input_tokens"] == 123 and res["input_tokens_estimated"] is False
    assert res["output_tokens"] == 2 and res["finish_reason"] == "stop"
    # backward-compat: all pre-existing keys still present
    for k in ("text", "ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
              "finish_reason", "truncated", "streamed"):
        assert k in res, k
    assert res["ttft_ms"] is not None


def test_targets_stream_usage_fallback_estimate():
    t = OpenAITarget("http://fake:8000/v1", "m")
    lines = [
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    t._post = lambda payload, stream: FakeStreamResp(lines)
    prompt = "x" * 400
    res = t.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=16)
    assert res["input_tokens"] == 100                     # len//4 estimate
    assert res["input_tokens_estimated"] is True


class FakeOnceResp:
    def __init__(self, obj):
        self.body = json.dumps(obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_targets_nonstream_usage_capture():
    t = OpenAITarget("http://fake:8000/v1", "m")
    obj = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 77, "completion_tokens": 1}}
    t._post = lambda payload, stream: FakeOnceResp(obj)
    res = t._chat_once([{"role": "user", "content": "q"}], 0.0, 16)
    assert res["input_tokens"] == 77 and res["input_tokens_estimated"] is False
    assert res["ttft_ms"] is None and res["streamed"] is False


def main():
    grid = None
    steps = [
        ("prompt_sets", test_prompt_sets),
        ("agg_hand_check", test_agg_hand_check),
    ]
    for name, fn in steps:
        fn()
        print(f"PASS {name}")
    grid = test_direct_grid_shape_and_math()
    print("PASS direct_grid_shape_and_math")
    test_direct_grid_error_continue()
    print("PASS direct_grid_error_continue")
    test_to_results_direct(grid)
    print("PASS to_results_direct")
    test_harness_timing()
    print("PASS harness_timing")
    test_targets_stream_usage_capture()
    print("PASS targets_stream_usage_capture")
    test_targets_stream_usage_fallback_estimate()
    print("PASS targets_stream_usage_fallback_estimate")
    test_targets_nonstream_usage_capture()
    print("PASS targets_nonstream_usage_capture")
    print("ALL GREEN (9/9)")


if __name__ == "__main__":
    main()
