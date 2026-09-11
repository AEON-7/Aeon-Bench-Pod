"""Self-test: unified benchmark cards + full-parity card compare (aeon/cards.py).

Covers the API contract both dashboard sides build against:
  * jg grouping: one job_group across text / agentic(x2 harnesses) / vision / audio /
    perf (+ arena artifacts) -> ONE card with correct per-board payloads;
  * legacy clustering: 3 runs 1h apart cluster into one 'lg:' card, a 4h gap splits,
    and a run WITH job_group never joins a legacy cluster;
  * compare_cards parity: a section one side lacks is null but the KEY is present;
  * card_id resolution for both forms ('jg:<group>' and 'lg:<member run id>');
  * flagged run -> card flagged_any + per-board flagged;
  * malformed env_json / recipe rows are skipped, never a crash.

Runs fully offline (temp SQLite). From the mvp dir:  python test_submission_cards.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

# Point the DB layer at a throwaway SQLite file BEFORE aeon.db is imported.
os.environ.pop("AEON_DB_URL", None)
_TMP = tempfile.mkdtemp(prefix="aeon-cards-test-")
os.environ["AEON_DB"] = os.path.join(_TMP, "test.db")

from aeon import cards, db  # noqa: E402

db.init_db()

PASS = 0


def ok(cond, label):
    global PASS
    assert cond, "FAIL: " + label
    PASS += 1
    print("PASS:", label)


HW_ENV = {"hardware": {"detected_label": "single DGX Spark (GB10)"},
          "engine": {"name": "vllm"}, "concurrency": 8}
RECIPE = {"engine": "vllm", "image": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
          "image_digest": "sha256:" + "ab" * 8, "spec_decode": "dflash",
          "drafter_repo": "z-lab/Uni-DFlash", "drafter_n": 4,
          "flags": ["--gpu-memory-utilization", "0.70",
                    "--served-model-name", "model-under-test", "--port", "8000"]}


def mk_run(model, *, board="text", cases=(), env=HW_ENV, job_group=None, harness=None,
           harness_version=None, hf_repo=None, recipe=None, suite_id="aeon-suite-v3",
           started_at=None, status="succeeded"):
    """cases: [(case_id, category, score, raw_output)]"""
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="pod-submission", judge_model=None,
                  judge_is_self=False, suite_id=suite_id, suite_hash="h" * 12,
                  n_cases=len(cases), params={}, env=env, board=board, hf_repo=hf_repo,
                  trust_tier="attested" if hf_repo else "self_reported",
                  harness=harness, harness_version=harness_version, recipe=recipe,
                  job_group=job_group)
    for cid, cat, score, raw in cases:
        db.save_result(rid, cid, category=cat, tier=1, status="scored", score=score,
                       raw_output=raw, evidence={}, speed={}, board=board)
    db.finish_run(rid, status)
    if started_at is not None:
        with db.connect() as c:
            c.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                      (started_at, started_at + 600, rid))
    return rid


def mk_perf_run(model, *, job_group=None, cells=(), started_at=None, hf_repo=None):
    """cells: [(case_id, evidence_dict)] — perf metrics live in evidence_json."""
    rid = uuid.uuid4().hex[:12]
    db.create_run(rid, model=model, target_url="pod-submission", judge_model=None,
                  judge_is_self=False, suite_id="aeon-perf-v1", suite_hash=None,
                  n_cases=len(cells), params={}, env=HW_ENV, board="perf",
                  hf_repo=hf_repo, trust_tier="attested" if hf_repo else "self_reported",
                  job_group=job_group)
    for cid, ev in cells:
        db.save_result(rid, cid, category="perf", tier=0, status="scored", score=None,
                       raw_output="", evidence=ev, speed={}, board="perf")
    db.finish_run(rid, "succeeded")
    if started_at is not None:
        with db.connect() as c:
            c.execute("UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                      (started_at, started_at + 600, rid))
    return rid


def card_by_id(cid):
    return next((c for c in cards.submission_cards(limit=100)["cards"]
                 if c["card_id"] == cid), None)


# ---------- 1) jg grouping: one job across 5 boards + arena -> ONE card ----------
G = "a1" * 12
REPO = "lab/uni-model"
rid_text = mk_run(REPO, cases=[("t.m1", "math", 1.0, "4"),
                               ("t.c1", "codegen", 0.5, "x" * 5000)],
                  job_group=G, hf_repo=REPO, recipe=RECIPE)
rid_h1 = mk_run(REPO, cases=[("a.1", "agentic", 1.0, "done"), ("a.2", "agentic", 0.0, "nope")],
                job_group=G, hf_repo=REPO, harness="openclaw", harness_version="2026.1.0",
                suite_id="agentic-v2")
rid_h2 = mk_run(REPO, cases=[("a.1", "agentic", 1.0, "done")],
                job_group=G, hf_repo=REPO, harness="opencode", harness_version="0.9.1",
                suite_id="agentic-v2")
rid_vis = mk_run(REPO, board="vision", cases=[("v.1", "OCR", 1.0, "text")],
                 job_group=G, hf_repo=REPO, suite_id="aeon-vision-v2")
rid_aud = mk_run(REPO, board="audio", cases=[("au.1", "ASR", 0.75, "words")],
                 job_group=G, hf_repo=REPO, suite_id="aeon-audio-v1")
rid_perf = mk_perf_run(REPO, job_group=G, hf_repo=REPO, cells=[
    ("perf.direct.Math.c1", {"ttft_ms_mean": 100.0, "tpot_ms_mean": 10.0,
                             "decode_tps_mean": 42.0, "agg_decode_tps": 42.0,
                             "prefill_tps_mean": 500.0}),
    ("perf.direct.Coding.c8", {"ttft_ms_mean": 180.0, "tpot_ms_mean": 12.0,
                               "decode_tps_mean": 38.0, "agg_decode_tps": 300.0,
                               "prefill_tps_mean": 450.0}),
    ("perf.bogus", {"agg_decode_tps": 9999.0}),          # malformed cell id -> ignored
])
db.save_artifact("art1", kind="app", prompt_id="p1", model=REPO, html="<p>x</p>", ok=True)
db.save_artifact("art2", kind="game", prompt_id="p2", model=f"{REPO} @openclaw",
                 html="<p>y</p>", ok=True)
db.save_artifact("artX", kind="app", prompt_id="p1", model="other/model", html="<p>z</p>")

out = cards.submission_cards(limit=100)
uni = [c for c in out["cards"] if c["canonical"] == REPO]
ok(len(uni) == 1, "jg: the whole 6-run job folds into ONE card")
card = uni[0]
ok(card["card_id"] == "jg:" + G, "jg card is keyed by its job_group")
ok(sorted(card["run_ids"]) == sorted([rid_text, rid_h1, rid_h2, rid_vis, rid_aud, rid_perf]),
   "jg card carries every run id of the job")
ok(card["hf_repo"] == REPO and card["trust_tier"] == "attested", "identity: hf_repo + tier")
ok(card["hardware"] == "single DGX Spark (GB10)" and card["engine"] == "vllm",
   "hardware + engine from env_json")
b = card["boards"]
ok(b["text"] and b["text"]["run"] == rid_text and b["text"]["composite"] == 75.0
   and b["text"]["categories"] == {"math": 100.0, "codegen": 50.0}
   and b["text"]["n_cases"] == 2 and b["text"]["suite_id"] == "aeon-suite-v3"
   and b["text"]["flagged"] is False, "text board payload (composite = category mean)")
ok([h["harness"] for h in b["agentic"]] == ["openclaw", "opencode"],
   "agentic board lists BOTH harness runs")
h1 = b["agentic"][0]
ok(h1["run"] == rid_h1 and h1["score"] == 50.0 and h1["n_cases"] == 2
   and h1["harness_version"] == "2026.1.0", "agentic entry: mean task score + version")
ok(b["vision"] and b["vision"]["composite"] == 100.0, "vision board payload")
ok(b["audio"] and b["audio"]["composite"] == 75.0, "audio board payload")
ok(b["video"] is None, "video slot the job never ran is null")
ok(b["perf"] and b["perf"]["run"] == rid_perf and b["perf"]["peak_agg_tps"] == 300.0
   and b["perf"]["peak_single_tps"] == 42.0 and b["perf"]["conc_levels"] == [1, 8],
   "perf board: peak cohort + single-stream + rungs (malformed cell ignored)")
ok(b["arena"] and b["arena"]["n_artifacts"] == 2
   and b["arena"]["kinds"] == {"app": 1, "game": 1},
   "arena chip counts ONLY this job's artifacts (plain + @harness; other models excluded)")

# ---------- 2) legacy clustering (job_group NULL) ----------
LEG = "lab/legacy-model"
t0 = time.time() - 40000
rid_l1 = mk_run(LEG, cases=[("t.m1", "math", 1.0, "4")], started_at=t0)
rid_l2 = mk_run(LEG, board="vision", cases=[("v.1", "OCR", 0.5, "y")],
                suite_id="aeon-vision-v2", started_at=t0 + 3600)
rid_l3 = mk_perf_run(LEG, cells=[("perf.direct.Math.c1", {"decode_tps_mean": 10.0,
                                                          "agg_decode_tps": 10.0})],
                     started_at=t0 + 7200)
rid_l4 = mk_run(LEG, cases=[("t.m1", "math", 0.0, "5")], started_at=t0 + 7200 + 14400)
rid_lg = mk_run(LEG, cases=[("t.m1", "math", 1.0, "4")], job_group="b2" * 12,
                started_at=t0 + 1800)                    # inside the window, but grouped

leg_cards = [c for c in cards.submission_cards(limit=100)["cards"] if c["canonical"] == LEG]
ok(len(leg_cards) == 3, "legacy model: 3h-gap cluster + split run + jg run = 3 cards")
cluster = card_by_id("lg:" + rid_l1)
ok(cluster is not None, "legacy cluster is keyed by its FIRST run id")
ok(sorted(cluster["run_ids"]) == sorted([rid_l1, rid_l2, rid_l3]),
   "1h-apart runs cluster into one card (text+vision+perf)")
ok(card_by_id("lg:" + rid_l4) is not None
   and card_by_id("lg:" + rid_l4)["run_ids"] == [rid_l4],
   "a 4h gap starts a NEW legacy card")
ok(card_by_id("jg:" + "b2" * 12)["run_ids"] == [rid_lg],
   "a run WITH job_group never joins a legacy cluster")
ok(cluster["boards"]["agentic"] == [], "no harness runs -> agentic is [] (not null)")

# ---------- 3) compare_cards: parity nulls + both id forms ----------
cmp_ = cards.compare_cards("jg:" + G, "lg:" + rid_l2)     # NON-first member id resolves too
ok("error" not in cmp_, "compare resolves both id forms (jg + lg via any member run)")
ok(cmp_["b"]["card_id"] == "lg:" + rid_l1,
   "lg side resolved via a member run still reports the canonical (first-run) card id")
ok(tuple(sorted(cmp_["sections"])) == tuple(sorted(cards.SECTION_KEYS)),
   "every section key is ALWAYS present")
ok(cmp_["sections"]["audio"]["a"] is not None and cmp_["sections"]["audio"]["b"] is None,
   "parity: the side without audio is null, key still present")
ok(cmp_["sections"]["agentic"]["b"] is None, "parity: no harness runs -> agentic side null")
txt = cmp_["sections"]["text"]["a"]
ok(txt["composite"] == 75.0 and txt["suite_id"] == "aeon-suite-v3"
   and txt["suite_hash"] == "h" * 12, "text section header")
by_case = {c["case_id"]: c for c in txt["cases"]}
ok(by_case["t.m1"]["answer"] == "4" and by_case["t.m1"]["score"] == 1.0
   and by_case["t.m1"]["category"] == "math", "text case carries the raw answer + score")
ok(len(by_case["t.c1"]["answer"]) == 4000, "answers are truncated to 4000 chars")
ag = cmp_["sections"]["agentic"]["a"]["harnesses"]
ok(set(ag) == {"openclaw", "opencode"} and ag["openclaw"]["score"] == 50.0
   and ag["openclaw"]["version"] == "2026.1.0"
   and {t["case_id"]: t["score"] for t in ag["openclaw"]["tasks"]} == {"a.1": 1.0, "a.2": 0.0},
   "agentic section: per-harness score + raw per-task scores")
pf = cmp_["sections"]["perf"]["a"]
ok(pf["peak_agg_tps"] == 300.0 and pf["peak_cell"] == {"category": "Coding", "conc": 8}
   and pf["conc_levels"] == [1, 8], "perf section peaks + provenance cell")
cell = pf["direct"][1]["Math"]
ok(set(cell) == {"ttft_ms", "tpot_ms", "decode_tps", "agg_decode_tps", "prefill_tps"}
   and cell["decode_tps"] == 42.0, "direct grid cells carry EXACTLY the contract metrics")
ok(cmp_["sections"]["perf"]["b"] is not None and cmp_["sections"]["perf"]["b"]["peak_agg_tps"] == 10.0,
   "the legacy side's perf run is compared too")
ar = cmp_["sections"]["arena"]["a"]
ok(ar and sorted(x["aid"] for x in ar["artifacts"]) == ["art1", "art2"]
   and cmp_["sections"]["arena"]["b"] is None, "arena artifacts listed for a, null for b")
rc = cmp_["sections"]["recipe"]["a"]
ok(rc["engine"] == "vllm" and rc["image"] == RECIPE["image"]
   and rc["image_digest"] == RECIPE["image_digest"], "recipe section: engine + image provenance")
ok("--served-model-name" not in rc["serve_flags"] and "--gpu-memory-utilization" in rc["serve_flags"],
   "recipe serve_flags are the SANITIZED champion flags (bench wiring stripped)")
ok(rc["spec_decode"] and rc["spec_decode"]["repo"] == "z-lab/Uni-DFlash"
   and rc["spec_decode"]["n"] == 4, "spec_decode is the drafter disclosure object")
ok(rc["spec_decode"]["method"] == "dflash" and rc["spec_decode"]["uses_drafter"] is True,
   "spec_decode disclosure is method-aware (dflash-with-drafter here)")
ok(cards.compare_cards("jg:" + G, "lg:nonexistent").get("error"), "unknown lg card -> error")
ok(cards.compare_cards("jg:nope", "jg:" + G).get("error"), "unknown jg card -> error")
ok(cards.compare_cards("jg:" + G, "lg:" + rid_lg).get("error"),
   "a job_group run's id never resolves as a legacy cluster")

# ---------- 4) flagged run -> flagged_any + per-board flagged ----------
db.flag_run(rid_vis, True, "capability mismatch")
card = card_by_id("jg:" + G)
ok(card["flagged_any"] is True, "any flagged run flags the whole card")
ok(card["boards"]["vision"]["flagged"] is True and card["boards"]["text"]["flagged"] is False,
   "the flag is disclosed per board, not smeared")

# ---------- 5) DSpark spec-decode disclosure: drafter form + in-checkpoint form ----------
DSPARK_DRAFTER_JSON = '{"method":"dspark","model":"/drafter","num_speculative_tokens":7}'
DSPARK_NATIVE_JSON = '{"method":"dspark","num_speculative_tokens":4}'
rid_dspd = mk_run("lab/dspark-drafter", cases=[("t.m1", "math", 1.0, "4")],
                  hf_repo="lab/dspark-drafter",
                  recipe={"engine": "aeon-vllm-ultimate",
                          "image": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
                          "drafter_repo": "deepseek-ai/dspark_qwen3_8b_block7",
                          "flags": ["--gpu-memory-utilization", "0.70",
                                    "--speculative-config", DSPARK_DRAFTER_JSON,
                                    "--served-model-name", "model-under-test"]})
rid_dspn = mk_run("lab/dspark-native", cases=[("t.m1", "math", 1.0, "4")],
                  hf_repo="lab/dspark-native",
                  recipe={"engine": "aeon-vllm-ultimate",
                          "image": "ghcr.io/aeon-7/aeon-vllm-ultimate:latest",
                          "flags": ["--gpu-memory-utilization", "0.70",
                                    "--speculative-config", DSPARK_NATIVE_JSON,
                                    "--served-model-name", "model-under-test"]})
cmp_dsp = cards.compare_cards("lg:" + rid_dspd, "lg:" + rid_dspn)
ok("error" not in cmp_dsp, "dspark cards resolve on both sides")
rcd = cmp_dsp["sections"]["recipe"]["a"]
ok(rcd["spec_decode"] and rcd["spec_decode"]["method"] == "dspark"
   and rcd["spec_decode"]["repo"] == "deepseek-ai/dspark_qwen3_8b_block7"
   and rcd["spec_decode"]["n"] == 7,
   "dspark drafter form: disclosure names the block7 drafter repo + n")
ok(rcd["spec_decode"]["uses_drafter"] is True,
   "dspark drafter form: uses_drafter=True (pull + /drafter mount)")
ok(rcd["serve_flags"][rcd["serve_flags"].index("--speculative-config") + 1] == DSPARK_DRAFTER_JSON,
   "dspark drafter option JSON survives the champion sanitizer byte-identical")
rcn = cmp_dsp["sections"]["recipe"]["b"]
ok(rcn["spec_decode"] and rcn["spec_decode"]["method"] == "dspark"
   and rcn["spec_decode"]["n"] == 4 and rcn["spec_decode"]["repo"] is None,
   "native dspark: method + n disclosed, no drafter repo")
ok(rcn["spec_decode"]["uses_drafter"] is False,
   "native (in-checkpoint) dspark: uses_drafter=False — nothing to pull or mount")
ok(rcn["serve_flags"][rcn["serve_flags"].index("--speculative-config") + 1] == DSPARK_NATIVE_JSON,
   "native dspark option JSON survives the champion sanitizer byte-identical")

# ---------- 6) malformed rows are skipped, never a 500 ----------
rid_bad = mk_run("lab/mangled", cases=[("t.m1", "math", 1.0, "4")])
with db.connect() as c:
    c.execute("UPDATE runs SET env_json='{{{not json', recipe='also not json' WHERE id=?",
              (rid_bad,))
out = cards.submission_cards(limit=100)
bad = [c for c in out["cards"] if c["canonical"] == "lab/mangled"]
ok(len(bad) == 1 and bad[0]["hardware"] is None and bad[0]["engine"] is None,
   "malformed env_json/recipe degrade to nulls — the card still renders")
cmp_bad = cards.compare_cards("lg:" + rid_bad, "jg:" + G)
ok("error" not in cmp_bad and cmp_bad["sections"]["recipe"]["a"] is None,
   "compare over a malformed recipe yields a null recipe side, never a crash")

print(f"\nOK  unified benchmark cards: {PASS} checks passed")
