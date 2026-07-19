"""Self-test: pod/arena_gen.py generation + aeon/ingest.py artifact ingest.

Runs fully offline (mock target, temp SQLite). From the mvp dir:
    python test_arena_gen.py
"""
import os
import sys
import tempfile
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# Point the DB at a throwaway SQLite file BEFORE any aeon import (db.py reads the
# env at import time; AEON_DB_URL would select the prod Postgres backend — kill it).
_TMP = tempfile.mkdtemp(prefix="aeon-arena-selftest-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")
os.environ.pop("AEON_DB_URL", None)

from aeon import arena, db, ingest                      # noqa: E402
from pod import arena_gen                               # noqa: E402
from pod.arena_gen import generate_for_model            # noqa: E402

db.init_db()                                            # fresh schema in the temp sqlite

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("PASS:", label)


# ---------- (a) mock generation ----------
progress = []
arts = generate_for_model("mock", "mock-model", per_kind=2, seed=7,
                          progress_cb=lambda d, t, it: progress.append((d, t)))
ok(len(arts) == 2 * len(arena.KINDS), "mock: per_kind*3 artifacts (%d)" % len(arts))
ok(all(a["ok"] for a in arts), "mock: all ok")
ok(all(a["html"].lstrip().lower().startswith("<!doctype html") for a in arts),
   "mock: all html documents")
ok(all(set(a) >= {"kind", "prompt_id", "title", "html", "ok", "gen_ms", "bytes"} for a in arts),
   "mock: item shape")
ok(all(a["bytes"] == len(a["html"].encode("utf-8")) and 0 < a["bytes"] <= arena_gen.MAX_HTML_BYTES
       for a in arts), "mock: bytes accurate + capped")
for kind in arena.KINDS:
    ok(sum(1 for a in arts if a["kind"] == kind) == 2, "mock: 2 artifacts for kind %r" % kind)
ok(progress == [(i + 1, len(arts)) for i in range(len(arts))], "mock: progress_cb called per item")

# ---------- (b) seed determinism ----------
ids_m1 = [a["prompt_id"] for a in generate_for_model("mock", "model-A", per_kind=2, seed=42)]
ids_m2 = [a["prompt_id"] for a in generate_for_model("mock", "model-B", per_kind=2, seed=42)]
ids_s2 = [a["prompt_id"] for a in generate_for_model("mock", "model-A", per_kind=2, seed=43)]
ok(ids_m1 == ids_m2, "seed: same seed -> identical prompt_ids across models")
ok(ids_m1 != ids_s2, "seed: different seed -> different prompt_ids")
ok(all(arena.find_prompt(a["kind"], a["prompt_id"]) for a in arts), "seed: picks are real prompts")

# ---------- (b2) guaranteed god slot ----------
# When a kind carries god_mode prompts, every draw reserves ONE slot for a god challenge —
# god-tier generation is a reliable part of every bench, not a lottery ticket.
_saved_prompts = arena.PROMPTS
arena.PROMPTS = {
    "app": ([{"id": "god.x%d" % i, "title": "G%d" % i, "brief": "b", "prompt": "p",
              "difficulty": "god_mode"} for i in range(2)]
            + [{"id": "app.n%d" % i, "title": "N%d" % i, "brief": "b", "prompt": "p",
                "difficulty": "medium"} for i in range(6)]),
    "game": [{"id": "game.n%d" % i, "title": "N%d" % i, "brief": "b", "prompt": "p"}
             for i in range(4)],                     # no god prompts -> classic draw
    "animation": [{"id": "anim.g0", "title": "G", "brief": "b", "prompt": "p",
                   "difficulty": "god_mode", "agent_only": True}],  # agent_only never draws
}
try:
    for s in (1, 2, 3, 99):
        sel = arena_gen.pick_prompts(per_kind=3, seed=s)
        app_picks = [p for k, p in sel if k == "app"]
        ok(sum(1 for p in app_picks if p["id"].startswith("god.")) == 1 and len(app_picks) == 3,
           "god slot: seed %d draw has exactly one god pick among 3 (cost unchanged)" % s)
    g1 = [p["id"] for k, p in arena_gen.pick_prompts(per_kind=3, seed=5)]
    g2 = [p["id"] for k, p in arena_gen.pick_prompts(per_kind=3, seed=5)]
    ok(g1 == g2, "god slot: draw stays deterministic per seed")
    game_picks = [p for k, p in arena_gen.pick_prompts(per_kind=3, seed=5) if k == "game"]
    ok(len(game_picks) == 3 and all(not p["id"].startswith("god.") for p in game_picks),
       "god slot: a kind without god prompts draws exactly as before")
    anim_picks = [p for k, p in arena_gen.pick_prompts(per_kind=1, seed=5) if k == "animation"]
    ok(anim_picks == [], "god slot: agent_only god prompts stay out of the direct pool")
finally:
    arena.PROMPTS = _saved_prompts

# ---------- failure path: broken target never raises ----------
class _Boom:
    def chat(self, *a, **k):
        raise RuntimeError("connection refused")

_orig = arena_gen._make_target
arena_gen._make_target = lambda *a, **k: _Boom()
try:
    failed = generate_for_model("http://127.0.0.1:1/v1", "dead-model", per_kind=1, seed=1)
finally:
    arena_gen._make_target = _orig
ok(len(failed) == len(arena.KINDS) and all(not a["ok"] and a["html"] == "" for a in failed),
   "failure: target errors -> ok=False, html='', no raise")

# ---------- (c) ingest path (temp sqlite) ----------
GOOD_HTML = "<!DOCTYPE html><html><body><h1>hi</h1></body></html>"
BIG_HTML = "<!DOCTYPE html><html><body>" + "x" * (250 * 1024) + "</body></html>"

def art(i, **over):
    d = {"kind": "app", "prompt_id": "app.tip", "title": "t%d" % i, "html": GOOD_HTML,
         "ok": True, "gen_ms": 10.0 + i, "bytes": len(GOOD_HTML)}
    d.update(over)
    return d

artifacts = [
    art(1),                                        # saved
    art(2, kind="game", prompt_id="game.snake"),   # saved
    art(3, ok=False),                              # skipped: failed generation
    art(4, html="   "),                            # skipped: empty html
    art(5, html=BIG_HTML),                         # saved TRUNCATED to 200KB
    art(6, kind="weird"),                          # skipped: unknown kind
    art(7, prompt_id="<img src=x>"),               # saved with sanitized prompt_id
] + [art(10 + i, kind="animation", prompt_id="anim.balls",           # 5 DISTINCT generations of one
         html=GOOD_HTML.replace("hi", "hi v%d" % i)) for i in range(5)]  # prompt — all must save
artifacts.append(art(20))                           # byte-identical resend of item 1 -> content-deduped
base_saved = 9                                      # items 1,2,5,7 + 5 animations (dupe of 1 skipped)
base_len = len(artifacts)
# Add enough valid extras to prove the configurable MAX_ARTIFACTS cap still applies.
for i in range(max(0, ingest.MAX_ARTIFACTS - base_len + 1)):
    artifacts.append(art(100 + i, prompt_id="app.extra%d" % i))
expected_saved = base_saved + max(0, ingest.MAX_ARTIFACTS - base_len)

pod = {"run_id": "run" + uuid.uuid4().hex[:8], "model": 'evil<script>"m"`x`' + "Y" * 100,
       "suite_id": "aeon-suite-v1", "board": "text"}
results = [{"case_id": "c1", "category": "math", "score": 1.0},
           {"case_id": "c2", "category": "code", "score": 0.5}]
bundle = {"results": results, "artifacts": artifacts}

# Artifacts persist on EVERY checkpoint (content-deduped) — riding only the final commit
# lost a real submission's gallery items when its last POST never arrived (PrismaAURA).
ingest._commit(pod, bundle, final=False)           # checkpoint 1
rows = db.list_artifacts(with_html=True)
ok(len(rows) == expected_saved, "ingest: FIRST checkpoint already saves artifacts (%d, got %d)" % (expected_saved, len(rows)))
ingest._commit(pod, bundle, final=False)           # checkpoint resend
ok(len(db.list_artifacts()) == expected_saved, "ingest: checkpoint resend dedups by content (no duplicates)")
ingest._commit(pod, bundle, final=True)            # final commit
rows = db.list_artifacts(with_html=True)
ok(len(rows) == expected_saved, "ingest: final commit adds nothing new (still exactly %d, got %d)" % (expected_saved, len(rows)))
ok(all(not set('<>"\'`') & set(r["model"]) and len(r["model"]) <= 80 for r in rows),
   "ingest: model sanitized (no markup, <=80 chars)")
ok(all(not set('<>"\'`') & set(r["prompt_id"]) for r in rows), "ingest: prompt_id sanitized")
ok(all(len(r["html"].encode("utf-8")) <= ingest.MAX_ARTIFACT_HTML for r in rows),
   "ingest: html truncated to 200KB")
ok(any(len(r["html"].encode("utf-8")) == ingest.MAX_ARTIFACT_HTML for r in rows),
   "ingest: oversized artifact was truncated, not dropped")
ok(db.get_run(pod["run_id"])["status"] == "succeeded" and len(db.result_case_ids(pod["run_id"])) == 2,
   "ingest: results committed alongside artifacts")

# the real-world duplicate-FINAL guard: claim_pod_run consumes the run exactly once
rid2 = "run" + uuid.uuid4().hex[:8]
db.create_pod_run(rid2, public_key="pk", run_nonce="n", run_token="t",
                  model="m", suite_id="s", board="text")
ok(db.claim_pod_run(rid2, "committed") is True and db.claim_pod_run(rid2, "committed") is False,
   "ingest: duplicate FINAL submit loses the claim race (no re-commit)")

# backwards compat: bundle without "artifacts"
pod2 = {"run_id": "run" + uuid.uuid4().hex[:8], "model": "plain-model",
        "suite_id": "aeon-suite-v1", "board": "text"}
ingest._commit(pod2, {"results": results}, final=True)
ok(len(db.list_artifacts()) == expected_saved and db.get_run(pod2["run_id"])["status"] == "succeeded",
   "ingest: bundle without 'artifacts' fully backwards compatible")

print("\nALL %d CHECKS PASS" % PASS)
