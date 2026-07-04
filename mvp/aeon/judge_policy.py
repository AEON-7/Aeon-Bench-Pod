"""Judge policy — a benchmark answer is scored EITHER deterministically (a known correct
answer / programmatic checker) OR by a designated FRONTIER model. Never by the model under
test (self-judge), never by an arbitrary/weak model. A model must never be able to inflate
its own score, and a weak judge must never gate the board.

The frontier allow-list is operator config: AEON_FRONTIER_JUDGES = comma-separated name
fragments. A judge is 'frontier' iff its id matches one of those fragments AND is not the
model under test. With no (or a disallowed) judge, only deterministic criteria are scored.
"""
from __future__ import annotations

import os

# Conservative defaults; the operator overrides with AEON_FRONTIER_JUDGES.
_DEFAULT_FRONTIER = [
    "claude-opus", "claude-sonnet", "claude-3", "claude-4", "claude-fable", "claude-5",
    "gpt-4", "gpt-5", "o1", "o3", "o4-",
    "gemini-1.5", "gemini-2", "gemini-3", "gemini-ultra", "gemini-pro",
    "grok-2", "grok-3", "grok-4", "deepseek-r1", "deepseek-v3", "llama-3.1-405", "mistral-large",
]


def frontier_patterns():
    env = os.environ.get("AEON_FRONTIER_JUDGES")
    if env:
        return [p.strip().lower() for p in env.split(",") if p.strip()]
    return _DEFAULT_FRONTIER


def is_frontier_judge(judge_model) -> bool:
    if not judge_model:
        return False
    j = str(judge_model).strip().lower()
    return any(p in j for p in frontier_patterns())


def judge_mode(judge_model, model_under_test) -> str:
    """'deterministic' (no judge), 'frontier' (allowed), or 'disallowed' (self-judge or a
    non-frontier judge — its subjective criteria must NOT be scored)."""
    if not judge_model:
        return "deterministic"
    if model_under_test and str(judge_model).strip().lower() == str(model_under_test).strip().lower():
        return "disallowed"            # self-judge — never allowed
    return "frontier" if is_frontier_judge(judge_model) else "disallowed"


def allowed(judge_model, model_under_test) -> bool:
    return judge_mode(judge_model, model_under_test) != "disallowed"
