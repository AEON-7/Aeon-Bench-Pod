# AEON Bench — Vision Evaluation Matrix

> Status: implementation-ready for the static (single-image) dimensions; **video/motion are design-complete but feasibility-gated** behind a frame-decomposition path that is honestly bounded (see §5). Consistent with `DESIGN.md` v0.3 (§6 capability gating, §6b tiered determinism, §10b BYO judge, §11 speed honesty, §12 server-side re-derivation, §14 coverage, §17 schema). Small-team ethos: the cheap decisive controls are mandatory; the heavyweight machinery is deferred and flagged.

The VISION board is a **standalone evaluation graph**, separate from the text leaderboard and from the AUDIO board. A model appears here **only if** the capability probe confirms it ingests `image_url` content blocks. A model without vision simply does not appear — it is **never zero-penalized** on the text board (§6). Coverage is shown as a per-board badge ("vision composite over 9/11 dimensions"). Speed is a separate column set, never a summand (§14).

This document supersedes the earlier VISION architect draft on three points the adversarial review found over-stated: **bbox grounding**, **video/motion frame regeneration**, and **vision speed measurability**. Where a prior claim does not survive scrutiny on the user's actual stack (LM Studio /v1, qwen3-vl-30b/8b), it is dropped or down-ranked here, explicitly.

---

## 0. Feasibility grounding (what is actually true on this stack)

The user's fleet is served by LM Studio behind an OpenAI-compatible `/v1` API. The MVP runner (`mvp/aeon/targets.py`) streams `/v1/chat/completions` and captures TTFT / decode tok/s / e2e. `_clean()` copies only `role` + `content` and passes list-of-blocks `content` through untouched, so the `image_url` transport works without a timing-path change.

| Fact | Status on this stack | Consequence for the board |
|---|---|---|
| `image_url` base64 data-URI blocks | **Confirmed** for `qwen3-vl-30b`, `qwen3-vl-8b` | Single-image dimensions D1–D9 are live |
| A third image model (`gemma-3-12b` family; the prior draft's "gemma-4-12b" is **not a real id** — confirm the served id before listing) | **Probe-then-trust** | Admitted only if it passes the board-gate sanity probe; a gibberish responder is excluded, not scored 0 |
| Native video (`video_url` content type) | **Unsupported** over `/v1` | D10/D11 run as frame-decomposition (multi-image) only; native video is **not exercised** |
| Per-image token usage in `/v1` `usage` | **Generally not exposed** (LM Studio reports combined `prompt_tokens`, not an image breakdown) | Token-per-image speed metrics are **demoted to diagnostics, hidden when null, never fabricated** (§3) |
| MockTarget (offline demo) | **Image-blind** today | A `MockVisionTarget` persona is required or the board can't be exercised without a GPU (§6) |

**Single-vision-family consequence (record eligibility).** The only vision models on the fleet are `qwen3-vl-30b` and `qwen3-vl-8b` — the **same family**. A cross-family vision verifier (§6b.4) is therefore structurally unavailable. Any un-shadowed vision Tier-1 criterion is permanently `single_judge` and **non-record-eligible** here. The board is therefore designed to be **Tier-0-dominant for record purposes**; Tier-1 descriptive grounding (D8) contributes diagnostics only until a second vision family is reachable. This is stated up front because it changes how D8 is weighted (§1, §4.7).

---

## 1. Capability gating (mandatory, runs before any case)

The vision target resolver (`probe.py`) runs a tiny content-pinned mini-suite and writes the result to the run row (`vision_probe_json`). For the MVP we keep this lightweight — a `board` column on `runs`/`results` plus the probe blob — and **defer** the full normalized `target_capabilities` table to M6 (small-team ethos: the heavyweight schema buys nothing while the fleet is two models).

### 1.1 Board gate — color, not OCR

The board-admission probe sends one synthetic **solid-color square** (a 64×64 PNG, content-pinned bytes) and asks *"What single color fills this image? One word."* Pass iff the normalized reply contains the gold color.

This **fixes a review blocker**: the earlier draft gated the whole board on an OCR probe (`AEON7`), which conflates "ingests images" with "can OCR." A model that sees images but reads text poorly would have been wrongly excluded from counting/color/VQA it could pass. Color is the correct gate; OCR becomes a **sub-flag** that only gates OCR cases.

### 1.2 Control-pair: did the image actually reach the model

Alongside the gate question, send the **same question with the image block removed**. If the answer is identical with and without the image, the model is provably ignoring the block → `unsupported` (not `supported_weak`). This makes "did the modality reach the model" a deterministic comparison, never a judge call.

### 1.3 Sub-flags (gate individual dimensions, not the board)

| Sub-flag | Probe | Gates |
|---|---|---|
| `vision_ok` (board gate) | color square | the whole board |
| `vision_ocr` | 96×32 PNG rendering pinned token `K7Q` | D2 OCR, D7 fine-detail-string |
| `vision_count` | PNG with exactly 3 dots | D4 counting |
| `vision_multi_image` | two image blocks (red, then blue); "color of the SECOND image?" | D9, **and D10/D11 (frames are a usage pattern of multi-image)** |

**`vision.frames` is not a separate capability.** N image blocks is functionally identical to multi-image; there is no distinct API transport to probe. The earlier draft's separate `vision.frames` gate could spuriously exclude a model from D10/D11 if an 8-image payload tripped a context/payload limit that the 2-image probe didn't. We therefore define frames as "multi-image at N=8 within payload/context limits"; a payload-limit failure is a **coverage exclusion with a distinct reason**, not `capability=false`.

A failed sub-flag gates its dimension to `N/A` (counted only in the coverage denominator), never scored 0.

---

## 2. The dimension set

Tier legend: **0** = programmatic, no model judge · **1** = binary evidence-grounded rubric (judge = launching/target model, §10b) · **2** = arena only, excluded from the auto-composite.

Every Tier-0 case forces the answer into a **fenced typed slot**, and **only the slot is scored** (`on_missing: fail`, no whole-text fallback — see §2.1). The model may reason freely before the slot; capability is exercised, but grading is a pure function, not prose-parsing (§6b.2.2).

| # | Dimension | What it tests | Tier | Deterministic scoring | Checker(s) | Judge-invariance basis |
|---|---|---|---|---|---|---|
| **D1** | Image understanding / VQA | Closed question about one image | **0** | MCQ letter / canonical entity set into `<answer>` | `exact_match`, `set_match` | Closed answer space + pinned reference; same bytes → same boolean |
| **D2** | OCR | Read printed/scene text verbatim | **0** | CER ≤ τ vs pinned reference (per-case τ + normalization) | `cer_threshold` | CER is pure edit-distance; **τ/normalization are authoring params carrying validity, not stability, risk** (§2.2) |
| **D3** | Document / chart / diagram QA | Read a value/cell/node | **0** | numeric ± tol, or closed entity | `numeric_tolerance`, `exact_match`, `set_match` | Reference + tolerance pinned; **prefer generated charts** so the gold is machine-known (§2.4) |
| **D4** | Counting | Count instances of a named class | **0** | exact integer in `<count>` | `numeric_exact_slot` (new, slot-strict) | Integer equality is judge-free; **prefer synthetic scenes** so "what counts" is unambiguous |
| **D5** | Spatial / relational | left/right/above/inside, nearest, ordering | **0** | closed enum into `<answer>` (slot-exact, not substring) | `exact_match` over closed set, `set_match` | Closed pinned set; slot-only matching (§2.3) |
| **D6** | Visual grounding (bbox) | Localize a referred object | **0 (synthetic) / diagnostic (natural)** | IoU ≥ τ after convention-normalization | `json_schema` → `bbox_iou` | **Re-spec'd — see §2.5; natural-image bbox is demoted to diagnostic** |
| **D7** | Fine detail | Tiny/low-contrast detail | **0** | exact/set on detail, or CER for a read-a-string detail | `exact_match`, `set_match`, `cer_threshold` | Single pinned ground-truth token |
| **D8** | Descriptive grounding | When a dimension genuinely needs grounded description | **1 (diagnostic on this fleet)** | binary present/absent visual-fact criteria | `judge_verdict.v1`, Tier-0-shadowed where possible | Each criterion is read-a-fact; **but single-family fleet ⇒ non-record-eligible (§0, §4.7)** |
| **D9** | Multi-image reasoning | Compare/aggregate across ≥2 images | **0** | closed answer (which image / same-diff / index) into slot | `exact_match`, `set_match` | Requires `vision_multi_image`; answer closed + pinned |
| **D10** | VIDEO understanding | Event/sequence from ordered frames | **0, feasibility-gated** | closed answer about ordering/event/state | `exact_match`, `set_match` | **Frames = pinned image bytes, not regenerated; see §5** |
| **D11** | MOTION / action | Direction/action from frame deltas | **0, feasibility-gated** | closed action vocab / direction enum | `exact_match` over pinned set | **Prefer synthetic frame sequences so motion truth is machine-known; see §5** |
| **— SPEED —** | Image-ingest throughput | Vision latency | **measurement** | separate column set, never a summand | §3 | Measurement, not judgment; co-location flagged (§11) |

**Tier-2 carve-outs (arena only, never in the composite):** "is this caption evocative", "which description is better", image aesthetic/mood ("describe the atmosphere", "is this scene beautiful/tense"). These route to the human Bradley-Terry arena (§13), exactly as prose aesthetics do.

### 2.1 The slot-strict extraction rule (fixes a real code/spec gap)

The existing `chk_numeric_tolerance` (`evaluators.py`) does `src = slot if slot is not None else candidate` — when the slot is absent it scans the **entire** candidate for numbers. That is the opposite of `on_missing: fail` and lets a model game a count by rambling numbers ("about 5 to 7 cars"). **Vision Tier-0 must not reuse that fallback.**

Required:
- A shared `extract_answer(text, slot)` reading the fenced slot via RE2 with `on_missing: "fail"` — the only admissible non-`inconclusive` value (§6b.3 lint). Missing slot → `satisfied=false`, no whole-text scan.
- A new **`numeric_exact_slot`** checker for D4: extract the `<count>` integer; compare `== reference` under a pinned `single_integer` grammar. Do **not** reuse the set-subset heuristic.
- D5/D9/D11 closed-set matching is **exact match on slot contents**, never substring scan over prose. ("the cup is left of the bowl, not the right" contains both "left" and "right"; "right" is a substring of "bright" — substring-first-match-wins is order-dependent and mildly judge-variant. Banned.)

### 2.2 D2 OCR — judge-invariant, but acceptance is an authoring decision

CER is a pure function (§6b.2.4, unrestricted), so it is **judge-invariant**. But the review correctly notes that judge-invariance ≠ validity: `max_cer` and the normalization are dials that decide pass/fail.

Resolution (re-spec of acceptance, **not** a tier change — D2 stays Tier-0):
- **Per-case** τ and normalization, not one global. A number-heavy receipt keeps digits/punctuation significant (`1,947` ≠ `1947`, `0600` ≠ `06:00`); a prose crop may normalize whitespace/case/punctuation.
- The **reference stores its canonical normalized form** so scoring direction is fixed at authoring; the runtime does not re-normalize the reference with a normalizer that could drift.
- Calibrate τ against a small human-gold "acceptable transcript" set, not a round number, and record that the threshold carries validity risk per §6b.3.5.
- **Dead-band** (default δ=0.01): `τ−δ ≤ CER ≤ τ+δ` → `partially_scored` → drift queue, never a knife-edge pass/fail (§6b.2.4).

### 2.3 D5 Spatial / D9 / D11 — closed-set on the slot only

Slot extraction (`on_missing: fail`) + exact membership against the pinned closed set on the slot contents. Authoring rejects genuinely ambiguous configurations (near-equal positions, diagonal "both left and above") rather than letting the checker coin-flip them (§6b.3).

### 2.4 D3 Chart QA — prefer generated charts

For deterministically generated charts (`imagegen.render_bar_chart`), the plotted value is **machine-known** and τ can be a true rounding band (or 0). Composite-eligible D3 cases should be generated. Natural/screenshot charts carry **read-off annotation risk** (the "pinned reference" is itself a human estimate between gridlines) — they are admissible only with tight τ where the gold is genuinely exact, and are flagged as carrying validity risk.

### 2.5 D6 Visual grounding — re-spec'd (the most over-stated dimension)

The geometry of IoU is pure, but the **end-to-end verdict is not robustly judge-invariant** as the prior draft implied. Three variance sources were hidden:

1. **Coordinate convention drift.** Models emit pixel coords, `xyxy`, center-based `cxcywh`, or qwen-style 0–1000 integer grids. A model *right about the object* but using a different convention scores IoU≈0 — that measures format-following, not grounding.
2. **Threshold knife-edge.** A box a human calls "correctly localized" lands anywhere in 0.3–0.9 depending on gold-box tightness.
3. **Gold-box annotation.** On any natural image the gold box is hand-drawn — a human appraisal smuggled in as a "pinned reference." The §6b.2.1 lint checks the ref is *pinned*, not that it is judge-invariantly *derived*.

**Resolution:**
- **Composite-eligible D6 uses synthetic images only.** `imagegen` draws the object; the gold box is the **exact pixel rect the generator drew** — machine-known, not annotated.
- A deterministic **convention normalizer** auto-detects `xyxy`/`xywh`/`cxcywh`/0–1000 and canonicalizes before IoU, so a format-only difference does not masquerade as a grounding failure. The normalizer is content-pinned.
- IoU is reported as a **graded evidence value**; composite credit gates on a **generous** threshold (0.4) with a **dead-band** (0.35–0.45 → `partially_scored` → drift queue).
- **Natural-image bbox is demoted to a diagnostic, NOT in the composite.** Its gold box is an annotation and carries human-validity risk (§6b.3.5), stated on the board.
- `bbox_iou` is always preceded by a `json_schema` gate; a malformed box is `satisfied=false`, never coin-flipped.

---

## 3. Vision-specific speed metrics (Tier 0, separate column set, §11 + §14)

Speed is **never summed into quality** (§14). It populates a sortable column set and a Pareto scatter on the VISION board only. All carry the §11 honesty machinery: warmup discard (K=5), `colocated_loadgen` flag, load-model label, error accounting, sample-size badge on percentiles.

**The honest measurability boundary (fixes a review major).** Over LM Studio's black-box streaming `/v1`, the client observes only request-send → first-token (TTFT). TTFT bundles base64 **upload** (a multi-hundred-KB data URI, ~33% inflated) + server image decode + vision-tower encode + LLM prefill. **You cannot cleanly isolate "vision prompt cost" from upload over this API.** The prior draft's headline `Δttft = img_ttft − text_ttft` removes text prefill but **not** image upload, so it is meaningful only on a co-located box (upload ≈ 0) — exactly the configuration §11 already brands a lower bound. We therefore report what the runner can actually measure and demote the rest.

| Metric key | Definition | Unit | Status |
|---|---|---|---|
| `img_ingest_ttft_ms` | TTFT for a single-image prompt | ms | **Kept (headline)**, badged "includes upload+decode+prefill; co-located = lower bound" |
| `multi_image_ingest_ms` | TTFT with M images (default M=4) of fixed P | ms | Kept — D9 ingest cost |
| `frame_throughput_ttft_ms` | TTFT with N ordered frames | ms | Kept — D10/D11 ingest path (TTFT-based, not a fictional fps) |
| `e2e_decode_tok_s` | Decode tok/s on the textual answer | tok/s | Kept — reuses MVP decode metric |
| `image_bytes` | total base64 payload length | bytes | Kept — deterministic ingest-size proxy |
| ~~`prompt_proc_rate_kpx_s`~~ | P / (TTFT − baseline) | kpx/s | **Dropped** — if the backend tiles, processed pixels ≠ P, so the rate is a fiction; and the baseline subtraction does not remove upload |
| ~~`tokens_per_image_by_res`~~ | image tokens vs resolution | tokens | **Demoted to diagnostic, hidden when null** — LM Studio generally does not expose per-image `usage`; never fabricated. Verify against a live `/v1` `usage` payload before promoting to a column |

**Δttft is reported only as a co-located diagnostic**, explicitly badged, and never cross-compared with 2-box numbers. This matches the MVP plan's own honesty note ("we cannot separate network-upload from server-decode over a black-box API — call this out, don't fake a split").

A pinned resolution ladder (`P ∈ {256², 512², 768², 1024²}`, pinned Lanczos resampler) may still be swept for `img_ingest_ttft_ms` to surface a curve — but the curve is TTFT, with the upload caveat attached, not a derived pixel rate.

---

## 4. Worked evaluator specs (`constraints_json` shapes)

Each fits the existing `case_versions` row. **Content-pinning rule (fixes the runtime-generation hole):** images are pinned by the **sha256 of the actual generated PNG bytes**, stored in the blob store (§17 key=sha256) and referenced by `image_ref` in `prompt_json`. The generator spec is **provenance metadata only** — Pillow/freetype rasterization is *not* byte-identical across versions/platforms, so hashing the spec would break the §12 "same bytes → same boolean" guarantee. **Image-byte caching is mandatory, not optional.** All `sha256:` refs must be well-formed 64-hex digests that resolve to a stored object; a `sha256:`-prefixed *label* fails the suite-build lint.

### 4.1 D4 Counting (Tier 0) — slot-strict integer

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok", "vision_count"],
  "prompt_image_refs": ["sha256:<64hex of generated PNG>"],
  "image_provenance": { "generator": "render_shapes", "args": {"n": 7, "color": "red", "shape": "circle"}, "pillow_major": 12 },
  "extraction": { "slot": "count", "regex": "<count>\\s*(-?\\d+)\\s*</count>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "numeric_exact_slot", "version": "numexact.v1",
      "params": { "reference": 7, "grammar": "single_integer.v1" } }
  ]
}
```
Judge-invariance: the count `7` is generator-known on a synthetic scene; exact integer equality is judge-free; re-derived server-side from stored bytes (§12).

### 4.2 D2 OCR (Tier 0) — per-case normalization

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok", "vision_ocr"],
  "prompt_image_refs": ["sha256:<64hex>"],
  "extraction": { "slot": "ocr", "regex": "<ocr>([\\s\\S]*?)</ocr>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "cer_threshold", "version": "cer.v1",
      "params": { "max_cer": 0.05, "dead_band": 0.01,
                  "normalize_ref": "ocr_norm.receipt_digits_significant.v1",
                  "case_sensitive": false } }
  ],
  "reference_json": { "text_canonical_normalized": "blue bottle coffee" }
}
```
Judge-invariance: CER is a pure edit-distance over a pinned, per-case normalization whose canonical reference form is fixed at authoring; near-boundary → `partially_scored`.

### 4.3 D6 Visual grounding (Tier 0, synthetic only)

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok"],
  "prompt_image_refs": ["sha256:<64hex of generated PNG>"],
  "image_dims": { "w": 1024, "h": 768 },
  "image_provenance": { "generator": "render_positioned", "args": {"shape": "dog_icon", "rect_px": [318,322,225,269]} },
  "extraction": { "slot": "bbox", "regex": "<bbox>([\\s\\S]*?)</bbox>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "json_schema", "version": "jsonschema.2020-12", "params": { "schema_ref": "sha256:<box_schema>" } },
    { "checker": "bbox_iou", "version": "bboxiou.v1",
      "params": { "iou_threshold": 0.4, "dead_band": 0.05,
                  "convention_normalizer": "bbox_convention_autodetect.v1" } }
  ],
  "reference_json": { "gold_box_px": [318, 322, 225, 269] }
}
```
Judge-invariance: the gold box is the exact rect the generator drew; the convention normalizer absorbs format-only differences; IoU is exact geometry; the generous threshold + dead-band remove the knife-edge. **Natural-image variants of this case are diagnostic-only.**

### 4.4 D3 Chart QA (Tier 0, generated chart)

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok"],
  "prompt_image_refs": ["sha256:<64hex of generated chart PNG>"],
  "image_provenance": { "generator": "render_bar_chart", "args": {"labels_values": {"Q1":31,"Q2":38,"Q3":42,"Q4":35}} },
  "extraction": { "slot": "answer", "regex": "<answer>([\\s\\S]*?)</answer>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "numeric_tolerance", "version": "numtol.v1",
      "params": { "reference": 42.0, "tol": 0.5, "tol_kind": "absolute", "grammar": "currency_or_number.v1" } }
  ]
}
```
Judge-invariance: value is generator-known; tol is a true rounding band, not an appraisal.

### 4.5 D8 Descriptive grounding (Tier 1 — diagnostic on this fleet)

D8 is the **only** judged vision dimension. On a single-vision-family fleet its un-shadowed criteria are `single_judge` / non-record-eligible (§0). It therefore contributes **diagnostics only** and is **weight-capped so it cannot move the composite**. The composite-eligible signal is restricted to **Tier-0-shadowed criteria** (decided programmatically, judge = audit-only). Where possible, prefer reducing D8 to a Tier-0 JSON slot instead of a judge:

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok"],
  "prompt_image_refs": ["sha256:<64hex>"],
  "prompt_text": "Name the held object and its color as JSON in <answer></answer>, e.g. {\"object\":\"...\",\"color\":\"...\"}.",
  "extraction": { "slot": "answer", "regex": "<answer>([\\s\\S]*?)</answer>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "json_schema", "version": "jsonschema.2020-12", "params": { "schema_ref": "sha256:<obj_color_schema>" } },
    { "checker": "set_match", "version": "setmatch.v1",
      "params": { "field": "object", "accepted": ["umbrella","parasol"] } },
    { "checker": "set_match", "version": "setmatch.v1",
      "params": { "field": "color", "accepted": ["red","crimson","scarlet"] } }
  ]
}
```
This makes object/color **structural facts** (no judge, no compositional "binding" parse). Reserve the `judge_verdict.v1` rubric form only for genuinely descriptive cases, and route "how vivid/evocative" to the arena (Tier 2). Avoid compositional binding criteria unless reduced to a `{object, color}` slot.

### 4.6 D9 Multi-image (Tier 0)

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok", "vision_multi_image"],
  "prompt_image_refs": ["sha256:<imgA>", "sha256:<imgB>"],
  "prompt_text": "Which image contains more circles? Answer A or B in <answer></answer>.",
  "extraction": { "slot": "answer", "regex": "<answer>\\s*(A|B)\\s*</answer>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "exact_match", "version": "exact.v1", "params": { "reference": "A", "case_insensitive": true } }
  ]
}
```
Generated images make the comparison machine-known. Slot-exact, no substring scan.

### 4.7 D8 weight note

D8's composite weight is set so that, even at full Tier-0-shadowed credit, the board clears the §6 ≥0.7 deterministic-dominance floor on Tier-0 dimensions alone (D1–D7, D9). On this fleet, treat D8 as **near-zero composite weight, full diagnostic visibility**.

---

## 5. Video (D10) & Motion (D11) — design-complete, feasibility-gated

**Native video is unsupported over `/v1`. These dimensions run via frame-decomposition only, and that decomposition is honestly bounded.** Two prior-draft claims do not survive review and are corrected here:

1. **"Pinned ffmpeg + timestamps regenerates identical frames" is FALSE across builds.** Frame output at a timestamp depends on libavcodec version, HW vs SW decode, seek mode, swscale flags, and pixel-format conversion. `ffmpeg.v1` is not a content hash of bytes. **Resolution: the sha256 of each extracted/generated frame PNG is the SOLE pinned authority.** The decoder/timestamps are non-authoritative provenance only. The two mechanisms are no longer in contradiction — frames are **pinned bytes, not regenerated**.

2. **Clip-level ground truth is an annotation judgment.** "Did the cup fall," "direction toward/away from camera" from 8 sampled frames is frequently genuinely ambiguous — exactly the §6b.3.5 human-validity trap. **Resolution: composite-eligible D10/D11 use SYNTHETIC frame sequences** (`imagegen.render_motion_frames`: a shape translating across N PNGs with generator-known per-frame positions). Motion direction / event / count are then **machine-known**, clearing the §6b.3 authoring gate cleanly. Any natural-clip case must pass the authoring gate (`raw_agreement ≥ 0.95` across human annotators) and **reject ambiguous toward/away and diagonal cases at authoring**; otherwise it is diagnostic-only.

### 5.1 Board note (rendered verbatim on the board)

> "Video = N pinned frame **images** (not regenerated); native video ingestion is not exercised. Temporal resolution is bounded by the sampling cadence — sub-frame motion and audio-sync events are unmeasurable. Composite-eligible cases use synthetic frame sequences with machine-known motion truth; natural clips are diagnostic-only."

### 5.2 D11 Motion (Tier 0, synthetic, feasibility-gated)

```json
{
  "tier": 0, "modality": "vision", "board": "vision",
  "capability_required": ["vision_ok", "vision_multi_image"],
  "frame_decomposition": {
    "frame_refs": ["sha256:<f0>","sha256:<f1>","sha256:<f2>","sha256:<f3>",
                   "sha256:<f4>","sha256:<f5>","sha256:<f6>","sha256:<f7>"],
    "frame_bytes_are_authority": true,
    "provenance": { "generator": "render_motion_frames", "args": {"shape":"dot","path":"left_to_right"}, "decoder_ref": "non_authoritative" },
    "preamble_template": "Frame {k}"
  },
  "extraction": { "slot": "answer", "regex": "<answer>([\\s\\S]*?)</answer>", "on_missing": "fail" },
  "checker_chain": [
    { "checker": "exact_match", "version": "exact.v1",
      "params": { "reference": "left_to_right",
                  "closed_set": ["left_to_right","right_to_left","up","down","toward_camera","away_from_camera","none"],
                  "case_insensitive": true } }
  ],
  "board_note": "Synthetic frame sequence; machine-known motion; native video not exercised."
}
```
Judge-invariance: frames are pinned bytes; the generator knows the motion vector; the answer is a pinned closed set; slot-exact membership is judge-free.

### 5.3 Build sequencing

D10/D11 reuse the multi-image plumbing built for D9. They are **gated on `vision_multi_image`** and badged `video:via_frames`. A model failing multi-image loses D10/D11 to **coverage**, not to a 0. Build single-image dimensions first; add motion frames once D9 is green.

---

## 6. Offline demo (MockVisionTarget) — required, not optional

The current MVP's headline property — full pipeline + dashboard demoable with zero GPU — is silently lost for vision unless the mock is image-aware. `MockTarget` keys canned answers off a text `_case_id` dict and is image-blind, so against `mock` every vision case would score 0 / `capability_absent`.

**Required:** a `MockVisionTarget` persona that (a) returns `vision_ok=True` from the probe stub, and (b) keys slot-formatted canned answers (`<answer>`/`<count>`/`<ocr>`/`<bbox>`) off the vision case id the runner already tags. Without it the vision board cannot be exercised offline — state this in the runbook.

---

## 7. Composite & honesty

- **Per-dimension quality** = Tier-0 pass-rate (D1–D7, D9–D11) + Tier-0-shadowed fraction for D8, with §13 cluster-bootstrap CIs; overlapping-CI models render tied.
- **VISION composite** = `Σ(weightᵢ · qualityᵢ)` over admitted dimensions, **client-side weighted**, with a **per-board coverage badge**. To avoid the §14 incomparability trap, the headline ranking is **default-filtered to a minimum coverage** (models below it render in a separate "partial coverage" section, not co-ranked with a badge alone). Speed is a separate sortable column + Pareto, never a summand.
- **Cross-board isolation:** a vision-capable model is fully scored here; a vision-incapable model never appears and is never penalized on the text board (§6). The text composite's denominator ranges over text categories only — the structural guarantee.
- **BYO-judge:** only D8 ever invokes a judge (defaulting to the launching model, which must itself be vision-capable; if it is not, D8 is gated `N/A`, never scored 0). On this single-vision-family fleet, **D8 is non-record-eligible** — the record-eligible vision board is **entirely Tier-0**, which is exactly the determinism posture §6/§6b demand.

---

## 8. Deltas to land (small-team scope)

**New files:** `mvp/aeon/imagegen.py` (deterministic Pillow generators returning `(png_bytes, ground_truth)`; **byte-cache + sha256 mandatory**), `mvp/aeon/probe.py` (`probe_vision` with color gate + control-pair), `mvp/aeon/vision_suite.py`.

**Changed:** `targets.py` (`image_block`/`text_block` helpers, `MockVisionTarget`, vision speed fields `n_images`/`image_bytes`/`img_ingest_ttft_ms`; `_clean` unchanged); `evaluators.py` (`extract_answer` with `on_missing:fail`, `numeric_exact_slot`, `cer_threshold`, slot-strict `closed_set`, `bbox_iou` + convention normalizer, `_levenshtein`/`_norm_ocr`; **do not let vision reuse the whole-text numeric fallback**); `runner.py` (`run_vision_benchmark` with probe gate + image assembly + byte-pinning); `db.py` (`board` column on `runs`+`results`, `vision_probe_json` on `runs`, guarded `ALTER`); `scoring.py` (`vision_leaderboard()`, capability gating, min-coverage filter, vision speed aggregates — never merged into `leaderboard()`); `app.py` (`/api/vision/*`); `web/` (a real but modest app.js refactor: `CATS`/`WEIGHTS`/`refreshBoard` parameterized per board, per-board weight profiles, coverage badge — the radar is data-driven so it reuses, but per-tab switching is **not free**).

**Deferred to M6 (flagged, not built now):** the normalized `target_capabilities` / `deployment_capabilities` tables, per-board `anchor_sets`, Hungarian multi-box matching, the resolution-ladder pixel-rate curve, native-`video_url` probing. The MVP ships the lightweight `board` column + probe blob.

---

## 9. What was dropped or down-ranked (explicit)

| Item | Disposition | Reason |
|---|---|---|
| `prompt_proc_rate_kpx_s` | **Dropped** | Tiling makes processed-pixels ≠ P; baseline subtraction doesn't remove upload — the rate is a fiction over a black-box API |
| `tokens_per_image_by_res` as a column | **Demoted to diagnostic, hidden when null** | LM Studio generally doesn't expose per-image `usage`; never fabricated |
| `Δttft` as the speed headline | **Down-ranked to a co-located-only diagnostic** | Cannot isolate vision cost from base64 upload over `/v1` |
| Natural-image bbox (D6) | **Demoted to diagnostic, not in composite** | Gold box is a human annotation + convention drift + threshold knife-edge |
| "Pinned ffmpeg regenerates identical frames" | **Removed** | False across builds; frame **bytes** are the sole authority |
| Natural-clip video/motion truth | **Diagnostic unless it passes the human-agreement gate** | Clip-level labels are annotation judgments (§6b.3.5) |
| `vision.frames` as a separate capability | **Collapsed into `vision_multi_image`** | No distinct API transport; separate gate risked spurious D10/D11 exclusion |
| OCR (`AEON7`) as board gate | **Replaced with color-square gate** | Conflated "sees images" with "can OCR"; risked false-negative board exclusion |
| `numeric_tolerance` whole-text fallback for counting | **Replaced with slot-strict `numeric_exact_slot`** | Whole-text scan violates `on_missing:fail`; gameable |
| D8 descriptive grounding as a record-eligible composite contributor | **Diagnostic-only on this fleet** | Single vision family ⇒ no cross-family verifier ⇒ permanently `single_judge` |
| Hashing the image *spec* for content-pinning | **Replaced with hashing the PNG bytes** | Pillow/freetype rasterization isn't byte-identical across versions/platforms |
| `gemma-4-12b` as a listed candidate | **Flagged — confirm the real served id (likely `gemma-3-12b`)** | No public Gemma 4; don't pin a nonexistent identifier |

---

*Authoritative source: `C:/Users/Albert/AEON Bench/DESIGN.md` (§6, §6b, §10b, §11, §12, §14, §17). New artifacts implied: `packages/shared/schema/vision_evaluator_spec.v1.json`, checker impls `cer_threshold` / `bbox_iou` / `numeric_exact_slot` registered in the §6b.2.1 primitive set, `metric_definitions(modality='vision')` rows for §3. Companion docs: `02-audio-matrix.md`, `03-boards-and-capability-gating.md`, `04-mvp-vision-plan.md`.*
