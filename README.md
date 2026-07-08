<p align="center">
  <a href="https://aeon-bench.com"><img src="docs/images/hero.svg" alt="AEON Bench — open, attested benchmarks for local LLMs" width="880"></a>
</p>

# AEON Bench Pod

The **open benchmark pod** for [AEON Bench](https://aeon-bench.com): run the
full AEON suite against a model **on your own hardware**, with a controlled, verifiable pipeline —

```
pull (HuggingFace) → verify weights (LFS sha256 + manifest) → serve (recorded recipe)
→ benchmark (text · agentic ×3 harnesses · vision · audio · arena · perf)
→ sign (ed25519 device key) → submit (attested)
```

Results submitted through the controlled flow are **attested** and eligible for the global
leaderboard. Direct-endpoint runs are stored as *self-reported* — useful locally, never globally ranked.

## Quickstart — one command, prebuilt container

Pull the maintained multi-platform image (x86 / ARM / DGX Spark / Apple-silicon Docker Desktop)
and open the dashboard — everything happens from the GUI:

```bash
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Then open **http://localhost:8091 → Run tab**.

> `--gpus all` needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/) and matters more than it looks: without GPU access the pod detects a CPU-only box — CUDA engines (aeon-vllm-ultimate / vLLM / SGLang) disable themselves and the recipe-tuning catalog shrinks. On a Mac or CPU-only host, drop the flag.

### Apple silicon (MLX) quickstart

macOS uses `-p` instead of `--network host`, and no `--gpus` flag:

```bash
docker run -d --name aeon-pod -p 8091:8091 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

The pod detects the Apple-silicon host automatically and **recommends MLX** — macOS can't run
MLX inside a container, so the Run tab hands you the exact bare-metal command
(`pip install mlx-lm` once, then the generated `mlx_lm.server …` line), waits for the endpoint,
benches it, and records that startup recipe **exactly like a docker recipe** — the result is
just as attested and replicable. **LM Studio** works the same way (pick it as the engine; the
pod generates the `lms server start` / `lms load` lines). GGUF models can also run fully
containerized via **llama.cpp** (CPU inside the VM — fine for correctness, slow for perf).

From the **Run tab**: paste an HF link — or hit **⌕ scan system** (every model already on disk:
HF cache, LM Studio library, sizes + locations, each auto-reconciled to its HF card) or
**▤ browse** to pick a folder. A hash-matched local copy is good as gold: **no re-download**.
Watch the **VALIDATED MODEL** light go green, pick the engine for your hardware —
**aeon-vllm-ultimate** (AEON's own optimal engine, the one behind the official boards),
**vLLM**, **SGLang**, **llama.cpp**, **vLLM ROCm**, a **custom image**, or the bare-metal pair:
**Apple MLX** (macOS) and **LM Studio** (Windows/macOS/Linux host performance) — bare startup
recipes are recorded exactly like docker recipes. **⚙ Recipe tuning** exposes every common
startup flag as an annotated control (64K context floor enforced), a **DFlash drafter** slot
(the drafter's HF card is hash-validated like the model) and freeform extras — then launch.
The pod validates, serves, benchmarks, signs, submits: **attested**, replicable, on the global
board, with the inference engine + hardware + full startup recipe shown on every result.

The mounts, in one line each: the **docker socket** lets the pod launch engine + harness
containers; **aeon-pod-state** persists your ed25519 device key + local runs; **/models** (with
`AEON_MODELS_HOST_DIR` naming its host path) is where validated weights live so sibling engine
containers can mount them; **/host-home** is a read-only view of your home directory so
**scan system** can find HF cache, LM Studio, `~/models`, and other existing local model folders.

## Update an existing install

Pull the latest image, drop the old container, run the new one — this is also the fix for
`name "aeon-pod" already in use`:

```bash
docker pull ghcr.io/aeon-7/aeon-pod:latest && docker rm -f aeon-pod
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Your device key, runs and models live in the named volumes — **updating never loses them**.
If :8091 is taken on your host (e.g. a bare-metal dashboard is already running), add
`-e AEON_PORT=8092` and open :8092 instead.

### Full-host model scan

The quickstart includes a **read-only** mount of your home at `/host-home`:

```bash
-v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME"
```

That makes **⌕ scan system** sweep the whole machine — Hugging Face cache, LM Studio library,
`~/models`, and `~/aeon-models` — not just the pod's own `/models` mount. Found models validate
and serve directly from where they live: the pod translates `/host-home/...` back to the real
host path and mounts it read-only into the serve engine. Nothing is copied or moved, and the
pod can never write to the host-home mount.

> ⚠ `docker run` flags do **not** persist across an update — if your old container had extra
> `-e` flags, add them to the new run line too. Common ones: `-e AEON_SYSTEM=<hardware-label>`
> (names your hardware on results) and `-e AEON_PAUSE_CONTAINERS=<name>` (auto-stops a
> production inference container that holds the GPU/port during a bench, auto-restarts it
> after — without it a bench can collide with your resident server and refuse to run).
> Check what the old container had with: `docker inspect aeon-pod --format '{{json .Config.Env}}'`
> **before** removing it.

## Start · stop · logs

```bash
docker stop aeon-pod          # stop the dashboard (state persists in the volumes)
docker start aeon-pod         # start it again
docker restart aeon-pod       # reload (e.g. after changing env on a recreated container)
docker logs -f aeon-pod       # follow the dashboard + job logs live
docker ps --filter name=aeon-pod   # is it running?
```

The dashboard is stateless between restarts — everything durable (device key, run history,
saved tokens, pulled models) lives in `aeon-pod-state` and your models folder. A benchmark
interrupted by a stop doesn't lose what it already submitted (results stream to the mothership
in checkpoints); relaunch it from the Run tab — validated local weights are reused, no
re-download.

<details><summary>Alternative: docker compose (build from source)</summary>

```bash
git clone https://github.com/AEON-7/Aeon-Bench-Pod.git && cd Aeon-Bench-Pod

# infrastructure up (no model needed): dashboard on :8091 + harness images —
# then run benchmarks from the GUI / API
docker compose -f deploy/pod/docker-compose.yml up -d --build

# OR the headless one-shot pipeline (pull → verify → serve → bench → submit, then exit):
AEON_HF_LINK=org/Your-Model  docker compose --profile pipeline -f deploy/pod/docker-compose.yml up --build
```
(Copy `deploy/pod/.env.example` to `.env` only to override a default or add an `HF_TOKEN`.)
</details>

Docs: [`docs/pod-quickstart.md`](docs/pod-quickstart.md) ·
[`docs/run-a-benchmark.md`](docs/run-a-benchmark.md) ·
[`docs/attestation.md`](docs/attestation.md) · [`deploy/pod/AGENTS.md`](deploy/pod/AGENTS.md)

## What a full attested run measures

| Dimension | Suite | How |
|---|---|---|
| Text (5 categories × 4 difficulty tiers) | `aeon-suite-v2` | deterministic Tier-0 + binary-rubric Tier-1 |
| Agentic | `aeon-agentic-v2` | 16 environment-execution tasks (file ops + app/game/animation codegen) through **three real harnesses** (Hermes / OpenClaw / OpenCode) in fresh containers, scored on observable file outcomes |
| Vision | `aeon-mvp-vision` | probe-gated image suite |
| Audio | `aeon-audio-v1` | probe-gated, deterministic synthetic stimuli |
| Generative arena | apps / games / animations | seeded prompts, artifacts ship with the signed bundle |
| Performance | `aeon-perf-v1` | direct + through-harness grid, c=1…32, aggregate tok/s + TTFT |

Every run carries its **serve recipe** (exact docker command, engine version, flags), **verified
weights hash** (`repo@revision`), and **detected hardware** — so anyone can reproduce it.

## Trust model (short version)

The pod holds an ed25519 **device key** (`~/.aeon/device_key.pem`). Submissions are signed bundles
over the full result set; the mothership verifies signature + weight verification metadata and
tiers the run (`attested` / `self_reported`). See [`docs/attestation.md`](docs/attestation.md).

---
Open source under the [MIT License](LICENSE). This repo ships the **pod distribution**:
everything needed to run, verify and submit benchmarks. The mothership's server-side
trust internals (evaluator accounts, moderation, submission acceptance) are deliberately
not part of it.
