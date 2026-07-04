"""run_harness2 — run the aeon-agentic-v2 ENVIRONMENT-EXECUTION suite through a real harness.

For each task: make a fresh temp workdir, populate the task's setup files, let the harness
adapter launch ONE one-shot docker container with the workdir mounted at /work, then score the
OBSERVABLE OUTCOME (files written + final answer) with `aeon.agentic_v2.score_agentic_v2`.
Tasks run in parallel (ThreadPoolExecutor — each task is its own container, containers
parallelize fine). A per-task failure becomes status="harness_error" (score 0); the batch
never aborts.

    run_agentic_v2(harness_id, model_base_url, served_alias,
                   *, concurrency=4, timeout=240, progress_cb=None) -> [row, ...]

    row = {case_id, category, tier, status, score,
           raw_output,          # truncated transcript JSON {answer, steps, raw}
           evidence,            # per-criterion [{criterion, ok, detail}, ...]
           speed: {e2e_s},
           suite_id, harness, harness_version}

    discover(harness_id) -> {"harness": ..., "harness_version": ...}   (cached)

CLI:
    python -m pod.run_harness2 --harness mock --target http://127.0.0.1:8000/v1 --model m
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

_MVP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../mvp
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import agentic_v2                    # noqa: E402
from pod import adapters                       # noqa: E402

_VER_RE = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}|\d+\.\d+\.\d+(?:-[\w.]+)?")
_RAW_LIMIT = 8000                              # per-row transcript budget (chars)
_discover_cache: dict[str, dict] = {}


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------

def discover(harness_id: str) -> dict:
    """{harness, harness_version} for disclosure — queries the harness container image once
    (`docker run --rm <image> --version`, falling back to the image digest), then caches."""
    if harness_id in _discover_cache:
        return _discover_cache[harness_id]

    version = None
    if harness_id == "mock":
        version = "mock-0"
    else:
        try:
            cls = adapters.ADAPTERS[harness_id]
        except KeyError:
            raise KeyError(f"unknown harness {harness_id!r}; known: {sorted(adapters.ADAPTERS)}")
        image = getattr(cls, "IMAGE", None)
        if image and shutil.which("docker"):
            try:
                out = subprocess.run(["docker", "run", "--rm", image, "--version"],
                                     capture_output=True, text=True, timeout=60)
                m = _VER_RE.search((out.stdout or "") + " " + (out.stderr or ""))
                if m:
                    version = m.group(0)
            except Exception:
                pass
            if not version:
                try:                            # fall back to the local image id (still exact)
                    out = subprocess.run(["docker", "image", "inspect", image,
                                          "--format", "{{.Id}}"],
                                         capture_output=True, text=True, timeout=30)
                    iid = (out.stdout or "").strip()
                    if iid:
                        version = iid[:19]      # sha256:xxxxxxxxxxxx
                except Exception:
                    pass

    info = {"harness": harness_id, "harness_version": version}
    _discover_cache[harness_id] = info
    return info


# --------------------------------------------------------------------------------------------
# The batch runner
# --------------------------------------------------------------------------------------------

def _truncated_transcript(result: dict) -> str:
    """Compact JSON transcript for the row: answer + steps + a slice of the raw output."""
    doc = {
        "answer": str(result.get("answer", ""))[:2000],
        "steps": (result.get("steps") or [])[:50],
        "raw": str(result.get("raw", ""))[:4000],
    }
    return json.dumps(doc, default=str)[:_RAW_LIMIT]


def _run_one(adapter, case: dict, model_base_url: str, served_alias: str,
             scratch_root: str, default_timeout: int) -> dict:
    cid = case.get("id")
    workdir = tempfile.mkdtemp(prefix=f"task_{agentic_v2._norm(cid)[:24]}_", dir=scratch_root)
    row = {"case_id": cid,
           "category": case.get("category", "Agentic"),
           "tier": case.get("tier", 0),
           "suite_id": agentic_v2.SUITE_ID}
    try:
        agentic_v2.populate_workdir(case, workdir)
        timeout = int(case.get("timeout_s") or default_timeout)
        result = adapter.run_task(case, model_base_url, served_alias, workdir,
                                  timeout=timeout)
        score, evidence = agentic_v2.score_agentic_v2(case, workdir,
                                                      result.get("answer", ""))
        row.update(status="scored", score=score,
                   raw_output=_truncated_transcript(result),
                   evidence=evidence,
                   speed={"e2e_s": round(float(result.get("duration_s") or 0.0), 3)})
    except Exception as e:                       # NEVER aborts the batch
        row.update(status="harness_error", score=0.0,
                   raw_output=json.dumps({"error": f"{type(e).__name__}: {e}"[:1000]}),
                   evidence=[{"criterion": "harness ran the task", "ok": False,
                              "detail": f"{type(e).__name__}: {e}"[:400]}],
                   speed={"e2e_s": 0.0})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return row


def run_agentic_v2(harness_id: str, model_base_url: str, served_alias: str, *,
                   concurrency: int = 4, timeout: int = 240, progress_cb=None) -> list:
    """Run every aeon-agentic-v2 task through `harness_id`'s adapter and score the outcomes.

    Returns the per-task rows in CASES order. `progress_cb(case_id, score, status)` fires as
    each task completes. Per-task failures -> status="harness_error", score 0.
    """
    adapter = adapters.get(harness_id)
    info = discover(harness_id)

    run_root = tempfile.mkdtemp(prefix=f"aeonv2_{harness_id}_")
    scratch_root = os.path.join(run_root, "tasks")
    os.makedirs(scratch_root, exist_ok=True)
    try:
        adapter.prepare_run(model_base_url, served_alias, run_root)

        rows: list = [None] * len(agentic_v2.CASES)
        with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
            futs = {pool.submit(_run_one, adapter, case, model_base_url, served_alias,
                                scratch_root, timeout): i
                    for i, case in enumerate(agentic_v2.CASES)}
            for fut, i in futs.items():
                row = fut.result()                # _run_one never raises
                row.update(info)                  # harness, harness_version
                rows[i] = row
                if progress_cb:
                    progress_cb(row["case_id"], row["score"], row["status"])
        return rows
    finally:
        try:
            adapter.cleanup_run()
        except Exception:
            pass
        shutil.rmtree(run_root, ignore_errors=True)


def _summarize(rows: list) -> dict:
    scores = [r["score"] for r in rows]
    return {"suite_id": agentic_v2.SUITE_ID,
            "n_cases": len(rows),
            "n_perfect": sum(1 for r in rows if r["score"] == 1.0),
            "n_harness_error": sum(1 for r in rows if r["status"] == "harness_error"),
            "mean_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "harness": rows[0].get("harness") if rows else None,
            "harness_version": rows[0].get("harness_version") if rows else None}


def main():
    ap = argparse.ArgumentParser(
        description="Run the aeon-agentic-v2 environment-execution suite through a harness.")
    ap.add_argument("--harness", required=True, choices=sorted(adapters.ADAPTERS))
    ap.add_argument("--target", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--model", default="model-under-test")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--json", action="store_true", help="print full per-task rows as JSON")
    a = ap.parse_args()

    def cb(cid, score, status):
        print(f"  {cid:28s} {status:14s} {score:.3f}", file=sys.stderr)

    rows = run_agentic_v2(a.harness, a.target, a.model,
                          concurrency=a.concurrency, timeout=a.timeout, progress_cb=cb)
    print(json.dumps({"summary": _summarize(rows),
                      "results": rows if a.json else "(use --json for full rows)"},
                     indent=2, default=str))
    raise SystemExit(0 if all(r["status"] == "scored" for r in rows) else 1)


if __name__ == "__main__":
    main()
