"""Artifact size limits: per-artifact vs per-bundle, and that the layers agree.

An artifact that embeds its own assets (base64 textures, audio) is legitimately megabytes. The old
200 KB cap cut those mid-data-URI, producing exactly the truncated artifacts the gallery now
refuses to publish — so the cap was manufacturing the defect it was meant to bound.

But per-artifact and per-bundle are different questions, and the layers have to agree or the
failure is worse than truncation: a bundle the edge rejects loses the WHOLE run.

Run: python3 mvp/test_artifact_size_budget.py
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_size_budget_test.db")

from pod import arena_gen            # noqa: E402

# aeon/ingest.py is MOTHERSHIP-ONLY and is deliberately absent from the public pod repo, where this
# file also runs. The cross-side agreement checks need it; the pod-side budget checks do not — so
# run what can be run rather than failing the whole file on a component that is not supposed to
# be here.
try:
    from aeon import ingest          # noqa: E402
except ImportError:
    ingest = None

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


MB = 1024 * 1024


def art(mb, name="a"):
    """An artifact of roughly `mb` megabytes that is structurally COMPLETE."""
    body = "x" * int(mb * MB)
    return {"ok": True, "kind": "game", "prompt_id": name,
            "html": "<html><body>" + body + "</body></html>"}


print("the layers agree (a mismatch loses whole runs, not just artifacts)")
check("per-artifact cap fits inside the bundle budget",
      arena_gen.MAX_HTML_BYTES <= arena_gen.MAX_BUNDLE_ARTIFACT_BYTES)
check("an asset-bearing artifact (10 MB) is under the per-artifact cap",
      10 * MB < arena_gen.MAX_HTML_BYTES)
if ingest is None:
    print("  skip  mothership-side agreement (aeon/ingest.py is not in the pod repo)")
else:
    check("pod per-artifact cap == mothership per-artifact cap",
          arena_gen.MAX_HTML_BYTES == ingest.MAX_ARTIFACT_HTML)
    check("mothership bundle cap >= pod bundle budget (pod can never build a rejected bundle)",
          ingest.MAX_BUNDLE_BYTES >= arena_gen.MAX_BUNDLE_ARTIFACT_BYTES)

print("per-artifact capping no longer mangles realistic content")
big = art(10)["html"]
check("a 10 MB artifact passes through _cap_html untouched",
      arena_gen._cap_html(big) == big)
check("and stays complete afterwards", arena_gen.is_complete(arena_gen._cap_html(big)) is True)
if ingest is not None:
    check("ingest does not truncate it either", ingest._cap_html(big) == big)
    check("ingest still sees it as complete", ingest._html_complete(ingest._cap_html(big)) is True)

print("fit_bundle trims to the bundle budget, in order, and reports what it dropped")
small = [art(1, "p%d" % i) for i in range(4)]
kept, dropped = arena_gen.fit_bundle(small)
check("a small set is kept whole", len(kept) == 4 and dropped == [])

many = [art(20, "p%d" % i) for i in range(6)]          # 120 MB against a 64 MB budget
kept, dropped = arena_gen.fit_bundle(many)
total = sum(len((a["html"]).encode("utf-8")) for a in kept)
check("an oversized set is trimmed", len(kept) < 6 and len(dropped) > 0)
check("kept bytes are within the budget", total <= arena_gen.MAX_BUNDLE_ARTIFACT_BYTES)
check("nothing is lost — kept + dropped accounts for every artifact",
      len(kept) + len(dropped) == 6)
check("corpus order preserved (coverage degrades evenly, not by kind)",
      [a["prompt_id"] for a in kept] == ["p%d" % i for i in range(len(kept))])

huge = [art(70, "solo")]                                # one artifact bigger than the whole budget
kept, dropped = arena_gen.fit_bundle(huge)
check("a single over-budget artifact is still kept (never submit an empty gallery)",
      len(kept) == 1 and dropped == [])

kept, dropped = arena_gen.fit_bundle([])
check("empty in, empty out", kept == [] and dropped == [])
kept, dropped = arena_gen.fit_bundle(None)
check("None is tolerated", kept == [] and dropped == [])

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all artifact size-budget tests passed")
