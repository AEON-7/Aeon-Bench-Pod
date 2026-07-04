"""Per-harness ADAPTER scaffolds — the thin drivers that take an AEON agentic task, run it
through one of the vanilla agent harnesses (Hermes / OpenClaw / OpenCode) pointed at the pod's
served model, and return a transcript `{"steps": [...], "answer": ...}` that
`aeon.agentic.score_agentic` can score deterministically.

Each adapter is responsible for the harness-specific glue ONLY:
  1. configure the harness to talk to the pod's served model (`model_base_url` + `served_alias`),
  2. present the task's tools (converted to the harness's tool/function schema),
  3. drive the goal (`task["prompt"]`) to completion,
  4. capture the tool-call sequence + final answer into the AEON transcript shape.

The scoring metric lives in `aeon.agentic` and is harness-agnostic, so the harnesses can be
compared apples-to-apples; the disclosed (harness, harness_version) travels with every result
(see `pod.harnesses.disclose`).

`MockAdapter` lets the whole pipeline run end-to-end with NO harness installed — it reads each
task's `success` spec and emits a transcript that satisfies it. The three real adapters import
cleanly without their harness present (every docker/CLI call is guarded) so this module is always
importable; calling `run_task` on a real adapter without its harness raises a clear error.
"""
from __future__ import annotations

from .hermes import HermesAdapter
from .mock import MockAdapter
from .openclaw import OpenClawAdapter
from .opencode import OpenCodeAdapter

# Registry keyed by the same harness ids used in pod.harnesses.HARNESSES.
ADAPTERS = {
    "hermes": HermesAdapter,
    "openclaw": OpenClawAdapter,
    "opencode": OpenCodeAdapter,
    # not a real harness — only for testing the pipeline without a GPU host:
    "mock": MockAdapter,
}


def get(harness: str):
    """Return an *instance* of the adapter for `harness`.

    Raises KeyError with the list of known harnesses if unknown.
    """
    try:
        cls = ADAPTERS[harness]
    except KeyError:
        raise KeyError(f"unknown harness {harness!r}; known: {sorted(ADAPTERS)}") from None
    return cls()


__all__ = ["ADAPTERS", "get", "HermesAdapter", "OpenClawAdapter", "OpenCodeAdapter", "MockAdapter"]
