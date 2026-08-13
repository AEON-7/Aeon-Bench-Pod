"""What each concurrent stream is saying, RIGHT NOW — reasoning included.

The Live view could show a bench's stages and its finished cases, but nothing in between. On a
god-tier suite a single case can think for twenty minutes before emitting one line of answer, so
"in between" is where nearly all the time goes — and it looked identical to a hung process.

This holds a small rolling tail per IN-FLIGHT case: the reasoning as it arrives, the answer as it
arrives, and enough counters to show it moving. Sixteen concurrent cases become sixteen live
terminals instead of one silent progress bar.

The buffer lives in the BENCH process; the dashboard is a different process entirely. So, exactly
like livelog, the state crosses that boundary through a file — written by a background thread on a
timer, never by the token path, because chunk() is called once per streamed delta and must stay
free of I/O.

DESIGN CONSTRAINTS, in order of importance:
  * It must never slow the bench. Appends are O(1) under a lock held for microseconds, and the
    per-stream tail is capped, so a case that emits 60k tokens costs the same as one emitting 60.
  * It must never break the bench. Every entry point swallows its own errors: telemetry that can
    fail a benchmark is worse than no telemetry.
  * It is OPT-IN (`enable()`), so the shared targets.py stays inert on the mothership, which has
    no business buffering model output.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

TAIL_CHARS = 4000          # per stream, per channel — a readable terminal, not a transcript
MAX_STREAMS = 64           # far above any real concurrency; a guard, not a policy
DONE_LINGER_S = 20.0       # keep a finished stream visible briefly so it does not just vanish
FLUSH_EVERY_S = 1.0        # publish cadence; the wall is for watching, not for frame-perfect replay
STALE_S = 90.0             # a snapshot older than this is a dead process, not a quiet one

_LOCK = threading.Lock()
_STREAMS = {}              # case_id -> dict
_ON = False
_FLUSHER = None


def path():
    d = os.environ.get("AEON_STATE_DIR") or os.path.expanduser("~/.aeon")
    return os.path.join(d, "livestreams.json")


def enable(on=True):
    """Turn buffering on and start publishing. Off by default so importing targets.py costs
    nothing on the mothership, which shares this code but has no bench to watch."""
    global _ON, _FLUSHER
    _ON = bool(on)
    if not _ON:
        return
    try:
        if _FLUSHER is None:
            _FLUSHER = threading.Thread(target=_flush_loop, name="livestreams", daemon=True)
            _FLUSHER.start()
    except Exception:
        pass


def _flush_loop():
    """Publish the wall on a timer. Deliberately NOT called from chunk(): a write per delta would
    put the filesystem in the middle of the decode loop, which is the one place it must never be."""
    while True:
        try:
            time.sleep(FLUSH_EVERY_S)
            if _ON:
                _write(snapshot(limit=MAX_STREAMS))
        except Exception:
            pass


def _write(rows):
    try:
        p = path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        blob = json.dumps({"t": round(time.time(), 3), "streams": rows})
        # Atomic replace, so a reader can never catch a half-written wall.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".ls-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:
        pass


def read():
    """Reader side (the dashboard process): the published wall plus its age.

    Age is the honest part. A wall with no age is indistinguishable from a wall left behind by a
    bench that exited hours ago, and a stale terminal presented as live is worse than none."""
    try:
        p = path()
        with open(p, encoding="utf-8") as f:
            blob = json.load(f)
        age = time.time() - float(blob.get("t") or 0)
        return {"streams": blob.get("streams") or [], "age_s": round(age, 1),
                "live": age < STALE_S}
    except Exception:
        return {"streams": [], "age_s": None, "live": False}


def enabled():
    return _ON


def begin(case_id, label=None):
    if not _ON or not case_id:
        return
    try:
        with _LOCK:
            _STREAMS[case_id] = {
                "case": case_id, "label": label or case_id, "t0": time.time(),
                "reasoning": "", "answer": "", "n_reason": 0, "n_answer": 0,
                "done": False, "t_end": None, "last": time.time(),
            }
            if len(_STREAMS) > MAX_STREAMS:
                # drop the oldest FINISHED stream first; never evict a live one
                dead = sorted((s for s in _STREAMS.values() if s["done"]),
                              key=lambda s: s.get("t_end") or 0)
                for s in dead[: len(_STREAMS) - MAX_STREAMS]:
                    _STREAMS.pop(s["case"], None)
    except Exception:
        pass


def chunk(case_id, text, kind="answer"):
    """Append a streamed delta. `kind` is 'answer' or 'reasoning'."""
    if not _ON or not case_id or not text:
        return
    try:
        with _LOCK:
            st = _STREAMS.get(case_id)
            if st is None:
                return
            key = "reasoning" if kind == "reasoning" else "answer"
            buf = st[key] + text
            # keep the TAIL: the newest output is what a watcher is reading, and an unbounded
            # buffer would turn a 60k-token case into 60k of live payload on every poll
            st[key] = buf[-TAIL_CHARS:] if len(buf) > TAIL_CHARS else buf
            st["n_" + ("reason" if key == "reasoning" else "answer")] += len(text)
            st["last"] = time.time()
    except Exception:
        pass


def end(case_id, status=None, score=None):
    if not _ON or not case_id:
        return
    try:
        with _LOCK:
            st = _STREAMS.get(case_id)
            if st is None:
                return
            st["done"] = True
            st["t_end"] = time.time()
            st["status"] = status
            st["score"] = score
    except Exception:
        pass


def snapshot(limit=24):
    """Live streams first, then recently-finished ones while they linger.

    Finished streams stay briefly on purpose: a terminal that blanks the instant a case ends reads
    as a crash, and the last thing a case said is often the most interesting thing on screen."""
    try:
        now = time.time()
        with _LOCK:
            vals = list(_STREAMS.values())
        keep = [s for s in vals
                if not s["done"] or (now - (s.get("t_end") or now)) <= DONE_LINGER_S]
        with _LOCK:
            for s in vals:
                if s not in keep:
                    _STREAMS.pop(s["case"], None)
        keep.sort(key=lambda s: (s["done"], -(s.get("t0") or 0)))
        out = []
        for s in keep[:limit]:
            out.append({
                "case": s["case"], "label": s["label"],
                "elapsed_s": round(now - s["t0"], 1),
                "reasoning": s["reasoning"], "answer": s["answer"],
                "n_reason": s["n_reason"], "n_answer": s["n_answer"],
                "done": s["done"], "status": s.get("status"), "score": s.get("score"),
                # Seconds since this stream last produced ANYTHING — the number that separates
                # "thinking hard" from "wedged", which is the whole question a watcher has.
                "idle_s": round(now - s["last"], 1),
            })
        return out
    except Exception:
        return []


def clear():
    """Drop everything, including the published wall — a new bench must not inherit the last
    bench's terminals, which would read as sixteen cases that finished before the run began."""
    try:
        with _LOCK:
            _STREAMS.clear()
        _write([])
    except Exception:
        pass
