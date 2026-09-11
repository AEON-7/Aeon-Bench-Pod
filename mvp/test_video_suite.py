"""Self-test for the VIDEO suite — runs GREEN locally, no GPU/model.

Covers:
  1. videogen: every generator produces a DECODABLE MP4 (decoded back with the same
     imageio stack) with the expected tiny footprint (<= 64 frames, <= 256px), and a
     content-address that is stable across calls (raw-frame sha, encoder-independent).
  2. Suite well-formedness + integrity: unique ids, known categories/difficulties,
     valid eval specs, every pinned answer agrees with the generator's own ground-truth
     meta; the gold answer scores 1.0 via evaluate() with NO judge; wrong scores 0.0.
  3. probe_video gating: mock short-circuit, transport rejection (TargetError -> rejected,
     not model_unavailable), control-pair (same answer with/without clip -> not reached).
  4. run_video_benchmark end-to-end on a temp SQLite DB with MockVideoTarget: probe
     passes, all cases board="video", scored 1.0, run succeeded (composite 100); the
     '*-bad' persona scores 0.0 on every case; a video-rejecting target records
     capability_absent and writes no case results.

Run:  python mvp/test_video_suite.py     (needs imageio[ffmpeg], like the suite itself)
"""
import os
import sys
import tempfile

# Point the DB layer at a throwaway SQLite BEFORE importing aeon.* (db reads env at import).
os.environ.pop("AEON_DB_URL", None)                      # never touch the Postgres mothership
_TMP = tempfile.mkdtemp(prefix="aeon_video_selftest_")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aeon import db, video_suite, videogen  # noqa: E402
from aeon.evaluators import CHECKERS, evaluate  # noqa: E402
from aeon.probe import probe_video  # noqa: E402
from aeon.runner import run_video_benchmark  # noqa: E402
from aeon.targets import MockVideoTarget, TargetError, _gold_case_answer  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


if not videogen.available():
    print("RESULT: SKIP — video suite requires imageio[ffmpeg] (pip install \"imageio[ffmpeg]\")")
    sys.exit(1)

import imageio.v2 as imageio  # noqa: E402  (decode with the same lib the generator encodes with)


def _decode(mp4_bytes):
    """(n_frames, w, h) decoded from MP4 bytes via the same imageio/ffmpeg stack."""
    p = os.path.join(_TMP, "probe_decode.mp4")
    with open(p, "wb") as fh:
        fh.write(mp4_bytes)
    rd = imageio.get_reader(p, format="FFMPEG")
    try:
        frames = [f for f in rd]
    finally:
        rd.close()
    h, w = frames[0].shape[:2]
    return len(frames), w, h


# ------------------------------------------------------------- 1. videogen
print("== 1. videogen: decodable + deterministic tiny clips ==")
default_specs = [{"gen": name} for name in videogen.GENERATORS]
suite_specs = [s for c in video_suite.CASES for s in c["video"]]
seen = set()
for spec in default_specs + suite_specs:
    key = repr(spec)
    if key in seen:
        continue
    seen.add(key)
    sha, mp4, meta = videogen.generate(spec)
    n, w, h = _decode(mp4)
    sha2, mp42, meta2 = videogen.generate(spec)
    check(f"generate {spec['gen']}{spec.get('args', {})}",
          n > 0 and n <= 64 and w <= 256 and h <= 256
          and sha2 == sha and mp42 == mp4 and meta2 == meta,
          f"frames={n} size={w}x{h} stable={sha2 == sha}")

# --------------------------------------------- 2. suite well-formedness + integrity
print("== 2. suite integrity: pinned answers == generator truth; gold 1.0, wrong 0.0 ==")
check("suite id", video_suite.SUITE_ID == "aeon-video-v1", video_suite.SUITE_ID)
h1, h2 = video_suite.suite_hash(), video_suite.suite_hash()
check("suite_hash stable", bool(h1) and h1 == h2, f"{h1} vs {h2}")
ids = [c["id"] for c in video_suite.CASES]
check("unique case ids", len(ids) == len(set(ids)), str(len(ids)))
check("case count >= 8", len(video_suite.CASES) >= 8, str(len(video_suite.CASES)))
cats = {c["category"] for c in video_suite.CASES}
check("all categories used", cats == set(video_suite.CATEGORIES_VIDEO), str(cats))


def _first_chk(c):
    return c["eval"]["checkers"][0]


def _gt(c):
    return videogen.generate(c["video"][0])[2]


_CONSIST = {
    "video.motion.right": lambda c: _first_chk(c)["answer"] == _gt(c)["direction"],
    "video.motion.up": lambda c: _first_chk(c)["answer"] == _gt(c)["direction"],
    "video.motion.grow": lambda c: {"grow": "larger", "shrink": "smaller"}[_gt(c)["mode"]]
        == _first_chk(c)["answer"],
    "video.temporal.first_flash": lambda c: _first_chk(c)["answer"] == _gt(c)["order"][0],
    "video.temporal.flash_order": lambda c: [g[0] for g in _first_chk(c)["groups"]]
        == _gt(c)["order"],
    "video.count.blinks": lambda c: _first_chk(c)["value"] == _gt(c)["blinks"],
    "video.count.crossings": lambda c: _first_chk(c)["value"] == _gt(c)["crossed"],
    "video.objects.appeared": lambda c: _gt(c)["appeared"].endswith(_first_chk(c)["answer"]),
    "video.objects.disappeared": lambda c: _first_chk(c)["answer"] == _gt(c)["vanished"],
    "video.speed.faster": lambda c: _first_chk(c)["answer"] == _gt(c)["faster"],
}
check("every case has a consistency probe", set(_CONSIST) == set(ids),
      str(set(ids) ^ set(_CONSIST)))

for c in video_suite.CASES:
    cid = c["id"]
    check(f"{cid} tier0/video_ok", c["tier"] == 0 and c["requires"] == "video_ok")
    check(f"{cid} difficulty known", c.get("difficulty") in video_suite.DIFFICULTIES,
          str(c.get("difficulty")))
    for chk in c["eval"]["checkers"]:
        check(f"{cid} checker registered", chk["type"] in CHECKERS, chk["type"])
    if cid in _CONSIST:
        check(f"{cid} checker==generator-truth", _CONSIST[cid](c))
    gold = _gold_case_answer(c)
    score, ev = evaluate(c, gold, None)          # judge=None: deterministic only
    check(f"{cid} gold -> 1.0", score == 1.0, f"score={score} ev={ev} gold={gold!r}")
    wrong, wev = evaluate(c, "<answer>zzzz</answer><count>-999</count>", None)
    check(f"{cid} wrong -> 0.0", wrong == 0.0, f"score={wrong} ev={wev}")

# ------------------------------------------------------- 3. probe gating
print("== 3. probe_video gating ==")
pr = probe_video(MockVideoTarget("mock-video"))
check("probe short-circuits mock", pr.get("video_ok") is True, str(pr))


class _RejectingTarget:
    """Simulates an endpoint that 400s on video_url content parts (transport rejection)."""
    model = "no-video"

    def chat(self, messages, **kw):
        blocks = messages[0].get("content")
        if isinstance(blocks, list) and any(b.get("type") == "video_url" for b in blocks):
            raise TargetError("HTTP 400 from http://x: unknown content part video_url")
        return {"text": "ready", "output_tokens": 1}


class _BlindTarget:
    """Answers every question identically — the control pair must conclude the video
    never reached the model."""
    model = "blind"

    def chat(self, messages, **kw):
        return {"text": "I cannot see any video.", "output_tokens": 5}


pr = probe_video(_RejectingTarget())
check("transport rejection classified", pr.get("video_ok") is False
      and pr.get("transport") == "rejected", str(pr))
pr = probe_video(_BlindTarget())
check("control pair: same answer -> not reached", pr.get("video_ok") is False
      and pr.get("transport") == "accepted", str(pr))

# ------------------------------------------------ 4. end-to-end mock runs
print("== 4. run_video_benchmark on temp sqlite (mock target) ==")
progress = []
pr_run = run_video_benchmark("video-selftest-good", "mock-video", "mock",
                             progress_cb=lambda cid, s, st: progress.append((cid, s, st)))
check("run probe video_ok", pr_run.get("video_ok") is True, str(pr_run))
check("progress_cb fired per case", len(progress) == len(video_suite.CASES), str(len(progress)))

with db.connect() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("video-selftest-good",)).fetchall()]
    run_row = dict(c.execute("SELECT status, board, suite_id, n_cases FROM runs WHERE id=?",
                             ("video-selftest-good",)).fetchone())
check("one result per case", len(rows) == len(video_suite.CASES), str(len(rows)))
check("all board=video", all(r["board"] == "video" for r in rows),
      str({r["board"] for r in rows}))
check("all scored 1.0 (composite 100)", all(r["status"] == "scored" and r["score"] == 1.0 for r in rows),
      str([(r["case_id"], r["status"], r["score"]) for r in rows if r["score"] != 1.0]))
check("run succeeded + board=video", run_row["status"] == "succeeded" and run_row["board"] == "video",
      str(run_row))
check("run suite_id", run_row["suite_id"] == "aeon-video-v1", str(run_row["suite_id"]))

run_video_benchmark("video-selftest-bad", "mock-video-bad", "mock")
with db.connect() as c:
    bad = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("video-selftest-bad",)).fetchall()]
check("bad persona: one result per case", len(bad) == len(video_suite.CASES), str(len(bad)))
check("bad persona: all scored 0.0", all(r["status"] == "scored" and r["score"] == 0.0 for r in bad),
      str([(r["case_id"], r["score"]) for r in bad if r["score"] != 0.0]))

# a video-rejecting endpoint records capability_absent, writes NO case results, and the
# runner returns the probe verdict (aeon_pod then skips submission)
import aeon.probe as _probe_mod  # noqa: E402
_orig = _probe_mod.probe_video
_probe_mod.probe_video = lambda t: {"video_ok": False, "transport": "rejected",
                                    "error": "HTTP 400: unknown part video_url"}
try:
    pr_abs = run_video_benchmark("video-selftest-absent", "some-model", "http://127.0.0.1:9/v1")
finally:
    _probe_mod.probe_video = _orig
check("rejecting endpoint -> video_ok False", pr_abs.get("video_ok") is False, str(pr_abs))
with db.connect() as c:
    absent_run = dict(c.execute("SELECT status FROM runs WHERE id=?",
                                ("video-selftest-absent",)).fetchone())
    absent_rows = c.execute("SELECT COUNT(*) AS n FROM results WHERE run_id=?",
                            ("video-selftest-absent",)).fetchone()
check("capability_absent recorded", absent_run["status"] == "capability_absent", str(absent_run))
check("no case results for absent run", dict(absent_rows)["n"] == 0, str(dict(absent_rows)))

# ---------------------------------------------------------------- verdict
print()
if FAILURES:
    print(f"RESULT: FAIL ({len(FAILURES)} failures)")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"RESULT: ALL TESTS PASS ({len(video_suite.CASES)} cases, suite {video_suite.SUITE_ID} "
      f"hash {h1}, db {os.environ['AEON_DB']})")
