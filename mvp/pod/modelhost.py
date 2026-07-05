"""aeon-pod model host — the controlled A→B benchmark appliance's MODEL layer.

Given an HF link, the pod:
  1. resolve()       parse the link -> repo @ revision
  2. fetch_ref()     pull HF's canonical reference (commit sha + per-file LFS sha256 + config)
  3. pull()          download the weights (integrity-checked by huggingface_hub)
  4. verify()        hash the weights -> a content-addressed `weights_hash`, compare to HF's
                     LFS sha256 where published, and pin the commit sha = the SIGNATURE that
                     the model on disk IS exactly repo@sha as hosted on HF
  5. derive_recipe() best-effort serving recipe from the HF card/config (user can override)

The recipe + verification + hardware profile travel WITH the benchmark so anyone can see
exactly how a measurement was produced. Serving the model (vLLM/llama.cpp) and the harness
containers run on the GPU host; THIS module is the portable, testable control plane.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request

HF = "https://huggingface.co"
WEIGHT_EXT = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".npz")
DEFAULT_ALIAS = "model-under-test"   # the served alias Hermes / OpenClaw / OpenCode connect to
BENCH_MAX_CTX = 65536                 # standard bench context: >=64K (Hermes harness floor) + KV-sane


def resolve(hf_link: str):
    """Parse an HF link / repo-id into (repo_id, revision). Accepts 'org/model',
    'https://huggingface.co/org/model', '.../tree/<rev>', 'org/model@rev'."""
    s = re.sub(r"^https?://(www\.)?huggingface\.co/", "", (hf_link or "").strip())
    rev = "main"
    m = re.search(r"/tree/([^/]+)/?$", s)
    if m:
        rev, s = m.group(1), s[:m.start()]
    if "@" in s:
        s, rev = s.split("@", 1)
    parts = s.strip("/").split("/")
    repo = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
    return repo, rev


def _hf_token(token: str | None = None):
    """Resolve an HF token: explicit arg first, else the ambient env (what the pod subprocess
    injects for gated/private repos). Returns None when none is set (public repos)."""
    return token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def fetch_ref(repo: str, revision: str = "main", timeout: int = 15, token: str | None = None) -> dict:
    """HF's canonical reference for repo@revision: commit sha + per-file LFS sha256 + card."""
    # ?blobs=true is REQUIRED — the bare endpoint returns only rfilename, no lfs.sha256.
    url = f"{HF}/api/models/{repo}/revision/{urllib.parse.quote(revision, safe='')}?blobs=true"
    headers = {"User-Agent": "aeon-pod/0.4"}
    tok = _hf_token(token)
    if tok:                                    # gated/private repos: the ref lookup itself needs auth
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read().decode("utf-8", "replace"))
    files = {s["rfilename"]: (s.get("lfs") or {}).get("sha256")
             for s in j.get("siblings", []) if s.get("rfilename")}
    return {"repo": repo, "revision": revision, "sha": j.get("sha"), "files": files,
            "card": j.get("cardData") or {}, "config": j.get("config") or {},
            "tags": j.get("tags") or [], "pipeline_tag": j.get("pipeline_tag")}


def pull(repo: str, revision: str, dest: str, token: str | None = None) -> str:
    """Download the model into `dest` (huggingface_hub verifies each file's hash on download).
    `token` (or the ambient HF_TOKEN/HUGGING_FACE_HUB_TOKEN env) authenticates gated/private repos."""
    from huggingface_hub import snapshot_download
    return snapshot_download(repo_id=repo, revision=revision, local_dir=dest, token=_hf_token(token))


def _sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def verify(local_dir: str, ref: dict) -> dict:
    """Hash the weight files -> a content-addressed `weights_hash`; compare to HF's LFS
    sha256 where published. Bytes are already guaranteed to be repo@sha (hub-verified on
    download); explicit LFS matches add signature confirmation for the big weight files."""
    per_file, mismatches, lfs_checked = {}, [], 0
    for root, _, files in os.walk(local_dir):
        for fn in files:
            if not fn.lower().endswith(WEIGHT_EXT):
                continue
            rel = os.path.relpath(os.path.join(root, fn), local_dir).replace("\\", "/")
            digest = _sha256_file(os.path.join(root, fn))
            per_file[rel] = digest
            adv = (ref.get("files") or {}).get(rel)
            if adv:
                lfs_checked += 1
                if adv != digest:
                    mismatches.append(rel)
    manifest = ";".join(f"{k}:{per_file[k]}" for k in sorted(per_file))
    return {
        "verified": bool(per_file) and not mismatches,
        "method": "lfs_sha256+manifest" if lfs_checked else "hub_download+revision+manifest",
        "weights_hash": hashlib.sha256(manifest.encode()).hexdigest(),
        "revision": ref.get("sha"),
        "n_weight_files": len(per_file), "lfs_checked": lfs_checked,
        "mismatches": mismatches, "per_file": per_file,
    }


def _gpu_names():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=8)
        if out.returncode == 0:
            return [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass
    return []


def is_dgx_spark() -> bool:
    """True on an NVIDIA DGX Spark (GB10 Grace-Blackwell). Force with AEON_SYSTEM=dgx-spark."""
    if os.environ.get("AEON_SYSTEM", "").lower().replace("_", "-") == "dgx-spark":
        return True
    names = " ".join(_gpu_names()).lower()
    return "gb10" in names or "dgx spark" in names


def aeon_vllm_ultimate_launcher():
    """The optimized first-party engine launcher, or None. AEON_VLLM_ULTIMATE=<path>|1 forces
    it; otherwise we look for `aeon-vllm-ultimate` on PATH."""
    env = os.environ.get("AEON_VLLM_ULTIMATE")
    if env and env != "0":
        return "aeon-vllm-ultimate" if env in ("1", "true", "yes") else env
    return shutil.which("aeon-vllm-ultimate")


def _ultimate_supports(files) -> bool:
    """aeon-vllm-ultimate is a vLLM derivative (safetensors transformers, incl. NVFP4); GGUF
    stays on llama.cpp. Extend with an arch allow/deny list if a model ever needs it."""
    return not any(f.endswith(".gguf") for f in files)


def derive_recipe(local_dir, ref, *, port=8000, alias=DEFAULT_ALIAS, engine=None) -> dict:
    """Serving recipe from the local config.json + HF card. Engine selection:
      - GGUF weights -> llama.cpp
      - **DGX Spark + aeon-vllm-ultimate available + supported -> aeon-vllm-ultimate (DEFAULT)**
      - otherwise -> vanilla vLLM
    `engine=` pins a specific engine; the user can also override the whole recipe. Whatever is
    used is recorded WITH the benchmark for reproducibility."""
    cfg = {}
    cfgp = os.path.join(local_dir, "config.json")
    if os.path.exists(cfgp):
        try:
            cfg = json.load(open(cfgp, encoding="utf-8"))
        except Exception:
            cfg = {}
    native_ctx = cfg.get("max_position_embeddings") or cfg.get("n_positions") or 8192
    # Serve at a consistent bench context: the agentic harnesses (Hermes) REQUIRE >=64K, and 64K is
    # ample for every suite prompt while a model's full window (e.g. 256K) needlessly bloats the KV
    # cache. Cap at the model's native max (never exceed it); AEON_MAX_MODEL_LEN overrides explicitly.
    ctx = int(os.environ.get("AEON_MAX_MODEL_LEN") or min(native_ctx, BENCH_MAX_CTX))
    quant = (cfg.get("quantization_config") or {}).get("quant_method")
    arch = (cfg.get("architectures") or [None])[0]
    local_files = [f for _, _, fs in os.walk(local_dir) for f in fs]

    gguf = next((os.path.join(r, f) for r, _, fs in os.walk(local_dir)
                 for f in fs if f.endswith(".gguf")), None)
    if gguf and engine not in ("vllm", "aeon-vllm-ultimate"):
        return {"engine": "llama.cpp", "served_alias": alias, "port": port, "source": "auto",
                "architecture": arch, "context_len": ctx,
                "command": ["llama-server", "-m", gguf, "-c", str(ctx),
                            "--host", "0.0.0.0", "--port", str(port), "--alias", alias]}

    ult = aeon_vllm_ultimate_launcher()
    use_ultimate = (engine == "aeon-vllm-ultimate") or (
        engine is None and is_dgx_spark() and bool(ult) and _ultimate_supports(local_files))
    launcher, eng = (ult or "aeon-vllm-ultimate", "aeon-vllm-ultimate") if use_ultimate else ("vllm", "vllm")
    cmd = [launcher, "serve", local_dir, "--served-model-name", alias,
           "--host", "0.0.0.0", "--port", str(port), "--max-model-len", str(ctx)]
    if quant:
        cmd += ["--quantization", str(quant)]
    recipe = {"engine": eng, "served_alias": alias, "port": port, "source": "auto",
              "architecture": arch, "context_len": ctx, "quant": quant, "command": cmd}
    if use_ultimate and engine is None:
        recipe["reason"] = "DGX Spark default: aeon-vllm-ultimate"
    return recipe


import urllib.parse   # noqa: E402  (used by fetch_ref)
