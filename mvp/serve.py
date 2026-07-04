"""Start the AEON Bench MVP mothership.

    python serve.py            # http://localhost:8080
    AEON_PORT=9000 python serve.py
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("AEON_PORT", "8080"))
    host = os.environ.get("AEON_HOST", "0.0.0.0")
    print(f"AEON Bench MVP -> http://localhost:{port}  (bind {host}:{port})")
    uvicorn.run("aeon.app:app", host=host, port=port, log_level="info")
