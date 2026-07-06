"""Deterministic audio generation (stdlib `wave` — no deps) with MACHINE-KNOWN
ground truth, mirroring imagegen.py for the AUDIO board (DESIGN §6c.6).

Every generator is a pure function of its args; the generated WAV bytes are the
pinned artifact (sha256), cached under assets/audio/<sha>.wav. Ground truth is
what the generator synthesized — no human annotation, no TTS dependency — so
audio Tier-0 cases are judge-free and re-derivable. 16 kHz mono 16-bit PCM.
`tone_wav` (the tiny transport-probe tone) is kept unchanged for probe_audio.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
import random
import struct
import wave

RATE = 16000  # suite sample rate: 16 kHz mono 16-bit (speech-model native)

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "audio")


def tone_wav(freq=440, secs=0.5, rate=8000, vol=0.5):
    n = int(rate * secs)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    frames = bytearray()
    for i in range(n):
        s = int(vol * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", s)
    w.writeframes(bytes(frames))
    w.close()
    return buf.getvalue()


# ---- frame builders (16-bit mono PCM at RATE) ------------------------------

def _tone_frames(freq, ms, rate=RATE, vol=0.5):
    """Sine burst with a 5ms linear fade in/out (no clicks; still deterministic)."""
    n = int(rate * ms / 1000)
    fade = int(rate * 0.005)
    out = bytearray()
    for i in range(n):
        env = 1.0
        if fade:
            if i < fade:
                env = i / fade
            elif i >= n - fade:
                env = max(0.0, (n - 1 - i) / fade)
        s = int(vol * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<h", s)
    return bytes(out)


def _silence_frames(ms, rate=RATE):
    return b"\x00\x00" * int(rate * ms / 1000)


def _noise_frames(ms, rate=RATE, vol=0.5, seed=1337):
    """White noise from a FIXED-seed PRNG — same bytes every run (pinnable)."""
    rng = random.Random(seed)
    n = int(rate * ms / 1000)
    out = bytearray()
    for _ in range(n):
        s = int(vol * 32767 * (rng.random() * 2.0 - 1.0))
        out += struct.pack("<h", s)
    return bytes(out)


def _finish(frames, meta, rate=RATE):
    """frames -> (sha, wav_bytes, meta); content-addressed cache like imagegen."""
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(rate)
    w.writeframes(frames)
    w.close()
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    os.makedirs(ASSET_DIR, exist_ok=True)
    path = os.path.join(ASSET_DIR, sha + ".wav")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(data)
    return sha, data, meta


# ---- generators: (args) -> (sha, wav_bytes, ground_truth) ------------------

def beeps(n=3, freq=880, gap_ms=200, beep_ms=140):
    """n identical tone bursts separated by silence. Ground truth: the count."""
    n = int(n)
    fr = _silence_frames(120)
    for i in range(n):
        if i:
            fr += _silence_frames(gap_ms)
        fr += _tone_frames(freq, beep_ms)
    fr += _silence_frames(120)
    return _finish(fr, {"n": n, "freq": freq, "gap_ms": gap_ms, "beep_ms": beep_ms})


def two_tones(f1=440, f2=880, tone_ms=400, gap_ms=250):
    """Two sequential tones. Ground truth: which is higher in pitch."""
    fr = (_silence_frames(120) + _tone_frames(f1, tone_ms) + _silence_frames(gap_ms)
          + _tone_frames(f2, tone_ms) + _silence_frames(120))
    return _finish(fr, {"f1": f1, "f2": f2, "higher": "first" if f1 > f2 else "second"})


def long_short(a_ms=600, b_ms=200, freq=660, gap_ms=300):
    """Two beeps of different durations. Ground truth: which is longer."""
    fr = (_silence_frames(120) + _tone_frames(freq, a_ms) + _silence_frames(gap_ms)
          + _tone_frames(freq, b_ms) + _silence_frames(120))
    return _finish(fr, {"a_ms": a_ms, "b_ms": b_ms, "freq": freq,
                        "longer": "first" if a_ms > b_ms else "second"})


def noise_or_tone(kind="tone", ms=600, freq=440):
    """A single sound: pure sine tone vs fixed-seed white noise."""
    body = _noise_frames(ms) if kind == "noise" else _tone_frames(freq, ms)
    fr = _silence_frames(120) + body + _silence_frames(120)
    return _finish(fr, {"kind": kind, "ms": ms})


def pattern(seq="SLS", freq=770, short_ms=120, long_ms=420, gap_ms=220):
    """Short/long beep sequence (e.g. 'SLS'). Ground truth: the S/L string."""
    seq = seq.upper()
    fr = _silence_frames(120)
    for i, ch in enumerate(seq):
        if i:
            fr += _silence_frames(gap_ms)
        fr += _tone_frames(freq, long_ms if ch == "L" else short_ms)
    fr += _silence_frames(120)
    return _finish(fr, {"seq": seq, "short_ms": short_ms, "long_ms": long_ms})


# ---- pinned SPEECH assets (real recorded speech with exact ground truth) ----------------
# Free Spoken Digit Dataset (github.com/Jakobovski/free-spoken-digit-dataset, CC BY-SA 4.0 —
# see assets/audio/speech/ATTRIBUTION.md). Each file is pinned by sha256: a modified or
# missing asset fails LOUDLY instead of silently testing different audio. 8 kHz mono WAV
# (the header carries the rate; transports pass the file verbatim).
SPEECH_DIR = os.path.join(os.path.dirname(__file__), "assets", "audio", "speech")


def speech_asset(file, sha256):
    """A pinned recorded-speech WAV -> (sha, wav_bytes, meta). Integrity-checked against
    the sha embedded in the CASE SPEC (so suite_hash pins the exact recording)."""
    path = os.path.join(SPEECH_DIR, os.path.basename(file))
    with open(path, "rb") as fh:
        data = fh.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != sha256:
        raise RuntimeError(f"speech asset {file} hash mismatch: {actual[:16]} != {sha256[:16]}")
    return actual, data, {"file": os.path.basename(file)}


GENERATORS = {
    "beeps": beeps,
    "two_tones": two_tones,
    "long_short": long_short,
    "noise_or_tone": noise_or_tone,
    "pattern": pattern,
    "speech_asset": speech_asset,
}


def synth(spec):
    """spec = {'gen': name, 'args': {...}} -> (case_key, wav_bytes, meta).
    case_key is the sha256 of the WAV bytes (content-addressed, like imagegen)."""
    return GENERATORS[spec["gen"]](**spec.get("args", {}))


if __name__ == "__main__":
    for name, fn in GENERATORS.items():
        sha, data, gt = fn()
        print(f"{name:14s} {sha[:12]} {len(data):6d}B  gt={gt}")
