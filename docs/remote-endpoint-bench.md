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

---

## Attestation: what ranks, and the honest caveat

An endpoint run earns the ranked **`attested`** tier only when the pod can **fingerprint** the live
serve against the verified weights — a greedy-logprob match proves the endpoint really serves those
weights. That reference fingerprint is captured by loading the weights **on the pod**.

- **Pod has a GPU / can load the weights** → the fingerprint is captured, and on a match the run is
  **attested and ranked**.
- **Pod cannot load the weights** (no GPU / not enough RAM — common for a lightweight remote pod) →
  no fingerprint can be taken, so the run honestly records **`self_reported`**: the weights are
  verified and the hardware is correct, but nothing proves *this endpoint* serves them. Stored and
  shown, not globally ranked.

*(Capturing the fingerprint on the serving machine over the same SSH channel — so a
weightless pod can still rank a remote serve — is a planned enhancement.)*

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
