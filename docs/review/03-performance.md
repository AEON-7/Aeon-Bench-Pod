## Performance & scalability

This section hardens the v0.1 design along the axes that determine whether AEON Bench produces *trustworthy* numbers at *operable* cost: measurement isolation, judge economics, time-series volume, database hot paths, leaderboard recompute, realtime fan-out, artifact serving, and scale-out. The governing constraint is unchanged — a 1–3 person team must be able to `docker compose up` and operate the result — so every change below ships a **secure, sane default** and pushes heavier machinery into **optional hardening tiers** gated behind explicit triggers.

The throughline: AEON's product *is* the numbers. A measurement that is silently wrong is worse than a measurement that is slow, so the bar for "is the speed/quality figure honest?" is higher than the bar for "is the dashboard fast?". We spend complexity accordingly.

---

### 1. Measurement-accuracy isolation (load-gen vs. clock vs. monitor)

**v0.1 decision.** §4.3 runs the inference client, the concurrency sweep (1→N), the resource monitor (DCGM/nvidia-smi/psutil), the harness adapter, and — in probe-launched mode — the model server itself, all inside one probe process on one host, and reports TTFT / inter-token latency / p50/p95/p99 from that process.

**Problem.** The probe is simultaneously the load generator and the stopwatch. Under a high-concurrency sweep, Python's GIL serializes the asyncio loop that timestamps tokens, so measured inter-token latency absorbs probe-side scheduling jitter rather than pure server behavior. Worse, in probe-launched-on-DGX-Spark mode the vLLM server and the load generator share the box, so client-side CPU contention directly inflates the server's *apparent* latency — and the manifest faithfully records a reproducible-but-wrong number. (PERF-01, adjusted **high**.)

**v0.2 resolution.**
- **Separate the timing loop from everything else as OS processes, but keep it Python.** The load generator runs as its own process (`multiprocessing`), the resource monitor as a second, the harness/reporter as a third. Timing uses `time.perf_counter_ns()` (monotonic) recording per-request **send / first-byte / last-byte** explicitly; the monitor and harness never share the timing process. Where the platform allows, pin the load-gen process with CPU affinity (`os.sched_setaffinity` / `taskset`). *Rationale: takes monitor and adapter work off the timing path — the dominant, cheap source of jitter — without breaking the "one Python image everywhere" invariant.*
- **Mandatory warmup discard + clock-stabilization gate.** Discard the first `K` requests (default `K=5` per concurrency level) and block timing until GPU clocks stabilize (poll `nvidia-smi` clock/thermal until variance < threshold); record both facts in the manifest. *Rationale: removes cold-start and thermal-ramp contamination that otherwise lands in p50.*
- **Co-location honesty flag.** Add `runs.colocated_loadgen boolean` (trivially derivable: true whenever the target is `probe_launched` on the same host). When true, speed numbers are labeled **"co-located — lower bound on latency"** in the Reproducibility view and the leaderboard, and **co-located speed numbers are never cross-compared against 2-box numbers** in trend charts. *Rationale: a labeling problem, solved by labeling, instead of mandating a topology a small team can't always provide.*

**Rejected:** a dedicated Go/Rust load core, and a hard 2-box requirement for any published number (PERF-01's headline). The second-language binary breaks the single-image simplicity a small team depends on, and a 2-box mandate fights the DGX-Spark single-box convenience story. If high-concurrency fidelity ever becomes a hard requirement, **vendor a battle-tested generator** (vLLM `benchmark_serving`, GuideLLM, or llmperf) as a subprocess rather than hand-roll a timing core — deferred, not built.

---

### 2. Coordinated omission in the concurrency sweep

**v0.1 decision.** §6: "Throughput under concurrency (sweep 1→N concurrent requests); p50/p95/p99," with no stated load model.

**Problem.** A naive closed-loop sweep (N workers each firing the next request only after the previous returns) is textbook coordinated omission: when the server stalls, the generator stops sending, so the slow window is under-sampled and p99 comes out optimistic by up to an order of magnitude. There is also no defined behavior for 429/queue-full. (PERF-02, adjusted **high**.)

**v0.2 resolution.**
- **Label the load model per measurement; don't ban closed-loop.** A *labeled* closed-loop concurrency sweep ("hold N in flight") is a legitimate, industry-standard measurement (vLLM/sglang/llmperf all do it) and stays the default. The defect is mislabeling it as latency-at-offered-load, so the load model (`closed_loop` concurrency N vs. `open_loop` arrival rate λ) is stamped into the manifest and shown on every speed chart. *Rationale: closes the honesty gap without forcing an open-loop rig into M1.*
- **Account for errors; never silently drop.** 429 / queue-full / timeout are recorded as outcomes (success-rate plus their own latency bucket), not skipped. Dropping is a worse bias than CO itself. *Rationale: cheap, removes the largest correctness hole.*
- **Surface sample size.** Every p95/p99 is rendered with `n`; p99 over `n < 100` is badged **low-confidence**. *Rationale: `n_cases` already exists; this is presentation discipline.*
- **Optional open-loop tier (post-M1).** For the distinct "latency under offered request rate" question, offer a scheduled-departure open-loop profile (wrk2 / Gil Tene correction: latency measured from *scheduled* send time), gated behind a **mandatory `max_in_flight` safety cap** and reporting achieved-vs-intended rate so a fragile target isn't melted.

**Rejected:** mandating a full open-loop arrival-rate generator + HdrHistogram in v1 (PERF-02 headline). A fixed-base log-bucket histogram (or t-digest) is sufficient; the load-bearing fix is correct accounting + labeling, not the percentile data structure. Building an open-loop rig for M1 adds a real failure mode (unbounded queueing crashing the target) the team must operate.

---

### 3. Judge cost, throughput, caching & degradation

**v0.1 decision.** §4.5 does rubric scoring with position-swap (×2) and multi-sample median (×3–5); §16 lists per-run budget caps only as an open TODO; `judge_scores` has no dedupe key; §9 fans judge jobs onto the shared Dramatiq pool.

**Problem.** The judge is the dominant variable cost *and* the throughput bottleneck, and the design multiplies its calls (swap × samples × dimensions × cases × categories = tens of thousands of frontier-API calls per suite). There is no budget cap mechanism, no rate-limit/concurrency control, no cache, and no graceful degradation — a judge brownout becomes a retry storm on the shared worker pool that starves deterministic Math/Coding work that needs no judge. Poison cases (content-filter 400s) retry forever or vanish silently. (PERF-03 adjusted **high**; SRE-04 adjusted **high**.)

**v0.2 resolution — ship the cheap guardrails now, defer the heavy machinery.**

| Control | Spec | Rationale |
|---|---|---|
| **Per-run + per-day budget cap** | Token + dollar caps checked by the dispatcher **before each judge batch**; on exceed, the run pauses and is marked `partially_scored` with judge dimensions `pending`, surfaced in the run console. | The actual high-severity item — one misconfigured run = unbounded bill. |
| **Per-backend concurrency/rate semaphore** | A token-bucket gate per judge backend respecting provider RPM/TPM; judge jobs get their **own Dramatiq queue** with bounded concurrency, isolated from ingest/normalize. | Stops a judge stall from starving deterministic categories. |
| **Opt-in sampling** | Position-swap and multi-sample-N become **per-category opt-in, default OFF** for low-subjectivity dimensions. | Collapses the cost multiplier with zero new infrastructure — the single biggest raw saving. |
| **Provider prompt caching** | Anthropic prompt caching on the static rubric+reference prefix for the Claude judge path; no-op fallback for local judge. | Large constant prefix → near-free input-token cut. |
| **Content-addressed judge cache** | `judge_scores.content_hash = hash(judge_model+judge_version, rubric_version, normalized_prompt+output, sample_idx, swap_position)`; nullable column + unique index, **bypassable for calibration runs**. | Re-runs and unchanged-dimension re-scores become near-free; keyed by seed/position so it never masks the variance multi-sample exists to measure. |
| **Error classification + degradation** | Retry 429/5xx/timeout with capped backoff; **do not** retry 4xx/content-filter — mark `judge_failed` immediately. Deterministic + speed scores **always publish**; judge-dependent category scores render **incomplete**, never computed from a partial sample set. | Poison containment + honest partial results. |

The "DLQ" reuses `results.status` (`judge_failed`) + an admin list filter for back-fill — **no dedicated `judge_dlq` table**. The "circuit breaker" is a lightweight in-worker failure-rate gate (open after N consecutive 5xx/429 in a window, drop the run's remaining judge jobs to the degraded state) — **no pybreaker dependency**.

**Rejected:** the cheap-judge-then-escalate tier (PERF-03), a dedicated DLQ table + triage UI, and a heavyweight breaker library (SRE-04). A second judge model plus a disagreement threshold is a perpetual judge-quality-calibration burden — defer behind evidence the levers above are insufficient.

---

### 4. Time-series volume management (`resource_samples`)

**v0.1 decision.** §8: `resource_samples(run_id, ts, gpu_util, gpu_mem, gpu_power, cpu, ram)` on optional TimescaleDB; no sample rate, retention, rollup, or `gpu_index`.

**Problem.** (a) The row **cannot represent a multi-GPU box** — there is no `gpu_index`, yet §7 assumes DGX/8-GPU hosts. This is a correctness bug, not a perf nit. (b) Raw telemetry across multi-GPU at a useful rate grows unbounded (the classic self-host disk blow-up), and the leaderboard / J-per-token metric (§6) would read the hot raw table. (c) An ECharts line over a 2-hour run would pull hundreds of thousands of points and kill the client. (PERF-04 adjusted **high**; DATA-07 adjusted **medium**.)

**v0.2 resolution — cheap core now, Timescale machinery deferred behind a "when it gets hot" trigger.**
- **Fix the schema (P0).** Key becomes `(run_id, gpu_index, ts)`; `gpu_index` added. *Rationale: closes the multi-GPU correctness bug — a one-column fix.*
- **Pin the sample rate.** Default **1 Hz** (not 5–10 Hz); raise only on explicit request. *Rationale: 1 Hz is ample for run-level correlation and cuts row counts ~5–10×.*
- **Materialize the per-run summary at ingest.** A worker computes mean/p95 util, max power, and the **energy integral (total joules / J-per-token)** into the `environments` row when scores finalize. The leaderboard and trends **never touch the time-series**. *Rationale: permanently decouples the hottest read path from raw data — just arithmetic in the existing ingest worker.*
- **Raw series → MinIO Parquet, referenced from the manifest.** §7 already says "Timescale/MinIO"; store the full raw series as a compressed Parquet/Arrow blob (reproducibility evidence, not a query target), keep only rollups in Postgres. *Rationale: makes raw retention essentially free and bounded by object lifecycle.*
- **Server-side downsample for the detail chart.** Cap the chart at ~1–2k points via bucketed avg/max or LTTB; never ship raw points to the browser.
- **Defer (optional hardening, when row counts actually bite):** Timescale hypertable conversion, columnar compression (segmentby `run_id`, compress chunks > 7d), continuous aggregates, and `drop_chunks` retention (raw 30d, rollups indefinitely). With summary-at-ingest + MinIO raw blobs in place, a nightly `DELETE` (or `drop_chunks`) suffices and Postgres is no longer the raw system of record.

**Rejected:** mandating the full Timescale continuous-aggregate + compression + retention stack and a parallel non-Timescale partitioned schema in v0.1 (PERF-04/DATA-07 headlines). One rule replaces the dual-path schema: *if you don't run Timescale, store only per-run rollups in Postgres and the raw series in MinIO.* One well-trodden path, not two.

---

### 5. DB read/write hot paths, indexing & partitioning

**v0.1 decision.** §8 lists tables with no indexes, no FKs spelled out, and no partitioning; §4.1 implies timeseries joins, run-detail reads, and judge aggregation.

**Problem.** The query shapes are predictable and will full-scan without indexes, and ORM traversal (`results → judge_scores → artifacts`) is a classic N+1 when rendering run detail or recomputing category scores. (PERF-06 adjusted **low**; DATA-06 retention overlaps.)

**v0.2 resolution — boring indexes in the *first* Alembic migration, no premature partitioning.**
- **Composite indexes for the real access paths (ship from M1):**
  - `results(run_id, case_id)`
  - `judge_scores(result_id)` and `judge_scores(result_id, dimension)`
  - `category_scores(model_version_id, category_id, run_id)` for the §4.1 timeseries
  - `arena_votes(match_id)` plus **`UNIQUE(match_id, voter_ref)`** (doubles as the §4.6 anti-gaming dedupe constraint)
  - `runs(model_version_id, suite_id, finished_at)` for leaderboard/trend filtering
- **Declare the foreign keys** (missing from the abridged list; needed for retention cascades).
- **Kill the N+1**: batch-load `judge_scores` for all results of a run in one `selectinload` / `GROUP BY` during category-score recompute and run-detail rendering. *Rationale: the only change here with real teeth; a code convention, not infra.*

**Rejected:** partitioning `results`/`judge_scores` by `run_id` (PERF-06 itself recants this — one partition per run explodes partition count), and a read replica. Both are deferred behind a **measured trigger** (per-run query p95 or table-size threshold); if partitioning ever lands, range-partition by `runs.finished_at` week, never per-run. At small-team scale these are deliberate benchmark runs, not user-facing QPS.

---

### 6. Append-only score integrity & resumable run state

These two findings underpin every number's correctness, so they belong in the performance section even though they read as data-model items.

**Idempotent ingest (SRE-02 / DATA-03, adjusted critical / high).**
- **v0.1:** `POST /runs/{id}/results` is batched and the probe does resumable retrying uploads, but there is no uniqueness on `(run_id, case_id)` and judge jobs enqueue per-result.
- **Problem:** at-least-once delivery meeting a non-idempotent SQL sink → a single 502 or lost ACK double-inserts result rows, silently inflating `n_cases`/pass@k denominators and double-spending judge calls. Signing proves authenticity, not uniqueness.
- **v0.2:** `UNIQUE(run_id, case_id, attempt)` on `results` with `INSERT … ON CONFLICT DO NOTHING`; `UNIQUE(run_id)` on `environments` (one manifest/run, signature-checked on conflict); `UNIQUE(run_id, stage, seq)` on `progress`; artifacts dedupe by content hash; **enqueue judge jobs only when the upsert actually inserts a row** (via `RETURNING`), killing the double-spend for free. Add a **finalize reconciliation check**: stored case count == suite case count, else mark `partially_failed` rather than publishing a misleading composite. *Rationale: a one-migration fix that closes a silent, after-the-fact-undetectable corruption of the only deliverable.* **Rejected:** a separate client-transmitted idempotency-key + `ingest_idempotency` table — the natural key already lives in the payload.

**Durable, resumable run state (SRE-01, adjusted medium).**
- **v0.1:** "long work on Dramatiq+Redis"; Redis holds run/progress state.
- **Problem:** a mid-run restart re-runs or loses work; Redis without AOF is non-durable.
- **v0.2:** **Postgres is the source of truth** (`runs.status` + `results(run_id, case_id, status)` is already the per-case ledger); Redis is cache/transport/pub-sub only and may be lost. Resumption needs no reconciler loop — a restart skips cases with a terminal `results` row via the indexed query. Add `runs.heartbeat_at`; a sweep marks stale-heartbeat runs `failed/partially_failed`. Set Redis `appendonly yes`, `appendfsync everysec`. *Rationale: the worst case is re-spending bounded compute (capped by §3 budgets), not corruption — so name the authority and add a heartbeat, don't adopt Temporal.* **Rejected:** Temporal/Hatchet/Restate (SRE-01/OPS-01) — the saga has no human-gated waits or irreversible side effects; a durable workflow engine is a new stateful service for a 1–3 person team to operate.

**Anchored normalization to stop historical scores moving (SV-02 / DATA-01, adjusted high).** Make anchored (criterion-referenced) scoring the **default**, not "preferred": report Math/Coding/IFEval as raw pass-rate/pass@k (do not min-max an already-absolute %), normalize judge categories against the fixed rubric maximum. Min-max remains a labeled, non-canonical fallback for anchor-less categories. Stamp `anchor_set_version` / `rubric_version` on `category_scores` and make recomputes **additive** (`scoring_run_id` epoch + nullable `superseded_at`, partial index `WHERE superseded_at IS NULL`). *Rationale: pool-relative min-max silently rewrites every existing model's score when a model enters — corrupting the §11 time-series — and the design already prefers anchoring; this fix is mostly deletion.* **Rejected:** DB `BEFORE UPDATE/DELETE` immutability triggers + `REVOKE` on the worker role (DATA-01 headline) — the tamper-evident evidence already lives in content-hashed MinIO + signed manifests; triggers/REVOKE impose a permanent migration/admin tax for marginal gain.

---

### 7. Leaderboard recompute strategy

**v0.1 decision.** §11 re-ranks client-side from cached per-category scores (correct), but §8 *also* stores per-profile `leaderboard_snapshots(profile_id, computed_at, ranking_json)` and §9 workers "snapshot leaderboard" after every run.

**Problem.** Two contradictory strategies. Per-profile snapshots multiply with users × profiles; every new run or re-normalization invalidates all of them (especially under min-max); and since weights are applied client-side anyway, the snapshot rarely matches what the user sees. It's write amplification + a cache-coherency trap for the one metric that didn't need server precompute. (PERF-05 adjusted **medium**; DATA-08 adjusted **low**.)

**v0.2 resolution.**
- **Delete per-profile `leaderboard_snapshots`; remove "snapshot leaderboard" from the §9 worker flow.** The canonical artifact is the profile-**independent** per-`(model_version, category)` normalized `quality_score`/`speed_score` in `category_scores`. *Rationale: the composite `Σ(weightᵢ·scoreᵢ)` is a pure function of scores + weights; the client computes it.*
- **Serve one cached `GET /api/leaderboard`** returning the per-category score matrix plus active normalization anchors; cache keyed on `suite_version + max(run.finished_at)` (ETag/Last-Modified as an implementation detail). The browser/Zustand applies weights. *Rationale: matches §11's stated client-side re-rank with zero per-profile storage.*
- **Add a suite-version-scoped read model** `leaderboard_matrix(suite_version_id, model_version_id, category_id, quality_score, speed_score, computed_at, source_run_id)` with `UNIQUE(suite_version_id, model_version_id, category_id)` and an index on `(suite_version_id, category_id)`, upserted on score finalize. *Rationale: `category_scores` is keyed by `run_id`, so "current scores for suite version V" otherwise needs a live multi-join; the matrix is the clean key.* For small instances an even cheaper interim is fine: a "latest finalized run per model+suite" view + Redis cache invalidated on finalize.
- **SSR/SEO:** keep **exactly one** materialized snapshot for the default balanced profile (not one per user).

**Rejected:** importing a full `scoring_epoch` entity + structured `leaderboard_entries` to make shared links reproducible (DATA-04/08 headline). §11 only requires profiles be *shareable by URL* — a profile is a weights vector, so a shared URL re-ranks client-side from the current matrix. If a "pin a frozen ranking" feature is ever requested, a single `shared_leaderboards` row pointing at a frozen matrix copy in MinIO is far cheaper than a new immutable-epoch subsystem.

---

### 8. Realtime streaming fan-out

**v0.1 decision.** §4.7 Redis pub/sub for live logs; §4.1/§14 WebSocket/SSE for progress + streamed logs.

**Problem.** Redis pub/sub is fire-and-forget with no replay and no backpressure: a reconnecting tab drops log lines (a gap with no replay), a chatty probe can flood subscribers, FastAPI WS handlers are per-process in-memory (so multi-replica fan-out is unstated), and high-frequency heartbeats can saturate the event loop. (PERF-07 adjusted **low**.)

**v0.2 resolution — keep pub/sub; take the cheap wins; document the scaling boundary.**
- **SSE over WebSocket for the one-directional progress/log feed.** Launch/cancel already go through REST (§14), so SSE is strictly *simpler*: free auto-reconnect, no per-process WS state. *Rationale: a simplification, not added complexity.*
- **Coalesce on the probe.** Heartbeats rate-capped at **≤ 2 Hz**; logs shipped in batched chunks, not per line (extends the §14 batching discipline). *Rationale: good hygiene at any scale, near-zero cost.*
- **Treat the live stream as lossy; backfill from the durable record.** §4.3/§4.7 already persist the full log bundle to MinIO. On reconnect, the console fetches the authoritative bundle to close any gap — replay without stream-replay machinery. *Rationale: write down the lossy-stream / durable-record contract that v0.1 left implicit.*
- **Document the scaling boundary:** live fan-out is single-replica in v1; horizontal API scale-out requires either sticky-hash routing of a run's stream consumers **or** a switch to Redis Streams. Deferred to the Helm/multi-replica milestone (§13/M6).

**Rejected:** Redis Streams with consumer groups + `MAXLEN` trimming now (PERF-07 headline). The persisted MinIO bundle already makes the stream safely lossy; Streams add memory tuning and a trim-eats-history failure mode to solve a problem v1's single-compose default doesn't have.

---

### 9. Artifact serving & CDN

**v0.1 decision.** §12/§4.6 renders two model-generated artifacts per arena match in sandboxed iframes, served from MinIO; artifacts are content-addressed (`artifacts.hash`, §8).

**Problem.** Each match fetches two arbitrary (potentially multi-MB) HTML/JS bundles from MinIO on every view, with no caching/CDN strategy, no immutable cache headers, and no size cap — a runaway generated game can be a 50 MB cold GET on the interactive path. Because artifacts are model-generated, MIME/serving mistakes also compound the arena XSS surface. (PERF-08 **low**; EXEC-7 serving hardening.)

**v0.2 resolution.**
- **Content-addressed, immutable caching.** Object key = content hash; serve with `Cache-Control: public, max-age=31536000, immutable`. A self-hoster drops Caddy/nginx (or Cloudflare/R2) in front of the artifact origin for free CDN/edge caching; browser-immutable-cache alone gets most of the benefit with no external dependency. *Rationale: artifacts are perfectly cacheable and never change; exploit it.*
- **Server-set serving contract (closes the EXEC-7 serving gap cheaply).** The server sets `Content-Type` from a **server-side sniff at ingest** (not the model-supplied `type`), plus `X-Content-Type-Options: nosniff` and `Content-Disposition: attachment` for everything except the html/game type the arena intentionally iframes. The model-supplied `type`/`playable` are UI hints only — bytes get no extra privilege from a label, since they always render in the locked sandbox/opaque origin (security covered in the EXEC section; the iframe stays `sandbox="allow-scripts"`, never `allow-same-origin`).
- **Size cap + lazy-load.** Reporter warns and stores oversized artifacts in full but the arena streams progressively; lazy-load the second artifact only after the first renders; optionally prewarm the next pair. Serve by content-addressed URL so the arena provably shows the exact blob that was judged.

**Rejected:** a mandatory CDN (PERF-08 keeps it optional — browser-immutable-cache + nginx is the no-dependency default) and **server-side HTML validation/allowlisting** of artifacts (EXEC-7 headline) — proving arbitrary creative HTML "safe" is unbounded and would reject the exotic-but-safe artifacts the creativity arena exists to surface. Origin-isolated sandboxing is the containment boundary; serving hardening is defense-in-depth.

---

### 10. Autoscaling & deployment footprint

**v0.1 decision.** §13: one `docker compose up` (single Postgres, Redis, MinIO, API, workers); "Helm chart for k8s later."

**Problem.** The temptation is to add HA, replicas, sharding, and a CDN to the baseline. For the stated audience that is premature complexity; for the rare large operator the scale-out path must still exist.

**v0.2 resolution — single-node-but-recoverable is the default; scale-out is additive and opt-in.**
- **KEEP single-node compose as the default** (PERF-09). Read replicas, partitioning, k8s, and CDN are a documented **"large deployment" profile**, gated behind measured triggers — never baseline.
- **The worker layer is where the heavy jobs live**: judge dispatch + budget caps (§3), per-run summary / energy integral materialization (§4), category-score + `leaderboard_matrix` materialization (§7). Scale workers horizontally first (stateless Dramatiq consumers on Redis) before touching the data tier — the cheapest real scaling lever.
- **Durability before HA (SRE-05, adjusted high — the one mandatory ops add).** A self-hosted product sold on reproducible history must not ship with no restore path. Specify: Postgres **nightly `pg_dump` shipped off-host** (compressed; ~24h RPO is adequate for this regenerable, modest-schema data — continuous-WAL/PITR is an *optional* profile, not the default); MinIO **versioning + off-host `mc mirror`/`rclone`** (artifacts/transcripts/signed manifests are the genuinely irreplaceable asset); Redis `appendonly yes`. **Backups MUST land off-host** — never archive Postgres backups into the same MinIO instance that is itself the SPOF. Ship one backup sidecar in compose, a one-command **tested** restore, and add RPO/RTO + data-durability to §16. *Rationale: an untested backup is not a backup; off-host is the load-bearing constraint.*
- **Liveness/readiness + zombie-run reaper (SRE-06, adjusted medium).** Add `/healthz` (liveness) and `/readyz` (Postgres + Redis + MinIO reachable + migrations current); ingest endpoints return **503 (retryable), not 500**, when a dependency is down so probe backoff behaves. A Dramatiq periodic actor reaps runs whose **heartbeat receipt** (server-side timestamp, never probe wall-clock) is stale > 3× the interval (default 30s → 90s), releasing the runner handle and surfacing the failure. *Rationale: reap on heartbeat staleness, not absolute stage duration — a legitimate long sweep keeps heartbeating, so it's never false-killed.*

**Rejected:** full HA (Patroni/Postgres replicas, MinIO distributed mode, Redis Sentinel) as a baseline (SRE-05/PERF-09) — too heavy for the stated small-team self-host and it undermines the one-compose simplicity. The pragmatic stance is single-node + cheap off-host backups + tested restore, giving bounded RPO/RTO without operating a clustered control plane.

---

### 11. Observability (just enough to debug six hops)

**v0.1 decision.** §13: WebSocket/SSE log streaming is the only telemetry; no tracing, metrics, structured logging, or error tracking.

**Problem.** The system is distributed (UI → API → workers → Redis → MinIO → Postgres, plus N off-host probes), yet a wrong number or a probe that dies before connecting is undebuggable after the fact — and you cannot distinguish "the model is slow" from "our resource monitor stalled" without per-stage instrumentation, which is the product's own correctness claim. (OPS-02 adjusted **medium**.)

**v0.2 resolution — the "honest minimum," not an LGTM stack.**
- **Structured JSON logs** (`structlog`) across API, workers, **and probe**, with `run_id`/`probe_id`/`case_id`/`stage` on every line (the `run_id` correlation key already flows end-to-end). Persist mothership logs to stdout + rotating file; a tiny Loki container is **opt-in**, not mandated.
- **Probe → mothership log/error backchannel** over the **existing authenticated, resumable ingest channel** (extend the §4.3.8 reporter + the §8 MinIO log-bundle slot with `POST /api/runs/{id}/logs`), flushing a final error report on crash/heartbeat-timeout. *Rationale: fixes the Mode B "can't shell in / died before connecting" case without making the off-host probe open a second egress port through the operator's firewall.*
- **OpenTelemetry tracing on the in-cluster API→worker→judge path only** (FastAPI + Dramatiq auto-instrumentation is cheap), exported to console/OTLP; the collector/Tempo is an **opt-in compose profile, OFF by default**. Probe-side: propagate `run_id` (already done) and optionally `traceparent`; do **not** require the off-host probe to run an OTLP exporter.
- **Sentry/GlitchTip** for exception aggregation, enabled via DSN env var (no DSN = no Sentry).

**Rejected:** the full Loki + Tempo + Prometheus/VictoriaMetrics + Alloy stack and a metrics/SLO backend as defaults (OPS-02 headline). For a batch-job system with no continuous traffic these are premature, and — critically — they'd contend for RAM/VRAM on the GPU host that should be spending it on the model under test, which is self-defeating for a benchmarking tool. Defer the metrics backend to a hosted/multi-tenant deployment.

---

### 12. Cross-cutting tradeoffs & conflict resolutions

- **Speed accuracy vs. single-image simplicity (PERF-01).** We chose process separation over a Go/Rust load core: ~90% of the jitter reduction at zero cost to the "one Python probe everywhere" invariant. The residual high-concurrency error on co-located boxes is *labeled*, not eliminated — an honest lower-bound beats a hidden inaccuracy.
- **Judge cost vs. judge rigor (PERF-03 ↔ SV-04).** Opt-in sampling (default OFF) cuts cost but reduces variance characterization; we keep the deterministic backbone (Math/Coding/IFEval ≥ 0.7 weight) as the real anti-gaming floor, so dialing samples down degrades a *subjective sub-weight*, not the leaderboard's spine.
- **Timescale machinery vs. ops burden (PERF-04 ↔ DATA-07).** Summary-at-ingest + MinIO Parquet raw + downsampled charts deliver the disk-blow-up and chart-killer fixes *without* the hypertable/compression/CAGG stack, which becomes opt-in. One storage path, not two.
- **Snapshot precompute vs. client re-rank (PERF-05 ↔ DATA-08).** Deleting per-profile snapshots is the rare change that is *both* simpler and more correct — less schema, fewer worker steps, no cache-coherency trap.
- **Realtime durability vs. complexity (PERF-07).** The already-persisted MinIO log bundle lets us keep cheap pub/sub and defer Redis Streams to the multi-replica milestone — the durable record, not the transport, carries the guarantee.
- **Scale-out vs. operability (PERF-09 ↔ SRE-05).** Single-node-but-recoverable: cheap off-host backups + a tested restore are mandatory; HA is rejected from the baseline. The only non-negotiable ops add is durability, because losing the artifacts the platform exists to preserve is an existential failure, not an outage.

**Net effect.** Every measurement the platform publishes is now either independently honest (load-model labeled, co-location flagged, errors accounted, sample sizes shown) or explicitly marked provisional; the judge can't run the bill or the worker pool into the ground; the time-series and leaderboard read paths never touch unbounded raw data; ingest is exactly-once; and the whole thing still boots from one `docker compose up` with a one-command restore. Heavier machinery (open-loop rig, Timescale CAGGs, Redis Streams, read replicas, LGTM observability, Temporal) is documented as opt-in hardening behind measured triggers — chosen against, on purpose, for v0.2's small-team default.
