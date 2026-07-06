"""The AUDIO suite — all Tier-0 programmatic (judge-free) on synthetic stimuli
with machine-known ground truth (no TTS/ASR dependency). Mirrors vision_suite.py.

Each case carries `audio` (a list of audiogen specs), a `prompt` that instructs
a fenced answer slot, an `eval` (slot-strict checker), and `requires`
"audio_ok" for the probe gate (probe.probe_audio).
"""
from __future__ import annotations

import hashlib
import itertools
import json

from . import audiogen

CATEGORIES_AUDIO = ["Counting", "Pitch", "Duration", "Timbre", "Pattern", "Speech"]
SUITE_ID = "aeon-audio-v2"          # v2: + Speech (real recorded spoken digits, exact-match)

# Pinned recorded-speech assets (Free Spoken Digit Dataset — see audiogen.speech_asset).
# digit -> (file, sha256); one recording per digit, speakers alternated.
_SPEECH = {
    0: ("0_jackson_0.wav", "eea86018ce1730baaf7f5dd6ec88c1f727dd90203521a9115b489310a248ea05"),
    1: ("1_theo_0.wav", "a856fff7bcb68328a76b269c0d8b1d4269c28ac7e3d6725fe9da5bdd05405b55"),
    2: ("2_jackson_0.wav", "214bac0c813b584410e3cb8cace2673d256b3fd735810bd516b2bfd0c2298620"),
    3: ("3_theo_0.wav", "d58347dea5ba78c84cff98c0cdc8bc133bff7d4ac379ab53f47cdadff566a9b8"),
    4: ("4_jackson_0.wav", "e0febd48e7cf7cfdca949d0d07769e0fc708fb2e8e7648691fa7e6ed7a5b1ded"),
    5: ("5_theo_0.wav", "a5c0beb43ad7e0236d927481d98b6f6bb1b21fbbd7bc26e7f42fcdf6d5f307d5"),
    6: ("6_jackson_0.wav", "fe7705fdfaddc378d72c479664ab8aacd53fa78a99c3d130ad74b1ff40212595"),
    7: ("7_theo_0.wav", "3074f5a5db8a3588bc8616c5888fa25bbac2c71728d14a6b413baf89d779ef70"),
    8: ("8_jackson_0.wav", "25172d71c574ee504094d4b704efa478a253d29ee7da562ce6ec4800ffdb6eaf"),
    9: ("9_theo_0.wav", "2a30491b41495dd457c496dc80c7f4066b28a8eb8bc4c57c100f4a7fa2a2e8ef"),
}

_DIGITS = [str(d) for d in range(10)]


def _speech_case(digit):
    f, sha = _SPEECH[digit]
    return {
        "id": f"audio.speech.digit{digit}", "category": "Speech", "tier": 0,
        "requires": "audio_ok",
        "audio": [{"gen": "speech_asset", "args": {"file": f, "sha256": sha}}],
        "prompt": "The audio contains ONE spoken English digit (zero through nine). "
                  "Which digit was spoken? Reply with ONLY <answer>N</answer> using the "
                  "single numeral, e.g. <answer>4</answer>.",
        "eval": {"checkers": [{"type": "closed_set", "options": _DIGITS, "answer": str(digit)}]},
    }

# All S/L strings of length 3 / 4 — the closed set for pattern transcription.
_PAT3 = ["".join(p) for p in itertools.product("SL", repeat=3)]
_PAT4 = ["".join(p) for p in itertools.product("SL", repeat=4)]

CASES = [
    # ---- Counting: n distinct beeps -> count_slot -------------------------
    {"id": "audio.count.beeps3", "category": "Counting", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "beeps", "args": {"n": 3, "freq": 880, "gap_ms": 220}}],
     "prompt": "The audio contains a series of short identical beeps separated by silence. "
               "How many beeps are there? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 3}]}},

    {"id": "audio.count.beeps5", "category": "Counting", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "beeps", "args": {"n": 5, "freq": 880, "gap_ms": 220}}],
     "prompt": "The audio contains a series of short identical beeps separated by silence. "
               "How many beeps are there? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 5}]}},

    {"id": "audio.count.beeps7", "category": "Counting", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "beeps", "args": {"n": 7, "freq": 700, "gap_ms": 200}}],
     "prompt": "The audio contains a series of short identical beeps separated by silence. "
               "How many beeps are there? Reply with ONLY <count>N</count>.",
     "eval": {"checkers": [{"type": "count_slot", "value": 7}]}},

    # ---- Pitch: which of two sequential tones is higher --------------------
    {"id": "audio.pitch.higher_first", "category": "Pitch", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "two_tones", "args": {"f1": 880, "f2": 330}}],
     "prompt": "Two tones play one after the other. Which tone is HIGHER in pitch? "
               "Reply with ONLY <answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "first"}]}},

    {"id": "audio.pitch.higher_second", "category": "Pitch", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "two_tones", "args": {"f1": 300, "f2": 900}}],
     "prompt": "Two tones play one after the other. Which tone is HIGHER in pitch? "
               "Reply with ONLY <answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "second"}]}},

    # ---- Duration: which of two beeps is longer ----------------------------
    {"id": "audio.duration.longer_first", "category": "Duration", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "long_short", "args": {"a_ms": 700, "b_ms": 180}}],
     "prompt": "Two beeps play one after the other. Which beep is LONGER in duration? "
               "Reply with ONLY <answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "first"}]}},

    {"id": "audio.duration.longer_second", "category": "Duration", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "long_short", "args": {"a_ms": 160, "b_ms": 640}}],
     "prompt": "Two beeps play one after the other. Which beep is LONGER in duration? "
               "Reply with ONLY <answer>X</answer> using one of: first, second.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["first", "second"], "answer": "second"}]}},

    # ---- Timbre: pure tone vs white noise ----------------------------------
    {"id": "audio.timbre.noise", "category": "Timbre", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "noise_or_tone", "args": {"kind": "noise"}}],
     "prompt": "The audio contains a single sound. Is it a pure musical tone or white noise (static hiss)? "
               "Reply with ONLY <answer>X</answer> using one of: tone, noise.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["tone", "noise"], "answer": "noise"}]}},

    {"id": "audio.timbre.tone", "category": "Timbre", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "noise_or_tone", "args": {"kind": "tone"}}],
     "prompt": "The audio contains a single sound. Is it a pure musical tone or white noise (static hiss)? "
               "Reply with ONLY <answer>X</answer> using one of: tone, noise.",
     "eval": {"checkers": [{"type": "closed_set", "options": ["tone", "noise"], "answer": "tone"}]}},

    # ---- Pattern: transcribe short/long beeps as an S/L string -------------
    {"id": "audio.pattern.sls", "category": "Pattern", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "pattern", "args": {"seq": "SLS"}}],
     "prompt": "The audio contains a sequence of 3 beeps; each beep is either SHORT (S) or LONG (L). "
               "Transcribe the pattern in order as a string of S and L characters. "
               "Reply with ONLY <answer>PATTERN</answer>, e.g. <answer>SSL</answer>.",
     "eval": {"checkers": [{"type": "closed_set", "options": _PAT3, "answer": "SLS"}]}},

    {"id": "audio.pattern.lssl", "category": "Pattern", "tier": 0, "requires": "audio_ok",
     "audio": [{"gen": "pattern", "args": {"seq": "LSSL"}}],
     "prompt": "The audio contains a sequence of 4 beeps; each beep is either SHORT (S) or LONG (L). "
               "Transcribe the pattern in order as a string of S and L characters. "
               "Reply with ONLY <answer>PATTERN</answer>, e.g. <answer>SLLS</answer>.",
     "eval": {"checkers": [{"type": "closed_set", "options": _PAT4, "answer": "LSSL"}]}},

    # ---- Speech: REAL recorded spoken digits (FSDD), exact-match transcription ----
    *[_speech_case(d) for d in range(10)],
]


def suite_hash():
    # fold the pinned WAV bytes (sha) + the eval specs — same recipe as vision
    parts = []
    for c in CASES:
        shas = [audiogen.synth(s)[0] for s in c["audio"]]
        parts.append({"id": c["id"], "shas": shas, "eval": c["eval"]})
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def summary():
    return {
        "suite_id": SUITE_ID,
        "suite_hash": suite_hash(),
        "n_cases": len(CASES),
        "categories": CATEGORIES_AUDIO,
        "cases": [{"id": c["id"], "category": c["category"], "tier": c["tier"],
                   "requires": c["requires"], "n_audio": len(c["audio"])} for c in CASES],
    }
