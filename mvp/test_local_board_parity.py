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
import ast
import inspect
import os
import sys
import tempfile
import textwrap

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

# ---- EVERY hop must forward it, checked structurally rather than by string match -----------------
# The first version of this test asserted `"board=board" in source`. That is true when the two END
# layers forward it and the MIDDLE one drops it, which is exactly what shipped: _bench_and_results
# grew a `board` parameter and never passed it on, so the attested path — the only path that yields
# a rankable GOD MODE run — still wrote sentinels as board='text'. A substring cannot see a hole in
# the middle of a chain; an AST can.
def forwards(fn, callee, kwarg="board"):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name == callee and any(k.arg == kwarg for k in node.keywords):
                return True
    return False


ok(forwards(runner.run_benchmark, "create_run"),
   "run_benchmark forwards the board to create_run (not the column default)")
ok(forwards(aeon_pod._bench_and_results, "run_benchmark"),
   "_bench_and_results forwards the board onward — THE HOP THAT WAS SILENTLY DROPPED")

# Stronger than checking named functions one at a time: EVERY call anywhere in the pod module that
# opens a run must carry the board. A new call site added later cannot quietly omit it.
def unguarded_calls(module, callees, kwarg="board"):
    tree = ast.parse(inspect.getsource(module))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in callees and not any(k.arg == kwarg for k in node.keywords):
                out.append("%s(...) line %d" % (name, node.lineno))
    return out


_gaps = unguarded_calls(aeon_pod, {"run_benchmark", "_bench_and_results"})
ok(not _gaps, "every run-opening call in aeon_pod carries a board" + (" — MISSING: " + ", ".join(_gaps) if _gaps else ""))

# ...and prove it at RUNTIME, because a kwarg can be present in the AST and still be the wrong value.
db.init_db()            # _bench_and_results harvests its own rows at the end; idempotent
_seen = {}
_real = runner.run_benchmark
runner.run_benchmark = lambda *a, **kw: _seen.update(kw)
try:
    aeon_pod._bench_and_results("alias", "http://127.0.0.1:1/v1", board="god")
finally:
    runner.run_benchmark = _real
ok(_seen.get("board") == "god",
   "a god bench really does reach the runner as board='god', not the 'text' default")

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

# ---- THE SECOND BOARD COLUMN, which made the first fix inert -------------------------------------
# db.all_results_with_runs() — the query every leaderboard is built from — filters
# `WHERE r.board = ?`: the RESULTS row, NOT the run row. So labelling the run correctly and leaving
# its result rows at the 'text' default keeps the sentinels invisible to the god board, and the
# GOD SCORE still renders from agentic alone. runner._persist forgot exactly this while
# _board_persist (the multimodal path) had always passed it.
#
# This check goes through the REAL query rather than asserting on source, so it cannot be satisfied
# by a fix that looks right and does nothing.
def result(run_id, cid, board):
    db.save_result(run_id, cid, category="Math", tier=0, status="scored", score=1.0,
                   raw_output="x", evidence={}, speed={}, board=board)


result("godrow", "v4.math.god_mode.01", "god")
result("godrow", "v4.math.god_mode.02", "text")     # the bug's signature: god run, text-boarded row
db.finish_run("godrow", "succeeded")
joined = db.all_results_with_runs(board="god")
cids = {r["case_id"] for r in joined if r["run"] == "godrow"}
ok("v4.math.god_mode.01" in cids,
   "a god-boarded RESULT row reaches the god board through the real join")
ok("v4.math.god_mode.02" not in cids,
   "…and a text-boarded row under the SAME god run does not — results.board is what the board sees")

ok(not unguarded_calls(runner, {"save_result"}),
   "every save_result in the runner carries a board — _persist included")

print("\nOK  local/global board parity: %d checks passed" % PASS)
