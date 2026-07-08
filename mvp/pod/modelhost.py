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
    """STRICT bit-for-bit verification of a local weight set against HF's manifest.

    Contract: the local WEIGHT SET must EQUAL the repo@revision's weight set — every
    weight file HF lists exists locally with the exact published sha256, no weight file
    missing, no weight file the repo doesn't have. A single differing byte fails, and a
    disjoint file set fails loudly ('missing:'/'extra:' entries) — it can never pass
    vacuously. (The old behavior compared only same-named files, so pairing a local dir
    with the WRONG repo 'passed' with zero files checked — found by an owner red-team
    paste of a Nemotron card against an Ornith dir, 2026-07-07.)"""
    expected = {n: s for n, s in (ref.get("files") or {}).items()
                if n.lower().endswith(WEIGHT_EXT)}
    per_file, mismatches, lfs_checked = {}, [], 0
    for root, _, files in os.walk(local_dir):
        for fn in files:
            if not fn.lower().endswith(WEIGHT_EXT):
                continue
            rel = os.path.relpath(os.path.join(root, fn), local_dir).replace("\\", "/")
            digest = _sha256_file(os.path.join(root, fn))
            per_file[rel] = digest
            if rel not in expected:
                mismatches.append(f"extra:{rel}")       # not part of the repo -> adulterated set
                continue
            adv = expected[rel]
            if adv:                                     # hash published (LFS) -> must match exactly
                lfs_checked += 1
                if adv != digest:
                    mismatches.append(f"hash:{rel}")
    # Completeness. Sharded model repos (safetensors/torch) need EVERY weight file — a
    # missing shard is an unservable, unverifiable set. GGUF repos are collections of
    # SELF-CONTAINED artifacts (many quantizations of one model; LM Studio downloads just
    # one) — there, every LOCAL file must be repo-exact (enforced above) but absent sibling
    # quants are fine, EXCEPT that a split-GGUF group partially present must be whole.
    gguf_only = bool(expected) and all(n.lower().endswith(".gguf") for n in expected)
    if gguf_only:
        split = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)
        for rel in list(per_file):
            m = split.match(rel)
            if not m:
                continue
            stem, total = m.group(1), int(m.group(3))
            for i in range(1, total + 1):
                part = f"{stem}-{i:05d}-of-{total:05d}.gguf"
                if part not in per_file:
                    mismatches.append(f"missing:{part}")
    else:
        for rel in expected:                            # repo weight files that never appeared
            if rel not in per_file:
                mismatches.append(f"missing:{rel}")
    manifest = ";".join(f"{k}:{per_file[k]}" for k in sorted(per_file))
    return {
        "verified": bool(expected) and bool(per_file) and not mismatches,
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


def derive_recipe(local_dir, ref, *, port=8000, alias=DEFAULT_ALIAS, engine=None,
                  image=None, extra_flags=None, drafter_dir=None) -> dict:
    """Serving recipe from the local config.json + HF card. Engine selection:
      - GGUF weights -> llama.cpp
      - **DGX Spark + aeon-vllm-ultimate available + supported -> aeon-vllm-ultimate (DEFAULT)**
      - a CATALOG engine id (pod.engines: vllm / vllm-rocm / sglang / llama.cpp / mlx /
        aeon-vllm-ultimate) -> that engine's containerized `docker run` recipe (bare-metal for
        MLX — macOS can't run MLX in containers), with `image=` overriding the container image;
      - otherwise -> vanilla vLLM on PATH (legacy bare-metal fallback)
    `engine=` pins a specific engine; the user can also override the whole recipe. Whatever is
    used is recorded WITH the benchmark for reproducibility."""
    cfg = {}
    cfgp = os.path.join(local_dir, "config.json")
    if os.path.exists(cfgp):
        try:
            cfg = json.load(open(cfgp, encoding="utf-8"))
        except Exception:
            cfg = {}
    # Native context: MULTIMODAL configs nest the text settings (Gemma4ForConditionalGeneration
    # keeps max_position_embeddings=262144 inside text_config with NOTHING top-level — the old
    # top-level-only read fell back to a fictitious 8192 and refused a 262K model). None = the
    # config simply doesn't say; that is NOT evidence of a short window.
    def _native_ctx(c):
        for scope in (c, c.get("text_config") or {}, c.get("llm_config") or {},
                      c.get("language_config") or {}):
            for k in ("max_position_embeddings", "n_positions", "model_max_length", "seq_length"):
                v = scope.get(k) if isinstance(scope, dict) else None
                if isinstance(v, (int, float)) and v > 0:
                    return int(v)
        return None
    native_ctx = _native_ctx(cfg)
    # An EXPLICIT operator context (recipe-tuning flag or env) wins over the derived default —
    # the operator may know about rope scaling the config doesn't advertise.
    op_ctx = None
    ef = [str(t) for t in (extra_flags or [])]
    for cf in ("--max-model-len", "--context-length", "-c"):
        if cf in ef and ef.index(cf) + 1 < len(ef):
            try:
                op_ctx = int(float(ef[ef.index(cf) + 1]))
            except ValueError:
                pass
            break
    # Serve at a consistent bench context: the agentic harnesses (Hermes) REQUIRE >=64K, and 64K is
    # ample for every suite prompt while a model's full window (e.g. 256K) needlessly bloats the KV
    # cache. Cap at the model's native max when KNOWN; unknown native serves at the bench standard.
    ctx = int(os.environ.get("AEON_MAX_MODEL_LEN") or op_ctx
              or (min(native_ctx, BENCH_MAX_CTX) if native_ctx else BENCH_MAX_CTX))
    # HARD floor: Hermes refuses to run below 64K, so a short-context verified serve would burn a
    # full bench on a doomed harness pass. Refuse up front — but ONLY on a KNOWN-short model with
    # no operator override; AEON_ALLOW_SHORT_CTX=1 serves at native anyway (hermes then skipped).
    if ctx < BENCH_MAX_CTX and os.environ.get("AEON_ALLOW_SHORT_CTX") != "1":
        raise SystemExit(f"[pod] bench requires --max-model-len >= {BENCH_MAX_CTX} (Hermes harness "
                         f"floor); model native ctx = {native_ctx}. Set AEON_ALLOW_SHORT_CTX=1 to "
                         "serve at native context anyway (hermes will be skipped).")
    quant = (cfg.get("quantization_config") or {}).get("quant_method")
    arch = (cfg.get("architectures") or [None])[0]
    local_files = [f for _, _, fs in os.walk(local_dir) for f in fs]

    gguf = next((os.path.join(r, f) for r, _, fs in os.walk(local_dir)
                 for f in fs if f.endswith(".gguf")), None)

    # DECLARED modalities from config.json — travels with the recipe so the bench can tell
    # "model has no audio" (probe skip is correct) apart from "model DECLARES audio but the
    # serve rejected it" (a recipe/engine problem that must be surfaced, never silently skipped).
    modalities = ["text"]
    if isinstance(cfg.get("vision_config"), dict) or "image_token_id" in cfg \
            or isinstance(cfg.get("mm_vision_config"), dict):
        modalities.append("vision")
    if isinstance(cfg.get("audio_config"), dict) or "audio_token_id" in cfg \
            or isinstance(cfg.get("speech_config"), dict):
        modalities.append("audio")

    from pod import engines as engmod
    from pod import presets as presetmod
    # FAMILY BEST-PRACTICE PRESET: detect the family from config.json and fold its conservative
    # recommended flags UNDER the operator's extra_flags — so a headless/GUI run gets known-good
    # defaults (Gemma-4 -> kv auto + triton + gemma4 parsers; Qwen3.5 -> fp8 KV + qwen3 parser +
    # 16384 budget; etc.), while any flag the operator set still wins (merge_flags dedups,
    # operator last). The preset + which flags it contributed travel in the recipe for the card.
    _preset = presetmod.detect(cfg, name=os.path.basename(local_dir.rstrip("/\\")))
    _preset_flags = presetmod.apply_flags(_preset, modalities)
    extra_flags = _preset_flags + [str(t) for t in (extra_flags or [])]

    base = {"served_alias": alias, "port": port, "source": "auto",
            "architecture": arch, "context_len": ctx, "quant": quant,
            "modalities": modalities,
            "family_preset": {"id": _preset["id"], "label": _preset["label"],
                              "confidence": _preset["confidence"], "flags": _preset_flags}}

    # An explicit catalog engine (Run-tab dropdown / --engine) -> that engine's containerized
    # recipe; a custom `image` rides along and is recorded. MLX serves the LOCAL DIR bare-metal
    # and its served id is that path (mlx_lm.server has no alias flag), recorded as such.
    plat = engmod.host_platform()
    if engine in engmod.ENGINES and (engine != "aeon-vllm-ultimate" or not aeon_vllm_ultimate_launcher()):
        srv = engmod.build_serve(engine, local_dir=local_dir, alias=alias, port=port, ctx=ctx,
                                 quant=quant, image=image, plat=plat, extra_flags=extra_flags,
                                 drafter_dir=drafter_dir)
        if srv["engine"] == "mlx":
            base["served_alias"] = os.path.abspath(local_dir)
        return {**base, **srv}

    if gguf and engine not in ("vllm", "aeon-vllm-ultimate"):
        if plat.get("docker") and os.environ.get("AEON_BARE_SERVE") != "1":
            return {**base, **engmod.build_serve("llama.cpp", local_dir=local_dir, alias=alias,
                                                 port=port, ctx=ctx, image=image, plat=plat,
                                                 extra_flags=extra_flags)}
        return {**base, "engine": "llama.cpp", "serve_mode": "bare",
                "command": ["llama-server", "-m", gguf, "-c", str(ctx),
                            "--host", "0.0.0.0", "--port", str(port), "--alias", alias]}

    # Apple silicon default: MLX bare-metal (macOS cannot run MLX — or CUDA vLLM — in containers).
    if engine is None and plat.get("accel") == "metal":
        srv = engmod.build_serve("mlx", local_dir=local_dir, alias=alias, port=port, ctx=ctx, plat=plat)
        base["served_alias"] = os.path.abspath(local_dir)
        return {**base, **srv, "reason": "Apple silicon default: MLX (bare metal)"}

    ult = aeon_vllm_ultimate_launcher()
    use_ultimate = (engine == "aeon-vllm-ultimate") or (
        engine is None and is_dgx_spark() and bool(ult) and _ultimate_supports(local_files))
    if not use_ultimate and engine is None and plat.get("docker") and not shutil.which("vllm") \
            and os.environ.get("AEON_BARE_SERVE") != "1":
        # no serve binary on PATH but docker IS here (e.g. the containerized dashboard):
        # default to the platform's containerized flagship instead of failing on Popen.
        return {**base, **engmod.build_serve(engmod.recommended_engine(plat),
                                             local_dir=local_dir, alias=alias, port=port,
                                             ctx=ctx, quant=quant, image=image, plat=plat,
                                             extra_flags=extra_flags, drafter_dir=drafter_dir)}
    launcher, eng = (ult or "aeon-vllm-ultimate", "aeon-vllm-ultimate") if use_ultimate else ("vllm", "vllm")
    flags = ["--served-model-name", alias,
             "--host", "0.0.0.0", "--port", str(port), "--max-model-len", str(ctx)]
    if quant:
        flags += ["--quantization", str(quant)]
    flags, applied = engmod.merge_flags(flags, extra_flags)   # recipe tuning on the bare path too
    cmd = [launcher, "serve", local_dir] + flags
    recipe = {**base, "engine": eng, "serve_mode": "bare", "command": cmd, "flags": flags}
    if applied:
        recipe["custom_flags"] = applied
    if use_ultimate and engine is None:
        recipe["reason"] = "DGX Spark default: aeon-vllm-ultimate"
    return recipe


# ---- z-lab DFlash drafter auto-discovery (speculative decode: LOSSLESS — speed only) ----------

DFLASH_ORG = "z-lab"
# draft depth (num_speculative_tokens) per model family — concurrent-optimal values
_DFLASH_NST = {"gemma4": 11, "qwen3": 6}
_DFLASH_NST_DEFAULT = 6


def dflash_repo_for(repo: str):
    """Candidate z-lab drafter repo for org/Name -> 'z-lab/<Name>-DFlash'."""
    name = (repo or "").rstrip("/").split("/")[-1]
    return f"{DFLASH_ORG}/{name}-DFlash" if name else None


def dflash_nst(repo: str, arch: str | None = None) -> int:
    """num_speculative_tokens for the model family (gemma4 drafts deeper than qwen3)."""
    s = re.sub(r"[-_.]", "", f"{repo or ''} {arch or ''}".lower())
    for fam, n in _DFLASH_NST.items():
        if fam in s:
            return n
    return _DFLASH_NST_DEFAULT


def discover_dflash(repo: str, timeout: int = 3, token: str | None = None):
    """Best-effort probe for a z-lab DFlash drafter (HEAD the HF model API, 3s). Returns the
    drafter repo id when it exists, else None. AEON_NO_DFLASH=1 disables. Never raises —
    a failed probe just means plain decode."""
    if os.environ.get("AEON_NO_DFLASH") == "1":
        return None
    cand = dflash_repo_for(repo)
    if not cand:
        return None
    try:
        headers = {"User-Agent": "aeon-pod/0.4"}
        tok = _hf_token(token)
        if tok:
            headers["Authorization"] = "Bearer " + tok
        req = urllib.request.Request(f"{HF}/api/models/{cand}", method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return cand if 200 <= getattr(r, "status", 200) < 300 else None
    except Exception:
        return None


def apply_dflash(recipe: dict, drafter_dir: str, drafter_repo: str, nst: int) -> dict:
    """Attach a DFlash drafter to a derived serve recipe. Spec decode is LOSSLESS — the target
    model verifies every draft token, so answers are bit-identical; only speed changes."""
    spec = {"method": "dflash", "model": drafter_dir, "num_speculative_tokens": nst}
    recipe.setdefault("command", []).extend(["--speculative-config", json.dumps(spec)])
    recipe.update({"drafter": drafter_dir, "drafter_repo": drafter_repo, "spec_decode": "dflash",
                   "spec_decode_note": "lossless — draft tokens verified by the target model; speed only"})
    return recipe


import urllib.parse   # noqa: E402  (used by fetch_ref)
