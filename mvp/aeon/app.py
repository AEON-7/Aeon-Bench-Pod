"""FastAPI mothership (MVP): serves the dashboard + the orchestration/ingest API.

Runs are executed in a background thread (in-process probe) — faithful to the
"local run" mode in DESIGN §3, minus the container orchestration.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import arena, attest, db, evaluators, frontier, modelmeta, probe, runner, scoring, vram

# Mothership-only trust surface: evaluator accounts/auth, the admin portal, and the
# signed-submission ingest gate. These modules are NOT part of the public pod
# distribution — a pod boots without them and every route that needs them 404s
# (see _no_trust_stack). The mothership (private repo) always has them.
try:
    from . import accounts, admin, ingest
except ImportError:                                # public pod distribution
    accounts = admin = ingest = None               # type: ignore[assignment]
from . import suite as suite_mod
from . import vision_suite
from . import agentic_v2
from .targets import OpenAITarget, list_models

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
POD_REPO_URL = os.environ.get("AEON_POD_REPO", "https://github.com/AEON-7/Aeon-Bench-Pod")
# ROLE splits the SAME app into two dashboards: 'pod' = the user's LOCAL lab (their own runs + the
# LIVE view, read from local pod.db, plus a submit-verified action); 'mothership' = the GLOBAL
# authority (only accepted/attested runs — no live, no in-progress). Default mothership (safe).
ROLE = os.environ.get("AEON_ROLE", "mothership").lower()
IS_POD = ROLE == "pod"
# Optional lab lock for a pod exposed on a LAN/Tailscale: when AEON_POD_TOKEN is set, the pod's
# run-launcher + secrets endpoints require it (header x-aeon-pod-token or ?token=). Unset (default)
# = open, for a private single-operator box — lets an operator lock down without standing up accounts.
_POD_TOKEN = os.environ.get("AEON_POD_TOKEN") or None
# Pod deploy artifacts the dashboard's "Run a benchmark" panel offers for download (the user
# runs the benchmark from the pod, never from the mothership). Files live in the open pod repo.
_POD_FILES = {
    "docker-compose.yml": os.path.join(_REPO, "deploy", "pod", "docker-compose.yml"),
    "agents.md": os.path.join(_REPO, "deploy", "pod", "AGENTS.md"),
    ".env.example": os.path.join(_REPO, "deploy", "pod", ".env.example"),
    "run-a-benchmark.md": os.path.join(_REPO, "docs", "run-a-benchmark.md"),
}

app = FastAPI(title="AEON Bench — MVP")
db.init_db()
arena.seed_demo()
arena.seed_bogus()


@app.middleware("http")
async def _no_cache_assets(request: Request, call_next):
    """The dashboard HTML/JS/CSS must never be heuristically cached, or browsers run
    stale code after an update (the cause of the 'fix didn't take' login issue)."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


DEFAULT_KEY = os.environ.get("AEON_API_KEY") or None


class LaunchBody(BaseModel):
    model: str
    target_url: str = "http://127.0.0.1:11434/v1"
    judge_model: str | None = None
    api_key: str | None = None
    agent_judge: bool = False


class VerdictBody(BaseModel):
    case_id: str
    verdicts: list[dict] = []
    creativity: int | None = None
    creativity_reason: str | None = None


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    # no-cache on the SHELL: css/js are ?v= cache-busted, but the HTML that references them
    # must always revalidate — otherwise a browser that saw an old dashboard keeps rendering
    # stale markup after an image update ("where did the new Run options go?")
    return FileResponse(os.path.join(WEB, "index.html"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Liveness + drain signal for the edge load-balancer and zero-downtime deploys.
    The rolling-deploy script (deploy/onyx/scripts/deploy.sh) touches /tmp/draining
    inside a replica to pull it from the WAF rotation BEFORE recycling it; the WAF's
    active health probe sees the 503 and fails over to the peer within ~1s, so a
    request never lands on a container that is about to stop."""
    if os.path.exists("/tmp/draining"):
        return JSONResponse({"ok": False, "draining": True}, status_code=503)
    return {"ok": True}


@app.get("/api/config")
def config():
    """Dashboard role — the frontend shows the Live tab + local-lab affordances (Run tab) only on a pod."""
    return {"role": ROLE, "live": IS_POD, "suite_id": suite_mod.SUITE_ID,
            "pod_token_required": bool(_POD_TOKEN) if IS_POD else False}


@app.get("/api/suite")
def get_suite():
    return suite_mod.summary()


@app.get("/api/models")
def get_models(target: str = "http://127.0.0.1:11434/v1", api_key: str | None = None):
    # SSRF fix: /api/models is a pod/local-lab affordance (the Run tab); the mothership dashboard
    # never uses it. Gate it behind the SAME pod-only guard as /api/pod/* so it 404s off-pod and a
    # caller can't drive the mothership into arbitrary outbound requests. (targets.list_models also
    # validates the scheme/host as defense-in-depth.)
    if (g := _require_pod()):
        return g
    return {"target": target, "models": list_models(target, api_key=api_key or DEFAULT_KEY)}


@app.get("/api/leaderboard")
def leaderboard(suite: str | None = None):
    """Default = the comprehensive suite. `?suite=aeon-suite-v2-hard` shows a tier board on its own
    (hard runs are a different test and must not average into the comprehensive standing)."""
    return scoring.leaderboard(suite=suite)


@app.get("/api/perf/board")
def perf_board():
    """PERFORMANCE board / recipe-discovery: per model, the latest perf run's direct grid
    (TTFT/TPOT/tok-s per prompt category × concurrency) + through-harness task timing + the four
    headline axes (peak single-stream, peak aggregate, lowest latency, quality) — and the exact
    serve recipe behind them (assembled docker run + DFlash drafter disclosure), so the board is
    an optimal-recipe finder per hardware."""
    d = scoring.perf_board()
    for m in d.get("models", []):
        recipe = m.pop("recipe", None)               # raw recipe stays server-side; expose the assembly
        m["reproduction"] = {
            "docker_run_assembled": _docker_cmd(recipe, m.get("hf_repo"), m.get("hf_revision")),
            "bare_cmd": (recipe or {}).get("bare_cmd"),   # MLX bare-metal recipe, same card
            "image": (recipe or {}).get("image"),
            "engine": (recipe or {}).get("engine"),
            "engine_version": (recipe or {}).get("engine_version"),
            "spec_decode": (recipe or {}).get("spec_decode"),
            "drafter": _drafter_info(recipe),
            "run": m.get("run"),
        }
    return d


@app.get("/api/compare/seeds")
def compare_seeds(board: str = "text"):
    """Fast-bench seeds available for A/B comparison on this board (a seed run by >=2 models
    on the same suite is a ready apples-to-apples comparison)."""
    return {"board": board, "seeds": scoring.seed_index(board)}


@app.get("/api/compare/{seed}")
def compare_seed(seed: str, board: str = "text"):
    """True A/B for one fast-bench seed: every model's run on the IDENTICAL questions,
    aligned case-by-case so per-category and per-question strengths are directly comparable."""
    return scoring.compare_by_seed(seed, board)


_CAT_COUNTS = None


def _suite_cat_counts():
    global _CAT_COUNTS
    if _CAT_COUNTS is None:
        from collections import Counter
        _CAT_COUNTS = dict(Counter(c["category"] for c in suite_mod.CASES))
    return _CAT_COUNTS


# A run killed without finalizing (crash, pod restart mid-run) leaves a 'running' row that
# would keep the REC light on forever. Results rows carry no timestamp, so freshness is
# progress-change tracking: a 'running' row whose progress hasn't moved in _LIVE_STALE_S is
# a ghost and drops out of /api/live. (A real case never takes 45 min — resource limits kill
# it long before.) Cache is per-process; after a restart a ghost survives at most one window.
_LIVE_BEAT: dict = {}          # run id -> [progress-signature, last-change-ts]
_LIVE_STALE_S = 45 * 60


def _drop_stale_running(rows):
    now = time.time()
    fresh = []
    for r in rows:
        sig = (r.get("progress") or 0, r.get("n_cases") or 0)
        beat = _LIVE_BEAT.get(r["id"])
        if beat is None or beat[0] != sig:
            _LIVE_BEAT[r["id"]] = [sig, now]
            fresh.append(r)
        elif now - beat[1] <= _LIVE_STALE_S:
            fresh.append(r)
    live_ids = {r["id"] for r in rows}
    for k in [k for k in _LIVE_BEAT if k not in live_ids]:   # finished runs leave the cache
        _LIVE_BEAT.pop(k, None)
    return fresh


@app.get("/api/live")
def live(board: str = "text"):
    """In-progress (running) runs with their PARTIAL results — the live benchmark view. Fed by the
    pod's incremental checkpoints: per-category progress + a feed of the most recent prompts/answers
    as each case is scored. No polling cost beyond a normal read; safe to call every few seconds."""
    if not IS_POD:
        return {"running": [], "role": ROLE}   # LIVE is a POD view; the mothership shows only accepted runs
    running = [r for r in db.list_runs(200)
               if r.get("status") == "running" and (r.get("board") or "text") == board]
    running = _drop_stale_running(running)
    # A running row from a SUPERSEDED suite (e.g. the old aeon-suite-v2, 290 cases) — or a
    # different sub-suite that also files under board="text" (agentic-v2.1, 16 cases) — must not
    # hijack the card: its n_cases drives the headline total ("37/290") while the per-category
    # denominators come from the CURRENT suite via _suite_cat_counts() (/30), producing a
    # nonsensical mismatch. The text live view only describes the current text suite, so scope
    # it to SUITE_ID; other boards each have a single suite, so board-scoping already suffices.
    if board == "text":
        running = [r for r in running if r.get("suite_id") == suite_mod.SUITE_ID]
    # list_runs is newest-first; keep only the most-recent running run per model — older "running"
    # rows are stale partials a killed/relaunched run left behind.
    seen, dedup = set(), []
    for r in running:
        m = r.get("canonical_id") or r.get("model")
        if m not in seen:
            seen.add(m); dedup.append(r)
    running = dedup
    pm = _prompt_map(board)
    expected = _suite_cat_counts()
    out = []
    for run in running[:4]:
        full = db.get_run(run["id"])
        results = full.get("results", [])
        by_cat = {}
        for x in results:
            b = by_cat.setdefault(x["category"], {"done": 0, "sum": 0.0, "scored": 0})
            b["done"] += 1
            s = x.get("score")
            if isinstance(s, (int, float)):
                b["sum"] += s; b["scored"] += 1
        cats = []
        for c in suite_mod.CATEGORIES:
            b = by_cat.get(c, {"done": 0, "sum": 0.0, "scored": 0})
            cats.append({"category": c, "done": b["done"], "expected": expected.get(c, 0),
                         "mean": round(100 * b["sum"] / b["scored"], 1) if b["scored"] else None})
        recent = [{"case_id": x["case_id"], "category": x["category"], "score": x.get("score"),
                   "prompt": pm.get(x["case_id"], ""), "answer": (db.result_output(x) or "")[:1800],
                   "disputed": bool(x.get("disputed"))} for x in results[-14:]][::-1]
        scored = [x["score"] for x in results if isinstance(x.get("score"), (int, float))]
        out.append({"run": run["id"], "model": run.get("hf_repo") or run.get("model"),
                    "n_cases": run.get("n_cases") or 0, "done": len(results),
                    "mean": round(100 * sum(scored) / len(scored), 1) if scored else None,
                    "trust_tier": run.get("trust_tier"), "started_at": run.get("started_at"),
                    "categories": cats, "recent": recent})
    return {"running": out}


# ---- SHARE: server-rendered OG cards (scrapers read meta tags + fetch a PNG; no JS runs) ------

_SHARE_KEY_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _share_info(key: str):
    """Card payload for a share key (canonical id with '/'->'__'). None when unknown."""
    if len(key) > 140 or any(ch not in _SHARE_KEY_OK for ch in key):
        return None
    lb = scoring.leaderboard()
    row = rank = None
    kl = key.lower()                       # canonical ids are lowercased; display names aren't —
    for i, m in enumerate(lb.get("models") or []):     # accept either casing in a shared link
        if any((v or "").replace("/", "__").lower() == kl
               for v in (m.get("canonical"), m.get("model"))):
            row, rank = m, i + 1
            break
    if not row:
        return None
    peak = hw = None
    try:
        for pm in scoring.perf_board().get("models", []):
            if pm.get("canonical") == row.get("canonical"):
                peak, hw = pm.get("peak_agg_tps"), pm.get("hardware")
                break
    except Exception:
        pass
    # PEAK CONCURRENT = the best recorded aggregate for this model — the perf grid's peak OR
    # the quality run's aggregate under its test load (a stale/low perf entry, e.g. a
    # single-stream-tuned recipe, must not undersell a fresher concurrent number).
    row_agg = row.get("agg_tps")
    if row_agg and (not peak or row_agg > peak):
        peak = row_agg
    model = row.get("model") or row.get("canonical") or ""
    org, _, name = model.rpartition("/")
    avatar = None
    try:
        avatar = (modelmeta.resolve(model) or {}).get("avatar_url")
    except Exception:
        pass
    return {"model": model, "org": org, "name": name or model, "rank": rank,
            "composite": row.get("composite"), "peak_tps": peak,
            "trust": "attested" if row.get("record_eligible") else "local",
            "hardware": hw, "suite": f"{lb.get('suite_shown') or ''} · rank {rank}",
            "avatar_url": avatar}


@app.get("/api/share/card/{key}.png")
def share_card(key: str):
    """The 1200×630 social card PNG for one benchmark (cached; never 500s)."""
    from . import sharecard
    try:
        info = _share_info(key)
        png = sharecard.cached("m:" + key, (lambda: sharecard.render_model_card(info)) if info
                               else (lambda: sharecard.render_fallback_card()))
    except Exception:
        from . import sharecard as sc
        png = sc.render_fallback_card()
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=900"})


@app.get("/share/{key}", response_class=HTMLResponse)
def share_page(key: str):
    """Scraper-facing share page: OG/Twitter meta + instant hop into the app. The IMAGE carries
    the design; these tags carry the words."""
    info = _share_info(key)
    base = (os.environ.get("AEON_PUBLIC_URL") or "https://aeon-bench.com").rstrip("/")
    if info:
        bits = []
        if info.get("composite") is not None:
            bits.append(f"composite {info['composite']:.1f}")
        if info.get("peak_tps"):
            bits.append(f"peak {info['peak_tps']:.0f} tok/s concurrent")
        if info.get("trust") == "attested":
            bits.append("attested")
        title = f"{info['name']} — rank {info['rank']:02d} on AEON Bench"
        desc = " · ".join(bits) or "open, attested local-LLM benchmarks"
    else:
        title, desc = "AEON Bench", "Open, attested benchmarks for local LLMs — run a pod on your own hardware."
    img = f"{base}/api/share/card/{key}.png"
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta property="og:type" content="website"><meta property="og:site_name" content="AEON Bench">
<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">
<meta property="og:url" content="{base}/share/{e(key)}"><meta property="og:image" content="{img}">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{e(title)}">
<meta name="twitter:description" content="{e(desc)}"><meta name="twitter:image" content="{img}">
<meta name="theme-color" content="#00f0ff">
<meta http-equiv="refresh" content="0;url=/"></head>
<body style="background:#07070d;color:#e3e3ee;font-family:monospace">
<p>▲ AEON//BENCH — <a style="color:#00f0ff" href="/">continue to the board</a></p></body></html>"""


@app.get("/api/model/meta")
def model_meta(model: str | None = None):
    """Resolve a raw leaderboard model name -> creator/org card + circular avatar
    (HF org avatar, or the local Aeon mark for own models). Cached server-side and
    never blocks > ~2s; falls back to a generic avatar on miss. Never 500s."""
    if not model:
        return JSONResponse({"error": "model query param required"}, status_code=400)
    try:
        return modelmeta.resolve(model)
    except Exception:
        return JSONResponse(modelmeta.resolve(""), status_code=200)


@app.get("/api/system_presets")
def system_presets():
    """Known local-AI rigs (name -> usable VRAM GB) for the 'fits my system' filter."""
    return {"presets": vram.PRESETS}


@app.get("/api/runs")
def runs():
    return {"runs": db.list_runs()}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found", "status": "starting"}, status_code=404)
    return r


# NOTE: the mothership NEVER starts a run. It is an ingest / verify / display authority, not
# a producer — the verifier must not also be a producer. Benchmarks originate ONLY from pods
# (POST /api/v1/runs + .../results). The former launchers (POST /api/runs, /api/vision/runs,
# /api/audio/probe, /api/arena/generate) and the mothership-side agent-judge flow
# (/api/runs/{id}/pending + /verdict) were removed for this. Judging happens at the pod and is
# frontier-model-or-deterministic only (no self-judge) — see aeon.judge_policy.


# ---- Vision board ----

@app.get("/api/vision/suite")
def vision_suite_summary():
    return vision_suite.summary()


@app.get("/api/vision/leaderboard")
def vision_leaderboard():
    return scoring.vision_leaderboard()


# (vision + audio run launchers removed — runs originate only from pods; see note above.)


# ---- Generated-artifact arena (Apps / Games / Animations + human voting) ----

class ArenaGenBody(BaseModel):
    kind: str
    prompt_id: str
    model: str
    target_url: str = "http://127.0.0.1:11434/v1"
    api_key: str | None = None


class ArenaVoteBody(BaseModel):
    match_id: str
    winner: str  # a|b|tie


class AuthBody(BaseModel):
    username: str
    password: str


class PasswordChangeBody(BaseModel):
    current_password: str
    new_password: str


def _token_of(request: Request):
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-aeon-token")


def _no_trust_stack():
    """404 when the mothership trust stack (accounts/admin/ingest) isn't shipped —
    i.e. on the public pod distribution, which has no evaluator accounts to serve."""
    return None if accounts is not None else JSONResponse(
        {"error": "not available"}, status_code=404)


# ---- evaluator accounts (anonymous: username + password, capped per IP) ----

@app.post("/api/auth/signup")
def auth_signup(body: AuthBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    r = accounts.signup(body.username, body.password, accounts.client_ip(request))
    if "error" in r:
        return JSONResponse(r, status_code=429 if "too many" in r["error"] else 400)
    return r


@app.post("/api/auth/login")
def auth_login(body: AuthBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    r = accounts.login(body.username, body.password, accounts.client_ip(request))
    if "error" in r:
        code = 429 if ("too many" in r["error"] or "locked" in r["error"]) else 401
        return JSONResponse(r, status_code=code)
    return r


@app.get("/api/auth/me")
def auth_me(request: Request):
    if (g := _no_trust_stack()):
        return g
    u = accounts.user_from_request(request)
    if not u:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    return {"user": accounts.public_state(u["id"])}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    t = _token_of(request)
    if t:
        db.delete_session(t)
    return {"ok": True}


@app.post("/api/auth/password")
def auth_change_password(body: PasswordChangeBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    u = accounts.user_from_request(request)
    if not u:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    r = accounts.change_password(u["id"], body.current_password, body.new_password,
                                 accounts.client_ip(request), keep_token=_token_of(request))
    if "error" in r:
        code = 429 if "too many" in r["error"] else (401 if "incorrect" in r["error"] else 400)
        return JSONResponse(r, status_code=code)
    return r


@app.get("/api/arena/prompts")
def arena_prompts():
    return {"kinds": arena.KINDS, "labels": arena.KIND_LABEL, "prompts": arena.all_prompts()}


@app.get("/api/arena/artifacts")
def arena_artifacts(kind: str | None = None, prompt_id: str | None = None):
    return {"artifacts": db.list_artifacts(kind=kind, prompt_id=prompt_id)}


@app.get("/api/arena/render")
def arena_render(request: Request, match_id: str | None = None, side: str | None = None,
                 artifact_id: str | None = None):
    """Render one side of a match for ITS OWNER only, returning ONLY the html — never
    prompt_id/model/bogus metadata. This is the sole way the client gets artifact
    bodies, so a honeypot decoy cannot be identified before voting and the model
    identity stays hidden until the vote response reveals it.

    Gallery mode (?artifact_id=): public read of ONE ok, non-bogus artifact body for
    the Code Gallery's sandboxed preview. Missing, failed and bogus ids all 404
    identically, so this branch can never be used to probe for honeypot decoys."""
    if artifact_id:
        a = db.get_artifact(artifact_id)
        if not a or a.get("bogus") or not a.get("ok"):
            return JSONResponse({"error": "not found"}, status_code=404)
        return {"html": a["html"]}
    if (g := _no_trust_stack()):   # match-render needs accounts; gallery mode above doesn't
        return g
    u = accounts.user_from_request(request)
    if not u:
        return JSONResponse({"error": "sign in"}, status_code=401)
    m = db.get_match(match_id)
    if not m or m["user_id"] != u["id"] or side not in ("a", "b"):
        return JSONResponse({"error": "not found"}, status_code=404)
    art = db.get_artifact(m["a_id"] if side == "a" else m["b_id"])
    if not art:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"html": art["html"]}


@app.get("/api/arena/ranking")
def arena_ranking(kind: str | None = None):
    return {"ranking": arena.ranking(kind)}


# ---- public Code Gallery (top-rated artifacts per prompt + full-source download) ----

@app.get("/api/arena/gallery")
def arena_gallery(kind: str):
    """Per-prompt top artifacts with per-artifact Elo (replaying the same trust-filtered
    votes as the model ranking). Metadata only — artifact bodies are fetched per click
    via the sandboxed render route / the zip download below."""
    if kind not in arena.KINDS:
        return JSONResponse({"error": "kind must be one of " + "|".join(arena.KINDS)},
                            status_code=400)
    return {"kind": kind, "label": arena.KIND_LABEL.get(kind, kind),
            "prompts": arena.gallery(kind)}


@app.get("/api/arena/download/{aid}")
def arena_download(aid: str):
    """One gallery artifact as an in-memory ZIP: index.html (the artifact, verbatim) +
    README.md (provenance). Missing, failed and bogus artifacts all 404 IDENTICALLY —
    a distinct status for decoys would leak which ids are honeypots."""
    a = db.get_artifact(aid)
    if not a or a.get("bogus") or not a.get("ok"):
        return JSONResponse({"error": "not found"}, status_code=404)
    p = arena.find_prompt(a["kind"], a["prompt_id"])
    title = p["title"] if p else a["prompt_id"]
    r = arena.artifact_ratings(a["kind"]).get(a["id"])
    rating = (f"Elo {round(r['elo'])} · {r['w']}W-{r['l']}L-{r['t']}T over {r['votes']} counted vote(s)"
              if r else "unrated (no counted votes yet)")
    when = time.strftime("%Y-%m-%d", time.gmtime(a["created_at"])) if a.get("created_at") else "unknown"
    readme = (
        f"# {title} — AEON Bench arena artifact\n\n"
        f"- model: {a['model']}\n"
        f"- kind: {arena.KIND_LABEL.get(a['kind'], a['kind'])} ({a['kind']})\n"
        f"- prompt: {title} ({a['prompt_id']})\n"
        f"- rating: {rating}\n"
        f"- generated: {when}\n\n"
        f"This single-file artifact was generated by the model `{a['model']}` on the "
        f"AEON Bench arena and ranked by blind human A/B votes.\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.html", a["html"] or "")
        z.writestr("README.md", readme)
    # ids/kinds/prompt ids are our own charset, but the header value must stay quote-safe
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", f"aeon-{a['kind']}-{a['prompt_id']}-{a['id']}.zip")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# (arena artifact generation removed — the mothership does not produce content; arena
#  artifacts are submitted by pods. The mothership only serves matches + records votes.)


@app.get("/api/arena/match")
def arena_match(request: Request, kind: str, prompt_id: str | None = None):
    if (g := _no_trust_stack()):
        return g
    u = accounts.user_from_request(request)
    if not u:
        return JSONResponse({"error": "sign in to evaluate"}, status_code=401)
    m = arena.build_match(u, kind, prompt_id=prompt_id or None)
    if m is arena.EXHAUSTED:
        return JSONResponse({"error": "you've reviewed every available comparison in this category — "
                                      "new artifacts arrive as each benchmark completes",
                             "exhausted": True}, status_code=409)
    if not m:
        return JSONResponse({"error": "not enough artifacts in this category yet — generate some",
                             "need_generate": True}, status_code=409)
    return m


@app.post("/api/arena/vote")
def arena_vote(body: ArenaVoteBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    u = accounts.user_from_request(request)
    if not u:
        return JSONResponse({"error": "sign in to vote"}, status_code=401)
    result, status = arena.submit_vote(u, body.match_id, body.winner)
    return result if status == 200 else JSONResponse(result, status_code=status)


# ---- Admin: integrity + moderation (gated by AEON_ADMIN_USERS) ----

class AdminUserBody(BaseModel):
    user_id: str


class AdminArtifactBody(BaseModel):
    artifact_id: str


def _require_admin(request: Request):
    if accounts is None:                 # pod distribution ships no admin surface
        return None
    u = accounts.user_from_request(request)
    return u if accounts.is_admin(u) else None


@app.get("/api/admin/evaluators")
def admin_evaluators(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return admin.summary()


@app.get("/api/admin/evaluator/history")
def admin_evaluator_history(user_id: str, request: Request):
    """One evaluator's vote trail (incl. honeypot verdicts) — the evidence behind
    their trust score. Admin-only; model names in honeypot rows are internal."""
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"user_id": user_id, "votes": db.admin_vote_history(user_id)}


@app.post("/api/admin/ban")
def admin_ban(body: AdminUserBody, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.set_user_flags(body.user_id, status="flagged")
    return {"ok": True}


@app.post("/api/admin/unban")
def admin_unban(body: AdminUserBody, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.set_user_flags(body.user_id, status="active")
    return {"ok": True}


@app.get("/api/admin/artifacts")
def admin_artifacts(request: Request, kind: str | None = None):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"artifacts": db.list_artifacts(kind=kind)}   # real generations (bogus excluded)


@app.get("/api/admin/artifact/{aid}")
def admin_artifact(aid: str, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    a = db.get_artifact(aid)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": a["id"], "model": a["model"], "prompt_id": a["prompt_id"], "html": a["html"]}


@app.post("/api/admin/artifact_delete")
def admin_artifact_delete(body: AdminArtifactBody, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.delete_artifact(body.artifact_id)
    return {"ok": True}


# ---- Submission transparency + admin oversight (every run fully inspectable) ----

class AdminFlagBody(BaseModel):
    run_id: str
    flagged: bool = True
    reason: str | None = None


class AdminRunBody(BaseModel):
    run_id: str


def _prompt_map(board):
    cases = vision_suite.CASES if board == "vision" else suite_mod.CASES
    pm = {c["id"]: c.get("prompt", "") for c in cases}
    # Harness (agentic-v2) runs are stored under the text board but their case ids come from
    # agentic_v2.CASES — fold them in so harness-case prompts resolve in the transparency drill-down.
    for c in agentic_v2.CASES:
        pm.setdefault(c["id"], c.get("prompt", ""))
    return pm


def _difficulty_map():
    """case_id -> difficulty class (easy/medium/hard/expert/frontier) from every suite that
    declares one — shown on each prompt in the submission detail explorer."""
    dm = {}
    for c in suite_mod.CASES:
        if c.get("difficulty"):
            dm[c["id"]] = c["difficulty"]
    for c in agentic_v2.CASES:
        if c.get("difficulty"):
            dm.setdefault(c["id"], c["difficulty"])
    return dm


_DIFF_MAP = _difficulty_map()


def _harness_transparency(raw_out):
    """Parse a harness result's stored transcript ({answer, steps:[{tool,args}], raw}) into a
    STRUCTURED transparency object the UI can render: the agent's final answer, its tool-call
    trajectory (one entry per step), and a slice of the raw harness output. Tolerant of a
    malformed/absent transcript (an errored run stores {error: ...} instead)."""
    doc = {}
    try:
        doc = json.loads(raw_out) if raw_out else {}
    except (ValueError, TypeError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    steps = doc.get("steps")
    trajectory = []
    if isinstance(steps, list):
        for s in steps:
            if isinstance(s, dict):
                trajectory.append({"tool": s.get("tool", ""), "args": s.get("args")})
    return {
        "harness_case": True,
        "final_answer": doc.get("answer", ""),
        "trajectory": trajectory,
        "raw": doc.get("raw", ""),
        "harness_error": doc.get("error"),
    }


@app.get("/api/submissions")
def submissions(board: str | None = None, model: str | None = None, limit: int = 300):
    rows = db.list_submissions(board=board, model=model, limit=limit)
    if not IS_POD:                        # mothership shows only ACCEPTED (succeeded) runs, never in-progress
        rows = [r for r in rows if r.get("status") == "succeeded"]
    means = db.run_mean_scores()
    cats = db.run_category_scores()
    for r in rows:
        m = means.get(r["id"])
        r["mean_score"] = round(100 * m, 1) if m is not None else None
        r["categories"] = cats.get(r["id"]) or {}
    return {"submissions": rows}


@app.get("/api/compare_runs")
def compare_runs(a: str, b: str):
    # NOTE: deliberately NOT /api/compare/runs — the parametric /api/compare/{seed} route
    # would swallow "runs" as a seed name (registration order made it win).
    """SIDE-BY-SIDE run comparison: two models — or the same model under two recipes — joined
    per case. Each row carries both answers, both scores, the case's category + difficulty;
    the headers carry each run's summary + reproduction so recipe deltas are visible."""
    da, db_ = submission_detail(a), submission_detail(b)
    for d, key in ((da, a), (db_, b)):
        if not isinstance(d, dict):
            return JSONResponse({"error": f"run {key} not found"}, status_code=404)

    def _slim(d):
        cats, comp = {}, None
        by_cat = {}
        for c in d["cases"]:
            if isinstance(c.get("score"), (int, float)):
                by_cat.setdefault(c["category"], []).append(c["score"])
        cats = {k: round(100 * sum(v) / len(v), 1) for k, v in by_cat.items()}
        comp = round(sum(cats.values()) / len(cats), 1) if cats else None
        return {"run": d["run"], "reproduction": d.get("reproduction") or {},
                "env": d.get("env") or {}, "categories": cats, "composite": comp}

    ca = {c["case_id"]: c for c in da["cases"]}
    cb = {c["case_id"]: c for c in db_["cases"]}
    keys = [k for k in ca if k in cb]
    # stable suite order: category then case id
    keys.sort(key=lambda k: (ca[k]["category"] or "", k))

    def _side(c):
        return {"score": c.get("score"), "status": c.get("status"),
                "answer": c.get("final_answer") if c.get("harness_case") else c.get("answer"),
                "speed": c.get("speed"), "judged_by": c.get("judged_by")}

    cases = [{"case_id": k, "category": ca[k]["category"], "tier": ca[k]["tier"],
              "difficulty": ca[k].get("difficulty"), "prompt": ca[k].get("prompt", ""),
              "a": _side(ca[k]), "b": _side(cb[k])} for k in keys]
    return {"a": _slim(da), "b": _slim(db_), "cases": cases,
            "only_a": sorted(set(ca) - set(cb)), "only_b": sorted(set(cb) - set(ca))}


@app.get("/api/harness_passes")
def harness_passes(model: str):
    """Group one model's harness runs into bench PASSES — the 3-harness sweep of a single
    comprehensive run — so the UI can compare hermes/openclaw/opencode side by side with the
    full prompt + tool-call + response log per task. Clustering: runs time-sorted; a >45 min
    gap or a repeated harness starts a new pass (one pass never runs a harness twice)."""
    rows = db.all_results_with_runs(board="text")
    per_run = {}
    for r in rows:
        if not r.get("harness"):
            continue
        if (r.get("canonical_id") or r.get("model")) != model and r.get("model") != model:
            continue
        d = per_run.setdefault(r["run"], {
            "run_id": r["run"], "harness": r["harness"],
            "harness_version": r.get("harness_version"),
            "started_at": r.get("started_at") or 0, "scores": []})
        if r.get("score") is not None:
            d["scores"].append(r["score"])
    runs = sorted(per_run.values(), key=lambda x: x["started_at"])
    passes, cur = [], None
    for r in runs:
        sc = r.pop("scores")
        r["n_cases"] = len(sc)
        r["mean_score"] = round(100 * sum(sc) / len(sc), 1) if sc else None
        if cur is None or r["started_at"] - cur["_last"] > 2700 or r["harness"] in cur["runs"]:
            cur = {"started_at": r["started_at"], "_last": r["started_at"], "runs": {}}
            passes.append(cur)
        cur["runs"][r["harness"]] = r
        cur["_last"] = r["started_at"]
    for p in passes:
        p.pop("_last", None)
    passes.reverse()
    return {"model": model, "passes": passes}


@app.get("/api/harness_runs")
def harness_runs(model: str, harness: str, board: str = "text"):
    """Resolve the harness-board cell (model × harness) to its underlying RUN(s) so the matrix can
    drill into per-case transparency. A cell aggregates every succeeded harness run for that model
    on that harness — returns them newest first, each with its run_id + mean score + case count."""
    rows = db.all_results_with_runs(board=board)
    per_run = {}
    for r in rows:
        if r.get("harness") != harness:
            continue
        if (r.get("canonical_id") or r.get("model")) != model and r.get("model") != model:
            continue
        d = per_run.setdefault(r["run"], {
            "run_id": r["run"], "model": r.get("model"),
            "harness": harness, "harness_version": r.get("harness_version"),
            "started_at": r.get("started_at"), "scores": []})
        if r.get("score") is not None:
            d["scores"].append(r["score"])
    out = []
    for d in per_run.values():
        sc = d.pop("scores")
        d["n_cases"] = len(sc)
        d["mean_score"] = round(100 * sum(sc) / len(sc), 1) if sc else None
        out.append(d)
    out.sort(key=lambda d: d.get("started_at") or 0, reverse=True)
    return {"runs": out}


@app.get("/api/submissions/{run_id}")
def submission_detail(run_id: str):
    """Full transparency for one run: per case — what was ASKED, how it was ANSWERED,
    the score, and HOW + BY WHAT it was judged (with rationale)."""
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    board = r.get("board", "text")
    is_harness = bool(r.get("harness"))
    pm = _prompt_map(board)
    cases = []
    for x in r.get("results", []):
        ev = x.get("evidence")
        if ev is None:
            ev = {}
        # Agentic-v2 (harness) cases carry LIST evidence [{criterion, ok, detail}] and their
        # deterministic verdict is produced by the program checker.
        harness_case = is_harness or isinstance(ev, list)
        if harness_case:
            judged_by = "program (deterministic)"
        elif x["tier"] == 0:
            judged_by = "program (deterministic)"
        elif isinstance(ev, dict) and ev.get("judged_by") == "agent":
            judged_by = "agent (launcher)"
        else:
            judged_by = r.get("judge_model") or "self-judge"
        raw_out = db.result_output(x)
        case = {
            "case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
            "status": x["status"], "score": x["score"], "creativity": x.get("creativity"),
            "difficulty": _DIFF_MAP.get(x["case_id"]),
            "prompt": pm.get(x["case_id"], ""), "answer": raw_out,
            "judged_by": judged_by, "evidence": ev, "speed": x.get("speed"),
            "disputed": bool(x.get("disputed")), "disputed_reason": x.get("disputed_reason"),
        }
        if harness_case:
            # raw_output is the compact transcript JSON {answer, steps:[{tool,args}], raw}.
            # Surface a STRUCTURED trajectory: the agent's final answer, its tool-call steps, and
            # a slice of the raw harness output — so viewers see exactly how the harness handled it.
            case.update(_harness_transparency(raw_out))
        cases.append(case)
    return {
        "run": {k: r.get(k) for k in (
            "id", "model", "board", "status", "judge_model", "judge_is_self", "suite_id",
            "suite_hash", "n_cases", "started_at", "finished_at", "flagged", "flag_reason",
            "trust_tier", "bench_seed",
            # disclosure: model identity + judge + the exact agentic harness build used
            "canonical_id", "hf_repo", "hf_revision", "model_verified",
            "harness", "harness_version")},
        "env": json.loads(r.get("env_json") or "{}"),
        # REPRODUCTION: the exact serve recipe (image + engine version + docker_run) and the
        # DETECTED hardware (pulled from the machine the bench ran on, not the operator's claim).
        "reproduction": _reproduction(r),
        "cases": cases,
        "manifest_url": f"/api/runs/{run_id}/manifest",
    }


# engine name (as recorded in pod recipes) -> a public image viewers can actually pull.
# Recipes that already carry an explicit "image" field win over this map.
_ENGINE_IMAGES = {
    "aeon-vllm-ultimate": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
    "vllm": "vllm/vllm-openai:latest",
}


def _sh_quote(tok):
    """Minimal POSIX-shell quoting for a serve flag value (JSON configs, globs, spaces)."""
    tok = str(tok)
    if tok and not any(ch in tok for ch in " \t\"'{}[]$`\\"):
        return tok
    return "'" + tok.replace("'", "'\\''") + "'"


def _recipe_serve(recipe):
    """Resolved (image, port, flags, drafter) for a run's stored serve recipe, or None when
    the run carries no serve flags (e.g. external-endpoint self-reported runs)."""
    flags = (recipe or {}).get("flags")
    if not flags:
        return None
    image = (recipe.get("image")
             or _ENGINE_IMAGES.get(recipe.get("engine") or "")
             or recipe.get("engine") or "vllm/vllm-openai:latest")
    return image, recipe.get("port") or 8000, [str(f) for f in flags], recipe.get("drafter")


def _drafter_info(recipe):
    """Speculative-decode disclosure for a stored recipe, or None for plain decode.

    DFlash pins a LOCAL drafter dir; the public z-lab HF drafter repo (`drafter_repo`) lets others
    replicate. Native MTP has no drafter, so the method/n are parsed from --speculative-config.
    """
    if not recipe:
        return None
    n = recipe.get("spec_decode_n") or recipe.get("drafter_n") or recipe.get("drafter_nst")
    method = recipe.get("spec_decode_method") or recipe.get("spec_decode")
    spec_model = None
    for seq in (recipe.get("flags") or [], recipe.get("command") or []):
        for i, f in enumerate(seq):
            if f == "--speculative-config" and i + 1 < len(seq):
                try:
                    cfg = json.loads(seq[i + 1])
                    method = method or cfg.get("method")
                    n = n or cfg.get("num_speculative_tokens")
                    spec_model = cfg.get("model")
                except Exception:
                    pass
                break
            if isinstance(f, str) and f.startswith("--speculative-config="):
                try:
                    cfg = json.loads(f.split("=", 1)[1])
                    method = method or cfg.get("method")
                    n = n or cfg.get("num_speculative_tokens")
                    spec_model = cfg.get("model")
                except Exception:
                    pass
                break
    if not (recipe.get("drafter") or recipe.get("drafter_repo") or method):
        return None
    method = method or "dflash"
    uses_drafter = bool(recipe.get("drafter") or recipe.get("drafter_repo") or
                       (str(method).lower() == "dflash" and str(spec_model or "").startswith("/drafter")))
    return {"method": method,
            "repo": recipe.get("drafter_repo"),          # e.g. z-lab/gemma-4-26B-A4B-it-DFlash
            "revision": recipe.get("drafter_revision"), "n": n,
            "uses_drafter": uses_drafter}


def _portable_speculative(flags):
    """Flags with any --speculative-config drafter `model` path normalised to the /drafter mount,
    so the assembled command never leaks (or depends on) a bench-host-local drafter path."""
    out = list(flags)
    for i, f in enumerate(out):
        if f == "--speculative-config" and i + 1 < len(out):
            try:
                cfg = json.loads(out[i + 1])
                if isinstance(cfg, dict) and cfg.get("model"):
                    cfg["model"] = "/drafter"
                    out[i + 1] = json.dumps(cfg)
            except Exception:
                pass
    return out


def _docker_cmd(recipe, hf_repo, hf_revision):
    """Assemble the copy-pasteable replication command for a run's stored serve recipe.
    Flags are VERBATIM — identical serve settings, minus the bench itself. Host-specific paths
    are made portable: weights mount at ./weights, and (for DFlash spec-decode) the z-lab drafter
    is pulled by NAME to ./drafter and mounted at /drafter, with num_speculative_tokens disclosed.
    Docker flag/startup choices move real performance per model, so this is the exact config
    behind the numbers on the board — and it truly replicates, drafter included."""
    serve = _recipe_serve(recipe)
    if not serve:
        return None
    image, port, flags, _ = serve
    d = _drafter_info(recipe)
    if d and d.get("uses_drafter"):
        flags = _portable_speculative(flags)       # point --speculative-config at the /drafter mount
    lines = []
    if hf_repo:
        rev = f" --revision {hf_revision}" if hf_revision else ""
        lines += ["# 1) pull the exact weights this run benchmarked (sha256-verified upstream)",
                  f"hf download {hf_repo}{rev} --local-dir ./weights", ""]
    if d and d.get("repo"):
        drev = f" --revision {d['revision']}" if d.get("revision") else ""
        ncmt = f", num_speculative_tokens={d['n']}" if d.get("n") else ""
        lines += [f"# 1b) pull the z-lab DFlash drafter — lossless speculative decode (speed only{ncmt})",
                  f"hf download {d['repo']}{drev} --local-dir ./drafter", ""]
    if d:
        method = str(d.get("method") or "dflash")
        if method.lower() == "dflash":
            disc = f"DFlash spec-decode: {d['repo'] or 'z-lab drafter (repo not recorded in this run)'}"
        elif "mtp" in method.lower():
            disc = f"Native MTP spec-decode: {method}"
        else:
            disc = f"Spec-decode: {method}"
        if d.get("revision"):
            disc += f"@{str(d['revision'])[:12]}"
        if d.get("n"):
            disc += f" · n={d['n']}"
        lines.append("# " + disc + " — lossless: the target verifies every draft token; speed only")
    lines.append("# 2) serve with the exact flags from this run")
    lines.append("docker run --rm --gpus all --name replica \\")
    lines.append("  -v ./weights:/model \\")
    if d and d.get("uses_drafter"):
        lines.append("  -v ./drafter:/drafter \\"
                     + (f"  # {d['repo']}" if d.get("repo") else "  # z-lab DFlash drafter weights"))
    lines.append(f"  -p {port}:{port} \\")
    lines.append(f"  --entrypoint vllm {image} \\")
    lines.append("  serve /model \\")
    i, rows = 0, []
    while i < len(flags):                      # group "--flag value" pairs, one per line
        f = flags[i]
        if f.startswith("--") and i + 1 < len(flags) and not flags[i + 1].startswith("--"):
            rows.append("  " + f + " " + _sh_quote(flags[i + 1])); i += 2
        else:
            rows.append("  " + _sh_quote(f)); i += 1
    lines += [rw + (" \\" if n < len(rows) - 1 else "") for n, rw in enumerate(rows)]
    return "\n".join(lines)


def _reproduction(r):
    """Serve recipe + detected hardware for a run, for exact reproduction by viewers."""
    recipe = json.loads(r.get("recipe") or "null")
    dm = json.loads(r.get("deployment_manifest") or "null") or {}
    env = json.loads(r.get("env_json") or "{}")
    hw = (env.get("hardware") or {}) if isinstance(env, dict) else {}
    return {
        "image": (recipe or {}).get("image") if recipe else None,
        "engine": (recipe or {}).get("engine") if recipe else None,
        "engine_version": (recipe or {}).get("engine_version") if recipe else None,
        "docker_run": (recipe or {}).get("docker_run") if recipe else None,
        # bare-metal serves (Apple MLX — macOS can't run MLX in a container) report their startup
        # recipe EXACTLY like a docker recipe: same card, honestly labeled.
        "bare_cmd": (recipe or {}).get("bare_cmd") if recipe else None,
        "serve_mode": (recipe or {}).get("serve_mode") if recipe else None,
        "flags": (recipe or {}).get("flags") if recipe else None,
        "spec_decode": (recipe or {}).get("spec_decode") if recipe else None,
        # DFlash drafter disclosure (repo + revision + n) so viewers can truly replicate spec-decode
        "drafter": _drafter_info(recipe),
        "weights_hash": r.get("weights_hash"),
        "hf_repo": r.get("hf_repo"), "hf_revision": r.get("hf_revision"),
        "build_hash": dm.get("build_hash"),
        # hardware AS DETECTED on the bench machine (single/dual DGX Spark, RTX 5090, Strix Halo, …)
        "hardware_detected": hw.get("detected_label"),
        "hardware_claimed": hw.get("label"),
        "gpus": hw.get("gpus"), "platform": hw.get("platform"), "machine": hw.get("machine"),
        # copy-pasteable replication command assembled from the stored recipe (None when
        # the run carries no serve flags, e.g. external-endpoint self-reported runs)
        "docker_run_assembled": _docker_cmd(recipe, r.get("hf_repo"), r.get("hf_revision")),
    }


# ---- downloadable replication files (serve.sh / docker-compose.yml) --------------------------

def _replicate_header(r):
    """Shared provenance comment block for the downloadable replication files."""
    env = json.loads(r.get("env_json") or "{}")
    hw = (env.get("hardware") or {}) if isinstance(env, dict) else {}
    hf = (r.get("hf_repo") or "?") + (f"@{r['hf_revision']}" if r.get("hf_revision") else "")
    lines = ["AEON Bench — replicate this attested serve",
             f"model:      {r.get('model')}",
             f"hf:         {hf}",
             f"weights:    sha256 {r.get('weights_hash') or 'n/a'}",
             f"benched on: {hw.get('detected_label') or hw.get('label') or 'unknown'}",
             f"run:        {r.get('id')}",
             f"provenance: https://aeon-bench.com/api/runs/{r.get('id')}/manifest (signed)"]
    return "\n".join("# " + l for l in lines)


def _replicate_script(r, recipe):
    """serve.sh: header + the same hf-download pre-step and docker run as the repro card."""
    cmd = _docker_cmd(recipe, r.get("hf_repo"), r.get("hf_revision"))
    if not cmd:
        return None
    return "#!/usr/bin/env bash\n" + _replicate_header(r) + "\n\n" + cmd + "\n"


# words YAML would parse as booleans/null if left bare in the compose command list
_YAML_BARE_BAD = frozenset(("true", "false", "yes", "no", "on", "off", "null", "none", "~"))


def _yaml_quote(tok):
    """Single-quote a YAML sequence scalar unless it's plainly safe: JSON-y flag values,
    numbers, and boolean-ish words are quoted so compose sees the VERBATIM string."""
    tok = str(tok)
    bare = (tok and any(c.isalpha() for c in tok)
            and all(c.isalnum() or c in "._/-=" for c in tok)
            and tok.lower() not in _YAML_BARE_BAD)
    return tok if bare else "'" + tok.replace("'", "''") + "'"


def _compose_yaml(r, recipe):
    """docker-compose.yml equivalent of the serve script (string-built — no yaml dep)."""
    serve = _recipe_serve(recipe)
    if not serve:
        return None
    image, port, flags, _ = serve
    d = _drafter_info(recipe)
    if d and d.get("uses_drafter"):
        flags = _portable_speculative(flags)       # point --speculative-config at the /drafter mount
    cmd = "\n".join("      - " + _yaml_quote(t) for t in ["serve", "/model"] + flags)
    vols = "      - ./weights:/model"
    if d and d.get("uses_drafter"):
        vols += ("\n      - ./drafter:/drafter"
                 + (f"   # {d['repo']}" if d.get("repo") else "   # z-lab DFlash drafter weights"))
    usage = ""
    if r.get("hf_repo"):
        rev = f" --revision {r['hf_revision']}" if r.get("hf_revision") else ""
        dpull = ""
        if d and d.get("repo"):
            drev = f" --revision {d['revision']}" if d.get("revision") else ""
            ncmt = f" (num_speculative_tokens={d['n']})" if d.get("n") else ""
            dpull = ("# 1b) pull the z-lab DFlash drafter — lossless spec-decode; speed only"
                     f"{ncmt}:\n#      hf download {d['repo']}{drev} --local-dir ./drafter\n")
        usage = ("#\n# 1) pull the exact weights this run benchmarked (sha256-verified upstream):\n"
                 f"#      hf download {r['hf_repo']}{rev} --local-dir ./weights\n"
                 f"{dpull}# 2) docker compose up\n")
    return _replicate_header(r) + "\n" + usage + f"""services:
  model:
    image: {image}
    entrypoint: vllm
    command:
{cmd}
    volumes:
{vols}
    ports:
      - "{port}:{port}"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    restart: unless-stopped
"""


@app.get("/api/runs/{run_id}/replicate")
def run_replicate(run_id: str, format: str = "script"):
    """Downloadable replication file built from the run's stored serve recipe (the SAME
    recipe behind the repro card): format=script (default) -> aeon-serve-<runid>.sh,
    format=compose -> aeon-serve-<runid>-compose.yml. Anything else is a 400; a missing
    run or one with no serve recipe is a 404."""
    # run_id feeds the Content-Disposition filename — same charset gate as the nonce
    if len(run_id) > 64 or any(ch not in _NONCE_OK for ch in run_id):
        return JSONResponse({"error": "run id must be <=64 chars of [A-Za-z0-9._-]"}, status_code=400)
    if format not in ("script", "compose"):
        return JSONResponse({"error": "format must be 'script' or 'compose'"}, status_code=400)
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    recipe = json.loads(r.get("recipe") or "null")
    if format == "compose":
        body, fname, media = _compose_yaml(r, recipe), f"aeon-serve-{run_id}-compose.yml", "text/yaml"
    else:
        body, fname, media = _replicate_script(r, recipe), f"aeon-serve-{run_id}.sh", "text/x-shellscript"
    if not body:
        return JSONResponse({"error": "run has no serve recipe to replicate"}, status_code=404)
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/admin/run/flag")
def admin_flag_run(body: AdminFlagBody, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    db.flag_run(body.run_id, body.flagged, body.reason)
    return {"ok": True, "run_id": body.run_id, "flagged": body.flagged}


@app.post("/api/admin/run/rejudge")
def admin_rejudge_run(body: AdminRunBody, request: Request):
    """Reset a run's Tier-1 cases to pending so they can be re-judged (agent/MCP verdict
    flow). The new verdicts record who judged + the rationale."""
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    n = db.reset_tier1_pending(body.run_id)
    return {"ok": True, "run_id": body.run_id, "reset_to_pending": n}


@app.post("/api/admin/audit/rescore")
def admin_audit_rescore(request: Request):
    """Re-run the CURRENT deterministic checkers over stored answers and CORRECT any checker-fix
    false-negatives (score-0 cases that now pass — e.g. after fixing a checker bug). Admin only.
    This is layer 1 of aeon.audit; layer 2 (frontier agent-judge over still-failing cases) runs
    from the CLI so the judge endpoint/key stay off the server."""
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from aeon import audit
    return audit.rescore_all(apply=True)


@app.get("/api/admin/audit/disputed")
def admin_audit_disputed(request: Request):
    """Cases the agent-judge flagged as LIKELY checker false-negatives — a standing to-review list.
    Deterministic scores are unchanged; each entry points a human at a checker to fix + re-score."""
    if not _require_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"disputed": db.disputed_cases()}


# ---- Attestation: verifiable build + signed submissions (DESIGN §8/§13 trust chain) ----

@app.get("/.well-known/aeon-bench.json")
def well_known():
    """Stable, cacheable trust anchor published at https://aeon-bench.com/.well-known/
    aeon-bench.json. A verifier pins this public_key once, then challenges
    /api/attestation?nonce=<random> and checks the signature + build_hash against it."""
    return {
        "name": "AEON Bench",
        "site": "https://aeon-bench.com",
        "version": "0.4",
        "alg": "ed25519",
        "public_key": attest.public_key_b64(),
        "build_hash": attest.build_hash(),
        "attestation_endpoint": "/api/attestation",
        "verify": "GET /api/attestation?nonce=<random>; verify ed25519 sig over the canonical body with this public_key",
    }


_NONCE_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@app.get("/api/attestation")
def get_attestation(nonce: str | None = None):
    """Signed (build_hash, public_key, ts, nonce). A verifier pins our public key,
    sends a fresh nonce, and confirms the live deployment runs the expected code."""
    # Bound the caller-controlled nonce before it reaches the signer: cap length and restrict
    # charset to [A-Za-z0-9._-] so an attacker can't have us sign an arbitrarily large / arbitrary
    # payload (attestation-nonce unbounded finding).
    if nonce is not None:
        nonce = nonce[:128]
        if any(ch not in _NONCE_OK for ch in nonce):
            return JSONResponse({"error": "nonce must be <=128 chars of [A-Za-z0-9._-]"}, status_code=400)
    return attest.attestation(nonce=nonce)


@app.get("/api/attestation/manifest")
def get_source_manifest():
    """The full per-file source manifest the build_hash is computed over (so a third
    party with the source can recompute and compare)."""
    return attest.source_manifest()


@app.get("/api/runs/{run_id}/manifest")
def run_manifest(run_id: str):
    """A SIGNED run manifest (signed submission): identity + suite hash + scores +
    ed25519 signature over the canonical body."""
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    cats = {}
    for x in r.get("results", []):
        if x.get("score") is not None:
            cats.setdefault(x["category"], []).append(x["score"])
    cat_scores = {c: round(100 * sum(v) / len(v), 1) for c, v in cats.items()}
    manifest = {
        "run_id": r["id"], "model": r["model"], "board": r.get("board", "text"),
        "suite_id": r.get("suite_id"), "suite_hash": r.get("suite_hash"),
        "judge_model": r.get("judge_model"), "judge_is_self": bool(r.get("judge_is_self")),
        "status": r.get("status"), "n_cases": r.get("n_cases"),
        "categories": cat_scores,
        "composite": round(sum(cat_scores.values()) / len(cat_scores), 1) if cat_scores else 0.0,
        "started_at": r.get("started_at"), "finished_at": r.get("finished_at"),
        "env": json.loads(r.get("env_json") or "{}"),
    }
    return attest.sign_manifest(manifest)


# ---- Pod submission channel (trust-chain P0): enroll -> open run -> signed sandboxed ingest ----

class EnrollBody(BaseModel):
    public_key: str
    challenge: str
    signature: str


class OpenRunBody(BaseModel):
    public_key: str
    signature: str
    model: str
    suite_id: str | None = None
    board: str = "text"


# Per-IP sliding-window throttle for the signed ingest POSTs: no auth precedes signature/HF
# verification, and under AEON_ATTESTED_ONLY every /results submit forces a blocking HF verify —
# so an unthrottled client can spin the verifier. Generous enough for a real pod (tens/min).
import time as _time

_V1_RATE_LIMIT = 40      # events per IP
_V1_RATE_WINDOW = 60     # seconds


def _v1_rate_ok(request: Request, bucket: str):
    """True if this IP is under the ingest limit (records the hit). client_ip is the trusted
    proxy IP (AEON_TRUST_PROXY=1)."""
    return db.rate_hit(f"v1:{bucket}:{accounts.client_ip(request)}",
                       _V1_RATE_LIMIT, _V1_RATE_WINDOW, _time.time())


@app.get("/api/v1/enroll/challenge")
def v1_enroll_challenge():
    if (g := _no_trust_stack()):   # signed-submission RECEIVER is mothership-only
        return g
    return {"challenge": ingest.issue_challenge(), "ttl": ingest._CHALLENGE_TTL}


@app.post("/api/v1/enroll")
def v1_enroll(body: EnrollBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    if not _v1_rate_ok(request, "enroll"):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    r, code = ingest.enroll(body.public_key, body.challenge, body.signature)
    return r if code == 200 else JSONResponse(r, status_code=code)


@app.post("/api/v1/runs")
def v1_open_run(body: OpenRunBody, request: Request):
    if (g := _no_trust_stack()):
        return g
    if not _v1_rate_ok(request, "runs"):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    r, code = ingest.open_run(body.public_key, body.signature, model=body.model,
                              suite_id=body.suite_id, board=body.board)
    return r if code == 200 else JSONResponse(r, status_code=code)


@app.post("/api/v1/runs/{run_id}/results")
async def v1_submit_results(run_id: str, request: Request):
    if (g := _no_trust_stack()):
        return g
    if not _v1_rate_ok(request, "results"):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    token = request.headers.get("x-aeon-run-token") or ""
    raw = await request.body()
    r, code = ingest.submit_results(run_id, token, raw)
    return r if code == 200 else JSONResponse(r, status_code=code)


@app.get("/api/harness_board")
def harness_board():
    """AI Harness Bench: model × harness matrix (which harness gets the most out of a model)."""
    return scoring.harness_board()


@app.get("/api/pod/info")
def pod_info():
    """The open pod repo + which deploy artifacts the mothership can hand out."""
    return {"repo": POD_REPO_URL,
            "downloads": [n for n, p in _POD_FILES.items() if os.path.exists(p)]}


# ---- POD-ONLY: launch a benchmark from the browser + per-lab secrets (NEVER on the mothership) ----
# The mothership deliberately has no run-launchers (runs originate only from pods). These routes 404
# off-pod via _require_pod(), and (optionally) require the lab token via _require_pod_token().

def _require_pod():
    """Hard pod-only gate: 404 on the mothership."""
    return None if IS_POD else JSONResponse(
        {"error": "not available on the mothership", "role": ROLE}, status_code=404)


@app.on_event("startup")
def _pod_boot_reconcile():
    """A pod boot proves no bench job is alive — heal what a mid-run kill leaves behind
    (orphaned aeon-bench-serve, paused-but-never-restored production containers, stranded
    local 'running' rows). No-op on the mothership."""
    if not IS_POD:
        return
    try:
        from pod import recover
        threading.Thread(target=recover.reconcile, daemon=True).start()   # container starts can
    except Exception:                                                     # take minutes — never
        pass                                                              # block serving the GUI


def _require_pod_token(request: Request):
    """Optional lab lock (AEON_POD_TOKEN). Unset = open (private single-operator pod)."""
    if not _POD_TOKEN:
        return None
    tok = request.headers.get("x-aeon-pod-token") or request.query_params.get("token")
    return None if tok == _POD_TOKEN else JSONResponse({"error": "pod token required"}, status_code=401)


class PodEndpointRunBody(BaseModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str
    difficulty: str | None = None       # None = full suite; "hard" / "hard,expert" = named tiers
    category: str | None = None         # None = all categories; comma-list scopes the text suite
    preset: str | None = None           # None | "comprehensive" | "hard-bench" (one-shot bundle)
    api_key_name: str | None = None     # name of a saved pod secret to send as the endpoint's api key
    engine: str | None = None
    perf_max_conc: int | None = None    # cap for the perf-grid concurrency ladder (clamped 1..64)
    concurrency: int | None = None      # cases in flight at once; None = auto (clamped 1..64)


class PodFrontierRunBody(BaseModel):
    frontier_id: str
    api_key_name: str
    difficulty: str | None = None
    category: str | None = None
    preset: str | None = None
    perf_max_conc: int | None = None
    concurrency: int | None = None
    max_tokens: int | None = None


class PodFrontierValidateBody(BaseModel):
    frontier_id: str
    api_key_name: str


class PodVerifiedRunBody(BaseModel):
    hf_link: str
    difficulty: str | None = None
    category: str | None = None         # None = all categories; comma-list scopes the text suite
    preset: str | None = None           # None | "comprehensive" | "hard-bench" (one-shot bundle)
    hf_token_name: str | None = None    # saved secret name for a gated/private repo token
    engine: str | None = None           # catalog engine id (pod.engines) — the Run-tab dropdown
    engine_image: str | None = None     # custom container image override (recorded with the run)
    local_dir: str | None = None        # model already on disk: hash-validate, don't re-download
    serve_url: str | None = None        # operator-started serve (macOS/MLX bare-metal path)
    serve_flags: list[str] | None = None  # recipe tuning: flag overrides merged into the serve cmd
    drafter_hf: str | None = None       # DFlash drafter HF card: validated like the model, -> /drafter
    port: int | None = None
    perf_max_conc: int | None = None    # cap for the perf-grid concurrency ladder (clamped 1..64)
    concurrency: int | None = None      # cases in flight at once; None = auto (clamped 1..64)
    max_tokens: int | None = None       # per-answer TOKEN BUDGET (generation cap incl. thinking);
                                        # None = pod default (32768). Clamped 256..131072
    pause_all: bool | None = None       # CLEAR HOST: stop every non-pod container before serving
    restore_paused: bool | None = None  # restart the paused containers after the bench (default yes)
    arena_per_kind: int | None = None   # arena sweep breadth (prompts per kind, 0 disables; None = default 6)
    serve_cmd: str | None = None        # FULL serve-command override (advanced): verbatim startup cmd
    temperature: float | None = None    # sampling temperature (0 = greedy/deterministic; None = pod default 0)


def _clamp_conc(v):
    """Server-side guard for browser-supplied concurrency knobs (perf ladder cap /
    run concurrency): int, clamped 1..64; anything else -> None (pod default)."""
    try:
        return max(1, min(64, int(v))) if v is not None else None
    except (TypeError, ValueError):
        return None


def _clean_serve_flags(flags):
    """Recipe-tuning overrides from the browser: a bounded list of printable tokens. They only
    ever land in the SERVE process argv (list-form exec, never a shell) and pod.engines.merge_flags
    drops the protected bench wiring — this guard just keeps the payload sane."""
    if not isinstance(flags, list):
        return None
    out = []
    for t in flags[:64]:
        t = str(t).strip()
        if t and len(t) <= 300 and t.isprintable():
            out.append(t)
    return out or None


class PodSecretBody(BaseModel):
    name: str
    value: str
    kind: str = "api_key"               # api_key | hf_token


class PodNameBody(BaseModel):
    name: str


@app.post("/api/pod/run/endpoint")
def pod_run_endpoint(body: PodEndpointRunBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    if not (body.model or "").strip() or not (body.base_url or "").strip():
        return JSONResponse({"error": "model and base_url are required"}, status_code=400)
    if body.preset and body.preset not in ("comprehensive", "hard-bench"):
        return JSONResponse({"error": "preset must be 'comprehensive' or 'hard-bench'"}, status_code=400)
    from pod import jobs
    jid = jobs.submit_endpoint(body.base_url.strip(), body.model.strip(),
        difficulty=(body.difficulty or None), category=(body.category or None),
        preset=(body.preset or None), api_key_name=(body.api_key_name or None),
        engine=(body.engine or None), perf_max_conc=_clamp_conc(body.perf_max_conc),
        concurrency=_clamp_conc(body.concurrency))
    return {"job_id": jid}


@app.get("/api/pod/frontier")
def pod_frontier_models(request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    return {"models": frontier.public_definitions()}


@app.post("/api/pod/frontier/validate")
def pod_frontier_validate(body: PodFrontierValidateBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    key = db.get_secret((body.api_key_name or "").strip())
    try:
        return frontier.validate_api((body.frontier_id or "").strip(), key)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:400]}, status_code=400)


@app.post("/api/pod/run/frontier")
def pod_run_frontier(body: PodFrontierRunBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    if body.preset and body.preset not in ("comprehensive", "hard-bench"):
        return JSONResponse({"error": "preset must be 'comprehensive' or 'hard-bench'"}, status_code=400)
    from pod import jobs
    try:
        jid = jobs.submit_frontier((body.frontier_id or "").strip(),
            api_key_name=(body.api_key_name or "").strip(),
            difficulty=(body.difficulty or None), category=(body.category or None),
            preset=(body.preset or None), perf_max_conc=_clamp_conc(body.perf_max_conc),
            concurrency=_clamp_conc(body.concurrency),
            max_tokens=(min(131072, max(256, int(body.max_tokens))) if body.max_tokens else None))
    except Exception as e:
        return JSONResponse({"error": str(e)[:400]}, status_code=400)
    return {"job_id": jid}


@app.post("/api/pod/run/verified")
def pod_run_verified(body: PodVerifiedRunBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    if not (body.hf_link or "").strip():
        return JSONResponse({"error": "hf_link is required"}, status_code=400)
    if body.preset and body.preset not in ("comprehensive", "hard-bench"):
        return JSONResponse({"error": "preset must be 'comprehensive' or 'hard-bench'"}, status_code=400)
    from pod import jobs
    jid = jobs.submit_verified(body.hf_link.strip(), difficulty=(body.difficulty or None),
        category=(body.category or None), preset=(body.preset or None),
        hf_token_name=(body.hf_token_name or None), engine=(body.engine or None), port=(body.port or None),
        engine_image=(body.engine_image or None), local_dir=(body.local_dir or None),
        serve_url=(body.serve_url or None), serve_flags=_clean_serve_flags(body.serve_flags),
        drafter_hf=(body.drafter_hf or "").strip() or None,
        perf_max_conc=_clamp_conc(body.perf_max_conc), concurrency=_clamp_conc(body.concurrency),
        max_tokens=(min(131072, max(256, int(body.max_tokens))) if body.max_tokens else None),
        pause_all=bool(body.pause_all), restore_paused=body.restore_paused,
        arena_per_kind=(min(12, max(0, int(body.arena_per_kind)))
                        if body.arena_per_kind is not None else None),
        serve_cmd=((body.serve_cmd or "").strip() or None),
        temperature=(min(2.0, max(0.0, float(body.temperature)))
                     if body.temperature is not None else None))
    return {"job_id": jid}


@app.get("/api/pod/engines")
def pod_engines(request: Request):
    """POD-ONLY: the curated inference-engine catalog, annotated for THIS host (platform,
    availability, the recommended default) — drives the Run tab's engine dropdown."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import engines
    return engines.catalog()


class PodValidateBody(BaseModel):
    hf_link: str
    local_path: str | None = None       # weights already on disk -> full sha256 vs the HF manifest
    hf_token_name: str | None = None    # saved secret for gated/private repos


@app.post("/api/pod/validate")
def pod_validate(body: PodValidateBody, request: Request):
    """POD-ONLY: start async model validation (resolve the HF repo; hash a local dir against its
    LFS manifest when given). The GUI polls GET /api/pod/validate/{id} for the green light."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    if not (body.hf_link or "").strip():
        return JSONResponse({"error": "hf_link is required"}, status_code=400)
    token = None
    if body.hf_token_name:
        token = db.get_secret(body.hf_token_name)
    from pod import validate as vmod
    return {"validate_id": vmod.start(body.hf_link, body.local_path, token)}


@app.get("/api/pod/validate/{vid}")
def pod_validate_status(vid: str, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import validate as vmod
    st = vmod.status(vid)
    return st if st else JSONResponse({"error": "unknown validation id"}, status_code=404)


@app.get("/api/pod/scan_models")
def pod_scan_models(request: Request):
    """POD-ONLY: sweep this host's model homes (AEON /models, ~/.aeon/models, the HF hub cache,
    LM Studio dirs, ~/models, AEON_SCAN_DIRS) — every model found with size, location, format and
    an auto-reconciled HF repo guess where the layout carries identity. Names + stat sizes only;
    no file contents are read."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import diskscan
    return diskscan.scan()


@app.get("/api/pod/browse")
def pod_browse(request: Request, path: str | None = None):
    """POD-ONLY: one directory level of the POD host's filesystem (dirs + weight files) for the
    local-weights browser — the dashboard may be remote/containerized, so browsing is server-side.
    Operator-trust surface, same gate as the launchers; listings only, never file contents."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import diskscan
    return diskscan.browse(path)


@app.get("/api/pod/jobs")
def pod_jobs(request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import jobs
    return {"jobs": jobs.list_jobs()}


@app.get("/api/pod/stats")
def pod_stats(request: Request):
    """POD-ONLY: live host telemetry (GPU VRAM/util, host RAM, CPU load, serve-container
    state + CPU/MEM) for the Live view's serve-watch strip — proof that a multi-minute
    model load is progressing, not stalled. On-demand samples, cached a few seconds."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import hoststats
    return hoststats.sample()


@app.get("/api/pod/launches")
def pod_launches(request: Request):
    """Prior launch configs as TEMPLATES for the Run form (knobs only; token NAMES, never
    values). Pod-only, same gate as the launchers."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from . import db
    return {"launches": db.list_launches()}


@app.get("/api/pod/launches/best")
def pod_best_launch(request: Request, model: str):
    """The prior launch config for `model` whose run scored HIGHEST — the 'apply
    best-performing template'. None until this model has a scored prior run on THIS pod."""
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from . import db
    return {"best": db.best_launch((model or "").strip())}


@app.get("/api/pod/jobs/{job_id}")
def pod_job(job_id: str, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import jobs
    j = jobs.get_job(job_id)
    if not j:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return j


@app.post("/api/pod/jobs/{job_id}/stop")
def pod_job_stop(job_id: str, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import jobs
    return {"ok": jobs.stop_job(job_id)}


@app.get("/api/pod/keys")
def pod_keys(request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    return {"keys": db.list_secrets()}          # masked previews only — never the values


@app.post("/api/pod/keys")
def pod_keys_set(body: PodSecretBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    name = (body.name or "").strip()
    if not name or not body.value:
        return JSONResponse({"error": "name and value are required"}, status_code=400)
    db.set_secret(name, body.value, kind=(body.kind or "api_key"))
    return {"ok": True, "name": name}


@app.post("/api/pod/keys/delete")
def pod_keys_delete(body: PodNameBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    db.delete_secret((body.name or "").strip())
    return {"ok": True}


# The single-segment deploy-artifact catch-all MUST be registered LAST, after every specific
# /api/pod/* route (jobs, keys, info, run/*), or it would swallow them (name="jobs"/"keys"/...).
@app.get("/api/pod/{name}")
def pod_artifact(name: str):
    """Serve a pod deploy artifact (docker-compose.yml / agents.md / .env.example / docs) so a
    user can spin up the benchmark pod. The mothership never runs the benchmark itself."""
    path = _POD_FILES.get(name)
    if not path or not os.path.exists(path):
        return JSONResponse({"error": "artifact not available yet", "name": name,
                             "repo": POD_REPO_URL}, status_code=404)
    media = "text/markdown" if name.endswith(".md") else \
            ("text/yaml" if name.endswith((".yml", ".yaml")) else "text/plain")
    return FileResponse(path, media_type=media, filename=name)


# static assets (index references /static/app.js, /static/styles.css)
app.mount("/static", StaticFiles(directory=WEB), name="static")
