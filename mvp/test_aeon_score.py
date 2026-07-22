"""Global-leaderboard dials + AEON score + audio board + no_answer weighting guard.

Covers the scoring/data side of the Global Leaderboard redesign:
  * dials object per model row (intelligence always; perf/agentic/vision/audio/video
    null = not tested, never zero)
  * aeon_score blend (0.5/0.3/0.2), renormalized over present components,
    aeon_provisional flag when agentic/performance are missing
  * performance percentile ranked WITHIN the hw bucket (single-row bucket = 100)
  * audio_leaderboard surfaces board='audio' runs (the lost Gemma audio data),
    joined to the text board by CANONICAL id (lowercased hf_repo), not the alias
  * best_intelligence_run = the highest-composite ELIGIBLE text run
  * no_answer rows: category mean = sum(scores)/(n_answered + 0.25*n_no_answer),
    they count toward the coverage floor, and runs WITHOUT such rows are
    bit-identical unchanged

    python test_aeon_score.py
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
_TMP = tempfile.mkdtemp(prefix="aeon-score-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import audio_suite, db, hwnorm, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

FULL = "lab/full-model"              # intelligence + agentic + performance (+ audio)
IA = "lab/intel-agentic-model"       # intelligence + agentic (no perf) -> reweighted
TEXT_ONLY = "lab/text-only-model"    # intelligence only -> every other dial null
BEST = "lab/best-run-model"          # 2 eligible runs + 1 higher self_reported run
SLOW = "lab/slow-spark-model"        # perf cohort filler (Spark bucket)
MID = "lab/mid-spark-model"          # perf cohort filler (Spark bucket)
LONER = "lab/single-bucket-model"    # only row in its bucket -> percentile 100
NOANS = "lab/quarter-weight-model"   # no_answer scoring math
COVER = "lab/noans-coverage-model"   # scored < floor but scored+no_answer >= floor
OLD = "lab/old-plain-run-model"      # invariance: no no_answer rows anywhere

GEMMA_HF = "AEON-7/Gemma-Test-12B"   # audio ran under an alias; text under another

HW_SPARK = "single DGX Spark (GB10)"
HW_5090 = "RTX 5090 32GB"

_N = 0


def _ts():
    global _N
    _N += 1
    return 1_700_000_000 + _N * 100


def _pin(rid, started_at):
    with db.connect() as c:
        c.execute("UPDATE runs SET started_at=? WHERE id=?", (started_at, rid))


def _text_run(model, *, score=1.0, tier="attested", hf_repo=None, no_answer_ids=(),
              scores_by_id=None):
    """A comprehensive text pass. `no_answer_ids` rows land as status='no_answer'
    (score NULL); `scores_by_id` overrides the uniform score per case."""
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(suite_mod.CASES),
                  params={}, env={}, hf_repo=hf_repo or model, trust_tier=tier)
    for c in suite_mod.CASES:
        if c["id"] in no_answer_ids:
            db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                           status="no_answer", score=None, raw_output="",
                           evidence={}, speed={})
        else:
            s = (scores_by_id or {}).get(c["id"], score)
            db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                           status="scored", score=s, raw_output="ok",
                           evidence={}, speed={})
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


def _harness_run(model, harness, scores, *, hf_repo=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(scores),
                  params={}, env={}, hf_repo=hf_repo or model, trust_tier="attested",
                  harness=harness, harness_version="1.0.0")
    for i, s in enumerate(scores):
        db.save_result(rid, f"case-{i}", category="Agentic", tier=1, status="scored",
                       score=s, raw_output="ok", evidence={}, speed={})
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


def _perf_run(model, hw, peak_tps, *, hf_repo=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id="aeon-perf-v1", suite_hash="perf",
                  n_cases=2, params={}, env={"hardware": {"detected_label": hw}},
                  hf_repo=hf_repo or model, trust_tier="attested",
                  recipe={"engine": "vllm", "flags": ["--max-model-len", "8192"]})
    for cid, tps in (("perf.direct.Coding.c8", peak_tps),
                     ("perf.direct.Math.c8", round(peak_tps * 0.5, 1))):
        db.save_result(rid, cid, category="perf", tier=1, status="scored", score=None,
                       raw_output="", evidence={"agg_decode_tps": tps,
                                                "decode_tps_mean": round(tps / 8, 1)},
                       speed={}, board="perf")
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


def _audio_run(model, *, score=1.0, hf_repo=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=audio_suite.SUITE_ID,
                  suite_hash="audio-hash", n_cases=len(audio_suite.CASES),
                  params={}, env={}, board="audio",
                  hf_repo=hf_repo or model, trust_tier="attested")
    for c in audio_suite.CASES:
        db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 0),
                       status="scored", score=score, raw_output="ok",
                       evidence={}, speed={"ttft_after_audio_ms": 42.0, "decode_tps": 10.0},
                       board="audio")
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


def row(board, canonical):
    for m in board["models"]:
        if m["canonical"] == canonical:
            return m
    return None


def main():
    cases = suite_mod.CASES
    assert len(cases) >= 30, "suite corpus unexpectedly small"
    cat0 = cases[0]["category"]
    cat0_ids = [c["id"] for c in cases if c["category"] == cat0]

    # ---- fixture: the full cast ---------------------------------------------------------
    _text_run(FULL, score=1.0)
    _harness_run(FULL, "hermes", [0.8])
    _harness_run(FULL, "opencode", [0.6])
    _perf_run(FULL, HW_SPARK, 100.0)          # fastest of 3 in the Spark bucket -> 100
    _perf_run(MID, HW_SPARK, 60.0)            # middle of 3 -> 50
    _perf_run(SLOW, HW_SPARK, 30.0)           # slowest of 3 -> 0
    _perf_run(LONER, HW_5090, 20.0)           # only row in its bucket -> 100
    _audio_run(FULL, score=0.5)

    _text_run(IA, score=1.0)
    _harness_run(IA, "hermes", [0.6])

    _text_run(TEXT_ONLY, score=0.5)

    r_low = _text_run(BEST, score=0.6)
    r_high = _text_run(BEST, score=0.8)
    r_selfrep = _text_run(BEST, score=1.0, tier="self_reported")

    # Gemma-style split identity: audio benched under one alias, text under another,
    # both declaring the same HF repo -> ONE canonical row with the audio dial lit.
    _text_run("gemma-local-text", score=1.0, hf_repo=GEMMA_HF)
    gemma_audio = _audio_run("model-under-test", score=1.0, hf_repo=GEMMA_HF)
    gemma_canon = GEMMA_HF.lower()

    lb = scoring.leaderboard()
    spark_bucket = hwnorm.normalize_label(HW_SPARK)["bucket"]

    # ---- audio board surfaces the audio runs (the lost-data bug) -------------------------
    ab = scoring.audio_leaderboard()
    check(ab["categories"] == audio_suite.CATEGORIES_AUDIO,
          "audio board reports the audio suite categories")
    g = row(ab, gemma_canon)
    check(g is not None, "audio run surfaces on the audio leaderboard")
    check(g["run"] == gemma_audio, "audio board row points at the audio run")
    check(g["composite"] == 100.0, "audio composite = category mean of the scored cases")
    check(g["hf_repo"] == GEMMA_HF and g["model"] == "model-under-test",
          "audio row keeps the declared alias AND carries the canonical hf_repo")
    check(row(ab, FULL)["composite"] == 50.0, "second audio model scored independently")

    # ---- dials: full model --------------------------------------------------------------
    f = row(lb, FULL)
    d = f["dials"]
    check(d["intelligence"] == {"score": 100.0, "run": f["best_run"]},
          "intelligence dial = text composite + best run")
    check(d["agentic"] == {"score": 70.0, "harnesses": {"hermes": 80.0, "opencode": 60.0}},
          "agentic dial = mean of available harness scores")
    check(d["performance"] == {"score": 100.0, "peak_agg_tps": 100.0, "hw": spark_bucket,
                               "conc": 8},
          "performance dial = top percentile in its hw bucket (+ the peak cell's concurrency)")
    check(d["audio"] == {"score": 50.0, "run": row(ab, FULL)["run"]},
          "audio dial joined from the audio board")
    check(d["vision"] is None and d["video"] is None,
          "untested modality dials are null (never zero)")
    check(f["aeon_score"] == round(0.5 * 100.0 + 0.3 * 70.0 + 0.2 * 100.0, 1),
          "aeon_score = 0.5*intelligence + 0.3*agentic + 0.2*performance")
    check(f["aeon_score_parts"] == {"intelligence": 0.5, "agentic": 0.3, "performance": 0.2},
          "full model uses the native weights")
    check(f["aeon_provisional"] is False, "all three components present -> not provisional")
    # COMPLETENESS GATE: the FULL run (intelligence+agentic+performance) is the only shape that RANKS
    check(f["record_eligible"] is True and f.get("ranked_excluded") is None,
          "a full (non-provisional) attested run RANKS")

    # ---- perf percentile within bucket ----------------------------------------------------
    for canon, want in ((MID, 50.0), (SLOW, 0.0), (LONER, 100.0)):
        p = scoring._perf_percentile_index()[canon]
        check(p["score"] == want, f"{canon} perf percentile within its bucket = {want}")
    check(scoring._perf_percentile_index()[LONER]["hw"] ==
          hwnorm.normalize_label(HW_5090)["bucket"],
          "single-row bucket keeps its own hw bucket label")

    # ---- reweighting + provisional --------------------------------------------------------
    ia = row(lb, IA)
    check(ia["dials"]["performance"] is None, "no perf run -> performance dial null")
    check(ia["aeon_score_parts"] == {"intelligence": 0.625, "agentic": 0.375,
                                     "performance": 0.0},
          "missing perf -> weights renormalized over present components")
    check(ia["aeon_score"] == round(0.625 * 100.0 + 0.375 * 60.0, 1),
          "reweighted blend math (i=100, a=60 -> 85.0)")
    check(ia["aeon_provisional"] is True, "missing performance -> provisional")
    check(ia["record_eligible"] is False and ia.get("ranked_excluded") == "incomplete",
          "an attested-but-provisional run (missing performance) is NOT counted — local only")

    t = row(lb, TEXT_ONLY)
    check(t["aeon_score_parts"] == {"intelligence": 1.0, "agentic": 0.0, "performance": 0.0},
          "intelligence-only model carries full weight on intelligence")
    check(t["aeon_score"] == t["composite"] == 50.0,
          "intelligence-only aeon_score = the text composite")
    check(t["aeon_provisional"] is True, "intelligence-only -> provisional")
    check(all(t["dials"][k] is None
              for k in ("performance", "agentic", "vision", "audio", "video")),
          "every untested dial is null on the text-only model")

    # ---- best_intelligence_run = highest-composite ELIGIBLE run ---------------------------
    b = row(lb, BEST)
    check(b["best_intelligence_run"] == r_high,
          "best_intelligence_run is the higher eligible run")
    check(b["best_intelligence_run"] != r_selfrep,
          "a higher self_reported run never becomes best_intelligence_run")
    check(b["dials"]["intelligence"]["run"] == r_high and b["best_run"] == r_high,
          "intelligence dial run agrees with best_run")
    check(b["best_intelligence_run"] != r_low, "the lower eligible run is not the best")

    # ---- canonical join across boards (the Gemma case) ------------------------------------
    gm = row(lb, gemma_canon)
    check(gm is not None and gm["dials"]["audio"] == {"score": 100.0, "run": gemma_audio},
          "audio benched under an alias joins its text row via the canonical id")

    # ---- backward compat: every pre-existing board field survives -------------------------
    for k in ("model", "canonical", "hf_repo", "verified", "trust_tier", "record_eligible",
              "n_runs", "composite", "best", "worst", "best_run", "worst_run", "latest_run",
              "categories", "category_speed", "creativity", "avg_ttft_ms", "avg_decode_tps",
              "avg_e2e_ms", "agg_tps", "n_cases", "vram_est_gb", "tags", "runs"):
        assert k in f, f"pre-existing leaderboard field lost: {k}"
    check(True, "every pre-existing leaderboard field is still present")

    # ---- no_answer: quarter-weight category math -------------------------------------------
    k = 4
    _text_run(NOANS, score=1.0, no_answer_ids=set(cat0_ids[:k]))
    lb2 = scoring.leaderboard()
    n_ans = len(cat0_ids) - k
    want = round(100 * n_ans / (n_ans + 0.25 * k), 1)
    nm = row(lb2, NOANS)
    check(nm is not None, "run with a few no_answer rows still ranks (attempted = covered)")
    check(nm["categories"][cat0] == want,
          f"no_answer category mean = sum/(n_answered + 0.25*n_no_answer) = {want}")
    other = [c for c in nm["categories"] if c != cat0]
    check(all(nm["categories"][c] == 100.0 for c in other),
          "categories without no_answer rows are untouched")

    # ---- no_answer: coverage counting --------------------------------------------------------
    floor = scoring.MIN_SUITE_COVERAGE * len(cases)
    # smallest no_answer count that drops scored-only coverage below the floor while
    # scored+no_answer still clears it (16 on the 155-case v3 suite, 17 on 160-case v4)
    n_na = int(len(cases) - floor) + 1
    assert len(cases) - n_na < floor <= len(cases), "coverage fixture math off"
    _text_run(COVER, score=1.0, no_answer_ids={c["id"] for c in cases[:n_na]})
    lb3 = scoring.leaderboard()
    cm = row(lb3, COVER)
    check(cm is not None,
          "scored < floor but scored+no_answer >= floor -> the run RANKS")
    check(COVER in {kk[0] for kk in scoring._quality_index()},
          "quality index (perf-board join) applies the same attempted-counts-floor")

    # ---- old-run invariance: no no_answer rows anywhere -> plain-mean math, stable payload --
    per_case = {c["id"]: (0.25 if i % 3 == 0 else 1.0) for i, c in enumerate(cases)}
    _text_run(OLD, scores_by_id=per_case)
    lb4 = scoring.leaderboard()
    om = row(lb4, OLD)
    by_cat = {}
    for c in cases:
        by_cat.setdefault(c["category"], []).append(per_case[c["id"]])
    expect_cats = {cc: round(100 * sum(v) / len(v), 1) for cc, v in by_cat.items()}
    check(om["categories"] == expect_cats,
          "run without no_answer rows scores by the ORIGINAL plain-mean formula")
    check(om["composite"] == round(sum(expect_cats.values()) / len(expect_cats), 1),
          "composite of the plain run matches the original math")
    snap1 = json.dumps(scoring.leaderboard(), sort_keys=True)
    snap2 = json.dumps(scoring.leaderboard(), sort_keys=True)
    check(snap1 == snap2, "leaderboard snapshot is bit-identical across reads (add nothing)")

    print(f"\nOK  aeon score dials: {PASSED} checks passed")


if __name__ == "__main__":
    main()
