"""pod/fingerprint.py — model-IDENTITY attestation of a running endpoint.

The attested global board normally requires the pod to pull + hash the weights against HF
(you can't hash weights you don't have on disk). But an operator may want to bench a serve
they ALREADY have running — a live production endpoint, or a multi-node cluster — without the
pod stopping and re-serving it. This module lets the pod confirm that the running endpoint is
genuinely serving the claimed weights, by LOGPROB FINGERPRINTING:

  reference()  — from a serve of weights the pod HASH-VERIFIED against HF, capture the greedy
                 tokens + top-k logprobs on a fixed canary set. This is ground truth.
  probe()      — ask the TARGET endpoint the identical canaries and capture the same.
  compare()    — token-agreement (argmax path) + logprob divergence. A different model diverges
                 in the token path almost immediately; the SAME model at a different quantization
                 (fp8 vs nvfp4 vs bf16) keeps most of the path but shifts logprobs measurably —
                 both are caught.

HONEST LIMIT (travels with the evidence): an endpoint owner who fully controls the serve could
proxy the canary prompts to the real weights while cheating the actual bench. Fingerprinting
detects an honest mismatch (wrong model, wrong quant, wrong fine-tune, a swapped serve), not a
determined adversary. The reference MUST come from hash-verified weights for the match to mean
anything — a probe with no trusted reference is 'unverifiable', never a pass.
"""
from __future__ import annotations

import hashlib
import json
import math
import urllib.request

# Fixed canary prompts — diverse enough that a different model's greedy path diverges fast,
# deterministic (temperature 0), short. Bumping this list bumps CANARY_VERSION so a reference
# and a probe are only ever compared over the SAME canary set (set hash pinned in the evidence).
CANARIES = [
    "The capital of France is",
    "def fibonacci(n):\n    if n < 2:\n        return n\n    return",
    "Q: What is 17 times 24?\nA: Let me compute step by step. 17 times 24 equals",
    "The three primary colors are red, blue, and",
    "In 1969, the first humans to walk on the Moon were Neil Armstrong and",
    "```json\n{\n  \"name\":",
]
CANARY_VERSION = "fp-v1"
# per-canary: greedy tokens to capture, and how many top alternatives to record at each step.
FP_TOKENS = 12
FP_TOP_K = 5

# match thresholds — tuned so identical weights pass and a quantization/model swap fails.
TOKEN_AGREEMENT_MIN = 0.97     # fraction of greedy positions whose argmax token must match
LOGPROB_DIVERGENCE_MAX = 0.20  # mean |Δ logprob| (nats) on the shared argmax path


def canary_set_hash() -> str:
    """Stable id of the canary set + capture params — a ref and a probe only compare when equal."""
    blob = json.dumps({"v": CANARY_VERSION, "canaries": CANARIES,
                       "tokens": FP_TOKENS, "top_k": FP_TOP_K}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _http_completion(base_url: str, model: str, prompt: str, *, max_tokens: int,
                     top_k: int, api_key=None, timeout=120):
    """Raw /v1/completions with greedy decode + per-token logprobs. Returns the parsed choice,
    or raises. Kept separate from targets.OpenAITarget.chat (which does not surface logprobs)."""
    url = base_url.rstrip("/")
    if not url.endswith("/completions"):
        url = url.rstrip("/") + ("/completions" if url.endswith("/v1") else "/v1/completions")
    body = json.dumps({"model": model, "prompt": prompt, "temperature": 0.0,
                       "max_tokens": max_tokens, "logprobs": top_k, "echo": False}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return (d.get("choices") or [{}])[0]


def _one_fingerprint(choice: dict) -> dict | None:
    """Distill an OpenAI completions `logprobs` block into {tokens, top}: the greedy token path
    and the rounded top-k logprob vector at each step. None when the endpoint returned no
    logprobs (can't fingerprint — an honest 'unverifiable', never a silent zero)."""
    lp = choice.get("logprobs") or {}
    toks = lp.get("tokens")
    chosen = lp.get("token_logprobs")
    tops = lp.get("top_logprobs")
    if not isinstance(toks, list) or not isinstance(chosen, list) or not toks:
        return None
    out = {"tokens": [], "chosen": [], "top": []}
    for i, tk in enumerate(toks[:FP_TOKENS]):
        out["tokens"].append(tk)
        c = chosen[i] if i < len(chosen) else None
        out["chosen"].append(round(c, 4) if isinstance(c, (int, float)) else None)
        row = tops[i] if isinstance(tops, list) and i < len(tops) and isinstance(tops[i], dict) else {}
        # top-k as a sorted (token, rounded-logprob) list — stable, comparable across serves
        out["top"].append(sorted(((t, round(v, 4)) for t, v in row.items()),
                                 key=lambda kv: (-kv[1], kv[0]))[:FP_TOP_K])
    return out


def _capture(base_url, model, *, api_key=None, transport=None, timeout=120):
    """Fingerprint EVERY canary against a serve. `transport(prompt)->choice` is injectable
    (tests, or a local-weights forward pass); default hits the OpenAI completions endpoint.
    Returns {canary_set, model, canaries:[fp|None], ok, n_ok}. Never raises — a per-canary
    failure records None so a flaky endpoint degrades to 'unverifiable', not a crash."""
    call = transport or (lambda p: _http_completion(base_url, model, p, max_tokens=FP_TOKENS,
                                                     top_k=FP_TOP_K, api_key=api_key, timeout=timeout))
    fps = []
    for prompt in CANARIES:
        try:
            fps.append(_one_fingerprint(call(prompt)))
        except Exception:
            fps.append(None)
    n_ok = sum(1 for f in fps if f)
    return {"canary_set": canary_set_hash(), "model": model, "canaries": fps,
            "ok": n_ok == len(CANARIES), "n_ok": n_ok}


def reference(base_url, model, *, api_key=None, transport=None, timeout=120):
    """Ground-truth fingerprint: MUST be taken from a serve of weights the pod hash-verified
    against HF. The caller stamps the verified weights_hash onto the returned dict."""
    ref = _capture(base_url, model, api_key=api_key, transport=transport, timeout=timeout)
    ref["role"] = "reference"
    return ref


def probe(base_url, model, *, api_key=None, transport=None, timeout=120):
    """Fingerprint of the TARGET endpoint under test (identity unverified until compare())."""
    p = _capture(base_url, model, api_key=api_key, transport=transport, timeout=timeout)
    p["role"] = "probe"
    return p


def reference_from_weights(*, local_dir, recipe=None, _llm_factory=None):
    """GROUND-TRUTH reference straight from the HASH-VERIFIED weights on disk — a short vLLM
    OFFLINE greedy pass over the canaries (no server, loads once, releases). This is how the pod
    fingerprints a serve it did NOT launch (serve_url / a live cluster): the reference comes from
    the weights it verified against HF, the probe from the operator's endpoint.

    Returns the standard fingerprint dict, or None when the weights can't be loaded for a capture
    (no GPU room, offline engine unavailable) — the caller then proceeds on serve_url trust rather
    than blocking. `_llm_factory` is injectable for tests; production imports vLLM lazily."""
    try:
        if _llm_factory is not None:
            gen = _llm_factory(local_dir, recipe)
        else:                                            # pragma: no cover — GPU/vLLM only
            from vllm import LLM, SamplingParams
            quant = (recipe or {}).get("quant")
            llm = LLM(model=local_dir, trust_remote_code=True, gpu_memory_utilization=0.30,
                      max_model_len=4096, enforce_eager=True,
                      **({"quantization": quant} if quant else {}))
            sp = SamplingParams(temperature=0.0, max_tokens=FP_TOKENS, logprobs=FP_TOP_K)

            def gen(prompts):
                outs = llm.generate(prompts, sp)
                rows = []
                for o in outs:
                    g = o.outputs[0]
                    toks, chosen, tops = [], [], []
                    for i, tid in enumerate(list(g.token_ids)[:FP_TOKENS]):
                        lps = (g.logprobs or [])[i] if i < len(g.logprobs or []) else {}
                        entry = lps.get(tid)
                        dec = getattr(entry, "decoded_token", None) or str(tid)
                        toks.append(dec)
                        chosen.append(getattr(entry, "logprob", None) if entry else None)
                        tops.append({(getattr(v, "decoded_token", None) or str(k)):
                                     getattr(v, "logprob", 0.0) for k, v in lps.items()})
                    rows.append({"tokens": toks, "token_logprobs": chosen, "top_logprobs": tops})
                return rows
    except Exception:
        return None
    try:
        rows = gen(list(CANARIES))
        fps = [_one_fingerprint({"logprobs": r}) if r else None for r in rows]
    except Exception:
        return None
    n_ok = sum(1 for f in fps if f)
    return {"canary_set": canary_set_hash(), "model": local_dir, "canaries": fps,
            "ok": n_ok == len(CANARIES), "n_ok": n_ok, "role": "reference",
            "source": "pod-local-weights"}


def compare(ref: dict, prb: dict) -> dict:
    """Decide whether the probed endpoint serves the reference weights.

    Returns {match, status, token_agreement, logprob_divergence, n_compared, reason}. status is
    one of: 'match' (same weights + quant), 'mismatch' (token path or logprobs diverge),
    'unverifiable' (canary sets differ, or one side returned no logprobs — cannot decide, and
    an undecidable probe is NEVER a pass)."""
    if not ref or not prb or ref.get("canary_set") != prb.get("canary_set"):
        return {"match": False, "status": "unverifiable", "reason": "canary sets differ / missing",
                "token_agreement": None, "logprob_divergence": None, "n_compared": 0}
    rc, pc = ref.get("canaries") or [], prb.get("canaries") or []
    pos_total = pos_match = 0
    diffs = []
    for rf, pf in zip(rc, pc):
        if not rf or not pf:
            continue
        for i in range(min(len(rf["tokens"]), len(pf["tokens"]))):
            pos_total += 1
            if rf["tokens"][i] == pf["tokens"][i]:
                pos_match += 1
                a, b = rf["chosen"][i], pf["chosen"][i]
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    diffs.append(abs(a - b))
    if pos_total == 0:
        return {"match": False, "status": "unverifiable", "reason": "no logprobs on one side",
                "token_agreement": None, "logprob_divergence": None, "n_compared": 0}
    agreement = pos_match / pos_total
    divergence = (sum(diffs) / len(diffs)) if diffs else math.inf
    match = agreement >= TOKEN_AGREEMENT_MIN and divergence <= LOGPROB_DIVERGENCE_MAX
    if match:
        status, reason = "match", "greedy path + logprobs consistent with the verified weights"
    elif agreement < TOKEN_AGREEMENT_MIN:
        status, reason = "mismatch", f"greedy token path diverges (agreement {agreement:.2f} < {TOKEN_AGREEMENT_MIN})"
    else:
        status, reason = "mismatch", f"logprobs shifted (Δ {divergence:.3f} > {LOGPROB_DIVERGENCE_MAX} nats — likely a different quantization)"
    return {"match": match, "status": status, "reason": reason,
            "token_agreement": round(agreement, 4),
            "logprob_divergence": (round(divergence, 4) if math.isfinite(divergence) else None),
            "n_compared": pos_total}


def evidence(ref: dict, prb: dict, cmp: dict, *, weights_hash=None, ref_source=None) -> dict:
    """The compact, submittable fingerprint record that rides the bundle → mothership. Carries
    ONLY the decision + provenance, never the raw logprob dumps (kept small + non-gameable):
    the canary set id, the verified weights_hash the reference was taken from, the match verdict,
    and the honest limit so the badge can never overclaim."""
    return {
        "method": "logprob-fingerprint",
        "canary_version": CANARY_VERSION,
        "canary_set": canary_set_hash(),
        "weights_hash": weights_hash,                 # the HF-verified ref weights (ground truth)
        "reference_source": ref_source,               # e.g. 'pod-local-serve' | 'mothership-prior-attested'
        "match": bool(cmp.get("match")),
        "status": cmp.get("status"),
        "token_agreement": cmp.get("token_agreement"),
        "logprob_divergence": cmp.get("logprob_divergence"),
        "n_compared": cmp.get("n_compared"),
        "ref_n_ok": (ref or {}).get("n_ok"),
        "probe_n_ok": (prb or {}).get("n_ok"),
        "limit": "identity-only: confirms the endpoint serves the verified weights; cannot bind "
                 "a fully adversarial host that proxies canaries to the real model",
    }
