"""Optional inline AV scan of untrusted UPLOADED artifact bytes, via clamd (INSTREAM).

Why field-selective (see docs/data-and-security.md): the signed results bundle is ONE
JSON body that mixes (a) generated arena artifact HTML — untrusted content this platform
will later SERVE to voters — with (b) model prompt/response text, which is arbitrary LLM
output (code, base64, exploit discussion, EICAR-lookalikes) that trips AV signatures
constantly. So AV runs ONLY over artifact HTML at ingest._save_artifacts — NEVER over
results[*].raw_output / evidence. A byte-level scan of the whole POST body at the WAF
would false-positive on model output; that is why this lives in the app, not the edge.

Disabled unless AEON_CLAMD_HOST is set (pods / dev = no-op). Uses only the stdlib.
Fail-OPEN on any clamd error: AV here is defense-in-depth (artifacts are additionally
rendered in a `sandbox="allow-scripts"` iframe with no same-origin), so a scanner outage
must not break result ingestion — it logs and lets the artifact through.
"""
from __future__ import annotations

import os
import socket
import struct

_HOST = os.environ.get("AEON_CLAMD_HOST")
_PORT = int(os.environ.get("AEON_CLAMD_PORT", "3310"))
_TIMEOUT = float(os.environ.get("AEON_CLAMD_TIMEOUT", "8"))
_CHUNK = 65536

enabled = bool(_HOST)


def scan(data) -> tuple[bool, str]:
    """Scan bytes/str. Returns (clean, detail):
      - (True,  "av:disabled")        AV not configured — no-op
      - (True,  "av:clean")           clamd says OK
      - (True,  "av:error:<Type>")    clamd unreachable/timeout — FAIL-OPEN (allow)
      - (False, "av:found:<sig>")     clamd matched a signature — caller must reject/quarantine
    """
    if not enabled:
        return True, "av:disabled"
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    try:
        with socket.create_connection((_HOST, _PORT), timeout=_TIMEOUT) as s:
            s.settimeout(_TIMEOUT)
            s.sendall(b"zINSTREAM\0")
            for i in range(0, len(data), _CHUNK):
                chunk = data[i:i + _CHUNK]
                s.sendall(struct.pack("!I", len(chunk)) + chunk)
            s.sendall(struct.pack("!I", 0))            # zero-length chunk = end of stream
            resp = b""
            while b"\0" not in resp and len(resp) < 4096:
                buf = s.recv(4096)
                if not buf:
                    break
                resp += buf
    except Exception as e:                              # connect/timeout/reset → fail-open
        return True, f"av:error:{type(e).__name__}"
    text = resp.decode("utf-8", "replace").strip("\0 \r\n")
    if text.endswith("FOUND"):
        # format: "stream: Eicar-Test-Signature FOUND"
        sig = text.split(":", 1)[-1].replace("FOUND", "").strip() or "unknown"
        return False, f"av:found:{sig}"
    return True, "av:clean"
