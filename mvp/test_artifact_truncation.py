"""Truncated gallery artifacts (`pod.arena_gen.is_complete`).

Runs green with no GPU, docker or network.

Why this exists: every gallery artifact from the first two external GOD MODE runs was cut off
mid-token — no closing `</html>`, an unclosed `<script>`, ending on `const p1all = p1 &&` or
`box-shadow:`. They rendered blank or half-drawn, and looked exactly like bad model code. They
were not: generation was capped at 8000 tokens (~23 KB at the ~3 chars/token those runs measured),
and the `ok` test was `bool(html.strip()) and "<" in html`, which a truncated file passes.

The fixtures below are reduced from those real artifacts.
"""
from __future__ import annotations

import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import arena_gen                                          # noqa: E402

COMPLETE = ('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>t</title>'
            '<style>body{margin:0}</style></head><body><canvas id="c"></canvas>'
            '<script>const ctx=document.getElementById("c").getContext("2d");</script>'
            '</body></html>')

# reduced from c0eb2fe05f — cut mid-expression inside <script>
CUT_IN_JS = ('<!DOCTYPE html><html lang="en"><head><title>Vault</title></head><body>'
             '<canvas id="c"></canvas><script>const plates=[];'
             'const p1 = plates[0].pressed, p2 = plates[1].pressed;'
             'const p1all = p1 &&')

# reduced from ef1194da3e — cut mid-CSS-property, no <script> at all
CUT_IN_CSS = ('<!DOCTYPE html><html lang="en"><head><title>World Bible</title><style>'
              '.panel{border-bottom:1px solid var(--border);flex-shrink:0;box-shadow:')

NO_SCRIPT_BUT_COMPLETE = '<html><body><h1>A static page</h1></body></html>'


def test_the_real_truncated_shapes_are_caught():
    assert arena_gen.is_complete(CUT_IN_JS) is False
    assert arena_gen.is_complete(CUT_IN_CSS) is False


def test_complete_documents_pass():
    assert arena_gen.is_complete(COMPLETE) is True
    assert arena_gen.is_complete(NO_SCRIPT_BUT_COMPLETE) is True


def test_buggy_but_complete_still_publishes():
    """THE line not to cross. We are detecting OUR truncation, not judging the model's work.
    A complete artifact that happens to be broken JavaScript is a real result and must still be
    published and votable — otherwise the gallery only shows models that write flawless code."""
    buggy = '<html><body><script>undefinedFunction(</script></body></html>'
    assert arena_gen.is_complete(buggy) is True
    infinite = '<html><body><script>while(true){}</script></body></html>'
    assert arena_gen.is_complete(infinite) is True


def test_unbalanced_script_is_truncation():
    assert arena_gen.is_complete('<html><body><script>let a=1;</script><script>let b=2') is False
    assert arena_gen.is_complete('<html><body><script>let a=1;</script></body></html>') is True


def test_empty_and_garbage():
    for x in ("", "   ", None):
        assert arena_gen.is_complete(x) is False
    assert arena_gen.is_complete("just some prose, no markup at all") is False


def test_closing_tag_is_case_insensitive():
    assert arena_gen.is_complete('<HTML><BODY>x</BODY></HTML>') is True


def test_budgets_are_sized_to_the_storage_cap():
    """The budget is not a round number pulled from the air: at the ~3 chars/token those runs
    measured, the god budget lands just under MAX_HTML_BYTES, so nothing is generated that cannot
    then be stored."""
    assert arena_gen.DEFAULT_ARENA_MAX_TOKENS >= 32768, "8000 truncated every real artifact"
    assert arena_gen.GOD_ARENA_MAX_TOKENS >= arena_gen.DEFAULT_ARENA_MAX_TOKENS
    est_bytes = arena_gen.GOD_ARENA_MAX_TOKENS * 3
    assert est_bytes <= arena_gen.MAX_HTML_BYTES * 1.05, (est_bytes, arena_gen.MAX_HTML_BYTES)


def test_god_scope_gets_the_bigger_budget():
    """A god-tier draw must not run on the standard budget — those prompts ask for a raycaster or
    a path tracer as one file."""
    import inspect
    src = inspect.getsource(arena_gen.generate_for_model)
    assert "GOD_ARENA_MAX_TOKENS" in src and 'only_difficulty == "god_mode"' in src


if __name__ == "__main__":
    import traceback
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception:
                failed.append(name)
                print(f"  FAIL  {name}")
                traceback.print_exc(limit=3)
    print("\n" + ("FAILED: " + ", ".join(failed) if failed else "all green"))
    sys.exit(1 if failed else 0)
