"""Self-test for the AUDIO suite (Task C) — runs GREEN locally, no GPU/model.

Covers:
  1. audiogen.synth: every generator produces a valid, parseable, deterministic
     16 kHz mono 16-bit WAV with duration > 0 (content-addressed key stable).
  2. Suite integrity: every case's checker agrees with the generator's own
     ground-truth meta; the gold answer scores 1.0 via evaluate() with NO judge;
     a wrong answer scores 0.0. suite_hash() is stable.
  3. run_audio_benchmark end-to-end on a temp SQLite DB with MockAudioTarget:
     probe passes, all cases board="audio", status scored, score 1.0, run
     succeeded. A '*-bad' persona scores 0.0 on every case.

Run:  python mvp/test_audio_suite.py
"""
import io
import os
import sys
import tempfile
import wave

# Point the DB layer at a throwaway SQLite BEFORE importing aeon.* (db reads env at import).
os.environ.pop("AEON_DB_URL", None)                      # never touch the Postgres mothership
_TMP = tempfile.mkdtemp(prefix="aeon_audio_selftest_")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aeon import audio_suite, audiogen, db  # noqa: E402
from aeon.evaluators import evaluate  # noqa: E402
from aeon.probe import probe_audio  # noqa: E402
from aeon.runner import run_audio_benchmark  # noqa: E402
from aeon.targets import MockAudioTarget  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def gold_answer(case):
    chk = case["eval"]["checkers"][0]
    if chk["type"] == "count_slot":
        slot = chk.get("slot", "count")
        return f"<{slot}>{chk['value']}</{slot}>"
    slot = chk.get("slot", "answer")
    return f"<{slot}>{chk['answer']}</{slot}>"


# ---------------------------------------------------------------- 1. synth
print("== 1. audiogen.synth: valid + deterministic WAVs ==")
extra_specs = [
    {"gen": "beeps", "args": {"n": 4, "freq": 500, "gap_ms": 150}},
    {"gen": "two_tones", "args": {"f1": 220, "f2": 1200}},
    {"gen": "long_short", "args": {"a_ms": 300, "b_ms": 500}},
    {"gen": "noise_or_tone", "args": {"kind": "noise"}},
    {"gen": "pattern", "args": {"seq": "LLSS"}},
]
all_specs = [s for c in audio_suite.CASES for s in c["audio"]] + extra_specs
for spec in all_specs:
    key, wav_bytes, meta = audiogen.synth(spec)
    with wave.open(io.BytesIO(wav_bytes)) as w:
        # synthesized audio is 16 kHz by construction; pinned FSDD speech recordings carry
        # their native 8 kHz in the header (transports pass the file verbatim)
        rate_ok = (w.getframerate() > 0 if spec["gen"] == "speech_asset"
                   else w.getframerate() == audiogen.RATE)
        ok = (w.getnchannels() == 1 and w.getsampwidth() == 2
              and rate_ok and w.getnframes() > 0)
        dur = w.getnframes() / w.getframerate()
    key2, wav2, meta2 = audiogen.synth(spec)
    check(f"synth {spec['gen']}{spec.get('args', {})}",
          ok and dur > 0 and key2 == key and wav2 == wav_bytes and meta2 == meta,
          f"ok={ok} dur={dur} stable={key2 == key}")

# --------------------------------------------------- 2. suite gold answers
print("== 2. suite integrity: gold scores 1.0, wrong scores 0.0, no judge ==")
check("suite id", audio_suite.SUITE_ID == "aeon-audio-v2", audio_suite.SUITE_ID)
h1, h2 = audio_suite.suite_hash(), audio_suite.suite_hash()
check("suite_hash stable", bool(h1) and h1 == h2, f"{h1} vs {h2}")
check("case count ~10", len(audio_suite.CASES) >= 10, str(len(audio_suite.CASES)))
cats = {c["category"] for c in audio_suite.CASES}
check("all 5 categories", cats == set(audio_suite.CATEGORIES_AUDIO), str(cats))

for case in audio_suite.CASES:
    chk = case["eval"]["checkers"][0]
    _, _, meta = audiogen.synth(case["audio"][0])
    # checker ground truth must MATCH what the generator actually synthesized
    consistent = {
        "Counting": lambda: chk["value"] == meta["n"],
        "Pitch": lambda: chk["answer"] == meta["higher"],
        "Duration": lambda: chk["answer"] == meta["longer"],
        "Timbre": lambda: chk["answer"] == meta["kind"],
        "Pattern": lambda: chk["answer"] == meta["seq"],
        # FSDD files are named <digit>_<speaker>_<take>.wav — the digit IS the ground truth
        "Speech": lambda: chk["answer"] == meta["file"].split("_", 1)[0],
    }[case["category"]]()
    check(f"{case['id']} checker==generator-truth", consistent, f"chk={chk} meta={meta}")

    score, ev = evaluate(case, gold_answer(case), None)     # judge=None: deterministic only
    check(f"{case['id']} gold -> 1.0", score == 1.0, f"score={score} ev={ev}")
    wrong, wev = evaluate(case, "<answer>zzzz</answer><count>-999</count>", None)
    check(f"{case['id']} wrong -> 0.0", wrong == 0.0, f"score={wrong} ev={wev}")
    check(f"{case['id']} tier0/audio_ok", case["tier"] == 0 and case["requires"] == "audio_ok")

# ------------------------------------------------ 3. end-to-end mock runs
print("== 3. run_audio_benchmark on temp sqlite (mock target) ==")
pr = probe_audio(MockAudioTarget("mock-audio"))
check("probe_audio short-circuits mock", pr.get("audio_ok") is True, str(pr))

progress = []
pr_run = run_audio_benchmark("audio-selftest-good", "mock-audio", "mock",
                             progress_cb=lambda cid, s, st: progress.append((cid, s, st)))
check("run probe audio_ok", pr_run.get("audio_ok") is True, str(pr_run))
check("progress_cb fired per case", len(progress) == len(audio_suite.CASES), str(len(progress)))

with db.connect() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("audio-selftest-good",)).fetchall()]
    run_row = dict(c.execute("SELECT status, board, suite_id, n_cases FROM runs WHERE id=?",
                             ("audio-selftest-good",)).fetchone())
check("one result per case", len(rows) == len(audio_suite.CASES), str(len(rows)))
check("all board=audio", all(r["board"] == "audio" for r in rows),
      str({r["board"] for r in rows}))
check("all scored 1.0", all(r["status"] == "scored" and r["score"] == 1.0 for r in rows),
      str([(r["case_id"], r["status"], r["score"]) for r in rows if r["score"] != 1.0]))
check("run succeeded + board=audio", run_row["status"] == "succeeded" and run_row["board"] == "audio",
      str(run_row))
check("run suite_id", run_row["suite_id"] == "aeon-audio-v2", str(run_row["suite_id"]))

run_audio_benchmark("audio-selftest-bad", "mock-audio-bad", "mock")
with db.connect() as c:
    bad = [dict(r) for r in c.execute(
        "SELECT case_id, board, status, score FROM results WHERE run_id=?",
        ("audio-selftest-bad",)).fetchall()]
check("bad persona: one result per case", len(bad) == len(audio_suite.CASES), str(len(bad)))
check("bad persona: all scored 0.0", all(r["status"] == "scored" and r["score"] == 0.0 for r in bad),
      str([(r["case_id"], r["score"]) for r in bad if r["score"] != 0.0]))

# ---------------------------------------------------------------- verdict
print()
if FAILURES:
    print(f"RESULT: FAIL ({len(FAILURES)} failures)")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"RESULT: ALL TESTS PASS ({len(audio_suite.CASES)} cases, suite {audio_suite.SUITE_ID} "
      f"hash {h1}, db {os.environ['AEON_DB']})")
