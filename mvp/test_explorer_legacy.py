"""Explorer during a legacy-fallback window — the suite-v4 launch gap.

After a suite bump the board falls back to the newest legacy suite that has runs
(suite_shown/legacy badge). The explorer must follow: join those runs against the LEGACY
corpus's own difficulty table (suites/v3 cells), not the new suite's (mislabeled grid) and
not nothing (rows the board shows would vanish from the explorer). Uses its own throwaway
DB seeded ONLY with a v3 run, so the fallback path is what leaderboard() actually takes."""
import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-explorer-legacy-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


# ---- 1) the frozen legacy corpus itself ----------------------------------------------------

V3 = suite_mod.legacy_cases("aeon-suite-v3")
check(len(V3) == 155, f"legacy v3 corpus composes from suites/v3 cells (155 cases, got {len(V3)})")
check(all(c.get("difficulty") for c in V3), "every legacy case carries its difficulty label")
check(suite_mod.legacy_cases(suite_mod.SUITE_ID) is suite_mod.CASES,
      "current suite id returns the live corpus object")
check(suite_mod.legacy_cases("aeon-suite-v0") == [], "unknown legacy id -> empty, never a raise")

# ---- 2) a v3-only DB: board falls back, explorer follows -----------------------------------

MODEL = "lab/legacy-window-model"
rid = uuid.uuid4().hex[:12]
db.create_run(rid, model=MODEL, target_url="http://x", judge_model=None, judge_is_self=True,
              suite_id="aeon-suite-v3", suite_hash="v3-hash", n_cases=len(V3),
              params={}, env={"hardware": {"detected_label": "single DGX Spark (GB10)"}},
              hf_repo=MODEL, trust_tier="attested")
for c in V3:
    db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                   status="scored", score=0.8, raw_output="ok", evidence={},
                   speed={"decode_tps": 21.0})
db.finish_run(rid, "succeeded")
with db.connect() as conn:
    conn.execute("UPDATE runs SET started_at=1700000100, finished_at=1700000700 WHERE id=?", (rid,))

lb = scoring.leaderboard()
check(lb.get("suite_shown") == "aeon-suite-v3" and lb.get("legacy") is True,
      "fixture board really is in the legacy-fallback window")

ex = scoring.explorer_matrix()
check(ex["suite_id"] == "aeon-suite-v3",
      "explorer reports the SHOWN suite, not the empty current one")
check(len(ex["models"]) == 1 and ex["models"][0]["canonical"] == MODEL,
      "the legacy run the board shows appears in the explorer")
cells = ex["models"][0]["cells"]
check(set(cells) == {"Math", "Instruction", "Reasoning", "Coding", "Prose"},
      "all five categories joined against the v3 corpus")
n_cells = sum(len(v) for v in cells.values())
check(n_cells == 30, f"full 5x6 grid labeled from the legacy table (got {n_cells} cells)")
check(all(c["score"] == 80.0 for byd in cells.values() for c in byd.values()),
      "scores aggregate correctly through the legacy join")
check(ex["models"][0]["hw_bucket"], "hardware facet rides along for the legacy run")

print(f"\nOK  explorer legacy fallback: {PASSED} checks passed")
