"""Pod-side arena artifact generation — every benchmark also produces playable artifacts.

For each benchmarked model the pod asks it to build a few single-file HTML artifacts
(apps / games / animations) from the SAME seeded prompt selection, so different models
answer identical prompts (true blind A/B on the mothership arena). The pod does NO DB
writes here — the artifacts ride inside the signed submission bundle
(pod/aeon_submit.py `Pod.submit(..., artifacts=...)`) and the mothership ingests them
in aeon/ingest.py.

Security note: the html is untrusted model output. It is treated as inert data here
(size-capped string) and only ever rendered client-side in a sandboxed iframe.
"""
from __future__ import annotations

import random

from aeon import arena
from aeon.targets import OpenAITarget

# Hard per-artifact cap (bytes of UTF-8). The mothership enforces the same cap on
# ingest — keep the two in sync so a bundle is never rejected for size.
MAX_HTML_BYTES = 200 * 1024


def _cap_html(html: str, limit: int = MAX_HTML_BYTES) -> str:
    b = html.encode("utf-8")
    if len(b) <= limit:
        return html
    return b[:limit].decode("utf-8", "ignore")


def pick_prompts(per_kind: int = 2, seed=None, only_difficulty=None):
    """Deterministically pick `per_kind` prompts per kind from aeon.arena.PROMPTS.

    Same seed (and same prompt corpus) -> same selection, independent of the model —
    that is what makes cross-model A/B comparisons fair. Per-kind RNG streams (seeded
    from a string, which CPython hashes with sha512 — stable across processes, unlike
    hash()) mean adding prompts to one kind never shifts another kind's picks.
    """
    out = []
    for kind in arena.KINDS:
        pool = sorted((p for p in arena.PROMPTS.get(kind, []) if not p.get("agent_only")),
                      key=lambda p: p["id"])
        if only_difficulty:                       # GOD MODE BENCH: the draw pool IS the god tier
            pool = [p for p in pool if p.get("difficulty") == only_difficulty]
        n = min(per_kind, len(pool))
        if n <= 0:
            continue
        rng = random.Random() if seed is None else random.Random(f"aeon-arena|{seed}|{kind}")
        # GUARANTEED GOD SLOT: when the kind has god_mode prompts, one draw slot is always
        # a god challenge (seeded choice among them) — god-tier generation is a reliable
        # part of every bench, not a lottery ticket, at identical total cost. The remaining
        # slots draw from the rest of the pool exactly as before.
        gods = [p for p in pool if p.get("difficulty") == "god_mode"]
        if gods and n >= 1:
            god_pick = rng.choice(gods)
            rest = [p for p in pool if p["id"] != god_pick["id"]]
            picks = [god_pick] + (rng.sample(rest, min(n - 1, len(rest))) if n > 1 else [])
        else:
            picks = rng.sample(pool, n)
        out.extend((kind, p) for p in picks)
    return out


class _MockArenaTarget:
    """The `target_url == "mock"` branch (mirrors arena.generate_artifact's mock path).

    aeon.targets.MockTarget's canned table is keyed by suite case ids and never emits
    HTML, so the arena mock returns a deterministic, valid single-file HTML document —
    letting the whole pipeline (extract -> cap -> bundle -> ingest) run green with no GPU.
    """

    def __init__(self, alias):
        self.model = alias

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        prompt = messages[-1].get("content", "") if messages else ""
        title = prompt.split(".")[0][:80]
        html = ("<!DOCTYPE html><html><head><meta charset=utf-8><title>mock</title></head>"
                "<body><h1>%s</h1><p>mock artifact by %s</p>"
                "<script>document.body.dataset.ok='1'</script></body></html>"
                % (title.replace("<", "").replace(">", ""), self.model))
        return {"text": html, "ttft_ms": 5.0, "decode_tps": 100.0, "e2e_ms": 8.0,
                "output_tokens": max(1, len(html) // 4), "finish_reason": "stop",
                "streamed": True}


def _make_target(target_url, alias, api_key, conc=1):
    if target_url == "mock":
        return _MockArenaTarget(alias)
    # scale the per-request timeout with arena concurrency: streams time-slice the serve, so a
    # long god-tier generation runs slower wall-clock under contention (floored at the proven 600s)
    return OpenAITarget(target_url, alias, api_key=api_key, timeout=max(600, 120 * max(1, conc)))


def generate_for_model(target_url, alias, *, api_key=None, per_kind=2, seed=None,
                       max_tokens=8000, temperature=0.4, progress_cb=None,
                       only_difficulty=None, concurrency=1):
    """Generate arena artifacts for one model. NEVER raises.

    Returns a list of {kind, prompt_id, title, html, ok, gen_ms, bytes} dicts —
    exactly the shape aeon/ingest.py accepts as bundle["artifacts"]. A failed
    generation (target error, empty/non-HTML output) yields ok=False, html="".
    `progress_cb(done, total, item)` (optional) is called after each artifact.

    `concurrency` artifacts generate IN FLIGHT against the served model — the same
    endpoint the text/harness boards already hammer with a ThreadPoolExecutor, which
    batches concurrent streams. Artifacts are independent + unjudged at generation, so
    this is a pure throughput win; the returned list stays in seeded selection order
    (written by index, not completion order) and progress is a monotonic main-thread
    counter, so determinism + the (1,N)->(N,N) progress contract are preserved.
    `concurrency<=1` keeps the exact single-stream loop (mock / no-GPU fallback).
    """
    selection = pick_prompts(per_kind=per_kind, seed=seed, only_difficulty=only_difficulty)
    total = len(selection)
    workers = max(1, min(int(concurrency or 1), total or 1))
    try:
        target = _make_target(target_url, alias, api_key, workers)
    except Exception:
        target = None  # constructor failure -> every artifact reports ok=False below

    def _gen_one(kind, p):
        """ONE artifact -> its item dict. NEVER raises. Thread-safe: OpenAITarget.chat builds a
        fresh request per call and holds only immutable config, so worker threads share one target
        exactly as the text/harness boards do."""
        html, ok, gen_ms = "", False, None
        try:
            if target is None:
                raise RuntimeError("target unavailable")
            msgs = [{"role": "system", "content": arena.SYS},
                    {"role": "user", "content": p["prompt"]}]
            resp = target.chat(msgs, temperature=temperature, max_tokens=max_tokens)
            html = _cap_html(arena.extract_html(resp.get("text", "")))
            ok = bool(html.strip()) and "<" in html
            gen_ms = resp.get("e2e_ms")
            if not ok:
                html = ""
        except Exception:
            html, ok, gen_ms = "", False, None
        return {"kind": kind, "prompt_id": p["id"], "title": p["title"], "html": html,
                "ok": ok, "gen_ms": gen_ms, "bytes": len(html.encode("utf-8"))}

    out = [None] * total
    if workers <= 1:                                   # serial fallback — mock / single-stream
        for i, (kind, p) in enumerate(selection):
            out[i] = _gen_one(kind, p)
            if progress_cb:
                try:
                    progress_cb(i + 1, total, out[i])
                except Exception:
                    pass
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_gen_one, kind, p): i for i, (kind, p) in enumerate(selection)}
        done = 0
        try:
            for fut in as_completed(futs):
                i = futs[fut]
                out[i] = fut.result()                  # _gen_one never raises
                done += 1
                if progress_cb:                        # monotonic (1..N) on the MAIN thread only
                    try:
                        progress_cb(done, total, out[i])
                    except Exception:
                        pass
        except BaseException:                          # interrupt/bug: cancel cleanly, then re-raise
            for f in futs:
                f.cancel()
            raise
    return out
