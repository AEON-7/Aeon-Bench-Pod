## Resilience & reliability

> v0.2 hardening of the failure-and-recovery surface. The guiding principle: a **Run is a long-lived, externally-driven saga, not a fire-and-forget task**. Postgres is the single source of truth; Redis and MinIO are caches/transport that must be reconstructable or recoverable. Every default here must be operable by a 1–3 person self-hosting team — so we ship *secure, durable defaults* and gate heavyweight machinery behind explicit opt-in tiers.

This section resolves SRE-01..08, DATA-03, OPS-01, and the durability half of OPS-05, plus the offline/partition concerns raised by the red-team "deploy anywhere" scenarios. Security-of-results (forgery, trust tiers) is handled in the identity/scientific-validity sections; here we only guarantee that *whatever was legitimately measured survives crashes, restarts, partitions, and disk loss exactly once*.

---

### 1. Durable, resumable run lifecycle

**v0.1 decision.** §4.2/§13: "Long work runs on workers (Dramatiq/Celery + Redis)." §4.7: Redis holds "queue + ephemeral run/progress state." §9 shows a multi-stage lifecycle (launch → stream → ingest → judge fan-out → normalize → Elo → snapshot). `runs.status` exists but is never defined as a state machine; §16 lists cost caps as an open TODO that implicitly needs authoritative run state.

**Problem.** A Run spans 10 categories × many cases × concurrency sweeps × judge passes = minutes to hours. Dramatiq gives at-least-once message delivery with no workflow state, no fan-out/fan-in barrier, no run-scoped cancellation, and no "where is run X stuck" visibility. If a worker or the API restarts mid-run, an in-flight message is either lost (acked early) or blindly re-run from scratch (acked late). With progress living only in Redis, a Redis restart without AOF, a `maxmemory` eviction, or a `FLUSHALL` erases the authoritative picture of run state. There is no per-case completion ledger a recovering worker can read to skip done work.

**v0.2 resolution.** Make **Postgres the authoritative run state machine; Redis becomes pure transport/cache that may be lost without data loss.**

- **State machine.** `runs.status` becomes an explicit enum: `queued → profiling → running → judging → normalizing → finalizing → {succeeded | partially_scored | failed | canceled}`. Every transition is a single Postgres transaction. Add `runs.heartbeat_at` (server-receipt timestamp).
- **Per-case ledger already exists — make it load-bearing.** `results(run_id, case_id, …, status)` is the completion ledger. Resumption is *"skip every case with a terminal `results` row,"* a single indexed query — no bespoke reconciler engine, no replay log. Judge work resumes the same way: `judge_scores` rows that already exist are skipped.
- **Reconciler.** A Dramatiq periodic actor (cron-style, runs every 60s — reuses the queue we already have, zero new infra) scans non-terminal runs whose `heartbeat_at` is older than **3× the heartbeat interval** (default heartbeat 30s → stale at 90s), transitions them to `failed`/`partially_scored`, releases any RunnerProvider handle (Mode A), and surfaces the failure in the run console. Operators re-launch; the run resumes by skipping done cases.
- **Hard rule (write it into the design):** Dramatiq only *triggers* work; it never *holds truth*. Any state a recovering process needs must be in Postgres before the triggering message is acked.

*Rationale: the durable per-case ledger is already in the schema — formalizing the state machine + a staleness reaper turns "lost the run" into "resume the missing 4%" for a half-day of work and no new services.*

**REJECTED — Temporal / Hatchet / Restate as the v0.2 orchestrator (OPS-01).** A durable workflow engine gives resumability and timers "for free" but adds a second stateful service (its own Postgres schema, backups, upgrades) and a new programming model (workflows/activities, determinism constraints, versioning) for a 1–3 person team. Decisively, **the long externally-driven segment (Mode B probe phoning home) is not Dramatiq-orchestrated at all** — the probe drives itself and POSTs to ingest — and the post-submit scoring segment is a short, fully re-derivable computation over data already durable in Postgres. There is no human-gated wait and no irreversible external side effect inside the orchestrated span, so a workflow engine is solving a problem we don't have. Captured as a deferred ADR: revisit only if genuinely long human-gated stages appear.

---

### 2. Idempotent, exactly-once ingestion

**v0.1 decision.** §14: `POST /runs/{id}/results` ("batched"), `/artifacts` ("resumable"), `/manifest`, `/progress`. §4.3 calls the probe "idempotent" and the reporter does "resumable upload." §10 signs and verifies results before ingest. The §8 schema has **no uniqueness** on `(run_id, case_id)`, no unique manifest-per-run, and no dedupe key anywhere.

**Problem.** At-least-once delivery (a probe outbox, a lost 200 ACK, a 502 from a restarting API, a network partition) **meets a non-idempotent SQL sink.** Signing proves *authenticity*, never *uniqueness* — a faithfully retried signed batch is still a valid signed batch the server happily inserts again. Result: duplicate `results` rows silently inflate `category_scores` denominators/numerators (pass@k, n_cases, judge medians), duplicate judge jobs double the frontier-API bill, and a re-POSTed manifest can create two `environments` rows with conflicting signatures. The corruption is silent and undetectable after the fact — the worst possible failure for a product whose entire value is trustworthy numbers.

**v0.2 resolution.** Make ingest **idempotent end-to-end via natural keys** (this is table-stakes and depends on nothing in §1):

- `UNIQUE(run_id, case_id, attempt)` on `results`. The `attempt` column distinguishes legitimate pass@k re-samples within one run from retries; the natural key (not a separately transmitted UUID) does the dedupe. Ingest is `INSERT … ON CONFLICT (run_id, case_id, attempt) DO NOTHING`. The idempotency key must exclude nondeterministic fields (wall-clock, `speed_json` timings).
- `UNIQUE(run_id)` on `environments`. Manifest re-POST is `ON CONFLICT DO NOTHING`; if the incoming `manifest_signature` *differs* from the stored one, **reject loudly** (signals a tampering or bug, not a benign retry) rather than silently keeping the first.
- **Judge enqueue only on actual insert.** Enqueue a judge job only when the results UPSERT actually inserts a row (`… RETURNING` / rows-affected). This kills the double-spend for free once the unique constraint lands.
- **Artifacts dedupe by content hash.** Store-by-digest using `artifacts.hash` (already in §8): the MinIO object key *is* the sha256, so a re-upload of identical bytes is a no-op. (See §3 storage and the content-addressing in the schema review.)
- **Progress is idempotent** by `UNIQUE(run_id, stage, seq)`.
- **Finalize reconciliation check:** at `finalizing`, assert `stored case count == suite case count`. On mismatch, mark the run `partially_scored` and refuse to publish a composite computed from a partial denominator — this also catches *dropped* batches, not just duplicates.

*Rationale: a handful of unique constraints + `ON CONFLICT` clauses (one Alembic migration) removes an entire class of silent, after-the-fact-undetectable score corruption and judge over-spend, with no new tables and no new failure modes.*

**REJECTED — dedicated `ingest_idempotency(key, response_hash, …)` table + mandatory client-generated UUIDv7 keys (DATA-03).** Warranted when endpoints lack a natural key or must return byte-identical cached responses to non-idempotent POSTs (payment-style "create"). Here every ingest endpoint has a natural key, so the table is redundant ceremony — extra surface to maintain and GC for benefit the constraints already deliver. Add a client idempotency key only if a future endpoint genuinely lacks a natural key.

---

### 3. Offline / partition handling — store-and-forward on the probe

**v0.1 decision.** §3 Mode B / §4.3: probe "phones home"; reporter does "resumable upload"; suite loader can use "a bundled copy for air-gapped runs." §9 Mode B ends at "submit results."

**Problem.** "Resumable upload" only helps a *single interrupted transfer*; it does nothing for "the mothership was unreachable for 3 hours" or "the container restarted mid-run." A standalone probe on a DGX Spark behind an intermittent or truly air-gapped link runs for hours, then tries to submit — with no specified durable local buffer, a submit-time outage or a probe restart discards hours of GPU time. "Air-gapped" is listed as supported but the store-and-forward mechanics that make it real are absent. This also bites Mode A: §9 only "submits results" at the very end, so an API/Postgres blip at submit time torches a completed multi-hour run regardless of mode.

**v0.2 resolution.** Give the probe a **local durable outbox**, drained by a background shipper:

- **Persist-before-network.** The instant a case result or manifest fragment is produced, append it to an on-disk outbox **before any network attempt.** Default: append-only NDJSON + `fsync` + an acked-offset marker (simplest for the slim probe image; SQLite-WAL is an acceptable alternative left to implementation). Bound the outbox (size cap + rotation); on disk-full, fail the run with backpressure rather than silently dropping.
- **Shipper.** A background loop drains the outbox to the existing ingest API with **exponential backoff + full jitter** (base 1s, cap 5 min), carrying the §2 natural idempotency keys, deleting entries only after a 2xx canonical ACK.
- **Crash recovery for free.** On probe restart, re-run only cases absent from the outbox/acked set. A single case interrupted mid-execution is simply re-run — we explicitly do **not** checkpoint sub-case state.
- **Air-gap (M6).** The same outbox serializes to a signed bundle the operator sneakernets in via a new `POST /api/runs/{id}/import`, which lands on the **same idempotent ingest path** — so a sneakernet import dedupes exactly like a network retry.

*Rationale: the standard store-and-forward pattern, reusing the idempotent ingest endpoints, is the difference between "deploy anywhere / air-gapped" being real versus aspirational — at the cost of a few hundred probe-side lines and bounded local disk.*

**Phasing & dependency.** The outbox + jittered shipper + idempotent resume ship in **M1–M2** (alongside §2, which it hard-depends on). The signed-bundle sneakernet `/import` path ships in **M6** with the air-gapped/local-judge work.

---

### 4. Queue durability, isolation & backpressure

**v0.1 decision.** §4.7: Redis is "queue + ephemeral run/progress state + pub/sub for live logs." §4.2: all long work on one Dramatiq pool. §16 flags judge cost/drift but not judge *availability*.

**Problem.** Three distinct gaps. (a) Redis with no AOF loses queued work on restart. (b) A single shared worker pool means a judge brownout (429/5xx/timeout from a frontier API, multiplied by position-swap ×2 and multi-sample ×3–5) becomes a retry storm that starves *deterministic* Math/Coding ingest/normalize work that needs no judge at all. (c) There is no dead-letter handling: a poison case (a prompt that always trips a content filter and 400s) either retries forever or is silently dropped, and no defined degradation outcome exists — does the run publish deterministic scores with quality pending, or fail entirely?

**v0.2 resolution.**

- **Redis durability:** `appendonly yes`, `appendfsync everysec`. Pair with the §1 invariant: nothing in Redis is sole source of truth, so even total Redis loss is recoverable from Postgres (re-enqueue from the per-case ledger). Set `maxmemory-policy noeviction` on the queue instance so the broker errors loudly rather than silently dropping jobs under memory pressure.
- **Queue isolation:** judge jobs get their own Dramatiq queue with **bounded concurrency**, separate from ingest/normalize. A judge stall can never steal slots from deterministic work. Deterministic + speed scores (scored in-probe, §4.3) **always publish** — they never touch the judge.
- **Per-backend rate gate:** a token-bucket / lightweight failure-rate gate per judge backend, respecting provider RPM/TPM. On sustained failure (e.g. N consecutive 5xx/429 in a window) the gate opens: stop hammering, let the run proceed to `partially_scored` with judge dimensions marked pending and back-filled later.
- **Error classification + DLQ-lite:** retry `429/5xx/timeout` with backoff and an explicit `max_retries`; **do not retry** `4xx`/content-filter — fail immediately. Reuse `results.status` with a `judge_failed` value (no separate `judge_dlq` table) plus an admin list filter for triage/back-fill.
- **Budget hard-stop:** honor the §16 per-run *and* per-day budget cap (tokens + dollars), checked by the dispatcher *before* each judge batch; exceeding it halts further judge dispatch for that run and opens the gate — graceful degradation, not a runaway bill.

*Rationale: queue isolation + error classification + a degraded state + the budget hard-stop are a few config knobs and one status value; they convert "one frontier outage stalls the whole platform" into "quality scores arrive late, everything objective is unaffected."*

**REJECTED — heavyweight `pybreaker` + a dedicated `judge_dlq` table with its own admin surface (SRE-04).** A self-hosted batch system with operationally-visible runs doesn't need a full circuit-breaker library or a parallel DLQ subsystem; an in-worker failure-rate gate plus a `judge_failed` status on the existing `results` table delivers the same containment for far less code and ops burden.

---

### 5. Live log / progress fan-out

**v0.1 decision.** §4.7 "Redis … pub/sub for live logs"; §4.1/§9 WebSocket/SSE for live progress + streamed logs; §14 `WS /api/runs/{id}/stream`.

**Problem.** Redis pub/sub is fire-and-forget: a slow or reconnecting browser tab drops messages with no replay (gap in the log). FastAPI WebSocket handlers are per-process in-memory; a future second API replica won't see a pub/sub message unless every replica subscribes to every channel. High-frequency heartbeats + per-line logs can saturate the connection.

**v0.2 resolution.** Keep it simple and treat the live stream as **best-effort over a durable record**:

- **The authoritative log record is the MinIO log bundle** (already in §4.3 reporter and §4.7). The live stream is *allowed* to be lossy: on reconnect the run console backfills the gap from the persisted bundle. No stream-replay machinery needed.
- **SSE, not WebSocket**, for the one-directional progress/log feed: simpler, free auto-reconnect via `Last-Event-ID`, no per-process WS state. Launch/cancel already go through REST (§14), so bidirectional WS buys nothing here.
- **Coalesce:** probe sends stage/percent heartbeats at ≤2 Hz and ships logs in batched chunks, not per line (extend the §2 batching discipline to logs).
- **Document the scaling boundary:** live fan-out is single-replica in v2. Horizontal API scale-out requires either sticky-hash routing of a run's stream consumers or a switch to Redis Streams — gated to the Helm/multi-replica milestone, not built now.

*Rationale: the durable MinIO log bundle already makes the stream disposable; SSE + coalescing is a net simplification, and writing down the single-replica boundary prevents a silent multi-replica surprise.*

**REJECTED (for v2) — Redis Streams with consumer groups + `MAXLEN`.** It solves replay/backpressure/multi-replica in one primitive, but adds memory tuning and a trim-eats-history failure mode to solve a problem already solved by the persisted bundle and not yet in scope (single-compose default has one replica). Deferred to the milestone that actually introduces multiple replicas.

---

### 6. Graceful degradation — explicit partial-success semantics

**v0.1 decision.** Implicit: a run either succeeds or is stuck in `running`. No defined partial outcome.

**Problem.** Many failures are *partial*: a judge outage, a poison case, a dropped batch, a stale-heartbeat probe death. Without a first-class partial state, the system either wedges (waits on a corpse) or publishes a misleading composite computed from incomplete data.

**v0.2 resolution.** First-class degradation, wired through the §1 state machine and §4 isolation:

- **`partially_scored` is a terminal state**, not an error. Deterministic + speed categories publish; judge-dependent categories are marked **incomplete** (`judge_failed` / pending), never computed from a partial sample set.
- **The composite is suppressed or badged** when coverage is incomplete: the finalize reconciliation (§2) refuses to publish a full composite over a partial denominator; the UI shows "composite over 8/10 categories" rather than a falsely precise number.
- **Readiness gating** (see §7): ingest returns `503` (retryable) — never `500` — when a dependency (Postgres/Redis/MinIO) is down, so probe backoff behaves correctly instead of retrying a hard error forever.
- **Resource-limit kills are a distinct status, not a wrong answer.** A sandbox killed for OOM/PID/wall-clock surfaces as `killed: resource_limit` and is excluded from pass@k scoring rather than counted as a failed test (a correctness bug if mis-scored).

*Rationale: making partial success a designed outcome stops both wedged runs and silently-wrong composites, and keeps back-fill possible because no judge result is fabricated from incomplete data.*

---

### 7. Liveness, readiness & self-healing

**v0.1 decision.** §14 `POST /progress` is a "heartbeat"; §8 `probes.last_seen` exists; §4.3 health-checks the *target*. Nothing consumes the heartbeat; no readiness gate; no zombie-run reaper.

**Problem.** A heartbeat with no consumer is decoration. An OOM-killed, power-lost, partitioned, or reaped probe leaves its run in `running` forever — no timeout, no resource release, no alert, leaderboard waiting on a corpse. This is routine in Mode B ("deploy anywhere") where the mothership holds no container handle. There is also no readiness gate, so a probe can submit into a half-up mothership and get a `500` it retries forever.

**v0.2 resolution.**

- **Reaper on heartbeat *staleness*** (the §1 reconciler): runs with no progress POST in 3× the interval (90s default) are transitioned to `failed`/`partially_scored`. **Reap on staleness, not absolute duration** — a probe mid-sweep keeps heartbeating, so a legitimately long run is never false-failed. Probes **must heartbeat during long stages**, not only at boundaries.
- **Server-side receipt timestamps**, never probe-reported wall-clock, for freshness — air-gapped probes drift.
- **`/healthz` (liveness)** and **`/readyz` (readiness: Postgres + Redis + MinIO reachable + migrations current)** on the API. Ingest returns `503` when not ready.
- **Probe offline marking** from stale `last_seen`, reflected in the run console.
- **Per-case inference timeout + whole-run wall-clock budget** (the latter overlaps §16's per-run budget cap — build them together).

*Rationale: the queue substrate the reaper needs already exists; staleness-based reaping plus a readiness gate fixes the predictable Mode-B steady-state (stuck runs) and a concrete retry-storm bug for a few lines of code.*

**REJECTED — per-stage timeouts driven by maintained "expected stage durations" (SRE-06).** Expected-duration tables for inherently variable benchmark stages are fragile and are the main source of false-positive failures; staleness-based reaping + the whole-run wall-clock budget dominate them at lower cost.

---

### 8. Sandbox resource limits (DoS containment)

**v0.1 decision.** §4.3/§10/§4.3.7: model-generated code runs "resource-limited" and "killed on timeout."

**Problem.** "Killed on timeout" addresses wall-clock but not a **fork bomb** that exhausts PIDs *before* the timeout fires, nor memory pressure that OOM-kills the wrong process, nor disk exhaustion (no time-based remedy). On a Mode-A LocalDocker DGX the probe is co-located with mothership containers, so a self-inflicted PID/OOM/disk storm can wedge an expensive host or crash the dashboard mid-run.

**v0.2 resolution.** Concrete kernel-enforced limits on every sandbox, per-suite-tunable, with these defaults:

- cgroup v2 `pids.max=256` (defeats fork bombs the wall-clock can't catch), `memory.max` + `memory.swap.max=0` with OOM scoped to the sandbox cgroup, `cpu.max` quota, read-only root + size-capped tmpfs scratch (256 MB) so disk can't be exhausted, `ulimit nofile`, **both** a CPU-time rlimit **and** a wall-clock watchdog.
- **SIGKILL the whole cgroup, not PID 1** — orphaned children must die.
- **Max concurrent sandboxes per host** config knob (default `min(N, cores/2)`); the concurrency *sweep* is config-bounded.
- A resource kill surfaces as the distinct `killed: resource_limit` status (§6), never scored as a wrong answer.

*Rationale: standard cgroup limits supported out-of-the-box by the runtimes already named (nsjail/Docker/gVisor) — a config block, not new infrastructure — that bounds a self-inflicted-DoS blast radius on the operator's own GPU host.*

> Note: stronger *isolation* (gVisor/microVM floor, egress default-deny, no docker.sock) belongs to the security/execution section. This subsection is purely about availability — keeping a hostile-or-pathological workload from wedging the host.

---

### 9. Single-points-of-failure: backups, PITR & DR

**v0.1 decision.** §13: "one `docker compose up`" → one Postgres, one Redis, one MinIO, one API host. No HA, backup, PITR, replication, or restore-drill anywhere; §16 omits data durability.

**Problem.** The product's core promise is durable, reproducible history, yet every stateful store is an undefended SPOF. Postgres holds the run state machine and all hash references into MinIO — losing it loses everything, with realistic RPO = "whenever the operator last manually dumped" and RTO = "rebuild from nothing." **MinIO holds the *only* copy** of artifacts, transcripts, signed manifests — referenced by hash in Postgres — so a MinIO disk loss orphans every result and breaks the reproduce-command and arena. Silent, permanent loss of exactly the artifacts the platform exists to preserve.

**v0.2 resolution — single-node-but-recoverable** (HA is explicitly a non-goal):

- **Off-host is the hard constraint.** Backups must **never** land in the same MinIO instance they protect — a host/disk loss would destroy the DB and its only backups together. Default backup target is an external volume mount at minimum, an external S3/B2 bucket as the documented default.
- **Postgres:** compressed **nightly `pg_dump` shipped off-host** is the default (the DB holds scores/manifests/run rows, not bulk blobs — a few hundred MB; ~24h RPO is adequate and the data is partly re-runnable). Continuous **WAL archiving / PITR (pgBackRest or wal-g)** is an **optional hardening tier**, not the default.
- **MinIO:** enable **bucket versioning** + a scheduled **`mc mirror` / `rclone` to a second, off-host target** (or native replication for cloud S3). Artifacts are write-once content-addressed, so a consistent restore only requires the object backup to be a **superset** of objects referenced by the latest DB dump — back up objects at-or-after the DB dump; no distributed snapshot needed.
- **Redis:** `appendonly yes` (§4); but per §1 nothing in Redis is sole source of truth, so its durability is a convenience, not a correctness requirement.
- **One backup sidecar container** in compose (cron-driven dump + mirror) and a **one-command, documented, *tested* restore.** An untested backup is not a backup — a restore drill is a required step, not a footnote.
- **Stated targets in the design:** RPO ≤ 24h (nightly off-host dump + WAL optional for tighter), RTO bounded by tested restore time. Add data-durability to §16.
- **Storage abstraction (cheap insurance):** program strictly against the S3 API (no MinIO-admin-specific calls) and expose a config knob to point at cloud S3/R2/B2 for teams that don't want to operate storage. This is essentially already true (§4.7 stores only refs+hashes) — confirm it in one ADR.
- **Integrity reconciliation (deferred, M4+):** a periodic `fsck`-style job reconciling Postgres `*_ref` rows against actual MinIO objects to detect drift early. Content-addressed hashes already allow lazy on-read verification, so this is a nicety, not a launch blocker.

*Rationale: cheap off-host backups + versioning + a tested restore give a bounded, honest RPO/RTO without operating a clustered control plane — the right trade for a single-node-self-host-first product whose data is partly regenerable.*

**REJECTED — full HA (Patroni / Postgres replicas, MinIO distributed mode, Redis Sentinel) and mandatory continuous-WAL PITR.** HA is too heavy for the stated small-team context and undermines one-compose simplicity; mandatory PITR is over-prescriptive when a nightly off-host dump suffices for this data shape. Both remain documented optional tiers for the rare large operator.

---

### 10. Conflicts resolved & explicit tradeoffs

- **SRE-01 (durable run state) vs OPS-01 (workflow engine).** Both diagnose the same gap; they conflict on remedy. **Resolved toward the Postgres-state-machine + reconciler** and against Temporal, because the heavy externally-driven segment isn't queue-orchestrated and the orchestrated segment is short and re-derivable. *Tradeoff:* we hand-roll retry/resume/timeout logic that a workflow engine gives free; we accept that because the saga has no human-gated waits or irreversible side effects, making the hand-rolled version small and bounded.
- **SRE-02 (idempotency) is the precondition for SRE-03 (outbox).** The probe outbox re-ships after ambiguous ACKs; without the §2 natural-key dedupe that would double-count. They ship together in M1–M2.
- **Single-compose simplicity vs durability (OPS-05).** *Tradeoff:* we add a backup sidecar and require an off-host target, mildly complicating the pure one-compose story. Accepted: silent permanent data loss is categorically worse than one extra container and a documented external bucket.
- **Live-stream richness vs operability (SRE/PERF live-logs).** *Tradeoff:* the live log can show a reconnect gap; we accept cosmetic gaps because the authoritative record is the durable MinIO bundle, and we avoid Redis-Streams machinery the single-replica default doesn't need.
- **Tight sandbox limits vs legitimate heavy tests.** *Tradeoff:* default `pids/mem/cpu/tmpfs` caps can false-fail a genuinely heavy compile or memory-hungry numeric test. Mitigated by per-suite-tunable limits and the distinct `killed: resource_limit` status so a limit kill is never silently scored as a wrong answer.

---

### 11. Operational summary (defaults vs hardening tiers)

| Concern | Secure/durable default (small team) | Optional hardening tier |
|---|---|---|
| Run state | Postgres state machine + per-case ledger + 60s reconciler | — (Temporal only if human-gated stages appear) |
| Ingest | Natural-key `ON CONFLICT` dedupe + finalize reconciliation | client idempotency key (only if a non-natural-key endpoint appears) |
| Offline/partition | On-disk NDJSON outbox + jittered shipper | signed-bundle `/import` sneakernet (M6) |
| Queue | Redis AOF `everysec`, `noeviction`; isolated judge queue; rate gate; budget hard-stop | — |
| Live logs | SSE + ≤2 Hz coalescing over durable MinIO bundle | Redis Streams (multi-replica milestone) |
| Liveness | Staleness reaper (3× heartbeat), `/healthz` + `/readyz`, `503` not `500` | — |
| Sandbox DoS | cgroup v2 pids/mem/cpu/tmpfs caps, whole-cgroup SIGKILL | (isolation tiers in security section) |
| Backups/DR | Nightly off-host `pg_dump` + MinIO versioning + `mc mirror` + tested restore; RPO ≤ 24h | WAL/PITR (pgBackRest/wal-g); cloud S3; fsck reconciler (M4+); HA (large operator) |

*Net: every guarantee above is reachable with a handful of unique constraints, one state-machine enum, one periodic reconciler actor, a probe-side outbox, AOF + one isolated queue, and a backup sidecar + off-host target. No new stateful services are mandatory; the heavyweight options (workflow engine, Redis Streams, PITR, HA) are documented tiers a small team can adopt later under real pressure — not v0.2 requirements.*
