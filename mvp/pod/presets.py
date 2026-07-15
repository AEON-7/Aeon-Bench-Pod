"""pod/presets.py — model-family best-practice serve recipes.

A "smart starting point" per model family/architecture: the serve flags, reasoning + tool
parsers, and capability mapping that a family needs to bench WELL, derived from AEON field
experience on the GB10. Detection keys on config.json `model_type` (most robust — the
architecture string and repo name are fallbacks), so a family is recognized no matter how the
repo is named.

Two ways it's used:
  1. `detect(config, name)` → the matched preset. The Run tab surfaces it as an "apply
     best-practice preset" chip that FILLS Recipe Tuning (editable — never silent), so the
     operator sees exactly what will be served.
  2. `apply_flags(preset, modalities)` → the conservative subset derive_recipe applies by
     DEFAULT (operator overrides always win via merge_flags), so a headless run also benefits.

CONFIDENCE is honest: 'high' = validated on AEON's own DGX; 'medium' = arch is understood but
not benched here; 'low' = plausible defaults for a family we haven't run — safe flags only, the
parser is a recommendation in the notes (a wrong --reasoning-parser CRASHES the serve, and the
failure diagnostician will name it). Every flag is overridable; nothing here is gospel.

COMPOSITION — family ⊕ hardware: a family preset carries only MODEL-INTRINSIC flags (parsers,
trust-remote-code, KV/mamba cache dtypes, multimodal allowances); everything the HOST needs
(e.g. the GB10 attention-backend pin) lives in HARDWARE_PRESETS, selected from
engines.host_platform() via hardware_preset(). apply_flags()/full_flags() compose the two:
family safe flags + hardware flags + (confidence-gated) parser flags + perf/audio. On a DGX
Spark the composed output is flag-for-flag identical to the pre-split presets; other hosts
simply stop inheriting GB10-specific flags."""
from __future__ import annotations

# Safe flags (KV dtype, trust-remote-code, mamba/mm) rarely crash a serve.
# Parser flags (--reasoning-parser / --tool-call-parser) MUST name a parser the engine build
# registers — so they're only auto-applied for high/medium-confidence families. Parser names in
# presets are ONLY ever ones present in the engines.FLAG_CATALOG option lists (test-enforced);
# an unverified parser is a NOTE, never a flag.
_GB10_ATTN = ["--attention-backend", "triton_attn"]     # FlashInfer is broken on GB10

# Qwen scheduler budgets that pair with speculative decode (DFlash drafter / native MTP): a
# spec-decode step verifies several draft tokens per sequence per pass, so the batched-token
# budget must be sized well above the sequence cap or the scheduler starves the drafter.
# FAMILY-level (not hardware): the budgets are model-intrinsic scheduling behavior, carry no
# host-specific pins, and applied on every host exactly as the pre-split presets did.
_QWEN_DFLASH_SCHED = [
    "--max-num-seqs", "64",
    "--max-num-batched-tokens", "32768",
    "--enable-chunked-prefill",
    "--generation-config", "vllm",
]

# ---- HARDWARE presets: what the HOST needs, independent of model family -----------------------
# Composes with the family preset in apply_flags/full_flags (family ⊕ hardware). Selected from
# engines.host_platform() by hardware_preset(); flags append AFTER the family's safe flags —
# exactly where _GB10_ATTN used to sit — so GB10 output is unchanged by the split.
HARDWARE_PRESETS: dict[str, dict] = {
    "dgx_spark": {
        "id": "dgx_spark", "label": "DGX Spark (GB10)",
        "flags": list(_GB10_ATTN),
        "notes": "FlashInfer is broken on GB10 — pin triton_attn (flash_attn also works). "
                 "Unified memory (CPU+GPU share one pool): keep gpu-util 0.6-0.7 (0.7 default) — "
                 ">~0.8 page-thrashes and stalls the box; go lower with co-located services, high "
                 "concurrency, fp16 KV, or DFlash (its buffers aren't counted). 16-24 seqs is the "
                 "64K-ctx sweet spot.",
    },
    "cuda_generic": {
        "id": "cuda_generic", "label": "CUDA (generic)",
        "flags": [],
        "notes": "Discrete NVIDIA GPUs: the engine's own attention-backend auto-pick is best — "
                 "no forced flags.",
    },
    "rocm": {
        "id": "rocm", "label": "AMD ROCm",
        "flags": [],
        "notes": "No AEON-validated ROCm-specific serve flags yet — rocm/vllm defaults apply.",
    },
    "metal": {
        "id": "metal", "label": "Apple Metal",
        "flags": [],
        "notes": "Apple silicon serves bare-metal MLX / LM Studio — vllm-style flags don't apply.",
    },
    "cpu": {
        "id": "cpu", "label": "CPU",
        "flags": [],
        "notes": "CPU serving (llama.cpp grammar) — no vllm-style hardware flags.",
    },
}


def hardware_preset(plat: dict | None = None) -> dict:
    """The HARDWARE_PRESETS entry for a host_platform() dict (None = detect THIS host).
    DGX Spark wins over generic CUDA; unknown accelerators fall back to the flagless cpu entry."""
    if plat is None:
        from pod import engines
        plat = engines.host_platform()
    if plat.get("dgx_spark"):
        return HARDWARE_PRESETS["dgx_spark"]
    key = {"cuda": "cuda_generic", "rocm": "rocm", "metal": "metal"}.get(plat.get("accel"), "cpu")
    return HARDWARE_PRESETS[key]


PRESETS: list[dict] = [
    {
        "id": "gemma4",
        "label": "Gemma 4  (12B · 31B · 266B-A4B · E4B)",
        "model_types": ["gemma4", "gemma4_unified", "gemma4_text", "gemma4_unified_text"],
        "arch_substr": ["Gemma4"],
        "name_substr": ["gemma-4", "gemma4", "gemma 4"],
        "confidence": "high",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--reasoning-parser", "gemma4", "--tool-call-parser", "gemma4",
                         "--enable-auto-tool-choice"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "Interleaved sliding-window layers: KV cache MUST be auto on triton_attn — "
                 "fp8 KV crashes at the first request ('Window left is not the same for all "
                 "layers'). The Unified variant (gemma4_unified) is audio-capable and auto-gets "
                 "--limit-mm-per-prompt. Handles an uncapped reasoning budget fine.",
    },
    {
        "id": "qwen3_5_moe",
        "label": "Qwen 3.5 / 3.6 MoE  (35B-A3B · 122B-A10B · Ornith)",
        "model_types": ["qwen3_5_moe", "qwen3_5_moe_text", "qwen3moe", "qwen3_moe"],
        "arch_substr": ["Qwen3_5Moe", "Qwen3Moe"],
        "name_substr": ["a3b", "a10b", "ornith", "qwen3.6", "qwen3.5"],
        "confidence": "high",
        "safe_flags": ["--kv-cache-dtype", "auto"] + _QWEN_DFLASH_SCHED,
        "parser_flags": ["--reasoning-parser", "qwen3",
                         "--tool-call-parser", "qwen3_coder", "--enable-auto-tool-choice"],
        "perf_flags": ["--kv-cache-dtype", "fp8_e4m3"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "MoE — fp8 KV cache is SAFE here (unlike Gemma-4) and buys concurrency at "
                 "64K+ ctx — it's in the recommendation as a perf option, not forced (KV auto "
                 "keeps full precision by default). DFlash speculative decoding pairs well (n=6).",
    },
    {
        "id": "qwen3_5",
        "label": "Qwen 3.5 / 3.6 dense  (9B · 27B)",
        "model_types": ["qwen3_5", "qwen3_5_text", "qwen3", "qwen2"],
        "arch_substr": ["Qwen3_5", "Qwen3For", "Qwen2For"],
        "name_substr": ["qwen3", "qwen2"],
        "confidence": "high",
        "safe_flags": ["--kv-cache-dtype", "auto"] + _QWEN_DFLASH_SCHED,
        "parser_flags": ["--reasoning-parser", "qwen3",
                         "--tool-call-parser", "qwen3_coder", "--enable-auto-tool-choice"],
        "perf_flags": ["--kv-cache-dtype", "fp8_e4m3"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "Dense Qwen — the qwen3_coder tool parser drives the agentic harnesses cleanly. fp8 KV cache is a safe perf option "
                 "(recommended, not forced) that frees KV memory for more concurrency.",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek V3 / V4 (+ Flash)",
        "model_types": ["deepseek_v4", "deepseek_v3", "deepseek_v2", "deepseek"],
        "arch_substr": ["DeepseekV4", "DeepseekV3", "DeepseekV2"],
        "name_substr": ["deepseek"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": ["--reasoning-parser", "deepseek_r1", "--tool-call-parser", "deepseek_v3",
                         "--enable-auto-tool-choice"],
        "audio_flags": [],
        "notes": "MLA attention — keep KV cache auto (fp8 KV + MLA is finicky). Large MoE variants "
                 "want --tensor-parallel-size across GPUs. Flash variants are lighter but the same "
                 "recipe. Verify the deepseek_r1 reasoning parser exists in your engine build.",
    },
    {
        "id": "glm",
        "label": "GLM 4 / ChatGLM",
        "model_types": ["glm4", "glm", "chatglm"],
        "arch_substr": ["Glm4", "GLM", "ChatGLM"],
        "name_substr": ["glm"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": ["--reasoning-parser", "glm45", "--tool-call-parser", "glm45",
                         "--enable-auto-tool-choice"],
        "audio_flags": [],
        "notes": "Ships custom modeling code (trust-remote-code on). The glm4 parser names vary by "
                 "engine version — if the serve rejects them, the diagnostician will point at the "
                 "parser flag; clear it and the model still benches (raw answers).",
    },
    {
        "id": "nemotron",
        "label": "Nemotron  (incl. Nano / Omni hybrid-Mamba)",
        "model_types": ["nemotron", "nemotron_h", "nemotron_nano", "nemotron_omni"],
        "arch_substr": ["Nemotron"],
        "name_substr": ["nemotron"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto", "--mamba-cache-dtype", "float32",
                       "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "Hybrid-Mamba variants (Nemotron-H / Nano / Omni) NEED --mamba-cache-dtype "
                 "float32 or the state math degrades. Omni is multimodal — audio allowance auto-"
                 "added. No standard reasoning parser; leave it unset.",
    },
    {
        "id": "kimi",
        "label": "Kimi K2 (Moonshot)",
        "model_types": ["kimi", "kimi_k2", "moonshot"],
        "arch_substr": ["Kimi", "Moonshot"],
        "name_substr": ["kimi"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "DeepSeek-style trillion-scale MoE — needs multi-GPU --tensor-parallel-size and a "
                 "lot of memory; the GB10 can't hold the large variants. Recommended (verify in "
                 "your build): --tool-call-parser kimi_k2 --enable-auto-tool-choice. LOW confidence "
                 "— not benched on AEON hardware.",
    },
    {
        "id": "stepfun",
        "label": "StepFun 3 / Step-3",
        "model_types": ["step3", "step2", "stepfun", "step"],
        "arch_substr": ["Step", "StepFun"],
        "name_substr": ["stepfun", "step-3", "step3", "step 3"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "LOW confidence — not benched on AEON hardware. Safe defaults only. Recommended "
                 "(verify in your build): --reasoning-parser step3 --tool-call-parser step3 "
                 "--enable-auto-tool-choice.",
    },
    {
        "id": "mimo",
        "label": "MiMo (Xiaomi)",
        "model_types": ["mimo", "mimo_vl"],
        "arch_substr": ["MiMo"],
        "name_substr": ["mimo"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "Qwen-derived reasoning model — the qwen3 reasoning parser usually works "
                 "(verify). LOW confidence — not benched here.",
    },
    # ---- families below were added from arch knowledge, NOT benched on AEON hardware ----------
    # (parser names are still strictly from the engines.FLAG_CATALOG option lists — test-enforced)
    {
        "id": "llama4",       # MUST precede llama3: model_type 'llama4' prefix-matches both
        "label": "Llama 4  (Scout · Maverick)",
        "model_types": ["llama4"],
        "arch_substr": ["Llama4"],
        "name_substr": ["llama-4", "llama4", "llama 4"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--tool-call-parser", "llama4_pythonic", "--enable-auto-tool-choice"],
        "audio_flags": [],
        "notes": "Early-fusion multimodal MoE. llama4_pythonic is the documented tool parser for "
                 "the instruct releases (llama4_json also exists in the catalog if a build "
                 "mis-parses — swap it in Recipe Tuning). No reasoning parser: Llama 4 doesn't "
                 "emit think tags. Not benched on AEON hardware.",
    },
    {
        "id": "llama3",
        "label": "Llama 3.x  (incl. 3.1 / 3.2 / CodeLlama)",
        "model_types": ["llama", "mllama"],
        "arch_substr": ["Llama", "MLlama"],
        "name_substr": ["llama-3", "llama3", "llama"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--tool-call-parser", "llama3_json", "--enable-auto-tool-choice"],
        "audio_flags": [],
        "notes": "The most-served open architecture. llama3_json is the standard tool parser for "
                 "3.1+ instruct — harmless on older Llamas (it only engages when tools are "
                 "requested). No reasoning parser: Llama 3.x doesn't emit think tags.",
    },
    {
        "id": "gpt_oss",
        "label": "GPT-OSS  (20B · 120B)",
        "model_types": ["gpt_oss"],
        "arch_substr": ["GptOss"],
        "name_substr": ["gpt-oss", "gpt_oss", "gpt oss"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--reasoning-parser", "gpt_oss"],
        "audio_flags": [],
        "notes": "Harmony response format: the gpt_oss reasoning parser splits the analysis "
                 "channel from the final answer — without it the trace leaks and tanks "
                 "Instruction/Prose. Native MXFP4 quantization on the official repos. Tool "
                 "calling is build-specific (no gpt_oss tool parser in the verified catalog) — "
                 "leave it unset unless your build documents one.",
    },
    {
        "id": "mistral",
        "label": "Mistral / Mixtral / Pixtral",
        "model_types": ["mistral", "mixtral", "pixtral", "mistral3", "ministral"],
        "arch_substr": ["Mistral", "Mixtral", "Pixtral"],
        "name_substr": ["mistral", "mixtral", "pixtral", "ministral", "magistral", "devstral"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--tool-call-parser", "mistral", "--enable-auto-tool-choice"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "The mistral tool parser covers the official instruct family. Official Mistral "
                 "repos ship tekken/mistral-format tokenizers — if tool calls mis-parse, add "
                 "--tokenizer-mode mistral in extra flags (not auto-set: HF-converted repos "
                 "don't want it). Magistral reasoning variants: set --reasoning-parser mistral "
                 "manually (in the catalog; verify in your build).",
    },
    {
        "id": "phi",
        "label": "Phi  (Phi-3 · Phi-4 · mini / MoE / multimodal)",
        "model_types": ["phi", "phi3", "phimoe", "phi4mm", "phi4_multimodal"],
        "arch_substr": ["Phi3", "Phi4", "PhiMoE", "Phi"],
        "name_substr": ["phi-4", "phi4", "phi-3", "phi3"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "LOW confidence — not benched on AEON hardware. Several Phi repos ship custom "
                 "modeling code (trust-remote-code on); Phi-4-multimodal declares audio (auto-"
                 "allowance). Recommended for Phi-4-mini instruct (verify in your build): "
                 "--tool-call-parser phi4_mini_json --enable-auto-tool-choice.",
    },
    {
        "id": "granite",
        "label": "IBM Granite  (3.x / 4.x)",
        "model_types": ["granite", "granitemoe", "granite_moe", "granitemoehybrid"],
        "arch_substr": ["Granite"],
        "name_substr": ["granite"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--tool-call-parser", "granite", "--enable-auto-tool-choice"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "granite is the instruct tool parser (the 20B function-calling model has its own "
                 "granite-20b-fc catalog entry). Thinking is OPT-IN on Granite 3.2+ — set "
                 "--reasoning-parser granite manually for a thinking bench. Granite 4 hybrid-"
                 "Mamba variants may want --mamba-cache-dtype float32 like Nemotron-H (verify).",
    },
    {
        "id": "ernie",
        "label": "ERNIE 4.5 (Baidu)",
        "model_types": ["ernie4_5", "ernie4_5_moe", "ernie"],
        "arch_substr": ["Ernie4_5", "Ernie"],
        "name_substr": ["ernie"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "LOW confidence — not benched on AEON hardware. Recommended for the -Thinking "
                 "variants (verify in your build): --reasoning-parser ernie45. Large MoE "
                 "variants want --tensor-parallel-size across GPUs; VL variants need a recent "
                 "engine build.",
    },
    {
        "id": "hunyuan",
        "label": "Hunyuan (Tencent)",
        "model_types": ["hunyuan", "hunyuan_v1_dense", "hunyuan_v1_moe"],
        "arch_substr": ["Hunyuan"],
        "name_substr": ["hunyuan"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "LOW confidence — not benched on AEON hardware. The catalog's hunyuan_a13b "
                 "parsers are named for Hunyuan-A13B: for that model set --reasoning-parser "
                 "hunyuan_a13b --tool-call-parser hunyuan_a13b --enable-auto-tool-choice "
                 "(verify in your build).",
    },
    {
        "id": "seed_oss",
        "label": "Seed-OSS (ByteDance)",
        "model_types": ["seed_oss"],
        "arch_substr": ["SeedOss"],
        "name_substr": ["seed-oss", "seed_oss"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--reasoning-parser", "seed_oss", "--tool-call-parser", "seed_oss",
                         "--enable-auto-tool-choice"],
        "audio_flags": [],
        "notes": "Reasoning model with a controllable thinking budget — both seed_oss parser "
                 "names are in the verified catalog. Handles the bench's uncapped budget; needs "
                 "a recent transformers for the seed_oss model_type. Not benched on AEON "
                 "hardware.",
    },
    {
        "id": "minimax",
        "label": "MiniMax  (M1 / M2 / Text-01)",
        "model_types": ["minimax", "minimax_text", "minimax_m1", "minimax_m2"],
        "arch_substr": ["MiniMax"],
        "name_substr": ["minimax"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"],
        "parser_flags": [],
        "audio_flags": [],
        "notes": "LOW confidence — trillion-scale hybrid/linear-attention MoE, far beyond a "
                 "single GB10 (needs --tensor-parallel-size across a rack). Recommended (verify "
                 "in your build): --reasoning-parser minimax_m1 for M1-style think traces, "
                 "--tool-call-parser minimax --enable-auto-tool-choice.",
    },
    {
        "id": "qwq",
        "label": "QwQ (Qwen reasoning)",
        "model_types": ["qwq"],
        "arch_substr": ["QwQ"],
        "name_substr": ["qwq"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto"],
        "parser_flags": ["--reasoning-parser", "qwq", "--tool-call-parser", "hermes",
                         "--enable-auto-tool-choice"],
        "perf_flags": ["--kv-cache-dtype", "fp8_e4m3"],
        "audio_flags": [],
        "notes": "Reasons like Qwen (uncapped <think> budget). QwQ repos usually declare "
                 "model_type qwen2, so config detection lands on the Qwen dense preset (its "
                 "qwen3 reasoning parser also handles <think> tags fine); this preset catches "
                 "name/explicit matches with the dedicated qwq parser. hermes is the documented "
                 "Qwen2.5-era tool format.",
    },
]

# Fallback when nothing matches: no family flags at all (the hardware preset still composes in,
# so a GB10 host keeps its attention pin). Never guesses a parser.
_GENERIC = {
    "id": "generic", "label": "Generic (no family preset matched)", "confidence": "low",
    "safe_flags": [], "parser_flags": [], "audio_flags": [],
    "notes": "No known family matched this architecture — applying only this host's hardware "
             "flags. Set quantization/parsers manually, or share the config so a preset can be "
             "added.",
}


def _model_type(config: dict) -> str:
    for scope in (config, config.get("text_config") or {}, config.get("llm_config") or {}):
        mt = scope.get("model_type") if isinstance(scope, dict) else None
        if mt:
            return str(mt).lower()
    return ""


def detect(config: dict | None, name: str = "") -> dict:
    """Best-matching family preset for a model (config.json + optional repo/dir name). Always
    returns a preset — the generic safe default if no family matches. Match precedence:
    model_type (exact/prefix) > architecture substring > name substring."""
    config = config or {}
    mt = _model_type(config)
    arch = ((config.get("architectures") or [""]) or [""])[0] or ""
    nm = (name or "").lower()

    def matches(p):
        if mt and any(mt == t or mt.startswith(t + "_") or mt.startswith(t) for t in p["model_types"]):
            return 3
        if arch and any(s.lower() in arch.lower() for s in p["arch_substr"]):
            return 2
        if nm and any(s in nm for s in p["name_substr"]):
            return 1
        return 0

    best, score = None, 0
    for p in PRESETS:
        s = matches(p)
        if s > score:
            best, score = p, s
    return best or _GENERIC


def _has_audio(modalities) -> bool:
    return "audio" in (modalities or [])


def _dedup(flags: list[str]) -> list[str]:
    """Collapse a flag list to one value per flag, LAST occurrence winning (so a perf override
    of a safe default lands cleanly), preserving first-seen order."""
    val: dict[str, object] = {}
    order: list[str] = []
    i = 0
    while i < len(flags):
        f = flags[i]
        if not str(f).startswith("-"):
            i += 1
            continue
        if f not in val:
            order.append(f)
        if i + 1 < len(flags) and not str(flags[i + 1]).startswith("-"):
            val[f] = flags[i + 1]
            i += 2
        else:
            val[f] = None
            i += 1
    out: list[str] = []
    for f in order:
        out.append(f)
        if val[f] is not None:
            out.append(val[f])
    return out


def apply_flags(preset: dict, modalities=None, hardware: dict | None = None) -> list[str]:
    """Conservative flags derive_recipe applies by DEFAULT: the family's safe flags plus this
    HOST's hardware-preset flags (family ⊕ hardware); parser flags only for high/medium
    confidence (a wrong parser crashes the serve). Audio allowance only when the model actually
    declares audio. `hardware` is a HARDWARE_PRESETS entry (None = detect this host). Operator
    flags still override via merge_flags."""
    hw = hardware if hardware is not None else hardware_preset()
    flags = list(preset.get("safe_flags") or []) + list(hw.get("flags") or [])
    if preset.get("confidence") in ("high", "medium"):
        flags += list(preset.get("parser_flags") or [])
    if _has_audio(modalities):
        flags += list(preset.get("audio_flags") or [])
    return flags


def full_flags(preset: dict, modalities=None, hardware: dict | None = None) -> list[str]:
    """The COMPLETE recommendation for the GUI 'apply preset' chip (safe + hardware + parser +
    perf + audio), regardless of confidence — the operator sees + edits it before launch. Perf
    flags (e.g. Qwen fp8 KV) are RECOMMENDED here but NOT auto-applied by apply_flags. Later
    occurrences win, so a perf flag that overrides a safe one (kv auto -> fp8) lands correctly."""
    hw = hardware if hardware is not None else hardware_preset()
    flags = list(preset.get("safe_flags") or []) + list(hw.get("flags") or []) \
        + list(preset.get("parser_flags") or []) + list(preset.get("perf_flags") or [])
    if _has_audio(modalities):
        flags += list(preset.get("audio_flags") or [])
    return _dedup(flags)


def summary(preset: dict, modalities=None, hardware: dict | None = None) -> dict:
    """The GUI payload for a detected preset (flags = family ⊕ this host's hardware preset)."""
    hw = hardware if hardware is not None else hardware_preset()
    return {
        "id": preset["id"], "label": preset["label"], "confidence": preset["confidence"],
        "notes": preset.get("notes", ""),
        "flags": full_flags(preset, modalities, hw),
        "hardware": {"id": hw["id"], "label": hw["label"]},
        "capabilities": [m for m in (modalities or ["text"])],
    }
