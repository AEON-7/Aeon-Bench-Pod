# Data & Security Architecture — AEON Bench Mothership

> **Status:** greenfield decision record. No real production data exists yet, so every
> choice here can be made correctly *once*, before signed submissions and trust history
> accrue. Scope: the **private mothership** (datastore, performance, backup/DR, key &
> auth posture). The **open-source pod** keeps SQLite and is out of scope except where the
> mothership↔pod contract is affected.
>
> **Scale reality this is designed for:** thousands-to-low-millions of rows. NOT billions.
> Read-heavy public leaderboards + bursty signed-submission writes + heavy blobs in object
> storage. Optimize for **integrity (this is a trust system) → performance → Fort-Knox security**.
>
> Grounding docs: `docs/trust-architecture.md` §5 (schema/indexes/queries),
> `docs/onyx-deployment.md` (prod host, replicas, `deploy-enhanced.sh`, MinIO),
> `docs/architecture.md` (mothership/pod split). Current substrate: `mvp/aeon/{db,attest,accounts,ingest}.py`.

---

## 1. Decision — datastore

**Mothership system of record: PostgreSQL 17**, single primary + one streaming replica on the
`aeon-internal` network. Postgres is **earned, not cargo-culted**: the §5 schema is already
written in Postgres dialect against Postgres-only features — `DISTINCT ON` (trust-architecture.md:340),
GIN `jsonb_path_ops` on `hardware_json`/`software_json` (323-324), and partial indexes
`WHERE superseded_at IS NULL` / `WHERE status='active'` (331, 333). The decisive axis is that this
is a **trust ledger** with *concurrent* signed writers (2-3 `aeon-app` replicas, onyx-deployment.md:26)
whose integrity model is `UPDATE ... WHERE status='open'` race-winners (`claim_pod_run`, `claim_match`).
That demands real FKs, `CHECK` constraints, partial-unique, transactional COMMIT, **and** multi-writer
concurrency — the one combination no alternative delivers. The **pod stays SQLite** (local, single-user,
single-writer, zero-ops); the asymmetry is principled, and the `ingest.py` signed-bundle wire contract
between them is datastore-agnostic so the split costs nothing.

### Scored alternatives

Weighted: ACID/integrity (dominant) → jsonb faceted search → board aggregation → replication/ops/portability.
Scale capped at thousands-to-low-millions of rows.

| Candidate | ACID / integrity | jsonb facets | Board aggregation | Concurrent writers | Cloud-portable | Ops | Verdict |
|---|---|---|---|---|---|---|---|
| **PostgreSQL 17** | ✅ serializable, FK/CHECK/partial-unique, txn DDL | ✅ GIN `jsonb_path_ops` (§5) | ✅ matview-style read models + partial indexes | ✅ MVCC, true multi-writer | ✅ RDS/Cloud SQL/Neon/self-host | Medium | **WINNER** |
| **SQLite + Litestream** (steelman) | ✅ but **single-writer**; Litestream = async ship, not multi-writer | ⚠️ JSON1, no GIN → facet scans | ⚠️ ok small, no matviews | ❌ serializes every ingest COMMIT; `SQLITE_BUSY` storms during bursts | ✅ (a file) | Lowest | **pod only** — loses on the one axis a concurrent trust ledger needs |
| MySQL/MariaDB | ✅ but weaker CHECK history, no partial indexes, no `DISTINCT ON` | ⚠️ JSON, no expression-GIN parity | ⚠️ no matviews (MariaDB) | ✅ | ✅ | Medium | loses on jsonb + partial-unique + `DISTINCT ON` already in use |
| MongoDB / document | ⚠️ multi-doc txns = unhappy path; this is a **ledger**, not a doc store | ✅ native | ⚠️ pipeline, no SQL joins across runs/results/scores | ✅ | ✅ | Med-High | **wrong shape** — §5 is relational w/ FKs + joins |
| CockroachDB / distributed-SQL | ✅ serializable | ✅ (PG-ish) | ⚠️ weaker matview story | ✅✅ | ✅ | **High** | distributed-consensus tax for **non-billions** = over-engineering |
| ClickHouse / OLAP | ❌ no real UPDATE/DELETE/FK; eventual | ❌ not point-facet writes | ✅✅ but it's a **sink** | ❌ | ✅ | High | **never the system of record** for a trust system |
| Neon / serverless-PG | ✅ (it *is* Postgres) | ✅ | ✅ | ✅ | ✅✅ branching | Low | **the cloud-migration target**, not the on-prem onyx choice today |

**The honest contender** is not Mongo/Cockroach/ClickHouse (correctly dismantled above) — it is
*staying on SQLite for the mothership too, with Litestream for PITR*. It loses **decisively on one
axis the others don't touch: concurrent writers.** The whole ingest integrity model is concurrent
race-winner `UPDATE`s under bursty signed submissions across 2-3 replicas; SQLite serializes every
COMMIT behind a global write lock and `SQLITE_BUSY` becomes the norm precisely during bursts. Postgres wins.

### Version + extensions

| Extension | Use | Status |
|---|---|---|
| **`citext`** | case-insensitive usernames — replaces the `COLLATE NOCASE` hack (db.py:456, comment admits ASCII-only) | **Required** |
| **`pgcrypto`** | `gen_random_uuid()` / digests only — **never** password hashing | **Required** |
| **`pg_stat_statements`** | board-query tuning | **Required** |
| `pgvector` | semantic search / embedding dedup | **Defer (YAGNI)** — one cheap migration if a feature lands |

**No** document-store, **no** OLAP sink (ClickHouse/DuckDB) as system of record. An OLAP sink is justified
*only* if ad-hoc analytical scans over telemetry outgrow the precomputed read models — at this scale they
won't; if they ever do, **DuckDB-over-Parquet-in-MinIO** (zero standing infra) beats standing up ClickHouse.

---

## 2. Performance architecture

**Headline:** at thousands-to-low-millions of rows, a single well-indexed Postgres with **blobs in MinIO**
and **precomputed board tables** serves every §5 query in single-digit ms. The replica and PgBouncer are
**operational headroom and a clean read/write split**, not because the primary would fall over. The one item
that is a **bug, not an optimization**, is the single-process challenge/rate state.

### 2.1 Blobs out of the DB — the biggest single lever (P0, non-negotiable)

The §5 schema already does this (`raw_output_ref`, `transcript_ref`, `raw_series_ref`, `tee_quote_ref`,
trust-architecture.md:294-308). The **current MVP does not** — `db.py` inlines `results.raw_output TEXT`
and `arena_artifacts.html TEXT`. **That inlining must not migrate into the Postgres cut.**

| Payload | Size | Where | DB stores |
|---|---|---|---|
| `raw_output` (per case) | 1–50 KB | MinIO | `raw_output_ref` (key=sha256) + `raw_output_hash` |
| transcript (agentic loop) | 10 KB–2 MB | MinIO | `transcript_ref` + hash |
| arena HTML artifact | 5–500 KB | MinIO | ref + hash (render inert in client iframe) |
| telemetry raw series | 0.1–10 MB | MinIO | `raw_series_ref`; only `resource_summary_json` (p95/mean/joules) stays in DB |
| TEE quote | 1–20 KB | MinIO | `tee_quote_ref` |

Why it pays (not cargo-cult): (1) **TOAST off the hot path** — narrow hot tables fit the working set in
`shared_buffers`, so board scans are buffer-cache hits; (2) **content-addressed dedup is O(1) and an integrity
property** — the sha256 *is* the object key, so a blob cannot be swapped without breaking the signed manifest;
(3) **`pg_dump` stays small/fast**, protecting the deploy-time backup SLA; (4) **WAL stays small**, keeping the
replica fresh. **Rule:** anything >~8 KB, append-only, not a filter/sort key → MinIO. Anything you
`WHERE`/`ORDER BY`/`GROUP BY` on → a DB column.

**Ordering matters (DR consistency — see §3.4):** write the blob to MinIO **before** the DB row commits
(content-addressed ⇒ re-PUT is idempotent). Board reads never dereference blobs, so the second hop (PG→MinIO,
LAN-local) is paid only by the transcript/detail view.

### 2.2 Index strategy (§5 set + the one high-impact addition)

Use the §5 set exactly — no GIN-on-everything, no per-column btrees "just in case" (each index taxes the
bursty submission writes):

| Index | Serves |
|---|---|
| `gin (hardware_json jsonb_path_ops)`, `gin (software_json jsonb_path_ops)` | facet containment (`jsonb_path_ops` ≈ ½ the size of default ops; we never need key-existence) |
| `runs (suite_version_id, trust_tier, harness, engine_name, finished_at)` | primary faceted board filter + recency sort |
| `runs (model_version_id, suite_version_id, finished_at)` | per-model time series (§5 query 2) |
| `runs (judge_model, judge_family)` | judge facet |
| `leaderboard_matrix (suite_version_id, category_id, quality_score DESC)` | hot board top-N (`DESC` matches `DISTINCT ON ... ORDER BY`) |
| `category_scores (model_version_id, category_id, run_id) WHERE superseded_at IS NULL` | **partial** — only live scores |
| `enrolled_keys (fingerprint) WHERE status='active'` | **partial** — auth hot path |
| **`runs (suite_version_id, harness, finished_at) WHERE trust_tier IN ('orchestrated','attested')`** | **ADD THIS.** Boards never show `self_reported` (which dominates row count), so a partial index over only record-eligible rows is the board's exact working set — smaller and the single most impactful index given the tier distribution. |

### 2.3 Leaderboard read models — event-driven table, NOT `MATERIALIZED VIEW`

`category_scores` and `leaderboard_matrix` are **precomputed read-model tables** (§5), refreshed by an
**upsert at COMMIT inside the ingest transaction**, scoped to the `(suite_version_id, model_version_id, category_id)`
cells the new run touched:

```
COMMIT txn:
  INSERT runs, results, environments, category_scores …
  INSERT INTO leaderboard_matrix … ON CONFLICT (suite_version_id, model_version_id, category_id)
    DO UPDATE  -- recompute just that cell from current category_scores
```

| | Event-driven table (chosen) | `MATERIALIZED VIEW` + cron |
|---|---|---|
| Freshness | correct the instant a run commits | stale until next tick |
| Cost | recompute **one cell** | recompute **whole view** |
| Locks | normal row upsert | `CONCURRENTLY` needs UNIQUE + full scan each time |
| Already in §5 | yes | would be a regression |

**Mitigation for incremental drift:** a **nightly full-recompute reconciliation job** (app cron) that rebuilds
`leaderboard_matrix` from scratch and diffs against the live table. This is a *correctness audit*, not the
refresh mechanism — the one cron worth having. Set `fillfactor=80` on `leaderboard_matrix` + `category_scores`
so upserts stay HOT (no index churn).

### 2.4 Connection pooling + replica

- **PgBouncer, `transaction` pooling mode**, one container on `aeon-internal` in front of `postgres-primary`
  (`default_pool_size≈20`, primary `max_connections≈100`). Required the moment `aeon-app` scales past 1 replica
  to avoid connection storms. Caveat: transaction mode disallows server-side prepared statements unless PgBouncer's
  prepared-statement support is enabled — **verify psycopg config**.
- **Read replica** (`postgres-replica`, already named in compose): route **public faceted board reads**
  (`leaderboard_matrix`, `category_scores`, `list_submissions`) here via an explicit app-level `read_db()`/`write_db()`
  accessor. Boards tolerate seconds of replication-lag staleness (data is already committed + signed).
  **Never route auth, nonce-claim, `claim_pod_run`, or read-your-writes paths to the replica** — those hit the primary.
  Single-DB first (P0) → add replica (P1) is a valid path.

### 2.5 Shared challenge/nonce store fix (CORRECTNESS — gates multi-replica)

`ingest._challenges` is an in-process dict (`ingest.py:35`). `issue_challenge()` writes replica A's RAM; the
load-balanced `enroll()` POST lands on replica B whose dict lacks the challenge → **enrollment fails
nondeterministically** the moment `--scale aeon-app>1` runs. The **run-level** nonce is already shared-safe
(`pod_submissions` + `claim_pod_run`), so the fix is scoped to **one table + two functions**:

```sql
CREATE TABLE enroll_challenges (challenge text PRIMARY KEY, expires_at timestamptz NOT NULL);
-- atomic single-use consume:
DELETE FROM enroll_challenges WHERE challenge = $1 AND expires_at >= now() RETURNING 1;
```

The same defect afflicts `accounts._hits`/`_fails`/`_signup_lock` (accounts.py:28-32) — the IP-cap silently
becomes N× under N replicas. Fix: DB-backed rate state (or Redis if already deployed — don't add Redis just for
this) + enforce the signup cap with a `UNIQUE` partial index / atomic insert rather than the per-process lock.
TTL cleanup = a tiny `DELETE WHERE expires_at < now()` piggybacked on the nightly job.

### 2.6 What is YAGNI now

| Item | Verdict |
|---|---|
| Partition `results` / telemetry | **Don't build.** Telemetry is already out of DB (`raw_series_ref`). **Pre-pick** the key = `RANGE (finished_at)` monthly so the later add is a migration, not a redesign. Trigger: `results` > ~50–100M + degraded vacuum/scan. |
| Citus / sharding / TimescaleDB / columnar | **No.** Single node + replica covers millions with headroom. |
| `MATERIALIZED VIEW` + `REFRESH CONCURRENTLY` | **No** — inferior to the event-driven table. |
| Redis | **No** — Postgres handles the tiny challenge/rate volume; don't add a service. |
| Autovacuum | Global defaults fine; **light per-table** tuning on churny tables (`runs`, `results`, `pod_submissions`, `category_scores`, `leaderboard_matrix`): `autovacuum_vacuum_scale_factor=0.02`, `autovacuum_analyze_scale_factor=0.01`. |

---

## 3. Backup & Disaster Recovery

The DB is **tiny** (blobs are refs): the entire compressed PG estate is **~0.5 GB at 1M runs**, so retention
is a *recovery-confidence* decision, not a cost decision. Both backup layers are pushed **off-box** to
S3-compatible object storage (MinIO now → R2/S3 later), **encrypted before leaving the host** with a key that
is **not on the database host**.

### 3.1 Two layers (right-sized — see the trim note)

1. **`pg_dump -Fc` (logical, portable)** — taken **pre-deploy** (bound to the git SHA) *and* on the GFS cadence,
   pushed off-box encrypted (`age`/gpg). This is the **rollback unit + the GFS unit + cross-version-restore**
   (physical backups can't restore across a PG major upgrade; logical can). Fastest restore for a 0.5 GB DB.
2. **WAL archiving for PITR** — `wal_level=replica`, `archive_mode=on`, continuous WAL → object storage. Gives
   roll-to-the-second recovery (e.g. the instant *before* a bad ingest/migration). **This is the one primitive
   that earns standing infra for a trust ledger.**

> **Trim note (validation cut):** the original plan ran **pgBackRest full+diff+incremental *and* `pg_dump`** as
> two parallel full estates. At 0.5 GB that's gold-plating — pgBackRest's scheduling engine is TB-scale
> machinery. **Keep `pg_dump -Fc` (logical) + WAL-PITR as the minimum.** Optionally retain pgBackRest *only*
> for its free **page-checksum verification** (genuinely nice for a trust system), but drop the diff/incremental
> ceremony. This is "trim," not "delete."

**WAL safety:** the `archive_command` **must fail the WAL switch on push failure** (don't run async-archive in a
mode that masks a stalled push), and **monitor last-archived-LSN lag** — otherwise WAL silently stops shipping and
PITR rots without alarm. This cannot be retrofitted: set it greenfield or lose the early window.

### 3.2 GFS retention (user floor = 1 monthly / 1 weekly / 2 daily; right-sized up — it's nearly free)

| Tier | Count | Rationale |
|---|---|---|
| **WAL / PITR window** | **14 days continuous** | any-second recovery for 2 weeks (catches a bad ingest / silent corruption found days later) |
| **Daily** | **14** (floor 2) | aligned to the WAL window |
| **Weekly full** | **8** (floor 1) | 8 weeks of restore points independent of WAL replay |
| **Monthly full** | **12** (floor 1) | 1 year of audit/forensic snapshots ("what did the board look like in Q1") |
| **Yearly full** | **2** (optional) | long-horizon archival → cold/Glacier-class |
| **Pre-deploy `pg_dump -Fc`** | **last 20 deploys** | git-SHA-tagged; orthogonal to GFS; fast rollback unit |

Total estate ≈ **20–30 GB even at 1M runs** → retention is not a cost decision; keep the window generous.

### 3.3 `deploy-enhanced.sh` integration (onyx Compose)

Data at `/srv/appdata/aeon-bench/{pg,keys,artifacts,backups}`, `aeon-internal` net. Replaces the current
`deploy/deploy.sh` `pg_dump | gzip` (no encryption, no off-box copy, no PITR, prune-only `tail -n +31`).

```
deploy-enhanced.sh:
  1. git pull (pinned GHCR digest)
  2. PRE-DEPLOY BACKUP + TAG
     a. pg_dump -Fc → backups/predeploy-$GIT_SHA-$ts.dump  (then age-encrypt + push off-box)
     b. (optional) WAL/PITR base checkpoint
     c. record {git_sha, dump_path, wal_lsn} in .deployment-state.json  (restore coordinates for THIS deploy)
  3. migrate (Alembic/SQL on §5 schema)
  4. health-checked rolling (start new replica, GET /api/suite, drain old, 1 at a time)
  5. ROLLBACK (health/migration fail):
       - app:  redeploy previous GHCR digest
       - data: restore predeploy-$GIT_SHA dump (seconds)  OR  PITR --target=<pre-migrate LSN>
  6. PRUNE TO RETENTION: expire per §3.2 + keep last 20 predeploy dumps  (replaces the tail -n +31 line)
```

**Safety rule baked in:** the **signing key is never touched by the deploy backup path** — `pg_dump`/WAL back up
`pg/` only; the key (`aeon_keys:/keys:ro`) has its own separate cron to a separate offline-wrapped destination
(§4.5). Confirm no `AEON_KEY_PASS` lands in any backed-up `.env`.

### 3.4 Blob/DB consistency on restore (the silent-corruption hole — fix)

MinIO is on its own replication schedule, so a PITR rollback can leave a **missing ref**: a signed,
record-eligible run whose `*_ref` resolves to nothing. The signature still verifies (it's over the manifest, and
the ref *is* the sha256), so **nothing trips** — a verifiably-signed run with no transcript. For a trust system
this is a silent integrity gap, not a 404. **Fixes:**
- **Write-blob-then-row** ordering (§2.1) so blob durability precedes the DB commit.
- `restore-verify.sh` (§3.6) **must dereference a sample of `*_ref` keys against MinIO**, not just check row checksums.
- Post-failover (region loss), re-verify ref resolvability across the replicated buckets.

### 3.5 Object-store policy (artifacts / transcripts / HTML)

| Control | Setting |
|---|---|
| Versioning | enabled (content-addressed keys are immutable, but versioning blocks accidental/malicious overwrite/delete) |
| Object Lock / WORM | compliance-mode retention (e.g. 90d) on `transcripts/` + `raw_output/` — forensic evidence for trust disputes, tamper-proof even to an admin |
| Lifecycle | hot 90d → IA → cold (Glacier/R2-equiv) after 1y; quarantined bundles (`ingest/quarantine/`) retained inert then expired |
| Replication | cross-region async of **both** the artifacts bucket **and** the WAL/backup repo bucket → 3-2-1 off-box |
| Encryption | SSE w/ customer-managed key; bucket policy denies unencrypted PUT + denies delete on locked prefixes |

### 3.6 Mandatory test-restore (cron + CI, fail-loud) — an unverified backup is not a backup

```
restore-verify.sh  (weekly + on every retention change; scratch volume, never touches prod):
  1. restore latest (pg_dump OR PITR --target) → ephemeral postgres container
  2. INTEGRITY GATE:
       - page-checksum scan (pg_amcheck) — no corruption
       - row-count + per-table checksum vs source (runs, results, enrolled_keys, pod_submissions)
       - re-verify a SAMPLE of environments.manifest_signature → signed rows still verify   ← AEON-specific, load-bearing
       - DEREFERENCE a sample of *_ref keys against MinIO  → blob/DB consistency             ← closes §3.4
       - confirm leaderboard_matrix / category_scores rebuild cleanly
  3. PASS → metric/green ;  FAIL → page admin (CrowdSec/alert) + HARD-BLOCK next deploy
  4. tear down scratch ; record wall-clock (= real RTO measurement)
```

A **key-restore drill** runs on the **same cadence** from the **offline escrow copy only** (prove you can decrypt
the off-box backups *without* the live box) — also a hard-block gate. The signature spot-check + blob deref + escrow
drill are what make the restore *actually* tested, not just byte-copied.

### 3.7 Sizing + RPO/RTO

| Scenario | RPO | RTO |
|---|---|---|
| Bad migration / bad ingest (most common) | **0** (roll to pre-migrate LSN) | **< 2 min** (pre-deploy `pg_dump -Fc` restore, small DB) |
| Host loss / volume corruption | **≤ archive interval (`archive_timeout=60` → ≤ 60s)** | **< 30 min** (off-box WAL+full restore to new host) |
| Object-store region loss | **≤ replication lag (minutes)** | **< 1 h** (fail reads to replicated bucket) |
| **Signing-key loss** | **N/A — not recoverable by PITR** | depends on offline escrow; **tested by the key-restore drill** |

Hot standby (`postgres-replica`) covers **availability/failover**, NOT backup — logical corruption / malicious
delete replicates instantly. PITR + off-box backups are what save you there. Keep both; different failures.

---

## 4. Fort Knox — keys, encryption, auth

### 4.1 The crown jewel — signing-key protection ladder

**Current state (the single highest-severity issue in the whole review):** `attest.py:91-103`
`_load_or_create_key()` writes PKCS8 with **`NoEncryption()`** (line 98), auto-generates on first use, and is
called **inside the request path** (`sign()` :113 → `attestation()`, `sign_manifest()`). Three concrete failures:
**(1) plaintext at rest** — any read of `keys/` = full trust-anchor forgery; **(2) silent auto-re-anchor** — a
wiped/unmounted volume makes the next signing request mint a *brand-new* anchor and serve attestations under it,
breaking (or worse, silently re-pinning) every verifier; **(3)** wide on-disk exposure window per call.

**P0 fix (mandatory before any real signed submission):** split
`_load_or_create_key()` → `_load_key()` (decrypt with `AEON_KEY_PASS`) **+** an explicit `aeon-keygen` CLI.
**Never generate inside the request path — a missing key must hard-fail.** Cache the decrypted
`Ed25519PrivateKey` in a module global; at rung 4 the body of `sign()` becomes a KMS call with callers unchanged.

| Rung | Mechanism (onyx Compose) | Protects against | When |
|---|---|---|---|
| 0 (today) | `0o600` PEM, **plaintext** | nothing but other unix users | — |
| **1 (do now)** | **Encrypted PKCS8** (`BestAvailableEncryption`), pass from **Docker secret** on tmpfs `/run/secrets`; LUKS on `keys/` | offline volume/backup/snapshot theft | P0 |
| 2 | SOPS+age encrypt the key file in git-ops; decrypt to tmpfs at boot | secrets-in-repo, config drift | P1 |
| 3 | Vault transit (sign-as-a-service) / cloud KMS | key never at rest in app memory | later |
| **4 (target)** | **KMS-resident Ed25519** (AWS/GCP KMS) or **YubiHSM2** on-prem; `sign()` → API, private bytes never leave | host compromise, memory scrape, insider | cloud cutover |

**Honest threat-model line:** rung 1 closes **offline/backup theft**; it does **not** close a **live root** on
onyx (or co-tenant breakout) scraping the decrypted key / tmpfs passphrase from process memory. Only rung 4
(KMS/HSM) closes that — **accepted as residual until cloud cutover.** Don't sell rung 1 as closing host compromise.

### 4.2 Rotation + multi-key trust anchor

`public_key_b64()` returns a single key; verifiers pin exactly one → rotation is a breaking change. Fix with a
**JWKS-style multi-key anchor** at `/.well-known/aeon-bench.json`:

```json
{ "keys": [
    {"kid":"2026-06","alg":"ed25519","public_key":"<b64>","not_before":"…","not_after":null},
    {"kid":"2025-12","alg":"ed25519","public_key":"<b64>","not_before":"…","not_after":"2026-06-01","status":"retired"}
  ],
  "revoked": ["<b64-of-compromised>"],
  "anchor_sig": "<signature over the keyset by the current key>"
}
```

- Add `kid` to the signed body in `attestation()` / `sign_manifest()`; `verify_manifest()` selects key by `kid`
  and **rejects revoked/expired** kids. Old signatures stay valid under their retired-but-not-revoked key.
- **Sign the keyset document itself** (`anchor_sig`). The anchor is served by the app; if an attacker can push to
  it they could **revoke the legitimate key (DoS the trust root)** or un-revoke a compromised one. Verifiers must
  reject a tampered keyset. (KMS-side, also **reconcile every `Sign` call against the `audit_log`** — §4.4.)
- **Cadence:** scheduled key every N months + **break-glass** on suspected compromise (publish to `revoked`, mint
  new `kid`; if truly compromised, mark affected historical runs `attestation_uncertain`).

### 4.3 Encryption everywhere

| Layer | Action |
|---|---|
| At-rest (volume) | **LUKS** on `/srv/appdata/aeon-bench` (esp. `keys/`, `pg/`, `backups/`) |
| At-rest (field) | **`signup_ip` → keyed-HMAC only** (see cut below) |
| In-transit app↔PG | **TLS + client cert** (`sslmode=verify-full`) even on `aeon-internal` (defends a compromised co-tenant) |
| Network exposure | PG **only** on `aeon-internal`, no published port, never on the tunnel net |
| Backups | `pg_dump | age -r <recipient>` → `backups/…sql.age`; decrypt key **off-host & escrowed** (§3.6) |
| Blobs | transcripts / `raw_output` / arena HTML → object store, SSE; DB holds only `*_ref` + `*_hash` (fix the MVP's inlined TEXT at the PG cut) |

> **Validation cut (over-engineering):** the original plan **pgp-encrypted `signup_ip` *and* kept an HMAC blind
> index**. `signup_ip`'s *only* consumer is `count_users_by_ip` — an exact-match COUNT for the 5/IP cap (db.py:460).
> Two crypto constructs for a rate-limit field is too much. **Store ONLY the keyed-HMAC of the canonicalized IP**
> (the cap works on HMAC equality; you lose the ability to read the raw IP back — which for an *anonymous,
> no-email, no-recovery* system, per accounts.py:1-7, is a **feature**). Cut the decryptable copy. Don't encrypt
> public facet columns (`hardware_json->>'gpu_model'`) — they need the §5 GIN indexes.

### 4.4 Auth hardening

- **Password hashing pbkdf2-200k → Argon2id** (accounts.py:35-37 `_hash`). `argon2-cffi`, tune `m=64MB,t=2,p=1`,
  store the full PHC string in `pw_hash` (`pw_salt` becomes vestigial — Argon2 embeds salt+params).
  **Migrate-on-login:** if `pw_hash` lacks `$argon2id$`, verify with pbkdf2 then transparently re-hash. No forced
  reset. **Keep the constant-time dummy-hash on unknown user** (accounts.py:135-138) — wrap Argon2's raising
  `verify` to preserve the no-enumeration timing property.
- **Close the unauthenticated enroll** (ingest.py:59 — accepts *any* self-generated keypair that signs a
  challenge; PoP-of-a-fresh-key binds nothing → swarm a thousand `self_reported` channels). **Bind enrollment to a
  logged-in evaluator account**: require a bearer token, stamp `owner_user_id` (already exists in `enroll_key`);
  **rate-cap enroll per account + per IP** and cap keys-per-account. Keep the PoP challenge — it's correct; account
  binding is what *authorizes* it.
- **Least-privilege DB roles** (client-cert auth, not just password):

  | Role | Grants | Used by |
  |---|---|---|
  | `aeon_owner` | owns schema; **no app uses it** | manual |
  | `aeon_migrator` | DDL on schema | `deploy-enhanced.sh` migrate step |
  | `aeon_app` | `SELECT/INSERT/UPDATE` data tables; **no DELETE on runs/results/audit; no DDL/DROP/TRUNCATE** | FastAPI app |
  | `aeon_readonly` | `SELECT` only | read-replica / board / analytics |

- **Admin 2FA** via Authentik `forward_auth` on the admin surface; `is_admin()` env-allowlist (accounts.py:144)
  stays as a second gate (defense in depth).
- **Tokens:** evaluator 30-day absolute TTL (db.py:501) is fine; **admin ≤1h + sliding refresh + invalidate on
  logout** (wire `delete_session` to a real logout). Already high-entropy (`token_urlsafe(32)`), good.
- **Append-only `audit_log`** (`aeon_app` INSERT-only): `audit_log(id, ts, actor, action, target, ip, kid, detail_json)`.
  Write a row for every admin flag/revoke/delete, **every `sign()`/`attestation()`/`sign_manifest()`**, and every
  `enroll`/`revoke_key`/`bump_key_fail`. This is the cross-check that proves the anchor wasn't misused (and at
  rung 4, reconcile against KMS CloudTrail `Sign` events).

### 4.5 Secrets management

- `AEON_KEY_PASS`, the backup `age` recipient/passphrase, DB client-cert keys, HMAC key → **Docker secrets**
  (tmpfs `/run/secrets`), **not** in plain `.env`. `.env` stays `chmod 600` (onyx convention) for non-crypto config.
- The **encryption passphrase + offline key-escrow are themselves "lose-this-and-you're-locked-out" DR items** —
  escrowed offline, tested by the key-restore drill (§3.6).
- The signing key is backed up **separately**, encrypted to an offline-held key, with its own 3-2-1 copies, and
  **never** rides in the DB bundle. Losing it is unrecoverable (every published attestation pins it); leaking it
  is catastrophic (only rotation + re-pin mitigates).

---

## 5. Phased build plan

### NOW — greenfield, before any real signed submission or multi-replica deploy

| # | Action | Files |
|---|---|---|
| P0-1 | **Split `_load_or_create_key` → `_load_key()` + `aeon-keygen` CLI; hard-fail on missing key; encrypt PKCS8 (`BestAvailableEncryption`, pass from Docker secret on tmpfs).** Kills silent re-anchor + plaintext-at-rest. **Highest-leverage fix.** | `attest.py:91-103` |
| P0-2 | **Move `ingest._challenges` → `enroll_challenges` table** (atomic `DELETE … RETURNING`); move `accounts._hits/_fails/_signup_lock` → DB-backed state + `UNIQUE` partial index for the cap. **Gate the 2nd replica on this.** | `ingest.py:35,50,55`; `accounts.py:28-32` |
| P0-3 | Stand up **Postgres 17** + extensions (`citext`, `pgcrypto`, `pg_stat_statements`); apply **§5 schema** verbatim (FKs/CHECK/partial-unique/`DISTINCT ON`). `citext` username replaces `COLLATE NOCASE` (db.py:456). | `db.py`, §5 |
| P0-4 | **Blobs → MinIO `*_ref` + `*_hash`; write-blob-then-row.** Ensure MVP's inlined `raw_output`/`html` TEXT does NOT migrate in. | `db.py`, §5 |
| P0-5 | **Greenfield WAL/PITR:** `wal_level=replica`, `archive_mode=on`, `archive_timeout=60`, archiving that **fails the WAL switch on push failure** + LSN-lag monitoring. (Cannot be retrofitted.) | onyx PG config |
| P0-6 | **LUKS** on `/srv/appdata/aeon-bench`; encrypted off-box `pg_dump -Fc` in `deploy-enhanced.sh` (replace `pg_dump|gzip`); GFS retention per §3.2. | `deploy-enhanced.sh` |
| P0-7 | **Weekly `restore-verify.sh`** (page-checksum + row-checksum + **manifest_signature re-verify** + **`*_ref` MinIO deref**) + **key-restore drill from escrow**; both hard-block next deploy. | new cron/CI |
| P0-8 | **Argon2id** with migrate-on-login; keep constant-time dummy-hash. | `accounts.py:35-37,135-138` |

### LATER — earned, not now

| # | Action | Trigger |
|---|---|---|
| P1-1 | **Account-bind enrollment** (bearer required, stamp `owner_user_id`, per-account/IP enroll cap). | before opening public enrollment |
| P1-2 | **JWKS multi-key anchor + `kid` + signed keyset (`anchor_sig`).** | before first key rotation |
| P1-3 | **PgBouncer (transaction mode)** + route board reads to **`postgres-replica` + `aeon_readonly`** (never auth/nonce/claim); least-privilege roles. | when scaling `aeon-app` past 1 |
| P1-4 | **Event-driven `leaderboard_matrix` upsert in ingest COMMIT** + nightly reconciliation diff; `fillfactor=80`. | with the §5 cut |
| P1-5 | Admin 2FA (Authentik forward_auth); admin ≤1h tokens + logout invalidation; `audit_log`. | before multi-admin |
| P2 | SOPS+age key-at-rest (rung 2); per-table autovacuum tuning. | as ops matures |
| P3+ | **KMS-resident Ed25519 / YubiHSM2 (rung 4)** + CloudTrail↔`audit_log` reconciliation; Vault transit; managed-PG mTLS. | **cloud cutover** |
| Deferred | `pgvector`; `results` partitioning (pre-pick `RANGE (finished_at)` monthly, don't build); ClickHouse/DuckDB OLAP sink; Redis. | only when a measured need lands |

---

## Appendix — explicit trade-offs & residual risks

**Trade-offs accepted:** blobs-out adds a PG→MinIO hop on *detail* views (boards never pay it; it *strengthens*
integrity); event-driven board table trades a little COMMIT complexity for always-fresh cheap boards (mitigated by
nightly reconciliation); replica trades read-your-writes staleness on public boards (fine) for clean isolation
(correctness reads stay on primary); PgBouncer transaction mode trades server-side prepared statements (driver
caveat) for connection-storm protection; Argon2id costs ~2-4× login CPU/memory (negligible); HMAC-only `signup_ip`
gives up the ability to read raw IPs back (a feature for an anonymous system); LUKS + encrypted off-box backups add
a passphrase/escrow that is itself a DR dependency (the Fort-Knox trade); single primary is a SPOF until the
replica + promotion runbook exist (the right complexity tier for non-billions vs. paying CockroachDB consensus
latency on every write).

**Residual risks (write into the threat model):**
1. **Live-root memory scrape of the signing key** is NOT closed until rung 4 (KMS/HSM) — accepted until cloud cutover.
2. **Software self-attestation can't prove a malicious host runs the claimed code** — only TEE/Nitro fixes it (out of P0).
3. **Silent trust-root re-anchor** (P0-1) and **silent WAL-archive stall** (P0-5) both fail *quietly* — must hard-fail/alarm.
4. **Blob/DB PITR inconsistency** (missing `*_ref`) verifies-but-is-empty — closed only by write-blob-then-row + deref in restore-verify.
5. **The encryption passphrase / offline escrow** are "lose-this-and-locked-out" — tested by the key-restore drill or they're theater.
6. **A backup is only as good as its last successful test-restore** — the weekly verify failing must **hard-block** the next deploy, not warn.
7. **Scaling past 1 replica before P0-2** silently weakens the IP-cap (N×) and breaks enrollment nondeterministically — gate the rollout.
