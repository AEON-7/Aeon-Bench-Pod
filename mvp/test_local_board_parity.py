"""A pod must not flatter a model that the mothership will mark down.

THE BUG THIS PINS. `run_benchmark` recorded the local sentinel run with NO board argument, so it
fell back to the column default 'text' — even for a GOD MODE run. The agentic runs were written
with board='god'. The pod's own god board therefore found the harnesses but NOT its own sentinels,
and computed GOD SCORE from agentic alone.

Measured on a real run of Gemma-4-26B-A4B:

    pod            god_score 78.7   sentinels MISSING   eligible false
    aeon-bench.com god_score 35.5   sentinels 6.7       eligible true

Same model, same weights, same completed bench — a 43-point gap, and the pod was the flattering
one. An operator would watch their model score 78.7 at home and 35.5 in public, which reads as the
leaderboard cheating them rather than as their own dashboard being wrong. The submitted bundle was
always correct; only the LOCAL MIRROR was mislabelled.

Run: python3 mvp/test_local_board_parity.py
"""
import inspect
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", os.path.join(tempfile.mkdtemp(prefix="aeon_board_"), "t.db"))

from aeon import db, runner            # noqa: E402
from pod import aeon_pod               # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    if not cond:
        print("FAIL  %s" % label)
        sys.exit(1)
    PASS += 1
    print("ok    %s" % label)


# ---- the board must be threadable all the way to the row ---------------------------------------
rb = inspect.signature(runner.run_benchmark).parameters
ok("board" in rb, "run_benchmark accepts a board")
ok(rb["board"].default == "text", "…defaulting to text, so non-god callers are unchanged")

ba = inspect.signature(aeon_pod._bench_and_results).parameters
ok("board" in ba, "_bench_and_results accepts a board")

src = inspect.getsource(runner.run_benchmark)
ok("board=board" in src, "run_benchmark hands the board to create_run (not the column default)")

pod_src = inspect.getsource(aeon_pod)
ok("_bench_and_results(alias, target, board=board" in pod_src,
   "the dimension runner passes its real board down")

# ---- and the row actually carries it ------------------------------------------------------------
db.init_db()
db.create_run("godrow", model="m", target_url="mock", judge_model=None, judge_is_self=False,
              suite_id="aeon-suite-v4", suite_hash="h", n_cases=1, params={}, env={}, board="god")
db.create_run("txtrow", model="m", target_url="mock", judge_model=None, judge_is_self=False,
              suite_id="aeon-suite-v4", suite_hash="h", n_cases=1, params={}, env={})
g = db.get_run("godrow") or {}
t = db.get_run("txtrow") or {}
ok(g.get("board") == "god", "a run created with board='god' stores 'god'")
ok((t.get("board") or "text") == "text", "a run created without one still stores the text default")

# THE REGRESSION ITSELF: the god row must be visible to a god-board query, and the text row must
# not be. Before the fix the sentinel row landed in the second bucket and vanished from the board.
rows = [r for r in db.list_runs(50) if (r.get("board") or "text") == "god"]
ok(any(r["id"] == "godrow" for r in rows), "the god row is FOUND by a board='god' query")
ok(not any(r["id"] == "txtrow" for r in rows), "the text row is not swept into the god board")

print("\nOK  local/global board parity: %d checks passed" % PASS)
