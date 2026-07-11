# AEON Bench — Trust Architecture (unified)

> **Authoritative trust-chain spec.** Synthesizes the per-layer designs + their red-team
> verdicts into one honest picture: what each layer *actually proves*, what it *cannot*,
> and exactly where software stops and a hardware root of trust must take over.
> Grounded in (do not duplicate): `mvp/aeon/attest.py`, `docs/attestation.md`,
> `docs/architecture.md`, `docs/onyx-deployment.md`, `DESIGN.md` (§2, §6b, §10b, §12, §17).

---

## 0. The headline (read this first)

A signature proves **authorship, not authenticity** (DESIGN §2). The whole pod runs on
hardware its operator owns — including the ed25519 key it self-generates at enrollment,
the target endpoint, the harness, and every telemetry source. Therefore:

- **Software-only verification of an operator-hosted pod is `self_reported`.** It is
  *tamper-evident* (deters casual cheats), never *tamper-proof* (a motivated operator wins).
- **`orchestrated` only counts when the mothership itself drives the work** — it
  re-*generates* outputs / re-runs Tier-0 in *its own* sandbox, not when it re-scores
  pod-supplied text. (The single most important correction the red-team forced; see §3.)
- **Only `attested` (hardware TEE measuring execution) resists a motivated operator** —
  and even then only for self-hosted/local-weight targets, never for opaque remote APIs.

If you remember one sentence: **re-deriving a score from pod-supplied output proves
"this text scores X," never "the claimed model produced this text." Closing that gap
needs either the mothership generating the output, or a hardware quote over execution.**

---

## 1. Threat model (one screen)

**The attacker** is a *motivated operator* who wants a fake top score on a public,
multi-operator leaderboard. They own the box end-to-end: root, the dockerd, the pod
source (it is open), the enrolled signing key, the model endpoint config, the harness
runtime config, and every probe/telemetry surface. They will:

- Author `raw_output` that satisfies the (publicly shipped) Tier-0 checkers, never running a model.
- Patch the open pod to print a known-good `build_hash` and sign with the real enrolled key.
- Put a localhost proxy in front of the model/engine to spoof identity or rewrite tool traces.
- Cherry-pick / regenerate-until-pass, then submit only the best of N as "one run."
- Enroll many keys (anonymous signup) so revoking one costs nothing.

**Not in scope** (handled elsewhere or accepted): DoS of the mothership (WAF/CrowdSec),
operator who only wants to bench privately (they never submit), genuine aesthetic disputes
(Tier-2 arena, out of the auto-composite).

### The Iron Rule
> **Software cannot prove its own integrity to a remote party.** A malicious host can
> patch the very code that hashes/signs itself ("who validates the validators"). Any layer
> must state what it *actually* proves vs what it *cannot*, and whether it needs a hardware
> root of trust (TPM measured boot / AMD SEV-SNP / Intel TDX / AWS Nitro) to be real.
> Anything that only deters a casual cheat is **TAMPER-EVIDENT, not tamper-proof.**

---

## 2. Layered verification chain

Order of trust flow: **model → runtime/engine → harness → pod/OS/hardware → submission channel → sandbox ingest.**
Each layer below states PROVES / DOES NOT PROVE / hardware dependency, with the red-team
verdict folded in. Layers marked **THEATER (demoted)** were claimed to prove more than they
do; they are kept only at their honest, reduced strength.

### 2.1 Model identity (`attest.model_attestation`, `verify_model_ref` — PARTIAL/BUILT)
- **PROVES:** *Identity-of-claim* against canonical upstream — HF advertised commit sha +
  per-file LFS sha256 for `repo@rev`. *Identity-of-bytes* **only** when local weights are on
  disk (`weights_sha256` over gguf/safetensors).
- **DOES NOT PROVE:** that the *served* model is that model. For any remote API / unexposed
  weights, the operator can claim a strong ref while serving a different/cheaper/tool-augmented
  model. Nothing binds `weights_sha256` to the bytes that produced `raw_output` unless the
  weights are local AND execution is proven to have loaded them.
- **Hardware:** local-weight identity = software-real; remote-API identity-of-bytes is
  **impossible without a TEE measuring the loaded weights** (and even then only if the model
  runs inside *your* enclave — a third-party API never does, `attestation.md` Layer 3 ceiling).

### 2.2 Runtime/engine identity (engine fingerprint — **THEATER (demoted to advisory)**)
- **Design:** passive fingerprint of the OpenAI-compatible endpoint (`/v1/models`, Ollama
  `/api/version`+`/api/tags`, llama.cpp `/props`+`Server:`, vLLM `/version`, UA/Server headers).
- **RED-TEAM (defeats it, cost trivial):** a ~30-line reverse proxy echoes any version string /
  header / `/v1/models` payload. "A third party re-probes to corroborate" fails because the
  third party hits the **same lying endpoint**. This is a passive read of attacker-controlled strings.
- **HONEST POSTURE:** engine_name/engine_version are an **UNVERIFIED, operator-reported advisory
  facet** (badge: *"engine claim — operator-reported, not proven"*). **It must never gate or
  rank a board.** An optional active challenge (logprobs-shape / tokenizer quirk) raises proxy
  cost but a proxy can forward the challenge to a real engine while serving cheap weights for
  scored traffic — so it never reaches identity-of-bytes without a TEE co-measuring engine+weights.
- **First-party exception:** `aeon-vllm-ultimate` shipped as a cosign-signed image gives
  *identity-of-image* (still not identity-of-loaded-weights without a TEE). Third-party engines
  stay `self_reported`; do not let first-party provenance leak credibility onto them.

### 2.3 Harness integrity (dual Hermes/OpenClaw, cosign + digest-pin — PARTIAL)
- **PROVES (real, keep):** *Supply-chain integrity to the image boundary.* cosign-verify against
  the bundled managed key + digest-pin + central ingest allowlist ⇒ "the harness image **bytes**
  equal the CI-built artifact of the open source, rebuildable to the same digest by any third
  party, and the mothership ingests only allowlisted digests."
- **DOES NOT PROVE:** that the blessed image **actually executed unmodified**, nor what it
  **talked to**. Red-team breaks, all cheap:
  - **Proxy-shim (trivial):** run the genuine signed harness UNMODIFIED but point its `base_url`
    at an operator localhost proxy that rewrites tool_call args/order/final_state to satisfy the
    *public, enumerable* Tier-0 `tool_trace` predicates. cosign verifies bytes; **nothing verifies
    what the harness talks to.** Server re-derivation re-scores the same forged transcript → passes.
  - **Config-leak / "vanilla" bypass (trivial):** the image is byte-identical but the operator
    passes a tuned system prompt, richer tool docs, generous retry/resample, higher max_steps —
    *vanilla-ness lives in config, not bytes.* Unless **every runtime input is hash-pinned into the
    signed manifest and allowlist-constrained**, "unmodified harness" is meaningless.
  - **Per-harness model swap (trivial):** series execution lets the proxy detect Hermes vs OpenClaw
    by request shape and serve different weights/configs; the model×harness delta becomes fiction.
  - **Patched-binary-with-blessed-digest (moderate):** on the operator's host the digest is a manifest
    string; the bootstrap "cosign verify before launch" is operator-side open code — patch it to no-op.
- **Hardware:** harness-execution integrity is closed **only** by a TEE measuring image+rootfs+
  kernel/cmdline; engine/model/tool integrity is closed only if engine+weights+tools are **co-measured
  in the same enclave** as the harness (whole agentic loop attested).

### 2.4 Pod / OS / build attestation (`attest.build_hash`, challenge-nonce — BUILT but **THEATER for tamper-proofing**)
- **PROVES:** the responder **controls the pinned ed25519 key** and **reports** the expected
  `build_hash`; the challenge nonce makes it **replay-proof** (a captured statement can't be reused).
- **DOES NOT PROVE (the recursion):** that the process actually *runs* that code.
  `build_hash()` is computed by the same process that signs; a patched pod returns the known-good
  hash as a constant and answers the nonce from the same patched process. **`SYSTEM_OK` / "validate
  the validators" is self-attestation by the very process under suspicion — it adds nothing over the
  signature** absent a TEE. Demote it: it proves *liveness + key-possession*, not executed code.
- **Hardware:** closing this is the whole point of Layer 4 — a hardware-quoted measurement
  (TPM measured boot / SEV-SNP / TDX / Nitro) wrapping the Layer-1 statement.

### 2.5 Submission channel (TLS + WAF allow-list + signed bundle — **REAL, ship it**)
- **PROVES (genuinely, no hardware needed):**
  - **Authorship + integrity-in-transit:** an accepted bundle was signed by a pinned, enrolled,
    non-revoked device key and is byte-for-byte untampered (`attest.verify_manifest` over canonical
    body). **A compromised TLS edge (Caddy/Cloudflare) cannot forge an accepted bundle** — content is
    signed end-to-end, not just the channel.
  - **Replay resistance:** server-issued single-use nonce + run-scoped token ⇒ a bundle is valid for
    exactly one run; no resubmission/duplication/precompute.
- **DOES NOT PROVE:** anything about *content truth*. mTLS/WAF/CrowdSec protect channel + rate, not
  whether the numbers reflect a real run. A validly-signed forgery from a compromised-but-enrolled
  pod sails through this layer.
- **Hardware:** none required; this layer is sound software.

### 2.6 Sandboxed ingest (noexec landing zone + state machine — **REAL, ship it**)
- **PROVES (genuinely):** the bundle is treated as **inert DATA, never code**, on a
  `noexec,nosuid,nodev` landing zone with no interpreter on PATH; strict JSON-Schema +
  streaming bounded reader (zip-bomb/oversize/deep-nesting) + content-addressed dedup, all
  **before** any DB write. Malformed/oversized/replayed payloads are quarantined; the ingest
  host is not exploitable by the bundle. Separable reason codes keep forgery vs corruption vs
  honest config-drift distinguishable.
- **DOES NOT PROVE:** that the bundle's numbers reflect a real run (that is §3's job).
- **Hardware:** none required.

### Layer scorecard

| Layer | Honest verdict | Proves | Hardware dep |
|---|---|---|---|
| Model identity | partial | claim vs HF; bytes only if local weights | TEE for remote-API bytes |
| Runtime/engine | **theater → advisory** | nothing verifiable (proxy-spoofable) | TEE co-measuring engine+weights |
| Harness | partial | image-byte supply-chain only | TEE for execution + co-measured loop |
| Pod/build attest | **theater → liveness only** | key-possession + reports build_hash | TEE for "code actually ran" |
| Submission channel | **real** | authorship, integrity-in-transit, replay | none |
| Sandbox ingest | **real** | ingest-host safety, data hygiene | none |

---

## 3. Capability × trust-tier matrix

Tiers (carried in `runs.trust_tier`, DESIGN §2):

- **`self_reported`** — software attestation on an operator host. *Tamper-evident only.*
  **Default for every operator-hosted submission.** Shown, badged distinctly, never co-ranked, never a record.
- **`orchestrated`** — **the mothership itself drove the work**: it acquired/launched the probe on
  infrastructure it controls and **re-GENERATED** the outputs (re-ran Tier-0 / re-issued the agentic
  loop into its own sandbox), not merely re-scored pod-supplied text. Record-eligible for the
  re-derived (deterministic) backbone.
- **`attested`** — `self_reported`/`orchestrated` **plus a hardware TEE quote** binding image
  digest + run nonce + (for full soundness) the executing benchmark process, loaded weights, and
  tool sandbox to genuine hardware.

✅ guaranteed · ⚠️ claim-only / tamper-evident · ❌ not guaranteed (operator can forge)

| Capability | `self_reported` (software) | `orchestrated` (mothership re-generates) | `attested` (hardware TEE) |
|---|---|---|---|
| **Bundle authorship + integrity-in-transit** | ✅ | ✅ | ✅ |
| **Replay resistance** | ✅ | ✅ | ✅ |
| **Ingest-host safety** | ✅ | ✅ | ✅ |
| **Tier-0 objective scores reflect a real run** | ❌ (pod-authored `raw_output`) | ✅ *iff mothership generated/re-ran it* (else still ❌) | ✅ |
| **Tier-1 (judge) scores** | ⚠️ claim; un-shadowed single-judge **not record-eligible** | ⚠️ cross-family verifier = mothership re-judge | ⚠️ (judge bias is orthogonal to hardware) |
| **Model identity = served bytes** | ⚠️ claim (HF ref); ✅ only if local weights | ⚠️ same; ✅ local weights re-loaded by mothership | ✅ self/local only; ⚠️ remote API stays claim |
| **Engine identity** | ❌ advisory only (proxy-spoofable) | ❌ unless mothership owns the engine | ✅ only if engine co-measured in enclave |
| **Harness executed unmodified** | ⚠️ image-bytes only; execution ❌ | ✅ iff mothership ran the harness | ✅ iff harness+loop co-measured |
| **"Vanilla" harness config** | ❌ unless every runtime input hash-pinned | ✅ mothership sets the config | ✅ |
| **Cherry-pick / regenerate-until-pass resistance** | ❌ | ⚠️ only if sampling discipline enforced server-side | ✅ only if enclave enforces single-sampling |
| **Speed metrics** | ❌ (operator-timed) | ⚠️ if mothership-timed on its box | ✅ |
| **Resists a *motivated* operator** | **❌ NO** | **partial** (deterministic backbone only, reachable targets only) | **✅** (self/local-weight targets; remote API never fully) |

**The load-bearing row is the last one.** `self_reported` deters lazy fakes and nothing more.
`orchestrated` is honest **only** when redefined as re-generation by the mothership, and even then
covers only the deterministic backbone for *reachable* endpoints/local weights. `attested` is the
only tier that resists a motivated operator — and remote-API model identity stays claim-only even
with hardware.

---

## 4. Sandboxed ingest pipeline (state machine)

A submission is **DATA, never code.** Outbound TLS 443 → `api.aeon-bench.com` (own
Cloudflare-proxied origin / dedicated `cloudflared` tunnel, `onyx-deployment.md`) → Coraza
positive allow-list (`POST /api/v1/runs`, `POST /api/v1/runs/{id}/results`, method + `Content-Type`
pinned, per-route body caps, JSON depth/field caps, CrowdSec rate-limits). Fail-closed; every
transition logged with `run_id` + `run_nonce`.

```
RECEIVED
  └─▶ AUTH_OK        bearer + enrolled, non-revoked device key; nonce unused
  └─▶ SIG_OK         attest.verify_manifest over canonical body; public_key == enrolled pin
                     (build_hash compared to known-good release allowlist = advisory, NOT integrity)
  └─▶ LIVENESS_OK    folded into the submit handshake (pod opens NO inbound port, architecture.md):
                     pod proves key-possession + liveness mid-submission.
                     HONEST: proves liveness, NOT executed code (§2.4). TEE quote verified HERE if present.
  └─▶ MODEL_OK       verify_model_ref: HF commit sha + per-file LFS sha256; weights_sha256 if local
                     (identity-of-CLAIM for API targets, §2.1)
  └─▶ HARNESS_OK     cosign-verify reported harness digest vs pinned Hermes/OpenClaw allowlist
                     (image-bytes only; NOT execution, §2.3) + config-hash ∈ allowlist (§6 fix)
  └─▶ SCHEMA_OK      strict JSON Schema: required fields, enum status/tier, score∈[0,1],
                     creativity∈[0,3], case_ids ⊆ pinned suite_hash, additionalProperties:false
  └─▶ SIZE_OK        streaming bounded reader: decompressed-size + compression-ratio caps,
                     per-field length caps  → zip-bomb / oversize rejected pre-parse
  └─▶ DEDUP          content-addressed: each blob keyed by sha256 in MinIO/S3; shared images O(1)
  └─▶ SCORE_CHECK    self_reported: store as-is, NON-record-eligible, badge applied.
                     orchestrated: mothership RE-GENERATES (re-runs Tier-0 / re-drives agentic loop
                     in its own gVisor sandbox, §3) and scores THAT; pod numbers become advisory.
                     Tier-1 un-shadowed single-judge → dropped from denominator (§6 fix).
  └─▶ COMMIT         single txn: runs + results rows (DESIGN §17) with verified trust_tier.

ANY FAILED TRANSITION ─▶ QUARANTINE  (ingest/quarantine/<run_id>/, still noexec)
```

The landing zone is `/srv/appdata/aeon-bench/ingest/<run_id>/` mounted
`noexec,nosuid,nodev`, no interpreter on PATH. **Nothing in the bundle is ever executed.**
Arena HTML is stored inert and only ever rendered later in the existing client-side
`sandbox="allow-scripts"` iframe (DESIGN §8.4), never server-side.

### Quarantine / reject reason codes (separable)

| Code | Stage | Class |
|---|---|---|
| `REPLAY_NONCE` | AUTH | forgery |
| `UNKNOWN_KEY` / `REVOKED_KEY` | AUTH | forgery |
| `BAD_SIG` | SIG | forgery |
| `BUILD_HASH_MISMATCH` | SIG | drift/advisory |
| `LIVENESS_FAIL` | LIVENESS | forgery/abandon |
| `TEE_QUOTE_INVALID` | LIVENESS | forgery (attested tier) |
| `MODEL_MISMATCH` | MODEL | drift or forgery |
| `HARNESS_UNVERIFIED` / `CONFIG_NOT_ALLOWLISTED` | HARNESS | forgery |
| `SCHEMA_INVALID` | SCHEMA | corruption/forgery |
| `SIZE_EXCEEDED` / `ZIP_BOMB` | SIZE | abuse |
| `TIER0_MISMATCH` | SCORE_CHECK | forgery (orchestrated re-gen only) |
| `SINGLE_JUDGE_INELIGIBLE` | SCORE_CHECK | down-tier, not reject |

On a **forgery-class** failure: increment the device key `fail_count` (sliding window,
`accounts.py` style); repeated forgery-class → auto-revoke + CrowdSec decision + admin flag.
**Honest config-drift** (`MODEL_MISMATCH` because an HF revision moved, `BUILD_HASH_MISMATCH`
on a stale pod) is auto-retryable and does **not** count toward revocation. Quarantined
bundles are retained inert for forensic review per a published retention window.

---

## 5. Postgres data model (full searchable profile)

The submission profile reuses the DESIGN §17 schema. Key tables and the indexes that make
every variable searchable/sortable/filterable for the leaderboards:

```sql
-- identity & catalog (immutable, content-pinned)
models(id, name, family, params, provider, license)
model_versions(id, model_id, checkpoint_hash, quantization, context_len)
suites(id, name)
suite_versions(id, suite_id, version, content_hash, determinism_report_ref)
categories(id, suite_version_id, key, weight_default)
case_versions(id, case_id, version, content_hash, prompt_json, reference_json,
              constraints_json, exposure, is_canary, scoring_tier, scoring_json)

-- enrolled pod keys (NEW — channel identity, reuses attest.public_key_b64)
enrolled_keys(id, public_key, fingerprint, owner_user_id, created_at,
              status /* active|revoked */, fail_count, revoked_at)

-- the run = the searchable profile spine
runs(id, model_version_id, target_id, probe_id, suite_version_id, harness,
     trust_tier /* self_reported|orchestrated|attested */, run_nonce, colocated_loadgen,
     status, heartbeat_at, started_at, finished_at, triggered_by, owner_user_id,
     build_hash, suite_hash,
     engine_name, engine_version, engine_verified /* false=advisory */,
     judge_model, judge_family, judge_is_launcher, self_judge_warning, verification_mode)

-- full machine + runtime profile, signed manifest, materialized telemetry
environments(id, run_id UNIQUE, hardware_json, software_json, recipe_json,
             recipe_hash, manifest_hash, manifest_signature,
             tee_quote_ref /* NULL unless attested */,
             resource_summary_json /* mem/GPU/CPU mean+p95, joules */, raw_series_ref)

-- per-case results (+ per-harness via harness column on the run, dual-harness stored as 2 runs/result-sets)
results(id, run_id, case_version_id, attempt, harness_id, raw_output_ref, raw_output_hash,
        transcript_ref, transcript_hash, speed_json, deterministic_score, status,
        n_criteria_effective, UNIQUE(run_id, case_version_id, attempt))

-- judging provenance (BYO-judge)
judge_runs(id, run_id, judge_model, judge_version, judge_calibration_epoch, judge_config_hash,
           decoding_json, seed, resolution_source, launcher_model, is_self_judge,
           is_same_family, self_judge_ack, warning_flags_json, created_at)
judge_scores(id, result_id, judge_run_id, tier, criterion_id, satisfied, evidence_span_ref,
             abstained, score, scorer_version, superseded_at)
criterion_verdicts(id, result_id, criterion_id, satisfied, evidence_span_ref,
                   decided_by /* judge|tier0_shadow|verifier_consensus */, runtime_agreement)

-- precomputed read models for the boards
category_scores(run_id, category_id, quality_score, quality_score_var, ci_low, ci_high,
                speed_score, n_cases, n_criteria_effective, anchor_set_version,
                rubric_version, scoring_epoch_id, superseded_at)
leaderboard_matrix(suite_version_id, model_version_id, category_id, quality_score, speed_score,
                   computed_at, source_run_id, UNIQUE(suite_version_id, model_version_id, category_id))
```

### Key indexes (searchable/sortable profile + board hot paths)

```sql
-- profile facets: filter boards by hardware, engine, harness, judge, tier
CREATE INDEX runs_hw_gin       ON environments USING gin (hardware_json jsonb_path_ops);
CREATE INDEX runs_sw_gin       ON environments USING gin (software_json jsonb_path_ops);
CREATE INDEX runs_facets       ON runs (suite_version_id, trust_tier, harness, engine_name, finished_at);
CREATE INDEX runs_model_time   ON runs (model_version_id, suite_version_id, finished_at);
CREATE INDEX runs_judge        ON runs (judge_model, judge_family);
-- board reads (never touch raw)
CREATE INDEX lbm_lookup        ON leaderboard_matrix (suite_version_id, category_id, quality_score DESC);
CREATE INDEX catscore_lookup   ON category_scores (model_version_id, category_id, run_id)
  WHERE superseded_at IS NULL;
CREATE INDEX results_run_case  ON results (run_id, case_version_id);
CREATE INDEX enrolled_active   ON enrolled_keys (fingerprint) WHERE status = 'active';
```

### Leaderboard / view queries

**Per-hardware global board** (top performer per category, on one GPU class, record-eligible only):
```sql
SELECT DISTINCT ON (lm.category_id)
       lm.category_id, mv.id AS model_version_id, lm.quality_score, lm.speed_score
FROM leaderboard_matrix lm
JOIN runs r           ON r.id = lm.source_run_id
JOIN environments e   ON e.run_id = r.id
JOIN model_versions mv ON mv.id = lm.model_version_id
WHERE lm.suite_version_id = $1
  AND r.trust_tier IN ('orchestrated','attested')          -- never self_reported on a board
  AND e.hardware_json->>'gpu_model' = $2                    -- e.g. 'NVIDIA H100 80GB'
ORDER BY lm.category_id, lm.quality_score DESC;
```

**Model × hardware historical mean** (how a model performs per GPU class over time):
```sql
SELECT e.hardware_json->>'gpu_model' AS gpu, cs.category_id,
       avg(cs.quality_score) AS mean_quality,
       avg(cs.speed_score)   AS mean_speed,
       count(*)              AS n_runs
FROM category_scores cs
JOIN runs r         ON r.id = cs.run_id
JOIN environments e ON e.run_id = r.id
WHERE r.model_version_id = $1
  AND r.trust_tier IN ('orchestrated','attested')
  AND cs.superseded_at IS NULL
GROUP BY gpu, cs.category_id
ORDER BY gpu, cs.category_id;
```

**Model × harness agentic view** (Hermes vs OpenClaw delta on agentic categories):
```sql
SELECT r.model_version_id, r.harness, cs.category_id,
       avg(cs.quality_score) AS mean_quality, count(*) AS n
FROM category_scores cs
JOIN runs r          ON r.id = cs.run_id
JOIN categories c    ON c.id = cs.category_id
WHERE c.key = 'agentic_tool_use'
  AND r.trust_tier IN ('orchestrated','attested')
  AND cs.superseded_at IS NULL
GROUP BY r.model_version_id, r.harness, cs.category_id;
-- UI pivots harness → columns to render the per-model delta with §13 CIs.
```

`engine_name`/`engine_version` are exposed as searchable/filterable facets **with the
`engine_verified=false` advisory badge** (§2.2) — never as a board gate.

---

## 6. Fixes the red-team forces (apply these or the tiers are dishonest)

1. **Redefine `orchestrated` = re-GENERATE, not re-derive.** Re-scoring pod-supplied
   `raw_output` is recomputation, not regeneration — `TIER0_MISMATCH` *structurally cannot
   fire* when pod-claimed == re-derived. For record eligibility the mothership must itself call
   the target / run the local-weight model and produce its **own** outputs. Until then, every
   operator-host submission is `self_reported`. **This is the single change that moves the
   stack from theater → honest.**
2. **Re-drive the agentic loop server-side** for record-eligible agentic runs: the mothership
   re-issues the model's tool calls into **its own** tool sandbox and feeds **mothership-computed**
   tool results — defeating the proxy-shim and pre-cooked-tool-result attacks. Re-scoring a stored
   transcript does not.
3. **Hold back the answer keys.** The suite + checkers currently ship to the pod
   (`architecture.md`), so the operator has every expected answer offline. Resolve checkers
   server-side or use a **sealed in-memory holdout** fetched per-run (DESIGN §13), plus **canary
   cases** with never-public tokens (perfect recall flags memorization/fabrication). Raises
   fabrication from *trivial* to *must actually elicit the answer*.
4. **Hash-pin EVERY runtime input** (system prompt, tool schema/descriptions, max_steps,
   retry/resample policy, temperature, stop rules) into the signed manifest and constrain to a
   published allowlist (`CONFIG_NOT_ALLOWLISTED`). Otherwise "vanilla harness" leaks through config.
5. **Demote engine fingerprint to advisory** (`engine_verified=false`); never gate a board on it.
6. **Demote `LIVENESS_OK`/build-hash challenge** to "liveness + key-possession" in `proves[]`;
   stop calling it anti-tamper. Gate any real integrity claim on a TEE quote.
7. **Enforce the Tier-1 self-judge carve-out at COMMIT:** if a Tier-1 criterion is un-shadowed AND
   judged by a single same-family/self judge with no cross-family verifier, **drop it from the
   denominator** (`n_criteria_effective`) / mark non-record-eligible (DESIGN §6b.4, §10b.6). The
   ingest pipeline must apply this as an explicit transition — it currently does not.
8. **Bind sampling discipline** (per-case nonces echoed in each generation/transcript, attempt caps,
   server-issued time-boxed case manifest) to raise cherry-pick cost. Software-only this is still
   bypassable by a patched pod → label it **TAMPER-EVIDENT**.
9. **Gate revocation on something costlier than a key:** tie enrollment to a proof-of-personhood /
   staked account so fabricate-then-burn-a-key Sybil costs more than one anonymous signup; add
   cross-submission anomaly detection (impossible-speed-for-hardware, score-vs-hardware-class
   outliers) since clean fabrications never trip the failure counter.
10. **Publish the boundary verbatim** in the trust anchor (`/.well-known/aeon-bench.json`):
    cosign+digest+allowlist = supply-chain integrity to the image boundary **only**; harness
    execution, engine identity, model-served-bytes, and tool-sandbox integrity are `self_reported`
    until server-side re-driving (fix 1/2) or a full-loop hardware quote backs them.

---

## 7. Phased build plan (real-today-in-software → hardware-later)

Tied to existing `attest.py` (`build_hash`, ed25519 keypair, `sign_manifest`/`verify_manifest`,
`model_attestation`/`verify_model_ref`) and the DESIGN milestones.

| Phase | Build (software, real today) | Honest tier reached |
|---|---|---|
| **P0 — channel + ingest (ship now)** | `enrolled_keys` table + `POST /api/v1/enroll` (PoP over `attest.public_key_b64`); run-open mints single-use nonce + run-scoped token; results stream into `noexec,nosuid,nodev` landing zone via bounded reader; `aeon/ingest.py` state machine AUTH→SIG→SCHEMA→SIZE→DEDUP→COMMIT; quarantine + reason codes; Coraza allow-list + CrowdSec. **Reuses `verify_manifest` verbatim.** | `self_reported`, with **real** authorship/integrity/replay/ingest-safety guarantees |
| **P1 — honest re-generation = `orchestrated`** | Mothership PULL/Mode-A acquisition; re-RUN Tier-0 (Math/IFEval/`unit_test` pass@k) in its **own gVisor sandbox** from a server-side holdout; re-drive the agentic loop with mothership-computed tool results; hold back answer keys + canary cases; enforce Tier-1 single-judge carve-out at COMMIT. | `orchestrated` (deterministic backbone, reachable targets only) — record-eligible |
| **P2 — pin the soft edges** | Hash-pin all harness runtime inputs into the manifest + config allowlist; dual Hermes/OpenClaw cosign-signed digest-pinned images + central allowlist; engine fingerprint shipped as **advisory** facet; proof-of-personhood / staked enrollment + cross-submission anomaly detection. | hardens `self_reported`; raises forgery cost |
| **P3 — hardware root of trust = `attested`** | TEE quote (SEV-SNP / TDX / Nitro / TPM measured boot) verified at the `LIVENESS_OK`/`SYSTEM_OK` step **without changing the verifier contract** (`attestation.md` Layer 4): quote must bind image+rootfs+kernel/cmdline measurement, the run nonce, AND the executing benchmark process + loaded weights + tool sandbox (whole-loop attestation) — else fabrication survives. Stored in `environments.tee_quote_ref`. | `attested` — resists a motivated operator (self/local-weight targets; remote API stays claim-only) |

The API/manifest shape from `attest.py` is deliberately unchanged across phases: a TEE quote
slots into the same pinned-key + nonce + manifest verification, now gated on a valid quote.

---

## 8. The trust anchor (publish verbatim)

`aeon-bench.com/.well-known/aeon-bench.json` (cacheable, public): pinned mothership public key
+ known-good `build_hash` releases + pinned Hermes/OpenClaw image digests + the **boundary
statement** from fix 10. A third party uses it to run the `attestation.md` verifier checklist.
The boundary statement says, in plain words, that on an operator host the leaderboard *number*
is operator-authored unless the run is `orchestrated` (mothership re-generated) or `attested`
(hardware-quoted whole loop).

---

## 9. Summary of what is real vs aspirational

- **REAL today (software):** the submission channel and sandboxed ingest — TLS + WAF allow-list
  + CrowdSec, ed25519 content-signing over a canonical body with a single-use server nonce
  (replay-proof, edge-compromise-proof), the `noexec` DATA landing zone, streaming bounded reader,
  strict JSON-Schema + content-addressed dedup, fail-closed quarantine with separable reason codes.
  These defeat a malicious edge forging a bundle, replay/duplication, bundle-as-code exploitation,
  and malformed/oversized abuse. **Ship them; they are genuine.**
- **REAL with redefinition:** `orchestrated` becomes honest **only** when it means the mothership
  re-GENERATED the outputs (re-ran Tier-0 / re-drove the agentic loop) — covering the deterministic
  backbone for reachable endpoints/local weights.
- **NEEDS HARDWARE:** "the blessed code/harness/engine actually executed," "the served bytes are the
  claimed model" (self/local), and verified speed — all require a TEE measuring **execution**, and a
  full-loop quote (engine+weights+tools co-measured) to stop fabrication/proxy-shim/cherry-pick.
- **IMPOSSIBLE even with hardware:** identity-of-bytes for a remote third-party API model — the
  weights never enter your enclave (`attestation.md` Layer 3 ceiling); the enclave can attest
  "I sent these bytes to that URL and recorded the reply," not "that URL served model M."
```

---

## 10. End-to-end injection analysis — can the mothership verify & run the submit script?

> Answers the direct question: *"Can the mothership hash-verify the submit script itself and
> execute the verified submit script server-side, so there's no room for a malicious injection
> that spoofs a valid signature at any layer?"* Grounded in `mvp/pod/aeon_submit.py` (the courier),
> `mvp/aeon/ingest.py` (the state machine), `mvp/aeon/attest.py` (signing/HF-ref), `suites/cases.json`
> (in-band answers). Nothing here contradicts §0–§9; it makes the courier question concrete.

### 10.1 Direct answer

| Sub-question | Verdict |
|---|---|
| **Hash-verify the submit script?** | **Theater.** The hash is reported by the host under suspicion (self-report recursion, §2.4). Worse, it's *moot*: the standard attack runs the **unmodified** courier — its hash already matches. You'd verify that an honest courier carried dishonest cargo. |
| **Execute the submit script server-side?** | **Right instinct, wrong unit.** `aeon_submit.py` is a **courier, not the cargo** — it `json.load`s an operator-authored `results.json` and signs it verbatim (`aeon_submit.py:92-96, 116-118`). Running it on the mothership relocates the *mailman*, not the *lie in the envelope*. The thing that must move server-side is **generation + scoring**, not transport. |
| **What server-side execution actually has to mean** | **Orchestrated re-generation** (§ P1): the mothership *produces the outputs itself* (re-runs Tier-0 in its gVisor sandbox / re-drives the agentic loop against a held-back key). Then the courier is irrelevant — there is nothing to "submit" because the mothership is the producer. |
| **Full closure for self/local-weight execution** | **Whole-loop hardware TEE** (§ P3) — quote binding image+rootfs+kernel+live process+loaded weights+tool sandbox+run nonce. |
| **Remote-API model identity** | **Impossible even then** (§9, §0). The weights never enter any enclave; best attainable claim is "I sent these bytes to URL X and got these back," never "URL X served model M." |

### 10.2 The reframe (this is the whole point)

The threat is **not** "prevent signature spoofing." Forging a signature without the private key is
infeasible, and the transport/ingest layer is **already closed** (real ed25519 over `_canon(bundle)`,
single-use server nonce, atomic claim — §9 "REAL today"). Adding more signing/verification layers
touches none of the actual risk.

The threat **is**: a **legitimately-keyed adversary signing fabricated content.** The operator owns the
box, self-generates their own ed25519 keypair (`attest._load_or_create_key`), enrolls it legitimately
(open signup, `uuid` row, no identity, no cost), and signs **made-up numbers** with a **genuinely-valid**
signature. Every gate — `SIG_OK`, `NONCE_OK`, `AUTH_OK` — passes, because the signature *is* valid. The
enrollment ritual authenticates **possession of a key**, never **provenance of the content that key signs.**

> Hashing or server-executing the courier cannot detect this, because **there is no spoof to detect.**

### 10.3 Per-layer injection table

For each layer: can fabricated content acquire a *valid* signature here? does the user's
proposal (hash-verify + server-side execute the courier) close it? what actually closes it? residual.

| Layer | Fabricated content gets a **valid sig**? | User-proposal closes it? | What actually closes it | Residual after best fix |
|---|---|---|---|---|
| **Key enrollment** (`ingest.enroll`, `attest._load_or_create_key`) | **Yes, trivial.** Self-generated key, proves possession of *a* key over a nonce; never proves provenance of signed content. No identity/cost/rate-cap in code. | **No.** Hole is key↔identity binding, upstream of any script. | Nothing makes it *true*; it's a **cost lever**: PoP/staked `owner_user_id` + rate caps + anomaly detection raise Sybil/burn cost. Truth closed only upstream by orchestrated-regen or TEE. | Funded adversary farms/rents PoP; key-sharing undetectable from key alone; binds key to *account*, never to *execution*. |
| **Submit script / courier** (`aeon_submit.py`) | **Yes, trivial.** Signs whatever `results.json` it's handed; attack uses the **unmodified** script with a hand-authored input. | **No** (this layer *is* the proposal). Hash = self-report recursion + moot (unmodified); server-exec = re-POST operator cargo from a new host. | Move **generation** server-side (orchestrated-regen). Courier's honest ceiling = authorship + integrity-in-transit + replay, which it already delivers. | Tier-1/creativity not re-derivable; remote-API identity; "blessed harness ran" stays self-reported without a quote. |
| **Generation / engine / served bytes** (`raw_output`, `attest.model_attestation`) | **Yes, trivial** — 3 ways: hand-authored `raw_output`; localhost rewrite-proxy; cheaper-model swap under a strong HF ref (`attest.py:166` `claim_only`). | **No.** Hash = theater; server-exec moves *transmission* not *generation* — bytes were authored on the operator's box first. | **Local/open weights:** orchestrated-regen (server PULLs/re-loads `weights_sha256`, re-generates → `TIER0_MISMATCH` unfakeable). **Owned engine:** + TEE co-measuring engine+weights+tools. **Opaque API:** impossible. | Remote-API identity never closes; non-owned engine identity advisory; Tier-1 subjectivity carve-out. |
| **Scoring / answer keys** (`evaluators.py`, `suites/cases.json`) | **Yes, cheapest in the stack.** Suite ships `eval` with answers **in-band** (`numeric_tolerance.value`, `exact_match.value`, `closed_set.answer`, `unit_test.test`). Author `raw_output` to satisfy public checkers; never call a model. | **No.** Fabrication happens *before* the courier; verifying/relocating transport never touches *where scores came from*. | **Sealed holdout** (keys mothership-only, resolved at ingest) → raises trivial→real-inference. **Mothership re-generation** → record-eligible. **Canary cases** → perfect recall without elicitation is statistically impossible. | Low-entropy answers guessable; holdout exhaustion over many submissions (finite 146-case suite); remote-API identity; best-of-N selection invisible. |
| **Transport + ingest** (the "CLOSED" layer) | **Yes — but not via this layer.** Real ed25519 + single-use nonce + atomic `claim_pod_run` make MITM/edge-compromise drop/corrupt but **never forge/mutate**. The only valid-sig fabrication is the legitimately-keyed operator (the documented `self_reported` path). | **No, and barely touches it** — server already authenticates the *bundle* by signature regardless of which client serialized it. | This layer is **already software-closed and correct.** The *content* threat is out of scope here — closed only by orchestrated-regen / TEE. | Open unauthenticated `enroll` (Sybil); non-atomic `bump_key_fail` revoke path (minor evasion); in-memory `_challenges`/SQLite atomicity assume single process (deployment caveat). |

### 10.4 Provenance vs execution — the category error at the heart of the question

| Reproducible-build + cosign **DO** prove | They **NEVER** prove |
|---|---|
| **Artifact identity / provenance:** the published `aeon_submit.py` (and harness/image) bytes == the audited CI-built open source; third-party-rebuildable to the same digest; mothership ingest allowlists only blessed digests. **Software-real, needs no hardware.** | **Execution:** that the operator *ran* those bytes, on the claimed model, producing those numbers. The genuine signed courier, run **unmodified**, faithfully signs a hand-authored `results.json` or proxy-rewritten output. Blessed bytes, forged inputs. |

"Verify the submit script via hash" **is** reproducible-build provenance — artifact identity, the same
self-report recursion demoted in §2.4. Cosign verifies *bytes-on-a-registry*; nothing verifies *what
those bytes computed or talked to*. That gap is exactly why the answer to the headline question is "no."

### 10.5 Surviving attacks despite the full software stack (cheapest first)

Stack assumed deployed: P0 signed ingest + orchestrated-regen (deterministic backbone, reachable) +
sealed holdout + canaries + cross-operator reproduction + anomaly detection + staked PoP +
reproducible-build provenance. **No TEE.** The courier proposal closes **none** of these.

| # | Attack | Cost | Top defense it beats | Dies only under TEE? |
|---|---|---|---|---|
| 1 | **Remote-API model-identity swap / rewrite-proxy** (serve a stronger/cheaper/contaminated model behind your URL) | trivial | orchestrated-regen (authenticates the *exchange*, never *identity*) | **No — irreducible.** Weights never enter any enclave. |
| 2 | **Best-of-N with a real cheaper model** (run cheap, sample many, submit only passing transcript) | trivial–moderate | sealed holdout (selection is invisible in the final bundle) | Partial — local self-hosted only; remote-API variant survives. |
| 3 | **Sybil the consensus panel** (farm N PoP creds, run N "independent" operators, manufacture agreement) | moderate (N × stake) | cross-operator reproduction + PoP (measures *agreement*, not *truth*) | Survives where target unreachable; TEE closes for local-weight targets. |
| 4 | **Tier-1 / judge / creativity-axis inflation** (game the rubric on non-re-derivable axes) | moderate | orchestrated-regen (only covers the deterministic backbone) | **No** — a quote can't make a subjective score objective. |
| 5 | **Un-hostable large open weights** (claim 200B+ the mothership can't re-run; submit quantized/distilled) | moderate | orchestrated-regen (unavailable *by capacity* → falls back to self-report) | **Yes — TEE closes (if reachable):** quote over loaded weights binds claim to execution. |
| 6 | **Hand-authored JSON** (baseline: never run a model, satisfy checkers, sign with legit key) | trivial | sealed holdout + regen | No — regen closes it for reachable/local; residual = holdout exhaustion + unreachable targets. |

**The cheapest path to a fabricated #1 is attack #1 — remote-API model-identity swap — at trivial cost,
and it survives every software layer AND a whole-loop hardware TEE.** You don't fabricate scores; you
submit *genuine* high scores from a model you misrepresent. A TEE meaningfully closes only #5 (and
reinforces #2/#3 for *local-weight, reachable* targets).

### 10.6 Strongest software-only defense-in-depth shippable now

Use **together** — each is trust-but-verify / tamper-evident, **not proof**:

| Defense | Raises forgery from → to | Honest limit |
|---|---|---|
| **Sealed answer-key holdout** + server-side checker resolution | author-JSON → must actually elicit an answer | low-entropy answers still guessable; doesn't bind output to the *claimed* model |
| **Canary cases** (never-public answers) | blind fabrication caught (~chance recall) | statistical; a sophisticated operator who *distinguishes* canaries evades; small fraction bounds power |
| **Cross-operator reproduction** | lone fabricator must match honest cluster (gains nothing) or Sybil the panel | measures agreement ≠ truth; needs ≥N independent owners of the target |
| **Anomaly detection** (speed-for-hardware, score-vs-class, impossible determinism) | careless fabrication caught | **clean in-envelope fabrications never trip the fail counter**; outlier filter only |
| **Proof-of-personhood / staked enrollment** | fabricate-then-burn-a-key & Sybil from trivial → costly | first-offense unchanged; PoP rentable/farmable at the margin |
| **Reproducible-build provenance** (cosign, digest-pin) | proves *which courier bytes* | proves provenance, **never execution** (§10.4) |

Collectively these make forgery **expensive and likely-caught** — robust tamper-evidence, **not** soundness.
The only conversions of "expensive" → "sound" are moving **generation** to the mothership (`orchestrated`,
deterministic backbone, reachable targets only) or a whole-loop **TEE** (`attested`) — and remote-API
model identity leaks past even those.

### 10.7 Bottom line

Verifying and server-side-executing the submit script closes **zero** layers, because the script is a
**courier** that signs operator-supplied numbers with a **legitimately-owned key** — there is no
signature spoof anywhere in the chain to close. The correct unit of "server-side execution" is the
**generation + scoring loop**, not the transport script; that *is* orchestrated re-generation. The
honest posture the rest of this doc already prescribes stands: deterministic backbone for reachable/
local targets → `orchestrated`; whole-loop quote → `attested`; **remote-API entries are permanently
`⚠️ claim` and must never gate a record board.**
