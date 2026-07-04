"""run_harness — run the AEON agentic suite through a vanilla agent harness.

`run_agentic_suite(harness, model_base_url, served_alias, pin=None, cases=None)` drives every
`aeon.agentic.AGENTIC_CASES` task through the harness's adapter (pod.adapters), scores each
transcript with `aeon.agentic.score_agentic`, and returns per-task results tagged with the
DISCLOSED harness identity (`harness`, `harness_version`, repo, name) so a result always says
which harness BUILD produced it (model x harness must be apples-to-apples).

The harnesses run on the GPU host pointed at the pod's served model alias. This module is the
portable driver; the adapters contain the harness-specific glue. Use `harness="mock"` to exercise
the whole pipeline (run -> score -> disclose) with no harness installed.

CLI:
    python -m pod.run_harness --harness mock --target http://dgx:8000/v1 --model model-under-test
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_MVP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../mvp
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from aeon import agentic                       # noqa: E402
from pod import adapters, harnesses            # noqa: E402


def run_agentic_suite(harness: str, model_base_url: str, served_alias: str,
                      pin: str | None = None, cases=None, *, timeout: int = 300,
                      progress_cb=None) -> list:
    """Run every agentic task through `harness` and score it.

    Returns a list of per-task result dicts:
        {case_id, category, tier, prompt, optimal_steps,
         score, task_success, metrics, transcript, n_steps, status,
         harness, harness_name, harness_repo, harness_version}

    A per-task adapter failure is captured as status="harness_error" (score 0, success False)
    so one broken task never aborts the whole suite — the disclosure fields are still attached.
    """
    adapter = adapters.get(harness)
    cases = cases if cases is not None else agentic.AGENTIC_CASES
    disclosure = adapter.disclose(pin) if hasattr(adapter, "disclose") else harnesses.disclose(harness, pin)

    results = []
    for case in cases:
        cid = case.get("id")
        try:
            transcript = adapter.run_task(case, model_base_url, served_alias, timeout=timeout)
            score, metrics = agentic.score_agentic(case, transcript)
            status = "scored"
            err = None
        except Exception as e:                  # adapter/harness failure for THIS task only
            transcript = {"steps": [], "answer": ""}
            score, metrics = 0.0, {"tier": case.get("tier", 1), "agentic": True,
                                   "task_success": False, "error": f"{type(e).__name__}: {e}"[:200]}
            status = "harness_error"
            err = str(e)

        row = {
            "case_id": cid,
            "category": case.get("category", "Agentic"),
            "tier": case.get("tier", 1),
            "prompt": case.get("prompt"),
            "optimal_steps": case.get("optimal_steps"),
            "suite_id": agentic.SUITE_ID,
            "status": status,
            "score": score,
            "task_success": bool(metrics.get("task_success")),
            "n_steps": metrics.get("n_steps", len(transcript.get("steps", []))),
            "metrics": metrics,
            "transcript": transcript,
            **disclosure,                       # harness, harness_name, harness_repo, harness_version
        }
        if err:
            row["error"] = err[:300]
        results.append(row)
        if progress_cb:
            progress_cb(cid, score, status)
    return results


def _summarize(results: list) -> dict:
    scored = [r["score"] for r in results if isinstance(r.get("score"), float)]
    n_success = sum(1 for r in results if r.get("task_success"))
    return {
        "n_cases": len(results),
        "n_task_success": n_success,
        "mean_score": round(sum(scored) / len(scored), 3) if scored else 0.0,
        "harness": results[0].get("harness") if results else None,
        "harness_version": results[0].get("harness_version") if results else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Run the AEON agentic suite through an agent harness.")
    ap.add_argument("--harness", required=True, choices=sorted(adapters.ADAPTERS),
                    help="hermes | openclaw | opencode | mock")
    ap.add_argument("--target", default="http://127.0.0.1:8000/v1",
                    help="OpenAI-compatible base URL of the served model under test")
    ap.add_argument("--model", default="model-under-test",
                    help="served model alias the harness connects to")
    ap.add_argument("--pin", default=None, help="explicit harness version/tag to disclose")
    ap.add_argument("--timeout", type=int, default=300, help="per-task timeout (seconds)")
    ap.add_argument("--json", action="store_true", help="print full per-task results as JSON")
    a = ap.parse_args()

    def cb(cid, score, status):
        s = f"{score:.3f}" if isinstance(score, float) else str(score)
        print(f"  {cid:24s} {status:14s} {s}", file=sys.stderr)

    results = run_agentic_suite(a.harness, a.target, a.model, pin=a.pin,
                                timeout=a.timeout, progress_cb=cb)
    summary = _summarize(results)
    print(json.dumps({"summary": summary,
                      "results": results if a.json else "(use --json for full results)"},
                     indent=2, default=str))
    # success exit if every task scored (no harness errors).
    raise SystemExit(0 if all(r["status"] == "scored" for r in results) else 1)


if __name__ == "__main__":
    main()
