"""Champion-recipe guard: per (hardware × model) the BEST-tok/s-with-quality perf run wins,
loose hardware matching works, bench wiring + anything token-shaped never leaks into the
public payload, and malformed stored recipes are skipped — never a 500. Also exercises the
pod-side proxy route with a stubbed mothership fetch (no network).

    python test_champion_recipes.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Throwaway SQLite BEFORE aeon.db is imported; pod role BEFORE aeon.app is imported.
os.environ.pop("AEON_DB_URL", None)
os.environ.pop("AEON_POD_TOKEN", None)
os.environ["AEON_ROLE"] = "pod"
_TMP = tempfile.mkdtemp(prefix="aeon-champ-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

HW_SPARK = "single DGX Spark (GB10)"
HW_5090 = "RTX 5090 32GB"
M1 = "lab/model-one"
M2 = "lab/model-two"
M3 = "lab/fast-but-unproven"          # perf only, NO quality run anywhere -> never a champion

_BASE_FLAGS = ["--served-model-name", "model-under-test", "--host", "0.0.0.0",
               "--port", "8000", "--max-model-len", "65536",
               "--gpu-memory-utilization", "0.7", "--max-num-seqs", "24"]


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


def _perf_run(model, hw, peak_tps, *, flags=None, recipe=..., extra_recipe=None):
    rid = uuid.uuid4().hex[:12]
    if recipe is ...:
        recipe = {"engine": "aeon-vllm-ultimate",
                  "image": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
                  "flags": list(_BASE_FLAGS) if flags is None else list(flags)}
        if extra_recipe:
            recipe.update(extra_recipe)
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id="aeon-perf-v1", suite_hash="perf",
                  n_cases=2, params={}, env={"hardware": {"detected_label": hw}},
                  hf_repo=model, trust_tier="attested", recipe=recipe)
    # perf.direct.<scope>.c<N> cells; the champion peak must come from a REAL category cell
    for cid, tps in ((f"perf.direct.Coding.c8", peak_tps),
                     (f"perf.direct.Math.c8", round(peak_tps * 0.5, 1)),
                     (f"perf.direct.overall.c8", round(peak_tps * 10, 1))):  # never the peak source
        db.save_result(rid, cid, category="perf", tier=1, status="scored", score=None,
                       raw_output="", evidence={"agg_decode_tps": tps,
                                                "decode_tps_mean": round(tps / 8, 1)},
                       speed={}, board="perf")
    db.finish_run(rid, "succeeded")
    return rid


def main():
    assert len(suite_mod.CASES) >= 30, "suite corpus unexpectedly small"

    _quality_run(M1, HW_SPARK)        # M1 quality proven on the Spark (+ hw-agnostic fallback)
    _quality_run(M2, HW_5090)         # M2 quality proven on the 5090

    r_slow = _perf_run(M1, HW_SPARK, 100.0)
    # the champion: best tok/s for (Spark, M1) — and it carries a token-shaped flag value plus a
    # secret-named flag that MUST be scrubbed from the public payload, and a local drafter path
    # that must be normalised to /drafter
    r_champ = _perf_run(
        M1, HW_SPARK, 150.0,
        flags=_BASE_FLAGS + [
            "--hf-token", "hf_" + "A" * 24,
            "--kv-cache-dtype", "fp8_e4m3",
            "--speculative-config",
            '{"method":"dflash","model":"/home/aeon/drafters/m1","num_speculative_tokens":6}'],
        extra_recipe={"spec_decode": "dflash", "drafter_repo": "z-lab/model-one-DFlash"})
    r_m1_5090 = _perf_run(M1, HW_5090, 90.0)      # quality via the (canonical, None) fallback
    r_m2 = _perf_run(M2, HW_5090, 300.0)
    _perf_run(M3, HW_SPARK, 999.0)                             # fastest, but no quality -> out
    _perf_run(M2, HW_SPARK, 500.0, recipe={"engine": "vllm"})  # no flags -> not applyable -> out
    _perf_run(M2, HW_SPARK, 400.0, recipe="not-a-recipe-dict")  # malformed -> skipped, no 500

    d = scoring.champion_recipes()
    by = {(c["hardware"], c["canonical"]): c for c in d["champions"]}
    assert set(by) == {(HW_SPARK, M1), (HW_5090, M1), (HW_5090, M2)}, sorted(by)
    assert sorted(d["hardwares"]) == sorted([HW_SPARK, HW_5090])

    champ = by[(HW_SPARK, M1)]
    assert champ["run"] == r_champ and champ["run"] != r_slow, "best-tok/s-with-quality must win"
    assert champ["peak_agg_tps"] == 150.0
    assert champ["peak_agg_cell"] == {"category": "Coding", "conc": 8}, champ["peak_agg_cell"]
    assert champ["quality"] == 100.0 and champ["quality_run"]
    assert champ["engine"] == "aeon-vllm-ultimate" and champ["image"]
    assert by[(HW_5090, M1)]["run"] == r_m1_5090      # hardware-agnostic quality fallback works
    assert by[(HW_5090, M2)]["run"] == r_m2

    # bench wiring stripped; the operator-meaningful tuning knobs kept
    sf = champ["serve_flags"]
    for wired in ("--served-model-name", "--host", "--port"):
        assert wired not in sf, f"{wired} leaked into the champion template"
    assert "--gpu-memory-utilization" in sf and "--kv-cache-dtype" in sf
    # drafter path normalised to the portable /drafter mount; disclosure intact
    spec = json.loads(sf[sf.index("--speculative-config") + 1])
    assert spec["model"] == "/drafter", spec
    assert champ["drafter"] == {"method": "dflash", "repo": "z-lab/model-one-DFlash",
                                "revision": None, "n": 6, "uses_drafter": True}, champ["drafter"]

    # NO token-shaped strings anywhere in the public payload
    blob = json.dumps(d)
    assert not re.search(r"hf_[A-Za-z0-9]{16,}", blob), "hf token leaked into champion payload"
    assert "--hf-token" not in blob

    # loose hardware match: 'dgx spark' finds 'single DGX Spark (GB10)'
    loose = scoring.champion_recipes(hardware="dgx spark")
    assert {c["hardware"] for c in loose["champions"]} == {HW_SPARK}
    assert [c["run"] for c in loose["champions"]] == [r_champ]
    exact = scoring.champion_recipes(hardware=HW_5090)
    assert {(c["hardware"], c["canonical"]) for c in exact["champions"]} == \
        {(HW_5090, M1), (HW_5090, M2)}
    # per-hardware list is the top list: best tok/s first
    assert [c["canonical"] for c in exact["champions"]] == [M2, M1]
    assert scoring.champion_recipes(hardware="TPU v7")["champions"] == []

    # model filter
    m = scoring.champion_recipes(model=M2)
    assert m["champions"] and all(c["canonical"] == M2 for c in m["champions"])

    # ---- method-aware speculative disclosure (_drafter_info / _champion_drafter) --------------
    from aeon import app as app_mod
    # native MTP parsed from --speculative-config: no drafter to pull, mount, or advertise
    mtp = app_mod._drafter_info({"flags": [
        "--speculative-config", '{"method":"mtp","num_speculative_tokens":2}']})
    assert mtp == {"method": "mtp", "repo": None, "revision": None, "n": 2,
                   "uses_drafter": False}, mtp
    # inline '--speculative-config=JSON' form on the recorded command parses too
    qmtp = app_mod._drafter_info({"command": [
        '--speculative-config={"method":"qwen3_next_mtp","num_speculative_tokens":3}']})
    assert qmtp["method"] == "qwen3_next_mtp" and qmtp["n"] == 3 \
        and qmtp["uses_drafter"] is False, qmtp
    # DFlash targeting the /drafter mount uses a drafter even without top-level recipe fields
    dfl = app_mod._drafter_info({"flags": [
        "--speculative-config", '{"method":"dflash","model":"/drafter","num_speculative_tokens":6}']})
    assert dfl["method"] == "dflash" and dfl["n"] == 6 and dfl["uses_drafter"] is True, dfl
    # no speculative config anywhere -> plain decode, no disclosure
    assert app_mod._drafter_info({"flags": ["--port", "8000"]}) is None
    assert app_mod._drafter_info(None) is None
    # replication command: MTP names the method honestly and never mounts /drafter …
    mtp_cmd = app_mod._docker_cmd({"engine": "vllm", "flags": [
        "--max-model-len", "65536",
        "--speculative-config", '{"method":"mtp","num_speculative_tokens":2}']}, "org/model", None)
    assert "Native MTP spec-decode: mtp" in mtp_cmd and "n=2" in mtp_cmd, mtp_cmd
    assert "/drafter" not in mtp_cmd, mtp_cmd
    # … while DFlash keeps the drafter pull + /drafter mount + repo disclosure
    dfl_cmd = app_mod._docker_cmd({"engine": "vllm", "spec_decode": "dflash",
                                   "drafter_repo": "z-lab/model-one-DFlash", "flags": [
        "--speculative-config",
        '{"method":"dflash","model":"/home/aeon/drafters/m1","num_speculative_tokens":6}']},
        "org/model", None)
    assert "-v ./drafter:/drafter" in dfl_cmd, dfl_cmd
    assert "DFlash spec-decode: z-lab/model-one-DFlash" in dfl_cmd, dfl_cmd
    assert "/home/aeon/drafters/m1" not in dfl_cmd, dfl_cmd     # local path never leaks
    # _champion_drafter mirrors: an mtp champion must not advertise a drafter repo
    cd = scoring._champion_drafter({"flags": [
        "--speculative-config", '{"method":"mtp","num_speculative_tokens":4}']})
    assert cd == {"method": "mtp", "repo": None, "revision": None, "n": 4,
                  "uses_drafter": False}, cd
    # …and the pod-side producer records the method into the recipe from the FINAL flags
    from pod import modelhost
    rec = modelhost.normalize_dflash_spec({"flags": [
        "--speculative-config", '{"method":"qwen3_next_mtp","num_speculative_tokens":2}']})
    assert rec["spec_decode"] == "qwen3_next_mtp" and rec["spec_decode_method"] == "qwen3_next_mtp"
    assert rec["spec_decode_n"] == 2 and "native MTP" in rec["spec_decode_note"], rec
    assert app_mod._drafter_info(rec)["uses_drafter"] is False
    assert "spec_decode" not in modelhost.normalize_dflash_spec({"flags": ["--port", "8000"]})

    # ---- pod proxy route: stubbed mothership fetch (never the network) ------------------------
    calls = {}

    def _stub(base, hw):
        calls["base"], calls["hw"] = base, hw
        return {"champions": [{"model": M1, "hardware": HW_SPARK}], "hardwares": [HW_SPARK]}

    real = app_mod._fetch_champions
    try:
        app_mod._fetch_champions = _stub
        r = app_mod.pod_champion_recipes(None)      # no pod token configured -> request unused
        assert r["available"] is True and r["champions"] == [{"model": M1, "hardware": HW_SPARK}]
        assert calls["base"], "mothership URL was not passed to the fetch"

        def _boom(base, hw):
            raise OSError("no route to mothership")
        app_mod._fetch_champions = _boom
        r = app_mod.pod_champion_recipes(None)      # offline: degrade, never break the Run tab
        assert r["available"] is False and r["reason"]
    finally:
        app_mod._fetch_champions = real

    print("OK  champion recipes: best-with-quality wins per (hardware × model), loose hw match, "
          "wiring/secrets scrubbed, malformed recipes skipped, offline proxy degrades gracefully")


if __name__ == "__main__":
    main()
