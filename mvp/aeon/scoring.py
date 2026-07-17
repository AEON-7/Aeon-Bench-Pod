"""Aggregate per-case results into category scores + a leaderboard.

Composite = mean of per-category quality (0..100), client can reweight.
Speed is reported separately, never folded into the quality rank (DESIGN §14).
"""
from __future__ import annotations

import json
import re

from . import capabilities
from . import db
from . import hwnorm
from . import suite as suite_mod
from . import vram

# Trust tiers ranked on the GLOBAL leaderboard. Mirrors ingest.ELIGIBLE_TIERS — a run earns
# 'attested' ONLY through the controlled HF-pull flow (verified weights + recipe + signature).
ELIGIBLE_TIERS = {"attested"}

# A run RANKS on the leaderboard only if it actually covered its suite. Full passes land a
# few short of the corpus (capability-gated cases, judge errors) — 90% absorbs that without
# letting a genuinely partial pass (crashed mid-suite yet committed as succeeded) stand in
# for a comprehensive score.
MIN_SUITE_COVERAGE = 0.9


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
    a reasoning model is slow on reasoning prompts, fast on lookups).

    NO_ANSWER weighting (pod contract): a results row may carry status='no_answer' (score
    NULL) — the model was asked and declined/failed to commit to an answer. Category mean =
    sum(scores) / (n_answered + 0.25 * n_no_answer): a no-answer costs a quarter of a wrong
    answer. Runs without such rows compute EXACTLY as before (denominator = n_answered)."""
    cats, ttfts, tpss, e2es, crv = {}, [], [], [], {}
    noans = {}    # category -> count of status='no_answer' rows (score NULL by contract)
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
        elif (r.get("status") or "") == "no_answer":
            noans[c] = noans.get(c, 0) + 1
        if r.get("creativity") is not None:
            crv.setdefault(c, []).append(r["creativity"])
    # Category order: scored categories first (in row order, exactly as before), then any
    # category attempted ONLY via no_answer rows — so runs without no_answer rows serialize
    # bit-identically to the pre-no_answer payload.
    cat_scores = {}
    for c in list(cats) + [c for c in noans if c not in cats]:
        sc = cats.get(c, [])
        denom = len(sc) + 0.25 * noans.get(c, 0)
        cat_scores[c] = round(100 * sum(sc) / denom, 1) if denom else 0.0
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
        # healthy passes score every submitted row (verified in prod 2026-07-11); nulls mean
        # error/killed cases, so coverage gates count SCORED cases, not rows — except
        # no_answer rows, which are ATTEMPTED (asked + declined) and count toward coverage
        "n_scored": sum(1 for r in info["results"] if r["score"] is not None),
        "n_no_answer": sum(noans.values()),
        "frontier": info.get("frontier"),
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
                return _attach_dials(lb)
    out["suite_shown"] = suite or suite_mod.SUITE_ID
    return _attach_dials(out)


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
        if r.get("bench_seed"):              # fast-bench draw: a ~25-case subsample, not the
            continue                         # comprehensive suite — compare-by-seed only
        if not _in_scope(r.get("suite_id")):
            continue
        if r["run"] not in by_run:
            try:
                renv = json.loads(r.get("env_json") or "{}")
            except Exception:
                renv = {}
            fmeta = renv.get("frontier") if isinstance(renv, dict) else None
            by_run[r["run"]] = {
                "run": r["run"], "model": r["model"],
                "canonical": r.get("canonical_id") or r["model"],
                "hf_repo": r.get("hf_repo"), "verified": r.get("model_verified"),
                "trust_tier": r.get("trust_tier") or "self_reported",
                "bench_seed": r.get("bench_seed"), "suite_hash": r.get("suite_hash"),
                "started_at": r["started_at"], "environment": renv, "frontier": fmeta,
                "results": []}
        by_run[r["run"]]["results"].append(r)

    runs = [_run_summary(info) for info in by_run.values()]

    # Coverage floor: the corpus size is authoritative for the current suite; for legacy/tier
    # scopes (whose corpora are no longer shipped) the largest committed pass calibrates it.
    # ATTEMPTED = covered: a no_answer row is a case the model was asked (it costs score via
    # the ¼-weight rule), so it counts toward coverage like a scored row.
    scope = suite or COMPREHENSIVE
    if scope == COMPREHENSIVE:
        expected = len(suite_mod.CASES)
    else:
        expected = max((r["n_scored"] + r["n_no_answer"] for r in runs), default=0)
    runs = [r for r in runs
            if (r["n_scored"] + r["n_no_answer"]) >= MIN_SUITE_COVERAGE * expected]

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
            "frontier": latest.get("frontier"),
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


# ---- AEON score: the six-dial per-model summary + blended headline number --------------
#
# Weights over the three RANKED components; the modality dials (vision/audio/video) are
# informational and never blend in. When a component is missing the remaining weights are
# renormalized and the row is flagged provisional — an honest partial score, never a
# hidden zero.
AEON_WEIGHTS = {"intelligence": 0.5, "agentic": 0.3, "performance": 0.2}


def _perf_percentile_index():
    """canonical -> {'score', 'peak_agg_tps', 'hw'}: the model's best PERCENTILE standing on
    the perf board. Percentile ranks the model's peak_agg_tps among the perf rows of the
    SAME hw bucket (apples to apples — a Spark never races a 5090): 100 = fastest in
    bucket, 0 = slowest, single-row bucket = 100. A model with rows on several rigs keeps
    its best showing (ties broken by higher absolute peak)."""
    buckets = {}
    for m in perf_board()["models"]:
        if isinstance(m.get("peak_agg_tps"), (int, float)):
            buckets.setdefault(m["hw_bucket"], []).append(m)
    out = {}
    for bucket, ms in buckets.items():
        n = len(ms)      # one row per (canonical, bucket) by construction — a real cohort
        for m in ms:
            if n == 1:
                pct = 100.0
            else:
                below = sum(1 for x in ms if x["peak_agg_tps"] < m["peak_agg_tps"])
                pct = round(100.0 * below / (n - 1), 1)
            cur = out.get(m["canonical"])
            if cur is None or (pct, m["peak_agg_tps"]) > (cur["score"], cur["peak_agg_tps"]):
                out[m["canonical"]] = {"score": pct, "peak_agg_tps": m["peak_agg_tps"],
                                       "hw": m["hw_bucket"]}
    return out


def _agentic_index():
    """canonical -> {'score': mean of its available harness scores, 'harnesses': {hid: score}}
    from the AI-Harness matrix (same canonical join key as the text board)."""
    out = {}
    for canon, cells in harness_board()["matrix"].items():
        hs = {h: cell["score"] for h, cell in cells.items() if cell.get("score") is not None}
        if hs:
            out[canon] = {"score": round(sum(hs.values()) / len(hs), 1), "harnesses": hs}
    return out


def _modality_index(lb):
    """canonical -> {'score', 'run'} for a modality board payload (rows carry 'canonical' —
    the join fix: these boards now group by the same canonical id as the text board)."""
    return {m.get("canonical") or m["model"]: {"score": m["composite"], "run": m["run"]}
            for m in lb["models"]}


def _attach_dials(lb):
    """Extend each text-board model row with the six-dial object + the blended aeon_score.
    Purely additive: every pre-existing field is untouched. A dial is null when the model
    was never tested on that axis (null = not tested, NEVER a zero)."""
    if not lb.get("models"):
        return lb
    perf = _perf_percentile_index()
    agentic = _agentic_index()
    vis = _modality_index(vision_leaderboard())
    aud = _modality_index(audio_leaderboard())
    vid = _modality_index(video_leaderboard())
    for m in lb["models"]:
        canon = m["canonical"]
        p, a = perf.get(canon), agentic.get(canon)
        m["dials"] = {
            "intelligence": {"score": m["composite"], "run": m["best_run"]},  # always present
            "performance": p,
            "agentic": a,
            "vision": vis.get(canon),
            "audio": aud.get(canon),
            "video": vid.get(canon),
        }
        comps = {"intelligence": m["composite"],
                 "agentic": a["score"] if a else None,
                 "performance": p["score"] if p else None}
        present = {k: v for k, v in comps.items() if v is not None}
        wsum = sum(AEON_WEIGHTS[k] for k in present)
        parts = {k: (round(AEON_WEIGHTS[k] / wsum, 4) if k in present else 0.0)
                 for k in AEON_WEIGHTS}
        m["aeon_score"] = round(sum(parts[k] * present[k] for k in present), 1) if present else None
        m["aeon_score_parts"] = parts                  # the weights ACTUALLY used (0 = absent)
        m["aeon_provisional"] = len(present) < len(AEON_WEIGHTS)
        # the HIGHEST-composite run among the runs this row aggregates (eligible runs when
        # the model has any) — the run the dashboard auto-opens for the intelligence dial
        m["best_intelligence_run"] = m["best_run"]
    return lb


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
        if r.get("bench_seed"):    # fast-bench subsample — never a model's quality-of-record
            continue
        by_run.setdefault(r["run"], {
            "run": r["run"], "model": r["model"], "canonical": r.get("canonical_id") or r["model"],
            "hf_repo": r.get("hf_repo"), "verified": r.get("model_verified"),
            "trust_tier": r.get("trust_tier") or "self_reported", "started_at": r["started_at"],
            "env_json": r.get("env_json"), "results": []})["results"].append(r)
    idx = {}
    floor = MIN_SUITE_COVERAGE * len(suite_mod.CASES)
    for info in by_run.values():
        # attempted = covered: no_answer rows count toward the floor (same rule as the board)
        if sum(1 for r in info["results"]
               if r["score"] is not None or (r.get("status") or "") == "no_answer") < floor:
            continue
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


def perf_direct_grid(results):
    """Per-run DIRECT-grid extraction — perf.direct.<scope>.c<N> result rows (case_id +
    parsed evidence) -> (direct[conc][scope] metric cells, sorted conc levels). Shared by
    perf_board and the unified benchmark cards so the two can never disagree about what a
    perf run demonstrated. Malformed case ids / evidence are skipped, never raised."""
    direct, concs = {}, set()
    for x in results:
        cid, ev = x.get("case_id") or "", x.get("evidence") or {}
        parts = str(cid).split(".")
        if len(parts) != 4 or parts[0] != "perf" or parts[1] != "direct" \
                or not parts[3].startswith("c"):
            continue
        try:
            conc = int(parts[3][1:])
        except ValueError:
            continue
        if not isinstance(ev, dict):
            ev = {}
        concs.add(conc)
        direct.setdefault(conc, {})[parts[2]] = {
            "ttft_ms": ev.get("ttft_ms_mean"), "ttft_p95": ev.get("ttft_ms_p95"),
            "tpot_ms": ev.get("tpot_ms_mean"),
            "decode_tps": ev.get("decode_tps_mean"),
            "agg_decode_tps": ev.get("agg_decode_tps"),
            "prefill_tps": ev.get("prefill_tps_mean"),
            "n_errors": ev.get("n_errors"),
        }
    return direct, sorted(concs)


def perf_board():
    """PERFORMANCE board: one row per (canonical model × hardware BUCKET) = that pairing's
    LATEST perf run (suite aeon-perf-v1) — a model benched on a Spark AND a 5090 holds a row
    on each rig, so per-hardware clustering never hides a result. Rows unpack into two grids
    the dashboard can chart directly:
      direct[conc][scope]   = {ttft_ms, ttft_p95, tpot_ms, decode_tps, agg_decode_tps, prefill_tps}
      harness[hid][conc][scope] = {mean_task_s, p95_task_s, tasks_per_min, failures}
    scope = a prompt category (Math/Coding/...) or 'overall'. Older runs that predate a
    metric (e.g. TPOT) simply carry null there — the frontend renders gaps honestly.
    Buckets come from hwnorm (Spark counts / RTX by model / Apple chip / Unlabeled); the
    payload adds hw_bucket/hw_family/spark_count per row plus an ordered hardware_groups
    summary — every pre-existing field (flat 'hardwares' list included) is unchanged."""
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
    for info in by_run.values():           # newest perf run per (canonical model, hw bucket)
        info["_hw_label"] = _hw_label(info.get("env") or {})
        info["_hw"] = hwnorm.normalize_label(info["_hw_label"])
        k = (info["canonical"], info["_hw"]["bucket"])
        if k not in latest or (info["started_at"] or 0) > (latest[k]["started_at"] or 0):
            latest[k] = info
    qidx = _quality_index()                          # (canonical, hw) -> best v3 quality composite
    models = []
    for (c, _bucket), info in latest.items():
        direct, dconcs = perf_direct_grid(info["results"])
        harness, concs = {}, set(dconcs)
        for x in info["results"]:
            cid, ev = x.get("case_id") or "", x.get("evidence") or {}
            parts = cid.split(".")
            # perf.harness.<hid>[.<scope>].c<N> (the direct cells are parsed above)
            if len(parts) < 4 or parts[0] != "perf" or parts[1] != "harness" \
                    or not parts[-1].startswith("c"):
                continue
            try:
                conc = int(parts[-1][1:])
            except ValueError:
                continue
            concs.add(conc)
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
        # peak aggregate throughput = the BEST REAL COHORT anywhere in the ladder — one
        # category × concurrency cell (e.g. Coding @ c32). Never the cross-category MEAN
        # row: that understates the demonstrated peak by roughly the category count
        # (each rung's mean averages fast cells with slow ones).
        peak_agg, peak_cell = None, None
        for conc_lvl, scopes in direct.items():
            for cat, cell in scopes.items():
                if cat == "overall" or not isinstance(cell, dict):
                    continue
                a = cell.get("agg_decode_tps")
                if isinstance(a, (int, float)) and (peak_agg is None or a > peak_agg):
                    peak_agg, peak_cell = a, {"category": cat, "conc": conc_lvl}
        hwlabel, hwn = info["_hw_label"], info["_hw"]
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
            # canonical cluster identity (hwnorm): the section this row competes in
            "hw_bucket": hwn["bucket"], "hw_family": hwn["family"],
            "spark_count": hwn["spark_count"],
            "conc_levels": sorted(concs),
            # the four axes the recipe-discovery tool ranks on (per model, filterable by hardware):
            "peak_agg_tps": peak_agg,                     # best real cohort (category × conc cell)
            "peak_agg_cell": peak_cell,                   # provenance: which cell demonstrated it
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
    # Ordered per-bucket summary for the clustered board: Spark buckets first (ascending node
    # count), then every other rig by its best demonstrated peak, Unlabeled always last.
    groups = {}
    for m in models:                       # models are peak-sorted, so first hit = bucket best
        g = groups.setdefault(m["hw_bucket"], {
            "bucket": m["hw_bucket"], "family": m["hw_family"], "label": m["hw_bucket"],
            "spark_count": m["spark_count"], "n_models": 0,
            "best": {"model": m["model"], "peak_agg_tps": m["peak_agg_tps"]}})
        g["n_models"] += 1
    def _gorder(g):
        if g["family"] == hwnorm.FAMILY_SPARK:
            return (0, g["spark_count"] or 0, 0.0, g["bucket"])
        if g["family"] == hwnorm.FAMILY_UNLABELED:
            return (2, 0, 0.0, g["bucket"])
        return (1, 0, -(g["best"]["peak_agg_tps"] or 0), g["bucket"])
    hardware_groups = sorted(groups.values(), key=_gorder)
    return {"categories": suite_mod.CATEGORIES, "models": models,
            "hardwares": hardwares, "hardware_groups": hardware_groups}


# ---- CHAMPION recipes: the winning serve recipe per (hardware × model) ------------------------

# Bench wiring the pod's launcher always re-adds itself (mirrors pod.engines.PROTECTED_FLAGS —
# scoring must not import pod modules on the mothership), stripped from champion payloads so a
# template never carries another lab's alias/host/port.
_WIRING_FLAGS = {"--served-model-name", "--host", "--port", "--alias", "--model-path", "-m"}
# Defense in depth: recipes must never contain credentials, but a champion payload is PUBLIC —
# drop any flag pair that is credential-named or whose value looks like a token.
_SECRET_FLAGS = {"--api-key", "--hf-token", "--huggingface-token", "--token"}
_TOKENISH = re.compile(r"\b(hf_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{16,})")


def _champion_flags(recipe):
    """The applyable serve-flag list from a stored recipe: bench wiring stripped, any
    --speculative-config drafter path normalised to the portable /drafter mount, anything
    credential-shaped dropped. None when the recipe carries no usable flags (not a template)."""
    flags = recipe.get("flags")
    if not isinstance(flags, list) or not flags:
        return None
    toks = [str(f) for f in flags]
    out, i = [], 0
    while i < len(toks):
        f = toks[i]
        val = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].startswith("-") else None
        step = 2 if val is not None else 1
        if not f.startswith("-"):                       # stray positional token — never applyable
            i += 1
            continue
        if f in _WIRING_FLAGS or f.lower() in _SECRET_FLAGS or (val and _TOKENISH.search(val)):
            i += step
            continue
        if f == "--speculative-config" and val:
            try:
                cfg = json.loads(val)
                if isinstance(cfg, dict) and cfg.get("model"):
                    cfg["model"] = "/drafter"           # never leak a bench-host-local path
                    # compact separators: byte-identical to the Run tab's spec-preset option
                    # values, so applying a champion re-selects the matching dropdown preset
                    val = json.dumps(cfg, separators=(",", ":"))
            except Exception:
                pass
        out.append(f)
        if val is not None:
            out.append(val)
        i += step
    return out or None


def _champion_drafter(recipe):
    """Speculative-decode disclosure for a champion, or None (plain decode). Method-aware like
    app._drafter_info: DFlash names its z-lab drafter repo (+ revision + n); native MTP has NO
    drafter, so an mtp champion never advertises one (uses_drafter=False). Method/n come from the
    recorded top-level fields, else from --speculative-config (both flag forms)."""
    n = recipe.get("spec_decode_n") or recipe.get("drafter_n") or recipe.get("drafter_nst")
    method = recipe.get("spec_decode_method") or recipe.get("spec_decode")
    spec_model = None
    flags = recipe.get("flags") if isinstance(recipe.get("flags"), list) else []
    for i, f in enumerate(flags):
        cfg = None
        if f == "--speculative-config" and i + 1 < len(flags):
            try:
                cfg = json.loads(flags[i + 1])
            except Exception:
                pass
        elif isinstance(f, str) and f.startswith("--speculative-config="):
            try:
                cfg = json.loads(f.split("=", 1)[1])
            except Exception:
                pass
        else:
            continue
        if isinstance(cfg, dict):
            method = method or cfg.get("method")
            n = n or cfg.get("num_speculative_tokens")
            spec_model = cfg.get("model")
        break
    if not (recipe.get("drafter") or recipe.get("drafter_repo") or method):
        return None
    method = method or "dflash"
    uses_drafter = bool(recipe.get("drafter") or recipe.get("drafter_repo") or
                        (str(method).lower() == "dflash" and str(spec_model or "").startswith("/drafter")))
    return {"method": method,
            "repo": recipe.get("drafter_repo"),
            "revision": recipe.get("drafter_revision"), "n": n,
            "uses_drafter": uses_drafter}


def _peak_agg_cell(results):
    """Best REAL cohort in a perf run's ladder: max agg_decode_tps over the
    perf.direct.<category>.c<N> cells (never the cross-category mean — see perf_board)."""
    peak, cell = None, None
    for x in results:
        parts = str(x.get("case_id") or "").split(".")
        if (len(parts) != 4 or parts[0] != "perf" or parts[1] != "direct"
                or parts[2] == "overall" or not parts[3].startswith("c")):
            continue
        try:
            conc = int(parts[3][1:])
        except ValueError:
            continue
        a = (x.get("evidence") or {}).get("agg_decode_tps")
        if isinstance(a, (int, float)) and (peak is None or a > peak):
            peak, cell = a, {"category": parts[2], "conc": conc}
    return peak, cell


def champion_recipes(hardware=None, model=None):
    """CHAMPION recipes — per (detected hardware label × canonical model), the perf run with the
    best demonstrated peak aggregate throughput whose model ALSO has a quality composite for that
    pairing (fast AND answers well; the same quality join the perf board uses). Pods pull this
    filtered to their own detected hardware and offer each champion as an applyable template.

    `hardware` matches the canonical hwnorm BUCKET first ('Single DGX Spark', 'NVIDIA RTX 5090'
    — and a free query like 'dgx spark' or '2x dgx spark' normalizes to its bucket), with the
    original exact-label / loose case-insensitive containment kept as the fallback ('dgx spark'
    still matches 'single DGX Spark (GB10)' by containment). `model` matches canonical/hf_repo
    (case-insensitive). Each champion carries its `hw_bucket`. Champions per hardware come back
    sorted best-tok/s-first, so the list IS the per-hardware top list. Defensive by contract: a
    malformed stored recipe/env skips that run — this endpoint never 500s over bad data."""
    rows = db.perf_results()
    by_run = {}
    for r in rows:
        by_run.setdefault(r["run"], {
            "run": r["run"], "model": r["model"],
            "canonical": r.get("canonical_id") or r["model"],
            "hf_repo": r.get("hf_repo"), "hf_revision": r.get("hf_revision"),
            "recipe": r.get("recipe"), "trust_tier": r.get("trust_tier") or "self_reported",
            "env": r.get("env") or {},
            "started_at": r["started_at"], "results": []})["results"].append(r)
    qidx = _quality_index()                      # (canonical, hw) -> best v3 quality composite
    best = {}
    for info in by_run.values():
        try:
            hw = _hw_label(info.get("env") or {})
            if not hw:
                continue                          # no hardware identity — can't be a champion
            peak, cell = _peak_agg_cell(info["results"])
            if peak is None:
                continue
            try:
                recipe = json.loads(info.get("recipe") or "null")
            except Exception:
                recipe = None
            if not isinstance(recipe, dict):
                continue
            serve_flags = _champion_flags(recipe)
            if not serve_flags:
                continue                          # nothing applyable — not a template
            c = info["canonical"]
            q = qidx.get((c, hw)) or qidx.get((c, None))
            if not q:
                continue                          # champions must answer WELL, not just fast
            cur = best.get((hw, c))
            if cur is not None and (cur["peak_agg_tps"] or 0) >= peak:
                continue
            best[(hw, c)] = {
                "hardware": hw,
                "hw_bucket": hwnorm.normalize_label(hw)["bucket"],
                "model": info["hf_repo"] or info["model"], "canonical": c,
                "hf_repo": info["hf_repo"], "hf_revision": info.get("hf_revision"),
                "engine": recipe.get("engine"), "image": recipe.get("image"),
                "serve_flags": serve_flags,
                "spec_decode": recipe.get("spec_decode"),
                "drafter": _champion_drafter(recipe),
                "peak_agg_tps": peak, "peak_agg_cell": cell,
                "quality": q.get("composite"), "quality_run": q.get("run"),
                "trust_tier": info.get("trust_tier"),
                "run": info["run"], "started_at": info.get("started_at"),
            }
        except Exception:
            continue                              # weird stored data: skip the run, never 500
    champions = sorted(best.values(),
                       key=lambda ch: (ch["hardware"].lower(), -(ch["peak_agg_tps"] or 0)))
    hardwares = sorted({ch["hardware"] for ch in champions})
    if hardware:
        hl = str(hardware).strip().lower()
        qn = hwnorm.normalize_label(hardware)     # 'dgx spark'/'2x dgx spark' -> its bucket

        def _hw_hit(ch):
            raw, bkt = ch["hardware"].lower(), (ch.get("hw_bucket") or "").lower()
            if hl == raw or hl in raw or raw in hl:            # legacy loose containment
                return True
            if bkt and (hl == bkt or hl in bkt or bkt in hl):  # bucket name, exact or loose
                return True
            # normalized-query equality — never through the Unlabeled catch-all (a junk
            # query like 'TPU v7' must not sweep up every unlabeled champion)
            return qn["family"] != hwnorm.FAMILY_UNLABELED and qn["bucket"].lower() == bkt
        champions = [ch for ch in champions if _hw_hit(ch)]
    if model:
        ml = str(model).strip().lower()
        champions = [ch for ch in champions
                     if ml == (ch["canonical"] or "").lower()
                     or ml == (ch["hf_repo"] or "").lower()]
    return {"hardwares": hardwares, "champions": champions}


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


def _modality_board(board, categories, ttft_key, tag_board, **tag_kw):
    """Shared modality-board builder (vision/audio/video): one row per CANONICAL model
    (lowercased hf_repo when known — the SAME join key the text/harness/perf boards group
    by, so a run benched under a local alias lines up with its text standing), showing that
    model's LATEST run on this board. Rows carry `canonical` + `hf_repo` alongside the
    original fields; `model` stays the run's declared name."""
    rows = db.all_results_with_runs(board=board)
    by_run = {}
    for r in rows:
        by_run.setdefault(
            r["run"], {"model": r["model"],
                       "canonical": r.get("canonical_id") or r["model"],
                       "hf_repo": r.get("hf_repo"),
                       "started_at": r["started_at"], "results": []}
        )["results"].append(r)

    latest = {}
    for run_id, info in by_run.items():
        k = info["canonical"]
        if k not in latest or info["started_at"] > latest[k]["started_at"]:
            latest[k] = {"run": run_id, **info}

    total_cats = len(categories)
    board_rows = []
    for canonical, info in latest.items():
        model = info["model"]
        cats, ttfts, tpss = {}, [], []
        for r in info["results"]:
            sp = r.get("speed") or {}
            ttfts.append(sp.get(ttft_key))
            tpss.append(sp.get("decode_tps"))
            if r["score"] is not None:
                cats.setdefault(r["category"], []).append(r["score"])
        cat_scores = {c: round(100 * sum(v) / len(v), 1) for c, v in cats.items()}
        composite = round(sum(cat_scores.values()) / len(cat_scores), 1) if cat_scores else 0.0
        board_rows.append({
            "model": model, "canonical": canonical, "hf_repo": info["hf_repo"],
            "run": info["run"], "composite": composite,
            "categories": cat_scores, "coverage": f"{len(cat_scores)}/{total_cats}",
            "avg_" + ttft_key: _avg(ttfts), "avg_decode_tps": _avg(tpss),
            "n_cases": len(info["results"]),
            "vram_est_gb": vram.estimate_gb(model),
            "tags": capabilities.model_tags(model, cat_scores, tag_board, **tag_kw),
        })
    board_rows.sort(key=lambda x: -x["composite"])
    return {"categories": categories, "models": board_rows}


def vision_leaderboard():
    """Separate VISION board (DESIGN §6c). Only models whose latest vision run was
    capability-probed OK appear here (capability_absent runs are excluded by the
    succeeded-status filter). Never merged into the text leaderboard."""
    from . import vision_suite as vs
    return _modality_board("vision", vs.CATEGORIES_VISION, "ttft_after_image_ms", "vision")


def audio_leaderboard():
    """Separate AUDIO board — mirrors vision_leaderboard (board='audio'). Only models
    whose latest audio run was capability-probed OK appear here (capability_absent runs
    are excluded by the succeeded-status filter). Never merged into other boards."""
    from . import audio_suite as auds
    return _modality_board("audio", auds.CATEGORIES_AUDIO, "ttft_after_audio_ms", "audio",
                           audio_ok=True)


def video_leaderboard():
    """Separate VIDEO board — mirrors vision_leaderboard (board='video'). Only models
    whose latest video run was capability-probed OK appear here (capability_absent runs
    are excluded by the succeeded-status filter). Never merged into other boards."""
    from . import video_suite as vids
    return _modality_board("video", vids.CATEGORIES_VIDEO, "ttft_after_video_ms", "video")
