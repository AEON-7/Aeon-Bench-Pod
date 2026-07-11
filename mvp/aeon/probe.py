"""Capability probes (DESIGN §6c.2). The vision probe uses a CONTROL PAIR: ask
the same question with and without the image block; the modality is "reached"
only if the answer changes when the image is present (and names the color). This
makes "did the modality reach the model" a deterministic comparison, never a
judge call. A model that fails the probe is recorded `capability_absent` and is
excluded from the vision board with zero effect on the text board.
"""
from __future__ import annotations

from . import imagegen
from .targets import (MockAudioTarget, MockVideoTarget, MockVisionTarget, TargetError,
                      image_block, text_block, video_block)


def _ask(target, blocks, cid="_probe"):
    # REASONING models emit hidden reasoning BEFORE the answer, all inside max_tokens — a tight cap
    # (16) truncates mid-think and returns EMPTY content, so the probe would falsely conclude the
    # modality never reached the model. Give reasoning headroom; the model stops early when done.
    return target.chat([{"role": "user", "content": blocks, "_case_id": cid}],
                       max_tokens=2048)["text"].lower()


def probe_vision(target):
    """{vision_ok, multi_image_ok, ocr_ok, evidence[, error]}."""
    if isinstance(target, MockVisionTarget):
        return {"vision_ok": True, "multi_image_ok": True, "ocr_ok": True, "evidence": "mock"}

    _, red, _ = imagegen.solid_square("red")
    _, blue, _ = imagegen.solid_square("blue")
    q = "What single color fills this image? Answer with one word."
    try:
        with_img = _ask(target, [text_block(q), image_block(red)])
        without = _ask(target, [text_block(q)])
    except TargetError as e:
        return {"vision_ok": False, "multi_image_ok": False, "ocr_ok": False, "error": str(e)[:160]}

    reached = ("red" in with_img) and (with_img.strip() != without.strip())
    multi = ocr = False
    if reached:
        try:
            two = _ask(target, [text_block("What color is the SECOND image? One word."),
                                image_block(red), image_block(blue)])
            multi = "blue" in two
        except TargetError:
            multi = False
        try:
            _, tok, _ = imagegen.token("AEON7")
            ocr = "aeon7" in _ask(target, [text_block("Reply with only the text shown."),
                                           image_block(tok)]).replace(" ", "")
        except TargetError:
            ocr = False
    return {"vision_ok": reached, "multi_image_ok": multi, "ocr_ok": ocr,
            "evidence": with_img[:80]}


def probe_video(target):
    """{video_ok, transport, evidence[, error]} — a CONTROL PAIR like probe_vision (same
    question with and without a tiny clip of a moving RED square; the modality is reached
    only when the answer changes AND names the color), with probe_audio's transport
    classification on rejection (HTTP 400/415 -> video_url unsupported ≠ model missing)."""
    if isinstance(target, MockVideoTarget):
        return {"video_ok": True, "transport": "mock", "evidence": "mock"}
    if isinstance(target, (MockVisionTarget, MockAudioTarget)):
        return {"video_ok": False, "transport": "mock", "evidence": "mock target has no video"}
    from . import videogen
    try:
        _, clip, _ = videogen.probe_clip()
    except RuntimeError as e:                # encoder dep missing — inconclusive, not a rejection
        return {"video_ok": False, "transport": "unavailable", "error": str(e)[:200]}

    q = "A short video clip is attached. What single color is the moving square? Answer with one word."
    try:
        with_vid = _ask(target, [text_block(q), video_block(clip)], cid="_vprobe")
        without = _ask(target, [text_block(q)], cid="_vprobe_ctl")
    except TargetError as e:
        err = str(e)
        # a model-load failure is inconclusive, NOT a video_url rejection
        if "load model" in err.lower() or "failed to load" in err.lower():
            return {"video_ok": False, "transport": "model_unavailable", "error": err[:200]}
        return {"video_ok": False, "transport": "rejected", "error": err[:200]}
    except Exception as e:
        return {"video_ok": False, "transport": "error", "error": str(e)[:200]}

    reached = ("red" in with_vid) and (with_vid.strip() != without.strip())
    return {"video_ok": reached, "transport": "accepted", "evidence": with_vid[:80]}


def probe_audio(target):
    """Real transport probe (DESIGN §6c.6): send a tiny WAV as an `input_audio`
    block and classify by whether the endpoint ACCEPTS it (HTTP 2xx) vs rejects
    it (400/415 -> input_audio unsupported). This resolves the open feasibility
    question for LM Studio rather than assuming. Note: acceptance proves transport,
    not that the model understood the audio — the full audio suite is the next step."""
    if isinstance(target, MockAudioTarget):
        return {"audio_ok": True, "transport": "mock", "evidence": "mock"}
    if isinstance(target, (MockVisionTarget, MockVideoTarget)):
        return {"audio_ok": False, "transport": "mock", "evidence": "mock target has no audio"}
    from . import audiogen
    from .targets import audio_block

    # Control: confirm the model actually LOADS and answers a plain text request.
    # Without this, a model-load failure masquerades as "audio unsupported".
    # max_tokens=2048 like _ask: a tight cap truncates a REASONING model mid-think
    # and returns empty content, falsely concluding the model/modality is absent.
    try:
        target.chat([{"role": "user", "content": [text_block("Reply with the single word: ready")],
                      "_case_id": "_aprobe_text"}], max_tokens=2048)
    except Exception as e:
        return {"audio_ok": False, "transport": "model_unavailable",
                "error": "model did not load/answer a text request, so audio can't be assessed: " + str(e)[:170]}

    wav = audiogen.tone_wav()
    try:
        r = target.chat([{"role": "user", "content": [
            text_block("Is an audio clip attached to this message? Answer yes or no."),
            audio_block(wav)], "_case_id": "_aprobe"}], max_tokens=2048)
        return {"audio_ok": True, "transport": "accepted", "evidence": r.get("text", "")[:160]}
    except TargetError as e:
        err = str(e)
        # a model-load failure is inconclusive, NOT an input_audio rejection
        if "load model" in err.lower() or "failed to load" in err.lower():
            return {"audio_ok": False, "transport": "model_unavailable", "error": err[:200]}
        return {"audio_ok": False, "transport": "rejected", "error": err[:200]}
    except Exception as e:
        return {"audio_ok": False, "transport": "error", "error": str(e)[:200]}
