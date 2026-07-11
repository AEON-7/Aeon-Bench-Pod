"""Leaderboard coverage guard: fast-bench (seeded) draws and partial passes never rank.

A model whose only succeeded runs are seeded subsamples or partial passes must not
appear on the board at all (the Nemotron-Puzzle 25/155 incident, 2026-07-11) — while
the same seeded runs stay visible to the compare-by-seed A/B tooling.

    python test_leaderboard_coverage.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Point the DB layer at a throwaway SQLite file BEFORE aeon.db is imported.
os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-lb-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

FULL = "lab/full-model"
SEEDED_ONLY = "lab/fast-bench-only-model"
PARTIAL_ONLY = "lab/partial-pass-model"
ERRORS_ONLY = "lab/all-rows-no-scores-model"


def _mk_run(model, cases, *, seed=None, score=1.0):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(cases),
                  params={}, env={}, hf_repo=model, trust_tier="attested",
                  bench_seed=seed)
    for c in cases:
        db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                       status="scored", score=score, raw_output="ok",
                       evidence={}, speed={})
    db.finish_run(rid, "succeeded")
    return rid


def main():
    all_cases = suite_mod.CASES
    assert len(all_cases) >= 30, "suite corpus unexpectedly small"

    _mk_run(FULL, all_cases)                                  # comprehensive pass -> ranks
    _mk_run(SEEDED_ONLY, all_cases[:25], seed="deadbeef")     # fast-bench draw -> never ranks
    _mk_run(PARTIAL_ONLY, all_cases[:30])                     # unseeded partial -> below floor
    _mk_run(ERRORS_ONLY, all_cases, score=None)               # full rows, zero SCORED -> below floor

    board = scoring.leaderboard()
    models = {m["canonical"] for m in board["models"]}
    assert FULL in models, f"full run missing from board: {models}"
    assert SEEDED_ONLY not in models, "seeded fast-bench run ranked on the leaderboard"
    assert PARTIAL_ONLY not in models, "partial (low-coverage) run ranked on the leaderboard"
    assert ERRORS_ONLY not in models, "all-error run (rows but no scores) ranked on the leaderboard"

    # the seeded draw must STAY available to the A/B tooling
    seeds = {s["seed"] for s in scoring.seed_index()}
    assert "deadbeef" in seeds, "seeded run vanished from compare-by-seed"

    # quality index (perf-board join) must also ignore the seeded + partial runs
    qidx = scoring._quality_index()
    canon = {k[0] for k in qidx}
    assert SEEDED_ONLY not in canon and PARTIAL_ONLY not in canon and ERRORS_ONLY not in canon

    print("OK  leaderboard coverage guard: seeded + partial runs excluded, A/B intact")


if __name__ == "__main__":
    main()
