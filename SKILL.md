---
name: run-aeon-benchmark
description: >
  Use when asked to run, execute, benchmark, evaluate, or score an LLM with AEON Bench.
  You deploy the AEON Bench **Pod** locally and drive the whole verified flow — pull or
  hash-verify a model → serve it → run the comprehensive benchmark (quality + speed +
  agentic + vision/audio/video) → submit the signed, attested result to the public
  leaderboard. An AI agent can do the ENTIRE job through the pod's MCP tools, no clicking.
  The mothership (aeon-bench.com) is a READ-ONLY leaderboard + submission endpoint; it
  never runs a job — all benchmarking happens on the pod.
---

# Skill: Run an AEON Bench benchmark (agent-driven, end to end)

**Two truths up front.** (1) Benchmarking happens on the **Pod** — a local appliance you run on
the user's hardware. The **mothership** is just the public scoreboard + the place finished results
are submitted; it never starts a run. (2) Only a **complete, validated (attested)** run ranks:
the pod pulls/verifies the model against Hugging Face, serves it, runs the *whole* suite, signs
the result, and submits it. A quick smoke test or a raw-endpoint run is for local eyes only —
**never present one as validated.**

Full operational detail lives in [`AGENTS.md`](AGENTS.md) (repo root). This skill is the short
loop; the judge/scoring determinism contract is `AGENTS.md` **Appendix (the judge role)**.

---

## 0. Get a current pod running (always pull latest first)

```bash
docker pull ghcr.io/aeon-7/aeon-pod:latest && docker rm -f aeon-pod   # ALWAYS pull before a session
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Dashboard on **http://localhost:8091**. Platform variants (Apple silicon drops `--gpus`, swaps
`--network host` for `-p 8091:8091`; CPU-only likewise) are in `AGENTS.md` §2. **Pull latest
before every benchmarking session** — a stale pod can serve old recipe defaults or an outdated
suite, so its results won't line up with the live board.

## 1. Drive it via MCP (the headless path — preferred for agents)

The pod ships a dependency-free MCP server, **`mvp/mcp/aeon_pod_mcp.py`**. Point your MCP client
at it (it talks to the pod over HTTP; run it wherever your agent runs):

```json
{ "mcpServers": { "aeon-bench-pod": {
    "command": "python",
    "args": ["/path/to/Aeon-Bench-Pod/mvp/mcp/aeon_pod_mcp.py"],
    "env": { "AEON_BASE": "http://127.0.0.1:8091" }
} } }
```

It's a single stdlib file, so you can also just fetch it:
`curl -O https://raw.githubusercontent.com/AEON-7/Aeon-Bench-Pod/main/mvp/mcp/aeon_pod_mcp.py`.
(Set `AEON_POD_TOKEN` if the pod has its optional lab lock enabled.)

**The tools, in the order you use them:**

| Tool | What it does |
|---|---|
| `aeon_pod_info` | Confirm you're on a live pod (`role: pod`), token required? |
| `aeon_pod_scan_models` | Models already on disk (each hash-verifiable against its HF repo) |
| `aeon_pod_engines` | Engine catalog for this host + the recommended default |
| `aeon_pod_champion_recipes` | Best proven recipes for the detected hardware (one-click templates) |
| `aeon_pod_validate` / `aeon_pod_validate_status` | Pre-check a model resolves + hash-verifies (optional) |
| **`aeon_pod_run`** | **Launch the VALIDATED comprehensive benchmark** (the main event) |
| `aeon_pod_jobs` / `aeon_pod_job` | Track status + per-stage progress + pending submissions |
| `aeon_pod_stats` | Live tok/s, active/queued streams, GPU/RAM (proof it's progressing) |
| `aeon_pod_resume` | Continue an interrupted job from its last scored case |
| `aeon_pod_submit` | Push a finished job to the mothership (idempotent) if auto-submit failed |
| `aeon_pod_leaderboard` / `aeon_pod_suite` | Read the board / the suite summary |
| `aeon_pod_guide` | This verified-path playbook, from the pod |

## 2. The VERIFIED PATH — point at a model three ways (all earn attested)

This is the part to get right; it is what makes the result trustworthy and rankable.

1. **Fresh Hugging Face pull.** Pass `hf_link` = `org/Model` (or a huggingface.co URL) to
   `aeon_pod_run`. The pod downloads the weights and **sha256-verifies every file against HF's
   published LFS manifest** before serving.
2. **A model already on disk (hash-verified, no re-download).** Call `aeon_pod_scan_models`, pick
   the entry, then call `aeon_pod_run` with **both** `hf_link` (the reconciled repo id) **and**
   `local_dir` (the on-disk path). The pod hashes the local bytes against that repo — a match is
   "good as gold"; a mismatch **hard-stops** the run (it refuses to benchmark unverified weights).
3. **Point-and-pull.** If the user names a model that isn't local, just use path (1) — the pod's
   built-in fetch pulls it fresh and verifies it.

> A raw OpenAI-style **endpoint** (a server you already run) is `self_reported`: shown locally,
> **never ranked**, because the pod can't hash the weights behind an API. Use it only for a
> deliberate private comparison — never as the validated deliverable.

## 3. Recipe → run → monitor → submit

1. **Recipe.** Prefer `aeon_pod_champion_recipes` → apply the top champion's `serve_flags` (and
   spec-decode / drafter) via `aeon_pod_run`. Else omit them and the pod auto-applies a family
   preset. On **unified-memory** boxes (DGX Spark GB10) keep `--gpu-memory-utilization` at
   **0.6–0.7** — above ~0.8 the shared CPU+GPU pool page-thrashes; discrete-VRAM GPUs can go higher.
2. **Run comprehensive.** `aeon_pod_run` defaults `preset: "comprehensive"` = the whole exam
   (text · 3 agentic harnesses · vision · audio · video · arena · perf). **Validated means
   comprehensive** — do not submit `hard-bench`, subset, or smoke runs as validated.
3. **Monitor + tell your human.** Poll `aeon_pod_jobs` for per-stage progress and `aeon_pod_stats`
   for live tok/s + streams. **A big/slow model's full run takes HOURS — say so up front**, and
   give the human the dashboard URL (`http://<pod-host>:8091`) so they can watch. If interrupted,
   `aeon_pod_resume` continues from the last scored case.
4. **Submit.** Completed validated runs auto-submit. If the mothership was unreachable, the
   results persist and `aeon_pod_submit` pushes them later — idempotently (a finished job can't
   land twice; you'll get *"job already submitted and available on the Mothership"*).

## 4. Not using MCP? The same flow is in the GUI

Open **http://localhost:8091 → Run tab**, work the **"◉ Validated bench"** card top to bottom
(paste HF link or ⌕-scan a local model → apply a ★ champion recipe → keep Test plan =
Comprehensive → launch). The illustrated tour is [`docs/walkthrough/README.md`](docs/walkthrough/README.md).

## The golden rules (don't break these)

- **Pull the latest pod image before you queue any job.**
- **Only attested + comprehensive runs rank.** Attested = weights hash-verified against HF +
  recipe + signature; comprehensive = the whole suite (≥90% coverage). Everything else is local-only.
- **Never dress a smoke test, subset, or endpoint run up as validated.**
- **Never bypass weight verification** — a `WEIGHTS VERIFICATION FAILED` stop is by design.
- **The mothership never runs jobs.** All benchmarking is on the pod; the mothership only shows
  the leaderboard and accepts signed submissions.

*How scoring works (Tier-0 programs + deterministic binary-criteria Tier-1 + a creativity overlay)
is the determinism contract in `AGENTS.md` Appendix (the judge role) — read it if you need to
understand or defend a score, not to run a benchmark.*
