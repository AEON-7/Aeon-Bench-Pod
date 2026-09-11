"""Endpoint logprob-fingerprint identity check — the security heart of verified target mode.

No network, no GPU: an injectable transport returns synthetic OpenAI-completions logprob blocks
for four served 'models' — the reference weights, a byte-identical copy, a DIFFERENT model
(greedy path diverges), the SAME model at a coarser QUANTIZATION (path mostly holds, logprobs
shift), and a serve that returns NO logprobs. Asserts each lands on the right verdict, and that
an undecidable probe is never a pass."""
import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import fingerprint as fp  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


def _lp(tokens, chosen, tops):
    return {"logprobs": {"tokens": tokens, "token_logprobs": chosen, "top_logprobs": tops}}


def make_transport(*, seed=0.0, drift=0.0, model="A", no_logprobs=False):
    """A deterministic synthetic serve. `model` picks a token vocabulary (a different model ->
    a different greedy path); `drift` shifts every logprob (a coarser quantization); `no_logprobs`
    returns empty. Same (model, seed, drift) -> identical fingerprints, like a real greedy serve."""
    def transport(prompt):
        if no_logprobs:
            return {"text": "hi"}
        h = abs(hash((prompt, model))) % 1000
        toks, chosen, tops = [], [], []
        for i in range(fp.FP_TOKENS):
            base = (h + i * 7) % 50
            tok = f"{model}_{base}"                      # model-specific token identity
            lp = -0.5 - ((base % 5) * 0.1) + seed + drift
            toks.append(tok)
            chosen.append(lp)
            row = {tok: lp}
            for k in range(1, fp.FP_TOP_K):
                row[f"{model}_{(base + k) % 50}"] = lp - 0.4 * k + drift
            tops.append(row)
        return _lp(toks, chosen, tops)
    return transport


# ---- 1) canary set hash is stable + pins the params -----------------------------------------
h1, h2 = fp.canary_set_hash(), fp.canary_set_hash()
check(h1 == h2 and len(h1) == 16, "canary_set_hash is stable")

# ---- 2) identical weights -> MATCH ----------------------------------------------------------
ref = fp.reference("mock", "m", transport=make_transport(model="A"))
check(ref["ok"] and ref["role"] == "reference", "reference captures all canaries")
same = fp.probe("mock", "m", transport=make_transport(model="A"))
c = fp.compare(ref, same)
check(c["match"] and c["status"] == "match", "identical weights -> match")
check(c["token_agreement"] == 1.0 and c["logprob_divergence"] == 0.0,
      "identical weights: full token agreement, zero logprob divergence")

# ---- 3) a DIFFERENT model -> mismatch on the token path -------------------------------------
other = fp.probe("mock", "m", transport=make_transport(model="B"))
c = fp.compare(ref, other)
check(not c["match"] and c["status"] == "mismatch", "a different model is rejected")
check(c["token_agreement"] < fp.TOKEN_AGREEMENT_MIN, "different model: greedy path diverges")

# ---- 4) same model, coarser QUANTIZATION -> path holds, logprobs shift -> mismatch ----------
quant = fp.probe("mock", "m", transport=make_transport(model="A", drift=0.9))
c = fp.compare(ref, quant)
check(not c["match"] and c["status"] == "mismatch", "same model at a different quantization is rejected")
check(c["token_agreement"] == 1.0 and c["logprob_divergence"] > fp.LOGPROB_DIVERGENCE_MAX,
      "quantization drift: identical path but logprobs shift past tolerance")

# ---- 5) a small numerical wobble WITHIN tolerance still matches ------------------------------
wobble = fp.probe("mock", "m", transport=make_transport(model="A", drift=0.05))
c = fp.compare(ref, wobble)
check(c["match"], "sub-tolerance numerical noise still matches (not over-strict)")

# ---- 6) endpoint returns NO logprobs -> unverifiable, never a pass --------------------------
blind = fp.probe("mock", "m", transport=make_transport(no_logprobs=True))
c = fp.compare(ref, blind)
check(not c["match"] and c["status"] == "unverifiable", "no-logprob endpoint is unverifiable, not a pass")
check(blind["n_ok"] == 0, "a blind endpoint fingerprints nothing")

# ---- 7) canary-set mismatch -> unverifiable -------------------------------------------------
stale = dict(ref); stale = {**ref, "canary_set": "deadbeefdeadbeef"}
c = fp.compare(stale, same)
check(not c["match"] and c["status"] == "unverifiable", "different canary sets never compare as a pass")

# ---- 8) evidence record: decision + provenance, honest limit, no raw logprob dump -----------
ev = fp.evidence(ref, quant, fp.compare(ref, quant),
                 weights_hash="abc123", ref_source="pod-local-serve")
check(ev["method"] == "logprob-fingerprint" and ev["weights_hash"] == "abc123", "evidence carries method + verified weights_hash")
check(ev["match"] is False and ev["status"] == "mismatch", "evidence carries the verdict")
check("limit" in ev and "adversarial" in ev["limit"], "evidence carries the honest adversary limit")
import json as _json  # noqa: E402
check("tokens" not in _json.dumps(ev), "evidence never ships the raw token/logprob dump")

# ---- 9) reference_from_weights (offline capture) round-trips against an HTTP probe -----------
# The vLLM-offline reference and the HTTP endpoint probe of the SAME weights must compare as a
# match — proving the offline adapter yields fingerprints comparable to a served probe.
def _row(prompt, model):
    ch = make_transport(model=model)(prompt)["logprobs"]
    return {"tokens": ch["tokens"], "token_logprobs": ch["token_logprobs"],
            "top_logprobs": ch["top_logprobs"]}


def factory_A(local_dir, recipe):
    return lambda prompts: [_row(p, "A") for p in prompts]


ref_w = fp.reference_from_weights(local_dir="/weights/A", recipe={"quant": "modelopt"},
                                  _llm_factory=factory_A)
check(ref_w and ref_w["n_ok"] == len(fp.CANARIES) and ref_w["source"] == "pod-local-weights",
      "reference_from_weights captures every canary from the offline pass")
served_A = fp.probe("mock", "m", transport=make_transport(model="A"))
check(fp.compare(ref_w, served_A)["match"],
      "offline-weights reference matches an HTTP probe of the same weights")
served_B = fp.probe("mock", "m", transport=make_transport(model="B"))
check(not fp.compare(ref_w, served_B)["match"],
      "offline-weights reference rejects a probe of a different model")


def factory_boom(local_dir, recipe):
    raise RuntimeError("no GPU room")


check(fp.reference_from_weights(local_dir="/w", _llm_factory=factory_boom) is None,
      "an unloadable-weights capture returns None (caller falls back, never blocks)")

print(f"\nOK  endpoint fingerprint: {PASSED} checks passed")
