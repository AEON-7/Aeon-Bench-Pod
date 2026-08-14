"""The performance sweep, visible while it runs.

The perf grid is the LAST phase of a bench and one of the longest — a c1/c4/c8 ladder across five
prompt categories, then the same again per harness. It published nothing to the live view: a
`progress_cb(conc, done, total)` existed and only ever moved a stage counter. So for the whole
sweep an operator saw a number tick while the interesting part — how fast is this serve, right
now, at this concurrency — stayed invisible until the run ended and the numbers appeared at once.

That is exactly the state the agentic phase was in before its containers started streaming, and
the fix is the same one, reusing the same wall: one tile per CELL (a category at one concurrency,
which is the unit the grid actually measures), fed as each request lands.

A cell is the right grain. `Math @ c4` means four concurrent Math streams and nothing else in
flight — the grid never mixes categories within a level, because that would dilute a category's
aggregate tok/s with another's wall time. So a tile maps one-to-one onto a number that will later
appear on the performance board, and watching it is watching that number being made.

Never raises: this is telemetry attached to a measurement phase, and a broken tile must not cost
an operator the perf numbers they waited hours for.
"""
from __future__ import annotations

try:
    from pod import livestreams
except Exception:                                    # pragma: no cover - import shape varies
    try:
        import livestreams                           # type: ignore
    except Exception:
        livestreams = None                           # type: ignore


def _fmt(v, unit="", nd=1):
    if v is None:
        return "—"
    try:
        return ("%.*f%s" % (nd, float(v), unit))
    except Exception:
        return str(v)


class Cell:
    """A live tile for one (category, concurrency) cell of the grid."""

    def __init__(self, kind, category, conc, total):
        self.cid = "perf:%s.%s.c%s" % (kind, str(category).lower(), conc)
        self.label = "%s %s @ c%s" % (kind, category, conc)
        self.total = int(total or 0)
        self.n = 0
        self._closed = False

    def tick(self, req):
        """One completed request. `req` is a row from perf_grid._one_request."""
        try:
            self.n += 1
            if livestreams is None:
                return
            r = req or {}
            livestreams.chunk(self.cid, "  %3d/%-3d  ttft %-9s decode %-12s %s tok\n" % (
                self.n, self.total,
                _fmt(r.get("ttft_ms"), "ms", 0),
                _fmt(r.get("decode_tps"), " tok/s"),
                r.get("output_tokens") or 0), "answer")
        except Exception:
            pass

    def error(self, msg):
        try:
            self.n += 1
            if livestreams is not None:
                livestreams.chunk(self.cid, "  %3d/%-3d  ERROR %s\n"
                                  % (self.n, self.total, str(msg)[:90]), "reasoning")
        except Exception:
            pass

    def close(self, cell=None):
        """Publish the cell's aggregate — the figure that reaches the performance board."""
        if self._closed:
            return
        self._closed = True
        try:
            if livestreams is None:
                return
            c = cell or {}
            if c:
                livestreams.chunk(self.cid, "  ── cell: %s agg · ttft %s · %s tok/s/stream\n" % (
                    _fmt(c.get("agg_tps"), " tok/s"),
                    _fmt(c.get("ttft_ms_mean"), "ms", 0),
                    _fmt(c.get("decode_tps_mean"))), "reasoning")
            livestreams.end(self.cid, status="perf", score=None)
        except Exception:
            pass


class _Null:
    cid = None

    def tick(self, req):
        return None

    def error(self, msg):
        return None

    def close(self, cell=None):
        return None


NULL = _Null()


def cell(kind, category, conc, total):
    """A live tile for this cell, or an inert stand-in when the wall is off.

    Returns something callable either way so the measurement loop stays free of `if streaming:` —
    the perf grid is timing-sensitive code and must not grow branches for telemetry."""
    try:
        if livestreams is None or not livestreams.enabled():
            return NULL
        c = Cell(kind, category, conc, total)
        livestreams.begin(c.cid, label=c.label)
        return c
    except Exception:
        return NULL
