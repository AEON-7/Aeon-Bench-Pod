# AEON Bench — repository & deployment architecture

## Two repos, two trust postures
| | **aeon-mothership** (private) | **aeon-pod** (open source) |
|---|---|---|
| Visibility | Private (the "secret sauce") | Public-able (audit what runs on your box) |
| Contains | Honeypot scheduling + the decoy pool, evaluator-trust math, arena Elo, accounts/sessions, admin, attestation **signing key**, leaderboard aggregation, ingest/verification, the canonical suite authoring | The **bench engine** (suite runner, Tier-0 evaluators, target adapters), the **agent harness(es)**, the local dashboard, the **submit client**, hardware probe, model/harness fingerprinting |
| Why secret | If the decoy pool, honeypot rate, or trust thresholds are public, malicious evaluators game them; the signing private key must never ship | Users must see exactly what touches their machine, their models, and their keys — privacy is only credible if it's auditable |
| Image | `ghcr.io/<org>/aeon-mothership` (you host) | `ghcr.io/<org>/aeon-pod` (you host; users may also build from source) |

The **suite content** is shared: the pod needs the cases to run them. Honeypot/decoy material and the trust math stay mothership-only. The pod ships the public suite + Tier-0 checkers; the mothership re-derives objective scores on ingest (`orchestrated` trust tier) so a tampered pod can't inflate Tier-0.

## Privacy & secure auth (pod → mothership)
The pod runs entirely locally. It only talks to the mothership when the user clicks **Submit**. Documented guarantees (in the open repo):
- **Model API keys never leave the pod.** The target endpoint + key are operator-supplied and used only locally; submissions contain scores + outputs, never keys.
- **Enrollment, run-scoped, single-use keys.** Pod enrolls once (device keypair) → gets a run-scoped token → signs each submission with its device key. Mothership pins enrolled public keys → "signed submissions" (see `docs/attestation.md`). No long-lived shared secret on disk.
- **What is published is explicit and shown before submit:** verified hardware, the signed harness image digest, the verified model identity, the judge identity, the per-case outputs + scores. The user sees the exact payload and consents.
- **Transport:** outbound TLS 443 only; the pod opens no inbound ports to the network (local dashboard binds localhost unless the user opts in).

## Verified submission (what "verified" means)
On submit the pod attaches, each independently checkable:
- **Hardware** — GPU model/VRAM, CPU, RAM, OS, driver; cross-checked against the TEE quote when present (Layer 4), else self-reported + plausibility-checked.
- **Harness** — the agent harness container's **image digest + signed provenance** (cosign/GHCR attestation) → "this exact harness, unmodified."
- **Model** — identity + (local weights) content hash, or HF revision reference (`attest.verify_model_ref`).
- **Judge** — judge model id / "agent" + judge config hash.
This is what powers the cross-cut metrics: **model × harness** and **model × judge** — how a model's score shifts under different harnesses and different judges.

## Harness-in-series validation
A submission can run the **same suite under two harnesses in series** (e.g. a minimal `direct` harness and a richer agentic one) and publish both, so the board can show *capability deltas attributable to the harness*, not just the model. The pod records each harness's signed digest; the mothership stores both result sets keyed to one model+hardware so differences are attributable and reproducible.

## Production topology (mothership)
Matches the `/opt/stacks` zero-downtime pattern; recommended first cut (Docker Swarm for native rolling + auto-rollback), with a k3s path noted for autoscaling later:
- **Traefik** — the only network-exposed service (TLS, load-balances the app replicas).
- **app ×3 replicas** — `deploy.update_config: {order: start-first, failure_action: rollback, monitor}` ⇒ a bad build is health-checked and **auto-rolled-back before it reaches all replicas → zero impact**.
- **postgres-primary + postgres-replica** — streaming replication, **internal docker network only, never exposed**. (The SQLite store is for local pods; the mothership moves the data layer to Postgres for human-vote write volume.)
- **deploy.sh** — dated backup (pg_dump) → pull pinned GHCR image → rolling `stack deploy` (start-first + rollback) → health-gate → on failure, restore backup. "A bad build never rolls out; the failed container is restored; 0 impact."

See `deploy/` for the compose stack, Dockerfile, deploy script, and the GHCR release workflow. **k3s alternative:** the same services map to a Deployment (HPA, `RollingUpdate` maxSurge/maxUnavailable), a StatefulSet Postgres (or CloudNativePG), and an Ingress — chosen at deploy time; the app is written to run under either.

## Build/release flow
Tag in each repo → GitHub Actions builds + pushes a digest-pinned image to **GHCR** (+ cosign signature/SBOM) → `deploy.sh` on the prod host pulls the pinned digest → rolling deploy. The pod's image is reproducible from the open source so users can verify the published image matches the code.
