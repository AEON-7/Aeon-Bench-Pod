"""pod/endpoints.py — discover running OpenAI-compatible inference servers on this host (and
optionally on declared cluster/LAN hosts), so an operator can bench a LIVE serve IN PLACE:

    scan → pick an endpoint → it shows the model it serves → the pod AUTODETECTS the HF repo →
    (fill/confirm the HF link) → the pod hash-verifies those weights + logprob-fingerprints the
    endpoint → attested (if it matches).

POD-ONLY. Probes GET /v1/models on common inference ports (vLLM/SGLang 8000, TGI 8080, Ollama
11434, LM Studio 1234, …). Names + ports only; no weights are pulled here. The mothership never
serves this route (pod-gated), so there is no SSRF surface on the public site — it is the
operator scanning their own machine/LAN.

HF AUTODETECT. The single best endpoint-only signal is `/v1/models[].root` — vLLM/SGLang set it
to the launch `--model` value, which is the exact `org/model` repo whenever the serve was started
from a Hub id (e.g. `vllm serve Qwen/Qwen3-ASR-0.6B`). When the serve was started from a LOCAL
path (`vllm serve /model`), `root` is just that path and reveals nothing — so, for LOCALHOST
serves, a best-effort docker fallback maps the backing container's `--model` mount back to a repo
via the on-disk breadcrumbs (diskscan.reconcile_path). Every guess is only a PREFILL for the HF
link field: the launch still pulls+hashes those weights and fingerprints the endpoint against
them, so a wrong guess fails verification loudly — it can never weaken attestation."""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.request

from pod import diskscan

# common OpenAI-compatible serve ports: vLLM/SGLang, ASR/TTS sidecars, TGI, Ollama, LM Studio,
# SGLang router, llama.cpp server. The pod's OWN prod serves (8000/8001/8002) are included.
COMMON_PORTS = [8000, 8001, 8002, 8080, 1234, 11434, 30000, 8010, 5000, 8081]
MAX_HOSTS = 8
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
# a value that is a filesystem path, not an org/model repo id (leading / ./ ../ or a Windows drive)
_PATHY = re.compile(r"^(/|\./|\.\./|[A-Za-z]:[\\/])")


def _guess_from_served(mid, root):
    """Autodetect the HF repo from a served model's endpoint metadata alone. `root` (the launch
    --model value, when the server exposes it) is the strongest signal; the served id is a
    fallback. Either counts only when it's an org/model repo id, not a bare alias or a local path.
    Returns (hf_guess, source, confidence) or (None, None, None)."""
    for val, src in ((root, "served-root"), (mid, "served-id")):
        if isinstance(val, str) and val and not _PATHY.match(val) and diskscan._ORG_NAME_RE.match(val):
            return val, src, "high"
    return None, None, None


def _probe(base_url, *, timeout=2, transport=None):
    """GET <base>/v1/models. Returns {url, host, models, served, reachable} when an
    OpenAI-compatible server answers with at least one model id, else None. `models` is the flat
    id list (back-compat); `served` folds aliases of one physical model into a single entry and
    carries the per-model HF autodetect ({ids, root, hf_guess, source, confidence, max_model_len}).
    `transport(url)->parsed-json` is injectable for tests. Never raises — an unreachable port is
    just None."""
    url = base_url.rstrip("/") + "/v1/models"
    try:
        if transport is not None:
            d = transport(url)
        else:                                            # pragma: no cover — real network
            req = urllib.request.Request(url, headers={"User-Agent": "aeon-pod/scan"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.load(r)
        models, served, by_key = [], [], {}
        for m in (d.get("data") or []):
            if not (isinstance(m, dict) and m.get("id")):
                continue
            mid = m["id"]
            models.append(mid)
            root = m.get("root") if isinstance(m.get("root"), str) else None
            # one physical model is often exposed under many served-model-name aliases (all share
            # the same `root`) — fold those into a single served entry, don't over-count
            key = root or mid
            if key in by_key:
                by_key[key]["ids"].append(mid)
                continue
            hf_guess, gsrc, conf = _guess_from_served(mid, root)
            entry = {"ids": [mid], "root": root, "hf_guess": hf_guess, "hf_revision": None,
                     "source": gsrc, "confidence": conf, "max_model_len": m.get("max_model_len")}
            by_key[key] = entry
            served.append(entry)
        if models:
            return {"url": base_url.rstrip("/") + "/v1", "host": base_url.split("//", 1)[-1],
                    "models": models, "served": served, "reachable": True}
    except Exception:
        return None
    return None


# ---- docker fallback: resolve a LOCAL-path serve's HF repo from its backing container ----------
# When /v1/models `root` is a local path (the serve was launched from disk, not a Hub id), the
# endpoint alone can't name the repo. On the pod's OWN host we can: inspect the running containers,
# match one to the endpoint by its --served-model-name, read its --model, and reconcile that path
# (translated back into the pod's mount namespace) to an HF repo via the on-disk breadcrumbs.

def _default_docker(argv):                               # pragma: no cover — real docker
    return subprocess.run(argv, capture_output=True, text=True, timeout=8).stdout


def _flatten_cmd(parts):
    """Flatten a container's Entrypoint+Cmd into a token list, expanding shell wrappers. A serve
    launched as ["/bin/bash","-lc","exec vllm serve /model --served-model-name a b --port 8000"]
    hides its flags inside one shell string — shlex-split any multi-word element so --model /
    --served-model-name become findable tokens."""
    toks = []
    for p in parts:
        if not isinstance(p, str):
            continue
        if " " in p:
            try:
                toks += shlex.split(p)
            except Exception:
                toks.append(p)
        else:
            toks.append(p)
    return toks


def _flag_value(toks, flag):
    """The single token after `flag` (or None)."""
    for i, t in enumerate(toks):
        if t == flag and i + 1 < len(toks):
            return toks[i + 1]
    return None


def _flag_values(toks, flag):
    """All consecutive non-flag tokens after `flag` (for nargs flags like --served-model-name)."""
    out = []
    for i, t in enumerate(toks):
        if t == flag:
            for nxt in toks[i + 1:]:
                if nxt.startswith("-"):
                    break
                out.append(nxt)
            break
    return out


def _serve_positional(toks):
    """`vllm serve <model>` / `sglang ... serve <model>`: the first non-flag token after 'serve'."""
    if "serve" in toks:
        for t in toks[toks.index("serve") + 1:]:
            if not t.startswith("-"):
                return t
    return None


def _docker_serves(*, runner=None):
    """Running containers with their (model_arg, served_names, mounts), parsed from the launch
    command via `docker inspect`. Returns [] when docker is unavailable or anything goes wrong —
    the fallback is strictly best-effort."""
    if runner is None and not shutil.which("docker"):
        return []
    run = runner or _default_docker
    try:
        ids = (run(["docker", "ps", "-q"]) or "").split()
        if not ids:
            return []
        data = json.loads(run(["docker", "inspect"] + ids) or "[]")
    except Exception:
        return []
    res = []
    for c in (data if isinstance(data, list) else []):
        try:
            cfg = c.get("Config") or {}
            toks = _flatten_cmd(list(cfg.get("Entrypoint") or []) + list(cfg.get("Cmd") or []))
            model_arg = _flag_value(toks, "--model") or _serve_positional(toks)
            mounts = {(m.get("Destination") or "").rstrip("/"): m.get("Source")
                      for m in (c.get("Mounts") or []) if m.get("Destination") and m.get("Source")}
            res.append({"name": (c.get("Name") or "").lstrip("/"), "model_arg": model_arg,
                        "served_names": _flag_values(toks, "--served-model-name"), "mounts": mounts})
        except Exception:
            continue
    return res


def _host_to_pod(host_path):
    """Translate a HOST filesystem path (from a container mount Source) into a path the POD can
    open — the inverse of engines._host_path. A containerized pod that mounted the host home at
    /host-home (AEON_HOST_HOME_DIR) or the models volume (AEON_MODELS_HOST_DIR->AEON_MODELS_DIR)
    reads the serve's weights there. Returns a directory that exists, else None."""
    q = (host_path or "").replace("\\", "/")
    if not q:
        return None
    for pod_inner, host_dir in (("/host-home", os.environ.get("AEON_HOST_HOME_DIR")),
                                (os.environ.get("AEON_MODELS_DIR"), os.environ.get("AEON_MODELS_HOST_DIR"))):
        if not (pod_inner and host_dir):
            continue
        hd = host_dir.rstrip("/").replace("\\", "/")
        if q == hd or q.startswith(hd + "/"):
            cand = pod_inner.rstrip("/") + q[len(hd):]
            if os.path.isdir(cand):
                return cand
    return q if os.path.isdir(q) else None                # not containerized (or same namespace)


def _container_model_dir(model_arg, container):
    """The pod-readable directory holding a serve's weights: map the in-container --model path
    through the container's mounts to the host path, then into the pod's namespace."""
    mounts = container.get("mounts") or {}
    host = None
    for dest, src in mounts.items():
        if model_arg == dest:
            host = src
        elif model_arg.startswith(dest + "/"):
            host = (src or "").rstrip("/") + model_arg[len(dest):]
        if host:
            break
    return _host_to_pod(host or model_arg)


def _docker_enrich(served_entries, *, runner=None):
    """Fill the HF guess for served models the endpoint couldn't name (local-path serves), using
    the backing container's --model. In place; best-effort; LOCALHOST callers only."""
    unresolved = [s for s in served_entries if not s.get("hf_guess")]
    if not unresolved:
        return
    containers = _docker_serves(runner=runner)
    if not containers:
        return
    for s in unresolved:
        ids = set(s.get("ids") or [])
        c = next((c for c in containers if ids & set(c.get("served_names") or [])), None)
        marg = c and c.get("model_arg")
        if not marg:
            continue
        s["container"] = c["name"]
        if not _PATHY.match(marg) and diskscan._ORG_NAME_RE.match(marg):
            s.update(hf_guess=marg, source="docker-model-arg", confidence="high")  # launched from a Hub id
            continue
        local = _container_model_dir(marg, c)             # a local path — reconcile via breadcrumbs
        if not local:
            continue
        repo, rev, rsrc = diskscan.reconcile_path(local)
        if repo:
            s.update(hf_guess=repo, hf_revision=rev, source="docker-mount:" + (rsrc or ""),
                     confidence="medium")
        else:                                             # no repo breadcrumb — surface the folder name as a hint
            s["local_name"] = os.path.basename(local.rstrip("/\\")) or None


def scan(hosts=None, ports=None, *, transport=None, timeout=2, docker_runner=None):
    """Sweep (hosts × ports) for OpenAI-compatible servers, concurrently, and AUTODETECT each
    served model's HF repo. `hosts` defaults to localhost; pass a declared LAN/cluster list
    (capped at MAX_HOSTS) to find remote serves. Returns {endpoints:[…], scanned, hosts}. Deduped
    by URL. For LOCALHOST endpoints whose repo the endpoint couldn't name (local-path serves), a
    best-effort docker pass resolves it from the backing container's --model mount."""
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
    # docker fallback only for the pod's OWN host — it cannot inspect a remote node's containers
    for ep in out:
        if ep["host"].split(":", 1)[0] in _LOCAL_HOSTS:
            _docker_enrich(ep.get("served") or [], runner=docker_runner)
    return {"endpoints": out, "scanned": len(targets), "hosts": hosts}
