"""Perf-board hardware clustering guard: rows key per (canonical model × hw bucket) — a model
benched on two rigs holds two rows, each the LATEST run on that rig — hardware_groups come
back Spark-first (ascending node count) then best-peak-desc with Unlabeled last, every
pre-clustering payload field survives, and champion queries match normalized buckets.

    python test_perf_hw_board.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Throwaway SQLite BEFORE aeon.db is imported — never a prod database.
os.environ.pop("AEON_DB_URL", None)
os.environ["AEON_ROLE"] = "pod"
_TMP = tempfile.mkdtemp(prefix="aeon-hwboard-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

HW_SPARK1 = "single DGX Spark (GB10)"
HW_SPARK2 = "dual DGX Spark (GB10)"
HW_5090 = "RTX 5090 32GB"
HW_5090_2X = "2× RTX 5090 32GB"
HW_CPU = "aarch64 (CPU)"          # the known mislabel era -> Unlabeled bucket, honestly

M1 = "lab/model-one"
M2 = "lab/model-two"
M3 = "lab/mystery-rig"

_FLAGS = ["--max-model-len", "65536", "--gpu-memory-utilization", "0.7"]


def _quality_run(model, hw):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(suite_mod.CASES),
                  params={}, env={"hardware": {"detected_label": hw}}, hf_repo=model,
                  trust_tier="attested")
    for c in suite_mod.CASES:
        db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                       status="scored", score=1.0, raw_output="ok", evidence={}, speed={})
    db.finish_run(rid, "succeeded")
    return rid


def _perf_run(model, hw, peak_tps, *, started_at=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id="aeon-perf-v1", suite_hash="perf",
                  n_cases=2, params={}, env={"hardware": {"detected_label": hw}},
                  hf_repo=model, trust_tier="attested",
                  recipe={"engine": "aeon-vllm-ultimate", "image": "ghcr.io/x/y:latest",
                          "flags": list(_FLAGS)})
    for cid, tps in (("perf.direct.Coding.c8", peak_tps),
                     ("perf.direct.Math.c8", round(peak_tps * 0.5, 1))):
        db.save_result(rid, cid, category="perf", tier=1, status="scored", score=None,
                       raw_output="", evidence={"agg_decode_tps": tps,
                                                "decode_tps_mean": round(tps / 8, 1)},
                       speed={}, board="perf")
    db.finish_run(rid, "succeeded")
    if started_at is not None:            # deterministic recency — never a same-second tie
        with db.connect() as c:
            c.execute("UPDATE runs SET started_at=? WHERE id=?", (started_at, rid))
    return rid


def main():
    _quality_run(M1, HW_SPARK1)
    _quality_run(M2, HW_5090_2X)

    t0 = 1_700_000_000
    r_m1_spark_old = _perf_run(M1, HW_SPARK1, 100.0, started_at=t0)
    r_m1_spark_new = _perf_run(M1, HW_SPARK1, 150.0, started_at=t0 + 100)
    r_m1_rtx = _perf_run(M1, HW_5090, 300.0, started_at=t0 + 200)
    r_m2_rtx2 = _perf_run(M2, HW_5090_2X, 200.0, started_at=t0 + 300)
    r_m2_dual = _perf_run(M2, HW_SPARK2, 500.0, started_at=t0 + 400)
    r_m3_cpu = _perf_run(M3, HW_CPU, 50.0, started_at=t0 + 500)

    d = scoring.perf_board()

    # ---- rows: one per (canonical, bucket); the LATEST run wins WITHIN a bucket only ---------
    rows = {(m["canonical"], m["hw_bucket"]): m for m in d["models"]}
    assert len(d["models"]) == len(rows) == 5, sorted(rows)
    assert set(rows) == {(M1, "Single DGX Spark"), (M1, "NVIDIA RTX 5090"),
                         (M2, "NVIDIA RTX 5090"), (M2, "2× DGX Spark"),
                         (M3, "Unlabeled")}, sorted(rows)
    m1s = rows[(M1, "Single DGX Spark")]
    assert m1s["run"] == r_m1_spark_new and m1s["run"] != r_m1_spark_old
    assert m1s["peak_agg_tps"] == 150.0
    assert rows[(M1, "NVIDIA RTX 5090")]["run"] == r_m1_rtx      # the second rig KEEPS its row
    assert rows[(M2, "NVIDIA RTX 5090")]["run"] == r_m2_rtx2
    assert rows[(M3, "Unlabeled")]["run"] == r_m3_cpu

    # bucket identity fields + the verbatim row label
    assert m1s["hw_family"] == "dgx-spark" and m1s["spark_count"] == 1
    assert m1s["hardware"] == HW_SPARK1
    m2r = rows[(M2, "NVIDIA RTX 5090")]
    assert m2r["hardware"] == HW_5090_2X                  # multi-GPU visible on the row
    assert m2r["hw_family"] == "nvidia-rtx" and m2r["spark_count"] is None
    assert rows[(M3, "Unlabeled")]["hw_family"] == "unlabeled"

    # ---- backward compatibility: every pre-clustering field intact ---------------------------
    assert d["categories"] == suite_mod.CATEGORIES
    assert d["hardwares"] == sorted({HW_SPARK1, HW_SPARK2, HW_5090, HW_5090_2X, HW_CPU})
    for m in d["models"]:
        for key in ("model", "canonical", "hf_repo", "hf_revision", "verified", "trust_tier",
                    "run", "started_at", "hardware", "conc_levels", "peak_agg_tps",
                    "peak_agg_cell", "peak_single_tps", "latency", "quality", "quality_run",
                    "recipe", "direct", "harness"):
            assert key in m, (key, sorted(m))
    peaks = [m["peak_agg_tps"] or 0 for m in d["models"]]
    assert peaks == sorted(peaks, reverse=True)           # global peak-desc sort unchanged
    assert m1s["quality"] == 100.0                        # quality join survives the rekeying

    # ---- hardware_groups: Sparks ascending, then best-peak desc, Unlabeled last --------------
    gs = d["hardware_groups"]
    assert [g["bucket"] for g in gs] == \
        ["Single DGX Spark", "2× DGX Spark", "NVIDIA RTX 5090", "Unlabeled"], \
        [g["bucket"] for g in gs]                         # dual-Spark 500 beats RTX 300, yet
    #                                                       Spark buckets ALWAYS lead ascending
    by_bucket = {g["bucket"]: g for g in gs}
    rtx = by_bucket["NVIDIA RTX 5090"]
    assert rtx["n_models"] == 2 and rtx["family"] == "nvidia-rtx"
    assert rtx["best"] == {"model": M1, "peak_agg_tps": 300.0}
    assert by_bucket["Single DGX Spark"]["n_models"] == 1
    assert by_bucket["Single DGX Spark"]["best"]["peak_agg_tps"] == 150.0
    assert by_bucket["2× DGX Spark"]["spark_count"] == 2
    assert by_bucket["Unlabeled"]["family"] == "unlabeled"
    for g in gs:
        assert set(g) == {"bucket", "family", "label", "spark_count", "n_models", "best"}, g

    # ---- champion bucket matching -------------------------------------------------------------
    d2 = scoring.champion_recipes()
    assert all("hw_bucket" in ch for ch in d2["champions"])

    one = scoring.champion_recipes(hardware="Single DGX Spark")   # canonical bucket name
    assert [(ch["hardware"], ch["run"]) for ch in one["champions"]] == \
        [(HW_SPARK1, r_m1_spark_new)]

    # a bucket name whose raw labels never contain it — only normalized matching finds these
    rtxq = scoring.champion_recipes(hardware="NVIDIA RTX 5090")
    assert {ch["run"] for ch in rtxq["champions"]} == {r_m1_rtx, r_m2_rtx2}, rtxq["champions"]

    loose = scoring.champion_recipes(hardware="dgx spark")        # legacy containment: ALL Sparks
    assert {ch["hardware"] for ch in loose["champions"]} == {HW_SPARK1, HW_SPARK2}

    dual = scoring.champion_recipes(hardware="2x dgx spark")      # query normalizes to 2× bucket
    assert [ch["run"] for ch in dual["champions"]] == [r_m2_dual]

    exact = scoring.champion_recipes(hardware=HW_SPARK1)          # exact raw label still exact
    assert [ch["run"] for ch in exact["champions"]] == [r_m1_spark_new]

    assert scoring.champion_recipes(hardware="TPU v7")["champions"] == []   # junk never sweeps
    #                                                                         up Unlabeled rows

    print("OK  perf hw board: (model × bucket) rows w/ latest-per-rig, groups ordered "
          "Spark > peak > Unlabeled, payload backward compatible, champion bucket matching")


if __name__ == "__main__":
    main()
