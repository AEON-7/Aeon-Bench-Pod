"""Run the VISION board against a model.

    python run_vision.py mock-vision mock                         # offline, no GPU
    AEON_API_KEY=<tok> python run_vision.py qwen/qwen3-vl-8b http://127.0.0.1:1234/v1
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aeon import runner, scoring  # noqa: E402


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "mock-vision"
    target = sys.argv[2] if len(sys.argv) > 2 else "mock"
    api_key = os.environ.get("AEON_API_KEY") or None
    rid = uuid.uuid4().hex[:10]
    print(f"vision run {rid}: model={model} target={target}")

    def cb(cid, score, status):
        s = f"{score:.2f}" if isinstance(score, float) else str(score)
        print(f"  {cid:26s} {status:16s} {s}", flush=True)

    pr = runner.run_vision_benchmark(rid, model, target, progress_cb=cb, api_key=api_key)
    print("probe:", {k: pr.get(k) for k in ("vision_ok", "multi_image_ok", "ocr_ok")})
    if not pr.get("vision_ok"):
        print("-> capability_absent: model excluded from the vision board (text board untouched).")
        return
    print("VISION LEADERBOARD:")
    for m in scoring.vision_leaderboard()["models"]:
        print(f"  {m['composite']:6.1f}  {m['model']:28s} cov={m['coverage']:>5} "
              f"ttft={m['avg_ttft_after_image_ms']}  {m['categories']}")


if __name__ == "__main__":
    main()
