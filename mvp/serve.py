"""Start the AEON Bench MVP mothership.

    python serve.py            # http://localhost:8080
    AEON_PORT=9000 python serve.py
"""
import os
import os as _os

# Apple-silicon Docker Desktop (aarch64 + linuxkit kernel): OpenSSL's ARM capability probe hits
# an instruction the VM doesn't support -> SIGILL before the first log line (cryptography 49 /
# macOS 26 observed). Disable the probe THERE ONLY — real ARM hosts (DGX Grace) keep hw crypto.
try:
    if _os.uname().machine == "aarch64" and "linuxkit" in _os.uname().release:
        _os.environ.setdefault("OPENSSL_armcap", "0")
except AttributeError:
    pass                                   # non-POSIX (Windows dev) — not applicable

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("AEON_PORT", "8080"))
    host = os.environ.get("AEON_HOST", "0.0.0.0")
    print(f"AEON Bench MVP -> http://localhost:{port}  (bind {host}:{port})")
    uvicorn.run("aeon.app:app", host=host, port=port, log_level="info")
