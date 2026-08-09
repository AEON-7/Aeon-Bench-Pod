"""The tool-calling probe (`pod.probe_tools`).

Runs green with NO GPU, NO docker, NO network — a fake endpoint stands in for the server, one
per real-world failure mode:

  * the parser is right                        -> ok
  * no parser configured, syntax leaks as text -> parser_fault, and the leak NAMES the fix
  * parser works unstreamed but not streamed   -> parser_fault (this is why phase 2 exists)
  * parser breaks only under concurrency       -> parser_fault (why phase 3 exists)
  * model was never trained for tools          -> model_incapable (a REAL low score, not a fault)
  * endpoint down                              -> inconclusive, and the parser is NOT blamed

The last two matter as much as the failures: a probe that cries "parser" at a weak model would
excuse models from the agentic suite, which is the mirror image of the bug being fixed.
"""
from __future__ import annotations

import json
import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import probe_tools                                        # noqa: E402

BASE, MODEL = "http://127.0.0.1:8000/v1", "model-under-test"


# ---- a fake endpoint --------------------------------------------------------------------------
class FakeServer:
    """Stands in for `_call`. `mode` selects which real failure it reproduces."""

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def __call__(self, base_url, model, *, api_key=None, tools=None, tool_choice=None,
                 stream=False, prompt=probe_tools.PROBE_PROMPT, timeout=None, max_tokens=256):
        self.calls.append({"tools": bool(tools), "tool_choice": tool_choice, "stream": stream,
                           "prompt": prompt})
        if self.mode == "down":
            return {"ok": False, "tool_calls": [], "content": "",
                    "error": "URLError: connection refused"}
        if not tools:                                   # the control request
            return {"ok": True, "tool_calls": [], "content": "READY", "error": None}
        return getattr(self, "_" + self.mode)(tool_choice, stream)

    # a correctly configured server
    @staticmethod
    def _good(tool_choice, stream):
        return {"ok": True, "content": "", "error": None,
                "tool_calls": [{"function": {"name": "get_weather",
                                             "arguments": '{"city": "Paris"}'}}]}

    # no --tool-call-parser: the model's raw syntax comes back as prose
    @staticmethod
    def _no_parser(tool_choice, stream):
        if tool_choice == "required":                   # constrained decoding needs no parser
            return FakeServer._good(tool_choice, stream)
        return {"ok": True, "tool_calls": [], "error": None,
                "content": "<tool_call>\n<function=get_weather>\n<parameter=city>\nParis\n"}

    # parser handles the buffered path but mangles streamed deltas
    @staticmethod
    def _stream_broken(tool_choice, stream):
        if stream:
            return {"ok": True, "tool_calls": [], "error": None,
                    "content": '<tool_call>{"name": "get_weather"}</tool_call>'}
        return FakeServer._good(tool_choice, stream)

    # shared parser state: fine alone, wrong when four land at once
    def _concurrent_broken(self, tool_choice, stream):
        tool_reqs = sum(1 for c in self.calls if c["tools"])
        if tool_reqs > 2:
            return {"ok": True, "tool_calls": [], "error": None,
                    "content": "<tool_call>{\"name\": \"get\""}
        return self._good(tool_choice, stream)

    # a model with no tool training at all — answers, never calls, leaks nothing
    @staticmethod
    def _untrained(tool_choice, stream):
        return {"ok": True, "tool_calls": [], "error": None,
                "content": "I am not able to check the weather, but Paris is usually mild."}

    # partial parse: a call AND leftover markup — the subtle one
    @staticmethod
    def _partial(tool_choice, stream):
        return {"ok": True, "error": None,
                "tool_calls": [{"function": {"name": "get_weather", "arguments": "{}"}}],
                "content": "<tool_call><function=get_weather>"}


def run(mode, **kw):
    fake = FakeServer(mode)
    real = probe_tools._call
    probe_tools._call = fake
    try:
        return probe_tools.probe(BASE, MODEL, **kw), fake
    finally:
        probe_tools._call = real


# ---- verdicts -----------------------------------------------------------------------------------

def test_correct_parser_is_ok():
    res, fake = run("good")
    assert res["verdict"] == "ok", res
    assert res["modes"] == {"nonstream": True, "stream": True, "concurrent": True}, res["modes"]
    # the oracle is a diagnostic — it must NOT be spent when everything already works
    assert res["required_oracle"] is None
    assert not any(c["tool_choice"] == "required" for c in fake.calls)


def test_missing_parser_is_a_parser_fault_and_names_the_fix():
    res, _ = run("no_parser")
    assert res["verdict"] == "parser_fault", res
    assert res["leaked_format"] == "qwen3_xml", res
    assert res["suggested_parser"] == "qwen3_coder", res
    assert res["remediation"] == "--tool-call-parser qwen3_coder --enable-auto-tool-choice"
    assert res["required_oracle"] is True, "required must succeed where auto failed"


def test_streaming_only_break_is_caught():
    """The harnesses stream. A non-streaming-only probe would pass this and manufacture
    confidence, which is worse than no probe at all."""
    res, _ = run("stream_broken")
    assert res["modes"]["nonstream"] is True
    assert res["modes"]["stream"] is False
    assert res["verdict"] == "parser_fault", res


def test_concurrency_only_break_is_caught():
    """run_agentic_v2 runs 4-wide; a parser with shared state fails only there."""
    res, _ = run("concurrent_broken")
    assert res["modes"]["nonstream"] is True and res["modes"]["stream"] is True
    assert res["modes"]["concurrent"] is False, res["modes"]
    assert res["verdict"] == "parser_fault", res


def test_untrained_model_is_not_blamed_on_the_parser():
    """A genuinely non-agentic model must be reported as such: its low agentic score is a REAL
    measurement. Calling this a parser fault would let weak models dodge the suite."""
    res, _ = run("untrained")
    assert res["verdict"] == "model_incapable", res
    assert res["leaked_format"] is None and res["required_oracle"] is False


def test_partial_parse_counts_as_failure():
    """tool_calls populated AND raw markup left in content = the parser consumed part of the
    output. It works on this toy call and breaks on a real transcript."""
    res, _ = run("partial")
    assert res["verdict"] == "parser_fault", res
    assert res["leaked_format"] == "qwen3_xml"


def test_dead_endpoint_never_blames_the_parser():
    res, _ = run("down")
    assert res["verdict"] == "inconclusive", res
    assert res["modes"]["nonstream"] is None, "no tool verdict may be drawn from a dead endpoint"
    assert "parser is NOT implicated" in res["detail"]


def test_fingerprint_supplies_a_suggestion_when_nothing_leaked():
    """A server that swallows the syntax entirely leaks nothing, so the chat-template
    fingerprint is the fallback source for the remediation."""
    res, _ = run("untrained", fingerprint={"candidates": ["glm"]})
    assert res["suggested_parser"] == "glm45", res
    assert res["remediation"] == "--tool-call-parser glm45 --enable-auto-tool-choice"


# ---- contract -----------------------------------------------------------------------------------

def test_probe_never_raises():
    """Same contract as probe_vision/video/audio: a diagnostic must never be able to take down
    the multi-hour run it is protecting. `_call` is written not to raise, so this guards against
    a future refactor that forgets — the probe must degrade to `inconclusive`, which gates
    nothing, rather than propagate."""
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    real = probe_tools._call
    probe_tools._call = boom
    try:
        res = probe_tools.probe(BASE, MODEL)          # must NOT raise
    finally:
        probe_tools._call = real
    assert res["verdict"] == "inconclusive", res
    assert "the probe itself failed" in res["detail"]
    assert "RuntimeError" in res["detail"]
    assert probe_tools.summarize(res)                  # summary must survive it too


def test_budget_is_respected():
    res, _ = run("good", budget_s=5.0)
    assert res["elapsed_s"] <= 5.0


def test_summary_is_actionable():
    res, _ = run("no_parser")
    text = probe_tools.summarize(res)
    assert "WRONG TOOL-CALL PARSER" in text
    assert "--tool-call-parser qwen3_coder --enable-auto-tool-choice" in text
    assert "restart the serve with" in text
    ok = probe_tools.summarize(run("good")[0])
    assert "OK" in ok and "nonstream=ok" in ok


def test_request_shape_is_what_a_harness_sends():
    _res, fake = run("good")
    tool_reqs = [c for c in fake.calls if c["tools"]]
    assert fake.calls[0]["tools"] is False, "control request must carry no tools"
    assert any(c["stream"] for c in tool_reqs), "must exercise streaming"
    assert any(not c["stream"] for c in tool_reqs), "must exercise non-streaming"
    assert sum(1 for c in tool_reqs if c["stream"]) >= probe_tools._CONCURRENCY


# ---- stream reassembly --------------------------------------------------------------------------

def test_sse_deltas_are_stitched_by_index():
    """Tool-call fragments arrive split across chunks and indexed; stitching them wrong is itself
    one of the streaming bugs this probe is meant to catch, so the reassembler is tested."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "get_wea", "arguments": '{"ci'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "ther", "arguments": 'ty": "Paris"}'}}]}}]},
    ]
    raw = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    res = probe_tools._parse_stream(raw)
    assert len(res["tool_calls"]) == 1
    fn = res["tool_calls"][0]["function"]
    assert fn["name"] == "get_weather"
    assert json.loads(fn["arguments"]) == {"city": "Paris"}


def test_sse_survives_junk_lines():
    raw = ("data: not-json\n\n"
           ": a comment\n\n"
           'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
           "data: [DONE]\n\n")
    res = probe_tools._parse_stream(raw)
    assert res["ok"] and res["content"] == "hi" and res["tool_calls"] == []


if __name__ == "__main__":
    import traceback
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception:
                failed.append(name)
                print(f"  FAIL  {name}")
                traceback.print_exc(limit=3)
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "all green"))
    sys.exit(1 if failed else 0)
