"""pod/endpoints.py — discover running OpenAI-compatible inference servers on this host (and
optionally on declared cluster/LAN hosts), so an operator can bench a LIVE serve IN PLACE:

    scan → pick an endpoint → it shows the model it serves → provide the HF link → the pod
    hash-verifies those weights + logprob-fingerprints the endpoint → attested (if it matches).

POD-ONLY. Probes GET /v1/models on common inference ports (vLLM/SGLang 8000, TGI 8080, Ollama
11434, LM Studio 1234, …). Names + ports only; no weights are pulled here. The mothership never
serves this route (pod-gated), so there is no SSRF surface on the public site — it is the
operator scanning their own machine/LAN."""
from __future__ import annotations

import concurrent.futures as cf
import json
import urllib.request

# common OpenAI-compatible serve ports: vLLM/SGLang, ASR/TTS sidecars, TGI, Ollama, LM Studio,
# SGLang router, llama.cpp server. The pod's OWN prod serves (8000/8001/8002) are included.
COMMON_PORTS = [8000, 8001, 8002, 8080, 1234, 11434, 30000, 8010, 5000, 8081]
MAX_HOSTS = 8


def _probe(base_url, *, timeout=2, transport=None):
    """GET <base>/v1/models. Returns {url, models, reachable} when an OpenAI-compatible server
    answers with at least one model id, else None. `transport(url)->parsed-json` is injectable
    for tests. Never raises — an unreachable port is just None."""
    url = base_url.rstrip("/") + "/v1/models"
    try:
        if transport is not None:
            d = transport(url)
        else:                                            # pragma: no cover — real network
            req = urllib.request.Request(url, headers={"User-Agent": "aeon-pod/scan"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
        models = [m.get("id") for m in (d.get("data") or []) if isinstance(m, dict) and m.get("id")]
        if models:
            return {"url": base_url.rstrip("/") + "/v1", "host": base_url.split("//", 1)[-1],
                    "models": models, "reachable": True}
    except Exception:
        return None
    return None


def scan(hosts=None, ports=None, *, transport=None, timeout=2):
    """Sweep (hosts × ports) for OpenAI-compatible servers, concurrently. `hosts` defaults to
    localhost; pass a declared LAN/cluster list (capped at MAX_HOSTS) to find remote serves —
    e.g. a multi-node cluster head. Returns {endpoints:[…], scanned}. Deduped by URL, so two
    ports fronting the same server list once."""
    hosts = [h for h in (hosts or ["127.0.0.1"]) if h][:MAX_HOSTS]
    ports = ports or COMMON_PORTS
    seen_p = []
    for p in ports:                                      # keep first occurrence order, dedup
        if p not in seen_p:
            seen_p.append(p)
    targets = [f"http://{h}:{p}" for h in hosts for p in seen_p]
    out, seen = [], set()
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(lambda t: _probe(t, timeout=timeout, transport=transport), targets):
            if r and r["url"] not in seen:
                seen.add(r["url"])
                out.append(r)
    return {"endpoints": out, "scanned": len(targets), "hosts": hosts}
