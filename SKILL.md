---
name: run-aeon-benchmark
description: >
  Use when asked to run, benchmark, evaluate, or score an LLM with AEON Bench.
  You run the AEON Bench Pod on the user's machine, point it at a model, run the
  benchmark, and submit the signed result to the public leaderboard at aeon-bench.com.
  All work happens on the pod. The mothership only shows the board and accepts results.
---

# Run an AEON Bench benchmark

**What you are doing:** start a pod (a Docker container), point it at a model, run the benchmark,
hand the user a link. The pod verifies the model's weights against Hugging Face and signs the
result, which is what makes it rankable.

**Before you start, tell the user this:** a full run takes **hours** on a large model. That is
normal. They can close the tab; it keeps going.

---

## Step 1 — Start the pod

Always pull first. A stale pod produces results that do not match the live board.

```bash
docker pull ghcr.io/aeon-7/aeon-pod:latest && docker rm -f aeon-pod
docker run -d --name aeon-pod --network host --gpus all \
  -v /var/run/docker.sock:/var/run/docker.sock -v aeon-pod-state:/root/.aeon \
  -v "$HOME/aeon-models:/models" -e AEON_MODELS_HOST_DIR="$HOME/aeon-models" \
  -v "$HOME:/host-home:ro" -e AEON_HOST_HOME_DIR="$HOME" \
  ghcr.io/aeon-7/aeon-pod:latest
```

Every mount matters. Drop the docker socket and the coding-agent tests cannot run.

- **No NVIDIA GPU** (Apple silicon, CPU-only): remove `--gpus all`, replace `--network host` with
  `-p 8091:8091`.
- Check it worked: `curl -s localhost:8091/healthz`. The dashboard is http://localhost:8091.

Then connect the MCP server so you can drive it without clicking:

```json
{ "mcpServers": { "aeon-bench-pod": {
    "command": "python",
    "args": ["/path/to/Aeon-Bench-Pod/mvp/mcp/aeon_pod_mcp.py"],
    "env": { "AEON_BASE": "http://127.0.0.1:8091" }
} } }
```

No MCP? Use the dashboard: **Run tab → paste the HF link → launch**. Same flow.

---

## Step 2 — Decide how to point at the model

**Ask one question: is the model already running as a server?**

**YES — it is already serving.** Use this. It does not download and does not restart their server.

```
aeon_pod_scan_endpoints()            → gives you `hf_guess` (the model's HF repo) and the URL
aeon_pod_run(hf_link=<hf_guess>, serve_url=<url>, verify_endpoint=true)
```
If the server is on a **different machine**, add `remote_host="user@host"` to BOTH calls, and run
`aeon_pod_ssh_key()` first so the user can authorize this pod's key on that host. Without
`remote_host` the result is filed under the wrong hardware.

**NO — it is not running.** Use this. The pod downloads and verifies the weights.

```
aeon_pod_run(hf_link="org/Model")
```

**Already downloaded and you want to skip the re-download:**

```
aeon_pod_scan_models()                                    → gives you the repo id and path
aeon_pod_run(hf_link=<repo id>, local_dir=<path>)
```

**`hf_link` is required in all three.** It is the identity the pod hash-verifies. A run without it
can never rank, so never omit it to "save time".

Point at the **exact repo being served** — the specific quant, not the base model.

---

## Step 3 — Run it

`hf_link` is the only required argument. Everything else already defaults correctly:
`preset="comprehensive"` is the full exam and the only shape that ranks **on the global board**.

Do not set `preset`, `engine`, `serve_flags`, or `concurrency` unless the user asked for something
specific. The pod picks the right recipe for their hardware.

One optional improvement: `aeon_pod_champion_recipes()` returns proven settings for the detected
hardware. If it returns one, pass its `serve_flags` to `aeon_pod_run`.

**The two presets that rank**, so you can pick when the user asks for one by name:

| Preset | What it is | Where it ranks |
|---|---|---|
| `comprehensive` *(default)* | The full exam: text · 3 coding-agent harnesses · vision · audio · video · arena · performance. | Global leaderboard. |
| `god-mode` | Beyond-frontier only: god-tier questions + 15 god coding-agent tasks + arena + performance. Most models score low; that is the point. | Its own GOD MODE board. |

Anything else (`--fast`, `--limit`, `--difficulty`, `--category`) is a local check and never ranks.

**If the user hands you a specific serve recipe**, use it as given and change only what is unsafe
for their hardware. The one value to check: `--gpu-memory-utilization` above ~0.8 hangs a
unified-memory box (DGX Spark) — drop it to 0.6–0.7 and say you did. Also set `--perf-max-conc` to
match the serve's `--max-num-seqs`, or the top of the performance ladder measures queueing instead
of throughput. Everything else — quantization flags, spec-decode, parsers — comes from the model's
Hugging Face card; read it before writing a recipe. Details in [`AGENTS.md`](AGENTS.md) §4(c-quant)
and §4(f).

---

## Step 4 — Watch it

```
aeon_pod_jobs()      → per-stage progress
aeon_pod_stats()     → live tokens/sec, proof it is moving
```

Give the user the dashboard URL so they can watch: `http://<host>:8091`.

**Read what the pod prints before the coding-agent stage.** If it says `WRONG TOOL-CALL PARSER`, it
also prints the exact flag to restart the serve with. Apply it and re-run — otherwise that third of
the score comes back near zero for a reason unrelated to the model.

If the run is interrupted: `aeon_pod_resume()` continues from the last scored case. Nothing is lost.

---

## Step 5 — Hand off

Finished runs submit themselves. If the mothership was unreachable, `aeon_pod_submit()` pushes them
later — it is idempotent, so calling it twice is safe.

Tell the user: the model name, the score and rank, the link, and one sentence on why it is
trustworthy ("weights hash-verified against Hugging Face and signed").

---

## If something goes wrong

| What you see | What to do |
|---|---|
| `WEIGHTS VERIFICATION FAILED` | Stop. This is correct behaviour. The repo does not match the weights. Do not work around it. |
| A harness image will not build | The pod prints the prerequisites and the exact build command. Usually the docker socket mount is missing. |
| `WRONG TOOL-CALL PARSER` | Restart the serve with the flag it prints, then re-run. |
| The run finishes but agentic scored ~0 | The plumbing failed, not the model. It still ranks, badged "agentic untested". Fix and re-run when you can. |
| Submission rejected `NOT_ATTESTED` | The weights were not verified against Hugging Face. Re-run with a correct `hf_link`. |
| It is taking hours | That is normal. Do not kill it. |

---

## Rules

1. **Pull the latest pod image before every session.**
2. **Always pass `hf_link`.** Without it the run cannot rank.
3. **Do not present a smoke test, subset, or unverified run as validated.**
4. **Never bypass weight verification.** A verification stop is by design.
5. **Do not kill a slow run.** Benchmarks legitimately take days on large models.
6. **A run missing its coding-agent score is still worth submitting.** It ranks on what it measured
   and says what to fix. A partial honest result beats no result.

Deeper detail — engines, recipe flags, tool-call parsers, remote serves, the scoring contract —
is in [`AGENTS.md`](AGENTS.md). You do not need it for a normal run.
