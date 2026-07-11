# Run a Benchmark

This is the guide behind the mothership's **"Run A Benchmark"** button. You run the benchmark on
**your own hardware** — the AEON Bench mothership **never runs benchmarks itself**; the **pod** (the
appliance you download here) does. The mothership receives, verifies-what-it-can, and displays your
signed results.

Three steps: **Execute a test → View your results → Submit results.** (Submit is automatic.)

> TL;DR / copy-paste version: [`docs/pod-quickstart.md`](pod-quickstart.md).

---

## 1. Execute a test

The pod pulls and **hash-verifies** the model weights from Hugging Face, serves the model in the
engine you pick, drives the AEON suite through the three agent harnesses, and submits the signed
result.

### 1.0 The dashboard way (recommended) — prebuilt container, everything from the GUI

```bash
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Then open **http://localhost:8091 → Run tab**. (macOS: `-p 8091:8091` instead of `--network host`.)

> `--gpus all` needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/) and matters more than it looks: without GPU access the pod detects a CPU-only box — CUDA engines (aeon-vllm-ultimate / vLLM / SGLang) disable themselves and the recipe-tuning catalog shrinks. On a Mac or CPU-only host, drop the flag.

From the **Run tab**:

1. **Pick the model** — paste an HF link, **⌕ scan system** (every model already on disk: HF
   cache, LM Studio library, AEON pulls — each auto-reconciled to its HF card), or **▤ browse**.
   A hash-matched local copy is good as gold — **no re-download**. Wait for the green
   **VALIDATED MODEL** light.
2. **Apply a template (optional, recommended)** — under the validation strip:
   **★ CHAMPION RECIPES** offers the mothership's winning recipe per model on hardware like
   yours (best demonstrated peak tok/s that also scored well) — **apply template →** fills
   engine + Recipe Tuning + spec decode with the exact winning recipe; and when validation
   recognizes the model family, the **★ best-practice preset** chip fills Recipe Tuning with the
   family ⊕ hardware flags (honest high/medium/low confidence tag). Both only *fill* the
   controls — everything stays editable. Skip both and the pod still auto-applies the
   conservative family ⊕ hardware defaults at launch.
3. **Pick the engine** — **aeon-vllm-ultimate** (AEON's own boards run this), **vLLM**,
   **SGLang**, **llama.cpp** (GGUF), **vLLM-ROCm** (AMD), a **custom image**, or bare-metal
   **Apple MLX** / **LM Studio** (startup recipe recorded exactly like a docker recipe).
4. **Tune the recipe (optional)** — **⚙ RECIPE TUNING** exposes every common startup flag as an
   annotated card — what it does, the upside, the risk — with **live conflict warnings** when a
   flag clashes with this model/engine/platform (amber strip, never a hard-disable; the 64K
   context floor is enforced — Hermes rejects less). Plus a **DFlash drafter** slot (paste the
   drafter's HF card: validated like the model, mounted at `/drafter`, preset `n` configs —
   spec decode is lossless, speed only) and freeform extra flags. The final recipe travels with
   the result and is downloadable as `serve.sh` / `compose.yml`. Modalities (vision / audio /
   video) are auto-detected from the HF config and probed at bench time; the Run tab's modality
   toggles override that detection — go by what the tab shows.
5. **Launch.** Validate → serve → benchmark → sign → submit **attested**. If a launch fails, the
   job card shows a plain-language **▸ fix** hint plus **⚠ check these toggles** chips that jump
   to the implicated Recipe Tuning cards. A stopped or crashed bench is **interrupted, not
   failed** — its scored cases are intact and **⟲ RESUME** continues from the last scored case.

### 1.1 Alternative: the compose pipeline (build from source)

```bash
git clone https://github.com/AEON-7/Aeon-Bench-Pod.git
cd Aeon-Bench-Pod
```

(Or download the repo zip from the GitHub page and `cd` into it.)

### 1.2 Configure — usually nothing

The **only required input is the model**. Everything else auto-defaults: submissions go to the
live mothership `https://aeon-bench.com`, the engine is auto-detected from `nvidia-smi`, ports are
fixed, and the pod's ed25519 device key is generated on first enrol. So you can skip `.env`
entirely and pass the model inline (see 1.3).

| Var | What it is |
|---|---|
| `AEON_HF_LINK` | **(required)** the model to benchmark — a Hugging Face repo id or URL (`org/model`, a full `huggingface.co/...` URL, `org/model@<rev>`, or `.../tree/<rev>`). Selects the served model **and** travels with your submission for model-identity verification. |
| `AEON_MOTHERSHIP` | *(optional)* mothership base URL — defaults to `https://aeon-bench.com`; set only to submit to a private/test mothership. |

To override any default, copy the template and edit only what you need:

```bash
cp deploy/pod/.env.example deploy/pod/.env
```

Useful optional vars (full list documented in `deploy/pod/.env.example`):

- `AEON_SYSTEM=dgx-spark` — on an NVIDIA DGX Spark, makes the pod default to the first-party
  `aeon-vllm-ultimate` engine.
- `AEON_ENGINE` — pin the engine (`aeon-vllm-ultimate` | `vllm` | `vllm-rocm` | `sglang` |
  `llama.cpp` | `mlx` | `lmstudio`); the dashboard's engine dropdown sets this for you.
- `AEON_HARDWARE` — a label recorded with the run, e.g. `"NVIDIA DGX Spark GB10 128GB"`.
- `AEON_JUDGE` / `AEON_JUDGE_URL` / `AEON_JUDGE_KEY` — a **frontier** judge for subjective Tier-1
  cases. Leave empty for deterministic-only scoring. **Never** the model under test judging itself.
- `AEON_MAX_TOKENS` (default 2048) — generation cap; reasoning models need headroom.
- `AEON_LIMIT` — benchmark only the first N cases for a quick smoke before a full run.
- `HF_TOKEN` — only for gated/private HF repos.

### 1.3 Run

Pass the model inline — no `.env` needed:

```bash
# infrastructure only (no model — then bench from the GUI/API):
docker compose -f deploy/pod/docker-compose.yml up -d --build

# OR the headless one-shot pipeline:
AEON_HF_LINK=org/Your-Model  docker compose --profile pipeline -f deploy/pod/docker-compose.yml up --build
```

(If you created a `deploy/pod/.env`, the inline var is optional.)
Watch it live at **http://localhost:8091**.

What happens (the controlled A→B flow — see [`deploy/pod/AGENTS.md`](../deploy/pod/AGENTS.md)):

1. **Pull → verify.** The pod resolves the HF link, downloads the snapshot (each file hash-checked
   by `huggingface_hub`), then **hash-verifies** every weight file into a content-addressed
   `weights_hash` and compares it to Hugging Face's published per-file LFS sha256. That match is the
   *signature* that the bytes on disk are exactly `repo@commit` as hosted on HF. On a mismatch the
   pod refuses to serve.
2. **Serve.** The verified weights are served on the fixed alias **`model-under-test`** (vLLM by
   default; `aeon-vllm-ultimate` on a DGX Spark; `llama.cpp` for GGUF). The serving recipe is
   recorded with the run.
3. **Benchmark through each harness.** The suite runs against the served alias into a **local**
   SQLite dashboard (`~/.aeon/pod.db` — never the mothership). The agentic suite is driven through
   **Hermes, OpenClaw, and OpenCode**, each pinned to a disclosed version.
4. **Submit.** The signed results bundle goes to your mothership (see step 3 below).

The pod is a one-shot job: it benchmarks, submits, and exits. Your enrolled signing key and local
dashboard persist in the `pod-state` volume.

---

## 2. View your results

Open your mothership's board (the same host you set as `AEON_MOTHERSHIP`). Your run appears grouped
by the model's **canonical identity** (resolved from the verified HF `repo@commit` + `weights_hash`),
so re-runs and quantizations line up under one model rather than fragmenting.

What you'll see:

- **Per-model aggregates** — **mean / best / worst** across your runs of that model.
- **Per-category quality + speed** — quality and speed broken out by suite category
  (Math, Instruction, Reasoning, Coding, Prose, Agentic, …).
- **The model×harness AI Harness Bench** — the agentic categories pivoted by harness, showing the
  per-harness delta (e.g. how the same model does under Hermes vs OpenClaw vs OpenCode), with each
  harness's **disclosed release version**.
- **Disclosure facets** — the **trust tier** badge (below), the **engine** (advisory),
  **hardware**, the **harness versions**, and the **judge** used. These are searchable/filterable,
  not hidden.

> **Only comprehensive passes rank.** The global leaderboard shows the comprehensive suite only:
> fast-bench seeded draws are compare-by-seed views, tier-scoped runs (e.g. hard-only) get their
> own boards, and a run must score **at least 90% of the suite** to stand — a partial or
> text-only pass is stored and viewable but never ranked. Run **Comprehensive** and run it to
> completion.

### Trust tiers (be honest about what a number means)

The board badges every run by how its truth was established, and **only one tier is ranked on the
global leaderboard**.

| Tier | How it's earned | On the board |
|---|---|---|
| **`self_reported`** (the board labels this **`local`**) | A run against a **direct endpoint** (`--target`) — any model, any server. Signed by your enrolled key (tamper-evident) but the model identity is **not** bit-for-bit verified. | Stored, shown, and badged **`local`**; **never globally ranked**. Run these all you like. |
| **`attested`** (the board labels this **`✓ verified`**) | The **controlled HF-pull flow** (`--hf-link`): pulled fresh from Hugging Face → **every weight file hash-verified bit-for-bit against HF's published hashes** at a pinned commit → served by a recorded recipe under our alias → suite run through the harnesses → the whole bundle ed25519-signed. The mothership then **independently re-fetches HF and re-checks every weight hash** before it counts. | The **only globally-ranked tier**. |

`attested` cryptographically pins **model identity** (the served weights *are* `repo@commit` as hosted
on HF), the **serving recipe**, and **authorship** — and the mothership re-verifies it, so a forged
bundle with the wrong hashes is rejected. What it does **not** yet prove on its own is that your
hardware actually produced the reported numbers (an operator runs the bench): closing *that*
execution-integrity gap is a future hardware-TEE sub-level. Full detail:
[`docs/attestation.md`](attestation.md).

---

## 3. Submit results

**Submission is automatic** — the pod does it at the end of the run over the enrolled, signed
channel (`mvp/pod/aeon_submit.py`), and results **stream to the mothership in checkpoints during
the run**, so a mid-run kill loses nothing already sent. You don't run a separate step. Here's
what travels and why it's safe:

1. **Enroll** — on first run the pod generates a local **ed25519 device key** (`~/.aeon/device_key.pem`,
   chmod 600) and proves possession of it to the mothership. The **private key never leaves the pod**;
   the mothership only ever sees the public key, signatures, and the results bundle.
2. **Open a run** — the mothership mints a **single-use nonce + run-scoped token** (the request is
   signed). This makes a bundle valid for exactly one run — no replay, no resubmission.
3. **Submit the bundle** — the ed25519-signed results bundle is sent. It contains the per-case
   results, the `suite_hash`, the **environment** (hardware + engine profile), `target_class =
   "local_weights"`, the **HF repo** (for model-identity verification), and the **judge** used. The
   mothership validates the schema server-side, treats the bundle as **inert data (never executed)**,
   and stores it.

### Interrupted, incomplete, or offline — nothing is lost

- **Completeness gate:** an incomplete bench is **not auto-submitted** — it stays local and
  resumable. Use **⟲ RESUME** on the job card to finish the remaining cases first (`--force-submit`
  is the CLI-only escape hatch; a forced partial will never rank anyway).
- **Mothership down at submit time?** The results persist on the pod (surviving restarts) and the
  job card grows a **⬆ SUBMIT TO MOTHERSHIP** button — one click commits them later.
- **Idempotent by construction:** every bundle carries a pod-minted `job_sig` (a hash of launch
  time + model + hardware + suite). Re-submitting a job the mothership already has answers
  `"job already submitted and available on the Mothership"` (`ok: true, duplicate: true`) — that's
  success, not an error; the same job can never land on the board twice.

### Which tier your run earns

It depends entirely on **how you ran it**:

- **`--hf-link` (controlled flow) → `attested` → globally ranked.** The bundle carries the pinned HF
  `repo@commit`, the **per-file weight hashes**, the `weights_hash`, the **serving recipe**, and the
  pod **build hash** (`target_class = "hf_pull_controlled"`). The mothership independently re-fetches
  HF and re-checks every weight hash; only on a full match does the run become `attested` and appear
  on the global board. A bundle with mismatched hashes is stored but **rejected from the ranking**.
- **`--target` (direct endpoint) → `self_reported` → local only.** Useful for trying any model on any
  server; signed and shown, but **never globally ranked** (the model identity isn't verified).

Either way the mothership treats the bundle as **inert data (never executed)**, validates it
server-side, and the numbers are stored exactly as your pod produced them. The one thing even
`attested` doesn't prove by itself — that your hardware produced the numbers — is the future
hardware-TEE sub-level.

---

## See also

- [`docs/pod-quickstart.md`](pod-quickstart.md) — the 3-command version.
- [`deploy/pod/AGENTS.md`](../deploy/pod/AGENTS.md) — the harnesses + the A→B flow in detail.
- [`deploy/pod/.env.example`](../deploy/pod/.env.example) — every configuration variable.
- [`docs/attestation.md`](attestation.md) — the full trust-chain spec.
