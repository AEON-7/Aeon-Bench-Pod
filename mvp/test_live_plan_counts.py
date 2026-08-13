"""Live progress bars must divide by the plan the run is ACTUALLY executing.

A GOD MODE run is the god_mode tier alone — 24 cases. The live view divided its per-category
progress by the whole 174-case suite, so a category that had finished every one of its cases read
"4 / 34" and every bar sat near 12% for the run's entire life. That is worse than showing nothing:
it tells an operator "barely started" about a run that is nearly done, which is exactly the
misreading the live view exists to prevent.

`--difficulty` filters by whole tiers (aeon_pod: `c["difficulty"] in want`), so a plan is always a
union of complete tiers and is recoverable from n_cases. These tests pin that recovery, and pin the
two ways it must REFUSE to guess: an ambiguous size, and a run from a different suite.

Run: python3 mvp/test_live_plan_counts.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_ROLE", "pod")
os.environ.setdefault("AEON_DB", os.path.join(BASE, ".test_live_plan.db"))

from aeon import app as A          # noqa: E402
from aeon import suite as S        # noqa: E402

PASS = 0


def ok(cond, label):
    global PASS
    if not cond:
        print("FAIL  %s" % label)
        sys.exit(1)
    PASS += 1
    print("ok    %s" % label)


FULL = A._suite_cat_counts()
TOTAL = len(S.CASES)

# ---- the suite is shaped the way the fix assumes ---------------------------------------------
per = A._difficulty_cat_counts()
ok(sum(FULL.values()) == TOTAL, "full counts sum to the suite (%d)" % TOTAL)
ok("god_mode" in per, "the suite has a god_mode tier")
GOD_N = sum(per["god_mode"].values())

# ---- a GOD MODE run divides by the god tier, not the suite ------------------------------------
god = A._plan_cat_counts(GOD_N, suite_id=S.SUITE_ID)
ok(sum(god.values()) == GOD_N, "god plan denominators sum to the god tier (%d)" % GOD_N)
ok(god != FULL, "god plan is NOT the full-suite counts")
ok(all(god[c] <= FULL[c] for c in god), "no category expects more than the suite holds")
# The bug in one assertion: a finished god category must be able to read 100%.
worst = max(god.values())
ok(worst <= 8, "a god category tops out in single digits (%d), so its bar can fill" % worst)

# ---- a full run is unchanged -------------------------------------------------------------------
ok(A._plan_cat_counts(TOTAL, suite_id=S.SUITE_ID) == FULL, "a full-suite run keeps full counts")
ok(A._plan_cat_counts(None) == FULL, "no n_cases -> full counts (nothing to infer from)")
ok(A._plan_cat_counts(TOTAL + 50, suite_id=S.SUITE_ID) == FULL,
   "a run bigger than the suite falls back rather than inventing")

# ---- it REFUSES to guess when the size is ambiguous --------------------------------------------
# 25 is both {hard} and {easy, medium}; their per-category splits differ, so a guess would be a
# fabricated denominator. Falling back is pessimistic but never wrong about what was planned.
sizes = {}
for d, cats in per.items():
    sizes.setdefault(sum(cats.values()), []).append(d)
ambiguous = None
import itertools  # noqa: E402
diffs = sorted(d for d in per if d)
for r in range(1, len(diffs) + 1):
    for combo in itertools.combinations(diffs, r):
        n = sum(sum(per[d].values()) for d in combo)
        hits = []
        for r2 in range(1, len(diffs) + 1):
            for c2 in itertools.combinations(diffs, r2):
                if sum(sum(per[d].values()) for d in c2) == n:
                    hits.append(c2)
        if len(hits) > 1:
            ambiguous = (n, hits)
            break
    if ambiguous:
        break

if ambiguous:
    n, hits = ambiguous
    ok(A._plan_cat_counts(n, suite_id=S.SUITE_ID) == FULL,
       "ambiguous size %d (%d subsets) falls back instead of guessing" % (n, len(hits)))
    # ...but a single observed case is enough to break the tie.
    pick = hits[0]
    resolved = A._plan_cat_counts(n, seen_difficulties=set(pick), suite_id=S.SUITE_ID)
    ok(sum(resolved.values()) == n,
       "observed difficulties %s resolve it to exactly %d" % (list(pick), n))
else:
    print("note  this suite has no ambiguous tier sum; tie-break path not exercised")

# ---- a DIFFERENT suite is never matched against text tiers -------------------------------------
# A vision run is 31 cases. If that ever coincides with a text tier sum, the text counts would be
# silently applied to a vision run.
ok(A._plan_cat_counts(GOD_N, suite_id="aeon-vision-v2") == FULL,
   "a run from another suite is never matched against text tiers")

# ---- every case maps to a difficulty ------------------------------------------------------------
cd = A._case_difficulty()
ok(len(cd) == TOTAL, "every suite case has a difficulty entry")
ok(all(v for v in cd.values()), "no case has an empty difficulty")
ok(A._plan_cat_counts(GOD_N, seen_difficulties={"god_mode"}, suite_id=S.SUITE_ID) == god,
   "observing god_mode agrees with the size-only inference")

print("\nOK  live plan denominators: %d checks passed" % PASS)
