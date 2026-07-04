# AEON Bench — verification & trust chain

Goal: a third party can trust a published result without trusting the operator —
"this score came from *our* unmodified bench running *that* unmodified model, and the
numbers weren't edited in transit." This is layered; each layer narrows the trust gap,
and we are explicit about where pure software stops and hardware must take over.

## Layer 0 — Deterministic outcomes (the foundation, already built)
The strongest verification is **not needing to trust the scorer at all**: 140/157
cases are Tier-0 programs (`aeon/evaluators.py`). Anyone can re-run the suite against
the same model and recompute the exact same score. Determinism is what makes the rest
of the chain meaningful — a signed wrong number is still wrong.

## Layer 1 — Build attestation (BUILT, `aeon/attest.py`)
- **Build hash** = `sha256` over the sorted manifest of every build-defining file
  (`aeon/*.py`, `suites/*.json`, `web/*`, `serve.py`). A verifier with the source
  recomputes it and checks it equals a known-good release hash.
  `GET /api/attestation/manifest` returns the per-file manifest.
- **Challenge-response signature.** The deployment holds an **ed25519** keypair.
  `GET /api/attestation?nonce=<fresh-random>` returns a signature over
  `(build_hash, public_key, ts, nonce)`. A verifier pins our published public key,
  sends a fresh nonce (replay-proof), checks the signature, that `build_hash` matches a
  known release, and that `ts` is fresh.
- **What this proves:** the responder controls the pinned key and reports the expected
  build hash. **What it does NOT prove on its own:** that the process actually *runs*
  that code — a malicious host can print any `build_hash`. Closing that gap is Layer 4.

## Layer 2 — Signed submissions (BUILT)
`GET /api/runs/{id}/manifest` returns the run's identity + suite hash + scores with an
ed25519 signature over the canonical body (`attest.sign_manifest`). Published results
are therefore tamper-evident: `attest.verify_manifest` rejects any edited field.
Publishing accepts only signatures from pinned, enrolled keys → "signed submissions".

## Layer 3 — Model attestation (PARTIAL, `attest.model_attestation` / `verify_model_ref`)
- **Local weights** (gguf / safetensors on disk): we `sha256` the files → byte-verifiable
  identity, comparable to the HF-advertised hashes.
- **HF reference:** `verify_model_ref(repo, revision)` fetches HF's advertised commit sha
  + per-file LFS sha256, so a claim of "this is `org/model@rev`" is checkable against
  canonical upstream.
- **Honest limit:** a model served behind a remote OpenAI-compatible **API** never exposes
  its weights, so for API targets this is *identity-of-claim*, not *identity-of-bytes*.
  Byte-level model attestation requires either local weights or the provider running the
  bench inside an attested enclave (Layer 4) that measures the loaded weights.

## Layer 4 — Hardware root of trust (DESIGN, not built — the honest boundary)
Pure software self-attestation cannot stop a modified host from lying about what it runs.
To make "the signing key is truly bound to *this exact code executing*" enforceable, wrap
the Layer-1 statement in a hardware-quoted measurement:
- **TPM 2.0 remote attestation** / **AMD SEV-SNP** / **Intel TDX** / **AWS Nitro Enclaves**:
  the TEE measures the container image + loaded model and signs the measurement with a
  manufacturer-rooted key. The verifier checks the hardware quote, then trusts the
  enclosed `build_hash` + run manifest.
- The container runtime fingerprint (image digest, rootfs hash, kernel + cmdline,
  `/proc` measurements) is collected and folded into the quote, so "the benchmark
  container runtime is truly what it says it is" becomes hardware-enforced rather than
  self-reported.
- Our Layer-1/2 API is shaped so a TEE quote slots in **without changing the verifier
  contract** (same pinned-key + nonce + manifest verification, now gated on a valid quote).

## Trust tiers (how this maps to the leaderboard)
- `self_reported` — operator-run, software attestation only (Layers 0–3). Shown,
  segregated, never record-eligible on its own.
- `orchestrated` — AEON re-derives the Tier-0 scores server-side from the captured
  outputs; record-eligible.
- `attested` — Layer-4 hardware quote present; the highest trust badge.

## Verifier checklist (software tiers, today)
1. `GET /api/attestation?nonce=<random>` → verify ed25519 sig against the **pinned**
   public key; confirm `build_hash` == known release; `ts` fresh.
2. Recompute `build_hash` from the published source; compare.
3. For each published result, `verify_manifest` the signed run manifest.
4. If a model identity is claimed, `verify_model_ref` against HF (and weight-hash if local).
5. Re-run Tier-0 yourself for spot-check reproduction (the ultimate check).
