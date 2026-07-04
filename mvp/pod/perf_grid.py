"""perf_grid — performance grid benchmark (TASK D).

DIRECT-to-model latency/throughput at a concurrency ladder (1,4,8,16,32) over
fixed per-category prompt sets, capturing per-stream decode tok/s, TTFT ms and
prefill throughput (prompt_tokens / ttft_sec), plus an AGGREGATE decode tok/s
per level (sum output_tokens / level wall clock). Also a lighter through-harness
timing mode driven by a caller-supplied runner callable (no adapter imports).

Public API:
    run_direct_grid(target_url, alias, *, api_key=None, conc_levels=(1,4,8,16,32),
                    max_tokens=256, temperature=0.0, repeats=1, progress_cb=None,
                    target_factory=None) -> grid dict
    run_harness_timing(harness_id, model_base_url, alias, *, conc_levels=(1,4),
                       n_tasks=4, timeout=240, runner=None) -> timing dict
    to_results(grid) -> submission-ready result rows (SUITE_ID = aeon-perf-v1)
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

_MVP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../mvp
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon.targets import OpenAITarget, TargetError  # noqa: E402

SUITE_ID = "aeon-perf-v1"


# ---------------------------------------------------------------- prompt sets
# Deterministic long prompts (~1500 tokens each) so prefill throughput is a
# meaningful measurement, built from pure f-strings over fixed ranges.

def _long_math():
    lines = [
        f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}  txn-{i:04d}  vendor-{i % 17:02d}"
        f"  amount: {(i * 37) % 995 + 5}.{i % 100:02d} USD  memo: recurring service charge unit {i % 9}"
        for i in range(1, 61)
    ]
    return ("Below is a transaction ledger.\n" + "\n".join(lines) +
            "\nHow many transactions are listed above? Reply with the number only.")


def _long_reasoning():
    lines = [
        f"Fact {i}: person P{i % 23} was observed in room R{(i * 7) % 12} at hour {(i * 3) % 24}"
        f" holding badge B{i % 7} and wearing tag T{(i * 5) % 31}."
        for i in range(1, 66)
    ]
    return ("Consider the following facts.\n" + "\n".join(lines) +
            "\nBased only on the facts above, name one room id that person P3 appears in."
            " Reply with the room id only.")


def _long_coding():
    chunks = [
        f"def util_{i:03d}(x):\n"
        f"    \"\"\"helper {i}: scales the input by {i % 13} then offsets by {i % 7}.\"\"\"\n"
        f"    return x * {i % 13} + {i % 7}\n"
        for i in range(1, 56)
    ]
    return ("Here is a Python module.\n```python\n" + "\n".join(chunks) +
            "```\nHow many function definitions appear in the module above?"
            " Reply with the number only.")


def _long_prose():
    lines = [
        f"In the {i}th hour the harbor town kept its slow watch, and lamplighter {i % 9}"
        f" counted {(i * 3) % 40 + 1} boats returning under a copper sky while bell {i % 5} tolled."
        for i in range(1, 46)
    ]
    return ("Read the passage below.\n" + " ".join(lines) +
            "\nSummarize the passage above in exactly one sentence.")


def _long_instruction():
    lines = [
        f"Rule {i}: when the input index equals {i}, respond in lowercase, keep the reply under"
        f" {(i % 9) + 3} words, and never mention the number {(i * 11) % 97}."
        for i in range(1, 56)
    ]
    return ("Here is a rulebook.\n" + "\n".join(lines) +
            "\nFollowing only Rule 7, write the single word ok.")


PROMPTS = {
    "Math": [
        "Compute 847 * 63. Reply with the number only.",
        "What is 15% of 2400? Reply with the number only.",
        "Solve for x: 3x + 11 = 47. Reply with the number only.",
        _long_math(),
    ],
    "Reasoning": [
        "If all bloops are razzies and all razzies are lazzies, are all bloops lazzies? Answer yes or no.",
        "A farmer has 17 sheep; all but 9 run away. How many are left? Reply with the number only.",
        "Which is heavier: a kilogram of steel or a kilogram of feathers? Answer in one word.",
        _long_reasoning(),
    ],
    "Coding": [
        "Write a Python function that reverses a string.",
        "Write a one-line Python list comprehension that squares the numbers 1 through 10.",
        "What does this print? print(sum(range(5))) Reply with the number only.",
        _long_coding(),
    ],
    "Prose": [
        "Write a haiku about mountains.",
        "Write one sentence describing rain on a tin roof.",
        "Give a two-sentence opening for a mystery novel set in a lighthouse.",
        _long_prose(),
    ],
    "Instruction": [
        "Reply with exactly the word PONG.",
        "List three primary colors, one per line, with no other text.",
        "Write the word echo exactly five times, separated by commas.",
        _long_instruction(),
    ],
}

CATEGORIES = list(PROMPTS.keys())

# short prompt per category, used by the harness timing mode
HARNESS_PROMPTS = [PROMPTS[c][0] for c in CATEGORIES]


# ---------------------------------------------------------------- aggregation

def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def _pct(xs, p):
    """Percentile with linear interpolation (hand-checkable, no numpy)."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (p / 100.0) * (len(s) - 1)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _r(x, nd=2):
    return round(x, nd) if isinstance(x, (int, float)) else None


def _agg(reqs, wall_clock_s, n_errors=0):
    ttfts = [r["ttft_ms"] for r in reqs if r.get("ttft_ms") is not None]
    dtps = [r["decode_tps"] for r in reqs if r.get("decode_tps") is not None]
    ptps = [r["prefill_tps"] for r in reqs if r.get("prefill_tps") is not None]
    e2es = [r["e2e_ms"] for r in reqs if r.get("e2e_ms") is not None]
    out_sum = sum(r.get("output_tokens") or 0 for r in reqs)
    in_sum = sum(r.get("input_tokens") or 0 for r in reqs)
    return {
        "n": len(reqs),
        "n_errors": n_errors,
        "ttft_ms_mean": _r(_mean(ttfts)),
        "ttft_ms_p50": _r(_pct(ttfts, 50)),
        "ttft_ms_p95": _r(_pct(ttfts, 95)),
        "decode_tps_mean": _r(_mean(dtps)),          # mean per-stream decode tok/s
        "prefill_tps_mean": _r(_mean(ptps)),         # mean prompt_tokens/ttft_sec
        "e2e_ms_mean": _r(_mean(e2es)),
        "output_tokens_total": out_sum,
        "input_tokens_total": in_sum,
        # AGGREGATE decode tok/s: total generated tokens over the level's wall clock
        "agg_decode_tps": _r(out_sum / wall_clock_s) if wall_clock_s and wall_clock_s > 0 else None,
        "input_tokens_estimated": any(r.get("input_tokens_estimated") for r in reqs),
    }


# ---------------------------------------------------------------- direct grid

def _one_request(target, category, prompt, temperature, max_tokens):
    resp = target.chat([{"role": "user", "content": prompt}],
                       temperature=temperature, max_tokens=max_tokens)
    ttft = resp.get("ttft_ms")
    in_tok = resp.get("input_tokens")
    in_est = bool(resp.get("input_tokens_estimated"))
    if in_tok is None:                                   # target without usage capture
        in_tok, in_est = max(1, len(prompt) // 4), True
    prefill = _r(in_tok / (ttft / 1000.0)) if (ttft and ttft > 0) else None
    return {
        "category": category,
        "ttft_ms": ttft,
        "decode_tps": resp.get("decode_tps"),
        "prefill_tps": prefill,
        "e2e_ms": resp.get("e2e_ms"),
        "output_tokens": resp.get("output_tokens") or 0,
        "input_tokens": in_tok,
        "input_tokens_estimated": in_est,
    }


def run_direct_grid(target_url, alias, *, api_key=None, conc_levels=(1, 4, 8, 16, 32),
                    max_tokens=256, temperature=0.0, repeats=1, progress_cb=None,
                    target_factory=None):
    """Direct-to-model perf grid. Returns
    {kind:'direct', suite_id, alias, target_url, conc_levels, levels:{c:{...}}}
    with per-level 'overall' + 'categories' aggregates, raw 'requests' and 'errors'.
    A TargetError (or any per-request exception) is recorded and the run continues.
    """
    factory = target_factory or OpenAITarget
    target = factory(target_url, alias, api_key=api_key)
    tasks = [(cat, p) for _ in range(max(1, int(repeats)))
             for cat in CATEGORIES for p in PROMPTS[cat]]
    grid = {
        "kind": "direct", "suite_id": SUITE_ID, "alias": alias, "target_url": target_url,
        "conc_levels": list(conc_levels), "max_tokens": max_tokens,
        "temperature": temperature, "repeats": repeats, "levels": {},
    }
    for conc in conc_levels:
        reqs, errors, done = [], [], 0
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=int(conc)) as ex:
            futs = [(cat, p, ex.submit(_one_request, target, cat, p, temperature, max_tokens))
                    for cat, p in tasks]
            for cat, p, fut in futs:
                try:
                    reqs.append(fut.result())
                except TargetError as e:
                    errors.append({"category": cat, "error": str(e)[:300], "prompt_head": p[:80]})
                except Exception as e:
                    errors.append({"category": cat,
                                   "error": f"{type(e).__name__}: {e}"[:300],
                                   "prompt_head": p[:80]})
                done += 1
                if progress_cb:
                    progress_cb(conc, done, len(tasks))
        wall = time.perf_counter() - t0
        cats = {}
        for cat in CATEGORIES:
            creqs = [r for r in reqs if r["category"] == cat]
            cerrs = len([e for e in errors if e["category"] == cat])
            cats[cat] = _agg(creqs, wall, n_errors=cerrs)
        grid["levels"][int(conc)] = {
            "conc": int(conc),
            "wall_clock_s": round(wall, 3),
            "overall": _agg(reqs, wall, n_errors=len(errors)),
            "categories": cats,
            "requests": reqs,
            "errors": errors,
        }
    return grid


# ------------------------------------------------------------- harness timing

def _timed_call(runner, prompt):
    t0 = time.perf_counter()
    runner(prompt)
    return time.perf_counter() - t0


def run_harness_timing(harness_id, model_base_url, alias, *, conc_levels=(1, 4),
                       n_tasks=4, timeout=240, runner=None):
    """Light through-harness timing. `runner(prompt) -> None` is caller-supplied
    (the integrator wraps the real adapter; tests pass a sleeper) so this module
    never imports harness adapters. Returns
    {kind:'harness', harness_id, alias, model_base_url, levels:{c:{...}}}.
    """
    if runner is None:
        raise ValueError("run_harness_timing requires a runner(prompt)->None callable")
    out = {"kind": "harness", "suite_id": SUITE_ID, "harness_id": harness_id,
           "alias": alias, "model_base_url": model_base_url,
           "conc_levels": list(conc_levels), "n_tasks": n_tasks, "levels": {}}
    for conc in conc_levels:
        prompts = [HARNESS_PROMPTS[i % len(HARNESS_PROMPTS)] for i in range(int(n_tasks))]
        durations, failures = [], 0
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=int(conc)) as ex:
            futs = [ex.submit(_timed_call, runner, p) for p in prompts]
            for fut in futs:
                try:
                    durations.append(fut.result(timeout=timeout))
                except Exception:
                    failures += 1
        wall = time.perf_counter() - t0
        out["levels"][int(conc)] = {
            "conc": int(conc),
            "n_tasks": int(n_tasks),
            "mean_task_s": _r(_mean(durations), 4),
            "p95_task_s": _r(_pct(durations, 95), 4),
            "tasks_per_min": _r(len(durations) / (wall / 60.0)) if wall > 0 else None,
            "failures": failures,
            "wall_clock_s": round(wall, 3),
        }
    return out


# ----------------------------------------------------------------- to_results

def _row(case_id, evidence, speed):
    return {"case_id": case_id, "category": "Performance", "tier": 0, "status": "perf",
            "score": None, "raw_output": "", "evidence": evidence, "speed": speed}


def _cell_speed(cell):
    return {"ttft_ms": cell.get("ttft_ms_mean"), "decode_tps": cell.get("decode_tps_mean"),
            "e2e_ms": cell.get("e2e_ms_mean"), "output_tokens": cell.get("output_tokens_total"),
            "streamed": True}


def to_results(grid):
    """Flatten a run_direct_grid / run_harness_timing dict into submission-ready
    result rows for suite aeon-perf-v1."""
    rows = []
    if grid.get("kind") == "harness":
        hid = grid.get("harness_id", "unknown")
        for conc, lv in grid["levels"].items():
            rows.append(_row(f"perf.harness.{hid}.c{conc}", dict(lv), speed={}))
        return rows
    for conc, lv in grid["levels"].items():
        for cat in CATEGORIES:
            cell = lv["categories"].get(cat)
            if cell is None:
                continue
            ev = dict(cell)
            ev.update({"conc": conc, "wall_clock_s": lv["wall_clock_s"], "scope": cat})
            rows.append(_row(f"perf.direct.{cat.lower()}.c{conc}", ev, _cell_speed(cell)))
        ev = dict(lv["overall"])
        ev.update({"conc": conc, "wall_clock_s": lv["wall_clock_s"], "scope": "overall"})
        rows.append(_row(f"perf.direct.overall.c{conc}", ev, _cell_speed(lv["overall"])))
    return rows
