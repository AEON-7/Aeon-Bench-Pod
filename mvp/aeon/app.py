"""FastAPI mothership (MVP): serves the dashboard + the orchestration/ingest API.

Runs are executed in a background thread (in-process probe) — faithful to the
"local run" mode in DESIGN §3, minus the container orchestration.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import accounts, admin, arena, attest, db, evaluators, ingest, modelmeta, probe, runner, scoring, vram
from . import suite as suite_mod
from . import vision_suite
from .targets import OpenAITarget, list_models

WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
POD_REPO_URL = os.environ.get("AEON_POD_REPO", "https://github.com/AEON-7/aeon-pod")
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


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB, "index.html"))


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
    return {"target": target, "models": list_models(target, api_key=api_key or DEFAULT_KEY)}


@app.get("/api/leaderboard")
def leaderboard(suite: str | None = None):
    """Default = the comprehensive suite. `?suite=aeon-suite-v2-hard` shows a tier board on its own
    (hard runs are a different test and must not average into the comprehensive standing)."""
    return scoring.leaderboard(suite=suite)


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


@app.get("/api/live")
def live(board: str = "text"):
    """In-progress (running) runs with their PARTIAL results — the live benchmark view. Fed by the
    pod's incremental checkpoints: per-category progress + a feed of the most recent prompts/answers
    as each case is scored. No polling cost beyond a normal read; safe to call every few seconds."""
    if not IS_POD:
        return {"running": [], "role": ROLE}   # LIVE is a POD view; the mothership shows only accepted runs
    running = [r for r in db.list_runs(200)
               if r.get("status") == "running" and (r.get("board") or "text") == board]
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


def _token_of(request: Request):
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-aeon-token")


# ---- evaluator accounts (anonymous: username + password, capped per IP) ----

@app.post("/api/auth/signup")
def auth_signup(body: AuthBody, request: Request):
    r = accounts.signup(body.username, body.password, accounts.client_ip(request))
    if "error" in r:
        return JSONResponse(r, status_code=429 if "too many" in r["error"] else 400)
    return r


@app.post("/api/auth/login")
def auth_login(body: AuthBody, request: Request):
    r = accounts.login(body.username, body.password, accounts.client_ip(request))
    if "error" in r:
        code = 429 if ("too many" in r["error"] or "locked" in r["error"]) else 401
        return JSONResponse(r, status_code=code)
    return r


@app.get("/api/auth/me")
def auth_me(request: Request):
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


@app.get("/api/arena/prompts")
def arena_prompts():
    return {"kinds": arena.KINDS, "labels": arena.KIND_LABEL, "prompts": arena.all_prompts()}


@app.get("/api/arena/artifacts")
def arena_artifacts(kind: str | None = None, prompt_id: str | None = None):
    return {"artifacts": db.list_artifacts(kind=kind, prompt_id=prompt_id)}


@app.get("/api/arena/render")
def arena_render(request: Request, match_id: str, side: str):
    """Render one side of a match for ITS OWNER only, returning ONLY the html — never
    prompt_id/model/bogus metadata. This is the sole way the client gets artifact
    bodies, so a honeypot decoy cannot be identified before voting and the model
    identity stays hidden until the vote response reveals it."""
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


# (arena artifact generation removed — the mothership does not produce content; arena
#  artifacts are submitted by pods. The mothership only serves matches + records votes.)


@app.get("/api/arena/match")
def arena_match(request: Request, kind: str, prompt_id: str | None = None):
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
    return {c["id"]: c.get("prompt", "") for c in cases}


@app.get("/api/submissions")
def submissions(board: str | None = None, model: str | None = None, limit: int = 300):
    rows = db.list_submissions(board=board, model=model, limit=limit)
    if not IS_POD:                        # mothership shows only ACCEPTED (succeeded) runs, never in-progress
        rows = [r for r in rows if r.get("status") == "succeeded"]
    means = db.run_mean_scores()
    for r in rows:
        m = means.get(r["id"])
        r["mean_score"] = round(100 * m, 1) if m is not None else None
    return {"submissions": rows}


@app.get("/api/submissions/{run_id}")
def submission_detail(run_id: str):
    """Full transparency for one run: per case — what was ASKED, how it was ANSWERED,
    the score, and HOW + BY WHAT it was judged (with rationale)."""
    r = db.get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    board = r.get("board", "text")
    pm = _prompt_map(board)
    cases = []
    for x in r.get("results", []):
        ev = x.get("evidence") or {}
        if x["tier"] == 0:
            judged_by = "program (deterministic)"
        elif ev.get("judged_by") == "agent":
            judged_by = "agent (launcher)"
        else:
            judged_by = r.get("judge_model") or "self-judge"
        cases.append({
            "case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
            "status": x["status"], "score": x["score"], "creativity": x.get("creativity"),
            "prompt": pm.get(x["case_id"], ""), "answer": db.result_output(x),
            "judged_by": judged_by, "evidence": ev, "speed": x.get("speed"),
            "disputed": bool(x.get("disputed")), "disputed_reason": x.get("disputed_reason"),
        })
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
        "flags": (recipe or {}).get("flags") if recipe else None,
        "spec_decode": (recipe or {}).get("spec_decode") if recipe else None,
        "weights_hash": r.get("weights_hash"),
        "hf_repo": r.get("hf_repo"), "hf_revision": r.get("hf_revision"),
        "build_hash": dm.get("build_hash"),
        # hardware AS DETECTED on the bench machine (single/dual DGX Spark, RTX 5090, Strix Halo, …)
        "hardware_detected": hw.get("detected_label"),
        "hardware_claimed": hw.get("label"),
        "gpus": hw.get("gpus"), "platform": hw.get("platform"), "machine": hw.get("machine"),
    }


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


@app.get("/api/attestation")
def get_attestation(nonce: str | None = None):
    """Signed (build_hash, public_key, ts, nonce). A verifier pins our public key,
    sends a fresh nonce, and confirms the live deployment runs the expected code."""
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


@app.get("/api/v1/enroll/challenge")
def v1_enroll_challenge():
    return {"challenge": ingest.issue_challenge(), "ttl": ingest._CHALLENGE_TTL}


@app.post("/api/v1/enroll")
def v1_enroll(body: EnrollBody):
    r, code = ingest.enroll(body.public_key, body.challenge, body.signature)
    return r if code == 200 else JSONResponse(r, status_code=code)


@app.post("/api/v1/runs")
def v1_open_run(body: OpenRunBody):
    r, code = ingest.open_run(body.public_key, body.signature, model=body.model,
                              suite_id=body.suite_id, board=body.board)
    return r if code == 200 else JSONResponse(r, status_code=code)


@app.post("/api/v1/runs/{run_id}/results")
async def v1_submit_results(run_id: str, request: Request):
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
    api_key_name: str | None = None     # name of a saved pod secret to send as the endpoint's api key
    engine: str | None = None


class PodVerifiedRunBody(BaseModel):
    hf_link: str
    difficulty: str | None = None
    hf_token_name: str | None = None    # saved secret name for a gated/private repo token
    engine: str | None = None
    port: int | None = None


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
    from pod import jobs
    jid = jobs.submit_endpoint(body.base_url.strip(), body.model.strip(),
        difficulty=(body.difficulty or None), api_key_name=(body.api_key_name or None),
        engine=(body.engine or None))
    return {"job_id": jid}


@app.post("/api/pod/run/verified")
def pod_run_verified(body: PodVerifiedRunBody, request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    if not (body.hf_link or "").strip():
        return JSONResponse({"error": "hf_link is required"}, status_code=400)
    from pod import jobs
    jid = jobs.submit_verified(body.hf_link.strip(), difficulty=(body.difficulty or None),
        hf_token_name=(body.hf_token_name or None), engine=(body.engine or None), port=(body.port or None))
    return {"job_id": jid}


@app.get("/api/pod/jobs")
def pod_jobs(request: Request):
    if (g := _require_pod()):
        return g
    if (g := _require_pod_token(request)):
        return g
    from pod import jobs
    return {"jobs": jobs.list_jobs()}


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
