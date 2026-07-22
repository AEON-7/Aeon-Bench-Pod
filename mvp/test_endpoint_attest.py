"""Trust-tier decision for endpoint-fingerprint attestation (mothership ingest).

Verified-weights + a MATCHED fingerprint whose reference is those exact weights earns attested
(the owner's call: fingerprint-verified endpoints rank). Every weaker shape — unmatched, a
reference against a DIFFERENT model's weights, unverified weights — stays self_reported. Imports
aeon.ingest, so it runs on the private repo only (documented precedent)."""
import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import ingest  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


WH = "a1b2c3weightshash"
BASE = {"weights_hash": WH, "recipe": {"engine": "vllm", "flags": []}}


def fp_ev(match, *, wh=WH, status=None):
    return {"method": "logprob-fingerprint", "match": match,
            "status": status or ("match" if match else "mismatch"), "weights_hash": wh}


# ---- served-by-pod attested (no fingerprint) ------------------------------------------------
t = ingest._trust_tier(dict(BASE), "verified")
check(t == "attested", "verified weights, pod-served -> attested")
check(ingest._attestation_method(dict(BASE), t) == "hf_pull_served", "method = hf_pull_served")

# ---- endpoint fingerprint MATCHED, reference == the verified weights -> attested ------------
b = {**BASE, "endpoint_fingerprint": fp_ev(True)}
t = ingest._trust_tier(b, "verified")
check(t == "attested", "verified weights + matched fingerprint of those weights -> attested (ranks)")
check(ingest._attestation_method(b, t) == "endpoint_fingerprint", "method = endpoint_fingerprint")

# ---- fingerprint MISMATCH -> self_reported --------------------------------------------------
b = {**BASE, "endpoint_fingerprint": fp_ev(False)}
check(ingest._trust_tier(b, "verified") == "self_reported",
      "a mismatched endpoint fingerprint never ranks")

# ---- fingerprint 'match' flag but status not 'match' (tampered) -> self_reported ------------
b = {**BASE, "endpoint_fingerprint": fp_ev(True, status="unverifiable")}
check(ingest._trust_tier(b, "verified") == "self_reported",
      "match=True with a non-'match' status is rejected (both must agree)")

# ---- fingerprint reference is a DIFFERENT model's weights -> self_reported ------------------
b = {**BASE, "endpoint_fingerprint": fp_ev(True, wh="DIFFERENT_model_hash")}
check(ingest._trust_tier(b, "verified") == "self_reported",
      "fingerprint matched against another model's reference is rejected (weights_hash must equal)")

# ---- unverified weights: fingerprint cannot rescue it ---------------------------------------
b = {**BASE, "endpoint_fingerprint": fp_ev(True)}
check(ingest._trust_tier(b, "claim") == "self_reported",
      "unverified weights stay self_reported even with a matched fingerprint")
check(ingest._trust_tier({"endpoint_fingerprint": fp_ev(True)}, "verified") == "self_reported",
      "no weights_hash/recipe -> self_reported regardless of fingerprint")
check(ingest._attestation_method(b, "self_reported") is None, "no attestation method when not attested")

print(f"\nOK  endpoint attestation tier: {PASSED} checks passed")
