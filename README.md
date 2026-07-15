<p align="center">
  <a href="https://aeon-bench.com"><img src="docs/images/hero.svg" alt="AEON Bench — open, attested benchmarks for local LLMs" width="880"></a>
</p>

# AEON Bench Pod

**Find out how good any AI model *really* is — on your own computer — and prove it.**

AI companies love to post impressive scores for their models. AEON Bench lets you check those
claims yourself: point it at any open model, and it runs a full, fair exam — quality, real
speed, coding-agent skill, even vision, audio, and video — right on your own hardware. Then it
**cryptographically signs the result** so the number can't be faked, and (if you want) posts it
to the public leaderboard at **[aeon-bench.com](https://aeon-bench.com)**.

Think of it as a **certified dyno for AI models**. Anyone can re-run your exact test and get your
exact result — that's what makes the scoreboard trustworthy instead of marketing.

> ### 🤖 Don't want to set it up yourself?
> **Point your AI agent (Claude, etc.) at the [`AGENTS.md`](AGENTS.md) file in this repo and say
> "deploy AEON Bench and benchmark this model for me."** It will install the pod wherever you
> want it, configure the optimal settings for your hardware, run the whole benchmark, and hand
> you back the results with a live link to watch. That file is written specifically so an AI
> agent can do the entire job start to finish. **This is the easy button.**

📖 **New here? Take the [illustrated walkthrough](docs/walkthrough/README.md)** — every feature,
with screenshots. It's the friendliest way to see what AEON Bench does before you install anything.

---

## What you get

- **The truth about a model's speed and quality**, measured on *your* hardware, not a lab's.
- **A score you can trust** — every run is weight-verified against Hugging Face and signed, so
  nobody can post a fake number.
- **A public leaderboard spot** — validated runs rank at [aeon-bench.com](https://aeon-bench.com)
  next to everyone else's, with the exact recipe shown so anyone can reproduce it.
- **One-click best settings** — the board already knows the fastest proven recipe for your exact
  hardware and hands it to you to apply, then tweak.

Under the hood, a "validated" run is a controlled pipeline:

```
pull (Hugging Face) → verify weights (LFS sha256 + manifest) → serve (recorded recipe)
→ benchmark (text · agentic ×3 harnesses · vision · audio · video · arena · perf)
→ sign (ed25519 device key) → submit (attested)
```

Runs through this flow are **attested** and eligible for the global leaderboard. Direct-endpoint
runs are stored as *self-reported* — handy for private testing, but never globally ranked.

---

## Quickstart — one command

Pull the maintained multi-platform image (x86 / ARM / DGX Spark / Apple-silicon Docker Desktop)
and open the dashboard. **Everything else happens by clicking in the GUI — no commands after this.**

```bash
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Then open **http://localhost:8091 → Run tab**. Paste a Hugging Face link (or pick a model already
on your disk), click the ★ champion recipe for your hardware, and hit launch. That's it.

> **A big or slow model can take several hours to benchmark fully — that's normal and expected.**
> A thorough exam runs the whole suite (text, three coding-agent harnesses, vision/audio/video,
> and a full performance sweep). You can close the tab and come back; it keeps going and you can
> watch progress live. **Only a complete run should be submitted as validated** — a quick "smoke
> test" is for your eyes only.

> `--gpus all` needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/)
> and matters more than it looks: without GPU access the pod thinks it's on a CPU-only box —
> the fast engines (aeon-vllm-ultimate / vLLM / SGLang) turn themselves off, the recipe options
> shrink, and your hardware gets mislabeled. On a Mac or CPU-only host, drop the flag (see below).

### Apple silicon (Mac) quickstart

macOS uses `-p` instead of `--network host`, and **no** `--gpus` flag:

```bash
docker run -d --name aeon-pod -p 8091:8091 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

The pod detects the Apple-silicon host and **recommends MLX** — macOS can't run MLX inside a
container, so the Run tab hands you the exact bare-metal command (`pip install mlx-lm` once, then
the generated `mlx_lm.server …` line), waits for the endpoint, benches it, and records that
startup recipe **exactly like a docker recipe** — the result is just as attested and replicable.
**LM Studio** works the same way (pick it as the engine). GGUF models can also run fully
containerized via **llama.cpp** (CPU inside the VM — fine for correctness, slow for perf).

### What each mount does (one line each)

- **docker socket** — lets the pod start the model server + coding-agent test containers for you.
- **aeon-pod-state** — remembers your signing key + past runs (survives updates).
- **/models** (with `AEON_MODELS_HOST_DIR`) — where verified model weights live so the server can use them.
- **/host-home** (read-only) — lets **⌕ scan system** find models you already have (HF cache, LM Studio, `~/models`) without copying anything.

---

## Using it (the 30-second version)

From the **Run tab**:

1. **Point at a model** — paste an HF link *(e.g. `org/model`)*, or hit **⌕ scan system** to pick
   one already on disk. Either way the pod hash-verifies the weights against Hugging Face before
   running, so the result is honest. Watch the **VALIDATED MODEL** light go green.
2. **Pick a recipe** — click the **★ champion recipe** for your hardware (proven fastest), or let
   the family preset auto-fill. Every setting is a plain-English card with pros/cons and warns you
   before a bad combo crashes the run.
3. **Launch.** The pod serves the model, runs the full exam, signs the results, and submits.
   Validated · replicable · on the global board — with engine, hardware, and full recipe shown.

**Want the guided tour with pictures?** → [**docs/walkthrough/README.md**](docs/walkthrough/README.md)
**Are you an AI agent?** → [**AGENTS.md**](AGENTS.md) has the complete deploy-and-run playbook.

---

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
If :8091 is taken, add `-e AEON_PORT=8092` and open :8092 instead.

> ⚠ `docker run` flags do **not** persist across an update — if your old container had extra
> `-e` flags, add them to the new run line too. Common ones: `-e AEON_SYSTEM=<hardware-label>`
> (names your hardware on results) and `-e AEON_PAUSE_CONTAINERS=<name>` (auto-stops a
> production inference container that holds the GPU/port during a bench, auto-restarts it after).
> Check what the old container had with `docker inspect aeon-pod --format '{{json .Config.Env}}'`
> **before** removing it.

## Start · stop · logs

```bash
docker stop aeon-pod          # stop the dashboard (state persists in the volumes)
docker start aeon-pod         # start it again
docker restart aeon-pod       # reload (e.g. after changing env on a recreated container)
docker logs -f aeon-pod       # follow the dashboard + job logs live
docker ps --filter name=aeon-pod   # is it running?
```

A benchmark interrupted by a stop doesn't lose what it already submitted (results stream to the
mothership in checkpoints); relaunch it from the Run tab and it **resumes from where it left off** —
validated local weights are reused, no re-download.

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

Docs: [**illustrated walkthrough**](docs/walkthrough/README.md) ·
[**AGENTS.md** (for AI agents)](AGENTS.md) ·
[`docs/pod-quickstart.md`](docs/pod-quickstart.md) ·
[`docs/run-a-benchmark.md`](docs/run-a-benchmark.md) ·
[`docs/attestation.md`](docs/attestation.md)

---

## What a full validated run measures

| Dimension | Suite | How |
|---|---|---|
| Text (5 categories × 6 difficulty tiers) | `aeon-suite-v3` | deterministic Tier-0 + binary-rubric Tier-1, 155 cases |
| Agentic | `aeon-agentic-v2` | environment-execution tasks (file ops + app/game/animation codegen) through **three real coding-agent harnesses** (Hermes / OpenClaw / OpenCode) in fresh containers, scored on observable file outcomes |
| Vision | `aeon-vision-v2` | probe-gated image suite (31 cases, deterministic) |
| Audio | `aeon-audio-v2` | probe-gated, deterministic synthetic stimuli (21 cases) |
| Video | `aeon-video-v1` | probe-gated deterministic video suite (keyword/element-graded) |
| Generative arena | apps / games / animations | seeded prompts, playable artifacts ship with the signed bundle |
| Performance | `aeon-perf-v1` | direct + through-harness grid, aggregate tok/s + TTFT across a concurrency ladder |

Every run carries its **serve recipe** (exact docker command, engine version, flags), **verified
weights hash** (`repo@revision`), and **detected hardware** — so anyone can reproduce it.

## Why you can trust the numbers (short version)

Every pod holds a private **device key**. When it submits a result, it signs the whole bundle,
and the mothership independently re-checks the model's weight hashes against Hugging Face before
accepting it. Only runs that pass — **attested** — rank on the public board. A "self-reported"
number (from a raw API endpoint the pod can't weight-verify) is stored for your own use but never
ranks. That single rule is what keeps the leaderboard honest. Details:
[`docs/attestation.md`](docs/attestation.md).

---
Open source under the [MIT License](LICENSE). This repo ships the **pod distribution**:
everything needed to run, verify and submit benchmarks. The mothership's server-side trust
internals (evaluator accounts, moderation, submission acceptance) are deliberately not part of it.
