"""Self-test: benchmark resume + deferred idempotent submission (job_sig).

Covers the owner's contract end-to-end, fully offline (temp SQLite, throwaway ed25519):
  * job_sig determinism (timestamp|model|hardware|suite digest) + presence in the
    signed bundle Pod.submit builds;
  * ingest duplicate path: enroll -> open_run -> signed submit TWICE with the same
    job_sig — the second answers {ok, duplicate, run_id} with the owner's exact
    message, ONE run row exists, and the second nonce is released as 'duplicate'
    (never quarantined); bundles WITHOUT job_sig keep today's behaviour (two runs);
  * completeness gate: an incomplete local run never reaches finalize via
    pending.submit_pending (409), force=True overrides;
  * resume: runner.run_benchmark skips done_case_ids, re-opens the interrupted row
    in place, stamps job_sig on the runs row, and find_resumable_run anchors it.

Runs fully offline. From the mvp dir:  python test_resume_submit.py
"""
import json
import os
import sys
import tempfile
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Point the DB at a throwaway SQLite file BEFORE importing aeon (db.py reads the env at
# import time; AEON_DB_URL would select the prod Postgres backend — kill it).
_TMP = tempfile.mkdtemp(prefix="aeon-resume-selftest-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("AEON_DB_URL", None)
os.environ.pop("AEON_ATTESTED_ONLY", None)

from aeon import db, runner                                # noqa: E402
from aeon import suite as suite_mod                        # noqa: E402
from pod import pending                                    # noqa: E402
from pod.aeon_pod import _job_ctx, _job_sig, _missing_case_ids   # noqa: E402
from pod.aeon_submit import Pod, _canon                    # noqa: E402

# aeon/ingest.py is the MOTHERSHIP receiving side and is deliberately absent from the public pod
# repo, where this file also runs. Everything it checks about the POD — job_sig minting, the signed
# bundle, the completeness gate, resume, the local run rows — needs no ingest at all. Only the
# sections that drive the server's enroll/commit/duplicate path do, and those announce themselves
# as SKIP rather than quietly vanishing: a test that silently drops coverage is a test that stopped
# complaining. (Same guard as test_artifact_size_budget.py.)
try:
    from aeon import ingest                                # noqa: E402
except ImportError:
    ingest = None

db.init_db()
pending.DIR = os.path.join(_TMP, "pending_submits")        # never touch the real ~/.aeon
KEY = os.path.join(_TMP, "device_key.pem")

PASS = 0
SKIPPED = []


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("PASS:", label)


def skip(label):
    """Announce a section that cannot run HERE. Loud and counted — never silent."""
    SKIPPED.append(label)
    print("SKIP:", label, "— needs aeon/ingest.py (mothership-only, not in the pod repo)")


# ---------- 1) job_sig: determinism + presence in the signed bundle ----------
ctx = {"started": "2026-07-11T00:00:00Z", "model": "lab/model-a", "hw": "single DGX Spark (GB10)"}
s1 = _job_sig(ctx, "aeon-suite-v3")
ok(s1 == _job_sig(dict(ctx), "aeon-suite-v3"), "job_sig is deterministic for identical inputs")
ok(len(s1) == 24 and all(c in "0123456789abcdef" for c in s1), "job_sig is a 24-char hex digest")
ok(s1 != _job_sig({**ctx, "started": "2026-07-11T00:00:01Z"}, "aeon-suite-v3"),
   "a new launch timestamp mints a NEW job_sig")
ok(s1 != _job_sig(ctx, "aeon-suite-v3@hermes"),
   "per-bundle suite scope disambiguates one comprehensive job's bundles")
jc = _job_ctx("m", {"detected_label": "RTX 5090 32GB"}, started="2026-01-01T00:00:00Z")
ok(jc["hw"] == "RTX 5090 32GB" and jc["started"] == "2026-01-01T00:00:00Z",
   "_job_ctx uses the DETECTED hardware label + the pinned start ts")
ok(jc["group"] == _job_ctx("m", {"detected_label": "RTX 5090 32GB"},
                           started="2026-01-01T00:00:00Z")["group"]
   and len(jc["group"]) == 24 and all(c in "0123456789abcdef" for c in jc["group"]),
   "_job_ctx mints a deterministic 24-hex job GROUP (started|model|hw, no suite scope)")
ok(jc["group"] != _job_ctx("m", {"detected_label": "RTX 5090 32GB"},
                           started="2026-01-01T00:00:01Z")["group"],
   "a new launch timestamp mints a NEW job_group")

pod_client = Pod("http://mothership.invalid", KEY)
captured = {}
pod_client._post = lambda path, obj, headers=None, retries=5: (captured.update(obj), (200, {"ok": True}))[1]
st, _ = pod_client.submit("rid", "nonce", "tok", [{"case_id": "c1", "category": "x"}],
                          job_sig=s1, job_group=jc["group"])
ok(st == 200 and captured["bundle"]["job_sig"] == s1, "Pod.submit carries job_sig in the bundle")
ok(captured["bundle"]["job_group"] == jc["group"], "Pod.submit carries job_group in the bundle")
ok("signature" in captured, "the bundle is still signed")

# ---------- 2a) the dedup ANCHOR itself (aeon/db.py — present in the pod repo) ----------
# The mothership's idempotent-duplicate answer is built on db.find_run_by_job_sig. That function
# lives in aeon/db.py, which IS here, so its contract stays covered on the pod side even when the
# ingest caller is absent. A DISTINCT job_sig from the ingest block below, so seeding this row can
# never masquerade as the duplicate the ingest section is trying to provoke.
SIG_LOCAL = "9" * 24
GROUP_LOCAL = "8" * 24
rid_anchor = uuid.uuid4().hex[:10]
db.create_run(rid_anchor, model="lab/anchor", target_url="pod-submission", judge_model=None,
              judge_is_self=False, suite_id="aeon-suite-v3", suite_hash="h", n_cases=1,
              params={}, env={}, job_sig=SIG_LOCAL, job_group=GROUP_LOCAL)
db.save_result(rid_anchor, "c1", category="math", tier=0, status="scored", score=1.0,
               raw_output="ok", evidence={}, speed={})
ok(db.get_run(rid_anchor)["job_sig"] == SIG_LOCAL, "job_sig is stored on the run row")
ok(db.get_run(rid_anchor)["job_group"] == GROUP_LOCAL, "job_group is stored on the run row")
ok(db.find_run_by_job_sig(SIG_LOCAL) is None,
   "an UNFINISHED run carrying the job_sig is NOT an anchor (a retry is never blocked)")
db.finish_run(rid_anchor, "succeeded")
ok(db.find_run_by_job_sig(SIG_LOCAL)["id"] == rid_anchor,
   "find_run_by_job_sig anchors the COMMITTED run")
ok(db.find_run_by_job_sig(None) is None and db.find_run_by_job_sig("0" * 24) is None,
   "an empty/unknown job_sig anchors nothing")

# ---------- 2b) ingest duplicate path (real ed25519, ingest functions directly) ----------
SIG = "f" * 24
GROUP = "e" * 24


def enroll_and_open(model="lab/model-a", suite="aeon-suite-v3"):
    ch = ingest.issue_challenge()
    r, code = ingest.enroll(pod_client.pub, ch, pod_client._sign(ch.encode()))
    assert code == 200, r
    body = {"action": "open_run", "public_key": pod_client.pub, "model": model,
            "suite_id": suite, "board": "text"}
    r, code = ingest.open_run(pod_client.pub, pod_client._sign(_canon(body)),
                              model=model, suite_id=suite, board="text")
    assert code == 200, r
    return r


def signed_submit(opened, job_sig=None, job_group=None):
    bundle = {"run_id": opened["run_id"], "run_nonce": opened["run_nonce"], "final": True,
              "results": [{"case_id": "c1", "category": "math", "tier": 0,
                           "status": "scored", "score": 1.0, "raw_output": "ok"}]}
    if job_sig:
        bundle["job_sig"] = job_sig
    if job_group:
        bundle["job_group"] = job_group
    raw = json.dumps({"bundle": bundle, "signature": pod_client._sign(_canon(bundle))}).encode()
    return ingest.submit_results(opened["run_id"], opened["run_token"], raw)


if ingest is None:
    skip("mothership duplicate path (enroll -> open_run -> double signed submit)")
else:
    o1 = enroll_and_open()
    r1, c1 = signed_submit(o1, job_sig=SIG, job_group=GROUP)
    ok(c1 == 200 and r1.get("ok") and not r1.get("duplicate"), "first submit commits normally")
    ok(db.get_run(o1["run_id"])["job_sig"] == SIG, "job_sig stored on the committed run row")
    ok(db.get_run(o1["run_id"])["job_group"] == GROUP, "job_group stored on the committed run row")
    ok(db.find_run_by_job_sig(SIG)["id"] == o1["run_id"],
       "find_run_by_job_sig anchors the committed run")

    o2 = enroll_and_open()
    r2, c2 = signed_submit(o2, job_sig=SIG)
    ok(c2 == 200 and r2.get("ok") and r2.get("duplicate") is True,
       "second submit answers idempotent success")
    ok(r2.get("run_id") == o1["run_id"], "duplicate answer points at the EXISTING run")
    ok(r2.get("message") == "job already submitted and available on the Mothership",
       "duplicate message is the owner's exact wording")
    ok(db.get_run(o2["run_id"]) is None, "no second run row was stored")
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM runs WHERE job_sig=?", (SIG,)).fetchone()[0]
    ok(n == 1, "exactly one run carries the job_sig")
    pr2 = db.get_pod_run(o2["run_id"])
    ok(pr2["status"] == "duplicate" and pr2["reason"] == "DUPLICATE_JOB",
       "the duplicate's nonce is released cleanly (status 'duplicate', not quarantined)")
    k = db.get_enrolled_key(pod_client.pub)
    ok(k["status"] == "active" and not k["fail_count"], "duplicate never bumps the forgery counter")

    # back-compat: bundles WITHOUT job_sig behave exactly as today (both commit)
    oa, ob = enroll_and_open(model="lab/legacy"), enroll_and_open(model="lab/legacy")
    ra, ca = signed_submit(oa)
    rb, cb = signed_submit(ob)
    ok(ca == 200 and cb == 200 and not ra.get("duplicate") and not rb.get("duplicate"),
       "no-job_sig bundles: two submits -> two committed runs (old pods unchanged)")
    ok(db.get_run(oa["run_id"]) and db.get_run(ob["run_id"]), "both legacy runs stored")

# ---------- 3) completeness gate: no submit for a partial run; force overrides ----------
rid_part = uuid.uuid4().hex[:10]
db.create_run(rid_part, model="lab/partial", target_url="mock", judge_model=None,
              judge_is_self=False, suite_id="aeon-suite-v3", suite_hash="h", n_cases=5,
              params={}, env={}, job_sig="ab" * 12)
for i in range(3):                                          # 3 of 5 planned cases scored
    db.save_result(rid_part, f"case.{i}", category="math", tier=0, status="scored",
                   score=1.0, raw_output="x", evidence={}, speed={})
db.finish_run(rid_part, "interrupted")
pending.save({"job_sig": "ab" * 12, "run_id": None, "run_nonce": None, "run_token": None,
              "mothership": "http://mothership.invalid", "model": "lab/partial",
              "suite_id": "aeon-suite-v3", "board": "text", "created_at": 0.0,
              "local_rid": rid_part, "extra": {}})
calls = []
_real_finalize = pending.finalize
pending.finalize = lambda *a, **kw: (calls.append(a), (200, {"ok": True}))[1]
st, r = pending.submit_pending("ab" * 12, key_path=KEY)
ok(st == 409 and r.get("error") == "incomplete" and not calls,
   "incomplete run: submit_pending refuses, finalize (Pod.submit) never called")
st, r = pending.submit_pending("ab" * 12, key_path=KEY, force=True)
ok(st == 200 and len(calls) == 1, "force=True escape hatch submits the partial run")
pending.finalize = _real_finalize
ok(pending.load("ab" * 12) is not None, "session file survives until a confirmed commit")

# gate arithmetic itself (what the pod checks before its inline submit)
_cases = suite_mod.CASES[:4]
full = [{"case_id": c["id"]} for c in _cases]
suite_backup = suite_mod.CASES
suite_mod.CASES = _cases
ok(_missing_case_ids(full) == [], "gate: a full case set has nothing missing")
ok(len(_missing_case_ids(full[:2])) == 2, "gate: a partial case set reports the missing ids")
suite_mod.CASES = suite_backup

# ---------- 4) resume: runner skips done case ids + re-opens the row in place ----------
suite_mod.CASES = suite_mod.CASES[:4]                       # small fixed plan for the mock run
plan_ids = [c["id"] for c in suite_mod.CASES]
rid = uuid.uuid4().hex[:10]
seen = []
runner.run_benchmark(rid, "mock-good", "mock", progress_cb=lambda cid, s, st_: seen.append(cid),
                     job_sig="cd" * 12)
ok(sorted(seen) == sorted(plan_ids), "baseline mock run scores every planned case")
run = db.get_run(rid)
ok(run["status"] == "succeeded" and run["job_sig"] == "cd" * 12,
   "runner stamps job_sig on the local run row")

# forge an interruption: drop the last two results, mark the row interrupted
with db.connect() as c:
    c.execute("DELETE FROM results WHERE run_id=? AND case_id IN (?,?)",
              (rid, plan_ids[2], plan_ids[3]))
    c.execute("UPDATE runs SET status='interrupted', finished_at=NULL WHERE id=?", (rid,))
anchor = db.find_resumable_run("mock-good", suite_mod.SUITE_ID)
ok(anchor and anchor["id"] == rid and anchor["job_sig"] == "cd" * 12,
   "find_resumable_run anchors the interrupted run + its job_sig")
done_ids = db.result_case_ids(rid)
ok(done_ids == set(plan_ids[:2]), "done case ids = the surviving results")

seen2 = []
runner.run_benchmark(rid, "mock-good", "mock", progress_cb=lambda cid, s, st_: seen2.append(cid),
                     job_sig="cd" * 12, done_case_ids=done_ids, resume=True)
ok(sorted(seen2) == sorted(plan_ids[2:]), "resume runs ONLY the not-yet-scored cases")
run = db.get_run(rid)
ok(run["status"] == "succeeded" and len(run["results"]) == len(plan_ids),
   "resumed row completes IN PLACE (same rid, full result set)")
ok(db.find_resumable_run("mock-good", suite_mod.SUITE_ID) is None,
   "a completed run is no longer a resume anchor")

# plan-variant guard: an interruption whose cases fall OUTSIDE the current case plan
# (e.g. a full-suite kill while relaunching a tier-filtered bench) is never anchored
from pod.aeon_pod import _resume_anchor                    # noqa: E402
rid_v = uuid.uuid4().hex[:10]
db.create_run(rid_v, model="mock-good", target_url="mock", judge_model=None, judge_is_self=False,
              suite_id=suite_mod.SUITE_ID, suite_hash="h", n_cases=4, params={}, env={})
db.save_result(rid_v, "not.in.plan", category="x", tier=0, status="scored", score=1.0,
               raw_output="", evidence={}, speed={})
db.finish_run(rid_v, "interrupted")
old, done = _resume_anchor("mock-good")
ok(old is None and not done, "anchor guard: results outside the current plan never resume into it")

# ---- stale mid-stream ingest sweep (the PrismaAURA zombie: final commit never arrived) ------

import time as _time  # noqa: E402

def _mk_running(age_h, with_rows=True):
    rid = uuid.uuid4().hex[:10]
    db.create_run(rid, model=f"zombie/{rid}", target_url="pod-submission", judge_model=None,
                  judge_is_self=False, suite_id=suite_mod.SUITE_ID, suite_hash="h",
                  n_cases=3, params={}, env={}, trust_tier="attested")
    if with_rows:
        db.save_result(rid, "case-a", category="Math", tier=1, status="scored", score=0.9,
                       raw_output="ok", evidence={}, speed={})
    with db.connect() as c:
        c.execute("UPDATE runs SET status='running', finished_at=NULL, started_at=? WHERE id=?",
                  (_time.time() - age_h * 3600, rid))
    return rid


# A RUNNING bench is never finalized on a timer. There used to be a startup sweep that closed any
# ingest still 'running' after 48h, on the theory that it was a zombie. Elapsed time is not evidence
# of death: a large model benching a 174-case suite through three harnesses legitimately runs for
# days, and the sweep closed a live external run at 88/174 — publishing a half-finished result as
# 'succeeded'. Liveness is "did a case land recently", which only the pod can know, so the sweep is
# gone and these rows must survive untouched no matter how old they are.

stale_rows = _mk_running(72)                   # >48h + rows   — mid-run, just slow
fresh_rows = _mk_running(2)                    # <48h          — obviously live
stale_bare = _mk_running(72, with_rows=False)  # >48h, no rows — still not ours to close

if ingest is None:
    skip("mothership assertion that no age-based sweep function exists")
else:
    ok(not hasattr(ingest, "sweep_stale_running"),
       "no age-based sweep exists — bringing one back would kill live benches again")
# The rows themselves are pure aeon/db.py and are checked either way: nothing on the POD side may
# finalize a run just because it is old.
for _rid, _label in ((stale_rows, ">48h WITH rows"), (fresh_rows, "<48h"),
                     (stale_bare, ">48h rowless")):
    _r = db.get_run(_rid)
    ok(_r["status"] == "running" and not _r["finished_at"],
       f"a {_label} running ingest stays running — age alone never finalizes it")

# ---- artifacts checkpoint with rows (content dedup — the PrismaAURA artifact loss) ---------

_ART = {"ok": True, "kind": "game", "prompt_id": "game.snake",
        "html": "<!DOCTYPE html><html><body>sentinel-artifact</body></html>", "gen_ms": 1200}
_POD = {"run_id": uuid.uuid4().hex[:12], "model": "lab/ckpt-artifact-model",
        "suite_id": suite_mod.SUITE_ID, "board": "text"}
_BUNDLE = {"results": [{"case_id": "case-1", "category": "Math", "tier": 1,
                        "status": "scored", "score": 1.0, "raw_output": "ok"}],
           "artifacts": [_ART], "suite_hash": "h"}

if ingest is None:
    skip("mothership artifact checkpoint dedup (_save_artifacts / non-final _commit)")
else:
    n1 = ingest._save_artifacts(_POD, _BUNDLE)
    n2 = ingest._save_artifacts(_POD, _BUNDLE)   # checkpoint resend — content-identical
    ok(n1 == 1 and n2 == 0, "artifact saves once, checkpoint resend dedups by content")
    ok(len([a for a in db.list_artifacts(kind="game")
            if a["model"] == "lab/ckpt-artifact-model"]) == 1,
       "exactly one stored artifact after two checkpoint commits")
    ingest._commit(_POD, _BUNDLE, final=False)
    run_mid = db.get_run(_POD["run_id"])
    ok(run_mid and run_mid["status"] == "running"
       and len([a for a in db.list_artifacts(kind="game")
                if a["model"] == "lab/ckpt-artifact-model"]) == 1,
       "a NON-final commit already persists artifacts (no more final-commit-only loss)")

print(f"\nOK  resume + deferred idempotent submission: {PASS} checks passed")
if SKIPPED:
    print(f"    {len(SKIPPED)} mothership-only section(s) skipped (aeon/ingest.py is not in this "
          f"repo): " + "; ".join(SKIPPED))
