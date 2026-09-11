"""pod/validate.py — async MODEL VALIDATION for the Run tab's green light.

When the user picks a model (HF link, optionally + a local dir already on disk), the GUI calls
POST /api/pod/validate and polls. States:

  resolving  — parsing the link, fetching HF's canonical manifest (commit sha + per-file LFS sha256)
  hashing    — a LOCAL dir was given: sha256-hashing its weight files against that manifest
  validated  — local weights are bit-for-bit the HF repo@sha -> good as gold, NO re-download at
               launch; the run is submission-validated (attested) using these exact bytes
  resolved   — no local dir: the repo + manifest resolved; the launcher will pull + hash-verify
               automatically, so the run WILL validate barring a download failure
  mismatch   — local weights do NOT match the repo manifest (the run would be local-only)
  failed     — the link didn't resolve (typo / gated repo without a token / offline)

Hashing large weights takes real time, so validation runs on a daemon thread and the result is
polled — same pattern as pod/jobs.py. The payload also carries what the GUI needs to preconfigure
the launch: detected weight formats, the platform-recommended engine, and native context.
"""
from __future__ import annotations

import collections
import os
import threading
import time
import uuid

_LOCK = threading.Lock()
_STORE: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
_CAP = 50

_PUBLIC = ("id", "state", "hf_link", "local_path", "repo", "revision", "sha", "n_files",
           "lfs_advertised", "formats", "native_ctx", "weights_hash", "n_weight_files",
           "lfs_checked", "mismatches", "recommended_engine", "no_redownload", "error",
           "family_preset", "modalities", "created_at", "updated_at")


def _set(v, **kw):
    with _LOCK:
        v.update(kw)
        v["updated_at"] = time.time()


def status(vid: str) -> dict | None:
    with _LOCK:
        v = _STORE.get(vid)
        return {k: v.get(k) for k in _PUBLIC} if v else None


def _formats(files: dict) -> list[str]:
    fmts = set()
    for f in files:
        fl = f.lower()
        if fl.endswith(".gguf"):
            fmts.add("gguf")
        elif fl.endswith(".safetensors"):
            fmts.add("safetensors")
        elif fl.endswith((".npz", ".bin", ".pt", ".pth")):
            fmts.add("torch")
    return sorted(fmts) or ["unknown"]


def _run(v: dict, token: str | None):
    from pod import engines, modelhost, presets
    try:
        repo, rev = modelhost.resolve(v["hf_link"])
        _set(v, state="resolving", repo=repo, revision=rev)
        ref = modelhost.fetch_ref(repo, rev, token=token)
        files = ref.get("files") or {}
        fmts = _formats(files)
        cfg = ref.get("config") or {}
        native_ctx = cfg.get("max_position_embeddings")
        plat = engines.host_platform()
        rec = engines.recommended_engine(plat, gguf=fmts == ["gguf"])
        # FAMILY BEST-PRACTICE PRESET: detect from the HF config so the GUI can offer an
        # "apply preset" chip. Capabilities (vision/audio) come from the same config, so the
        # recommendation's parser + multimodal allowance line up with what the model declares.
        modalities = ["text"]
        if isinstance(cfg.get("vision_config"), dict) or "image_token_id" in cfg:
            modalities.append("vision")
        if isinstance(cfg.get("audio_config"), dict) or "audio_token_id" in cfg \
                or isinstance(cfg.get("speech_config"), dict):
            modalities.append("audio")
        # VIDEO has no config marker of its own: qwen-vl-style models take video_url through
        # the vision stack, so vision-capable (or explicit video_token_id) defaults the GUI's
        # VIDEO chip on. The bench probe (probe_video) still decides at run time.
        if "vision" in modalities or "video_token_id" in cfg:
            modalities.append("video")
        preset = presets.detect(cfg, name=repo)
        _set(v, sha=ref.get("sha"), n_files=len(files),
             lfs_advertised=sum(1 for s in files.values() if s),
             formats=fmts, native_ctx=native_ctx, recommended_engine=rec,
             modalities=modalities,
             family_preset=presets.summary(preset, modalities,
                                           hardware=presets.hardware_preset(plat)))

        lp = v.get("local_path")
        if not lp:
            _set(v, state="resolved", no_redownload=False)
            return
        if not os.path.isdir(lp):
            _set(v, state="failed", error=f"local path not found: {lp}")
            return
        _set(v, state="hashing")
        ver = modelhost.verify(lp, ref)          # sha256 every weight file vs the HF LFS manifest
        _set(v, weights_hash=ver["weights_hash"], n_weight_files=ver["n_weight_files"],
             lfs_checked=ver["lfs_checked"], mismatches=ver["mismatches"][:8])
        if ver["verified"] and ver["lfs_checked"]:
            _set(v, state="validated", no_redownload=True)   # good as gold — bytes ARE repo@sha
        elif ver["verified"]:
            # weights hashed clean but HF advertises no LFS sha256 to compare against (tiny/pointer
            # repos): the launcher's hub-verified pull is the validation path — honest, not green.
            _set(v, state="resolved", no_redownload=False,
                 error="repo advertises no LFS sha256 for these files; launch will pull hub-verified")
        else:
            _set(v, state="mismatch", no_redownload=False)
    except Exception as e:
        _set(v, state="failed", error=str(e)[:300])


def start(hf_link: str, local_path: str | None = None, token: str | None = None) -> str:
    vid = uuid.uuid4().hex[:12]
    v = {"id": vid, "state": "resolving", "hf_link": hf_link.strip(),
         "local_path": (local_path or "").strip() or None,
         "created_at": time.time(), "updated_at": time.time(), "error": None}
    with _LOCK:
        _STORE[vid] = v
        while len(_STORE) > _CAP:
            _STORE.popitem(last=False)
    threading.Thread(target=_run, args=(v, token), daemon=True,
                     name=f"aeon-validate-{vid}").start()
    return vid
