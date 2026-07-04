"""Estimate a model's VRAM footprint from its name + known-system presets, so users
filter the board to "models that actually run on my hardware."

The estimate is a heuristic (params parsed from the name × bytes/param at the detected
quant + runtime overhead). It is labelled as an estimate, never authoritative — a real
pod reports measured peak VRAM with the verified submission.
"""
from __future__ import annotations

import re

# bytes per weight at common quantizations (weights only)
QUANT_BPP = {"q2": 0.34, "q3": 0.43, "q4": 0.55, "q5": 0.68, "q6": 0.78,
             "q8": 1.06, "fp8": 1.0, "fp16": 2.0, "bf16": 2.0, "fp32": 4.0}
DEFAULT_QUANT = "q4"     # most common for local inference
OVERHEAD_GB = 1.5        # KV cache + context + runtime, rough flat add


def parse_params_b(name):
    """Billions of parameters parsed from a model name ('12b', '0.5b', '135m', '8x7b')."""
    n = (name or "").lower()
    m = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", n)      # MoE e.g. 8x7b
    if m:
        return int(m.group(1)) * float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", n)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", n)
    if m:
        return float(m.group(1)) / 1000.0
    return None


def detect_quant(name):
    n = (name or "").lower()
    for q in ("q2", "q3", "q4", "q5", "q6", "q8", "fp8", "fp16", "bf16", "fp32"):
        if q in n:
            return q
    if re.search(r"\bk4\b|q4_k|int4|4bit|-4b-|awq|gptq", n):
        return "q4"
    if re.search(r"q8_0|int8|8bit", n):
        return "q8"
    return DEFAULT_QUANT


def estimate_gb(name, quant=None):
    """Estimated VRAM (GB) to load + run the model, or None if size can't be parsed."""
    p = parse_params_b(name)
    if p is None:
        return None
    bpp = QUANT_BPP.get(quant or detect_quant(name), QUANT_BPP[DEFAULT_QUANT])
    return round(p * bpp + OVERHEAD_GB, 1)


# Known local-AI rigs -> GPU-allocatable VRAM (GB) for the filter presets. Unified-memory
# systems (Apple, Strix Halo, DGX Spark) are derated to roughly what the GPU can claim.
PRESETS = [
    {"name": "RTX 5070 (12GB)", "vram": 12},
    {"name": "RTX 5080 (16GB)", "vram": 16},
    {"name": "Apple M4 Pro 24GB", "vram": 18},
    {"name": "RTX 3090 (24GB)", "vram": 24},
    {"name": "RTX 4090 (24GB)", "vram": 24},
    {"name": "RTX 5090 (32GB)", "vram": 32},
    {"name": "Apple M4 Pro 48GB", "vram": 36},
    {"name": "AMD Strix Halo 64GB", "vram": 56},
    {"name": "RTX PRO 6000 (96GB)", "vram": 96},
    {"name": "DGX Spark (128GB)", "vram": 110},
    {"name": "Apple M5 Max 128GB", "vram": 110},
    {"name": "AMD Strix Halo 128GB", "vram": 112},
]
