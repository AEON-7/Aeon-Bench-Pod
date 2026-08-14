"""Live harness activity reaches the terminal wall.

The agentic phase is 40% of GOD SCORE and runs for hours, and it used to publish NOTHING: the wall
is fed by our own streaming client, and harness traffic goes container -> engine without passing
through it. Measured on a live run: `/api/pod/streams` returned `{"n_total": 0}` while four Hermes
containers were mid-task and the engine reported 4 requests running.

The three properties below are the ones that make the difference between a useful wall and a
misleading one, and each is a real hazard in the code this plugs into, not a hypothetical.

Run: python3 mvp/test_harness_stream.py
"""
import os
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
# BEFORE importing livestreams: it resolves its publish path at call time from AEON_STATE_DIR, and
# a test must never write into the operator's real ~/.aeon.
os.environ["AEON_STATE_DIR"] = tempfile.mkdtemp(prefix="aeon_hstream_")
os.environ.setdefault("AEON_DB", os.path.join(os.environ["AEON_STATE_DIR"], "t.db"))

from pod import harness_stream, livestreams            # noqa: E402
from pod.adapters import base                          # noqa: E402

PASS = 0
fails = []


def ok(cond, label, detail=""):
    global PASS
    print(("  ok   " if cond else "  FAIL ") + label + (("  " + detail) if detail else ""))
    if cond:
        PASS += 1
    else:
        fails.append(label)


def tile(cid):
    return next((s for s in livestreams.snapshot(limit=64) if s.get("case") == cid), None)


print("with the wall OFF, observers are inert and cannot raise")
o = harness_stream.observer("hermes", "av2-01")
ok(o is harness_stream.NULL, "a no-op observer is returned, not None (callers stay branch-free)")
o("a line")
o.close()
ok(True, "calling and closing a null observer is safe")

livestreams.clear()
livestreams.enable(True)

print("THE COLLISION HAZARD: the same task id runs under every harness")
# av2-01-compute-write executes on Hermes AND OpenClaw AND OpenCode. livestreams keys streams by
# case id, so unnamespaced these three would interleave into ONE tile and fight over its scroll pin.
a = harness_stream.observer("hermes", "av2-01-compute-write")
b = harness_stream.observer("openclaw", "av2-01-compute-write")
ok(a.cid != b.cid, "the same task under two harnesses gets two distinct streams",
   "%s vs %s" % (a.cid, b.cid))
a("hermes is working")
b("openclaw is working")
ta, tb = tile(a.cid), tile(b.cid)
ok(ta is not None and tb is not None, "both tiles exist on the wall")
ok(ta and "hermes is working" in ta["answer"] and "openclaw" not in ta["answer"],
   "and neither harness's output leaks into the other's tile")
ok(ta and ta["label"].startswith("hermes"), "the tile is labelled with its harness",
   str(ta and ta["label"]))

print("startup chatter is filtered, so a tile opens on the TASK")
c = harness_stream.observer("hermes", "av2-02")
c("state.db: linked SQLite 3.46.1 is vulnerable to the WAL-reset corruption bug (…)")
c("⚠ Deprecated .env settings detected:")
c("⚠️  Warning: API key appears invalid or missing")
ok((tile(c.cid) or {}).get("answer", "") == "", "known startup noise never reaches the wall")
c("\U0001f4dd User Query: BUILD A STARSHIP BRIDGE SIMULATOR")
ok("STARSHIP BRIDGE" in (tile(c.cid) or {}).get("answer", ""), "real task output does")

print("ANSI escapes are stripped — the wall renders text, not terminal control codes")
c("\x1b[2mdim text\x1b[0m and \x1b[31mred\x1b[0m")
ans = (tile(c.cid) or {}).get("answer", "")
ok("dim text and red" in ans and "\x1b[" not in ans, "escape sequences removed, content kept")

print("turn progress is parsed out of the harness's own output")
c("\U0001f504 Making API call #3/8...")
ok(c.turn == "3/8", "an 'API call #3/8' line is understood as turn 3 of 8", str(c.turn))

print("THE FALSE-STALL HAZARD: a healthy agent turn is a long SILENCE")
# swTile styles a tile amber past 30s idle and red 'stalled' past 120s. A real Hermes turn is a
# single API call taking 31s+ with no output, so without a heartbeat every healthy tile reads
# stalled. The heartbeat must be honest content, not a filler character.
d = harness_stream.observer("hermes", "av2-03")
d("\U0001f504 Making API call #2/8...")
before = (tile(d.cid) or {}).get("reasoning", "")
d.last = time.time() - (harness_stream.HEARTBEAT_AFTER_S + 1)   # simulate a quiet turn
d._maybe_heartbeat(time.time())
after = (tile(d.cid) or {}).get("reasoning", "")
ok(len(after) > len(before), "a quiet stream gets a heartbeat")
ok("working" in after and "turn 2/8" in after,
   "and the heartbeat says what it is waiting on, with elapsed time", after.strip()[-40:])
d.last = time.time()
d._maybe_heartbeat(time.time())
ok((tile(d.cid) or {}).get("reasoning", "") == after, "a BUSY stream is not heartbeated")

print("closing ends the tile (including on the timeout path)")
d.close()
ok((tile(d.cid) or {}).get("done") is True, "the stream is marked done")
ok(d.cid not in harness_stream.active(), "and it is deregistered from the ticker")

print("run_argv: streaming is opt-in and does not disturb the blocking path")
out, err, rc, dur = base.run_argv(
    [sys.executable, "-c", "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)"], 30)
ok("to-stdout" in out and "to-stderr" in err and rc == 0,
   "without on_line, stdout and stderr are captured separately as before")

seen = []
out, err, rc, dur = base.run_argv(
    [sys.executable, "-c", "import sys; print('line-a'); print('err-b', file=sys.stderr)"],
    30, on_line=seen.append)
ok(any("line-a" in s for s in seen), "with on_line, stdout lines arrive live")
ok(any("err-b" in s for s in seen), "so do stderr lines")
ok("line-a" in out and "err-b" in err,
   "AND the return value still separates the two — callers diagnose failures from stderr")
ok(rc == 0, "return code survives the streaming path")

print("the god-task budget still fires, with the SAME message run_harness2 reports")
t0 = time.time()
try:
    base.run_argv([sys.executable, "-c", "import time; time.sleep(30)"], 1.0, on_line=seen.append)
    ok(False, "a timeout must raise")
except base.AdapterError as e:
    took = time.time() - t0
    ok("timed out after 1.0s" in str(e), "AdapterError carries the timeout, as harness_error text",
       str(e)[:60])
    ok(took < 15, "and it actually kills the child rather than waiting it out", "%.1fs" % took)

print("a callback that throws cannot damage the run it is observing")
out, err, rc, _ = base.run_argv(
    [sys.executable, "-c", "print('still fine')"], 30,
    on_line=lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
ok(rc == 0 and "still fine" in out, "the subprocess completes and its output is intact")

livestreams.enable(False)
print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all harness-stream tests passed (%d checks)" % PASS)
