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
        used, total, util = [float(x.strip()) for x in out.splitlines()[0].split(",")[:3]]
        return {"used_gb": round(used / 1024, 1), "total_gb": round(total / 1024, 1),
                "util_pct": int(util)}
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


def sample() -> dict:
    """One telemetry snapshot, cached _TTL seconds so parallel GUI polls don't stampede."""
    now = time.time()
    if _CACHE["v"] is not None and now - _CACHE["t"] < _TTL:
        return _CACHE["v"]
    v = {"gpu": _gpu(), "ram": _ram(), "load": _load(), "serve": _serve_container(),
         "at": int(now)}
    _CACHE.update(t=now, v=v)
    return v
