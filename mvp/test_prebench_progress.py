"""The pre-bench phases must report progress, not silence.

A big run spends its first ~15 minutes hashing a 27 GB weight set, loading + compiling the model,
and probing tool calling. None of that emitted a machine-readable marker, so the dashboard showed
an empty progress strip and the run looked wedged — which is how a healthy run gets killed.

The dashboard parses exactly one wire format ("[pod][stage] <name> <done>/<total>", see
pod/jobs.py), so these tests pin the format as much as the coverage.

Run: python3 mvp/test_prebench_progress.py
"""
import io
import os
import re
import sys
from contextlib import redirect_stdout

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("AEON_DB", "/tmp/aeon_prebench_progress_test.db")

from pod import aeon_pod, modelhost   # noqa: E402

fails = []


def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails.append(name)


STAGE_RE = re.compile(r"^\[pod\]\[stage\] (\S+) (\d+)/(\d+)$")


def stages(fn):
    """Run fn, return the [pod][stage] lines it emitted as (name, done, total)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn()
    out = []
    for ln in buf.getvalue().splitlines():
        m = STAGE_RE.match(ln.strip())
        if m:
            out.append((m.group(1), int(m.group(2)), int(m.group(3))))
    return out


print("wire format is exactly what pod/jobs.py parses")
got = stages(lambda: aeon_pod._stage("verify", 3, 14))
check("one marker emitted", len(got) == 1)
check("parses as (name, done, total)", got == [("verify", 3, 14)])
check("name carries no space (rsplit in jobs.py would mis-split it)",
      all(" " not in n for n, _, _ in got))

print("weight verification reports a real denominator")
tmp = os.path.join("/tmp", "aeon_verify_progress")
os.makedirs(tmp, exist_ok=True)
names = []
for i in range(3):
    p = os.path.join(tmp, "model-0000%d.safetensors" % i)
    with open(p, "wb") as f:
        f.write(b"weights-%d" % i)
    names.append(os.path.basename(p))

seen = []
ref = {"files": {n: None for n in names}, "sha": "deadbeef"}
res = modelhost.verify(tmp, ref, progress_cb=lambda d, t: seen.append((d, t)))
check("progress_cb fired once per weight file", seen == [(1, 3), (2, 3), (3, 3)])
check("verification still produced a weights_hash", bool(res.get("weights_hash")))
check("and still reports verified for a matching set", res.get("verified") is True)

print("a progress callback can never break a verification")
res2 = modelhost.verify(tmp, ref, progress_cb=lambda d, t: 1 / 0)
check("a raising progress_cb is swallowed", res2.get("verified") is True)
check("and the weights_hash is unchanged by it",
      res2.get("weights_hash") == res.get("weights_hash"))

print("verify() is still callable without progress (existing call sites)")
res3 = modelhost.verify(tmp, ref)
check("no-progress call still works", res3.get("weights_hash") == res.get("weights_hash"))

print("the throttle emits the edges (a 1-file set must still report)")
got = stages(lambda: [aeon_pod._stage_throttled("verify", i, 1) for i in (1,)])
check("a single-file verify still emits", got == [("verify", 1, 1)])
got = stages(lambda: [aeon_pod._stage_throttled("verify", i, 14) for i in range(1, 15)])
check("first and last are always emitted",
      got[0] == ("verify", 1, 14) and got[-1] == ("verify", 14, 14))

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all pre-bench progress tests passed")
