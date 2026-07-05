"""aeon-submit — reference pod-side client for the AEON Bench submission channel.

OPEN SOURCE (ships in the `aeon-pod` repo). It implements the full trust-chain P0 protocol
so anyone can audit exactly what a pod sends upstream:

  1. enroll  — prove possession of a locally-generated ed25519 *device key*
  2. open    — request a single-use run nonce + run-scoped token (request is signed)
  3. submit  — send the ed25519-signed results bundle (inert data; schema-validated server-side)

The device PRIVATE key never leaves the pod. The mothership only ever sees the public key,
detached signatures, and the results bundle. Submissions are stored `self_reported` (not
record-eligible) — honesty by construction. See docs/trust-architecture.md.

Usage:
    python -m pod.aeon_submit --base https://aeon-bench.com \
        --model "gemma-3-27b-it" --suite-id aeon-suite-v1 --results results.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_KEY = os.path.expanduser("~/.aeon/device_key.pem")

# Transient statuses to retry (edge/Cloudflare/WAF/mothership blips) — a one-off failure must NOT
# discard an entire completed benchmark run. 403 is retried too: the observed failure was a transient
# CF 403 on the enroll GET; a genuine app-403 (e.g. NOT_ATTESTED) just costs a few short retries then
# surfaces to the caller with its body intact.
_RETRY_CODES = {403, 408, 425, 429, 500, 502, 503, 504}
_UA = "aeon-pod/1.0 (+https://aeon-bench.com)"


def _canon(obj) -> bytes:
    """Canonical JSON — MUST match the mothership's ingest._canon byte-for-byte."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_or_create_key(path: str = DEFAULT_KEY):
    """Load the pod's device key, generating a fresh ed25519 keypair on first use (chmod 600)."""
    if os.path.exists(path):
        with open(path, "rb") as f:
            sk = serialization.load_pem_private_key(f.read(), password=None)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        sk = Ed25519PrivateKey.generate()
        with open(path, "wb") as f:
            f.write(sk.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    pub_raw = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return sk, base64.b64encode(pub_raw).decode()


class Pod:
    def __init__(self, base: str, key_path: str = DEFAULT_KEY):
        self.base = base.rstrip("/")
        self.sk, self.pub = load_or_create_key(key_path)

    def _sign(self, data: bytes) -> str:
        return base64.b64encode(self.sk.sign(data)).decode()

    def _open(self, req, timeout, retries=5):
        """urlopen with backoff retry on transient statuses + network errors, so a passing edge/WAF
        blip never discards a completed run. Re-raises the final error if all attempts fail — a
        genuine 4xx still surfaces, a persistent outage still errors."""
        last = None
        for attempt in range(retries):
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                last = e
                if e.code not in _RETRY_CODES or attempt == retries - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt == retries - 1:
                    raise
            time.sleep(min(2 ** attempt, 20))            # 1, 2, 4, 8, 16 s
        raise last

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"User-Agent": _UA})
        with self._open(req, 20) as r:
            return json.loads(r.read())

    def _post(self, path, obj, headers=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json", "User-Agent": _UA, **(headers or {})}, method="POST")
        try:
            with self._open(req, 120) as r:               # 120s: large signed bundles over the tunnel
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())           # genuine app error after retries -> code+body

    def enroll(self):
        ch = self._get("/api/v1/enroll/challenge")["challenge"]
        return self._post("/api/v1/enroll",
                          {"public_key": self.pub, "challenge": ch, "signature": self._sign(ch.encode())})

    def open_run(self, model, suite_id, board="text"):
        body = {"action": "open_run", "public_key": self.pub, "model": model,
                "suite_id": suite_id, "board": board}
        return self._post("/api/v1/runs",
                          {"public_key": self.pub, "signature": self._sign(_canon(body)),
                           "model": model, "suite_id": suite_id, "board": board})

    def submit(self, run_id, run_nonce, run_token, results, *, final=True, **extra):
        bundle = {"run_id": run_id, "run_nonce": run_nonce, "results": results, "final": final, **extra}
        return self._post("/api/v1/runs/%s/results" % run_id,
                          {"bundle": bundle, "signature": self._sign(_canon(bundle))},
                          headers={"X-Aeon-Run-Token": run_token})

    def run_and_submit(self, model, suite_id, results, board="text", **extra):
        self.enroll()                                   # idempotent
        st, r = self.open_run(model, suite_id, board)
        if st != 200:
            return st, r
        return self.submit(r["run_id"], r["run_nonce"], r["run_token"], results, **extra)


def main():
    ap = argparse.ArgumentParser(description="Submit AEON Bench results to a mothership.")
    ap.add_argument("--base", required=True, help="mothership URL, e.g. https://aeon-bench.com")
    ap.add_argument("--model", required=True, help="HF model id being benchmarked")
    ap.add_argument("--suite-id", default="aeon-suite-v1")
    ap.add_argument("--board", default="text")
    ap.add_argument("--results", required=True,
                    help="path to a JSON file: [{case_id, category, score, raw_output, ...}, ...]")
    ap.add_argument("--key", default=DEFAULT_KEY, help="device key path (created on first use)")
    a = ap.parse_args()
    with open(a.results, encoding="utf-8") as f:
        results = json.load(f)
    st, r = Pod(a.base, a.key).run_and_submit(a.model, a.suite_id, results, board=a.board)
    print(json.dumps(r, indent=2))
    raise SystemExit(0 if st == 200 else 1)


if __name__ == "__main__":
    main()
