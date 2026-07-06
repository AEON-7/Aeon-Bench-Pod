# Pod Quickstart

Benchmark a model on your own hardware and submit signed results to the live
[aeon-bench.com](https://aeon-bench.com) leaderboard. **One copy-paste command** — the
prebuilt, multi-platform dashboard container does everything else from the browser.

**Prerequisites:** Docker (with the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)
on NVIDIA rigs). No python, no clone.

```bash
docker run -d --name aeon-pod --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  ghcr.io/aeon-7/aeon-pod:latest
# open http://localhost:8091 → Run tab
```

*(macOS: swap `--network host` for `-p 8091:8091` — Apple MLX serves bare-metal on the host
and the dashboard benches it at `host.docker.internal`.)*

From the **Run tab**: paste an HF link — or hit **⌕ scan system** (finds every model already on
disk: HF cache, LM Studio library, AEON pulls — size, location, format, each auto-reconciled to
its HF card so the hash check runs automatically) or **▤ browse** the pod host's folders. A
hash-matched local copy is good as gold: **no re-download**; a manually-typed HF link always
overrides the auto-reconciliation. The **VALIDATED MODEL** light goes green, you pick the
**engine** for your hardware (aeon-vllm-ultimate / vLLM / SGLang / llama.cpp / vLLM-ROCm /
custom image — or bare-metal **Apple MLX** and **LM Studio**, the Windows host-performance
path), optionally open **⚙ RECIPE TUNING** (every common startup flag, annotated; 64K context
floor enforced; a **DFlash drafter** slot that hash-validates the drafter's HF card like the
model), and launch. The pod **serves** the validated weights, **benchmarks** — driving the
agentic suite through **Hermes / OpenClaw / OpenCode** (versions disclosed) — and **submits**
the ed25519-signed bundle: **attested**, with the inference engine, bench hardware, and exact
startup recipe (docker or bare-metal, reported identically) on every result.

> On a DGX Spark (GB10) the pod defaults to the first-party `aeon-vllm-ultimate` engine with
> its optimal flags — the same engine behind AEON's own boards.

## Update to the latest version

Also the fix for `name "aeon-pod" already in use`:

```bash
docker pull ghcr.io/aeon-7/aeon-pod:latest && docker rm -f aeon-pod
docker run -d --name aeon-pod --network host   -v /var/run/docker.sock:/var/run/docker.sock   -v aeon-pod-state:/root/.aeon   -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models"   ghcr.io/aeon-7/aeon-pod:latest
```

Your device key, runs and models persist in the named volumes — updating never loses them.
Port taken on your host? Add `-e AEON_PORT=8092` (and open :8092).

## Full pipeline via compose (build from source)

The one-shot A→B pipeline (pull → verify → serve → bench → submit) as a compose stack:

```bash
git clone https://github.com/AEON-7/Aeon-Bench-Pod.git && cd Aeon-Bench-Pod
AEON_HF_LINK=org/Your-Model  docker compose -f deploy/pod/docker-compose.yml up --build
```

## Overriding defaults

You almost never need a `.env`. If you want to pin an engine image, set a judge, or point at a
non-prod mothership, copy the template and edit only what you need:

```bash
cp deploy/pod/.env.example deploy/pod/.env      # then edit; every var is documented + optional
```

Full walkthrough → [`docs/run-a-benchmark.md`](run-a-benchmark.md) · harnesses + A→B flow →
[`deploy/pod/AGENTS.md`](../deploy/pod/AGENTS.md) · every var → [`deploy/pod/.env.example`](../deploy/pod/.env.example).
