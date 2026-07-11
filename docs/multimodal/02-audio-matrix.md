# AEON Bench — AUDIO Evaluation Matrix

*Implementation-ready spec. Consistent with DESIGN.md v0.3 (§2 trust tiers, §6/§14 capability gating, §6b tiered determinism, §10b BYO-judge, §11 speed honesty, §12/§17 content-pinning & re-derivation). Small-team ethos: probe cheaply, gate honestly, build only what a served model can actually exercise.*

> **One-line truth for this board:** on the user's LM Studio fleet today, the AUDIO board is **almost certainly empty**, because LM Studio's OpenAI-compatible `/v1` accepts only `text` and `image_url` content blocks — `input_audio` blocks and `/v1/audio/*` endpoints are **not confirmed shipped**. This board is **dormant until a capability probe passes**, and that is the *correct* capability-gated outcome (a deployment gap, not a model failure), not a defect.

---

## 0. Scope and what changed after adversarial review

This document supersedes the earlier "AUDIO matrix" draft. Three claims in that draft were demoted or removed because review showed they were **not truly deterministic** or **not feasible on the user's setup**. They are called out explicitly so nobody re-introduces them:

| Earlier claim | Verdict | What we do instead |
|---|---|---|
| **chrF++/sacreBLEU are Tier-0 "fully deterministic, unrestricted (§6b.2.4)"** and `score = chrf/100` is the composite gate for translation | **WRONG.** DESIGN §6b.2.4 enumerates the unrestricted set as exactly *WER / CER / edit-distance / exact-ROUGE*. chrF/BLEU are **continuous graded scores with an author-chosen threshold** and tokenization/normalization/smoothing knobs that shift the number — i.e. the `similarity_threshold` **restricted** class. | Translation's **composite-eligible** signal is a **closed-fact comprehension probe** (Tier 0) and/or **keyword-presence over a pinned synonym set** (Tier 1, shadowed). chrF/BLEU are kept as a **reported diagnostic only**, under the §6b.2.4 guardrail (signature-pinned, fixed threshold, dead-band, **never the sole gate**). |
| **Audio speed populated with concrete numbers** (`rtf:0.34`, `ingest 412 ms`) and "audio-ingest latency isolates the encoder pass" | **OVERCLAIMED & infeasible today.** Audio input is unsupported on LM Studio, so there is no audio request to time; and even when supported, a black-box streaming `/v1` API exposes only request-send → first-token (TTFT). You **cannot** isolate upload/decode/encode from prefill. | All audio speed metrics are **gated behind the audio probe**, **null until it passes**, and `audio_ingest_latency_ms` is redefined as an **honest proxy = TTFT-on-audio-request** (includes upload+decode+prefill, not isolable — stated, not faked). RTF kept only for transcription. |
| **WER/CER "reproducible because normalized" via a single Whisper-style English normalizer** | **UNDER-SPECIFIED.** Number/date/currency expansion and CJK segmentation are exactly where reproducibility breaks; one English artifact does not cover the panel. | A **per-language, content-hashed normalizer artifact**; the **reference is stored pre-normalized** so scoring direction is fixed; **CER default for no-space languages**; WER only where a pinned segmenter id exists. Number/date expansion is a **named §6b.3 admission risk**. |

Other review fixes folded in below: the diarization metric is **speaker-count only as the always-on signal** (DER demoted to opportunistic/diagnostic, expected-inconclusive); the AED synonym map and SLU/long-form MC must pass the **§6b.3 authoring gate** (out-of-map surface forms abstain, not auto-miss); the capability probe gains a **modality-stripped control pair** and **distinct nonce per trial**; long-form treats **chunked decomposition as the expected path**, not the exception; the board inherits the **30-day recheck TTL** so it auto-revives if LM Studio ships audio.

---

## 1. ⚠️ FEASIBILITY FLAG — the audio-input probe gates the entire board (read first)

**LM Studio audio input is UNCERTAIN and must be probed at runtime, never assumed.** As of mid-2026 the LM Studio OpenAI-compatible `/v1/chat/completions` endpoint supports `text` and `image_url` content blocks; `input_audio` content blocks and the dedicated `/v1/audio/transcriptions` · `/v1/audio/speech` endpoints are an **open feature request**, not shipped. Consequence: **for an LM-Studio-served fleet the AUDIO board may have zero rows.** That is the honest capability-gated outcome.

### 1.1 Capability probe (runs in the target resolver, DESIGN §5.3; written to `targets.capabilities_json.audio`)

The probe classifies the target into one transport before any audio case runs. **First success wins.** The probe is itself a content-pinned mini-suite (tiny bundled WAV assets keyed by sha256) so the result is re-derivable (§12), and it uses a **modality-stripped control pair** so "did the audio reach the model" is a deterministic comparison, never a judge call.

```jsonc
{
  "probe_order": [
    { "transport": "chat_input_audio",            // preferred: inline in chat
      "test": "POST /v1/chat/completions, one user message with an input_audio block (tiny WAV base64, format:'wav'), prompt 'Reply with only the digits you hear.'",
      "pass_if": "HTTP 200 AND choices[0].message.content present AND no 'unsupported content' error" },
    { "transport": "audio_transcriptions_endpoint", // fallback: dedicated ASR endpoint
      "test": "POST /v1/audio/transcriptions (multipart, model=<id>, file=tiny.wav)",
      "pass_if": "HTTP 200 AND 'text' field present" },
    { "transport": "none",
      "effect": "model EXCLUDED from the AUDIO board entirely (capability-gated, never zero-penalized). Coverage = 0/N. Text-board score UNAFFECTED." }
  ],

  "control_pair_and_sanity_gate": {
    "purpose": "distinguish 'API accepts the block' from 'model actually listens'",
    "method": [
      "For T in 1..3: pick a DISTINCT random nonce digit string (e.g. 'seven four nine').",
      "Send the SAME prompt TWICE: (a) WITH the input_audio block, (b) WITHOUT it (modality-stripped control).",
      "Require: nonce digits appear in (a) AND NOT in (b). The answer must CHANGE when audio is present."
    ],
    "classify": {
      "grounded":              "≥2 of 3 trials show digits in (a) and absent in (b) → transport stands",
      "accepted_not_grounded": "block accepted (200) but (a)==(b) or digits absent → EXCLUDE from board, flag operator: 'API takes the bytes but the model is deaf to them'",
      "inconclusive":          "5xx/timeout → retry n=2, then status=unknown, board excluded, surfaced"
    }
  },

  "recheck": { "ttl_days": 30, "invalidate_on": ["target.recipe_hash change", "probe asset digest change"],
               "note": "auto-revives the board if LM Studio later ships input_audio" }
}
```

The `accepted_not_grounded` state is the dangerous one — an endpoint that returns 200 but ignores audio would otherwise produce garbage that *looks like a bad model*. The control pair (answer must differ with vs without the block) plus distinct per-trial nonces (so the model can't pass by guessing the prompt text) makes the gate cheap and robust.

**Board admission gate** = `audio_transport ∈ {chat_input_audio, audio_transcriptions_endpoint}` **AND** `sanity = grounded`. Otherwise: no row on the AUDIO board; **text composite unchanged** (its denominator only ranges over text categories — the structural "never penalized" guarantee, §6/§14).

### 1.2 Single-family judge note (carry into the board, mirrors vision)

Tier-1 audio criteria need a judge; DESIGN §6b.4 requires a **different-family verifier** for any **un-shadowed** Tier-1 criterion or it is badged `single_judge` / **non-record-eligible**. On a single-audio-family fleet, un-shadowed audio Tier-1 is **permanently single_judge** → it contributes **diagnostics only**, never the record-eligible composite. **Design consequence:** keep the audio composite **Tier-0-dominant by construction** (it already is — see §2), and author Tier-1 audio criteria as **Tier-0-shadowed** (`tier0_check` over a fenced slot) so they stay eligible. If the launching/BYO judge itself lacks audio, audio Tier-1 is gated **N/A (coverage), never scored 0**.

---

## 2. The AUDIO dimension matrix (the board)

Nine quality dimensions + a separate SPEED axis + a Tier-2 carve-out. **JI** = judge-invariance basis. The audio board is the **most deterministic board in AEON**: dimensions 1, 4, 5, 6, 7 are **100% Tier 0 with no judge**; 2, 3, 8, 9 keep their **composite-eligible weight in a Tier-0 answer gate**, with Tier-1 as audit-only enrichment. **No audio category is pure-Tier-2.** The §6 deterministic-dominance floor (≥0.7 Tier-0/shadowed per category) is cleared trivially.

| # | Dimension (key) | What it tests | Tier | Deterministic scoring (composite-eligible signal) | JI basis |
|---|---|---|---|---|---|
| 1 | **ASR / transcription** (`audio.asr`) | verbatim speech→text; clean / noisy / accented / disfluent / number-heavy | **0** | **WER** (word langs) / **CER** (no-space langs) vs **pre-normalized** reference; `score = 1 − min(1, WER)` | pure edit-distance over a **pinned per-language normalizer**; no judge (§6b.2.4 unrestricted) |
| 2 | **Translation** (`audio.s2tt`) | speech L1 → text L2 | **0** (+1 shadow) | **closed-fact comprehension probe** (e.g. "what number/city/day?") → `exact_match`/`numeric_tolerance`; **keyword-presence** over pinned synonym set as Tier-1 shadow. **chrF/BLEU = diagnostic only** | comprehension answer is closed + pinned; chrF/BLEU **never the gate** |
| 3 | **Spoken-language understanding** (`audio.slu`) | comprehension of spoken content (decision, coreference, numeric extraction) — not transcription | **0** (+1 shadow) | **MC / numeric / `set_match`** into a fenced `<answer>` slot | closed answer + fenced slot; no judge for the gated part |
| 4 | **Language identification** (`audio.lid`) | which language (closed panel) | **0** | **`exact_match`** to ISO-639 code in slot; category metric **accuracy + macro-F1** | closed-set exact; no judge |
| 5 | **Audio-event / scene recognition** (`audio.aed`) | non-speech events & acoustic scenes over a pinned ontology | **0** | single-label `exact_match`; multi-label **micro/macro-F1** via `set_match` + **pinned synonym map** | closed label set + gated synonym map; no judge |
| 6 | **Speaker count / diarization** (`audio.diar`) | how many distinct speakers (primary); who-spoke-when (opportunistic) | **0** | **speaker-count `numeric_tolerance{tol:0}`** is the always-on signal; **DER demoted** to diagnostic, expected-inconclusive | integer exact = pure function; no judge |
| 7 | **Intent classification** (`audio.intent`) | spoken-command intent over a closed taxonomy | **0** | **`set_match` exact** over closed intent set in slot; **accuracy + macro-F1** | closed set + fenced slot; no judge |
| 8 | **Long-form understanding** (`audio.longform`) | comprehension over long audio (meetings/lectures); needle-in-haystack, ordering | **0** (+1 shadow) | **pinned QA** (MC / numeric / `set_match`) in slot; **capability-gated by duration**; **chunked is the expected path** | MC/numeric exact; no judge for the gated part |
| 9 | **SPEED** (`audio.speed`) | ingest-proxy latency, RTF, TTFT-after-audio | n/a | measured, **separate axis, never summed** (§14); **null until probe passes** | mechanical timing; trust-tiered (§2, §11) |
| — | **Paralinguistics** (emotion / tone / prosody) | "what emotion?", sarcasm, expressiveness, naturalness | **2** | **NOT in the auto-composite.** A closed-label Tier-0 *sub-slice* is allowed **only** with an objective pinned label (e.g. acted-emotion corpus); the appraisal remainder → **human arena** | closed slice judge-free; appraisal genuinely subjective → arena |

---

## 3. Per-dimension detail (what / tier+scoring / example evaluator / judge-invariance)

All Tier-0 cases force the answer into a **fenced typed slot** (`<answer>…</answer>`, `<transcript>…</transcript>`) so grading is a pure function of the slot, never prose-parsing. Extraction is `on_missing: "fail"` (the only admissible non-`inconclusive` value, §6b.3) — **no benefit of the doubt** (§6b.2.2). Evaluator specs fit the existing `case_versions` row: `prompt_json`, `reference_json`, `scoring_json` (`eval` in the MVP suite shape).

### 3.1 ASR / transcription — `audio.asr` (Tier 0)
- **Tests:** verbatim transcription. Strata: `clean`, `noisy` (added SNR), `accented`, `spontaneous/disfluent`, `domain` (numbers, named entities, code-switching).
- **Scoring:** **WER** = (S+D+I)/N over a **pinned, per-language** normalizer; **CER** for no-space languages. `score = 1 − min(1, WER)`; category quality = mean over cases with §13 cluster-bootstrap CI; anchor bands stamp `anchor_set_version`.
- **Reference is stored already-normalized** so scoring direction is fixed and reproducible — the runtime never re-normalizes the reference with a normalizer that could drift.

```jsonc
{
  "case_key": "asr.numbers.0427",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Transcribe the audio verbatim into <transcript></transcript>.",
                   "audio_ref": "sha256:<digest>", "audio_meta": {"sr":16000,"dur_s":8.1,"codec":"wav/pcm_s16le"} },
  "reference_json": { "text_normalized": "the shipment of nineteen forty seven crates departs at oh six hundred",
                      "normalizer_ref": "asr_norm.en.v1@sha256:<digest>" },
  "scoring_json": {
    "tier": 0,
    "extraction": { "slot": "transcript", "on_missing": "fail" },
    "checker_chain": [ { "checker": "wer", "version": "wer.v1",
      "params": { "normalizer_ref": "asr_norm.en.v1@sha256:<digest>", "ref_field": "text_normalized", "unit": "word",
                  "segmenter_ref": "ws_split.en.v1" } } ],
    "score_map": { "type": "linear_inverse", "expr": "1 - min(1, wer)" }
  }
}
```
- **JI:** WER is a pure function of (slot string, pinned pre-normalized reference, pinned normalizer/segmenter). Re-derived byte-identically server-side (§12). **No model judge.** **Named admission risk (§6b.3):** number/date/ordinal expansion (`1947` ↔ "nineteen forty seven", `0600` ↔ "oh six hundred") — the normalizer's expansion direction is pinned and its calibration set must include number-heavy/disfluent/accented near-misses.

### 3.2 Translation — `audio.s2tt` (Tier 0 comprehension gate; chrF/BLEU diagnostic only)
- **Tests:** speech-to-text translation L1→L2 across a pinned language-pair panel.
- **Scoring — the honest constraint:** free-form translation quality is paraphrase-tolerant, which §6b.2.3 bars from the composite unless reduced to a closed surface form. So translation is **never graded by "rate the translation."** Two composite paths:
  - **2a (Tier 0, preferred):** a **closed-fact comprehension probe** answerable from the translation — "What number did the speaker say?" → `<answer>42</answer>`; or a closed-set entity → `<answer>Berlin</answer>`. `exact_match`/`numeric_tolerance`. This exercises translation *understanding* deterministically.
  - **2b (Tier 1, shadowed):** binary **keyword-presence** over pinned required content words with a pinned **accepted-synonym set** (§6b.2.3): "Does the translation state the meeting is on Tuesday?" → `regex_constraint` over `{tuesday, tues, tue}`. Each criterion is Tier-0-shadowed; the judge is audit-only.
- **chrF/BLEU = diagnostic only**, under the §6b.2.4 `similarity_threshold` guardrail: a **fully pinned sacreBLEU signature** (`nrefs/case/eff/tok/smooth/version`) folded into the content hash, a fixed threshold, a **dead-band** (near-boundary → `partially_scored` → drift queue), and **never the sole gate**. Reported beside the composite, not in it.

```jsonc
{
  "case_key": "s2tt.fr-en.num.0112",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Listen to the French audio, then answer in English. Put the number in <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "question": "At what hour (24h) does the train leave?" },
  "reference_json": { "answer": 15 },
  "scoring_json": {
    "tier": 0, "extraction": { "slot": "answer", "on_missing": "fail" },
    "checker_chain": [ { "checker": "numeric_tolerance", "version": "numtol.v1",
                         "params": { "ref_field": "answer", "tol": 0, "grammar": "single_integer.v1" } } ],
    "diagnostics": { "chrf": { "checker": "chrf", "version": "chrf.v1",
      "params": { "variant": "chrf++", "sacrebleu_sig": "chrF2+nc6+nw2+v2.4.x", "refs": ["The train leaves at 15:00.", "The train departs at 3 pm."] },
      "composite_eligible": false, "guardrail": "similarity_threshold", "dead_band": 0.03 } }
  }
}
```
- **JI:** the composite signal is a closed integer in a fenced slot — pure function, no judge. chrF is explicitly `composite_eligible:false`.

### 3.3 Spoken-language understanding — `audio.slu` (Tier 0 gate + Tier 1 audit)
- **Tests:** comprehension distinct from transcription — "what did the speaker decide?", coreference, implication, numeric extraction.
- **Scoring:** **Tier 0 is the composite gate** — questions authored as **MC / numeric / `set_match`** into `<answer>`. **Tier 1 is audit-only** for short free-text, only where binary + span-decidable, and **Tier-0-shadowed**.

```jsonc
{
  "case_key": "slu.dialog.resched.0031",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Listen, then answer. Put the single letter in <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "question": "What day was the meeting finally set for?",
                   "options": {"A":"Monday","B":"Tuesday","C":"Thursday","D":"Friday"} },
  "reference_json": { "answer": "C" },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"exact_match", "version":"exact.v1",
                         "params": { "ref_field":"answer", "case_insensitive": true, "closed_set":["A","B","C","D"] } } ] }
}
```
- **JI:** MC + fenced slot + exact-match = pure function. The Tier-1 enrichment never enters the composite. **Validity gate (§6b.3.5):** a sample of MC cases must pass a human-gold check that **distractors are defensibly wrong from audio alone** and the correct option is unambiguous — prefer needle/numeric questions with a single ground truth over open "what did they decide" MCs.

### 3.4 Language identification — `audio.lid` (Tier 0)
- **Tests:** spoken-language ID over a closed panel; short-utterance and code-switch-dominant-language sub-cases.
- **Scoring:** **`exact_match`** to ISO-639-1/3 code into a slot; category metric **accuracy + macro-F1** (so rare languages aren't masked by a dominant class).

```jsonc
{
  "case_key": "lid.short.sw.07",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Identify the spoken language. Output the ISO-639-1 code in <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "closed_set": ["en","fr","de","sw","hi","zh","ar","es"] },
  "reference_json": { "answer": "sw" },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"set_match", "version":"setmatch.v1",
                         "params": { "ref_field":"answer", "allowed_set_field":"closed_set", "mode":"exact_single" } } ],
    "aggregate": { "category_metric": ["accuracy","macro_f1"] } }
}
```
- **JI:** closed-set exact label = pure function. No judge.

### 3.5 Audio-event / scene recognition — `audio.aed` (Tier 0)
- **Tests:** non-speech sound events ("dog bark", "glass break", "siren") and acoustic scenes ("airport", "park") over a **pinned ontology** (AudioSet/ESC/TAU-style, content-hashed).
- **Scoring:** single-label → `set_match` exact; multi-label → **micro/macro-F1** over predicted vs gold label sets (`mode: multi_label_f1`), with a **pinned synonym map** collapsing surface variants.

```jsonc
{
  "case_key": "aed.multilabel.street.0188",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "List all sound events present, comma-separated, choosing only from the provided labels, inside <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "ontology_ref": "audioset_v1_527@sha256:<digest>",
                   "allowed_labels": ["car_horn","siren","speech","footsteps","dog_bark"] },
  "reference_json": { "labels": ["car_horn","siren","speech"] },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"set_match", "version":"setmatch.v1",
      "params": { "ref_field":"labels", "allowed_set_field":"allowed_labels", "mode":"multi_label_f1", "f1":"micro",
                  "synonym_map_ref":"audioset_syn.v1@sha256:<digest>", "on_out_of_map":"abstain" } } ],
    "score_map": { "type":"passthrough", "expr":"f1" } }
}
```
- **JI:** F1 over closed predicted/gold sets with a pinned synonym map = pure function. No judge. **Admission risk made load-bearing (§6b.3):** the synonym map must pass the authoring gate against adversarial paraphrases ("barking", "a dog", "canine vocalization"); an **out-of-map surface form routes to `abstain`/`partially_scored` (drift queue), not auto-miss** (mirrors §6b.2.3) — otherwise a legitimately-correct paraphrase silently scores zero.

### 3.6 Speaker count / diarization — `audio.diar` (Tier 0, count only)
- **Tests:** **how many distinct speakers** (robust primary); who-spoke-when (opportunistic).
- **Scoring:** **speaker-count `numeric_tolerance{tol:0}`** is the **only always-on, composite-eligible** signal. **DER is demoted** to an opportunistic diagnostic: general chat LLMs over `/v1` essentially never emit calibrated timed segments, so DER is **expected-inconclusive for nearly all targets** — `on_untimed: "inconclusive"`, and an **inconclusive DER does not count toward coverage** and never becomes a wrong answer.

```jsonc
{
  "case_key": "diar.count.meeting.0204",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "How many distinct speakers are in this audio? Put the integer in <answer></answer>.",
                   "audio_ref": "sha256:<digest>" },
  "reference_json": { "n_speakers": 3, "rttm_ref": "sha256:<digest>" },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"numeric_tolerance", "version":"numtol.v1",
                         "params": { "ref_field":"n_speakers", "tol":0, "grammar":"single_integer.v1" } } ],
    "diagnostics": { "der": { "checker":"der", "version":"der.v1",
      "params": { "scorer_ref":"pyannote_metrics@sha256:<digest>", "rttm_ref_field":"rttm_ref", "collar_ms":250,
                  "requires":"timed_segments", "on_untimed":"inconclusive" }, "composite_eligible": false } } }
}
```
- **JI:** integer exact-match is fully deterministic. The **ordered who-spoke-when turn-sequence task is dropped from the composite** unless a *fully specified* tolerant metric (pinned sequence-alignment algorithm + pinned `k` + partial-credit formula + canonical first-appearance relabeling proven order-invariant) ships and passes §6b.3 — an unspecified "turn F1 within ±k" is **not yet judge-invariant** and must not be presented as such.

### 3.7 Intent classification — `audio.intent` (Tier 0)
- **Tests:** spoken-command intent over a closed taxonomy (`set_alarm`, `play_music`, `lights_off`…); closed slot-values where applicable.
- **Scoring:** **`set_match` exact** over the closed intent set in a slot; **accuracy + macro-F1**; optional slot sub-score via `field_match` over closed slot values.

```jsonc
{
  "case_key": "intent.smarthome.0451",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Classify the spoken command's intent. Output the intent id in <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "intent_set": ["set_alarm","play_music","lights_off","weather_query"] },
  "reference_json": { "intent": "lights_off" },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"set_match", "version":"setmatch.v1",
                         "params": { "ref_field":"intent", "allowed_set_field":"intent_set", "mode":"exact_single" } } ],
    "aggregate": { "category_metric": ["accuracy","macro_f1"] } }
}
```
- **JI:** closed-set exact, fenced slot. No judge.

### 3.8 Long-form understanding — `audio.longform` (Tier 0, chunked is the default path)
- **Tests:** comprehension over long audio (meetings/lectures/podcasts): global facts, cross-segment coreference, **needle-in-a-haystack**, ordering.
- **Scoring:** **pinned QA** as MC / numeric / `set_match` into `<answer>`. **Duration-gated:** if the clip exceeds the model's discovered `max_audio_dur_s`, the case is **excluded (capability-gated), not scored 0** — coverage reflects it (§14).
- **Chunking is the EXPECTED path, not the exception.** Realistically most served models that accept audio will not have a multi-minute context, so the runner's **fixed-window + overlap chunk-and-aggregate** is the default for long clips. Chunked cases are **badged `chunked_decomposition`** and **segregated from native-long-context** scores (never silently co-ranked, mirroring §11 co-location honesty). Native single-block long-form is the **rare, badged exception**. `max_audio_dur_s` is **discovered by the capability probe before authoring long-form coverage**.

```jsonc
{
  "case_key": "longform.lecture.needle.0009",
  "modality": "audio", "board": "audio", "scoring_tier": 0,
  "prompt_json": { "instruction": "Answer from the audio. Put the number in <answer></answer>.",
                   "audio_ref": "sha256:<digest>", "audio_meta": {"dur_s":842},
                   "question": "What year did the speaker say the bridge was built?" },
  "reference_json": { "year": 1937 },
  "scoring_json": { "tier": 0, "extraction": { "slot":"answer", "on_missing":"fail" },
    "checker_chain": [ { "checker":"numeric_tolerance", "version":"numtol.v1",
                         "params": { "ref_field":"year", "tol":0, "grammar":"single_integer.v1" } } ],
    "capability_gate": { "max_audio_dur_s_field": "target.capabilities_json.audio.max_audio_dur_s",
                         "on_exceeds": "chunk_decompose_or_exclude", "badge": "chunked_decomposition" } }
}
```
- **JI:** numeric/MC exact, fenced slot, no judge. Coverage honesty + decomposition badge keep it honest.

### 3.9 Paralinguistics (emotion / tone / prosody) — Tier 2, arena-only
- **Tests:** "what emotion?", sarcasm, expressiveness, naturalness, voice quality.
- **Tier 2.** A **closed-label Tier-0 sub-slice** is permitted **only** where a discrete pinned label genuinely exists (e.g. an acted-emotion corpus with a gold categorical label → `set_match`, a small AED-adjacent composite-eligible slice). The **appraisal remainder** ("how warm / sarcastic / expressive / natural") is genuinely subjective → **human arena, Bradley-Terry, excluded from the auto-composite** (§6b.1, §13).
- **JI:** the closed-label slice is judge-free; the appraisal remainder is *not* judge-invariant **by design**, so it is routed away from the composite — no false determinism claim is made. "Felt" emotion on in-the-wild audio stays Tier 2; only objective acted-label corpora qualify for the slice.

---

## 4. AUDIO SPEED metrics (`audio.speed`) — separate axis, null until the probe passes

Speed is a **column + Pareto filter, never summed into quality** (§14). **Every metric here is gated behind §1.1 and is `null` until `audio_transport ∈ {supported}` and `sanity = grounded`.** Trust-tiered (§2, §11): `self_reported` shown-but-segregated; `orchestrated`/`attested` record-eligible. Timing uses §11 methodology (separate load-gen process, `perf_counter_ns`, warmup discard K=5, error-accounted).

**Honesty redefinition (the load-bearing fix):** over a black-box streaming `/v1` API the client observes only **request-send → first output token (TTFT)**. You **cannot** isolate audio upload, server-side decode, or encoder pass from prefill. Therefore:

| Metric | Definition (honest) | Unit | Notes |
|---|---|---|---|
| `audio_ingest_proxy_ms` | **= TTFT on the audio request.** Includes base64 upload (~+33% inflation, **counted not subtracted**) + server decode + encoder pass + prefill — **not isolable**. | ms | Renamed from "ingest latency" to kill the false "isolates the encoder" claim. Also reported **per audio-second** so short clips don't flatter. Co-located → "lower bound," never cross-compared with 2-box (§11). |
| `rtf` | `processing_time / audio_duration`, **transcription only** (needs a duration denominator). `processing_time` = e2e − client overhead, a **whole-request** figure, not isolated decode. | ratio (lower better) | `null` for non-ASR cases. |
| `ttft_after_audio_ms` | request-send → first output token for audio-conditioned generation. | ms | Audio analogue of text TTFT. (Same measurement as `audio_ingest_proxy_ms`; reported once, two names retired in favor of one — see shape below.) |
| `decode_tok_s`, `e2e_latency_ms`, `success_rate` | carried from the MVP runner. | — | Errors (`413` payload-too-large on big clips, `429`, `timeout`) are **recorded outcomes**, never dropped (§11). |

```jsonc
// results.speed_json — additive; metrics_schema_version bumped. NULL until audio probe passes.
{
  "metrics_schema_version": "audio.v1",
  "audio_meta": { "dur_s": 8.1, "sr": 16000, "bytes_b64": 259312, "codec": "wav/pcm_s16le" },
  "audio_ingest_proxy_ms": null,                 // = ttft on audio request; includes upload+decode+prefill (NOT isolable)
  "audio_ingest_proxy_ms_per_audio_s": null,
  "rtf": null,                                   // transcription only; null otherwise
  "decode_tok_s": null, "e2e_latency_ms": null,
  "load_model": "closed_loop", "concurrency": 1, "warmup_discarded": 5,
  "outcome": "audio_unsupported",                // | "success" | "413_payload_too_large" | "429" | "timeout"
  "colocated_loadgen": false,                    // co-located → "lower bound on latency", never cross-compared (§11)
  "trust_tier_note": "self_reported speed not record-eligible"
}
```

**Audio-specific honesty flags:** (1) RTF and the ingest proxy are duration-dependent → always also report the **per-audio-second** form. (2) base64 inflation (~33%) is **real upload cost, included**, not subtracted. (3) RTF is defined **only** for transcription. (4) On today's LM Studio fleet the expected `outcome` is `audio_unsupported` and every metric is `null` — that is correct, not a bug.

---

## 5. Evaluator JSON shapes (implementation-ready)

### 5.1 New Tier-0 checker primitives (extend DESIGN §6b.2.1)

Only the genuinely-unrestricted ones are Tier-0 composite-eligible; **`chrf` is registered as a restricted diagnostic**, never as §6b.2.4-unrestricted.

```jsonc
// packages/shared/schema/audio_checkers.v1.json — pure functions (candidate, reference, params) -> {satisfied|score, evidence}
{
  "wer":  { "class": "unrestricted",                 // §6b.2.4 (edit-distance)
            "params": { "normalizer_ref":"<pinned, per-language>", "segmenter_ref":"<pinned>", "ref_field":"text_normalized", "unit":"word" },
            "returns": { "wer":"float", "edits":{"S":"int","D":"int","I":"int","N":"int"} } },
  "cer":  { "class": "unrestricted",
            "params": { "normalizer_ref":"<pinned, per-language>", "ref_field":"text_normalized" },
            "returns": { "cer":"float" } },
  "set_match_multilabel": { "class": "unrestricted",
            "params": { "ref_field":"labels", "allowed_set_field":"allowed_labels", "mode":"multi_label_f1",
                        "f1":"micro|macro", "synonym_map_ref":"<pinned>", "on_out_of_map":"abstain" },
            "returns": { "f1":"float","precision":"float","recall":"float" } },
  "chrf": { "class": "restricted_diagnostic",        // §6b.2.4 similarity_threshold guardrail; NEVER sole gate; composite_eligible:false
            "params": { "variant":"chrf++|chrf", "sacrebleu_sig":"<full pinned signature>", "refs_field":"refs",
                        "threshold":"<fixed>", "dead_band":"<float>" },
            "returns": { "chrf":"float","status":"scored|partially_scored" } },
  "der":  { "class": "restricted_diagnostic",        // expected-inconclusive; composite_eligible:false
            "params": { "scorer_ref":"<pinned>", "rttm_ref_field":"rttm_ref", "collar_ms":250,
                        "requires":"timed_segments", "on_untimed":"inconclusive" },
            "returns": { "der":"float|null","status":"scored|inconclusive" } }
}
```
**Lint rule (extends §6b.2.1):** any audio checker with a missing `normalizer_ref` / `segmenter_ref` / `sacrebleu_sig` / `synonym_map_ref` / `scorer_ref` is **rejected at suite-build**. Any `chrf`/`der` with `composite_eligible: true` is **rejected** (they are diagnostics by class). Any `sha256:` value that is not a well-formed 64-hex digest resolving to a stored object is **rejected** (no label-as-hash).

### 5.2 Per-case evaluator spec (canonical, stored in `case_versions.constraints_json`)

```jsonc
{
  "case_key": "audio.<dimension>.<variant>.<id>",
  "modality": "audio", "board": "audio",
  "scoring_tier": 0,                                  // 0 dominant; 1 only as audit-only, Tier-0-shadowed; 2 -> arena
  "capability_requirements": {
    "audio_transport_in": ["chat_input_audio","audio_transcriptions_endpoint"],
    "sanity": "grounded",
    "max_audio_dur_s": 60,                            // case excluded/chunked (coverage--) if target max < this
    "needs_timed_output": false
  },
  "prompt_json": { "instruction":"…", "audio_ref":"sha256:<digest>",
                   "audio_meta": { "sr":16000, "dur_s":8.1, "codec":"wav/pcm_s16le" },
                   "closed_set":["…"]?, "options":{"A":"…"}?, "question":"…"? },
  "reference_json": { /* text_normalized | answer | labels | n_speakers | year … (+ normalizer_ref) */ },
  "scoring_json": {
    "tier": 0,
    "extraction": { "slot":"answer|transcript", "on_missing":"fail" },   // §6b: on_missing ∈ {fail, inconclusive}
    "checker_chain": [ { "checker":"wer|cer|exact_match|set_match|numeric_tolerance", "version":"<pinned>", "params": { … } } ],
    "score_map": { "type":"linear_inverse|linear|passthrough", "expr":"1 - min(1, wer)" },
    "aggregate": { "category_metric": ["accuracy","macro_f1"]? },
    "diagnostics": { "chrf": { … }?, "der": { … }? },  // composite_eligible:false by class
    "tier1_audit": null                                // or a judge_verdict.v1 binary rubric, Tier-0-shadowed, NEVER composite-weighted here
  },
  "exposure": "private", "is_canary": false
}
```

### 5.3 Capability/coverage gate (board admission, written by the probe)

```jsonc
// targets.capabilities_json.audio  (set by §1.1; gates the whole board)
{
  "audio_transport": "chat_input_audio | audio_transcriptions_endpoint | accepted_not_grounded | none",
  "sanity": "grounded | accepted_not_grounded | unknown",
  "accepts_formats": ["wav","mp3","flac"],
  "max_audio_dur_s": 60,                  // discovered/declared; gates & chunks long-form
  "probed_at": "2026-06-27T…Z", "probe_suite_hash": "<digest>", "recheck_after": "2026-07-27T…Z"
}
// If audio_transport ∈ {none, accepted_not_grounded} OR sanity != grounded:
//   -> model NOT on AUDIO board; coverage 0/N; text-board score UNAFFECTED (§6/§14).
```

### 5.4 Board composite (mirrors §14, audio-scoped)
- Quality composite = `Σ(weightᵢ · qualityᵢ) / Σ weightᵢ` over the audio categories the model **covers** (renormalized over covered categories so support = full score), client-side weights, with a **coverage-denominator badge** ("audio composite over 6/8 categories"). Per §14 incomparability discipline, the **headline ranking applies a default-on `min coverage ≥ X` guardrail**; lower-coverage models render in a separate "partial coverage" section rather than co-ranked behind only a badge.
- Speed is a **separate column + RTF/ingest filter + audio Pareto scatter** — never a summand.
- Tier-2 paralinguistics appears **only** in the audio arena Bradley-Terry view.

---

## 6. Genuinely deterministic vs Tier-1 vs Tier-2 (explicit verdict)

| Tier 0 — no judge, fully deterministic, re-derivable (§12) | Tier 1 — binary judge, audit-only & Tier-0-shadowed (single_judge on a one-family fleet → diagnostics only) | Tier 2 — arena only, never in composite |
|---|---|---|
| ASR (WER/CER), Translation **comprehension probe** (exact/numeric), LID (exact/F1), AED (exact/F1 + gated synonym map), Diarization **speaker-count** (exact), Intent (exact/F1), SLU + Long-form **answer gate** (MC/numeric/set), the **closed-label emotion slice** | Translation keyword-presence (2b), short free-text grounding in SLU/long-form — **shadowed** by the Tier-0 slot; program's boolean authoritative. **chrF/BLEU and DER are restricted diagnostics, not Tier-1 gates.** | **Paralinguistics appraisal** — emotion intensity, warmth, sarcasm beyond a discrete label, expressiveness, naturalness → human Bradley-Terry. |

The board makes its judge-invariance claim **honestly and easily**: the composite is essentially all Tier 0, the bias-prone "appraise quality" dial is absent, the one genuinely subjective dimension is carved to the arena, and the two seductive-but-non-deterministic metrics (chrF/BLEU, DER) are demoted to clearly-labeled diagnostics.

---

## 7. Implementation checklist (slots into DESIGN §20/§21 M6; sequence behind a working vision slice)

**Sequencing gate (small-team ethos):** build **only** the probe + gate + a hidden/empty Audio tab first. **Do not** author the checker suite or case content until **at least one served target returns `audio_transport ∈ {supported}` with `sanity = grounded`.** Today, on LM Studio, the expected result is `audio_transport: none` and **zero rows** — which is the correct outcome, and means steps 3–6 stay on the shelf.

1. **Probe (build now):** add §1.1 audio capability probe — transport classification + **control-pair + distinct-nonce sanity gate** + `max_audio_dur_s` discovery + 30-day recheck/recipe-hash invalidation → `targets.capabilities_json.audio`. **Hard gate** before any audio case. Surface `accepted_not_grounded` as an operator warning. Render the Audio tab as a hidden/empty state with the raw probe response until a target passes.
2. **Shared schema (build now, thin):** `packages/shared/schema/audio_checkers.v1.json` (`wer`, `cer`, `set_match_multilabel` unrestricted; `chrf`, `der` restricted-diagnostic). Extend the §6b.2.1 lint: reject unpinned refs, reject `composite_eligible:true` on chrf/der, reject label-as-hash.
3. **Per-language normalizers (deferred until a target passes):** one content-hashed artifact per language with a pinned segmenter id; **store references pre-normalized**; ship calibration sets proving §6b.3 thresholds on number-heavy/disfluent/accented near-misses. CER default for no-space languages.
4. **Suite content (deferred):** author categories 1–8; stratify ASR (clean/noisy/accented/disfluent/numbers) and translation (language-pair panel); run AED synonym maps and SLU/long-form MC through the §6b.3 validity gate; ship canary cases (§13).
5. **Speed (deferred):** extend `speed_json` to `audio.v1` (`audio_ingest_proxy_ms` + per-audio-s, `rtf`, `decode_tok_s`); errors (413/429/timeout) recorded; co-location/`self_reported` badged; **null until probe passes**.
6. **UI (thin now, full later):** `Text | Vision | Audio` tabs; Audio hidden/empty-state until `deployment_capabilities.audio.any_target_supported`; per-board coverage badge + default-on min-coverage guardrail; arena category for paralinguistics; capability chip `[A]` with sub-flags.

**Key paths (absolute):**
- `C:/Users/Albert/AEON Bench/DESIGN.md` — authoritative (§6 "Audio" row, §6b, §10b, §11, §14, §17, §20/§21 M6).
- `C:/Users/Albert/AEON Bench/mvp/aeon/evaluators.py` — register `wer`/`cer`/`set_match_multilabel`; keep `chrf`/`der` out of `CHECKERS` (diagnostics computed separately); enforce slot-strict extraction with `on_missing:fail` (the existing `chk_numeric_tolerance` whole-text fallback must **not** be reused for audio without an explicit slot-only path).
- Proposed: `C:/Users/Albert/AEON Bench/mvp/aeon/probe.py` (`probe_audio`), `audiogen.py` (deterministic WAV asset generation for the probe), `audio_suite.py`.
- Proposed: `C:/Users/Albert/AEON Bench/packages/shared/schema/audio_checkers.v1.json`, `audio_capabilities.v1.json`.

**Single most load-bearing caveat:** the entire AUDIO board is **dormant until the LM Studio audio-input feasibility probe (§1.1) passes**. With LM Studio's `/v1` accepting only text + image content blocks today, the expected result is `audio_transport: none` and **zero models on the board** — the correct capability-gated outcome, not a defect.
