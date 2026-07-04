"""Resolve a raw model name (as it appears on the leaderboard) to creator/org
metadata for the UI: the Hugging Face model card, the creator/org, and a circular
org avatar.

Model names on the board are plain strings from ``scoring.leaderboard()`` and come
from heterogeneous sources — proper HF repo ids (``google/gemma-3-27b-it``,
``Qwen/Qwen2.5-72B-Instruct``) as well as LM-Studio / Ollama style names with quant
or tag suffixes (``gemma-3-27b-it@q4_k_m``, ``llama3.1:latest``, ``...-GGUF``).

We:
  * normalise the name (strip quant/tag/format suffixes),
  * derive ``org/model`` (inferring the org from a curated prefix map when the name
    has no ``org/`` segment),
  * resolve the org's HF avatar URL **server-side** (cached, with a hard timeout) so
    the board never hammers HF and never blocks,
  * treat our own ``aeon`` org specially (local avatar + local profile),
  * and always degrade gracefully — unknown names get a generic avatar, never a 500.

The avatar URL shape (discovered against the live HF API):
  * ``GET https://huggingface.co/api/models/{repo}`` -> ``author`` (the org slug),
  * ``GET https://huggingface.co/api/organizations/{org}/overview`` -> ``avatarUrl``
    (a ``https://cdn-avatars.huggingface.co/...`` image).
The curated map ships a known-good avatar URL per org so the board renders instantly
even before (or without) any network call; the live lookup only fills gaps / refreshes.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict

HF = "https://huggingface.co"
GENERIC_AVATAR = "/static/generic-avatar.svg"
AEON_AVATAR = "/static/aeon-avatar.svg"
AEON_PROFILE = "https://aeon-bench.com"

# ---- own-model orgs (AEON_OWN_ORGS, comma list; "aeon" always included) ----
_OWN = {"aeon", "ornith"} | {
    o.strip().lower() for o in (os.environ.get("AEON_OWN_ORGS") or "aeon,ornith").split(",") if o.strip()
}

# ---- curated org map: name fragment -> canonical HF org metadata ----------------
# `frag` is matched (case-insensitively) against the bare model name when it has no
# explicit `org/` prefix. `avatar` is a known-good CDN URL so we render without a
# round-trip; `profile` is the org's HF page. The live HF lookup can refine `avatar`.
def _hf(org):
    return f"{HF}/{org}"


# avatar URLs verified against https://huggingface.co/api/organizations/{org}/overview
_ORGS = {
    "google": {
        "org": "google", "creator": "Google",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/5dd96eb166059660ed1ee413/WtA3YYitedOr9n02eHfJe.png",
        "frags": ["gemma", "google"]},
    "qwen": {
        "org": "Qwen", "creator": "Qwen",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/6215ca5692c0ecfba9186921/hrRM50-6XcdWgg2AKpENG.jpeg",
        "frags": ["qwen", "qwq"]},
    "meta-llama": {
        "org": "meta-llama", "creator": "Meta Llama",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/646cf8084eefb026fb8fd8bc/oCTqufkdTkjyGodsx1vo1.png",
        "frags": ["llama", "meta-llama", "codellama"]},
    "mistralai": {
        "org": "mistralai", "creator": "Mistral AI",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/62dac1c7a8ead43d20e3e17a/wrLf5yaGC6ng4XME70w6Z.png",
        "frags": ["mistral", "mixtral", "magistral", "ministral", "codestral", "devstral"]},
    "microsoft": {
        "org": "microsoft", "creator": "Microsoft",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/1583646260758-5e64858c87403103f9f1055d.png",
        "frags": ["phi"]},
    "deepseek-ai": {
        "org": "deepseek-ai", "creator": "DeepSeek",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/6538815d1bdb3c40db94fbfa/xMBly9PUMphrFVMxLX4kq.png",
        "frags": ["deepseek"]},
    "nvidia": {
        "org": "nvidia", "creator": "NVIDIA",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/1613114437487-60262a8e0703121c822a80b6.png",
        "frags": ["nemotron", "nvidia"]},
    "allenai": {
        "org": "allenai", "creator": "Allen Institute for AI",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/61b8e2ba285851687028d395/3Ru1d9-jZGAdgRiSQGfm-.png",
        "frags": ["olmo", "tulu", "molmo", "allenai"]},
    "huggingfacetb": {
        "org": "HuggingFaceTB", "creator": "Hugging Face Smol Models",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/651e96991b97c9f33d26bde6/e4VK7uW5sTthFJyZBL0lw.png",
        "frags": ["smollm", "smol", "huggingfacetb"]},
    "cohere": {
        "org": "CohereLabs", "creator": "Cohere Labs",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/5e70f6048ce3c604d78fe133/Abb-Ph2gp5x5gXP1n0qoy.png",
        "frags": ["command-r", "command", "aya", "cohere", "c4ai"]},
    "01-ai": {
        "org": "01-ai", "creator": "01.AI",
        "avatar": "https://cdn-avatars.huggingface.co/v1/production/uploads/63d10d4e8eaa4831005e92b5/o0SyEQbR2gP9hM8e7-Apw.png",
        "frags": ["yi-", "yi1", "01-ai"]},
    "aeon": {
        "org": "aeon", "creator": "Aeon",
        "avatar": AEON_AVATAR, "profile": AEON_PROFILE, "own": True,
        "frags": ["aeon"]},
}

# direct lookup by explicit `org/` prefix (case-insensitive on the slug)
_ORG_BY_SLUG = {v["org"].lower(): v for v in _ORGS.values()}
# extra slug aliases that don't match their key
_ORG_BY_SLUG["coherelabs"] = _ORGS["cohere"]
_ORG_BY_SLUG["cohereforai"] = _ORGS["cohere"]
_ORG_BY_SLUG["c4ai"] = _ORGS["cohere"]

# ---- in-process cache (name -> (meta, expiry_ts)) ------------------------------
# FIX(LOW): `model` is attacker-supplied and reachable unauth (GET /api/model/meta?model=),
# so an unbounded dict was a memory-DoS + HF-amplification vector. Bounded LRU (OrderedDict:
# move_to_end on read, popitem(last=False) on overflow) under _LOCK, preserving TTL semantics.
_CACHE: "OrderedDict[str, tuple[dict, float]]" = OrderedDict()
_CACHE_CAP = 4096                                             # hard entry cap (LRU eviction)
_LOCK = threading.Lock()
_TTL = float(os.environ.get("AEON_MODELMETA_TTL", "86400"))   # 24h
_NEG_TTL = 600.0                                               # cache misses for 10m
_HTTP_TIMEOUT = 1.5                                            # never block the response long
_MAX_NAME_LEN = 128                                            # reject implausibly long names pre-fetch
_MAX_HTTP_BYTES = 1 << 20                                      # cap outbound HF read (1 MiB) — DoS guard
# a sane leaderboard model name: repo-id-ish chars only (org/model, quant/tag suffixes)
_NAME_OK_RE = re.compile(r"^[A-Za-z0-9 ._:@/+-]+$")

# suffixes/markers to strip when normalising a raw name
_QUANT_RE = re.compile(
    r"(@.*$|:latest$|:[a-z0-9._-]*q\d.*$|[-_.](?:gguf|ggml|awq|gptq|mlx|bnb|"
    r"q\d[\w.]*|iq\d[\w.]*|fp16|fp8|bf16|int4|int8|\d+bit)\b.*$)",
    re.IGNORECASE,
)


def _strip_suffixes(name: str) -> str:
    """Drop quant/format/tag suffixes: ``gemma-3-27b-it@q4_k_m`` -> ``gemma-3-27b-it``."""
    n = (name or "").strip()
    # an explicit org/model survives intact except for a trailing @quant / :tag
    n = re.sub(r"@.*$", "", n)
    n = re.sub(r":latest$", "", n, flags=re.IGNORECASE)
    n = _QUANT_RE.sub("", n)
    return n.strip().strip("/-_.")


def _match_curated(bare: str):
    """Find a curated *vendor* org by a fragment in the bare model name. Own orgs are
    handled separately by whole-token matching (see _name_has_own_token), so they are
    skipped here to avoid substring false-positives (e.g. 'aeonic' != own)."""
    low = bare.lower()
    for meta in _ORGS.values():
        if meta.get("own"):
            continue
        for frag in meta["frags"]:
            if frag in low:
                return meta
    return None


def _is_own_org(org: str | None) -> bool:
    return bool(org) and org.lower() in _OWN


def _name_has_own_token(bare: str) -> bool:
    """True if a bare (org-less) name carries an own-org token as a delimited word,
    e.g. ``...-aeon-abliterated`` -> own. Matches whole tokens so 'aeonic' won't."""
    toks = re.split(r"[\s/@:._-]+", (bare or "").lower())
    return any(t in _OWN for t in toks if t)


def _hf_avatar(name: str) -> str | None:
    """Live HF lookup of a creator's avatar URL. A creator may be an ORG **or a USER**
    (e.g. AEON-7, sakamakismile) — HF serves their avatars from different overview
    endpoints, so we try both and return the first hit. Returns None on error/timeout."""
    for kind in ("users", "organizations"):
        try:
            url = f"{HF}/api/{kind}/{urllib.parse.quote(name)}/overview"
            req = urllib.request.Request(url, headers={"User-Agent": "aeon-bench/0.4"})
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
                data = json.loads(r.read(_MAX_HTTP_BYTES).decode("utf-8", "replace"))  # FIX(LOW): bound read
            if data.get("avatarUrl"):
                return data["avatarUrl"]
        except Exception:
            continue
    return None


def _hf_author(repo: str) -> str | None:
    """Live HF lookup of a repo's author (org slug). Returns None on any error/timeout."""
    try:
        url = f"{HF}/api/models/{repo}"
        req = urllib.request.Request(url, headers={"User-Agent": "aeon-bench/0.4"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            data = json.loads(r.read(_MAX_HTTP_BYTES).decode("utf-8", "replace"))  # FIX(LOW): bound read
        return data.get("author") or None
    except Exception:
        return None


def _hf_repo_exists(repo: str) -> bool:
    """True iff `repo` is a real public HF model. Used so a GUESSED repo id (e.g. a local
    derivative 'gemma4-26b' inferred to 'google/gemma4-26b') never produces a 404 card link."""
    try:
        url = f"{HF}/api/models/{urllib.parse.quote(repo)}"
        req = urllib.request.Request(url, headers={"User-Agent": "aeon-bench/0.4"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def _build(name: str) -> dict:
    bare = _strip_suffixes(name)
    repo = None
    org = None
    model_seg = bare
    curated = None

    if "/" in bare:
        org, _, model_seg = bare.partition("/")
        org = org.strip()
        repo = bare
        curated = _ORG_BY_SLUG.get(org.lower())
    else:
        # Own-org tokens win over a vendor fragment for bare names: a local fine-tune
        # named e.g. "gemma-4-12b-...-aeon-abliterated" is OURS, not Google's, even
        # though it carries the base "gemma" lineage in its name.
        if _name_has_own_token(bare):
            org = "aeon"
            curated = _ORGS["aeon"]
        else:
            curated = _match_curated(bare)
            if curated:
                org = curated["org"]
                repo = f"{org}/{bare}"

    # ---- own models: local avatar + local profile, no network ----
    if _is_own_org(org) or (curated and curated.get("own")):
        return {
            "name": name, "repo": repo, "org": org or "aeon",
            "creator": (curated or {}).get("creator", "Aeon"),
            "creator_url": AEON_PROFILE,
            "card_url": AEON_PROFILE,
            "avatar_url": AEON_AVATAR,
            "is_own": True,
        }

    # ---- curated org (known good avatar; refine via live lookup, best-effort) ----
    if curated:
        avatar = curated["avatar"]
        live = _hf_avatar(curated["org"])
        if live:
            avatar = live
        # Link a model CARD only for a REAL repo: an explicit org/model id (user-supplied,
        # trusted), or a fragment-guessed `{org}/{bare}` we VERIFY exists on HF. A local /
        # custom derivative (e.g. 'gemma4-26b' -> 'google/gemma4-26b') would 404 — show the
        # lineage org's avatar + creator, but no broken card link.
        explicit = "/" in bare
        card_repo = repo if (repo and (explicit or _hf_repo_exists(repo))) else None
        return {
            "name": name, "repo": card_repo, "org": curated["org"],
            "creator": curated["creator"],
            "creator_url": _hf(curated["org"]),
            "card_url": f"{HF}/{card_repo}" if card_repo else None,
            "avatar_url": avatar,
            "is_own": False,
        }

    # ---- explicit org we don't curate: ask HF for the avatar, best-effort ----
    if org and repo:
        avatar = _hf_avatar(org) or GENERIC_AVATAR
        return {
            "name": name, "repo": repo, "org": org,
            "creator": org,
            "creator_url": _hf(org),
            "card_url": f"{HF}/{repo}",
            "avatar_url": avatar,
            "is_own": False,
        }

    # ---- no org and not curated: try to discover an author from HF (rare path) ----
    author = None
    if "/" not in bare:
        # try the bare name as a repo id is unlikely; skip to keep us fast/cheap.
        author = None
    if author:
        return {
            "name": name, "repo": f"{author}/{bare}", "org": author,
            "creator": author, "creator_url": _hf(author),
            "card_url": f"{HF}/{author}/{bare}",
            "avatar_url": _hf_avatar(author) or GENERIC_AVATAR,
            "is_own": False,
        }

    # ---- total fallback: never 500 ----
    return {
        "name": name, "repo": None, "org": None,
        "creator": "unknown", "creator_url": None,
        "card_url": None,
        "avatar_url": GENERIC_AVATAR,
        "is_own": False,
    }


def resolve(model_name: str) -> dict:
    """Resolve a raw leaderboard model name to creator/org/avatar metadata.

    Cached in-process (TTL) so the board doesn't hammer HF. Never raises; on any
    error returns a generic-avatar fallback dict.
    """
    name = (model_name or "").strip()
    # FIX(LOW): short-circuit implausibly-shaped names BEFORE allocating a cache entry or
    # doing any HF fetch — bounds both the (attacker-controlled) cache key and the outbound
    # amplification. Empty / over-long / out-of-charset names get the generic fallback and
    # are NOT cached, so they can never grow _CACHE or trigger a network call.
    if not name or len(name) > _MAX_NAME_LEN or not _NAME_OK_RE.match(name):
        return {"name": name, "repo": None, "org": None, "creator": "unknown",
                "creator_url": None, "card_url": None, "avatar_url": GENERIC_AVATAR,
                "is_own": False}

    now = time.time()
    with _LOCK:
        hit = _CACHE.get(name)
        if hit and hit[1] > now:
            _CACHE.move_to_end(name)             # LRU: mark most-recently-used
            return hit[0]

    try:
        meta = _build(name)
    except Exception:
        meta = {"name": name, "repo": None, "org": None, "creator": "unknown",
                "creator_url": None, "card_url": None, "avatar_url": GENERIC_AVATAR,
                "is_own": False}

    # negative/positive TTL: a generic fallback is retried sooner than a solid hit
    ttl = _NEG_TTL if meta.get("avatar_url") == GENERIC_AVATAR else _TTL
    with _LOCK:
        _CACHE[name] = (meta, now + ttl)
        _CACHE.move_to_end(name)                 # LRU: newest write is most-recently-used
        while len(_CACHE) > _CACHE_CAP:          # evict oldest until back under the cap
            _CACHE.popitem(last=False)
    return meta
