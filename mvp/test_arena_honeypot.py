"""Self-test: honeypot per-match decoy ("_bogus_live") cleanup.

Covers the bounded-growth fix for arena_artifacts: every honeypot served mints one
per-match decoy row, so without a cleanup path a busy public arena grows that table
forever. This verifies:
  * the seeded BASE pool (prompt_id="_bogus") is never touched — list_bogus base
    selection still yields exactly the seeded bases;
  * a PENDING (unvoted) match's decoy is never pruned, and its vote still resolves;
  * a RESOLVED match's decoy is reclaimed (voted branch, regardless of TTL);
  * an ORPHANED decoy (match row gone) is reclaimed only by the TTL backstop;
  * a cap-triggered opportunistic prune keeps growth bounded under churn.

MOTHERSHIP-ONLY DEPENDENCY, AND WHY NOTHING IS SKIPPED
Honeypot MINTING — which base template gets picked, how it is mutated, the spot-check
policy — lives in aeon/arena_trust.py, which is mothership-only and NOT part of the public
pod distribution (a pod has no evaluator accounts, so it runs a no-honeypot arena).
The STORAGE and RECLAIM half is entirely public: db.list_bogus, db.count_live_decoys,
db.prune_live_decoys, db.delete_artifact and arena.submit_vote all ship here, and that is
where every invariant above is actually enforced. So when arena_trust is absent this file
mints the decoy rows itself through the very same public db API the mothership builder
uses, and every assertion below still executes against real pod code. No assertion is
skipped in either mode; only the minting call site differs.

Runs fully offline (temp SQLite). From the mvp dir:  python test_arena_honeypot.py
"""
import os
import random
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

try:                                    # mothership-only; absent in the public pod repo
    from aeon import arena_trust as _trust                 # noqa: E402
except ImportError:
    _trust = None

# The cap/TTL policy constants live with the minter. Mirror them when it is absent, so the
# reclaim behaviour under test is driven by the same numbers in both modes.
_CAP = getattr(_trust, "_LIVE_DECOY_CAP", 200)
_TTL = getattr(_trust, "_LIVE_DECOY_TTL", 6 * 3600)
MODE = ("mothership (arena_trust present — real builder)" if _trust else
        "pod (arena_trust absent — decoys minted via the public db API)")

db.init_db()

PASS = 0
MINTED = 0


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
_LOCAL_BASES = 5          # only used when arena_trust is absent and seed_bogus() no-ops


def _mint_decoy_local(kind):
    """Pod-side stand-in for arena_trust.build_test_match. Mints ONE per-match decoy and
    pairs it against a real artifact using exactly the public db API — including the same
    cap-triggered opportunistic prune the mothership builder performs. The decoy's HTML is
    mutated from a base template so every served decoy is byte-unique, which is precisely
    why these rows accumulate and need reclaiming."""
    if db.count_live_decoys(kind) >= _CAP:
        db.prune_live_decoys(kind=kind, ttl=_TTL)
    base = random.choice(bases(kind))
    real = [a for a in db.list_artifacts(kind=kind) if a["ok"]]   # list_artifacts drops bogus
    assert real, "expected a real %s artifact from seed_demo()" % kind
    r = random.choice(real)
    did = "decoy-" + uuid.uuid4().hex[:10]
    db.save_artifact(did, kind=kind, prompt_id="_bogus_live", model="demo-decoy",
                     html=(db.get_artifact(base["id"])["html"] + "<!--%s-->" % did),
                     ok=True, bogus=True)
    mid = uuid.uuid4().hex[:14]
    side = random.choice(("a", "b"))
    a_id, b_id = (did, r["id"]) if side == "a" else (r["id"], did)
    db.create_match(mid, user_id=user["id"], kind=kind, prompt_id=r["prompt_id"],
                    a_id=a_id, b_id=b_id, is_test=True, bogus_side=side)
    return mid


def honeypot(kind):
    """Build one honeypot directly (bypassing _should_test randomness). Returns
    (match_id, real_winning_side, decoy_artifact_id)."""
    global MINTED
    if _trust:
        payload = _trust.build_test_match(user, kind)
        assert payload, "expected a honeypot payload"
        mid = payload[0]                   # (match_id, kind, prompt_id) — arena shapes it
    else:
        mid = _mint_decoy_local(kind)
    MINTED += 1
    m = db.get_match(mid)
    decoy_id = m["a_id"] if m["bogus_side"] == "a" else m["b_id"]
    real_win = "a" if m["bogus_side"] == "b" else "b"   # voting the REAL side == a PASS
    return mid, real_win, decoy_id


# ---------- setup: real demo artifacts + the bogus base pool ----------
print("mode:", MODE)
arena.seed_demo()
arena.seed_bogus()          # real on the mothership; a documented no-op on a pod
user = mk_user("alice")

if not bases():             # pod: no arena_trust, so plant a deterministic local base pool
    for k in KINDS:
        for i in range(_LOCAL_BASES):
            db.save_artifact("selfbase-%s-%d" % (k, i), kind=k, prompt_id="_bogus",
                             model="demo-decoy", bogus=True, ok=True,
                             html="<!DOCTYPE html><title>bogus base %s %d</title>" % (k, i))

# DERIVED, never hardcoded. seed_bogus() plants one row per (kind x template) and that
# template pool grows over time, so pinning a literal turns every new decoy template into a
# test failure — it already did: the pool grew past 5 and this file failed on a change that
# was entirely correct. The property under test is that the base pool is INVARIANT across
# mint / prune / orphan, so the baseline is measured once, here, right after seeding.
N_PER_KIND = len(bases(KINDS[0]))
N_BASES = len(bases())

ok(N_PER_KIND > 0 and N_BASES == N_PER_KIND * len(KINDS),
   "seed: %d base templates (%d per kind, derived not hardcoded)" % (N_BASES, N_PER_KIND))
ok(all(len(bases(k)) == N_PER_KIND for k in KINDS),
   "seed: exactly %d bases per kind" % N_PER_KIND)
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
n = db.prune_live_decoys(kind="app", ttl=_TTL)
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

# ---------- (5) cap-triggered opportunistic prune bounds growth ----------
orig_cap = _CAP
_CAP = 5
if _trust:
    _trust._LIVE_DECOY_CAP = _CAP
try:
    for _ in range(20):
        mid, win, _bog = honeypot("app")
        arena.submit_vote(user, mid, win)          # resolve each so it becomes reclaimable
    got = db.count_live_decoys("app")
    ok(got <= _CAP,
       "cap: live-decoy pool stays bounded (<= cap=%d) across 20 build+vote cycles (got %d)"
       % (_CAP, got))
finally:
    _CAP = orig_cap
    if _trust:
        _trust._LIVE_DECOY_CAP = orig_cap

# ---------- (6) base selection integrity after all the churn ----------
for k in KINDS:
    b = bases(k)
    ok(len(b) == N_PER_KIND and all(x["prompt_id"] == "_bogus" for x in b),
       "base-select: kind %r still resolves to exactly the %d seeded bases" % (k, N_PER_KIND))
ok(len(db.list_bogus("app")) == N_PER_KIND + db.count_live_decoys("app"),
   "base-select: list_bogus('app') = %d bases + the remaining live decoys "
   "(filter does real work)" % N_PER_KIND)

# ---------- (7) the run did not silently degenerate into a no-op ----------
ok(MINTED >= 22, "self-check: %d decoys were actually minted and churned" % MINTED)

print("\nALL %d CHECKS PASS  [%s]" % (PASS, MODE))
