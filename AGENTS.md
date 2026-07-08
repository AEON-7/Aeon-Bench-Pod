# AGENTS.md — set up an AEON Bench Pod

> Read this first. It gets the pod **installed and running** on a host. Once it's up, follow
> [`SKILL.md`](SKILL.md) to actually deploy + operate benchmarks. Harness internals live in
> [`deploy/pod/AGENTS.md`](deploy/pod/AGENTS.md); the judging methodology lives in the
> mothership's `AGENTS.md` (you don't need it to run a pod — the pod judges deterministically).

## What this is

The **pod** is the appliance you run on your own hardware to benchmark a model through a
controlled, verifiable pipeline and submit an attested result:

```
pull (HuggingFace) → verify weights (LFS sha256 + manifest) → serve (recorded recipe)
→ benchmark (text · agentic ×3 harnesses · vision · audio · arena · perf)
→ sign (ed25519 device key) → submit → mothership (aeon-bench.com)
```

Attested runs are eligible for the global leaderboard. Pointing at an endpoint you already
serve gives a **self-reported** run (useful locally, never globally ranked).

## Prerequisites

- **Docker** + the **NVIDIA Container Toolkit** (for GPU serving). `docker run --rm --gpus all …`
  must work. The harness images are built on first run, so BuildKit should be enabled (default).
- A **GPU** to serve the model under test — *or* an existing OpenAI-compatible endpoint to point at.
- Outbound HTTPS to `huggingface.co` (pull weights) and to your mothership (submit).
- Optional native path: **Python 3.11+** (`pip install -r mvp/requirements.txt`).
- **~2× the model's disk size** free for the verified weight snapshot + HF cache.

## Fastest path — the prebuilt dashboard container (one command)

```bash
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Then open **http://localhost:8091 → Run tab**. (macOS: `-p 8091:8091` instead of `--network host`.)

> `--gpus all` needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/) and matters more than it looks: without GPU access the pod detects a CPU-only box — CUDA engines (aeon-vllm-ultimate / vLLM / SGLang) disable themselves and the recipe-tuning catalog shrinks. On a Mac or CPU-only host, drop the flag.

Everything happens from the **Run tab**:

- **Model** — paste an HF link, hit **⌕ scan system** (finds every model already on disk — HF
  cache, LM Studio library, AEON pulls — auto-reconciled to its HF card), or **▤ browse**. A
  hash-matched local copy is good as gold: **no re-download**. The **VALIDATED MODEL** light
  goes green when the identity is proven.
- **Engine** — pick the serve container for your hardware: **aeon-vllm-ultimate** (the engine
  behind AEON's own boards), **vLLM**, **SGLang**, **llama.cpp** (GGUF), **vLLM-ROCm** (AMD), a
  **custom image** — or the bare-metal pair, **Apple MLX** (macOS) and **LM Studio**
  (Windows/host performance); bare startup recipes are recorded exactly like docker recipes.
- **⚙ Recipe tuning** — every common startup flag as an annotated control (context floor 64K
  enforced — Hermes rejects less; GB10 gpu-util 0.70 OOM-safe; etc.), a **DFlash drafter** slot
  (paste the drafter's HF card → validated like the model, mounted at `/drafter`, preset n
  configs) and freeform extra flags. The final recipe is recorded, shown on the result, and
  downloadable — every tuned run is a data point in the optimal-recipe search.
- Launch → hash-validate → serve → benchmark (fresh agentic-harness container per task:
  Hermes / OpenClaw / OpenCode) → **ed25519-sign → submit attested**.

## Alternative — docker compose (build from source)

```bash
# infrastructure up (no model needed): dashboard :8091 + harness images; bench from the GUI/API
docker compose -f deploy/pod/docker-compose.yml up -d --build

# OR the headless one-shot pipeline (the only mode that needs a model):
AEON_HF_LINK=org/Your-Model  docker compose --profile pipeline -f deploy/pod/docker-compose.yml up --build
```

The `pull` step resolves the HF link, downloads + **hash-verifies** the weights; the engine
serves them on the fixed alias `model-under-test`; the pod benchmarks that alias and submits
the signed bundle. First `up` builds the three harness images; later runs reuse them.

> **DGX Spark / GB10:** set `AEON_SYSTEM=dgx-spark` and use the first-party engine image
> `ghcr.io/aeon-7/aeon-vllm-ultimate:latest` (DFlash speculative decoding). See its startup
> guide at `github.com/AEON-7/vllm-ultimate-dgx-spark`. **Serve ≥64K context** (`--max-model-len
> 65536`) — the Hermes harness rejects models reporting a <64K window (the pod enforces this
> floor on tuned recipes automatically).

## Config surface (`.env` / environment)

| var | meaning |
|---|---|
| `AEON_HF_LINK` | HF repo id or URL of the model under test (verified flow) |
| `AEON_MOTHERSHIP` | mothership base URL to submit to (default `https://aeon-bench.com`) |
| `AEON_HARDWARE` | label recorded with the run, e.g. `"NVIDIA DGX Spark GB10 128GB"` (else auto-detected) |
| `AEON_SYSTEM` | `dgx-spark` to force the `aeon-vllm-ultimate` recipe |
| `AEON_JUDGE` / `_URL` / `_KEY` | optional frontier judge for Tier-1 (else deterministic-only; never self-judges silently) |
| `HF_TOKEN` | gated/private HF repos |
| `AEON_MAX_TOKENS` | generation cap (reasoning models need headroom; default 2048) |
| `AEON_LIMIT` | first-N cases only (quick smoke) |
| `AEON_PAUSE_CONTAINERS` | comma list of containers the pod docker-stops during a bench serve and ALWAYS restarts after (e.g. `aeon-vllm` on a DGX that runs a production serve on :8000 — frees the port AND the unified memory) |

## State that persists (don't delete)

- **`~/.aeon/device_key.pem`** — the pod's ed25519 identity, enrolled with the mothership once.
  Deleting it re-enrolls as a new device. (Mounted as the `pod-state` volume in compose.)
- `~/.aeon/pod.db` — your local run history (the dashboard reads it live).

## Verify the install

```bash
cd mvp
python -m pod.aeon_pod --help          # the runner CLI
# smoke: 4 cases against any OpenAI-compatible endpoint (self-reported, not ranked)
python -m pod.aeon_pod --target http://127.0.0.1:8000/v1 --model <served-name> \
  --mothership "$AEON_MOTHERSHIP" --limit 4
```

Next: [`SKILL.md`](SKILL.md) — deploy + operate a full benchmark.
