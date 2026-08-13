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
import uuid
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


_ENGINE_METRICS_OK: dict = {}     # target_url -> False once we learn it exposes no /metrics


def _engine_tokens(target_url):
    """Total tokens the ENGINE reports having generated so far, or None if it does not say.

    Derived from the serve URL (…/v1 -> …/metrics) so it follows whatever the run is actually
    benchmarking, local or remote, rather than assuming a fixed port. vLLM and SGLang both expose
    a Prometheus `generation_tokens_total`; anything else returns None and the caller falls back
    to client-side counting."""
    import urllib.request
    # One probe decides it. Without this the grid pays the full timeout on EVERY cell of every
    # concurrency level against an engine that will never answer — minutes of dead wall clock
    # added to the very measurement we are trying to keep honest.
    if _ENGINE_METRICS_OK.get(target_url) is False:
        return None
    base = (target_url or "").rstrip("/")
    for suffix in ("/v1", "/v1/"):
        if base.endswith(suffix.rstrip("/")):
            base = base[: -len(suffix.rstrip("/"))]
            break
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/metrics", timeout=2) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        _ENGINE_METRICS_OK[target_url] = False
        return None
    for metric in ("vllm:generation_tokens_total", "sglang:generation_tokens_total",
                   "generation_tokens_total"):
        total, found = 0.0, False
        for ln in text.splitlines():
            if ln.startswith(metric) and not ln.startswith("#"):
                try:
                    total += float(ln.rsplit(None, 1)[1]); found = True
                except (ValueError, IndexError):
                    pass
        if found:
            _ENGINE_METRICS_OK[target_url] = True
            return total
    _ENGINE_METRICS_OK[target_url] = False    # reachable, but no token counter we recognise
    return None


def _agg(reqs, wall_clock_s, n_errors=0, engine_tokens=None):
    ttfts = [r["ttft_ms"] for r in reqs if r.get("ttft_ms") is not None]
    dtps = [r["decode_tps"] for r in reqs if r.get("decode_tps") is not None]
    ptps = [r["prefill_tps"] for r in reqs if r.get("prefill_tps") is not None]
    e2es = [r["e2e_ms"] for r in reqs if r.get("e2e_ms") is not None]
    tpots = [r["tpot_ms"] for r in reqs if r.get("tpot_ms") is not None]
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
        # TPOT: mean inter-token latency during decode (ms/token) — the steady-state
        # "feel" of a stream once it starts; complements TTFT (how long until it starts)
        "tpot_ms_mean": _r(_mean(tpots), 3),
        "tpot_ms_p50": _r(_pct(tpots, 50), 3),
        "tpot_ms_p95": _r(_pct(tpots, 95), 3),
        "output_tokens_total": out_sum,
        "input_tokens_total": in_sum,
        # AGGREGATE decode tok/s: total generated tokens over the level's wall clock. Under
        # concurrency this IS the headline throughput — every in-flight stream's tokens over the
        # elapsed time of the batch. Prefer the engine's own counter delta: it tallies every token
        # it generated (reasoning included) across all streams, so it needs no estimation and
        # cannot be skewed by how the HTTP stream was framed.
        "agg_decode_tps": _r((engine_tokens if engine_tokens is not None else out_sum)
                             / wall_clock_s) if wall_clock_s and wall_clock_s > 0 else None,
        "tokens_source": "engine" if engine_tokens is not None else "client",
        "engine_tokens_total": _r(engine_tokens) if engine_tokens is not None else None,
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
    # TPOT (time per output token): decode-phase inter-token latency. Prefer the direct
    # measurement (e2e minus ttft over the decode tokens); fall back to 1000/decode_tps.
    out_tok = resp.get("output_tokens") or 0
    e2e = resp.get("e2e_ms")
    # TPOT is the reciprocal of the OBSERVED decode rate, so it inherits that measurement rather
    # than being recomputed from e2e-minus-ttft. The old fallback had the same defect decode_tps
    # did: on a response that was not really streamed, e2e-minus-ttft is a few milliseconds spread
    # over hundreds of tokens, which reads as a 0.005 ms per-token latency. If no decode phase was
    # observed there is no per-token latency to report, and None says exactly that.
    tpot = _r(1000.0 / resp["decode_tps"], 3) if resp.get("decode_tps") else None
    return {
        "category": category,
        "ttft_ms": ttft,
        "decode_tps": resp.get("decode_tps"),
        "prefill_tps": prefill,
        "e2e_ms": resp.get("e2e_ms"),
        "tpot_ms": tpot,
        "output_tokens": resp.get("output_tokens") or 0,
        "input_tokens": in_tok,
        "input_tokens_estimated": in_est,
    }


# Per-PROCESS nonce. Two benchmark runs against the same serve would otherwise replay identical
# prompts and the second would inherit the first's prefix cache — unmeasurable, and there is no
# reset endpoint on this engine build to clear it.
_RUN_NONCE = uuid.uuid4().hex[:6]


def _bust(prompt, i, salt=""):
    """Unique per-replica tag so every stream pays REAL prefill.

    `i` alone is not enough. It restarts at 0 for each concurrency level and each run, so
    "[measurement 0000] X" at c1 is byte-identical to the same string at c4 — c1 primes vLLM's
    prefix cache and every higher level then measures free prefill on its overlapping indices.
    Since levels tile 0..n-1, the overlap is systematic and grows with the ladder: the curve
    reports prefill getting CHEAPER as concurrency rises, which is an artifact, not a result.

    `salt` carries the level and a per-process nonce, so no two measured requests in this run —
    or in any other run against the same serve — share a prefix. That matters because this engine
    build exposes no cache-reset endpoint, so there is nothing to clear between runs."""
    return f"[measurement {salt}{i:04d}] {prompt}"


def run_direct_grid(target_url, alias, *, api_key=None, conc_levels=(1, 4, 8, 16, 32),
                    max_tokens=256, temperature=0.0, repeats=1, progress_cb=None,
                    target_factory=None):
    """Direct-to-model perf grid, ONE CATEGORY AT A TIME per level: 'Math @ c4' means
    exactly 4 concurrent streams of Math prompts and nothing else in flight — never a
    mixed-category soup (which would contaminate per-category numbers and dilute a
    category's aggregate tok/s by the other categories' wall time). Each cell's
    aggregates use ITS OWN wall clock; a level's 'overall' spans its category cells
    (sequential, so overall wall = sum of cell walls). Prompts are tiled to >= conc
    tasks per cell with cache-busting tags so the level is actually saturated.
    Returns {kind:'direct', ..., levels:{c:{overall, categories, requests, errors}}}.
    A TargetError (or any per-request exception) is recorded and the run continues."""
    factory = target_factory or OpenAITarget
    target = factory(target_url, alias, api_key=api_key)
    grid = {
        "kind": "direct", "suite_id": SUITE_ID, "alias": alias, "target_url": target_url,
        "conc_levels": list(conc_levels), "max_tokens": max_tokens,
        "temperature": temperature, "repeats": repeats,
        "isolation": "per_category",             # methodology marker (vs the old mixed pool)
        "levels": {},
    }
    for conc in conc_levels:
        cats, all_reqs, all_errs = {}, [], []
        base_counts = {c: max(1, int(repeats)) * len(PROMPTS[c]) for c in CATEGORIES}
        total = sum(max(base_counts[c], int(conc)) for c in CATEGORIES)
        done, wall_sum = 0, 0.0
        cell_engine_tokens = []
        for cat in CATEGORIES:
            base = [p for _ in range(max(1, int(repeats))) for p in PROMPTS[cat]]
            n = max(len(base), int(conc))        # saturate the level with THIS category only
            # salt = per-run nonce + level + category, so a prompt is never repeated verbatim
            # anywhere: not across replicas, not across levels, not across runs on this serve.
            _salt = f"{_RUN_NONCE}-c{int(conc)}-{cat[:3]}-"
            tasks = [_bust(base[i % len(base)], i, _salt) for i in range(n)]
            reqs, errors = [], []
            eng0 = _engine_tokens(target_url)    # engine tally BEFORE this cell
            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=int(conc)) as ex:
                futs = [(p, ex.submit(_one_request, target, cat, p, temperature, max_tokens))
                        for p in tasks]
                for p, fut in futs:
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
                        progress_cb(conc, done, total)
            cw = time.perf_counter() - t0
            eng1 = _engine_tokens(target_url)    # …and AFTER: the delta is what this cell generated
            wall_sum += cw
            eng_delta = (eng1 - eng0) if (eng0 is not None and eng1 is not None
                                          and eng1 >= eng0) else None
            cell = _agg(reqs, cw, n_errors=len(errors),   # aggregates over the CELL's wall
                        engine_tokens=eng_delta)
            cell["cell_wall_s"] = round(cw, 3)
            cell_engine_tokens.append(eng_delta)
            cats[cat] = cell
            all_reqs.extend(reqs)
            all_errs.extend(errors)
        grid["levels"][int(conc)] = {
            "conc": int(conc),
            "wall_clock_s": round(wall_sum, 3),
            "overall": _agg(all_reqs, wall_sum, n_errors=len(all_errs),
                            engine_tokens=(sum(cell_engine_tokens)
                                           if cell_engine_tokens
                                           and all(t is not None for t in cell_engine_tokens)
                                           else None)),
            "categories": cats,
            "requests": all_reqs,
            "errors": all_errs,
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
        # Saturate each level AND cover every category: at least n_tasks, at least conc
        # tasks, cycling categories so per-category timing exists at every level.
        n = max(int(n_tasks), int(conc), len(CATEGORIES))
        pool = [(CATEGORIES[i % len(CATEGORIES)], HARNESS_PROMPTS[i % len(HARNESS_PROMPTS)])
                for i in range(n)]
        results, failures = [], 0                        # (category, seconds)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=int(conc)) as ex:
            futs = [(cat, ex.submit(_timed_call, runner, p)) for cat, p in pool]
            for cat, fut in futs:
                try:
                    results.append((cat, fut.result(timeout=timeout)))
                except Exception:
                    failures += 1
        wall = time.perf_counter() - t0
        durations = [s for _, s in results]
        # Per-category task timing ONLY at c1 (strictly sequential = a clean measurement of
        # each prompt type). At higher concs the pool mixes categories — the OVERALL
        # tasks/min stays meaningful (realistic mixed load), but attributing per-category
        # numbers from a mixed pool would be contaminated, so none are emitted there.
        cats = {}
        if int(conc) == 1:
            for cat in CATEGORIES:
                cd = [s for c, s in results if c == cat]
                if cd:
                    cats[cat] = {"n": len(cd), "mean_task_s": _r(_mean(cd), 4),
                                 "p95_task_s": _r(_pct(cd, 95), 4)}
        out["levels"][int(conc)] = {
            "conc": int(conc),
            "n_tasks": n,
            "mean_task_s": _r(_mean(durations), 4),
            "p95_task_s": _r(_pct(durations, 95), 4),
            "tasks_per_min": _r(len(durations) / (wall / 60.0)) if wall > 0 else None,
            "failures": failures,
            "wall_clock_s": round(wall, 3),
            "categories": cats,                          # per-prompt-category task timing
        }
    return out


# ----------------------------------------------------------------- to_results

def _row(case_id, evidence, speed):
    return {"case_id": case_id, "category": "Performance", "tier": 0, "status": "perf",
            "score": None, "raw_output": "", "evidence": evidence, "speed": speed}


def _cell_speed(cell):
    return {"ttft_ms": cell.get("ttft_ms_mean"), "decode_tps": cell.get("decode_tps_mean"),
            "tpot_ms": cell.get("tpot_ms_mean"),
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
            # one row per prompt category too, so the board can compare harness speed
            # per prompt TYPE (reasoning vs coding vs prose ...) at each concurrency
            for cat, cell in (lv.get("categories") or {}).items():
                ev = dict(cell)
                ev.update({"conc": conc, "scope": cat, "harness": hid})
                rows.append(_row(f"perf.harness.{hid}.{cat.lower()}.c{conc}", ev, speed={}))
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
