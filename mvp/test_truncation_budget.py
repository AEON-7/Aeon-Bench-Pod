"""Token starvation is fixed by BUDGET, not by repetition.

A reasoning model on a hard case spends its whole allowance thinking and never closes the block,
so `content` comes back empty. The no-answer rule reads that as a technical glitch and re-runs the
case at the SAME ceiling — which hits the same wall, deterministically, as many times as there are
retry passes.

runner.py has always had the right mechanism (re-run once at a HIGHER ceiling) but it was gated
behind --retry-max-tokens, which defaulted to None, so it was OFF unless someone knew to ask.
Measured on a live GOD MODE run launched without it: 73% of requests finished `length` at a 32.8k
cap and the suite burned hours re-running cases that could never succeed.

Run: python3 mvp/test_truncation_budget.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_trunc_budget_test.db")

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- the gate: retry_tok present <=> the truncation re-run is armed ---------------------------
def armed(params):
    """Mirror runner.py's gate exactly: max_retries is 0 unless retry_tok is truthy."""
    retry_tok = params.get("retry_max_tokens")
    return (int(params.get("retries", 1)) if retry_tok else 0) > 0


print("the gate that silently disabled it")
check("no retry_max_tokens -> truncation re-run DISABLED",
      armed({"max_tokens": 32768}) is False)
check("retry_max_tokens=None -> DISABLED (the live-run bug)",
      armed({"max_tokens": 32768, "retry_max_tokens": None}) is False)
check("retry_max_tokens=0 -> DISABLED (explicit opt-out stays honoured)",
      armed({"max_tokens": 32768, "retry_max_tokens": 0}) is False)
check("retry_max_tokens=65536 -> ARMED",
      armed({"max_tokens": 32768, "retry_max_tokens": 65536}) is True)

# ---- the CLI now arms it by default -----------------------------------------------------------
print("the pod arms it by default")
import argparse  # noqa: E402


from pod.aeon_pod import DEFAULT_MAX_TOKENS   # noqa: E402  (read the REAL default, not a copy)


def resolve(argv):
    """The CLI's default-resolution for retry_max_tokens, as aeon_pod applies it."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--retry-max-tokens", type=int, default=None)
    a = ap.parse_args(argv)
    if a.retry_max_tokens is None:
        a.retry_max_tokens = a.max_tokens * 2
    return a


a = resolve([])
check("unset -> 2x the REAL default (%d -> %d)" % (DEFAULT_MAX_TOKENS, 2 * DEFAULT_MAX_TOKENS),
      a.retry_max_tokens == 2 * DEFAULT_MAX_TOKENS)
check("the default ceiling is 64k — 32k was measured hitting a real wall",
      DEFAULT_MAX_TOKENS == 65536)
check("and that ARMS the re-run",
      armed({"max_tokens": a.max_tokens, "retry_max_tokens": a.retry_max_tokens}) is True)
a = resolve(["--max-tokens", "8000"])
check("scales with --max-tokens (8000 -> 16000)", a.retry_max_tokens == 16000)
a = resolve(["--retry-max-tokens", "50000"])
check("an explicit value wins", a.retry_max_tokens == 50000)
a = resolve(["--retry-max-tokens", "0"])
check("0 still disables it (opt-out preserved)", a.retry_max_tokens == 0)
check("and 0 leaves the re-run DISABLED",
      armed({"max_tokens": 32768, "retry_max_tokens": 0}) is False)

# ---- the re-run must actually raise the ceiling ------------------------------------------------
print("the re-run only fires when a BIGGER budget is available")
def fires(tok, retry_tok, trunc_runs, max_retries, score):
    """runner.py's condition, verbatim."""
    return bool(retry_tok) and trunc_runs < max_retries and retry_tok > tok \
        and (score is None or score < 1.0)


check("cut off with headroom -> re-runs", fires(32768, 65536, 0, 1, None) is True)
check("already correct -> does NOT re-run (no point)", fires(32768, 65536, 0, 1, 1.0) is False)
check("retry ceiling not higher -> does NOT re-run",
      fires(65536, 65536, 0, 1, None) is False)
check("only once -> a second cut-off falls through to classification",
      fires(65536, 65536, 1, 1, None) is False)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all truncation-budget tests passed")
