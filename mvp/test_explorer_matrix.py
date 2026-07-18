"""Explorer matrix guard: /api/explorer's scoring.explorer_matrix().

Covers the EXPLORE THE DATA data side:
  * cells[category][difficulty] = {score: mean 0-100, n, tps: mean decode_tps},
    joined to the CURRENT suite corpus by case_id (same join as compare_by_seed)
  * source run = the board's best_intelligence_run: eligible runs beat a
    higher-scoring self_reported run; seeded fast-bench draws never qualify
  * below-coverage-floor models never appear (board eligibility reused)
  * malformed rows are skipped, never raised: unknown case ids, no_answer /
    unscored rows (they also shrink the cell's n), junk speed payloads
  * model-level filter facets ride along: trust_tier + ctx_len from the board
    row, hw_bucket/hw_family = the source run's benched rig through hwnorm
    (absent hardware -> the honest 'Unlabeled' bucket, absent recipe -> ctx null)
  * payload is bit-identical across reads (computed-on-read, adds nothing)

    python test_explorer_matrix.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Point the DB layer at a throwaway SQLite file BEFORE aeon.db is imported.
os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-explorer-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, hwnorm, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

TIERED = "lab/tiered-model"          # scores stepped by difficulty + per-case tps
BESTPICK = "lab/best-pick-model"     # low+high eligible runs, higher self_reported, seeded
PARTIAL = "lab/partial-model"        # below the coverage floor -> never in the payload
SELFONLY = "lab/self-only-model"     # self_reported everywhere -> honest facet values

TIERED_HW = "NVIDIA GeForce RTX 5090 32GB"           # benched-rig label for the TIERED run
TIERED_CTX = 65536                                   # served context in its recipe

# score per difficulty tier — the decay staircase the matrix must reproduce exactly
DIFF_SCORE = {"easy": 1.0, "medium": 0.8, "hard": 0.6, "expert": 0.4,
              "frontier": 0.2, "god_mode": 0.1}

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


def _run(model, cases, *, tier="attested", seed=None, score=None, scores_by_id=None,
         speed_by_id=None, status_by_id=None, extra_rows=(), env=None, recipe=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(cases),
                  params={}, env=env or {}, hf_repo=model, trust_tier=tier,
                  bench_seed=seed, recipe=recipe)
    for c in cases:
        st = (status_by_id or {}).get(c["id"], "scored")
        s = None if st != "scored" else (scores_by_id or {}).get(c["id"], score)
        db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                       status=st, score=s, raw_output="ok", evidence={},
                       speed=(speed_by_id or {}).get(c["id"], {}))
    for cid, cat in extra_rows:                      # malformed / off-corpus rows
        db.save_result(rid, cid, category=cat, tier=1, status="scored", score=1.0,
                       raw_output="junk", evidence={}, speed={"decode_tps": 999.0})
    db.finish_run(rid, "succeeded")
    return rid


def entry(payload, canonical):
    for m in payload["models"]:
        if m["canonical"] == canonical:
            return m
    return None


def main():
    cases = suite_mod.CASES
    graded = [c for c in cases if c.get("difficulty") in suite_mod.DIFFICULTIES]
    assert len(graded) >= 30, "graded corpus unexpectedly small"

    # ---- TIERED fixture: stepped scores + per-case tps + deliberate bad rows -----------
    noans_case = graded[0]["id"]                     # asked, declined -> no score, n shrinks
    junk_speed_case = graded[1]["id"]                # tps is a string -> tps mean skips it
    scores = {c["id"]: DIFF_SCORE.get(c.get("difficulty"), 1.0) for c in cases}
    tps_of = {c["id"]: 10.0 * (i + 1) for i, c in enumerate(cases)}
    speed = {c["id"]: {"decode_tps": tps_of[c["id"]], "ttft_ms": 5.0} for c in cases}
    speed[junk_speed_case] = {"decode_tps": "fast"}
    _run(TIERED, cases, scores_by_id=scores, speed_by_id=speed,
         status_by_id={noans_case: "no_answer"},
         extra_rows=(("zz.not.in.corpus", "Math"), ("perf.direct.Math.c8", "perf")),
         env={"hardware": {"detected_label": TIERED_HW}},
         recipe={"flags": ["--max-model-len", str(TIERED_CTX)]})

    # ---- BESTPICK fixture: the eligible best run must be the matrix source -------------
    _run(BESTPICK, cases, score=0.4)                                  # eligible, low
    _run(BESTPICK, cases, score=0.8)                                  # eligible, BEST
    _run(BESTPICK, cases, score=1.0, tier="self_reported")            # higher but ineligible
    _run(BESTPICK, cases, score=1.0, seed="cafe01")                   # seeded -> never ranks

    # ---- PARTIAL fixture: below the coverage floor -> not on the board -----------------
    _run(PARTIAL, cases[:int(len(cases) * 0.5)], score=1.0)

    # ---- SELFONLY fixture: self_reported, no hardware label, no recipe ------------------
    _run(SELFONLY, cases, score=0.5, tier="self_reported")

    ex = scoring.explorer_matrix()

    # ---- payload frame ------------------------------------------------------------------
    check(ex["categories"] == suite_mod.CATEGORIES, "payload carries the suite category order")
    check(ex["difficulties"] == suite_mod.DIFFICULTIES, "payload carries the difficulty order")
    check(ex["suite_id"] == suite_mod.SUITE_ID, "payload names the current suite")
    check(entry(ex, PARTIAL) is None, "below-floor model never appears (board eligibility reused)")

    # ---- TIERED: every cell's mean, n and tps match the corpus exactly ------------------
    t = entry(ex, TIERED)
    check(t is not None, "full tiered run appears in the payload")
    lb = scoring.leaderboard()
    brow = next(m for m in lb["models"] if m["canonical"] == TIERED)
    check(t["run"] == brow["best_intelligence_run"],
          "matrix source run = the board's best_intelligence_run")
    check(t["composite"] == brow["composite"] and t["aeon_score"] == brow["aeon_score"],
          "composite + aeon_score ride along from the board row")

    # ---- filter facets: trust tier / hardware bucket / served context -------------------
    hwn = hwnorm.normalize_label(TIERED_HW)
    check(t["trust_tier"] == "attested" and t["record_eligible"] is True,
          "attested model carries trust_tier=attested (the board's own value)")
    check(t["hw_bucket"] == hwn["bucket"] and t["hw_family"] == hwn["family"],
          f"source run's rig normalizes through hwnorm ({hwn['bucket']} / {hwn['family']})")
    check(t["hw_family"] == hwnorm.FAMILY_RTX, "RTX label lands in the nvidia-rtx family")
    check(t["ctx_len"] == TIERED_CTX,
          "ctx_len = served context parsed from the best run's recipe (board join)")
    so = entry(ex, SELFONLY)
    check(so is not None and so["trust_tier"] == "self_reported"
          and so["record_eligible"] is False,
          "self_reported-only model keeps its honest trust facet")
    check(so["hw_bucket"] == "Unlabeled" and so["hw_family"] == hwnorm.FAMILY_UNLABELED,
          "no hardware label -> the honest Unlabeled bucket, never a guess")
    check(so["ctx_len"] is None, "no recipe anywhere -> ctx_len null (not recorded)")

    expect = {}
    for c in graded:
        if c["id"] == noans_case:
            continue                                # no_answer -> no score -> not counted
        cell = expect.setdefault(c["category"], {}).setdefault(c["difficulty"],
                                                               {"s": [], "t": []})
        cell["s"].append(DIFF_SCORE[c["difficulty"]])
        if c["id"] != junk_speed_case:              # junk tps skipped from the mean
            cell["t"].append(tps_of[c["id"]])
    n_cells = 0
    for cat, byd in expect.items():
        for d, cell in byd.items():
            got = t["cells"][cat][d]
            want_score = round(100 * sum(cell["s"]) / len(cell["s"]), 1)
            want_tps = round(sum(cell["t"]) / len(cell["t"]), 1) if cell["t"] else None
            assert got["score"] == want_score, \
                f"FAIL: {cat}×{d} score {got['score']} != {want_score}"
            assert got["n"] == len(cell["s"]), \
                f"FAIL: {cat}×{d} n {got['n']} != {len(cell['s'])}"
            assert got["tps"] == want_tps, \
                f"FAIL: {cat}×{d} tps {got['tps']} != {want_tps}"
            n_cells += 1
    check(n_cells >= 15, f"cell math verified across {n_cells} cells (>=3 tiers × 5 cats)")
    got_cells = sum(len(v) for v in t["cells"].values())
    check(got_cells == n_cells,
          "no extra cells: bogus case ids and off-corpus rows are skipped")
    check(all(cat in suite_mod.CATEGORIES for cat in t["cells"]),
          "cell categories all come from the suite (malformed rows never leak)")
    na_cat = next(c["category"] for c in graded if c["id"] == noans_case)
    na_diff = next(c["difficulty"] for c in graded if c["id"] == noans_case)
    corpus_n = sum(1 for c in graded
                   if c["category"] == na_cat and c["difficulty"] == na_diff)
    if corpus_n > 1:
        check(t["cells"][na_cat][na_diff]["n"] == corpus_n - 1,
              "no_answer row shrinks its cell's n (honest gap, never a fake zero)")

    # ---- BESTPICK: eligible best run wins; ineligible + seeded runs excluded ------------
    b = entry(ex, BESTPICK)
    check(b is not None, "multi-run model appears once")
    flat = [cell["score"] for byd in b["cells"].values() for cell in byd.values()]
    check(flat and all(s == 80.0 for s in flat),
          "cells come from the BEST ELIGIBLE run (0.8) — not the higher self_reported "
          "run (1.0), not the low eligible run (0.4), not the seeded draw")
    check(all(cell["tps"] is None
              for byd in b["cells"].values() for cell in byd.values()),
          "runs without speed data carry tps=null (never a fake number)")

    # ---- stability: computed-on-read, adds nothing ---------------------------------------
    snap1 = json.dumps(scoring.explorer_matrix(), sort_keys=True)
    snap2 = json.dumps(scoring.explorer_matrix(), sort_keys=True)
    check(snap1 == snap2, "explorer payload is bit-identical across reads")

    print(f"\nOK  explorer matrix: {PASSED} checks passed")


if __name__ == "__main__":
    main()
