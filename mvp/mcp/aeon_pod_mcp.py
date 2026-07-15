#!/usr/bin/env python3
"""AEON Bench **Pod** MCP server — lets an AI agent run a whole verified benchmark, headlessly.

The Pod is where benchmarks happen: it pulls a model, hash-verifies it against Hugging Face,
serves it, benchmarks quality + speed + agentic + multimodal, signs the results, and submits
them ATTESTED to the mothership (the public leaderboard at aeon-bench.com). The MOTHERSHIP is
read-only — it never starts a job — so this MCP talks ONLY to a local Pod.

A minimal, dependency-free MCP stdio server (newline-delimited JSON-RPC 2.0). Start your Pod
(`docker run … ghcr.io/aeon-7/aeon-pod:latest`, dashboard on :8091 — pull latest first), then
point your MCP client at this script:

  AEON_BASE=http://127.0.0.1:8091 python mvp/mcp/aeon_pod_mcp.py
  # or, from inside the pod image:  python -m mcp.aeon_pod_mcp
  # AEON_POD_TOKEN=<token>  if the pod's optional lab lock is set.

THE VERIFIED PATH (only this earns the ranked 'attested' tier):
  1. Point at a model ONE of three ways:
       (a) fresh HF pull   — pass hf_link "org/Model"; the pod downloads + hash-verifies it.
       (b) local on disk    — pass hf_link "org/Model" AND local_dir; the pod sha256-checks the
                              on-disk bytes against that HF repo's LFS manifest (no re-download).
       (c) discover local   — aeon_pod_scan_models first, then use (b) with the path it returns.
  2. Pick a recipe — prefer aeon_pod_champion_recipes for the detected hardware; else the pod
     auto-applies a family preset. Tune serve_flags if you must.
  3. aeon_pod_run with preset "comprehensive" (the WHOLE exam). Never submit a smoke test as
     validated — only a complete comprehensive run ranks.
  4. Poll aeon_pod_jobs / aeon_pod_stats; aeon_pod_resume if interrupted.
  5. Completed runs auto-submit; if the mothership was down, aeon_pod_submit pushes later
     (idempotent — a finished job can't land twice).

Tools:
  aeon_pod_info             — pod role, detected hardware, whether a pod token is required
  aeon_pod_scan_models      — models already on disk (each hash-verifiable against its HF repo)
  aeon_pod_engines          — inference-engine catalog for THIS host + the recommended default
  aeon_pod_champion_recipes — best proven recipes for the detected hardware (one-click templates)
  aeon_pod_validate         — start validating an HF link (+ optional local dir hash-check)
  aeon_pod_validate_status  — poll a validation (green light = ready to run attested)
  aeon_pod_run              — launch a VALIDATED comprehensive benchmark (the main event)
  aeon_pod_jobs             — every job's status + per-stage progress + pending submissions
  aeon_pod_job              — one job's full detail
  aeon_pod_stats            — live host + engine telemetry (aggregate tok/s, active/queued streams)
  aeon_pod_resume           — resume an interrupted job from its last scored case
  aeon_pod_submit           — submit a completed job to the mothership (idempotent)
  aeon_pod_leaderboard      — the current board (as this pod sees it)
  aeon_pod_suite            — the deterministic suite summary (id, hash, categories, #cases)
  aeon_pod_guide            — the verified-path playbook (source a model, comprehensive rule, trust)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AEON_BASE", "http://127.0.0.1:8091").rstrip("/")  # the local POD, not the mothership
POD_TOKEN = os.environ.get("AEON_POD_TOKEN") or None
PROTOCOL_VERSION = "2024-11-05"


def _http(method, path, body=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if POD_TOKEN:
        headers["x-aeon-pod-token"] = POD_TOKEN       # optional pod lab lock (AEON_POD_TOKEN)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        hint = ""
        if e.code == 404 and "/api/pod/" in path:
            hint = " (is AEON_BASE pointed at a running POD dashboard on :8091, role=pod?)"
        elif e.code == 401:
            hint = " (this pod has a lab lock — set AEON_POD_TOKEN)"
        return {"error": f"HTTP {e.code}{hint}", "detail": detail}
    except Exception as e:
        return {"error": str(e), "hint": f"could not reach the pod at {BASE} — start it and pull latest first"}


def _guide():
    """The verified-path playbook — prefer the repo's SKILL.md / AGENTS.md when present."""
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../SKILL.md", "../../AGENTS.md"):
        p = os.path.normpath(os.path.join(here, rel))
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return {"source": os.path.basename(p), "guide": f.read()}
    return {"guide": (
        "VERIFIED PATH: (1) point at a model — fresh HF pull (hf_link), or on-disk weights "
        "hash-verified against the same HF repo (hf_link + local_dir), or discover one with "
        "aeon_pod_scan_models. (2) Apply a champion recipe for the detected hardware. (3) Run "
        "with preset 'comprehensive' — the whole exam; only a complete run ranks. (4) It "
        "auto-submits attested; if the mothership was down, aeon_pod_submit later. Never submit "
        "a smoke test as validated. A raw endpoint run is self_reported and never ranks.")}


# ---- tool registry: (name, description, inputSchema, handler) ----

def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


TOOLS = [
    {"name": "aeon_pod_info",
     "description": "Confirm you're talking to a live pod: returns role (must be 'pod'), whether a pod token is required, the live flag, and the suite id. (Detected hardware comes from aeon_pod_champion_recipes / aeon_pod_stats.)",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/config")},

    {"name": "aeon_pod_scan_models",
     "description": "List models already on this host (HF cache, LM Studio, ~/models, /models). Each carries a size, location, and a reconciled HF repo id — pass that repo as hf_link and the path as local_dir to aeon_pod_run for a hash-verified attested run with no re-download.",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/pod/scan_models")},

    {"name": "aeon_pod_engines",
     "description": "The inference-engine catalog annotated for THIS host: which engines are available, the recommended default, and the tunable serve flags (with pros/cons/conflicts). Usually leave the recommended engine.",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/pod/engines")},

    {"name": "aeon_pod_champion_recipes",
     "description": "The best proven recipes for this pod's detected hardware, pulled from the mothership leaderboard — one-click starting templates (engine, serve_flags, spec-decode, peak tok/s, quality). Prefer the top champion; tweak from there.",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/pod/recipes/champions")},

    {"name": "aeon_pod_validate",
     "description": "Start validating a model before you run it: resolves the HF repo, and (if local_path is given) sha256-checks the on-disk weights against that repo's LFS manifest. Returns a validate_id; poll aeon_pod_validate_status. Optional — aeon_pod_run validates internally too — but a green light here confirms the attested path and surfaces the model's modalities.",
     "schema": _obj({"hf_link": {"type": "string", "description": "org/Model or a huggingface.co URL"},
                     "local_path": {"type": "string", "description": "optional: on-disk weights dir to hash-verify against the repo"},
                     "hf_token_name": {"type": "string", "description": "optional: saved pod secret name for a gated/private repo"}},
                    ["hf_link"]),
     "fn": lambda a: _http("POST", "/api/pod/validate", body={"hf_link": a["hf_link"],
                           "local_path": a.get("local_path"), "hf_token_name": a.get("hf_token_name")})},

    {"name": "aeon_pod_validate_status",
     "description": "Poll a validation started by aeon_pod_validate. A resolved/validated status with matching hashes means the model is ready for an attested run.",
     "schema": _obj({"validate_id": {"type": "string"}}, ["validate_id"]),
     "fn": lambda a: _http("GET", f"/api/pod/validate/{urllib.parse.quote(a['validate_id'])}")},

    {"name": "aeon_pod_run",
     "description": "Launch a VALIDATED benchmark — the main event. Provide an hf_link (fresh pull + hash-verify) OR hf_link + local_dir (on-disk bytes hash-verified against that HF repo). Defaults to preset 'comprehensive' = the WHOLE exam (text + 3 agentic harnesses + vision/audio/video + arena + perf); only a complete comprehensive run ranks on the global board. Returns {job_id}; then poll aeon_pod_jobs. Do NOT use this for a raw endpoint (that is self_reported and never ranks). Big/slow models take HOURS — tell your human.",
     "schema": _obj({
        "hf_link": {"type": "string", "description": "org/Model or HF URL — REQUIRED (the identity verified against HF)"},
        "local_dir": {"type": "string", "description": "optional: on-disk weights to hash-verify vs the hf_link repo (no re-download)"},
        "preset": {"type": "string", "enum": ["comprehensive", "hard-bench"], "default": "comprehensive",
                   "description": "comprehensive = the full validated exam (default, use this to rank); hard-bench = hard/expert tiers only"},
        "engine": {"type": "string", "description": "optional: engine id from aeon_pod_engines (else the recommended default)"},
        "serve_flags": {"type": "array", "items": {"type": "string"},
                        "description": "optional recipe flags, e.g. from a champion recipe: [\"--gpu-memory-utilization\",\"0.7\"]"},
        "drafter_hf": {"type": "string", "description": "optional DFlash drafter HF card (spec decode) — hash-verified, mounted at /drafter"},
        "modalities": {"type": "array", "items": {"type": "string", "enum": ["vision", "audio", "video"]},
                       "description": "optional override; omit to auto-detect from the model config (probe-gated)"},
        "concurrency": {"type": "integer", "description": "optional cases-in-flight; omit for auto (capacity-aware)"},
        "max_tokens": {"type": "integer", "description": "optional per-answer token budget; omit for the pod default (32768)"},
        "hf_token_name": {"type": "string", "description": "optional saved pod secret name for a gated/private repo"},
        "hardware": {"type": "string", "description": "optional hardware label override for the record"},
     }, ["hf_link"]),
     "fn": lambda a: _http("POST", "/api/pod/run/verified", body={
        "hf_link": a["hf_link"], "local_dir": a.get("local_dir"),
        "preset": a.get("preset") or "comprehensive", "engine": a.get("engine"),
        "serve_flags": a.get("serve_flags"), "drafter_hf": a.get("drafter_hf"),
        "modalities": a.get("modalities"), "concurrency": a.get("concurrency"),
        "max_tokens": a.get("max_tokens"), "hf_token_name": a.get("hf_token_name")})},

    {"name": "aeon_pod_jobs",
     "description": "Every benchmark job on this pod: status, per-dimension stage progress (text/harness/vision/audio/video/perf), plus 'pending' = completed-but-unsubmitted sessions (press aeon_pod_submit for those). Poll this to track a run.",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/pod/jobs")},

    {"name": "aeon_pod_job",
     "description": "Full detail for one job id (stages, serve phase, error/hint, submit state).",
     "schema": _obj({"job_id": {"type": "string"}}, ["job_id"]),
     "fn": lambda a: _http("GET", f"/api/pod/jobs/{urllib.parse.quote(a['job_id'])}")},

    {"name": "aeon_pod_stats",
     "description": "Live host + engine telemetry while a bench runs: aggregate tokens/sec across all streams, active + queued request counts, GPU/RAM, serve-container state. Proof a long model load is progressing, not hung.",
     "schema": _obj({}),
     "fn": lambda a: _http("GET", "/api/pod/stats")},

    {"name": "aeon_pod_resume",
     "description": "Resume an interrupted job (stopped or died mid-bench) from its last scored case — reuses the same job signature and never re-runs finished work.",
     "schema": _obj({"job_id": {"type": "string"}}, ["job_id"]),
     "fn": lambda a: _http("POST", f"/api/pod/jobs/{urllib.parse.quote(a['job_id'])}/resume")},

    {"name": "aeon_pod_submit",
     "description": "Submit a COMPLETED job's results to the mothership (use when a run finished but the earlier auto-submit failed, e.g. the site was down). Idempotent: if it already landed you get 'job already submitted and available on the Mothership'. Only complete runs submit as validated.",
     "schema": _obj({"job_id": {"type": "string"}}, ["job_id"]),
     "fn": lambda a: _http("POST", f"/api/pod/jobs/{urllib.parse.quote(a['job_id'])}/submit")},

    {"name": "aeon_pod_leaderboard",
     "description": "The current leaderboard as this pod sees it (mirrors the public aeon-bench.com board).",
     "schema": _obj({"board": {"type": "string", "enum": ["text", "vision", "video"], "default": "text"}}),
     "fn": lambda a: _http("GET", {"vision": "/api/vision/leaderboard", "video": "/api/video/leaderboard"}
                           .get(a.get("board"), "/api/leaderboard"))},

    {"name": "aeon_pod_suite",
     "description": "The deterministic suite summary (id, content hash, categories, #cases).",
     "schema": _obj({"board": {"type": "string", "enum": ["text", "vision", "video"], "default": "text"}}),
     "fn": lambda a: _http("GET", {"vision": "/api/vision/suite", "video": "/api/video/suite"}
                           .get(a.get("board"), "/api/suite"))},

    {"name": "aeon_pod_guide",
     "description": "The verified-path playbook: how to source a model (fresh HF pull / on-disk hash-verified / discover local), the comprehensive-only rule, and why only attested runs rank. Read this before your first run.",
     "schema": _obj({}),
     "fn": lambda a: _guide()},
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def _result(data):
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, default=str)}]}


def handle(req):
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "aeon-bench-pod", "version": "1.0"}}}
    if m in ("notifications/initialized", "initialized"):
        return None  # notification, no reply
    if m == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": t["name"], "description": t["description"], "inputSchema": t["schema"]} for t in TOOLS]}}
    if m == "tools/call":
        p = req.get("params") or {}
        t = _BY_NAME.get(p.get("name"))
        if not t:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown tool"}}
        try:
            return {"jsonrpc": "2.0", "id": rid, "result": _result(t["fn"](p.get("arguments") or {}))}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {"isError": True,
                    "content": [{"type": "text", "text": f"tool error: {e}"}]}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method {m} not found"}}
    return None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
