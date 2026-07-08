"""pod/diagnostics.py — turn an engine/serve failure into a plain-language RECIPE FIX.

When a bench dies during serve/startup, the vLLM (or other engine) traceback is in the job
log, but it's a wall of Python the operator has to decode. `diagnose(log_lines)` scans the
log for known failure SIGNATURES and returns the single most-relevant hint: what went wrong,
in one sentence, plus the exact Recipe-Tuning knob to change. Ordered most-specific first so
a precise match (the Gemma4 sliding-window crash) wins over a generic one (OOM).

Every signature is something we've actually hit on the GB10 or that vLLM raises verbatim; add
new ones as they show up in the field — the table is the whole surface."""
from __future__ import annotations

import re

# (compiled regex over the joined log, the RECIPE FLAG this concerns or None, hint). FIRST match
# wins, so put specific before generic. When the named flag was OPERATOR-SET (a custom recipe
# value), diagnose() prefixes the hint with exactly which custom flag is the likely culprit.
_UNRECOGNIZED = re.compile(r"unrecognized arguments?:\s*(--[\w-]+)", re.I)

_SIGNATURES: list[tuple[re.Pattern, str | None, str]] = [
    (re.compile(r"unrecognized arguments?:|error: unrecognized", re.I), None,
     "The engine rejected a flag it doesn't recognize (see 'unrecognized arguments: --…' in the "
     "log). Remove that flag from RECIPE TUNING — it isn't supported by this engine build. If it "
     "came from a family preset, clear it there; the model still benches without it."),
    (re.compile(r"Window left is not the same for all layers", re.I), None,
     "Sliding-window attention metadata failed. For Qwen+DFlash, keep attention-backend = "
     "triton_attn and include \"attention_backend\":\"TRITON_ATTN\" inside --speculative-config. "
     "For Gemma-4, use kv-cache-dtype = auto on triton_attn, or add --disable-sliding-window if "
     "you must keep fp8 KV."),
    (re.compile(r"Please install vllm\[audio\]|vllm\[audio\] for audio", re.I), None,
     "The engine image is missing audio decode deps (av/soxr/librosa). Use the audio-capable "
     "aeon-vllm-ultimate:latest (audio deps baked in), or pick a non-audio engine — a "
     "text/vision bench is unaffected."),
    (re.compile(r"No available memory for the cache|not enough (?:KV cache )?memory|"
                r"KV cache.*(?:too small|insufficient)|Available KV cache memory.*is (?:0|negative)", re.I),
     "--gpu-memory-utilization",
     "The KV cache didn't fit. In RECIPE TUNING lower gpu-memory-utilization (GB10 is OOM-safe "
     "around 0.70), or lower max-model-len (64K is the floor), or lower max-num-seqs (try "
     "16-24). fp8 KV cache also frees room — but never on Gemma-4."),
    (re.compile(r"CUDA out of memory|torch\.(?:cuda\.)?OutOfMemoryError|"
                r"HIP out of memory|out of memory.*Tried to allocate", re.I),
     "--gpu-memory-utilization",
     "Out of GPU memory loading/serving the model. In RECIPE TUNING lower gpu-memory-utilization "
     "(0.70 on GB10), reduce max-num-seqs, or reduce max-model-len. If a DFlash drafter is set, "
     "its weights add up — try without it first."),
    (re.compile(r"max_num_batched_tokens.*(?:smaller|less) than.*max_model_len|"
                r"max_model_len.*(?:larger|greater) than.*max_num_batched_tokens", re.I),
     "--enable-chunked-prefill",
     "max-model-len exceeds the batched-token budget. Enable chunked prefill "
     "(--enable-chunked-prefill) in RECIPE TUNING, or lower max-model-len."),
    (re.compile(r"max_num_scheduled_tokens is set to .*based on the speculative decoding settings|"
                r"does not allow any tokens to be scheduled.*additional draft token slots", re.I),
     "--max-num-batched-tokens",
     "Speculative decoding reserved more draft-token slots than vLLM's scheduler budget allows. "
     "In RECIPE TUNING set max-num-batched-tokens = 32768 and cap max-num-seqs (try 64 for "
     "64K bench context, or 16 for long-context sidecar profiles), or reduce "
     "num_speculative_tokens."),
    (re.compile(r"Unknown quantization|Unsupported quantization|quantization method.*not "
                r"(?:supported|recognized)|No supported quant|does not support.*quantization", re.I),
     "--quantization",
     "The --quantization value doesn't match these weights. It's normally auto-derived from "
     "config.json (NVFP4 -> modelopt, GGUF -> none). Clear the quantization override in RECIPE "
     "TUNING to use the derived one, or set the correct method (modelopt / compressed-tensors / "
     "awq / gptq / fp8)."),
    (re.compile(r"FlashInfer|flashinfer.*(?:not|unsupported|failed to|no kernel)", re.I),
     "--attention-backend",
     "FlashInfer is broken on the GB10. In RECIPE TUNING set attention-backend = triton_attn "
     "(or flash_attn)."),
    (re.compile(r"trust_remote_code|requires.*remote code|custom.*modeling.*code|"
                r"Loading this model requires you to (?:execute|trust)", re.I), "--trust-remote-code",
     "This repo ships custom modeling code. Enable trust-remote-code in RECIPE TUNING."),
    (re.compile(r"reasoning[_-]parser.*(?:not|unknown|invalid|no such)|"
                r"unknown reasoning parser|--reasoning-parser.*invalid", re.I), "--reasoning-parser",
     "The reasoning-parser name isn't supported by this engine build. In RECIPE TUNING pick your "
     "family's parser (Qwen -> qwen3, DeepSeek -> deepseek_r1, Gemma-4 -> gemma4, GLM-4.5 -> "
     "glm45, StepFun -> step3) or clear it. The model-family preset picks the right one "
     "automatically."),
    (re.compile(r"rope_scaling|rope scaling|position.*embeddings.*exceed|"
                r"max_position_embeddings.*(?:exceed|larger)", re.I), "--max-model-len",
     "The requested context exceeds the model's native window. Lower max-model-len to the model's "
     "native context (shown on the validation strip), or configure rope scaling if the model "
     "supports it."),
    (re.compile(r"tool[_-]call[_-]parser|tool call parser.*(?:not|unknown|invalid)", re.I),
     "--tool-call-parser",
     "The tool-call-parser name isn't supported by this build. In RECIPE TUNING pick your family's "
     "parser (Qwen -> qwen3_coder, DeepSeek -> deepseek_v3, GLM-4.5 -> glm45, Kimi K2 -> kimi_k2, "
     "StepFun -> step3, Gemma-4 -> gemma4, Llama -> llama3_json; hermes is the generic fallback) "
     "or clear it."),
    (re.compile(r"port.*already (?:in use|serving|bound)|address already in use|"
                r"refusing to bench whatever", re.I), None,
     "The serve port was still busy. A previous server may not have released :8000 — enable "
     "'stop other containers' (clear-host) so the pod frees it, or set AEON_PAUSE_CONTAINERS to "
     "the container holding it."),
    (re.compile(r"No space left on device|disk.*full|ENOSPC", re.I), None,
     "The disk filled up (model weights are large). Free space, or point the model cache at a "
     "bigger volume (AEON_MODELS_DIR)."),
    (re.compile(r"WEIGHTS VERIFICATION FAILED|mismatches=\[", re.I), None,
     "The on-disk weights don't match the HF repo (hash mismatch). Point the HF link at the repo "
     "these weights actually came from, or clear the local copy to pull the real repo fresh."),
    (re.compile(r"\b40[13]\b|gated|authentication|token.*required|need.*token|Repository Not Found",
                re.I), None,
     "The repo looks gated/private or the token is wrong. Add a saved HF token in the Run tab and "
     "select it, then relaunch."),
]

# Generic engine-death fallbacks — only used when nothing specific matched, so the operator
# still gets a direction instead of a bare exit code.
_ENGINE_DEATH = re.compile(
    r"EngineCore|Engine core initialization failed|engine.*(?:died|crashed|exited)|"
    r"HTTP 500|TargetError|Failed core proc|RuntimeError.*engine", re.I)


def _flag_value(custom_flags, flag):
    """The operator's value for `flag` in a serve-flags list (['--x','v',...]) or None."""
    if not custom_flags:
        return None
    fl = [str(t) for t in custom_flags]
    for i, t in enumerate(fl):
        if t == flag:
            return fl[i + 1] if i + 1 < len(fl) and not fl[i + 1].startswith("--") else "(set)"
    return None


def diagnose(log_lines: list[str], custom_flags=None) -> str | None:
    """The single most useful recipe-fix hint for a failed run, or None if the log carries no
    recognizable engine signature. When the matched signature concerns a flag the OPERATOR set
    in their custom recipe, the hint names that exact flag+value as the likely culprit — the
    direct 'which part of my recipe broke' feedback."""
    # Scan all retained lines. vLLM can flood the tail with repeated EngineDeadError traces after
    # the first EngineCore failure, so a small tail window often misses the actual root cause.
    text = "\n".join(log_lines)
    # highest priority: an engine that rejected a specific flag — name it exactly.
    um = _UNRECOGNIZED.search(text)
    if um:
        flag = um.group(1)
        if flag == "--reasoning-budget":
            return ("The engine rejected `--reasoning-budget`; this vLLM build does not expose a "
                    "serve-side reasoning budget. Remove it from RECIPE TUNING and use the Run "
                    "tab's Token budget instead. The pod default is now 32768 tokens, including "
                    "hidden reasoning.")
        return (f"The engine rejected `{flag}` — it isn't a supported flag on this engine build. "
                f"Remove `{flag}` from RECIPE TUNING (if a family preset added it, clear it there); "
                f"the model still benches without it.")
    if re.search(r"Window left is not the same for all layers", text, re.I):
        is_gemma = re.search(r"Gemma|gemma4", text, re.I)
        is_qwen = re.search(r"Qwen|qwen3", text, re.I)
        is_dflash = re.search(r"\bdflash\b|speculative-config|speculative_config|/drafter",
                              text, re.I)
        is_flashinfer_path = re.search(r"attention/backends/flashinfer|Using FLASHINFER "
                                       r"attention backend|FlashInferBackend", text, re.I)
        if is_gemma and not is_qwen:
            val = _flag_value(custom_flags, "--kv-cache-dtype")
            hint = ("Gemma-4's interleaved sliding-window layers crash under fp8 KV cache on "
                    "triton_attn. In RECIPE TUNING set kv-cache-dtype = auto, or add "
                    "--disable-sliding-window if you must keep fp8 KV. The Gemma-4 family preset "
                    "already does this.")
            return f"Your custom recipe flag `--kv-cache-dtype {val}` is the likely culprit. {hint}" \
                if val is not None else hint
        if is_qwen or is_dflash or is_flashinfer_path:
            val = _flag_value(custom_flags, "--speculative-config")
            hint = ("This is the Qwen+DFlash sliding-window backend mismatch, not a Gemma FP8-KV "
                    "failure. The outer recipe can say attention-backend = triton_attn, but "
                    "DFlash also needs its nested JSON set to "
                    "`\"attention_backend\":\"TRITON_ATTN\"`; otherwise the speculative path can "
                    "fall back to FlashInfer and crash. Relaunch with the validated Qwen recipe "
                    "or add that field to --speculative-config.")
            return f"Your custom recipe flag `--speculative-config {val}` is missing the nested backend. {hint}" \
                if val and "attention_backend" not in str(val) else hint
    for rx, flag, hint in _SIGNATURES:
        if rx.search(text):
            val = _flag_value(custom_flags, flag) if flag else None
            if val is not None:
                return f"Your custom recipe flag `{flag} {val}` is the likely culprit. {hint}"
            return hint
    if _ENGINE_DEATH.search(text):
        base = ("The inference engine failed to start. Open the job log and read the last "
                "traceback line for the exact cause; common fixes live in RECIPE TUNING "
                "(kv-cache-dtype = auto, lower gpu-memory-utilization, attention-backend = "
                "triton_attn). Try the model-family preset for known-good defaults.")
        if custom_flags:
            base += (f"  Since this run used custom flags ({' '.join(str(t) for t in custom_flags)}), "
                     "try relaunching with the family preset first, then re-add one custom flag at "
                     "a time to find the one that breaks.")
        return base
    return None
