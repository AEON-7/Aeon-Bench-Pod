"""aeon.audit — QA pass over DETERMINISTIC scoring, so a buggy checker can't silently tank a model.

Two layers, cheapest first:

1. mechanical re-check (deterministic, free): re-run the CURRENT checkers over each stored answer.
   A score-0 case that now PASSES is a checker-fix false-negative — a checker bug was fixed since
   the run (exactly the class the whitespace/anchored-regex fix resolved). `rescore_*` reports these
   and (with apply=True) corrects the recorded scores. This needs NO judge.

2. agent-judge audit (for what's STILL failing): hand a FRONTIER judge the full SCORECARD — prompt +
   the model's answer + the exact checkers/gold + why the checker failed — and ask only:
   "is the answer actually correct, i.e. is the checker WRONG here?" It FLAGS likely checker bugs
   for review; it never changes the deterministic scores. The deterministic tier stays the ranking
   authority — the judge is a lens on it, never the ranker (and never the model under test).

CLI:
  python -m aeon.audit rescore [--run RUN_ID] [--apply]         # layer 1
  python -m aeon.audit judge --judge-url URL --judge-key KEY --judge-model M [--run RUN_ID]  # layer 2
"""
from __future__ import annotations

import json
from . import db, evaluators
from . import suite as suite_mod

_BY_ID = None


def _case(cid):
    global _BY_ID
    if _BY_ID is None:
        _BY_ID = {c["id"]: c for c in suite_mod.CASES}
    return _BY_ID.get(cid)


def recheck(case_id, raw_output):
    """Re-run the CURRENT deterministic checkers on a stored answer -> (new_score, evidence).
    Returns (None, ...) if the case isn't in the current suite or isn't deterministic."""
    case = _case(case_id)
    if not case or case.get("tier") != 0:
        return None, {"skip": "not a current tier-0 case"}
    try:
        return evaluators.evaluate(case, raw_output or "", None)
    except Exception as e:
        return 0.0, {"error": f"recheck error: {e!r}"}


def _tier0_results(run):
    for r in run.get("results", []):
        if r.get("tier") == 0:
            yield r


def rescore_run(run_id, apply=False):
    """Re-check every tier-0 result in a run with the current checkers. Returns the cases whose
    score CHANGED (mostly 0 -> pass: checker-fix false-negatives). apply=True updates the DB."""
    run = db.get_run(run_id)
    if not run:
        return {"run": run_id, "error": "not found", "changed": []}
    changed = []
    for r in _tier0_results(run):
        new, ev = recheck(r["case_id"], db.result_output(r))
        old = r.get("score")
        if new is not None and new != old:
            changed.append({"case_id": r["case_id"], "old": old, "new": new})
            if apply:
                db.update_result(run_id, r["case_id"], status="scored", score=new, evidence=ev)
                if new == 1.0:
                    db.flag_disputed(run_id, r["case_id"], False)   # checker fixed -> dispute resolved
    return {"run": run_id, "model": run.get("model"), "changed": changed, "n_changed": len(changed)}


def rescore_all(apply=False, limit=500):
    """rescore_run over every stored run — corrects historical scores after a checker fix."""
    out = [rescore_run(r["id"], apply=apply) for r in db.list_runs(limit=limit)]
    out = [o for o in out if o.get("n_changed")]
    return {"runs_touched": len(out), "total_corrected": sum(o["n_changed"] for o in out), "runs": out}


def frontier_judge(url, key, model, temperature=0.0):
    """A judge callable for layer 2, backed by any OpenAI-compatible frontier endpoint. It must NOT
    be the model under test. Returns {checker_false_negative: bool, reason: str}."""
    from .targets import build_target
    tgt = build_target(model, url, key)

    def judge(scorecard):
        prompt = (
            "You audit a DETERMINISTIC benchmark checker for FALSE NEGATIVES. A programmatic checker "
            "scored this answer 0 (FAIL). Decide ONLY whether the answer actually satisfies the task "
            "and the intended criteria — i.e. whether the CHECKER is wrong here.\n\n"
            f"TASK:\n{scorecard['prompt']}\n\n"
            f"MODEL ANSWER:\n{scorecard['answer']}\n\n"
            f"GRADING CRITERIA (checkers + gold):\n{json.dumps(scorecard['eval'])}\n\n"
            f"WHY THE CHECKER FAILED IT:\n{json.dumps(scorecard.get('evidence'))}\n\n"
            "Be strict: only call it a false negative if the answer is genuinely correct and the "
            "checker/gold is the problem (whitespace, over-strict format regex, bad extraction, wrong "
            "gold, checker logic). If the answer is truncated, wrong, or violates a real constraint, it "
            "is NOT a false negative. Reply with ONLY minified JSON: "
            '{"checker_false_negative": true|false, "reason": "..."}'
        )
        resp = tgt.chat([{"role": "user", "content": prompt}], temperature=temperature, max_tokens=800)
        txt = (resp.get("text") or "").strip()
        try:
            s = txt[txt.index("{"): txt.rindex("}") + 1]
            v = json.loads(s)
            return {"checker_false_negative": bool(v.get("checker_false_negative")), "reason": v.get("reason", "")[:500]}
        except Exception:
            return {"checker_false_negative": False, "reason": f"unparseable judge reply: {txt[:120]}"}
    return judge


def agent_audit(judge, run_id=None, board="text", min_len=15):
    """Layer 2: ask the frontier `judge` about every STILL-failing tier-0 case with a real (non-empty,
    non-truncation) answer. Flags likely checker bugs; changes NO scores. `judge` is a callable
    (scorecard)->{checker_false_negative, reason}, e.g. from frontier_judge()."""
    runs = [db.get_run(run_id)] if run_id else [db.get_run(r["id"]) for r in db.list_runs(limit=200)]
    flagged, seen = [], set()
    for run in runs:
        if not run:
            continue
        for r in _tier0_results(run):
            cid = r["case_id"]
            if r.get("score") not in (0, 0.0) or cid in seen:
                continue
            ans = db.result_output(r) or ""
            case = _case(cid)
            if len(ans.strip()) < min_len or not case:
                continue                      # empty/truncated or not-in-suite -> not a checker issue
            seen.add(cid)
            v = judge({"prompt": case["prompt"], "answer": ans[:3000], "eval": case["eval"],
                       "evidence": r.get("evidence"), "category": r["category"]})
            if v.get("checker_false_negative"):
                db.flag_disputed(run["id"], cid, True, (v.get("reason") or "")[:500])   # persist the flag
                flagged.append({"run": run["id"], "case_id": cid, "category": r["category"],
                                "reason": v.get("reason", "")})
    return {"flagged_checker_bugs": len(flagged), "cases": flagged}


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="aeon.audit", description="QA pass over deterministic scoring")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("rescore", help="re-run current checkers; report/correct checker-fix false-negatives")
    pr.add_argument("--run", default=None); pr.add_argument("--apply", action="store_true")
    pj = sub.add_parser("judge", help="frontier agent-judge over still-failing cases (flags checker bugs)")
    pj.add_argument("--run", default=None)
    pj.add_argument("--judge-url", required=True); pj.add_argument("--judge-key", default=None)
    pj.add_argument("--judge-model", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "rescore":
        res = rescore_run(a.run, apply=a.apply) if a.run else rescore_all(apply=a.apply)
        print(json.dumps(res, indent=2))
    else:
        j = frontier_judge(a.judge_url, a.judge_key, a.judge_model)
        print(json.dumps(agent_audit(j, run_id=a.run), indent=2))


if __name__ == "__main__":
    import sys
    _main(sys.argv[1:])
