"""Does tool calling actually work on this endpoint? Ask it, before spending hours finding out.

The failure this exists to catch
-------------------------------
An agent harness only sees a tool call if the SERVER converts the model's output into OpenAI
`tool_calls`. That conversion is done by whatever `--tool-call-parser` the serve was started with.
Get it wrong and nothing errors: the model emits perfectly good tool calls, the server hands back
prose, all three harnesses watch an agent that never uses a tool, and agentic — 30% of the AEON
score — lands near zero. On the board that is indistinguishable from a model which genuinely cannot
do agentic work.

The pod has `probe_vision` / `probe_video` / `probe_audio` for exactly this shape of question and
had nothing for tools, which is the one capability the agentic suite is built on. This is the
fourth probe. Same contract as the others: never raises, returns a dict, costs seconds.

What it actually checks, and why each phase earns its place
----------------------------------------------------------
0. CONTROL — one plain request, no tools. If this fails the endpoint is down or overloaded and the
   parser must NOT be blamed; the verdict is `inconclusive` and it never gates anything.
1. AUTO, non-streaming — the baseline.
2. AUTO, streaming — the harnesses stream, and a documented class of parser bugs is streaming-only
   (tool-call deltas reassembled wrong). A non-streaming-only probe passes while the harness fails,
   which is worse than no probe: it manufactures confidence.
3. AUTO, 4 concurrent — `run_agentic_v2` runs at concurrency 4, and another documented class of
   parser bug is shared mutable state across simultaneous requests.
4. REQUIRED — only on failure, and it is the interesting one. `tool_choice="required"` is served
   through constrained decoding and needs no parser at all, so REQUIRED-passing while AUTO-fails is
   positive evidence of a PARSER fault rather than a weak model. That discriminator does not exist
   anywhere else in the codebase. It is a diagnostic only — never a run mode, since forcing a call
   every turn stops an agent ever answering or deciding it is done.

Leaked syntax is treated as failure even when `tool_calls` is populated, because raw markup sitting
in `content` is the signature of a partial parse — and the leaked tokens NAME the parser that
should have caught them, so the failure detector doubles as the repair hint.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import toolformat

# One tool, unambiguous, with a required argument — so "did it call correctly" is decidable.
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}
PROBE_PROMPT = "What is the weather in Paris right now? Use the tool."
CONTROL_PROMPT = "Reply with exactly the word: READY"

# Timeouts are CALIBRATED, not guessed. A 26B model on a loaded Spark took 12.8s to return 16
# tokens — a fixed 12s ceiling reported "inconclusive" on a perfectly healthy serve, which is a
# false negative on exactly the big models this has to work for. So the control request is given
# room, its latency is measured, and every later phase gets a multiple of what this endpoint
# actually costs. Fast endpoint: the whole probe is over in seconds. Slow one: it waits.
CONTROL_TIMEOUT_S = float(os.environ.get("AEON_PROBE_TOOLS_CONTROL_TIMEOUT", "75"))
_LATENCY_MULTIPLIER = 4.0        # a tool call generates more than the control's one word
_MIN_REQ_TIMEOUT_S = 15.0
_MAX_REQ_TIMEOUT_S = 120.0
_BUDGET_MULTIPLIER = 8.0         # whole-probe ceiling, also derived from measured latency
_MIN_BUDGET_S = 45.0
_MAX_BUDGET_S = float(os.environ.get("AEON_PROBE_TOOLS_MAX_BUDGET", "420"))
_CONCURRENCY = 4


# ---- minimal OpenAI client -------------------------------------------------------------------
# targets.OpenAITarget.chat() cannot send `tools` or read `tool_calls`, and the scored path is not
# the place to grow a diagnostic-only feature. This is deliberately small and self-contained.

def _post(base_url: str, payload: dict, api_key: str | None, timeout: float):
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def _call(base_url, model, *, api_key=None, tools=None, tool_choice=None, stream=False,
          prompt=PROBE_PROMPT, timeout=_MIN_REQ_TIMEOUT_S, max_tokens=256) -> dict:
    """One request. Returns {ok, tool_calls, content, error} — never raises."""
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "max_tokens": max_tokens, "stream": bool(stream)}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    try:
        resp = _post(base_url, payload, api_key, timeout)
        raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"ok": False, "tool_calls": [], "content": "",
                "error": f"HTTP {e.code}: {body or e.reason}"}
    except Exception as e:
        return {"ok": False, "tool_calls": [], "content": "",
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return (_parse_stream(raw) if stream else _parse_once(raw))


def _parse_once(raw: str) -> dict:
    try:
        doc = json.loads(raw)
    except ValueError:
        return {"ok": False, "tool_calls": [], "content": "", "error": "non-JSON response"}
    try:
        msg = (doc.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return {"ok": False, "tool_calls": [], "content": "", "error": "unexpected response shape"}
    return {"ok": True, "tool_calls": list(msg.get("tool_calls") or []),
            "content": msg.get("content") or "", "error": None}


def _parse_stream(raw: str) -> dict:
    """Reassemble SSE deltas. Tool-call fragments arrive indexed and must be stitched by index —
    doing that wrong is itself one of the streaming-only parser bugs this phase exists to catch."""
    content, calls = [], {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            doc = json.loads(body)
        except ValueError:
            continue
        try:
            delta = (doc.get("choices") or [{}])[0].get("delta") or {}
        except Exception:
            continue
        if delta.get("content"):
            content.append(delta["content"])
        for tc in (delta.get("tool_calls") or []):
            idx = tc.get("index", 0)
            slot = calls.setdefault(idx, {"function": {"name": "", "arguments": ""}})
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
    return {"ok": True, "tool_calls": [calls[k] for k in sorted(calls)],
            "content": "".join(content), "error": None}


# ---- verdict logic ----------------------------------------------------------------------------

def _called_ok(res: dict) -> tuple[bool, str]:
    """Did this response contain a USABLE tool call? (ok, why-not).

    Raw tool markup left in `content` counts as a FAILURE even when tool_calls is populated: it
    means the parser understood part of the output and passed the rest through, which is a
    misconfiguration that will bite the harness on a longer transcript."""
    if not res.get("ok"):
        return False, res.get("error") or "request failed"
    leak = toolformat.scan_leak(res.get("content") or "")
    if leak:
        return False, f"raw {leak} tool syntax left in message.content (parser did not consume it)"
    calls = res.get("tool_calls") or []
    if not calls:
        return False, "no tool_calls in the response"
    fn = (calls[0].get("function") or {})
    if not fn.get("name"):
        return False, "tool_call present but unnamed"
    return True, "called " + str(fn.get("name"))


def probe(base_url: str, model: str, *, api_key: str | None = "sk-local",
          budget_s: float | None = None, fingerprint: dict | None = None) -> dict:
    """Can this endpoint do OpenAI tool calling, in the modes the harnesses actually use?

    Returns a dict and NEVER raises (same contract as probe_vision/video/audio):

      verdict          ok | parser_fault | model_incapable | inconclusive
      modes            {nonstream, stream, concurrent} -> True/False/None
      required_oracle  True when tool_choice="required" worked while auto did not
      leaked_format    the wire format the model emitted as plain text, if any
      suggested_parser the --tool-call-parser that would have caught it
      remediation      a copy-pasteable line for the operator
    """
    try:
        return _probe(base_url, model, api_key=api_key, budget_s=budget_s, fingerprint=fingerprint)
    except Exception as e:
        # A diagnostic must never be able to take down the run it is protecting. `_call` is
        # written not to raise, so reaching here means a bug — report it as inconclusive (which
        # gates nothing) rather than aborting a multi-hour benchmark.
        return {"verdict": "inconclusive", "modes": {"nonstream": None, "stream": None,
                                                     "concurrent": None},
                "required_oracle": None, "leaked_format": None, "suggested_parser": None,
                "remediation": None, "elapsed_s": 0.0,
                "probe_version": "aeon-probe-tools-v1",
                "detail": f"the probe itself failed ({type(e).__name__}: {str(e)[:160]}); no "
                          f"conclusion drawn about tool calling and nothing is gated on it"}


def _probe(base_url: str, model: str, *, api_key: str | None = "sk-local",
           budget_s: float | None = None, fingerprint: dict | None = None) -> dict:
    t0 = time.monotonic()
    out = {"verdict": "inconclusive", "modes": {"nonstream": None, "stream": None,
                                                "concurrent": None},
           "required_oracle": None, "leaked_format": None, "suggested_parser": None,
           "remediation": None, "detail": "", "elapsed_s": 0.0,
           "probe_version": "aeon-probe-tools-v1"}

    def _left():
        return budget_s - (time.monotonic() - t0)

    def _finish(**kw):
        out.update(kw)
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
        return out

    # ---- 0. control: is the endpoint even answering? --------------------------------------
    c0 = time.monotonic()
    ctl = _call(base_url, model, api_key=api_key, prompt=CONTROL_PROMPT, max_tokens=16,
                timeout=CONTROL_TIMEOUT_S)
    ctl_s = time.monotonic() - c0
    if not ctl.get("ok"):
        # Retry once. A busy serve spikes: on a live production endpoint this control timed out
        # at 75s while a curl issued seconds later returned in 15s. A transient stall must not be
        # able to void the probe — and since `inconclusive` gates nothing, the cost of one retry
        # is far smaller than the cost of reporting nothing on a healthy endpoint.
        out["control_retried"] = True
        c0 = time.monotonic()
        ctl = _call(base_url, model, api_key=api_key, prompt=CONTROL_PROMPT, max_tokens=16,
                    timeout=CONTROL_TIMEOUT_S)
        ctl_s = time.monotonic() - c0
    out["control_latency_s"] = round(ctl_s, 2)
    if not ctl.get("ok"):
        return _finish(verdict="inconclusive",
                       detail="the endpoint did not answer a plain request in "
                              f"{CONTROL_TIMEOUT_S:.0f}s, so tool calling could not be assessed "
                              "(the parser is NOT implicated): " + str(ctl.get("error"))[:200])

    # Calibrate off what this endpoint actually costs. A tool call generates more than one word,
    # hence the multiplier; the clamps keep a pathologically fast or slow reading sane.
    req_timeout = min(_MAX_REQ_TIMEOUT_S, max(_MIN_REQ_TIMEOUT_S, ctl_s * _LATENCY_MULTIPLIER))
    if budget_s is None:
        budget_s = min(_MAX_BUDGET_S, max(_MIN_BUDGET_S, ctl_s * _BUDGET_MULTIPLIER))
    budget_s += ctl_s                      # the control was not free; do not charge it to the rest
    out["request_timeout_s"] = round(req_timeout, 1)
    out["budget_s"] = round(budget_s, 1)

    leaks: list[str] = []
    answered: list[bool] = []

    def _note(res):
        """Record what a response tells us: a leaked tool format, and whether the endpoint
        answered AT ALL.

        The second half is load-bearing. `_call` converts every exception — including a read
        timeout — into {ok: False, tool_calls: []}, which at the mode level is indistinguishable
        from a clean answer containing no tool call. Untracked, a serve that was merely SLOW earns
        the `model_incapable` verdict, whose own text claims "the endpoint answers normally".

        Measured on Qwen3.6-35B (2026-08-14): the probe ran 62.7s and reported "model does not do
        tool calling"; the same serve then scored 1.0 on the first six tool-using harness tasks.
        The same probe on a faster serve returned OK in 17.9s. The verdict tracked probe LATENCY,
        not model capability."""
        fmt = toolformat.scan_leak(res.get("content") or "")
        if fmt:
            leaks.append(fmt)
        answered.append(bool(res.get("ok")))

    # ---- 1. auto, non-streaming ------------------------------------------------------------
    r1 = _call(base_url, model, api_key=api_key, tools=[PROBE_TOOL], stream=False,
               timeout=min(req_timeout, max(3.0, _left())))
    ok1, why1 = _called_ok(r1)
    _note(r1)
    out["modes"]["nonstream"] = ok1

    # ---- 2. auto, streaming (what the harnesses actually do) --------------------------------
    ok2, why2 = None, ""
    if _left() > 2.0:
        r2 = _call(base_url, model, api_key=api_key, tools=[PROBE_TOOL], stream=True,
                   timeout=min(req_timeout, max(3.0, _left())))
        ok2, why2 = _called_ok(r2)
        _note(r2)
        out["modes"]["stream"] = ok2

    # ---- 3. auto, concurrent (shared parser state) ------------------------------------------
    ok3, why3 = None, ""
    if ok1 and ok2 and _left() > 3.0:
        try:
            with ThreadPoolExecutor(max_workers=_CONCURRENCY) as pool:
                futs = [pool.submit(_call, base_url, model, api_key=api_key, tools=[PROBE_TOOL],
                                    stream=True,
                                    timeout=min(req_timeout, max(3.0, _left())))
                        for _ in range(_CONCURRENCY)]
                results = [f.result() for f in futs]
            checks = [_called_ok(r) for r in results]
            for r in results:
                _note(r)
            ok3 = all(c[0] for c in checks)
            why3 = next((c[1] for c in checks if not c[0]), "all concurrent calls parsed")
        except Exception as e:
            ok3, why3 = None, f"concurrent phase error: {type(e).__name__}"
        out["modes"]["concurrent"] = ok3

    if ok1 and ok2 is not False and ok3 is not False:
        return _finish(verdict="ok",
                       detail="tool calling works in every mode the harnesses use "
                              f"(non-streaming, streaming, x{_CONCURRENCY} concurrent)")

    # ---- 4. the oracle: does REQUIRED work where AUTO did not? ------------------------------
    # tool_choice="required" is served by constrained decoding and needs no parser, so this
    # separates "the server never converted the call" from "the model never made one".
    if _left() > 2.0:
        rr = _call(base_url, model, api_key=api_key, tools=[PROBE_TOOL], tool_choice="required",
                   stream=False, timeout=min(req_timeout, max(3.0, _left())))
        okr, _whyr = _called_ok(rr)
        _note(rr)
        out["required_oracle"] = okr

    leaked = leaks[0] if leaks else None
    out["leaked_format"] = leaked
    if leaked:
        cands = toolformat.parser_candidates(leaked, "vllm")
        out["suggested_parser"] = cands[0] if cands else None
    elif fingerprint and fingerprint.get("candidates"):
        cands = toolformat.parser_candidates(fingerprint["candidates"][0], "vllm")
        out["suggested_parser"] = cands[0] if cands else None

    if out["suggested_parser"]:
        out["remediation"] = ("--tool-call-parser " + out["suggested_parser"]
                              + " --enable-auto-tool-choice")

    first_why = why1 if not ok1 else (why2 if ok2 is False else why3)

    # Model made a call under constraint, or leaked the syntax as text => the SERVER is at fault.
    if out["required_oracle"] or leaked:
        why = ("the model emitted tool-call syntax the server did not convert"
               if leaked else
               "tool_choice=required produced a call while tool_choice=auto did not, which is "
               "the server's parser, not the model")
        return _finish(verdict="parser_fault", detail=why + ". " + str(first_why))

    # "The model cannot do this" requires the model to have SPOKEN. If no probe request ever came
    # back, what was measured is the clock and the network — not tool calling. Saying otherwise
    # tells an operator their model is incapable when their serve was merely slow, and that is the
    # opposite of what this probe exists to do. `inconclusive` gates nothing, exactly like the
    # control-request failure above.
    if not any(answered):
        return _finish(verdict="inconclusive",
                       detail="no probe request returned a usable response within the per-request "
                              "timeout, so tool calling could not be assessed — a slow or "
                              "unreachable serve, NOT evidence that the model lacks tool use. "
                              + str(first_why))

    return _finish(verdict="model_incapable",
                   detail="the endpoint answers normally but never produces or emits a tool call, "
                          "even under tool_choice=required — this looks like a model that was not "
                          "trained for tool use rather than a misconfigured serve. "
                          + str(first_why))


def summarize(res: dict) -> str:
    """Operator-facing lines for the run log. The remediation must be copy-pasteable."""
    v = res.get("verdict")
    modes = res.get("modes") or {}
    shape = " ".join(f"{k}={'ok' if val else ('FAIL' if val is False else '-')}"
                     for k, val in modes.items())
    if v == "ok":
        return f"[pod] tool-calling probe: OK ({shape}, {res.get('elapsed_s')}s)"
    head = {"parser_fault": "WRONG TOOL-CALL PARSER",
            "model_incapable": "model does not do tool calling",
            "inconclusive": "inconclusive (endpoint did not answer)"}.get(v, str(v))
    lines = [f"[pod] tool-calling probe: {head}  ({shape}, {res.get('elapsed_s')}s)",
             f"[pod]   {res.get('detail')}"]
    if res.get("leaked_format"):
        lines.append(f"[pod]   the model emitted {res['leaked_format']}-format tool calls as "
                     f"plain text")
    if res.get("remediation"):
        lines.append(f"[pod]   restart the serve with:  {res['remediation']}")
    if v == "parser_fault":
        lines.append("[pod]   agentic will score near zero until this is fixed; the run continues "
                     "and a harness at or below the failure floor is dropped from the average "
                     "rather than published as the model's ability.")
    return "\n".join(lines)
