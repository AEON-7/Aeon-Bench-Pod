# AEON Bench — MVP VISION Board Build Plan + DESIGN deltas

**Status:** implementation-ready · **Scope:** add a *separate* VISION evaluation board to the existing `mvp/` against qwen3-vl served by LM Studio (OpenAI-compatible `/v1`) · **Sequenced behind:** the working text MVP (`mvp/aeon/*`, two SQLite tables) · **Defers:** native video, motion-as-frames, and the entire AUDIO board, each behind a probe.

This plan is deliberately proportionate to the small-team ethos (DESIGN v0.3 governing philosophy): a `board` column, one probe, deterministic image generation with **machine-known** ground truth, three additive checkers, and a tabbed dashboard. It does **not** introduce the architect-scale `target_capabilities` / `deployment_capabilities` tables, per-board `anchor_sets`, Hungarian bbox matching, or resolution ladders — those are M6 work and are explicitly listed as deferred at the end. The full VISION/AUDIO/BOARDS architect specs remain valid design-on-the-shelf; this is the buildable slice.

It honors DESIGN: **tiered determinism** (§6/§6b), **capability gating** (§6/§14 — a model without vision never appears on the vision board and is *never* zero-penalized on the text board), **BYO judge** (§10b), **speed shown separately** (§11/§14), **append-only content-pinned cases** (§17), and **server-side re-derivability** (§12).

---

## 0. What the current MVP gives us (verified against the code)

- `OpenAITarget.chat(messages, ...)` (`mvp/aeon/targets.py`) streams `/v1/chat/completions`, capturing `ttft_ms`, `decode_tps`, `e2e_ms`, `output_tokens`. **`_clean()` (targets.py:20–22) copies only `role` + `content`** — it passes `content` through untouched, so a list-of-blocks `content` (text + `image_url`) already survives, and the internal `_case_id` tag is already stripped. **No timing-primitive change is needed.**
- `MockTarget` (targets.py:150–200) answers from a hardcoded **text** `GOOD`/`SLOPPY` dict keyed by `messages[0]["_case_id"]`. It is image-blind — see the blocker fix in §1.
- `evaluators.py` exposes Tier-0 checkers via `CHECKERS` (`exact_match`, `numeric_tolerance`, `regex_constraint`, `structural_count`, `unit_test`) and a Tier-1 binary-rubric judge (`eval_tier1`). Adding image checkers is purely additive.
  - **Watch-out (real determinism hole):** `chk_numeric_tolerance` (evaluators.py:68–77) does `src = slot if slot is not None else candidate` — when no `\boxed{}`/`<answer>` slot is present it scans the **whole output** for numbers and passes if the gold appears with ≤1 extra. Reusing it for vision counting would let a rambling model satisfy a count case. We do **not** reuse it; we add a slot-strict checker (§4).
- `suite.py` defines `CASES` as plain dicts (`id/category/tier/prompt/eval`) + `CATEGORIES` + `suite_hash()`. A vision suite is a parallel module.
- `runner.py` builds `[{"role":"user","content": case["prompt"], "_case_id": cid}]` and persists per case.
- `db.py` (SQLite) has `runs` + `results`, no modality discriminator.
- `scoring.py:leaderboard()` computes one global board from `db.all_results_with_runs()`.
- `app.py` serves the dashboard + API; `web/` is a no-build inline-SVG dashboard.
- **Pillow is the pinned image dependency** (deterministic generation). qwen3-vl-30b / qwen3-vl-8b accept `image_url` base64 data-URI blocks over LM Studio `/v1` — confirmed transport.

---

## 1. Resolved review issues (what we keep, demote, or drop)

The adversarial review found real holes. Decisions, stated plainly:

| Issue | Decision in this plan |
|---|---|
| **MockTarget is image-blind → vision board undemoable offline** (blocker) | **FIXED.** Add a `MockVisionTarget` persona keyed off the vision `_case_id`, returning slot-formatted canned answers, and a probe stub that reports `vision_ok=True` for mock. Without it the board cannot be exercised without a GPU. |
| **Runtime-generated PNGs are not byte-identical across Pillow/freetype/platform → breaks §12 re-derivation** (blocker) | **FIXED.** Image-byte caching is **mandatory**: we hash the *generated PNG bytes* (sha256), store them under `mvp/aeon/assets/vision/<sha>.png`, and pin `image_sha` in the case. The generator spec is provenance metadata, **never** the pinning authority. Same bytes → same model input → same boolean. |
| **`chk_numeric_tolerance` whole-text fallback violates `on_missing:fail`** (major) | **FIXED.** New slot-strict checkers (`chk_count_slot`, `chk_closed_set`, `chk_cer_threshold`) all extract a fenced slot with `on_missing=fail`; a missing slot → `satisfied=false`, never whole-text scan. |
| **Closed-set substring matching is order-dependent / leaks** ("right" in "bright"; "left" + "not the right") (minor) | **FIXED.** `chk_closed_set` matches **exact** against the fenced `<answer>` slot contents only, against the closed option set — no substring scan over prose. |
| **bbox grounding (D6) is over-stated Tier-0**: coordinate-convention drift, IoU knife-edge, hand-annotated gold boxes are smuggled human appraisal (blocker) | **DROPPED from the MVP composite.** Free-form bbox grounding is **not** in the MVP. Rationale: on natural images the gold box is an annotation (a human judgment), and convention drift (xywh vs xyxy vs cxcywh vs 0–1000) measures format-following, not grounding. If we add grounding later it will be **synthetic-only** (gold box = the exact pixel rect the generator drew) with a generous threshold + dead-band. Stated explicitly so no one ships annotation-as-reference. |
| **VIDEO/MOTION (D10/D11) frame-extraction "regenerable" claim is false; clip-level labels are annotations** (blocker) | **DEFERRED** (build-deferred path, §7). When built, frames are **pinned bytes** (sha256), decoder/timestamps are non-authoritative provenance, and motion ground truth is **synthetic** (`render_motion_frames`, generator-known per-frame positions) so it clears the §6b.3 authoring gate. Natural-clip cases require a human-agreement gate; ambiguous toward/away and diagonal cases are rejected at authoring. |
| **Vision SPEED over-claims**: `prompt_proc_rate_kpx_s`, `tokens_per_image_by_res`, "Δttft isolates vision cost" not measurable over a black-box streaming API; conflates base64 upload with prefill (major) | **DEMOTED.** The MVP records only what the runner can actually measure: `ttft_after_image_ms` (= the existing `ttft_ms`, honestly labeled "includes upload + decode + prefill"), `n_images`, `image_bytes`, `e2e_ms`, `decode_tps`. We do **not** compute kpx/s or per-image token curves, and we do **not** claim a clean vision/upload split — matching the rest of the design's "don't fake an unmeasurable split" honesty (§11). |
| **OCR CER threshold + normalization embed authoring judgments** (major) | **ACKNOWLEDGED, kept Tier-0.** CER is judge-invariant (§6b.2.4); the threshold + normalization are per-case authoring parameters carrying *validity* (not stability) risk. We pin them **per case** (not one global), use generated text where the gold is exact, and add the §6b.2.4 dead-band (`partially_scored` near the boundary → drift, not a coin flip). |
| **Counting on natural photos has annotation-ambiguous gold** (major) | **FIXED for the MVP:** counting uses **synthetic** `render_shapes(n, …)` where `n` is generator-known and "what counts" is unambiguous. |
| **Chart QA `tol` hides read-off appraisal on natural charts** (minor) | **FIXED:** MVP chart cases use **generated** bar charts (machine-known values, `tol` is a true rounding band, not an estimate). |
| **D8 descriptive grounding is mostly one shadowed criterion; cross-family vision verifier is structurally unavailable on a single-family (qwen3-vl) fleet** (major) | **ACKNOWLEDGED.** On the user's fleet the only vision models are qwen3-vl-30b / qwen3-vl-8b — **same family** — so any un-shadowed vision Tier-1 criterion is permanently `single_judge` / non-record-eligible (§6b.4). The MVP keeps **one** descriptive-grounding Tier-1 case whose criteria are **all Tier-0-shadowed** (object-name + color-name via closed sets), so it stays composite-eligible; the un-shadowed "binding"/"vividness" remainder is **not** scored (→ arena, deferred). The vision composite is therefore effectively **Tier-0-only** for record purposes — which is exactly the determinism posture we want. |
| **`gemma-4-12b` likely a misnomer for `gemma-3-12b`** (minor) | **FLAGGED.** Whatever the served id, it is **probe-then-trust**: admitted to the board only if it passes the color-square gate (§2). No fabricated id is pinned. |
| **`vision.frames` is not a distinct API capability** (minor) | **COLLAPSED.** "Frames" is a *usage pattern* of multi-image, not a separate transport. The MVP has one image-ingest gate; multi-image is a sub-flag (§2). |

Net: the MVP vision board is **all Tier-0 programmatic** (OCR, counting, color, spatial, relation, chart, MCQ-VQA, fine-detail, multi-image) plus **one fully-shadowed Tier-1** grounding case. No bbox, no video, no audio, no speed-metric fabrication.

---

## 2. Capability probe (vision) — `mvp/aeon/probe.py` (new)

Deterministic, cheap, run **before** any vision case. Board admission uses a **solid-color square**, not OCR (aligning with the BOARDS spec; OCR is a *sub*-capability, so a model that sees images but reads text poorly is not falsely excluded from counting/color/VQA).

```python
# probe.py
def probe_vision(target):
    """Returns {vision_ok, multi_image_ok, ocr_ok, evidence, probe_latency_ms}."""
    if isinstance(target, MockVisionTarget):
        return {"vision_ok": True, "multi_image_ok": True, "ocr_ok": True,
                "evidence": "mock", "probe_latency_ms": 0.0}

    red = solid_square_png("red")          # 64x64, pinned RGB, cached by sha
    blue = solid_square_png("blue")

    # control pair: the SAME question without the image proves the block reached the model
    def ask(blocks): return target.chat([{"role": "user", "content": blocks,
                                          "_case_id": "_probe"}], max_tokens=12)["text"].lower()
    try:
        with_img = ask([text_block("What single color fills this image? One word."),
                        image_block(red)])
        without   = ask([text_block("What single color fills this image? One word.")])
    except TargetError as e:
        return {"vision_ok": False, "multi_image_ok": False, "ocr_ok": False,
                "error": str(e)}              # HTTP 400/415 image_url rejected -> unsupported

    reached = ("red" in with_img) and (with_img.strip() != without.strip())
    multi = False
    if reached:
        two = ask([text_block("What color is the SECOND image? One word."),
                   image_block(red), image_block(blue)])
        multi = "blue" in two
    ocr = False
    if reached:
        ocr = "aeon7" in ask([text_block("Reply with only the text shown."),
                              image_block(token_png("AEON7"))]).replace(" ", "")
    return {"vision_ok": reached, "multi_image_ok": multi, "ocr_ok": ocr,
            "evidence": with_img[:60]}
```

**Control-pair design (the strongest idea, borrowed from BOARDS §1.2):** send the color question *with* and *without* the image block. If the answer is identical, the model is provably ignoring the image → `vision_ok=False`. This makes "did the modality reach the model" a deterministic comparison, never a judge call.

**Gating rule (DESIGN §6/§14):** the runner calls `probe_vision` first.
- `vision_ok=False` (or HTTP 400/415) → record the run with `board="vision"`, status `capability_absent`, store probe evidence, **return**. The model **does not appear on the vision leaderboard** and is **untouched on the text board** (capability gating, not a zero score).
- `vision_ok=True` → run the suite. `multi_image_ok` / `ocr_ok` gate the multi-image / OCR cases (covered-or-`N/A`, shown in coverage; never scored 0).

The probe result is stored on the run row (`vision_probe_json`) for the coverage badge.

---

## 3. Deterministic test-image generation — `mvp/aeon/imagegen.py` (new)

Pure Pillow. Each generator is a **pure function of its args + pinned font + pinned size + pinned palette**, returning `(png_bytes, ground_truth)`. **The PNG bytes are the pinned artifact:** the suite stores `sha256(png_bytes)` and writes the bytes to `mvp/aeon/assets/vision/<sha>.png`. The generator spec (name + args + Pillow major + font id) is recorded as *provenance only* — never the re-derivation authority (resolves the byte-identity blocker; satisfies §12: same bytes → same input → same boolean).

| Generator | Produces | Ground truth (machine-known) | Vision dimension |
|---|---|---|---|
| `solid_square_png(color)` | 64×64 solid color | `{color}` | probe / color |
| `token_png(text)` | one printed token on white | exact string | OCR / probe |
| `paragraph_png(lines)` | multi-line text block | full text | OCR / document read |
| `render_shapes(n, color, shape)` | N non-overlapping shapes on a fixed grid | `{count:n, color, shape}` | **counting**, color, shape |
| `render_positioned(shape, quadrant)` | one shape in a known quadrant | `{position}` | **spatial** |
| `render_two_shapes(a, rel, b)` | A left-of/above B | `{relation}` | **relational** |
| `render_bar_chart(labels, values)` | deterministic Pillow bar chart | `{max_label, value_of[X]}` | **chart QA** (generated → exact gold) |
| `render_fine_detail(big, tiny)` | large distractor + one tiny corner token | tiny token | **fine detail** |

**Font determinism:** bundle one TTF (e.g. DejaVuSans) under `assets/`; if unresolvable, fall back to `ImageFont.load_default()` and record which font was used **in the bytes hash** (it changes the bytes, so it changes the pin — honest by construction). Because the *bytes* are pinned, cross-platform freetype differences cannot silently change a model's input: a host that re-generates different bytes simply produces a different sha and is rejected as not matching the pinned artifact.

---

## 4. Image evaluators + vision speed — `mvp/aeon/evaluators.py` (additive)

Add a shared slot extractor and three checkers; register in `CHECKERS`. All use `on_missing=fail` (DESIGN §6b.2.2 — no benefit of the doubt).

```python
def extract_slot(text, slot):
    """Last <slot>...</slot>. Returns None if absent (caller fails on None)."""
    m = list(re.finditer(rf"<{slot}>\s*(.*?)\s*</{slot}>", text, re.S | re.I))
    return m[-1].group(1).strip() if m else None
```

**(1) `chk_closed_set`** — exact membership against a pinned option set, scored on the **slot only** (no prose substring scan). Used for color, spatial, relation, MCQ-VQA, multi-image, shape.
```python
def chk_closed_set(candidate, p):
    got = extract_slot(candidate, p.get("slot", "answer"))
    if got is None:
        return False, "no <answer> slot"           # on_missing: fail
    got = got.lower()
    opts = {o.lower() for o in p["options"]}
    if got not in opts:
        return False, f"{got!r} not in closed set"
    return got == p["answer"].lower(), f"chose {got!r} want {p['answer']!r}"
```

**(2) `chk_count_slot`** — strict integer equality from a `<count>` slot (does **not** reuse the leaky `chk_numeric_tolerance`).
```python
def chk_count_slot(candidate, p):
    got = extract_slot(candidate, "count")
    if got is None:
        return False, "no <count> slot"
    m = re.fullmatch(r"-?\d+", got)
    if not m:
        return False, f"non-integer slot {got!r}"
    return int(got) == int(p["value"]), f"got {got} want {p['value']}"
```

**(3) `chk_cer_threshold`** — character error rate of normalized `<ocr>` (or `<answer>`) slot vs pinned reference; `satisfied = cer <= threshold`. Fully deterministic (§6b.2.4, unrestricted). Per-case `threshold` and `normalize` (the review's validity point), plus a dead-band.
```python
def chk_cer_threshold(candidate, p):
    got = extract_slot(candidate, p.get("slot", "ocr"))
    if got is None:
        return False, "no <ocr> slot"
    norm = p.get("normalize", "ocr_lower_collapse")    # pinned id; per case
    g, r = _norm(got, norm), _norm(p["value"], norm)
    cer = _levenshtein(g, r) / max(1, len(r))
    thr, band = p.get("threshold", 0.10), p.get("dead_band", 0.01)
    if abs(cer - thr) <= band:
        return False, f"cer={cer:.3f} ~ thr={thr} -> partially_scored (drift)"  # near-boundary
    return cer <= thr, f"cer={cer:.3f} thr={thr} got={g!r}"
```
`_norm`, `_levenshtein` are tiny pure helpers. `ocr_lower_collapse` = NFKC → lowercase → collapse whitespace → strip outer whitespace. Number-significant cases pin a stricter normalizer that keeps digits/punctuation.

**Registration:** `CHECKERS.update({"closed_set": chk_closed_set, "count_slot": chk_count_slot, "cer_threshold": chk_cer_threshold})`.

**Vision Tier-1 (one case, all criteria shadowed):** reuse `eval_tier1` unchanged. The descriptive-grounding case asks the model to emit `{"object": "...", "color": "..."}` and both criteria are Tier-0-shadowed via `closed_set` over pinned synonym sets — so no judge is invoked and it stays record-eligible despite the single-family limitation (§1). If a vision-capable judge were ever needed, the judge prompt builder would re-attach the image; on a single-vision-family fleet that path is `single_judge` and excluded — documented, not silently scored.

**Vision speed (honest subset):** per case, `speed_json` carries the existing `ttft_ms/decode_tps/e2e_ms/output_tokens` plus `ttft_after_image_ms` (= `ttft_ms`, labeled *"includes upload + decode + prefill; co-located = lower bound"*), `n_images`, `image_bytes`. The board surfaces mean `ttft_after_image_ms` and `decode_tps` as **separate columns**, never folded into quality (§14). No kpx/s, no per-image token curves (unmeasurable over the black-box API — §1).

---

## 5. Vision suite + separate board

**`mvp/aeon/vision_suite.py` (new)** — parallel to `suite.py`. Each case carries `board:"vision"`, an `image` spec (generator + args), `prompt` (instructing the fenced slot), `tier`, `eval`, plus `requires` ∈ {`vision_ok`, `multi_image_ok`, `ocr_ok`} for sub-gating. Cases use **generated** stimuli with machine-known gold:

```
vision.ocr.token        Tier0 cer_threshold   requires ocr_ok         -> OCR
vision.count.circles    Tier0 count_slot       requires vision_ok      -> Counting
vision.color.square     Tier0 closed_set       requires vision_ok      -> Color
vision.spatial.quadrant Tier0 closed_set       requires vision_ok      -> Spatial
vision.relation.leftof  Tier0 closed_set       requires vision_ok      -> Relational
vision.chart.maxbar     Tier0 closed_set       requires vision_ok      -> ChartQA
vision.chart.value      Tier0 count_slot       requires vision_ok      -> ChartQA
vision.vqa.mcq          Tier0 closed_set       requires vision_ok      -> VQA
vision.detail.tiny      Tier0 cer_threshold    requires ocr_ok         -> Detail
vision.multi.morecount  Tier0 closed_set       requires multi_image_ok -> MultiImage
vision.describe.scene   Tier1 (all shadowed)   requires vision_ok      -> Grounding
```

`CATEGORIES_VISION = ["OCR","Counting","Color","Spatial","Relational","ChartQA","VQA","Detail","MultiImage","Grounding"]`. `suite_hash()` folds the **image shas** (the pinned bytes) + checker specs.

**`mvp/aeon/runner.py`** — add `run_vision_benchmark(run_id, model, target_url, judge_model=None, ...)`:
1. Build target (mock → `MockVisionTarget`). Call `probe_vision`. If `not vision_ok` → `db.create_run(..., board="vision")`, store `vision_probe_json`, mark `capability_absent`, `finish_run`, **return**.
2. Per case: skip (record `na_capability`) if its `requires` flag is false (coverage, not 0). Else load image bytes from `assets/vision/<sha>.png`, assemble `content=[text_block(prompt), image_block(png), ...]`, call `target.chat`, capture speed (+ vision fields), `evaluate(case, text, judge)`, `db.save_result(..., board="vision")`.

**`mvp/aeon/db.py`** — add `board TEXT DEFAULT 'text'` to **both** `runs` and `results` via a guarded migration (SQLite has no column `IF NOT EXISTS`):
```python
def _ensure_columns(c):
    have = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    if "board" not in have:
        c.execute("ALTER TABLE runs ADD COLUMN board TEXT DEFAULT 'text'")
    if "vision_probe_json" not in have:
        c.execute("ALTER TABLE runs ADD COLUMN vision_probe_json TEXT")
    have_r = {r["name"] for r in c.execute("PRAGMA table_info(results)")}
    if "board" not in have_r:
        c.execute("ALTER TABLE results ADD COLUMN board TEXT DEFAULT 'text'")
```
Called from `init_db()`. `create_run` / `save_result` gain a `board="text"` kwarg (default preserves existing behavior). `all_results_with_runs(board="text")` gains a `WHERE r.board = ?` filter.

**`mvp/aeon/scoring.py`** — add `vision_leaderboard()` mirroring `leaderboard()` but: `db.all_results_with_runs(board="vision")`; only models whose latest vision run has `vision_ok=True` appear (`capability_absent` runs excluded); returns `{categories: CATEGORIES_VISION, models:[...]}` with vision speed fields (`avg_ttft_after_image_ms`, `avg_decode_tps`) and a per-model `coverage` (`"9/10"`). **Never merges into `leaderboard()`** — the text composite's denominator is unchanged (the structural "never penalized" guarantee).

**`mvp/aeon/app.py`** — add `GET /api/vision/suite` → `vision_suite.summary()`; `GET /api/vision/leaderboard` → `scoring.vision_leaderboard()`; `POST /api/vision/runs` → launches `run_vision_benchmark` in a thread (mirrors `/api/runs`). The text path is untouched.

**`mvp/web/`** — add a **board tab bar**: `[ Text ] [ Vision ] [ Audio (soon) ]`.
- **Honest scope note:** `CATS`/`WEIGHTS` are module globals set once in `init()`; per-tab switching is a **small but real refactor**, not free. Parameterize `CATS`/`WEIGHTS`/`refreshBoard`/`renderBoard`/`renderChart` by the active board and key weight profiles per board. The inline-SVG radar is data-driven by `CATS`, so once parameterized the vision radar renders with no new chart code.
- Vision tab fetches `/api/vision/suite` + `/api/vision/leaderboard`; adds two speed columns ("img-ingest TTFT (ms)", "decode tok/s") and a **coverage badge** per row. Uncovered radar axes render as a **dashed gap**, not a 0.
- Models without vision aren't in the response → they don't render. Optional footnote: "Probed, no vision: …".
- Audio tab is a disabled placeholder ("audio input unconfirmed in LM Studio — see §7").

---

## 6. Exact files to add / change

**Add:**
- `mvp/aeon/imagegen.py` — Pillow generators + ground truth; **writes pinned PNG bytes to `assets/vision/<sha>.png`**.
- `mvp/aeon/probe.py` — `probe_vision()` (control-pair color gate, multi-image + OCR sub-flags) and a `probe_audio()` stub (§7).
- `mvp/aeon/vision_suite.py` — vision `CASES`, `CATEGORIES_VISION`, `suite_hash`, `summary`.
- `mvp/aeon/assets/vision/` — cached pinned PNGs + a bundled `DejaVuSans.ttf` if not resolvable.

**Change:**
- `mvp/aeon/targets.py` — add `text_block`/`image_block` helpers; add `MockVisionTarget` persona (slot-formatted canned answers keyed by vision `_case_id`); add `n_images`/`image_bytes`/`ttft_after_image_ms` to the returned dict when image blocks are present. **`_clean` unchanged.**
- `mvp/aeon/evaluators.py` — add `extract_slot`, `_norm`, `_levenshtein`, `chk_closed_set`, `chk_count_slot`, `chk_cer_threshold`; register in `CHECKERS`.
- `mvp/aeon/runner.py` — add `run_vision_benchmark()` with the probe gate + image assembly + sub-gate skips.
- `mvp/aeon/db.py` — guarded `board` column on `runs` + `results`; `vision_probe_json` on `runs`; `board` kwarg on `create_run`/`save_result`; `board` filter on `all_results_with_runs`.
- `mvp/aeon/scoring.py` — `vision_leaderboard()` with capability gating + vision speed aggregates + coverage.
- `mvp/aeon/app.py` — `/api/vision/suite`, `/api/vision/leaderboard`, `/api/vision/runs`.
- `mvp/web/index.html` — board tab bar + vision coverage/speed columns markup.
- `mvp/web/app.js` — per-board state (the real refactor), vision fetches, coverage badge, dashed-gap radar axes.
- `mvp/web/styles.css` — tab + badge styling (minor).

**No change** to the text leaderboard's scoring or composite — vision is fully segregated.

**Offline acceptance test (must pass before any GPU):** `python -m aeon.runner_vision mock-good mock` → vision board renders with `MockVisionTarget` answering slot-formatted, probe `vision_ok=True`, all Tier-0 cases scored, coverage `10/10`. This preserves the MVP's "demoable with zero GPU" property for the new board.

---

## 7. Build-deferred (with the exact enabling path)

**VIDEO + MOTION — deferred.** LM Studio's `/v1` is image-only; there is no `video_url` content type. **Enable-later path:** add `imagegen.render_motion_frames(...)` emitting a deterministic frame sequence (a shape translating left→right across N PNGs with generator-known per-frame positions = **machine-known** motion ground truth), attach as a **multi-image** message, and ask a Tier-0 closed-set question ("which direction did the shape move?" / "how many frames show the dot in the top half?"). Pin the **frame bytes** (sha per frame); decoder/timestamps are non-authoritative provenance. Reuses the multi-image plumbing built above. Board label: *"video = N pinned frame images (not regenerated); native video not exercised; temporal resolution bounded by sampling — sub-frame and audio-sync events unmeasurable."* Native `video_url` stays gated behind a future probe.

**AUDIO board — deferred (not built now).** LM Studio support for `input_audio` content blocks (and `/v1/audio/*`) is **UNCONFIRMED** (open feature request, bug-tracker #1715). The user's vision models (qwen3-vl) are not audio models. The honest expected outcome today is **`audio_transport: none` → zero models on the board → board hidden** — a capability gap, not a defect. **Enable-later path, concrete:**
1. `probe.py: probe_audio(target)` — send a tiny pinned WAV (a known spoken nonce-digit string) as an `input_audio` base64 block; classify by HTTP-accept **and** a control-pair nonce-digit sanity gate (answer must contain the digits *only* when the audio block is present, distinguishing "API took the bytes" from "model actually listened"). HTTP 400/415 → `audio_unsupported`, board stays hidden with the raw probe response surfaced.
2. `audiogen.py` — deterministic WAV generation (`wave` + `struct`/numpy): tones, DTMF, bundled tiny clips with pinned transcripts.
3. Reuse `cer_threshold` for transcription (CER); add `wer_threshold` (both §6b.2.4-unrestricted). `closed_set` for language-ID / audio-event / intent; `count_slot` for counts (e.g. speaker count, "how many beeps"). **Translation is scored by closed-fact comprehension probes** (e.g. "what number did the speaker say?" → `count_slot`) + keyword-presence over a pinned synonym set — **not** raw BLEU/chrF as the gate (BLEU/chrF are *not* in the §6b.2.4 unrestricted class; they ride along as reported diagnostics only). **Paralinguistics/emotion → Tier 2, arena, excluded from the composite.**
4. Audio speed metrics (`audio_ingest_ms` as a TTFT-on-audio proxy — *not* an isolated ingest measurement; RTF for transcription only) are **null until the probe passes** and never fabricated.
5. A third `audio_suite.py` + `scoring.audio_leaderboard()` + `/api/audio/*` + the (already-stubbed) Audio tab. The plumbing — `board` column, modality tab, probe-gate-then-hide — is built once for vision and reused.

Both deferrals follow the same discipline: **probe first, hide if absent, never zero-penalize, never co-rank with text.**

---

## 8. DESIGN.md deltas (precise; append-only, bump to v0.4)

Add one new section and two pointer edits. Append-only; history is never rescaled.

**1. §6 table (Vision/Audio rows) — reframe.** Change the Vision/Audio notes to: *"Vision/Audio are **not categories in the text composite**; each is a **separate modality board** (§6c) with its own composite, gated by a capability probe. Absence excludes a model from that board only — never penalizes the text composite (coverage shown, §14)."*

**2. New §6c — Multimodal boards (separate evaluation graphs).** Authoritative text:
- **6c.1 Board separation.** Each modality (Vision, Audio) is a standalone evaluation graph with its own category set, composite, radar, and leaderboard, stored under `board ∈ {text, vision, audio}`. A model is evaluated on a modality board **iff** it passes that board's capability probe (§6c.2). Modality scores are **never summands in the text composite** and are never co-ranked across boards. Supersedes the v0.3 "Vision/Audio as if-supported text categories" treatment.
- **6c.2 Capability probe & gating.** Before a modality run, a deterministic probe sends a generated stimulus with a **control pair** (the same prompt with the modality block removed); the modality is "reached" only if the answer **changes** when the block is present. `reached=true` admits the model; `false`/`unsupported` records `capability_absent` and **excludes the model from that board with zero effect on any other board**. Sub-capabilities (multi-image, OCR, audio-understanding) gate individual categories (covered-or-`N/A`, coverage shown). Probe result + per-board coverage denominator are stored and shown.
- **6c.3 Tier map for modalities.** Tier 0 (programmatic, no judge): OCR (CER/WER), counting (slot-strict integer), color/position/relation/MCQ-VQA (closed pinned set, **slot-only exact match**), chart value (numeric ± pinned band on **generated** charts). Tier 1 (binary rubric): descriptive grounding only, and on a single-modality-family fleet **only Tier-0-shadowed criteria are composite-eligible** (the cross-family verifier of §6b.4 is structurally unavailable; un-shadowed criteria are `single_judge`, non-record-eligible). **Tier 2 (image aesthetics, voice emotion/paralinguistics) → human arena only, excluded from the auto-composite.** Free-form bbox grounding on natural images is **not admissible** (gold box is an annotation); synthetic-only grounding with a generous threshold + dead-band may be added later. Native video is **not** exercised (image-only API); video decomposes to **pinned frame images** (multi-image), with motion ground truth **synthetic** where composite-eligible. The §6 0.7 deterministic-dominance floor applies per board.
- **6c.4 Deterministic stimulus generation & pinning.** Modality stimuli are produced by pinned pure functions (Pillow image generators; `wave`/numpy audio generators). **The generated bytes are the pinned artifact** (`sha256`, stored in the blob store, referenced by `image_sha`/`audio_sha`); the generator spec is provenance metadata, never the re-derivation authority (rendering is not byte-identical across library/platform versions). This makes every modality Tier-0 case re-derivable byte-for-byte (§12).
- **6c.5 Modality speed.** Captured and shown **separately from quality and from text speed** (§14): vision records `ttft_after_image_ms` (labeled *includes upload + decode + prefill; co-located = lower bound*), `n_images`, `image_bytes`; audio records analogous proxies (RTF for transcription only). Black-box streaming APIs **cannot** separate network-upload from server-side decode/prefill — this is stated, never faked (§11). Metrics that require backend per-image/per-audio `usage` are derived **only** if exposed, else null and hidden.
- **6c.6 Feasibility & transport.** Vision input = OpenAI `image_url` base64 data-URI blocks (confirmed for qwen3-vl via LM Studio). **Audio input (`input_audio` blocks) support in LM Studio is unconfirmed** (bug-tracker #1715) and is probe-gated before any audio board publishes; the honest default outcome on today's fleet is an empty/hidden audio board. Capabilities are re-checked on a TTL / on a recipe-hash change so a board auto-revives if the backend later ships the modality.

**3. §14 — add one sentence:** *"Capability-gated modality boards (Vision/Audio, §6c) each carry their own coverage-denominator badge; a model absent from a modality board is omitted from that board only and is **never zero-penalized on the text composite**."*

**4. Glossary (§3) — add:** *`Board / modality`* = "an independent evaluation graph (`text|vision|audio`) with its own composite and capability probe; never co-ranked across boards (§6c)."

This sequences as **M6** in §20/§21. M2's BYO-judge/Tier machinery and M1's trust-tier/ingest are prerequisites and reused verbatim — boards add a partition dimension and a capability preflight, not a new scoring engine. The MVP preflight can ship as the §2 probe (board gate + sub-flags); the heavyweight `target_capabilities`/`deployment_capabilities` tables and per-board anchors are promoted only at full M6.

---

### Single most load-bearing caveat

The vision board is **all Tier-0** (judge-free, server-re-derivable) plus **one fully-shadowed Tier-1** case, built on **synthetic stimuli with machine-known ground truth** so it clears the §6b.3 authoring gate without smuggling human annotation in as "pinned reference." Anything that genuinely needed a judge's appraisal (bbox-on-natural-images, motion labels on ambiguous clips, image aesthetics) is **dropped or deferred**, not dressed up as deterministic. Audio is **probed, not assumed**, and expected to yield an empty board on today's LM-Studio fleet — the correct capability-gated outcome.

**Key files (absolute):** `C:/Users/Albert/AEON Bench/mvp/aeon/targets.py`, `evaluators.py`, `runner.py`, `db.py`, `scoring.py`, `app.py`, `suite.py`, `web/app.js`, `web/index.html`; new: `C:/Users/Albert/AEON Bench/mvp/aeon/imagegen.py`, `probe.py`, `vision_suite.py`, `assets/vision/`; spec: `C:/Users/Albert/AEON Bench/DESIGN.md` (§6c, v0.4).
