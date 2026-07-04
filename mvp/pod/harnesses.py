"""Agentic harness registry — the vanilla agent harnesses the pod runs the agentic suite
through, and how each one's EXACT release version is captured for disclosure in the report.

Every agentic pass records (harness, harness_version) so a result always discloses which
harness BUILD produced it — model×harness comparison must be apples-to-apples, and a harness
update can change scores, so the version is part of the measurement (like engine + hardware).

The harnesses run as vanilla deploys on the GPU host and are pointed at the pod's served model
alias (`model-under-test`). This module is the portable registry + version-capture; the actual
container/CLI orchestration runs where the harnesses are installed.
"""
from __future__ import annotations

import re
import shutil
import subprocess

# semver (1.17.11 / 0.17.0) OR date-version (2026.6.25)
_VER_RE = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}|\d+\.\d+\.\d+(?:-[\w.]+)?")

HARNESSES = {
    "hermes": {
        "name": "Hermes Agent", "repo": "https://github.com/NousResearch/hermes-agent",
        "deploy": "docker", "package": None, "version_cmd": ["hermes", "--version"],
        "endpoint_env": "OPENAI_BASE_URL", "supports_openai": True,
    },
    "openclaw": {
        "name": "OpenClaw", "repo": "https://github.com/openclaw/openclaw",
        "deploy": "npm", "package": "openclaw", "version_cmd": ["openclaw", "--version"],
        "config_file": "~/.openclaw/openclaw.json", "supports_openai": True,
    },
    "opencode": {
        "name": "OpenCode", "repo": "https://github.com/anomalyco/opencode",
        "deploy": "npm", "package": "opencode-ai", "version_cmd": ["opencode", "--version"],
        "supports_openai": True,
    },
}

ALL = list(HARNESSES)


def resolve_version(harness: str, pin: str | None = None) -> str | None:
    """The exact harness version in use: an explicit pin (release tag/digest) if given, else
    query the installed CLI. Returns None when neither is available — the report then flags
    the version as unknown rather than guessing."""
    if pin:
        return pin
    h = HARNESSES.get(harness)
    if not h or not h.get("version_cmd"):
        return None
    cmd = h["version_cmd"]
    if not shutil.which(cmd[0]):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        text = (out.stdout + " " + out.stderr).strip()
    except Exception:
        return None
    m = _VER_RE.search(text)
    return m.group(0) if m else (text.splitlines()[0][:40] if text else None)


def disclose(harness: str, pin: str | None = None) -> dict:
    """The {harness, name, version, repo} record disclosed WITH the benchmark report."""
    h = HARNESSES.get(harness, {})
    return {"harness": harness, "harness_name": h.get("name"), "harness_repo": h.get("repo"),
            "harness_version": resolve_version(harness, pin)}
