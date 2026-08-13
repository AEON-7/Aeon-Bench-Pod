"""A bench must not start when max_tokens cannot fit inside the served context.

Serving at exactly DEFAULT_MAX_TOKENS looks reasonable — 65536 is both our default generation
budget and a common --max-model-len — but it leaves ZERO room for the prompt, so the engine 400s
every request. Observed on a real god run: all 24 cases failed instantly and identically, surfaced
only as "endpoint answered none of the 24 attempted cases (every attempt failed in transport)",
which reads like a dead endpoint and sends you debugging the engine instead of the arithmetic.

Nothing clamps max_tokens to the served window, so this guard is the only thing standing between
that arithmetic and a full model load followed by 24 doomed requests.

Run: python3 mvp/test_token_headroom.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", os.path.join(BASE, ".test_headroom_ignore.db"))

from pod.aeon_pod import DEFAULT_MAX_TOKENS, _assert_token_headroom   # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    if not cond:
        print("FAIL  %s" % label)
        sys.exit(1)
    PASS += 1
    print("ok    %s" % label)


def raises(recipe, max_tokens):
    try:
        _assert_token_headroom(recipe, max_tokens)
        return False
    except SystemExit:
        return True


# ---- the exact shape that burned a run -------------------------------------------------------
ok(raises({"context_len": 65536}, 65536),
   "max_tokens == context is refused (the 400-every-request case)")
ok(raises({"context_len": 65536}, 131072),
   "max_tokens larger than context is refused")
ok(raises({"context_len": 40960}, DEFAULT_MAX_TOKENS),
   "the default budget against a smaller served context is refused")

# ---- healthy configurations still start --------------------------------------------------------
ok(not raises({"context_len": 262144}, 65536),
   "native 256k context with a 64k budget starts (196k of prompt headroom)")
ok(not raises({"context_len": 131072}, 65536),
   "exactly 2x the budget starts")

# ---- absent information must never block a run -------------------------------------------------
# An external endpoint the pod did not launch reports no context_len. Refusing there would break
# every --serve-url run to protect against arithmetic we cannot do.
ok(not raises({}, 65536), "no context_len known -> no opinion, run proceeds")
ok(not raises(None, 65536), "no recipe at all -> no opinion")
ok(not raises({"context_len": 0}, 65536), "context_len 0 (unknown) -> no opinion")
ok(not raises({"context_len": 262144}, 0), "no max_tokens -> no opinion")
ok(not raises({"context_len": "not-a-number"}, 65536), "junk context_len is ignored, not fatal")

# ---- the near-miss warns rather than blocking ---------------------------------------------------
# 1024 tokens of headroom is legal and short prompts will pass, so blocking would be wrong — but it
# is worth saying out loud, because the failure it precedes is silent and total.
ok(not raises({"context_len": 65536 + 1024}, 65536),
   "tight-but-legal headroom starts (warns instead of blocking)")

print("\nOK  token headroom guard: %d checks passed" % PASS)
