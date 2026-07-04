"""Deterministic test-image generation (Pillow) with MACHINE-KNOWN ground truth.

Per docs/multimodal/04-mvp-vision-plan.md: every generator is a pure function of
its args; the generated PNG bytes are the pinned artifact (sha256), cached under
assets/vision/<sha>.png. The generator spec is provenance only, never the
re-derivation authority. Ground truth is what the generator drew — no human
annotation, so vision Tier-0 cases are judge-free and re-derivable (DESIGN §6c.4).
"""
from __future__ import annotations

import hashlib
import io
import os

from PIL import Image, ImageDraw, ImageFont

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "vision")
os.makedirs(ASSET_DIR, exist_ok=True)

COLORS = {
    "red": (214, 48, 49), "blue": (45, 82, 217), "green": (39, 158, 63),
    "yellow": (243, 196, 47), "black": (17, 17, 17), "white": (255, 255, 255),
    "purple": (142, 68, 173), "orange": (230, 126, 34),
}

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}
_FONT_PATHS = ["arial.ttf", "C:/Windows/Fonts/arial.ttf", "DejaVuSans.ttf",
               os.path.join(ASSET_DIR, "DejaVuSans.ttf")]


def _font(size):
    if size not in _FONT_CACHE:
        f = None
        for p in _FONT_PATHS:
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
        _FONT_CACHE[size] = f or ImageFont.load_default()
    return _FONT_CACHE[size]


def _finish(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    path = os.path.join(ASSET_DIR, sha + ".png")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(data)
    return sha, data


def _shape(d, box, shape, color):
    x0, y0, x1, y1 = box
    if shape == "circle":
        d.ellipse(box, fill=color)
    elif shape == "square":
        d.rectangle(box, fill=color)
    elif shape == "triangle":
        d.polygon([(x0, y1), (x1, y1), ((x0 + x1) // 2, y0)], fill=color)
    else:
        d.rectangle(box, fill=color)


# ---- generators: (args) -> (sha, png_bytes, ground_truth) -----------------

def solid_square(color="red", size=96):
    img = Image.new("RGB", (size, size), COLORS[color])
    sha, data = _finish(img)
    return sha, data, {"color": color}


def token(text="AEON", w=380, h=130, fontsize=64):
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((24, (h - fontsize) // 2 - 6), text, fill=(17, 17, 17), font=_font(fontsize))
    sha, data = _finish(img)
    return sha, data, {"text": text}


def paragraph(lines=("the quick brown", "fox jumps over"), fontsize=40):
    pad, lh = 20, fontsize + 12
    img = Image.new("RGB", (560, pad * 2 + lh * len(lines)), "white")
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((pad, pad + i * lh), ln, fill=(17, 17, 17), font=_font(fontsize))
    sha, data = _finish(img)
    return sha, data, {"text": "\n".join(lines)}


def shapes(n=3, color="blue", shape="circle"):
    """N non-overlapping shapes on a fixed 4-col grid."""
    cell, cols = 90, 4
    rows = (n + cols - 1) // cols
    img = Image.new("RGB", (cols * cell + 20, rows * cell + 20), "white")
    d = ImageDraw.Draw(img)
    for i in range(n):
        r, c = divmod(i, cols)
        x, y = 10 + c * cell + 14, 10 + r * cell + 14
        _shape(d, (x, y, x + cell - 28, y + cell - 28), shape, COLORS[color])
    sha, data = _finish(img)
    return sha, data, {"count": n, "color": color, "shape": shape}


def positioned(shape="square", quadrant="top-left", color="green"):
    img = Image.new("RGB", (320, 240), "white")
    d = ImageDraw.Draw(img)
    cx = 40 if "left" in quadrant else 200
    cy = 40 if "top" in quadrant else 140
    _shape(d, (cx, cy, cx + 80, cy + 80), shape, COLORS[color])
    sha, data = _finish(img)
    return sha, data, {"position": quadrant, "shape": shape, "color": color}


def two_shapes(left_color="red", left_shape="circle",
               right_color="blue", right_shape="square"):
    """Left object vs right object — ground truth is the left/right relation."""
    img = Image.new("RGB", (360, 200), "white")
    d = ImageDraw.Draw(img)
    _shape(d, (40, 60, 120, 140), left_shape, COLORS[left_color])
    _shape(d, (240, 60, 320, 140), right_shape, COLORS[right_color])
    sha, data = _finish(img)
    return sha, data, {"left": f"{left_color} {left_shape}", "right": f"{right_color} {right_shape}"}


def bar_chart(labels=("A", "B", "C"), values=(3, 7, 5)):
    w, h, base, bw = 360, 240, 200, 70
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    maxv = max(values)
    for i, (lab, v) in enumerate(zip(labels, values)):
        x = 40 + i * (bw + 30)
        bh = int((v / maxv) * 150)
        d.rectangle((x, base - bh, x + bw, base), fill=COLORS["blue"])
        d.text((x + bw // 2 - 6, base + 8), lab, fill=(17, 17, 17), font=_font(28))
    sha, data = _finish(img)
    mx = labels[values.index(maxv)]
    return sha, data, {"max_label": mx, "value_of": dict(zip(labels, values))}


def fine_detail(big="HELLO", tiny="k7", fontbig=110, fonttiny=22):
    img = Image.new("RGB", (480, 300), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 90), big, fill=(210, 210, 210), font=_font(fontbig))
    d.text((430, 274), tiny, fill=(17, 17, 17), font=_font(fonttiny))  # tiny, bottom-right
    sha, data = _finish(img)
    return sha, data, {"tiny": tiny}


GENERATORS = {
    "solid_square": solid_square, "token": token, "paragraph": paragraph,
    "shapes": shapes, "positioned": positioned, "two_shapes": two_shapes,
    "bar_chart": bar_chart, "fine_detail": fine_detail,
}


def generate(spec):
    """spec = {'gen': name, 'args': {...}} -> (sha, png_bytes, ground_truth)."""
    return GENERATORS[spec["gen"]](**spec.get("args", {}))


if __name__ == "__main__":
    # self-test: generate each, print ground truth + sha
    for name, fn in GENERATORS.items():
        sha, data, gt = fn()
        print(f"{name:14s} {sha[:12]} {len(data):6d}B  gt={gt}")
