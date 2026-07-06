"""Aggregate per-case results into category scores + a leaderboard.

Composite = mean of per-category quality (0..100), client can reweight.
Speed is reported separately, never folded into the quality rank (DESIGN §14).
"""
from __future__ import annotations

import json

from . import capabilities
from . import db
from . import suite as suite_mod
from . import vram

# Trust tiers ranked on the GLOBAL leaderboard. Mirrors ingest.ELIGIBLE_TIERS — a run earns
# 'attested' ONLY through the controlled HF-pull flow (verified weights + recipe + signature).
ELIGIBLE_TIERS = {"attested"}


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


# The three agentic harnesses a controlled pod runs the suite through, disclosed by name +
# upstream repo on the AI-Harness page (the exact release version travels with each run).
HARNESS_META = {
    "hermes":   {"name": "Hermes Agent", "repo": "https://github.com/NousResearch/hermes-agent"},
    "openclaw": {"name": "OpenClaw",     "repo": "https://github.com/openclaw/openclaw"},
    "opencode": {"name": "OpenCode",     "repo": "https://github.com/anomalyco/opencode"},
}


def harness_board(board="text"):
    """AI Harness Bench — the model × harness matrix: how each model performs on each agentic
    harness (mean score per (canonical model, harness)), with the exact harness version
    disclosed. Only runs tagged with a harness appear. Empty until harnessed runs are submitted."""
    rows = db.all_results_with_runs(board=board)
    cell = {}
    for r in rows:
        h = r.get("harness")
        if not h:
            continue
        ckey = (r.get("canonical_id") or r["model"], h)
        d = cell.setdefault(ckey, {"version": r.get("harness_version"), "model": r["model"], "scores": []})
        if r["score"] is not None:
            d["scores"].append(r["score"])
    models, harnesses, matrix = set(), set(), {}
    for (canon, h), d in cell.items():
        models.add(canon); harnesses.add(h)
        sc = d["scores"]
        meta = HARNESS_META.get(h, {})
        matrix.setdefault(canon, {})[h] = {
            "score": round(100 * sum(sc) / len(sc), 1) if sc else None,
            "harness_version": d["version"], "n_cases": len(sc), "model": d["model"],
            "harness_name": meta.get("name", h), "harness_repo": meta.get("repo")}
    return {"models": sorted(models), "harnesses": sorted(harnesses), "matrix": matrix,
            "harness_meta": HARNESS_META}


def _run_summary(info):
    """Per-run composite + category + speed roll-up (one run = one benchmark pass). Speed is
    reported BOTH overall and per category (prompt types have very different speed profiles —
    a reasoning model is slow on reasoning prompts, fast on lookups)."""
    cats, ttfts, tpss, e2es, crv = {}, [], [], [], {}
    cat_sp = {}   # category -> {ttft:[], tps:[], e2e:[]}
    out_toks, busy_ms = 0, 0.0
    for r in info["results"]:
        sp = r.get("speed") or {}
        c = r["category"]
        ttfts.append(sp.get("ttft_ms")); tpss.append(sp.get("decode_tps")); e2es.append(sp.get("e2e_ms"))
        if sp.get("output_tokens") and sp.get("e2e_ms"):
            out_toks += sp["output_tokens"]; busy_ms += sp["e2e_ms"]
        b = cat_sp.setdefault(c, {"ttft": [], "tps": [], "e2e": []})
        b["ttft"].append(sp.get("ttft_ms")); b["tps"].append(sp.get("decode_tps")); b["e2e"].append(sp.get("e2e_ms"))
        if r["score"] is not None:
            cats.setdefault(c, []).append(r["score"])
        if r.get("creativity") is not None:
            crv.setdefault(c, []).append(r["creativity"])
    cat_scores = {c: round(100 * sum(v) / len(v), 1) for c, v in cats.items()}
    composite = round(sum(cat_scores.values()) / len(cat_scores), 1) if cat_scores else 0.0
    tier = info.get("trust_tier") or "self_reported"
    # AGGREGATE throughput under the run's actual test load: with N cases in flight the
    # per-stream number is throttled by design, so the honest raw-throughput figure is total
    # generated tokens over the run's busy wall-clock (sum of per-case time / lanes). Only
    # computable when the run recorded its bench concurrency (env.concurrency, new pods).
    conc = (info.get("environment") or {}).get("concurrency")
    agg_tps = None
    if conc and conc > 1 and out_toks and busy_ms:
        agg_tps = round(out_toks / ((busy_ms / 1000.0) / conc), 1)
    return {
        "run": info["run"], "model": info["model"], "canonical": info["canonical"],
        "hf_repo": info["hf_repo"], "verified": info["verified"], "started_at": info["started_at"],
        "bench_seed": info.get("bench_seed"), "suite_hash": info.get("suite_hash"),
        "trust_tier": tier, "eligible": tier in ELIGIBLE_TIERS,
        "composite": composite, "categories": cat_scores,
        "creativity": {c: round(sum(v) / len(v), 2) for c, v in crv.items()},
        "avg_ttft_ms": _avg(ttfts), "avg_decode_tps": _avg(tpss), "avg_e2e_ms": _avg(e2es),
        "agg_tps": agg_tps, "bench_concurrency": conc,
        "category_speed": {c: {"ttft_ms": _avg(b["ttft"]), "decode_tps": _avg(b["tps"]),
                               "e2e_ms": _avg(b["e2e"])} for c, b in cat_sp.items()},
        "n_cases": len(info["results"]),
    }


# When a NEW suite version ships, the default board scopes to it — which is empty until
# models re-run. Rather than a blank public leaderboard, fall back to the newest legacy
# suite that HAS runs, honestly labeled via suite_shown/legacy so the UI can badge it.
_LEGACY_SUITES = ["aeon-suite-v2", "aeon-suite-v1"]


def leaderboard(suite=None):
    """One board row per CANONICAL model identity (the HF repo when known, else the declared
    name) — so the same model under different local aliases lines up, and EVERY run for it is
    aggregated: mean composite across runs, plus best / worst / run-count and the run history.

    A TIER-scoped run (e.g. the hard-only pass, suite id 'aeon-suite-v2-hard') is a DIFFERENT test
    than the comprehensive suite and would skew the average — so the default board shows ONLY the
    comprehensive suite. Pass suite='aeon-suite-v2-hard' (etc.) to see a tier board on its own."""
    out = _leaderboard_scoped(suite)
    if suite is None and not out["models"]:
        for legacy in _LEGACY_SUITES:                    # new suite, no runs yet -> legacy view
            lb = _leaderboard_scoped(legacy)
            if lb["models"]:
                lb["suite_shown"], lb["legacy"] = legacy, True
                return lb
    out["suite_shown"] = suite or suite_mod.SUITE_ID
    return out


def _leaderboard_scoped(suite=None):
    rows = db.all_results_with_runs()
    COMPREHENSIVE = suite_mod.SUITE_ID

    def _in_scope(sid):
        sid = sid or COMPREHENSIVE
        if suite:
            return sid == suite
        return sid == COMPREHENSIVE           # default: comprehensive only, tier runs excluded

    by_run = {}
    for r in rows:
        if r.get("harness"):                 # harness-tagged runs belong to the AI-Harness board
            continue
        if not _in_scope(r.get("suite_id")):
            continue
        if r["run"] not in by_run:
            try:
                renv = json.loads(r.get("env_json") or "{}")
            except Exception:
                renv = {}
            by_run[r["run"]] = {
                "run": r["run"], "model": r["model"],
                "canonical": r.get("canonical_id") or r["model"],
                "hf_repo": r.get("hf_repo"), "verified": r.get("model_verified"),
                "trust_tier": r.get("trust_tier") or "self_reported",
                "bench_seed": r.get("bench_seed"), "suite_hash": r.get("suite_hash"),
                "started_at": r["started_at"], "environment": renv, "results": []}
        by_run[r["run"]]["results"].append(r)

    runs = [_run_summary(info) for info in by_run.values()]

    by_model = {}
    for rs in runs:
        by_model.setdefault(rs["canonical"], []).append(rs)

    board = []
    for canonical, mruns in by_model.items():
        mruns.sort(key=lambda r: r["started_at"] or 0)
        # The model's SHOWN standing aggregates over its globally-eligible (attested HF-pull)
        # runs when it has any; otherwise over its local runs. record_eligible drives the
        # global board: only models with at least one verified run rank there.
        elig = [r for r in mruns if r.get("eligible")]
        agg = elig or mruns
        record_eligible = bool(elig)
        comps = [r["composite"] for r in agg]
        latest = agg[-1]
        best = max(agg, key=lambda r: r["composite"])
        worst = min(agg, key=lambda r: r["composite"])
        cat_all = {}
        for r in agg:
            for c, v in r["categories"].items():
                cat_all.setdefault(c, []).append(v)
        mean_cats = {c: round(sum(v) / len(v), 1) for c, v in cat_all.items()}
        cspeed = {}
        for r in agg:
            for c, sp in (r.get("category_speed") or {}).items():
                d = cspeed.setdefault(c, {"ttft": [], "tps": [], "e2e": []})
                d["ttft"].append(sp.get("ttft_ms")); d["tps"].append(sp.get("decode_tps")); d["e2e"].append(sp.get("e2e_ms"))
        mean_cspeed = {c: {"ttft_ms": _avg(d["ttft"]), "decode_tps": _avg(d["tps"]), "e2e_ms": _avg(d["e2e"])}
                       for c, d in cspeed.items()}
        cr_vals = [x for v in latest["creativity"].values() for x in [v]]
        display = latest["hf_repo"] or latest["model"]
        board.append({
            "model": display,                       # canonical display name (HF repo when known)
            "canonical": canonical,
            "hf_repo": latest["hf_repo"],
            "verified": latest["verified"],         # verified | claim | claim_unverified | declared
            "trust_tier": "attested" if record_eligible else "self_reported",
            "record_eligible": record_eligible,     # ranked on the global board iff True
            "n_runs": len(agg),
            "n_runs_total": len(mruns),
            "composite": round(sum(comps) / len(comps), 1),   # MEAN across the aggregated runs
            "best": best["composite"], "worst": worst["composite"],
            "best_run": best["run"], "worst_run": worst["run"], "latest_run": latest["run"],
            "categories": mean_cats,
            "category_speed": mean_cspeed,        # per-category TTFT / decode_tps / e2e
            "creativity": latest["creativity"],
            "avg_creativity": round(sum(cr_vals) / len(cr_vals), 2) if cr_vals else None,
            "avg_ttft_ms": _avg([r["avg_ttft_ms"] for r in agg]),
            "avg_decode_tps": _avg([r["avg_decode_tps"] for r in agg]),
            "avg_e2e_ms": _avg([r["avg_e2e_ms"] for r in agg]),
            # concurrent throughput under test load (runs that recorded their concurrency)
            "agg_tps": _avg([r.get("agg_tps") for r in agg]),
            "bench_concurrency": max((r.get("bench_concurrency") or 0 for r in agg), default=0) or None,
            "n_cases": latest["n_cases"],
            "vram_est_gb": vram.estimate_gb(display),
            "tags": capabilities.model_tags(display, mean_cats, "text"),
            # newest-first run history for review (each run_id opens its full detail)
            "runs": [{"run": r["run"], "composite": r["composite"], "started_at": r["started_at"],
                      "model": r["model"], "trust_tier": r.get("trust_tier"),
                      "bench_seed": r.get("bench_seed")} for r in reversed(mruns)],
        })
    # global-eligible first, then by score — the verified ranking floats to the top
    board.sort(key=lambda x: (not x["record_eligible"], -x["composite"]))
    return {"categories": suite_mod.CATEGORIES, "models": board}


def _hw_label(env):
    """Hardware label a run was benched on (detected wins; operator claim is the fallback)."""
    hw = (env or {}).get("hardware") or {}
    return hw.get("detected_label") or hw.get("label")


def _quality_index():
    """(canonical, hardware_label) -> best current-suite QUALITY composite, so the Performance
    board can show each model's quality score alongside its speed. Also indexes (canonical, None)
    as a hardware-agnostic fallback (quality is ~model-intrinsic; a model may have benched perf on
    a rig it never ran the quality suite on). Current SUITE_ID only — legacy suites don't compare."""
    rows = db.all_results_with_runs("text")
    by_run = {}
    for r in rows:
        if r.get("harness") or (r.get("suite_id") or suite_mod.SUITE_ID) != suite_mod.SUITE_ID:
            continue
        by_run.setdefault(r["run"], {
            "run": r["run"], "model": r["model"], "canonical": r.get("canonical_id") or r["model"],
            "hf_repo": r.get("hf_repo"), "verified": r.get("model_verified"),
            "trust_tier": r.get("trust_tier") or "self_reported", "started_at": r["started_at"],
            "env_json": r.get("env_json"), "results": []})["results"].append(r)
    idx = {}
    for info in by_run.values():
        comp = _run_summary(info)["composite"]
        try:
            label = _hw_label(json.loads(info.get("env_json") or "{}"))
        except Exception:
            label = None
        for key in ((info["canonical"], label), (info["canonical"], None)):
            cur = idx.get(key)
            if cur is None or comp > cur["composite"]:      # keep the BEST quality run for this pairing
                idx[key] = {"composite": comp, "run": info["run"]}
    return idx


def _lowest_conc_metric(c_lo, metric, agg):
    """A single-stream metric at the lowest tested concurrency: prefer the 'overall' scope,
    else aggregate across category scopes (agg=max for throughput, min for latency)."""
    ov = (c_lo.get("overall") or {}).get(metric)
    if isinstance(ov, (int, float)):
        return ov
    vals = [(c_lo.get(s) or {}).get(metric) for s in c_lo if s != "overall"]
    vals = [x for x in vals if isinstance(x, (int, float))]
    return agg(vals) if vals else None


def perf_board():
    """PERFORMANCE board: one row per canonical model = its LATEST perf run (suite
    aeon-perf-v1), unpacked into two grids the dashboard can chart directly:
      direct[conc][scope]   = {ttft_ms, ttft_p95, tpot_ms, decode_tps, agg_decode_tps, prefill_tps}
      harness[hid][conc][scope] = {mean_task_s, p95_task_s, tasks_per_min, failures}
    scope = a prompt category (Math/Coding/...) or 'overall'. Older runs that predate a
    metric (e.g. TPOT) simply carry null there — the frontend renders gaps honestly."""
    rows = db.perf_results()
    by_run = {}
    for r in rows:
        by_run.setdefault(r["run"], {
            "run": r["run"], "model": r["model"],
            "canonical": r.get("canonical_id") or r["model"],
            "hf_repo": r.get("hf_repo"), "hf_revision": r.get("hf_revision"),
            "recipe": r.get("recipe"), "verified": r.get("model_verified"),
            "trust_tier": r.get("trust_tier") or "self_reported",
            "env": r.get("env") or {},
            "started_at": r["started_at"], "results": []})["results"].append(r)
    latest = {}
    for info in by_run.values():                     # newest perf run per canonical model
        c = info["canonical"]
        if c not in latest or (info["started_at"] or 0) > (latest[c]["started_at"] or 0):
            latest[c] = info
    qidx = _quality_index()                          # (canonical, hw) -> best v3 quality composite
    models = []
    for c, info in latest.items():
        direct, harness, concs = {}, {}, set()
        for x in info["results"]:
            cid, ev = x.get("case_id") or "", x.get("evidence") or {}
            parts = cid.split(".")
            # perf.direct.<scope>.c<N>  |  perf.harness.<hid>[.<scope>].c<N>
            if len(parts) < 4 or parts[0] != "perf" or not parts[-1].startswith("c"):
                continue
            try:
                conc = int(parts[-1][1:])
            except ValueError:
                continue
            concs.add(conc)
            if parts[1] == "direct" and len(parts) == 4:
                direct.setdefault(conc, {})[parts[2]] = {
                    "ttft_ms": ev.get("ttft_ms_mean"), "ttft_p95": ev.get("ttft_ms_p95"),
                    "tpot_ms": ev.get("tpot_ms_mean"),
                    "decode_tps": ev.get("decode_tps_mean"),
                    "agg_decode_tps": ev.get("agg_decode_tps"),
                    "prefill_tps": ev.get("prefill_tps_mean"),
                    "n_errors": ev.get("n_errors"),
                }
            elif parts[1] == "harness":
                hid = parts[2]
                scope = parts[3] if len(parts) == 5 else "overall"
                harness.setdefault(hid, {}).setdefault(conc, {})[scope] = {
                    "mean_task_s": ev.get("mean_task_s"), "p95_task_s": ev.get("p95_task_s"),
                    "tasks_per_min": ev.get("tasks_per_min"), "failures": ev.get("failures"),
                }
        if not direct and not harness:
            continue
        # The level SUMMARY is recomputed here as the ARITHMETIC MEAN across the per-category
        # cells — each category is a REAL concurrent cohort at that rung; the categories never
        # ran together in one pool, so the stored 'overall' (old mixed-pool runs: a cross-
        # category tally; new isolated runs: a time-weighted figure) is never displayed as-is.
        for scopes in direct.values():
            cat_cells = [v for k, v in scopes.items() if k != "overall" and isinstance(v, dict)]
            if not cat_cells:
                continue
            def _catmean(key):
                vals = [c.get(key) for c in cat_cells if isinstance(c.get(key), (int, float))]
                return round(sum(vals) / len(vals), 2) if vals else None
            scopes["overall"] = {
                "ttft_ms": _catmean("ttft_ms"), "ttft_p95": _catmean("ttft_p95"),
                "tpot_ms": _catmean("tpot_ms"), "decode_tps": _catmean("decode_tps"),
                "agg_decode_tps": _catmean("agg_decode_tps"),
                "prefill_tps": _catmean("prefill_tps"),
                "n_errors": sum(c.get("n_errors") or 0 for c in cat_cells),
                "summary": "category_mean",       # provenance marker for the API consumer
            }
        # peak aggregate throughput across the ladder = the row's headline + sort key
        # (now the mean cohort aggregate — total tok/s of the live concurrent streams)
        aggs = [(v.get("overall") or {}).get("agg_decode_tps") for v in direct.values()]
        aggs = [a for a in aggs if isinstance(a, (int, float))]
        hw = (info.get("env") or {}).get("hardware") or {}
        hwlabel = hw.get("detected_label") or hw.get("label")
        c_lo = (direct.get(min(concs)) if concs else {}) or {}    # single-stream = lowest concurrency
        q = qidx.get((c, hwlabel)) or qidx.get((c, None))         # v3 quality composite for this model+hw
        try:
            recipe = json.loads(info.get("recipe") or "null")
        except Exception:
            recipe = None
        models.append({
            "model": info["hf_repo"] or info["model"], "canonical": c,
            "hf_repo": info["hf_repo"], "hf_revision": info.get("hf_revision"),
            "verified": info["verified"],
            "trust_tier": info["trust_tier"], "run": info["run"],
            "started_at": info["started_at"],
            # hardware AS DETECTED on the bench machine; the operator's claim is the fallback
            "hardware": hwlabel,
            "conc_levels": sorted(concs),
            # the four axes the recipe-discovery tool ranks on (per model, filterable by hardware):
            "peak_agg_tps": max(aggs) if aggs else None,                     # throughput under load
            "peak_single_tps": _lowest_conc_metric(c_lo, "decode_tps", max),  # single-stream speed
            "latency": {"ttft_ms": _lowest_conc_metric(c_lo, "ttft_ms", min),
                        "tpot_ms": _lowest_conc_metric(c_lo, "tpot_ms", min),
                        "conc": min(concs) if concs else None},
            "quality": (q or {}).get("composite"),                          # v3 composite (joined)
            "quality_run": (q or {}).get("run"),
            "recipe": recipe,               # raw serve recipe; the endpoint assembles docker_run + drafter
            "direct": direct, "harness": harness,
        })
    models.sort(key=lambda m: -(m["peak_agg_tps"] or 0))
    hardwares = sorted({m["hardware"] for m in models if m["hardware"]})
    return {"categories": suite_mod.CATEGORIES, "models": models, "hardwares": hardwares}


def seed_index(board="text"):
    """Fast-bench seeds seen on this board, each with the models that ran it — drives the
    compare-by-seed picker. A seed run by >=2 models (same suite_hash) is a ready A/B."""
    rows = db.all_results_with_runs(board)
    seeds = {}
    for r in rows:
        s = r.get("bench_seed")
        if not s or r.get("harness"):
            continue
        e = seeds.setdefault(s, {"seed": s, "models": set(), "runs": set(),
                                 "suite_hashes": set(), "latest": 0})
        e["models"].add(r.get("hf_repo") or r["model"])
        e["runs"].add(r["run"])
        if r.get("suite_hash"):
            e["suite_hashes"].add(r["suite_hash"])
        e["latest"] = max(e["latest"], r.get("started_at") or 0)
    out = [{"seed": s, "n_models": len(e["models"]), "n_runs": len(e["runs"]),
            "models": sorted(e["models"]), "suite_consistent": len(e["suite_hashes"]) <= 1,
            "latest": e["latest"]} for s, e in seeds.items()]
    out.sort(key=lambda x: -x["latest"])
    return out


def compare_by_seed(seed, board="text"):
    """A TRUE A/B: each model's LATEST run on this exact fast-bench seed, aligned case-by-case.
    Same seed + same suite_hash => the identical 20 questions, so per-case and per-category
    deltas are pure model differences — you can see exactly where one model beats another."""
    seed = str(seed)
    rows = [r for r in db.all_results_with_runs(board)
            if str(r.get("bench_seed") or "") == seed and not r.get("harness")]
    by_run = {}
    for r in rows:
        by_run.setdefault(r["run"], {
            "run": r["run"], "model": r["model"], "canonical": r.get("canonical_id") or r["model"],
            "hf_repo": r.get("hf_repo"), "verified": r.get("model_verified"),
            "trust_tier": r.get("trust_tier") or "self_reported", "suite_hash": r.get("suite_hash"),
            "started_at": r["started_at"], "bench_seed": r.get("bench_seed"),
            "results": []})["results"].append(r)
    latest = {}                                  # one run per canonical model: the most recent
    for run in by_run.values():
        k = run["canonical"]
        if k not in latest or (run["started_at"] or 0) > (latest[k]["started_at"] or 0):
            latest[k] = run
    diff = {c["id"]: c.get("difficulty") for c in suite_mod.CASES}
    cat_of = {c["id"]: c["category"] for c in suite_mod.CASES}
    models, case_scores = [], {}
    for run in latest.values():
        summ = _run_summary(run)
        disp = run["hf_repo"] or run["model"]
        models.append({"model": disp, "canonical": run["canonical"], "run": run["run"],
                       "verified": run["verified"], "trust_tier": run["trust_tier"],
                       "suite_hash": run["suite_hash"], "started_at": run["started_at"],
                       "composite": summ["composite"], "categories": summ["categories"],
                       "n_cases": summ["n_cases"]})
        for r in run["results"]:
            case_scores.setdefault(r["case_id"], {})[disp] = r["score"]
    models.sort(key=lambda m: -m["composite"])
    corder = {c: i for i, c in enumerate(suite_mod.CATEGORIES)}
    dorder = {d: i for i, d in enumerate(suite_mod.DIFFICULTIES)}
    cases = [{"case_id": cid, "category": cat_of.get(cid), "difficulty": diff.get(cid),
              "scores": case_scores[cid]} for cid in case_scores]
    cases.sort(key=lambda x: (corder.get(x["category"], 9), dorder.get(x["difficulty"], 9), x["case_id"]))
    shashes = sorted({m["suite_hash"] for m in models if m["suite_hash"]})
    return {"seed": seed, "board": board, "categories": suite_mod.CATEGORIES,
            "difficulties": suite_mod.DIFFICULTIES, "models": models, "cases": cases,
            "suite_consistent": len(shashes) <= 1, "suite_hash": shashes[0] if shashes else None}


def vision_leaderboard():
    """Separate VISION board (DESIGN §6c). Only models whose latest vision run was
    capability-probed OK appear here (capability_absent runs are excluded by the
    succeeded-status filter). Never merged into the text leaderboard."""
    from . import vision_suite as vs

    rows = db.all_results_with_runs(board="vision")
    by_run = {}
    for r in rows:
        by_run.setdefault(
            r["run"], {"model": r["model"], "started_at": r["started_at"], "results": []}
        )["results"].append(r)

    latest = {}
    for run_id, info in by_run.items():
        m = info["model"]
        if m not in latest or info["started_at"] > latest[m]["started_at"]:
            latest[m] = {"run": run_id, **info}

    total_cats = len(vs.CATEGORIES_VISION)
    board = []
    for model, info in latest.items():
        cats, ttfts, tpss = {}, [], []
        for r in info["results"]:
            sp = r.get("speed") or {}
            ttfts.append(sp.get("ttft_after_image_ms"))
            tpss.append(sp.get("decode_tps"))
            if r["score"] is not None:
                cats.setdefault(r["category"], []).append(r["score"])
        cat_scores = {c: round(100 * sum(v) / len(v), 1) for c, v in cats.items()}
        composite = round(sum(cat_scores.values()) / len(cat_scores), 1) if cat_scores else 0.0
        board.append({
            "model": model, "run": info["run"], "composite": composite,
            "categories": cat_scores, "coverage": f"{len(cat_scores)}/{total_cats}",
            "avg_ttft_after_image_ms": _avg(ttfts), "avg_decode_tps": _avg(tpss),
            "n_cases": len(info["results"]),
            "vram_est_gb": vram.estimate_gb(model),
            "tags": capabilities.model_tags(model, cat_scores, "vision"),
        })
    board.sort(key=lambda x: -x["composite"])
    return {"categories": vs.CATEGORIES_VISION, "models": board}
