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

# ---- 3) suite-era boundary joins + floors (the 27B "missing expert/frontier" report) -------

known = {c["id"] for c in suite_mod.all_known_cases()}
v3_ids = {c["id"] for c in V3}
check(v3_ids <= known, "all_known_cases folds every v3 id (none vanish from label joins)")
check(len(known) >= len(suite_mod.CASES) + 105,
      "the 105 replaced v3 top-tier ids stay label-joinable alongside the v4 corpus")

from aeon.app import _difficulty_map  # noqa: E402
dm = _difficulty_map()
missing = [c["id"] for c in V3 if c["id"] not in dm]
check(missing == [],
      "submission-detail difficulty map labels EVERY v3 case (expert/frontier/god_mode included)")
check(dm.get("v3.math.expert.01") or any(k.startswith("v3.") and ".expert." in k for k in dm),
      "spot-check: a v3 expert id resolves a difficulty label")

from aeon.app import _prompt_map  # noqa: E402
pmap = _prompt_map("text")
no_prompt = [c["id"] for c in V3 if not pmap.get(c["id"])]
check(no_prompt == [],
      "submission-detail 'asked' prompt resolves for EVERY v3 case (none blank after the bump)")

check(suite_mod.corpus_size_for("aeon-suite-v3") == 155
      and suite_mod.corpus_size_for(suite_mod.SUITE_ID) == len(suite_mod.CASES)
      and suite_mod.corpus_size_for("aeon-suite-v0") == len(suite_mod.CASES),
      "corpus_size_for: run's own suite size; unknown suites fall back conservatively")

# the fixture's FULL v3 run must pass the best-run coverage gate measured against ITS corpus
idx = scoring._perf_percentile_index() if hasattr(scoring, "_perf_percentile_index") else None
lb2 = scoring.leaderboard()
row = next(m for m in lb2["models"] if m["canonical"] == MODEL)
check(row.get("best_intelligence_run") == rid or (row.get("runs") or [{}])[0].get("run") == rid,
      "full v3 pass survives the per-run coverage floor (155/155 of ITS OWN suite)")

print(f"\nOK  explorer legacy fallback: {PASSED} checks passed")
