"""pod/diskscan.py — find every model already on this system, and browse for one.

Backs the Run tab's local-weights picker:

  scan()          — sweep the known model homes (the pod's own /models volume, ~/.aeon/models,
                    the HuggingFace hub cache, LM Studio's model dirs, ~/models, plus any
                    AEON_SCAN_DIRS extras) and return every model found with its size, location,
                    weight format, and — wherever the on-disk layout carries the identity — an
                    AUTO-RECONCILED HuggingFace repo guess, so hash validation can run with zero
                    typing. The user can always override the guess by editing the HF link field.
  browse(path)    — one directory level at a time (dirs + weight files), so the operator can
                    navigate the POD host's filesystem from the GUI (the dashboard may be remote
                    or containerized — a client-side file picker cannot see this filesystem).

Reconciliation sources, most→least confident:
  .aeon-modelref.json   the pod's own pull metadata (exact repo@revision)
  HF hub cache layout   models--org--name/snapshots/<sha>/  ->  org/name @ sha
  LM Studio layout      <models root>/publisher/repo/*.gguf ->  publisher/repo
  AEON pull convention  org__name                           ->  org/name
  config.json           _name_or_path when it looks like org/name

Scanning reads directory entries and stat sizes only — never file contents (hashing happens in
pod/validate.py once the user picks a model)."""
from __future__ import annotations

import json
import os
import re
import string
import sys

WEIGHT_EXT = (".safetensors", ".gguf", ".bin", ".pt", ".pth", ".npz")
_MAX_MODELS = 200
_MAX_DEPTH = 4

_ORG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _known_roots() -> list[dict]:
    """The model homes worth sweeping, each tagged with how to reconcile what's inside."""
    home = os.path.expanduser("~")
    hf_home = os.environ.get("HF_HOME") or os.path.join(home, ".cache", "huggingface")
    roots = [
        {"path": os.environ.get("AEON_MODELS_DIR") or "", "kind": "aeon"},
        {"path": os.path.join(home, ".aeon", "models"), "kind": "aeon"},
        {"path": os.path.join(hf_home, "hub"), "kind": "hf-cache"},
        {"path": os.path.join(home, ".lmstudio", "models"), "kind": "lmstudio"},
        {"path": os.path.join(home, ".cache", "lm-studio", "models"), "kind": "lmstudio"},
        {"path": os.path.join(home, "models"), "kind": "generic"},
    ]
    # FULL-HOST scan (opt-in): when the operator mounts their host home read-only at
    # /host-home (-v $HOME:/host-home:ro -e AEON_HOST_HOME_DIR=$HOME), sweep the same model
    # homes THERE — HF cache, LM Studio, model folders — so a containerized pod sees every
    # model on the machine, not just the /models volume. Serving translates back to the
    # host path via AEON_HOST_HOME_DIR (engines._host_path).
    if os.path.isdir("/host-home"):
        hh = "/host-home"
        roots += [
            {"path": os.path.join(hh, ".cache", "huggingface", "hub"), "kind": "hf-cache"},
            {"path": os.path.join(hh, ".lmstudio", "models"), "kind": "lmstudio"},
            {"path": os.path.join(hh, ".cache", "lm-studio", "models"), "kind": "lmstudio"},
            {"path": os.path.join(hh, ".aeon", "models"), "kind": "aeon"},
            {"path": os.path.join(hh, "models"), "kind": "generic"},
            {"path": os.path.join(hh, "aeon-models"), "kind": "aeon"},
        ]
    for extra in re.split(r"[;:]", os.environ.get("AEON_SCAN_DIRS", "")):
        if extra.strip():
            roots.append({"path": os.path.expanduser(extra.strip()), "kind": "generic"})
    seen, out = set(), []
    for r in roots:
        p = os.path.abspath(r["path"]) if r["path"] else ""
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append({"path": p, "kind": r["kind"]})
    return out


def _dir_weights(path: str):
    """(total_bytes, n_files, formats) of weight files DIRECTLY in `path` (no recursion)."""
    total, n, fmts = 0, 0, set()
    try:
        for e in os.scandir(path):
            if e.is_file() and e.name.lower().endswith(WEIGHT_EXT):
                try:
                    total += e.stat().st_size
                except OSError:
                    continue
                n += 1
                fmts.add("gguf" if e.name.lower().endswith(".gguf")
                         else "safetensors" if e.name.lower().endswith(".safetensors") else "torch")
    except OSError:
        pass
    return total, n, sorted(fmts)


def _reconcile(path: str, root: dict, rel: str):
    """Best local-only HF repo guess for a model dir. Returns (repo, revision, source) or Nones."""
    # 1) the pod's own pull metadata — exact
    try:
        mref = json.load(open(os.path.join(path, ".aeon-modelref.json"), encoding="utf-8"))
        if mref.get("repo"):
            return mref["repo"], mref.get("revision"), "aeon-modelref"
    except Exception:
        pass
    # 2) layout-carried identity
    if root["kind"] == "lmstudio":
        parts = rel.replace("\\", "/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}", None, "lmstudio-layout"
    base = os.path.basename(path.rstrip("/\\"))
    if root["kind"] == "aeon" and "__" in base:
        return base.replace("__", "/", 1), None, "aeon-layout"
    # 3) config.json breadcrumb
    try:
        cfg = json.load(open(os.path.join(path, "config.json"), encoding="utf-8"))
        nop = cfg.get("_name_or_path") or ""
        if _ORG_NAME_RE.match(nop):
            return nop, None, "config.json"
    except Exception:
        pass
    return None, None, None


def _scan_hf_cache(root: str, out: list):
    """HF hub cache: models--org--name/snapshots/<sha>/ — identity is exact from the layout."""
    try:
        entries = [e for e in os.scandir(root) if e.is_dir() and e.name.startswith("models--")]
    except OSError:
        return
    for e in entries:
        repo = e.name[len("models--"):].replace("--", "/", 1)
        snaps = os.path.join(e.path, "snapshots")
        best = None
        try:
            for s in os.scandir(snaps):
                if not s.is_dir():
                    continue
                total, n, fmts = _dir_weights(s.path)
                if n and (best is None or total > best[1]):
                    best = (s, total, n, fmts)
        except OSError:
            continue
        if best:
            s, total, n, fmts = best
            out.append({"path": s.path, "name": repo, "size_bytes": total, "n_weight_files": n,
                        "formats": fmts, "source": "hf-cache",
                        "hf_guess": repo, "hf_revision": s.name, "guess_source": "hf-cache-layout"})


def _scan_tree(root: dict, out: list):
    """Bounded walk: a dir that directly holds weight files IS a model (pruned below)."""
    base_depth = root["path"].rstrip("/\\").count(os.sep)
    for dirpath, dirnames, _ in os.walk(root["path"]):
        if dirpath.rstrip("/\\").count(os.sep) - base_depth >= _MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".aeon"]
        total, n, fmts = _dir_weights(dirpath)
        if not n:
            continue
        dirnames[:] = []                                   # a model dir — don't descend into shards
        rel = os.path.relpath(dirpath, root["path"])
        repo, rev, src = _reconcile(dirpath, root, rel)
        out.append({"path": dirpath, "name": repo or os.path.basename(dirpath),
                    "size_bytes": total, "n_weight_files": n, "formats": fmts,
                    "source": root["kind"], "hf_guess": repo, "hf_revision": rev,
                    "guess_source": src})
        if len(out) >= _MAX_MODELS:
            return


def scan() -> dict:
    """Every model on this system: [{path, name, size_bytes, n_weight_files, formats, source,
    hf_guess, hf_revision, guess_source}], largest first."""
    out: list = []
    roots = _known_roots()
    for root in roots:
        if len(out) >= _MAX_MODELS:
            break
        if root["kind"] == "hf-cache":
            _scan_hf_cache(root["path"], out)
        else:
            _scan_tree(root, out)
    # de-dup by resolved path (roots can nest, e.g. AEON_MODELS_DIR under an AEON_SCAN_DIRS)
    seen, uniq = set(), []
    for m in out:
        key = os.path.normcase(os.path.abspath(m["path"]))
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    uniq.sort(key=lambda m: -m["size_bytes"])
    return {"models": uniq[:_MAX_MODELS], "roots": [r["path"] for r in roots],
            # tells the GUI whether the FULL-HOST sweep is active (opt-in /host-home mount)
            "host_scan": os.path.isdir("/host-home")}


def browse(path: str | None = None) -> dict:
    """One directory level for the GUI browser. No path -> the entry points (model homes, home
    dir, and drives on Windows). Dirs only + the weight files directly inside `path`."""
    if not path:
        roots = [{"path": r["path"], "label": f"{r['kind']}: {r['path']}"} for r in _known_roots()]
        home = os.path.expanduser("~")
        roots.append({"path": home, "label": f"home: {home}"})
        if sys.platform == "win32":
            for d in string.ascii_uppercase:
                if os.path.exists(f"{d}:\\"):
                    roots.append({"path": f"{d}:\\", "label": f"drive {d}:"})
        return {"path": None, "roots": roots}
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(p):
        return {"path": p, "error": "not a directory"}
    dirs, files = [], []
    try:
        for e in sorted(os.scandir(p), key=lambda x: x.name.lower()):
            try:
                if e.is_dir():
                    w_total, w_n, _ = _dir_weights(e.path)
                    dirs.append({"name": e.name, "path": e.path,
                                 "has_weights": w_n > 0, "weights_bytes": w_total})
                elif e.is_file() and e.name.lower().endswith(WEIGHT_EXT):
                    files.append({"name": e.name, "size_bytes": e.stat().st_size})
            except OSError:
                continue
    except OSError as ex:
        return {"path": p, "error": str(ex)}
    total, n, fmts = _dir_weights(p)
    return {"path": p, "parent": os.path.dirname(p.rstrip("/\\")) or None,
            "dirs": dirs[:400], "weight_files": files[:100],
            "is_model": n > 0, "weights_bytes": total, "formats": fmts}
