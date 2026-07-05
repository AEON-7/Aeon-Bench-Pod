# Pod Quickstart

Benchmark a model on your own GPU host and submit signed results to the live
[aeon-bench.com](https://aeon-bench.com) leaderboard. **Two copy-paste commands.**

**Prerequisites:** Docker + Docker Compose, an NVIDIA GPU with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/),
and (only for gated HF repos) an `HF_TOKEN`.

```bash
# 1. Get the pod
git clone https://github.com/AEON-7/Aeon-Bench-Pod.git && cd Aeon-Bench-Pod

# 2. Run it — the ONE input is the model. Everything else auto-defaults:
#    mothership → https://aeon-bench.com, engine auto-detected (nvidia-smi), ports,
#    and the pod's ed25519 device key is generated on first enrol. No file to edit.
AEON_HF_LINK=org/Your-Model  docker compose -f deploy/pod/docker-compose.yml up --build
```

That's it. The pod **pulls + hash-verifies** the HF weights (`weights_hash` vs HF's
published LFS sha256), **serves** them on the fixed alias `model-under-test`, **benchmarks**
the model — driving the agentic suite through **Hermes / OpenClaw / OpenCode** (versions
disclosed) — and **submits** the ed25519-signed bundle to aeon-bench.com. Watch it live at
**http://localhost:8080** (your local pod console), where you can also launch further runs and
manage HF tokens / API keys — no `.env` edits needed.

> On a DGX Spark (GB10) add `AEON_SYSTEM=dgx-spark` to use the first-party
> `aeon-vllm-ultimate` engine. For a gated/private HF repo add `HF_TOKEN=hf_…`.

## Just the console (no benchmark yet)

To bring up only the local dashboard — browse the site, point at a model you're already
serving, and launch runs from the browser:

```bash
docker compose -f deploy/pod/docker-compose.yml up -d pod-dashboard   # → http://localhost:8080
```

## Overriding defaults

You almost never need a `.env`. If you want to pin an engine image, set a judge, or point at a
non-prod mothership, copy the template and edit only what you need:

```bash
cp deploy/pod/.env.example deploy/pod/.env      # then edit; every var is documented + optional
```

Full walkthrough → [`docs/run-a-benchmark.md`](run-a-benchmark.md) · harnesses + A→B flow →
[`deploy/pod/AGENTS.md`](../deploy/pod/AGENTS.md) · every var → [`deploy/pod/.env.example`](../deploy/pod/.env.example).
