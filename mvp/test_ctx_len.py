"""Served CONTEXT LENGTH surfacing — extraction, board rows, cards, share card.

Covers the max-context disclosure chain end to end:
  * scoring.ctx_len_from_recipe: vLLM --max-model-len · SGLang --context-length ·
    llama.cpp -c / --ctx-size, each in both the two-token and the "=" form; absent /
    malformed / non-recipe payloads -> None (null = not recorded, never a guess)
  * leaderboard rows carry ctx_len from the best_intelligence_run's recipe, falling
    back to the model's NEWEST recipe-bearing run; recipe-less models carry null
  * unified benchmark cards carry a card-level ctx_len (text run's recipe first)
  * submission detail reproduction payload carries the run's own ctx_len
  * share card: _share_info carries rank + aeon + ctx, and render_model_card produces
    a real PNG (> 10 KB) for it

Runs fully offline (temp SQLite). From the mvp dir:  python test_ctx_len.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Point the DB layer at a throwaway SQLite file BEFORE aeon.db is imported.
os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-ctx-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import cards, db, scoring  # noqa: E402
from aeon import suite as suite_mod  # noqa: E402

db.init_db()

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


# ---- 1) extraction: every engine grammar, both flag forms --------------------------------

E = scoring.ctx_len_from_recipe
check(E({"flags": ["--max-model-len", "65536"]}) == 65536, "vLLM two-token form")
check(E({"flags": ["--gpu-memory-utilization", "0.7", "--max-model-len=131072"]}) == 131072,
      "vLLM = form (after unrelated flags)")
check(E({"flags": ["--context-length", "32768"]}) == 32768, "SGLang two-token form")
check(E({"flags": ["--context-length=32768"]}) == 32768, "SGLang = form")
check(E({"flags": ["-c", "8192"]}) == 8192, "llama.cpp -c form")
check(E({"flags": ["--ctx-size", "4096"]}) == 4096, "llama.cpp --ctx-size long form")
check(E({"command": ["llama-server", "-m", "w.gguf", "-c", "16384"]}) == 16384,
      "bare-metal recipes disclose through their command list")
check(E({"flags": ["--gpu-memory-utilization", "0.70"]}) is None, "no ctx flag -> None")
check(E({"flags": ["-c", "lots"]}) is None, "non-numeric value -> None")
check(E({"flags": ["--max-model-len", "-1"]}) is None, "flag followed by another flag -> None")
check(E({"flags": ["--max-model-len"]}) is None, "trailing flag without a value -> None")
check(E({"flags": "not-a-list"}) is None and E({}) is None and E(None) is None
      and E("x") is None, "malformed / absent recipes -> None, never a raise")
check(E({"flags": ["--max-model-len", "0"]}) is None, "zero is not a served window")

# ---- fixture: three models on the text board ----------------------------------------------

VLLM_RECIPE = {"engine": "vllm", "flags": ["--gpu-memory-utilization", "0.70",
                                           "--max-model-len", "65536"]}
PERF_RECIPE = {"engine": "vllm", "flags": ["--max-model-len", "32768"]}
NOCTX_RECIPE = {"engine": "vllm", "flags": ["--gpu-memory-utilization", "0.70"]}

_N = 0


def _ts():
    global _N
    _N += 1
    return 1_700_000_000 + _N * 100


def _pin(rid, t):
    with db.connect() as c:
        c.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?", (t, t + 600, rid))


def text_run(model, *, score=1.0, recipe=None, job_group=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id=suite_mod.SUITE_ID,
                  suite_hash=suite_mod.suite_hash(), n_cases=len(suite_mod.CASES),
                  params={}, env={}, hf_repo=model, trust_tier="attested",
                  recipe=recipe, job_group=job_group)
    for c in suite_mod.CASES:
        db.save_result(rid, c["id"], category=c["category"], tier=c.get("tier", 1),
                       status="scored", score=score, raw_output="ok", evidence={}, speed={})
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


def perf_run(model, *, recipe=None, job_group=None):
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="http://x", judge_model=None,
                  judge_is_self=True, suite_id="aeon-perf-v1", suite_hash="perf", n_cases=1,
                  params={}, env={"hardware": {"detected_label": "RTX 5090 32GB"}},
                  hf_repo=model, trust_tier="attested", recipe=recipe, job_group=job_group)
    db.save_result(rid, "perf.direct.Math.c8", category="perf", tier=1, status="scored",
                   score=None, raw_output="", evidence={"agg_decode_tps": 100.0}, speed={},
                   board="perf")
    db.finish_run(rid, "succeeded")
    _pin(rid, _ts())
    return rid


DIRECT = "lab/direct-recipe-model"     # best run carries the ctx itself
FALL = "lab/fallback-model"            # best text run has no recipe; a newer perf run does
BARE = "lab/no-recipe-model"           # no recipe anywhere -> null

r_direct = text_run(DIRECT, recipe=VLLM_RECIPE)
r_fall_text = text_run(FALL, score=0.9)                       # best intelligence run, no recipe
perf_run(FALL, recipe=PERF_RECIPE)                            # newest recipe-bearing run
text_run(BARE, score=0.4)

# ---- 2) leaderboard rows -------------------------------------------------------------------

lb = scoring.leaderboard()
rows = {m["canonical"]: m for m in lb["models"]}
check(rows[DIRECT]["ctx_len"] == 65536,
      "row ctx_len comes from the best_intelligence_run's recipe")
check(rows[FALL]["ctx_len"] == 32768,
      "recipe-less best run falls back to the model's newest recipe-bearing run")
check(rows[BARE]["ctx_len"] is None, "no recipe anywhere -> ctx_len null (never a guess)")
check(all("ctx_len" in m for m in lb["models"]), "every board row carries the ctx_len key")

# ---- 3) unified benchmark cards -------------------------------------------------------------

G = "cc" * 12
text_run("lab/card-model", recipe=VLLM_RECIPE, job_group=G)
perf_run("lab/card-model", recipe=PERF_RECIPE, job_group=G)
G2 = "dd" * 12
perf_run("lab/card-perf-only", recipe=PERF_RECIPE, job_group=G2)
G3 = "ee" * 12
text_run("lab/card-bare", recipe=NOCTX_RECIPE, job_group=G3)

by_id = {c["card_id"]: c for c in cards.submission_cards(limit=100)["cards"]}
check(by_id["jg:" + G]["ctx_len"] == 65536, "card ctx_len prefers the text run's recipe")
check(by_id["jg:" + G2]["ctx_len"] == 32768, "text-less card falls through to the perf recipe")
check(by_id["jg:" + G3]["ctx_len"] is None, "recipe without a ctx flag -> card ctx_len null")

# ---- 4) submission detail + share card (app payloads + PNG render) --------------------------

from aeon import app as app_mod  # noqa: E402  (after the DB env pin; import is side-effect free)
from aeon import sharecard  # noqa: E402

run_row = db.get_run(r_direct)
repro = app_mod._reproduction(run_row)
check(repro["ctx_len"] == 65536, "submission detail reproduction payload carries ctx_len")
repro_bare = app_mod._reproduction(db.get_run(text_run("lab/detail-bare")))
check(repro_bare["ctx_len"] is None, "recipe-less run detail carries ctx_len null")

info = app_mod._share_info(DIRECT.replace("/", "__"))
check(info is not None, "share info resolves the canonical key")
# the card fixtures above added board rows, so rank against a FRESH board (what render sees)
lb2 = scoring.leaderboard()
want = next((i + 1, m) for i, m in enumerate(lb2["models"]) if m["canonical"] == DIRECT)
check(info["rank"] == want[0], "share rank = position in scoring.leaderboard() order")
check(info["aeon"] == want[1]["aeon_score"], "share info carries the AEON score")
check(info["ctx_len"] == 65536, "share info carries the served ctx")
check(info["components"]["intelligence"] == want[1]["composite"]
      and "agentic" in info["components"] and "performance" in info["components"],
      "share info carries the component line")

png = sharecard.render_model_card(info)
check(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 10_000,
      f"share card renders a real PNG ({len(png)} bytes) with rank + ctx")
png2 = sharecard.render_model_card(app_mod._share_info(BARE.replace("/", "__")))
check(png2[:8] == b"\x89PNG\r\n\x1a\n" and len(png2) > 10_000,
      "ctx-less model still renders cleanly (chip simply omitted)")
check(sharecard.render_fallback_card()[:8] == b"\x89PNG\r\n\x1a\n",
      "fallback card still renders")
check(sharecard._fmt_ctx(65536) == "64K" and sharecard._fmt_ctx(131072) == "128K"
      and sharecard._fmt_ctx(512) == "512", "PNG ctx grammar: /1024 rounded + K")

print(f"\nOK  ctx_len: {PASSED} checks passed")
