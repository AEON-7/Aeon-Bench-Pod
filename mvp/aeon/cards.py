"""Unified benchmark cards — ONE card per pod benchmark JOB, not per run.

One pod job submits several per-board runs (text, agentic harness runs, vision, audio,
video, perf; arena artifacts ride the text bundle). Today each is its own row in
Submissions; this module groups them back into the job:

  * NEW pods stamp every bundle with job_group = sha256(started|model|hw)[:24]
    (aeon_pod._job_ctx) -> card_id "jg:<job_group>".
  * LEGACY runs (job_group NULL) cluster by time: per (canonical model, hardware label),
    consecutive runs join one cluster while the gap <= LEGACY_JOB_GAP_S (3 h)
    -> card_id "lg:<first run id>" (the cluster's earliest run). Runs WITH a job_group
    never join legacy clusters.

The API contract (mirrored by the dashboard frontend — do not drift):
  GET /api/submissions/cards  -> {"cards": [card, ...]}
  GET /api/compare_cards?a=&b= -> {"a": card, "b": card, "sections": {...}}
Every section key is ALWAYS present in compare "sections"; a side the card lacks is
null, so the frontend can render the parity filler ("no <section> results for this run").

Defensive by contract: malformed stored rows (env_json, recipe, evidence) skip that
run/section — these endpoints never 500 over bad data.
"""
from __future__ import annotations

import json
import re

from . import db, scoring

# Legacy time-cluster rule: runs of the same (canonical model, hardware) with gaps of at
# most 3 hours between consecutive starts are treated as one job.
LEGACY_JOB_GAP_S = 10800

# How many chars of a case's raw output ride a compare payload (full text stays behind
# /api/submissions/{run_id}).
ANSWER_CAP = 4000

# Attribution slack for arena artifacts (saved at the text bundle's final commit, which
# always lands inside the job window server-side; slack absorbs clock jitter + commits
# that land moments after finished_at is stamped).
_ART_SLACK_BEFORE = 900
_ART_SLACK_AFTER = 3600

_SINGLE_BOARDS = ("text", "vision", "audio", "video")   # one run -> one card slot each


def _env(run):
    try:
        e = json.loads(run.get("env_json") or "{}")
        return e if isinstance(e, dict) else {}
    except Exception:
        return {}


def _recipe(run):
    try:
        r = json.loads(run.get("recipe") or "null")
        return r if isinstance(r, dict) else None
    except Exception:
        return None


def _hw(run):
    return scoring._hw_label(_env(run))


def _engine(run):
    eng = (_env(run).get("engine") or {})
    name = eng.get("name") if isinstance(eng, dict) else None
    if name:
        return name
    rec = _recipe(run)
    return rec.get("engine") if rec else None


def _sanitize_model(name):
    """The same sanitize ingest applies to artifact model names, so matching is exact."""
    return re.sub(r"[<>\"'`]", "", name or "")[:80]


# ---- grouping --------------------------------------------------------------------------


def _legacy_clusters(runs):
    """Time-clusters over the job_group-NULL runs: per (canonical, hardware label), sort by
    started_at; consecutive runs join one cluster while the gap <= LEGACY_JOB_GAP_S."""
    by_key = {}
    for r in runs:
        if r.get("job_group"):
            continue                       # explicit-group runs NEVER join legacy clusters
        key = (r.get("canonical_id") or r.get("model"), _hw(r))
        by_key.setdefault(key, []).append(r)
    clusters = []
    for rs in by_key.values():
        rs.sort(key=lambda r: r.get("started_at") or 0)
        cur, last = None, None
        for r in rs:
            t = r.get("started_at") or 0
            if cur is None or (t - last) > LEGACY_JOB_GAP_S:
                cur = []
                clusters.append(cur)
            cur.append(r)
            last = t
    return clusters


def _groups(runs):
    """[(card_id, [runs])] over every succeeded run: explicit job groups first ('jg:'),
    then legacy time-clusters ('lg:<first run id>')."""
    jg = {}
    for r in runs:
        g = r.get("job_group")
        if g:
            jg.setdefault(g, []).append(r)
    out = [("jg:" + g, sorted(rs, key=lambda r: r.get("started_at") or 0))
           for g, rs in jg.items()]
    for cluster in _legacy_clusters(runs):
        out.append(("lg:" + cluster[0]["id"], cluster))
    return out


# ---- card assembly ---------------------------------------------------------------------


def _score_slot(run, cats, counts):
    """The text-shaped board payload: composite + per-category scores for one run."""
    c = cats.get(run["id"]) or {}
    composite = round(sum(c.values()) / len(c), 1) if c else 0.0
    return {"run": run["id"], "composite": composite, "categories": c,
            "n_cases": (counts.get(run["id"]) or {}).get("rows", 0),
            "suite_id": run.get("suite_id"), "flagged": bool(run.get("flagged"))}


def _perf_slot(run):
    """Peaks + rungs for the card's perf chip, from the same direct-grid extraction the
    perf board uses (scoring.perf_direct_grid / _peak_agg_cell)."""
    direct, concs = _perf_direct(run["id"])
    peak_agg, _cell = _peak_from_direct(direct)
    c_lo = (direct.get(concs[0]) if concs else {}) or {}
    return {"run": run["id"], "peak_agg_tps": peak_agg,
            "peak_single_tps": scoring._lowest_conc_metric(c_lo, "decode_tps", max),
            "conc_levels": concs, "flagged": bool(run.get("flagged"))}


def _perf_direct(run_id):
    """(direct grid, conc levels) for one perf run — evidence rows via db.get_run."""
    try:
        run = db.get_run(run_id)
        return scoring.perf_direct_grid(run.get("results") or []) if run else ({}, [])
    except Exception:
        return {}, []


def _peak_from_direct(direct):
    """Best REAL cohort (max agg_decode_tps over category x conc cells; never 'overall' —
    same rule as scoring._peak_agg_cell). Returns (peak, {category, conc}|None)."""
    peak, cell = None, None
    for conc, scopes in direct.items():
        for cat, v in scopes.items():
            if cat == "overall" or not isinstance(v, dict):
                continue
            a = v.get("agg_decode_tps")
            if isinstance(a, (int, float)) and (peak is None or a > peak):
                peak, cell = a, {"category": cat, "conc": conc}
    return peak, cell


def _card_window(runs):
    starts = [r.get("started_at") for r in runs if r.get("started_at")]
    ends = [r.get("finished_at") for r in runs if r.get("finished_at")]
    t0 = min(starts) if starts else None
    t1 = max(ends) if ends else None
    return t0, t1


def _arena_slot(card_runs, artifacts):
    """Arena artifacts confidently attributable to this card: the artifact's model name
    (or its '<model> @<harness>' base) matches the card's model/hf_repo AND its created_at
    falls inside the card window (+slack). Heuristic — artifacts carry no run link — so the
    payload says so in `note`. None when nothing attributable."""
    names = set()
    for r in card_runs:
        for n in (r.get("model"), r.get("hf_repo")):
            s = _sanitize_model(n)
            if s:
                names.add(s)
    t0, t1 = _card_window(card_runs)
    if t0 is None or not names:
        return None
    lo, hi = t0 - _ART_SLACK_BEFORE, (t1 or t0) + _ART_SLACK_AFTER
    kinds, matched = {}, []
    for a in artifacts:
        base = (a.get("model") or "").split(" @", 1)[0]
        ts = a.get("created_at") or 0
        if base in names and lo <= ts <= hi:
            matched.append(a)
            kinds[a.get("kind")] = kinds.get(a.get("kind"), 0) + 1
    if not matched:
        return None
    return {"n_artifacts": len(matched), "kinds": kinds,
            "note": "attributed by model name + job time window (artifacts carry no run link)"}


def _card(card_id, runs, cats, counts, means, artifacts):
    """One unified benchmark card over a job's runs — the list-endpoint contract shape."""
    runs = sorted(runs, key=lambda r: r.get("started_at") or 0)
    # identity comes from the primary (non-harness text) run when present, else the first
    text = [r for r in runs if r.get("board") == "text" and not r.get("harness")]
    anchor = (text or runs)[0]
    hf_repo = next((r.get("hf_repo") for r in runs if r.get("hf_repo")), None)
    boards = {"text": None, "agentic": [], "vision": None, "audio": None, "video": None,
              "perf": None, "arena": None}
    for b in _SINGLE_BOARDS:
        # latest run wins the slot if a cluster ever holds two passes of one board
        slot = [r for r in runs if r.get("board") == b and not r.get("harness")]
        if slot:
            boards[b] = _score_slot(slot[-1], cats, counts)
    seen_h = {}
    for r in runs:                                     # latest run per harness id
        if r.get("harness"):
            seen_h[r["harness"]] = r
    for hid in sorted(seen_h):
        r = seen_h[hid]
        cnt = counts.get(r["id"]) or {}
        m = means.get(r["id"])
        boards["agentic"].append({
            "harness": hid, "harness_version": r.get("harness_version"), "run": r["id"],
            "score": round(100 * m, 1) if m is not None else None,
            "n_cases": cnt.get("rows", 0), "flagged": bool(r.get("flagged"))})
    perf = [r for r in runs if r.get("board") == "perf"]
    if perf:
        boards["perf"] = _perf_slot(perf[-1])
    boards["arena"] = _arena_slot(runs, artifacts)
    t0, t1 = _card_window(runs)
    return {
        "card_id": card_id,
        "model": anchor.get("model"),
        "canonical": anchor.get("canonical_id") or anchor.get("model"),
        "hf_repo": hf_repo,
        "verified": anchor.get("model_verified"),
        "trust_tier": anchor.get("trust_tier") or "self_reported",
        "hardware": _hw(anchor),
        "engine": _engine(anchor),
        "started_at": t0,
        "finished_at": t1,
        "flagged_any": any(r.get("flagged") for r in runs),
        "boards": boards,
        "run_ids": [r["id"] for r in runs],
    }


def submission_cards(limit=100):
    """The Submissions CARD list: one card per job (explicit job_group or legacy time
    cluster), newest first. Malformed rows never 500 — a run that can't be summarized is
    skipped."""
    runs = db.runs_for_cards()
    cats = db.run_category_scores()
    counts = db.run_case_counts()
    means = db.run_mean_scores()
    try:
        artifacts = db.list_artifacts()
    except Exception:
        artifacts = []
    cards = []
    for card_id, rs in _groups(runs):
        try:
            cards.append(_card(card_id, rs, cats, counts, means, artifacts))
        except Exception:
            continue
    cards.sort(key=lambda c: c.get("started_at") or 0, reverse=True)
    return {"cards": cards[: max(1, int(limit))]}


# ---- compare ---------------------------------------------------------------------------


def _resolve(card_id, runs):
    """card_id -> the job's run rows, or None. 'jg:x' selects runs WHERE job_group=x;
    'lg:<run_id>' recomputes the legacy clusters and returns the one CONTAINING that run
    (any member id resolves, though cards are keyed by the first)."""
    if not isinstance(card_id, str):
        return None
    if card_id.startswith("jg:"):
        g = card_id[3:]
        sel = [r for r in runs if r.get("job_group") == g]
        return sel or None
    if card_id.startswith("lg:"):
        rid = card_id[3:]
        for cluster in _legacy_clusters(runs):
            if any(r["id"] == rid for r in cluster):
                return cluster
    return None


def _run_cases(run_id):
    """[{case_id, category, tier, score, answer}] for one run — raw output via
    db.result_output, truncated to ANSWER_CAP. Empty list when the run vanished."""
    try:
        run = db.get_run(run_id)
    except Exception:
        run = None
    out = []
    for x in (run or {}).get("results") or []:
        try:
            out.append({"case_id": x.get("case_id"), "category": x.get("category"),
                        "tier": x.get("tier"), "score": x.get("score"),
                        "answer": (db.result_output(x) or "")[:ANSWER_CAP]})
        except Exception:
            continue
    out.sort(key=lambda c: (c.get("category") or "", c.get("case_id") or ""))
    return out


def _text_section(run, cats):
    c = cats.get(run["id"]) or {}
    return {"composite": round(sum(c.values()) / len(c), 1) if c else 0.0,
            "categories": c, "suite_id": run.get("suite_id"),
            "suite_hash": run.get("suite_hash"), "cases": _run_cases(run["id"])}


def _agentic_section(hruns, means):
    """{harnesses: {hid: {score, version, tasks}}} — score is the 0-100 mean over scored
    tasks (same figure the harness board shows); per-task scores stay raw 0..1 (like
    compare_runs cases)."""
    harnesses = {}
    for hid in sorted(hruns):
        r = hruns[hid]
        m = means.get(r["id"])
        harnesses[hid] = {
            "score": round(100 * m, 1) if m is not None else None,
            "version": r.get("harness_version"),
            "tasks": [{"case_id": x["case_id"], "score": x["score"]}
                      for x in _run_cases(r["id"])]}
    return {"harnesses": harnesses}


# the exact metric set the compare contract carries per direct-grid cell
_DIRECT_METRICS = ("ttft_ms", "tpot_ms", "decode_tps", "agg_decode_tps", "prefill_tps")


def _perf_section(run):
    direct, concs = _perf_direct(run["id"])
    peak, cell = _peak_from_direct(direct)
    slim = {conc: {scope: {k: v.get(k) for k in _DIRECT_METRICS}
                   for scope, v in scopes.items() if isinstance(v, dict)}
            for conc, scopes in direct.items()}
    return {"direct": slim, "peak_agg_tps": peak, "peak_cell": cell, "conc_levels": concs}


def _arena_section(card_runs, artifacts):
    names = set()
    for r in card_runs:
        for n in (r.get("model"), r.get("hf_repo")):
            s = _sanitize_model(n)
            if s:
                names.add(s)
    t0, t1 = _card_window(card_runs)
    if t0 is None or not names:
        return None
    lo, hi = t0 - _ART_SLACK_BEFORE, (t1 or t0) + _ART_SLACK_AFTER
    arts = [{"aid": a.get("id"), "kind": a.get("kind"), "prompt_id": a.get("prompt_id"),
             "ok": bool(a.get("ok"))}
            for a in artifacts
            if (a.get("model") or "").split(" @", 1)[0] in names
            and lo <= (a.get("created_at") or 0) <= hi]
    return {"artifacts": arts} if arts else None


def _recipe_section(card_runs):
    """Serve-recipe disclosure for one side: engine/image/digest + the SANITIZED applyable
    flags (scoring._champion_flags strips bench wiring + anything credential-shaped — this
    payload is public) and the drafter disclosure object as spec_decode."""
    text = [r for r in card_runs if r.get("board") == "text" and not r.get("harness")]
    ordered = text + [r for r in card_runs if r not in text]
    for r in ordered:
        rec = _recipe(r)
        if not rec:
            continue
        try:
            return {"engine": rec.get("engine") or _engine(r),
                    "image": rec.get("image"),
                    "image_digest": rec.get("image_digest"),
                    "serve_flags": scoring._champion_flags(rec) or [],
                    "spec_decode": scoring._champion_drafter(rec)}
        except Exception:
            continue
    # no stored recipe anywhere in the job (self-reported/endpoint runs) — still disclose
    # the engine when the environment recorded one
    eng = next((e for e in (_engine(r) for r in ordered) if e), None)
    if eng:
        return {"engine": eng, "image": None, "image_digest": None,
                "serve_flags": [], "spec_decode": None}
    return None


def _sections_for(card_runs, cats, means, artifacts):
    """One side's per-section payloads (None where the card lacks the section)."""
    runs = sorted(card_runs, key=lambda r: r.get("started_at") or 0)
    out = {}
    for b in _SINGLE_BOARDS:
        slot = [r for r in runs if r.get("board") == b and not r.get("harness")]
        try:
            out[b] = _text_section(slot[-1], cats) if slot else None
        except Exception:
            out[b] = None
    hruns = {}
    for r in runs:
        if r.get("harness"):
            hruns[r["harness"]] = r
    try:
        out["agentic"] = _agentic_section(hruns, means) if hruns else None
    except Exception:
        out["agentic"] = None
    perf = [r for r in runs if r.get("board") == "perf"]
    try:
        out["perf"] = _perf_section(perf[-1]) if perf else None
    except Exception:
        out["perf"] = None
    try:
        out["arena"] = _arena_section(runs, artifacts)
    except Exception:
        out["arena"] = None
    try:
        out["recipe"] = _recipe_section(runs)
    except Exception:
        out["recipe"] = None
    return out


# every key the compare contract promises, in render order — ALWAYS present in "sections"
SECTION_KEYS = ("text", "agentic", "vision", "audio", "video", "perf", "arena", "recipe")


def compare_cards(a, b):
    """FULL-PARITY side-by-side of two benchmark cards. Both card forms resolve ('jg:' /
    'lg:'); a missing card returns {'error': ...} for the endpoint to 404. Every section
    key is present; a side without that section is null."""
    runs = db.runs_for_cards()
    ga, gb = _resolve(a, runs), _resolve(b, runs)
    if not ga:
        return {"error": f"card {a} not found"}
    if not gb:
        return {"error": f"card {b} not found"}
    cats = db.run_category_scores()
    counts = db.run_case_counts()
    means = db.run_mean_scores()
    try:
        artifacts = db.list_artifacts()
    except Exception:
        artifacts = []

    def _cid(group):        # canonical card id for the resolved group
        rs = sorted(group, key=lambda r: r.get("started_at") or 0)
        g = rs[0].get("job_group")
        return ("jg:" + g) if g else ("lg:" + rs[0]["id"])

    sa = _sections_for(ga, cats, means, artifacts)
    sb = _sections_for(gb, cats, means, artifacts)
    return {
        "a": _card(_cid(ga), ga, cats, counts, means, artifacts),
        "b": _card(_cid(gb), gb, cats, counts, means, artifacts),
        "sections": {k: {"a": sa.get(k), "b": sb.get(k)} for k in SECTION_KEYS},
    }
