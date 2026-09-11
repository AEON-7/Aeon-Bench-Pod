"""Self-test for the VISION suite v2 — runs GREEN locally, no GPU/model.

Covers:
  1. imagegen: every generator produces a valid, deterministic PNG (content-addressed
     key stable), including every spec the suite actually uses.
  2. Suite well-formedness: unique ids, known categories/difficulties/requires, valid
     eval specs (registered checkers, closed_set answers inside their option sets),
     the 11 aeon-mvp-vision cases carried over intact, suite_hash stable.
  3. Suite integrity: every pinned answer agrees with the generator's own ground-truth
     meta (emergent icon-grid answers re-derived here); the gold answer scores 1.0 via
     evaluate() with NO judge; a wrong answer scores 0.0.
  4. keyword_* checker unit tests: word boundaries, synonym groups, multi-word keywords,
     NFKC normalization, slot strictness, scan:"text", fractional keyword_set, ordering
     — and the DOCUMENTED negation limitation ("not red" matches "red").
  5. run_vision_benchmark end-to-end on a temp SQLite DB with MockVisionTarget:
     probe passes, all cases board="vision", scored 1.0, run succeeded; the '*-bad'
     persona scores 0.0 on every case.

Run:  python mvp/test_vision_suite.py
"""
import io
import os
import sys
import tempfile

# Point the DB layer at a throwaway SQLite BEFORE importing aeon.* (db reads env at import).
os.environ.pop("AEON_DB_URL", None)                      # never touch the Postgres mothership
_TMP = tempfile.mkdtemp(prefix="aeon_vision_selftest_")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image  # noqa: E402

from aeon import db, imagegen, vision_suite  # noqa: E402
from aeon.evaluators import CHECKERS, evaluate, run_checker  # noqa: E402
from aeon.probe import probe_vision  # noqa: E402
from aeon.runner import run_vision_benchmark  # noqa: E402
from aeon.targets import MockVisionTarget, _gold_case_answer  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


# ------------------------------------------------------------- 1. imagegen
print("== 1. imagegen: valid + deterministic PNGs (all generators + all suite specs) ==")
default_specs = [{"gen": name} for name in imagegen.GENERATORS]
suite_specs = [s for c in vision_suite.CASES for s in c["images"]]
seen = set()
for spec in default_specs + suite_specs:
    key = repr(spec)
    if key in seen:
        continue
    seen.add(key)
    sha, png, meta = imagegen.generate(spec)
    img = Image.open(io.BytesIO(png))
    img.load()
    sha2, png2, meta2 = imagegen.generate(spec)
    check(f"generate {spec['gen']}{spec.get('args', {})}",
          img.width > 0 and img.height > 0 and sha2 == sha and png2 == png and meta2 == meta,
          f"size={img.size} stable={sha2 == sha}")

# --------------------------------------------------- 2. suite well-formedness
print("== 2. suite well-formedness ==")
check("suite id", vision_suite.SUITE_ID == "aeon-vision-v2", vision_suite.SUITE_ID)
h1, h2 = vision_suite.suite_hash(), vision_suite.suite_hash()
check("suite_hash stable", bool(h1) and h1 == h2, f"{h1} vs {h2}")
ids = [c["id"] for c in vision_suite.CASES]
check("unique case ids", len(ids) == len(set(ids)), str(len(ids)))
check("case count >= 25", len(vision_suite.CASES) >= 25, str(len(vision_suite.CASES)))

# the aeon-mvp-vision corpus carries over intact (historical comparability)
_LEGACY = ["vision.ocr.token", "vision.count.circles", "vision.color.square",
           "vision.spatial.quadrant", "vision.relation.leftof", "vision.chart.maxbar",
           "vision.chart.value", "vision.vqa.mcq", "vision.detail.tiny",
           "vision.multi.morecount", "vision.describe.scene"]
check("legacy mvp cases intact", all(i in ids for i in _LEGACY),
      str([i for i in _LEGACY if i not in ids]))
check("has medium tier", any(c.get("difficulty") == "medium" for c in vision_suite.CASES))
check("has hard tier", any(c.get("difficulty") == "hard" for c in vision_suite.CASES))
check("has expert tier", any(c.get("difficulty") == "expert" for c in vision_suite.CASES))

for c in vision_suite.CASES:
    cid = c["id"]
    check(f"{cid} category known", c["category"] in vision_suite.CATEGORIES_VISION, c["category"])
    check(f"{cid} difficulty known", c.get("difficulty") in vision_suite.DIFFICULTIES,
          str(c.get("difficulty")))
    check(f"{cid} requires known", c["requires"] in ("vision_ok", "ocr_ok", "multi_image_ok"),
          c["requires"])
    ev = c["eval"]
    if "rubric" in ev:
        ok = all("tier0_check" in cr and cr["tier0_check"]["type"] in CHECKERS
                 for cr in ev["rubric"])
        check(f"{cid} rubric tier0-shadowed", ok)
        continue
    for chk in ev["checkers"]:
        check(f"{cid} checker registered", chk["type"] in CHECKERS, chk["type"])
        if chk["type"] == "closed_set":
            check(f"{cid} closed_set answer in options",
                  chk["answer"].lower() in {o.lower() for o in chk["options"]},
                  f"{chk['answer']} not in {chk['options']}")
        if chk["type"] in ("keyword_all", "keyword_set", "ordered_keywords"):
            check(f"{cid} keyword groups non-empty",
                  bool(chk.get("groups")) and all(g for g in chk["groups"]))

# --------------------------------------- 3. checker <-> generator ground truth
print("== 3. suite integrity: pinned answers == generator truth; gold 1.0, wrong 0.0 ==")


def _first_chk(c):
    return c["eval"]["checkers"][0]


def _gt(c, i=0):
    return imagegen.generate(c["images"][i])[2]


_CONSIST = {
    # counting on generated clutter/grids
    "vision.count.cluttered_tri": lambda c: _first_chk(c)["value"] == _gt(c)["count"],
    "vision.count.cluttered_tri_hard": lambda c: _first_chk(c)["value"] == _gt(c)["count"],
    "vision.count.occluded_circles": lambda c: _first_chk(c)["value"] == _gt(c)["count"],
    "vision.count.grid_stars": lambda c: _first_chk(c)["value"] == _gt(c)["counts"]["star"],
    "vision.relation.rows_star_heart":
        lambda c: _first_chk(c)["value"] == _gt(c)["rows_with_star_and_heart"],
    "vision.relation.cols_heart": lambda c: _first_chk(c)["value"] == _gt(c)["cols_with_heart"],
    # OCR truth is the rendered text
    "vision.ocr.rotated": lambda c: _first_chk(c)["value"] == _gt(c)["text"],
    "vision.ocr.tiny_noise": lambda c: _first_chk(c)["value"] == _gt(c)["text"],
    # multi-step spatial: the generator derives the answer from sizes+colors
    "vision.spatial.leftof_largest": lambda c: _first_chk(c)["answer"] == _gt(c)["answer"],
    "vision.spatial.leftof_smallest": lambda c: _first_chk(c)["answer"] == _gt(c)["answer"],
    # charts: the asked (series, label) cell
    "vision.chart.series_value":
        lambda c: _first_chk(c)["value"] == _gt(c)["value_of"]["beta"]["Q3"],
    "vision.chart.series_value_hard":
        lambda c: _first_chk(c)["value"] == _gt(c)["value_of"]["south"]["Apr"],
    # fine color: the 1-based odd patch
    "vision.color.odd_patch": lambda c: _first_chk(c)["answer"] == str(_gt(c)["odd"]),
    "vision.color.odd_patch_hard": lambda c: _first_chk(c)["answer"] == str(_gt(c)["odd"]),
    # patterns: next-in-sequence
    "vision.pattern.next_shape": lambda c: _first_chk(c)["answer"] == _gt(c)["next"],
    "vision.pattern.next_color": lambda c: _first_chk(c)["answer"] == _gt(c)["next"],
    # scenes: keyword groups must cover the generator's objects/colors
    "vision.scene.keywords": lambda c: all(
        any(obj in (chk.get("keywords") or []) for chk in c["eval"]["checkers"])
        for obj in _gt(c)["objects"]),
    "vision.scene.car": lambda c: ("car" in c["eval"]["checkers"][0]["groups"][0]
                                   and _gt(c)["car_color"] in c["eval"]["checkers"][0]["groups"][1]),
    "vision.scene.order": lambda c: [g[0] for g in c["eval"]["checkers"][0]["groups"]]
        == _gt(c)["order_left_to_right"],
    # multi-image: the answer is the SECOND image's quadrant
    "vision.multi.moved": lambda c: _first_chk(c)["answer"]
        == c["images"][1]["args"]["quadrant"],
}

for c in vision_suite.CASES:
    cid = c["id"]
    if cid in _CONSIST:
        check(f"{cid} checker==generator-truth", _CONSIST[cid](c))
    gold = _gold_case_answer(c)
    score, ev = evaluate(c, gold, None)          # judge=None: deterministic only
    check(f"{cid} gold -> 1.0", score == 1.0, f"score={score} ev={ev} gold={gold!r}")
    wrong, wev = evaluate(c, "<answer>zzzz</answer><count>-999</count><ocr>#####</ocr>"
                             "<object>zzzz</object><color>zzzz</color>", None)
    check(f"{cid} wrong -> 0.0", wrong == 0.0, f"score={wrong} ev={wev}")

# -------------------------------------------------- 4. keyword checker units
print("== 4. keyword_* checkers: boundaries, synonyms, order, strictness ==")
KW = {"type": "keyword_all", "groups": [["car", "vehicle"], ["red", "crimson"]]}
ok, _ = run_checker(KW, "<answer>a red car parked outside</answer>")
check("keyword_all: both groups", ok is True)
ok, _ = run_checker(KW, "<answer>a crimson vehicle</answer>")
check("keyword_all: synonyms accepted", ok is True)
ok, ev = run_checker(KW, "<answer>a red cart</answer>")
check("keyword_all: word boundary (cart != car)", ok is False, ev)
ok, ev = run_checker(KW, "<answer>a scared car</answer>")   # 'scared' must not match 'red'
check("keyword_all: word boundary (scared != red)", ok is False, ev)
ok, ev = run_checker(KW, "a red car with no slot")
check("keyword_all: slot-strict (no <answer> -> fail)", ok is False, ev)
ok, _ = run_checker({**KW, "scan": "text"}, "a red car with no slot")
check("keyword_all: scan:'text' matches whole reply", ok is True)
ok, _ = run_checker(KW, "<answer>A RED CAR</answer>")
check("keyword_all: case-insensitive", ok is True)
ok, _ = run_checker(KW, "<answer>ａ ｒｅｄ ｃａｒ</answer>")   # fullwidth -> NFKC folds to ascii
check("keyword_all: NFKC normalization (fullwidth)", ok is True)
ok, _ = run_checker({"type": "keyword_all", "groups": [["light blue"]]},
                    "<answer>a light  blue wall</answer>")
check("keyword_all: multi-word keyword spans whitespace", ok is True)
# DOCUMENTED LIMITATION: negation is NOT handled — presence-only matching. Questions must
# be designed so a negated answer is never the correct one.
ok, _ = run_checker(KW, "<answer>a car that is not red</answer>")
check("keyword_all: negation NOT handled (documented)", ok is True)

ANY = {"type": "keyword_any", "keywords": ["tree", "bush", "plant"]}
ok, _ = run_checker(ANY, "<answer>an oak tree</answer>")
check("keyword_any: one synonym is enough", ok is True)
ok, ev = run_checker(ANY, "<answer>a treehouse</answer>")
check("keyword_any: boundary (treehouse != tree)", ok is False, ev)
ok, _ = run_checker({"type": "keyword_any", "groups": [["tree", "bush"]]},
                    "<answer>a bush</answer>")
check("keyword_any: accepts groups form", ok is True)

SET = {"type": "keyword_set", "min_ratio": 0.66,
       "groups": [["house"], ["tree"], ["car"]]}
ok, _ = run_checker(SET, "<answer>a house near a tree</answer>")
check("keyword_set: 2/3 passes min_ratio 0.66", ok is True)
ok, ev = run_checker(SET, "<answer>just a house</answer>")
check("keyword_set: 1/3 fails min_ratio 0.66", ok is False, ev)
ok, _ = run_checker({**SET, "min_ratio": 1.0}, "<answer>house tree car</answer>")
check("keyword_set: min_ratio 1.0 == all", ok is True)

ORD = {"type": "ordered_keywords",
       "groups": [["red", "crimson"], ["green"], ["blue", "navy"]]}
ok, _ = run_checker(ORD, "<answer>red, then green, then blue</answer>")
check("ordered_keywords: in order", ok is True)
ok, _ = run_checker(ORD, "<answer>first crimson, later green, finally navy</answer>")
check("ordered_keywords: synonyms keep order", ok is True)
ok, ev = run_checker(ORD, "<answer>blue, green, red</answer>")
check("ordered_keywords: out of order fails", ok is False, ev)
ok, ev = run_checker(ORD, "<answer>red then blue</answer>")
check("ordered_keywords: missing group fails", ok is False, ev)

# ------------------------------------------------ 5. end-to-end mock runs
print("== 5. run_vision_benchmark on temp sqlite (mock target) ==")
pr = probe_vision(MockVisionTarget("mock-vision"))
check("probe_vision short-circuits mock", pr.get("vision_ok") is True, str(pr))

progress = []
pr_run = run_vision_benchmark("vision-selftest-good", "mock-vision", "mock",
                              progress_cb=lambda cid, s, st: progress.append((cid, s, st)))
check("run probe vision_ok", pr_run.get("vision_ok") is True, str(pr_run))
check("progress_cb fired per case", len(progress) == len(vision_suite.CASES), str(len(progress)))

with db.connect() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("vision-selftest-good",)).fetchall()]
    run_row = dict(c.execute("SELECT status, board, suite_id, n_cases FROM runs WHERE id=?",
                             ("vision-selftest-good",)).fetchone())
check("one result per case", len(rows) == len(vision_suite.CASES), str(len(rows)))
check("all board=vision", all(r["board"] == "vision" for r in rows),
      str({r["board"] for r in rows}))
check("all scored 1.0", all(r["status"] == "scored" and r["score"] == 1.0 for r in rows),
      str([(r["case_id"], r["status"], r["score"]) for r in rows if r["score"] != 1.0]))
check("run succeeded + board=vision", run_row["status"] == "succeeded" and run_row["board"] == "vision",
      str(run_row))
check("run suite_id", run_row["suite_id"] == "aeon-vision-v2", str(run_row["suite_id"]))

run_vision_benchmark("vision-selftest-bad", "mock-vision-bad", "mock")
with db.connect() as c:
    bad = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("vision-selftest-bad",)).fetchall()]
check("bad persona: one result per case", len(bad) == len(vision_suite.CASES), str(len(bad)))
check("bad persona: all scored 0.0", all(r["status"] == "scored" and r["score"] == 0.0 for r in bad),
      str([(r["case_id"], r["score"]) for r in bad if r["score"] != 0.0]))

# ---------------------------------------------------------------- verdict
print()
if FAILURES:
    print(f"RESULT: FAIL ({len(FAILURES)} failures)")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"RESULT: ALL TESTS PASS ({len(vision_suite.CASES)} cases, suite {vision_suite.SUITE_ID} "
      f"hash {h1}, db {os.environ['AEON_DB']})")
