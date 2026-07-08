"""pod/jobs.py — POD-ONLY background job manager for GUI-launched benchmark runs.

The pod dashboard (AEON_ROLE=pod) uses this so a user can launch a benchmark from the browser.
It enqueues a job; a single background worker runs ONE `python -m pod.aeon_pod ...` subprocess at
a time (the runner mutates process globals + os.environ, so two in one interpreter would corrupt
each other), streams the subprocess stdout into the job log, and parses stage transitions + the
pod-local run_id so the Live view can be deep-linked.

Secrets (target API key / HF token) are looked up server-side (db.get_secret) and passed to the
subprocess via ENV — never on argv (no `ps` leak), never logged, never returned to a client.
"""
from __future__ import annotations

import collections
import json as _json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

_MVP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # .../mvp
POD_DB = os.environ.get("AEON_DB") or os.path.expanduser("~/.aeon/pod.db")
MOTHERSHIP = os.environ.get("AEON_MOTHERSHIP", "https://aeon-bench.com")
HARDWARE = os.environ.get("AEON_HARDWARE") or None

# Optional host-configured verified-run launcher: a JSON argv list (e.g. the DGX docker + DFlash +
# integrity-verify + run_attested script) that reads AEON_HF_LINK / AEON_DIFFICULTY / AEON_MOTHERSHIP
# from env. If unset, the verified flow uses the builtin `aeon_pod --hf-link` (run_controlled +
# derive_recipe), which needs a serve engine on PATH. Host-config, NOT browser-supplied → no RCE.
try:
    VERIFIED_CMD = _json.loads(os.environ["AEON_VERIFIED_CMD"]) if os.environ.get("AEON_VERIFIED_CMD") else None
    if VERIFIED_CMD is not None and not isinstance(VERIFIED_CMD, list):
        VERIFIED_CMD = None
except Exception:
    VERIFIED_CMD = None

_LOCK = threading.Lock()
_JOBS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_Q: "queue.Queue[str]" = queue.Queue()
_worker_started = False

# (substring in a stdout line -> coarse stage). Scanned in order; the LAST match on a line wins,
# so later stages override earlier ones when a line contains several markers.
_STAGES = [
    ("HF link ->", "resolving"),
    ("pulling weights", "pulling"),
    ("verified:", "verifying"),
    ("VERIFICATION FAILED", "verify_failed"),
    ("launching engine", "serving"),
    ("engine ready", "serving"),
    ("run_id=", "benchmarking"),
    ("benchmarking", "benchmarking"),
    ("controlled suite:", "submitting"),
    ("local result:", "submitting"),
    ("submit (", "submitting"),
    ("submit ->", "submitting"),
    # dimension markers (after the submit markers so they win on their announce lines)
    ("ARENA generation", "arena"),
    ("harness ", "harness"),
    ("(vision suite", "vision"),
    ("(audio suite", "audio"),
    ("PERF grid", "perf"),
]

_PUBLIC = ("id", "kind", "status", "stage", "stages", "serve_phase", "model", "hf_link", "base_url",
           "difficulty", "preset", "run_id", "created_at", "updated_at", "error", "hint", "returncode")

# Engine-startup landmarks (vLLM startup chatter streamed into the job log) -> a live
# SERVE PHASE, so the 4-5 minute model load is visible instead of a silent 'serving' gap.
_SERVE_MARKS = (
    ("Starting to load model", "loading weights"),
    ("Loading safetensors checkpoint shards", "loading weights"),
    ("torch.compile", "compiling model"),
    ("Capturing CUDA graph", "capturing CUDA graphs"),
    ("GPU KV cache size", "allocating KV cache"),
    ("Available KV cache memory", "allocating KV cache"),
    ("init engine", "initializing engine"),
    ("Application startup complete", "engine up — readiness probe"),
    ("engine ready; served", "ready"),
)
_SHARD_PCT = re.compile(r"checkpoint shards:\s*(\d{1,3})%")


def _now():
    return time.time()


def _job_public(j):
    return {k: j.get(k) for k in _PUBLIC}


def list_jobs(limit=30):
    with _LOCK:
        js = list(_JOBS.values())[-limit:][::-1]
        return [_job_public(j) for j in js]


def get_job(job_id):
    with _LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return None
        d = _job_public(j)
        d["log"] = list(j["log"])
        return d


def stop_job(job_id):
    with _LOCK:
        j = _JOBS.get(job_id)
        proc = j.get("_proc") if j else None
    if not j:
        return False
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    _set(j, status="stopped", stage="stopped", error="stopped by user")
    return True


def _set(j, **kw):
    with _LOCK:
        j.update(kw)
        j["updated_at"] = _now()


def _mk_job(kind, *, argv, env, model=None, hf_link=None, base_url=None, difficulty=None,
            preset=None, serve_flags=None):
    jid = uuid.uuid4().hex[:12]
    j = {"id": jid, "kind": kind, "status": "queued", "stage": "queued",
         "model": model, "hf_link": hf_link, "base_url": base_url, "difficulty": difficulty,
         "preset": preset, "serve_flags": serve_flags,   # for the failure diagnostician
         "run_id": None, "created_at": _now(), "updated_at": _now(), "error": None,
         "hint": None,
         "returncode": None, "log": collections.deque(maxlen=500), "_argv": argv, "_env": env,
         "_proc": None}
    with _LOCK:
        _JOBS[jid] = j
    _Q.put(jid)
    _ensure_worker()
    return jid


def _ensure_worker():
    global _worker_started
    with _LOCK:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker, daemon=True, name="aeon-pod-jobs").start()


def _worker():
    while True:
        jid = _Q.get()
        try:
            _run_job(jid)
        except Exception as e:                 # never let the worker die
            with _LOCK:
                j = _JOBS.get(jid)
            if j:
                _set(j, status="error", stage="error", error=str(e))
        finally:
            _Q.task_done()
            _maybe_restore_after_queue()


def _maybe_restore_after_queue():
    """QUEUE-SPANNING restore: queue-managed benches (AEON_QUEUE_MANAGED) never restart the
    host containers they paused — the paused.json ledger persists across jobs so back-to-back
    queued benches don't reload the production server between runs. Once the LAST job finishes
    (queue drained), restore the host to its original state in one pass. A job enqueued in the
    tiny race window simply re-pauses — correct either way."""
    if not _Q.empty():
        return
    try:
        from . import recover
        for act in recover.restore_paused():
            print(f"[jobs][queue-drained] {act}", flush=True)
    except Exception:
        pass


def _run_job(jid):
    with _LOCK:
        j = _JOBS[jid]
        argv = j.pop("_argv", None)
        env = j.pop("_env", None)
    if j.get("status") == "stopped" or not argv:  # stopped before it started
        return
    _set(j, status="running", stage="starting")
    proc = subprocess.Popen(argv, cwd=_MVP, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    _set(j, _proc=proc)
    for line in proc.stdout:                    # blocks THIS worker thread (one job at a time — intended)
        line = line.rstrip("\n")
        with _LOCK:
            j["log"].append(line)
        if "run_id=" in line and not j.get("run_id"):
            try:
                rid = line.split("run_id=", 1)[1].strip().split()[0]
                _set(j, run_id=rid)
            except Exception:
                pass
        # structured per-dimension progress: "[pod][stage] <name> <done>/<total>" lines are
        # emitted by aeon_pod at every dimension (text/arena/harness:<id>/vision/audio/perf-cN)
        if line.startswith("[pod][stage] "):
            try:
                name, frac = line[13:].rsplit(" ", 1)
                dn, tt = frac.split("/")
                with _LOCK:
                    sts = j.setdefault("stages", [])
                    cur = next((s for s in sts if s["name"] == name), None)
                    if not cur:
                        cur = {"name": name, "done": 0, "total": int(tt)}
                        sts.append(cur)
                    cur["done"], cur["total"] = int(dn), int(tt)
                    j["updated_at"] = _now()
            except Exception:
                pass
            continue
        stage = None
        for sub, st in _STAGES:
            if sub in line:
                stage = st
        if stage:
            _set(j, stage=stage)
        # engine-startup visibility: vLLM's own startup lines (streamed through the serve
        # subprocess) become a live serve_phase + a weight-loading % bar, so the multi-minute
        # model load reads as PROGRESS instead of a silent 'serving' stage.
        for sub, ph in _SERVE_MARKS:
            if sub in line:
                _set(j, serve_phase=ph)
        m = _SHARD_PCT.search(line)
        if m:
            pct = max(0, min(100, int(m.group(1))))
            with _LOCK:
                sts = j.setdefault("stages", [])
                cur = next((s for s in sts if s["name"] == "load-weights"), None)
                if not cur:
                    cur = {"name": "load-weights", "done": 0, "total": 100}
                    sts.append(cur)
                cur["done"] = max(cur["done"], pct)   # tqdm redraws can repeat lower %s
                j["updated_at"] = _now()
    proc.wait()
    rc = proc.returncode
    if j.get("status") == "stopped":
        return
    ok = rc == 0
    final_stage = "done" if ok else (j.get("stage") if j.get("stage") == "verify_failed" else "error")
    hint = None
    if not ok:
        try:
            from pod import diagnostics
            hint = diagnostics.diagnose(j.get("log") or [], custom_flags=j.get("serve_flags"))
        except Exception:
            hint = None
    _set(j, status="done" if ok else "error", returncode=rc, stage=final_stage,
         error=None if ok else _tail_error(j, rc), hint=hint)


def _tail_error(j, rc):
    # surface the last non-empty log line (usually the SystemExit / traceback summary) with the code
    for line in reversed(j["log"]):
        if line.strip():
            return f"exit {rc}: {line.strip()[:240]}"
    return f"exit {rc}"


# ---- job builders (secrets injected into env, never argv) --------------------------------------

def _base_env(extra=None):
    env = os.environ.copy()
    env["AEON_ROLE"] = "pod"
    env["AEON_DB"] = POD_DB
    env.pop("AEON_DB_URL", None)               # pods are LOCAL SQLite, never the mothership PG
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def submit_endpoint(base_url, model, *, difficulty=None, category=None, preset=None,
                    api_key_name=None, engine=None, perf_max_conc=None, concurrency=None):
    """Flow A — benchmark an already-running OpenAI-compatible endpoint (self-reported)."""
    from aeon import db
    argv = [sys.executable, "-m", "pod.aeon_pod", "--target", base_url, "--model", model,
            "--mothership", MOTHERSHIP]
    if preset:                                  # resolved to the underlying knobs in aeon_pod.main()
        argv += ["--preset", preset]
    if difficulty:
        argv += ["--difficulty", difficulty]
    if category:
        argv += ["--category", category]
    if engine:
        argv += ["--engine", engine]
    if perf_max_conc:
        argv += ["--perf-max-conc", str(perf_max_conc)]
    if concurrency:                             # unset = aeon_pod's default (--concurrency 0 = auto)
        argv += ["--concurrency", str(concurrency)]
    if HARDWARE:
        argv += ["--hardware", HARDWARE]
    extra = {}
    if api_key_name:
        extra["AEON_API_KEY"] = db.get_secret(api_key_name)   # aeon_pod's --api-key defaults to this env
    return _mk_job("endpoint", argv=argv, env=_base_env(extra),
                   model=model, base_url=base_url, difficulty=difficulty, preset=preset)


def submit_verified(hf_link, *, difficulty=None, category=None, preset=None,
                    hf_token_name=None, engine=None, port=None, perf_max_conc=None, concurrency=None,
                    local_dir=None, engine_image=None, serve_url=None, serve_flags=None,
                    drafter_hf=None, max_tokens=None, pause_all=None, restore_paused=None,
                    arena_per_kind=None):
    """Flow B — verified HF run: pull -> integrity-verify -> serve -> bench -> submit ATTESTED.
    Uses the host-configured launcher (AEON_VERIFIED_CMD, e.g. DGX docker+DFlash) when present,
    else the builtin single-process controlled flow (needs a serve engine on PATH).

    `preset` ('comprehensive' | 'hard-bench') is a one-shot bundle resolved to the underlying
    knobs inside aeon_pod.main(): comprehensive turns everything on (all harnesses + vision +
    audio + arena + perf); hard-bench runs the hard,expert tiers through every harness only."""
    from aeon import db
    # Every launch's knobs become a reusable TEMPLATE (token NAME only — never the value), so
    # the Run form can be prefilled from a prior run and relaunched with one tweak. Best-effort:
    # template bookkeeping must never block a launch.
    try:
        db.save_launch("verified", hf_link, {
            "hf_link": hf_link, "difficulty": difficulty, "category": category, "preset": preset,
            "hf_token_name": hf_token_name, "engine": engine, "port": port,
            "perf_max_conc": perf_max_conc, "concurrency": concurrency, "local_dir": local_dir,
            "engine_image": engine_image, "serve_url": serve_url, "serve_flags": serve_flags,
            "drafter_hf": drafter_hf, "max_tokens": max_tokens,
            "pause_all": pause_all, "restore_paused": restore_paused,
            "arena_per_kind": arena_per_kind})
    except Exception:
        pass
    extra = {}
    if hf_token_name:                           # gated/private repos: token authenticates ref+download
        tok = db.get_secret(hf_token_name)
        extra["HF_TOKEN"] = tok
        extra["HUGGING_FACE_HUB_TOKEN"] = tok
    # CLEAR-HOST mode: stop every non-pod container before serving (GUI 'stop other
    # containers'); restore_paused=False leaves them stopped after the bench.
    if pause_all:
        extra["AEON_PAUSE_ALL"] = "1"
    if restore_paused is False:
        extra["AEON_RESTORE_PAUSED"] = "0"
    # QUEUE-MANAGED: the bench itself never restores what it paused — the paused.json
    # ledger accumulates across queued jobs and _maybe_restore_after_queue() restores the
    # host in one pass when the queue drains (no prod-server reload between queued runs).
    extra["AEON_QUEUE_MANAGED"] = "1"
    # An engine/local-dir/serve-url selection means the user chose a SPECIFIC serve config in the
    # GUI — honor it via the builtin flow even when a host launcher exists (the launcher owns only
    # the host's default serve, e.g. the DGX aeon-vllm-ultimate recipe).
    use_host_launcher = VERIFIED_CMD and not (engine or local_dir or engine_image or serve_url
                                              or serve_flags or drafter_hf)
    if use_host_launcher:                       # host launcher owns serving (recipe = pod config, not argv)
        argv = list(VERIFIED_CMD)               # the launcher reads these from env (not browser argv)
        extra.update({"AEON_HF_LINK": hf_link, "AEON_DIFFICULTY": difficulty or "",
                      "AEON_CATEGORY": category or "", "AEON_PRESET": preset or "",
                      "AEON_MOTHERSHIP": MOTHERSHIP})
        if perf_max_conc:                       # aeon_pod honors this env as its --perf-max-conc default
            extra["AEON_PERF_MAX_CONC"] = str(perf_max_conc)
        if concurrency:                         # unset = auto; aeon_pod honors AEON_CONCURRENCY
            extra["AEON_CONCURRENCY"] = str(concurrency)
        if arena_per_kind is not None:          # arena sweep breadth; aeon_pod honors this env
            extra["AEON_ARENA_PER_KIND"] = str(arena_per_kind)
    else:                                       # builtin: run_controlled (derive_recipe / generic vllm)
        argv = [sys.executable, "-m", "pod.aeon_pod", "--hf-link", hf_link, "--mothership", MOTHERSHIP]
        if preset:                              # resolved to the underlying knobs in aeon_pod.main()
            argv += ["--preset", preset]
        if difficulty:
            argv += ["--difficulty", difficulty]
        if category:
            argv += ["--category", category]
        if engine:
            argv += ["--engine", engine]
        if engine_image:                        # custom container image (recorded with the run)
            argv += ["--engine-image", engine_image]
        if local_dir:                           # hash-validated in place — no re-download, never deleted
            argv += ["--local-dir", local_dir]
        if serve_url:                           # operator-started serve (macOS/MLX bare-metal path)
            argv += ["--serve-url", serve_url]
        if serve_flags:                         # recipe tuning: JSON list merged into the serve cmd
            argv += ["--serve-flags", _json.dumps(serve_flags)]
        if drafter_hf:                          # DFlash drafter card: validated + mounted at /drafter
            argv += ["--drafter-hf", drafter_hf]
        if perf_max_conc:
            argv += ["--perf-max-conc", str(perf_max_conc)]
        if concurrency:                         # unset = aeon_pod's default (--concurrency 0 = auto)
            argv += ["--concurrency", str(concurrency)]
        if max_tokens:                          # per-answer TOKEN BUDGET (unset = pod default 2048)
            argv += ["--max-tokens", str(max_tokens)]
        if arena_per_kind is not None:          # arena sweep breadth (prompts per kind; 0 disables)
            argv += ["--arena", str(arena_per_kind)]
        if HARDWARE:
            argv += ["--hardware", HARDWARE]
        if port:
            argv += ["--port", str(port)]
    return _mk_job("verified", argv=argv, env=_base_env(extra),
                   model=hf_link, hf_link=hf_link, difficulty=difficulty, preset=preset,
                   serve_flags=serve_flags)
