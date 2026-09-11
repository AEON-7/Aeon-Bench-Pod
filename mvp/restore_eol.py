"""Restore a file's original line endings after a content-only edit.

    python3 mvp/restore_eol.py <repo-root> <path/relative/to/repo>

WHY. `mvp/web/app.js` is mixed CRLF/LF on purpose — ~5.8k CRLF lines and ~350 bare-LF ones — and
carries two deliberate NUL bytes. This is true in the private repo AND in the public pod repo. Any
edit that goes through a text layer flattens every line to CRLF, so a 130-line change lands as ~350
lines of churn here and ~12,000 in the pod repo. The content is right; only the endings drifted —
but the diff becomes unreviewable and the public history misstates what changed.

Run this after editing such a file, and again in the pod repo after syncing. It compares against
`git show HEAD:<path>`, aligns old and new by CONTENT with difflib, transplants the old ending onto
every surviving line, and leaves genuinely new lines alone. It works in BYTES throughout, so the NUL
bytes survive, and it asserts both invariants before writing.

To detect the problem in the first place: a large gap between `git diff --numstat` and
`git diff --ignore-cr-at-eol --numstat` is churn, not work.
"""
import difflib
import subprocess
import sys

repo, rel = sys.argv[1], sys.argv[2]
old = subprocess.run(["git", "show", "HEAD:" + rel], cwd=repo, capture_output=True, check=True).stdout
path = repo + "/" + rel
cur = open(path, "rb").read()


def split(b):
    out, start = [], 0
    for i, ch in enumerate(b):
        if ch == 0x0A:
            line = b[start:i + 1]
            out.append((line[:-2], b"\r\n") if line.endswith(b"\r\n") else (line[:-1], b"\n"))
            start = i + 1
    if start < len(b):
        out.append((b[start:], b""))
    return out


o, c = split(old), split(cur)
out, restored = [], 0
sm = difflib.SequenceMatcher(None, [x[0] for x in o], [x[0] for x in c], autojunk=False)
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
        for k in range(i2 - i1):
            end = o[i1 + k][1] if c[j1 + k][1] else c[j1 + k][1]
            if end != c[j1 + k][1]:
                restored += 1
            out.append(c[j1 + k][0] + end)
    else:
        for k in range(j1, j2):
            out.append(c[k][0] + c[k][1])

blob = b"".join(out)
assert blob.count(b"\x00") == cur.count(b"\x00"), "NUL bytes must survive"
assert blob.replace(b"\r\n", b"\n") == cur.replace(b"\r\n", b"\n"), "content must be identical"
open(path, "wb").write(blob)
print("%s: restored %d endings (CRLF=%d bare-LF=%d)"
      % (rel, restored, blob.count(b"\r\n"), blob.count(b"\n") - blob.count(b"\r\n")))
