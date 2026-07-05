"""Self-test: honeypot per-match decoy ("_bogus_live") cleanup.

Covers the bounded-growth fix for arena_artifacts: every honeypot served mints one
per-match decoy row (arena._build_test_match), so without a cleanup path a busy public
arena grows that table forever. This verifies:
  * the seeded BASE pool (prompt_id="_bogus") is never touched — list_bogus base
    selection still yields exactly the 5x3 seeded bases;
  * a PENDING (unvoted) match's decoy is never pruned, and its vote still resolves;
  * a RESOLVED match's decoy is reclaimed (voted branch, regardless of TTL);
  * an ORPHANED decoy (match row gone) is reclaimed only by the TTL backstop;
  * the opportunistic cap-triggered prune inside _build_test_match keeps growth bounded.

Runs fully offline (temp SQLite). From the mvp dir:  python test_arena_honeypot.py
"""
import os
import sys
import tempfile
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Point the DB at a throwaway SQLite file BEFORE importing aeon (db.py reads the env at
# import time; AEON_DB_URL would select the prod Postgres backend — kill it).
_TMP = tempfile.mkdtemp(prefix="aeon-honeypot-selftest-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("AEON_DB_URL", None)

from aeon import arena, db                                 # noqa: E402

db.init_db()

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("PASS:", label)


def mk_user(name):
    uid = uuid.uuid4().hex[:12]
    db.create_user(uid, username=name, pw_hash="x", pw_salt="", signup_ip="127.0.0.1")
    return db.get_user(uid)


def bases(kind=None):
    return [b for b in db.list_bogus(kind) if b["prompt_id"] == "_bogus"]


def live(kind=None):
    return [b for b in db.list_bogus(kind) if b["prompt_id"] == "_bogus_live"]


KINDS = arena.KINDS
N_BASES = 5 * len(KINDS)


def honeypot(kind):
    """Build one honeypot directly (bypassing _should_test randomness). Returns
    (match_id, real_winning_side, decoy_artifact_id)."""
    payload = arena._build_test_match(user, kind)
    assert payload, "expected a honeypot payload"
    mid = payload["match_id"]
    m = db.get_match(mid)
    decoy_id = m["a_id"] if m["bogus_side"] == "a" else m["b_id"]
    real_win = "a" if m["bogus_side"] == "b" else "b"   # voting the REAL side == a PASS
    return mid, real_win, decoy_id


# ---------- setup: real demo artifacts + the bogus base pool ----------
arena.seed_demo()
arena.seed_bogus()
user = mk_user("alice")

ok(len(bases()) == N_BASES, "seed: %d base templates (5 per kind)" % N_BASES)
ok(all(len(bases(k)) == 5 for k in KINDS), "seed: exactly 5 bases per kind")
ok(len(live()) == 0 and db.count_live_decoys() == 0, "seed: no live decoys yet")

# ---------- (1) each honeypot mints exactly ONE _bogus_live row; bases untouched ----------
mid1, win1, bog1 = honeypot("app")
ok(db.count_live_decoys("app") == 1, "build: honeypot mints one live decoy")
d1 = db.get_artifact(bog1)
ok(d1 is not None and d1["prompt_id"] == "_bogus_live" and d1["bogus"] == 1,
   "build: minted decoy is a bogus _bogus_live row")
ok(len(bases()) == N_BASES, "build: base pool unchanged after minting a decoy")

# ---------- (2) a PENDING match's decoy survives even a maximally aggressive prune, and its
#               vote still resolves ----------
db.prune_live_decoys(ttl=0, now=99_999_999_999)   # every decoy "aged"; pending must still survive
ok(db.get_artifact(bog1) is not None,
   "prune: a PENDING (unvoted) match's decoy is never deleted")
res, status = arena.submit_vote(user, mid1, win1)
ok(status == 200 and res.get("ok") and res.get("a_model") and res.get("b_model"),
   "vote: pending honeypot resolves after aggressive prune (models revealed)")

# ---------- (3) once RESOLVED, the decoy is reclaimed (voted branch, independent of TTL) ----------
n = db.prune_live_decoys(kind="app", ttl=arena._LIVE_DECOY_TTL)
ok(n >= 1 and db.get_artifact(bog1) is None, "prune: a resolved match's decoy is reclaimed")
ok(len(bases()) == N_BASES, "prune: base pool still intact after reclaiming a decoy")

# late/duplicate vote on the resolved+pruned match is still rejected (never a 200)
res2, status2 = arena.submit_vote(user, mid1, win1)
ok(status2 != 200, "vote: a duplicate vote on a resolved+pruned match is rejected (got %d)" % status2)

# ---------- (4) an ORPHANED decoy (match row deleted) is reclaimed only by the TTL backstop ----------
mid2, win2, bog2 = honeypot("game")
m2 = db.get_match(mid2)
real_id = m2["a_id"] if m2["bogus_side"] == "b" else m2["b_id"]
db.delete_artifact(real_id)                        # cancels the unvoted match -> bog2 orphaned
ok(db.get_match(mid2) is None, "orphan: deleting the real side cancels the unvoted match")
ok(db.get_artifact(bog2) is not None, "orphan: the decoy row itself survives (now unreferenced)")
db.prune_live_decoys(kind="game")                  # no ttl: not resolved, not aged -> kept
ok(db.get_artifact(bog2) is not None, "prune(no ttl): an unresolved orphan is conservatively kept")
db.prune_live_decoys(kind="game", ttl=1, now=9_999_999_999)   # ttl backstop -> reclaimed
ok(db.get_artifact(bog2) is None, "prune(ttl): the TTL backstop reclaims an aged orphan")
ok(len(bases()) == N_BASES, "orphan: base pool intact throughout")

# ---------- (5) cap-triggered opportunistic prune inside _build_test_match bounds growth ----------
orig_cap = arena._LIVE_DECOY_CAP
arena._LIVE_DECOY_CAP = 5
try:
    for _ in range(20):
        mid, win, _bog = honeypot("app")
        arena.submit_vote(user, mid, win)          # resolve each so it becomes reclaimable
    got = db.count_live_decoys("app")
    ok(got <= arena._LIVE_DECOY_CAP,
       "cap: live-decoy pool stays bounded (<= cap=%d) across 20 build+vote cycles (got %d)"
       % (arena._LIVE_DECOY_CAP, got))
finally:
    arena._LIVE_DECOY_CAP = orig_cap

# ---------- (6) base selection integrity after all the churn ----------
for k in KINDS:
    b = bases(k)
    ok(len(b) == 5 and all(x["prompt_id"] == "_bogus" for x in b),
       "base-select: kind %r still resolves to exactly the 5 seeded bases" % k)
ok(len(db.list_bogus("app")) == 5 + db.count_live_decoys("app"),
   "base-select: list_bogus('app') = 5 bases + the remaining live decoys (filter does real work)")

print("\nALL %d CHECKS PASS" % PASS)
