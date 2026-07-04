"""AEON Bench — local MVP.

A small, faithful slice of the AEON Bench design (see ../DESIGN.md):
a benchmark runner that drives any OpenAI-compatible model through a tiny
*deterministic* suite, scores it (Tier-0 programmatic + Tier-1 binary-rubric
judging where the judge defaults to the launching/target model), captures
speed metrics, and serves a leaderboard dashboard.
"""

__version__ = "0.1.0-mvp"
