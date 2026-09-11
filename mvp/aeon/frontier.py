"""Approved frontier-model definitions for AEON Bench.

Frontier API results are useful as reference lines against local models, but
they are not local-weight attestations. A frontier run is therefore validated
against an operator-approved provider/model definition, displayed with provider
metadata, and kept out of the attested local-weight global ranking.

Operators can replace or extend the built-ins with AEON_FRONTIER_MODELS_JSON:

{
  "openai:gpt-5-high": {
    "provider": "openai",
    "brand": "ChatGPT",
    "model": "gpt-5",
    "version": "GPT-5",
    "effort": "high",
    "api_format": "openai",
    "base_url": "https://api.openai.com/v1",
    "logo_url": "https://images.ctfassets.net/kftzwdyauwt9/3hUGLn3ypllZ0oa01qOYVq/28e8188e6f11b84c3e876569d492734f/Blossom_Light.svg",
    "logo_source_url": "https://openai.com/brand/",
    "request": {"reasoning_effort": "high"}
  }
}
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

_ID_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")

_OPENAI_LOGO_URL = (
    "https://images.ctfassets.net/kftzwdyauwt9/3hUGLn3ypllZ0oa01qOYVq/"
    "28e8188e6f11b84c3e876569d492734f/Blossom_Light.svg"
)
_OPENAI_BRAND_URL = "https://openai.com/brand/"
_ANTHROPIC_LOGO_URL = (
    "https://cdn.prod.website-files.com/67ce28cfec624e2b733f8a52/"
    "681d52619fec35886a7f1a70_favicon.png"
)
_ANTHROPIC_BRAND_URL = "https://www.anthropic.com/"
_XAI_LOGO_URL = "https://x.ai/favicon.ico"
_XAI_BRAND_URL = "https://x.ai/legal/brand-guidelines"

_BUILTINS: dict[str, dict[str, Any]] = {
    "openai:gpt-5.5-high": {
        "provider": "openai",
        "provider_name": "OpenAI",
        "brand": "ChatGPT",
        "model": "gpt-5.5",
        "version": "GPT-5.5",
        "effort": "high",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "logo_url": _OPENAI_LOGO_URL,
        "logo_source_url": _OPENAI_BRAND_URL,
        "website": "https://openai.com/",
        "request": {"reasoning": {"effort": "high"}, "_token_field": "max_completion_tokens",
                    "_omit_temperature": True},
        "max_tokens": 8192,
    },
    "openai:gpt-5-high": {
        "provider": "openai",
        "provider_name": "OpenAI",
        "brand": "ChatGPT",
        "model": "gpt-5",
        "version": "GPT-5",
        "effort": "high",
        "api_format": "openai",
        "base_url": "https://api.openai.com/v1",
        "logo_url": _OPENAI_LOGO_URL,
        "logo_source_url": _OPENAI_BRAND_URL,
        "website": "https://openai.com/",
        "request": {"reasoning": {"effort": "high"}, "_token_field": "max_completion_tokens",
                    "_omit_temperature": True},
        "max_tokens": 8192,
    },
    "xai:grok-4.5-high": {
        "provider": "xai",
        "provider_name": "xAI",
        "brand": "Grok",
        "model": "grok-4.5",
        "version": "Grok 4.5",
        "effort": "high",
        "api_format": "openai",
        "base_url": "https://api.x.ai/v1",
        "logo_url": _XAI_LOGO_URL,
        "logo_source_url": _XAI_BRAND_URL,
        "website": "https://x.ai/api",
        "request": {"reasoning_effort": "high"},
        "max_tokens": 8192,
    },
    "anthropic:claude-opus-4.8-high": {
        "provider": "anthropic",
        "provider_name": "Anthropic",
        "brand": "Claude",
        "model": "claude-opus-4-8",
        "version": "Opus 4.8",
        "effort": "high",
        "api_format": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "logo_url": _ANTHROPIC_LOGO_URL,
        "logo_source_url": _ANTHROPIC_BRAND_URL,
        "website": "https://www.anthropic.com/claude",
        "request": {"output_config": {"effort": "high"}},
        "max_tokens": 8192,
    },
    "anthropic:claude-fable-5-high": {
        "provider": "anthropic",
        "provider_name": "Anthropic",
        "brand": "Claude",
        "model": "claude-fable-5",
        "version": "Fable 5",
        "effort": "high",
        "api_format": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "logo_url": _ANTHROPIC_LOGO_URL,
        "logo_source_url": _ANTHROPIC_BRAND_URL,
        "website": "https://www.anthropic.com/claude/fable",
        # Fable uses adaptive thinking; do not send manual thinking budgets or disabled thinking.
        "request": {"output_config": {"effort": "high"}},
        "max_tokens": 16384,
        "notes": "Adaptive thinking is always on for Claude Fable 5; effort controls depth.",
    },
}


class FrontierError(ValueError):
    pass


def _clean_id(s: str) -> str:
    s = (s or "").strip()
    if not _ID_OK.match(s):
        raise FrontierError("frontier id must be 1-96 chars of [A-Za-z0-9_.:-]")
    return s


def _load_env_defs() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("AEON_FRONTIER_MODELS_JSON")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise FrontierError(f"AEON_FRONTIER_MODELS_JSON is not valid JSON: {e}") from e
    out: dict[str, dict[str, Any]] = {}
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item.get("id"):
                out[_clean_id(str(item["id"]))] = {k: v for k, v in item.items() if k != "id"}
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                out[_clean_id(str(k))] = v
    else:
        raise FrontierError("AEON_FRONTIER_MODELS_JSON must be an object or list")
    return out


def _normalise(fid: str, d: dict[str, Any]) -> dict[str, Any]:
    api_format = (d.get("api_format") or "openai").strip().lower()
    if api_format not in {"openai", "anthropic"}:
        raise FrontierError(f"{fid}: api_format must be openai or anthropic")
    model = (d.get("model") or "").strip()
    base_url = (d.get("base_url") or "").strip().rstrip("/")
    if not model or not base_url:
        raise FrontierError(f"{fid}: model and base_url are required")
    provider = (d.get("provider") or fid.split(":", 1)[0]).strip().lower()
    brand = (d.get("brand") or provider).strip()
    version = (d.get("version") or model).strip()
    effort = (d.get("effort") or d.get("reasoning_effort") or "default").strip()
    display = (d.get("display_name") or f"{brand} {version} ({effort})").strip()
    return {
        "id": fid,
        "provider": provider,
        "provider_name": d.get("provider_name") or brand,
        "brand": brand,
        "model": model,
        "version": version,
        "effort": effort,
        "display_name": display,
        "api_format": api_format,
        "base_url": base_url,
        "logo_url": d.get("logo_url") or "/static/generic-avatar.svg",
        "logo_source_url": d.get("logo_source_url") or d.get("website"),
        "website": d.get("website"),
        "request": d.get("request") if isinstance(d.get("request"), dict) else {},
        "max_tokens": int(d.get("max_tokens") or 8192),
        "notes": d.get("notes"),
        "canonical": d.get("canonical") or f"frontier/{provider}/{model}/{effort}".lower(),
    }


def definitions() -> dict[str, dict[str, Any]]:
    defs = copy.deepcopy(_BUILTINS)
    defs.update(_load_env_defs())
    out: dict[str, dict[str, Any]] = {}
    for fid, d in defs.items():
        try:
            out[_clean_id(fid)] = _normalise(fid, d)
        except FrontierError:
            continue
    return out


def get_definition(fid: str) -> dict[str, Any]:
    fid = _clean_id(fid)
    d = definitions().get(fid)
    if not d:
        raise FrontierError(f"unknown frontier model definition: {fid}")
    return d


def public_metadata(d: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "provider", "provider_name", "brand", "model", "version", "effort",
            "display_name", "api_format", "base_url", "logo_url", "logo_source_url", "website",
            "max_tokens", "canonical", "notes")
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def public_definitions() -> list[dict[str, Any]]:
    return [public_metadata(d) for d in definitions().values()]


def bundle_metadata(d: dict[str, Any]) -> dict[str, Any]:
    meta = public_metadata(d)
    meta["validated"] = True
    meta["verified"] = "frontier_api"
    return meta


def validate_bundle_metadata(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    try:
        expected = bundle_metadata(get_definition(str(meta.get("id") or "")))
    except FrontierError:
        return None
    for k in ("provider", "model", "effort", "api_format", "base_url"):
        if str(meta.get(k) or "") != str(expected.get(k) or ""):
            return None
    return expected


def build_target(fid: str, api_key: str | None = None):
    from .targets import AnthropicTarget, OpenAITarget
    d = get_definition(fid)
    if d["api_format"] == "anthropic":
        return AnthropicTarget(d["base_url"], d["model"], api_key=api_key,
                               extra_body=d.get("request") or {})
    return OpenAITarget(d["base_url"], d["model"], api_key=api_key,
                        extra_body=d.get("request") or {})


def validate_api(fid: str, api_key: str | None) -> dict[str, Any]:
    """Make a tiny live request to prove the configured key/model/API shape works."""
    if not api_key:
        return {"ok": False, "error": "missing API key"}
    d = get_definition(fid)
    try:
        target = build_target(fid, api_key)
        r = target.chat([{"role": "user", "content": "Reply with exactly: AEON_OK"}],
                        temperature=0.0, max_tokens=min(64, d.get("max_tokens") or 64))
        text = (r.get("text") or "").strip()
        ok = "AEON_OK" in text.upper().replace(" ", "_")
        return {"ok": ok, "frontier": bundle_metadata(d),
                "sample": text[:120], "speed": {k: r.get(k) for k in
                ("ttft_ms", "decode_tps", "e2e_ms", "output_tokens", "streamed")}}
    except Exception as e:
        return {"ok": False, "frontier": public_metadata(d),
                "error": f"{type(e).__name__}: {e}"[:400]}
