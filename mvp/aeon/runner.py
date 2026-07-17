"""The benchmark run loop (DESIGN §5.3 probe runner, collapsed in-process).

For each case: render prompt → call target (capture speed) → run the case's
evaluator (Tier-0 program, or Tier-1 binary-rubric judge = the launching/target
model by default) → persist. Speed + deterministic scores always record.

NO-ANSWER FAIRNESS (pinned contract with mothership scoring): a case whose attempt
produced NO ANSWER — a transport/HTTP/timeout failure or an empty/whitespace-only
completion — is a technical glitch, not a wrong answer. Such cases are re-run in up
to two retry passes (_run_retry_passes); only after all RETRY_PASSES attempts fail
does the case persist as status='no_answer' with score NULL (the mothership weights
it at ¼ of a case — distinct from 'error'-class statuses). An answered case scores
normally: a wrong answer stays 0 at full weight.
"""
from __future__ import annotations

import json
import platform
import sys

from . import db
from . import suite as suite_mod
from .evaluators import evaluate
from .targets import MockTarget, OpenAITarget, TargetError, no_answer_reason


def build_target(model, target_url, api_key=None):
    if target_url == "mock":
        return MockTarget(model)
    if str(target_url or "").startswith("frontier://"):
        from . import frontier
        return frontier.build_target(str(target_url).split("://", 1)[1], api_key=api_key)
    return OpenAITarget(target_url, model, api_key=api_key)


# Total attempts an unanswered case gets before it is CLASSIFIED (first pass + two retry
# passes). THE RULE (pinned contract with mothership scoring): a case that yields NO ANSWER
# (transport/HTTP/timeout failure, or an empty/whitespace-only completion) is a technical
# glitch, not a wrong answer — retry it; only after every pass fails does it get
# status='no_answer' with score NULL (weighted ¼ of a case by the mothership, distinct from
# 'error'). An ANSWERED case scores normally: a wrong answer stays 0 at full weight.
RETRY_PASSES = 3


def _run_retry_passes(pending, attempt, persist, *, concurrency=1):
    """The no-answer retry driver shared by every board's run loop.

    `attempt(case)` performs ONE generation(+evaluation) attempt and returns a result dict
    whose "unanswered" key is None for a genuine answer or a reason string (see
    targets.no_answer_reason). `persist(res)` runs on the MAIN thread, EXACTLY ONCE per
    case, on its FINAL resolution only — an unanswered attempt writes NOTHING, so:
      * checkpoint streaming never uploads an interim row (save_result's INSERT OR REPLACE
        would fix the local row, but ingest._commit dedups by case_id and would keep the
        stale copy forever);
      * progress_cb (inside persist) fires once per case — retries never inflate totals;
      * a mid-retry kill leaves the case ROWLESS: _missing_case_ids counts it as missing
        (completeness gate holds) and a resume re-attempts it with a fresh pass budget.

    Pass structure: pass 1 covers every pending case; unanswered cases are re-run in pass 2,
    then pass 3 ('[pod] retry pass N: M unanswered cases'). Still unanswered after pass
    RETRY_PASSES -> ONE row with status='no_answer', score NULL, evidence carrying the
    per-pass reasons. A case answered on a retry pass persists normally with the retry
    history merged into its evidence.

    DEAD-ENDPOINT GUARD: if pass 1 answers NOTHING and every attempt failed in transport
    (never a mere empty completion), the endpoint is down/misconfigured — raise TargetError
    so the run aborts (failed -> resumable) instead of burning two more passes of timeouts
    into an all-no_answer submission. This replaces the old first-TargetError abort: one
    transient HTTP failure no longer kills a run whose endpoint is otherwise answering.
    """
    reasons = {}                                    # case_id -> [reason per unanswered pass]

    def _resolved(res, pass_no):
        prior = reasons.get(res["cid"])
        if prior and res.get("unanswered") is None:  # answered on a retry pass — keep the history
            ev = res["evidence"] if isinstance(res["evidence"], dict) else {"evidence": res["evidence"]}
            res = {**res, "evidence": {**ev, "answered_on_pass": pass_no,
                                       "no_answer_retries": len(prior),
                                       "no_answer_reasons": prior}}
        res.pop("unanswered", None)
        persist(res)

    for pass_no in range(1, RETRY_PASSES + 1):
        if not pending:
            break
        if pass_no > 1:
            print(f"[pod] retry pass {pass_no}: {len(pending)} unanswered cases", flush=True)
        retry, answered = [], 0

        def _handle(case, res):
            nonlocal answered
            if res.get("unanswered"):
                reasons.setdefault(case["id"], []).append(res["unanswered"])
                if pass_no < RETRY_PASSES:
                    retry.append(case)              # not persisted — the next pass owns it
                else:                               # out of passes: classify, score NULL
                    _resolved({**res, "status": "no_answer", "score": None,
                               "evidence": {"no_answer": True, "attempts": RETRY_PASSES,
                                            "reasons": reasons[case["id"]]}}, pass_no)
            else:
                answered += 1
                _resolved(res, pass_no)

        if concurrency <= 1:
            for case in pending:
                _handle(case, attempt(case))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futs = {ex.submit(attempt, c): c for c in pending}
                try:
                    for fut in as_completed(futs):
                        _handle(futs[fut], fut.result())   # attempt never raises by design;
                except BaseException:                      # a bug/interrupt still cancels cleanly
                    for f in futs:
                        f.cancel()
                    raise
        if (pass_no == 1 and pending and not answered and retry
                and all(r.startswith("transport:")
                        for rs in reasons.values() for r in rs)):
            raise TargetError(
                f"endpoint answered none of the {len(pending)} attempted cases in pass 1 "
                f"(every attempt failed in transport) — aborting as down/misconfigured; "
                f"the run stays resumable")
        pending = retry


def _media_attempt(target, judge, params, *, build_blocks, speed_keys):
    """attempt(case) for the multimodal boards (vision/video/audio): one chat + evaluate
    attempt with the shared no-answer classification — _run_retry_passes drives the retry
    passes exactly as on the text board. Media blocks are rebuilt per attempt (the
    generators are deterministic + cheap synthetic assets)."""
    def attempt(case):
        cid = case["id"]
        user = {"role": "user", "content": build_blocks(case), "_case_id": cid}
        try:
            resp = target.chat([user], temperature=params["temperature"],
                               max_tokens=params["max_tokens"])
        except Exception as e:                     # transport/HTTP/timeout -> unanswered, not wrong
            return {"cid": cid, "category": case["category"], "tier": case["tier"],
                    "status": "no_answer", "score": None, "text": "", "evidence": {},
                    "speed": {}, "unanswered": no_answer_reason(exc=e)}
        text = resp["text"]
        speed = {k: resp.get(k) for k in speed_keys}
        unanswered = no_answer_reason(text=text)
        if unanswered:                             # 200 but empty/whitespace -> unanswered too
            return {"cid": cid, "category": case["category"], "tier": case["tier"],
                    "status": "no_answer", "score": None, "text": text, "evidence": {},
                    "speed": speed, "unanswered": unanswered}
        try:
            score, evidence = evaluate(case, text, judge)
        except Exception as e:
            score, evidence = 0.0, {"error": f"eval error: {e!r}"}
        return {"cid": cid, "category": case["category"], "tier": case["tier"],
                "status": "scored", "score": score, "text": text, "evidence": evidence,
                "speed": speed, "unanswered": None}
    return attempt


def _board_persist(run_id, board, progress_cb):
    """persist(res) for the multimodal boards — DB write + progress, main thread only."""
    def persist(res):
        db.save_result(run_id, res["cid"], category=res["category"], tier=res["tier"], board=board,
                       status=res["status"], score=res["score"], raw_output=res["text"],
                       evidence=res["evidence"], speed=res["speed"])
        if progress_cb:
            progress_cb(res["cid"], res["score"], res["status"])
    return persist


def run_benchmark(run_id, model, target_url, judge_model=None, params=None,
                  progress_cb=None, api_key=None, judge_url=None, judge_key=None,
                  hf_repo=None, trust_tier="self_reported", model_verified=None,
                  env_extra=None, canonical_id=None, job_sig=None,
                  done_case_ids=None, resume=False):
    # `model` is the served alias used for the actual /v1 requests (e.g. "model-under-test").
    # `hf_repo`/`trust_tier` carry the REAL verified identity so the pod-local run — and thus the
    # live view — shows the true model + badge while inference still targets the served alias.
    # RESUME: `resume=True` + the interrupted run's id re-opens that row in place, and
    # `done_case_ids` (its already-scored cases) are skipped — the pod's resume path.
    from . import judge_policy
    params = params or {"temperature": 0.0, "max_tokens": 512}
    target = build_target(model, target_url, api_key)
    # Judge policy: a FRONTIER model OR deterministic-only — NEVER the model under test
    # (self-judge) and never a weak/arbitrary judge (aeon.judge_policy). The frontier judge
    # runs on its own endpoint/key; with no valid judge, subjective cases are left unscored.
    mode = judge_policy.judge_mode(judge_model, model)
    if mode == "frontier":
        judge, eff_judge = build_target(judge_model, judge_url or target_url, judge_key or api_key), judge_model
    else:
        judge, eff_judge = None, None

    env = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "runner": "aeon-mvp", "judge_mode": mode,
    }
    if isinstance(env_extra, dict):
        env.update(env_extra)
    # RESUME re-opens the interrupted row (results append in place; n_cases stands) —
    # otherwise a fresh run row is minted as before.
    if not (resume and db.resume_run_row(run_id)):
        db.create_run(
            run_id, model=model, target_url=target_url,
            judge_model=eff_judge, judge_is_self=False,
            suite_id=suite_mod.SUITE_ID, suite_hash=suite_mod.suite_hash(),
            n_cases=len(suite_mod.CASES), params=params, env=env,
            hf_repo=hf_repo, trust_tier=trust_tier, model_verified=model_verified,
            canonical_id=canonical_id, job_sig=job_sig,
        )
    done_ids = set(done_case_ids or ())          # already-scored cases a resume skips
    cases = [c for c in suite_mod.CASES if c["id"] not in done_ids]
    base_tok = params.get("max_tokens", 512)
    retry_tok = params.get("retry_max_tokens")                # higher ceiling for a cut-off re-run
    max_retries = int(params.get("retries", 1)) if retry_tok else 0
    concurrency = max(1, int(params.get("concurrency", 1)))   # cases in flight at once (vLLM batches them)

    def _attempt_case(case):
        """ONE generation (with the truncation re-run) + evaluation attempt for ONE case ->
        a result dict; res["unanswered"] carries the no-answer reason (transport failure or
        empty completion) that routes it into the retry passes — such attempts persist
        nothing and are never scored. Called from worker threads; touches no shared state."""
        cid = case["id"]
        user = {"role": "user", "content": case["prompt"], "_case_id": cid}
        tok, trunc_runs = base_tok, 0
        while True:
            try:
                resp = target.chat([user], temperature=params["temperature"], max_tokens=tok)
            except Exception as e:
                # Transport/HTTP/timeout (TargetError included): NO ANSWER was produced — a
                # technical glitch, not a wrong answer. The retry passes own this case now;
                # a dead endpoint aborts via the pass-1 guard, not the old first-error raise.
                return {"cid": cid, "category": case["category"], "tier": case["tier"],
                        "status": "no_answer", "score": None, "text": "", "evidence": {},
                        "speed": {}, "unanswered": no_answer_reason(exc=e)}
            text = resp["text"]
            speed = {k: resp.get(k) for k in ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens", "streamed")}
            truncated = bool(resp.get("truncated"))
            unanswered = no_answer_reason(text=text)
            if unanswered:
                score, evidence = None, {}          # nothing to evaluate
            else:
                try:
                    score, evidence = evaluate(case, text, judge)
                except Exception as e:
                    score, evidence = 0.0, {"error": f"eval error: {e!r}"}
            # A cut-off <think> yields an empty / non-answer that is NOT a real miss — the model ran
            # out of tokens mid-reasoning. If truncated and not already correct, RE-RUN once at the
            # higher ceiling so it can finish. (temperature stays 0; only the budget grows.) This
            # runs BEFORE the no-answer classification settles: token starvation is fixed by budget,
            # not by the identical-budget retry passes.
            if truncated and trunc_runs < max_retries and retry_tok > tok and (score is None or score < 1.0):
                trunc_runs += 1
                tok = retry_tok
                continue
            break
        if unanswered:
            return {"cid": cid, "category": case["category"], "tier": case["tier"],
                    "status": "no_answer", "score": None, "text": text, "evidence": {},
                    "speed": speed, "unanswered": unanswered}
        if trunc_runs:
            if not isinstance(evidence, dict):
                evidence = {"evidence": evidence}
            evidence = {**evidence, "retried": trunc_runs, "retry_max_tokens": retry_tok, "was_truncated": True}
        if score is None:
            status = ("needs_frontier_judge"
                      if isinstance(evidence, dict) and evidence.get("needs_frontier_judge") else "tier2_arena")
        else:
            status = "scored"
        return {"cid": cid, "category": case["category"], "tier": case["tier"], "status": status,
                "score": score, "text": text, "evidence": evidence, "speed": speed, "unanswered": None}

    def _persist(res):                                        # main thread only: DB write + progress + checkpoint
        db.save_result(run_id, res["cid"], category=res["category"], tier=res["tier"],
                       status=res["status"], score=res["score"], raw_output=res["text"],
                       evidence=res["evidence"], speed=res["speed"])
        if progress_cb:
            progress_cb(res["cid"], res["score"], res["status"])

    try:
        _run_retry_passes(cases, _attempt_case, _persist, concurrency=concurrency)
        db.finish_run(run_id, "succeeded")
    except Exception as e:
        db.finish_run(run_id, "failed", error=str(e))
        raise


def run_vision_benchmark(run_id, model, target_url, judge_model=None, params=None,
                         progress_cb=None, api_key=None):
    """The VISION board run loop (DESIGN §6c). Capability-probe first; on failure
    the model is recorded `capability_absent` and never appears on the vision
    board (and is untouched on the text board)."""
    from . import imagegen, probe
    from . import vision_suite as vs
    from .targets import MockVisionTarget, image_block, text_block

    params = params or {"temperature": 0.0, "max_tokens": 2048}   # reasoning-model headroom (see probe._ask)
    target = MockVisionTarget(model) if target_url == "mock" else OpenAITarget(target_url, model, api_key=api_key)
    judge = target  # only Tier-0-shadowed Tier-1 here, so the judge is never actually invoked
    env = {"platform": platform.platform(), "python": platform.python_version(), "runner": "aeon-mvp-vision"}

    pr = probe.probe_vision(target)
    db.create_run(run_id, model=model, target_url=target_url, judge_model=model, judge_is_self=True,
                  suite_id=vs.SUITE_ID, suite_hash=vs.suite_hash(), n_cases=len(vs.CASES),
                  params=params, env=env, board="vision", vision_probe_json=json.dumps(pr))
    if not pr.get("vision_ok"):
        db.finish_run(run_id, "capability_absent", error=pr.get("error"))
        if progress_cb:
            progress_cb("_probe", None, "capability_absent")
        return pr
    def _blocks(case):
        blocks = [text_block(case["prompt"])]
        for spec in case["images"]:
            _, png, _ = imagegen.generate(spec)
            blocks.append(image_block(png))
        return blocks

    attempt = _media_attempt(target, judge, params, build_blocks=_blocks,
                             speed_keys=("ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
                                         "ttft_after_image_ms", "n_images", "image_bytes"))
    try:
        pending = []
        for case in vs.CASES:
            if not pr.get(case["requires"], False):
                db.save_result(run_id, case["id"], category=case["category"], tier=case["tier"],
                               board="vision", status="na_capability", score=None, raw_output="",
                               evidence={"skipped": case["requires"]}, speed={})
                if progress_cb:
                    progress_cb(case["id"], None, "na_capability")
                continue
            pending.append(case)
        _run_retry_passes(pending, attempt, _board_persist(run_id, "vision", progress_cb))
        db.finish_run(run_id, "succeeded")
    except Exception as e:
        db.finish_run(run_id, "failed", error=str(e))
        raise
    return pr


def run_video_benchmark(run_id, model, target_url, params=None,
                        progress_cb=None, api_key=None):
    """The VIDEO board run loop — mirrors run_vision_benchmark/run_audio_benchmark.
    probe_video gates first; on failure the model is recorded `capability_absent` and
    never appears on the video board (and is untouched on other boards). Every case is
    Tier-0 deterministic (count_slot/closed_set/ordered_keywords on synthetic clips with
    machine-known ground truth), so a judge is never invoked. Raises RuntimeError with an
    install hint when the encoder stack (imageio[ffmpeg]) is missing — callers that want
    a soft skip should check videogen.available() first (see aeon_pod._video_and_submit)."""
    from . import probe, videogen
    from . import video_suite as vids
    from .targets import MockVideoTarget, text_block, video_block

    params = params or {"temperature": 0.0, "max_tokens": 2048}   # reasoning-model headroom (see probe._ask)
    target = MockVideoTarget(model) if target_url == "mock" else OpenAITarget(target_url, model, api_key=api_key)
    judge = None  # all Tier-0 here — deterministic evaluate; a judge is never invoked
    env = {"platform": platform.platform(), "python": platform.python_version(), "runner": "aeon-mvp-video"}

    pr = probe.probe_video(target)
    db.create_run(run_id, model=model, target_url=target_url, judge_model=None, judge_is_self=False,
                  suite_id=vids.SUITE_ID, suite_hash=vids.suite_hash(), n_cases=len(vids.CASES),
                  params=params, env=env, board="video", vision_probe_json=json.dumps(pr))
    if not pr.get("video_ok"):
        db.finish_run(run_id, "capability_absent", error=pr.get("error"))
        if progress_cb:
            progress_cb("_probe", None, "capability_absent")
        return pr
    def _blocks(case):
        blocks = [text_block(case["prompt"])]
        for spec in case["video"]:
            _, mp4, _ = videogen.generate(spec)
            blocks.append(video_block(mp4))
        return blocks

    attempt = _media_attempt(target, judge, params, build_blocks=_blocks,
                             speed_keys=("ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
                                         "ttft_after_video_ms", "n_video", "video_bytes"))
    try:
        pending = []
        for case in vids.CASES:
            if not pr.get(case["requires"], False):
                db.save_result(run_id, case["id"], category=case["category"], tier=case["tier"],
                               board="video", status="na_capability", score=None, raw_output="",
                               evidence={"skipped": case["requires"]}, speed={})
                if progress_cb:
                    progress_cb(case["id"], None, "na_capability")
                continue
            pending.append(case)
        _run_retry_passes(pending, attempt, _board_persist(run_id, "video", progress_cb))
        db.finish_run(run_id, "succeeded")
    except Exception as e:
        db.finish_run(run_id, "failed", error=str(e))
        raise
    return pr


def run_audio_benchmark(run_id, model, target_url, params=None,
                        progress_cb=None, api_key=None):
    """The AUDIO board run loop (DESIGN §6c.6) — mirrors run_vision_benchmark.
    probe_audio gates first; on failure the model is recorded `capability_absent`
    and never appears on the audio board (and is untouched on other boards).
    Every case is Tier-0 deterministic (count_slot/closed_set on synthetic WAVs
    with machine-known ground truth), so a judge is never invoked."""
    from . import audiogen, probe
    from . import audio_suite as aus
    from .targets import MockAudioTarget, audio_block, text_block

    params = params or {"temperature": 0.0, "max_tokens": 2048}   # reasoning-model headroom (see probe._ask)
    target = MockAudioTarget(model) if target_url == "mock" else OpenAITarget(target_url, model, api_key=api_key)
    judge = None  # all Tier-0 here — deterministic evaluate; a judge is never invoked
    env = {"platform": platform.platform(), "python": platform.python_version(), "runner": "aeon-mvp-audio"}

    pr = probe.probe_audio(target)
    db.create_run(run_id, model=model, target_url=target_url, judge_model=None, judge_is_self=False,
                  suite_id=aus.SUITE_ID, suite_hash=aus.suite_hash(), n_cases=len(aus.CASES),
                  params=params, env=env, board="audio", vision_probe_json=json.dumps(pr))
    if not pr.get("audio_ok"):
        db.finish_run(run_id, "capability_absent", error=pr.get("error"))
        if progress_cb:
            progress_cb("_probe", None, "capability_absent")
        return pr
    def _blocks(case):
        blocks = [text_block(case["prompt"])]
        for spec in case["audio"]:
            _, wav, _ = audiogen.synth(spec)
            blocks.append(audio_block(wav))
        return blocks

    attempt = _media_attempt(target, judge, params, build_blocks=_blocks,
                             speed_keys=("ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
                                         "ttft_after_audio_ms", "n_audio", "audio_bytes"))
    try:
        pending = []
        for case in aus.CASES:
            if not pr.get(case["requires"], False):
                db.save_result(run_id, case["id"], category=case["category"], tier=case["tier"],
                               board="audio", status="na_capability", score=None, raw_output="",
                               evidence={"skipped": case["requires"]}, speed={})
                if progress_cb:
                    progress_cb(case["id"], None, "na_capability")
                continue
            pending.append(case)
        _run_retry_passes(pending, attempt, _board_persist(run_id, "audio", progress_cb))
        db.finish_run(run_id, "succeeded")
    except Exception as e:
        db.finish_run(run_id, "failed", error=str(e))
        raise
    return pr


if __name__ == "__main__":
    # quick CLI: python -m aeon.runner <model> [target_url] [judge_model]
    #   AEON_API_KEY=<token> for authed endpoints (e.g. LM Studio with auth)
    import os
    import uuid
    model = sys.argv[1] if len(sys.argv) > 1 else "mock-good"
    target_url = sys.argv[2] if len(sys.argv) > 2 else "mock"
    judge = sys.argv[3] if len(sys.argv) > 3 else None
    api_key = os.environ.get("AEON_API_KEY") or None
    rid = uuid.uuid4().hex[:10]
    print(f"run {rid}: model={model} target={target_url} judge={judge or 'self'}")

    def cb(cid, score, status):
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  {cid:24s} {status:10s} score={s}")

    run_benchmark(rid, model, target_url, judge_model=judge, progress_cb=cb, api_key=api_key)
    from . import scoring
    import json
    print(json.dumps(scoring.leaderboard(), indent=2))
