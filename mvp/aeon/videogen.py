"""Deterministic test-VIDEO generation (PIL frames -> tiny MP4) with MACHINE-KNOWN
ground truth, mirroring imagegen.py for the VIDEO board.

Every generator is a pure function of its args (all RNG seeded from the case spec).
The content-addressed key is the sha256 of the RAW RGB FRAME BYTES (+ dims + fps) —
deterministic across platforms and ffmpeg builds — while the encoded MP4 (the
transport artifact) is cached under assets/video/<sha>.mp4; a differing encoder
build re-encodes the SAME frames, so the suite identity never drifts with ffmpeg.
Ground truth is what the frames show — judge-free and re-derivable.

Encoding needs imageio + imageio-ffmpeg ("pip install 'imageio[ffmpeg]'"); the
suite fails with that exact message when they're absent (available() lets callers
soft-skip instead). Clips stay tiny: <= 64 frames, <= 256x256, a few seconds @ 8 fps.
"""
from __future__ import annotations

import hashlib
import os

from PIL import Image, ImageDraw

from .imagegen import COLORS

FPS = 8
ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "video")

_MISSING = ("video suite requires imageio[ffmpeg] — install with: "
            "pip install \"imageio[ffmpeg]\"")


def available():
    """True when the encoder stack (imageio + the ffmpeg plugin) is importable."""
    try:
        import imageio  # noqa: F401
        import imageio_ffmpeg  # noqa: F401
        return True
    except ImportError:
        return False


def _encode(frames, fps, path):
    try:
        import imageio.v2 as imageio
        import numpy as np
    except ImportError as e:
        raise RuntimeError(_MISSING) from e
    w = imageio.get_writer(path, format="FFMPEG", mode="I", fps=fps,
                           codec="libx264", quality=8, pixelformat="yuv420p",
                           macro_block_size=16)
    try:
        for f in frames:
            w.append_data(np.asarray(f))
    finally:
        w.close()


def _finish(frames, meta, fps=FPS):
    """frames (PIL RGB) -> (sha, mp4_bytes, meta). sha content-addresses the RAW frames;
    the MP4 is cached at assets/video/<sha>.mp4 and re-encoded only on a cache miss."""
    h = hashlib.sha256()
    h.update(f"{len(frames)}x{frames[0].width}x{frames[0].height}@{fps}".encode())
    for f in frames:
        h.update(f.tobytes())
    sha = h.hexdigest()
    os.makedirs(ASSET_DIR, exist_ok=True)
    path = os.path.join(ASSET_DIR, sha + ".mp4")
    if not os.path.exists(path):
        _encode(frames, fps, path)
    with open(path, "rb") as fh:
        data = fh.read()
    return sha, data, meta


def _blank(w, h):
    return Image.new("RGB", (w, h), "white")


def _sh(d, box, shape, color):
    x0, y0, x1, y1 = box
    col = COLORS[color]
    if shape == "circle":
        d.ellipse(box, fill=col)
    elif shape == "triangle":
        d.polygon([(x0, y1), (x1, y1), ((x0 + x1) // 2, y0)], fill=col)
    else:
        d.rectangle(box, fill=col)


# ---- generators: (args) -> (sha, mp4_bytes, ground_truth) -------------------

def moving_square(direction="right", color="red", n_frames=32, w=192, h=192, size=36):
    """One square traverses the frame. Ground truth: the direction."""
    frames = []
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        t = i / max(1, n_frames - 1)
        span_x, span_y = w - size - 16, h - size - 16
        if direction == "right":
            x, y = 8 + t * span_x, (h - size) // 2
        elif direction == "left":
            x, y = 8 + (1 - t) * span_x, (h - size) // 2
        elif direction == "down":
            x, y = (w - size) // 2, 8 + t * span_y
        else:  # up
            x, y = (w - size) // 2, 8 + (1 - t) * span_y
        d.rectangle((x, y, x + size, y + size), fill=COLORS[color])
        frames.append(img)
    return _finish(frames, {"direction": direction, "color": color})


def probe_clip():
    """Tiny transport-probe clip for probe_video: a moving RED square (12 frames, 96px)."""
    return moving_square(direction="right", color="red", n_frames=12, w=96, h=96, size=28)


def flash_sequence(colors=("red", "green", "blue"), flash_frames=8, gap_frames=6,
                   w=160, h=160):
    """A centered square flashes each color once, in order, with blank gaps.
    Ground truth: the flash order."""
    frames = [_blank(w, h) for _ in range(gap_frames)]
    for col in colors:
        for _ in range(flash_frames):
            img = _blank(w, h)
            ImageDraw.Draw(img).rectangle((w // 4, h // 4, 3 * w // 4, 3 * h // 4),
                                          fill=COLORS[col])
            frames.append(img)
        frames += [_blank(w, h) for _ in range(gap_frames)]
    return _finish(frames, {"order": list(colors)})


def dot_crossings(n_cross=4, n_stay=3, seed=5, n_frames=48, w=192, h=192, r=9):
    """Blue dots move horizontally; n_cross of them cross the fixed vertical center
    line (left -> right), n_stay oscillate on the left and never reach it.
    Ground truth: how many crossed."""
    import random
    rng = random.Random(seed)
    line_x = w // 2
    dots = []
    for _ in range(int(n_cross)):                    # start left, end right of the line
        y = rng.randint(r + 6, h - r - 6)
        x0 = rng.randint(r + 4, line_x - 3 * r)
        x1 = rng.randint(line_x + 3 * r, w - r - 4)
        start = rng.randint(0, n_frames // 3)        # staggered departures
        dots.append(("cross", x0, x1, y, start))
    for _ in range(int(n_stay)):                     # oscillate strictly left of the line
        y = rng.randint(r + 6, h - r - 6)
        x0 = rng.randint(r + 4, line_x - 6 * r)
        x1 = min(line_x - 2 * r, x0 + rng.randint(2 * r, 4 * r))
        start = rng.randint(0, n_frames // 3)
        dots.append(("stay", x0, x1, y, start))
    frames = []
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        d.line((line_x, 0, line_x, h), fill=(40, 40, 40), width=3)
        for kind, x0, x1, y, start in dots:
            t = min(1.0, max(0.0, (i - start) / max(1, n_frames - 1 - start)))
            if kind == "stay":                       # there and back again, never across
                t = 2 * t if t <= 0.5 else 2 * (1 - t)
            x = x0 + t * (x1 - x0)
            d.ellipse((x - r, y - r, x + r, y + r), fill=COLORS["blue"])
        frames.append(img)
    return _finish(frames, {"crossed": int(n_cross), "stayed": int(n_stay)})


def blink_square(n_blinks=5, color="purple", on_frames=4, off_frames=4, w=160, h=160):
    """A centered square blinks (appears) n_blinks times. Ground truth: the count."""
    frames = [_blank(w, h) for _ in range(off_frames)]
    for _ in range(int(n_blinks)):
        for _ in range(on_frames):
            img = _blank(w, h)
            ImageDraw.Draw(img).rectangle((w // 4, h // 4, 3 * w // 4, 3 * h // 4),
                                          fill=COLORS[color])
            frames.append(img)
        frames += [_blank(w, h) for _ in range(off_frames)]
    return _finish(frames, {"blinks": int(n_blinks), "color": color})


def appear_shape(base_shape="circle", base_color="blue", appear_shape_="triangle",
                 appear_color="green", appear_at=0.7, n_frames=40, w=192, h=192):
    """A static base shape sits on the left the whole clip; near the END a second shape
    appears on the right. Ground truth: what appeared."""
    frames = []
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        _sh(d, (24, h // 2 - 28, 80, h // 2 + 28), base_shape, base_color)
        if i >= int(appear_at * n_frames):
            _sh(d, (w - 84, h // 2 - 28, w - 28, h // 2 + 28), appear_shape_, appear_color)
        frames.append(img)
    return _finish(frames, {"appeared": f"{appear_color} {appear_shape_}",
                            "base": f"{base_color} {base_shape}"})


def disappear_shape(vanish="blue", vanish_at=0.5, n_frames=40, w=224, h=160):
    """Three shapes (red circle, blue square, green triangle) are present from the start;
    the `vanish`-colored one disappears midway. Ground truth: which vanished."""
    trio = [("circle", "red", 20), ("square", "blue", 92), ("triangle", "green", 164)]
    frames = []
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        for shape, color, x in trio:
            if color == vanish and i >= int(vanish_at * n_frames):
                continue
            _sh(d, (x, h // 2 - 24, x + 44, h // 2 + 24), shape, color)
        frames.append(img)
    shape = next(s for s, c, _ in trio if c == vanish)
    return _finish(frames, {"vanished": f"{vanish} {shape}"})


def two_speeds(fast=("circle", "red"), slow=("square", "blue"), slow_frac=0.45,
               n_frames=40, w=224, h=160, size=36):
    """Two shapes traverse left->right; the fast one covers the full width, the slow one
    only `slow_frac` of it in the same time. Ground truth: which moved faster."""
    frames = []
    span = w - size - 16
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        t = i / max(1, n_frames - 1)
        fx = 8 + t * span
        sx = 8 + t * slow_frac * span
        _sh(d, (fx, 22, fx + size, 22 + size), fast[0], fast[1])
        _sh(d, (sx, h - 22 - size, sx + size, h - 22), slow[0], slow[1])
        frames.append(img)
    return _finish(frames, {"faster": f"{fast[1]} {fast[0]}", "slower": f"{slow[1]} {slow[0]}"})


def grow_shrink(mode="grow", shape="circle", color="orange", n_frames=32, w=176, h=176):
    """One centered shape steadily grows or shrinks. Ground truth: the mode."""
    frames = []
    for i in range(n_frames):
        img = _blank(w, h)
        d = ImageDraw.Draw(img)
        t = i / max(1, n_frames - 1)
        r = 12 + (t if mode == "grow" else 1 - t) * 60
        _sh(d, (w // 2 - r, h // 2 - r, w // 2 + r, h // 2 + r), shape, color)
        frames.append(img)
    return _finish(frames, {"mode": mode, "shape": shape, "color": color})


GENERATORS = {
    "moving_square": moving_square,
    "flash_sequence": flash_sequence,
    "dot_crossings": dot_crossings,
    "blink_square": blink_square,
    "appear_shape": appear_shape,
    "disappear_shape": disappear_shape,
    "two_speeds": two_speeds,
    "grow_shrink": grow_shrink,
}


def generate(spec):
    """spec = {'gen': name, 'args': {...}} -> (sha, mp4_bytes, ground_truth).
    sha content-addresses the raw frames (see _finish)."""
    return GENERATORS[spec["gen"]](**spec.get("args", {}))


if __name__ == "__main__":
    for name, fn in GENERATORS.items():
        sha, data, gt = fn()
        print(f"{name:16s} {sha[:12]} {len(data):7d}B  gt={gt}")
