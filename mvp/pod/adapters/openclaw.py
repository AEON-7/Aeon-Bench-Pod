"""OpenClawAdapter — drives the `aeon-harness-openclaw` container image (openclaw/openclaw CLI
inside node:24-slim; built on the DGX, arm64, v2026.6.11).

Invocation (one one-shot container per task; the pod code runs ON the DGX):

    docker run --rm --network host -v <cfg_dir>:/root/.openclaw -v <workdir>:/work -w /work \
        aeon-harness-openclaw agent --local --json --agent default \
        -m "<prompt>" --model dgx/<alias>

`<cfg_dir>` is a FRESH per-model-run directory holding `openclaw.json`:

    {"models": {"providers": {"dgx": {"baseUrl": <model_base_url>, "apiKey": "sk-local",
                                      "api": "openai-completions",
                                      "models": [{"id": "<alias>", "name": "<alias>",
                                                  "contextWindow": 32768,
                                                  "maxTokens": 8192}]}}},
     "agents": {"defaults": {"model": {"primary": "dgx/<alias>"}}}}

Because /root/.openclaw is where OpenClaw also keeps its session state, mounting a fresh dir
per model-run guarantees no state carries between models.

stdout is a JSON document; the final answer lives at `result.payloads[].text`. OpenClaw emits
NO tool-call trace on stdout (diagnostics go to stderr), so `steps` is always `[]` — scoring
for the v2 suite is outcome-based (files in the workdir + answer), which needs no trace.
`parse_output(raw_stdout)` is a pure function (unit-tested against canned samples).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

from .base import Adapter, AdapterError, run_argv, run_container_io, safe_name, strip_reasoning

IMAGE = os.environ.get("AEON_OPENCLAW_IMAGE", "aeon-harness-openclaw")
_PROVIDER_ID = "dgx"
_API_KEY = "sk-local"


def _copy_tree_into(src: str, dst: str) -> None:
    """Copy every entry of `src` into existing dir `dst` (files overwrite, dirs merge).
    Best-effort: a per-entry failure (e.g. a root-owned artifact) never aborts the run."""
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        try:
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        except (OSError, shutil.Error):
            pass


def _rm_root_owned(path: str) -> None:
    """Remove a dir that may contain root-owned files (the container runs as root).
    Try a plain rmtree first; if that leaves anything, fall back to a throwaway root
    container to unlink the residue. Never raises."""
    shutil.rmtree(path, ignore_errors=True)
    if os.path.isdir(path):
        try:
            run_argv(["docker", "run", "--rm", "-v", f"{path}:/x",
                      "--entrypoint", "sh", IMAGE,
                      "-c", "rm -rf /x/* /x/.[!.]* 2>/dev/null || true"], 60)
        except Exception:
            pass
        shutil.rmtree(path, ignore_errors=True)


def build_config(model_base_url: str, served_alias: str) -> dict:
    """The exact openclaw.json this model-run uses (verified schema — see module docstring)."""
    return {
        "models": {
            "providers": {
                _PROVIDER_ID: {
                    "baseUrl": model_base_url,
                    "apiKey": _API_KEY,
                    "api": "openai-completions",
                    "models": [{"id": served_alias, "name": served_alias,
                                "contextWindow": 131072, "maxTokens": 8192}],
                }
            }
        },
        "agents": {"defaults": {"model": {"primary": f"{_PROVIDER_ID}/{served_alias}"}}},
    }


def _texts_from_payloads(payloads) -> list[str]:
    out = []
    if isinstance(payloads, list):
        for p in payloads:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
            elif isinstance(p, str) and p.strip():
                out.append(p.strip())
    return out


def parse_output(raw_stdout: str) -> dict:
    """Pure parser: OpenClaw stdout JSON -> {"answer": str, "steps": []}.

    The canonical shape is one JSON object with `result.payloads[].text`. Defensively also
    accepts top-level `payloads`, a bare `result.text`, or the JSON object appearing on one
    line of otherwise-noisy stdout. There is never a tool trace on stdout -> steps == [].
    """
    raw = (raw_stdout or "").strip()
    candidates = []
    if raw:
        try:
            candidates.append(json.loads(raw))
        except Exception:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        candidates.append(json.loads(line))
                    except Exception:
                        continue

    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        result = obj.get("result") if isinstance(obj.get("result"), dict) else obj
        texts = _texts_from_payloads(result.get("payloads"))
        if not texts and isinstance(result.get("text"), str):
            texts = [result["text"].strip()]
        if not texts and isinstance(obj.get("payloads"), list):
            texts = _texts_from_payloads(obj.get("payloads"))
        if texts:
            return {"answer": strip_reasoning("\n".join(texts)), "steps": []}
    return {"answer": "", "steps": []}


class OpenClawAdapter(Adapter):
    name = "openclaw"
    IMAGE = IMAGE

    def __init__(self):
        self._cfg_dir: str | None = None
        self._own_root: str | None = None
        self._model: str | None = None

    # ---- v2 contract ------------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """Fresh per-model-run config+state dir (mounted at /root/.openclaw) — a new dir per
        model means OpenClaw's session store starts empty for every model-run."""
        d = os.path.join(run_root, f"openclaw-{safe_name(served_alias)}")
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "openclaw.json"), "w", encoding="utf-8") as f:
            json.dump(build_config(model_base_url, served_alias), f, indent=2)
        self._cfg_dir = d
        self._model = f"{_PROVIDER_ID}/{served_alias}"
        return d

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 240) -> dict:
        if workdir is None:
            raise AdapterError("OpenClawAdapter (v2) requires a task workdir")
        model = self._model or f"{_PROVIDER_ID}/{served_alias}"

        # OpenClaw's agent tools (read/write/edit) operate inside its WORKSPACE
        # (~/.openclaw/workspace), NOT the container cwd — mounting the task files at /work
        # leaves the agent unable to find them (it loops, fills context, and trips a buggy
        # auto-compaction path). So we give OpenClaw a private, per-task home whose `workspace/`
        # subdir IS the seeded task files, mount that single dir at /root/.openclaw, and copy
        # the produced files back into `workdir` so run_harness2's outcome scoring sees them.
        home = tempfile.mkdtemp(prefix="aeon_claw_home_")
        try:
            ws = os.path.join(home, "workspace")
            os.makedirs(ws, exist_ok=True)
            _copy_tree_into(workdir, ws)   # seed the agent's workspace with the task files
            with open(os.path.join(home, "openclaw.json"), "w", encoding="utf-8") as f:
                json.dump(build_config(model_base_url, served_alias), f, indent=2)

            # docker-cp I/O (run_container_io): a bind mount of this pod-local `home` breaks
            # when the pod is containerized (daemon resolves the path on the HOST -> empty
            # /root/.openclaw -> "Unknown model: dgx/<alias>" and no task files).
            out, err, rc, dur = run_container_io(
                self.IMAGE,
                ["agent", "--local", "--json", "--agent", "main",
                 "-m", task.get("prompt", ""), "--model", model],
                seed=[(home, "/root/.openclaw")],
                collect=[("/root/.openclaw/workspace/.", ws)],
                timeout=timeout, name_hint=f"claw_{served_alias}")
            _copy_tree_into(ws, workdir)   # bring the agent's file outcomes back for scoring
        finally:
            _rm_root_owned(home)           # best-effort cleanup (docker-cp output is pod-owned)

        parsed = parse_output(out)
        if rc != 0 and not parsed["answer"]:
            raise AdapterError(f"openclaw exited {rc} with no parseable output; "
                               f"stderr: {err[:800]}")
        return {"answer": parsed["answer"], "steps": parsed["steps"],
                "raw": out, "duration_s": dur}

    def cleanup_run(self) -> None:
        if self._cfg_dir:
            shutil.rmtree(self._cfg_dir, ignore_errors=True)
        if self._own_root:
            shutil.rmtree(self._own_root, ignore_errors=True)
        self._cfg_dir = self._own_root = self._model = None
