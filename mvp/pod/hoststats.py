"""pod/hoststats.py — live host telemetry for the Live view's serve-watch strip.

Answers the operator's "is the model load stalled or just slow?" question with numbers:
GPU VRAM + utilization (nvidia-smi), host RAM (/proc/meminfo — host-wide even inside a
container), CPU load (/proc/loadavg), and the aeon-bench-serve container's state + live
CPU/MEM (docker ps / docker stats through the mounted socket). Sampled on demand per GUI
poll — nothing runs in the background."""
from __future__ import annotations

import os
import subprocess
import time

_CACHE: dict = {"t": 0.0, "v": None}
_TTL = 3.0          # GUI polls every ~4s; docker stats costs ~1.5s — never sample faster


def _run(argv, timeout=6):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _gpu():
    out = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits"], timeout=8)
    if not out:
        return None
    try:
        parts = [x.strip() for x in out.splitlines()[0].split(",")[:3]]

        def num(s):  # GB10/Jetson-class unified memory reports "[N/A]" for VRAM fields
            try:
                return float(s)
            except ValueError:
                return None
        used, total, util = (num(p) for p in parts)
        d = {}
        if util is not None:
            d["util_pct"] = int(util)
        if used is not None and total:
            d["used_gb"], d["total_gb"] = round(used / 1024, 1), round(total / 1024, 1)
        else:
            d["unified"] = True     # VRAM == system RAM: the RAM gauge IS the VRAM gauge
        return d or None
    except Exception:
        return None


def _ram():
    try:
        kv = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                kv[k] = int(v.strip().split()[0])          # kB
        total, avail = kv["MemTotal"], kv.get("MemAvailable", kv["MemTotal"])
        return {"used_gb": round((total - avail) / 1048576, 1),
                "total_gb": round(total / 1048576, 1)}
    except Exception:
        return None


def _load():
    try:
        with open("/proc/loadavg") as f:
            one = float(f.read().split()[0])
        return {"load1": round(one, 1), "ncpu": os.cpu_count() or 1}
    except Exception:
        return None


def _serve_container():
    status = _run(["docker", "ps", "--filter", "name=aeon-bench-serve",
                   "--format", "{{.Status}}"])
    if not status:
        return {"running": False}
    d = {"running": True, "status": status.splitlines()[0]}
    st = _run(["docker", "stats", "--no-stream", "--format",
               "{{.CPUPerc}}|{{.MemUsage}}", "aeon-bench-serve"], timeout=10)
    if st and "|" in st:
        cpu, mem = st.splitlines()[0].split("|", 1)
        d["cpu"], d["mem"] = cpu.strip(), mem.split("/")[0].strip()
    return d


# Live ENGINE metrics for the racing dash: scraped from the serve engine's own Prometheus
# endpoint (:8000/metrics on the bench contract port). Aggregate tok/s is the DELTA of the
# generation_tokens_total counter between two samples — the true engine-wide throughput across
# every concurrent stream, exactly what the perf grid measures but live. Names verified on
# aeon-vllm-ultimate 0.24: vllm:generation_tokens_total / prompt_tokens_total /
# num_requests_running / num_requests_waiting (labels summed).
_ENGINE_LAST = {"t": 0.0, "gen": None, "prompt": None}


def _prom_total(text: str, name: str):
    """Sum a Prometheus metric across its label sets; None if absent."""
    total, found = 0.0, False
    prefix = name + "{"
    for ln in text.splitlines():
        if ln.startswith(prefix) or ln.startswith(name + " "):
            try:
                total += float(ln.rsplit(None, 1)[1])
                found = True
            except (ValueError, IndexError):
                pass
    return total if found else None


def _engine_metrics():
    """{gen_tps, prompt_tps, running, waiting} from the live engine, or None when no engine
    is serving (idle pod / non-vLLM engine) — the dash simply hides then."""
    import urllib.request
    port = os.environ.get("AEON_SERVE_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=3) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        _ENGINE_LAST.update(t=0.0, gen=None, prompt=None)   # engine gone: reset the rate window
        return None
    gen = _prom_total(text, "vllm:generation_tokens_total")
    prompt = _prom_total(text, "vllm:prompt_tokens_total")
    running = _prom_total(text, "vllm:num_requests_running")
    waiting = _prom_total(text, "vllm:num_requests_waiting")
    now = time.time()
    out = {"running": int(running) if running is not None else None,
           "waiting": int(waiting) if waiting is not None else None}
    dt = now - _ENGINE_LAST["t"]
    if gen is not None and _ENGINE_LAST["gen"] is not None and 0 < dt < 120 and gen >= _ENGINE_LAST["gen"]:
        out["gen_tps"] = round((gen - _ENGINE_LAST["gen"]) / dt, 1)
    if prompt is not None and _ENGINE_LAST["prompt"] is not None and 0 < dt < 120 and prompt >= _ENGINE_LAST["prompt"]:
        out["prompt_tps"] = round((prompt - _ENGINE_LAST["prompt"]) / dt, 1)
    _ENGINE_LAST.update(t=now, gen=gen, prompt=prompt)
    return out


def sample() -> dict:
    """One telemetry snapshot, cached _TTL seconds so parallel GUI polls don't stampede."""
    now = time.time()
    if _CACHE["v"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["v"]
    v = {"gpu": _gpu(), "ram": _ram(), "load": _load(), "serve": _serve_container(),
         "engine": _engine_metrics(), "at": int(now)}
    _CACHE.update(t=now, v=v)
    return v
