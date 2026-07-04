#!/usr/bin/env python3
"""AEON Bench MCP server — lets a frontier model drive the benchmark.

A minimal, dependency-free MCP stdio server (newline-delimited JSON-RPC 2.0). Point
your MCP client at it; it forwards to a running AEON Bench over HTTP.

  AEON_BASE=http://127.0.0.1:8089 python mvp/mcp/aeon_mcp.py

Tools (what an agent can do):
  aeon_suite               — the deterministic suite (categories, #cases, hash)
  aeon_list_models         — models available at a target endpoint
  aeon_launch_run          — run a model on the text or vision board (you pick the judge)
  aeon_get_run             — run status + per-case results (scores, outputs, evidence)
  aeon_leaderboard         — current board
  aeon_pending_judgements  — Tier-1 cases awaiting YOUR verdict (agent-as-judge runs)
  aeon_submit_verdict      — submit binary rubric verdicts (+ optional creativity) for a case
  aeon_attestation         — signed (build_hash, public_key, ts, nonce) integrity proof
  aeon_run_manifest        — a signed, publishable run manifest
  aeon_judge_guide         — how to judge correctly (determinism + creativity overlay)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("AEON_BASE", "http://127.0.0.1:8089").rstrip("/")
PROTOCOL_VERSION = "2024-11-05"


def _http(method, path, body=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"error": str(e)}


def _judge_guide():
    here = os.path.dirname(os.path.abspath(__file__))
    skill = os.path.normpath(os.path.join(here, "..", "..", "SKILL.md"))
    if os.path.exists(skill):
        with open(skill, "r", encoding="utf-8") as f:
            return {"skill": "run-aeon-benchmark", "guide": f.read()}
    return {"guide": "Judge Tier-1 by answering each rubric criterion as a binary, "
                     "evidence-grounded yes/no; never rate 1-10. Correctness is the gate. "
                     "Award creativity (0-3) only when the answer is CORRECT and reaches it "
                     "in a novel/unexpected/elegant way the reference did not."}


# ---- tool registry: (name, description, inputSchema, handler) ----

def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or []}


TOOLS = [
    {"name": "aeon_suite", "description": "The deterministic suite summary (id, hash, categories, #cases).",
     "schema": _obj({"board": {"type": "string", "enum": ["text", "vision"], "default": "text"}}),
     "fn": lambda a: _http("GET", "/api/vision/suite" if a.get("board") == "vision" else "/api/suite")},

    {"name": "aeon_list_models", "description": "List models available at an OpenAI-compatible target endpoint.",
     "schema": _obj({"target": {"type": "string", "description": "e.g. http://127.0.0.1:11434/v1"},
                     "api_key": {"type": "string"}}, ["target"]),
     "fn": lambda a: _http("GET", "/api/models", params={"target": a["target"], "api_key": a.get("api_key")})},

    # aeon_launch_run REMOVED — the mothership never starts runs (verifier != producer);
    # benchmarks originate only from pods. The MCP is a READ / VERIFY interface.

    {"name": "aeon_get_run", "description": "Run status + every case result (score, raw_output, evidence, speed).",
     "schema": _obj({"run_id": {"type": "string"}}, ["run_id"]),
     "fn": lambda a: _http("GET", f"/api/runs/{a['run_id']}")},

    {"name": "aeon_leaderboard", "description": "Current leaderboard for a board.",
     "schema": _obj({"board": {"type": "string", "enum": ["text", "vision"], "default": "text"}}),
     "fn": lambda a: _http("GET", "/api/vision/leaderboard" if a.get("board") == "vision" else "/api/leaderboard")},

    # aeon_pending_judgements + aeon_submit_verdict REMOVED — judging is frontier-model-or-
    # deterministic at the POD, never via the mothership (no self-judge, no agent-as-judge here).

    {"name": "aeon_attestation", "description": "Signed integrity proof: (build_hash, public_key, ts, your nonce). "
                                               "Pin the public key, send a fresh nonce, verify the signature.",
     "schema": _obj({"nonce": {"type": "string"}}),
     "fn": lambda a: _http("GET", "/api/attestation", params={"nonce": a.get("nonce")})},

    {"name": "aeon_run_manifest", "description": "A signed, publishable run manifest (scores + ed25519 signature).",
     "schema": _obj({"run_id": {"type": "string"}}, ["run_id"]),
     "fn": lambda a: _http("GET", f"/api/runs/{a['run_id']}/manifest")},

    {"name": "aeon_judge_guide", "description": "How to judge AEON correctly (determinism contract + creativity overlay).",
     "schema": _obj({}), "fn": lambda a: _judge_guide()},
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
            "serverInfo": {"name": "aeon-bench", "version": "0.4"}}}
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
