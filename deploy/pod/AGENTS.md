# AEON Bench POD — Agent Harnesses & the Controlled A→B Flow

This is the `agents.md` for the AEON Bench pod: what the three agent harnesses are, how the pod
drives the model through each one, and how each harness's exact release version is disclosed in
the report. It also describes the pod's end-to-end controlled flow (pull → verify → serve →
bench-through-each-harness → submit).

> Why harnesses are part of the measurement: the agentic suite (`mvp/aeon/agentic.py`) scores a
> model by driving it through a real agent harness and grading the resulting tool-call transcript
> deterministically (task success, tool accuracy, arg validity, efficiency, no-forbidden-tools).
> A harness update can change scores, so **every agentic result records `(harness,
> harness_version)`** — exactly like engine and hardware. The model×harness comparison is only
> apples-to-apples if each harness is a **vanilla** deploy at a **pinned** version.

---

## The three harnesses

Defined in the registry at `mvp/pod/harnesses.py`. All three are OpenAI-compatible and are pointed
at the pod's served alias **`model-under-test`** (`modelhost.DEFAULT_ALIAS`) — never at a raw model
name, so the harness cannot tell which model it is driving.

| Key | Name | Repo | Deploy | Package / image | Version command |
|---|---|---|---|---|---|
| `hermes` | Hermes Agent | github.com/NousResearch/hermes-agent | docker (official image) | `<image>:<tag>` | `hermes --version` |
| `openclaw` | OpenClaw | github.com/openclaw/openclaw | npm | `openclaw@<pin>` | `openclaw --version` |
| `opencode` | OpenCode | github.com/anomalyco/opencode | npm | `opencode-ai@<pin>` (CLI `opencode`) | `opencode --version` |

### hermes — Hermes Agent
- **What it is:** NousResearch's agent harness, run from its **official docker image**
  (`harnesses.py`: `deploy="docker"`).
- **Driven by the pod:** the compose service `hermes` runs the image with
  `OPENAI_BASE_URL=http://model-under-test:8000/v1` (`harnesses.py`: `endpoint_env="OPENAI_BASE_URL"`,
  `supports_openai=True`). The pod issues each agentic task through it and records the transcript.
- **Version pin:** pin the image tag/digest via `AEON_HERMES_IMAGE` in `.env`.
  - `# TODO verify` the published image name + a concrete tag/digest (the registry only declares
    the GitHub repo).

### openclaw — OpenClaw
- **What it is:** an npm-distributed agent CLI (`harnesses.py`: `deploy="npm"`,
  `package="openclaw"`), wrapped here in a tiny image (`harness-openclaw.Dockerfile`) that does
  `npm i -g openclaw@<pin>`.
- **Driven by the pod:** pointed at the served alias via the OpenAI-compatible envs; config lives
  at `~/.openclaw/openclaw.json` (`harnesses.py`: `config_file`).
  - `# TODO verify` the exact flag/env OpenClaw uses to set the OpenAI base URL + model + key.
- **Version pin:** `AEON_OPENCLAW_VERSION` (build arg `OPENCLAW_VERSION`).

### opencode — OpenCode
- **What it is:** an npm-distributed agent CLI (`harnesses.py`: `deploy="npm"`,
  `package="opencode-ai"`, CLI binary `opencode`), wrapped in `harness-opencode.Dockerfile`.
- **Driven by the pod:** pointed at the served alias via the OpenAI-compatible envs.
  - `# TODO verify` OpenCode's exact endpoint flag/env.
- **Version pin:** `AEON_OPENCODE_VERSION` (build arg `OPENCODE_VERSION`).

---

## How a harness version is disclosed in the report

`harnesses.py` captures the exact build that produced each agentic result:

- `resolve_version(harness, pin)` — returns the **explicit pin** (release tag/digest) if you set
  one, else queries the installed CLI via its `version_cmd` (e.g. `openclaw --version`) and extracts
  a semver (`1.17.11`) or date-version (`2026.6.25`). If neither is available it returns `None`, and
  the report **flags the version as unknown rather than guessing**.
- `disclose(harness, pin)` — emits the `{harness, harness_name, harness_repo, harness_version}`
  record that travels **with** the benchmark.

Because version capture prefers an explicit pin, the values you set in `.env`
(`AEON_HERMES_IMAGE` tag/digest, `AEON_OPENCLAW_VERSION`, `AEON_OPENCODE_VERSION`) are exactly what
gets disclosed — pin them deliberately and they reproduce.

---

## The pod's controlled A→B flow

"A" is the portable control plane (`mvp/pod/`); "B" is the served model. The whole appliance is in
`docker-compose.yml`.

```
  1. PULL      modelhost.resolve(AEON_HF_LINK) → repo@rev
               modelhost.fetch_ref()  → HF's canonical commit sha + per-file LFS sha256 + card
               modelhost.pull()       → snapshot_download (huggingface_hub verifies each file)
  2. VERIFY    modelhost.verify()     → sha256 every weight file → a content-addressed
               `weights_hash`; compare to HF's published LFS sha256. This is the SIGNATURE that the
               bytes on disk ARE exactly repo@sha as hosted on HF. (Refuse to serve on mismatch.)
  3. SERVE     modelhost.derive_recipe() picks the engine and serves on the FIXED alias
               `model-under-test`:
                 • GGUF weights                              → llama.cpp
                 • DGX Spark + aeon-vllm-ultimate available  → aeon-vllm-ultimate  (DEFAULT)
                 • otherwise                                 → vanilla vLLM
               The recipe (engine, context_len, quant, command) is recorded WITH the benchmark.
  4. BENCH     aeon_pod.run_pod() runs the AEON suite against the served alias into a LOCAL SQLite
               dashboard (~/.aeon/pod.db — NEVER the mothership DB). The agentic suite is driven
               THROUGH each harness above; each agentic result records (harness, harness_version).
               Speed + deterministic scores always record; subjective Tier-1 cases score only if a
               FRONTIER --judge is configured (never the model judging itself).
  5. SUBMIT    aeon_submit.Pod.run_and_submit() →
                 enroll  (prove possession of the locally-generated ed25519 device key)
                 open    (server mints a single-use run nonce + run-scoped token; request signed)
                 submit  (ed25519-signed results bundle: results + suite_hash + environment
                          {hardware, engine} + target_class="local_weights" + hf_repo + judge_model)
               The device PRIVATE key never leaves the pod. Stored `self_reported`.
```

The recipe + weight-verification + hardware/engine profile + harness versions all travel with the
benchmark, so anyone can see exactly how a measurement was produced.

### Trust posture (be honest)

A pod runs entirely on hardware its operator owns, so a software-only submission is
**`self_reported`** — tamper-evident, not tamper-proof. It still carries **real** guarantees:
bundle authorship, integrity-in-transit, and replay resistance (single-use nonce + run-scoped
token). It is **never co-ranked** with mothership-verified records. Stronger tiers
(`orchestrated` = the mothership re-generates the work; `attested` = a hardware TEE quote) are the
mothership's job, not the pod's. See `docs/trust-architecture.md` and `docs/run-a-benchmark.md`.
