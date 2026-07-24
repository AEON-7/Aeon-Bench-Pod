# Benchmark a model running on ANOTHER machine

*Point a Bench Pod at a live serve on a different computer, and still earn a properly-attributed,
attestable result.*

Use this when the model you want to benchmark is **already serving on one machine, and you want to
run the pod from another** — e.g. the serving box is a DGX Spark (or a multi-Spark cluster) with no
room to also run the pod, or you simply keep the pod on a separate workstation.

You do **not** re-download the weights onto the serving box, and you do **not** disturb the running
serve. The pod benches it in place, over the network.

> **Why not just paste the serve URL?** You can — the bench will run. But then the result is filed
> under the **pod's** hardware, not the serving machine's. The Performance board normalizes 0–100
> *within* a hardware bucket, so a laptop-pod benching a DGX would file the Spark's throughput in
> the laptop bucket, topping it and crushing every real laptop row. `--remote-host` fixes that by
> probing the **serving** machine over SSH, so the run lands under the right rig.

---

## What you need

- **The pod machine** — runs AEON Bench Pod. Needs `ssh` and outbound network to the serving box.
  It does **not** need a GPU (it never serves the model), but it does need disk + bandwidth to pull
  and hash-verify the weights once.
- **The serving machine** — already running an OpenAI-compatible server (vLLM / SGLang / TGI /
  llama.cpp / Ollama / LM Studio), reachable from the pod over HTTP, with `ssh` access you can
  authorize a key on. If the model runs in Docker there, the pod can also read the **real serve
  recipe** (flags, spec-decode config) over the same SSH channel.

---

## Step 1 — get the pod's SSH key

The pod owns a dedicated identity at `~/.aeon/id_ed25519`, created on demand. You authorize **that**
key on the serving machine, so you never touch the pod's other SSH config.

- **Dashboard:** Run tab → **◉ Point at a running model** → type the **Serving machine** field. The
  authorize command appears, one per shell, with copy buttons. (This also creates the key.)
- **API:** `GET /api/pod/ssh_key` → `{ pubkey, pub_path }` (public key only; the private key never
  leaves the pod).
- **MCP:** the `aeon_pod_ssh_key` tool.

## Step 2 — authorize the pod on the serving machine

Run **one** of these on the machine where you have SSH access to the serving box (replace
`user@host`, e.g. `albert@192.168.1.116`). Each appends the pod's public key to that host's
`~/.ssh/authorized_keys`.

**Windows PowerShell** (there is no `ssh-copy-id` on Windows; this pipes the key and strips the CR
that PowerShell would otherwise append — a stray `\r` silently corrupts the authorized_keys line):

```powershell
Get-Content "$env:USERPROFILE\.aeon\id_ed25519.pub" | ssh user@host "mkdir -p ~/.ssh && chmod 700 ~/.ssh && tr -d '\r' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

**macOS (zsh) / Linux (bash):**

```bash
ssh-copy-id -i ~/.aeon/id_ed25519.pub user@host
```

That command itself authenticates to the serving box the usual way (your existing key or a
password); once it runs, the pod's own key is authorized and the pod can connect unattended.

**Verify** (from the pod machine):

```bash
ssh -i ~/.aeon/id_ed25519 user@host echo OK
```

## Step 3 — point the pod at the serve

**Dashboard:**
1. Run tab → **◉ Point at a running model**.
2. **Serving machine**: `user@host` (the SSH destination).
3. **⌕ Scan for running instances** — the pod sweeps the serving host's ports *and* inspects its
   Docker daemon over SSH, listing each live serve with its autodetected HF repo. Click **use this**.
4. **HF link**: the repo of the **exact artifact being served** — the specific quant, not a base
   model. (Autofilled when the serve exposes it; otherwise paste it. Quantized safetensors and a
   single GGUF file both hash-verify bit-for-bit; a base repo will fail verification.)
5. Leave **Verify endpoint (fingerprint)** on.
6. **RUN BENCH**.

**CLI:**

```bash
python -m pod.aeon_pod \
  --hf-link <org/exact-quant-repo> \
  --serve-url http://<serving-host>:8000/v1 \
  --remote-host user@host \
  --verify-endpoint \
  --preset comprehensive
```

`--endpoint-model <id>` picks which served id to address when the endpoint serves several.

**MCP:**

```
aeon_pod_scan_endpoints(remote="user@host")
aeon_pod_run(hf_link="org/exact-quant-repo",
             serve_url="http://<serving-host>:8000/v1",
             remote_host="user@host",
             verify_endpoint=true)
```

---

## What gets recorded

| Field | Source |
|---|---|
| **Hardware** | probed on the **serving machine** over SSH (`nvidia-smi`, `uname`). Marked `hardware_source: probed-remote`, `bench_host: user@host`. If the probe fails, the run is filed **UNLABELED** — never invented from the pod's hardware. |
| **Serve recipe** | read from the serving machine's Docker daemon over SSH — the **real** image + flags, including `--speculative-config` (DFlash/DSpark) and the drafter repo. Shown verbatim in *replicate this run*. |
| **Weights** | pulled to the pod once and hash-verified bit-for-bit against Hugging Face (as for any attested run). |
| **Serving-integrity** | the backing container's on-disk model (config.json + weight manifest, and with `--deep-verify` the per-file sha256) checked against HF — a GPU-free reliability guard (see below). Recorded in the deployment manifest. |

---

## Serving-integrity: "am I benching the model I think?"

Before it benches a serve it did **not** launch, the pod confirms the endpoint is actually serving the
model you named — a **GPU-free reliability check** that catches the everyday accidents:

- you pointed at the **wrong live instance** (three servers up, grabbed the wrong port),
- it's the **wrong size** (a 7B when you meant the 70B),
- it's the **wrong quant**, or
- the weights on disk are **stale / a different fine-tune**.

It works by inspecting the backing container over the **same channel** already used for the serve
recipe — locally, or over SSH for a remote serve — and comparing to the HF-verified reference:

1. **config.json** — architecture, layer count, hidden size, vocab, quant method vs the HF config.
2. **weight manifest** — the served safetensors shard set (or the single GGUF) vs the HF file set.
3. **`--deep-verify` (opt-in)** — sha256 of the **served weight files** vs HF's published per-file
   hashes. This reads every shard on the serving host (slow over SSH), so it's off by default; the
   config + manifest check already catches wrong-model / wrong-size / wrong-quant.

A clear structural **mismatch HALTS the run** — the pod refuses to benchmark a model that isn't the
one you named, and tells you exactly which field differed. A match prints a green confirmation.

> **This is a safety guard, not the ranked-attestation gate.** Everything it reads is reported by the
> serving host, and a file on disk isn't proof the running process *loaded* it — so it does **not**
> defeat a deliberate faker who mounts the real weights but serves something else. (You also can't
> sha256 the weights resident in VRAM against HF's *file* hash — the engine fuses/repacks/shards/
> requantizes them on load — which is why it hashes the **files the container was launched with**.)
> With `--deep-verify` (per-file sha256), a **complete** match of the running container's weights
> against HF earns the ranked **`endpoint_verified`** tier (see Attestation below) — host-attested,
> for a cooperative operator. The behavioral fingerprint remains the stronger, cheater-resistant
> path. The integrity check is what stops honest mistakes; the fingerprint is what stops cheating.

---

## Attestation: what ranks, and the honest caveat

A remote endpoint run can reach the ranked **`attested`** tier two ways. Both start from the same
floor — the mothership independently re-verifies the weights are authentic HF weights — and differ
only in **how the run is bound to that specific serve**. The method is recorded on the run, so the
board is transparent about which proof was used.

**1. `endpoint_fingerprint` — behavioral (strongest, cheater-resistant).** The pod loads the
HF-verified weights via vLLM on a GPU **the submitter controls**, computes greedy-logprob canaries,
probes the live endpoint, and compares. A match proves the *running process* really serves those
weights. The pod needn't be the serving machine — any GPU box pointed at the remote serve works; it
loads the model only briefly, at low context. This is the bar that resists a determined faker.

**2. `endpoint_verified` — container-hash (host-attested, catches accidents).** When no GPU is
available to compute a behavioral reference, the pod instead **hashes the running container's actual
weight files** (`docker exec … sha256sum`, over the same SSH channel) and requires **every** file to
match HF's published per-file sha256, tied to this bundle's `weights_hash` (run with
`--deep-verify`, or it happens automatically when `--verify-endpoint` finds no local GPU). This
proves the serve was **launched with these authentic weights** — closing the wrong-model / wrong-
instance / wrong-quant gap without a second model load.

> **The honest difference.** `endpoint_verified` is **host-asserted**: the serving host runs the
> hash, and a file on disk isn't proof the live *process* loaded it — so a *deliberate* faker who
> mounts the real weights while serving something else can defeat it. It is **not** a substitute for
> the behavioral fingerprint against an adversary; it is the right, sufficient proof for a
> **cooperative operator** benchmarking their own serve, which is the common case. (You cannot hash
> the weights resident in VRAM against HF's *file* hash — the engine fuses/repacks/shards/requantizes
> them on load — which is why this hashes the **files the container was launched with**.) The badge
> names which proof a run carries, so viewers can weigh it accordingly.

**Anything weaker stays `self_reported`** (stored, shown, not ranked): a config/manifest-only check
with no complete weight-hash match, a partial hash, or an endpoint the pod couldn't inspect at all.

---

## Troubleshooting

- **"Permission denied" / nothing detected after authorizing.** Re-check Step 2's verify command.
  Note the pod runs Docker commands as `ssh -i <pod key> user@host docker …` — it does **not** use
  Docker's `DOCKER_HOST=ssh://` transport (that can't be told which key to use). So the pod's key
  must be authorized for the **exact** `user@host` you typed.
- **Scan finds nothing but the serve is up.** The scan derives the HTTP host from the SSH
  destination (`user@host` → `host`). If you used a bare `~/.ssh/config` **alias** (no dotted
  IP/hostname), the pod can SSH to it but can't derive an HTTP address to probe — type `user@ip`, or
  paste the **Serve URL** directly.
- **Result filed under the wrong hardware.** You launched with just a serve URL and **no**
  `--remote-host`. Re-run with `--remote-host user@host`.
- **`hf_guess` is null on the scanned serve.** The server didn't expose its repo (e.g. it serves
  from a local path under an alias). The pod surfaces the model's folder name as a hint; supply the
  exact quant's HF repo yourself.

---

## See also

- [Run a Benchmark](run-a-benchmark.md) — the full run flow and trust tiers.
- `AGENTS.md` §4(a) — the agent runbook for pointing at a running model.
- [Attestation](attestation.md) — what each trust tier proves.
