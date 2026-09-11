"""Per-model capability tags for the dashboard filter.

Honesty matters (AEON's whole premise): a tag is either
  - "tested"   : earned from benchmark/probe EVIDENCE (Vision from the vision
                 board; Reasoning/Coding/Math/Instruction from category scores), or
  - "declared" : inferred from the model NAME/family and NOT verified here
                 (Uncensored, Tool Calling, name-implied Vision/Audio/Reasoning).
A name never upgrades a capability to "tested". Tested always wins over declared
for the same tag. The UI styles the two differently so a declared tag is never
mistaken for a measured one.
"""
from __future__ import annotations

import re

# (tag, name-pattern) — DECLARED only. Order doesn't matter.
NAME_RULES = [
    ("Uncensored",   r"uncensor|abliterat|unfilter|jailbreak|aggressive|unaligned|dolphin"),
    ("Tool Calling", r"qwen|llama-?3|gpt-oss|gemma|mistral|mixtral|hermes|command-?r|functionary|phi-?[34]|firefunction"),
    ("Reasoning",    r"reason|thinking|think\b|[-_]r1\b|qwq|deepseek-?r1|\bo1\b|cogito|marco-?o1"),
    ("Vision",       r"\bvl\b|[-_]vl[-_]|vision|llava|moondream|internvl|qwen.?\d*-?vl|pixtral|omni"),
    ("Audio",        r"omni|[-_]audio|whisper|voxtral|qwen.*audio|ultravox"),
    ("Video",        r"video|omni"),
]

# text-board categories that read as capabilities. A category is "tested" when the
# model was actually MEASURED on it (has a score) — the score bar conveys how strong;
# the tag only attests that we evaluated the axis. (Previously gated at score>=70,
# which wrongly hid every capability of any model that wasn't already excellent.)
TESTED_CATEGORIES = {"Reasoning", "Coding", "Math", "Instruction"}


def model_tags(model, category_scores, board, vision_ok=False, audio_ok=False, video_ok=False):
    """Return [{name, source}] for a model. source in {tested, declared}."""
    name = (model or "").lower()
    by = {}

    def add(tag, source):
        if tag not in by or (source == "tested" and by[tag] == "declared"):
            by[tag] = source

    for tag, pat in NAME_RULES:
        if re.search(pat, name):
            add(tag, "declared")

    for cat, score in (category_scores or {}).items():
        if cat in TESTED_CATEGORIES and score is not None:
            add(cat, "tested")

    if board == "vision" or vision_ok:
        add("Vision", "tested")
    if audio_ok:
        add("Audio", "tested")
    if board == "video" or video_ok:
        add("Video", "tested")

    return [{"name": t, "source": s} for t, s in by.items()]
