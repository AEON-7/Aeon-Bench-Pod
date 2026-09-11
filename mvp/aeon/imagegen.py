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
import random

from PIL import Image, ImageDraw, ImageFont

ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets", "vision")
os.makedirs(ASSET_DIR, exist_ok=True)

COLORS = {
    "red": (214, 48, 49), "blue": (45, 82, 217), "green": (39, 158, 63),
    "yellow": (243, 196, 47), "black": (17, 17, 17), "white": (255, 255, 255),
    "purple": (142, 68, 173), "orange": (230, 126, 34), "gray": (125, 125, 125),
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


# ---- v2 (hard-tier) generators. All randomness comes from random.Random(seed) with the
# seed pinned in the CASE SPEC (derived from the case id), so every image is a pure function
# of its args — regenerable anywhere; random module sequences are stable across platforms.

def cluttered_shapes(target_shape="triangle", target_color="red", n_targets=8,
                     distractors=(("square", "red", 6), ("triangle", "blue", 7),
                                  ("circle", "green", 5)),
                     seed=1, size=420):
    """A cluttered field: n_targets TARGET shapes + distractor shapes at seeded random
    positions, overlap allowed (occlusion via z-order). Distractors draw FIRST so targets
    always sit on top — occlusion can hide a distractor but never a target — and targets
    keep a minimum center distance from each other, so the true countable target count is
    exactly n_targets. Ground truth: that count."""
    rng = random.Random(seed)
    img = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(img)
    dist = []
    for shape, color, n in distractors:
        for _ in range(int(n)):
            r = rng.randint(14, 26)
            dist.append((shape, color, rng.randint(r + 4, size - r - 4),
                         rng.randint(r + 4, size - r - 4), r))
    rng.shuffle(dist)
    for shape, color, x, y, r in dist:
        _shape(d, (x - r, y - r, x + r, y + r), shape, COLORS[color])
    placed = []
    for _ in range(int(n_targets)):
        r = rng.randint(15, 24)
        x = y = None
        for _try in range(400):                      # deterministic rejection sampling
            x = rng.randint(r + 4, size - r - 4)
            y = rng.randint(r + 4, size - r - 4)
            if all((x - px) ** 2 + (y - py) ** 2 >= (r + pr) ** 2 for px, py, pr in placed):
                break
        placed.append((x, y, r))
        _shape(d, (x - r, y - r, x + r, y + r), target_shape, COLORS[target_color])
    sha, data = _finish(img)
    return sha, data, {"target": f"{target_color} {target_shape}", "count": int(n_targets),
                       "n_distractors": sum(int(n) for _, _, n in distractors)}


def rotated_text(text="KRJ4-VX7Q", angle=15, fontsize=20, noise=0.05, seed=7):
    """Small text under rotation + seeded speckle noise. Ground truth: the text."""
    w, h = 340, 150
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((30, h // 2 - fontsize // 2 - 2), text, fill=(17, 17, 17), font=_font(fontsize))
    img = img.rotate(angle, expand=False, fillcolor="white", resample=Image.BICUBIC)
    d = ImageDraw.Draw(img)
    rng = random.Random(seed)
    for _ in range(int(w * h * noise / 4)):
        g = rng.randint(90, 205)
        d.point((rng.randint(0, w - 1), rng.randint(0, h - 1)), fill=(g, g, g))
    sha, data = _finish(img)
    return sha, data, {"text": text, "angle": angle, "fontsize": fontsize}


def size_relation(sizes=(28, 44, 20, 36), colors=("red", "blue", "green", "yellow"),
                  extreme="largest", seed=3):
    """A row of GRAY circles (radii = sizes) in seeded shuffled order with seeded vertical
    jitter; a small colored square sits directly LEFT of each circle (colors[i] belongs to
    circle sizes[i]). Two-step question: find the largest/smallest circle, then read the
    color of the square beside it. Ground truth: colors[argmax/argmin(sizes)]."""
    rng = random.Random(seed)
    n = len(sizes)
    img = Image.new("RGB", (135 * n + 40, 220), "white")
    d = ImageDraw.Draw(img)
    order = list(range(n))
    rng.shuffle(order)                               # visual order is seeded; truth is per-index
    for pos, i in enumerate(order):
        r = sizes[i]
        cx, cy = 110 + pos * 135, 110 + rng.randint(-18, 18)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(88, 88, 88))
        sq = 12
        sx = cx - r - 14 - 2 * sq                    # directly left of this circle
        d.rectangle((sx, cy - sq, sx + 2 * sq, cy + sq), fill=COLORS[colors[i]])
    pick = max if extreme == "largest" else min
    idx = sizes.index(pick(sizes))
    sha, data = _finish(img)
    return sha, data, {"extreme": extreme, "answer": colors[idx],
                       "sizes": list(sizes), "colors": list(colors)}


def series_chart(labels=("Q1", "Q2", "Q3", "Q4"),
                 series=(("alpha", (3, 5, 2, 6)), ("beta", (4, 2, 7, 3))),
                 decoy="avg 4.5", y_max=8):
    """Grouped bar chart with a LEGEND, gridlines + tick numbers, and a DECOY text
    annotation that names a value which answers nothing. Ground truth: every
    (series, label) value."""
    w, h, x0, y0 = 560, 340, 70, 270
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    scale = 200 / y_max
    for t in range(0, y_max + 1, 2):                 # gridlines + y ticks
        y = y0 - int(t * scale)
        d.line((x0, y, w - 30, y), fill=(215, 215, 215))
        d.text((x0 - 34, y - 9), str(t), fill=(90, 90, 90), font=_font(18))
    palette = ["blue", "orange", "green"]
    group_w, bar_w = (w - x0 - 50) // len(labels), 32
    for gi, lab in enumerate(labels):
        gx = x0 + gi * group_w + 18
        for si, (name, vals) in enumerate(series):
            bx = gx + si * (bar_w + 8)
            bh = int(vals[gi] * scale)
            d.rectangle((bx, y0 - bh, bx + bar_w, y0), fill=COLORS[palette[si % len(palette)]])
        d.text((gx + (len(series) * (bar_w + 8)) // 2 - 12, y0 + 8), lab,
               fill=(17, 17, 17), font=_font(20))
    lx, ly = w - 170, 16                             # legend
    for si, (name, _vals) in enumerate(series):
        d.rectangle((lx, ly + si * 26, lx + 18, ly + si * 26 + 18),
                    fill=COLORS[palette[si % len(palette)]])
        d.text((lx + 26, ly + si * 26 - 1), name, fill=(17, 17, 17), font=_font(19))
    if decoy:                                        # decoy annotation — answers nothing
        d.text((x0 + 8, 14), "note: " + decoy, fill=(140, 60, 60), font=_font(18))
    sha, data = _finish(img)
    return sha, data, {"value_of": {name: dict(zip(labels, vals)) for name, vals in series},
                       "decoy": decoy}


def color_patches(n=6, base=(208, 64, 48), delta=(0, 26, 0), odd_index=4):
    """A numbered row of near-identical color patches; patch odd_index (0-based) differs
    from the rest by `delta` per RGB channel. Ground truth: the 1-based odd patch number."""
    pw, gap, top = 64, 18, 24
    img = Image.new("RGB", ((pw + gap) * n + gap, 130), "white")
    d = ImageDraw.Draw(img)
    for i in range(n):
        col = tuple(min(255, max(0, c + (dd if i == odd_index else 0)))
                    for c, dd in zip(base, delta))
        x = gap + i * (pw + gap)
        d.rectangle((x, top, x + pw, top + pw), fill=col)
        d.text((x + pw // 2 - 6, top + pw + 6), str(i + 1), fill=(17, 17, 17), font=_font(22))
    sha, data = _finish(img)
    return sha, data, {"odd": odd_index + 1, "n": n, "base": list(base), "delta": list(delta)}


# fixed icon palette so every icon type is drawn one way, one color — precise but crowded
_ICON_COLOR = {"star": "yellow", "heart": "red", "circle": "blue", "square": "green",
               "triangle": "purple", "diamond": "orange"}


def _icon(d, cx, cy, r, icon):
    col = COLORS[_ICON_COLOR[icon]]
    if icon == "star":
        import math
        pts = []
        for k in range(10):
            rr = r if k % 2 == 0 else r * 0.45
            a = math.pi * (k / 5.0) - math.pi / 2
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=col)
    elif icon == "heart":
        rr = r * 0.55
        d.ellipse((cx - r, cy - r * 0.7, cx - r + 2 * rr, cy - r * 0.7 + 2 * rr), fill=col)
        d.ellipse((cx + r - 2 * rr, cy - r * 0.7, cx + r, cy - r * 0.7 + 2 * rr), fill=col)
        d.polygon([(cx - r * 0.92, cy), (cx + r * 0.92, cy), (cx, cy + r)], fill=col)
    elif icon == "diamond":
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=col)
    else:
        _shape(d, (cx - r, cy - r, cx + r, cy + r), icon, col)


def icon_grid(rows=5, cols=5, icons=("star", "heart", "circle", "square"), seed=11):
    """rows x cols grid of icons drawn by a seeded RNG from `icons`. Ground truth carries
    the full grid plus derived counts (per icon, rows/cols containing combinations), so
    compositional questions ('how many rows contain both a star and a heart?') stay
    machine-known. Answers are pinned in the case; the self-test re-derives them here."""
    rng = random.Random(seed)
    grid = [[icons[rng.randrange(len(icons))] for _ in range(cols)] for _ in range(rows)]
    cell = 66
    img = Image.new("RGB", (cols * cell + 20, rows * cell + 20), "white")
    d = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            _icon(d, 10 + c * cell + cell // 2, 10 + r * cell + cell // 2, 20, grid[r][c])
    counts = {ic: sum(row.count(ic) for row in grid) for ic in icons}
    gt = {
        "grid": grid, "counts": counts,
        "rows_with_star_and_heart": sum(1 for row in grid if "star" in row and "heart" in row),
        "cols_with_heart": sum(1 for c in range(cols) if any(grid[r][c] == "heart" for r in range(rows))),
    }
    sha, data = _finish(img)
    return sha, data, gt


def shape_sequence(pattern=("circle", "square", "triangle"), repeats=3, color="purple"):
    """The pattern repeated `repeats` times with the LAST shape replaced by a '?' box.
    Ground truth: the shape under the '?' (next-in-pattern)."""
    full = list(pattern) * repeats
    shown, answer = full[:-1], full[-1]
    cell = 78
    img = Image.new("RGB", ((len(shown) + 1) * cell + 20, cell + 30), "white")
    d = ImageDraw.Draw(img)
    for i, sh in enumerate(shown):
        x = 10 + i * cell + 12
        _shape(d, (x, 22, x + cell - 24, 22 + cell - 24), sh, COLORS[color])
    qx = 10 + len(shown) * cell + 12
    d.rectangle((qx, 22, qx + cell - 24, 22 + cell - 24), outline=(120, 120, 120), width=2)
    d.text((qx + 14, 26), "?", fill=(120, 120, 120), font=_font(40))
    sha, data = _finish(img)
    return sha, data, {"pattern": list(pattern), "next": answer}


def color_sequence(pattern=("red", "blue", "blue"), repeats=3, shape="circle"):
    """Same as shape_sequence but the COLOR cycles (all shapes identical).
    Ground truth: the color under the '?'."""
    full = list(pattern) * repeats
    shown, answer = full[:-1], full[-1]
    cell = 78
    img = Image.new("RGB", ((len(shown) + 1) * cell + 20, cell + 30), "white")
    d = ImageDraw.Draw(img)
    for i, col in enumerate(shown):
        x = 10 + i * cell + 12
        _shape(d, (x, 22, x + cell - 24, 22 + cell - 24), shape, COLORS[col])
    qx = 10 + len(shown) * cell + 12
    d.rectangle((qx, 22, qx + cell - 24, 22 + cell - 24), outline=(120, 120, 120), width=2)
    d.text((qx + 14, 26), "?", fill=(120, 120, 120), font=_font(40))
    sha, data = _finish(img)
    return sha, data, {"pattern": list(pattern), "next": answer}


def scene(house_color="red", car_color="blue", seed=2):
    """A simple scene with pinned semantics: a house (wall + roof + door), a tree
    (trunk + canopy) and a car (body + cabin + wheels), left-to-right in that order.
    Ground truth: the objects, their colors and their x-order — free-form descriptions
    are validated by keyword checkers against these."""
    rng = random.Random(seed)
    w, h = 520, 280
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    gy = 236 + rng.randint(-4, 4)                    # ground line
    d.line((0, gy, w, gy), fill=(150, 150, 150), width=3)
    hx = 30 + rng.randint(0, 10)                     # house
    d.rectangle((hx, gy - 100, hx + 110, gy), fill=COLORS[house_color])
    d.polygon([(hx - 12, gy - 100), (hx + 122, gy - 100), (hx + 55, gy - 150)],
              fill=(90, 60, 40))
    d.rectangle((hx + 42, gy - 46, hx + 68, gy), fill=(60, 40, 25))
    tx = 235 + rng.randint(-6, 6)                    # tree
    d.rectangle((tx - 9, gy - 70, tx + 9, gy), fill=(110, 72, 40))
    d.ellipse((tx - 44, gy - 150, tx + 44, gy - 56), fill=COLORS["green"])
    cx = 360 + rng.randint(0, 10)                    # car
    d.rectangle((cx, gy - 46, cx + 130, gy - 14), fill=COLORS[car_color])
    d.rectangle((cx + 28, gy - 72, cx + 96, gy - 46), fill=COLORS[car_color])
    for wx in (cx + 28, cx + 100):
        d.ellipse((wx - 14, gy - 26, wx + 14, gy + 2), fill=(30, 30, 30))
    sha, data = _finish(img)
    return sha, data, {"objects": ["house", "tree", "car"],
                       "house_color": house_color, "car_color": car_color,
                       "order_left_to_right": ["house", "tree", "car"]}


GENERATORS = {
    "solid_square": solid_square, "token": token, "paragraph": paragraph,
    "shapes": shapes, "positioned": positioned, "two_shapes": two_shapes,
    "bar_chart": bar_chart, "fine_detail": fine_detail,
    # v2 hard-tier generators
    "cluttered_shapes": cluttered_shapes, "rotated_text": rotated_text,
    "size_relation": size_relation, "series_chart": series_chart,
    "color_patches": color_patches, "icon_grid": icon_grid,
    "shape_sequence": shape_sequence, "color_sequence": color_sequence,
    "scene": scene,
}


def generate(spec):
    """spec = {'gen': name, 'args': {...}} -> (sha, png_bytes, ground_truth)."""
    return GENERATORS[spec["gen"]](**spec.get("args", {}))


if __name__ == "__main__":
    # self-test: generate each, print ground truth + sha
    for name, fn in GENERATORS.items():
        sha, data, gt = fn()
        print(f"{name:14s} {sha[:12]} {len(data):6d}B  gt={gt}")
