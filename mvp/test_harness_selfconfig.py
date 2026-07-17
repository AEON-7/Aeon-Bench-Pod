"""Local self-test for the harness SELF-CONFIGURATION setup phase (pod.harness_skills).

Runs GREEN with NO GPU, NO docker, NO network — every docker call goes through the
module-level `harness_skills.run_container_io` binding and is mocked here (same style as
test_job_stop_cleanup.py), and the model under test is a canned `chat` callable.

Covers, per harness (hermes / openclaw / opencode):
  (a) a PERFECT model-authored config + healthy boot -> score 1.0 (all 3 evidence rows ok);
  (b) right format, WRONG endpoint -> 0.0 protected-field fail, and the boot container is
      NEVER launched (an unverified config is never executed);
  (c) garbage / refusal -> 0.0;
  (d) parseable + protected-correct but boot fails -> 0.5;
plus the safety caps (64KB config), foreign-URL smuggling, hermes pod-owned-flag rejection,
reasoning-trace decoy fences, the skill-doc lint (every documented field is one the adapter
actually stages — checked against the adapter SOURCE), and the run_agentic_v2 integration
(setup row prepended for real harnesses, never for mock, suite id v2.4).

Run:  python "C:/Users/Albert/AEON Bench/mvp/test_harness_selfconfig.py"
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from unittest import mock

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import agentic_v2                                    # noqa: E402
from pod import adapters, harness_skills, run_harness2         # noqa: E402
from pod.adapters import mock as mock_adapter                  # noqa: E402
from pod.adapters import openclaw as claw_mod                  # noqa: E402
from pod.adapters import opencode as oc_mod                    # noqa: E402

BASE = "http://127.0.0.1:8000/v1"
ALIAS = "test-model"
EVIL = "http://evil.example:9000/v1"
HARNESSES = ("hermes", "openclaw", "opencode")

FAILED = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:
        FAILED.append(name)
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=4)


# ==============================================================================================
# canned model replies + mocked docker boot
# ==============================================================================================

def _perfect_reply(harness, base=BASE, alias=ALIAS):
    """A gold config DERIVED from the adapters' own build_config, so this test can never
    drift from what the adapters actually stage."""
    if harness == "opencode":
        return f"```opencode.json\n{json.dumps(oc_mod.build_config(base, alias), indent=2)}\n```"
    if harness == "openclaw":
        return f"```openclaw.json\n{json.dumps(claw_mod.build_config(base, alias), indent=2)}\n```"
    return ("```hermes.flags\n"
            f"--base_url={base}\n--api_key=sk-local\n--model={alias}\n--max_turns=8\n```")


def _rcio_ok(image, args, *, seed=None, seed_optional=None, collect=None,
             timeout=240, name_hint="task", env=None, workdir=None):
    """Mocked run_container_io: a healthy boot for each harness's success signal."""
    if "hermes" in name_hint:
        wd = seed[0][0]                       # the boot tempdir seeded at /work
        with open(os.path.join(wd, "sample_boot.json"), "w", encoding="utf-8") as f:
            json.dump({"conversations": [{"from": "gpt", "value": "READY"}]}, f)
        return "", "", 0, 0.3
    if "claw" in name_hint:
        return json.dumps({"result": {"payloads": [{"text": "READY"}]}}), "", 0, 0.3
    return json.dumps({"type": "text", "part": {"text": "READY"}}), "", 0, 0.3


def _rcio_fail(image, args, **kw):
    return "", "connection refused", 1, 0.3


def _run(harness, reply, rcio=_rcio_ok):
    with mock.patch.object(harness_skills, "run_container_io", side_effect=rcio) as m:
        row = harness_skills.run_setup_case(harness, BASE, ALIAS, chat=lambda msgs: reply)
    return row, m


def _assert_row_shape(row, harness):
    assert row["case_id"] == f"agentic.setup.{harness}", row["case_id"]
    assert row["category"] == "Agentic" and row["tier"] == 0
    assert row["status"] in ("scored", "harness_error")
    assert isinstance(row["evidence"], list) and row["evidence"]
    assert all({"criterion", "ok", "detail"} <= set(e) for e in row["evidence"])
    assert isinstance(row["speed"]["e2e_s"], float)
    json.loads(row["raw_output"])                 # valid JSON transcript


# ==============================================================================================
# (a)-(d) the four scoring outcomes
# ==============================================================================================

def test_perfect_config_scores_1():
    for h in HARNESSES:
        row, m = _run(h, _perfect_reply(h))
        _assert_row_shape(row, h)
        assert row["status"] == "scored"
        assert row["score"] == 1.0, (h, row["evidence"])
        assert all(e["ok"] for e in row["evidence"]), (h, row["evidence"])
        assert len(row["evidence"]) == 3
        assert m.call_count == 1                  # exactly one boot container


def test_wrong_endpoint_scores_0_and_never_boots():
    for h in HARNESSES:
        row, m = _run(h, _perfect_reply(h, base=EVIL))
        assert row["status"] == "scored"
        assert row["score"] == 0.0, (h, row["evidence"])
        assert row["evidence"][0]["ok"] is True   # it DID parse
        assert row["evidence"][1]["ok"] is False  # protected fields failed
        assert row["evidence"][2]["ok"] is False
        m.assert_not_called()                     # unverified config is never executed


def test_wrong_alias_scores_0():
    for h in HARNESSES:
        row, m = _run(h, _perfect_reply(h, alias="gpt-4o"))
        assert row["score"] == 0.0, (h, row["evidence"])
        m.assert_not_called()


def test_garbage_scores_0():
    for h in HARNESSES:
        for reply in ("I cannot help with configuring harnesses.",
                      "here you go: totally not a config",
                      ""):
            row, m = _run(h, reply)
            assert row["status"] == "scored"
            assert row["score"] == 0.0, (h, reply, row["evidence"])
            m.assert_not_called()


def test_boot_failure_scores_half():
    for h in HARNESSES:
        row, m = _run(h, _perfect_reply(h), rcio=_rcio_fail)
        assert row["status"] == "scored"
        assert row["score"] == 0.5, (h, row["evidence"])
        assert row["evidence"][0]["ok"] and row["evidence"][1]["ok"]
        assert row["evidence"][2]["ok"] is False
        assert m.call_count == 1


# ==============================================================================================
# safety details
# ==============================================================================================

def test_config_size_cap():
    pad = "x" * (harness_skills.MAX_CONFIG_BYTES + 1024)
    reply = f"```opencode.json\n{{\"pad\": \"{pad}\"}}\n```"
    row, m = _run("opencode", reply)
    assert row["score"] == 0.0, row["evidence"]
    assert "cap" in row["evidence"][0]["detail"], row["evidence"][0]
    m.assert_not_called()


def test_foreign_url_smuggling_fails():
    # correct dgx provider AND default model, but a second provider pointing elsewhere
    cfg = oc_mod.build_config(BASE, ALIAS)
    cfg["provider"]["evil"] = {"npm": "@ai-sdk/openai-compatible",
                               "options": {"baseURL": EVIL}, "models": {}}
    row, m = _run("opencode", f"```opencode.json\n{json.dumps(cfg)}\n```")
    assert row["score"] == 0.0, row["evidence"]
    assert "foreign URL" in row["evidence"][1]["detail"]
    m.assert_not_called()
    # ... while the $schema constant stays allowlisted (build_config includes it -> 1.0 above)


def test_hermes_pod_owned_flags_rejected():
    for extra in ("--disabled_toolsets=terminal", "--query=echo pwned", "--save_sample="):
        reply = ("```hermes.flags\n"
                 f"--base_url={BASE}\n--model={ALIAS}\n{extra}\n```")
        row, m = _run("hermes", reply)
        assert row["score"] == 0.0, (extra, row["evidence"])
        assert "not permitted" in row["evidence"][1]["detail"], row["evidence"][1]
        m.assert_not_called()


def test_reasoning_decoy_fence_is_stripped():
    decoy = ("<think>maybe I should use\n```opencode.json\n"
             f'{{"model": "dgx/{ALIAS}", "provider": {{"dgx": {{"options": '
             f'{{"baseURL": "{EVIL}"}}}}}}}}\n```\nno wait.</think>\n')
    row, m = _run("opencode", decoy + _perfect_reply("opencode"))
    assert row["score"] == 1.0, row["evidence"]


def test_chat_transport_failure_is_harness_error():
    def boom(msgs):
        raise RuntimeError("endpoint down")
    row = harness_skills.run_setup_case("opencode", BASE, ALIAS, chat=boom)
    assert row["status"] == "harness_error" and row["score"] == 0.0
    _assert_row_shape(row, "opencode")


def test_prompt_carries_skill_and_facts():
    msgs = harness_skills.build_setup_prompt("openclaw", BASE, ALIAS)
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    text = msgs[0]["content"]
    assert BASE in text and ALIAS in text and "openclaw.json" in text
    assert "<BASE_URL>" not in text and "<ALIAS>" not in text   # placeholders substituted
    assert "fenced code block" in text


# ==============================================================================================
# skill-doc lint: every documented field is one the adapter actually stages TODAY
# ==============================================================================================

def test_skill_docs_lint():
    for h, fields in harness_skills.DOCUMENTED_FIELDS.items():
        doc = harness_skills.load_skill(h)
        with open(os.path.join(_MVP, "pod", "adapters", f"{h}.py"), encoding="utf-8") as f:
            src = f.read()
        for field in fields:
            assert field in doc, f"{h}.md does not document {field!r}"
            assert field in src, f"{h} adapter source does not stage documented field {field!r}"
        spec = harness_skills.SKILLS[h]
        assert spec["filename"] in doc, f"{h}.md must name its deliverable {spec['filename']}"
        assert f"```{spec['filename']}" in doc or "fenced" in doc.lower()
        assert "<BASE_URL>" in doc and "<ALIAS>" in doc      # placeholders for substitution
        assert "PROTECTED" in doc                            # the pinning rule is disclosed


def test_gold_configs_satisfy_own_validators():
    """The adapters' build_config output must pass the validators verbatim — validator and
    adapter can never drift apart."""
    cfg, _, ok, detail = harness_skills._validate_opencode(
        json.dumps(oc_mod.build_config(BASE, ALIAS)), BASE, ALIAS)
    assert cfg and ok, detail
    cfg, _, ok, detail = harness_skills._validate_openclaw(
        json.dumps(claw_mod.build_config(BASE, ALIAS)), BASE, ALIAS)
    assert cfg and ok, detail
    flags, _, ok, detail = harness_skills._validate_hermes(
        f"--base_url={BASE}\n--api_key=sk-local\n--model={ALIAS}\n--max_turns=8",
        BASE, ALIAS)
    assert flags and ok, detail


# ==============================================================================================
# run_agentic_v2 integration
# ==============================================================================================

def test_run_agentic_v2_prepends_setup_row():
    class FakeHermes(mock_adapter.MockAdapter):
        name = "hermes"

    stub = {"case_id": "agentic.setup.hermes", "category": "Agentic", "tier": 0,
            "status": "scored", "score": 1.0, "raw_output": "{}",
            "evidence": [{"criterion": "x", "ok": True, "detail": ""}],
            "speed": {"e2e_s": 0.1}}
    old_cls = adapters.ADAPTERS["hermes"]
    adapters.ADAPTERS["hermes"] = FakeHermes
    run_harness2._discover_cache["hermes"] = {"harness": "hermes", "harness_version": "test-1"}
    try:
        with mock.patch.object(harness_skills, "run_setup_case",
                               return_value=dict(stub)) as rsc:
            rows = run_harness2.run_agentic_v2("hermes", BASE, ALIAS, concurrency=2)
        rsc.assert_called_once_with("hermes", BASE, ALIAS)
    finally:
        adapters.ADAPTERS["hermes"] = old_cls
        run_harness2._discover_cache.pop("hermes", None)
    assert rows[0]["case_id"] == "agentic.setup.hermes"
    assert rows[0]["suite_id"] == agentic_v2.SUITE_ID           # stamped by run_harness2
    assert rows[0]["harness"] == "hermes" and rows[0]["harness_version"] == "test-1"
    assert [r["case_id"] for r in rows[1:]] == agentic_v2.CASE_IDS  # task order preserved
    assert all(r["score"] == 1.0 for r in rows)                 # mock agent is perfect


def test_mock_harness_and_kill_switch_skip_setup():
    with mock.patch.object(harness_skills, "run_setup_case") as rsc:
        rows = run_harness2.run_agentic_v2("mock", BASE, ALIAS, concurrency=2)
    rsc.assert_not_called()                                     # mock has no config surface
    assert [r["case_id"] for r in rows] == agentic_v2.CASE_IDS

    class FakeHermes(mock_adapter.MockAdapter):
        name = "hermes"
    old_cls = adapters.ADAPTERS["hermes"]
    adapters.ADAPTERS["hermes"] = FakeHermes
    run_harness2._discover_cache["hermes"] = {"harness": "hermes", "harness_version": "test-1"}
    try:
        with mock.patch.dict(os.environ, {"AEON_SELFCONFIG": "0"}), \
             mock.patch.object(harness_skills, "run_setup_case") as rsc:
            rows = run_harness2.run_agentic_v2("hermes", BASE, ALIAS, concurrency=2)
        rsc.assert_not_called()                                 # operator kill switch
        assert [r["case_id"] for r in rows] == agentic_v2.CASE_IDS
    finally:
        adapters.ADAPTERS["hermes"] = old_cls
        run_harness2._discover_cache.pop("hermes", None)


def test_suite_id_bumped():
    """Setup cases change the agentic suite composition — old and new runs must never mix
    in one matrix cell."""
    assert agentic_v2.SUITE_ID == "aeon-agentic-v2.4"


def main():
    print("== the four scoring outcomes (per harness) ==")
    check("perfect config + healthy boot -> 1.0", test_perfect_config_scores_1)
    check("wrong endpoint -> 0.0, boot never launched", test_wrong_endpoint_scores_0_and_never_boots)
    check("wrong served alias -> 0.0", test_wrong_alias_scores_0)
    check("garbage / refusal / empty -> 0.0", test_garbage_scores_0)
    check("protected-correct but boot fails -> 0.5", test_boot_failure_scores_half)

    print("== safety ==")
    check("64KB config cap", test_config_size_cap)
    check("foreign-URL smuggling (extra provider) -> 0.0", test_foreign_url_smuggling_fails)
    check("hermes pod-owned/unknown flags rejected", test_hermes_pod_owned_flags_rejected)
    check("reasoning-trace decoy fence stripped", test_reasoning_decoy_fence_is_stripped)
    check("chat transport failure -> harness_error row", test_chat_transport_failure_is_harness_error)
    check("setup prompt carries skill + endpoint facts", test_prompt_carries_skill_and_facts)

    print("== skill-doc contract ==")
    check("docs lint: documented fields == adapter-staged fields", test_skill_docs_lint)
    check("adapter gold configs pass the validators", test_gold_configs_satisfy_own_validators)

    print("== run_agentic_v2 integration ==")
    check("setup row prepended + stamped for a real harness", test_run_agentic_v2_prepends_setup_row)
    check("mock harness / AEON_SELFCONFIG=0 skip setup", test_mock_harness_and_kill_switch_skip_setup)
    check("agentic suite id bumped to v2.4", test_suite_id_bumped)

    if FAILED:
        print(f"\nRESULT: FAIL ({len(FAILED)} failing: {FAILED})")
        raise SystemExit(1)
    print("\nRESULT: PASS (all harness self-config tests green)")


if __name__ == "__main__":
    main()
