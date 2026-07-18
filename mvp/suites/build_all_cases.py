"""build_all_cases.py — assemble + validate suites/cases.json from the verified corpus.

    python suites/build_all_cases.py            (from mvp/, or anywhere)

The v4 corpus lives as per-cell files in suites/v4/final_<category>_<difficulty>.json —
each authored by a generator agent and then ADVERSARIALLY VERIFIED (every gold answer
independently re-derived; the verifier's own solution executed through the real
aeon.evaluators and required to score 1.0) before landing here. v4 carries v3's
easy/medium/hard cells UNCHANGED (they are canaries) and dials up only the ranking
tiers: brand-new expert/frontier cells plus a second god_mode sentinel per category.
This script is the single, reproducible path from those files to the cases.json the
suite loads:

  1. loads every suites/v4/final_*.json
  2. validates schema, id uniqueness, category/difficulty enums, checker types
  3. enforces the v4 design: 5 categories x (easy 2 / medium 3 / hard 5 / expert 8 /
     frontier 12 / god_mode 2) = exactly 160 cases
  4. smoke-tests every case through evaluators.evaluate() with a dummy answer
     (no checker may crash on arbitrary input; score just has to come back 0/None-ish)
  5. writes suites/cases.json (sorted by category, difficulty rank, id — stable diffs)

Fails loudly on ANY violation — a bad corpus must never ship silently.
"""
from __future__ import annotations

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # .../mvp/suites
MVP = os.path.dirname(HERE)                                  # .../mvp
sys.path.insert(0, MVP)

from aeon import evaluators  # noqa: E402  (the REAL checkers — smoke-test target)

OUT = os.path.join(HERE, "cases.json")
SRC = os.path.join(HERE, "v4")
CATEGORIES = ["Math", "Instruction", "Reasoning", "Coding", "Prose"]
DIFF_COUNTS = {"easy": 2, "medium": 3, "hard": 5, "expert": 8, "frontier": 12, "god_mode": 2}
DIFF_RANK = {d: i for i, d in enumerate(DIFF_COUNTS)}
REQUIRED = ("id", "category", "tier", "prompt", "eval")
KNOWN_CHECKERS = set(evaluators.CHECKERS)


def fail(msg):
    print(f"BUILD FAILED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main():
    files = sorted(glob.glob(os.path.join(SRC, "final_*.json")))
    if not files:
        fail(f"no corpus files under {SRC}")
    cases, seen = [], set()
    for path in files:
        with open(path, "r", encoding="utf-8") as f:
            cell = json.load(f)
        if not isinstance(cell, list):
            fail(f"{os.path.basename(path)}: not a JSON array")
        cases.extend(cell)

    counts = {}
    for c in cases:
        for k in REQUIRED:
            if k not in c:
                fail(f"{c.get('id', '<no id>')}: missing field {k!r}")
        if c["id"] in seen:
            fail(f"duplicate id {c['id']}")
        seen.add(c["id"])
        if c["category"] not in CATEGORIES:
            fail(f"{c['id']}: unknown category {c['category']!r}")
        d = c.get("difficulty")
        if d not in DIFF_COUNTS:
            fail(f"{c['id']}: unknown difficulty {d!r}")
        if c["tier"] not in (0, 1):
            fail(f"{c['id']}: tier must be 0 or 1")
        ev = c["eval"]
        if c["tier"] == 0:
            for chk in ev.get("checkers") or []:
                if chk.get("type") not in KNOWN_CHECKERS:
                    fail(f"{c['id']}: unknown checker type {chk.get('type')!r}")
            if not ev.get("checkers"):
                fail(f"{c['id']}: tier-0 case with no checkers")
        else:
            rub = ev.get("rubric") or []
            if not rub:
                fail(f"{c['id']}: tier-1 case with no rubric")
            for crit in rub:
                # required criteria must be program-decided (tier0_check shadow), so
                # scores never depend on judge availability
                if crit.get("required") and not crit.get("tier0_check"):
                    fail(f"{c['id']}: required criterion {crit.get('id')!r} lacks tier0_check")
        counts[(c["category"], d)] = counts.get((c["category"], d), 0) + 1

    for cat in CATEGORIES:
        for d, want in DIFF_COUNTS.items():
            got = counts.get((cat, d), 0)
            if got != want:
                fail(f"cell {cat}/{d}: {got} cases, design wants {want}")
    if len(cases) != sum(DIFF_COUNTS.values()) * len(CATEGORIES):
        fail(f"total {len(cases)} != {sum(DIFF_COUNTS.values()) * len(CATEGORIES)}")

    # smoke-test: no checker may crash on arbitrary text (judge=None: subjective
    # tier-1 criteria return pending — that's fine, we only require no exception)
    for c in cases:
        try:
            evaluators.evaluate(c, "dummy probe answer 42\n\\boxed{0}", None)
        except Exception as e:
            fail(f"{c['id']}: evaluator crashed on dummy input: {type(e).__name__}: {e}")

    cases.sort(key=lambda c: (CATEGORIES.index(c["category"]), DIFF_RANK[c["difficulty"]], c["id"]))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
        f.write("\n")
    by_diff = {}
    for c in cases:
        by_diff[c["difficulty"]] = by_diff.get(c["difficulty"], 0) + 1
    print(f"OK: wrote {len(cases)} cases -> {OUT}")
    print("   per difficulty:", " ".join(f"{d}={by_diff.get(d, 0)}" for d in DIFF_COUNTS))


if __name__ == "__main__":
    main()
