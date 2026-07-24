"""pod/endpoints.py — discover running OpenAI-compatible inference servers on this host (and
optionally on declared cluster/LAN hosts), so an operator can bench a LIVE serve IN PLACE:

    scan → pick an endpoint → it shows the model it serves → the pod AUTODETECTS the HF repo →
    (fill/confirm the HF link) → the pod hash-verifies those weights + logprob-fingerprints the
    endpoint → attested (if it matches).

POD-ONLY. Probes GET /v1/models on common inference ports. Names + ports only; no weights pulled
here. The mothership never serves this route (pod-gated), so there is no SSRF surface — it is the
operator scanning their own machine/LAN.

HF AUTODETECT — ENGINE-AGNOSTIC. Serving engines expose the model's identity differently, so
autodetect draws on several signals, most→least authoritative:
  * /v1/models[].root — vLLM sets it to the launch --model (the exact repo/path). SGLang mirrors
    it to the served id. llama.cpp / Ollama / TGI / LM Studio omit it.
  * /v1/models[].id — SGLang (no alias), TGI (=--model-id), LM Studio (publisher/repo) put the
    repo here; an Ollama `hf.co/owner/repo:quant` tag carries it too. vLLM/llama.cpp aliases don't.
  * docker fallback (localhost only) — match the backing container to the endpoint BY PORT (the one
    universal link — cmd --port/-p, env, or published ports), then read its model reference from a
    broad flag/env set (--model / --model-path / --model-id / -m / -hf / positional) and reconcile
    a directory (safetensors) or a .gguf file back to a repo.
Every guess only PREFILLS the HF-link field. The launch still pulls+hashes those weights
(modelhost.verify — quant-safetensors AND single-file GGUF are both bit-for-bit verifiable) and
fingerprints the endpoint, so a wrong guess fails verification and can never weaken attestation."""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.request

from pod import diskscan

# common OpenAI-compatible serve ports across engines: vLLM 8000, TGI 3000/8080, llama.cpp 8080,
# LM Studio 1234, Ollama 11434, SGLang 30000, plus the pod's ASR/TTS sidecars.
COMMON_PORTS = [8000, 8001, 8002, 8080, 1234, 11434, 30000, 3000, 8010, 5000, 8081]
MAX_HOSTS = 8
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
# a value that is a filesystem path, not an org/model repo id (leading / ./ ../ or a Windows drive)
_PATHY = re.compile(r"^(/|\./|\.\./|[A-Za-z]:[\\/])")
# an Ollama-style Hub tag / a huggingface.co URL -> the org/model repo id inside it
_HF_TAG_RE = re.compile(r"^(?:hf\.co|huggingface\.co)/([A-Za-z0-9][\w.-]*/[\w.-]+?)(?::[\w.-]+)?$", re.I)
_HF_URL_RE = re.compile(r"^https?://huggingface\.co/([A-Za-z0-9][\w.-]*/[\w.-]+?)(?:/(?:tree|blob)/[^/]+)?/?$", re.I)


def _repo_from_ref(ref):
    """If `ref` names a Hugging Face repo (a bare org/model, an hf.co/… tag, or a hf.co URL),
    return that repo id; else None. Strips an Ollama `:quant` tag and a URL tail. A filesystem
    path is never a repo."""
    if not isinstance(ref, str) or not ref:
        return None
    m = _HF_TAG_RE.match(ref) or _HF_URL_RE.match(ref)
    if m:
        return m.group(1)
    base = ref.split(":", 1)[0] if (":" in ref and not _PATHY.match(ref)) else ref
    if not _PATHY.match(base) and diskscan._ORG_NAME_RE.match(base):
        return base
    return None


def _guess_from_served(mid, root):
    """Autodetect the HF repo from a served model's endpoint metadata alone. `root` (vLLM's launch
    --model) is strongest; the served id is the fallback (SGLang no-alias, TGI, LM Studio, an
    Ollama hf.co tag). Returns (hf_guess, source, confidence) or (None, None, None)."""
    for val, src in ((root, "served-root"), (mid, "served-id")):
        repo = _repo_from_ref(val)
        if repo:
            return repo, src + (":hf-tag" if val != repo else ""), "high"
    return None, None, None


def _fmt_from_owner(owned_by, mid):
    """A weak format/engine hint from /v1/models: llama.cpp / Ollama / LM Studio serve GGUF (or
    MLX); vLLM / SGLang / TGI serve safetensors. Used only to message verifiability, never to gate."""
    o = (owned_by or "").lower()
    if o == "llamacpp" or o == "library" or (isinstance(mid, str) and mid.startswith("hf.co/")):
        return "gguf"
    if o in ("vllm", "sglang") or "/" in (owned_by or ""):     # tgi sets owned_by == model_id
        return "safetensors"
    return None


def _probe(base_url, *, timeout=2, transport=None):
    """GET <base>/v1/models. Returns {url, host, models, served, reachable} when an
    OpenAI-compatible server answers with ≥1 model id, else None. `served` folds aliases of one
    physical model (same root) into one entry and carries the per-model HF autodetect. Never
    raises."""
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
            key = root or mid                            # aliases share one root -> one entry
            if key in by_key:
                by_key[key]["ids"].append(mid)
                continue
            hf_guess, gsrc, conf = _guess_from_served(mid, root)
            entry = {"ids": [mid], "root": root, "hf_guess": hf_guess, "hf_revision": None,
                     "source": gsrc, "confidence": conf, "owned_by": m.get("owned_by"),
                     "format": _fmt_from_owner(m.get("owned_by"), mid),
                     "max_model_len": m.get("max_model_len")}
            by_key[key] = entry
            served.append(entry)
        if models:
            return {"url": base_url.rstrip("/") + "/v1", "host": base_url.split("//", 1)[-1],
                    "models": models, "served": served, "reachable": True}
    except Exception:
        return None
    return None


# ---- docker fallback: resolve a serve's HF repo from its backing container --------------------
# Any engine binds a PORT, so we match the container to the endpoint by port (the universal link),
# then read the model reference from whatever flag/env that engine uses and reconcile it.

def _pod_ssh_key():
    """The pod's dedicated ssh key, if present. This is the identity the operator authorizes on the
    serving machine, so the remote docker calls MUST use it explicitly."""
    k = os.environ.get("AEON_SSH_KEY")
    k = os.path.expanduser(k) if k else os.path.expanduser("~/.aeon/id_ed25519")
    return k if os.path.exists(k) else None


def _default_docker(argv, docker_host=None):             # pragma: no cover — real docker
    """`docker_host` = 'ssh://user@host' runs `argv` (a docker command) ON that machine.

    We do NOT use Docker's built-in `DOCKER_HOST=ssh://` transport: its internal ssh connection
    (`ssh … docker system dial-stdio`) can't be told which key to use and won't find the pod's
    dedicated ~/.aeon key — so even after the operator authorizes that key, it fails with
    'Permission denied'. Instead we run docker THROUGH ssh with `-i <pod key>`, which uses exactly
    the authorized identity (and needs no local docker daemon — good for a pod on a box without
    Docker Desktop). The pod key is added but NOT forced (no IdentitiesOnly), so an operator who
    instead relies on an ~/.ssh/config alias still works."""
    if docker_host and docker_host.startswith("ssh://"):
        dest = docker_host[len("ssh://"):]
        ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=8"]
        key = _pod_ssh_key()
        if key:
            ssh += ["-i", key]
        # argv is ["docker","inspect","c1",…]; ssh runs it as the remote command (hex ids/simple
        # flags only, so no remote-shell quoting hazard)
        return subprocess.run(ssh + [dest] + list(argv),
                              capture_output=True, text=True, timeout=25).stdout
    return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout


def _flatten_cmd(parts):
    """Flatten Entrypoint+Cmd into tokens, expanding shell wrappers so flags inside a
    ["/bin/bash","-lc","exec vllm serve … --port 8000"] string become findable tokens."""
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
    """The token after `flag`, or the RHS of `flag=value`."""
    for i, t in enumerate(toks):
        if t == flag and i + 1 < len(toks):
            return toks[i + 1]
        if t.startswith(flag + "="):
            return t[len(flag) + 1:]
    return None


def _flag_values(toks, flag):
    """All consecutive non-flag tokens after `flag` (nargs flags like --served-model-name)."""
    out = []
    for i, t in enumerate(toks):
        if t == flag:
            for nxt in toks[i + 1:]:
                if nxt.startswith("-"):
                    break
                out.append(nxt)
            break
    return out


def _positional_after(toks, kw):
    """The first non-flag token after subcommand `kw` (vllm `serve` / lms `load` / ollama `run`)."""
    if kw in toks:
        for t in toks[toks.index(kw) + 1:]:
            if not t.startswith("-"):
                return t
    return None


# model-reference flags across engines, priority order: explicit HF-repo first, then path/file
_HF_REPO_FLAGS = ("--model-id", "-hf", "-hfr", "--hf-repo", "-dr", "--docker-repo", "-mu", "--model-url")
_PATH_MODEL_FLAGS = ("--model", "--model-path", "--model_path", "-m")
_ENV_REPO_KEYS = ("MODEL_ID", "LLAMA_ARG_HF_REPO")
_ENV_PATH_KEYS = ("LLAMA_ARG_MODEL",)
_PORT_FLAGS = ("--port", "-p")


def _cmd_port(toks):
    for i, t in enumerate(toks):
        if t in _PORT_FLAGS and i + 1 < len(toks) and toks[i + 1].isdigit():
            return int(toks[i + 1])
        if t.startswith("--port="):
            v = t.split("=", 1)[1]
            if v.isdigit():
                return int(v)
    return None


def _env_port(env):
    for k in ("PORT", "LLAMA_ARG_PORT"):
        v = env.get(k)
        if v and str(v).isdigit():
            return int(v)
    oh = env.get("OLLAMA_HOST")                          # host:port | :port
    if oh and ":" in str(oh):
        p = str(oh).rsplit(":", 1)[1]
        if p.isdigit():
            return int(p)
    return None


def _published_ports(raw):
    """Host ports a bridge-network container publishes (NetworkSettings.Ports HostPort values)."""
    out = set()
    for _, binds in ((raw.get("NetworkSettings") or {}).get("Ports") or {}).items():
        for b in (binds or []):
            hp = b.get("HostPort")
            if hp and str(hp).isdigit():
                out.add(int(hp))
    return out


def _docker_serves(*, runner=None, docker_host=None):
    """Running containers with everything needed to match+resolve: name, flattened cmd tokens,
    env dict, mounts, the serve port (cmd/env), and published host ports. [] if docker absent.
    `docker_host` ('ssh://user@host') inspects the REMOTE machine's daemon instead of this one."""
    if runner is None and not shutil.which("docker"):
        return []
    run = runner or (lambda argv: _default_docker(argv, docker_host=docker_host))
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
            env = {}
            for e in (cfg.get("Env") or []):
                if isinstance(e, str) and "=" in e:
                    k, v = e.split("=", 1)
                    env[k] = v
            mounts = {(m.get("Destination") or "").rstrip("/"): m.get("Source")
                      for m in (c.get("Mounts") or []) if m.get("Destination") and m.get("Source")}
            res.append({"name": (c.get("Name") or "").lstrip("/"), "toks": toks, "env": env,
                        "mounts": mounts, "image": cfg.get("Image"),
                        "served_names": _flag_values(toks, "--served-model-name"),
                        "cmd_port": _cmd_port(toks) or _env_port(env),
                        "pub_ports": _published_ports(c)})
        except Exception:
            continue
    return res


def _match_container(port, ids, containers):
    """The container backing an endpoint: by serve PORT (cmd/env/published — the universal link),
    else by --served-model-name overlap, else by a served id appearing verbatim in the command."""
    idset = set(ids or [])
    for c in containers:                                 # 1) port — works across every engine
        if port and (c.get("cmd_port") == port or port in (c.get("pub_ports") or set())):
            return c
    for c in containers:                                 # 2) served-model-name alias overlap
        if idset & set(c.get("served_names") or []):
            return c
    for c in containers:                                 # 3) a served id literally in the argv
        if idset & set(c.get("toks") or []):
            return c
    return None


def _model_ref_from_container(c):
    """The model reference the container was launched with, across engines: explicit HF-repo flags
    (TGI --model-id, llama.cpp -hf) and env first, then path/file flags (--model / --model-path /
    -m), then env paths, then a positional after serve/load/run."""
    toks, env = c.get("toks") or [], c.get("env") or {}
    for f in _HF_REPO_FLAGS:
        v = _flag_value(toks, f)
        if v:
            return v
    for k in _ENV_REPO_KEYS:
        if env.get(k):
            return env[k]
    for f in _PATH_MODEL_FLAGS:
        v = _flag_value(toks, f)
        if v:
            return v
    for k in _ENV_PATH_KEYS:
        if env.get(k):
            return env[k]
    for kw in ("serve", "load", "run"):
        v = _positional_after(toks, kw)
        if v:
            return v
    return None


def _host_to_pod(host_path):
    """Translate a HOST path (a container mount Source) into a path the POD can open — the inverse
    of engines._host_path, via /host-home (AEON_HOST_HOME_DIR) or the models volume. Returns an
    existing file/dir, else None."""
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
            if os.path.exists(cand):
                return cand
    return q if os.path.exists(q) else None               # not containerized (or shared namespace)


def _container_path(ref, container):
    """The pod-readable path (file or dir) for a serve's --model value: map it through the
    container's mounts to the host path, then into the pod's namespace."""
    host = None
    for dest, src in (container.get("mounts") or {}).items():
        if ref == dest:
            host = src
        elif ref.startswith(dest + "/"):
            host = (src or "").rstrip("/") + ref[len(dest):]
        if host:
            break
    return _host_to_pod(host or ref)


def _reconcile_gguf(gguf_path):
    """Map a .gguf FILE back to an HF repo id. Returns (repo, revision, source) or Nones.
    Tries the pod's own pull metadata / HF-cache in the file's dir first, then the LM Studio /
    publisher-repo layout `.../<publisher>/<repo>/<file>.gguf`."""
    d = os.path.dirname(gguf_path)
    repo, rev, src = diskscan.reconcile_path(d)          # .aeon-modelref / hf-cache / org__name
    if repo:
        return repo, rev, src
    parts = d.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        cand = parts[-2] + "/" + parts[-1]               # LM Studio: <publisher>/<repo>/
        if diskscan._ORG_NAME_RE.match(cand):
            return cand, None, "gguf-path-layout"
    return None, None, None


def _resolve_ref(ref, container):
    """Turn a container's model reference into a guess dict {hf_guess, hf_revision, source,
    confidence, format, local_name} (missing keys omitted). Exception-safe."""
    repo = _repo_from_ref(ref)
    if repo:                                             # a Hub id / hf.co tag / URL — authoritative
        return {"hf_guess": repo, "source": "docker-model-arg", "confidence": "high",
                "format": "gguf" if ref.rstrip().endswith(".gguf") else None}
    local = _container_path(ref, container)              # a local path — reconcile the bytes' origin
    is_gguf = bool(local and local.rstrip().lower().endswith(".gguf")) or \
        (isinstance(ref, str) and ref.rstrip().lower().endswith(".gguf"))
    if not local:
        # Unreadable path — normal for a REMOTE serve (the pod can't see that machine's disk). We
        # still know the mount's folder NAME from docker, which is a real hint the operator can act
        # on ("that's my …-NVFP4 quant"), so surface it rather than showing nothing.
        out = {"format": "gguf" if is_gguf else None}
        src = (container.get("mounts") or {}).get(str(ref).rstrip("/"))
        name = os.path.basename(str(src or ref).replace("\\", "/").rstrip("/"))
        if name and not _PATHY.match(name):
            out["local_name"] = name
        return out
    if is_gguf:
        repo, rev, rsrc = _reconcile_gguf(local)
        out = {"format": "gguf"}
        if repo:
            out.update(hf_guess=repo, hf_revision=rev, source="docker-mount:" + (rsrc or ""),
                       confidence="medium")
        else:
            out["local_name"] = os.path.basename(local)
        return out
    repo, rev, rsrc = diskscan.reconcile_path(local)     # a directory of safetensors
    if repo:
        return {"hf_guess": repo, "hf_revision": rev, "format": "safetensors",
                "source": "docker-mount:" + (rsrc or ""), "confidence": "medium"}
    return {"format": "safetensors", "local_name": os.path.basename(local.rstrip("/\\"))}


def _docker_enrich(served_entries, *, endpoint_port, containers):
    """Fill the HF guess for served models the endpoint couldn't name, from the backing container
    (matched by port). In place; best-effort; localhost callers only."""
    for s in served_entries:
        if s.get("hf_guess"):
            continue
        c = _match_container(endpoint_port, s.get("ids"), containers)
        if not c:
            continue
        ref = _model_ref_from_container(c)
        if not ref:
            continue
        s["container"] = c["name"]
        for k, v in _resolve_ref(ref, c).items():        # merge only the keys the resolver set
            if v is not None:
                s[k] = v


def observed_serve_recipe(serve_url, served_ids, *, runner=None, docker_host=None):
    """Capture the ACTUAL startup command of a LOCALHOST endpoint's backing container, so an
    endpoint-mode ("point at a running model") run records the REAL serve recipe — image + the
    exact flags, INCLUDING `--speculative-config` — instead of the pod's hypothetical derived
    command (which it never ran). Matches the container by port (then served-name), reads its
    vLLM `serve <model> <flags…>`, and reconciles the `--speculative-config` drafter mount to an
    HF repo when the layout carries it. Returns a dict or None (remote endpoint / no docker / no
    match). Best-effort, exception-safe — capture never blocks or fails a bench."""
    try:
        host = (serve_url or "").split("//", 1)[-1].split("/", 1)[0]   # host[:port]
        # A remote serve is inspectable ONLY when the operator authorized a docker host for it
        # (DOCKER_HOST=ssh://…). Without that we cannot see the machine, so we say so rather than
        # guessing.
        if host.split(":")[0] not in _LOCAL_HOSTS and not (docker_host or runner):
            return None
        port = None
        if ":" in host:
            p = host.rsplit(":", 1)[1]
            port = int(p) if p.isdigit() else None
        c = _match_container(port, served_ids, _docker_serves(runner=runner, docker_host=docker_host))
        if not c:
            return None
        toks = c.get("toks") or []
        flags = []
        if "serve" in toks:                                           # vLLM: `serve <model> <flags…>`
            rest = toks[toks.index("serve") + 1:]
            flags = rest[1:] if (rest and not rest[0].startswith("-")) else rest
        spec = _flag_value(flags, "--speculative-config") or _flag_value(toks, "--speculative-config")
        spec_cfg = None
        if spec:
            try:
                spec_cfg = json.loads(spec)
            except Exception:
                spec_cfg = None
        drafter_repo = drafter_rev = None                             # full DFlash/DSpark disclosure
        if isinstance((spec_cfg or {}).get("model"), str):
            dlocal = _container_path(spec_cfg["model"], c)
            if dlocal:
                d = dlocal if os.path.isdir(dlocal) else os.path.dirname(dlocal)
                drafter_repo, drafter_rev, _ = diskscan.reconcile_path(d)
        return {"container": c.get("name"), "image": c.get("image"),
                "port": port or c.get("cmd_port"), "argv": toks, "flags": flags,
                "speculative_config": spec_cfg, "drafter_repo": drafter_repo,
                "drafter_revision": drafter_rev, "served_names": c.get("served_names")}
    except Exception:
        return None


# ---- serving-integrity: confirm the RUNNING serve is actually serving the named model ---------
# A GPU-FREE reliability check (NOT the ranked-attestation gate). Before benching a serve the pod
# did NOT launch, inspect the backing container's ON-DISK model — config.json + the weight manifest,
# and optionally per-file sha256 over ssh — and compare it to the HF-verified reference. It catches
# the common ACCIDENTS the operator actually hits: pointed at the wrong live instance, wrong size
# (7B when you meant 70B), wrong quant, stale/wrong weights on disk. A cooperative operator gets a
# definite "yes, this is the model" / "no, you are on the wrong instance".
#
# TRUST BOUNDARY (important): everything read here is reported by the operator's own host, and a file
# on disk is not proof the running process LOADED it — so this does NOT defeat a deliberate faker who
# mounts the real weights but serves something else. That is the behavioral fingerprint's job
# (ingest._trust_tier). A pass here is a SAFETY confirmation, never a substitute for the fingerprint.
# You also cannot hash the weights resident in VRAM against HF's FILE sha256 — the engine fuses/
# repacks/shards/requantizes them on load — so this hashes the FILES the container was launched with.

_STRUCT_KEYS = ("model_type", "num_hidden_layers", "hidden_size", "num_attention_heads",
                "num_key_value_heads", "vocab_size", "intermediate_size",
                "max_position_embeddings", "head_dim")
_STORE_EXTS = (".safetensors", ".gguf", ".bin")


def _sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def _loads(txt):
    try:
        return json.loads(txt)
    except Exception:
        return None


def _ssh_capture(docker_host, remote_argv, timeout=90):    # pragma: no cover — real ssh
    """Run a simple command on the serving host over the pod's AUTHORIZED key; return stdout ('' on
    failure). Tokens are shell-quoted because ssh joins them into one remote shell line."""
    if not (docker_host and docker_host.startswith("ssh://")):
        return ""
    dest = docker_host[len("ssh://"):]
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ConnectTimeout=8"]
    key = _pod_ssh_key()
    if key:
        ssh += ["-i", key]
    remote = " ".join(shlex.quote(t) for t in remote_argv)
    try:
        return subprocess.run(ssh + [dest, remote], capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def _model_dir_local(model_ref, container):
    """Pod-readable model DIRECTORY for the serve (local / shared namespace), or None (remote)."""
    local = _container_path(model_ref, container)
    if not local:
        return None
    return local if os.path.isdir(local) else os.path.dirname(local)


def _model_dir_remote(model_ref, container):
    """The model DIRECTORY's path on the REMOTE serving machine (for ssh reads), or None. Maps the
    --model ref through the container's mount to the host source path."""
    src = None
    for dest, s in (container.get("mounts") or {}).items():
        if model_ref == dest:
            src = s
        elif model_ref.startswith(dest + "/"):
            src = (s or "").rstrip("/") + model_ref[len(dest):]
        if src:
            break
    src = src or model_ref                                # bind-less: the ref is already a host path
    if not src:
        return None
    src = src.replace("\\", "/")
    if src.lower().endswith((".gguf", ".safetensors", ".bin", ".pt", ".pth", ".npz")):
        return src.rsplit("/", 1)[0] if "/" in src else "."      # a single-file --model -> its dir
    return src.rstrip("/")


def _read_model_dir_file(rel, model_ref, container, docker_host):
    """Read a small text file (config.json / *.index.json) from the SERVE's model dir. Local: through
    the container mount into the pod namespace. Remote: `ssh cat` on the serving host. Text or None."""
    d = _model_dir_local(model_ref, container)
    if d:
        p = os.path.join(d, rel)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()
            except Exception:
                return None
        return None
    if docker_host:
        rd = _model_dir_remote(model_ref, container)
        if rd:
            return _ssh_capture(docker_host, ["cat", rd + "/" + rel]) or None
    return None


def _quant_method(cfg):
    q = cfg.get("quantization_config") or {}
    return q.get("quant_method") or q.get("quant_algo") or q.get("quant_type")


def _compare_config(want, got):
    """Structural config identity: architecture + shape keys present in BOTH configs (comparing only
    shared keys avoids false alarms from an omitted-vs-defaulted field), plus the quant method."""
    out = []
    wa, ga = want.get("architectures"), got.get("architectures")
    if wa is not None and ga is not None:
        out.append({"name": "architectures", "status": "match" if wa == ga else "mismatch",
                    "detail": f"architecture: expected {wa}, serve reports {ga}"})
    for k in _STRUCT_KEYS:
        if k in want and k in got:
            out.append({"name": k, "status": "match" if want[k] == got[k] else "mismatch",
                        "detail": f"{k}: expected {want[k]}, serve reports {got[k]}"})
    wq, gq = _quant_method(want), _quant_method(got)
    if wq is not None or gq is not None:
        nq = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())   # tolerate encoding noise (fp4/_/-)
        out.append({"name": "quant_method", "status": "match" if nq(wq) == nq(gq) else "mismatch",
                    "detail": f"quantization: expected {wq}, serve reports {gq}"})
    return out


def _compare_manifest(ref, model_ref, container, docker_host, is_gguf, local_dir):
    """Weight-set identity, shard-boundary-INVARIANT. GGUF: the served file is one the repo publishes.
    safetensors: the serve's TENSOR-NAME set (index weight_map KEYS — invariant to how the shards are
    split) equals the HF-pulled reference's. Comparing tensor names, not shard FILENAMES, means a
    bit-identical model re-sharded to a different max_shard_size is NOT falsely flagged, while a
    genuinely different tensor set still is. Advisory ('unavailable') when there's no comparable index."""
    if is_gguf:
        served = os.path.basename(str(model_ref))
        repo_gguf = {os.path.basename(n) for n in (ref.get("files") or {}) if n.lower().endswith(".gguf")}
        inrepo = served in repo_gguf
        return [{"name": "weight_file", "status": "match" if inrepo else "mismatch",
                 "detail": (f"served GGUF {served} is published in {ref.get('repo')}" if inrepo
                            else f"served GGUF {served} is NOT a file in {ref.get('repo')}")}]
    serve_idx = _loads(_read_model_dir_file("model.safetensors.index.json", model_ref, container,
                                            docker_host) or "")
    ref_idx = _load_json(os.path.join(local_dir, "model.safetensors.index.json")) if local_dir else None
    s_tensors = set((serve_idx or {}).get("weight_map", {}).keys())
    r_tensors = set((ref_idx or {}).get("weight_map", {}).keys())
    if s_tensors and r_tensors:
        missing, extra = r_tensors - s_tensors, s_tensors - r_tensors
        ok = not missing and not extra
        return [{"name": "weight_tensors", "status": "match" if ok else "mismatch",
                 "detail": (f"tensor set matches {ref.get('repo')} ({len(s_tensors)} tensors)" if ok
                            else f"tensor set differs from {ref.get('repo')} "
                                 f"(missing {len(missing)}, unexpected {len(extra)})")}]
    return [{"name": "weight_manifest", "status": "unavailable",
             "detail": "no comparable safetensors index (shard-boundary-tolerant check skipped)"}]


def _parse_sha256sum(out):
    """Parse `sha256sum` stdout ('<hex>  <path>' per line) -> {path_as_echoed: hexdigest}. Keyed by
    the FULL path (never basename), so two files sharing a basename in different dirs stay distinct."""
    res = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64 and all(c in "0123456789abcdef" for c in parts[0].lower()):
            res[parts[1].strip().lstrip("*")] = parts[0]
    return res


def _container_hash(container_name, paths, docker_host):
    """sha256 files INSIDE the running container (`docker exec … sha256sum …`) — the most faithful
    'what is actually running' read, at the serve's own --model path in its mount namespace. Remote
    via ssh, local via docker. {path_as_echoed: hexdigest} (keyed by full path); {} on any failure."""
    if not (container_name and paths):
        return {}
    argv = ["docker", "exec", container_name, "sha256sum"] + list(paths)
    if docker_host:
        return _parse_sha256sum(_ssh_capture(docker_host, argv, timeout=3600))
    try:                                                 # pragma: no cover — real docker
        return _parse_sha256sum(subprocess.run(argv, capture_output=True, text=True,
                                               timeout=3600).stdout)
    except Exception:
        return {}


def _deep_hash(per_file, model_ref, container, docker_host, weights_hash):
    """Recompute the bundle's weights_hash from the SERVED weight files and require it to match.

    `per_file` = {rel_path: sha256} is EXACTLY what modelhost.verify() hashed for this bundle (the
    pod's HF-verified pull) and is what `weights_hash` was computed over. Hashing the running
    container's files at those same rel paths and rebuilding the same manifest reproduces
    `weights_hash` IFF the served weight set is bit-identical to the verified one. Keying by full rel
    path avoids basename collapse across sub-dirs; recomputing the aggregate hash needs no separate
    completeness gate (a missing or altered file changes the result); and it is inherently narrowed to
    the served artifact for a single-quant GGUF (per_file holds only that file). Returns {check, verified}."""
    want = {rel: s for rel, s in (per_file or {}).items() if s}
    if not (want and weights_hash):
        return {"check": {"name": "weight_sha256", "status": "unavailable",
                          "detail": "no per-file hashes to recompute the weights_hash from"},
                "verified": False}
    mr = str(model_ref)
    is_file = mr.lower().endswith((".gguf", ".safetensors", ".bin", ".pt", ".pth", ".npz"))
    base = (mr.rsplit("/", 1)[0] if ("/" in mr and is_file) else mr).rstrip("/")
    got = {}                                             # rel -> digest
    cpaths = {base + "/" + rel: rel for rel in want}     # container path -> rel key
    raw = _container_hash(container.get("name"), list(cpaths), docker_host)
    for p, rel in cpaths.items():
        if p in raw:
            got[rel] = raw[p]
    if not got:                                          # docker exec unavailable -> pod/host read
        ld = _model_dir_local(model_ref, container)
        if ld:
            for rel in want:
                p = os.path.join(ld, *rel.split("/"))
                if os.path.exists(p):
                    try:
                        got[rel] = _sha256_file(p)
                    except Exception:
                        pass
        elif docker_host:
            rd = _model_dir_remote(model_ref, container)
            if rd:
                hpaths = {rd + "/" + rel: rel for rel in want}
                raw = _parse_sha256sum(
                    _ssh_capture(docker_host, ["sha256sum"] + list(hpaths), timeout=3600))
                for p, rel in hpaths.items():
                    if p in raw:
                        got[rel] = raw[p]
    if not got:
        return {"check": {"name": "weight_sha256", "status": "unavailable",
                          "detail": "could not read the served weight files to hash them"},
                "verified": False}
    bad = [rel for rel in want if rel in got and got[rel] != want[rel]]
    complete = set(got) >= set(want)
    served_wh = None
    if complete and not bad:
        manifest = ";".join(f"{rel}:{got[rel]}" for rel in sorted(want))
        served_wh = hashlib.sha256(manifest.encode()).hexdigest()
    verified = bool(served_wh and served_wh == weights_hash)
    if verified:
        status, detail = "match", f"all {len(want)} served weight files reproduce the verified weights_hash"
    elif bad:
        status, detail = "mismatch", f"{len(bad)} served weight file(s) do NOT match HF's published sha256"
    elif not complete:
        status, detail = ("unavailable",
                          f"only {len(got)}/{len(want)} served weight files could be hashed (incomplete)")
    else:
        status, detail = "mismatch", "served weight set did not reproduce the bundle's weights_hash"
    return {"check": {"name": "weight_sha256", "status": status, "detail": detail}, "verified": verified}


def serving_integrity(serve_url, served_ids, *, ref, local_dir, runner=None,
                      docker_host=None, deep=False, weights_hash=None, per_file=None):
    """Confirm the running serve is actually serving `ref` (the HF repo the bench names) by comparing
    the backing container's on-disk model to the HF-verified reference. GPU-free. See the section
    header for the trust boundary. Returns an evidence dict:
        {status: 'match'|'mismatch'|'unavailable', ok: bool|None, checks: [...], summary, method,
         weights_verified: bool, weights_hash: <echoed when weights_verified>}
    `weights_verified` is True only when `deep` ran and EVERY served weight file sha256-matched HF;
    that (tied to `weights_hash`) is what the mothership's endpoint_verified attestation rides on.
    Never raises."""
    try:
        host = (serve_url or "").split("//", 1)[-1].split("/", 1)[0]
        port = None
        if ":" in host:
            pp = host.rsplit(":", 1)[1]
            port = int(pp) if pp.isdigit() else None
        if host.split(":")[0] not in _LOCAL_HOSTS and not (docker_host or runner):
            return {"status": "unavailable", "ok": None, "checks": [], "method": "none",
                    "summary": "serving host not inspectable (authorize a docker/ssh host to check it)"}
        c = _match_container(port, served_ids, _docker_serves(runner=runner, docker_host=docker_host))
        if not c:
            return {"status": "unavailable", "ok": None, "checks": [], "method": "none",
                    "summary": "no backing container matched the endpoint (bare-metal serve?)"}
        model_ref = _model_ref_from_container(c)
        if not model_ref:
            return {"status": "unavailable", "ok": None, "checks": [], "method": "docker-inspect",
                    "container": c.get("name"),
                    "summary": "the container exposes no --model reference to check"}
        is_gguf = str(model_ref).lower().endswith(".gguf")
        checks = []
        if not is_gguf:
            want_cfg = _load_json(os.path.join(local_dir, "config.json")) if local_dir else None
            got_txt = _read_model_dir_file("config.json", model_ref, c, docker_host)
            got_cfg = _loads(got_txt) if got_txt else None
            if want_cfg and got_cfg:
                checks += _compare_config(want_cfg, got_cfg)
            elif want_cfg:
                checks.append({"name": "config.json", "status": "unavailable",
                               "detail": "could not read the serve's config.json"})
        checks += _compare_manifest(ref, model_ref, c, docker_host, is_gguf, local_dir)
        weights_verified = False
        if deep:
            dh = _deep_hash(per_file, model_ref, c, docker_host, weights_hash)
            checks.append(dh["check"])
            weights_verified = bool(dh.get("verified"))
        real = [x for x in checks if x["status"] in ("match", "mismatch")]
        mism = [x for x in checks if x["status"] == "mismatch"]
        if not real:
            return {"status": "unavailable", "ok": None, "checks": checks, "method": "docker-inspect",
                    "container": c.get("name"), "weights_verified": False,
                    "summary": "backing container found, but its model was not inspectable for identity"}
        ok = not mism
        repo = ref.get("repo")
        summary = (f"confirmed serving {repo} — {len(real)} identity checks matched"
                   + (" (weights sha256-verified)" if weights_verified else "") if ok
                   else "SERVE MISMATCH — " + "; ".join(x["detail"] for x in mism[:3]))
        out = {"status": "match" if ok else "mismatch", "ok": ok, "checks": checks,
               "container": c.get("name"), "model_ref": model_ref,
               "weights_verified": weights_verified,
               "method": "config+manifest" + ("+sha256" if deep else ""), "summary": summary}
        if ok and weights_verified and weights_hash:      # ties the verification to THIS bundle's weights
            out["weights_hash"] = weights_hash
        return out
    except Exception as e:
        return {"status": "unavailable", "ok": None, "checks": [], "method": "none",
                "summary": f"serving-integrity check error ({e})"}


def scan(hosts=None, ports=None, *, transport=None, timeout=2, docker_runner=None, docker_host=None):
    """Sweep (hosts × ports) for OpenAI-compatible servers, concurrently, and AUTODETECT each
    served model's HF repo (engine-agnostic). Returns {endpoints:[…], scanned, hosts}. Deduped by
    URL. LOCALHOST endpoints the API couldn't name get a best-effort docker pass (matched by port)."""
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
    # docker fallback: the pod's OWN host, or — when the operator authorized one — a REMOTE daemon
    # via docker_host (ssh://user@host), which is how a pod benching ANOTHER machine still
    # autodetects that machine's HF repo. Only runs when something is still unnamed.
    unresolved = [ep for ep in out
                  if (docker_host or ep["host"].split(":", 1)[0] in _LOCAL_HOSTS)
                  and any(not s.get("hf_guess") for s in (ep.get("served") or []))]
    if unresolved:
        containers = _docker_serves(runner=docker_runner, docker_host=docker_host)
        if containers:
            for ep in unresolved:
                try:
                    port = int(ep["host"].rsplit(":", 1)[1])
                except Exception:
                    port = None
                _docker_enrich(ep.get("served") or [], endpoint_port=port, containers=containers)
    return {"endpoints": out, "scanned": len(targets), "hosts": hosts}
