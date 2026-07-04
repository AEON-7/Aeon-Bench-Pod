---
name: deploy-aeon-pod
description: >
  Use when asked to deploy, stand up, or operate an AEON Bench pod — run a controlled
  benchmark of an LLM on your own hardware (pull → verify weights → serve → benchmark
  through text + 3 agentic harnesses + vision/audio/arena/perf → sign → submit attested),
  or point at an existing endpoint for a quick self-reported run, then read the results.
  This is the OPERATE-the-system skill; the separate judge skill covers evaluating outputs.
---

# Skill: Deploy & operate an AEON benchmark pod

You install the pod (once) and then **run benchmarks with it**. Setup lives in
[`AGENTS.md`](AGENTS.md); harness internals in [`deploy/pod/AGENTS.md`](deploy/pod/AGENTS.md).
This skill is the operating procedure.

## Mental model

- **The pod is the control plane ("A")**, the served model is the subject ("B"). The pod never
  scores itself — it benchmarks the served alias `model-under-test` and submits a signed bundle.
- **Harnesses are ephemeral.** For the agentic suite the pod spawns a **fresh container per
  task** (Hermes / OpenClaw / OpenCode) via the Docker socket and tears it down — because agents
  accumulate state and would "learn" across tasks. You don't start them yourself; the pod does.
- **Trust tier is earned, not claimed.** A run pulled+verified from HuggingFace through this flow
  is `attested` (globally rankable). Pointing at an endpoint you already serve is `self_reported`.

## 1 — Deploy

**One-shot (recommended):** set `.env` (`AEON_HF_LINK`, `AEON_MOTHERSHIP`), then
`docker compose -f deploy/pod/docker-compose.yml up --build`. This runs the whole
pull→verify→serve→bench→submit flow and exits; the dashboard (`pod-dashboard`) stays up on `:8080`.

**Native/interactive:** `cd mvp && AEON_ROLE=pod python serve.py` → open `:8080` → **Run** tab.

## 2 — Run a benchmark (the CLI: `python -m pod.aeon_pod`)

Pick the mode that matches what you have:

| Goal | Command shape |
|---|---|
| **Attested** (pull+verify+serve+submit, globally rankable) | `--hf-link org/Model --mothership $M --harness all` |
| **Attested, sidecar-served** (weights pulled+verified separately, engine already up) | `--modelref /weights/.aeon-modelref.json --target $URL --mothership $M --harness all` |
| **Local self-reported** (endpoint you already serve) | `--target $URL --model <served-name> --mothership $M` |
| **Quick smoke** (first N cases) | add `--limit 8` |
| **Hard tier only** (grouped on its own board) | add `--difficulty hard` (or `easy,medium,hard,expert`) |
| **True A/B** (identical questions across models) | add `--fast --seed <shared-seed> --per-cell 5` |
| **Just the agentic harnesses** (skip text/vision/audio/perf) | add `--harness all --harness-only` |

Other useful flags: `--arena N` (games/apps/animations per kind, default 2; `0` disables),
`--no-vision` / `--no-audio` (default on, probe-gated), `--perf` (concurrency-ladder perf grid),
`--concurrency N`, `--max-tokens` / `--retry-max-tokens` (reasoning-model headroom),
`--judge <frontier-id>` (else deterministic-only), `--hardware "<label>"`.

A full attested run measures, in order: text suite → arena generation → the 3 harnesses →
vision → audio → perf; each dimension submits its own bundle carrying the verified `weights_hash`,
`repo@revision`, serve recipe, and detected hardware.

## 3 — Operating rules that bite

- **Serve ≥64K context for the agentic suite.** The Hermes harness refuses any model reporting a
  context window <64K (its tool-calling minimum) and every task fails `harness_error`. Serve with
  `--max-model-len 65536` (or higher) so it passes natively.
- **Unified-memory GPUs (DGX Spark/GB10) hard-hang on exhaustion.** `--gpu-memory-utilization` is
  a fraction of *total* unified memory shared with the OS + other processes — size it for
  co-residents (0.6–0.72), and never start a serve until the previous model's memory is released.
- **Weights that don't verify are refused.** The pull step exits non-zero if the on-disk bytes
  don't match HuggingFace's published LFS sha256 — by design; don't bypass it for an attested run.
- **First boot is slow** on the DGX engine (weight load + compile + autotune, ~10–15 min). Wait for
  `/v1/models` to list `model-under-test` before benchmarking; a silent boot is not a hang.
- **The device key persists** at `~/.aeon/device_key.pem`; keep it to stay the same enrolled device.

## 4 — Read the results

- **Locally:** the pod dashboard (`:8080`) — the **Live** view streams per-category progress + the
  prompt/answer feed while running; your run history + full per-case transparency afterward.
- **Globally:** once a verified run is accepted, it appears on the mothership leaderboard
  (`aeon-bench.com`) — text board (comprehensive vs hard grouped separately), the AI-Harness
  matrix (model × Hermes/OpenClaw/OpenCode with disclosed versions), Arena, Compare-by-seed, and
  full Submissions transparency (prompt, answer, score, judge rationale, signed manifest).

## Failure triage (exact string → fix)

- `harness hermes … harness_error 0.0` on every task → served context <64K; re-serve at ≥65536.
- `WEIGHTS VERIFICATION FAILED` → the HF snapshot didn't hash-match; re-pull, check the revision.
- submit `URLError` / timeout → mothership unreachable; check `AEON_MOTHERSHIP` + connectivity.
- engine `not ready within Ns` → boot slower than the wait cap; raise it or check `docker logs`.
- FlashInfer JIT / illegal-memory errors on Blackwell → set `--attention-backend TRITON_ATTN`
  (see the engine's startup guide).
