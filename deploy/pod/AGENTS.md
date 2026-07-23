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

A pod runs on hardware its operator owns, so nothing here is tamper-*proof* — but the tier a run
earns is decided by **what can be independently re-checked**, not by who owns the box.

**`attested`** — the ranked tier — is earned by the **controlled flow**, in any of its three
shapes: `--hf-link` (fresh pull), a hash-verified `--local-dir`, or `--hf-link` + `--serve-url` +
`--verify-endpoint` (bench a live serve, weights hash-verified and the endpoint logprob-fingerprinted
against them). In every case the weights are checked **bit-for-bit against Hugging Face's published
per-file LFS sha256** at a pinned commit, the serve recipe is recorded, and the bundle is
ed25519-signed. The **mothership then independently re-fetches HF and re-checks every weight hash**
before it counts — that re-verification, not a TEE, is what makes it rankable. Scoring and ingest
both gate on `ELIGIBLE_TIERS = {"attested"}`.

**`self_reported`** — everything short of that: an **unverified** endpoint run (`--target` with no
HF link, so there are no weights to hash), an unverifiable repo, or a `--serve-url` run whose
fingerprint mismatched. Still signed, so it keeps bundle authorship, integrity-in-transit, and
replay resistance (single-use nonce + run-scoped token) — it is stored and shown, but **never
ranked**. Prefer the verified endpoint path over this whenever a serve is already up.

See `docs/attestation.md` and `docs/run-a-benchmark.md`.

---

## The preset layer — family ⊕ hardware (`mvp/pod/presets.py`)

`derive_recipe` no longer starts from zero: two composable preset layers seed every serve, and
the operator's own flags always win last via `merge_flags`.

- **Family presets** (`PRESETS` — Gemma-4, Qwen 3.5/3.6 dense+MoE, DeepSeek, GLM, Nemotron,
  Llama 3/4, GPT-OSS, Mistral, Granite, and more, plus a generic fallback) carry only
  **model-intrinsic** flags: reasoning/tool parsers, trust-remote-code, KV/mamba cache dtypes,
  multimodal allowances. Detection (`detect(config, name)`) keys on config.json `model_type`
  first, then architecture substring, then repo name — so a family is recognized however the
  repo is named. Confidence is honest: `high` = validated on AEON's own DGX; `medium` = arch
  understood, not benched here; `low` = safe flags only, parsers stay recommendations in the
  notes (a wrong `--reasoning-parser` crashes the serve). Parser names in presets are only ever
  ones present in the `engines.FLAG_CATALOG` option lists (test-enforced).
- **Hardware presets** (`HARDWARE_PRESETS`: `dgx_spark` / `cuda_generic` / `rocm` / `metal` /
  `cpu`) carry what the **host** needs, independent of family — e.g. the GB10's
  `--attention-backend triton_attn` pin (FlashInfer is broken there). Selected from
  `engines.host_platform()` via `hardware_preset()`.
- **Composition:** `apply_flags(preset, modalities, hardware=)` is the conservative subset a
  headless/GUI run gets by **default** (family safe flags + hardware flags; parser flags only at
  high/medium confidence; audio allowance only when the model declares audio). `full_flags()` is
  the complete recommendation (adds perf options like Qwen fp8 KV) behind the Run tab's
  "★ family best-practice recipe → apply preset" chip — it **fills** Recipe Tuning, editable,
  never silent. The preset id + the flags it contributed travel in the recorded recipe.

## Champion recipes

The mothership publishes the **winning serve recipe per (hardware label × canonical model)**:
`GET /api/recipes/champions?hardware=&model=` (public, read-only; `mvp/aeon/scoring.py
champion_recipes`). A champion is the run with the best demonstrated **peak aggregate tok/s**
whose model **also** carries a quality composite for that pairing — fast AND answers well. Before
publication the recipe is scrubbed: bench wiring (`--served-model-name`/`--host`/`--port`/…) is
stripped, anything credential-named or token-shaped is dropped, and a drafter path is normalised
to the portable `/drafter` mount.

The pod proxies this as `GET /api/pod/recipes/champions`, filtered to its **own detected hardware
label** (a DGX Spark pod sees what won on a DGX Spark), and the Run tab offers each as
"★ CHAMPION RECIPES → apply template" — filling engine + Recipe Tuning + spec decode with the
exact winning recipe, editable before launch. Mothership unreachable → `{available: false}` and
the Run tab keeps working; the champion pull never blocks a bench.

## Resume + deferred idempotent submission

**Job identity:** at job start `aeon_pod` fixes a context (launch UTC timestamp + canonical model
+ detected hardware label — `_job_ctx`) and mints
`job_sig = sha256("started_ts|model|hardware|suite")[:24]` (`_job_sig`) per bundle; `suite_scope`
disambiguates the bundles of one comprehensive job (text suite vs `agentic-v2@hermes` vs
vision/audio/perf). Every bundle carries its `job_sig`.

- **Sessions** (`mvp/pod/pending.py`): `~/.aeon/pending_submits/{job_sig}.json` (chmod 600 — it
  holds a bearer `run_token`) is written the moment the bench opens its mothership run, **even
  when the mothership is unreachable** (run_id stays None, minted at submit time). Deleted only
  after a confirmed final commit (ok **or** duplicate). Session files survive pod restarts; the
  in-memory job list doesn't.
- **Checkpoint streaming:** the bench pushes cumulative results with `final=False` every 8 cases;
  the mothership claims the run only on the final commit and dedups per `(run_id, case_id)` — a
  mid-run kill loses nothing already submitted. Checkpoint failures back off exponentially and
  can never stall the bench.
- **Interrupted, not failed:** a stopped/crashed bench flips its local run row to `interrupted`
  (`jobs.py` on subprocess exit; `recover.py` at boot) — per-case results intact, so the Run tab
  offers **⟲ RESUME** (`POST /api/pod/jobs/{id}/resume`): the identical argv/env relaunches with
  `AEON_RESUME=1`, `_resume_anchor` picks the newest interrupted run for that model+suite
  (guarded by the current case plan), and reuses its rid + `job_sig` so the open mothership
  session and checkpoint stream simply continue.
- **Completeness gate:** an incomplete pass of the planned cases is **not** auto-submitted — it
  stays local + resumable; `--force-submit` is the CLI-only escape hatch. The deferred path
  (`pending.submit_pending`) enforces the same gate (409 `incomplete`).
- **Deferred submit:** **⬆ SUBMIT TO MOTHERSHIP** → `POST /api/pod/jobs/{id}/submit` (re-commits
  every pending session the job minted) or `POST /api/pod/submit/{job_sig}` for sessions from
  before a pod restart. Idempotent end-to-end: `GET /api/v1/jobs/{job_sig}` pre-checks so a
  multi-MB bundle the mothership already has is never re-uploaded, and a duplicate final submit
  answers HTTP 200 `{"ok":true,"duplicate":true,"run_id":…,"message":"job already submitted and
  available on the Mothership"}` — the nonce is released cleanly (`duplicate`, never
  quarantined): re-submitting a finished job is correct client behaviour, not forgery. Bundles
  without a `job_sig` (old pods in the field) behave exactly as before.

## Container hygiene & image provenance (honest posture)

- **Harness containers are ephemeral by design** — one `docker run --rm` (or named + `docker cp`)
  per agentic task, torn down immediately (see above). The engine container runs under the fixed
  name `aeon-bench-serve`.
- **Boot reconcile** (`mvp/pod/recover.py`): a fresh pod boot proves no bench is alive, so it
  removes an orphaned `aeon-bench-serve`, restarts production containers a dead run paused and
  never restored (the `paused.json` ledger — start only, never rm), and marks stranded `running`
  run rows `interrupted` (resumable). Every action prints `[pod][recover]` in
  `docker logs aeon-pod`.
- **Image digest provenance:** recipes record the engine image's digest identity
  (`image_digest` / `image_id` / `image_repo_digests`) even for `:latest` tags, so a result is
  reproducible against the exact bytes that served it, and the downloadable `serve.sh` / compose
  pin by digest (digest refs are format-validated before substitution). Harness + engine
  containers carry the `aeon.pod.harness` / `aeon.pod.job` labels and are swept by label on
  stop, on worker exit, and at boot reconcile.
- **Not yet done (known posture, stated honestly):** engine images are **not signature-verified
  or allowlisted** — the operator can point the pod at any custom image, and only the recorded
  recipe and its digest make that visible after the fact.
