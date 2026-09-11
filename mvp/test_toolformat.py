"""Tool-call wire-format fingerprinting (`pod.toolformat`).

Runs green with NO GPU, NO docker, NO network — it is a file read and a regex pass.

The fixtures below are reduced from REAL chat templates (Gemma-4, Qwen3.6/Ornith) plus the
documented markers of the other formats. The Gemma-4 case is the important one and is a
regression test, not an example: its template mentions `tool_call` seven times in Jinja FIELD
ACCESS (`message['tool_calls']`) while emitting a completely different wire token, so any
implementation that substring-greps the template file misclassifies it as `hermes`.
"""
from __future__ import annotations

import os
import sys
import tempfile

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import toolformat as tf                                   # noqa: E402

# ---- fixtures ---------------------------------------------------------------------------------
# Reduced from the real Gemma-4 template: field access everywhere, one asymmetric wire token.
GEMMA4 = """{%- for message in messages -%}
    {{- '<|turn>' + message['role'] + '\\n' }}
    {%- if message['tool_calls'] -%}
        {%- for tool_call in message['tool_calls'] -%}
            {%- set function = tool_call['function'] -%}
            {{- '<|tool_call>call:' + function['name'] + '{' -}}
            {{- function['arguments'] -}}
            {{- '}<tool_call|>' -}}
        {%- endfor -%}
    {%- endif -%}
{%- endfor -%}"""

QWEN3_XML = """{%- for message in messages %}
    {%- if message.tool_calls %}
        {%- for tool_call in message.tool_calls %}
            {{- '\\n<tool_call>\\n<function=' + tool_call.function.name + '>\\n' }}
            {%- for k, v in tool_call.function.arguments|items %}
                {{- '<parameter=' + k + '>\\n' + v + '\\n</parameter>\\n' }}
            {%- endfor %}
            {{- '</function>\\n</tool_call>' }}
        {%- endfor %}
    {%- endif %}
{%- endfor %}"""

HERMES = """{%- for message in messages %}
    {%- if message.tool_calls %}
        {%- for tool_call in message.tool_calls %}
            {{- '<tool_call>\\n' }}{{- tool_call|tojson }}{{- '\\n</tool_call>' }}
        {%- endfor %}
    {%- endif %}
{%- endfor %}"""

GLM = """{%- for message in messages %}
    {%- if message.tool_calls %}
        {%- for tc in message.tool_calls %}
            {{- '<tool_call>' + tc.function.name }}
            {%- for k, v in tc.function.arguments|items %}
                {{- '<arg_key>' + k + '</arg_key><arg_value>' + v + '</arg_value>' }}
            {%- endfor %}
            {{- '</tool_call>' }}
        {%- endfor %}
    {%- endif %}
{%- endfor %}"""

MISTRAL = """{%- if message.tool_calls %}{{- '[TOOL_CALLS]' + message.tool_calls|tojson }}{%- endif %}"""
LLAMA = """{%- if message.tool_calls %}{{- '<|python_tag|>' + message.tool_calls[0]|tojson }}{%- endif %}"""
NO_TOOLS = """{%- for m in messages %}{{- '<|im_start|>' + m.role + '\\n' + m.content }}{%- endfor %}"""


def _dir(template: str, filename: str = "chat_template.jinja") -> str:
    d = tempfile.mkdtemp(prefix="tf_")
    with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
        f.write(template)
    return d


def _detect(template: str) -> dict:
    return tf.detect(_dir(template))


# ---- the trap ---------------------------------------------------------------------------------

def test_gemma4_is_not_hermes():
    """THE regression test. Gemma-4's template says `tool_call` many times as FIELD ACCESS while
    emitting `<|tool_call>` / `<tool_call|>` — pipes on opposite sides. A substring grep sees
    `tool_call` and says hermes, which is a different format entirely."""
    assert GEMMA4.count("tool_call") >= 5, "fixture must retain the field-access decoys"
    r = _detect(GEMMA4)
    assert r["status"] == "matched"
    assert r["candidates"][0] == "gemma4", r
    assert "hermes" not in r["candidates"]
    # and the emitted token is genuinely NOT the hermes one
    lits = tf.emitted_literals(GEMMA4)
    assert not any(lit == "<tool_call>" for lit in lits), lits


def test_field_access_alone_never_matches():
    """A template that only READS tool_calls and emits no tool markup must not match a format."""
    only_reads = ("{%- for m in messages %}{%- if m['tool_calls'] %}"
                  "{{- m['tool_calls'][0]['function']['name'] }}{%- endif %}{%- endfor %}")
    assert _detect(only_reads)["candidates"] == []


# ---- formats ----------------------------------------------------------------------------------

def test_qwen3_xml_beats_hermes():
    """Both emit <tool_call>; the XML form is more specific and must win."""
    r = _detect(QWEN3_XML)
    assert r["candidates"][0] == "qwen3_xml", r
    assert tf.to_flags("qwen3_xml") == ["--tool-call-parser", "qwen3_coder",
                                        "--enable-auto-tool-choice"]


def test_glm_beats_hermes():
    r = _detect(GLM)
    assert r["candidates"][0] == "glm", r


def test_plain_hermes_still_matches():
    r = _detect(HERMES)
    assert r["candidates"] == ["hermes"], r


def test_other_formats():
    assert _detect(MISTRAL)["candidates"][0] == "mistral"
    assert _detect(LLAMA)["candidates"][0] == "llama_json"


def test_no_tool_support_is_unrecognised_not_a_guess():
    """A chat-only template must NOT be handed a parser. Guessing one is how a working model gets
    a broken serve; `unrecognised` is a probe-it verdict, not a give-up one."""
    r = _detect(NO_TOOLS)
    assert r["status"] == "unrecognised" and r["candidates"] == []
    assert tf.to_flags(None) == []


# ---- template location + absence ----------------------------------------------------------------

def test_template_read_from_tokenizer_config():
    """Qwen3.6 embeds the template in tokenizer_config.json rather than shipping the .jinja."""
    import json
    d = tempfile.mkdtemp(prefix="tf_")
    with open(os.path.join(d, "tokenizer_config.json"), "w", encoding="utf-8") as f:
        json.dump({"chat_template": QWEN3_XML}, f)
    r = tf.detect(d)
    assert r["candidates"][0] == "qwen3_xml"
    assert r["template_source"] == "tokenizer_config.json"


def test_missing_template_is_a_signal_not_an_error():
    """DeepSeek V3.2/V4 ship no chat template at all. That must be reported, not raised."""
    r = tf.detect(tempfile.mkdtemp(prefix="tf_"))
    assert r["status"] == "no_template" and r["candidates"] == []
    assert "probe" in r["note"]
    assert tf.detect("/nonexistent/path/xyz")["status"] == "absent"


def test_sha256_is_stable_and_distinguishing():
    a, b = _dir(QWEN3_XML), _dir(GEMMA4)
    assert tf.detect(a)["template_sha256"] == tf.detect(_dir(QWEN3_XML))["template_sha256"]
    assert tf.detect(a)["template_sha256"] != tf.detect(b)["template_sha256"]


# ---- engine translation -------------------------------------------------------------------------

def test_llamacpp_and_mlx_get_no_parser_flags():
    """Live bug this replaces: vLLM grammar was prepended for EVERY engine, so llama.cpp argv got
    flags it does not define."""
    for engine in ("llamacpp", "llama.cpp", "mlx", "ollama"):
        assert tf.to_flags("qwen3_xml", engine) == [], engine
        assert tf.parser_candidates("qwen3_xml", engine) == [], engine


def test_sglang_uses_its_own_spelling():
    assert tf.to_flags("deepseek_v3", "sglang")[1] == "deepseekv3"
    assert tf.to_flags("deepseek_v3", "vllm")[1] == "deepseek_v3"


def test_every_candidate_name_is_actually_registered():
    """A parser name the engine does not register does not degrade — it kills the serve at
    startup. Every vLLM name this module can emit must exist in the real registry."""
    for sig in tf.SIGNATURES:
        for name in sig["vllm"]:
            assert name in tf.VLLM_TOOL_PARSERS, f"{sig['id']} -> unregistered {name!r}"


def test_candidates_are_ordered_not_singular():
    """The template cannot tell qwen3_coder from qwen3_xml, or glm45 from glm47. Offering an
    ordered list is what lets the probe adjudicate instead of us guessing."""
    assert tf.parser_candidates("qwen3_xml") == ["qwen3_coder", "qwen3_xml"]
    assert tf.parser_candidates("glm") == ["glm45", "glm47"]
    assert tf.to_flags("glm", "vllm", parser="glm47")[1] == "glm47"


# ---- leak detection -----------------------------------------------------------------------------

def test_scan_leak_names_the_parser_that_should_have_caught_it():
    """When the server does not parse a tool call, the raw syntax lands in message.content. That
    leak is better evidence than the template: it is what the model actually emitted."""
    assert tf.scan_leak("sure!\n<tool_call>\n<function=get_weather>") == "qwen3_xml"
    assert tf.scan_leak("<tool_call>x<arg_key>city</arg_key>") == "glm"
    assert tf.scan_leak("<tool_call>{\"name\": \"f\"}</tool_call>") == "hermes"
    assert tf.scan_leak("[TOOL_CALLS][{\"name\":\"f\"}]") == "mistral"
    assert tf.scan_leak("the weather is fine") is None
    assert tf.scan_leak("") is None


def test_scan_leak_prefers_the_specific_marker():
    """Several formats also contain <tool_call>; the specific marker must win."""
    assert tf.scan_leak("<tool_call><function=f><arg_key>k") in ("glm", "qwen3_xml")
    assert tf.scan_leak("<tool_call><arg_key>k</arg_key>") == "glm"


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
    print(f"\n{'FAILED: ' + ', '.join(failed) if failed else 'all green'}")
    sys.exit(1 if failed else 0)
