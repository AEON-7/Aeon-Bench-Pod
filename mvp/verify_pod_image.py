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
import os
import sys

sys.path.insert(0, "/app/mvp")
ok = fail = 0

# Piped in over stdin (`python3 - < …`), so there is no __file__ — locate the tree through an
# imported package instead, which also lets this run against a local checkout unchanged.
import aeon as _aeon                                             # noqa: E402
BASE = os.path.dirname(os.path.dirname(os.path.abspath(_aeon.__file__)))


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

# ---- terminal wall ------------------------------------------------------------------------------
# The wall is worth verifying IN THE IMAGE because it spans three files that ship independently:
# the buffer (pod/), the publish call inside the decode loop (aeon/targets.py) and the endpoint
# (aeon/app.py). Any one of them missing degrades silently to "no streams", which looks identical
# to an idle bench — the exact failure the wall exists to eliminate.
from pod import livestreams
check("terminal-wall buffer present",
      all(hasattr(livestreams, n) for n in ("enable", "begin", "chunk", "end", "snapshot", "read")))
check("bench enables the wall at launch", "livestreams.enable(True)" in src)
_tsrc = open(os.path.join(BASE, "aeon", "targets.py"), encoding="utf-8").read()
check("deltas published from inside the decode loop",
      '_ls.chunk(_live_cid, reasoning, "reasoning")' in _tsrc
      and '_ls.chunk(_live_cid, c, "answer")' in _tsrc)
_rsrc = open(os.path.join(BASE, "aeon", "runner.py"), encoding="utf-8").read()
check("every result closes its terminal with a verdict", "livestreams.end(res[\"cid\"]" in _rsrc)
_asrc = open(os.path.join(BASE, "aeon", "app.py"), encoding="utf-8").read()
check("wall endpoint served", '@app.get("/api/pod/streams")' in _asrc)
livestreams.clear()
livestreams.enable(True)
livestreams.begin("verify.case.01")
livestreams.chunk("verify.case.01", "thinking", "reasoning")
livestreams.chunk("verify.case.01", "answered", "answer")
livestreams._write(livestreams.snapshot())
_wall = livestreams.read()
check("wall crosses the process boundary",
      _wall["live"] and _wall["streams"] and _wall["streams"][0]["answer"] == "answered")
livestreams.clear()
_js = open(os.path.join(BASE, "web", "app.js"), encoding="utf-8", errors="replace").read()
check("dashboard polls + renders the wall",
      "/api/pod/streams" in _js and "function streamWall(" in _js and "streamWall()" in _js)
# The racing dash reads the engine. Gating that fetch on a job hid it from every hand-launched run;
# gating it on "anything looks live" hid it during a quiet agentic phase, when it is the only proof
# of life left. Assert the ungated form, and that neither old gate came back.
check("engine telemetry fetched unconditionally",
      'try { tele = await api("/api/pod/stats"' in _js
      and 'if (job) { try { tele = await api("/api/pod/stats"' not in _js
      and 'if (_anyLive) { try { tele = await api("/api/pod/stats"' not in _js)
# Completion leads the page: statsHtml is concatenated before the instruments, the wall and the log.
_order = [_js.find(x) for x in ('statsHtml.join("")', "pipeStrip(false)", "streamWall()",
                                'feedHtml.join("")', "liveTerminal()")]
check("live view puts completion stats first",
      all(i > 0 for i in _order[:2]) and _order[0] < _order[1] < _js.find("queueStrip(queued);      "))
check("a failing /api/live no longer blanks the live view",
      "lost = true;" in _js and "Run progress unavailable" in _js)
check("peak-hold survives a phase change", "_LIVE_RUN_KEY" in _js)

# ---- live progress denominators ----------------------------------------------------------------
# A god run is the god_mode tier alone; dividing its progress by the whole suite pinned every bar
# near 12% for the entire run.
from aeon import app as _app
from aeon import suite as _suite
_full = _app._suite_cat_counts()
_gcount = sum(_app._difficulty_cat_counts().get("god_mode", {}).values())
_plan = _app._plan_cat_counts(_gcount, suite_id=_suite.SUITE_ID)
check("god-tier run divides by the god tier, not the suite",
      sum(_plan.values()) == _gcount and _plan != _full, "%d cases" % _gcount)
check("a full run is unchanged",
      _app._plan_cat_counts(len(_suite.CASES), suite_id=_suite.SUITE_ID) == _full)
check("another suite is never matched against text tiers",
      _app._plan_cat_counts(_gcount, suite_id="aeon-vision-v2") == _full)
_css = open(os.path.join(BASE, "web", "styles.css"), encoding="utf-8").read()
check("wall is styled", ".sw-grid" in _css and ".sw-reason" in _css)

# THE AGENTIC PHASE IS 40% OF GOD SCORE AND USED TO RENDER AS ONE COUNTER. The publisher lives in
# the bench process and the panels in the dashboard, so a broken image looks identical to a working
# one until a god run reaches its harnesses — hours in. Assert both ends of that seam here.
from pod import harness_stream                                       # noqa: E402
from pod.adapters import base as _adapters_base                      # noqa: E402
livestreams.enable(False)          # an earlier check left it on; assert the OFF state deliberately
_o = harness_stream.observer("hermes", "av2-01")                     # wall off -> inert, never None
check("harness observers are safe when the wall is off", _o is harness_stream.NULL)
livestreams.enable(True)
_h1 = harness_stream.observer("hermes", "av2-01-compute-write")
_h2 = harness_stream.observer("openclaw", "av2-01-compute-write")
check("the same task under two harnesses does not collide into one tile", _h1.cid != _h2.cid)
_h1("\U0001f504 Making API call #2/8...")
_snap = {s["case"]: s for s in livestreams.snapshot(limit=16)}
check("harness output reaches the wall",
      "hermes:av2-01-compute-write" in _snap
      and "API call #2/8" in _snap["hermes:av2-01-compute-write"]["answer"])
_h1.close(); _h2.close()
livestreams.enable(False)
livestreams.clear()
check("adapters can stream container output",
      "on_line" in inspect.signature(_adapters_base.run_container_io).parameters
      and "on_line" in inspect.signature(_adapters_base.run_argv).parameters)
check("all three harnesses publish",
      all("harness_stream.observer(" in open(os.path.join(BASE, "pod", "adapters", f + ".py"),
                                             encoding="utf-8", errors="replace").read()
          for f in ("hermes", "openclaw", "opencode")))
check("three harness panels render", "function harnessWall(" in _js
      and all(n in _js for n in ("hermes", "openclaw", "opencode")))
check("harness panels are styled",
      ".hw-grid" in _css and ".hw-active" in _css and ".hw-idle" in _css)

print()
print("PUBLISHED IMAGE: %d ok, %d failed" % (ok, fail))
sys.exit(1 if fail else 0)
