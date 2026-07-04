"""aeon-agentic-v2 — ENVIRONMENT-EXECUTION agentic suite.

Unlike aeon-agentic-v1 (synthetic tool specs, scored on the tool-call trace), v2 gives the
agent a real WORKDIR with setup files and scores the OBSERVABLE OUTCOME: the agent uses its
harness's OWN tools (read / write / shell) inside the workdir, and we deterministically check
the files it produced plus its final answer. This is uniform across every harness — no
per-harness tool translation, no trust in the trace.

Task schema (an entry of CASES):
    {"id": str,
     "category": "Agentic",
     "tier": 0,
     "prompt": str,                      # tells the agent the workdir is the current directory
                                         # and exactly what file(s) to write / answer to give
     "setup_files": {relpath: content},  # written into the workdir before the run
     "success": {
         "files": {relpath: {"contains": [needle, ...]} | {"equals": text}},
         "answer_contains": [needle, ...],
     },
     "timeout_s": int,
     "_expected": {"files": {relpath: content}, "answer": str}}   # private perfect-run oracle

Scoring — `score_agentic_v2(task, workdir, answer) -> (score, evidence)`:
  * every file `contains` needle is one criterion, every file `equals` is one criterion,
    every `answer_contains` needle is one criterion;
  * score = 1.0 iff ALL criteria pass, else the fraction of criteria that passed;
  * evidence is a list of {"criterion", "ok", "detail"} — one row per criterion.

Matching is deliberately forgiving on formatting, strict on content:
  * `contains`  — whitespace-stripped, lowercased substring match;
  * `equals`    — line-ending-normalised, per-line-rstripped, outer-stripped equality;
  * `answer_contains` — same normalisation as `contains`.

`apply_perfect(task, workdir)` scripts a PERFECT execution (writes the `_expected` files,
returns the `_expected` answer); `self_check()` asserts every task scores exactly 1.0 under
it — this runs in the local self-test so a task can never ship unsatisfiable.
"""
from __future__ import annotations

import os
import re

SUITE_ID = "aeon-agentic-v2"

_WS = re.compile(r"\s+")


def _norm(s) -> str:
    """Whitespace-stripped, lowercased canonical form for `contains` matching."""
    return _WS.sub("", str(s)).lower()


def _canon_text(s: str) -> str:
    """Line-ending-normalised, per-line-rstripped, outer-stripped form for `equals`."""
    s = str(s).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


def _read_file(workdir: str, relpath: str):
    """File content (utf-8, tolerant) or None if missing/unreadable."""
    path = os.path.join(workdir, relpath)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


# --------------------------------------------------------------------------------------------
# The tasks
# --------------------------------------------------------------------------------------------

_PREAMBLE = ("You are working in a sandbox: the CURRENT DIRECTORY is your workdir. "
             "Use your file/shell tools directly — do not ask questions, act autonomously. ")

_LOG = """\
2026-06-30 10:00:01 INFO boot sequence started
2026-06-30 10:00:02 ERROR failed to bind port 8080
2026-06-30 10:00:03 INFO retrying bind
2026-06-30 10:00:04 ERROR failed to bind port 8080
2026-06-30 10:00:05 WARN falling back to port 8081
2026-06-30 10:00:06 INFO listening on 8081
2026-06-30 10:00:07 ERROR upstream timeout gateway-1
2026-06-30 10:00:08 ERROR upstream timeout gateway-2
2026-06-30 10:00:09 INFO upstream recovered
2026-06-30 10:00:10 ERROR disk usage above 90%
2026-06-30 10:00:11 WARN cache eviction slow
2026-06-30 10:00:12 ERROR checksum mismatch on shard 3
2026-06-30 10:00:13 INFO shard 3 rebuilt
2026-06-30 10:00:14 ERROR worker crashed pid 4177
2026-06-30 10:00:15 INFO worker restarted
"""

_CSV = """\
name,score,active
alice,91,yes
bob,47,no
carol,88,yes
dave,59,yes
erin,95,no
"""

_FILTERED_CSV = """\
name,score,active
alice,91,yes
carol,88,yes
erin,95,no
"""

_BUGGY_PY = """\
def total(nums):
    s = 0
    for n in nums:
        s -= n
    return s

if __name__ == "__main__":
    print(total([3, 5, 7, 11]))
"""

_FIXED_PY = _BUGGY_PY.replace("s -= n", "s += n")

_FIB_PY = """\
a, b = 0, 1
out = []
for _ in range(8):
    out.append(str(a))
    a, b = b, a + b
print(",".join(out))
"""

CASES = [
    {
        "id": "av2-01-compute-write",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Compute 17 * 23 + 9. Write ONLY the resulting number to a file named result.txt "
        "in the current directory, then state the number in your final answer.",
        "setup_files": {},
        "success": {"files": {"result.txt": {"contains": ["400"]}},
                    "answer_contains": ["400"]},
        "timeout_s": 180,
        "_expected": {"files": {"result.txt": "400\n"}, "answer": "The result is 400."},
    },
    {
        "id": "av2-02-config-extract",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Read the file config.json in the current directory. Find the value of "
        "service.port and write ONLY that number to a file named port.txt, then state "
        "the port number in your final answer.",
        "setup_files": {
            "config.json": '{"service": {"name": "aeon-gateway", "port": 8443, '
                           '"debug": false}, "owner": "platform-team"}\n',
        },
        "success": {"files": {"port.txt": {"contains": ["8443"]}},
                    "answer_contains": ["8443"]},
        "timeout_s": 180,
        "_expected": {"files": {"port.txt": "8443\n"}, "answer": "The port is 8443."},
    },
    {
        "id": "av2-03-csv-filter",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Read data.csv in the current directory. Create filtered.csv containing the header "
        "row plus ONLY the data rows whose score column is greater than or equal to 80, in "
        "their original order and original column order (no extra columns, no extra rows). "
        "Reply DONE when filtered.csv is written.",
        "setup_files": {"data.csv": _CSV},
        "success": {"files": {"filtered.csv": {"equals": _FILTERED_CSV}},
                    "answer_contains": ["done"]},
        "timeout_s": 180,
        "_expected": {"files": {"filtered.csv": _FILTERED_CSV}, "answer": "DONE"},
    },
    {
        "id": "av2-04-log-count",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Count how many LINES in the file app.log contain the string ERROR. Write ONLY that "
        "number to a file named count.txt, then state the count in your final answer.",
        "setup_files": {"app.log": _LOG},
        "success": {"files": {"count.txt": {"equals": "7"}},
                    "answer_contains": ["7"]},
        "timeout_s": 180,
        "_expected": {"files": {"count.txt": "7\n"}, "answer": "There are 7 ERROR lines."},
    },
    {
        "id": "av2-05-script-and-output",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Write a Python script named fib.py in the current directory that prints the first 8 "
        "Fibonacci numbers starting from 0 and 1, on ONE line, separated by commas "
        "(i.e. it prints: 0,1,1,2,3,5,8,13). Run the script and save its exact output to a "
        "file named fib_out.txt. Then state the last number of the sequence in your final "
        "answer.",
        "setup_files": {},
        "success": {"files": {"fib.py": {"contains": ["print"]},
                              "fib_out.txt": {"contains": ["0,1,1,2,3,5,8,13"]}},
                    "answer_contains": ["13"]},
        "timeout_s": 240,
        "_expected": {"files": {"fib.py": _FIB_PY, "fib_out.txt": "0,1,1,2,3,5,8,13\n"},
                      "answer": "The last number is 13."},
    },
    {
        "id": "av2-06-bugfix-run",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "The script buggy.py in the current directory is supposed to print the SUM of the "
        "list [3, 5, 7, 11] (which is 26) but it has a one-line bug. Fix the bug by editing "
        "buggy.py in place, run the fixed script, and write its exact output to a file named "
        "out.txt. Then state the printed number in your final answer.",
        "setup_files": {"buggy.py": _BUGGY_PY},
        "success": {"files": {"buggy.py": {"contains": ["+"]},
                              "out.txt": {"equals": "26"}},
                    "answer_contains": ["26"]},
        "timeout_s": 240,
        "_expected": {"files": {"buggy.py": _FIXED_PY, "out.txt": "26\n"},
                      "answer": "The fixed script prints 26."},
    },
    {
        "id": "av2-07-json-spec",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        'Create a file named report.json in the current directory containing a single JSON '
        'object with exactly these keys and values: "status" set to the string "ok", '
        '"count" set to the number 3, and "items" set to the list ["a", "b", "c"]. '
        "Reply DONE when the file is written.",
        "setup_files": {},
        "success": {"files": {"report.json": {"contains": ['"status":"ok"', '"count":3',
                                                           '"items"', '"a"', '"b"', '"c"']}},
                    "answer_contains": ["done"]},
        "timeout_s": 180,
        "_expected": {"files": {"report.json":
                                '{"status": "ok", "count": 3, "items": ["a", "b", "c"]}\n'},
                      "answer": "DONE"},
    },
    {
        "id": "av2-08-multifile-summary",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "The current directory holds alpha.txt, beta.txt and gamma.txt, each containing a "
        "line like 'servers: N'. Read all three, add the three server counts together, and "
        "write a file named summary.txt containing exactly one line: 'total: <sum>'. Then "
        "state the total in your final answer.",
        "setup_files": {"alpha.txt": "servers: 4\n",
                        "beta.txt": "servers: 7\n",
                        "gamma.txt": "servers: 1\n"},
        "success": {"files": {"summary.txt": {"contains": ["total: 12"]}},
                    "answer_contains": ["12"]},
        "timeout_s": 180,
        "_expected": {"files": {"summary.txt": "total: 12\n"}, "answer": "The total is 12."},
    },
    {
        "id": "av2-09-rename",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Rename the file draft_notes.txt in the current directory to final_notes.txt, "
        "keeping its content unchanged. Reply DONE when the rename is complete.",
        "setup_files": {"draft_notes.txt": "quarterly sync notes v2\n"},
        "success": {"files": {"final_notes.txt": {"contains": ["quarterly sync notes v2"]}},
                    "answer_contains": ["done"]},
        "timeout_s": 180,
        "_expected": {"files": {"final_notes.txt": "quarterly sync notes v2\n"},
                      "answer": "DONE"},
    },
    {
        "id": "av2-10-reasoning-only",
        "category": "Agentic", "tier": 0,
        "prompt": _PREAMBLE +
        "Do NOT write any files for this task. A train departs at 09:20 and arrives at 11:05 "
        "the same morning. How many minutes does the journey take? Give the number of minutes "
        "in your final answer.",
        "setup_files": {},
        "success": {"files": {}, "answer_contains": ["105"]},
        "timeout_s": 120,
        "_expected": {"files": {}, "answer": "The journey takes 105 minutes."},
    },
]

CASE_IDS = [c["id"] for c in CASES]


# --------------------------------------------------------------------------------------------
# Workdir helpers
# --------------------------------------------------------------------------------------------

def populate_workdir(task: dict, workdir: str) -> None:
    """Write the task's `setup_files` into `workdir` (creating nested dirs as needed)."""
    for rel, content in (task.get("setup_files") or {}).items():
        path = os.path.join(workdir, rel)
        os.makedirs(os.path.dirname(path) or workdir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)


def apply_perfect(task: dict, workdir: str) -> str:
    """Script a PERFECT execution: write every `_expected` file into `workdir` and return
    the expected final answer. Used by the self-test oracle and the mock adapter."""
    exp = task.get("_expected") or {}
    for rel, content in (exp.get("files") or {}).items():
        path = os.path.join(workdir, rel)
        os.makedirs(os.path.dirname(path) or workdir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    return exp.get("answer", "")


# --------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------

def score_agentic_v2(task: dict, workdir: str, answer: str):
    """Score one task from the OBSERVABLE OUTCOME: the files now in `workdir` plus the
    agent's final `answer`. Returns `(score, evidence)`:

      score    — 1.0 iff every criterion passed, else passed/total (0..1);
      evidence — [{"criterion": str, "ok": bool, "detail": str}, ...], one per criterion.
    """
    spec = task.get("success") or {}
    evidence = []

    for rel, check in (spec.get("files") or {}).items():
        content = _read_file(workdir, rel)
        if not isinstance(check, dict):
            check = {"contains": [str(check)]}
        if "equals" in check:
            want = check["equals"]
            if content is None:
                ok, detail = False, "file missing"
            else:
                ok = _canon_text(content) == _canon_text(want)
                detail = "exact match" if ok else f"content mismatch (got {content[:80]!r})"
            evidence.append({"criterion": f"file {rel} equals {want[:40]!r}",
                             "ok": ok, "detail": detail})
        for needle in (check.get("contains") or []):
            if content is None:
                ok, detail = False, "file missing"
            else:
                ok = _norm(needle) in _norm(content)
                detail = "found" if ok else f"needle absent (file head: {content[:80]!r})"
            evidence.append({"criterion": f"file {rel} contains {needle!r}",
                             "ok": ok, "detail": detail})

    for needle in (spec.get("answer_contains") or []):
        ok = _norm(needle) in _norm(answer or "")
        evidence.append({"criterion": f"answer contains {needle!r}",
                         "ok": ok,
                         "detail": "found" if ok else f"absent (answer head: {(answer or '')[:80]!r})"})

    total = len(evidence)
    passed = sum(1 for e in evidence if e["ok"])
    score = (passed / total) if total else 0.0
    return (1.0 if passed == total and total else round(score, 4)), evidence


def self_check() -> None:
    """Every task's scripted perfect execution MUST score exactly 1.0 (and a bare setup-only
    workdir must NOT). Raises AssertionError with the offending task/evidence otherwise."""
    import tempfile
    for task in CASES:
        with tempfile.TemporaryDirectory(prefix="aeonv2_check_") as wd:
            populate_workdir(task, wd)
            answer = apply_perfect(task, wd)
            score, ev = score_agentic_v2(task, wd, answer)
            assert score == 1.0, f"{task['id']}: perfect run scored {score}: " \
                                 f"{[e for e in ev if not e['ok']]}"
        with tempfile.TemporaryDirectory(prefix="aeonv2_sab_") as wd:
            populate_workdir(task, wd)
            score, _ = score_agentic_v2(task, wd, "")
            assert score < 1.0, f"{task['id']}: sabotaged run scored {score} (spec too weak)"


__all__ = ["SUITE_ID", "CASES", "CASE_IDS", "populate_workdir", "apply_perfect",
           "score_agentic_v2", "self_check"]
