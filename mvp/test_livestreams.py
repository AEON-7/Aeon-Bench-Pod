"""The terminal wall: does it show what the model is saying, without getting in its way?

The four things that would make it useless, each pinned here:
  * it publishes to a FILE, because the bench and the dashboard are different processes — an
    in-memory buffer is invisible to the thing that renders it (true of the first draft);
  * it keeps the TAIL, because a 60k-token case must not become a 60k-char payload per poll;
  * it reports AGE, because a wall left behind by a dead bench must not render as live;
  * it publishes from inside the REAL streaming loop, keyed on the _case_id the prompt already
    carries — the wiring, not just the buffer.

Run: python3 mvp/test_livestreams.py
"""
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

STATE = tempfile.mkdtemp(prefix="aeon_livestreams_")
os.environ["AEON_STATE_DIR"] = STATE
os.environ.setdefault("AEON_DB", os.path.join(STATE, "test.db"))

from pod import livestreams as ls   # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    if not cond:
        print(f"FAIL  {label}")
        shutil.rmtree(STATE, ignore_errors=True)
        sys.exit(1)
    PASS += 1
    print(f"ok    {label}")


def fresh():
    ls.clear()
    ls.enable(True)


# ---- OFF by default: targets.py is shared with the mothership, which has no bench to watch ----
ls.clear()
ls.enable(False)
ls.begin("c1")
ls.chunk("c1", "hello")
ok(ls.snapshot() == [], "disabled: buffering is a no-op (mothership pays nothing)")

# ---- reasoning and answer are SEPARATE channels ----------------------------------------------
# A god-tier case thinks for a long time before it answers. Merging the two hides exactly the
# transition a watcher is waiting for.
fresh()
CID = "v4.coding.god_mode.01"
ls.begin(CID)
ls.chunk(CID, "let me consider", "reasoning")
ls.chunk(CID, "def f():", "answer")
(s,) = ls.snapshot()
ok(s["reasoning"] == "let me consider" and s["answer"] == "def f():",
   "reasoning and answer are buffered as distinct channels")
ok(s["n_reason"] == 15 and s["n_answer"] == 8 and s["done"] is False,
   "per-channel counters track an in-flight case")

# ---- the TAIL, not the transcript ------------------------------------------------------------
fresh()
ls.begin("big")
ls.chunk("big", "x" * 50_000, "answer")
ls.chunk("big", "THE-END", "answer")
(s,) = ls.snapshot()
ok(len(s["answer"]) == ls.TAIL_CHARS and s["answer"].endswith("THE-END"),
   "a 50k-char case keeps only the newest TAIL_CHARS — newest output survives")
ok(s["n_answer"] == 50_007, "the COUNT still reflects everything emitted, not just the tail")

# ---- a finished terminal keeps its verdict and lingers ----------------------------------------
fresh()
ls.begin("c1")
ls.chunk("c1", "42", "answer")
ls.end("c1", status="scored", score=1.0)
(s,) = ls.snapshot()
ok(s["done"] is True and s["status"] == "scored" and s["score"] == 1.0 and s["answer"] == "42",
   "end() records the verdict; the tile does not blank the instant its case ends")

# ---- the no-answer terminal is the one most worth reading ------------------------------------
fresh()
ls.begin("c1")
ls.chunk("c1", "thinking..." * 10, "reasoning")
ls.end("c1", status="no_answer", score=0.0)
(s,) = ls.snapshot()
ok(s["status"] == "no_answer" and s["n_answer"] == 0 and s["n_reason"] > 0,
   "a case that thought hard and answered nothing is kept, with its reasoning")

# ---- live streams sort ahead of finished ones -------------------------------------------------
fresh()
for cid in ("a", "b", "c"):
    ls.begin(cid)
ls.end("b", status="scored", score=1.0)
order = [s["case"] for s in ls.snapshot()]
ok(order[-1] == "b" and set(order[:2]) == {"a", "c"},
   "finished streams sink; a running one is never pushed off the visible rows")

# ---- THE load-bearing one: it crosses the process boundary ------------------------------------
fresh()
ls.begin("c1")
ls.chunk("c1", "streaming now", "answer")
ls._write(ls.snapshot())                       # what the flusher thread does on its timer
ok(os.path.exists(ls.path()), "the wall is published to a file the dashboard process can read")
blob = json.loads(open(ls.path(), encoding="utf-8").read())
ok(blob["streams"][0]["answer"] == "streaming now", "the published file carries the streamed text")
out = ls.read()
ok(out["live"] is True and out["age_s"] is not None and out["streams"][0]["case"] == "c1",
   "read() returns the wall with an age, from the other side of the boundary")

# ---- a stale wall is NOT live -----------------------------------------------------------------
# A bench that exited hours ago leaves its last frame behind. Rendering that as live is worse than
# rendering nothing: it is a lie in the direction of "everything is fine".
blob["t"] = blob["t"] - (ls.STALE_S + 60)
open(ls.path(), "w", encoding="utf-8").write(json.dumps(blob))
out = ls.read()
ok(out["live"] is False and out["age_s"] > ls.STALE_S,
   "a wall older than STALE_S reports live=False with its true age")

# ---- absence reads as empty, never as an error ------------------------------------------------
_real = ls.path
ls.path = lambda: os.path.join(STATE, "nope", "livestreams.json")
ok(ls.read() == {"streams": [], "age_s": None, "live": False},
   "a missing wall reads as empty, not an exception")
ls.path = _real

# ---- clear() drops the PREVIOUS bench's wall --------------------------------------------------
fresh()
ls.begin("old")
ls._write(ls.snapshot())
ls.clear()
ok(ls.read()["streams"] == [], "clear() wipes the published wall, so a new bench starts blank")

# ---- deltas outside a stream's window are ignored ----------------------------------------------
# Streams open in targets.py and close in runner.py. A delta arriving outside that window must not
# resurrect a terminal or invent one.
fresh()
ls.chunk("never-begun", "orphan text", "answer")
ok(ls.snapshot() == [], "a chunk for an unknown case is a no-op")

# ---- telemetry must never be able to fail the benchmark ---------------------------------------
fresh()
for bad in (None, "", 0):
    ls.begin(bad)
    ls.chunk(bad, "x")
    ls.end(bad)
ls.chunk("c1", None)
ok(ls.snapshot() == [], "junk input is swallowed — telemetry cannot take down the bench")

# ---- END-TO-END through the REAL streaming loop in targets.py ---------------------------------
# A fake SSE body is fed to the OpenAI target so the delta loop runs verbatim: if the publish call
# is moved out of that loop, or the _case_id tag stops reaching it, this fails.
fresh()
from aeon import targets   # noqa: E402

SSE = [
    b'data: {"choices":[{"delta":{"reasoning_content":"hmm, let me think"}}]}\n',
    b'data: {"choices":[{"delta":{"content":"the answer is 42"}}]}\n',
    b'data: [DONE]\n',
]


class _Resp:
    status = 200
    headers = {}

    def __iter__(self):
        return iter(SSE)

    def read(self):
        return b"".join(SSE)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_orig_urlopen = targets.urllib.request.urlopen
targets.urllib.request.urlopen = lambda *a, **k: _Resp()
try:
    t = targets.OpenAITarget("http://x/v1", "m")
    t.chat([{"role": "user", "content": "q", "_case_id": "v4.coding.god_mode.01"}],
           temperature=0.0, max_tokens=64)
finally:
    targets.urllib.request.urlopen = _orig_urlopen

rows = ls.snapshot()
ok(len(rows) == 1 and rows[0]["case"] == "v4.coding.god_mode.01",
   "the real streaming loop opens a stream keyed on the prompt's _case_id")
ok("let me think" in rows[0]["reasoning"] and "42" in rows[0]["answer"],
   "reasoning and answer deltas reach the wall from inside the decode loop")

shutil.rmtree(STATE, ignore_errors=True)
print(f"\nOK  terminal wall (live per-case model output): {PASS} checks passed")
