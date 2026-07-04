"""Deployment + submission attestation (DESIGN §8 / §13 trust chain).

What this gives you (software layer):
  1. A deterministic **build hash** over the running source manifest — a third party
     with the source recomputes it and checks it equals a known-good release hash.
  2. An **ed25519 keypair** the running process controls; an `/api/attestation`
     endpoint signs (build_hash, public_key, timestamp, caller-nonce) so a verifier
     can challenge the live deployment and confirm it holds the key AND reports the
     expected build hash (replay-proof via the nonce).
  3. **Signed submissions** — every published run manifest is signed by the same key,
     so results are verifiably from this deployment and untampered in transit.
  4. **Model attestation** — records the claimed model identity (HF repo + revision)
     and, when the weights are locally reachable, their content hash; for remote
     API endpoints the weights aren't accessible, so identity is recorded and the
     HF-advertised reference can be fetched for comparison (see verify_model_ref).

HONEST TRUST BOUNDARY: this is software self-attestation. It proves "the code that
matches build_hash X is signing with key K, and K is the key you pinned." It does
NOT, by itself, prove a malicious host isn't lying about which code it runs — a
modified process can print anything. Binding the key to the actual running code
("truly derived from the code itself") requires a hardware root of trust (TPM
remote attestation / AMD SEV-SNP / AWS Nitro / Intel TDX), which wraps THIS signed
statement in a hardware-quoted measurement. That hardware tier is the next layer;
the API and manifest here are shaped to slot a TEE quote in without changing the
verifier contract. See `docs/attestation.md`.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import platform
import secrets
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

MVP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.environ.get("AEON_KEY_PATH", os.path.join(MVP_DIR, ".aeon_attest_key.pem"))

# The exact set of files whose bytes define "this build". Order-independent: we sort.
# (The suite corpus is part of the bench's identity, so it is included.)
_MANIFEST = [
    "serve.py",
    "aeon/*.py",
    "suites/*.json",
    "web/index.html",
    "web/app.js",
    "web/styles.css",
]


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def source_manifest() -> dict:
    """Sorted [{path, sha256, bytes}] for every build-defining file + the build hash."""
    files = []
    for pat in _MANIFEST:
        for p in glob.glob(os.path.join(MVP_DIR, pat)):
            if os.path.isfile(p):
                rel = os.path.relpath(p, MVP_DIR).replace("\\", "/")
                files.append({"path": rel, "sha256": _sha256_file(p),
                              "bytes": os.path.getsize(p)})
    files.sort(key=lambda x: x["path"])
    build_hash = hashlib.sha256(_canon([(f["path"], f["sha256"]) for f in files])).hexdigest()
    return {"build_hash": build_hash, "files": files, "n_files": len(files)}


def build_hash() -> str:
    return source_manifest()["build_hash"]


# ---- keypair ----

_key_cache: Ed25519PrivateKey | None = None


def _load_key() -> Ed25519PrivateKey:
    """Load the signing key, decrypting with AEON_KEY_PASS when the PEM is encrypted.

    HARD-FAILS if the key is missing — it is NEVER auto-generated in the request path.
    Auto-creating on a wiped/unmounted volume would silently mint a brand-new trust
    anchor and serve attestations under it, invisibly re-pinning every verifier. Mint
    the key explicitly once with `python -m aeon.attest keygen`.
    """
    if not os.path.exists(KEY_PATH):
        raise RuntimeError(
            f"AEON signing key missing at {KEY_PATH}. It is NOT auto-generated (that would "
            f"silently re-anchor the trust root). Create it once: `python -m aeon.attest keygen` "
            f"(set AEON_KEY_PASS first to encrypt it at rest).")
    with open(KEY_PATH, "rb") as f:
        data = f.read()
    passphrase = os.environ.get("AEON_KEY_PASS")
    pw = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_pem_private_key(data, password=pw)
    except TypeError:
        # The key's encryption state disagrees with whether a passphrase was supplied.
        if pw is not None:                       # FIX(LOW): AEON_KEY_PASS set but key is plaintext.
            raise RuntimeError(                  # HARD-FAIL — silently loading it defeats the
                f"AEON_KEY_PASS is set but the signing key at {KEY_PATH} is UNENCRYPTED. "
                f"At-rest encryption was requested but is not in effect. Re-mint the key with "
                f"AEON_KEY_PASS set (`python -m aeon.attest keygen --force`), or unset AEON_KEY_PASS.")
        raise RuntimeError(                      # encrypted key, but no pass → tell the operator
            f"AEON signing key at {KEY_PATH} is encrypted — set AEON_KEY_PASS to load it.")


def _key() -> Ed25519PrivateKey:
    """Process-cached signing key (loaded once, not per request)."""
    global _key_cache
    if _key_cache is None:
        _key_cache = _load_key()
    return _key_cache


def create_key(*, force: bool = False) -> Ed25519PrivateKey:
    """Explicitly mint the signing key (encrypted iff AEON_KEY_PASS is set). Refuses to
    overwrite an existing key unless force=True — overwriting RE-ANCHORS the trust root
    and invalidates every published attestation, so it must be a deliberate rotation."""
    global _key_cache
    if os.path.exists(KEY_PATH) and not force:
        raise SystemExit(
            f"refusing to overwrite existing key at {KEY_PATH} — rotating RE-ANCHORS the trust "
            f"root and breaks every pinned verifier. Pass --force only for a deliberate rotation.")
    sk = Ed25519PrivateKey.generate()
    passphrase = os.environ.get("AEON_KEY_PASS")
    enc = (serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
           if passphrase else serialization.NoEncryption())
    pem = sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # owner-only
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    _key_cache = sk
    return sk


def public_key_b64() -> str:
    raw = _key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(data: bytes) -> str:
    return base64.b64encode(_key().sign(data)).decode()


def verify(data: bytes, sig_b64: str, pub_b64: str) -> bool:
    try:
        pk = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        pk.verify(base64.b64decode(sig_b64), data)
        return True
    except Exception:
        return False


# ---- attestation statement ----

def runtime_info() -> dict:
    return {"python": platform.python_version(), "platform": platform.platform(),
            "impl": platform.python_implementation(), "argv0": os.path.basename(sys.argv[0] or "")}


def attestation(nonce: str | None = None) -> dict:
    """A signed statement binding build hash + public key + time + caller nonce.

    A verifier: (1) pins our published public_key, (2) sends a fresh random nonce,
    (3) checks the signature, that build_hash equals the known-good release, and that
    ts is fresh. Passing all three proves the live deployment runs the expected code
    AND controls the pinned key (within the software trust boundary above)."""
    body = {
        "v": 1,
        "build_hash": build_hash(),
        "public_key": public_key_b64(),
        "alg": "ed25519",
        "ts": round(time.time(), 3),
        "nonce": nonce or secrets.token_hex(16),
        "runtime": runtime_info(),
    }
    return {**body, "signature": sign(_canon(body))}


def sign_manifest(manifest: dict) -> dict:
    """Signed submission: attach build_hash + a signature over the canonical manifest."""
    m = {**manifest, "build_hash": build_hash(), "public_key": public_key_b64(), "alg": "ed25519"}
    return {**m, "signature": sign(_canon(m))}


def verify_manifest(signed: dict) -> bool:
    s = dict(signed)
    sig = s.pop("signature", None)
    pub = s.get("public_key")
    return bool(sig and pub) and verify(_canon(s), sig, pub)


# ---- model identity attestation ----

def model_attestation(model: str, target_url: str, *, hf_repo: str | None = None,
                      hf_revision: str | None = None, weights_path: str | None = None) -> dict:
    """Record what model was claimed, and verify what we can.

    - weights_path given (local gguf/safetensors): we sha256 the file(s) -> content-verifiable.
    - hf_repo given: caller asserts the upstream identity; verify_model_ref() can fetch
      HF's advertised commit + file hashes to compare against (remote API weights are
      not locally accessible, so for API targets this is identity-of-claim, not of bytes).
    """
    att = {"model": model, "target_url": target_url, "hf_repo": hf_repo,
           "hf_revision": hf_revision, "verified": "claim_only", "ts": round(time.time(), 3)}
    if weights_path and os.path.exists(weights_path):
        att["weights_sha256"] = _sha256_file(weights_path)
        att["verified"] = "content_hashed"
    return att


# FIX(PLAUSIBLE-MED): verify_model_ref is called on every attested ingest (8s timeout,
# outbound HF call), so a repeated repo@rev lookup can stall a worker each time. Bound it
# with a small monotonic-TTL cache (hits AND misses) so identical lookups are near-free.
_MODEL_REF_TTL = 300.0            # seconds
_MODEL_REF_CAP = 512             # size cap so an attacker can't grow the dict unbounded
_model_ref_cache: dict[tuple, tuple[dict, float]] = {}


def verify_model_ref(hf_repo: str, hf_revision: str = "main", timeout: int = 8) -> dict:
    """Fetch HF's advertised reference for a repo@revision (commit sha + safetensors
    index hashes) so a deployment's model_attestation can be checked against the
    canonical upstream. Network, best-effort. Result cached for _MODEL_REF_TTL."""
    import urllib.request
    key = (hf_repo, hf_revision)
    now = time.time()
    hit = _model_ref_cache.get(key)              # cache both hits and misses (short TTL)
    if hit and hit[1] > now:
        return hit[0]
    # ?blobs=true is REQUIRED — without it siblings carry only rfilename (no lfs.sha256),
    # so independent re-verification silently degrades to "claim". Publicly readable, no token.
    url = f"https://huggingface.co/api/models/{hf_repo}/revision/{hf_revision}?blobs=true"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aeon-bench"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        sibs = {s.get("rfilename"): s.get("lfs", {}).get("sha256")
                for s in obj.get("siblings", []) if s.get("rfilename")}
        out = {"hf_repo": hf_repo, "sha": obj.get("sha"), "files": sibs, "ok": True}
    except Exception as e:
        out = {"hf_repo": hf_repo, "ok": False, "error": str(e)[:200]}
    if len(_model_ref_cache) >= _MODEL_REF_CAP:  # crude cap: clear when full (rarely hit)
        _model_ref_cache.clear()
    _model_ref_cache[key] = (out, now + _MODEL_REF_TTL)
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "keygen":
        sk = create_key(force="--force" in args)
        raw = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        enc = "encrypted (AEON_KEY_PASS)" if os.environ.get("AEON_KEY_PASS") else \
            "PLAINTEXT — set AEON_KEY_PASS before keygen to encrypt at rest"
        print(f"wrote signing key: {KEY_PATH} [{enc}]")
        print(f"public_key={base64.b64encode(raw).decode()}")
        print(f"build_hash={build_hash()}")
    elif args and args[0] in ("-h", "--help", "help"):
        print("usage: python -m aeon.attest [keygen [--force]]\n"
              "  keygen          mint the ed25519 signing key (encrypted iff AEON_KEY_PASS set)\n"
              "  keygen --force  rotate (overwrites — RE-ANCHORS the trust root)\n"
              "  (no args)       print a live attestation statement")
    else:
        print(json.dumps(attestation(), indent=2))
