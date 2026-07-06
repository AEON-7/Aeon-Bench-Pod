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

Defined in the registry at `mvp/pod/harnesses.py`; each has an adapter under `mvp/pod/adapters/`.
All three are pointed at the pod's served alias **`model-under-test`** (`modelhost.DEFAULT_ALIAS`)
— never at a raw model name, so the harness cannot tell which model it is driving.

> **Harnesses are ephemeral, NOT services.** The pod is the control plane: for each agentic task it
> launches ONE fresh `docker run --rm` (or `--name … + docker cp`) harness container via the Docker
> socket, then tears it down — so agent state can never leak between tasks (see `run_harness2.py`).
> The compose therefore does **not** define hermes/openclaw/opencode as long-running services; it
> only **builds** the three images under the exact names the adapters run
> (`aeon-harness-{hermes,openclaw,opencode}`) and mounts `/var/run/docker.sock` into `aeon-pod` so
> the pod can spawn them. The pod runs on the **host network** and reaches the model (and the
> host-net harness containers it spawns) on `127.0.0.1:${AEON_TARGET_PORT:-8000}`.

| Key | Name | Repo | Deploy | Image (local build) | Version command |
|---|---|---|---|---|---|
| `hermes` | Hermes Agent | github.com/NousResearch/hermes-agent | docker | `aeon-harness-hermes` (harness-hermes.Dockerfile) | `hermes --version` |
| `openclaw` | OpenClaw | github.com/openclaw/openclaw | npm | `aeon-harness-openclaw` (harness-openclaw.Dockerfile) | `openclaw --version` |
| `opencode` | OpenCode | github.com/anomalyco/opencode | npm | `aeon-harness-opencode` (harness-opencode.Dockerfile) | `opencode --version` |

### hermes — Hermes Agent
- **What it is:** NousResearch's agent harness, built from source into `aeon-harness-hermes`
  (`harness-hermes.Dockerfile`, `ENTRYPOINT python /app/run_agent.py`, `TERMINAL_ENV=local` baked
  in so its terminal/file tools run INSIDE the per-task container's `/work`).
- **Driven by the pod:** `mvp/pod/adapters/hermes.py` runs one named container per task
  (`--query=<prompt> --base_url=<url> --api_key=sk-local --model=<alias> --max_turns=8
  --save_sample`), mounting a `context_length:65536` config at `/root/.hermes/config.yaml` (Hermes
  refuses any served window <64K), then `docker cp`s `/work` back out and parses the ShareGPT sample.
- **Version pin:** `AEON_HERMES_REF` (git ref, build arg `HERMES_REF`).
  - `# TODO verify` a concrete release tag + that the source entry point is `run_agent.py`.

### openclaw — OpenClaw
- **What it is:** an npm-distributed agent CLI (`harnesses.py`: `deploy="npm"`,
  `package="openclaw"`), wrapped in `harness-openclaw.Dockerfile` (`npm i -g openclaw@<pin>`).
- **Driven by the pod:** `mvp/pod/adapters/openclaw.py` runs `docker run --rm --network host
  -v <home>:/root/.openclaw aeon-harness-openclaw agent --local --json --agent main -m "<prompt>"
  --model dgx/<alias>`, with the endpoint set in a generated `~/.openclaw/openclaw.json` (not env).
- **Version pin:** `AEON_OPENCLAW_VERSION` (build arg `OPENCLAW_VERSION`).

### opencode — OpenCode
- **What it is:** an npm-distributed agent CLI (`harnesses.py`: `deploy="npm"`,
  `package="opencode-ai"`, CLI binary `opencode`), wrapped in `harness-opencode.Dockerfile`.
- **Driven by the pod:** `mvp/pod/adapters/opencode.py` runs `docker run --rm --network host
  -v <workdir>:/work -w /work aeon-harness-opencode run --format json --auto -m dgx/<alias>
  "<prompt>"`, with the provider set in an `opencode.json` dropped into the workdir (not env).
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
(`AEON_HERMES_REF` git ref, `AEON_OPENCLAW_VERSION`, `AEON_OPENCODE_VERSION`) are exactly what
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
  3. SERVE     modelhost.derive_recipe() builds the serve recipe on the FIXED alias
               `model-under-test`. Engine: the operator's pick from the pod.engines catalog
               (aeon-vllm-ultimate / vLLM / vLLM-ROCm / SGLang / llama.cpp — containerized
               `docker run`; Apple MLX + LM Studio — bare-metal, recipe recorded identically;
               custom image override allowed), else auto: GGUF → llama.cpp; DGX Spark +
               aeon-vllm-ultimate → aeon-vllm-ultimate; Apple silicon → MLX; else vLLM.
               RECIPE TUNING: operator flag overrides merge in (engines.merge_flags — matching
               flags replaced, new appended; --served-model-name/--host/--port protected; the
               64K context floor is enforced). An explicit DFlash drafter card is pulled +
               hash-verified like the model and mounted at /drafter. The full recipe (engine,
               image, flags, custom_flags, drafter repo@rev) is recorded WITH the benchmark.
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
mothership's job, not the pod's. See `docs/attestation.md` and `docs/run-a-benchmark.md`.
