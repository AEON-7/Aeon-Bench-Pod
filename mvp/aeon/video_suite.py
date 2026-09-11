"""The VIDEO suite — all Tier-0 programmatic (judge-free) on synthetic clips with
machine-known ground truth. Mirrors vision_suite/audio_suite for the VIDEO board.

Each case carries `video` (a list of videogen specs), a `prompt` that instructs a
fenced answer slot, an `eval` (slot-strict or keyword checker — ordered_keywords
covers temporal-order questions), a `difficulty` tier, and `requires` "video_ok"
for the probe gate (probe.probe_video). Clips are tiny deterministic MP4s; the
suite identity (suite_hash) folds the RAW-FRAME content address, so it never
drifts with the ffmpeg build (see videogen._finish).
"""
from __future__ import annotations

import hashlib
import json

from . import videogen

CATEGORIES_VIDEO = ["Counting", "Temporal", "Motion", "Objects", "Speed"]
SUITE_ID = "aeon-video-v1"
DIFFICULTIES = ["easy", "medium", "hard", "expert"]


def _seed(cid):
    """Deterministic per-case RNG seed, derived from the case id (same recipe as vision v2)."""
    return int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16)


CASES = [
    # ---- Motion: direction / size change (closed set) -----------------------
    {"id": "video.motion.right", "category": "Motion", "tier": 0,
     "difficulty": "easy", "requires": "video_ok",
     "video": [{"gen": "moving_square", "args": {"direction": "right", "color": "red"}}],
     "prompt": "The video shows one square moving in a straight line. In which direction does "
               "it move? Reply with ONLY <answer>X</answer> using one of: left, right, up, down.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["left", "right", "up", "down"], "answer": "right"}]}},

    {"id": "video.motion.up", "category": "Motion", "tier": 0,
     "difficulty": "medium", "requires": "video_ok",
     "video": [{"gen": "moving_square", "args": {"direction": "up", "color": "blue"}}],
     "prompt": "The video shows one square moving in a straight line. In which direction does "
               "it move? Reply with ONLY <answer>X</answer> using one of: left, right, up, down.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["left", "right", "up", "down"], "answer": "up"}]}},

    {"id": "video.motion.grow", "category": "Motion", "tier": 0,
     "difficulty": "medium", "requires": "video_ok",
     "video": [{"gen": "grow_shrink", "args": {"mode": "grow", "shape": "circle",
                                               "color": "orange"}}],
     "prompt": "Watch the shape over the whole clip. Does it get larger or smaller? Reply "
               "with ONLY <answer>X</answer> using one of: larger, smaller.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["larger", "smaller"], "answer": "larger"}]}},

    # ---- Temporal: which flashed first / full ordering ----------------------
    {"id": "video.temporal.first_flash", "category": "Temporal", "tier": 0,
     "difficulty": "medium", "requires": "video_ok",
     "video": [{"gen": "flash_sequence", "args": {"colors": ["green", "red", "yellow"]}}],
     "prompt": "A square flashes three different colors, one after another. Which color "
               "flashed FIRST? Reply with ONLY <answer>COLOR</answer> using one of: "
               "green, red, yellow.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["green", "red", "yellow"], "answer": "green"}]}},

    {"id": "video.temporal.flash_order", "category": "Temporal", "tier": 0,
     "difficulty": "hard", "requires": "video_ok",
     "video": [{"gen": "flash_sequence", "args": {"colors": ["red", "green", "blue"]}}],
     "prompt": "A square flashes three different colors, one after another. List the colors "
               "in the order they flashed, e.g. <answer>white, black, gray</answer>. Reply "
               "with ONLY <answer>...</answer>.",
     "eval": {"checkers": [{"type": "ordered_keywords",
                            "groups": [["red", "crimson"], ["green"], ["blue", "navy"]]}]}},

    # ---- Counting: events over time ------------------------------------------
    {"id": "video.count.blinks", "category": "Counting", "tier": 0,
     "difficulty": "medium", "requires": "video_ok",
     "video": [{"gen": "blink_square", "args": {"n_blinks": 5, "color": "purple"}}],
     "prompt": "The square blinks (appears and disappears) several times. How many times does "
               "it blink? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 5}]}},

    {"id": "video.count.crossings", "category": "Counting", "tier": 0,
     "difficulty": "hard", "requires": "video_ok",
     "video": [{"gen": "dot_crossings",
                "args": {"n_cross": 4, "n_stay": 3,
                         "seed": _seed("video.count.crossings")}}],
     "prompt": "Blue dots move around a fixed vertical line. Some dots CROSS the line from the "
               "left side to the right side; others never reach it. How many dots crossed the "
               "line? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 4}]}},

    # ---- Objects: appearance / disappearance over time -----------------------
    {"id": "video.objects.appeared", "category": "Objects", "tier": 0,
     "difficulty": "medium", "requires": "video_ok",
     "video": [{"gen": "appear_shape",
                "args": {"base_shape": "circle", "base_color": "blue",
                         "appear_shape_": "triangle", "appear_color": "green"}}],
     "prompt": "One shape is visible for the whole clip; near the END a second shape appears. "
               "What SHAPE is the one that appears? Reply with ONLY <answer>X</answer> using "
               "one of: circle, square, triangle.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["circle", "square", "triangle"], "answer": "triangle"}]}},

    {"id": "video.objects.disappeared", "category": "Objects", "tier": 0,
     "difficulty": "hard", "requires": "video_ok",
     "video": [{"gen": "disappear_shape", "args": {"vanish": "blue"}}],
     "prompt": "Three objects are visible at the start; partway through, one of them "
               "disappears. Which one? Reply with ONLY <answer>X</answer> using one of: "
               "red circle, blue square, green triangle.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["red circle", "blue square", "green triangle"],
                            "answer": "blue square"}]}},

    # ---- Speed: relative motion comparison ------------------------------------
    {"id": "video.speed.faster", "category": "Speed", "tier": 0,
     "difficulty": "hard", "requires": "video_ok",
     "video": [{"gen": "two_speeds",
                "args": {"fast": ["circle", "red"], "slow": ["square", "blue"]}}],
     "prompt": "Two shapes move from left to right at different speeds. Which one moves "
               "FASTER? Reply with ONLY <answer>X</answer> using one of: red circle, "
               "blue square.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["red circle", "blue square"], "answer": "red circle"}]}},
]


def suite_hash():
    # fold the pinned clip content-addresses (raw-frame sha) + the eval specs — same
    # recipe as vision/audio
    parts = []
    for c in CASES:
        shas = [videogen.generate(s)[0] for s in c["video"]]
        parts.append({"id": c["id"], "shas": shas, "eval": c["eval"]})
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def summary():
    return {
        "suite_id": SUITE_ID,
        "suite_hash": suite_hash(),
        "n_cases": len(CASES),
        "categories": CATEGORIES_VIDEO,
        "difficulties": DIFFICULTIES,
        "cases": [{"id": c["id"], "category": c["category"], "tier": c["tier"],
                   "difficulty": c.get("difficulty"), "requires": c["requires"],
                   "n_video": len(c["video"])} for c in CASES],
    }
