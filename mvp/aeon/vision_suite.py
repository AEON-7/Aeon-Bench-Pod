"""The VISION suite — all Tier-0 programmatic (judge-free) on synthetic stimuli
with machine-known ground truth, plus one fully-Tier-0-shadowed Tier-1 grounding
case. See docs/multimodal/04-mvp-vision-plan.md.

v2 keeps every aeon-mvp-vision case intact (they carry over as the `easy` tier,
regenerable byte-for-byte for historical comparability) and adds medium/hard/expert
cases built to actually separate models: cluttered counting under occlusion,
rotated/noisy small-text OCR, multi-step spatial (size comparison + relative
position), multi-series charts with decoy annotations, fine color discrimination,
compositional icon-grid relations, next-in-pattern sequences, and keyword-checked
scene descriptions (flexible synonym matching, still deterministic — see the
keyword_* checkers in evaluators.py). Every image is a pure function of its case
spec; all RNG is seeded from the case id (_seed), so the suite is regenerable.

Each case carries `images` (list of imagegen specs), a `prompt` that instructs a
fenced answer slot, an `eval` (slot-strict or keyword checker), a `difficulty`
tier, and `requires` ∈ {vision_ok, multi_image_ok, ocr_ok} for sub-gating.
"""
from __future__ import annotations

import hashlib
import json

from . import imagegen

CATEGORIES_VISION = ["OCR", "Counting", "Color", "Spatial", "Relational",
                     "ChartQA", "VQA", "Detail", "MultiImage", "Grounding",
                     "Pattern", "Scene"]
SUITE_ID = "aeon-vision-v2"    # v2: + medium/hard/expert tiers; aeon-mvp-vision cases carry over
DIFFICULTIES = ["easy", "medium", "hard", "expert"]


def _seed(cid):
    """Deterministic per-case RNG seed — every stochastic generator arg derives from the
    case id, so the whole suite regenerates identically anywhere."""
    return int(hashlib.sha256(cid.encode()).hexdigest()[:8], 16)


# 1-based closed sets for "which numbered patch differs" questions
_NUMS = lambda n: [str(i) for i in range(1, n + 1)]  # noqa: E731

CASES = [
    {"id": "vision.ocr.token", "category": "OCR", "tier": 0, "difficulty": "easy", "requires": "ocr_ok",
     "images": [{"gen": "token", "args": {"text": "AEON"}}],
     "prompt": "Read the text in the image. Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "AEON", "threshold": 0.10}]}},

    {"id": "vision.count.circles", "category": "Counting", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "shapes", "args": {"n": 4, "color": "blue", "shape": "circle"}}],
     "prompt": "How many circles are in the image? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 4}]}},

    {"id": "vision.color.square", "category": "Color", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "solid_square", "args": {"color": "red"}}],
     "prompt": "What color fills the image? Reply with ONLY <answer>COLOR</answer> "
               "using one of: red, blue, green, yellow.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["red", "blue", "green", "yellow"], "answer": "red"}]}},

    {"id": "vision.spatial.quadrant", "category": "Spatial", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "positioned", "args": {"shape": "square", "quadrant": "top-left", "color": "green"}}],
     "prompt": "In which quadrant is the square? Reply with ONLY <answer>X</answer> using "
               "one of: top-left, top-right, bottom-left, bottom-right.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["top-left", "top-right", "bottom-left", "bottom-right"],
                            "answer": "top-left"}]}},

    {"id": "vision.relation.leftof", "category": "Relational", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "two_shapes", "args": {"left_color": "red", "left_shape": "circle",
                                               "right_color": "blue", "right_shape": "square"}}],
     "prompt": "Which object is on the LEFT? Reply with ONLY <answer>X</answer> using "
               "one of: red circle, blue square.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["red circle", "blue square"], "answer": "red circle"}]}},

    {"id": "vision.chart.maxbar", "category": "ChartQA", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "bar_chart", "args": {"labels": ["A", "B", "C"], "values": [3, 7, 5]}}],
     "prompt": "In the bar chart, which bar is tallest? Reply with ONLY <answer>X</answer> "
               "using one of: A, B, C.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["A", "B", "C"], "answer": "B"}]}},

    {"id": "vision.chart.value", "category": "ChartQA", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "bar_chart", "args": {"labels": ["A", "B", "C"], "values": [3, 7, 5]}}],
     "prompt": "In the bar chart, what is the height/value of bar A? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 3}]}},

    {"id": "vision.vqa.mcq", "category": "VQA", "tier": 0, "difficulty": "easy", "requires": "vision_ok",
     "images": [{"gen": "shapes", "args": {"n": 2, "color": "green", "shape": "triangle"}}],
     "prompt": "What shape are the objects in the image? Reply with ONLY <answer>X</answer> "
               "using one of: circle, square, triangle.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["circle", "square", "triangle"], "answer": "triangle"}]}},

    {"id": "vision.detail.tiny", "category": "Detail", "tier": 0, "difficulty": "easy", "requires": "ocr_ok",
     "images": [{"gen": "fine_detail", "args": {"big": "HELLO", "tiny": "k7"}}],
     "prompt": "There is a small piece of text in the bottom-right corner. Read it. "
               "Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "k7", "threshold": 0.25}]}},

    {"id": "vision.multi.morecount", "category": "MultiImage", "tier": 0, "difficulty": "easy", "requires": "multi_image_ok",
     "images": [{"gen": "shapes", "args": {"n": 2, "color": "purple", "shape": "circle"}},
                {"gen": "shapes", "args": {"n": 5, "color": "purple", "shape": "circle"}}],
     "prompt": "You are shown two images. Which image has MORE circles? Reply with ONLY "
               "<answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "second"}]}},

    # Tier-1 grounding — every criterion is Tier-0-shadowed (closed_set), so no
    # model judge is invoked and it stays composite-eligible on a single-family fleet.
    {"id": "vision.describe.scene", "category": "Grounding", "tier": 1, "difficulty": "easy",
     "requires": "vision_ok",
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

    # ================= v2 medium/hard/expert tiers (aeon-vision-v2) =================
    # Emergent answers (icon grids) were derived by RUNNING the seeded generator and are
    # pinned here; test_vision_suite.py re-derives every one and fails on any drift.

    # ---- Counting: cluttered fields, distractors, occlusion via z-order ----
    {"id": "vision.count.cluttered_tri", "category": "Counting", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "cluttered_shapes",
                 "args": {"target_shape": "triangle", "target_color": "red", "n_targets": 8,
                          "seed": _seed("vision.count.cluttered_tri")}}],
     "prompt": "The image is a cluttered field of overlapping shapes. Count ONLY the RED "
               "TRIANGLES — ignore red squares, blue triangles and every other shape. "
               "Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 8}]}},

    {"id": "vision.count.cluttered_tri_hard", "category": "Counting", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "cluttered_shapes",
                 "args": {"target_shape": "triangle", "target_color": "red", "n_targets": 11,
                          "distractors": [["square", "red", 9], ["triangle", "blue", 9],
                                          ["circle", "green", 6], ["circle", "orange", 5]],
                          "size": 460, "seed": _seed("vision.count.cluttered_tri_hard")}}],
     "prompt": "The image is a densely cluttered field of overlapping shapes. Count ONLY the "
               "RED TRIANGLES — ignore red squares, blue triangles and every other shape. "
               "Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 11}]}},

    {"id": "vision.count.occluded_circles", "category": "Counting", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "cluttered_shapes",
                 "args": {"target_shape": "circle", "target_color": "blue", "n_targets": 9,
                          "distractors": [["square", "gray", 8], ["circle", "green", 6],
                                          ["triangle", "blue", 7]],
                          "size": 440, "seed": _seed("vision.count.occluded_circles")}}],
     "prompt": "The image contains overlapping shapes; some are partially hidden behind others. "
               "Count ONLY the BLUE CIRCLES — ignore blue triangles, green circles and every "
               "other shape. Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 9}]}},

    # ---- OCR: small text under rotation + speckle noise ----
    {"id": "vision.ocr.rotated", "category": "OCR", "tier": 0,
     "difficulty": "medium", "requires": "ocr_ok",
     "images": [{"gen": "rotated_text",
                 "args": {"text": "KRJ4-VX7Q", "angle": 15, "fontsize": 20, "noise": 0.05,
                          "seed": _seed("vision.ocr.rotated")}}],
     "prompt": "The noisy image contains one short code printed at an angle. Read it exactly "
               "(letters, digits and the hyphen). Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "KRJ4-VX7Q",
                            "threshold": 0.15}]}},

    {"id": "vision.ocr.tiny_noise", "category": "OCR", "tier": 0,
     "difficulty": "expert", "requires": "ocr_ok",
     "images": [{"gen": "rotated_text",
                 "args": {"text": "w3f9-hq62", "angle": -15, "fontsize": 12, "noise": 0.09,
                          "seed": _seed("vision.ocr.tiny_noise")}}],
     "prompt": "The noisy image contains one small lowercase code printed at an angle. Read it "
               "exactly (letters, digits and the hyphen). Reply with ONLY <ocr>TEXT</ocr>.",
     "eval": {"checkers": [{"type": "cer_threshold", "slot": "ocr", "value": "w3f9-hq62",
                            "threshold": 0.25}]}},

    # ---- Spatial: multi-step (size comparison + relative position) ----
    {"id": "vision.spatial.leftof_largest", "category": "Spatial", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "size_relation",
                 "args": {"sizes": [28, 44, 20, 36], "colors": ["red", "blue", "green", "yellow"],
                          "extreme": "largest", "seed": _seed("vision.spatial.leftof_largest")}}],
     "prompt": "Each gray circle has a small colored square directly to its LEFT. Find the "
               "LARGEST circle. What color is the square directly to its left? Reply with ONLY "
               "<answer>COLOR</answer> using one of: red, blue, green, yellow.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["red", "blue", "green", "yellow"], "answer": "blue"}]}},

    {"id": "vision.spatial.leftof_smallest", "category": "Spatial", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "size_relation",
                 "args": {"sizes": [30, 22, 38, 26, 34],
                          "colors": ["purple", "orange", "red", "green", "blue"],
                          "extreme": "smallest", "seed": _seed("vision.spatial.leftof_smallest")}}],
     "prompt": "Each gray circle has a small colored square directly to its LEFT. The circles "
               "differ only slightly in size. Find the SMALLEST circle. What color is the square "
               "directly to its left? Reply with ONLY <answer>COLOR</answer> using one of: "
               "purple, orange, red, green, blue.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["purple", "orange", "red", "green", "blue"],
                            "answer": "orange"}]}},

    # ---- ChartQA: multi-series + legend + gridlines + a decoy annotation ----
    {"id": "vision.chart.series_value", "category": "ChartQA", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "series_chart", "args": {}}],
     "prompt": "The grouped bar chart has a legend and gridlines; ignore any text annotations. "
               "What is the value of series 'beta' at Q3? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 7}]}},

    {"id": "vision.chart.series_value_hard", "category": "ChartQA", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "series_chart",
                 "args": {"labels": ["Jan", "Feb", "Mar", "Apr", "May"],
                          "series": [["north", [5, 3, 6, 4, 7]], ["south", [4, 6, 3, 7, 5]],
                                     ["west", [6, 4, 5, 3, 4]]],
                          "decoy": "target 6", "y_max": 8}}],
     "prompt": "The grouped bar chart has three series (see the legend) and gridlines; ignore "
               "any text annotations. What is the value of series 'south' at Apr? Reply with "
               "ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 7}]}},

    # ---- Color: fine discrimination between near-identical hues ----
    {"id": "vision.color.odd_patch", "category": "Color", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "color_patches",
                 "args": {"n": 6, "base": [208, 64, 48], "delta": [0, 26, 0], "odd_index": 4}}],
     "prompt": "The numbered patches are almost the same color — exactly ONE differs slightly. "
               "Which patch number is different? Reply with ONLY <answer>N</answer> using one "
               "of: 1, 2, 3, 4, 5, 6.",
     "eval": {"checkers": [{"type": "closed_set", "options": _NUMS(6), "answer": "5"}]}},

    {"id": "vision.color.odd_patch_hard", "category": "Color", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "color_patches",
                 "args": {"n": 8, "base": [70, 110, 190], "delta": [0, 0, -24], "odd_index": 2}}],
     "prompt": "The numbered patches are almost the same color — exactly ONE differs slightly. "
               "Which patch number is different? Reply with ONLY <answer>N</answer> using one "
               "of: 1, 2, 3, 4, 5, 6, 7, 8.",
     "eval": {"checkers": [{"type": "closed_set", "options": _NUMS(8), "answer": "3"}]}},

    # ---- Relational/Counting: compositional questions on a seeded icon grid ----
    {"id": "vision.relation.rows_star_heart", "category": "Relational", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "icon_grid",
                 "args": {"rows": 5, "cols": 5, "icons": ["star", "heart", "circle", "square"],
                          "seed": _seed("vision.relation.rows_star_heart")}}],
     "prompt": "The image is a 5x5 grid of icons (stars, hearts, circles, squares). How many "
               "ROWS contain at least one star AND at least one heart? Reply with ONLY "
               "<count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 2}]}},   # pinned from the seeded grid

    {"id": "vision.relation.cols_heart", "category": "Relational", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "icon_grid",
                 "args": {"rows": 5, "cols": 5, "icons": ["star", "heart", "circle", "square"],
                          "seed": _seed("vision.relation.cols_heart")}}],
     "prompt": "The image is a 5x5 grid of icons (stars, hearts, circles, squares). How many "
               "COLUMNS contain at least one heart? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 4}]}},   # pinned from the seeded grid

    {"id": "vision.count.grid_stars", "category": "Counting", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "icon_grid",
                 "args": {"rows": 5, "cols": 5, "icons": ["star", "heart", "circle", "square"],
                          "seed": _seed("vision.count.grid_stars")}}],
     "prompt": "The image is a 5x5 grid of icons (stars, hearts, circles, squares). How many "
               "STARS are there in total? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 7}]}},   # pinned from the seeded grid

    # ---- Pattern: next-in-sequence (closed set) ----
    {"id": "vision.pattern.next_shape", "category": "Pattern", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "shape_sequence",
                 "args": {"pattern": ["circle", "square", "triangle"], "repeats": 3,
                          "color": "purple"}}],
     "prompt": "The shapes form a repeating pattern; the last position is a '?' box. Which "
               "shape belongs under the '?' to continue the pattern? Reply with ONLY "
               "<answer>X</answer> using one of: circle, square, triangle.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["circle", "square", "triangle"], "answer": "triangle"}]}},

    {"id": "vision.pattern.next_color", "category": "Pattern", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "color_sequence",
                 "args": {"pattern": ["yellow", "purple", "purple", "orange"], "repeats": 2,
                          "shape": "circle"}}],
     "prompt": "The colored circles form a repeating color pattern; the last position is a '?' "
               "box. Which color belongs under the '?' to continue the pattern? Reply with ONLY "
               "<answer>COLOR</answer> using one of: yellow, purple, orange, red.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["yellow", "purple", "orange", "red"],
                            "answer": "orange"}]}},

    # ---- Scene: free-form descriptions validated by FLEXIBLE keyword checkers ----
    # (synonym groups, word-boundary matching — see evaluators.keyword_*; questions are
    # phrased so negation/binding never decides correctness)
    {"id": "vision.scene.keywords", "category": "Scene", "tier": 0,
     "difficulty": "medium", "requires": "vision_ok",
     "images": [{"gen": "scene", "args": {"house_color": "red", "car_color": "blue",
                                          "seed": _seed("vision.scene.keywords")}}],
     "prompt": "Describe this scene in ONE short sentence, naming every object you see. "
               "Reply with ONLY <answer>your sentence</answer>.",
     "eval": {"combine": "fraction", "checkers": [    # partial credit per named object
         {"type": "keyword_any", "keywords": ["house", "building", "cottage", "home", "cabin"]},
         {"type": "keyword_any", "keywords": ["tree", "bush", "plant"]},
         {"type": "keyword_any", "keywords": ["car", "vehicle", "automobile", "van", "truck"]},
     ]}},

    {"id": "vision.scene.car", "category": "Scene", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "scene", "args": {"house_color": "yellow", "car_color": "green",
                                          "seed": _seed("vision.scene.car")}}],
     "prompt": "Look at the vehicle in the image. In ONE short phrase say what it is and what "
               "color it is, e.g. <answer>a purple truck</answer>. Reply with ONLY "
               "<answer>...</answer>.",
     "eval": {"checkers": [{"type": "keyword_all",
                            "groups": [["car", "vehicle", "automobile", "van", "truck"],
                                       ["green", "emerald"]]}]}},

    {"id": "vision.scene.order", "category": "Scene", "tier": 0,
     "difficulty": "hard", "requires": "vision_ok",
     "images": [{"gen": "scene", "args": {"house_color": "purple", "car_color": "orange",
                                          "seed": _seed("vision.scene.order")}}],
     "prompt": "Name the three objects in the image from LEFT to RIGHT, in order, e.g. "
               "<answer>boat, lamp, dog</answer>. Reply with ONLY <answer>...</answer>.",
     "eval": {"checkers": [{"type": "ordered_keywords",
                            "groups": [["house", "building", "cottage", "home", "cabin"],
                                       ["tree", "bush", "plant"],
                                       ["car", "vehicle", "automobile", "van", "truck"]]}]}},

    # ---- MultiImage: what changed between two frames ----
    {"id": "vision.multi.moved", "category": "MultiImage", "tier": 0,
     "difficulty": "medium", "requires": "multi_image_ok",
     "images": [{"gen": "positioned", "args": {"shape": "square", "quadrant": "top-left", "color": "green"}},
                {"gen": "positioned", "args": {"shape": "square", "quadrant": "bottom-right", "color": "green"}}],
     "prompt": "You are shown two images of the same scene; the square moved between the first "
               "and the second image. In which quadrant is the square in the SECOND image? "
               "Reply with ONLY <answer>X</answer> using one of: top-left, top-right, "
               "bottom-left, bottom-right.",
     "eval": {"checkers": [{"type": "closed_set",
                            "options": ["top-left", "top-right", "bottom-left", "bottom-right"],
                            "answer": "bottom-right"}]}},
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
        "difficulties": DIFFICULTIES,
        "cases": [{"id": c["id"], "category": c["category"], "tier": c["tier"],
                   "difficulty": c.get("difficulty"), "requires": c["requires"],
                   "n_images": len(c["images"])} for c in CASES],
    }
