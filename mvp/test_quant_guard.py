"""Quant-conflict guard + diagnostics — regression net for the 27B-Ultimate DSpark failure
(2026-07-17, job 3aad530ec038): a champion recipe carried `--quantization modelopt` from a
ModelOpt-NVFP4 donor onto an llm-compressor (compressed-tensors) NVFP4 checkpoint. vLLM
refuses that mismatch at startup — before spec-decode config is even parsed — and the hint
engine then mis-blamed the tool-call parser off vLLM's non-default-args config dump.

Covers: engines.quant_guard (drop-conflict / keep-match / no-declared-method / = form),
build_serve wiring (declared method serves, note recorded, 0.7 util default), the specific
quant-mismatch hint outranking everything on the real log, and the tool-call-parser signature
no longer matching config-dump lines."""
import os
import sys

_MVP = os.path.dirname(os.path.abspath(__file__))
if _MVP not in sys.path:
    sys.path.insert(0, _MVP)

from pod import diagnostics, engines  # noqa: E402

PASSED = 0


def check(cond, msg):
    global PASSED
    assert cond, "FAIL: " + msg
    PASSED += 1
    print("PASS:", msg)


# ---- 1) quant_guard unit behavior -----------------------------------------------------------

f, n = engines.quant_guard(["--quantization", "modelopt", "--max-num-seqs", "64"],
                           "compressed-tensors")
check("--quantization" not in f and "modelopt" not in f and f == ["--max-num-seqs", "64"],
      "conflicting two-token --quantization is dropped, other tuning survives")
check(n and "modelopt" in n and "compressed-tensors" in n,
      "guard note names both the recipe method and the declared method")

f, n = engines.quant_guard(["--quantization=modelopt"], "compressed-tensors")
check(f == [] and n, "= form is recognized and dropped too")

f, n = engines.quant_guard(["--quantization", "modelopt"], None)
check(f == ["--quantization", "modelopt"] and n is None,
      "checkpoint declares nothing -> operator flag stands (old-style ModelOpt needs it)")

f, n = engines.quant_guard(["--quantization", "compressed-tensors"], "compressed-tensors")
check(f == ["--quantization", "compressed-tensors"] and n is None,
      "matching operator flag is kept verbatim, no note")

f, n = engines.quant_guard(None, "compressed-tensors")
check(f == [] and n is None, "no extra flags -> clean no-op")

# ---- 2) build_serve wiring (docker vllm path) -----------------------------------------------

srv = engines.build_serve("aeon-vllm-ultimate", local_dir=".", alias="model-under-test",
                          port=8000, ctx=65536, quant="compressed-tensors",
                          extra_flags=["--quantization", "modelopt", "--max-num-seqs", "64"],
                          plat={"accel": "cuda", "docker": True})
cmd = srv["command"]
check(cmd[cmd.index("--quantization") + 1] == "compressed-tensors" and "modelopt" not in cmd,
      "launch argv serves the checkpoint's declared method — conflict never reaches vLLM")
check(bool(srv.get("quant_guard")) and "modelopt" in srv["quant_guard"],
      "guard note travels in the recipe (replicability: the override is visible)")
check(cmd[cmd.index("--gpu-memory-utilization") + 1] == "0.7",
      "docker vllm default stays at the 0.6-0.7 unified-memory policy")
check(cmd.index("--max-num-seqs") and cmd[cmd.index("--max-num-seqs") + 1] == "64",
      "unrelated recipe tuning still applies")

# ---- 3) diagnostics: the real failure log picks the right hint ------------------------------

LOG = [
    "[pod] launching engine: docker run --rm --entrypoint vllm "
    "ghcr.io/aeon-7/aeon-vllm-ultimate:latest serve /model --quantization modelopt "
    "--tool-call-parser qwen3_coder --speculative-config "
    '{"method":"dspark","model":"/drafter","num_speculative_tokens":8}',
    "(APIServer pid=1) INFO [api_utils.py:273] non-default args: {'model_tag': '/model', "
    "'enable_auto_tool_choice': True, 'tool_call_parser': 'qwen3_coder', 'host': '0.0.0.0'}",
    "(APIServer pid=1) pydantic_core._pydantic_core.ValidationError: 1 validation error for ModelConfig",
    "(APIServer pid=1)   Value error, Quantization method specified in the model config "
    "(compressed-tensors) does not match the quantization method specified in the "
    "`quantization` argument (modelopt). [type=value_error, input_value=ArgsKwargs(()",
]
h = diagnostics.diagnose(LOG, custom_flags=["--quantization modelopt"])
check(bool(h) and "CONTRADICTS" in h and "quantization" in h,
      "quant-mismatch hint wins on the verbatim 27B-Ultimate failure log")

h = diagnostics.diagnose([LOG[1]])
check(not (h and "tool-call-parser" in h),
      "vLLM's config-dump line alone no longer mis-blames the tool-call parser")

h = diagnostics.diagnose(["ValueError: invalid tool call parser: qwen9_coder (chose from hermes)"])
check(bool(h) and "parser" in h, "a genuine tool-call-parser error still hints correctly")

print(f"\nOK  quant guard + diagnostics: {PASSED} checks passed")
