"""Self-test: no-answer fairness — retry passes + status='no_answer' (score NULL).

THE RULE (pinned contract with mothership scoring): a benchmark case that yields NO
ANSWER (transport/HTTP/timeout failure, or an empty/whitespace-only completion) is a
technical glitch, not a wrong answer. The runner re-runs such cases in a second, then
third pass; only after all RETRY_PASSES attempts fail does the case persist as
status='no_answer' with score NULL — distinct from 'error'. An ANSWERED case scores
normally (a wrong answer stays 0 at full weight). Covered here, fully offline:

  * classification unit: no_answer_reason (exception -> 'transport: ...',
    200-but-blank/whitespace -> 'empty_completion', genuine text -> None);
  * (a) a target that fails case X on pass 1+2 and answers on pass 3 -> final row
    SCORED, exactly 3 attempts on X and 1 on every other case, retry history in
    evidence, '[pod] retry pass N: M unanswered cases' markers on stdout;
  * (b) an always-failing case -> status='no_answer', score NULL, exactly 3 attempts,
    the run still completes AND passes the completeness gate (a no_answer row IS a row);
  * (c) empty/whitespace-only completions classify unanswered (reason 'empty_completion');
  * (d) resume + retry interplay: done_case_ids are NEVER re-attempted; a case that was
    mid-retry at the kill is rowless, so the resume re-attempts it with a FRESH pass budget;
  * (e) progress_cb totals stay sane (exactly one event per planned case, retries never
    inflate them) — sequential and concurrency>1;
  * dead-endpoint guard: pass 1 answers nothing + every failure transport-class ->
    TargetError abort (run 'failed', zero rows, resumable) instead of an all-no_answer run;
  * ingest passthrough: a signed bundle carrying a no_answer result commits with the
    status + NULL score intact on the mothership row;
  * the shared driver on a multimodal board (audio): same retry + no_answer treatment.

Runs fully offline. From the mvp dir:  python test_retry_no_answer.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import uuid
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Throwaway SQLite BEFORE importing aeon (db.py reads the env at import time).
_TMP = tempfile.mkdtemp(prefix="aeon-noanswer-selftest-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("AEON_DB_URL", None)
os.environ.pop("AEON_ATTESTED_ONLY", None)

from aeon import db, ingest, runner                        # noqa: E402
from aeon import suite as suite_mod                        # noqa: E402
from aeon import targets as targets_mod                    # noqa: E402
from aeon.targets import MockTarget, TargetError, no_answer_reason  # noqa: E402
from pod import pending                                    # noqa: E402
from pod.aeon_pod import _missing_case_ids                 # noqa: E402
from pod.aeon_submit import Pod, _canon                    # noqa: E402

db.init_db()
pending.DIR = os.path.join(_TMP, "pending_submits")        # never touch the real ~/.aeon
KEY = os.path.join(_TMP, "device_key.pem")

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("PASS:", label)


class FlakyTarget(MockTarget):
    """mock-good, but `fail_case` yields NO ANSWER for its first `fail_times` attempts:
    mode='raise' -> TargetError (transport class), mode='empty' -> whitespace-only text."""

    def __init__(self, persona="mock-good", fail_case=None, fail_times=2, mode="raise"):
        super().__init__(persona)
        self.fail_case, self.fail_times, self.mode = fail_case, fail_times, mode
        self.attempts = Counter()

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        cid = messages[0].get("_case_id") if messages else None
        self.attempts[cid] += 1
        if cid == self.fail_case and self.attempts[cid] <= self.fail_times:
            if self.mode == "raise":
                raise TargetError(f"HTTP 502 from http://flaky.invalid: simulated "
                                  f"(attempt {self.attempts[cid]})")
            return {**super().chat(messages, temperature=temperature, max_tokens=max_tokens),
                    "text": "  \n\t "}
        return super().chat(messages, temperature=temperature, max_tokens=max_tokens)


class DeadTarget(MockTarget):
    """Every request fails in transport — a down/misconfigured endpoint."""

    def __init__(self):
        super().__init__("mock-good")
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        raise TargetError("HTTP 503 from http://dead.invalid: simulated outage")


def run_with(target, rid, **kw):
    """run_benchmark against an injected in-process target (build_target monkeypatch)."""
    orig = runner.build_target
    runner.build_target = lambda model, url, api_key=None: target
    try:
        return runner.run_benchmark(rid, target.model, "mock", **kw)
    finally:
        runner.build_target = orig


# ---------- 0) classification unit: no_answer_reason ----------
ok(no_answer_reason(exc=TargetError("HTTP 500 from x")).startswith("transport:"),
   "an exception (TargetError/HTTP) classifies 'transport: ...'")
ok(no_answer_reason(exc=TimeoutError("timed out")).startswith("transport:"),
   "a timeout exception classifies 'transport: ...'")
ok(no_answer_reason(text="") == "empty_completion", "empty text classifies 'empty_completion'")
ok(no_answer_reason(text="  \n\t ") == "empty_completion",
   "whitespace-only text classifies 'empty_completion'")
ok(no_answer_reason(text=None) == "empty_completion", "None text classifies 'empty_completion'")
ok(no_answer_reason(text="42") is None, "genuine text is an ANSWER (None)")
ok(no_answer_reason(text="wrong answer") is None,
   "a WRONG answer is still an answer — never retried by this rule")

suite_mod.CASES = suite_mod.CASES[:4]                       # small fixed plan for the mock runs
PLAN = [c["id"] for c in suite_mod.CASES]

# ---------- (a) fails pass 1+2, answers on pass 3 -> scored; exact attempt counts ----------
t = FlakyTarget(fail_case=PLAN[1], fail_times=2, mode="raise")
rid_a = uuid.uuid4().hex[:10]
events = []
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    run_with(t, rid_a, progress_cb=lambda c, s, st: events.append((c, s, st)))
out = buf.getvalue()
run = db.get_run(rid_a)
rows = {r["case_id"]: r for r in run["results"]}
ok(run["status"] == "succeeded", "(a) run completes")
ok(rows[PLAN[1]]["status"] == "scored" and isinstance(rows[PLAN[1]]["score"], float),
   "(a) the pass-3 answer is SCORED normally (no no_answer)")
ok(t.attempts[PLAN[1]] == 3, "(a) exactly 3 attempts on the flaky case")
ok(all(t.attempts[c] == 1 for c in PLAN if c != PLAN[1]),
   "(a) exactly 1 attempt on every other case")
ev = rows[PLAN[1]]["evidence"]
ok(ev.get("answered_on_pass") == 3 and ev.get("no_answer_retries") == 2
   and len(ev.get("no_answer_reasons") or []) == 2
   and all(r.startswith("transport:") for r in ev["no_answer_reasons"]),
   "(a) retry history rides the evidence (answered_on_pass/no_answer_retries/reasons)")
ok("[pod] retry pass 2: 1 unanswered cases" in out
   and "[pod] retry pass 3: 1 unanswered cases" in out,
   "(a) '[pod] retry pass N: M unanswered cases' markers logged")
ok(len(events) == len(PLAN) and {c for c, _, _ in events} == set(PLAN),
   "(e) progress_cb fired EXACTLY once per planned case (retries never inflate totals)")

# ---------- (b) always-fails -> no_answer, score NULL, run completes ----------
t = FlakyTarget(fail_case=PLAN[2], fail_times=99, mode="raise")
rid_b = uuid.uuid4().hex[:10]
events = []
with contextlib.redirect_stdout(io.StringIO()):
    run_with(t, rid_b, progress_cb=lambda c, s, st: events.append((c, s, st)))
run = db.get_run(rid_b)
rows = {r["case_id"]: r for r in run["results"]}
ok(run["status"] == "succeeded", "(b) an unanswerable case never blocks run completion")
ok(rows[PLAN[2]]["status"] == "no_answer" and rows[PLAN[2]]["score"] is None,
   "(b) still unanswered after pass 3 -> status='no_answer', score NULL")
ok(t.attempts[PLAN[2]] == 3, "(b) exactly 3 attempts, then classified")
ev = rows[PLAN[2]]["evidence"]
ok(ev.get("no_answer") is True and ev.get("attempts") == 3
   and len(ev.get("reasons") or []) == 3,
   "(b) no_answer evidence records all 3 per-pass reasons")
ok(all(rows[c]["status"] == "scored" for c in PLAN if c != PLAN[2]),
   "(b) every answered case scored normally")
ok(len(events) == len(PLAN), "(e) progress totals sane with a no_answer classification")
# completeness gate: a no_answer row IS a row — the run counts as complete
ok(_missing_case_ids(pending.collect_results(rid_b)) == [],
   "(b) completeness gate: no_answer rows leave nothing missing")
snap = {r["case_id"]: r for r in pending.collect_results(rid_b)}
ok(snap[PLAN[2]]["status"] == "no_answer" and snap[PLAN[2]]["score"] is None,
   "(b) the submit bundle snapshot carries status='no_answer' + NULL score")

# ---------- (c) empty/whitespace completions classify unanswered ----------
t = FlakyTarget(fail_case=PLAN[0], fail_times=99, mode="empty")
rid_c = uuid.uuid4().hex[:10]
with contextlib.redirect_stdout(io.StringIO()):
    run_with(t, rid_c)
rows = {r["case_id"]: r for r in db.get_run(rid_c)["results"]}
ok(rows[PLAN[0]]["status"] == "no_answer" and rows[PLAN[0]]["score"] is None,
   "(c) 200-but-whitespace completions end as no_answer")
ok(rows[PLAN[0]]["evidence"]["reasons"] == ["empty_completion"] * 3,
   "(c) all 3 reasons are 'empty_completion'")
ok(t.attempts[PLAN[0]] == 3, "(c) empty completions get the full 3-pass budget")

# ---------- (c2) empty-but-answered-later: empties on pass 1+2, answers pass 3 ----------
t = FlakyTarget(fail_case=PLAN[3], fail_times=2, mode="empty")
rid_c2 = uuid.uuid4().hex[:10]
with contextlib.redirect_stdout(io.StringIO()):
    run_with(t, rid_c2)
rows = {r["case_id"]: r for r in db.get_run(rid_c2)["results"]}
ok(rows[PLAN[3]]["status"] == "scored"
   and rows[PLAN[3]]["evidence"].get("no_answer_reasons") == ["empty_completion"] * 2,
   "(c2) empty pass 1+2 then a real pass-3 answer -> scored, history kept")

# ---------- (d) resume + retry interplay: done cases never retried, fresh budget ----------
rid_d = uuid.uuid4().hex[:10]
with contextlib.redirect_stdout(io.StringIO()):
    run_with(MockTarget("mock-good"), rid_d, job_sig="ee" * 12)
with db.connect() as c:                                    # forge a mid-run kill: 2 cases rowless
    c.execute("DELETE FROM results WHERE run_id=? AND case_id IN (?,?)",
              (rid_d, PLAN[2], PLAN[3]))
    c.execute("UPDATE runs SET status='interrupted', finished_at=NULL WHERE id=?", (rid_d,))
done_ids = db.result_case_ids(rid_d)
ok(done_ids == {PLAN[0], PLAN[1]}, "(d) done_case_ids = the surviving (finally-resolved) rows")
# resume with a target that would fail a DONE case forever, and a REMAINING case twice:
# the done case must never be touched; the rowless case gets a fresh 3-pass budget.
t = FlakyTarget(fail_case=PLAN[2], fail_times=2, mode="raise")
t_done_guard = t.attempts[PLAN[0]]
with contextlib.redirect_stdout(io.StringIO()):
    run_with(t, rid_d, job_sig="ee" * 12, done_case_ids=done_ids, resume=True)
run = db.get_run(rid_d)
rows = {r["case_id"]: r for r in run["results"]}
ok(t.attempts[PLAN[0]] == 0 and t.attempts[PLAN[1]] == 0,
   "(d) done cases are NEVER re-attempted on resume")
ok(t.attempts[PLAN[2]] == 3 and rows[PLAN[2]]["status"] == "scored",
   "(d) a case rowless at the kill resumes with a FRESH pass budget (3 attempts, scored)")
ok(t.attempts[PLAN[3]] == 1, "(d) the other rowless case takes a single attempt")
ok(run["status"] == "succeeded" and len(run["results"]) == len(PLAN),
   "(d) the resumed row completes in place with the full result set")

# ---------- (e) concurrency>1: same guarantees through the thread pool ----------
t = FlakyTarget(fail_case=PLAN[1], fail_times=2, mode="raise")
rid_e = uuid.uuid4().hex[:10]
events = []
with contextlib.redirect_stdout(io.StringIO()):
    run_with(t, rid_e, params={"temperature": 0.0, "max_tokens": 512, "concurrency": 3},
             progress_cb=lambda c, s, st: events.append(c))
rows = {r["case_id"]: r for r in db.get_run(rid_e)["results"]}
ok(len(events) == len(PLAN) and sorted(events) == sorted(PLAN),
   "(e) concurrency=3: one progress event per case, no duplicates")
ok(t.attempts[PLAN[1]] == 3 and rows[PLAN[1]]["status"] == "scored",
   "(e) concurrency=3: retry passes still resolve the flaky case")

# ---------- dead-endpoint guard: nothing answered + all transport -> abort ----------
t = DeadTarget()
rid_f = uuid.uuid4().hex[:10]
raised = False
try:
    with contextlib.redirect_stdout(io.StringIO()):
        run_with(t, rid_f)
except TargetError:
    raised = True
run = db.get_run(rid_f)
ok(raised, "guard: an endpoint that answers NOTHING in pass 1 aborts with TargetError")
ok(run["status"] == "failed", "guard: the run is marked failed (resumable), not all-no_answer")
ok(t.calls == len(PLAN), "guard: aborts after ONE pass — no pass-2/3 timeout burn")
ok(len(run["results"]) == 0, "guard: no interim rows were persisted")

# ---------- ingest passthrough: status='no_answer' + NULL score commit intact ----------
pod_client = Pod("http://mothership.invalid", KEY)
ch = ingest.issue_challenge()
r, code = ingest.enroll(pod_client.pub, ch, pod_client._sign(ch.encode()))
assert code == 200, r
body = {"action": "open_run", "public_key": pod_client.pub, "model": "lab/na-model",
        "suite_id": "aeon-suite-v3", "board": "text"}
opened, code = ingest.open_run(pod_client.pub, pod_client._sign(_canon(body)),
                               model="lab/na-model", suite_id="aeon-suite-v3", board="text")
assert code == 200, opened
bundle = {"run_id": opened["run_id"], "run_nonce": opened["run_nonce"], "final": True,
          "results": [
              {"case_id": "c1", "category": "math", "tier": 0, "status": "scored",
               "score": 1.0, "raw_output": "ok"},
              {"case_id": "c2", "category": "math", "tier": 0, "status": "no_answer",
               "score": None, "raw_output": "",
               "evidence": {"no_answer": True, "attempts": 3,
                            "reasons": ["transport: simulated"] * 3}},
          ]}
ok(ingest._validate_bundle(bundle), "ingest schema accepts status='no_answer' + NULL score")
raw = json.dumps({"bundle": bundle, "signature": pod_client._sign(_canon(bundle))}).encode()
resp, code = ingest.submit_results(opened["run_id"], opened["run_token"], raw)
ok(code == 200 and resp.get("ok"), "signed no_answer bundle commits")
mrows = {r["case_id"]: r for r in db.get_run(opened["run_id"])["results"]}
ok(mrows["c2"]["status"] == "no_answer" and mrows["c2"]["score"] is None,
   "mothership row keeps status='no_answer' + NULL score (ingest passthrough)")
ok(mrows["c1"]["status"] == "scored" and mrows["c1"]["score"] == 1.0,
   "answered cases in the same bundle commit unchanged")

# ---------- multimodal (audio board): same driver, same treatment ----------
from aeon import audio_suite as aus                        # noqa: E402

aus.CASES = aus.CASES[:4]
APLAN = [c["id"] for c in aus.CASES]
AUDIO_INSTANCES = []


class FlakyAudio(targets_mod.MockAudioTarget):
    def __init__(self, persona="mock-audio"):
        super().__init__(persona)
        self.attempts = Counter()
        AUDIO_INSTANCES.append(self)

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        cid = messages[0].get("_case_id") if messages else None
        if cid != "_probe":
            self.attempts[cid] += 1
        if cid == APLAN[1] and self.attempts[cid] <= 99:
            raise TargetError("HTTP 500 from http://flaky.invalid: simulated (audio)")
        return super().chat(messages, temperature=temperature, max_tokens=max_tokens)


_orig_audio = targets_mod.MockAudioTarget
targets_mod.MockAudioTarget = FlakyAudio                   # run_audio_benchmark binds at call time
try:
    rid_m = uuid.uuid4().hex[:10]
    aevents = []
    with contextlib.redirect_stdout(io.StringIO()):
        runner.run_audio_benchmark(rid_m, "mock-audio", "mock",
                                   progress_cb=lambda c, s, st: aevents.append(c))
finally:
    targets_mod.MockAudioTarget = _orig_audio
t = AUDIO_INSTANCES[0]
run = db.get_run(rid_m)
rows = {r["case_id"]: r for r in run["results"]}
ok(run["status"] == "succeeded" and len(run["results"]) == len(APLAN),
   "audio board: run completes with a row per case")
ok(rows[APLAN[1]]["status"] == "no_answer" and rows[APLAN[1]]["score"] is None
   and t.attempts[APLAN[1]] == 3,
   "audio board: unanswerable case -> no_answer after exactly 3 attempts")
ok(all(rows[c]["status"] == "scored" and rows[c]["score"] == 1.0 for c in APLAN if c != APLAN[1]),
   "audio board: answered cases score normally")
ok(len(aevents) == len(APLAN), "audio board: progress fired once per case")

print(f"\nOK  no-answer fairness (retry passes + status='no_answer'): {PASS} checks passed")
