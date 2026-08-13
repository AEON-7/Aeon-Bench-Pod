"""A live terminal feed for the dashboard — whatever the bench is doing, whoever launched it.

WHY THIS EXISTS. The Live view had two sources and both go dark during a normal run:

  * `/api/live` lists DB runs with status='running'. The text suite creates one; ARENA, the
    agentic harnesses and the perf grid do not — so the view empties out for the longest stretches
    of a bench, exactly when an operator most wants reassurance.
  * `/api/pod/jobs` carries a stage strip, but only for runs the pod's own job manager spawned.
    A bench started by hand (`docker exec ... python -m pod.aeon_pod`) has no job row, so that
    source is empty for the entire run.

Observed live: a GOD MODE run three hours in, mid-agentic, showing "No benchmark is running right
now." A blank screen during a multi-hour job is how a healthy run gets killed.

The one thing that always exists is the bench process's own stdout. This tees it — every stage
marker, every scored case, every banner — into a bounded ring file the pod serves back. No print
site has to opt in, so a dimension can never be silently missing from the feed again.

Bounded on purpose: a long run prints a lot and this must never fill a disk or a browser.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

# Ring buffer sized for "what is happening now", not for archival — the full log is the process's
# real stdout, which the launcher owns.
MAX_LINES = int(os.environ.get("AEON_LIVELOG_LINES", "400"))
MAX_LINE_CHARS = 2000

_LOCK = threading.Lock()


def _path():
    d = os.environ.get("AEON_STATE_DIR") or os.path.expanduser("~/.aeon")
    return os.path.join(d, "livelog.jsonl")


def emit(text, kind="out"):
    """Append one line to the live feed. NEVER raises — a telemetry failure must not be able to
    take down the benchmark it is reporting on."""
    try:
        text = (text or "").rstrip("\n")
        if not text.strip():
            return
        rec = {"t": round(time.time(), 3), "k": kind, "s": text[:MAX_LINE_CHARS]}
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            lines = []
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()[-(MAX_LINES - 1):]
                except OSError:
                    lines = []
            lines.append(json.dumps(rec) + "\n")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp, p)
    except Exception:
        pass


def tail(since=0.0, limit=200):
    """Records newer than `since` (a unix ts). Returns (records, latest_ts, age_s).

    `age_s` is how long ago the newest line was written — the dashboard uses it to say "this feed
    is live" vs "this is the tail of something that already finished", instead of showing stale
    output as though it were current."""
    p = _path()
    out = []
    latest = 0.0
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                latest = max(latest, r.get("t") or 0.0)
                if (r.get("t") or 0.0) > since:
                    out.append(r)
    except OSError:
        return [], 0.0, None
    return out[-limit:], latest, (round(time.time() - latest, 1) if latest else None)


class _Tee:
    """Wraps a stream: writes through unchanged, and mirrors whole lines into the feed.

    Line-buffered by hand because the bench prints with flush=True mid-line in places; a partial
    write must not become a truncated feed entry."""

    def __init__(self, stream, kind="out"):
        self._s = stream
        self._kind = kind
        self._buf = ""

    def write(self, data):
        n = self._s.write(data)
        try:
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                emit(line, self._kind)
            if len(self._buf) > MAX_LINE_CHARS:      # a very long unterminated line
                emit(self._buf, self._kind)
                self._buf = ""
        except Exception:
            pass
        return n

    def flush(self):
        return self._s.flush()

    def isatty(self):
        try:
            return self._s.isatty()
        except Exception:
            return False

    def __getattr__(self, k):
        return getattr(self._s, k)


def install():
    """Tee stdout/stderr into the feed. Idempotent."""
    if not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout, "out")
    if not isinstance(sys.stderr, _Tee):
        sys.stderr = _Tee(sys.stderr, "err")


def reset(note=None):
    """Start a fresh feed for a new bench, so the view never shows the previous run's tail."""
    try:
        p = _path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            open(p, "w").close()
    except OSError:
        pass
    if note:
        emit(note, "stage")
