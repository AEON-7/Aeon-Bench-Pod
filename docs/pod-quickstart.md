# Pod Quickstart

Benchmark a model on your own GPU host and submit signed results to a mothership. Three commands.

**Prerequisites:** Docker + Docker Compose, an NVIDIA GPU with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/),
and (for gated HF repos) an `HF_TOKEN`.

```bash
# 1. Get the pod
git clone https://github.com/<org>/aeon-bench.git && cd aeon-bench/deploy/pod

# 2. Configure: set AEON_HF_LINK (the model) and AEON_MOTHERSHIP (where to submit)
cp .env.example .env && ${EDITOR:-nano} .env

# 3. Run — pulls + hash-verifies weights, serves the model, benchmarks through each
#    harness, then auto-submits the ed25519-signed results bundle.
docker compose up --build
```

That's it. The pod:

- **pulls + hash-verifies** the HF weights (`weights_hash` vs HF's published LFS sha256),
- **serves** them on the fixed alias `model-under-test` (vLLM; `aeon-vllm-ultimate` on a DGX Spark — set `AEON_SYSTEM=dgx-spark`),
- **benchmarks** the model, driving the agentic suite through **Hermes / OpenClaw / OpenCode** (versions disclosed),
- **submits** the signed bundle to your mothership (stored `self_reported`), then **exits**.

View results on your mothership's board — grouped by canonical model identity, with per-category
quality/speed, the model×harness bench, and the trust-tier + harness-version disclosure.

**Minimal `.env`:**

```ini
AEON_HF_LINK=org/Your-Model-Name
AEON_MOTHERSHIP=https://aeon-bench.com
# AEON_SYSTEM=dgx-spark          # on a DGX Spark
# AEON_LIMIT=10                  # quick smoke (first 10 cases) before a full run
```

Full walkthrough → [`docs/run-a-benchmark.md`](run-a-benchmark.md) · harnesses + A→B flow →
[`deploy/pod/AGENTS.md`](../deploy/pod/AGENTS.md) · every var → [`deploy/pod/.env.example`](../deploy/pod/.env.example).
