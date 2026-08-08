"""`Adapter` — the common interface every per-harness adapter implements.

An adapter is the (small) amount of harness-specific code that turns an AEON agentic task into
a scored transcript. The contract is one method:

    run_task(task, model_base_url, served_alias, *, timeout=300) -> {"steps": [...], "answer": ...}

where `task` is an entry of `aeon.agentic.AGENTIC_CASES`:

    {"prompt": str,
     "tools": [{"name": str, "params": [str, ...]}, ...],
     "success": {...},               # the deterministic success spec (scored, not given to the model)
     "optimal_steps": int}

and the returned transcript is exactly what `aeon.agentic.score_agentic(task, transcript)` expects:

    {"steps": [{"tool": <name>, "args": {<param>: <value>, ...}}, ...],
     "answer": <final assistant text>}

Each concrete adapter MUST:
  1. configure the harness to use the OpenAI-compatible endpoint `model_base_url` and the served
     model name `served_alias` (this is the model under test — the pod serves it),
  2. present `task["tools"]` to the harness (use `_tools_to_openai` to get the standard
     function-calling schema, or translate further to the harness's own tool format),
  3. run `task["prompt"]` to completion within `timeout` seconds,
  4. parse the harness's tool-call log / output into the `{steps, answer}` transcript.

NOTE: the tools are *presented* to the model but, for the deterministic agentic suite, they do not
need to be backed by real implementations — scoring only inspects which tools were called, with
what args, and the final answer. A harness that requires executable tools should register no-op /
stub handlers (each adapter notes how). The success spec is never shown to the model.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import uuid
from typing import Any

from .. import harnesses


class AdapterError(RuntimeError):
    """Raised when an adapter cannot run a task (harness missing, bad output, timeout, ...)."""


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(s: str) -> str:
    """Filesystem/volume-safe token for aliases like 'dgx/aeon-ultimate:latest'."""
    return _SAFE_RE.sub("_", str(s)).strip("_") or "model"


# Reasoning-trace scrubber. Agentic harnesses run the model inside their OWN third-party
# container and surface its RAW assistant text in their transcript — so a reasoning model's
# <think>...</think> block (even an EMPTY one: gemma emits "<think>\n</think>\n" before the
# answer) leaks into the final answer. The serve-side --reasoning-parser only cleans the
# direct chat path (message.content), never a harness container's transcript. <think> content
# is NEVER part of an answer, so strip it at every adapter's parse boundary.
_THINK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?\s*>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"^\s*<think(?:ing)?\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop <think>...</think> reasoning blocks (incl. empty) and a LEADING unclosed/truncated
    <think> trace from a harness answer. Idempotent; a no-op on plain text."""
    if not text:
        return text
    text = _THINK_RE.sub("", text)
    text = _THINK_OPEN_RE.sub("", text)   # a reasoning trace that opened but got cut off
    return text.strip()


def run_container_io(image: str, args: list, *, seed=None, seed_optional=None, collect=None,
                     timeout: float = 240, name_hint: str = "task", env=None, workdir=None):
    """Run a one-shot harness container with file I/O that works no matter where the POD
    itself runs — bare metal OR inside a container. Bind-mounting a pod-local path breaks
    when the pod is containerized: the docker CLI talks to the HOST daemon, which resolves
    the path on the HOST filesystem and silently mounts an EMPTY directory (this zeroed
    harness scores: configs vanished, task seed files vanished). `docker cp` streams bytes
    through the client instead, so it is placement-independent:

        docker create -> docker cp seeds IN -> docker start -a (stdout+stderr+exit code)
        -> docker cp outcomes OUT (best-effort) -> docker rm -f

    seed / seed_optional: [(src_path, container_dst)] — optional seeds tolerate failure
    (e.g. a config whose parent dir may not exist in the image). Directory sources try the
    contents form (`src/.` -> existing dst) first, then the create-dst form.
    collect: [(container_src, dst_path)] copied out after the run, never raising.
    Returns (stdout, stderr, returncode, duration_s)."""
    cname = f"aeon_{safe_name(name_hint)}_{uuid.uuid4().hex[:10]}"
    # Label every harness container so the job manager / boot reconciler can sweep orphans:
    # this in-process `finally: docker rm -f` never runs when the runner is SIGTERM'd mid-stage.
    create = ["docker", "create", "--name", cname, "--network", "host",
              "--label", "aeon.pod.harness=1"]
    _jid = os.environ.get("AEON_JOB_ID")
    if _jid:
        create += ["--label", f"aeon.pod.job={_jid}"]
    for k, v in (env or {}).items():
        create += ["-e", f"{k}={v}"]
    if workdir:
        create += ["-w", workdir]
    create += [image] + [str(a) for a in args]

    def _cp_in(src, dst, required):
        if os.path.isdir(src):
            o, e, rc, _ = run_argv(["docker", "cp", src.rstrip("/\\") + "/.", f"{cname}:{dst}"], 120)
            if rc != 0:                       # dst may not exist in the image -> create-dst form
                o, e, rc, _ = run_argv(["docker", "cp", src, f"{cname}:{dst}"], 120)
        else:
            o, e, rc, _ = run_argv(["docker", "cp", src, f"{cname}:{dst}"], 120)
        if rc != 0 and required:
            raise AdapterError(f"docker cp seed failed ({src} -> {dst}): {e[:300]}")

    try:
        o, e, rc, _ = run_argv(create, 120)
        if rc != 0:
            raise AdapterError(f"docker create failed rc={rc}: {e[:300]}")
        for src, dst in (seed or []):
            _cp_in(src, dst, required=True)
        for src, dst in (seed_optional or []):
            try:
                _cp_in(src, dst, required=False)
            except AdapterError:
                pass
        out, err, rcode, dur = run_argv(["docker", "start", "-a", cname], timeout)
        for src, dst in (collect or []):
            try:
                run_argv(["docker", "cp", f"{cname}:{src}", dst], 120)
            except Exception:
                pass
        return out, err, rcode, dur
    finally:
        try:
            run_argv(["docker", "rm", "-f", cname], 30)
        except Exception:
            pass


def run_argv(argv: list[str], timeout: float, cwd: str | None = None):
    """Run one subprocess (typically `docker run --rm ...`) and capture everything.

    Returns `(stdout, stderr, returncode, duration_s)`. Raises AdapterError on launch
    failure or timeout — the caller (run_harness2) turns that into a per-task
    `harness_error` without aborting the batch.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd)
    except FileNotFoundError as e:
        raise AdapterError(f"cannot launch {argv[0]!r}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError(f"{argv[0]} timed out after {timeout}s "
                           f"(cmd: {' '.join(argv[:8])}...)") from e
    return proc.stdout or "", proc.stderr or "", proc.returncode, time.monotonic() - t0


# ---- harness images: BUILT HERE, never redistributed -----------------------------------------
# The harness images wrap third-party agents (Hermes / OpenClaw / OpenCode). Rather than ship
# prebuilt copies of somebody else's software, the pod builds them on whatever machine it is
# running on, from the Dockerfiles it carries — the operator gets the upstream code straight from
# its own source, and we redistribute nothing.
#
# Before this, a pod without the image just failed `docker create` with "pull access denied" and
# then scored 0 on EVERY agentic task — silently, for an entire multi-hour run.
#
# WHERE it builds follows DOCKER_HOST, so both shapes of run are covered with no extra machinery:
#   unset / unix socket   -> builds and runs here, on the pod's own machine, pointed at the model's
#                            endpoint (this is the shape for benching a remote endpoint from here)
#   ssh://user@host       -> builds and RUNS on that host; the docker client streams this context
#                            over the operator's own ssh, so the harnesses land on the same machine
#                            that serves the model
HARNESS_DIR = os.environ.get("AEON_HARNESS_DIR", "/app/harness")   # baked in by deploy/pod/Dockerfile
BUILD_TIMEOUT = float(os.environ.get("AEON_HARNESS_BUILD_TIMEOUT", "2400"))
_ensured: set[str] = set()


def _image_present(image: str) -> bool:
    try:
        _o, _e, rc, _d = run_argv(["docker", "image", "inspect", image], 60)
    except AdapterError:
        return False
    return rc == 0


def _prereq_help(image: str, harness: str, dockerfile: str, err: str) -> str:
    """A build failure has to tell the operator exactly what to install and what to run, not just
    echo docker's stderr. In practice it is always one of: no docker socket, no permission on it,
    no outbound network to the base-image/package registries, or no disk."""
    dh = os.environ.get("DOCKER_HOST") or ""
    where = ("the REMOTE host (DOCKER_HOST=" + dh + ")") if dh.startswith("ssh://") \
        else "this machine (the pod's own docker)"
    return (
        "could not build the agentic harness image '" + image + "'.\n"
        "  Dockerfile : " + dockerfile + "\n"
        "  building on: " + where + "\n"
        "  build error: " + (err[:500] or "(no output)") + "\n"
        "\n"
        "REQUIREMENTS for the agentic suite on " + where + "\n"
        "  1. Docker Engine, reachable by the pod. The pod builds and runs harness containers\n"
        "     through the mounted socket, so start the pod with:\n"
        "       -v /var/run/docker.sock:/var/run/docker.sock\n"
        "     verify:  docker info\n"
        "     install: https://docs.docker.com/engine/install/\n"
        "  2. Permission to use that socket without sudo:\n"
        "       sudo usermod -aG docker \"$USER\" && newgrp docker\n"
        "  3. Outbound network to the sources the build pulls from:\n"
        "       ghcr.io / docker.io (base images) - github.com - pypi.org - registry.npmjs.org\n"
        "  4. Free disk: roughly 3 GB per harness image.\n"
        "  5. buildx/BuildKit is optional; a plain `docker build` is enough.\n"
        "\n"
        "BUILD IT YOURSELF (identical to what the pod just tried)\n"
        "  docker build -f " + dockerfile + " -t " + image + " " + HARNESS_DIR + "\n"
        "\n"
        "OR reuse an image you already have\n"
        "  export AEON_" + harness.upper() + "_IMAGE=<your-image>\n"
        "\n"
        "Agentic scores 0 without a working harness, and agentic is 30% of the AEON score - a run\n"
        "missing it is not a complete benchmark and will not rank."
    )


def ensure_image(image: str, harness: str) -> None:
    """Make `image` exist on this machine, building it from the shipped Dockerfile if it doesn't.

    Idempotent, cached per process. Raises AdapterError carrying the prerequisites (see
    `_prereq_help`) when the build fails, so the operator sees what to install rather than a bare
    docker error buried in a per-task transcript."""
    if image in _ensured or _image_present(image):
        _ensured.add(image)
        return
    dockerfile = os.path.join(HARNESS_DIR, "harness-" + harness + ".Dockerfile")
    if not os.path.exists(dockerfile):
        raise AdapterError(
            "harness image '" + image + "' is not on this machine and its Dockerfile is missing ("
            + dockerfile + "). Update the pod image, or build the harness yourself and point "
            "AEON_" + harness.upper() + "_IMAGE at it.")
    print("[pod] agentic: building harness image " + image + " from " + dockerfile
          + " (first run on this machine; expect a few minutes)", flush=True)
    t0 = time.time()
    try:
        o, e, rc, _d = run_argv(
            ["docker", "build", "-f", dockerfile, "-t", image, HARNESS_DIR], BUILD_TIMEOUT)
    except AdapterError as ex:                      # docker missing entirely, or build timed out
        raise AdapterError(_prereq_help(image, harness, dockerfile, str(ex))) from None
    if rc != 0 or not _image_present(image):
        raise AdapterError(_prereq_help(image, harness, dockerfile, (e or o or "").strip()))
    print("[pod] agentic: built %s in %.0fs" % (image, time.time() - t0), flush=True)
    _ensured.add(image)


class Adapter:
    #: harness id — must match a key in `pod.harnesses.HARNESSES`. Overridden by subclasses.
    name: str = ""

    # ---- version / disclosure -------------------------------------------------------------

    def version(self, pin: str | None = None) -> str | None:
        """The exact harness build in use, for disclosure. Delegates to
        `pod.harnesses.resolve_version` (explicit pin wins, else queries the installed CLI,
        else None = unknown)."""
        return harnesses.resolve_version(self.name, pin)

    def disclose(self, pin: str | None = None) -> dict:
        """{harness, harness_name, harness_repo, harness_version} record for the report."""
        return harnesses.disclose(self.name, pin)

    # ---- tool schema helpers --------------------------------------------------------------

    @staticmethod
    def _tools_to_openai(tools: list[dict] | None) -> list[dict]:
        """Convert the AEON tool spec -> OpenAI function-calling `tools` array.

        AEON tool:  {"name": "calculator", "params": ["expression"]}
        OpenAI tool:{"type": "function",
                     "function": {"name": "calculator", "description": ...,
                                  "parameters": {"type": "object",
                                                 "properties": {"expression": {"type": "string"}},
                                                 "required": ["expression"]}}}

        All params are typed as free-form strings (the AEON spec carries names only) and are all
        marked required — the suite's `arg_validity` sub-metric rewards supplying every declared
        param. This schema is what Hermes / OpenClaw / OpenCode are handed (each may translate it
        further to its own tool format)."""
        out = []
        for t in tools or []:
            params = list(t.get("params") or [])
            out.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or f"AEON agentic tool '{t['name']}'.",
                    "parameters": {
                        "type": "object",
                        "properties": {p: {"type": "string"} for p in params},
                        "required": params,
                    },
                },
            })
        return out

    @staticmethod
    def _tool_names(tools: list[dict] | None) -> list[str]:
        return [t["name"] for t in (tools or [])]

    # ---- the run contract -----------------------------------------------------------------

    def prepare_run(self, model_base_url: str, served_alias: str, run_root: str):
        """v2 contract: create a FRESH config/state dir for this model-run under `run_root`
        (no state reuse between models). Default: no-op (adapter needs no prepared state)."""
        return None

    def cleanup_run(self) -> None:
        """v2 contract: tear down whatever `prepare_run` created. Default: no-op."""
        return None

    def run_task(self, task: dict, model_base_url: str, served_alias: str,
                 workdir: str | None = None, *, timeout: int = 300) -> dict:
        """Drive the harness through one task.

        v2 (environment-execution) contract — `workdir` given: launch ONE one-shot
        `docker run --rm --network host` with `workdir` mounted at /work (and -w /work) so
        the agent's file operations land in `workdir` for scoring; return
        `{"answer": str, "steps": [{"tool", "args"}, ...], "raw": str, "duration_s": float}`.

        v1 (synthetic-tools) legacy contract — `workdir` None: return
        `{"steps": [...], "answer": ...}`.

        Subclasses MUST override."""
        raise NotImplementedError(f"{type(self).__name__}.run_task is not implemented")

    # ---- shared parser surface ------------------------------------------------------------

    @staticmethod
    def _empty_transcript(answer: str = "") -> dict:
        return {"steps": [], "answer": answer}

    @staticmethod
    def _normalize_transcript(obj: Any) -> dict:
        """Coerce a loosely-shaped harness result into the strict transcript shape.

        Accepts any object that already looks like `{"steps": [...], "answer": ...}` and
        normalises each step to `{"tool": str, "args": dict}` — tolerating common aliases
        (`tool`/`name`/`function`, `args`/`arguments`/`parameters`/`input`). Unknown shapes
        degrade to an empty step list rather than crashing the run."""
        if not isinstance(obj, dict):
            return Adapter._empty_transcript()
        raw_steps = obj.get("steps") or obj.get("tool_calls") or []
        steps = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            tool = s.get("tool") or s.get("name") or s.get("function")
            if isinstance(tool, dict):                     # {"function": {"name": ...}}
                tool = tool.get("name")
            args = (s.get("args") or s.get("arguments")
                    or s.get("parameters") or s.get("input") or {})
            if isinstance(args, str):                      # JSON-encoded arguments string
                import json
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_raw": args}
            if tool:
                steps.append({"tool": tool, "args": args})
        answer = obj.get("answer")
        if answer is None:
            answer = obj.get("output") or obj.get("final") or obj.get("content") or ""
        return {"steps": steps, "answer": answer if isinstance(answer, str) else str(answer)}
