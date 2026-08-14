"""The performance sweep is visible while it runs.

The perf grid is the last and one of the longest phases of a bench — a c1/c4/c8 ladder over five
prompt categories, then again per harness — and it published NOTHING to the live view. A
`progress_cb(conc, done, total)` existed and only moved a stage counter, so for hours the one
thing an operator wanted to see (how fast is this serve, at this concurrency, right now) was
invisible until the whole sweep ended and every number appeared at once.

Same shape of blindness the agentic phase had before its containers streamed, and the same fix,
onto the same wall: one tile per CELL. A cell is a category at one concurrency, which is the unit
the grid actually measures — `Math @ c4` is four concurrent Math streams and nothing else in
flight, because mixing categories within a level would dilute a category's aggregate tok/s with
another's wall time. So a tile maps one-to-one onto a figure that later appears on the performance
board, and watching it is watching that figure being made.

Run: python3 mvp/test_perf_stream.py
"""
import ast
import inspect
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ["AEON_STATE_DIR"] = tempfile.mkdtemp(prefix="aeon_perfstream_")
os.environ.setdefault("AEON_DB", os.path.join(os.environ["AEON_STATE_DIR"], "t.db"))

from pod import livestreams, perf_grid, perf_stream     # noqa: E402

PASS = 0
fails = []


def _safe(s):
    """Console-proof. The tile text carries em-dashes and box rules on purpose (it renders in a
    browser), but a cp1252 Windows console cannot encode them and the whole run dies on a print
    rather than on an assertion — a test that fails for a reason it is not testing."""
    return str(s).encode("ascii", "replace").decode("ascii")


def ok(cond, label, detail=""):
    global PASS
    print(_safe(("  ok   " if cond else "  FAIL ") + label + (("  " + detail) if detail else "")))
    if cond:
        PASS += 1
    else:
        fails.append(label)


def tile(cid):
    return next((s for s in livestreams.snapshot(limit=64) if s.get("case") == cid), None)


print("telemetry must never cost an operator the numbers they waited hours for")
c0 = perf_stream.cell("direct", "Math", 4, 8)
ok(c0 is perf_stream.NULL, "wall off -> an inert stand-in, so the measurement loop needs no branch")
c0.tick({"ttft_ms": 1}); c0.error("x"); c0.close({})
ok(True, "and every method on it is safe to call")

livestreams.clear()
livestreams.enable(True)

print("one tile per CELL — the unit the grid actually measures")
c = perf_stream.cell("direct", "Math", 4, 8)
ok(c.cid == "perf:direct.math.c4", "cid names kind, category and concurrency", c.cid)
ok(c.label == "direct Math @ c4", "and the label reads the way the board will", c.label)

print("measurements appear AS THEY LAND, not after the sweep")
c.tick({"ttft_ms": 542.66, "decode_tps": 46.95, "output_tokens": 962})
t = tile(c.cid)
ok(t is not None and "1/8" in t["answer"], "progress within the cell is shown")
ok(t and "543ms" in t["answer"] and "47.0 tok/s" in t["answer"] and "962 tok" in t["answer"],
   "with the three numbers that matter: ttft, decode rate, tokens", t["answer"].strip())
c.tick({"ttft_ms": 611.2, "decode_tps": 44.1, "output_tokens": 1004})
ok("2/8" in tile(c.cid)["answer"], "and the count advances per request")

print("a failed request is recorded WITHOUT poisoning the measurement channel")
c.error(RuntimeError("connection reset"))
t = tile(c.cid)
ok("ERROR connection reset" in t["reasoning"], "errors go to the dim channel")
ok("ERROR" not in t["answer"], "…so the bright channel stays a clean run of measurements")

print("closing publishes the aggregate — the figure that reaches the performance board")
c.close({"agg_tps": 180.4, "ttft_ms_mean": 576.9, "decode_tps_mean": 45.5})
t = tile(c.cid)
ok(t and "180.4 tok/s" in t["reasoning"], "the cell's aggregate throughput is published",
   (t or {}).get("reasoning", "").strip()[-70:])
ok(t and t.get("done") is True, "and the tile ends rather than lingering live")

print("junk in never raises out")
c2 = perf_stream.cell("direct", "Prose", 1, 2)
c2.tick(None)
c2.tick({"ttft_ms": None, "decode_tps": None, "output_tokens": None})
c2.tick({"ttft_ms": "not-a-number"})
c2.close(None)
ok(True, "None rows, null fields and a non-numeric value are all tolerated")
ok("—" in (tile(c2.cid) or {}).get("answer", ""), "missing values render as a dash, never as 0")

print("the grid actually calls it — a publisher nothing invokes is worse than none")
src = inspect.getsource(perf_grid.run_direct_grid)
tree = ast.parse(src.lstrip())
calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
names = {getattr(n.func, "attr", None) for n in calls}
ok("cell" in names, "run_direct_grid opens a tile per cell")
ok("tick" in names, "…feeds it each completed request")
ok("close" in names, "…and closes it with the cell aggregate")
ok(src.index("perf_stream.cell") < src.index("time.perf_counter()"),
   "the tile opens BEFORE the cell's clock starts, so its lifetime is the cell's")

livestreams.enable(False)
print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all perf-stream tests passed (%d checks)" % PASS)
