"""Smoke-verify a PUBLISHED pod image: does it actually DO what its commit claims?

    docker exec -i aeon-pod python3 - < mvp/verify_pod_image.py

A matching commit hash proves what was BUILT, not what works — a file can be present and a wiring
change missed (this session shipped a --think-budget whose kwarg never reached _run_boards, and the
run died at launch). This exercises each behaviour inside the container a stranger gets from
`docker pull`, so "published" and "working" are the same claim.

Run it after every image publish, and extend it whenever a fix ships that a user would notice.
Original purpose: verifying the 2026-08-13 batch (64k ceiling, think budget, true-fail no-answer,
judge variance, cache-bust salt, averaged boards, artifact budget, live feed).

A matching commit hash proves what was built, not what works. This exercises each change inside
the running container — the same image a stranger gets from `docker pull`.
"""
import sys

sys.path.insert(0, "/app/mvp")
ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print("  ok   %-52s %s" % (name, detail))
    else:
        fail += 1
        print("  FAIL %-52s %s" % (name, detail))


# ---- generation ceiling + truncation escalation -------------------------------------------------
from pod.aeon_pod import DEFAULT_MAX_TOKENS
check("generation ceiling is 64k", DEFAULT_MAX_TOKENS == 65536, str(DEFAULT_MAX_TOKENS))

import inspect
from pod import aeon_pod
src = inspect.getsource(aeon_pod)
check("truncation re-run armed by default (2x)", "a.retry_max_tokens = a.max_tokens * 2" in src)
check("--think-budget flag present", "--think-budget" in src)
check("think_budget accepted by _run_boards",
      "think_budget" in inspect.signature(aeon_pod._run_boards).parameters)
check("live feed installed at launch", "livelog.install()" in src)

# ---- thinking budget reaches the wire -----------------------------------------------------------
from aeon.runner import build_target, RETRY_PASSES
t = build_target("m", "http://x/v1", None, think_budget=16384)
p = t._apply_extra({"model": "m", "max_tokens": 65536, "temperature": 0}, 65536)
check("think budget rides the request body", p.get("thinking_token_budget") == 16384)
check("and max_tokens is untouched", p.get("max_tokens") == 65536)
check("one attempt + five retries", RETRY_PASSES == 6, "RETRY_PASSES=%d" % RETRY_PASSES)

# ---- no-answer is a true fail -------------------------------------------------------------------
rsrc = inspect.getsource(sys.modules["aeon.runner"])
check("exhausted no-answer scores 0.0, not NULL", '"score": 0.0' in rsrc and '"true_fail": True' in rsrc)

# ---- judge robustness ---------------------------------------------------------------------------
from aeon import evaluators as E
T = "CfL5wRDdZqB2411"
for label, cand in (("**bold**", "**%s**" % T), ("Answer: x.", "Answer: %s." % T),
                    ("fenced", "```\n%s\n```" % T), ("curly quotes", "“%s”" % T),
                    ("preamble line", "Let me think.\n%s" % T),
                    ("double space", "my  ledger")):
    want = "my ledger" if cand == "my  ledger" else T
    check("judge accepts %s" % label, E.chk_exact_match(cand, {"value": want})[0])
check("judge still rejects a wrong value",
      E.chk_exact_match("CfL5wRDdZqB2412", {"value": T})[0] is False)
check("brace-balanced boxed", E.extract_boxed(r"\boxed{\frac{1}{2}}") == r"\frac{1}{2}")

# ---- perf: cache-bust salt ----------------------------------------------------------------------
from pod import perf_grid
check("cache-bust salted per run", hasattr(perf_grid, "_RUN_NONCE"))
check("salt makes levels distinct",
      perf_grid._bust("x", 0, "a-") != perf_grid._bust("x", 0, "b-"))
check("engine-counter throughput", "_engine_tokens" in inspect.getsource(perf_grid))

# ---- scoring: averaging + god section -----------------------------------------------------------
from aeon import scoring, cards
check("board averages a model's passes", hasattr(scoring, "_avg_sentinels"))
check("agentic floor applied per pass", hasattr(scoring, "_avg_harness"))
check("god is a first-class card section", "god" in cards._SINGLE_BOARDS)
check("cards count unanswered cases", "run_category_weighted_scores" in inspect.getsource(cards))

# ---- artifacts ----------------------------------------------------------------------------------
from pod import arena_gen
check("artifact cap is 50MB", arena_gen.MAX_HTML_BYTES == 50 * 1024 * 1024)
check("per-bundle artifact budget", hasattr(arena_gen, "fit_bundle"))
check("completeness check present", arena_gen.is_complete("<html><body>x</body></html>") is True)

# ---- sustained load ----------------------------------------------------------------------------
from pod import kvwatch
check("sustained-load watcher present", hasattr(kvwatch, "begin") and hasattr(kvwatch, "finish"))
check("metrics url derived from the target",
      kvwatch._metrics_url("http://h:8000/v1") == "http://h:8000/metrics")
_w = kvwatch.Watcher("http://x/v1")
_w.points = [{"t": 1000.0 + 15 * i, "kv_pct": kv, "gen_tok_rate": tps, "running": 8,
              "waiting": 0, "preempt_d": pre}
             for i, (kv, tps, pre) in enumerate(
                 [(0.10, 400, 0), (0.12, 396, 0), (0.15, 404, 0),
                  (0.40, 300, 0), (0.45, 296, 1), (0.50, 304, 2),
                  (0.85, 200, 5), (0.88, 196, 6), (0.92, 204, 7)])]
_s = _w.summary()
check("degradation computed from terciles", _s and _s["degradation_pct"] == 50.0,
      str(_s and _s["degradation_pct"]))
check("preemptions counted", _s and _s["preemptions"] == 21)
check("silent when it cannot compare regimes", kvwatch.Watcher("http://x/v1").summary() is None)
check("rides the perf bundle as rows",
      len(perf_grid.sustained_rows(_s, _w.series())) == 2)
check("and no rows when there is no summary", perf_grid.sustained_rows(None, []) == [])
check("bench starts the watcher at engine-ready", "kvwatch.begin(target)" in src)
check("and harvests it at perf submit", "kvwatch.finish()" in src)

# ---- live feed ----------------------------------------------------------------------------------
from pod import livelog
livelog.emit("[verify] image check", "stage")
recs, latest, age = livelog.tail()
check("live feed writes + reads", any("[verify]" in (r.get("s") or "") for r in recs))

print()
print("PUBLISHED IMAGE: %d ok, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
