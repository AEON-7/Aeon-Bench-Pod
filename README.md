# AEON Bench Pod

The **open benchmark pod** for [AEON Bench](https://aeon-bench.com): run the
full AEON suite against a model **on your own hardware**, with a controlled, verifiable pipeline —

```
pull (HuggingFace) → verify weights (LFS sha256 + manifest) → serve (recorded recipe)
→ benchmark (text · agentic ×3 harnesses · vision · audio · arena · perf)
→ sign (ed25519 device key) → submit (attested)
```

Results submitted through the controlled flow are **attested** and eligible for the global
leaderboard. Direct-endpoint runs are stored as *self-reported* — useful locally, never globally ranked.

## Quickstart

The only input is the model — everything else auto-defaults (mothership →
`https://aeon-bench.com`, engine auto-detected, device key generated on first enrol):

```bash
git clone https://github.com/AEON-7/Aeon-Bench-Pod.git && cd Aeon-Bench-Pod
AEON_HF_LINK=org/Your-Model  docker compose -f deploy/pod/docker-compose.yml up --build
# pull → hash-verify → serve → bench (incl. Hermes/OpenClaw/OpenCode) → submit to aeon-bench.com
```

Watch it live + launch more runs at **http://localhost:8080**. (Copy `deploy/pod/.env.example`
to `deploy/pod/.env` only to override a default or add an `HF_TOKEN`.)

Or run just the pod dashboard for a GUI (launch runs, saved keys, live progress):

```bash
cd mvp && pip install -r requirements.txt
AEON_ROLE=pod python serve.py           # http://localhost:8080 — use the Run tab
```

Docs: [`docs/pod-quickstart.md`](docs/pod-quickstart.md) ·
[`docs/run-a-benchmark.md`](docs/run-a-benchmark.md) ·
[`docs/attestation.md`](docs/attestation.md) · [`deploy/pod/AGENTS.md`](deploy/pod/AGENTS.md)

## What a full attested run measures

| Dimension | Suite | How |
|---|---|---|
| Text (5 categories × 4 difficulty tiers) | `aeon-suite-v2` | deterministic Tier-0 + binary-rubric Tier-1 |
| Agentic | `aeon-agentic-v2` | 16 environment-execution tasks (file ops + app/game/animation codegen) through **three real harnesses** (Hermes / OpenClaw / OpenCode) in fresh containers, scored on observable file outcomes |
| Vision | `aeon-mvp-vision` | probe-gated image suite |
| Audio | `aeon-audio-v1` | probe-gated, deterministic synthetic stimuli |
| Generative arena | apps / games / animations | seeded prompts, artifacts ship with the signed bundle |
| Performance | `aeon-perf-v1` | direct + through-harness grid, c=1…32, aggregate tok/s + TTFT |

Every run carries its **serve recipe** (exact docker command, engine version, flags), **verified
weights hash** (`repo@revision`), and **detected hardware** — so anyone can reproduce it.

## Trust model (short version)

The pod holds an ed25519 **device key** (`~/.aeon/device_key.pem`). Submissions are signed bundles
over the full result set; the mothership verifies signature + weight verification metadata and
tiers the run (`attested` / `self_reported`). See [`docs/attestation.md`](docs/attestation.md).

---
*Private during hardening — see [`PRE-PUBLIC-CHECKLIST.md`](PRE-PUBLIC-CHECKLIST.md) before flipping public.*
