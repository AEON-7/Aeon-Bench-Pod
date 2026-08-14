"""Sustained load: does the serve degrade as KV pressure builds?

The concurrency ladder measures a fresh engine sprinting. It cannot answer what an operator lives
with — after hours under load, is it still that fast? The mechanism is in the engine's own
counters: as kv_cache_usage_perc climbs, vLLM PREEMPTS sequences and recomputes them, so throughput
falls with nothing erroring and no log line.

Half of these tests are about SILENCE. A telemetry section that cannot be computed honestly must be
absent, never zero — a "0% degradation" badge on a serve nobody measured is worse than no badge.

Run: python3 mvp/test_kvwatch.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_kvwatch_test.db")

from pod import kvwatch, perf_grid   # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + detail) if detail else ""))
    if not cond:
        fails.append(name)


def watcher(points):
    w = kvwatch.Watcher("http://x/v1")
    w.points = points
    return w


def pts(spec):
    """spec: [(kv_pct, tok_rate, running, preempt_delta), ...] on a 15s clock."""
    out = []
    for i, (kv, tps, run, pre) in enumerate(spec):
        out.append({"t": 1000.0 + 15 * i, "kv_pct": kv, "gen_tok_rate": tps,
                    "running": run, "waiting": 0, "preempt_d": pre,
                    "preempt_rate": pre / 15.0})
    return out


def mixed(spec):
    """spec: [(kv_pct, tok_rate, running, spec_accept_pct|None), ...] on a 15s clock.

    Unlike pts(), this one lets CONCURRENCY vary across the series — which is what a real
    c1/c4/c8 ladder does, and what every pre-existing test in this file holds constant."""
    out = []
    for i, (kv, tps, run, acc) in enumerate(spec):
        p = {"t": 1000.0 + 15 * i, "kv_pct": kv, "gen_tok_rate": tps,
             "running": run, "waiting": 0, "preempt_d": 0, "preempt_rate": 0.0}
        if acc is not None:
            p["spec_accept_pct"] = acc
        out.append(p)
    return out


print("the metrics URL follows whatever is being benchmarked")
check("…/v1 -> …/metrics", kvwatch._metrics_url("http://h:8000/v1") == "http://h:8000/metrics")
check("trailing slash", kvwatch._metrics_url("http://h:8000/v1/") == "http://h:8000/metrics")
check("no /v1 suffix", kvwatch._metrics_url("http://h:8000") == "http://h:8000/metrics")

print("SILENCE when there is nothing honest to say")
check("no samples -> no section", watcher([]).summary() is None)
check("too few samples -> no section",
      watcher(pts([(0.1, 100, 4, 0)] * 4)).summary() is None)
check("samples but engine idle -> no section (idle gaps are not slowness)",
      watcher(pts([(0.1, 0, 0, 0)] * 12)).summary() is None)
check("a watcher that never scraped reports unavailable",
      kvwatch.Watcher("http://127.0.0.1:1/v1").available is None)
check("no summary -> no rows at all", perf_grid.sustained_rows(None, []) == [])

print("real degradation is detected and quantified")
# throughput halves as KV climbs, with preemptions appearing at the top
deg = watcher(pts([(0.10, 400, 8, 0), (0.12, 396, 8, 0), (0.15, 404, 8, 0),
                   (0.40, 300, 8, 0), (0.45, 296, 8, 1), (0.50, 304, 8, 2),
                   (0.85, 200, 8, 5), (0.88, 196, 8, 6), (0.92, 204, 8, 7)]))
s = deg.summary()
check("a section is produced", s is not None)
check("low-KV throughput is the low-pressure mean", s and s["tps_at_low_kv"] == 400.0,
      str(s and s["tps_at_low_kv"]))
check("high-KV throughput is the high-pressure mean", s and s["tps_at_high_kv"] == 200.0,
      str(s and s["tps_at_high_kv"]))
check("degradation is reported as a percentage", s and s["degradation_pct"] == 50.0,
      str(s and s["degradation_pct"]))
check("preemptions are counted (the MECHANISM, not just the symptom)",
      s and s["preemptions"] == 21, str(s and s["preemptions"]))
check("the KV range measured is disclosed", s and s["kv_pct_min"] == 0.10 and s["kv_pct_max"] == 0.92)

print("a serve that does NOT degrade says so")
flat = watcher(pts([(0.1 + i * 0.09, 400, 8, 0) for i in range(9)]))
s2 = flat.summary()
check("flat throughput -> ~0% degradation", s2 and abs(s2["degradation_pct"]) < 0.01,
      str(s2 and s2["degradation_pct"]))
check("and zero preemptions", s2 and s2["preemptions"] == 0)

print("getting FASTER under pressure is a real result, not clamped away")
faster = watcher(pts([(0.10, 200, 8, 0), (0.12, 204, 8, 0), (0.15, 196, 8, 0),
                      (0.40, 300, 8, 0), (0.45, 300, 8, 0), (0.50, 300, 8, 0),
                      (0.85, 400, 8, 0), (0.88, 396, 8, 0), (0.92, 404, 8, 0)]))
s3 = faster.summary()
check("negative degradation is reported, not floored at 0",
      s3 and s3["degradation_pct"] < 0, str(s3 and s3["degradation_pct"]))

print("the series survives a long run without losing its shape")
long_w = watcher(pts([(0.1 + (i % 90) * 0.01, 300 + (i % 10), 8, 0) for i in range(2000)]))
ser = long_w.series(limit=100)
check("thinned to the limit", len(ser) <= 100, "%d points" % len(ser))
check("still spans the whole run (first and last kept)",
      ser and ser[0]["t"] == long_w.points[0]["t"])

print("rows ride the existing perf bundle — no schema change")
rows = perf_grid.sustained_rows(s, deg.series())
check("two rows: summary + timeline", len(rows) == 2)
check("namespaced under perf.sustained",
      all(r["case_id"].startswith("perf.sustained.") for r in rows))
check("scored None — telemetry is not a grade", all(r["score"] is None for r in rows))
check("category matches the perf board", all(r["category"] == "Performance" for r in rows))
check("timeline carries the points",
      any(r["case_id"].endswith("timeline") and r["evidence"].get("points") for r in rows))

print("THE LADDER CONFOUND: KV and concurrency climb together and must be separated")
# A c1 then c8 ladder. WITHIN each regime throughput falls as KV deepens — real degradation.
# Globally the c8 samples sit at both high KV and high aggregate throughput, so an unstratified
# comparison concludes the serve got FASTER under pressure. This reproduces the shape measured on
# a real Gemma-4-26B god run: 191.2 -> 445.8 tok/s, reported as -133.1% "degradation".
ladder = mixed(
    [(0.05, 200, 1, 62), (0.06, 196, 1, 60), (0.07, 204, 1, 61),
     (0.13, 150, 1, 12), (0.14, 146, 1, 11), (0.15, 154, 1, 13)]
    + [(0.40, 460, 8, 58), (0.41, 456, 8, 57), (0.42, 464, 8, 59),
       (0.50, 410, 8, 30), (0.51, 406, 8, 31), (0.52, 414, 8, 29),
       (0.63, 360, 8, 9), (0.64, 356, 8, 8), (0.65, 364, 8, 10)])
sl = watcher(ladder).summary()
_all = sorted(ladder, key=lambda p: p["kv_pct"])
_t3 = len(_all) // 3
_nlo = sum(p["gen_tok_rate"] for p in _all[:_t3]) / _t3
_nhi = sum(p["gen_tok_rate"] for p in _all[-_t3:]) / _t3
check("the UNSTRATIFIED comparison inverts the sign on this data (the bug)",
      (_nlo - _nhi) / _nlo < 0, "naive %.1f%%" % (100 * (_nlo - _nhi) / _nlo))
check("stratified degradation is POSITIVE — the serve does slow as its own KV deepens",
      sl and sl["degradation_pct"] > 0, str(sl and sl["degradation_pct"]))
check("every qualifying concurrency regime is disclosed, not averaged away",
      sl and sorted(r["running"] for r in sl["by_concurrency"]) == [1, 8],
      str(sl and [r["running"] for r in sl["by_concurrency"]]))
check("the headline names the concurrency it was measured at", sl and sl["concurrency"] == 8,
      str(sl and sl.get("concurrency")))
check("headline throughputs come from that same regime, so they reconcile",
      sl and sl["tps_at_low_kv"] == 460.0 and sl["tps_at_high_kv"] == 360.0,
      str(sl and (sl["tps_at_low_kv"], sl["tps_at_high_kv"])))
check("each regime carries its own KV cut points",
      sl and all(r["kv_lo_cut"] < r["kv_hi_cut"] for r in sl["by_concurrency"]))

print("acceptance rides along, because it is usually the MECHANISM behind the decay")
check("acceptance is reported at both ends of the KV range",
      sl and sl["accept_at_low_kv"] is not None and sl["accept_at_high_kv"] is not None)
check("and shows the drafter falling away as the sequence deepens",
      sl and sl["accept_at_low_kv"] > sl["accept_at_high_kv"],
      str(sl and (sl["accept_at_low_kv"], sl["accept_at_high_kv"])))
check("a serve with no drafter reports no acceptance rather than 0%",
      watcher(pts([(0.1 + i * 0.09, 400, 8, 0) for i in range(9)]))
      .summary()["accept_at_low_kv"] is None)

print("SILENCE again when no concurrency regime holds still long enough to attribute a change")
churn = watcher(mixed([(0.10 + i * 0.05, 300 + 10 * i, i + 1, None) for i in range(8)])).summary()
check("the run is still described", churn is not None)
check("but degradation is None, not a number that cannot be defended",
      churn and churn["degradation_pct"] is None, str(churn and churn["degradation_pct"]))
check("and it says why", churn and "no concurrency regime" in (churn.get("degradation_note") or ""))
check("no by_concurrency entries qualified", churn and churn["by_concurrency"] == [])

print("the spec-decode counters are read from the engine, and not shadowed by their siblings")
check("both counters are scraped",
      "spec_accept" in kvwatch._COUNTERS and "spec_draft" in kvwatch._COUNTERS)
_m = kvwatch._parse_metrics(
    'vllm:spec_decode_num_accepted_tokens_per_pos_total{engine="0"} 999.0\n'
    'vllm:spec_decode_num_accepted_tokens_total{engine="0"} 42.0\n'
    'vllm:spec_decode_num_draft_tokens_total{engine="0"} 500.0\n'
    'vllm:kv_cache_usage_perc{engine="0"} 0.147\n')
check("accepted-tokens total is read", _m.get("spec_accept") == 42.0, str(_m.get("spec_accept")))
check("the per-position sibling does NOT shadow it", _m.get("spec_accept") != 999.0)
check("drafted-tokens total is read", _m.get("spec_draft") == 500.0, str(_m.get("spec_draft")))
check("labelled gauges still parse", _m.get("kv_pct") == 0.147, str(_m.get("kv_pct")))

print("the new fields reach the submission row without a schema change")
_rows = perf_grid.sustained_rows(sl, [])
check("by_concurrency survives into the row evidence",
      _rows and _rows[0]["evidence"].get("by_concurrency"))
check("so does the concurrency the headline was measured at",
      _rows and _rows[0]["evidence"].get("concurrency") == 8)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all sustained-load tests passed")
