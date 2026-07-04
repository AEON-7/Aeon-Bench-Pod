"""The benchmark run loop (DESIGN §5.3 probe runner, collapsed in-process).

For each case: render prompt → call target (capture speed) → run the case's
evaluator (Tier-0 program, or Tier-1 binary-rubric judge = the launching/target
model by default) → persist. Speed + deterministic scores always record.
"""
from __future__ import annotations

import json
import platform
import sys

from . import db
from . import suite as suite_mod
from .evaluators import evaluate
from .targets import MockTarget, OpenAITarget, TargetError


def build_target(model, target_url, api_key=None):
    if target_url == "mock":
        return MockTarget(model)
    return OpenAITarget(target_url, model, api_key=api_key)


def run_benchmark(run_id, model, target_url, judge_model=None, params=None,
                  progress_cb=None, api_key=None, judge_url=None, judge_key=None):
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
    db.create_run(
        run_id, model=model, target_url=target_url,
        judge_model=eff_judge, judge_is_self=False,
        suite_id=suite_mod.SUITE_ID, suite_hash=suite_mod.suite_hash(),
        n_cases=len(suite_mod.CASES), params=params, env=env,
    )
    base_tok = params.get("max_tokens", 512)
    retry_tok = params.get("retry_max_tokens")                # higher ceiling for a cut-off re-run
    max_retries = int(params.get("retries", 1)) if retry_tok else 0
    concurrency = max(1, int(params.get("concurrency", 1)))   # cases in flight at once (vLLM batches them)

    def _score_case(case):
        """Generate (with truncation retry) + evaluate ONE case -> a result dict. Raises TargetError
        to abort the whole run (endpoint dead). Called from worker threads; touches no shared state."""
        cid = case["id"]
        user = {"role": "user", "content": case["prompt"], "_case_id": cid}
        tok, attempts = base_tok, 0
        while True:
            try:
                resp = target.chat([user], temperature=params["temperature"], max_tokens=tok)
                text = resp["text"]
                speed = {k: resp.get(k) for k in ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens", "streamed")}
                truncated = bool(resp.get("truncated"))
                gen_status = "scored"
            except TargetError:
                raise  # endpoint is unreachable/broken → abort the whole run
            except Exception as e:  # one bad generation shouldn't kill the run
                text, speed, truncated, gen_status = "", {}, False, f"gen_error: {e!r}"[:120]
            try:
                score, evidence = evaluate(case, text, judge)
            except Exception as e:
                score, evidence = 0.0, {"error": f"eval error: {e!r}"}
            # A cut-off <think> yields an empty / non-answer that is NOT a real miss — the model ran
            # out of tokens mid-reasoning. If truncated and not already correct, RE-RUN once at the
            # higher ceiling so it can finish. (temperature stays 0; only the budget grows.)
            if truncated and attempts < max_retries and retry_tok > tok and (score is None or score < 1.0):
                attempts += 1
                tok = retry_tok
                continue
            break
        if attempts:
            if not isinstance(evidence, dict):
                evidence = {"evidence": evidence}
            evidence = {**evidence, "retried": attempts, "retry_max_tokens": retry_tok, "was_truncated": True}
        if score is None:
            status = ("needs_frontier_judge"
                      if isinstance(evidence, dict) and evidence.get("needs_frontier_judge") else "tier2_arena")
        else:
            status = "scored" if gen_status == "scored" else gen_status
        return {"cid": cid, "category": case["category"], "tier": case["tier"],
                "status": status, "score": score, "text": text, "evidence": evidence, "speed": speed}

    def _persist(res):                                        # main thread only: DB write + progress + checkpoint
        db.save_result(run_id, res["cid"], category=res["category"], tier=res["tier"],
                       status=res["status"], score=res["score"], raw_output=res["text"],
                       evidence=res["evidence"], speed=res["speed"])
        if progress_cb:
            progress_cb(res["cid"], res["score"], res["status"])

    try:
        if concurrency <= 1:
            for case in suite_mod.CASES:
                _persist(_score_case(case))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futs = [ex.submit(_score_case, c) for c in suite_mod.CASES]
                try:
                    for fut in as_completed(futs):
                        _persist(fut.result())                # a TargetError propagates -> abort the run
                except TargetError:
                    for f in futs:
                        f.cancel()
                    raise
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
    try:
        for case in vs.CASES:
            cid = case["id"]
            if not pr.get(case["requires"], False):
                db.save_result(run_id, cid, category=case["category"], tier=case["tier"], board="vision",
                               status="na_capability", score=None, raw_output="",
                               evidence={"skipped": case["requires"]}, speed={})
                if progress_cb:
                    progress_cb(cid, None, "na_capability")
                continue
            blocks = [text_block(case["prompt"])]
            for spec in case["images"]:
                _, png, _ = imagegen.generate(spec)
                blocks.append(image_block(png))
            user = {"role": "user", "content": blocks, "_case_id": cid}
            try:
                resp = target.chat([user], temperature=params["temperature"], max_tokens=params["max_tokens"])
                text = resp["text"]
                speed = {k: resp.get(k) for k in
                         ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
                          "ttft_after_image_ms", "n_images", "image_bytes")}
                gen_status = "scored"
            except TargetError:
                raise
            except Exception as e:
                text, speed, gen_status = "", {}, f"gen_error: {e!r}"[:120]
            try:
                score, evidence = evaluate(case, text, judge)
            except Exception as e:
                score, evidence = 0.0, {"error": f"eval error: {e!r}"}
            status = "scored" if gen_status == "scored" else gen_status
            db.save_result(run_id, cid, category=case["category"], tier=case["tier"], board="vision",
                           status=status, score=score, raw_output=text, evidence=evidence, speed=speed)
            if progress_cb:
                progress_cb(cid, score, status)
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
    try:
        for case in aus.CASES:
            cid = case["id"]
            if not pr.get(case["requires"], False):
                db.save_result(run_id, cid, category=case["category"], tier=case["tier"], board="audio",
                               status="na_capability", score=None, raw_output="",
                               evidence={"skipped": case["requires"]}, speed={})
                if progress_cb:
                    progress_cb(cid, None, "na_capability")
                continue
            blocks = [text_block(case["prompt"])]
            for spec in case["audio"]:
                _, wav, _ = audiogen.synth(spec)
                blocks.append(audio_block(wav))
            user = {"role": "user", "content": blocks, "_case_id": cid}
            try:
                resp = target.chat([user], temperature=params["temperature"], max_tokens=params["max_tokens"])
                text = resp["text"]
                speed = {k: resp.get(k) for k in
                         ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens",
                          "ttft_after_audio_ms", "n_audio", "audio_bytes")}
                gen_status = "scored"
            except TargetError:
                raise
            except Exception as e:
                text, speed, gen_status = "", {}, f"gen_error: {e!r}"[:120]
            try:
                score, evidence = evaluate(case, text, judge)
            except Exception as e:
                score, evidence = 0.0, {"error": f"eval error: {e!r}"}
            status = "scored" if gen_status == "scored" else gen_status
            db.save_result(run_id, cid, category=case["category"], tier=case["tier"], board="audio",
                           status=status, score=score, raw_output=text, evidence=evidence, speed=speed)
            if progress_cb:
                progress_cb(cid, score, status)
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
