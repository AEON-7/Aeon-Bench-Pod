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
failure diagnostician will name it). Every flag is overridable; nothing here is gospel."""
from __future__ import annotations

# Safe flags (attention backend, KV dtype, trust-remote-code, mamba/mm) rarely crash a serve.
# Parser flags (--reasoning-parser / --tool-call-parser) MUST name a parser the engine build
# registers — so they're only auto-applied for high/medium-confidence families.
_GB10_ATTN = ["--attention-backend", "triton_attn"]     # FlashInfer is broken on GB10

PRESETS: list[dict] = [
    {
        "id": "gemma4",
        "label": "Gemma 4  (12B · 31B · 266B-A4B · E4B)",
        "model_types": ["gemma4", "gemma4_unified", "gemma4_text", "gemma4_unified_text"],
        "arch_substr": ["Gemma4"],
        "name_substr": ["gemma-4", "gemma4", "gemma 4"],
        "confidence": "high",
        "safe_flags": ["--kv-cache-dtype", "auto"] + _GB10_ATTN,
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
        "safe_flags": ["--kv-cache-dtype", "auto"] + _GB10_ATTN,
        "parser_flags": ["--reasoning-parser", "qwen3", "--reasoning-budget", "16384",
                         "--tool-call-parser", "hermes", "--enable-auto-tool-choice"],
        "perf_flags": ["--kv-cache-dtype", "fp8_e4m3"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "MoE — reasoning budget capped at 16384 (Qwen destabilizes with a longer "
                 "<think>). fp8 KV cache is SAFE here (unlike Gemma-4) and buys concurrency at "
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
        "safe_flags": ["--kv-cache-dtype", "auto"] + _GB10_ATTN,
        "parser_flags": ["--reasoning-parser", "qwen3", "--reasoning-budget", "16384",
                         "--tool-call-parser", "hermes", "--enable-auto-tool-choice"],
        "perf_flags": ["--kv-cache-dtype", "fp8_e4m3"],
        "audio_flags": ["--limit-mm-per-prompt", '{"image":4,"audio":4}'],
        "notes": "Dense Qwen — reasoning budget 16384 (destabilizes higher); the hermes tool "
                 "parser drives the agentic harnesses cleanly. fp8 KV cache is a safe perf option "
                 "(recommended, not forced) that frees KV memory for more concurrency.",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek V3 / V4 (+ Flash)",
        "model_types": ["deepseek_v4", "deepseek_v3", "deepseek_v2", "deepseek"],
        "arch_substr": ["DeepseekV4", "DeepseekV3", "DeepseekV2"],
        "name_substr": ["deepseek"],
        "confidence": "medium",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"] + _GB10_ATTN,
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
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"] + _GB10_ATTN,
        "parser_flags": ["--reasoning-parser", "glm4", "--tool-call-parser", "glm4",
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
                       "--trust-remote-code"] + _GB10_ATTN,
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
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"] + _GB10_ATTN,
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
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"] + _GB10_ATTN,
        "parser_flags": [],
        "audio_flags": [],
        "notes": "LOW confidence — not benched on AEON hardware. Safe defaults only; add a "
                 "reasoning/tool parser once you confirm the names your engine build registers.",
    },
    {
        "id": "mimo",
        "label": "MiMo (Xiaomi)",
        "model_types": ["mimo", "mimo_vl"],
        "arch_substr": ["MiMo"],
        "name_substr": ["mimo"],
        "confidence": "low",
        "safe_flags": ["--kv-cache-dtype", "auto", "--trust-remote-code"] + _GB10_ATTN,
        "parser_flags": [],
        "audio_flags": [],
        "notes": "Qwen-derived reasoning model — the qwen3 reasoning parser usually works "
                 "(verify), with --reasoning-budget 16384. LOW confidence — not benched here.",
    },
]

# Fallback when nothing matches: the only universally-safe GB10 flag. Never guesses a parser.
_GENERIC = {
    "id": "generic", "label": "Generic (no family preset matched)", "confidence": "low",
    "safe_flags": list(_GB10_ATTN), "parser_flags": [], "audio_flags": [],
    "notes": "No known family matched this architecture — applying only a safe attention backend. "
             "Set quantization/parsers manually, or share the config so a preset can be added.",
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


def apply_flags(preset: dict, modalities=None) -> list[str]:
    """Conservative flags derive_recipe applies by DEFAULT: safe flags always; parser flags only
    for high/medium confidence (a wrong parser crashes the serve). Audio allowance only when the
    model actually declares audio. Operator flags still override via merge_flags."""
    flags = list(preset.get("safe_flags") or [])
    if preset.get("confidence") in ("high", "medium"):
        flags += list(preset.get("parser_flags") or [])
    if _has_audio(modalities):
        flags += list(preset.get("audio_flags") or [])
    return flags


def full_flags(preset: dict, modalities=None) -> list[str]:
    """The COMPLETE recommendation for the GUI 'apply preset' chip (safe + parser + perf +
    audio), regardless of confidence — the operator sees + edits it before launch. Perf flags
    (e.g. Qwen fp8 KV) are RECOMMENDED here but NOT auto-applied by apply_flags. Later
    occurrences win, so a perf flag that overrides a safe one (kv auto -> fp8) lands correctly."""
    flags = list(preset.get("safe_flags") or []) + list(preset.get("parser_flags") or []) \
        + list(preset.get("perf_flags") or [])
    if _has_audio(modalities):
        flags += list(preset.get("audio_flags") or [])
    return _dedup(flags)


def summary(preset: dict, modalities=None) -> dict:
    """The GUI payload for a detected preset."""
    return {
        "id": preset["id"], "label": preset["label"], "confidence": preset["confidence"],
        "notes": preset.get("notes", ""),
        "flags": full_flags(preset, modalities),
        "capabilities": [m for m in (modalities or ["text"])],
    }
