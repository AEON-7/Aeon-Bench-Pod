"""What we tell a harness about the model's window, and how much one turn may emit.

TWO REAL FAILURES, both measured on 2026-08-14:

1. A god task truncated at `finish_reason='length'` on the custom provider's default
   max_tokens=65536 — the model emitted 65k tokens in ONE turn. At the 20-40 tok/s these serves
   sustain that is 27-55 minutes, longer than the whole 1800s task budget, spent on a single
   reply that then gets cut anyway. The per-turn ceiling is the fix; RAISING it would be the bug.

2. The window we declared had drifted from reality in three places at once: hermes said 65536
   ("our serves cap max-model-len at 32K"), openclaw's code said 131072 while its own docstring
   said 32768 — and the recipes serve 229376-262144.

WHAT THE HARNESSES ACTUALLY DO, verified against a live container rather than assumed:
Hermes reads the window off the endpoint itself and auto-compacts — it logged
"Context limit: 262,144 tokens (compress at 75% = 196,608)" against a 262144 serve, ignoring our
declared 65536. So for Hermes the declared value is only a FLOOR that clears its 64K gate.
OpenClaw takes an explicit JSON config with no such auto-detection, so there the real number
matters, and it is asked for rather than hardcoded.

Run: python3 mvp/test_harness_limits.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", os.path.join(tempfile.mkdtemp(prefix="aeon_hlimits_"), "t.db"))

from pod.adapters import base, hermes, openclaw     # noqa: E402

PASS = 0
fails = []


def ok(cond, label, detail=""):
    global PASS
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + detail) if detail else ""))
    if cond:
        PASS += 1
    else:
        fails.append(label)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def with_models(payload):
    """Point base.served_context at a canned /v1/models document."""
    real = base.urllib.request.urlopen
    base.urllib.request.urlopen = lambda *a, **k: _Resp(payload)
    try:
        return base.served_context("http://x/v1")
    finally:
        base.urllib.request.urlopen = real


print("the window is ASKED FOR, so it cannot drift from the serve again")
ok(with_models({"data": [{"id": "m", "max_model_len": 262144}]}) == 262144,
   "max_model_len is read from /v1/models")
ok(with_models({"data": [{"id": "m", "context_length": 131072}]}) == 131072,
   "context_length is accepted as an alias")
ok(with_models({"data": [{"id": "m"}]}) is None,
   "an endpoint that will not say returns None, so the caller uses its floor")

print("a harness config must never be able to fail a benchmark")
ok(base.served_context(None) is None, "no base_url -> None, no raise")
ok(base.served_context("http://127.0.0.1:1/v1", timeout=2) is None,
   "a dead endpoint -> None, no raise")
ok(with_models({"data": [{"id": "m", "max_model_len": "not-an-int"}]}) is None,
   "a junk value is ignored rather than propagated into a config")

print("HERMES: the declared window is a FLOOR (it auto-detects), the output ceiling is ours")
a = hermes.HermesAdapter()
a._run_dir = tempfile.mkdtemp(prefix="aeon_hcfg_")
cfg = open(a._ensure_cfg(), encoding="utf-8").read()
ok("context_length: 65536" in cfg,
   "the 64K gate is cleared, so Hermes never refuses the model outright")
ok(("max_tokens: %d" % hermes._MAX_OUTPUT_TOKENS) in cfg,
   "and one turn is capped — the default 65536 is what truncated a god task", cfg.strip())
ok(hermes._MAX_OUTPUT_TOKENS < 65536,
   "the cap is BELOW the provider default; raising it would let one turn eat the task budget",
   str(hermes._MAX_OUTPUT_TOKENS))

print("OPENCLAW: no auto-detection, so it is handed the real window")
real = base.urllib.request.urlopen
base.urllib.request.urlopen = lambda *a, **k: _Resp({"data": [{"id": "m", "max_model_len": 262144}]})
try:
    m = openclaw.build_config("http://x/v1", "alias")["models"]["providers"]["dgx"]["models"][0]
finally:
    base.urllib.request.urlopen = real
ok(m["contextWindow"] == 262144, "contextWindow comes from the endpoint, not a literal",
   str(m["contextWindow"]))
ok(m["maxTokens"] == openclaw._MAX_OUTPUT_TOKENS, "and maxTokens is the same bounded ceiling",
   str(m["maxTokens"]))

m2 = openclaw.build_config("http://127.0.0.1:1/v1", "alias")["models"]["providers"]["dgx"]["models"][0]
ok(m2["contextWindow"] == openclaw._CTX_FALLBACK,
   "an unreachable endpoint falls back rather than writing a broken config",
   str(m2["contextWindow"]))

print("the ceiling never crowds out the input budget")
real = base.urllib.request.urlopen
base.urllib.request.urlopen = lambda *a, **k: _Resp({"data": [{"id": "m", "max_model_len": 8192}]})
try:
    small = openclaw.build_config("http://x/v1", "a")["models"]["providers"]["dgx"]["models"][0]
finally:
    base.urllib.request.urlopen = real
ok(small["maxTokens"] <= small["contextWindow"] // 4,
   "on a small window the per-turn cap scales down with it",
   "%d of %d" % (small["maxTokens"], small["contextWindow"]))

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all harness-limit tests passed (%d checks)" % PASS)
