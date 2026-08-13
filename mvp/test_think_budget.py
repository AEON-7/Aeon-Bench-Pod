"""A thinking budget is not a smaller max_tokens — it is what stops a null.

A reasoning model on a hard case spends its whole allowance thinking, never closes the block, and
returns EMPTY content. The suite has to score that as a no-answer, so the hardest questions — the
ones a GOD MODE suite is made of — are exactly the ones that produce no information.

vLLM's thinking_token_budget CLOSES the reasoning block at the budget, leaving the rest of
max_tokens for an actual answer. Measured on Qwen3.6-27B, one hard prompt at max_tokens=3000:

    unbounded        finish=length  8,543 reasoning chars  0 content chars      <- a null
    budget=600       finish=stop    1,899 reasoning chars  2,282 content chars  <- an answer

Fewer than half the tokens, and a gradeable result instead of nothing.

Run: python3 mvp/test_think_budget.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_think_budget_test.db")

from aeon.runner import build_target   # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


URL, MODEL = "http://127.0.0.1:8000/v1", "aeon"


def payload(budget, max_tokens=32768):
    t = build_target(MODEL, URL, None, think_budget=budget)
    return t, t._apply_extra({"model": MODEL, "max_tokens": max_tokens, "temperature": 0.0},
                             max_tokens)


print("the budget reaches the wire")
t, p = payload(16384)
check("extra_body carries thinking_token_budget", t.extra_body == {"thinking_token_budget": 16384})
check("and it lands in the request payload", p.get("thinking_token_budget") == 16384)
check("max_tokens is UNTOUCHED — the budget caps THINKING, not the answer",
      p.get("max_tokens") == 32768)

print("absent unless asked for (never changes an existing run's shape)")
for val in (None, 0):
    t, p = payload(val)
    check("think_budget=%r -> no extra_body" % val, t.extra_body == {})
    check("think_budget=%r -> key absent from payload" % val,
          "thinking_token_budget" not in p)

print("accepts what a CLI hands it")
t, p = payload("16384")
check("a string budget is coerced to int", p.get("thinking_token_budget") == 16384)

print("the judge is deliberately NOT budgeted")
import inspect  # noqa: E402
src = inspect.getsource(sys.modules["aeon.runner"])
i = src.index("target = build_target(model, target_url, api_key")
j = src.index("build_target(judge_model", i)
check("the model under test gets think_budget", "think_budget=params.get" in src[i:i + 200])
check("the judge target does NOT (capping a judge changes grading, not the graded)",
      "think_budget" not in src[j:j + 160])

print("mock/frontier targets are unaffected")
check("mock ignores it", build_target("m", "mock", None, think_budget=16384) is not None)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all think-budget tests passed")
