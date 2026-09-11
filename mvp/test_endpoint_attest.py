"""Endpoint attestation, tested from BOTH sides of the pod/mothership boundary.

POD HALF (runs in EVERY repo, including this public one): the evidence the pod actually emits —
pod/fingerprint.evidence(), pod/endpoints.serving_integrity() — carries the exact keys the
mothership's gate reads, and pod/aeon_pod.run_controlled stamps and attaches them where the gate
looks for them. If any of that drifts, every endpoint run silently stops ranking; nothing else in
the pod repo asserts it.

MOTHERSHIP HALF (runs only where aeon/ingest.py exists — it is deliberately never published to the
pod repo): the tier decision itself. Verified weights + a MATCHED fingerprint whose reference is
those exact weights earns attested; every weaker shape — unmatched, a reference against a DIFFERENT
model's weights, unverified weights — stays self_reported. Guarded, never silent: without
aeon.ingest the file says exactly which half did not run and where to run it, and the pod half
still fails loudly on its own.
"""
import inspect
import json as _json
import os
import re
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

try:                                       # mothership-only module, absent from the pod repo
    from aeon import ingest                # noqa: E402
except ImportError:                        # pragma: no cover - depends on which repo this is
    ingest = None

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


WH = "a1b2c3weightshash"
BASE = {"weights_hash": WH, "recipe": {"engine": "vllm", "flags": []}}
# an endpoint run: the pod hash-verified the weights but benched a serve it did NOT launch
EP = {"weights_hash": WH, "recipe": {"engine": "vllm", "flags": [], "serve_mode": "endpoint"}}


def fp_ev(match, *, wh=WH, status=None):
    """The gate-relevant subset of a pod fingerprint record (faithfulness asserted below)."""
    return {"method": "logprob-fingerprint", "match": match,
            "status": status or ("match" if match else "mismatch"), "weights_hash": wh}


def si_ev(ok, verified, *, wh=WH):
    """The gate-relevant subset of a pod serving-integrity record."""
    e = {"ok": ok, "weights_verified": verified, "status": "match" if ok else "mismatch"}
    if ok and verified and wh:
        e["weights_hash"] = wh
    return e


# =================================================================================================
# POD HALF — the producer of the evidence. Runs everywhere.
# =================================================================================================
from pod import aeon_pod as _ap           # noqa: E402
from pod import endpoints as _ep          # noqa: E402
from pod import fingerprint as _fp        # noqa: E402

_REF, _PRB = {"n_ok": 8}, {"n_ok": 8}
_CMP_OK = {"match": True, "status": "match", "token_agreement": 1.0,
           "logprob_divergence": 0.0, "n_compared": 8}
_CMP_BAD = {"match": False, "status": "mismatch", "token_agreement": 0.41,
            "logprob_divergence": 1.93, "n_compared": 8}

_ev = _fp.evidence(_REF, _PRB, _CMP_OK, weights_hash=WH, ref_source="pod-local-weights")
check(_ev.get("match") is True and _ev.get("status") == "match",
      "pod: a matching fingerprint emits match=True AND status='match' — the gate demands both, "
      "so neither may be dropped or renamed")
check(_ev.get("weights_hash") == WH,
      "pod: the fingerprint record carries the VERIFIED weights_hash its reference came from — "
      "without it the mothership cannot tie the probe to this bundle and the run never ranks")
check(set(fp_ev(True)) <= set(_ev),
      "pod: the emitted fingerprint record is a superset of the keys the tier gate reads "
      "(so the fixtures the mothership half uses below are faithful, not fiction)")

_ev = _fp.evidence(_REF, _PRB, _CMP_BAD, weights_hash=WH, ref_source="pod-local-weights")
check(_ev.get("match") is False and _ev.get("status") != "match",
      "pod: a diverging fingerprint emits an unambiguous non-match — never a silent pass")

# An uninspectable serve must not manufacture attestation evidence. Offline and instant: an
# unreachable, non-local host with no docker/ssh authorized returns before any I/O. (The docker-
# backed COMPLETE-match and partial-hash paths — where the weights_hash echo is asserted — are
# covered behaviourally in pod/test_serving_integrity.py; not duplicated here.)
_si = _ep.serving_integrity("http://10.255.255.1:8000/v1", ["m"],
                            ref={"repo": "AEON-7/x"}, local_dir=None)
check(_si.get("status") == "unavailable" and not _si.get("weights_verified")
      and "weights_hash" not in _si,
      "pod: a serve it cannot inspect reports 'unavailable' and echoes NO weights_hash — only a "
      "COMPLETE container-hash match may echo it, which is what endpoint_verified rides on")

# WIRING: three stamps in run_controlled decide whether an endpoint run can ever be attested.
# Textual guards (the surrounding code needs a live serve + GPU to execute), but they catch the
# rename/refactor that would otherwise silently unrank every endpoint run.
_SRC = inspect.getsource(_ap.run_controlled)


def _wired(pattern, what):
    check(re.search(pattern, _SRC) is not None, "pod: run_controlled " + what)


_wired(r"""recipe\[\s*['"]serve_mode['"]\s*\]\s*=\s*['"]endpoint['"]""",
       "stamps recipe.serve_mode='endpoint' for a serve it did not launch — drop that marker and "
       "an UNPROVEN endpoint run wears the hf_pull_served badge it has not earned")
_wired(r"""provenance\[\s*['"]endpoint_fingerprint['"]\s*\]\s*=""",
       "attaches endpoint_fingerprint TOP-LEVEL on provenance — the gate reads "
       "bundle['endpoint_fingerprint'], never the nested deployment_manifest copy")
_wired(r"""provenance\[\s*['"]serving_integrity['"]\s*\]\s*=""",
       "attaches serving_integrity TOP-LEVEL on provenance — endpoint_verified rides on it")

# =================================================================================================
# MOTHERSHIP HALF — the tier decision (aeon/ingest.py). Skipped, loudly, where it does not exist.
# =================================================================================================
if ingest is not None:
    # ---- served-by-pod attested (no fingerprint) --------------------------------------------
    t = ingest._trust_tier(dict(BASE), "verified")
    check(t == "attested", "verified weights, pod-served -> attested")
    check(ingest._attestation_method(dict(BASE), t) == "hf_pull_served", "method = hf_pull_served")

    # ---- endpoint fingerprint MATCHED, reference == the verified weights -> attested ---------
    b = {**BASE, "endpoint_fingerprint": fp_ev(True)}
    t = ingest._trust_tier(b, "verified")
    check(t == "attested", "verified weights + matched fingerprint of those weights -> attested (ranks)")
    check(ingest._attestation_method(b, t) == "endpoint_fingerprint", "method = endpoint_fingerprint")

    # ---- fingerprint MISMATCH -> self_reported ------------------------------------------------
    b = {**BASE, "endpoint_fingerprint": fp_ev(False)}
    check(ingest._trust_tier(b, "verified") == "self_reported",
          "a mismatched endpoint fingerprint never ranks")

    # ---- fingerprint 'match' flag but status not 'match' (tampered) -> self_reported ----------
    b = {**BASE, "endpoint_fingerprint": fp_ev(True, status="unverifiable")}
    check(ingest._trust_tier(b, "verified") == "self_reported",
          "match=True with a non-'match' status is rejected (both must agree)")

    # ---- fingerprint reference is a DIFFERENT model's weights -> self_reported ----------------
    b = {**BASE, "endpoint_fingerprint": fp_ev(True, wh="DIFFERENT_model_hash")}
    check(ingest._trust_tier(b, "verified") == "self_reported",
          "fingerprint matched against another model's reference is rejected (weights_hash must equal)")

    # ---- unverified weights: fingerprint cannot rescue it -------------------------------------
    b = {**BASE, "endpoint_fingerprint": fp_ev(True)}
    check(ingest._trust_tier(b, "claim") == "self_reported",
          "unverified weights stay self_reported even with a matched fingerprint")
    check(ingest._trust_tier({"endpoint_fingerprint": fp_ev(True)}, "verified") == "self_reported",
          "no weights_hash/recipe -> self_reported regardless of fingerprint")
    check(ingest._attestation_method(b, "self_reported") is None,
          "no attestation method when not attested")

    # ---- ENDPOINT RUN WITH NO BINDING EVIDENCE MUST NOT RANK ----------------------------------
    # The pod hash-verified the weights but never served them, and neither fingerprinted the
    # endpoint nor container-hash-verified it. With no binding evidence the run stays local — and
    # must never wear the hf_pull_served badge, whose contract is literally "served THEM. No gap."
    t = ingest._trust_tier(dict(EP), "verified")
    check(t == "self_reported",
          "endpoint run with NO fingerprint -> self_reported (weights verified, endpoint unproven)")
    check(ingest._attestation_method(dict(EP), t) is None,
          "an unproven endpoint run claims no attestation method (never hf_pull_served)")

    # the same run WITH a matched fingerprint is the whole point — it ranks
    epm = {**EP, "endpoint_fingerprint": fp_ev(True)}
    t = ingest._trust_tier(epm, "verified")
    check(t == "attested", "endpoint run + MATCHED fingerprint -> attested (the gap is closed)")
    check(ingest._attestation_method(epm, t) == "endpoint_fingerprint",
          "a proven endpoint run is badged endpoint_fingerprint, not hf_pull_served")

    # a mismatched fingerprint on an endpoint run is still self_reported (unchanged, guarded)
    check(ingest._trust_tier({**EP, "endpoint_fingerprint": fp_ev(False)}, "verified") == "self_reported",
          "endpoint run + MISMATCHED fingerprint -> self_reported")

    # ---- ENDPOINT RUN + CONTAINER-HASH VERIFICATION -> attested (endpoint_verified) -----------
    # No behavioral fingerprint, but the pod hashed the RUNNING container's weight files and every
    # one matched HF, tied to weights_hash. The owner's call: this ranks as 'endpoint_verified'.
    b = {**EP, "serving_integrity": si_ev(True, True)}
    t = ingest._trust_tier(b, "verified")
    check(t == "attested", "endpoint run + COMPLETE container-hash verification -> attested")
    check(ingest._attestation_method(b, t) == "endpoint_verified",
          "container-hash attestation is badged endpoint_verified (distinct from the fingerprint)")

    # config/manifest matched but weights NOT sha256-verified (no --deep-verify) -> self_reported
    check(ingest._trust_tier({**EP, "serving_integrity": si_ev(True, False)}, "verified") == "self_reported",
          "a serve-identity check without a COMPLETE weight-hash match does not rank")

    # weights_verified but tied to a DIFFERENT model's hash -> self_reported (the tie must hold)
    check(ingest._trust_tier({**EP, "serving_integrity": si_ev(True, True, wh="OTHER")}, "verified")
          == "self_reported", "container-hash tied to another model's weights_hash is rejected")

    # a serve MISMATCH never ranks
    check(ingest._trust_tier({**EP, "serving_integrity": si_ev(False, False)}, "verified") == "self_reported",
          "a serve-mismatch serving_integrity never ranks")

    # when BOTH exist, the stronger behavioral fingerprint labels the method
    b = {**EP, "endpoint_fingerprint": fp_ev(True), "serving_integrity": si_ev(True, True)}
    check(ingest._attestation_method(b, ingest._trust_tier(b, "verified")) == "endpoint_fingerprint",
          "with both present, the behavioral fingerprint takes precedence for the method label")

    # POLICY (owner, 2026-08-03): a byte-identical CONTAINER HASH carries the run even when the
    # behavioral fingerprint disagrees. The reference is captured from an eager, low-util, 4k-context
    # offline load while the endpoint under test is a production serve (multi-node, long context,
    # spec-decode), so honest numeric drift can trip the 0.97/0.20 thresholds on a correct serve —
    # and on an attested-only mothership that brittleness DISCARDS the operator's entire bench. When
    # the serving container provably holds byte-identical HF weights, that is the better evidence
    # about WHAT is served. The badge honestly degrades to the weaker host-asserted method.
    b = {**EP, "endpoint_fingerprint": fp_ev(False), "serving_integrity": si_ev(True, True)}
    t = ingest._trust_tier(b, "verified")
    check(t == "attested",
          "a verified container hash carries the run when the fingerprint disagrees")
    check(ingest._attestation_method(b, t) == "endpoint_verified",
          "...and is badged endpoint_verified, NOT endpoint_fingerprint")

    # but a mismatched fingerprint with NO container-hash proof still never ranks
    check(ingest._trust_tier({**EP, "endpoint_fingerprint": fp_ev(False),
                              "serving_integrity": si_ev(True, False)}, "verified") == "self_reported",
          "a mismatched fingerprint with no complete weight-hash still stays local")

    # a pod-SERVED run ignores serving_integrity entirely (it never needed it)
    check(ingest._attestation_method({**BASE, "serving_integrity": si_ev(True, True)}, "attested")
          == "hf_pull_served", "a pod-served run stays hf_pull_served regardless of serving_integrity")

    # recipes arrive JSON-encoded from some paths — endpoint detection must survive that
    EPS = {"weights_hash": WH, "recipe": _json.dumps({"engine": "vllm", "serve_mode": "endpoint"})}
    check(ingest._trust_tier(EPS, "verified") == "self_reported",
          "endpoint detection works when the recipe rides as a JSON string")

    # REGRESSION GUARD: a genuine pod-SERVED run (no serve_mode) still attests without a fingerprint
    check(ingest._trust_tier({"weights_hash": WH, "recipe": {"engine": "vllm", "serve_mode": "docker"}},
                             "verified") == "attested",
          "a pod-served run is unaffected — still attested with no fingerprint")

    print(f"\nOK  endpoint attestation (pod + mothership): {PASSED} checks passed")
else:
    print("\n" + "-" * 94)
    print("SKIPPED — NOT A PASS: the mothership tier gate did not run in this repo.")
    print("  aeon/ingest.py is mothership-only and is deliberately never published to the pod repo,")
    print("  so ingest._trust_tier / ingest._attestation_method cannot be exercised here.")
    print("  Covered above instead: the POD half of the same contract — the evidence emitters and")
    print("  the run_controlled wiring that feed that gate.")
    print("  NOT covered anywhere in this repo: which tier a given bundle earns. Before changing")
    print("  aeon/ingest.py, run THIS SAME FILE on the private repo, where the gate checks execute.")
    print("-" * 94)
    print(f"\nOK  endpoint attestation (pod half only): {PASSED} checks passed")
