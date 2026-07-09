"""The MVP suite — small but discriminating, and (near-)deterministic in outcome.

Every Tier-0 case is a pure programmatic check. The one Tier-1 case shows the
binary-rubric judge path: two criteria are Tier-0-shadowed (program-decided),
one is left to the (self-)judge. See DESIGN §6 / §6b.
"""
from __future__ import annotations

import hashlib
import json
import os

_BUILTIN = [
    # ---- Math (Tier 0) ----
    {"id": "math.mul", "category": "Math", "tier": 0,
     "prompt": "Compute 17 * 23. Give only the final number inside \\boxed{}.",
     "eval": {"checkers": [{"type": "numeric_tolerance", "value": "391"}]}},
    {"id": "math.div", "category": "Math", "tier": 0,
     "prompt": "What is 144 / 12? Put the final answer inside \\boxed{}.",
     "eval": {"checkers": [{"type": "numeric_tolerance", "value": "12"}]}},
    {"id": "math.quad", "category": "Math", "tier": 0,
     "prompt": "Solve x^2 + 2x - 15 = 0. Put the two roots inside \\boxed{} as a comma-separated list.",
     "eval": {"checkers": [{"type": "numeric_tolerance", "value": "3, -5", "as_set": True}]}},

    # ---- Instruction-following (Tier 0) ----
    {"id": "if.pong", "category": "Instruction", "tier": 0,
     "prompt": "Respond with exactly the single word PONG and nothing else.",
     "eval": {"checkers": [{"type": "exact_match", "value": "PONG"}]}},
    {"id": "if.three_colors", "category": "Instruction", "tier": 0,
     "prompt": "List exactly three colors, one per line, with no other text.",
     "eval": {"checkers": [{"type": "structural_count", "unit": "line", "op": "==", "n": 3}]}},
    {"id": "if.no_e", "category": "Instruction", "tier": 0,
     "prompt": "Write one short sentence that does not contain the letter e. Output only the sentence.",
     "eval": {"checkers": [{"type": "regex_constraint", "pattern": "e", "mode": "must_not_match"}]}},

    # ---- Reasoning (Tier 0, answer-gated) ----
    {"id": "reason.syllogism", "category": "Reasoning", "tier": 0,
     "prompt": "If all bloops are razzies and all razzies are lazzies, are all bloops lazzies? "
               "Answer yes or no inside \\boxed{}.",
     "eval": {"checkers": [{"type": "regex_constraint", "pattern": r"\\boxed\{\s*yes\s*\}"}]}},
    {"id": "reason.batball", "category": "Reasoning", "tier": 0,
     "prompt": "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
               "How many cents does the ball cost? Put the number inside \\boxed{}.",
     "eval": {"checkers": [{"type": "numeric_tolerance", "value": "5"}]}},

    # ---- Coding (Tier 0, executed) ----
    {"id": "code.add", "category": "Coding", "tier": 0,
     "prompt": "Write a Python function named add(a, b) that returns their sum. "
               "Output only a single Python code block.",
     "eval": {"checkers": [{"type": "unit_test",
                            "test": "assert add(2, 3) == 5\nassert add(-1, 1) == 0\nassert add(10, 20) == 30"}]}},
    {"id": "code.palindrome", "category": "Coding", "tier": 0,
     "prompt": "Write a Python function is_palindrome(s) that returns True if s reads the same "
               "forwards and backwards, ignoring case. Output only a single Python code block.",
     "eval": {"checkers": [{"type": "unit_test",
                            "test": "assert is_palindrome('Racecar') is True\nassert is_palindrome('hello') is False"}]}},

    # ---- Prose / Creativity (Tier 1 — binary rubric; r1,r2 shadowed, r3 self-judged) ----
    {"id": "prose.ocean3", "category": "Prose", "tier": 1,
     "prompt": "Write a 3-line poem about the ocean. Output only the poem.",
     "eval": {"rubric": [
         {"id": "r1", "question": "Does the response have exactly 3 non-empty lines?",
          "decision_rule": "Count non-empty lines; true iff exactly 3.", "required": True,
          "tier0_check": {"type": "structural_count", "unit": "line", "op": "==", "n": 3}},
         {"id": "r2", "question": "Does the poem mention the ocean/sea/waves/tide?",
          "decision_rule": "True iff it matches ocean|sea|wave|tide|surf (case-insensitive).",
          "tier0_check": {"type": "regex_constraint", "pattern": r"ocean|sea|wave|tide|surf"}},
         {"id": "r3", "question": "Is the text written as a poem (short lyrical lines) rather than a prose paragraph?",
          "decision_rule": "True iff it reads as verse/short lines, false if it is one running paragraph."},
     ]}},
]

# The corpus (generated + adversarially verified; every gold answer independently
# re-derived and executed through the real evaluators before admission) lives in
# suites/cases.json — built by suites/build_all_cases.py from suites/v3/*.json.
# v3 design: 155 cases, 5 categories x 6 difficulty tiers with an exponential hard
# skew plus a mandatory GOD MODE sentinel (easy 2 / medium 3 / hard 5 / expert 8 /
# frontier 12 / god_mode 1 per category). easy+medium are CANARIES (is the model
# configured right / can a weak model score at all); the ranking signal lives in
# hard->god_mode, which is most of the suite by count.
# When the corpus is healthy it IS the whole suite (exactly 150). The tiny _BUILTIN
# set is a FALLBACK only — a missing/malformed corpus degrades to built-ins so the
# mock pipeline still works, never a half-merged hybrid.
_CASES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "suites", "cases.json")
_REQUIRED = ("id", "category", "tier", "prompt", "eval")


def _load_cases():
    try:
        if os.path.exists(_CASES_FILE):
            with open(_CASES_FILE, "r", encoding="utf-8") as f:
                corpus = json.load(f)
            cases, seen = [], set()
            for c in corpus:
                if not isinstance(c, dict) or not all(k in c for k in _REQUIRED):
                    continue
                if c["id"] in seen:
                    continue
                seen.add(c["id"])
                cases.append(c)
            if cases:
                return cases
    except Exception:
        pass  # never let a bad corpus file break the server — fall back to built-ins
    return list(_BUILTIN)


CASES = _load_cases()

# fixed order drives the radar axes; every generated category maps to one of these
CATEGORIES = ["Math", "Instruction", "Reasoning", "Coding", "Prose"]
SUITE_ID = "aeon-suite-v3" if len(CASES) > len(_BUILTIN) else "aeon-mvp-mini"


DIFFICULTIES = ["easy", "medium", "hard", "expert", "frontier", "god_mode"]


def _grid():
    """(category, difficulty) -> [case] over the graded corpus. Built-ins that carry no
    `difficulty` are excluded; the fast bench samples the graded grid only."""
    g = {}
    for c in CASES:
        d = c.get("difficulty")
        if d in DIFFICULTIES:
            g.setdefault((c["category"], d), []).append(c)
    return g


def sample_fast(seed, per_cell=1):
    """Deterministic 'fast bench' draw: `per_cell` case(s) from each (category x
    difficulty) cell, selected by `seed`. Same seed + same suite_hash => the identical
    question set for every model => a true A/B comparison. Cases come back in a stable
    category-then-tier order. Per-cell seeding (seed|cat|tier) keeps a cell's pick stable
    even when an unrelated cell gains cases."""
    import random
    g, out = _grid(), []
    for cat in CATEGORIES:
        for d in DIFFICULTIES:
            pool = sorted(g.get((cat, d), []), key=lambda c: c["id"])
            if not pool:
                continue
            rng = random.Random(f"{seed}|{cat}|{d}")
            out.extend(rng.sample(pool, min(per_cell, len(pool))))
    return out


def random_seed():
    """A short, human-friendly seed (8 hex chars) for a fresh fast-bench draw."""
    return hashlib.sha256(os.urandom(16)).hexdigest()[:8]


def suite_hash():
    blob = json.dumps(CASES, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def summary():
    return {
        "suite_id": SUITE_ID,
        "suite_hash": suite_hash(),
        "n_cases": len(CASES),
        "categories": CATEGORIES,
        "cases": [{"id": c["id"], "category": c["category"], "tier": c["tier"]} for c in CASES],
    }
