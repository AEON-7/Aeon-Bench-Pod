"""The MVP VISION suite — all Tier-0 programmatic (judge-free) on synthetic
stimuli with machine-known ground truth, plus one fully-Tier-0-shadowed Tier-1
grounding case. See docs/multimodal/04-mvp-vision-plan.md.

Each case carries `images` (list of imagegen specs), a `prompt` that instructs a
fenced answer slot, an `eval` (slot-strict checker), and `requires` ∈
{vision_ok, multi_image_ok, ocr_ok} for sub-gating.
"""
from __future__ import annotations

import hashlib
import json

from . import imagegen

CATEGORIES_VISION = ["OCR", "Counting", "Color", "Spatial", "Relational",
                     "ChartQA", "VQA", "Detail", "MultiImage", "Grounding"]
SUITE_ID = "aeon-mvp-vision"

CASES = [
    {"id": "vision.ocr.token", "category": "OCR", "tier": 0, "requires": "ocr_ok",
     "images": [{"gen": "token", "args": {"text": "AEON"}}],
     "prompt": "Read the text in the image. Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "AEON", "threshold": 0.10}]}},

    {"id": "vision.count.circles", "category": "Counting", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "shapes", "args": {"n": 4, "color": "blue", "shape": "circle"}}],
     "prompt": "How many circles are in the image? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 4}]}},

    {"id": "vision.color.square", "category": "Color", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "solid_square", "args": {"color": "red"}}],
     "prompt": "What color fills the image? Reply with ONLY <answer>COLOR</answer> "
               "using one of: red, blue, green, yellow.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["red", "blue", "green", "yellow"], "answer": "red"}]}},

    {"id": "vision.spatial.quadrant", "category": "Spatial", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "positioned", "args": {"shape": "square", "quadrant": "top-left", "color": "green"}}],
     "prompt": "In which quadrant is the square? Reply with ONLY <answer>X</answer> using "
               "one of: top-left, top-right, bottom-left, bottom-right.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["top-left", "top-right", "bottom-left", "bottom-right"],
                            "answer": "top-left"}]}},

    {"id": "vision.relation.leftof", "category": "Relational", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "two_shapes", "args": {"left_color": "red", "left_shape": "circle",
                                               "right_color": "blue", "right_shape": "square"}}],
     "prompt": "Which object is on the LEFT? Reply with ONLY <answer>X</answer> using "
               "one of: red circle, blue square.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["red circle", "blue square"], "answer": "red circle"}]}},

    {"id": "vision.chart.maxbar", "category": "ChartQA", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "bar_chart", "args": {"labels": ["A", "B", "C"], "values": [3, 7, 5]}}],
     "prompt": "In the bar chart, which bar is tallest? Reply with ONLY <answer>X</answer> "
               "using one of: A, B, C.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["A", "B", "C"], "answer": "B"}]}},

    {"id": "vision.chart.value", "category": "ChartQA", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "bar_chart", "args": {"labels": ["A", "B", "C"], "values": [3, 7, 5]}}],
     "prompt": "In the bar chart, what is the height/value of bar A? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 3}]}},

    {"id": "vision.vqa.mcq", "category": "VQA", "tier": 0, "requires": "vision_ok",
     "images": [{"gen": "shapes", "args": {"n": 2, "color": "green", "shape": "triangle"}}],
     "prompt": "What shape are the objects in the image? Reply with ONLY <answer>X</answer> "
               "using one of: circle, square, triangle.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["circle", "square", "triangle"], "answer": "triangle"}]}},

    {"id": "vision.detail.tiny", "category": "Detail", "tier": 0, "requires": "ocr_ok",
     "images": [{"gen": "fine_detail", "args": {"big": "HELLO", "tiny": "k7"}}],
     "prompt": "There is a small piece of text in the bottom-right corner. Read it. "
               "Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "k7", "threshold": 0.25}]}},

    {"id": "vision.multi.morecount", "category": "MultiImage", "tier": 0, "requires": "multi_image_ok",
     "images": [{"gen": "shapes", "args": {"n": 2, "color": "purple", "shape": "circle"}},
                {"gen": "shapes", "args": {"n": 5, "color": "purple", "shape": "circle"}}],
     "prompt": "You are shown two images. Which image has MORE circles? Reply with ONLY "
               "<answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "second"}]}},

    # Tier-1 grounding — every criterion is Tier-0-shadowed (closed_set), so no
    # model judge is invoked and it stays composite-eligible on a single-family fleet.
    {"id": "vision.describe.scene", "category": "Grounding", "tier": 1, "requires": "vision_ok",
     "images": [{"gen": "positioned", "args": {"shape": "circle", "quadrant": "top-right", "color": "red"}}],
     "prompt": "Describe the single shape in the image. Reply with ONLY "
               "<object>SHAPE</object><color>COLOR</color>.",
     "eval": {"rubric": [
         {"id": "r1", "question": "Is the object a circle?", "required": True,
          "tier0_check": {"type": "closed_set", "slot": "object",
                          "options": ["circle", "square", "triangle"], "answer": "circle"}},
         {"id": "r2", "question": "Is the color red?",
          "tier0_check": {"type": "closed_set", "slot": "color",
                          "options": ["red", "blue", "green"], "answer": "red"}},
     ]}},
]


def suite_hash():
    # fold the pinned image bytes (sha) + the eval specs
    parts = []
    for c in CASES:
        shas = [imagegen.generate(s)[0] for s in c["images"]]
        parts.append({"id": c["id"], "shas": shas, "eval": c["eval"]})
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def summary():
    return {
        "suite_id": SUITE_ID,
        "suite_hash": suite_hash(),
        "n_cases": len(CASES),
        "categories": CATEGORIES_VISION,
        "cases": [{"id": c["id"], "category": c["category"], "tier": c["tier"],
                   "requires": c["requires"], "n_images": len(c["images"])} for c in CASES],
    }
