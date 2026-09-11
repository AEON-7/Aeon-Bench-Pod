# Why ~180 vs ~66–74 — Spark MIXED perf gap

**When:** 2026-09-11 ~12:45 ET  
**Host:** spark-ecdd via Aorus WSL `86645c3e-b359-484f-b7e3-9e8a7fce0d04`  
**Sources:** live `aeon-pod` `/app/mvp/pod/perf_grid.py`, `/app/mvp/aeon/targets.py`, `/app/mvp/aeon/scoring.py`; `/tmp/live_peak16_grid_levels.json`; `spark-perf-seqs32-n5.md`; AGENTS DFlash2 table.

---

## Bottom line (ÆON lock)

**`agg` MUST mean decode×concurrency = true concurrent throughput.**  
Today's board field `agg_decode_tps` / `peak_agg_tps` does **not** — it is `engine_tokens / wall_clock_s` (TTFT + wave ramp diluted). That is why peak16 c8 shows **66.7** while `decode_tps_mean × 8 ≈ 188` (the remembered ~180).

| Number | What it actually is |
|--------|---------------------|
| **~180 / ~187 / ~188** | Per-stream decode rate × concurrency (or a short Prometheus `generation_tokens` rate while decode is saturated). **ÆON's intended agg.** |
| **66.7 / 73.6** | Current `agg_decode_tps` at peak16 c8 / c10 = Σ tokens ÷ cell wall (includes TTFT, incomplete overlap). |
| **125.69** | Board `peak_agg_tps` for MIXED run `a2618e4eeb` (max of old-style `agg_decode_tps` cells @c32 prose). |
| **144** | AGENTS published DFlash2 **n=7 @c8** aggregate (older recipe / shorter-ctx matrix) — not the peak16 run. |

---

## Exact aeon-pod formulas (live, confirmed)

### 1. Per-request `decode_tps` — `aeon/targets.py::_decode_rate`

```text
span = t_last_token_chunk - t_first_token_chunk   # NOT e2e; excludes TTFT + teardown
decoded = max(1, output_tokens - 1)              # first chunk attributed to TTFT
decode_tps = decoded / span                      # requires ≥2 streamed chunks
```

TTFT is time to first token-bearing chunk (content **or** reasoning). Decode window is first→last token chunk only.

### 2. Cell `decode_tps_mean` — `pod/perf_grid.py::_agg`

```text
decode_tps_mean = mean(per-request decode_tps)   # per-stream, decode-phase only
```

### 3. Cell `agg_decode_tps` — **CURRENT (wrong vs ÆON lock)**

```text
agg_decode_tps = (Δ vllm:generation_tokens_total  OR  Σ output_tokens)
                 / cell_wall_clock_s
```

`cell_wall_clock_s` = ThreadPoolExecutor wall for that category cell (TTFT + decode + drain).  
Comment in code *claims* this is "headline throughput under concurrency" — but it is **tokens / wall**, not **decode×conc**.

### 4. Level `overall` in the grid

Categories run **one at a time** (isolated). Stored overall uses `wall_sum = Σ category walls` and sums tokens → time-weighted.  
**Board display** then **throws that away** and recomputes overall as **arithmetic mean of category cells** (`scoring.perf_board`).

### 5. `peak_agg_tps` — `scoring.perf_board`

```text
peak_agg_tps = max(cell.agg_decode_tps)
               over all category × concurrency cells
               (never the overall/mean row)
peak_agg_cell = {category, conc} of that max
```

Perf dial = percentile of `peak_agg_tps` within hw bucket — **same field**, just wrong quantity today.

### Peak16 c8 arithmetic (banked `/tmp/live_peak16_grid_levels.json`)

| Metric | c8 | c10 |
|--------|-----|-----|
| decode_tps_mean | 23.55 | 22.58 |
| **decode × conc (ÆON agg)** | **188.4** | **225.8** |
| reported agg_decode_tps | **66.66** | **73.6** |
| ttft_ms_mean | 3447 | 3613 |
| DSD K | 7 | 6 |

`23.55 × 8 = 188.4` ≈ remembered ~180. Gap is definitional, not a GPU regression.

### ~187 prometheus sample (`spark-perf-seqs32-n5.md`)

Mid-c32 ~10s window: `generation_tokens 24900→26771` ⇒ **~187 tok/s** while batch draining. Instantaneous engine rate during decode — same *family* as decode×conc, **not** board `peak_agg`.

---

## Recipe diffs (secondary; not the 180↔74 cause)

| | Remembered / AGENTS n=7 | peak16 DSD |
|--|-------------------------|------------|
| n | fixed **7** | DSD: c8→**7**, c10→**6**, falls with batch |
| seqs | 16 or 32 | **16** (no c32) |
| max-model-len | often shorter in AGENTS matrix | **262144** (huge TTFT) |

DSD / seqs=16 can change absolute decode_tps, but **cannot** turn 188 into 67 under a correct decode×conc definition.

---

## Stuck run (incomplete numbers)

At probe ~12:41 ET: GPU **0%**, `num_requests_running=0`, peak16 launcher PID alive **~1h42m**, log stuck mid-c2 after n=11 bounce; banked JSON has c1–c14 from earlier arm. **Do not treat current ladder as finished.** Serve itself answers `/v1/models` (healthy enough for a smoke; no bounce required for docs).

---

## Fix plan (ÆON Bench) — refined

**Goal:** `agg_decode_tps` / `peak_agg_tps` = **concurrent-total tok/s** over the decode
overlap window. `decode_tps_mean` stays per-stream. Perf dial remains percentile of
`peak_agg_tps` (same field, correct quantity).

### Exact new formulas

```text
# Per-stream (unchanged)
decode_tps = (output_tokens - 1) / (t_last_token - t_first_token)

# Concurrent-window AGG (NEW headline)
concurrent_window = max(decode_t1) - min(decode_t0)   # across requests in the cell
agg_decode_tps = (engine_token_delta or Σ output_tokens) / concurrent_window
agg_source = "concurrent_window"

# Fallbacks
# 1) no timestamps: saturated_est = decode_tps_mean × conc   (simultaneous total estimate)
# 2) else: tokens_per_wall_s = tokens / full_cell_wall       (legacy; debug only)
```

### Why decode×conc ≈ 188 matched memory but is not the definition

With uniform per-stream rates and a fully overlapped cohort,
`Σ tokens / concurrent_window ≈ mean(decode_tps) × conc`.
That is why `23.55 × 8 = 188.4` recovers ~180. The **definition** is the
measured concurrent-window rate; the product is only the no-timestamp estimate.

### Code touch points

| File | Change |
|------|--------|
| `pod/perf_grid.py::_agg` | concurrent-window agg; keep `tokens_per_wall_s`, `agg_saturated_est` |
| `pod/perf_grid.py::_one_request` | forward/reconstruct `decode_t0`/`decode_t1` |
| `aeon/targets.py::_chat_stream` | emit absolute `decode_t0`/`decode_t1` (optional; reconstruction works) |
| `aeon/scoring.py::perf_board` / `_peak_agg_cell` | peak = max concurrent-total; legacy cells → `decode×conc` estimate |
| `test_perf_grid.py` | concurrent_window + saturated_est unit tests |

### Rollout

1. PR → `AEON-7/Aeon-Bench-Pod` (and Mothership if it vendors `scoring.py`)
2. Hot-patch live spark `aeon-pod` `/app/mvp/pod/perf_grid.py` + `aeon/scoring.py`
3. Apples-to-apples smoke after patch: fixed n=7, seqs=16, c8-only — expect
   `agg_source=concurrent_window` and agg in the ~180 ballpark if decode_mean≈23–25
4. No bounce unless serve unhealthy

### Stuck run note

peak16 launcher still hung (GPU 0%) at doc time — banked c8/c10 **66.7/73.6** are
**legacy wall-agg**. After hot-patch, board re-read of those cells via
`decode×conc` estimate yields ~188 / ~226 without re-bench.
