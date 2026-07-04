"""Watch an OpenAI-compatible endpoint (LM Studio) and auto-run the benchmark
the moment a matching model (default: gemma) is loaded and reachable.

    PYTHONPATH=. python watch_lmstudio.py
    AEON_API_KEY=<token> AEON_MATCH=gemma AEON_LMS_URL=http://127.0.0.1:1234/v1 ...

Exits after running the benchmark (so the caller is notified), or after a
timeout. Polls quietly; only prints on state changes + the final result.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aeon import runner, scoring                       # noqa: E402
from aeon.targets import _ipv4                         # noqa: E402

TARGET = _ipv4(os.environ.get("AEON_LMS_URL", "http://127.0.0.1:1234/v1").rstrip("/"))
KEY = os.environ.get("AEON_API_KEY") or None
MATCH = os.environ.get("AEON_MATCH", "gemma").lower()
POLL = int(os.environ.get("AEON_POLL", "15"))
MAX_MIN = int(os.environ.get("AEON_MAX_MIN", "90"))


def probe():
    """(state, payload): refused | auth | error | empty | ready."""
    headers = {"Authorization": f"Bearer {KEY}"} if KEY else {}
    try:
        req = urllib.request.Request(TARGET + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read())
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        gem = [i for i in ids if MATCH in i.lower()]
        return ("ready", gem[0]) if gem else ("empty", ids)
    except urllib.error.HTTPError as e:
        return ("auth", None) if e.code == 401 else ("error", f"HTTP {e.code}")
    except Exception as e:
        return ("refused", str(e)[:80])


def main():
    print(f"[watch] target={TARGET} match='{MATCH}' key={'set' if KEY else 'none'} "
          f"poll={POLL}s max={MAX_MIN}m", flush=True)
    deadline = time.monotonic() + MAX_MIN * 60
    last = None
    while time.monotonic() < deadline:
        state, payload = probe()
        msg = {
            "refused": "server not reachable on :1234",
            "auth": "401 — turn OFF 'Require API key' in LM Studio, or restart me with AEON_API_KEY=<token>",
            "error": f"error: {payload}",
            "empty": f"server up, loaded models = {payload} (waiting for a '{MATCH}' model)",
            "ready": f"FOUND model: {payload}",
        }[state]
        if msg != last:
            print(f"[watch] {state}: {msg}", flush=True)
            last = msg
        if state == "ready":
            model = payload
            rid = uuid.uuid4().hex[:10]
            print(f"[watch] running benchmark on '{model}' (self-judged) ...", flush=True)

            def cb(cid, score, status):
                s = f"{score:.2f}" if isinstance(score, float) else str(score)
                print(f"   {cid:24s} {status:12s} {s}", flush=True)

            runner.run_benchmark(rid, model, TARGET, judge_model=None,
                                 progress_cb=cb, api_key=KEY)
            print(f"[watch] DONE (run {rid}). Leaderboard:", flush=True)
            for m in scoring.leaderboard()["models"]:
                print(f"  {m['composite']:6.1f}  {m['model']:30s} "
                      f"tok/s={m['avg_decode_tps']}  {m['categories']}", flush=True)
            return
        time.sleep(POLL)
    print("[watch] timed out — Gemma never became reachable (auth still on? model not loaded?)", flush=True)


if __name__ == "__main__":
    main()
