"""Self-test for the v4 TEXT suite corpus — runs GREEN locally, no GPU/model.

Guards the aeon-suite-v4 bump (2026-07):
  1. Corpus shape: 160 cases = 5 categories x (easy 2 / medium 3 / hard 5 /
     expert 8 / frontier 12 / god_mode 2); unique ids; SUITE_ID + stable hash.
  2. Carry-over contract: easy/medium/hard cells are v3's, byte-identical
     (v4 dialed up ONLY expert/frontier/god_mode — all-new v4.* top-tier cases).
  3. Every case's eval spec parses and each checker type exists in
     aeon.evaluators.CHECKERS; required rubric criteria are tier0-shadowed;
     evaluate() never crashes on arbitrary text (judge=None).
  4. Arena prompts: 24 new v4-* entries appended (8 per kind), no id
     collisions, total 140.
  5. Board fallback: 'aeon-suite-v3' heads _LEGACY_SUITES so the public board
     falls back to v3 runs until v4 runs exist.

Run:  python mvp/test_suite_v4.py
"""
import json
import os
import re
import sys
import tempfile

# Point the DB layer at a throwaway SQLite BEFORE importing aeon.* (db reads env at import).
os.environ.pop("AEON_DB_URL", None)                      # never touch the Postgres mothership
_TMP = tempfile.mkdtemp(prefix="aeon_suite_v4_selftest_")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

_MVP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _MVP)

from aeon import evaluators, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

FAILURES = []

CATEGORIES = ["Math", "Instruction", "Reasoning", "Coding", "Prose"]
DIFF_COUNTS = {"easy": 2, "medium": 3, "hard": 5, "expert": 8, "frontier": 12, "god_mode": 2}
CARRIED = ("easy", "medium", "hard")                     # v3 cells, unchanged in v4
DIALED = ("expert", "frontier", "god_mode")              # all-new v4.* cells


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


# ---------------------------------------------------------------- 1. shape
print("== 1. corpus shape: 160 cases, 5 cats x (2/3/5/8/12/2), unique ids ==")
cases = suite_mod.CASES
check("suite id", suite_mod.SUITE_ID == "aeon-suite-v4", suite_mod.SUITE_ID)
h1, h2 = suite_mod.suite_hash(), suite_mod.suite_hash()
check("suite_hash stable", bool(h1) and h1 == h2, f"{h1} vs {h2}")
check("total 160 cases", len(cases) == 160, str(len(cases)))
ids = [c["id"] for c in cases]
check("ids unique", len(ids) == len(set(ids)), f"{len(ids)} ids, {len(set(ids))} unique")

cells = {}
for c in cases:
    cells.setdefault((c["category"], c.get("difficulty")), []).append(c)
for cat in CATEGORIES:
    for d, want in DIFF_COUNTS.items():
        got = len(cells.get((cat, d), []))
        check(f"cell {cat}/{d} == {want}", got == want, str(got))
check("no stray cells", set(cells) == {(c, d) for c in CATEGORIES for d in DIFF_COUNTS},
      str(sorted(set(cells) - {(c, d) for c in CATEGORIES for d in DIFF_COUNTS})))

# id conventions: carried tiers keep their v3.* ids, dialed-up tiers are all-new v4.*
for c in cases:
    d = c["difficulty"]
    pat = r"^v3\." if d in CARRIED else r"^v4\."
    if not re.match(pat, c["id"]):
        check(f"{c['id']} id prefix matches tier provenance", False, f"difficulty={d}")
        break
else:
    check("id prefixes: carried=v3.*, dialed=v4.*", True)
v4_pat = re.compile(r"^v4\.(coding|instruction|math|prose|reasoning)"
                    r"\.(expert|frontier|god_mode)\.\d{2}$")
bad = [c["id"] for c in cases if c["difficulty"] in DIALED and not v4_pat.match(c["id"])]
check("v4 top-tier id format v4.<cat>.<tier>.NN", not bad, str(bad[:5]))

# ------------------------------------------------------- 2. carry-over
print("== 2. carry-over: v4 easy/medium/hard cells byte-identical to v3's ==")
_SUITES = os.path.join(_MVP, "suites")
for cat in ("coding", "instruction", "math", "prose", "reasoning"):
    for d in CARRIED:
        p3 = os.path.join(_SUITES, "v3", f"final_{cat}_{d}.json")
        p4 = os.path.join(_SUITES, "v4", f"final_{cat}_{d}.json")
        with open(p3, "rb") as f:
            b3 = f.read()
        with open(p4, "rb") as f:
            b4 = f.read()
        check(f"v4/final_{cat}_{d}.json == v3's", b3 == b4)

# ------------------------------------------- 3. eval specs + checkers
print("== 3. every eval spec parses; checker types exist; evaluate() never crashes ==")
known = set(evaluators.CHECKERS)
required = ("id", "category", "tier", "difficulty", "prompt", "eval")
for c in cases:
    missing = [k for k in required if k not in c]
    if missing:
        check(f"{c.get('id', '<no id>')} has all required fields", False, str(missing))
        continue
    ev = c["eval"]
    if c["tier"] == 0:
        chks = ev.get("checkers") or []
        ok = bool(chks) and all(chk.get("type") in known for chk in chks)
        if not ok:
            check(f"{c['id']} tier-0 checkers known", False,
                  str([chk.get("type") for chk in chks]))
    else:
        rub = ev.get("rubric") or []
        ok = bool(rub)
        for crit in rub:
            t0 = crit.get("tier0_check")
            if crit.get("required") and not t0:
                ok = False
                check(f"{c['id']} required criterion {crit.get('id')!r} tier0-shadowed", False)
            if t0 and t0.get("type") not in known:
                ok = False
                check(f"{c['id']} tier0_check type known", False, str(t0.get("type")))
    try:
        score, _ = evaluators.evaluate(c, "dummy probe answer 42\n\\boxed{0}", None)
        crashed = False
    except Exception as e:  # noqa: BLE001
        crashed, score = True, f"{type(e).__name__}: {e}"
    if crashed:
        check(f"{c['id']} evaluate() survives dummy input", False, str(score))
check("all eval specs valid + evaluate() crash-free",
      not [f for f in FAILURES if "checker" in f or "evaluate" in f or "criterion" in f], "see above")

# --------------------------------------------------- 4. arena prompts
print("== 4. arena prompts: +24 v4-* (8 per kind), no collisions, total 140 ==")
with open(os.path.join(_SUITES, "arena_prompts.json"), encoding="utf-8") as f:
    prompts = json.load(f)
pids = [p["id"] for p in prompts]
check("arena total 140", len(prompts) == 140, str(len(prompts)))
check("arena ids unique", len(pids) == len(set(pids)), f"{len(pids)} vs {len(set(pids))} unique")
v4p = [p for p in prompts if p["id"].startswith("v4-")]
check("24 v4-* prompts", len(v4p) == 24, str(len(v4p)))
kinds = {}
for p in v4p:
    kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
check("v4 kind balance 8/8/8", kinds == {"game": 8, "app": 8, "animation": 8}, str(kinds))
check("v4 prompts carry full schema",
      all(all(k in p and p[k] for k in ("kind", "id", "title", "brief", "prompt")) for p in v4p))

# ------------------------------------------------- 5. board fallback
print("== 5. legacy fallback: v3 heads _LEGACY_SUITES ==")
check("_LEGACY_SUITES front is aeon-suite-v3",
      scoring._LEGACY_SUITES[0] == "aeon-suite-v3", str(scoring._LEGACY_SUITES))
check("_LEGACY_SUITES keeps v2+v1",
      scoring._LEGACY_SUITES[1:] == ["aeon-suite-v2", "aeon-suite-v1"],
      str(scoring._LEGACY_SUITES))

# ---------------------------------------------------------------- verdict
print()
if FAILURES:
    print(f"RESULT: FAIL ({len(FAILURES)} failures)")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"RESULT: ALL TESTS PASS ({len(cases)} cases, suite {suite_mod.SUITE_ID} hash {h1}, "
      f"{len(prompts)} arena prompts)")
