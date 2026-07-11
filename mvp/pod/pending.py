"""pod/pending.py — persisted submission sessions + deferred, idempotent submits (pod only).

The problem this solves (owner's words): a completed benchmark must NEVER be lost to a
down mothership/network — results persist locally and a big "SUBMIT TO MOTHERSHIP" button
(pod dashboard) submits them later; the same job must never land twice.

A SESSION is one bundle's submission state, keyed by its pod-minted job_sig
(sha256 of started_ts|model|hardware|suite — see aeon_pod._job_sig) and written to
~/.aeon/pending_submits/{job_sig}.json the moment the bench opens its mothership run
(even when open_run itself fails: run_id stays None and is minted at submit time).
It holds everything a later submit needs — run_id/run_nonce/run_token, the mothership
base, model/suite/board, the local pod.db rid the results live under, and the bundle
extras (provenance/recipe/environment/artifacts). chmod 600: run_token is a bearer
credential. The file is deleted ONLY after a confirmed final commit (ok or duplicate).

Pod-side only: talks to the mothership exclusively over the signed /api/v1 channel
(pod.aeon_submit) — never imports mothership-private modules (ingest et al.).
"""
from __future__ import annotations

import json
import os
import time

from pod.aeon_submit import DEFAULT_KEY, Pod

DIR = os.path.join(os.path.expanduser("~"), ".aeon", "pending_submits")


def _path(job_sig):
    return os.path.join(DIR, f"{job_sig}.json")


def save(session):
    """Atomic write (tmp + replace) with 0600 — the session carries a bearer run_token."""
    os.makedirs(DIR, exist_ok=True)
    p = _path(session["job_sig"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def load(job_sig):
    try:
        with open(_path(job_sig), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def delete(job_sig):
    try:
        os.unlink(_path(job_sig))
    except OSError:
        pass


def list_all():
    """Every persisted session (newest first) — the Run tab's unsubmitted-results cards
    survive a pod restart through this, not the in-memory job list."""
    out = []
    try:
        names = [n for n in os.listdir(DIR) if n.endswith(".json")]
    except OSError:
        return out
    for n in names:
        s = load(n[:-5])
        if s and s.get("job_sig"):
            out.append(s)
    out.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return out


def collect_results(rid):
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


def open_session(pod, *, job_sig, model, suite_id, board="text", local_rid=None, extra=None):
    """enroll + open_run against the pod's mothership and PERSIST the session. Persists even
    when the mothership is unreachable (run_id stays None) so a fully-offline bench still
    leaves a submittable session behind; a failure here must never block the bench."""
    ses = {"job_sig": job_sig, "run_id": None, "run_nonce": None, "run_token": None,
           "mothership": pod.base, "model": model, "suite_id": suite_id, "board": board,
           "created_at": time.time(), "local_rid": local_rid, "extra": extra or {}}
    try:
        pod.enroll()                                   # idempotent
        st, r = pod.open_run(model, suite_id, board)
        if st == 200:
            ses.update({"run_id": r["run_id"], "run_nonce": r["run_nonce"],
                        "run_token": r["run_token"]})
        else:
            print(f"[pod] open_run refused (HTTP {st} {json.dumps(r)[:160]}) — "
                  f"session persisted; submit later from the pod dashboard")
    except Exception as e:
        print(f"[pod] mothership unreachable at open_run ({e}) — "
              f"session persisted; submit later from the pod dashboard")
    save(ses)
    return ses


def checkpoint_fn(pod, session):
    """Streaming-checkpoint callback for the bench loop: pushes the CUMULATIVE results with
    final=False over the session, so a mid-run kill loses nothing already submitted.
    Network-tolerant by design: one low-retry POST per checkpoint, and consecutive failures
    back off exponentially (skipped calls) so a dead mothership can never stall the bench.
    Returns None when the session has no open mothership run (offline bench)."""
    if not (session and session.get("run_id")):
        return None
    state = {"fail": 0, "skip": 0}
    extra = dict(session.get("extra") or {})

    def cb(results):
        if state["skip"] > 0:
            state["skip"] -= 1
            return
        try:
            st, _ = pod.submit(session["run_id"], session["run_nonce"], session["run_token"],
                               results, final=False, retries=1,
                               job_sig=session["job_sig"], **extra)
        except Exception:
            st = 0
        if st == 200:
            state["fail"] = 0
        else:
            state["fail"] += 1
            state["skip"] = min(2 ** state["fail"], 32)
    return cb


def _job_committed(pod, job_sig):
    """Best-effort mothership pre-check: {exists, run_id, status} or None (unreachable /
    an old mothership without the route)."""
    try:
        r = pod.job_status(job_sig)
        return r if isinstance(r, dict) and "exists" in r else None
    except Exception:
        return None


def _duplicate(job_sig, run_id=None):
    print(f"[pod][submit] duplicate job_sig={job_sig} — "
          f"job already submitted and available on the Mothership")
    delete(job_sig)
    return 200, {"ok": True, "duplicate": True, "run_id": run_id,
                 "message": "job already submitted and available on the Mothership"}


def _try_submit(pod, session, results, retries):
    try:
        return pod.submit(session["run_id"], session["run_nonce"], session["run_token"],
                          results, final=True, retries=retries,
                          job_sig=session["job_sig"], **dict(session.get("extra") or {}))
    except Exception as e:
        return 0, {"error": f"submit failed: {e}"}


def finalize(pod, session, results, *, extra_update=None, retries=5):
    """FINAL commit over a persisted session. Returns (http_status, response).
      - success (ok or duplicate) deletes the session file — the ONLY deletion path;
      - a dead session (mothership reset / run already consumed) heals itself: job_status
        pre-check first, then ONE fresh open_run + submit (job_sig makes that idempotent);
      - failure keeps the file and prints the '[pod][submit] pending' marker the GUI job
        manager turns into the SUBMIT TO MOTHERSHIP button."""
    sig = session["job_sig"]
    if extra_update:                               # e.g. arena artifacts, known only post-bench
        session["extra"] = {**(session.get("extra") or {}), **extra_update}
        save(session)
    js = _job_committed(pod, sig)                  # skip a multi-MB upload the mothership has
    if js and js.get("exists"):
        return _duplicate(sig, js.get("run_id"))
    st, r = 0, {"error": "no open mothership run"}
    if session.get("run_id"):
        st, r = _try_submit(pod, session, results, retries)
        if st == 200:
            if r.get("duplicate"):
                return _duplicate(sig, r.get("run_id"))
            print(f"[pod][submit] ok job_sig={sig} run_id={r.get('run_id')}")
            delete(sig)
            return st, r
        if st in (403, 404, 409):                  # dead session: token/run lost or consumed
            js = _job_committed(pod, sig)          # consumed AND committed? -> duplicate
            if js and js.get("exists"):
                return _duplicate(sig, js.get("run_id"))
            session["run_id"] = None               # else re-open below
    if not session.get("run_id"):
        try:
            pod.enroll()
            ost, orr = pod.open_run(session["model"], session["suite_id"],
                                    session.get("board") or "text")
        except Exception as e:
            ost, orr = 0, {"error": f"open_run failed: {e}"}
        if ost == 200:
            session.update({"run_id": orr["run_id"], "run_nonce": orr["run_nonce"],
                            "run_token": orr["run_token"]})
            save(session)
            st, r = _try_submit(pod, session, results, retries)
            if st == 200:
                if r.get("duplicate"):
                    return _duplicate(sig, r.get("run_id"))
                print(f"[pod][submit] ok job_sig={sig} run_id={r.get('run_id')}")
                delete(sig)
                return st, r
        else:
            st, r = ost, orr
    print(f"[pod][submit] pending job_sig={sig} — submit failed (HTTP {st} "
          f"{json.dumps(r)[:200]}); results persisted — use SUBMIT TO MOTHERSHIP on the "
          f"pod dashboard to submit later")
    return st, r


def submit_pending(job_sig, key_path=None, retries=2, force=False):
    """Deferred submit for a persisted session: re-read the local results from pod.db and
    commit them (final=True). The pod-dashboard SUBMIT TO MOTHERSHIP endpoint calls this.
    COMPLETENESS GATE: refuses a partial run (fewer results than the run's case plan) unless
    `force` — resume the bench to completion first. Returns (http_status, response)."""
    ses = load(job_sig)
    if not ses:
        return 404, {"error": "no pending session for this job_sig"}
    from aeon import db                            # pod-local SQLite (AEON_DB)
    rid = ses.get("local_rid")
    run = db.get_run(rid) if rid else None
    if not run:
        return 410, {"error": "local results for this session are gone (pod.db reset?)",
                     "local_rid": rid}
    results = collect_results(rid)
    expected = run.get("n_cases") or 0
    if not force and expected and len(results) < expected:
        return 409, {"error": "incomplete",
                     "message": f"only {len(results)}/{expected} cases scored — resume the "
                                f"run to completion before submitting"}
    pod = Pod(ses["mothership"], key_path or DEFAULT_KEY)
    return finalize(pod, ses, results, retries=retries)
