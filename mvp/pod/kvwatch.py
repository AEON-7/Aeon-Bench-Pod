"""Sustained-load telemetry: does this serve DEGRADE as KV pressure builds?

The concurrency ladder answers "how fast is this serve when we ask it to be fast" — a fresh engine,
a short burst, a clean cache. It cannot answer the question an operator actually lives with: after
three hours under load, is it still that fast?

The mechanism is visible in the engine's own counters. As `kv_cache_usage_perc` climbs, vLLM starts
PREEMPTING sequences and recomputing them. Throughput falls and nothing errors — no log line, no
failed request, just a serve that is quietly slower than its benchmark said. `num_preemptions_total`
is the smoking gun, and it is free to read.

Everything here is the engine's own tally sampled on a wall clock, and every rate is a delta
between two samples. Nothing is inferred, and a serve that exposes no /metrics simply produces no
section rather than a fabricated one.
"""
from __future__ import annotations

import re
import threading
import time
import urllib.request

# Counters (monotonic — rates come from deltas) and gauges (instantaneous).
_COUNTERS = {
    "gen_tok": "generation_tokens_total",
    "prompt_tok": "prompt_tokens_total",
    "preempt": "num_preemptions_total",
    "pfx_hits": "prefix_cache_hits_total",
    "pfx_queries": "prefix_cache_queries_total",
    # Speculative decoding, when the serve runs a drafter. Acceptance is the MECHANISM behind
    # most long-run throughput decay: a drafter conditions on a fraction of what the target sees,
    # so the deeper the sequence the less it lands. Without these two counters the timeline
    # records the symptom (gen_tok_rate falling) with no way to explain it.
    "spec_accept": "spec_decode_num_accepted_tokens_total",
    "spec_draft": "spec_decode_num_draft_tokens_total",
}
_GAUGES = {
    "kv_pct": "kv_cache_usage_perc",
    "running": "num_requests_running",
    "waiting": "num_requests_waiting",
}

MAX_POINTS = 600            # a day-long run stays renderable; older points are thinned, never cut
DEFAULT_EVERY_S = 15.0


def _metrics_url(target_url):
    """…/v1 -> …/metrics, so it follows whatever the run is actually benchmarking."""
    base = (target_url or "").rstrip("/")
    for suffix in ("/v1", "/v1/"):
        if base.endswith(suffix.rstrip("/")):
            base = base[: -len(suffix.rstrip("/"))]
            break
    return base.rstrip("/") + "/metrics"


def _parse_metrics(text):
    """Prometheus text -> {our key: float}.

    The trailing `$` and the anchored name are load-bearing. vLLM publishes
    `spec_decode_num_accepted_tokens_per_pos_total` right beside
    `spec_decode_num_accepted_tokens_total`; an unanchored search would let the per-position
    histogram shadow the counter and silently report a nonsense acceptance rate."""
    out = {}
    for key, name in {**_COUNTERS, **_GAUGES}.items():
        m = re.search(r"^vllm:%s(?:\{[^}]*\})? ([0-9.eE+-]+)$" % re.escape(name), text, re.M)
        if m:
            try:
                out[key] = float(m.group(1))
            except ValueError:
                pass
    return out


def _scrape(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        text = r.read().decode("utf-8", "replace")
    return _parse_metrics(text)


MIN_REGIME_SAMPLES = 6      # fewer than this cannot be split into thirds and mean anything


def _mean(group, key):
    vals = [p[key] for p in group if p.get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _regime(group):
    """Low-KV vs high-KV throughput WITHIN a single concurrency regime.

    Slices the sorted samples directly rather than comparing against boundary VALUES: with
    `<= kv[n//3]` the low group swallows the first sample of the middle third, which on a
    9-sample run pulled a 400 tok/s baseline down to 375 and understated the degradation."""
    ranked = sorted(group, key=lambda p: p["kv_pct"])
    third = max(1, len(ranked) // 3)
    lo_pts, hi_pts = ranked[:third], ranked[-third:]
    lo_tps = sum(p["gen_tok_rate"] for p in lo_pts) / len(lo_pts)
    hi_tps = sum(p["gen_tok_rate"] for p in hi_pts) / len(hi_pts)
    return {
        "running": int(round(group[0].get("running") or 0)),
        "n": len(group),
        "kv_lo_cut": round(lo_pts[-1]["kv_pct"], 4),
        "kv_hi_cut": round(hi_pts[0]["kv_pct"], 4),
        "tps_at_low_kv": round(lo_tps, 1),
        "tps_at_high_kv": round(hi_tps, 1),
        # NEGATIVE means it genuinely got faster as its OWN KV deepened, at unchanged concurrency.
        # That is a real result and is not clamped to zero.
        "degradation_pct": round(100.0 * (lo_tps - hi_tps) / lo_tps, 1) if lo_tps else None,
        # Speculative-decode acceptance across those same two buckets, when the serve runs a
        # drafter. This is usually WHY the throughput moved: a drafter sees a fraction of the
        # context the target does, so the deeper the sequence the less of its draft survives.
        "accept_at_low_kv": _mean(lo_pts, "spec_accept_pct"),
        "accept_at_high_kv": _mean(hi_pts, "spec_accept_pct"),
    }


class Watcher:
    """Samples the engine for the LIFETIME OF A BENCH, in the background.

    Never raises and never blocks the run: a telemetry failure must not be able to damage the
    benchmark it is reporting on, so a dead endpoint just means an empty series."""

    def __init__(self, target_url, every_s=DEFAULT_EVERY_S):
        self.url = _metrics_url(target_url)
        self.every = max(2.0, float(every_s))
        self.points = []
        self._stop = threading.Event()
        self._t = None
        self._prev = None
        self._prev_t = None
        self.available = None       # None until the first scrape decides

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self):
        if self._t:
            return self
        self._t = threading.Thread(target=self._loop, name="kvwatch", daemon=True)
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=self.every + 2)
        return self.summary()

    def _loop(self):
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(self.every)
        self._sample()          # a final reading, so the tail of the run is represented

    def _sample(self):
        try:
            now = time.time()
            cur = _scrape(self.url)
            if not cur:
                self.available = False
                return
            self.available = True
            p = {"t": round(now, 2)}
            for k in _GAUGES:
                if k in cur:
                    p[k] = round(cur[k], 4)
            if self._prev and self._prev_t:
                dt = now - self._prev_t
                if dt > 0:
                    for k in _COUNTERS:
                        if k in cur and k in self._prev:
                            d = cur[k] - self._prev[k]
                            if d >= 0:
                                p[k + "_rate"] = round(d / dt, 2)
                                if k == "preempt":
                                    p["preempt_d"] = round(d, 2)
                    # Acceptance as a share of what was DRAFTED, over this same delta window.
                    # A ratio of lifetime totals would average the whole run into one number and
                    # bury the decay this series exists to expose.
                    drafted = p.get("spec_draft_rate")
                    if drafted:
                        p["spec_accept_pct"] = round(
                            100.0 * (p.get("spec_accept_rate") or 0.0) / drafted, 1)
            self._prev, self._prev_t = cur, now
            self.points.append(p)
            # Heartbeat every ~20 samples (5 min at the default cadence): proof of life in the
            # live feed, and an early warning if KV is already saturated.
            if len(self.points) % 20 == 0:
                try:
                    print("[pod] sustained-load: %d samples · KV %.0f%% · %d running · "
                          "%d preemptions so far"
                          % (len(self.points), 100 * (p.get("kv_pct") or 0),
                             int(p.get("running") or 0),
                             int(sum(x.get("preempt_d") or 0 for x in self.points))), flush=True)
                except Exception:
                    pass
            if len(self.points) > MAX_POINTS * 2:
                # THIN, never truncate: keeping every other point preserves the SHAPE of the whole
                # run. Dropping the head would hide exactly the early, low-pressure baseline the
                # later samples have to be compared against.
                self.points = self.points[::2]
        except Exception:
            pass

    # ---- the question this module exists to answer ----------------------------------------
    def summary(self):
        """Throughput at LOW vs HIGH KV pressure, MEASURED AT FIXED CONCURRENCY.

        Returns None when there is nothing honest to say — no /metrics, or too few samples to
        compare two regimes. A section that cannot be computed must be absent, not zero.

        Holding concurrency fixed is the whole correctness story. KV% and concurrency climb
        together, so a bench that walks a c1/c4/c8 ladder files its single-stream samples under
        "low KV" and its 8-way samples under "high KV" — comparing those two measures BATCHING
        and prints it as degradation. On a real Gemma-4-26B god run that read 191.2 -> 445.8 tok/s
        for -133.1% "degradation", which is a property of the ladder, not of the serve. Comparing
        only within one `running` value leaves KV depth as the one thing that moved."""
        pts = [p for p in self.points if p.get("gen_tok_rate") is not None
               and p.get("kv_pct") is not None]
        if len(pts) < 6:
            return None
        # Only samples where the engine was actually working: idle gaps between dimensions would
        # otherwise average a run's throughput toward zero and invent a degradation that is really
        # just the bench thinking.
        busy = [p for p in pts if (p.get("running") or 0) > 0 and p["gen_tok_rate"] > 0]
        if len(busy) < 6:
            return None
        groups = {}
        for p in busy:
            groups.setdefault(int(round(p.get("running") or 0)), []).append(p)
        regimes = [_regime(g) for _, g in sorted(groups.items())
                   if len(g) >= MIN_REGIME_SAMPLES]
        regimes = [r for r in regimes if r["degradation_pct"] is not None]
        # The headline quotes the regime with the most evidence behind it, so its two throughput
        # figures and its percentage always reconcile with each other. Every other regime stays
        # visible in by_concurrency rather than being averaged into a number nobody can check.
        primary = max(regimes, key=lambda r: r["n"]) if regimes else None
        kv = [p["kv_pct"] for p in busy]
        preempt = sum(p.get("preempt_d") or 0 for p in busy)
        pq = [p for p in busy if p.get("pfx_queries_rate")]
        hit_rate = None
        if pq:
            q = sum(p["pfx_queries_rate"] for p in pq)
            h = sum(p.get("pfx_hits_rate") or 0 for p in pq)
            hit_rate = round(100.0 * h / q, 1) if q else None
        out = {
            "n_samples": len(self.points), "n_busy": len(busy),
            "window_s": round(busy[-1]["t"] - busy[0]["t"], 1),
            "kv_pct_min": round(min(kv), 4), "kv_pct_max": round(max(kv), 4),
            "preemptions": round(preempt, 0),
            "prefix_cache_hit_pct": hit_rate,
            "peak_running": max((p.get("running") or 0) for p in busy),
            "peak_waiting": max((p.get("waiting") or 0) for p in busy),
            "by_concurrency": regimes,
        }
        if primary:
            out.update({
                "concurrency": primary["running"],
                "kv_lo_cut": primary["kv_lo_cut"], "kv_hi_cut": primary["kv_hi_cut"],
                "tps_at_low_kv": primary["tps_at_low_kv"],
                "tps_at_high_kv": primary["tps_at_high_kv"],
                "degradation_pct": primary["degradation_pct"],
                "accept_at_low_kv": primary["accept_at_low_kv"],
                "accept_at_high_kv": primary["accept_at_high_kv"],
            })
        else:
            # Enough busy samples to describe the run, but no single concurrency held still long
            # enough to attribute a throughput change to KV depth. Say so, rather than publish a
            # percentage we cannot defend.
            out["degradation_pct"] = None
            out["degradation_note"] = ("no concurrency regime held for %d+ busy samples"
                                       % MIN_REGIME_SAMPLES)
        return out

    def series(self, limit=MAX_POINTS):
        """The points themselves, thinned to `limit` — for the chart."""
        pts = self.points
        if len(pts) <= limit:
            return pts
        step = max(1, len(pts) // limit)
        return pts[::step][:limit]


# ---- process-level lifecycle -----------------------------------------------------------------
# The watcher has to span the WHOLE bench — sentinels, arena, harnesses, then the perf grid — because
# "sustained" is the entire point: a ladder run against a fresh engine cannot show degradation. It is
# started once near the top of a run and read at the end, in two places far apart in the call graph.
#
# Deliberately a module singleton rather than a parameter threaded through the run functions: a pod
# process runs ONE bench, and threading an optional argument through every signature between those
# two points is how this session already shipped a --think-budget that never reached _run_boards.

_ACTIVE = None


def begin(target_url, every_s=DEFAULT_EVERY_S):
    """Start sampling for this bench. Safe to call twice; never raises.

    Announces itself on stdout — which the live feed tees — because the watcher otherwise produces
    NO observable output until it is harvested hours later, at the very end of the run. A telemetry
    thread that silently failed to start would look identical to one working perfectly, right up
    until the section was missing from the results."""
    global _ACTIVE
    try:
        if _ACTIVE is None:
            _ACTIVE = Watcher(target_url, every_s=every_s).start()
            print("[pod] sustained-load telemetry: sampling %s every %ss for the whole bench"
                  % (_ACTIVE.url, int(every_s)), flush=True)
    except Exception as e:
        print("[pod] sustained-load telemetry unavailable (non-fatal): %r" % (e,), flush=True)
        _ACTIVE = None
    return _ACTIVE


def finish():
    """(summary, series) for the bench, or (None, []) when there was nothing to measure."""
    global _ACTIVE
    w = _ACTIVE
    _ACTIVE = None
    if not w:
        return None, []
    try:
        return w.stop(), w.series()
    except Exception:
        return None, []


def snapshot():
    """Current summary WITHOUT stopping — for the live view mid-run."""
    w = _ACTIVE
    if not w:
        return None
    try:
        return w.summary()
    except Exception:
        return None
