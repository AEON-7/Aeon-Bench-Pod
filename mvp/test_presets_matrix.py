"""Family-preset matrix guard: every parser flag a preset carries must name a parser the
engine catalog actually registers (no invented parser names), every family must resolve from
its config.json model_type, family flags must stay hardware-free, and the family ⊕ hardware
composition must emit the EXACT pre-refactor flag set on a DGX Spark (GB10 regression).

    python test_presets_matrix.py
"""
from __future__ import annotations

import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import engines, presets  # noqa: E402


def _catalog_options():
    """{flag: set(options) | None} parsed straight from the vllm FLAG_CATALOG — the ground truth
    a preset's parser flags are checked against (None = a bare boolean flag)."""
    opts = {}
    for row in engines.FLAG_CATALOG["vllm"]:
        opts[row["flag"]] = set(row["options"]) if row.get("kind") == "enum" else None
    return opts


def _pairs(flags):
    out, i = [], 0
    while i < len(flags):
        f = str(flags[i])
        v = str(flags[i + 1]) if i + 1 < len(flags) and not str(flags[i + 1]).startswith("-") else None
        out.append((f, v))
        i += 2 if v is not None else 1
    return out


def test_parser_flags_are_cataloged():
    opts = _catalog_options()
    n = 0
    for p in presets.PRESETS + [presets._GENERIC]:
        for f, v in _pairs(list(p.get("parser_flags") or [])):
            assert f in opts, f"{p['id']}: parser flag {f} is not in the vllm FLAG_CATALOG"
            if opts[f] is not None:
                assert v in opts[f], f"{p['id']}: '{f} {v}' is not a cataloged option ({sorted(opts[f])})"
                n += 1
            else:
                assert v is None, f"{p['id']}: bare flag {f} must not carry a value (got {v})"
    assert n >= 10, f"suspiciously few parser flags checked ({n}) — did parser_flags move?"


# one representative config.json model_type per family — detect() must resolve each
REPRESENTATIVE_MODEL_TYPE = {
    "gemma4": "gemma4",
    "qwen3_5_moe": "qwen3_5_moe",
    "qwen3_5": "qwen3_5",
    "deepseek": "deepseek_v3",
    "glm": "glm4",
    "nemotron": "nemotron_h",
    "kimi": "kimi_k2",
    "stepfun": "step3",
    "mimo": "mimo",
    "llama4": "llama4",
    "llama3": "llama",
    "gpt_oss": "gpt_oss",
    "mistral": "mistral",
    "phi": "phi3",
    "granite": "granite",
    "ernie": "ernie4_5",
    "hunyuan": "hunyuan_v1_moe",
    "seed_oss": "seed_oss",
    "minimax": "minimax_m1",
    "qwq": "qwq",
}


def test_detect_resolves_every_family():
    ids = {p["id"] for p in presets.PRESETS}
    assert set(REPRESENTATIVE_MODEL_TYPE) == ids, (
        f"preset matrix drifted — update REPRESENTATIVE_MODEL_TYPE "
        f"(missing {ids - set(REPRESENTATIVE_MODEL_TYPE)}, "
        f"stale {set(REPRESENTATIVE_MODEL_TYPE) - ids})")
    for pid, mt in REPRESENTATIVE_MODEL_TYPE.items():
        got = presets.detect({"model_type": mt})
        assert got is not None, f"detect returned None for model_type={mt}"
        assert got["id"] == pid, f"model_type={mt} resolved to {got['id']}, expected {pid}"
    # nested text_config model_type (multimodal configs) resolves too
    assert presets.detect({"text_config": {"model_type": "gemma4_text"}})["id"] == "gemma4"
    # unknown families NEVER return None — the generic fallback stands in
    assert presets.detect({"model_type": "totally-unknown-arch"})["id"] == "generic"
    assert presets.detect({})["id"] == "generic"
    assert presets.detect(None, name="")["id"] == "generic"


def test_family_flags_are_hardware_free():
    """De-hardware-ized: no family preset may pin an attention backend (or any other
    host-specific knob) — that's the HARDWARE_PRESETS layer's job."""
    for p in presets.PRESETS + [presets._GENERIC]:
        fam = (list(p.get("safe_flags") or []) + list(p.get("parser_flags") or [])
               + list(p.get("perf_flags") or []) + list(p.get("audio_flags") or []))
        assert "--attention-backend" not in fam, f"{p['id']}: family flags pin an attention backend"
        for tok in fam:
            assert "triton_attn" not in str(tok) and "flashinfer" not in str(tok), \
                f"{p['id']}: hardware-specific token {tok} in family flags"


def test_hardware_preset_selection():
    hw = presets.hardware_preset({"dgx_spark": True, "accel": "cuda"})
    assert hw["id"] == "dgx_spark" and hw["flags"] == ["--attention-backend", "triton_attn"]
    assert presets.hardware_preset({"dgx_spark": False, "accel": "cuda"})["id"] == "cuda_generic"
    assert presets.hardware_preset({"accel": "rocm"})["id"] == "rocm"
    assert presets.hardware_preset({"accel": "metal"})["id"] == "metal"
    assert presets.hardware_preset({"accel": "cpu"})["id"] == "cpu"
    assert presets.hardware_preset({})["id"] == "cpu"          # unknown -> flagless fallback
    # only the Spark preset carries flags today — every other hardware entry must stay empty
    for hid, h in presets.HARDWARE_PRESETS.items():
        if hid != "dgx_spark":
            assert h["flags"] == [], f"hardware preset {hid} grew flags — regression-check GB10"


def test_gb10_regression_exact_flags():
    """The composed family ⊕ hardware output on a DGX Spark must equal the PRE-REFACTOR
    hardcoded flag lists, byte for byte — existing DGX behavior is a contract."""
    gb10 = presets.hardware_preset({"dgx_spark": True, "accel": "cuda"})
    by_id = {p["id"]: p for p in presets.PRESETS}

    # gemma4 (high confidence: safe + attn + parsers), text-only then audio
    got = presets.apply_flags(by_id["gemma4"], ["text"], hardware=gb10)
    assert got == ["--kv-cache-dtype", "auto", "--attention-backend", "triton_attn",
                   "--reasoning-parser", "gemma4", "--tool-call-parser", "gemma4",
                   "--enable-auto-tool-choice"], got
    got_audio = presets.apply_flags(by_id["gemma4"], ["text", "audio"], hardware=gb10)
    assert got_audio == got + ["--limit-mm-per-prompt", '{"image":4,"audio":4}'], got_audio

    # deepseek (medium: safe incl. trust-remote-code + attn + parsers)
    got = presets.apply_flags(by_id["deepseek"], ["text"], hardware=gb10)
    assert got == ["--kv-cache-dtype", "auto", "--trust-remote-code",
                   "--attention-backend", "triton_attn",
                   "--reasoning-parser", "deepseek_r1", "--tool-call-parser", "deepseek_v3",
                   "--enable-auto-tool-choice"], got

    # nemotron (medium, no parsers: safe incl. mamba dtype + attn)
    got = presets.apply_flags(by_id["nemotron"], ["text"], hardware=gb10)
    assert got == ["--kv-cache-dtype", "auto", "--mamba-cache-dtype", "float32",
                   "--trust-remote-code", "--attention-backend", "triton_attn"], got

    # kimi (low confidence: parser flags NOT auto-applied)
    got = presets.apply_flags(by_id["kimi"], ["text"], hardware=gb10)
    assert got == ["--kv-cache-dtype", "auto", "--trust-remote-code",
                   "--attention-backend", "triton_attn"], got

    # generic fallback still emits exactly the GB10 attention pin
    got = presets.apply_flags(presets._GENERIC, ["text"], hardware=gb10)
    assert got == ["--attention-backend", "triton_attn"], got

    # full_flags (GUI chip) for qwen3_5: safe (incl. the spec-decode scheduler budgets restored
    # from the pre-sync pod presets — _QWEN_DFLASH_SCHED) + attn + parsers + fp8 perf override,
    # deduped. NOTE: the composed ORDER differs from the pre-split presets (family sched flags
    # now precede the hardware attention pin instead of following it) — flag order is
    # semantically irrelevant to the engine; the values are the contract.
    got = presets.full_flags(by_id["qwen3_5"], ["text"], hardware=gb10)
    assert got == ["--kv-cache-dtype", "fp8_e4m3",
                   "--max-num-seqs", "64", "--max-num-batched-tokens", "32768",
                   "--enable-chunked-prefill", "--generation-config", "vllm",
                   "--attention-backend", "triton_attn",
                   "--reasoning-parser", "qwen3", "--tool-call-parser", "qwen3_coder",
                   "--enable-auto-tool-choice"], got


def test_qwen_sched_flags_restored():
    """The pre-sync pod presets carried _QWEN_DFLASH_SCHED (spec-decode scheduler budgets) on
    BOTH Qwen families' safe flags — restored here at the FAMILY layer (they're model-intrinsic
    scheduling behavior, not a host pin), so every host composes them by default, exactly the
    families the pre-split file gave them to (and no others)."""
    sched = ["--max-num-seqs", "64", "--max-num-batched-tokens", "32768",
             "--enable-chunked-prefill", "--generation-config", "vllm"]
    assert presets._QWEN_DFLASH_SCHED == sched, presets._QWEN_DFLASH_SCHED
    by_id = {p["id"]: p for p in presets.PRESETS}
    for pid in ("qwen3_5_moe", "qwen3_5"):
        got = presets.apply_flags(by_id[pid], ["text"],
                                  hardware=presets.hardware_preset({"accel": "cuda"}))
        assert got[:2] + got[2:2 + len(sched)] == ["--kv-cache-dtype", "auto"] + sched, (pid, got)
    for p in presets.PRESETS:
        if p["id"] in ("qwen3_5_moe", "qwen3_5"):
            continue
        assert "--max-num-batched-tokens" not in (p.get("safe_flags") or []), \
            f"{p['id']}: sched budgets leaked beyond the Qwen families"


def test_non_gb10_hosts_drop_the_attention_pin():
    cuda = presets.hardware_preset({"dgx_spark": False, "accel": "cuda"})
    by_id = {p["id"]: p for p in presets.PRESETS}
    got = presets.apply_flags(by_id["gemma4"], ["text"], hardware=cuda)
    assert "--attention-backend" not in got, got
    assert got == ["--kv-cache-dtype", "auto", "--reasoning-parser", "gemma4",
                   "--tool-call-parser", "gemma4", "--enable-auto-tool-choice"], got


def test_summary_payload_shape():
    gb10 = presets.hardware_preset({"dgx_spark": True, "accel": "cuda"})
    s = presets.summary(presets.detect({"model_type": "gemma4"}), ["text"], hardware=gb10)
    assert s["id"] == "gemma4" and s["hardware"] == {"id": "dgx_spark", "label": "DGX Spark (GB10)"}
    assert "--attention-backend" in s["flags"]


# ---- chat-template fingerprint: fills a gap, never contradicts --------------------------------
# The fingerprint reads the tool-call wire format out of the model's own chat template. It is
# only allowed to touch the serve path because it cannot alter a preset that already names a
# parser — a wrong --tool-call-parser is a crashed serve or a silent near-zero agentic score, so
# "it cannot regress" has to be enforced, not asserted in prose.

_HERMES_TPL = ("{%- for m in messages %}{%- if m.tool_calls %}{{- '<tool_call>' }}"
               "{{- m.tool_calls[0]|tojson }}{{- '</tool_call>' }}{%- endif %}{%- endfor %}")
_GLM_TPL = ("{%- for m in messages %}{%- if m.tool_calls %}"
            "{{- '<tool_call>' + m.tool_calls[0].function.name }}"
            "{{- '<arg_key>city</arg_key>' }}{%- endif %}{%- endfor %}")
_CHAT_ONLY_TPL = ("{%- for m in messages %}{{- '<|im_start|>' + m.role + m.content }}"
                  "{%- endfor %}")
_UNKNOWN_FMT_TPL = ("{%- for m in messages %}{%- if m.tool_calls %}"
                    "{{- '<<<CALL:' + m.tool_calls[0].function.name + '>>>' }}"
                    "{%- endif %}{%- endfor %}")


def _fingerprint_for(template):
    import shutil, tempfile
    from pod import toolformat
    d = tempfile.mkdtemp(prefix="tplfp_")
    try:
        with open(os.path.join(d, "chat_template.jinja"), "w", encoding="utf-8") as f:
            f.write(template)
        return toolformat.detect(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _parser_in(flags):
    return flags[flags.index("--tool-call-parser") + 1] if "--tool-call-parser" in flags else None


def _flags_for(cfg, template=None):
    hw = presets.HARDWARE_PRESETS["dgx_spark"]
    p = presets.detect(cfg, "x")
    fp = _fingerprint_for(template) if template else None
    return p, presets.apply_flags(p, [], hardware=hw, fingerprint=fp)


def test_fingerprint_never_alters_an_existing_parser():
    """THE safety property. Every family that already names a tool-call parser must produce
    byte-identical flags with the fingerprint active — even when the template disagrees."""
    hw = presets.HARDWARE_PRESETS["dgx_spark"]
    checked = 0
    for p in presets.PRESETS:
        before = presets.apply_flags(p, [], hardware=hw, fingerprint=None)
        if "--tool-call-parser" not in before:
            continue                      # gap cases are covered separately
        checked += 1
        for tpl in (_HERMES_TPL, _GLM_TPL, _CHAT_ONLY_TPL, _UNKNOWN_FMT_TPL):
            after = presets.apply_flags(p, [], hardware=hw, fingerprint=_fingerprint_for(tpl))
            assert after == before, (p["id"], before, after)
    assert checked >= 8, f"expected to exercise the parser-bearing families, got {checked}"


def test_gemma4_keeps_its_parser_against_a_contradicting_template():
    """Named case: the preset says gemma4, the template looks like hermes. The preset wins."""
    _p, flags = _flags_for({"model_type": "gemma4",
                            "architectures": ["Gemma4ForCausalLM"]}, _HERMES_TPL)
    assert _parser_in(flags) == "gemma4", flags


def test_fingerprint_fills_a_gap_for_an_unknown_family():
    """The benefit: a family with no preset serves with NO parser today, which means tool calls
    are never converted and every agentic task scores ~0."""
    p, before = _flags_for({"model_type": "zzz_novel", "architectures": ["ZzzForCausalLM"]})
    assert p["id"] == "generic" and _parser_in(before) is None
    _p, after = _flags_for({"model_type": "zzz_novel", "architectures": ["ZzzForCausalLM"]},
                           _HERMES_TPL)
    assert _parser_in(after) == "hermes", after


def test_fingerprint_fills_a_gap_for_a_low_confidence_family():
    """Low-confidence presets withhold their parser flags by design; the template is independent
    evidence, so it may fill that gap."""
    _p, after = _flags_for({"model_type": "kimi_k2", "architectures": ["KimiForCausalLM"]},
                           _GLM_TPL)
    assert _parser_in(after) == "glm45", after


def test_unrecognised_templates_never_produce_a_guess():
    """A chat-only template and an unknown wire format must both yield nothing. Guessing a parser
    is how a working serve breaks; `unrecognised` is a probe-it verdict, not a fill-it one."""
    for tpl in (_CHAT_ONLY_TPL, _UNKNOWN_FMT_TPL):
        _p, flags = _flags_for({"model_type": "zzz_novel"}, tpl)
        assert _parser_in(flags) is None, (tpl[:40], flags)


def test_kill_switch_disables_the_fill():
    prev = os.environ.get("AEON_TOOLFORMAT")
    os.environ["AEON_TOOLFORMAT"] = "0"
    try:
        _p, flags = _flags_for({"model_type": "zzz_novel"}, _HERMES_TPL)
        assert _parser_in(flags) is None, flags
    finally:
        if prev is None:
            os.environ.pop("AEON_TOOLFORMAT", None)
        else:
            os.environ["AEON_TOOLFORMAT"] = prev


def test_filled_parser_is_one_the_engine_registers():
    """An unregistered name does not degrade — it kills the serve at startup."""
    from pod import toolformat
    for tpl in (_HERMES_TPL, _GLM_TPL):
        _p, flags = _flags_for({"model_type": "zzz_novel"}, tpl)
        name = _parser_in(flags)
        if name:
            assert name in toolformat.VLLM_TOOL_PARSERS, name
            assert "--enable-auto-tool-choice" in flags, "a parser without auto-tool-choice is a no-op"


def main():
    test_parser_flags_are_cataloged()
    test_detect_resolves_every_family()
    test_family_flags_are_hardware_free()
    test_hardware_preset_selection()
    test_gb10_regression_exact_flags()
    test_qwen_sched_flags_restored()
    test_non_gb10_hosts_drop_the_attention_pin()
    test_summary_payload_shape()
    test_fingerprint_never_alters_an_existing_parser()
    test_gemma4_keeps_its_parser_against_a_contradicting_template()
    test_fingerprint_fills_a_gap_for_an_unknown_family()
    test_fingerprint_fills_a_gap_for_a_low_confidence_family()
    test_unrecognised_templates_never_produce_a_guess()
    test_kill_switch_disables_the_fill()
    test_filled_parser_is_one_the_engine_registers()
    print("OK  preset matrix: fingerprint fills gaps but never contradicts, parsers cataloged, all families detected, "
          "family flags hardware-free, GB10 composition regression-exact, "
          "Qwen spec-decode scheduler budgets restored")


if __name__ == "__main__":
    main()
