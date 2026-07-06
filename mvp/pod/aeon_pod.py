"""aeon-pod — run the AEON Bench suite against a LOCAL model and submit results upstream.

This is what a user runs on their own hardware (e.g. a DGX Spark serving a local model via
vLLM / llama.cpp / Ollama / LM Studio). It:
  1. benchmarks the model at --target with the AEON suite (into a pod-local SQLite dashboard),
  2. captures the hardware + runtime-engine profile,
  3. submits the ed25519-signed results bundle to the mothership (--mothership) over the
     enrolled device-key channel (see aeon_submit).

Local weights => target_class 'local_weights' (can later reach orchestrated/attested). The
mothership stores the submission 'self_reported' until it re-generates / hardware-attests it.

    python -m pod.aeon_pod --target http://DGX:8000/v1 --model "Ornith-35B-A3B-AEON-..." \
        --mothership http://localhost:8090 --engine vllm --hardware "NVIDIA DGX Spark GB10 128GB"
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid

_MVP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../mvp
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)


def _gpu_desc(gpu_line):
    """One nvidia-smi csv line -> a human GPU label:
    'NVIDIA GeForce RTX 5090, 32607 MiB, 575.x' -> 'RTX 5090 32GB' (GB10 keeps the Spark name)."""
    parts = [p.strip() for p in gpu_line.split(",")]
    name = parts[0]
    if "GB10" in name.upper():
        return "DGX Spark (GB10)"              # unified memory: nvidia-smi reports [N/A] anyway
    short = name.replace("NVIDIA ", "").replace("GeForce ", "")
    mib = "".join(ch for ch in (parts[1] if len(parts) > 1 else "") if ch.isdigit())
    return f"{short} {round(int(mib) / 1024)}GB" if mib else short


def _apple_label():
    """Apple Silicon label via ONE fast sysctl (never system_profiler — too slow for startup):
    'MacBook Pro M4 48GB' when hw.model resolves a marketing family, else 'Apple M4 48GB'."""
    try:
        r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string", "hw.memsize", "hw.model"],
                           capture_output=True, text=True, timeout=2)
        chip, mem, model = ([l.strip() for l in r.stdout.splitlines()] + ["", "", ""])[:3]
    except Exception:
        return None
    gb = f" {round(int(mem) / (1024 ** 3))}GB" if mem.isdigit() else ""
    fams = {"MacBookPro": "MacBook Pro", "MacBookAir": "MacBook Air", "Macmini": "Mac mini",
            "MacStudio": "Mac Studio", "MacPro": "Mac Pro", "iMac": "iMac"}
    fam = next((v for k, v in fams.items() if model.startswith(k)), None)
    if fam and chip.startswith("Apple "):
        return f"{fam} {chip[len('Apple '):]}{gb}"        # 'MacBook Pro M4 48GB'
    return f"{chip}{gb}".strip() or None                  # 'Apple M4 48GB' fallback


def _detect_label(prof):
    """Canonical human hardware label from the DETECTED profile (never the operator's claim)."""
    gpus = prof.get("gpus") or []
    if gpus:
        descs = [_gpu_desc(g) for g in gpus]
        n = len(descs)
        if len(set(descs)) > 1:
            return ", ".join(descs)                       # mixed GPUs -> comma list
        d = descs[0]
        if "DGX Spark" in d:                              # keep the established Spark naming
            mult = {1: "single", 2: "dual", 3: "triple", 4: "quad"}.get(n, f"{n}x")
            return f"{mult} {d}"
        return d if n == 1 else f"{n}× {d}"               # identical GPUs -> '2× RTX 5090 32GB'
    if "aarch64" in (prof.get("machine") or "") and os.path.exists("/etc/nv_tegra_release"):
        return "single DGX Spark (GB10)"
    if platform.system() == "Darwin" and (prof.get("machine") or "").startswith("arm"):
        lbl = _apple_label()
        if lbl:
            return lbl
    m = prof.get("machine") or "unknown"
    return f"{m} (CPU)"                                   # no accelerator found


def _hardware_profile(label=None):
    prof = {"label": label, "platform": platform.platform(), "machine": platform.machine(),
            "cpu_count": os.cpu_count()}
    try:                                       # best-effort GPU profile
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8)
        if out.returncode == 0:
            prof["gpus"] = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
    except Exception:
        pass
    # DETECTED canonical hardware label — pulled from the machine the bench actually ran on (not
    # the operator's claim), so viewers see "single DGX Spark" / "RTX 5090 32GB" / "Apple M4 48GB".
    try:                                       # detection must NEVER crash a bench
        prof["detected_label"] = _detect_label(prof)
    except Exception:
        prof["detected_label"] = None          # unknown — the claimed label stands, marked unverified
    return prof


def run_pod(target, model, mothership, *, api_key=None, engine=None, hardware=None,
            board="text", suite_id=None, key_path=None, hf_repo=None, limit=None, difficulty=None,
            category=None, max_tokens=2048, temperature=0.0, judge=None, judge_url=None, judge_key=None,
            concurrency=1):
    # Pod state is LOCAL SQLite (its own job dashboard) — never the mothership DB.
    os.environ.pop("AEON_DB_URL", None)
    os.environ.setdefault("AEON_DB", os.path.expanduser("~/.aeon/pod.db"))
    os.makedirs(os.path.dirname(os.environ["AEON_DB"]), exist_ok=True)

    from aeon import db, runner, scoring
    from aeon import suite as suite_mod
    from pod.aeon_submit import Pod

    if difficulty:                             # only the named tiers (e.g. "hard" or "hard,expert")
        want = {d.strip() for d in difficulty.split(",") if d.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("difficulty") in want]
        if not suite_id:                       # group tier runs separately from the comprehensive suite
            suite_id = suite_mod.SUITE_ID + "-" + "-".join(sorted(want))
    if category:                               # optionally scope to a comma-list of categories (e.g. 'codegen')
        cats = {c.strip() for c in category.split(",") if c.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("category") in cats]
    if limit:                                  # quick subset for a fast smoke
        suite_mod.CASES = suite_mod.CASES[:limit]

    rid = uuid.uuid4().hex[:10]
    print(f"[pod] run_id={rid}", flush=True)   # the GUI job manager correlates the pod.db run by this
    n = len(suite_mod.CASES)
    print(f"[pod] benchmarking {model}  @ {target}   ({n} cases)")
    done = {"i": 0}

    def cb(cid, score, status):
        done["i"] += 1
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  {done['i']:3d}/{n}  {cid:24s} {status:13s} {s}")

    params = {"temperature": temperature, "max_tokens": max_tokens,   # headroom for reasoning models
              "concurrency": concurrency}
    # Judge policy: a frontier model OR deterministic-only (never self-judge). With no --judge,
    # subjective Tier-1 cases are left unscored; deterministic cases always score.
    runner.run_benchmark(rid, model, target, api_key=api_key, params=params, progress_cb=cb,
                         judge_model=judge, judge_url=judge_url, judge_key=judge_key)

    run = db.get_run(rid)
    results = [{
        "case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
        "status": x["status"], "score": x["score"], "creativity": x.get("creativity"),
        "raw_output": db.result_output(x), "evidence": x.get("evidence") or {},
        "speed": x.get("speed") or {},
    } for x in run["results"]]

    scored = [x["score"] for x in results if isinstance(x["score"], float)]
    mean = sum(scored) / len(scored) if scored else 0.0
    print(f"[pod] local result: mean {mean:.3f} over {len(scored)} scored / {n} cases")

    env = {"hardware": _hardware_profile(hardware), "engine": {"name": engine}, "runner": "aeon-pod"}
    pod = Pod(mothership, key_path or os.path.expanduser("~/.aeon/device_key.pem"))
    st, r = pod.run_and_submit(model, suite_id or suite_mod.SUITE_ID, results, board=board,
                               suite_hash=suite_mod.suite_hash(), environment=env,
                               target_class="local_weights", hf_repo=hf_repo, engine=engine,
                               judge_model=judge)   # frontier judge or None — NEVER the model itself
    print(f"[pod] submit -> {mothership}: HTTP {st}  {json.dumps(r)[:400]}")
    return st, r


def _env_int(name, default):
    """Integer env override (>=1) or `default` — the DGX launcher passes knobs via env."""
    try:
        return max(1, int(os.environ.get(name, "")))
    except (TypeError, ValueError):
        return default


def _auto_concurrency():
    """Capacity-aware default for --concurrency (cases run through the served model at once).
    Over-subscribing a vLLM serve is SAFE — it just queues beyond its own max-num-seqs — so we bias
    HIGH whenever real accelerator memory is present and only fall back to single-stream when NO
    GPU/accelerator is detected. An explicit --concurrency N (>0) always wins."""
    import subprocess
    gb = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=6)
        vals = [int(x) for x in r.stdout.split() if x.strip().isdigit()]
        if vals:
            gb = max(vals) / 1024.0                          # largest GPU, MiB -> GiB
    except Exception:
        pass
    if gb is None:                                           # GB10/unified reports N/A to nvidia-smi
        try:
            from pod import modelhost
            if modelhost.is_dgx_spark():
                gb = 128.0                                   # DGX Spark unified memory
        except Exception:
            pass
    if gb is None:
        return 1                                             # no accelerator found -> single stream
    # tier table: unified/DGX 128GB + VRAM>=96 -> 24, >=48 -> 16, >=24 -> 12, >=16 -> 8, else 4
    for thr, c in ((96, 24), (48, 16), (24, 12), (16, 8)):
        if gb >= thr:
            return c
    return 4


def _scale_http_timeout(concurrency):
    """Scale the per-request HTTP timeout with concurrency: N concurrent streams time-slice the
    server's decode, so each INDIVIDUAL request takes longer even though total wall time drops —
    at the old fixed timeout a c=24 run would spuriously kill healthy long generations.
    effective = base(180s) * ceil(conc/4), capped at 1800s, exported via AEON_HTTP_TIMEOUT
    (honored by aeon.targets.OpenAITarget) so every target this run builds inherits it without
    threading a knob through each constructor. An operator's explicit AEON_HTTP_TIMEOUT wins."""
    if os.environ.get("AEON_HTTP_TIMEOUT"):
        return int(os.environ["AEON_HTTP_TIMEOUT"] or 180)
    eff = min(1800, 180 * max(1, (int(concurrency) + 3) // 4))
    os.environ["AEON_HTTP_TIMEOUT"] = str(eff)
    return eff


def _skip_short_ctx_harnesses(harness_ids, recipe):
    """Hermes REQUIRES a >=64K serve. When a short-context serve was explicitly allowed
    (AEON_ALLOW_SHORT_CTX=1 in modelhost.derive_recipe), drop hermes from the harness pass
    rather than burn a doomed run; the other harnesses still measure."""
    from pod import modelhost
    ctx = (recipe or {}).get("context_len")
    if not harness_ids or not ctx or int(ctx) >= modelhost.BENCH_MAX_CTX:
        return harness_ids
    kept = [h for h in harness_ids if h != "hermes"]
    if len(kept) != len(harness_ids):
        print(f"[pod] hermes harness SKIPPED: serve context {ctx} < {modelhost.BENCH_MAX_CTX} "
              "(Hermes floor; short-context serve allowed via AEON_ALLOW_SHORT_CTX)")
    return kept


# ---- controlled HF-pull flow (the ONLY path that earns an attested / globally-ranked run) ----

def _collect_results(rid):
    """Snapshot the pod-local results for a run as submit-ready dicts (cumulative so far)."""
    from aeon import db
    run = db.get_run(rid)
    if not run:
        return []
    return [{
        "case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
        "status": x["status"], "score": x["score"], "creativity": x.get("creativity"),
        "raw_output": db.result_output(x), "evidence": x.get("evidence") or {},
        "speed": x.get("speed") or {},
    } for x in run["results"]]


def _bench_and_results(model, target, *, api_key=None, max_tokens=2048, temperature=0.0,
                       judge=None, judge_url=None, judge_key=None, checkpoint=None, checkpoint_every=8,
                       retry_max_tokens=None, concurrency=1,
                       hf_repo=None, trust_tier="self_reported", model_verified=None):
    """Benchmark `model` at `target` into the pod-local DB; return (rid, results, mean). If a
    `checkpoint(results)` callback is given, it's called every `checkpoint_every` cases with the
    CUMULATIVE results-so-far — for incremental submission so a mid-run kill loses nothing."""
    from aeon import runner
    from aeon import suite as suite_mod
    rid = uuid.uuid4().hex[:10]
    print(f"[pod] run_id={rid}", flush=True)   # the GUI job manager correlates the pod.db run by this
    n = len(suite_mod.CASES)
    done = {"i": 0}

    def cb(cid, score, status):
        done["i"] += 1
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  {done['i']:3d}/{n}  {cid:24s} {status:13s} {s}")
        if checkpoint and done["i"] % checkpoint_every == 0:
            try:
                checkpoint(_collect_results(rid))
            except Exception as e:
                print(f"[pod] checkpoint submit failed (non-fatal, retried next batch): {e}")

    params = {"temperature": temperature, "max_tokens": max_tokens,
              "retry_max_tokens": retry_max_tokens, "retries": 1, "concurrency": concurrency}
    runner.run_benchmark(rid, model, target, api_key=api_key, params=params, progress_cb=cb,
                         judge_model=judge, judge_url=judge_url, judge_key=judge_key,
                         hf_repo=hf_repo, trust_tier=trust_tier, model_verified=model_verified)
    results = _collect_results(rid)
    scored = [x["score"] for x in results if isinstance(x["score"], float)]
    mean = sum(scored) / len(scored) if scored else 0.0
    return rid, results, mean


def _vision_and_submit(pod, repo, target, alias, *, env, provenance, max_tokens=256, temperature=0.0):
    """Run the VISION suite on the served (multimodal) model into pod.db, then submit ATTESTED
    (board='vision'). The capability probe inside run_vision_benchmark records `capability_absent`
    and we skip submission for models with no vision — so a text-only model never gets a bogus
    vision run. Returns (st, r) on submit, or None if the model has no vision."""
    from aeon import db, runner
    from aeon import vision_suite as vs
    rid = uuid.uuid4().hex[:10]
    print(f"[pod] run_id={rid}  (vision suite, {len(vs.CASES)} cases)", flush=True)
    n = len(vs.CASES)
    done = {"i": 0}

    def cb(cid, score, status):
        done["i"] += 1
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  [vision] {done['i']:2d}/{n}  {cid:26s} {status:15s} {s}")

    pr = runner.run_vision_benchmark(rid, alias, target, params={"temperature": temperature,
                                     "max_tokens": max_tokens}, progress_cb=cb)
    if not pr.get("vision_ok"):
        print(f"[pod] vision: model reports NO vision capability ({pr.get('error')}) — not submitting a vision run")
        return None
    run = db.get_run(rid)
    results = [{"case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
                "status": x["status"], "score": x["score"], "creativity": x.get("creativity"),
                "raw_output": db.result_output(x), "evidence": x.get("evidence") or {},
                "speed": x.get("speed") or {}} for x in run["results"]]
    scored = [x["score"] for x in results if isinstance(x["score"], float)]
    mean = sum(scored) / len(scored) if scored else 0.0
    print(f"[pod] vision suite: mean {mean:.3f} over {len(scored)} scored / {n} cases")
    st, r = pod.run_and_submit(repo, vs.SUITE_ID, results, board="vision", suite_hash=vs.suite_hash(),
        environment=env, target_class="hf_pull_controlled", **provenance)
    print(f"[pod] submit (vision) -> HTTP {st}  {json.dumps(r)[:200]}")
    return st, r


def _audio_and_submit(pod, repo, target, alias, *, env, provenance, max_tokens=2048, temperature=0.0):
    """AUDIO suite on the served model -> attested (board='audio'). Probe-gated like vision:
    a model that doesn't accept input_audio records capability_absent and nothing is submitted."""
    from aeon import audio_suite as aus
    from aeon import db, runner
    rid = uuid.uuid4().hex[:10]
    print(f"[pod] run_id={rid}  (audio suite, {len(aus.CASES)} cases)", flush=True)
    n = len(aus.CASES)
    done = {"i": 0}

    def cb(cid, score, status):
        done["i"] += 1
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  [audio] {done['i']:2d}/{n}  {cid:26s} {status:15s} {s}")

    pr = runner.run_audio_benchmark(rid, alias, target, params={"temperature": temperature,
                                    "max_tokens": max_tokens}, progress_cb=cb)
    if not pr.get("audio_ok"):
        print(f"[pod] audio: model does not accept input_audio ({pr.get('transport')}) — not submitting an audio run")
        return None
    results = _collect_results(rid)
    scored = [x["score"] for x in results if isinstance(x["score"], float)]
    mean = sum(scored) / len(scored) if scored else 0.0
    print(f"[pod] audio suite: mean {mean:.3f} over {len(scored)} scored / {n} cases")
    st, r = pod.run_and_submit(repo, aus.SUITE_ID, results, board="audio", suite_hash=aus.suite_hash(),
        environment=env, target_class="hf_pull_controlled", **provenance)
    print(f"[pod] submit (audio) -> HTTP {st}  {json.dumps(r)[:200]}")
    return st, r


def _cap_conc(base, max_conc, extend=False):
    """Cap a concurrency ladder at --perf-max-conc: rungs above the cap drop; with `extend`, a
    cap that isn't itself a standard rung becomes the new top rung (e.g. 24 -> 1/4/8/16/24).
    max_conc None/invalid leaves the ladder as-is; the cap is guarded to >= 1."""
    try:
        mx = max(1, int(max_conc))
    except (TypeError, ValueError):
        return tuple(base)
    levels = [c for c in base if c <= mx]
    if extend and mx not in levels:
        levels.append(mx)
    return tuple(sorted(levels)) or (1,)


def _perf_and_submit(pod, repo, target, alias, *, env, provenance, harness_ids=None,
                     conc_levels=(1, 4, 8, 16, 32), max_tokens=256, max_conc=None):
    """PERFORMANCE grid: direct-to-model across the concurrency ladder x categories (tok/s decode,
    TTFT, PP prefill tok/s), plus per-harness single/concurrent task timing. Submitted as its own
    run (suite aeon-perf-v1, board='perf' so quality boards are untouched)."""
    from pod import perf_grid
    conc_levels = _cap_conc(conc_levels, max_conc, extend=True)
    print(f"[pod] PERF grid: direct conc {conc_levels} x {len(perf_grid.CATEGORIES)} categories", flush=True)
    grid = perf_grid.run_direct_grid(target, alias, conc_levels=conc_levels, max_tokens=max_tokens,
        progress_cb=lambda c, d, tot: print(f"  [perf] c={c}  {d}/{tot}", flush=True) if d in (1, tot) else None)
    rows = perf_grid.to_results(grid)
    for h in (harness_ids or []):
        try:
            from pod import adapters, run_harness2
            ad = adapters.get(h)
            hroot = os.path.join(os.path.expanduser("~/.aeon"), f"perfh-{h}")
            ad.prepare_run(target, alias, hroot)
            import tempfile

            def _runner(prompt, _ad=ad):
                wd = tempfile.mkdtemp(prefix="aeonperf-")
                try:
                    _ad.run_task({"id": "perf.task", "prompt": prompt, "setup_files": {},
                                  "timeout_s": 240}, target, alias, wd, timeout=240)
                finally:
                    shutil.rmtree(wd, ignore_errors=True)

            # full ladder so harness-vs-harness performance is comparable at every level;
            # n_tasks floors at len(CATEGORIES) per level so each prompt TYPE is timed
            ht = perf_grid.run_harness_timing(h, target, alias,
                conc_levels=_cap_conc((1, 4, 8, 16), max_conc), n_tasks=5, runner=_runner)
            rows += perf_grid.to_results(ht)
            ad.cleanup_run()
            print(f"  [perf] harness {h}: " + json.dumps(ht.get("levels", {}))[:160], flush=True)
        except Exception as e:
            print(f"  [perf] harness {h} timing failed (non-fatal): {e}")
    st, r = pod.run_and_submit(repo, perf_grid.SUITE_ID, rows, board="perf",
        environment=env, target_class="hf_pull_controlled", **provenance)
    print(f"[pod] submit (perf {len(rows)} cells) -> HTTP {st}  {json.dumps(r)[:200]}")
    return st, r


def _arena_artifacts(target, alias, *, seed=None, per_kind=2):
    """Game/app/animation artifacts from the served model (part of EVERY benchmark). Seeded so
    every model in a sweep answers the IDENTICAL prompts. Returned for the signed submit bundle."""
    from pod import arena_gen
    print(f"[pod] ARENA generation: {per_kind} per kind (app/game/animation), seed={seed}", flush=True)
    arts = arena_gen.generate_for_model(target, alias, per_kind=per_kind, seed=seed,
        progress_cb=lambda d, tot, it: print(
            f"  [arena] {d}/{tot} {it.get('kind')}/{it.get('prompt_id')}: {'ok' if it.get('ok') else 'FAILED'}", flush=True))
    ok = sum(1 for a in arts if a.get("ok"))
    print(f"[pod] arena: {ok}/{len(arts)} artifacts generated")
    return arts


def _serve(recipe):
    """Launch the inference engine per the recipe (serves the verified weights on the GPU host)."""
    cmd = [str(x) for x in recipe["command"]]
    print(f"[pod] launching engine: {' '.join(cmd)}")
    return subprocess.Popen(cmd)


def _wait_ready(base_url, timeout=1200, interval=4):
    """Poll the OpenAI /models endpoint until the engine is serving; return the served ids."""
    import time
    import urllib.request
    url = base_url.rstrip("/") + "/models"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ids = [m.get("id") for m in json.loads(r.read()).get("data", [])]
            if ids:
                return ids
        except Exception as e:
            last = e
        time.sleep(interval)
    raise SystemExit(f"[pod] engine not ready at {base_url} within {timeout}s ({last})")


def _stop(proc):
    if not proc:
        return
    try:
        proc.terminate(); proc.wait(timeout=20)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_controlled(hf_link, mothership, *, engine=None, hardware=None, board="text",
                   suite_id=None, key_path=None, weights_dir=None, keep_weights=False,
                   port=8000, max_tokens=2048, temperature=0.0, judge=None, judge_url=None,
                   judge_key=None, harness_ids=None, limit=None, serve=True, fast=False, seed=None,
                   per_cell=1, difficulty=None, category=None, vision=True, concurrency=1,
                   local_dir=None, serve_url=None, engine_image=None):
    """Controlled A→B — the ONLY path to a globally-ranked (attested) result:
      pull from HF → hash-verify against HF → serve the verified weights under the harness alias
      → benchmark the served endpoint → run the agentic suite through each harness → sign + submit
      with full provenance (weights hash + recipe + build hash). The mothership re-verifies the
      per-file hashes against HF before it counts. Serving + the real harness CLIs run on the GPU
      host; everything else here is portable.

    `local_dir`   — weights ALREADY on disk: hash-validated against the HF manifest instead of
                    re-downloaded ("good as gold" when the bytes match); never deleted.
    `serve_url`   — an operator-started serve of THOSE validated weights (the macOS/MLX bare-metal
                    path, where the containerized dashboard cannot spawn a host process): the pod
                    validates + benches + signs, and the bare startup recipe is recorded exactly
                    like a docker recipe.
    `engine_image`— custom container image override for the chosen engine (recorded)."""
    os.environ.pop("AEON_DB_URL", None)              # pod state is LOCAL SQLite, never the mothership DB
    os.environ.setdefault("AEON_DB", os.path.expanduser("~/.aeon/pod.db"))
    os.makedirs(os.path.dirname(os.environ["AEON_DB"]), exist_ok=True)

    from aeon import attest
    from aeon import suite as suite_mod
    from pod import modelhost
    from pod.aeon_submit import DEFAULT_KEY, Pod

    bench_seed = None
    if fast:                                    # FAST bench: `per_cell` random cases per (category x difficulty)
        bench_seed = seed or suite_mod.random_seed()
        suite_mod.CASES = suite_mod.sample_fast(bench_seed, per_cell)
        print(f"[pod] FAST bench — seed={bench_seed}  ({len(suite_mod.CASES)} cases, "
              f"{per_cell} per category x difficulty; suite_hash {suite_mod.suite_hash()})")
    elif difficulty:                            # rapid bench: only the named tiers (e.g. "hard,expert")
        want = {d.strip() for d in difficulty.split(",") if d.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("difficulty") in want]
    if category:                                # optionally scope to a comma-list of categories (e.g. 'codegen')
        cats = {c.strip() for c in category.split(",") if c.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("category") in cats]
    if limit:
        suite_mod.CASES = suite_mod.CASES[:limit]

    repo, rev = modelhost.resolve(hf_link)
    print(f"[pod] HF link -> {repo}@{rev}")
    ref = modelhost.fetch_ref(repo, rev)
    print(f"[pod] HF commit {(ref.get('sha') or '?')[:12]} — {len(ref.get('files') or {})} files advertised")

    if local_dir:                                   # weights already on disk: validate, don't re-pull
        local_dir = os.path.abspath(os.path.expanduser(local_dir))
        keep_weights = True                          # NEVER delete a user-supplied model dir
        print(f"[pod] LOCAL weights {local_dir} — hash-validating against {repo}@{rev} (no re-download)")
    else:
        mdl_root = os.environ.get("AEON_MODELS_DIR")           # containerized dashboard: /models volume
        dest = weights_dir or (os.path.join(mdl_root, repo.replace("/", "__")) if mdl_root
                               else os.path.expanduser(f"~/.aeon/models/{repo.replace('/', '__')}"))
        print(f"[pod] pulling weights -> {dest}  (first run can take a while)")
        local_dir = modelhost.pull(repo, ref.get("revision") or rev, dest)

    ver = modelhost.verify(local_dir, ref)
    if not ver["verified"]:
        if not keep_weights:
            shutil.rmtree(local_dir, ignore_errors=True)
        raise SystemExit(f"[pod] WEIGHTS VERIFICATION FAILED for {repo}: "
                         f"mismatches={ver['mismatches'][:5]} — refusing to benchmark unverified weights.")
    print(f"[pod] verified: weights_hash={ver['weights_hash'][:16]}… method={ver['method']} "
          f"({ver['lfs_checked']} LFS-checked / {ver['n_weight_files']} weight files)")

    recipe = modelhost.derive_recipe(local_dir, ref, port=port, engine=engine, image=engine_image)
    alias = recipe["served_alias"]
    print(f"[pod] recipe: {recipe['engine']} ({recipe.get('serve_mode', 'bare')}, "
          f"ctx {recipe.get('context_len')}) -> '{alias}' on :{port}"
          + (f"  [{recipe['reason']}]" if recipe.get("reason") else ""))
    if recipe.get("no_harness"):                     # e.g. MLX: no served-alias contract for harnesses
        print("[pod] harness pass skipped for this engine (no served-alias contract)")
        harness_ids = []
    else:
        harness_ids = _skip_short_ctx_harnesses(harness_ids, recipe)
    if serve_url:                                    # operator-started serve (MLX / LM Studio bare metal)
        serve = False
        print(f"[pod] external serve: benching {serve_url} against the validated weights")
    elif serve and not recipe.get("command"):        # engine can't be pod-launched (e.g. LM Studio app)
        raise SystemExit(f"[pod] engine '{recipe['engine']}' is operator-started — start it with the "
                         "recipe's startup commands, then relaunch with --serve-url <its /v1 URL>")

    # z-lab DFlash drafter auto-discovery (spec decode is LOSSLESS — speed only). Best-effort:
    # any probe/pull failure here falls back to plain decode, never blocking the serve.
    if recipe.get("engine") in ("vllm", "aeon-vllm-ultimate"):
        try:
            drepo = modelhost.discover_dflash(repo)
            if drepo:
                ddir = os.path.expanduser(f"~/.aeon/models/{drepo.replace('/', '__')}")
                print(f"[pod] DFlash drafter found: {drepo} — pulling for speculative decode")
                modelhost.pull(drepo, "main", ddir)
                nst = modelhost.dflash_nst(repo, recipe.get("architecture"))
                modelhost.apply_dflash(recipe, ddir, drepo, nst)
                print(f"[pod] spec-decode enabled: dflash nst={nst} (lossless; speed only)")
        except Exception as e:
            print(f"[pod] DFlash setup failed (non-fatal, plain decode): {e}")

    server = _serve(recipe) if serve else None
    target = serve_url or f"http://127.0.0.1:{port}/v1"
    try:
        # for an operator-started serve (serve_url) we ALSO wait: the user may still be launching it
        ids = _wait_ready(target) if (serve or serve_url) else [alias]
        if recipe.get("alias_from_server") and ids and alias not in ids:
            # bare-metal servers (MLX / LM Studio) name the model themselves — bench under the id
            # the server ACTUALLY reports rather than an alias it would reject
            alias = ids[0]
            recipe["served_alias"] = alias
            print(f"[pod] served id adopted from server: '{alias}'")
        served_ok = alias in ids
        print(f"[pod] engine ready; served = {ids}  (alias present: {served_ok})")

        deployment_manifest = {
            "build_hash": attest.build_hash(), "recipe": recipe,
            "verification": {k: ver[k] for k in ("verified", "method", "weights_hash",
                                                 "revision", "n_weight_files", "lfs_checked")},
            "served_model_check": {"endpoint": target, "served": ids, "alias_present": served_ok},
            "hf": {"repo": repo, "revision": ver["revision"]},
            "hardware": _hardware_profile(hardware),
        }
        env = {"hardware": _hardware_profile(hardware), "engine": {"name": recipe["engine"]},
               "runner": "aeon-pod-controlled"}
        # provenance that travels with EVERY submission from this run (suite + each harness) and
        # lets the mothership re-verify the model identity against HF before it counts as attested.
        provenance = dict(hf_repo=repo, hf_revision=ver["revision"], weights_hash=ver["weights_hash"],
                          weights_per_file=ver["per_file"], recipe=recipe,
                          deployment_manifest=deployment_manifest, bench_seed=bench_seed)
        pod = Pod(mothership, key_path or DEFAULT_KEY)

        # 1) the standard suite through the verified-served model -> the ATTESTED text submission
        _rid, results, mean = _bench_and_results(alias, target, max_tokens=max_tokens,
            temperature=temperature, judge=judge, judge_url=judge_url, judge_key=judge_key,
            concurrency=concurrency,
            hf_repo=repo, trust_tier="attested", model_verified="verified")
        print(f"[pod] controlled suite: mean {mean:.3f} over {len(results)} cases")
        st, r = pod.run_and_submit(repo, suite_id or suite_mod.SUITE_ID, results, board=board,
            suite_hash=suite_mod.suite_hash(), environment=env, target_class="hf_pull_controlled",
            judge_model=judge, **provenance)
        print(f"[pod] submit (suite) -> {mothership}: HTTP {st}  {json.dumps(r)[:300]}")

        # 2) the agentic suite through EACH harness -> the AI-Harness board. Best-effort: an
        #    uninstalled / stubbed harness degrades to per-task harness_error, never aborting.
        if harness_ids:
            from aeon import agentic
            from pod import run_harness
            for h in harness_ids:
                try:
                    hres = run_harness.run_agentic_suite(
                        h, target, alias,
                        progress_cb=lambda c, s, stt: print(f"    [{h}] {c:22s} {stt:13s} {s}"))
                except Exception as e:
                    print(f"[pod] harness {h} could not run: {e}")
                    continue
                disc = hres[0] if hres else {}
                hresults = [{"case_id": x["case_id"], "category": x["category"], "tier": x["tier"],
                             "status": x["status"], "score": x["score"],
                             "raw_output": json.dumps(x.get("transcript") or {})[:4000],
                             "evidence": x.get("metrics") or {}, "speed": {}} for x in hres]
                hst, hr = pod.run_and_submit(repo, agentic.SUITE_ID, hresults, board=board,
                    suite_hash=suite_mod.suite_hash(), environment=env,
                    target_class="hf_pull_controlled", judge_model=judge,
                    harness=disc.get("harness", h), harness_version=disc.get("harness_version"),
                    **provenance)
                print(f"[pod] submit (harness {h} {disc.get('harness_version', '?')}) -> "
                      f"HTTP {hst}  {json.dumps(hr)[:200]}")

        # 3) the VISION suite through the served (multimodal) model -> the VISION board (probe-gated)
        if vision:
            _vision_and_submit(pod, repo, target, alias, env=env, provenance=provenance,
                               max_tokens=max_tokens, temperature=temperature)
        return st, r
    finally:
        _stop(server)
        if recipe.get("container_name") and serve:   # docker-served: make cleanup unconditional —
            subprocess.run(["docker", "rm", "-f", recipe["container_name"]],   # SIGTERM on the client
                           capture_output=True, timeout=60)                    # can strand the container
        if not keep_weights:
            print(f"[pod] removing weights {local_dir} (use --keep-weights to retain)")
            shutil.rmtree(local_dir, ignore_errors=True)


def run_attested(target, modelref_path, mothership, *, hardware=None, board="text", suite_id=None,
                 key_path=None, max_tokens=2048, temperature=0.0, judge=None, judge_url=None,
                 judge_key=None, harness_ids=None, limit=None, difficulty=None, category=None,
                 fast=False, seed=None, per_cell=1, retry_max_tokens=None, concurrency=1, vision=True,
                 arena_per_kind=2, audio=True, perf=False, perf_max_conc=None, harness_only=False):
    """Split-pod path: a `pull` sidecar already PULLED + HASH-VERIFIED the weights (writing
    .aeon-modelref.json) and an engine already SERVES them at --target. Benchmark that endpoint
    and submit ATTESTED, carrying the sidecar's verification (weights_hash + per-file hashes +
    recipe) so the mothership re-verifies against HF and ranks it globally. The single-process
    equivalent is run_controlled()."""
    os.environ.pop("AEON_DB_URL", None)
    os.environ.setdefault("AEON_DB", os.path.expanduser("~/.aeon/pod.db"))
    os.makedirs(os.path.dirname(os.environ["AEON_DB"]), exist_ok=True)

    from aeon import agentic, attest
    from aeon import suite as suite_mod
    from pod import modelhost, run_harness
    from pod.aeon_submit import DEFAULT_KEY, Pod

    bench_seed = None
    if fast:                                    # FAST bench: `per_cell` random cases per (category x difficulty)
        bench_seed = seed or suite_mod.random_seed()
        suite_mod.CASES = suite_mod.sample_fast(bench_seed, per_cell)
        print(f"[pod] FAST bench — seed={bench_seed}  ({len(suite_mod.CASES)} cases, "
              f"{per_cell} per category x difficulty; suite_hash {suite_mod.suite_hash()})")
    elif difficulty:                            # rapid bench: only the named tiers (e.g. "hard,expert")
        want = {d.strip() for d in difficulty.split(",") if d.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("difficulty") in want]
        # Tier-filtered runs are a DIFFERENT test than the comprehensive suite — give them their
        # own suite id (e.g. aeon-suite-v2-hard) so boards group hard runs vs comprehensive runs.
        if not suite_id:
            suite_id = suite_mod.SUITE_ID + "-" + "-".join(sorted(want))
    if category:                                # optionally scope to a comma-list of categories (e.g. 'codegen')
        cats = {c.strip() for c in category.split(",") if c.strip()}
        suite_mod.CASES = [c for c in suite_mod.CASES if c.get("category") in cats]
    if limit:
        suite_mod.CASES = suite_mod.CASES[:limit]

    with open(modelref_path, encoding="utf-8") as f:
        mref = json.load(f)
    repo, rev = mref.get("repo"), mref.get("revision")
    ver = mref.get("verification") or {}
    recipe = mref.get("recipe") or {}
    alias = recipe.get("served_alias") or modelhost.DEFAULT_ALIAS
    if not ver.get("verified"):
        raise SystemExit("[pod] modelref reports weights NOT verified — refusing to submit attested.")
    print(f"[pod] attested submit for {repo}@{(rev or '')[:12]} "
          f"(weights_hash {(ver.get('weights_hash') or '')[:16]}…) serving '{alias}' @ {target}")
    harness_ids = _skip_short_ctx_harnesses(harness_ids, recipe)

    deployment_manifest = {
        "build_hash": attest.build_hash(), "recipe": recipe,
        "verification": {k: ver.get(k) for k in ("verified", "method", "weights_hash",
                                                 "revision", "n_weight_files", "lfs_checked")},
        "served_model_check": {"endpoint": target, "alias": alias},
        "hf": {"repo": repo, "revision": rev}, "hardware": _hardware_profile(hardware),
    }
    env = {"hardware": _hardware_profile(hardware), "engine": {"name": recipe.get("engine")},
           "runner": "aeon-pod-controlled"}
    provenance = dict(hf_repo=repo, hf_revision=rev, weights_hash=ver.get("weights_hash"),
                      weights_per_file=ver.get("per_file") or {}, recipe=recipe,
                      deployment_manifest=deployment_manifest, bench_seed=bench_seed)
    pod = Pod(mothership, key_path or DEFAULT_KEY)
    st, r = 0, {"skipped": "harness_only"}
    if not harness_only:
        # Benchmark LOCALLY into the pod's own pod.db — the POD dashboard reads it live (per case, no
        # network). The mothership only ever receives a COMPLETE, verified run, submitted once here, and
        # shows it once accepted. (No mid-run streaming: the pod owns its data; a killed run's cases stay
        # in pod.db + are visible in the pod's dashboard for the user to re-run or submit.)
        _rid, results, mean = _bench_and_results(alias, target, max_tokens=max_tokens,
            temperature=temperature, judge=judge, judge_url=judge_url, judge_key=judge_key,
            retry_max_tokens=retry_max_tokens, concurrency=concurrency,
            hf_repo=repo, trust_tier="attested", model_verified="verified")
        print(f"[pod] controlled suite: mean {mean:.3f} over {len(results)} cases")

        # ARENA generation (games/apps/animations) is part of EVERY benchmark: generate from the
        # served model NOW and ship the artifacts INSIDE the signed text-run bundle (ingest saves
        # them into the arena on the final commit).
        artifacts = _arena_artifacts(target, alias, seed=bench_seed or suite_mod.SUITE_ID,
                                     per_kind=arena_per_kind) if arena_per_kind else []

        st, r = pod.run_and_submit(repo, suite_id or suite_mod.SUITE_ID, results, board=board,
            suite_hash=suite_mod.suite_hash(), environment=env, target_class="hf_pull_controlled",
            judge_model=judge, artifacts=artifacts, **provenance)
        print(f"[pod] submit (complete verified run + {len(artifacts)} artifacts) -> {mothership}: "
              f"HTTP {st}  {json.dumps(r)[:300]}")

    # AGENTIC through each REAL harness (fresh container state per model-run; env-execution tasks
    # scored on observable outcomes). Measures the harness AND the model together.
    hstatuses = []
    # If a difficulty filter is in effect (e.g. hard-bench = hard,expert) AND the agentic tasks now
    # carry a `difficulty` field, scope the harness pass to those tiers too; tasks without a
    # difficulty field are always kept (a task that never opted into tiering still runs).
    if harness_ids and difficulty:
        from aeon import agentic_v2
        want_d = {d.strip() for d in difficulty.split(",") if d.strip()}
        if any(c.get("difficulty") for c in agentic_v2.CASES):
            agentic_v2.CASES = [c for c in agentic_v2.CASES
                                if not c.get("difficulty") or c.get("difficulty") in want_d]
            print(f"[pod] agentic harness pass scoped to difficulty {sorted(want_d)}: "
                  f"{len(agentic_v2.CASES)} tasks")
    for h in (harness_ids or []):
        try:
            from aeon import agentic_v2
            from pod import run_harness2
            disc = run_harness2.discover(h)
            print(f"[pod] harness {h} ({disc.get('harness_version', '?')}): "
                  f"{len(agentic_v2.CASES)} env-execution tasks, fresh container state")
            hres = run_harness2.run_agentic_v2(h, target, alias, concurrency=4,
                progress_cb=lambda c, s, stt: print(f"    [{h}] {c:26s} {stt:13s} {s}"))
        except Exception as e:
            print(f"[pod] harness {h} could not run: {e}")
            continue
        hresults = [{k: x.get(k) for k in ("case_id", "category", "tier", "status",
                                           "score", "raw_output", "evidence", "speed")} for x in hres]
        hscored = [x["score"] for x in hresults if isinstance(x["score"], float)]
        print(f"[pod] harness {h}: mean {sum(hscored)/len(hscored):.3f} over {len(hscored)} tasks"
              if hscored else f"[pod] harness {h}: no scored tasks")
        # A submit blip on one harness must not abort the others (each is an independent bundle).
        try:
            hst, hr = pod.run_and_submit(repo, agentic_v2.SUITE_ID, hresults, board=board,
                suite_hash=suite_mod.suite_hash(), environment=env, target_class="hf_pull_controlled",
                judge_model=judge, harness=disc.get("harness", h),
                harness_version=disc.get("harness_version"), **provenance)
            print(f"[pod] submit (harness {h} {disc.get('harness_version', '?')}) -> HTTP {hst}  {json.dumps(hr)[:200]}")
        except Exception as e:
            hst = 0
            print(f"[pod] submit (harness {h}) FAILED: {e}")
        hstatuses.append(hst)

    # In harness-only mode the text submit was skipped, so `st` is a placeholder — report the
    # harness submits instead so the caller's exit code reflects whether the harness data LANDED
    # (all 200 -> ok; any failure -> non-zero so a sweep re-runs this model).
    if harness_only:
        st = 200 if (hstatuses and all(s == 200 for s in hstatuses)) else (hstatuses[-1] if hstatuses else 0)

    # VISION suite (probe-gated; a text-only model records capability_absent, not submitted).
    if vision and not harness_only:
        _vision_and_submit(pod, repo, target, alias, env=env, provenance=provenance,
                           max_tokens=max_tokens, temperature=temperature)
    # AUDIO suite (probe-gated the same way).
    if audio and not harness_only:
        _audio_and_submit(pod, repo, target, alias, env=env, provenance=provenance,
                          temperature=temperature)
    # PERFORMANCE grid (direct concurrency ladder + per-harness timing).
    if perf and not harness_only:
        _perf_and_submit(pod, repo, target, alias, env=env, provenance=provenance,
                         harness_ids=harness_ids, max_conc=perf_max_conc)
    return st, r


def main():
    ap = argparse.ArgumentParser(description="Benchmark a model and submit to a mothership. "
        "Use --hf-link for a CONTROLLED, globally-rankable run; --target for a LOCAL run.")
    # controlled (global) path:
    ap.add_argument("--hf-link", default=None, help="HuggingFace link/repo — CONTROLLED A→B: "
        "pull → hash-verify → serve → bench → harnesses → sign. The ONLY path to the global board.")
    ap.add_argument("--port", type=int, default=8000, help="port the controlled engine serves on")
    ap.add_argument("--local-dir", default=None, help="model ALREADY on disk: hash-validated against "
        "the --hf-link repo's manifest instead of re-downloaded (good as gold when the bytes match); "
        "never deleted")
    ap.add_argument("--serve-url", default=None, help="operator-started serve of the validated weights "
        "(macOS/MLX bare-metal path): the pod validates + benches this URL + signs; the bare startup "
        "recipe is recorded like a docker recipe")
    ap.add_argument("--engine-image", default=os.environ.get("AEON_ENGINE_IMAGE"),
        help="custom container image for the chosen --engine (recorded with the run)")
    ap.add_argument("--weights-dir", default=None, help="where to pull weights (default ~/.aeon/models/...)")
    ap.add_argument("--keep-weights", action="store_true", help="retain downloaded weights after the run")
    ap.add_argument("--no-serve", action="store_true", help="model already served at --port (skip launching the engine)")
    ap.add_argument("--modelref", default=None, help="(split pod) path to .aeon-modelref.json from the "
        "pull sidecar — bench --target and submit ATTESTED using its verification (weights_hash + recipe)")
    ap.add_argument("--harness", default=None, help="'all' or comma list (hermes,openclaw,opencode) — "
        "also run the agentic suite through each harness (AI-Harness board)")
    # local path:
    ap.add_argument("--target", default=None, help="OpenAI base URL for a LOCAL run (not globally ranked)")
    ap.add_argument("--model", default=None, help="model name as the server reports it (LOCAL run)")
    # shared:
    ap.add_argument("--mothership", required=True, help="mothership base URL, e.g. http://localhost:8090")
    ap.add_argument("--api-key", default=os.environ.get("AEON_API_KEY"))
    ap.add_argument("--engine", default=None, help="catalog engine id: aeon-vllm-ultimate|vllm|"
        "vllm-rocm|sglang|llama.cpp|mlx (containerized recipes; mlx = macOS bare metal) — or a "
        "legacy label (ollama|lmstudio) for --target runs")
    ap.add_argument("--hardware", default=None, help="hardware label, e.g. 'NVIDIA DGX Spark GB10 128GB'")
    ap.add_argument("--hf-repo", default=None, help="(local path) HF repo id to claim for identity")
    ap.add_argument("--board", default="text")
    ap.add_argument("--suite-id", default=None)
    ap.add_argument("--key", default=None, help="device key path (created on first use)")
    ap.add_argument("--limit", type=int, default=None, help="benchmark only the first N cases (quick smoke)")
    ap.add_argument("--difficulty", default=None, help="only cases whose difficulty is in this comma-list "
        "(e.g. 'hard,expert' for the rapid bench); applies to the graded suite-v2 cases")
    ap.add_argument("--category", default=None, help="only cases whose category is in this comma-list "
        "(e.g. 'codegen') — applied ALONGSIDE --difficulty on the text suite; default: all categories")
    ap.add_argument("--preset", default=None, choices=("comprehensive", "hard-bench"),
        help="one-shot bundle: 'comprehensive' = everything on (all harnesses + vision + audio + arena "
        "+ perf); 'hard-bench' = the hard,expert tiers through all harnesses only (no vision/audio/arena/perf)")
    ap.add_argument("--fast", action="store_true", help="FAST bench: one random case per "
        "(category x difficulty) = 20 cases spanning the whole radar at every tier")
    ap.add_argument("--seed", default=None, help="fast-bench seed — same seed + same suite gives EVERY "
        "model the IDENTICAL questions (a true A/B). Omit with --fast to draw + print a fresh seed")
    ap.add_argument("--per-cell", type=int, default=1, help="fast bench: cases drawn per (category x "
        "difficulty) cell (1=20 cases; 5=~100; a thorough-but-feasible balanced sample)")
    ap.add_argument("--max-tokens", type=int, default=2048, help="generation cap (reasoning models need headroom)")
    ap.add_argument("--retry-max-tokens", type=int, default=None, help="if a case is CUT OFF mid-reasoning "
        "(finish_reason=length) and has no/incorrect answer, RE-RUN it once at this higher ceiling (e.g. "
        "50000) so the model can finish — a no-answer is usually truncation, not a real miss")
    ap.add_argument("--concurrency", type=int, default=_env_int("AEON_CONCURRENCY", 0),
        help="cases to run CONCURRENTLY through the served "
        "model (vLLM batches them). 0 = AUTO (default): capacity-aware — high (up to 24) when a capable "
        "GPU is detected, single-stream when none is. Pass an explicit N (e.g. 16 on a Spark) to pin it; "
        "env AEON_CONCURRENCY sets the default (the GUI launcher passes it)")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--no-vision", action="store_true", help="skip the VISION suite (default: run it; a "
        "capability probe auto-skips text-only models so this is only needed to force-disable)")
    ap.add_argument("--no-audio", action="store_true", help="skip the AUDIO suite (default: run it, probe-gated)")
    ap.add_argument("--arena", type=int, default=2, help="arena artifacts per kind (app/game/animation) "
        "generated by the served model and shipped in the signed bundle; 0 disables")
    ap.add_argument("--perf", action="store_true", help="run the PERFORMANCE grid (direct c=1/4/8/16/32 x "
        "categories: tok/s, TTFT, prefill tok/s; + per-harness task timing) and submit as aeon-perf-v1")
    ap.add_argument("--perf-max-conc", type=int, default=_env_int("AEON_PERF_MAX_CONC", 32),
        help="cap the perf-grid concurrency ladder: rungs above N drop; a non-rung cap becomes the "
        "top rung (24 -> 1/4/8/16/24). Default 32 (or env AEON_PERF_MAX_CONC)")
    ap.add_argument("--harness-only", action="store_true", help="run ONLY the agentic harness pass "
        "(skip text/arena/vision/audio/perf) — targeted harness re-run at a given served context")
    ap.add_argument("--judge", default=None, help="FRONTIER judge model id (else deterministic-only; never self)")
    ap.add_argument("--judge-url", default=None, help="judge endpoint (defaults to --target)")
    ap.add_argument("--judge-key", default=None, help="judge API key")
    a = ap.parse_args()

    if a.concurrency <= 0:                            # 0 = AUTO: bias high when the box can handle it
        a.concurrency = _auto_concurrency()
        print(f"[pod] concurrency=auto -> {a.concurrency} (detected capacity; pin with --concurrency N)")
    # Concurrent streams individually slow down while total wall time drops — grow the per-request
    # HTTP timeout to match before any target is built (see _scale_http_timeout).
    eff = _scale_http_timeout(a.concurrency)
    print(f"[pod] per-request HTTP timeout: {eff}s (scaled for concurrency {a.concurrency})")

    # Presets resolve to the underlying knobs BEFORE dispatch, so every downstream path (harness
    # expansion + run_attested/run_controlled) sees a plain, already-normalised set of flags.
    if a.preset == "comprehensive":               # everything on: all harnesses + vision + audio + arena + perf
        a.harness = a.harness or "all"
        a.perf = True
        a.no_vision = False
        a.no_audio = False
        if a.arena == 0:                          # keep an explicit --arena N override; default stays 2
            a.arena = 2
    elif a.preset == "hard-bench":                # the hard,expert tiers through every harness, nothing else
        a.difficulty = a.difficulty or "hard,expert"
        a.harness = a.harness or "all"
        a.no_vision = True
        a.no_audio = True
        a.arena = 0
        a.perf = False

    hids = None
    if a.harness:
        from pod import adapters
        hids = (sorted(set(adapters.ADAPTERS) - {"mock"}) if a.harness.lower() == "all"
                else [h.strip() for h in a.harness.split(",") if h.strip()])

    if a.modelref and a.target:                       # split pod: sidecar pulled+verified+served
        st, _ = run_attested(a.target, a.modelref, a.mothership, hardware=a.hardware, board=a.board,
            suite_id=a.suite_id, key_path=a.key, max_tokens=a.max_tokens, temperature=a.temperature,
            judge=a.judge, judge_url=a.judge_url, judge_key=a.judge_key, harness_ids=hids, limit=a.limit,
            difficulty=a.difficulty, category=a.category, fast=a.fast, seed=a.seed, per_cell=a.per_cell,
            retry_max_tokens=a.retry_max_tokens, concurrency=a.concurrency, vision=not a.no_vision,
            arena_per_kind=a.arena, audio=not a.no_audio, perf=a.perf, perf_max_conc=a.perf_max_conc,
            harness_only=a.harness_only)
    elif a.hf_link:                                   # single-process controlled flow
        st, _ = run_controlled(a.hf_link, a.mothership, engine=a.engine, hardware=a.hardware,
            board=a.board, suite_id=a.suite_id, key_path=a.key, weights_dir=a.weights_dir,
            keep_weights=a.keep_weights, port=a.port, max_tokens=a.max_tokens,
            temperature=a.temperature, judge=a.judge, judge_url=a.judge_url, judge_key=a.judge_key,
            harness_ids=hids, limit=a.limit, serve=not a.no_serve, fast=a.fast, seed=a.seed,
            per_cell=a.per_cell, difficulty=a.difficulty, category=a.category, vision=not a.no_vision,
            concurrency=a.concurrency, local_dir=a.local_dir, serve_url=a.serve_url,
            engine_image=a.engine_image)
    elif a.target and a.model:                        # local run (not globally ranked)
        st, _ = run_pod(a.target, a.model, a.mothership, api_key=a.api_key, engine=a.engine,
                        hardware=a.hardware, board=a.board, suite_id=a.suite_id, key_path=a.key,
                        hf_repo=a.hf_repo, limit=a.limit, difficulty=a.difficulty, category=a.category,
                        max_tokens=a.max_tokens,
                        temperature=a.temperature, judge=a.judge, judge_url=a.judge_url, judge_key=a.judge_key,
                        concurrency=a.concurrency)
    else:
        ap.error("provide --modelref + --target (split pod), --hf-link (single-process controlled), "
                 "OR --target + --model (local run)")
    raise SystemExit(0 if st == 200 else 1)


if __name__ == "__main__":
    main()
