# Capability Detection & Separate Multimodal Boards

*AEON Bench — multimodal module, doc 03. Extends DESIGN.md (v0.3 → v0.4). Additive: where this touches §6 / §14 / §17 / §20–21 it extends, never overrides.*

This document is the **decisive, buildable** spec for how AEON detects whether a model
has a modality and how it scores modal ability on a **board of its own** — one for Vision,
one for Audio — fully separate from the Text leaderboard. It consolidates the architect
specs (VISION / AUDIO / HARD-DETERMINISM / BOARDS / MVP-PLAN) and **resolves the adversarial
review**: every claim the review showed was not truly deterministic or not feasible on the
user's LM-Studio stack is dropped or down-ranked here, explicitly.

The two sibling docs (`01-vision-board.md`, `02-audio-board.md`) carry the per-dimension case
catalogues. This doc owns the **cross-cutting machinery**: probing, the board model, the
data-model deltas, per-board speed, and the `Text | Vision | Audio` UI.

---

## 0. Governing principle

> **Modality is a partition of the leaderboard, not a category inside one.**

There are three boards — `text`, `vision`, `audio` — each a full evaluation graph with its
own composite, anchors, coverage denominator, radar, and speed columns. A model is scored on
a board **iff** it supports that board's modality:

- **Has the modality → full score on that board.** Its board composite is computed over the
  categories it covers; coverage is shown as a badge, never folded into the number.
- **Lacks the modality → absent from that board.** No row. And — the load-bearing guarantee —
  its **text composite is computed over text categories only**, so a text-only model is
  **never zero-penalized** for lacking vision or audio.

This **supersedes** the v0.3 §6 treatment ("Vision/Audio are `N/A` categories inside the one
composite"). Those rows become their own boards here. There is deliberately **no global
cross-board composite** — that would re-introduce the `N/A`-penalty problem the directive
forbids. The three boards rank independently; a model can be #1 on text and absent from audio,
and both facts stand alone.

Everything below inherits DESIGN unchanged: tiered determinism (§6/§6b), BYO judge (§10b),
trust tiers (§2), append-only content-pinning (§17), speed-shown-separately (§11/§14).

---

## 1. Feasibility ground truth (what the user's stack actually does)

The whole design is anchored to the real setup, not an idealized API.

| Transport | Status on LM Studio `/v1` (mid-2026) | Consequence |
|---|---|---|
| `text` content blocks | **Supported** | Text board works today. |
| `image_url` base64 data-URI blocks | **Supported** for `qwen3-vl-30b`, `qwen3-vl-8b`; **probe-then-trust** for the Gemma VL model | Vision board works today for the qwen3-vl pair. |
| `input_audio` base64 blocks / `/v1/audio/*` | **NOT confirmed** (open feature request, lmstudio bug-tracker #1715) | Audio board is **expected to be empty today** — the correct capability-gated outcome, not a defect. |
| `video_url` / native video | **Unsupported** over the API | Video is **not exercised**; see §1.2. |

Two corrections the review forced, stated up front:

- **Model id.** There is no public "gemma-4-12b". The served model is almost certainly
  **`gemma-3-12b`** (or a local rename). The manifest must not pin a nonexistent id; the
  probe (§2) decides admission regardless of the label, so a wrong label simply fails the
  probe rather than corrupting the board.
- **Audio is dormant, not built.** Per the small-team ethos we **do not build the audio
  checker suite, schemas, or case content now.** We ship only `probe_audio` + the board gate
  + a hidden Audio tab. The full audio graph is designed-on-the-shelf (`02-audio-board.md`)
  and built **only after** at least one served target returns `audio_transport = supported`.

### 1.1 `_clean` already passes multimodal content through

Confirmed against `mvp/aeon/targets.py:20`: `_clean` copies only `role` + `content` and passes
`content` **verbatim**. A message whose `content` is a list of `{type:"text"}` / `{type:"image_url"}`
blocks survives untouched, and internal tags (`_case_id`) are dropped because they are
non-`role`/`content` keys. **No change to `_clean` is needed** for the image transport.

### 1.2 Video = pinned frame images, not regenerated video (review blocker resolved)

Native video is unsupported, so "video understanding" / "motion" decompose to **multi-image**
(N ordered `image_url` blocks + a textual frame-index preamble). The review correctly killed
two over-claims, which this spec now bans:

1. **Frames are pinned BYTES, not "regenerable" from a decoder.** `ffmpeg` output at a given
   timestamp is **not** byte-identical across builds (libavcodec version, HW vs SW decode, seek
   mode, swscale flags). Therefore the **`sha256` of each extracted frame PNG is the sole pinned
   authority** (§17 key=sha256). `decoder_ref`/timestamps are recorded as **provenance only**,
   never as a regeneration guarantee.
2. **Prefer synthetic frame sequences** where the per-frame ground truth (direction, event,
   count) is **machine-known** (e.g. a shape translating across N PNGs from `imagegen`). Natural
   clips carry **annotation-validity risk** (toward/away from camera and diagonal motion are
   genuinely ambiguous from 8 samples) and must pass the §6b.3 authoring gate; ambiguous cases
   are rejected at authoring.

Board note, rendered in the UI: *"Video = N pinned frame images (not regenerated); native video
not exercised; temporal resolution bounded by sampling cadence — sub-frame and audio-sync events
are unmeasurable."*

---

## 2. Capability detection

Capability is a **Tier-0 fact** — programmatic, no model judge. Detection is a tiny
content-pinned probe run in the target resolver (DESIGN §5.3) before any modal case.

### 2.1 The control-pair: the one idea that makes detection judge-free

The hard part is distinguishing "the API accepted the block" from "the model actually used it."
We solve it with a **modality-stripped control twin**: every probe is sent **twice** — once
**with** the media block, once **without** it (same text prompt). The verdict is a deterministic
comparison, never a judge call:

```
answer_with_media == answer_without_media   → model IGNORED the block        → UNSUPPORTED
answer_with_media  ≠ answer_without_media   → block REACHED the model         → SUPPORTED
   …and matches the pinned ground truth      → SUPPORTED                       (board-admitted)
   …reaches model but wrong content          → SUPPORTED_WEAK                  (admitted; quality TBD)
HTTP 4xx naming the content type            → UNSUPPORTED                      (e.g. 400 image_url)
HTTP 5xx / timeout                          → INCONCLUSIVE → retry n=2 → unknown (board excluded, surfaced)
```

A model that merely *guesses* the answer from the text prompt cannot pass, because its answer
would be identical with and without the media. This is what lets us call detection
"judge-invariant" honestly.

### 2.2 The probe matrix (fine-grained flags, not one boolean)

Partial support is the common case (image-only/no-video; ASR-only/no-understanding), so we
detect a **set of flags**. Each flag is one or two trivial pinned-answer cases.

**Vision** (board gate = `supports_vision_image`):

| Flag | Stimulus | Pinned question | Tier-0 checker | Gates |
|---|---|---|---|---|
| `supports_vision_image` | 64×64 PNG, solid **red** square | "What single color fills this image? One word." | `exact_match{red, ci}` | **board admission** |
| `vision_ocr` | 96×32 PNG rendering `K7Q` (pinned font) | "Transcribe the text exactly." | `exact_match{K7Q}` | OCR cases |
| `vision_count` | PNG with exactly 3 black dots | "How many dots? Number only." | `numeric_exact{3}` | counting cases |
| `vision_multi_image` | two blocks (red, then blue) | "What color is the SECOND image? One word." | `exact_match{blue}` | multi-image **and** video-as-frames cases |

> **Board gate is the color square, NOT OCR (review blocker resolved).** The MVP plan's
> original `AEON7` OCR gate conflated "sees images" with "can OCR" and would falsely exclude a
> model with weak OCR from the *entire* board. OCR is demoted to a **sub-flag** that gates only
> OCR cases. A solid-color square also removes a single-point font/render dependency from the
> admission decision.

> **`vision_frames` is not a separate capability.** Sending 8 `image_url` blocks is the same
> transport as 2. We fold "frames" into `vision_multi_image`; a payload/context-limit failure at
> N=8 is recorded as a **coverage exclusion with reason `payload_limit`**, never as
> `capability=false`.

**Audio** (board gate = `supports_audio_input`) — probed, expected to fail on LM Studio today:

| Flag | Stimulus | Pinned question | Tier-0 checker | Gates |
|---|---|---|---|---|
| `supports_audio_input` | 0.5 s WAV of the spoken word "seven" + control twin | "What word is spoken? One word." | `exact_match{seven, ci}` **and** control differs | **board admission** |
| `audio_asr` | 1.2 s "the cat sat" | "Transcribe exactly." | WER=0 after pinned normalize | ASR cases |
| `audio_understanding` | same clip | "How many words were spoken? Number only." | `numeric_exact{3}` | SLU / LID / intent / long-form |
| `audio_nonspeech` | 0.5 s pinned bell | "Speech or non-speech sound? One word." | `exact_match{non-speech\|sound\|tone}` | audio-event cases |

The audio sanity check uses a **distinct random nonce digit string per trial** plus the control
twin, and requires the digits to appear **only** when the audio is present — defeating
text-prior guessing. If `supports_audio_input` fails (the expected LM Studio outcome) the model
is excluded from the audio board with the raw probe response captured.

All stimuli are **bundled content-addressed assets** (`suites/_capability/<v>/assets/`, key =
`sha256`) — never synthesized at probe time (synthesis is non-deterministic). The mini-suite is
itself a pinned `suite_version` so detection is re-derivable server-side (§12).

### 2.3 Re-check policy (so a board auto-revives)

Capabilities are not assumed stable forever — a backend upgrade can add `input_audio`.

- Detected on first run against a target, cached.
- `recheck_after` default **30 days**; **invalidated immediately** when the target's
  `recipe_hash` changes (different quant/engine can drop a vision tower) or the probe asset
  digest changes.
- A full run's **preflight always re-confirms the board-gate flag** (one cheap call per
  supported modality) even on a cache hit, so a board never scores a model that silently lost
  the modality mid-epoch.
- `POST /api/targets/{id}/recheck-capabilities` forces a re-probe.

This is the hook that makes the audio board **auto-revive** if LM Studio ships `input_audio` —
no manual intervention, the dormant board lights up on the next recipe change or TTL expiry.

---

## 3. The board model: full score if supported, absent if not

Each board reuses the §14 quality-only composite and §6 anchored/dominance rules, **scoped to
that board's categories**:

```
board_composite(model) =  Σ_{c ∈ board, covered(model,c)}  weightc · qualityc(model)
                          ───────────────────────────────────────────────────────────
                              Σ_{c ∈ board, covered(model,c)}  weightc
```

- **Denominator renormalizes over covered categories** → support = full score; an uncovered
  category drops out of **both** numerator and denominator and never contributes a 0.
- **Coverage is shown, not folded in:** `coverage = covered / total` per board, rendered as the
  §14 badge scoped per board ("Vision composite over 8/10 categories").
- **§6 deterministic-dominance floor (≥0.7 Tier-0+Tier-1 per category) applies per board.**
- §13 CIs, tied-rank groups, and anchored normalization apply per board independently.

**The structural "never penalized" guarantee.** A text-only model produces **zero vision/audio
result rows**, so it is **absent** from those boards' read models entirely. Its text composite's
denominator ranges only over `board='text'` categories and is **arithmetically unchanged** by
the existence of the other boards. This is enforced by the data shape (§5), not by a policy.

### 3.1 Comparability guardrail (review minor resolved)

Renormalizing lets a 2/10-coverage model sit in the same ranked list as a 10/10 model,
distinguished only by a badge — the incomparability §14 warns about, leaned on harder. Fix:
the headline ranked list **defaults to a min-coverage filter** (e.g. coverage ≥ a board-set
threshold); models below it render in a separate **"partial coverage"** section, not co-ranked.
The badge stays; the default-on filter does the honest work.

### 3.2 Tier map per board (what is genuinely deterministic — review-corrected)

| | Tier 0 (no judge, re-derivable) | Tier 1 (binary, judge=launching model) | Tier 2 (arena only, excluded) |
|---|---|---|---|
| **Vision** | OCR (CER, judge-invariant), counting (slot-strict integer), color/position/relation/VQA (closed pinned set on a fenced slot), chart value on **synthetic** charts (numeric ± tol), bbox IoU on **synthetic** boxes | descriptive grounding **only**, reduced to closed criteria | image aesthetics / "mood" / caption vividness |
| **Audio** *(dormant)* | ASR (WER/CER), LID, audio-event (F1 over pinned ontology), **speaker-count** exact, intent (closed set), SLU/long-form **answer gate** (MC/numeric/set exact) | short free-text grounding, Tier-0-shadowed | paralinguistics (emotion/tone/prosody) |

The review forced several **demotions** — recorded here so they are load-bearing, not footnotes:

- **bbox grounding → composite-eligible ONLY on synthetic images** where the gold box is the
  exact pixel rect the generator drew (machine-known, not annotated). Free-form bbox on natural
  images carries coordinate-convention drift (models emit pixel/xyxy/cxcywh/0–1000 grids) and
  annotation-tightness subjectivity → it is a **diagnostic, not in the composite**. Even on
  synthetic boxes, use a **deterministic convention-normalizer** (auto-detect xyxy/xywh/cxcywh
  and canonicalize before IoU) so a format-only failure isn't scored as a grounding failure, a
  **generous threshold (~0.4)**, and a **dead-band (0.35–0.45 → partially_scored → drift
  queue)** per §6b.2.4.
- **chart/doc-value QA → prefer synthetic charts** (machine-known plotted values, `tol` can be a
  true rounding band). Natural-chart reads carry read-off annotation risk and are marked as such.
- **counting → synthetic scenes** where `n` is generator-known and "what counts as X" is
  unambiguous; natural-photo counting requires a human-annotator agreement gate.
- **OCR CER is judge-invariant but its threshold + normalization are authoring parameters**
  carrying **validity (not stability) risk**. Pin them **per case** (number-heavy receipts keep
  digits/punctuation significant; prose crops may normalize) and calibrate the threshold against
  a small human-gold "acceptable transcript" set, not a round number. Keep the §6b.2.4 dead-band.
- **descriptive grounding (Tier 1)** must be reduced to **closed criteria over a fenced JSON
  slot** (`{object, color}` → `json_schema` → `exact`/`set_match`) so "binding" is a structural
  fact, not a parse judgment. Compositional free-text binding criteria are disallowed.

### 3.3 The single-vision-family consequence (review major resolved)

On the user's actual fleet the only vision models are `qwen3-vl-30b` and `qwen3-vl-8b` — the
**same family**. The §6b.4 cross-family verifier is therefore **structurally unavailable**, so
**every un-shadowed vision Tier-1 criterion is permanently `single_judge` and non-record-eligible.**

Decisive consequence, stated plainly: **the vision composite is effectively Tier-0-only for
record purposes** on this deployment. We therefore keep vision Tier-1 either (a) purely
**Tier-0-shadowed** (program decides, judge is audit-only), or (b) as **diagnostics excluded
from the record composite** until a second vision family is reachable. The same note applies to
any future audio Tier-1. This is honest and easy: the bulk of both boards has **zero judge
dependence**, which is exactly the determinism posture §6/§6b demand.

> **BYO-judge corollary.** A vision Tier-1 judge must itself be vision-capable and be re-sent the
> image. If the launching/judge model fails the vision probe, those cases are gated **N/A
> (coverage)**, never scored 0.

---

## 4. Per-board speed (separate axis, never a summand)

Speed is its own columns + a per-board Pareto scatter (§11/§14), never summed into quality.
The review trimmed the over-claimed metrics; **only what the runner can actually measure over a
black-box streaming `/v1` API survives**.

**Honesty rule that governs all of it:** over LM Studio's streaming API the client observes only
*request-send → first-token* (TTFT). **We cannot isolate base64 upload from server-side decode /
vision-tower encode / prefill.** Any "vision prompt cost" or "audio ingest cost" is a **proxy
that bundles upload + decode + prefill** — we say so, we never fake a split. (This matches the
MVP plan's own note and **overrides** the VISION-SPEED draft's claim that `Δttft` isolates
vision cost.)

**Vision board (works today):**

| Metric | Definition | Status |
|---|---|---|
| `img_ingest_ttft_ms` | TTFT on a single-image prompt | **kept** — badged "includes upload+decode+prefill; co-located = lower bound" |
| `multi_image_ingest_ms` | TTFT with M images (default 4) | **kept** |
| `frames_ingest_ms` | TTFT with N ordered frames | **kept** (video-as-frames path; not cross-compared to native) |
| `e2e_decode_tok_s` | decode tok/s on the textual answer | **kept** (reuses MVP metric) |
| `prompt_proc_rate_kpx_s` | pixels / (TTFT − text baseline) | **DROPPED from columns** — not cleanly measurable; tiling makes "processed pixels" a fiction. Diagnostic only. |
| `tokens_per_image_by_res` | per-image token count vs resolution | **DROPPED** unless a live LM Studio `usage` payload is verified to expose a per-image breakdown (it generally reports only combined `prompt_tokens`). Stored as `null` and **hidden**, never fabricated. |

**Audio board (dormant — metrics defined, gated behind the probe, null until it passes):**

| Metric | Definition | Note |
|---|---|---|
| `audio_ingest_ttft_ms` | TTFT on an audio-conditioned request | proxy = whole-request TTFT (upload+decode+prefill, **not** isolated) |
| `audio_rtf` | `processing_time / audio_duration` | **transcription only** (needs a duration denominator); `processing_time` = e2e − client overhead, a whole-request figure |
| `e2e_decode_tok_s` | decode tok/s on the text response | reuses MVP metric |

The fabricated example numbers in the AUDIO draft (`rtf:0.34`, `ingest 412ms`) are **illustrative
only**; on the real stack every audio speed field is `null` until `supports_audio_input` passes.

Co-location honesty (`colocated_loadgen` → "lower bound on latency", never cross-compared with
2-box numbers) and trust-tier badging (`self_reported` shown-but-segregated) apply unchanged
(§2, §11).

---

## 5. Data-model deltas

Two shapes: a **light MVP shape** (ship now) and a **full shape** (defer to M6). The review was
right that the full `target_capabilities` / `deployment_capabilities` / per-board `anchor_sets`
machinery is M6-scale for a store that is today two SQLite tables. We ship the light shape and
promote only when a second operator / orchestrated re-derivation actually needs it.

### 5.1 MVP shape (ship now — SQLite, `mvp/aeon/db.py`)

Add a `board` discriminator to **both** tables (guarded `ALTER TABLE … ADD COLUMN`, since SQLite
has no `IF NOT EXISTS` on columns — check `PRAGMA table_info` first), plus a probe-cache blob on
the run row. `create_run` / `save_result` gain a `board` kwarg defaulting to `'text'`, so all
existing text behavior is **byte-for-byte unchanged**.

```sql
-- runs: add board + a denormalized probe cache
ALTER TABLE runs    ADD COLUMN board        TEXT DEFAULT 'text';   -- text|vision|audio
ALTER TABLE runs    ADD COLUMN probe_json   TEXT;                  -- {gate:true, flags:{...}, coverage:[...], evidence_ref}
-- results: add board so a vision result can never enter the text leaderboard
ALTER TABLE results ADD COLUMN board        TEXT DEFAULT 'text';
```

`all_results_with_runs()` gains a `board='text'` filter (default) so the existing text
leaderboard is untouched; `scoring.vision_leaderboard()` queries `board='vision'`. A run whose
probe gate failed is recorded with `status='capability_absent'` and **excluded from the board
read model**, so the model simply does not appear (coverage surfaces it elsewhere).

### 5.2 Full shape (defer to M6 — Postgres, for multi-operator + re-derivation)

When orchestrated re-derivation and multiple operators arrive, promote to authoritative,
append-only, content-pinned rows. Sketch (not built in the MVP):

```sql
ALTER TYPE board_enum AS ENUM ('text','vision','audio');

-- authoritative per-target capabilities; targets.capabilities_json kept as a denormalized cache
target_capabilities(
  id, target_id, model_version_id,
  capability_key,            -- supports_vision_image | audio_asr | vision_multi_image | ...
  status,                    -- supported|supported_weak|unsupported|inconclusive|unknown
  board, is_board_gate,
  detected_via,              -- capability suite_version content_hash (re-derivable)
  probe_run_id, evidence_ref, control_pair_ref,   -- raw + modality-stripped twin in blob store
  detected_at, recheck_after,
  UNIQUE(target_id, capability_key, detected_via)   -- re-check INSERTs a new row, never mutates
);

-- board-keyed read model (replaces the single leaderboard_matrix)
leaderboard_matrix(
  suite_version_id, board, model_version_id, category_id,
  quality_score, speed_json, coverage_num, coverage_den,
  computed_at, source_run_id,
  UNIQUE(suite_version_id, board, model_version_id, category_id)
);
-- a model appears in a board's rows ONLY if covered on ≥1 of that board's categories.

category_scores(... , board, coverage_num, coverage_den);
anchor_sets(... , board);                         -- per-board anchors
deployment_capabilities(deployment_id, board, any_target_supported, updated_at);  -- drives tab visibility

metric_definitions(key, unit, higher_is_better, modality):
  ('img_ingest_ttft_ms','ms',false,'vision'),
  ('frames_ingest_ms','ms',false,'vision'),
  ('audio_ingest_ttft_ms','ms',false,'audio'),
  ('audio_rtf','ratio',false,'audio');
```

Capability rows are content-pinned (`detected_via` = suite hash) and append-only (the §17
`superseded_at` pattern). `evidence_ref` / `control_pair_ref` make detection re-derivable
server-side exactly like Tier-0 scores (§12), and each detection emits an `audit_events` row
(`object_type='target_capability'`).

### 5.3 Stimulus pinning: hash the BYTES, not the spec (review major resolved)

The MVP plan's "hash the generator spec + Pillow version" is **insufficient**: Pillow/freetype
rasterization is **not byte-identical** across patch releases or platforms (anti-aliasing,
hinting), so two hosts re-deriving the same case can feed the model **different pixels** — which
silently breaks the Tier-0 "same bytes → same boolean" guarantee (§6b.4/§12).

**Decision: image-byte caching is MANDATORY, not optional.** The pinned artifact is the
**`sha256` of the generated PNG bytes**, stored in the blob store (key = `sha256`, §17) and
referenced by `image_ref` in `prompt_json`. The generator spec is **provenance metadata only**,
never the pinning authority. Same rule applies to extracted video frames (§1.2) and to any future
audio asset.

---

## 6. Evaluator & runner deltas (vision; audio deferred)

These resolve the remaining review blockers about checker correctness and offline demoability.

### 6.1 Slot-strict checkers — no whole-text fallback (review major resolved)

The existing `chk_numeric_tolerance` (`evaluators.py:70`) does `src = slot if slot is not None
else candidate` — i.e. when the fenced slot is **missing** it scans the **entire** output for
numbers and passes on a subset heuristic. That is the **opposite** of §6b.2.2 `on_missing:fail`
and is gameable ("about 5 to 7 red cars" can pass). The §4.1 vision spec already implies a
strict single-integer checker; the **code must match the spec**:

- Add `extract_answer(text, slot)` reading a fenced `<answer>` / `<count>` / `<bbox>` / `<ocr>`
  slot via RE2 with **`on_missing:fail`** — a missing slot returns `satisfied=false`, never a
  whole-text scan.
- Add **`numeric_exact`** (extract the slot integer, compare `==` reference) and route vision
  counting through it — **not** the set-subset `chk_numeric_tolerance`.
- Add **`closed_set`** that exact-matches the **slot contents** against a pinned closed set —
  **not** a substring scan over the prose. The substring approach ("the cup is left of the bowl,
  not the right" contains both `left` and `right`; `right` ⊂ `bright`) is order-dependent and
  mildly judge-variant; it is **rejected** for closed sets per §6b.2.2.
- Add **`cer_threshold`** (Levenshtein/len over a **per-case** pinned normalization; dead-band
  per §6b.2.4) for OCR.
- For bbox, add the **convention-normalizer + `bbox_iou`** (synthetic-only in the composite,
  §3.2), preceded by a `json_schema` gate.

`extract_boxed` currently recognizes only `\boxed{}` / `<answer>`. **Standardize all vision
Tier-0 answers into the single `<answer>` slot** where possible (minimizes new code and reuses
the existing extractor); add the typed `<count>`/`<bbox>`/`<ocr>` extractors only where the typed
shape genuinely helps, each with `on_missing:fail`.

### 6.2 A vision mock so the board is demoable offline (review blocker resolved)

`MockTarget` is text-only and image-blind, keyed off `_case_id`. Without a fix, the headline MVP
property — full pipeline + dashboard with zero GPU — is lost for vision. Add a **`MockVisionTarget`**
persona that keys canned, **slot-formatted** answers (`<answer>`/`<count>`/`<ocr>`) off the
vision case id the runner already tags, and a **probe stub** that returns `gate=true` for the
mock. State explicitly: without it, the vision board cannot be exercised offline.

### 6.3 Runner

`run_vision_benchmark(run_id, model, target_url, judge_model, …)`:
1. Build target → **call the capability probe with the control twin (§2.1)**. If the board gate
   fails, record `board='vision'`, `status='capability_absent'`, store `probe_json`, return.
   The model is absent from the vision board and **untouched on the text board**.
2. Per case: load pinned image bytes by `image_ref`, assemble `content=[text_block(prompt),
   image_block(png)…]`, call `target.chat`, capture speed (incl. `img_ingest_ttft_ms`,
   `n_images`, `image_bytes`), run `evaluate`. Sub-flags gate per-category inclusion; an
   uncovered category is coverage-excluded, never scored 0.
3. Persist with `board='vision'`.

---

## 7. UI: `Text | Vision | Audio`

A top-level tab strip. Each tab is an **independent leaderboard + radar + Pareto** for that board
only; tabs are URL-routable (`/leaderboard/vision`) with **per-board weight profiles**.

- **Tab visibility.** The **Audio tab is hidden** when no target has ever returned
  `supports_audio_input` (the expected LM Studio case), replaced by an explanatory empty-state
  showing the **raw probe response** ("400 unsupported_content_block"). This is honest capability
  gating made visible, not a missing feature.
- **Leaderboard table.** Quality composite (CI band, tied groups) → **separate speed columns**
  for that modality (`img_ingest_ttft_ms` for vision; `audio_rtf` for audio) → a **coverage
  badge** per row ("8/10"). Filters: "min coverage ≥ X" (default-on per §3.1), "≥ X tok/s",
  "RTF ≤ X".
- **Radar.** Axes = that board's categories only. A model's polygon spans only **covered** axes;
  uncovered axes render as a **dashed gap, not a 0** — visually encoding "absent, not failed."
- **Capability chips.** Each model row carries `[T] [V] [A]` lit/dim with sub-flag tooltips
  (`V: image ✓ · OCR ✓ · multi-image ✓ · video via-frames · native ✗`; `A: probe failed on this
  deployment`). `supported_weak` = half-lit chip. `video:via_frames`, `single_judge`, and
  trust-tier badges share one badge row.
- **app.js is a modest refactor, not "free."** `CATS`/`WEIGHTS` are module globals set once in
  `init`; per-tab switching requires lifting them into per-board state and parameterizing
  `renderBoard`/`renderChart`/`refreshBoard` by active board. The inline-SVG radar then **reuses**
  the same data-driven renderer — small, but real work, not zero.
- **Trust + BYO-judge are orthogonal to board** and shown identically on every board: a vision
  Tier-1 score carries the same `judge_model`/`is_self_judge`/`single_judge` provenance and
  cross-family eligibility rules as a text Tier-1 score. Nothing about modality relaxes the
  determinism contract.

---

## 8. Build sequencing (small-team ethos)

1. **M6a — Vision, single board, deterministic spine.** `board` column + `probe_json` cache
   (§5.1); control-pair probe + color-square gate (§2); mandatory image-byte caching (§5.3);
   slot-strict `numeric_exact`/`closed_set`/`cer_threshold` + `extract_answer` (§6.1);
   `MockVisionTarget` (§6.2); `run_vision_benchmark`; `vision_leaderboard()`; `/api/vision/*`;
   the `Text | Vision | Audio` tabs with Audio hidden. Cases: OCR, counting, color, spatial,
   relation, VQA, **synthetic** chart-value, fine-detail, multi-image. bbox only as synthetic
   diagnostic.
2. **M6b — Video-as-frames.** `render_motion_frames` synthetic sequences (machine-known motion),
   reusing the multi-image plumbing; pinned frame bytes; the board note from §1.2.
3. **M6c — Audio, probe-only.** Ship `probe_audio` + gate + hidden tab. **Do not** build the
   audio checker suite / schemas / cases until a served target returns `supports_audio_input`.
   The 30-day recheck TTL (§2.3) auto-revives the board if LM Studio ships `input_audio`.
4. **M6d (deferred) — full Postgres capability tables + per-board anchors** (§5.2), promoted only
   when a second operator or orchestrated re-derivation requires it.

---

## 9. DESIGN.md deltas (v0.3 → v0.4)

1. **§6 table (lines 156–157).** Reframe the Vision/Audio rows: "Vision/Audio are **not
   categories in the text composite**; each is a **separate modality board** (§6c) with its own
   composite, gated by a capability probe; absence excludes a model from that board only and
   **never penalizes the text composite** (coverage shown, §14)."
2. **New §6c — Multimodal boards.** Board separation (§0 here); the **control-pair capability
   probe** (§2.1); the **per-board Tier map with the review-forced demotions** (§3.2: bbox/chart/
   count synthetic-only in composite; OCR threshold/normalization as per-case authoring
   parameters); the **single-vision-family → Tier-0-only-for-record** consequence (§3.3);
   **stimuli pinned by generated bytes, not spec** (§5.3); **video = pinned frame images, not
   regenerated** (§1.2); **per-board speed, with the "cannot isolate upload from decode" honesty
   rule and the dropped kpx/token metrics** (§4); **audio dormant-until-probed** with the
   30-day recheck hook (§1, §2.3).
3. **§14.** Add: "Capability-gated modality boards (§6c) each carry their **own coverage badge**;
   the headline ranked list defaults to a **min-coverage filter**, with partial-coverage models
   shown separately rather than co-ranked."
4. **Glossary (§3).** `Board / modality` = "an independent evaluation graph (`text|vision|audio`)
   with its own composite and a control-pair capability probe; never co-ranked across boards
   (§6c)."
5. **Roadmap (§20/§21 M6).** Refine the M6 row to the §8 sequencing: vision deterministic spine
   first, video-as-frames second, audio probe-only third, full capability tables deferred.

---

## 10. What we explicitly dropped or down-ranked (and why)

| Item | Verdict | Reason |
|---|---|---|
| Free-form bbox on natural images in the composite | **Dropped to diagnostic** | Coordinate-convention drift + annotation-tightness subjectivity → not judge-invariant end-to-end (review blocker). Synthetic bbox stays, with convention-normalizer + dead-band. |
| `prompt_proc_rate_kpx_s` speed column | **Dropped** | Not cleanly measurable; tiling makes "processed pixels" a fiction. |
| `tokens_per_image_by_res` speed column | **Dropped unless verified** | LM Studio generally reports only combined `prompt_tokens`; stored `null` + hidden, never fabricated. |
| "Δttft isolates vision cost" | **Dropped** | Cannot separate base64 upload from server decode/prefill over a black-box stream — proxy only, stated as such. |
| chrF/BLEU as a Tier-0 "unrestricted" gate (audio) | **Down-ranked to diagnostic** | §6b.2.4 lists only WER/CER/edit-distance/exact-ROUGE as unrestricted; chrF/BLEU are graded with author-chosen thresholds + tokenization. Translation's composite signal = closed-fact probe; chrF reported under the similarity_threshold guardrail. |
| Audio speed example numbers | **Dropped** | Audio input unsupported today → all audio speed fields `null` until the probe passes. |
| "ffmpeg.v1 regenerates identical frames" | **Dropped** | Decoder output is not byte-identical across builds; frame `sha256` is the sole authority. |
| `vision_frames` as a distinct capability | **Folded into `vision_multi_image`** | Same transport; N=8 payload failure is a coverage exclusion, not `capability=false`. |
| AEON7 OCR as the board gate | **Replaced by color-square** | OCR gate conflated "sees images" with "can OCR" → false board exclusion. OCR demoted to a sub-flag. |
| Full `target_capabilities`/`deployment_capabilities`/per-board `anchor_sets` in the MVP | **Deferred to M6d** | M6-scale schema for a two-SQLite-table store and a board that yields zero rows today; MVP uses `board` column + `probe_json` cache. |
| Building the audio checker suite/schemas/cases now | **Deferred** | `input_audio` unconfirmed on LM Studio; build only after a target returns `supported`. |
| `whole-text` fallback in `chk_numeric_tolerance` for counting | **Replaced by slot-strict `numeric_exact`** | §6b.2.2 `on_missing:fail`; the fallback is gameable. |

---

*Relevant files (absolute):*
*`C:/Users/Albert/AEON Bench/DESIGN.md` (§6 lines 156–157, §14 line 380, §20/§21 M6 line 580; this module is the proposed §6c).*
*`C:/Users/Albert/AEON Bench/mvp/aeon/{db.py,targets.py,evaluators.py,runner.py,scoring.py,app.py}` (deltas in §5.1, §6).*
*New: `C:/Users/Albert/AEON Bench/mvp/aeon/{imagegen.py,probe.py,vision_suite.py}`.*
*Sibling docs: `01-vision-board.md`, `02-audio-board.md` (per-dimension case catalogues).*
