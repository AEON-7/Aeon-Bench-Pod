"""pod/engines.py — the inference-engine catalog behind the pod's "pick your container" launcher.

The pod benchmarks a model by SERVING it first. This module owns:
  * ENGINES        — the curated catalog (top engines, each pointing at its upstream repo +
                     official container image) a user picks from in the Run tab;
  * host_platform()— what THIS machine can run (cuda / rocm / metal / cpu, DGX Spark, docker,
                     whether the dashboard itself is containerized);
  * catalog()      — the platform-annotated list the GUI renders (available / recommended);
  * build_serve()  — the engine-specific serve recipe: a `docker run` argv for containerized
                     engines (everything except MLX), or a bare-metal command for Apple MLX
                     (macOS does not support MLX inside containers). Whatever is built is
                     RECORDED verbatim with the run — docker recipe and bare-metal recipe are
                     reported the same way, so every result replicates.

AEON's own boards run aeon-vllm-ultimate with fully-optimal per-model settings; everyone else
picks the ideal engine for their hardware (or overrides the image entirely) — the recipe travels
with the submission either way.
"""
from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import subprocess

SERVE_CONTAINER = "aeon-bench-serve"      # fixed name so a wedged serve is always findable/removable

# ---- the curated catalog -----------------------------------------------------------------------
# style = command grammar; image may be overridden per-run (custom container) without changing it.
ENGINES = {
    "aeon-vllm-ultimate": {
        "name": "AEON vLLM Ultimate", "style": "vllm",
        "image": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
        "url": "https://github.com/aeon-7/aeon-vllm-ultimate",
        "platforms": ["cuda"], "formats": ["safetensors"],
        "note": "AEON's tuned vLLM build (NVFP4/modelopt + DFlash speculative decode) — the "
                "engine behind AEON's own attested boards; optimal on DGX Spark GB10.",
    },
    "vllm": {
        "name": "vLLM", "style": "vllm",
        "image": "vllm/vllm-openai:latest",
        "url": "https://github.com/vllm-project/vllm",
        "platforms": ["cuda"], "formats": ["safetensors"],
        "note": "The upstream OpenAI-compatible vLLM server — the portable CUDA default.",
    },
    "vllm-rocm": {
        "name": "vLLM (ROCm)", "style": "vllm",
        "image": "rocm/vllm:latest",
        "url": "https://hub.docker.com/r/rocm/vllm",
        "platforms": ["rocm"], "formats": ["safetensors"],
        "note": "AMD GPUs: vLLM on ROCm (MI/Radeon; needs /dev/kfd + /dev/dri passthrough).",
    },
    "sglang": {
        "name": "SGLang", "style": "sglang",
        "image": "lmsysorg/sglang:latest",
        "url": "https://github.com/sgl-project/sglang",
        "platforms": ["cuda"], "formats": ["safetensors"],
        "note": "LMSYS's high-throughput serving runtime (RadixAttention); OpenAI-compatible.",
    },
    "llama.cpp": {
        "name": "llama.cpp", "style": "llama",
        "image": "ghcr.io/ggml-org/llama.cpp:server-cuda",
        "image_cpu": "ghcr.io/ggml-org/llama.cpp:server",
        "url": "https://github.com/ggml-org/llama.cpp",
        "platforms": ["cuda", "cpu"], "formats": ["gguf"],
        "note": "GGUF serving anywhere — CUDA offload or pure CPU (x86/ARM).",
    },
    "mlx": {
        "name": "Apple MLX", "style": "mlx", "containerized": False,
        "image": None,
        "url": "https://github.com/ml-explore/mlx-lm",
        "platforms": ["metal"], "formats": ["safetensors", "mlx"],
        "note": "Apple-silicon native (Metal). macOS cannot run MLX inside containers, so this "
                "is the pod's BARE-METAL path: `pip install mlx-lm`, serve with the generated "
                "command, and the startup recipe is reported exactly like a docker recipe.",
    },
    "lmstudio": {
        "name": "LM Studio", "style": "lmstudio", "containerized": False,
        "image": None,
        "url": "https://lmstudio.ai",
        "platforms": ["cuda", "metal", "cpu"], "formats": ["gguf", "mlx"],
        "note": "Desktop-native serving (Windows / macOS / Linux; llama.cpp + MLX backends) — "
                "BARE-METAL host performance, no container. Start its OpenAI-compatible server "
                "with the generated commands, point the pod at it; the startup recipe is "
                "reported exactly like a docker recipe.",
    },
}


# ---- the FLAG CATALOG: every common serve knob we've used or tuned, per engine grammar --------
# Drives the Run tab's "recipe tuning" panel — the point of the bench is finding the OPTIMAL
# recipe per system, so the knobs that move real performance are first-class, annotated with
# what we've learned (DGX GB10: gpu-util 0.70 is OOM-safe on unified memory; FlashInfer is
# broken on GB10 — use triton_attn/flash_attn; 64K ctx is the Hermes harness floor; DFlash
# spec-decode is lossless and n trades single-stream vs concurrent speed).
FLAG_CATALOG = {
    "vllm": [   # shared grammar: aeon-vllm-ultimate / vLLM / vLLM-ROCm
        {"flag": "--max-model-len", "kind": "number", "default": 65536, "min": 65536,
         "label": "max model len", "note": "served context; 64K is the BENCH FLOOR (Hermes refuses less) — only HIGHER values allowed"},
        {"flag": "--gpu-memory-utilization", "kind": "number", "default": 0.90, "step": 0.05,
         "label": "gpu memory util", "note": "VRAM fraction; unified-memory boxes (DGX Spark GB10) are OOM-safe at 0.70"},
        {"flag": "--max-num-seqs", "kind": "number", "default": 32,
         "label": "max num seqs", "note": "concurrent sequence cap; 32 is a sane ceiling at 64K ctx (16-24 is the GB10 sweet spot)"},
        {"flag": "--quantization", "kind": "enum", "options": ["modelopt", "compressed-tensors", "awq", "gptq", "fp8", "bitsandbytes"],
         "label": "quantization", "note": "usually auto-derived from config.json (NVFP4 repos -> modelopt)"},
        {"flag": "--kv-cache-dtype", "kind": "enum", "options": ["auto", "fp8_e4m3", "fp8_e5m2"],
         "label": "kv cache dtype",
         "note": "fp8 KV halves cache memory -> more concurrency at long ctx. CAUTION: crashes "
                 "Gemma4 (interleaved sliding-window layers) on triton_attn — EngineCore dies at "
                 "first request with 'Window left is not the same for all layers'. Use auto for "
                 "gemma4, or pair fp8 KV with --disable-sliding-window (costs KV memory)"},
        {"flag": "--attention-backend", "kind": "enum", "options": ["triton_attn", "flash_attn", "flashinfer", "xformers"],
         "label": "attention backend", "note": "GB10: triton_attn/flash_attn (FlashInfer is broken on GB10)"},
        {"flag": "--dtype", "kind": "enum", "options": ["auto", "bfloat16", "float16"],
         "label": "dtype", "note": "activation dtype; auto respects the checkpoint"},
        {"flag": "--enable-prefix-caching", "kind": "bool", "label": "prefix caching",
         "note": "reuses shared-prefix KV across requests (the perf grid cache-busts anyway)"},
        {"flag": "--enable-chunked-prefill", "kind": "bool", "label": "chunked prefill",
         "note": "interleaves prefill with decode — smoother TTFT under load"},
        {"flag": "--trust-remote-code", "kind": "bool", "label": "trust remote code",
         "note": "required by repos with custom modeling code"},
        {"flag": "--tensor-parallel-size", "kind": "number", "default": 1,
         "label": "tensor parallel", "note": "multi-GPU: shards the model across N GPUs"},
        {"flag": "--reasoning-parser", "kind": "enum",
         "options": ["qwen3", "deepseek_r1", "gemma4", "glm45", "granite", "hunyuan_a13b",
                     "mistral", "step3", "ernie45", "seed_oss", "minimax_m1", "gpt_oss", "qwq"],
         "label": "reasoning parser",
         "note": "separates <think> from the answer — WITHOUT it a reasoning model leaks its trace "
                 "and tanks Instruction/Prose. Pick your family: Qwen 3.x -> qwen3, DeepSeek -> "
                 "deepseek_r1, Gemma-4 -> gemma4, GLM-4.5 -> glm45, StepFun -> step3. The family "
                 "preset sets the right one automatically."},
        {"flag": "--tool-call-parser", "kind": "enum",
         "options": ["qwen3_coder", "qwen3_xml", "hermes", "deepseek_v3", "deepseek_v31",
                     "gemma4", "glm45", "kimi_k2", "step3", "llama3_json", "llama4_json",
                     "llama4_pythonic", "mistral", "internlm", "granite", "granite-20b-fc",
                     "jamba", "hunyuan_a13b", "phi4_mini_json", "minimax", "xlam", "pythonic",
                     "seed_oss"],
         "label": "tool-call parser",
         "note": "the harness tool-call format for this family — pairs with auto tool choice. "
                 "Qwen 3.x -> qwen3_coder, DeepSeek -> deepseek_v3 (v3.1 -> deepseek_v31), "
                 "GLM-4.5 -> glm45, Kimi K2 -> kimi_k2, StepFun -> step3, Gemma-4 -> gemma4, "
                 "Llama -> llama3_json/llama4_pythonic; hermes is the generic fallback. The "
                 "family preset picks it for you."},
        {"flag": "--enable-auto-tool-choice", "kind": "bool", "label": "auto tool choice",
         "note": "lets harnesses drive native tool calling"},
        {"flag": "--swap-space", "kind": "number", "default": 4, "label": "swap space (GiB)",
         "note": "CPU offload headroom per GPU"},
        {"flag": "--limit-mm-per-prompt", "kind": "string", "label": "multimodal limits",
         "note": "per-prompt multimodal item caps, e.g. {\"audio\":2,\"image\":4} — some builds "
                 "need this for a declared-audio model to ACCEPT input_audio (the bench warns "
                 "on a declared-vs-served mismatch)"},
        {"flag": "--reasoning-budget", "kind": "number", "label": "reasoning budget",
         "note": "cap on <think> tokens per response; empty = engine default (uncapped). "
                 "Qwen models destabilize above ~16384 — set 16384 or lower there; Gemma4 "
                 "handles uncapped fine"},
        # --speculative-config is handled by the dedicated SPEC DECODE block in the Run tab
        # (drafter HF card + preset dropdown), not as a raw catalog knob.
    ],
    "sglang": [
        {"flag": "--context-length", "kind": "number", "default": 65536, "min": 65536,
         "label": "context length", "note": "64K is the BENCH FLOOR (Hermes) — only higher allowed"},
        {"flag": "--mem-fraction-static", "kind": "number", "default": 0.88, "step": 0.05,
         "label": "mem fraction", "note": "KV pool fraction — lower if you OOM"},
        {"flag": "--max-running-requests", "kind": "number", "default": 256,
         "label": "max running requests", "note": "concurrency cap"},
        {"flag": "--quantization", "kind": "enum", "options": ["fp8", "awq", "gptq", "modelopt"],
         "label": "quantization", "note": "match the checkpoint"},
        {"flag": "--tp", "kind": "number", "default": 1, "label": "tensor parallel", "note": "multi-GPU sharding"},
    ],
    "llama": [
        {"flag": "-c", "kind": "number", "default": 65536, "min": 65536, "label": "context (-c)",
         "note": "64K is the BENCH FLOOR (Hermes) — only higher allowed"},
        {"flag": "-ngl", "kind": "number", "default": 999, "label": "gpu layers (-ngl)",
         "note": "999 = everything on GPU; lower to fit VRAM"},
        {"flag": "--threads", "kind": "number", "label": "cpu threads", "note": "CPU-side worker threads"},
        {"flag": "--parallel", "kind": "number", "default": 4, "label": "parallel slots",
         "note": "concurrent request slots (pair with --cont-batching)"},
        {"flag": "--cont-batching", "kind": "bool", "label": "continuous batching",
         "note": "required for real concurrency"},
        {"flag": "--flash-attn", "kind": "bool", "label": "flash attention", "note": "faster attention where supported"},
    ],
}

#: bench contract — these are the pod's wiring, never operator-tunable
PROTECTED_FLAGS = {"--served-model-name", "--host", "--port", "--alias", "--model-path", "-m"}


def merge_flags(base: list[str], extra: list[str] | None) -> tuple[list[str], list[str]]:
    """Overlay operator flag overrides onto an engine's base flags: a flag already present has
    its value REPLACED (or is kept bare for bools); a new flag is appended. PROTECTED_FLAGS are
    dropped (the bench contract owns them). Returns (merged, applied) — `applied` is the
    provenance list recorded in the recipe."""
    if not extra:
        return list(base), []
    # tokenize extra into (flag, value|None) pairs
    pairs, i = [], 0
    while i < len(extra):
        tok = str(extra[i]).strip()
        if not tok:
            i += 1
            continue
        if tok.startswith("-"):
            val = None
            if i + 1 < len(extra) and not str(extra[i + 1]).startswith("-"):
                val = str(extra[i + 1]).strip()
                i += 1
            if tok not in PROTECTED_FLAGS:
                pairs.append((tok, val))
        i += 1
    merged, applied = list(base), []
    for flag, val in pairs:
        applied.append(flag if val is None else f"{flag} {val}")
        if flag in merged:                               # replace in place
            j = merged.index(flag)
            has_val = j + 1 < len(merged) and not str(merged[j + 1]).startswith("-")
            if val is None:
                if has_val:
                    del merged[j + 1]
            elif has_val:
                merged[j + 1] = val
            else:
                merged.insert(j + 1, val)
        else:                                            # append new
            merged.append(flag)
            if val is not None:
                merged.append(val)
    return merged, applied


#: the served-context bench floor — Hermes refuses models reporting less, which would silently
#: burn a whole run's harness pass. Overrides BELOW the floor are raised back to it.
CTX_FLOOR = 65536
_CTX_FLAG = {"vllm": "--max-model-len", "sglang": "--context-length", "llama": "-c"}


def _floor_ctx(args: list[str], style: str) -> list[str]:
    """Raise a sub-floor context override back to CTX_FLOOR (AEON_ALLOW_SHORT_CTX=1 opts out —
    the pod then skips the Hermes harness honestly instead of failing it)."""
    if os.environ.get("AEON_ALLOW_SHORT_CTX") == "1":
        return args
    flag = _CTX_FLAG.get(style)
    if flag and flag in args:
        i = args.index(flag)
        try:
            if i + 1 < len(args) and int(float(args[i + 1])) < CTX_FLOOR:
                args[i + 1] = str(CTX_FLOOR)
        except (TypeError, ValueError):
            pass
    return args


def _has_rocm() -> bool:
    return os.path.exists("/dev/kfd") or bool(shutil.which("rocm-smi"))


def _has_cuda() -> bool:
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=8)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def _daemon_has_nvidia() -> bool:
    """Ask the DOCKER DAEMON whether the host has the NVIDIA runtime. The containerized
    dashboard may lack GPU access itself (someone forgot --gpus all), but sibling engine
    containers it launches CAN still get GPUs — the daemon, not this container, is the truth."""
    try:
        out = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                             capture_output=True, text=True, timeout=8)
        return out.returncode == 0 and "nvidia" in (out.stdout or "")
    except Exception:
        return False


_PLAT_CACHE: dict | None = None


def host_platform() -> dict:
    """What THIS host can serve with. `accel` is the primary accelerator; `in_container` means
    the dashboard itself runs inside docker (serve containers become SIBLINGS via the mounted
    socket, and weight mounts must use HOST paths — see _host_path). Cached for the process
    lifetime — hardware doesn't change mid-run, and per-request nvidia-smi probes can flap."""
    global _PLAT_CACHE
    if _PLAT_CACHE is not None:
        return _PLAT_CACHE
    from pod import modelhost
    osname = {"Darwin": "macos", "Windows": "windows"}.get(_platform.system(), "linux")
    metal = osname == "macos" and _platform.machine().lower() in ("arm64", "aarch64")
    in_container = os.path.exists("/.dockerenv")
    docker = bool(shutil.which("docker"))
    try:      # Apple-silicon Docker Desktop: linux VM (linuxkit) on an arm64 Mac — the HOST is
        apple_vm = (_platform.machine().lower() == "aarch64"      # macOS, so bare-metal MLX /
                    and "linuxkit" in os.uname().release)          # LM Studio paths apply
    except AttributeError:
        apple_vm = False
    accel_source = "probe"
    if _has_cuda():
        accel = "cuda"
    elif _has_rocm():
        accel = "rocm"
    elif metal:
        accel = "metal"
    elif in_container and docker and _daemon_has_nvidia():
        accel = "cuda"                       # host is CUDA-capable even if THIS container isn't
        accel_source = "docker-daemon"       # (run the dashboard with --gpus all for full detail)
    else:
        accel = "cpu"
    _PLAT_CACHE = {
        "os": osname, "arch": _platform.machine().lower(), "accel": accel,
        "accel_source": accel_source,
        "dgx_spark": modelhost.is_dgx_spark(),
        "docker": docker,
        "in_container": in_container,
        "apple_vm": apple_vm,
    }
    return _PLAT_CACHE


def recommended_engine(plat: dict, *, gguf: bool = False) -> str:
    """The default pick for a platform: DGX Spark -> AEON's own optimal engine; GGUF weights ->
    llama.cpp; NO docker at all -> the bare-metal engines (MLX on Apple silicon, LM Studio
    elsewhere — e.g. a bare Windows box); else the accelerator's containerized flagship."""
    if plat.get("dgx_spark"):
        return "aeon-vllm-ultimate"
    if plat.get("apple_vm"):
        return "mlx"                 # containerized dashboard on a Mac: host MLX beats VM CPU
    if not plat.get("docker"):
        return "mlx" if plat["accel"] == "metal" else "lmstudio"
    if gguf:
        return "llama.cpp"
    return {"cuda": "vllm", "rocm": "vllm-rocm", "metal": "mlx", "cpu": "llama.cpp"}[plat["accel"]]


def catalog(plat: dict | None = None) -> dict:
    """The platform-annotated engine list the Run tab renders as its dropdown."""
    plat = plat or host_platform()
    rec = recommended_engine(plat)
    out = []
    for eid, e in ENGINES.items():
        available = plat["accel"] in e["platforms"] or "cpu" in e["platforms"]
        if eid == "mlx":       # host-is-a-Mac in both shapes: native macOS or the Apple VM
            available = plat["os"] == "macos" or bool(plat.get("apple_vm"))
        if eid == "lmstudio":
            available = True                     # desktop app, any OS, no container needed
        if e.get("containerized", True) and not plat["docker"]:
            available = False
        out.append({"id": eid, "name": e["name"], "image": e.get("image"),
                    "url": e["url"], "note": e["note"], "formats": e["formats"],
                    "platforms": e["platforms"],
                    "containerized": e.get("containerized", True),
                    "available": available, "recommended": eid == rec,
                    # the tunable-knob catalog for this engine's grammar (recipe tuning panel)
                    "flags": FLAG_CATALOG.get(e["style"], [])})
    return {"platform": plat, "engines": out, "recommended": rec}


def _host_path(p: str) -> str:
    """Translate an in-container weights path to the HOST path for sibling `docker run -v`
    mounts. When the dashboard runs in a container, AEON_MODELS_DIR is where IT sees the
    models volume and AEON_MODELS_HOST_DIR is where the HOST has it; outside a container
    the path is already a host path. Pure string prefix-swap — the mapping crosses filesystem
    namespaces, so os.path.abspath (which would prepend the local drive/cwd) must not touch it."""
    q = p.replace("\\", "/")
    for inner, host in ((os.environ.get("AEON_MODELS_DIR"), os.environ.get("AEON_MODELS_HOST_DIR")),
                        ("/host-home", os.environ.get("AEON_HOST_HOME_DIR"))):
        if not (inner and host):
            continue
        inner = inner.rstrip("/").replace("\\", "/")
        if q == inner or q.startswith(inner + "/"):
            return host.rstrip("/") + q[len(inner):]
    return os.path.abspath(p)


def _gpu_flags(engine_id: str, plat: dict) -> list[str]:
    if engine_id == "vllm-rocm" or plat.get("accel") == "rocm":
        return ["--device=/dev/kfd", "--device=/dev/dri", "--ipc=host",
                "--group-add", "video", "--security-opt", "seccomp=unconfined"]
    if plat.get("accel") == "cuda":
        return ["--gpus", "all"]
    return []


def _find_gguf(local_dir: str) -> str | None:
    for root, _, files in os.walk(local_dir):
        for f in sorted(files):
            if f.endswith(".gguf"):
                return os.path.relpath(os.path.join(root, f), local_dir).replace("\\", "/")
    return None


def _declares_audio(local_dir: str) -> bool:
    """True when the model's config declares an audio tower (audio_config / audio_token_id) —
    i.e. serving it WITHOUT an audio allowance silently amputates a real capability."""
    try:
        with open(os.path.join(local_dir, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return any(k in cfg for k in ("audio_config", "audio_token_id", "audio_token_index"))
    except Exception:
        return False


def build_serve(engine_id: str, *, local_dir: str, alias: str, port: int, ctx: int,
                quant: str | None = None, image: str | None = None,
                plat: dict | None = None, extra_flags: list[str] | None = None,
                drafter_dir: str | None = None) -> dict:
    """The engine-specific serve recipe. Containerized engines -> a `docker run` argv (host
    networking, weights mounted read-only at /model, fixed container name so cleanup is always
    possible). MLX -> the bare-metal command. `image` overrides the catalog image (custom
    container); `extra_flags` are operator recipe-tuning overrides merged via merge_flags
    (protected bench wiring dropped). Both are RECORDED so the result still replicates."""
    e = ENGINES.get(engine_id)
    if not e:
        raise ValueError(f"unknown engine '{engine_id}' (catalog: {', '.join(ENGINES)})")
    plat = plat or host_platform()
    img = image or (e.get("image_cpu") if engine_id == "llama.cpp" and plat["accel"] != "cuda"
                    else e.get("image"))

    if e["style"] == "mlx":
        # BARE METAL by necessity (no MLX in macOS containers). The startup recipe is recorded
        # exactly like a docker recipe. Serving is the operator's step when the dashboard itself
        # is containerized; the pod validates weights + benches + signs either way.
        cmd = ["mlx_lm.server", "--model", os.path.abspath(local_dir),
               "--host", "0.0.0.0", "--port", str(port)]
        return {"engine": "mlx", "serve_mode": "bare", "image": None, "command": cmd,
                "bare_cmd": "pip install mlx-lm   # once\n" + " ".join(cmd),
                "no_harness": True,   # harness containers address a served alias MLX can't provide (yet)
                "alias_from_server": True,   # bench under whatever id the server actually reports
                "setup": "pip install mlx-lm"}

    if e["style"] == "lmstudio":
        # BARE METAL desktop serving (Windows/macOS/Linux) — the host-performance path for
        # non-container users. The operator starts LM Studio's OpenAI-compatible server with
        # these exact commands; the pod validates the weights on disk, benches the endpoint,
        # and this startup recipe travels with the result exactly like a docker recipe.
        mdl = os.path.abspath(local_dir)
        bare = (f"lms server start --port {port}\n"
                f'lms load "{mdl}" --context-length {ctx}\n'
                f"# (or load the model in the LM Studio app with context length {ctx})")
        return {"engine": "lmstudio", "serve_mode": "bare", "image": None, "command": None,
                "bare_cmd": bare, "no_harness": True, "alias_from_server": True,
                "setup": "LM Studio + its `lms` CLI — https://lmstudio.ai"}

    docker = ["docker", "run", "--rm", "--name", SERVE_CONTAINER, "--network", "host",
              *_gpu_flags(engine_id, plat), "-v", f"{_host_path(local_dir)}:/model:ro"]
    if drafter_dir:                                      # validated spec-decode drafter -> /drafter
        docker += ["-v", f"{_host_path(drafter_dir)}:/drafter:ro"]
    applied: list[str] = []
    if e["style"] == "vllm":
        flags = ["--served-model-name", alias, "--host", "0.0.0.0", "--port", str(port),
                 "--max-model-len", str(ctx)]
        if quant:
            flags += ["--quantization", str(quant)]
        if _declares_audio(local_dir):
            # A declared-audio model served without an audio allowance rejects every
            # input_audio request with "At most 0 audio(s) may be provided" (vLLM's default
            # mm limit) — the audio suite then probe-skips and the capability is silently
            # untested. Grant it up-front; an operator --limit-mm-per-prompt in recipe
            # tuning still overrides via merge_flags.
            flags += ["--limit-mm-per-prompt", '{"image":4,"audio":4}']
        flags, applied = merge_flags(flags, extra_flags)
        flags = _floor_ctx(flags, "vllm")
        cmd = docker + ["--entrypoint", "vllm", img, "serve", "/model"] + flags
        srv = {"flags": flags}                          # vllm-style flags: the repro card renders these
    elif e["style"] == "sglang":
        args = ["--model-path", "/model", "--served-model-name", alias,
                "--host", "0.0.0.0", "--port", str(port), "--context-length", str(ctx)]
        args, applied = merge_flags(args, extra_flags)
        args = _floor_ctx(args, "sglang")
        cmd = docker + ["--ipc=host", img, "python3", "-m", "sglang.launch_server"] + args
        srv = {}
    elif e["style"] == "llama":
        gguf = _find_gguf(local_dir)
        if not gguf:
            raise ValueError("llama.cpp needs GGUF weights; none found in the model dir")
        args = ["-m", f"/model/{gguf}", "-c", str(ctx),
                "--host", "0.0.0.0", "--port", str(port), "--alias", alias]
        if plat["accel"] == "cuda":
            args += ["-ngl", "999"]
        args, applied = merge_flags(args, extra_flags)
        args = _floor_ctx(args, "llama")
        cmd = docker + [img] + args
        srv = {}
    else:                                                # pragma: no cover — catalog is closed
        raise ValueError(f"engine '{engine_id}' has no serve builder")

    return {"engine": engine_id, "serve_mode": "docker", "image": img, "command": cmd,
            "docker_run": " ".join(cmd), "container_name": SERVE_CONTAINER,
            "image_overridden": bool(image),
            **({"custom_flags": applied} if applied else {}), **srv}
