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
}


def _has_rocm() -> bool:
    return os.path.exists("/dev/kfd") or bool(shutil.which("rocm-smi"))


def _has_cuda() -> bool:
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=8)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


def host_platform() -> dict:
    """What THIS host can serve with. `accel` is the primary accelerator; `in_container` means
    the dashboard itself runs inside docker (serve containers become SIBLINGS via the mounted
    socket, and weight mounts must use HOST paths — see _host_path)."""
    from pod import modelhost
    osname = {"Darwin": "macos", "Windows": "windows"}.get(_platform.system(), "linux")
    metal = osname == "macos" and _platform.machine().lower() in ("arm64", "aarch64")
    accel = "cuda" if _has_cuda() else "rocm" if _has_rocm() else "metal" if metal else "cpu"
    return {
        "os": osname, "arch": _platform.machine().lower(), "accel": accel,
        "dgx_spark": modelhost.is_dgx_spark(),
        "docker": bool(shutil.which("docker")),
        "in_container": os.path.exists("/.dockerenv"),
    }


def recommended_engine(plat: dict, *, gguf: bool = False) -> str:
    """The default pick for a platform: DGX Spark -> AEON's own optimal engine; GGUF weights ->
    llama.cpp regardless; else the accelerator's flagship."""
    if gguf:
        return "llama.cpp"
    if plat.get("dgx_spark"):
        return "aeon-vllm-ultimate"
    return {"cuda": "vllm", "rocm": "vllm-rocm", "metal": "mlx", "cpu": "llama.cpp"}[plat["accel"]]


def catalog(plat: dict | None = None) -> dict:
    """The platform-annotated engine list the Run tab renders as its dropdown."""
    plat = plat or host_platform()
    rec = recommended_engine(plat)
    out = []
    for eid, e in ENGINES.items():
        available = plat["accel"] in e["platforms"] or "cpu" in e["platforms"]
        if eid == "mlx" and plat["os"] != "macos":
            available = False
        if e.get("containerized", True) and not plat["docker"]:
            available = False
        out.append({"id": eid, "name": e["name"], "image": e.get("image"),
                    "url": e["url"], "note": e["note"], "formats": e["formats"],
                    "platforms": e["platforms"],
                    "containerized": e.get("containerized", True),
                    "available": available, "recommended": eid == rec})
    return {"platform": plat, "engines": out, "recommended": rec}


def _host_path(p: str) -> str:
    """Translate an in-container weights path to the HOST path for sibling `docker run -v`
    mounts. When the dashboard runs in a container, AEON_MODELS_DIR is where IT sees the
    models volume and AEON_MODELS_HOST_DIR is where the HOST has it; outside a container
    the path is already a host path. Pure string prefix-swap — the mapping crosses filesystem
    namespaces, so os.path.abspath (which would prepend the local drive/cwd) must not touch it."""
    inner, host = os.environ.get("AEON_MODELS_DIR"), os.environ.get("AEON_MODELS_HOST_DIR")
    q = p.replace("\\", "/")
    if inner and host:
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


def build_serve(engine_id: str, *, local_dir: str, alias: str, port: int, ctx: int,
                quant: str | None = None, image: str | None = None,
                plat: dict | None = None) -> dict:
    """The engine-specific serve recipe. Containerized engines -> a `docker run` argv (host
    networking, weights mounted read-only at /model, fixed container name so cleanup is always
    possible). MLX -> the bare-metal command. `image` overrides the catalog image (custom
    container); the override is RECORDED so the result still replicates."""
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
                "setup": "pip install mlx-lm"}

    docker = ["docker", "run", "--rm", "--name", SERVE_CONTAINER, "--network", "host",
              *_gpu_flags(engine_id, plat), "-v", f"{_host_path(local_dir)}:/model:ro"]
    if e["style"] == "vllm":
        flags = ["--served-model-name", alias, "--host", "0.0.0.0", "--port", str(port),
                 "--max-model-len", str(ctx)]
        if quant:
            flags += ["--quantization", str(quant)]
        cmd = docker + ["--entrypoint", "vllm", img, "serve", "/model"] + flags
        srv = {"flags": flags}                          # vllm-style flags: the repro card renders these
    elif e["style"] == "sglang":
        cmd = docker + ["--ipc=host", img, "python3", "-m", "sglang.launch_server",
                        "--model-path", "/model", "--served-model-name", alias,
                        "--host", "0.0.0.0", "--port", str(port), "--context-length", str(ctx)]
        srv = {}
    elif e["style"] == "llama":
        gguf = _find_gguf(local_dir)
        if not gguf:
            raise ValueError("llama.cpp needs GGUF weights; none found in the model dir")
        cmd = docker + [img, "-m", f"/model/{gguf}", "-c", str(ctx),
                        "--host", "0.0.0.0", "--port", str(port), "--alias", alias]
        if plat["accel"] == "cuda":
            cmd += ["-ngl", "999"]
        srv = {}
    else:                                                # pragma: no cover — catalog is closed
        raise ValueError(f"engine '{engine_id}' has no serve builder")

    return {"engine": engine_id, "serve_mode": "docker", "image": img, "command": cmd,
            "docker_run": " ".join(cmd), "container_name": SERVE_CONTAINER,
            "image_overridden": bool(image), **srv}
