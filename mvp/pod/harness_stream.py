"""Live harness activity on the terminal wall.

The agentic dimension is 40% of GOD SCORE and runs for HOURS, and until now it published NOTHING
to the live view. `targets.py` feeds the wall from OUR streaming client; harness traffic goes
container -> engine directly and never passes through it, so an operator watching a god run saw an
empty wall and a single `harness:hermes 4/25` counter for the longest phase of the bench.

The containers were never silent. `docker start -a` simply buffered their output and handed it
over once the task had already finished. This module turns that buffer into a live feed, and the
existing wall renders it with no UI change — livestreams already crosses the process boundary via
`~/.aeon/livestreams.json`, and the flusher is already running by the time the harnesses start.

THREE THINGS THIS HAS TO GET RIGHT, each learned from the code it plugs into:

1. NAMESPACED IDS. livestreams keys streams by case id, and the same task id runs under every
   harness (`av2-01-compute-write` executes on Hermes AND OpenClaw AND OpenCode). Unnamespaced,
   two harnesses would interleave into one tile and fight over its scroll pin.

2. HEARTBEATS. The wall's tile styling is tuned for token streams: amber past 30s idle, red
   "stalled" past 120s. A perfectly healthy harness turn is one API call taking 31s+ with no
   output at all, so without a heartbeat every healthy agentic tile would read as stalled.

3. NEVER DAMAGE THE RUN IT WATCHES. Every entry point swallows its own exceptions. Telemetry that
   can break a multi-hour benchmark is worse than no telemetry.
"""
from __future__ import annotations

import re
import threading
import time

try:
    from pod import livestreams
except Exception:                                    # pragma: no cover - import shape varies
    try:
        import livestreams                           # type: ignore
    except Exception:
        livestreams = None                           # type: ignore

HEARTBEAT_AFTER_S = 12.0     # comfortably inside the wall's 30s amber threshold
HEARTBEAT_EVERY_S = 5.0      # ticker cadence; cheap, and only fires for genuinely quiet streams
MAX_LINE_CHARS = 400

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# Startup chatter every Hermes container prints before it does any work. Dropping it means the
# tile opens on the task rather than on a SQLite advisory and a deprecated-config warning.
_NOISE = (
    "is vulnerable to the WAL-reset corruption bug",
    "Deprecated .env settings detected",
    "this is deprecated.",
    "Move to config.yaml instead",
    "Then remove the old entries",
    "Warning: API key appears invalid or missing",
    "Some tools may not work due to missing requirements",
)

_LOCK = threading.RLock()
_ACTIVE: dict = {}
_TICKER = None


def _clean(line):
    s = _ANSI.sub("", line or "").rstrip()
    if len(s) > MAX_LINE_CHARS:
        s = s[:MAX_LINE_CHARS] + " …"
    return s


def _interesting(s):
    if not s.strip():
        return False
    return not any(n in s for n in _NOISE)


def _ticker_loop():
    """One shared thread for every observer, rather than one thread each.

    A god run has up to 4 task containers in flight per harness; a thread apiece would be fine but
    a single 5s sweep is cheaper and, more importantly, cannot leak a thread when a task dies in a
    way that skips close()."""
    while True:
        time.sleep(HEARTBEAT_EVERY_S)
        now = time.time()
        try:
            with _LOCK:
                obs = list(_ACTIVE.values())
            for o in obs:
                o._maybe_heartbeat(now)
        except Exception:
            pass


def _ensure_ticker():
    global _TICKER
    if _TICKER is None:
        _TICKER = threading.Thread(target=_ticker_loop, name="harness-stream", daemon=True)
        _TICKER.start()


class Observer:
    """An `on_line` callable for one (harness, task) container.

    Passed straight to `run_container_io(on_line=...)`; call `close()` when the task ends."""

    def __init__(self, harness, task_id):
        self.harness = harness or "harness"
        self.task_id = task_id or "task"
        self.cid = "%s:%s" % (self.harness, self.task_id)
        self.t0 = time.time()
        self.last = time.time()
        self.lines = 0
        self.turn = None
        self._closed = False

    # -- producer side ---------------------------------------------------------------------
    def __call__(self, line):
        try:
            s = _clean(line)
            if not _interesting(s):
                return
            with _LOCK:
                self.last = time.time()
                self.lines += 1
            m = re.search(r"API call #(\d+)\s*/\s*(\d+)", s)
            if m:
                self.turn = "%s/%s" % (m.group(1), m.group(2))
            if livestreams is not None:
                livestreams.chunk(self.cid, s + "\n", "answer")
        except Exception:
            pass

    def _maybe_heartbeat(self, now):
        try:
            if self._closed:
                return
            with _LOCK:
                quiet = now - self.last
                if quiet < HEARTBEAT_AFTER_S:
                    return
                self.last = now
            # Honest content, not a filler character: while a harness is quiet it is waiting on
            # the model, and the elapsed number is the thing a watcher actually wants.
            tag = (" turn %s" % self.turn) if self.turn else ""
            if livestreams is not None:
                livestreams.chunk(self.cid, "   · working%s — %ds\n" % (tag, int(now - self.t0)),
                                  "reasoning")
        except Exception:
            pass

    def close(self, status=None, score=None):
        if self._closed:
            return
        self._closed = True
        try:
            with _LOCK:
                _ACTIVE.pop(self.cid, None)
            if livestreams is not None:
                livestreams.end(self.cid, status=status, score=score)
        except Exception:
            pass


class _Null:
    """Stand-in when streaming is off, so callers need no conditional."""

    cid = None

    def __call__(self, line):
        return None

    def close(self, status=None, score=None):
        return None


NULL = _Null()


def observer(harness, task_id):
    """An Observer for this container, or a no-op when the wall is not enabled.

    Returning a callable either way keeps the adapters free of `if streaming:` branches — the
    harness path must stay readable, since it is the part that must never break."""
    try:
        if livestreams is None or not livestreams.enabled():
            return NULL
        o = Observer(harness, task_id)
        with _LOCK:
            _ACTIVE[o.cid] = o
        _ensure_ticker()
        livestreams.begin(o.cid, label="%s · %s" % (o.harness, o.task_id))
        return o
    except Exception:
        return NULL


def active():
    """Live observers, for tests and diagnostics."""
    with _LOCK:
        return dict(_ACTIVE)
