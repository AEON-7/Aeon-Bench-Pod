"""Harness self-configuration — a SCORED setup case per agentic harness (agentic.setup.<h>).

THE POINT: a real agentic model must be able to SET UP its own harness, not just run inside
a pre-configured one. Before the aeon-agentic-v2 task loop, the pod hands the MODEL UNDER
TEST a per-harness "helper skill" document (harness_skills/<harness>.md — precise, honest
docs of the config surface each adapter stages today) plus the endpoint facts (base_url /
served alias / protocol), and asks it to author the harness config itself, via a direct chat
to the served endpoint (aeon.targets.OpenAITarget). The reply is graded deterministically
and recorded as a normal agentic-v2 row, so setup quality flows into the harness score:

    1.0  config parsed + protected fields correct + boot check passed
    0.5  config parsed + protected fields correct, but the boot check failed
    0.0  unparseable / wrong endpoint or model (protected-field fail) / refused

SAFETY — the model's output is UNTRUSTED:
  * it is never executed: JSON configs are parsed and RE-SERIALIZED (canonical json.dump)
    before they touch a container; the hermes flag file is parsed into a whitelisted
    {flag: value} dict and rebuilt as an argv list (no shell anywhere, config files only);
  * PROTECTED fields: the endpoint/base_url and model/alias MUST equal the pod-supplied
    values, and no other URL may appear anywhere in the config (opencode's `$schema`
    constant is the one allowlisted exception) — a config pointing the harness anywhere
    else scores 0.0 and is NEVER booted;
  * the config is written only into a throwaway boot tempdir, never the pod tree; the
    config size is capped at 64 KB (MAX_CONFIG_BYTES) and the reply at 256 K chars;
  * whatever setup scores, the REAL task loop always runs with the adapter's OWN
    known-good config (task scores stay comparable across models; setup is its own
    signal) — see pod.run_harness2.run_agentic_v2.

BOOT CHECK — the cheapest reach-the-served-model handshake per harness, one one-shot
container (pod.adapters.base.run_container_io, so it works pod-in-container too):
  * hermes   — argv rebuilt from the validated hermes.flags + pod-owned
               `--query="<boot prompt>" --save_sample`, max_turns capped at 3; PASS = a
               sample_*.json transcript appears in /work (hermes only writes it after
               completing a conversation with the endpoint);
  * openclaw — canonical openclaw.json seeded as the /root/.openclaw home,
               `agent --local --json --agent main -m "<boot prompt>" --model dgx/<alias>`;
               PASS = a non-empty answer parses from the stdout JSON;
  * opencode — canonical opencode.json seeded into /work,
               `run --format json --auto -m dgx/<alias> "<boot prompt>"`; PASS = exit 0
               and a non-empty answer or tool step parses from the NDJSON stdout.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import tempfile
import time

from ..adapters import hermes as _hermes
from ..adapters import openclaw as _openclaw
from ..adapters import opencode as _opencode
from ..adapters.base import run_container_io, strip_reasoning  # noqa: F401  (patched in tests)

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_CONFIG_BYTES = 64 * 1024          # hard cap on the model-authored config
_MAX_REPLY_CHARS = 256_000            # hard cap on the raw chat reply we even look at
BOOT_PROMPT = "Reply with the single word READY. Do not use any tools."
_OPENCODE_SCHEMA_URL = "https://opencode.ai/config.json"


def _boot_timeout() -> int:
    try:
        return max(30, int(os.environ.get("AEON_SELFCONFIG_BOOT_TIMEOUT", "")))
    except (TypeError, ValueError):
        return 180


def _chat_max_tokens() -> int:
    try:
        return max(256, int(os.environ.get("AEON_SELFCONFIG_MAX_TOKENS", "")))
    except (TypeError, ValueError):
        return 4096                    # headroom: reasoning models burn tokens before the fence


# The harnesses that HAVE a self-config skill (mock has no config surface — never listed).
SKILLS = {
    "hermes":   {"filename": "hermes.flags",  "format": "cli flags (one --flag=value per line)",
                 "doc": "hermes.md"},
    "openclaw": {"filename": "openclaw.json", "format": "json", "doc": "openclaw.md"},
    "opencode": {"filename": "opencode.json", "format": "json", "doc": "opencode.md"},
}

# The shared skill-doc <-> adapter contract: every field a skill doc teaches MUST be one the
# adapter actually stages today (test_harness_selfconfig lints both directions: each name
# below appears in the .md AND in the adapter source). Do not document invented fields.
DOCUMENTED_FIELDS = {
    "hermes":   ("--base_url", "--api_key", "--model", "--max_turns"),
    "openclaw": ("models", "providers", "baseUrl", "apiKey", "api", "id", "name",
                 "contextWindow", "maxTokens", "agents", "primary"),
    "opencode": ("$schema", "provider", "npm", "name", "options", "baseURL", "apiKey",
                 "models", "tool_call", "model"),
}


def case_id(harness_id: str) -> str:
    return f"agentic.setup.{harness_id}"


def load_skill(harness_id: str) -> str:
    """The raw helper-skill markdown for `harness_id` (placeholders unsubstituted)."""
    path = os.path.join(_SKILL_DIR, SKILLS[harness_id]["doc"])
    with open(path, encoding="utf-8") as f:
        return f.read()


def render_skill(harness_id: str, base_url: str, alias: str) -> str:
    """The helper skill with the pod's real endpoint facts substituted in."""
    return (load_skill(harness_id)
            .replace("<BASE_URL>", base_url)
            .replace("<ALIAS>", alias))


def build_setup_prompt(harness_id: str, base_url: str, alias: str) -> list[dict]:
    """The single-turn chat that asks the model under test to configure its own harness."""
    spec = SKILLS[harness_id]
    content = (
        "You are the model under test on the AEON agentic benchmark. Before the task loop "
        f"starts, you must configure the '{harness_id}' agent harness to use YOU as its "
        "model. The harness skill document below describes this harness's exact "
        "configuration surface.\n\n"
        f"{render_skill(harness_id, base_url, alias)}\n\n"
        "Endpoint facts (authoritative — use these EXACT values):\n"
        f"- base_url: {base_url}\n"
        f"- served model alias: {alias}\n"
        "- protocol: OpenAI-compatible chat completions\n"
        "- api key: the local server accepts any bearer token; use sk-local\n\n"
        f"Now reply with ONLY the {spec['filename']} content inside ONE fenced code block "
        f"tagged with its filename (```{spec['filename']}). No other fenced blocks, no "
        "commentary outside the block."
    )
    return [{"role": "user", "content": content}]


def _default_chat(base_url: str, alias: str):
    """Direct chat to the served model via the existing target plumbing (streamed,
    deterministic temperature). Imported lazily so this package needs aeon only when a real
    setup chat happens."""
    from aeon import targets

    tgt = targets.OpenAITarget(base_url, alias, api_key="sk-local")

    def _chat(messages):
        res = tgt.chat(messages, temperature=0.0, max_tokens=_chat_max_tokens()) or {}
        return res.get("text") or ""

    return _chat


# --------------------------------------------------------------------------------------------
# Extraction + validation (pure functions — unit-tested directly)
# --------------------------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)


def _extract_config(reply: str, filename: str):
    """The config text out of the model's reply. Preference order: a fenced block whose
    info string mentions the expected filename; else the ONLY fenced block; else (no fences
    at all) the whole reply, letting the format parser decide. Returns (text|None, how)."""
    blocks = _FENCE_RE.findall(reply or "")
    tagged = [body for tag, body in blocks if filename.lower() in tag.strip().lower()]
    if tagged:
        return tagged[-1].strip(), f"fenced block tagged {filename}"
    if len(blocks) == 1:
        return blocks[0][1].strip(), "single untagged fenced block"
    if not blocks:
        text = (reply or "").strip()
        if text:
            return text, "no fenced block — whole reply taken as the config"
        return None, "empty reply"
    return None, f"{len(blocks)} fenced blocks, none tagged {filename}"


def _norm_url(u: str) -> str:
    return str(u or "").strip().rstrip("/").lower()


def _foreign_urls(obj, base_url: str, *, allow_opencode_schema: bool = False) -> list:
    """Every URL-looking string in the parsed config that is NOT the pod endpoint. PROTECTED:
    the model must not point the harness (or any secondary provider) anywhere else."""
    bad = []

    def walk(x, key=None):
        if isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and "://" in x:
            if allow_opencode_schema and key == "$schema" and x == _OPENCODE_SCHEMA_URL:
                return
            if _norm_url(x) != _norm_url(base_url):
                bad.append(x)

    walk(obj)
    return bad


def _validate_opencode(text: str, base_url: str, alias: str):
    """(parsed|None, parse_detail, protected_ok, protected_detail) for opencode.json."""
    try:
        cfg = json.loads(text)
    except Exception as e:
        return None, f"invalid JSON: {e}"[:200], False, "skipped (config unparseable)"
    if not isinstance(cfg, dict):
        return None, "top level is not a JSON object", False, "skipped (config unparseable)"
    probs = []
    provider = cfg.get("provider") if isinstance(cfg.get("provider"), dict) else {}
    prov = provider.get("dgx") if isinstance(provider.get("dgx"), dict) else {}
    options = prov.get("options") if isinstance(prov.get("options"), dict) else {}
    if _norm_url(options.get("baseURL")) != _norm_url(base_url):
        probs.append(f"provider.dgx.options.baseURL is {options.get('baseURL')!r}, "
                     f"expected {base_url!r}")
    if cfg.get("model") != f"dgx/{alias}":
        probs.append(f"model is {cfg.get('model')!r}, expected {'dgx/' + alias!r}")
    models = prov.get("models")
    if not (isinstance(models, dict) and alias in models):
        probs.append(f"provider.dgx.models must contain the served alias {alias!r}")
    probs += [f"foreign URL {u!r}" for u in
              _foreign_urls(cfg, base_url, allow_opencode_schema=True)]
    return (cfg, "valid JSON object", not probs,
            "; ".join(probs)[:400] if probs else "endpoint + model pinned to pod values")


def _validate_openclaw(text: str, base_url: str, alias: str):
    """(parsed|None, parse_detail, protected_ok, protected_detail) for openclaw.json."""
    try:
        cfg = json.loads(text)
    except Exception as e:
        return None, f"invalid JSON: {e}"[:200], False, "skipped (config unparseable)"
    if not isinstance(cfg, dict):
        return None, "top level is not a JSON object", False, "skipped (config unparseable)"
    probs = []
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
    prov = providers.get("dgx") if isinstance(providers.get("dgx"), dict) else {}
    if _norm_url(prov.get("baseUrl")) != _norm_url(base_url):
        probs.append(f"models.providers.dgx.baseUrl is {prov.get('baseUrl')!r}, "
                     f"expected {base_url!r}")
    entries = prov.get("models") if isinstance(prov.get("models"), list) else []
    if not any(isinstance(m, dict) and m.get("id") == alias for m in entries):
        probs.append(f"models.providers.dgx.models must contain an entry with id {alias!r}")
    agents = cfg.get("agents") if isinstance(cfg.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    dmodel = defaults.get("model") if isinstance(defaults.get("model"), dict) else {}
    if dmodel.get("primary") != f"dgx/{alias}":
        probs.append(f"agents.defaults.model.primary is {dmodel.get('primary')!r}, "
                     f"expected {'dgx/' + alias!r}")
    probs += [f"foreign URL {u!r}" for u in _foreign_urls(cfg, base_url)]
    return (cfg, "valid JSON object", not probs,
            "; ".join(probs)[:400] if probs else "endpoint + model pinned to pod values")


_HERMES_ALLOWED = ("base_url", "api_key", "model", "max_turns")
_HERMES_FLAG_RE = re.compile(r"^--([A-Za-z_]+)=(.*)$")
_API_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _validate_hermes(text: str, base_url: str, alias: str):
    """(parsed flags|None, parse_detail, protected_ok, protected_detail) for hermes.flags."""
    flags: dict[str, str] = {}
    for i, line in enumerate(str(text).replace("\r\n", "\n").split("\n"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _HERMES_FLAG_RE.match(line)
        if not m:
            return (None, f"line {i} is not --flag=value: {line[:60]!r}",
                    False, "skipped (config unparseable)")
        k, v = m.group(1).lower(), m.group(2).strip()
        if k in flags:
            return None, f"duplicate flag --{k}", False, "skipped (config unparseable)"
        flags[k] = v
    if not flags:
        return None, "no flags found", False, "skipped (config unparseable)"
    probs = []
    for k in flags:
        if k not in _HERMES_ALLOWED:
            probs.append(f"flag --{k} is not permitted (pod-owned or unknown)")
    if _norm_url(flags.get("base_url")) != _norm_url(base_url):
        probs.append(f"--base_url is {flags.get('base_url')!r}, expected {base_url!r}")
    if flags.get("model") != alias:
        probs.append(f"--model is {flags.get('model')!r}, expected {alias!r}")
    if not _API_KEY_RE.fullmatch(flags.get("api_key", "sk-local")):
        probs.append("--api_key has a disallowed charset (want [A-Za-z0-9._-]{1,128})")
    mt = flags.get("max_turns", "8")
    if not (mt.isdigit() and 1 <= int(mt) <= 32):
        probs.append(f"--max_turns {mt!r} is not an integer in 1..32")
    for k, v in flags.items():
        if k != "base_url" and "://" in v:
            probs.append(f"foreign URL in --{k}")
    return (flags, f"parsed {len(flags)} flag(s)", not probs,
            "; ".join(probs)[:400] if probs else "endpoint + model pinned to pod values")


_VALIDATORS = {"hermes": _validate_hermes,
               "openclaw": _validate_openclaw,
               "opencode": _validate_opencode}


# --------------------------------------------------------------------------------------------
# Boot checks — apply the model-authored (validated + canonicalized) config in a one-shot
# container and verify the harness reaches the served model. All docker I/O goes through the
# module-level `run_container_io` binding so tests can patch it.
# --------------------------------------------------------------------------------------------

def _boot_hermes(flags: dict, alias: str, timeout: float):
    # argv REBUILT from the validated flags (never the model's raw text); max_turns capped
    # at 3 — the boot handshake needs no tools, so keep it cheap.
    mt = max(1, min(int(flags.get("max_turns", "8") or 8), 3))
    args = [f"--base_url={flags['base_url']}",
            f"--api_key={flags.get('api_key', 'sk-local')}",
            f"--model={flags['model']}",
            f"--max_turns={mt}",
            f"--query={BOOT_PROMPT}",
            "--save_sample"]
    wd = tempfile.mkdtemp(prefix="aeon_selfcfg_hermes_")
    try:
        # Same context-window disclosure config the adapter always stages (pod-owned).
        cfg_path = os.path.join(wd, "hermes-config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("model:\n  context_length: 65536\n")
        out, err, rc, _ = run_container_io(
            _hermes.IMAGE, args,
            seed=[(wd, "/work")],
            seed_optional=[(cfg_path, "/root/.hermes/config.yaml")],
            collect=[("/work/.", wd)],
            timeout=timeout, name_hint=f"setup_hermes_{alias}",
            env={"TERMINAL_CWD": "/work"}, workdir="/work")
        samples = glob.glob(os.path.join(wd, "sample_*.json"))
        if samples:
            return True, (f"handshake transcript written "
                          f"({os.path.basename(samples[0])}); rc={rc}")
        return False, f"no sample transcript (rc={rc}; stderr tail: {err[-160:]!r})"
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def _boot_openclaw(cfg: dict, alias: str, timeout: float):
    home = tempfile.mkdtemp(prefix="aeon_selfcfg_claw_")
    try:
        os.makedirs(os.path.join(home, "workspace"), exist_ok=True)
        with open(os.path.join(home, "openclaw.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)     # canonical re-serialization, never raw model text
        out, err, rc, _ = run_container_io(
            _openclaw.IMAGE,
            ["agent", "--local", "--json", "--agent", "main",
             "-m", BOOT_PROMPT, "--model", f"dgx/{alias}"],
            seed=[(home, "/root/.openclaw")],
            timeout=timeout, name_hint=f"setup_claw_{alias}")
        answer = _openclaw.parse_output(out)["answer"]
        if answer:
            return True, f"answer received ({answer[:60]!r})"
        return False, f"no parseable answer (rc={rc}; stderr tail: {err[-160:]!r})"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _boot_opencode(cfg: dict, alias: str, timeout: float):
    wd = tempfile.mkdtemp(prefix="aeon_selfcfg_oc_")
    try:
        with open(os.path.join(wd, "opencode.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)     # canonical re-serialization, never raw model text
        out, err, rc, _ = run_container_io(
            _opencode.IMAGE,
            ["run", "--format", "json", "--auto", "-m", f"dgx/{alias}", BOOT_PROMPT],
            seed=[(wd, "/work")],
            timeout=timeout, name_hint=f"setup_opencode_{alias}", workdir="/work")
        parsed = _opencode.parse_output(out)
        if rc == 0 and (parsed["answer"] or parsed["steps"]):
            got = parsed["answer"] or "(tool steps only)"
            return True, f"rc=0, answer received ({got[:60]!r})"
        return False, f"rc={rc}, no parseable answer (stderr tail: {err[-160:]!r})"
    finally:
        shutil.rmtree(wd, ignore_errors=True)


_BOOTS = {"hermes": _boot_hermes, "openclaw": _boot_openclaw, "opencode": _boot_opencode}


# --------------------------------------------------------------------------------------------
# The setup case
# --------------------------------------------------------------------------------------------

def run_setup_case(harness_id: str, model_base_url: str, served_alias: str, *,
                   chat=None, timeout: float | None = None) -> dict:
    """Run the agentic.setup.<harness> case and return a result row (without suite/harness
    disclosure keys — run_harness2 stamps those). Deterministic scoring:

        1.0  parsed + protected fields correct + boot passed
        0.5  parsed + protected fields correct + boot failed
        0.0  unparseable / protected-field fail / refused / oversized

    `chat` is an injectable callable(messages) -> reply text (tests); default is a direct
    OpenAITarget chat to the served endpoint. Never raises: a transport failure becomes a
    status="harness_error" row (score 0), like any other agentic row.
    """
    if harness_id not in SKILLS:
        raise KeyError(f"no self-config skill for harness {harness_id!r}; "
                       f"known: {sorted(SKILLS)}")
    spec = SKILLS[harness_id]
    timeout = timeout or _boot_timeout()
    t0 = time.monotonic()
    row = {"case_id": case_id(harness_id), "category": "Agentic", "tier": 0}

    try:
        chat_fn = chat or _default_chat(model_base_url, served_alias)
        reply = chat_fn(build_setup_prompt(harness_id, model_base_url, served_alias)) or ""
    except Exception as e:                       # transport/infra failure — not a model grade
        row.update(status="harness_error", score=0.0,
                   raw_output=json.dumps(
                       {"error": f"setup chat failed: {type(e).__name__}: {e}"[:800]}),
                   evidence=[{"criterion": "setup: model produced a config reply",
                              "ok": False,
                              "detail": f"{type(e).__name__}: {e}"[:300]}],
                   speed={"e2e_s": round(time.monotonic() - t0, 3)})
        return row

    reply = strip_reasoning(str(reply)) or ""    # <think> traces may carry decoy fences

    if len(reply) > _MAX_REPLY_CHARS:
        cfg_text, how = None, f"reply exceeds {_MAX_REPLY_CHARS} chars"
    else:
        cfg_text, how = _extract_config(reply, spec["filename"])
    if cfg_text is not None and len(cfg_text.encode("utf-8", "replace")) > MAX_CONFIG_BYTES:
        cfg_text, how = None, f"config exceeds the {MAX_CONFIG_BYTES // 1024} KB cap"

    parsed, parse_ok, prot_ok = None, False, False
    parse_detail, prot_detail = how, "skipped (no config extracted)"
    if cfg_text is not None:
        parsed, vdetail, prot_ok, prot_detail = _VALIDATORS[harness_id](
            cfg_text, model_base_url, served_alias)
        parse_ok = parsed is not None
        parse_detail = f"{how}; {vdetail}"

    boot_ok = False
    boot_detail = "skipped (an unvalidated config is never booted)"
    if parse_ok and prot_ok:
        try:
            boot_ok, boot_detail = _BOOTS[harness_id](parsed, served_alias, timeout)
        except Exception as e:
            boot_ok, boot_detail = False, f"boot error: {type(e).__name__}: {e}"[:300]

    score = 1.0 if (parse_ok and prot_ok and boot_ok) else \
            0.5 if (parse_ok and prot_ok) else 0.0
    evidence = [
        {"criterion": f"setup: config parsed ({spec['format']})",
         "ok": parse_ok, "detail": str(parse_detail)[:400]},
        {"criterion": "setup: protected endpoint/model fields equal the pod-supplied values",
         "ok": bool(parse_ok and prot_ok), "detail": str(prot_detail)[:400]},
        {"criterion": "setup: boot check — harness reaches the served model with the "
                      "model-authored config",
         "ok": boot_ok, "detail": str(boot_detail)[:400]},
    ]
    row.update(status="scored", score=score,
               raw_output=json.dumps({"reply": reply[:3000],
                                      "config": (cfg_text or "")[:2000],
                                      "boot": str(boot_detail)[:400]},
                                     default=str)[:8000],
               evidence=evidence,
               speed={"e2e_s": round(time.monotonic() - t0, 3)})
    return row


__all__ = ["SKILLS", "DOCUMENTED_FIELDS", "MAX_CONFIG_BYTES", "BOOT_PROMPT", "case_id",
           "load_skill", "render_skill", "build_setup_prompt", "run_setup_case"]
