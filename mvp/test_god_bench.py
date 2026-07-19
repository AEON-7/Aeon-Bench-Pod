"""GOD MODE BENCH scoring — the dedicated hardest-tier board (board='god').

Covers: sentinel component (atomic god cells, no_answer ¼-rule, own-suite coverage floor),
agentic component (per-harness god runs, latest wins), the 0.6/0.4 GOD SCORE blend with
renormalization + provisional flag, attested-only eligibility ordering, partial-pass
exclusion, and seeded-draw exclusion."""
import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-god-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


GODS = [c for c in suite_mod.CASES if c.get("difficulty") == "god_mode"]
N_GOD = len(GODS)
check(N_GOD >= 20, f"live corpus carries the god tier ({N_GOD} sentinels)")

_N = 0


def _ts():
    global _N
    _N += 1
    return 1_700_000_000 + _N * 100


def god_text_run(model, *, score=0.0, tier="attested", n=None, no_answer=0, seed=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None, judge_is_self=True,
                  suite_id=suite_mod.SUITE_ID, suite_hash=suite_mod.suite_hash(),
                  n_cases=n or N_GOD, params={}, env={}, board="god", hf_repo=model,
                  trust_tier=tier, bench_seed=seed)
    cases = GODS[:(n or N_GOD)]
    for i, c in enumerate(cases):
        if i < no_answer:
            db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 0),
                           status="no_answer", score=None, raw_output="", evidence={},
                           speed={}, board="god")
        else:
            db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 0),
                           status="scored", score=score, raw_output="x", evidence={},
                           speed={}, board="god")
    db.finish_run(rid, "succeeded")
    with db.connect() as conn:
        conn.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                     (_ts(), _ts() + 60, rid))
    return rid


def god_harness_run(model, harness, score):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None, judge_is_self=True,
                  suite_id="aeon-agentic-v2.4", suite_hash="ag", n_cases=3, params={}, env={},
                  board="god", hf_repo=model, trust_tier="attested",
                  harness=harness, harness_version="1.0.0")
    for i in range(3):
        db.save_result(rid, f"av2-god-{i}", category="Agentic", tier=1, status="scored",
                       score=score, raw_output="x", evidence={}, speed={}, board="god")
    db.finish_run(rid, "succeeded")
    with db.connect() as conn:
        conn.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                     (_ts(), _ts() + 60, rid))
    return rid


# FULL: sentinels 20% + agentic on two harnesses -> full blend
FULL = "lab/god-full"
god_text_run(FULL, score=0.2)
god_harness_run(FULL, "hermes", 0.5)
god_harness_run(FULL, "opencode", 0.3)
# TEXTONLY: sentinels only -> renormalized + provisional
TEXTONLY = "lab/god-textonly"
god_text_run(TEXTONLY, score=0.1)
# PARTIAL: only 10 sentinels attempted -> excluded outright
PARTIAL = "lab/god-partial"
god_text_run(PARTIAL, score=1.0, n=10)
# SEEDED: fast-bench draw -> never ranks
god_text_run("lab/god-seeded", score=1.0, seed="abcd1234")
# NOANS: no_answer ¼-rule sanity (2 no-answers, rest zero)
NOANS = "lab/god-noans"
god_text_run(NOANS, score=0.0, no_answer=2)

b = scoring.god_leaderboard()
rows = {m["canonical"]: m for m in b["models"]}

check(FULL in rows and TEXTONLY in rows and NOANS in rows, "scored models appear")
check(PARTIAL not in rows, "a partial god pass (10 sentinels) never ranks")
check("lab/god-seeded" not in rows, "seeded draws never rank")

f = rows[FULL]
check(f["sentinels"]["composite"] == 20.0, "sentinel composite from god cells")
check(f["agentic"]["score"] == 40.0 and f["agentic"]["harnesses"]["hermes"] == 50.0,
      "agentic = mean across harnesses, per-harness disclosed")
check(f["god_score"] == round(0.6 * 20.0 + 0.4 * 40.0, 1), "GOD SCORE = 0.6·sent + 0.4·agentic")
check(not f["god_provisional"], "both components present -> not provisional")
check(f["record_eligible"], "attested sentinel run is record-eligible")

t = rows[TEXTONLY]
check(t["god_score"] == 10.0 and t["god_provisional"] and t["agentic"] is None,
      "sentinels-only renormalizes to the sentinel score, provisional, agentic null")

n = rows[NOANS]
check(n["sentinels"]["n_attempted"] == N_GOD, "no_answer rows count as attempted (coverage)")
check(n["sentinels"]["composite"] == 0.0, "all-zero + no_answers -> composite 0, never inflated")

order = [m["canonical"] for m in b["models"]]
check(order.index(FULL) < order.index(TEXTONLY), "board sorts by GOD SCORE desc")
check(b["weights"] == {"sentinels": 0.6, "agentic": 0.4} and b["god_corpus"] == N_GOD,
      "payload discloses the blend weights + current god corpus size")

print(f"\nOK  god mode bench: {PASSED} checks passed")
