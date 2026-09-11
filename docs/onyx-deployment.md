# Deploying AEON Bench on `onyx` (192.168.1.80) + cloud migration

Findings from a read-only audit of the production host, and how AEON fits the existing
patterns rather than inventing new ones. (Supersedes the Swarm/Traefik first cut in
`deploy/` — onyx uses **Docker Compose + Caddy + Coraza/CrowdSec**, and we should match it.)

## What onyx runs (relevant bits)
- **Reverse proxy:** Caddy. Two layers: `onyx-caddy-cloudflare` (`/opt/stacks/proxy`, custom
  build with the Cloudflare-DNS module for wildcard `*.lab.unhash.me` TLS) doing
  `Cloudflare → Caddy → Authentik forward_auth → service`; and a dedicated WAF Caddy.
- **WAF:** `/opt/stacks/waf` — `onyx-waf-caddy` (Caddy built with the **Coraza** WAF module,
  OWASP CRS in `config/coraza`) + **CrowdSec** bouncer + `waf-block-api`, load-balancing to
  frontend replicas on `waf-internal`, access/audit logs to GoAccess/Loki/Grafana.
- **Cloudflare:** two `cloudflared` Zero-Trust tunnels (`*.lab.unhash.me`, `frombottlestobruh.com`).
- **Auth:** Authentik (forward_auth SSO snippet in the proxy Caddyfile).
- **Deploy:** Compose (not Swarm). `frombottlestobruh/scripts/deploy-enhanced.sh` =
  git pull → pre-deploy DB backup → build → migrate → health-checked rolling (start new,
  health-check `:3000/api/health`, then remove old) → auto-rollback (git tag +
  `.deployment-state.json` + `.deployment-backups/`); `rollback.sh` restores to a tag/commit.
  `docker-socket-proxy` (tecnativa) gates Docker API for the instance-manager.
- **Conventions** (`/opt/stacks/CLAUDE.md`): stack dir `/opt/stacks/<n>/docker-compose.yml`;
  persistent data `/srv/appdata/<n>/`; secrets `<n>/.env` (chmod 600, gitignored); external
  `core_net` (172.18.0.0/16) to reach Caddy; private `<n>-internal` for DB/Redis.

## AEON Bench as a new stack (`/opt/stacks/aeon-bench`)
- **Topology:** `aeon-waf-caddy` (Coraza + CrowdSec, positive model — below) → `aeon-app` ×2-3
  replicas (Compose `--scale` / instance-manager rolling) → `postgres-primary` + `postgres-replica`
  on a private `aeon-internal` network (never on `core_net`/tunnel). Data in
  `/srv/appdata/aeon-bench/{pg,keys,artifacts}`. Secrets in `.env`.
- **Domain: `aeon-bench.com`** (purchased) — a NEW Cloudflare zone (point the registrar's
  nameservers at Cloudflare), separate from the homelab `*.lab.unhash.me` zone. Subdomain split
  (your ingest-headroom concern is right):
  | Host | Purpose | Path / traffic |
  |---|---|---|
  | `aeon-bench.com` (+ `www` → apex) | Dashboard, board, arena, auth, admin | browser, steady; `bench.lab.unhash.me`-style ZT tunnel OR proxied origin |
  | `api.aeon-bench.com` | **Pod submission ingest** + MCP control API | server-to-server, heavy/bursty; its **own conventional Cloudflare-proxied origin** (or a dedicated `cloudflared-aeon` tunnel like `cloudflared-fb2b`) so bulk uploads scale independently and never starve the shared tunnel |
  | `aeon-bench.com/.well-known/aeon-bench.json` | Published **trust anchor** (pinned pubkey + build hash) | cacheable, public |
  - The public board is anonymous + uses AEON's own accounts → **not** behind Authentik. The
    **admin** surface optionally gets Authentik `forward_auth` in front of AEON's own admin gate.
  - The app uses relative `/api/*` paths and bearer-token (not cookie) auth, so it's
    origin-agnostic — same app serves both hosts; Caddy maps `api.aeon-bench.com` → the
    ingest/MCP routes with its own WAF allow-list + rate-limits, `aeon-bench.com` → the rest.
  - TLS: Caddy auto-certs via the Cloudflare-DNS module for `aeon-bench.com` + `*.aeon-bench.com`
    (same mechanism as `*.lab.unhash.me`). Cloudflare proxied (orange cloud) on the public records.
- **Auth:** AEON's own accounts for evaluators/admin; pod→mothership uses enrolled device-key
  signed submissions (`docs/attestation.md`), not Authentik.

## Positive security model for the bench WAF (Coraza)
AEON's API surface is small and fully enumerable, which makes a **default-deny allow-list**
practical (a true positive model, on top of CRS's negative rules):
- **Allow-list exact routes + methods:** e.g. `GET /api/leaderboard`, `GET /api/submissions(/{id})?`,
  `GET /api/attestation`, `POST /api/auth/{signup,login,logout}`, `POST /api/arena/vote`,
  `GET /api/arena/match`, `POST /api/v1/runs`, `POST /api/v1/runs/{id}/results`, `GET /api/runs/{id}/manifest`,
  static `/static/*`, `/`. **Everything else → 403** at the WAF.
- **Per-route constraints:** method whitelist, `Content-Type: application/json` required on writes,
  body-size caps (small for auth/vote; larger only on the ingest route), JSON depth/field caps,
  and reject unexpected query params. Auth/vote/match get tight rate-limits (CrowdSec scenarios).
- Keep CRS enabled underneath as defense-in-depth; the allow-list is the primary gate. Decoy/
  honeypot routes can be added as CrowdSec tripwires.

## Deploy script — align to onyx's pattern (replace the Swarm draft)
Mirror `deploy-enhanced.sh`: pull pinned **GHCR** image (pod builds-from-source verify the
image == open code) → **dated backup** (`pg_dump` to `/srv/appdata/aeon-bench/backups/aeon-<ts>.sql.gz`)
→ **health-checked rolling** (`/api/suite`): scale up new replica, wait healthy, drain+remove old,
one at a time → **auto-rollback** to the previous image/tag on health failure (a bad build never
reaches all replicas → 0 impact) → keep N dated backups. Same `.deployment-state.json` +
`.deployment-backups/` versioning + a standalone `rollback.sh`.

## Cloud migration (lift `onyx` → cloud later, no rewrite)
The app already assumes nothing host-specific; keep these invariants:
- **Images in GHCR**, digest-pinned → run identically on a VM, k3s, or a managed runtime.
- **State only in Postgres** (→ RDS / Cloud SQL / Neon) and an **S3-compatible object store**
  behind one interface (local MinIO now → S3/R2 later) for shared/deduped images + artifacts.
- **Proxy/WAF/TLS is a swappable edge:** Caddy+Coraza in-cluster ports to cloud as-is, or swap to
  a cloud LB + managed WAF — the app neither knows nor cares (it only reads `CF-Connecting-IP`).
- **Cloudflare stays the front door** in both worlds (tunnel on-prem ↔ proxied origin in cloud),
  so DNS/edge config is portable.
- **One declarative stack** (Compose now; k3s manifest path noted) ⇒ "go to cloud" is a target
  change, not a redesign. The pod is unaffected — it always just submits to a mothership URL.
