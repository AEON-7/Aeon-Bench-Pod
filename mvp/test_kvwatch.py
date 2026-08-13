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

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all sustained-load tests passed")
