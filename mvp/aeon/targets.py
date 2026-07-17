"""Target adapters — how the runner talks to a model under test / a judge.

`OpenAITarget` speaks the OpenAI-compatible /v1 API (Ollama, LM Studio, vLLM,
TGI, llama.cpp server, OpenAI, ...). It streams so we can measure TTFT and
decode throughput honestly (DESIGN §11). `MockTarget` returns canned answers
so the pipeline + dashboard can be exercised without a model.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class TargetError(RuntimeError):
    pass


def no_answer_reason(exc=None, text=None):
    """Classify ONE generation attempt for the no-answer fairness rule: a case that yields
    NO ANSWER is a technical glitch, not a wrong answer — the runner re-runs it in up to
    two retry passes, and only when every pass fails does it become results.status
    ='no_answer' (score NULL; the mothership weights it at ¼ of a case).

    Returns None for a genuine answer, else a short reason string:
      * 'transport: <exc>'  — the attempt RAISED (connection refused/reset, timeout,
        HTTP failure incl. TargetError from a non-2xx response): nothing was generated;
      * 'empty_completion'  — the request succeeded (HTTP 200) but the completion is
        empty or whitespace-only: still not an answer.
    Any NON-EMPTY completion is an ANSWER — a wrong answer scores 0 at full weight and
    is never retried by this rule."""
    if exc is not None:
        return f"transport: {exc!r}"[:200]
    if text is None or not str(text).strip():
        return "empty_completion"
    return None


def _clean(messages):
    """Send only standard OpenAI fields (drop internal tags like _case_id)."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _ipv4(url):
    """Avoid the Windows localhost->::1 slow-refuse: prefer the IPv4 loopback."""
    return url.replace("//localhost", "//127.0.0.1")


# ---- multimodal content helpers (vision board) ----

def text_block(text):
    return {"type": "text", "text": text}


def image_block(png_bytes):
    b64 = base64.b64encode(png_bytes).decode()
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}


def audio_block(wav_bytes, fmt="wav"):
    b64 = base64.b64encode(wav_bytes).decode()
    return {"type": "input_audio", "input_audio": {"data": b64, "format": fmt}}


def video_block(mp4_bytes, mime="video/mp4"):
    """OpenAI-compatible video content part — the vLLM convention for qwen-vl-style
    models ({"type":"video_url"}), mirroring image_block's data-URL transport."""
    b64 = base64.b64encode(mp4_bytes).decode()
    return {"type": "video_url", "video_url": {"url": f"data:{mime};base64," + b64}}


def _prompt_chars(messages):
    """Total characters of prompt text across messages (str or block-list content)."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    total += len(blk.get("text") or "")
    return total


def _input_tokens(messages, usage):
    """(input_tokens, estimated) — prefer server-reported usage.prompt_tokens
    (vLLM sends it in the final stream chunk when stream_options.include_usage
    is set); otherwise fall back to a chars//4 estimate and say so honestly."""
    tok = (usage or {}).get("prompt_tokens")
    if isinstance(tok, (int, float)) and tok > 0:
        return int(tok), False
    return max(1, _prompt_chars(messages) // 4), True


def _img_stats(messages):
    """(n_images, total_decoded_bytes) across image_url blocks in the messages."""
    n, nbytes = 0, 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "image_url":
                    n += 1
                    url = blk.get("image_url", {}).get("url", "")
                    payload = url.split(",", 1)[1] if "," in url else url
                    nbytes += (len(payload) * 3) // 4
    return n, nbytes


def _audio_stats(messages):
    """(n_audio, total_decoded_bytes) across input_audio blocks in the messages."""
    n, nbytes = 0, 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "input_audio":
                    n += 1
                    payload = blk.get("input_audio", {}).get("data", "")
                    nbytes += (len(payload) * 3) // 4
    return n, nbytes


def _video_stats(messages):
    """(n_video, total_decoded_bytes) across video_url blocks in the messages."""
    n, nbytes = 0, 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "video_url":
                    n += 1
                    url = blk.get("video_url", {}).get("url", "")
                    payload = url.split(",", 1)[1] if "," in url else url
                    nbytes += (len(payload) * 3) // 4
    return n, nbytes


# Per-request HTTP timeout. Under concurrency each stream INDIVIDUALLY slows (the server
# time-slices decode across N streams) even though total wall time drops, so a fixed timeout
# that is fine at c=1 spuriously kills healthy long generations at c=24. The pod scales it
# once via AEON_HTTP_TIMEOUT (aeon_pod._scale_http_timeout: base 180s * ceil(conc/4), cap
# 1800s) and every target built afterwards inherits it; an explicit timeout= arg still wins.
_BASE_TIMEOUT = 180


def _http_timeout():
    try:
        return min(1800, max(1, int(os.environ.get("AEON_HTTP_TIMEOUT", ""))))
    except (TypeError, ValueError):
        return _BASE_TIMEOUT


class OpenAITarget:
    def __init__(self, base_url, model, api_key=None, timeout=None, extra_body=None):
        self.base_url = _ipv4(base_url.rstrip("/"))
        self.model = model
        self.api_key = api_key
        self.timeout = timeout or _http_timeout()   # None -> env-scaled default (see above)
        self.extra_body = extra_body or {}

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = "Bearer " + self.api_key
        return h

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        """Return {text, ttft_ms, decode_tps, e2e_ms, output_tokens}.

        Streams; falls back to a non-streaming request if the server rejects
        the stream (then ttft/decode_tps are null — never faked as e2e).
        """
        try:
            res = self._chat_stream(messages, temperature, max_tokens)
        except TargetError:
            raise
        except Exception:
            res = self._chat_once(messages, temperature, max_tokens)
        n, nbytes = _img_stats(messages)
        if n:
            res["n_images"] = n
            res["image_bytes"] = nbytes
            # honest label: this TTFT includes upload + server decode + prefill (§6c.5)
            res["ttft_after_image_ms"] = res.get("ttft_ms")
        na, abytes = _audio_stats(messages)
        if na:
            res["n_audio"] = na
            res["audio_bytes"] = abytes
            # honest label: this TTFT includes upload + server decode + prefill (§6c.5)
            res["ttft_after_audio_ms"] = res.get("ttft_ms")
        nv, vbytes = _video_stats(messages)
        if nv:
            res["n_video"] = nv
            res["video_bytes"] = vbytes
            # honest label: this TTFT includes upload + server decode + prefill (§6c.5)
            res["ttft_after_video_ms"] = res.get("ttft_ms")
        return res

    def _post(self, payload, stream):
        url = self.base_url + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        return urllib.request.urlopen(req, timeout=self.timeout)

    def _apply_extra(self, payload, max_tokens):
        if not self.extra_body:
            return payload
        token_field = self.extra_body.get("_token_field")
        omit_temperature = bool(self.extra_body.get("_omit_temperature"))
        for k, v in self.extra_body.items():
            if not str(k).startswith("_"):
                payload[k] = v
        if token_field and token_field != "max_tokens":
            payload.pop("max_tokens", None)
            payload[str(token_field)] = max_tokens
        if omit_temperature:
            payload.pop("temperature", None)
        return payload

    def _chat_stream(self, messages, temperature, max_tokens):
        payload = {
            "model": self.model,
            "messages": _clean(messages),
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream_options": {"include_usage": True},
        }
        payload = self._apply_extra(payload, max_tokens)
        t0 = time.perf_counter()
        ttft = None          # time to the FIRST generated token of ANY kind (incl. hidden reasoning)
        chunks = 0           # streamed token-chunks (reasoning + content) — for timing + fallback count
        parts = []           # ANSWER text only (content); reasoning is never part of the answer
        usage = None
        finish = None        # finish_reason of the last choice; "length" == hit max_tokens (truncated)
        try:
            resp = self._post(payload, stream=True)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise TargetError(f"HTTP {e.code} from {self.base_url}: {body}")
        with resp as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload_str = line[5:].strip()
                if payload_str == "[DONE]":
                    break
                try:
                    obj = json.loads(payload_str)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    if choices[0].get("finish_reason"):
                        finish = choices[0]["finish_reason"]
                    delta = choices[0].get("delta") or {}
                    c = delta.get("content")
                    # Reasoning models stream hidden thinking in a separate field BEFORE the
                    # answer. It counts toward generation timing + throughput, but is NOT part
                    # of the answer text. (Without this, TTFT inflates to the whole think time
                    # and decode_tps divides all tokens by the tiny content-only window.)
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if (c or reasoning) and ttft is None:
                        ttft = time.perf_counter() - t0
                    if c or reasoning:
                        chunks += 1
                    if c:
                        parts.append(c)
                if obj.get("usage"):
                    usage = obj["usage"]
        t_last = time.perf_counter()
        text = "".join(parts)
        if not text and chunks == 0:
            # Server streamed nothing useful — treat as a non-stream fallback.
            return self._chat_once(messages, temperature, max_tokens)
        out_tok = (usage or {}).get("completion_tokens") or chunks or max(1, len(text) // 4)
        in_tok, in_est = _input_tokens(messages, usage)
        decode_span = (t_last - t0) - (ttft or 0.0)
        tps = (out_tok / decode_span) if decode_span > 1e-6 else None
        return {
            "text": text,
            "ttft_ms": round(ttft * 1000, 2) if ttft is not None else None,
            "decode_tps": round(tps, 2) if tps else None,
            "e2e_ms": round((t_last - t0) * 1000, 2),
            "output_tokens": out_tok,
            "input_tokens": in_tok,
            "input_tokens_estimated": in_est,
            "finish_reason": finish,
            "truncated": finish == "length",
            "streamed": True,
        }

    def _chat_once(self, messages, temperature, max_tokens):
        payload = {
            "model": self.model,
            "messages": _clean(messages),
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        payload = self._apply_extra(payload, max_tokens)
        t0 = time.perf_counter()
        try:
            resp = self._post(payload, stream=False)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise TargetError(f"HTTP {e.code} from {self.base_url}: {body}")
        with resp as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        t_last = time.perf_counter()
        choice0 = (obj.get("choices") or [{}])[0]
        text = choice0.get("message", {}).get("content", "") or ""
        finish = choice0.get("finish_reason")
        out_tok = (obj.get("usage") or {}).get("completion_tokens") or max(1, len(text) // 4)
        in_tok, in_est = _input_tokens(messages, obj.get("usage"))
        return {
            "text": text,
            "ttft_ms": None,          # not measurable without streaming — never faked
            "decode_tps": None,
            "e2e_ms": round((t_last - t0) * 1000, 2),
            "output_tokens": out_tok,
            "input_tokens": in_tok,
            "input_tokens_estimated": in_est,
            "finish_reason": finish,
            "truncated": finish == "length",
            "streamed": False,
        }


class AnthropicTarget:
    """Anthropic Messages API adapter with the same Target.chat contract."""

    def __init__(self, base_url, model, api_key=None, timeout=None, extra_body=None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout or _http_timeout()
        self.extra_body = extra_body or {}

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "anthropic-version": os.environ.get("AEON_ANTHROPIC_VERSION", "2023-06-01"),
        }
        if self.api_key:
            h["x-api-key"] = self.api_key
        return h

    def _messages(self, messages):
        system, out = [], []
        for m in messages:
            role = m.get("role") or "user"
            content = m.get("content") or ""
            if isinstance(content, list):
                content = "\n".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            if role == "system":
                system.append(str(content))
            elif role in ("user", "assistant"):
                out.append({"role": role, "content": str(content)})
            else:
                out.append({"role": "user", "content": str(content)})
        payload = {"messages": out or [{"role": "user", "content": ""}]}
        if system:
            payload["system"] = "\n\n".join(system)
        return payload

    def _base_payload(self, messages, max_tokens, stream):
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "stream": stream,
            **self._messages(_clean(messages)),
        }
        if self.extra_body:
            payload.update(self.extra_body)
        return payload

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/messages",
            data=data,
            headers=self._headers(),
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=self.timeout)

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        try:
            return self._chat_stream(messages, max_tokens)
        except TargetError:
            raise
        except Exception:
            return self._chat_once(messages, max_tokens)

    def _chat_stream(self, messages, max_tokens):
        payload = self._base_payload(messages, max_tokens, True)
        t0 = time.perf_counter()
        ttft = None
        chunks = 0
        parts = []
        usage = {}
        stop_reason = None
        try:
            resp = self._post(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise TargetError(f"HTTP {e.code} from {self.base_url}: {body}")
        with resp as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ == "content_block_delta":
                    delta = obj.get("delta") or {}
                    text = delta.get("text") or delta.get("thinking") or ""
                    if text and ttft is None:
                        ttft = time.perf_counter() - t0
                    if text:
                        chunks += 1
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        parts.append(delta["text"])
                elif typ == "message_delta":
                    usage.update(obj.get("usage") or {})
                    stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason
                elif typ == "message_stop":
                    break
        t_last = time.perf_counter()
        text = "".join(parts)
        out_tok = usage.get("output_tokens") or chunks or max(1, len(text) // 4)
        decode_span = (t_last - t0) - (ttft or 0.0)
        tps = (out_tok / decode_span) if decode_span > 1e-6 else None
        return {
            "text": text,
            "ttft_ms": round(ttft * 1000, 2) if ttft else None,
            "decode_tps": round(tps, 2) if tps else None,
            "e2e_ms": round((t_last - t0) * 1000, 2),
            "output_tokens": out_tok,
            "finish_reason": stop_reason,
            "truncated": stop_reason == "max_tokens",
            "streamed": True,
        }

    def _chat_once(self, messages, max_tokens):
        payload = self._base_payload(messages, max_tokens, False)
        t0 = time.perf_counter()
        try:
            resp = self._post(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise TargetError(f"HTTP {e.code} from {self.base_url}: {body}")
        with resp as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        t_last = time.perf_counter()
        text = "".join(
            b.get("text", "") for b in obj.get("content", [])
            if b.get("type") == "text"
        )
        out_tok = (obj.get("usage") or {}).get("output_tokens") or max(1, len(text) // 4)
        stop_reason = obj.get("stop_reason")
        return {
            "text": text,
            "ttft_ms": None,
            "decode_tps": None,
            "e2e_ms": round((t_last - t0) * 1000, 2),
            "output_tokens": out_tok,
            "finish_reason": stop_reason,
            "truncated": stop_reason == "max_tokens",
            "streamed": False,
        }


class MockTarget:
    """Deterministic canned answers keyed by case id, for pipeline/UI testing.

    Two personas: 'mock-good' answers correctly; 'mock-sloppy' makes the kinds
    of mistakes weak models make (wrong format, off-by, verbose).
    """

    GOOD = {
        "math.mul": "The product is \\boxed{391}.",
        "math.div": "\\boxed{12}",
        "math.quad": "The roots are \\boxed{3, -5}.",
        "if.pong": "PONG",
        "if.three_colors": "red\ngreen\nblue",
        "if.no_e": "An odd941 quiz",
        "reason.syllogism": "Yes. \\boxed{yes}",
        "reason.batball": "\\boxed{5}",
        "code.add": "```python\ndef add(a, b):\n    return a + b\n```",
        "code.palindrome": "```python\ndef is_palindrome(s):\n    s = s.lower()\n    return s == s[::-1]\n```",
        "prose.ocean3": "The tide breathes slow on shoals of grey\nthe ocean folds the light away\nand gulls go scattering with the spray",
    }
    SLOPPY = {
        "math.mul": "Let me compute 17 times 23... it is 17*23 = 371.",
        "math.div": "144/12 equals 12, so the answer is 12.",
        "math.quad": "x = 3 or x = 5",
        "if.pong": "Sure! PONG!",
        "if.three_colors": "Here are some colors: red, green, blue, yellow.",
        "if.no_e": "Here are several letters everywhere.",
        "reason.syllogism": "Hmm, not necessarily. \\boxed{no}",
        "reason.batball": "The ball costs \\boxed{10} cents.",
        "code.add": "```python\ndef add(a, b):\n    return a - b\n```",
        "code.palindrome": "def is_palindrome(s): return s == s[::-1]",
        "prose.ocean3": "The ocean is very big and blue and I like it a lot because it has waves and fish and boats.",
    }

    def __init__(self, persona="mock-good"):
        self.model = persona
        self.table = self.GOOD if persona == "mock-good" else self.SLOPPY

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        case_id = messages[0].get("_case_id") if messages else None
        text = self.table.get(case_id, "I am not sure.")
        time.sleep(0.01)
        toks = max(1, len(text) // 4)
        return {
            "text": text,
            "ttft_ms": 12.0,
            "decode_tps": 95.0,
            "e2e_ms": 10.0 + toks,
            "output_tokens": toks,
            "streamed": True,
        }


# wrong-on-purpose reply for '*-bad' mock personas: in-slot everywhere, correct nowhere
# (closed_set rejects non-members, count_slot mismatches, CER blows past every threshold,
# and no keyword group matches)
_BAD_REPLY = ("<answer>zzzz</answer><count>-999</count><ocr>#####</ocr>"
              "<object>zzzz</object><color>zzzz</color>")


def _gold_case_answer(case):
    """Derive the CORRECT reply for a suite case from its OWN eval spec, so mock targets
    never drift from the suite. Slot checkers emit their slot-formatted answers; keyword
    checkers emit one sentence carrying the first synonym of every group in group order
    (so ordered_keywords holds); a Tier-1 rubric emits its tier0-shadow slots."""
    ev = case["eval"]
    if "rubric" in ev:
        return "".join(
            "<{s}>{a}</{s}>".format(s=cr["tier0_check"].get("slot", "answer"),
                                    a=cr["tier0_check"]["answer"])
            for cr in ev["rubric"] if "tier0_check" in cr)
    parts, kw, kw_slot, kw_scan = [], [], "answer", False
    for chk in ev["checkers"]:
        t = chk["type"]
        if t in ("keyword_all", "keyword_set", "ordered_keywords", "keyword_any"):
            groups = chk.get("groups") or ([chk["keywords"]] if chk.get("keywords") else [])
            kw += [g[0] for g in groups if g]
            kw_slot = chk.get("slot", "answer")
            kw_scan = kw_scan or chk.get("scan") == "text"
        elif t == "count_slot":
            slot = chk.get("slot", "count")
            parts.append(f"<{slot}>{chk['value']}</{slot}>")
        elif t == "closed_set":
            slot = chk.get("slot", "answer")
            parts.append(f"<{slot}>{chk['answer']}</{slot}>")
        elif t == "cer_threshold":
            slot = chk.get("slot", "ocr")
            parts.append(f"<{slot}>{chk['value']}</{slot}>")
    if kw:
        sent = "it shows " + ", then ".join(kw)
        parts.append(sent if kw_scan else f"<{kw_slot}>{sent}</{kw_slot}>")
    return " ".join(parts) or "<answer>unknown</answer>"


class MockVisionTarget:
    """Slot-formatted answers for the vision suite, keyed by _case_id — lets the vision
    board be exercised with zero GPU. probe_vision() short-circuits this class to
    vision_ok=True. The gold table is DERIVED from vision_suite's own checkers
    (_gold_case_answer), so it never drifts from the suite. Personas: 'mock-vision*'
    answers correctly; any '*-bad' persona answers wrong on every case."""

    def __init__(self, persona="mock-vision"):
        self.model = persona
        self.bad = persona.endswith("-bad")
        self._table = None

    def _gold(self):
        if self._table is None:
            from . import vision_suite  # deferred: no import cycle
            self._table = {c["id"]: _gold_case_answer(c) for c in vision_suite.CASES}
        return self._table

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        cid = messages[0].get("_case_id") if messages else None
        text = _BAD_REPLY if self.bad else self._gold().get(cid, "<answer>unknown</answer>")
        n, nbytes = _img_stats(messages)
        time.sleep(0.005)
        return {"text": text, "ttft_ms": 11.0, "decode_tps": 90.0, "e2e_ms": 9.0,
                "output_tokens": max(1, len(text) // 4), "streamed": True,
                "n_images": n, "image_bytes": nbytes, "ttft_after_image_ms": 11.0}


class MockVideoTarget:
    """Slot-formatted answers for the video suite, keyed by _case_id — lets the video
    board be exercised with zero GPU (and no ffmpeg: the mock never decodes anything).
    probe_video() short-circuits this class to video_ok=True. The gold table is DERIVED
    from video_suite's own checkers (_gold_case_answer). Personas: 'mock-video*' answers
    correctly; any '*-bad' persona answers wrong on every case."""

    def __init__(self, persona="mock-video"):
        self.model = persona
        self.bad = persona.endswith("-bad")
        self._table = None

    def _gold(self):
        if self._table is None:
            from . import video_suite  # deferred: no import cycle
            self._table = {c["id"]: _gold_case_answer(c) for c in video_suite.CASES}
        return self._table

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        cid = messages[0].get("_case_id") if messages else None
        text = _BAD_REPLY if self.bad else self._gold().get(cid, "<answer>unknown</answer>")
        n, nbytes = _video_stats(messages)
        time.sleep(0.005)
        return {"text": text, "ttft_ms": 11.0, "decode_tps": 90.0, "e2e_ms": 9.0,
                "output_tokens": max(1, len(text) // 4), "streamed": True,
                "n_video": n, "video_bytes": nbytes, "ttft_after_video_ms": 11.0}


class MockAudioTarget:
    """Slot-formatted answers for the audio suite, keyed by _case_id — lets the
    audio board be exercised with zero GPU. probe_audio() short-circuits this
    class to audio_ok=True. The gold table is DERIVED from audio_suite's own
    deterministic checkers (count_slot/closed_set), so it never drifts from the
    suite. Personas: 'mock-audio*' answers correctly; any '*-bad' persona
    answers wrong on every case (for scoring-path tests)."""

    def __init__(self, persona="mock-audio"):
        self.model = persona
        self.bad = persona.endswith("-bad")
        self._table = None

    def _gold(self):
        if self._table is None:
            from . import audio_suite  # deferred: no import cycle (audio_suite imports audiogen only)
            table = {}
            for c in audio_suite.CASES:
                chk = c["eval"]["checkers"][0]
                if chk["type"] == "count_slot":
                    slot = chk.get("slot", "count")
                    table[c["id"]] = f"<{slot}>{chk['value']}</{slot}>"
                else:  # closed_set
                    slot = chk.get("slot", "answer")
                    table[c["id"]] = f"<{slot}>{chk['answer']}</{slot}>"
            self._table = table
        return self._table

    def chat(self, messages, *, temperature=0.0, max_tokens=512):
        cid = messages[0].get("_case_id") if messages else None
        if self.bad:
            text = "<answer>zzzz</answer><count>-999</count>"  # in-slot but wrong everywhere
        else:
            text = self._gold().get(cid, "<answer>unknown</answer>")
        n, nbytes = _audio_stats(messages)
        time.sleep(0.005)
        return {"text": text, "ttft_ms": 11.0, "decode_tps": 90.0, "e2e_ms": 9.0,
                "output_tokens": max(1, len(text) // 4), "streamed": True,
                "n_audio": n, "audio_bytes": nbytes, "ttft_after_audio_ms": 11.0}


# SSRF guard for the caller-driven list_models fetch: only http/https, no redirects, a bounded read,
# and a resolved-IP block on internal address space so it can't be used to hit a cloud metadata
# service. The primary SSRF fix is app.py gating /api/models to the pod; this is defense-in-depth.
# NOTE: the pod's WHOLE JOB is to list models from its LOCAL serving endpoint (default
# 127.0.0.1:8000/v1) or a LAN/Tailscale box (e.g. 192.168.x / 100.x CGNAT), so loopback + private +
# CGNAT are ALLOWED by default; set AEON_SSRF_STRICT=1 to also reject those (public targets only).
# The cloud-metadata link-local range (169.254/16, fd00-style unique-local) is NEVER a legitimate
# model endpoint and is blocked unconditionally.
_LIST_MAX = 2 * 1024 * 1024      # hard cap on the outbound response body (was an unbounded read)
_SSRF_STRICT = os.environ.get("AEON_SSRF_STRICT") == "1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow 3xx — a redirect could bounce a validated host to an internal one."""
    def redirect_request(self, *a, **k):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _ip_is_blocked(ip):
    """True for any address list_models must not fetch from. Always blocks the metadata/link-local
    range and other non-routable specials; additionally blocks loopback/private/CGNAT/ULA when
    AEON_SSRF_STRICT=1 (those are legitimate LOCAL model endpoints on a pod otherwise)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    # Unconditional: the cloud-metadata / link-local range (169.254.169.254 lives here) and other
    # reserved/unspecified/multicast space are never a real model endpoint.
    if addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    if not _SSRF_STRICT:
        return False
    # Strict (public-only) mode: also reject loopback, RFC1918 private, ULA, and CGNAT.
    if addr.is_loopback or addr.is_private or not addr.is_global:
        return True
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True  # CGNAT
    return False


def _safe_open(url, headers, timeout):
    """Open `url` only after its host resolves to an allowed IP, pinning that IP into the request
    to defeat DNS-rebinding (re-check after DNS resolution), with redirects disabled."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise TargetError("only http/https targets are allowed")
    host = parts.hostname
    if not host:
        raise TargetError("no host in target url")
    # Resolve + validate EVERY address the host maps to (reject if any is blocked).
    infos = socket.getaddrinfo(host, parts.port, proto=socket.IPPROTO_TCP)
    ips = {ai[4][0] for ai in infos}
    if not ips or any(_ip_is_blocked(ip) for ip in ips):
        raise TargetError("target host resolves to a blocked address")
    pin = next(iter(ips))
    # Pin the resolved IP into the URL and send Host: so a second DNS lookup can't swap the target.
    netloc = f"[{pin}]" if ":" in pin else pin
    if parts.port:
        netloc += f":{parts.port}"
    pinned = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    req = urllib.request.Request(pinned, headers={**headers, "Host": host})
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


def list_models(base_url, timeout=2, api_key=None):
    """Best-effort list of model ids from an OpenAI-compatible or Ollama endpoint."""
    base = _ipv4(base_url.rstrip("/"))
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    candidates = [base + "/models", base.rsplit("/v1", 1)[0] + "/api/tags"]
    for url in candidates:
        try:
            with _safe_open(url, headers, timeout) as r:   # scheme+host validated, no redirects
                raw = r.read(_LIST_MAX + 1)
            if len(raw) > _LIST_MAX:                        # bound the outbound read (unbounded-read finding)
                return []
            obj = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(obj, dict) and "data" in obj:          # OpenAI /v1/models
                return [m["id"] for m in obj["data"]]
            if isinstance(obj, dict) and "models" in obj:        # Ollama /api/tags
                return [m["name"] for m in obj["models"]]
        except Exception:
            continue
    return []
