"""Work out which tool-call WIRE FORMAT a checkpoint speaks, from the checkpoint itself.

Why this exists
---------------
For an agent harness to work, the serving engine has to turn the model's tool calls into OpenAI
`tool_calls`. That needs the right `--tool-call-parser`, and picking it wrong is the worst kind of
wrong: the model emits perfectly good tool calls, the server fails to parse them, the harness sees
no tool use, and the run produces a low score that looks exactly like a model that cannot do
agentic work. Agentic is 30% of the AEON score, so that silently corrupts the headline number.

Until now the parser came from a hand-maintained family table keyed on `model_type` /
architecture / repo name. That approach cannot keep up, and not merely because the field moves:

  * Nemotron-3 emits QWEN's format. Kimi K2 has its own. GLM-5.2 wants a parser whose name matches
    no model name. The right parser is NOT a function of the vendor.
  * A fine-tune, merge, or community quant carries an arbitrary repo name and often an unchanged
    `model_type`, so name/type matching says nothing about what it emits.
  * A genuinely new architecture matches nothing and gets NO parser at all.

So key on the thing that actually determines the answer: the chat template. It is the file that
defines the tokens the model was trained to emit for a tool call, it ships inside the checkpoint,
and the pod already downloads and hashes it for attestation. Same evidence the model itself used.

The one real trap
-----------------
You cannot grep the template for `tool_call`. Jinja FIELD ACCESS is textually identical to a WIRE
TOKEN: `message['tool_calls']` is the template reading a field, `'<tool_call>'` is the template
emitting markup. A raw substring search hits the real Gemma-4 template several times and would
confidently label it `hermes` — a completely different format.

Only what the template EMITS is the format. So we extract string literals from Jinja OUTPUT
expressions (`{{ ... }}`) and from raw text between tags, and ignore everything inside statements
(`{% ... %}`) and every bare identifier. `_emitted_literals` is the whole trick, and the reason
SIGNATURES below is keyed on wire format rather than on family.

Nothing here talks to the network, a GPU, or docker: it is a file read and a regex pass, so it runs
in microseconds on any pod, before anything is served.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

# ---- reading the template out of a checkpoint -------------------------------------------------
# Three locations, all in active use: Gemma-4 ships the standalone .jinja, Qwen3.6 embeds it in
# tokenizer_config.json, some repos use chat_template.json. DeepSeek V3.2/V4 ship NONE, and that
# absence is itself a signal rather than an error.
TEMPLATE_FILES = ("chat_template.jinja", "chat_template.json", "tokenizer_config.json")


def read_template(model_dir: str) -> tuple[str | None, str | None]:
    """(template_text, source_filename) for a checkpoint on disk. (None, None) when it has none."""
    for fn in TEMPLATE_FILES:
        path = os.path.join(model_dir, fn)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
        except OSError:
            continue
        if fn.endswith(".jinja"):
            if raw.strip():
                return raw, fn
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            continue
        tpl = doc.get("chat_template") if isinstance(doc, dict) else None
        # chat_template.json occasionally holds a list of named templates
        if isinstance(tpl, list):
            tpl = next((t.get("template") for t in tpl
                        if isinstance(t, dict) and t.get("name") in (None, "default")), None) \
                or next((t.get("template") for t in tpl if isinstance(t, dict)), None)
        if isinstance(tpl, str) and tpl.strip():
            return tpl, fn
    return None, None


def template_sha256(template: str | None) -> str | None:
    return hashlib.sha256(template.encode("utf-8")).hexdigest() if template else None


# ---- the trick: what does the template actually EMIT? -----------------------------------------
_STMT_RE = re.compile(r"\{%-?.*?-?%\}", re.S)        # {% if ... %}  -> logic, never output
_COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
_OUTPUT_RE = re.compile(r"\{\{-?(.*?)-?\}\}", re.S)  # {{ '<tool_call>' }} -> output
_STRLIT_RE = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'|\"([^\"\\]*(?:\\.[^\"\\]*)*)\"")
# a literal only counts as a wire token if it carries markup — plain words are prose, and
# `tool_calls` on its own is a field name, not something the model types.
_MARKUP_RE = re.compile(r"[<\[|]")


def emitted_literals(template: str) -> set[str]:
    """Every string the template can actually EMIT that looks like markup.

    Two sources, both genuine output: string literals inside `{{ ... }}` expressions, and the raw
    text that sits between Jinja tags. Statements and comments are stripped first — that is what
    stops `{%- if message['tool_calls'] %}` from being read as a wire token."""
    if not template:
        return set()
    out: set[str] = set()

    for expr in _OUTPUT_RE.findall(template):
        for a, b in _STRLIT_RE.findall(expr):
            lit = (a or b).strip()
            if lit and _MARKUP_RE.search(lit):
                out.add(lit)

    # Raw text between tags is emitted verbatim too. The token shapes below are deliberately
    # generous about pipe placement: Gemma-4 uses `<|tool_call>` … `<tool_call|>` — pipes on
    # opposite sides — so a strict `<|…|>` pattern silently drops the only marker that identifies
    # the family, and a strict `<…>` pattern shreds it into fragments.
    literal_text = _OUTPUT_RE.sub(" ", _STMT_RE.sub(" ", _COMMENT_RE.sub(" ", template)))
    for tok in re.findall(r"<\|?[A-Za-z_▁][\w.▁-]*\|?>|\[[A-Z_]{3,}\]|"
                          r"<｜[^｜>]+｜>", literal_text):
        out.add(tok.strip())
    return out


# ---- the engine's real parser set --------------------------------------------------------------
# Read out of vLLM 0.26's `_TOOL_PARSERS_TO_REGISTER` on the rig, not copied from documentation.
# Held as data so a name can be checked BEFORE it reaches a command line: an unregistered parser
# name does not degrade, it kills the serve at startup.
VLLM_TOOL_PARSERS = (
    "apertus", "cohere_command3", "cohere_command4", "deepseek_v3", "deepseek_v31", "deepseek_v32",
    "deepseek_v4", "ernie45", "functiongemma", "gemma4", "gigachat3", "glm45", "glm47", "granite",
    "granite-20b-fc", "granite4", "hermes", "hunyuan_a13b", "hy_v3", "inkling", "internlm", "jamba",
    "kimi_k2", "lfm2", "llama3_json", "llama4_json", "llama4_pythonic", "longcat", "mimo",
    "minicpm5", "minimax_m2", "minimax_m3", "mistral", "olmo3", "openai", "phi4_mini_json",
    "poolside_v1", "pythonic", "qwen3_coder", "qwen3_xml", "seed_oss", "step3", "step3p5", "xlam",
)
VLLM_REASONING_PARSERS = (
    "cohere_command3", "cohere_command4", "deepseek_r1", "deepseek_v3", "deepseek_v4", "ernie45",
    "gemma4", "glm45", "glm47", "granite", "holo2", "hunyuan_a13b", "hy_v3", "inkling", "kimi_k2",
    "mimo", "minimax_m2", "minimax_m2_append_think", "minimax_m3", "mistral", "nemotron_v3",
    "olmo3", "openai_gptoss", "qwen3", "poolside_v1", "seed_oss", "step3", "step3p5",
)

# ---- wire formats -----------------------------------------------------------------------------
# Keyed on FORMAT, never on family - that is the whole point. `require` is what the format's tokens
# look like; `forbid` disambiguates formats that share a token.
#
# `vllm` / `sglang` are ORDERED CANDIDATE LISTS, not single names, because one wire format can have
# several plausible parsers: vLLM ships both qwen3_coder and qwen3_xml for the XML family, and both
# glm45 and glm47. The template cannot tell those apart, and guessing is exactly the failure this
# module exists to end - so the fingerprint proposes an ORDER and the endpoint probe adjudicates.
#
# Order within the list matters and so does order of the list itself: `hermes` is LAST because
# `<tool_call>` alone is the least specific marker, and several richer formats also emit it.
SIGNATURES = [
    {"id": "gemma4", "label": "Gemma-4 pipe-delimited call",
     "require": ["<|tool_call>"], "forbid": [],
     "vllm": ["gemma4", "functiongemma"], "sglang": [],
     "note": "<|tool_call>call:name{args}<tool_call|> - note the ASYMMETRIC pipes. It contains "
             "neither `<tool_call>` nor `<|tool_call|>`, so naive token matching misses it AND "
             "a substring grep for `tool_call` mislabels it hermes."},
    {"id": "qwen3_xml", "label": "Qwen3-Coder XML tool blocks",
     "require": ["<tool_call>", "<function="], "forbid": [],
     "vllm": ["qwen3_coder", "qwen3_xml"], "sglang": ["qwen25"],
     "note": "<tool_call><function=name><parameter=k>v. Also what Nemotron-3 emits - which is "
             "exactly why this table is not keyed on vendor."},
    {"id": "glm", "label": "GLM 4.5+ / 5.x",
     "require": ["<tool_call>", "<arg_key>"], "forbid": [],
     "vllm": ["glm45", "glm47"], "sglang": ["glm"],
     "note": "<tool_call>name<arg_key>k</arg_key><arg_value>v. Distinguished from hermes by "
             "<arg_key>, so a GLM point release lands here with no new table entry."},
    {"id": "deepseek_v3", "label": "DeepSeek V3 line tool-calls block",
     "require": ["<\uff5ctool\u2581calls\u2581begin\uff5c>"], "forbid": [],
     "vllm": ["deepseek_v3", "deepseek_v31", "deepseek_v32", "deepseek_v4"],
     "sglang": ["deepseekv3"],
     "note": "Unicode-delimited begin/end markers, shared across the V3 line; each point release "
             "has its own parser, so the probe resolves which."},
    {"id": "mistral", "label": "Mistral [TOOL_CALLS]",
     "require": ["[TOOL_CALLS]"], "forbid": [],
     "vllm": ["mistral"], "sglang": ["mistral"], "note": "[TOOL_CALLS] then a JSON array."},
    {"id": "llama_json", "label": "Llama 3.1+ / 4 JSON",
     "require": ["<|python_tag|>"], "forbid": [],
     "vllm": ["llama3_json", "llama4_json", "llama4_pythonic"], "sglang": ["llama3"],
     "note": "<|python_tag|> then a JSON object."},
    {"id": "kimi_k2", "label": "Kimi K2",
     "require": ["<|tool_calls_section_begin|>"], "forbid": [],
     "vllm": ["kimi_k2"], "sglang": [], "note": "Moonshot's own section markers."},
    {"id": "internlm", "label": "InternLM action block",
     "require": ["<|action_start|>"], "forbid": [],
     "vllm": ["internlm"], "sglang": [], "note": "<|action_start|><|plugin|>."},
    {"id": "phi4_json", "label": "Phi-4 functools",
     "require": ["functools["], "forbid": [],
     "vllm": ["phi4_mini_json"], "sglang": [], "note": "functools[{...}] prefix."},
    {"id": "hermes", "label": "Hermes / Qwen2.5 JSON-in-tags",
     "require": ["<tool_call>"], "forbid": ["<function=", "<arg_key>"],
     "vllm": ["hermes"], "sglang": ["qwen25"],
     "note": "<tool_call>{json}</tool_call>. The most common format and the least specific - it "
             "is deliberately last."},
]

# Engines that take no tool-call parser flags at all. Passing vLLM grammar to these is not merely
# useless, it is a live bug: the flags get appended to argv the engine does not define.
NO_PARSER_ENGINES = ("llamacpp", "llama.cpp", "llama_cpp", "mlx", "ollama")


def match_signature(literals: set[str]) -> list[dict]:
    """Wire formats consistent with these emitted literals, most specific first.

    Specificity = number of required tokens matched, so GLM (which also emits <tool_call>) beats
    the generic hermes match rather than tying with it."""
    hits = []
    for sig in SIGNATURES:
        if not all(any(req in lit for lit in literals) for req in sig["require"]):
            continue
        if any(any(bad in lit for lit in literals) for bad in sig["forbid"]):
            continue
        hits.append(sig)
    return sorted(hits, key=lambda s: -len(s["require"]))


def detect(model_dir: str) -> dict:
    """Everything we can say about a checkpoint's tool-call format without serving it.

    Returns {status, candidates, literals, template_sha256, template_source, note} where status is
    one of:
      matched        - one or more wire formats recognised (candidates[0] is the best guess)
      unrecognised   - a template exists but emits no tool markup we know; it may still support
                       tools in a format we have not catalogued, so this is a "probe it" verdict,
                       never a "give up" one
      no_template    - the checkpoint ships no chat template at all (DeepSeek V3.2/V4 do this)
      absent         - no such directory / nothing readable
    """
    if not model_dir or not os.path.isdir(model_dir):
        return {"status": "absent", "candidates": [], "literals": [],
                "template_sha256": None, "template_source": None,
                "note": "no model directory to inspect"}
    template, source = read_template(model_dir)
    if not template:
        return {"status": "no_template", "candidates": [], "literals": [],
                "template_sha256": None, "template_source": None,
                "note": "checkpoint ships no chat template; the engine supplies one, so the "
                        "served format cannot be read from the artifacts - probe the endpoint"}
    lits = emitted_literals(template)
    cands = match_signature(lits)
    return {
        "status": "matched" if cands else "unrecognised",
        "candidates": [c["id"] for c in cands],
        "literals": sorted(lits)[:40],
        "template_sha256": template_sha256(template),
        "template_source": source,
        "note": cands[0]["note"] if cands else
                "template emits no tool markup this table knows; either the model has no tool "
                "training or the format is new - probe before trusting a low agentic score",
    }


def parser_candidates(signature_id: str | None, engine_style: str = "vllm") -> list[str]:
    """Parser names to try for a wire format on this engine, best first; [] if none apply.

    Engines spell the same format differently (vLLM `deepseek_v3` vs SGLang `deepseekv3`), and
    llama.cpp / MLX take no such flag at all — handing them vLLM grammar appends arguments the
    engine does not define, which is a live bug this replaces."""
    style = (engine_style or "vllm").lower()
    if any(k in style for k in NO_PARSER_ENGINES):
        return []
    sig = next((s for s in SIGNATURES if s["id"] == signature_id), None)
    if not sig:
        return []
    names = sig.get("sglang") if "sglang" in style else sig.get("vllm")
    names = list(names or [])
    # Only offer names this engine actually registers — an unknown one does not degrade
    # gracefully, it kills the serve at startup.
    if "sglang" not in style:
        names = [n for n in names if n in VLLM_TOOL_PARSERS]
    return names


def to_flags(signature_id: str | None, engine_style: str = "vllm",
             parser: str | None = None) -> list[str]:
    """Serve flags for a wire format, using `parser` when one has already been chosen (by the
    probe, the ladder, or the learned cache) and the best candidate otherwise. [] when the engine
    takes no such flag."""
    style = (engine_style or "vllm").lower()
    if any(k in style for k in NO_PARSER_ENGINES):
        return []
    name = parser or next(iter(parser_candidates(signature_id, engine_style)), None)
    if not name:
        return []
    return ["--tool-call-parser", name, "--enable-auto-tool-choice"]


# The raw syntax a model leaks into `message.content` when the server did NOT parse it. This is
# better evidence than the template: it is what the model actually emitted, on this serve, right
# now. Used to explain a failed tool probe and to name the parser that should have caught it.
LEAK_SENTINELS = [
    ("<arg_key>", "glm"),
    ("<function=", "qwen3_xml"),
    ("<\uff5ctool\u2581calls\u2581begin\uff5c>", "deepseek_v3"),
    ("<|tool_calls_section_begin|>", "kimi_k2"),
    ("<|python_tag|>", "llama_json"),
    ("[TOOL_CALLS]", "mistral"),
    ("<|action_start|>", "internlm"),
    ("functools[", "phi4_json"),
    ("<tool_call>", "hermes"),          # last: the least specific marker
]


def scan_leak(text: str) -> str | None:
    """The wire format a model leaked as plain text, or None. Order matters — the specific
    markers are checked before `<tool_call>`, which several formats also contain."""
    if not text:
        return None
    for token, sig_id in LEAK_SENTINELS:
        if token in text:
            return sig_id
    return None
