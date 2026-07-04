"""Pod submission channel — trust-chain P0 (the genuinely-real-today layer).

Guarantees WITHOUT hardware (see docs/trust-architecture.md §2.5/§2.6):
  - **Authorship + integrity-in-transit:** ed25519 signature over a canonical body by a
    pinned, enrolled, non-revoked device key — a compromised TLS edge cannot forge a bundle.
  - **Replay resistance:** server-issued single-use run_nonce + run-scoped token, consumed
    atomically on first submit.
  - **Sandboxed ingest:** the bundle is treated as INERT DATA — never executed; strict
    schema + size caps validated before any DB write; failures quarantine with reason codes.

It does NOT prove the numbers reflect a real run — every operator-host submission is stored
`self_reported` (not record-eligible). Honest `orchestrated` (mothership re-generates) is P1.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid

from . import attest, avscan, db, judge_policy

# Verified-only acceptance. When set (the PUBLIC mothership sets AEON_ATTESTED_ONLY=1),
# submit_results REFUSES any bundle that would not earn 'attested' — the mothership
# stores ONLY verified HF-pull runs. Default off, so pods/dev keep the "store
# self_reported locally" behaviour unchanged.
_ATTESTED_ONLY = os.environ.get("AEON_ATTESTED_ONLY", "0") == "1"

MAX_BUNDLE_BYTES = 4 * 1024 * 1024     # 4 MB results-bundle cap (pre-parse)
MAX_CASES = 2000
MAX_ARTIFACTS = 12                      # arena artifacts accepted per bundle
MAX_ARTIFACT_HTML = 200 * 1024          # bytes of UTF-8 per artifact html (matches pod/arena_gen.py)
_ARTIFACT_KINDS = {"app", "game", "animation"}   # arena.KINDS (kept local: ingest stays dependency-light)
_CHALLENGE_TTL = 300
_REVOKE_AT = 5                          # forgery-class failures before auto-revoke

# clear-forgery codes by the TOKEN HOLDER (bump the key's fail counter). Auth failures are
# handled before any quarantine so a non-holder who learns a run_id can't consume the run.
_FORGERY = {"BAD_SIG", "NONCE_MISMATCH"}
_CODE_STATUS = {"REPLAY_NONCE": 409, "UNKNOWN_KEY": 403, "REVOKED_KEY": 403, "BAD_SIG": 400,
                "SCHEMA_INVALID": 422, "SIZE_EXCEEDED": 413, "TOKEN_MISMATCH": 403, "NONCE_MISMATCH": 400}

def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fingerprint(pub_b64: str) -> str:
    return hashlib.sha256(pub_b64.encode()).hexdigest()[:16]


# ---- enrollment (prove possession of the device key) ----

def issue_challenge() -> str:
    ch = secrets.token_urlsafe(24)
    db.issue_challenge(ch, time.time() + _CHALLENGE_TTL)   # shared store (multi-replica safe)
    return ch


def _consume_challenge(ch: str) -> bool:
    return db.consume_challenge(ch, time.time())            # atomic single-use


def enroll(public_key, challenge, signature):
    if not (public_key and challenge and signature):
        return {"error": "public_key, challenge, signature required"}, 400
    if not _consume_challenge(challenge):
        return {"error": "invalid or expired challenge"}, 400
    if not attest.verify(challenge.encode(), signature, public_key):
        return {"error": "signature does not verify"}, 400
    db.enroll_key(uuid.uuid4().hex[:12], public_key=public_key, fingerprint=_fingerprint(public_key))
    return {"ok": True, "fingerprint": _fingerprint(public_key), "status": "active"}, 200


# ---- open a run (mint single-use nonce + run-scoped token) ----

def open_run(public_key, signature, *, model, suite_id, board="text"):
    key = db.get_enrolled_key(public_key)
    if not key:
        return {"error": "key not enrolled"}, 403
    if key["status"] != "active":
        return {"error": "key revoked"}, 403
    body = {"action": "open_run", "public_key": public_key, "model": model,
            "suite_id": suite_id, "board": board}
    if not attest.verify(_canon(body), signature, public_key):
        return {"error": "signature does not verify"}, 400
    run_id = uuid.uuid4().hex[:12]
    nonce, token = secrets.token_urlsafe(18), secrets.token_urlsafe(24)
    db.create_pod_run(run_id, public_key=public_key, run_nonce=nonce, run_token=token,
                      model=model, suite_id=suite_id, board=board)
    return {"run_id": run_id, "run_nonce": nonce, "run_token": token}, 200


# ---- ingest the signed results bundle ----

def _quarantine(pod, code):
    db.claim_pod_run(pod["run_id"], "quarantined", code)     # consumes the run (idempotent)
    if code in _FORGERY:
        k = db.get_enrolled_key(pod["public_key"])
        if k:
            if db.bump_key_fail(pod["public_key"]) >= _REVOKE_AT:
                db.revoke_key(pod["public_key"])
    return {"error": "rejected", "reason": code}, _CODE_STATUS.get(code, 400)


def _validate_bundle(bundle) -> bool:
    if not isinstance(bundle, dict):
        return False
    res = bundle.get("results")
    if not isinstance(res, list) or not res or len(res) > MAX_CASES:
        return False
    for r in res:
        if not isinstance(r, dict) or "case_id" not in r or "category" not in r:
            return False
        s = r.get("score")
        if s is not None and not (isinstance(s, (int, float)) and 0 <= s <= 1):
            return False
        cr = r.get("creativity")
        if cr is not None and not (isinstance(cr, (int, float)) and 0 <= cr <= 3):
            return False
    return True


def submit_results(run_id, run_token, raw_body_bytes):
    """State machine: AUTH(token/key) -> SIZE -> parse -> SIG -> NONCE -> SCHEMA -> CLAIM -> COMMIT."""
    pod = db.get_pod_run(run_id)
    if not pod:
        return {"error": "unknown run"}, 404
    if pod["status"] != "open":
        return {"error": "already submitted", "reason": "REPLAY_NONCE"}, 409
    # AUTH FIRST — failures do NOT quarantine/consume the run (so a non-holder who learns a
    # run_id can't deny the real pod its submission).
    if not run_token or run_token != pod["run_token"]:
        return {"error": "bad run token", "reason": "TOKEN_MISMATCH"}, 403
    key = db.get_enrolled_key(pod["public_key"])
    if not key:
        return {"error": "key not enrolled", "reason": "UNKNOWN_KEY"}, 403
    if key["status"] != "active":
        return {"error": "key revoked", "reason": "REVOKED_KEY"}, 403
    # past AUTH: the token holder is committing — bad payloads now quarantine the run.
    # SIZE (pre-parse, cheap)
    if raw_body_bytes is None or len(raw_body_bytes) > MAX_BUNDLE_BYTES:
        return _quarantine(pod, "SIZE_EXCEEDED")
    # parse
    try:
        payload = json.loads(raw_body_bytes.decode("utf-8"))
        bundle, signature = payload["bundle"], payload["signature"]
    except Exception:
        return _quarantine(pod, "SCHEMA_INVALID")
    # SIG — ed25519 over the canonical bundle, by the enrolled key
    if not attest.verify(_canon(bundle), signature, pod["public_key"]):
        return _quarantine(pod, "BAD_SIG")
    # NONCE binding
    if bundle.get("run_id") != run_id or bundle.get("run_nonce") != pod["run_nonce"]:
        return _quarantine(pod, "NONCE_MISMATCH")
    # SCHEMA
    if not _validate_bundle(bundle):
        return _quarantine(pod, "SCHEMA_INVALID")
    # CLAIM only on the FINAL checkpoint. Until then the run stays 'open' so the pod can stream
    # more results — each batch is token-authed and dedups by (run_id, case_id), so a mid-run kill
    # keeps every case already submitted (no catastrophic loss).
    final = bool(bundle.get("final", True))
    # VERIFIED-ONLY (mothership): refuse anything that would not earn 'attested'
    # BEFORE claiming/committing — nothing untrusted is stored. The run is left
    # OPEN (not quarantined) so a pod can retry the same run_id if HF was merely
    # transiently unreachable; the note distinguishes that from a genuine
    # endpoint/self-reported run that can never qualify.
    if _ATTESTED_ONLY:
        _hf, _sha, _verified = _resolve_identity(bundle)
        if _trust_tier(bundle, _verified) not in ELIGIBLE_TIERS:
            transient = _verified in ("claim", "claim_unverified")
            return {"error": "not attested", "reason": "NOT_ATTESTED", "verified_state": _verified,
                    "note": ("this mothership accepts ONLY attested (verified HF-pull) runs. "
                             + ("HF verification did not confirm the served weights right now — "
                                "retry shortly; if it persists, check the repo/revision + weights hashes."
                                if transient else
                                "this looks like an endpoint / self-reported run — run the controlled "
                                "HF-pull flow (pull → hash-verify → serve → sign) to qualify."))}, 403
    if final and not db.claim_pod_run(run_id, "committed"):
        return {"error": "already submitted", "reason": "REPLAY_NONCE"}, 409
    tier = _commit(pod, bundle, final)
    eligible = tier in ELIGIBLE_TIERS
    return {"ok": True, "run_id": run_id, "trust_tier": tier, "record_eligible": eligible,
            "n_cases": len(bundle["results"]),
            "note": ("stored attested — verified HF-pull controlled run; ELIGIBLE for the global leaderboard"
                     if eligible else
                     "stored self_reported — local only, NOT globally ranked. Submit via the HF-pull "
                     "controlled flow (pull from HF → hash-verify → serve → harnesses → sign) to qualify.")}, 200


def _resolve_identity(bundle):
    """Canonical model identity + bit-for-bit verification against HF. The controlled HF-pull
    flow sends the per-file sha256 of the weights it pulled (weights_per_file); the mothership
    INDEPENDENTLY re-fetches HF's published LFS hashes for the SAME pinned commit and requires
    every published weight file to match AND the pinned revision to be the one served. That —
    not a self-asserted flag — is what earns 'verified'. An API-only/local run has no per-file
    hashes and stays 'claim'/'declared'."""
    hf_repo = (bundle.get("hf_repo") or "").strip() or None
    if not hf_repo:
        return None, None, "declared"               # custom/local model — identity is the declared name
    pinned = (bundle.get("hf_revision") or "").strip()
    ref = attest.verify_model_ref(hf_repo, pinned or "main")
    if not ref.get("ok") and pinned:
        ref = attest.verify_model_ref(hf_repo)       # fall back to the default branch
    if not ref.get("ok"):
        return hf_repo, None, "claim_unverified"     # user-claimed repo we couldn't confirm on HF
    sha = ref.get("sha")
    hf_files = ref.get("files") or {}
    pod_files = bundle.get("weights_per_file") or {}
    # re-verify: every weight file HF publishes an LFS sha256 for must match the pod's hash.
    checked = matched = 0
    for rel, hf_sha in hf_files.items():
        if hf_sha and rel in pod_files:
            checked += 1
            matched += int(pod_files[rel] == hf_sha)
    rev_ok = (not pinned) or (pinned == sha)
    if checked and matched == checked and rev_ok:
        verified = "verified"                        # bit-for-bit confirmed against HF
    else:
        ws = bundle.get("weights_sha256")            # legacy single-hash bundles: weak check only
        verified = "verified" if (ws and ws in hf_files.values()) else "claim"
    return hf_repo, sha, verified


# Tiers that are ranked on the GLOBAL leaderboard. Everything else is stored + shown, but local.
ELIGIBLE_TIERS = {"attested"}


def _trust_tier(bundle, verified):
    """A run qualifies for the GLOBAL leaderboard ('attested') ONLY through the controlled
    HF-pull flow: the model is bit-for-bit verified against HF (which requires the pod to have
    pulled + hashed the ACTUAL weights — impossible over a bare API endpoint), and a serve
    recipe + the pod-computed weights hash travelled inside the (already signature-verified)
    bundle. Anything short of that is 'self_reported' — stored and shown, but never ranked."""
    if verified == "verified" and bundle.get("weights_hash") and bundle.get("recipe"):
        return "attested"
    return "self_reported"


def _cap_html(html, limit=MAX_ARTIFACT_HTML):
    b = html.encode("utf-8")
    if len(b) <= limit:
        return html
    return b[:limit].decode("utf-8", "ignore")


def _save_artifacts(pod, bundle):
    """Arena artifacts riding a signed bundle (pod/arena_gen.py) -> arena_artifacts rows.

    Inert data only: max 12 items, html size-capped, kind whitelisted, model/prompt_id
    sanitized (the model name is rendered in the non-sandboxed ranking UI — never store
    markup; the html itself is only ever rendered in a sandboxed iframe). Called ONLY on
    the FINAL commit of a run, so checkpoint resends never duplicate. Never raises —
    a bad artifact must not break the results commit. Returns the number saved.
    """
    arts = bundle.get("artifacts")
    if not isinstance(arts, list):
        return 0
    model = re.sub(r"[<>\"'`]", "", pod.get("model") or "")[:80]   # same sanitize as arena.generate_artifact
    saved = 0
    for a in arts[:MAX_ARTIFACTS]:
        try:
            if not isinstance(a, dict) or not a.get("ok"):
                continue
            html, kind, pid = a.get("html"), a.get("kind"), a.get("prompt_id")
            if not (isinstance(html, str) and html.strip()):
                continue
            if kind not in _ARTIFACT_KINDS or not isinstance(pid, str) or not pid.strip():
                continue
            pid = re.sub(r"[<>\"'`]", "", pid)[:80]
            gen_ms = a.get("gen_ms")
            if not isinstance(gen_ms, (int, float)) or isinstance(gen_ms, bool):
                gen_ms = None
            capped = _cap_html(html)
            # AV scan the artifact HTML (the only untrusted bytes we will re-serve).
            # NEVER scan results[*].raw_output — that's model output and false-positives.
            clean, detail = avscan.scan(capped)
            if not clean:
                print(f"[ingest] artifact quarantined ({detail}) run={pod.get('run_id')} kind={kind} pid={pid}")
                continue                                # drop it — do not persist known-bad content
            db.save_artifact(uuid.uuid4().hex[:10], kind=kind, prompt_id=pid,
                             model=model, html=capped, ok=True, gen_ms=gen_ms)
            saved += 1
        except Exception:
            continue
    return saved


def _commit(pod, bundle, final=True):
    """Inert DATA -> rows, INCREMENTALLY. The run is created (status running, identity resolved,
    tier fixed) on the FIRST checkpoint and its cases appended (dedup by case_id) on each;
    finish_run() on the FINAL checkpoint flips it to 'succeeded' — only then is it ranked. A run
    killed mid-stream keeps every case it had submitted (no catastrophic loss). Eligible for the
    global board ONLY when the controlled HF-pull flow proves it. Returns the trust tier."""
    run_id = pod["run_id"]
    run = db.get_run(run_id)
    if not run:                                  # first checkpoint: resolve identity + open the run
        hf_repo, hf_revision, verified = _resolve_identity(bundle)
        tier = _trust_tier(bundle, verified)
        # Judge policy: credit ONLY a frontier judge; a self / weak / absent judge -> deterministic.
        jm = bundle.get("judge_model")
        jmode = judge_policy.judge_mode(jm, pod["model"])
        env = dict(bundle.get("environment") or {})
        env["judge_mode"] = jmode
        db.create_run(run_id, model=pod["model"], target_url="pod-submission",
                      judge_model=(jm if jmode == "frontier" else None), judge_is_self=False,
                      suite_id=pod["suite_id"], suite_hash=bundle.get("suite_hash"),
                      n_cases=bundle.get("n_cases") or len(bundle["results"]),
                      params=bundle.get("params") or {}, env=env, board=pod["board"],
                      hf_repo=hf_repo, hf_revision=hf_revision, model_verified=verified,
                      harness=bundle.get("harness"), harness_version=bundle.get("harness_version"),
                      trust_tier=tier, weights_hash=bundle.get("weights_hash"),
                      recipe=bundle.get("recipe"), deployment_manifest=bundle.get("deployment_manifest"),
                      bench_seed=bundle.get("bench_seed"))
    else:
        tier = run.get("trust_tier") or "self_reported"
    seen = db.result_case_ids(run_id)            # append only NEW cases (dedup cumulative resends)
    for r in bundle["results"]:
        if r["case_id"] in seen:
            continue
        db.save_result(run_id, r["case_id"], category=r["category"], tier=r.get("tier", 0),
                       status=r.get("status", "scored"), score=r.get("score"),
                       raw_output=r.get("raw_output", ""), evidence=r.get("evidence") or {},
                       speed=r.get("speed") or {}, board=pod["board"], creativity=r.get("creativity"))
    if final:
        # Arena artifacts land ONLY on the final commit — mid-run checkpoints resend the
        # same bundle extras, and claim_pod_run already guarantees a single final commit.
        _save_artifacts(pod, bundle)
        db.finish_run(run_id, "succeeded")
    return tier
