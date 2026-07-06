"""aeon/sharecard.py — server-rendered SOCIAL CARDS (OG images) for shared benchmarks.

Social scrapers (X, iMessage, Discord, Slack, WhatsApp) read OG meta tags and fetch a static
image — they never execute JS — so each shareable benchmark gets a Pillow-rendered 1200×630 PNG
in the site's NIGHT CITY grammar: near-black ground, chamfered cyan frame, scanlines + horizon
bloom, podium-metal rank, the model's name + owner avatar, composite score and peak concurrent
tok/s. Cards are cached in-memory (the board moves slowly) and NEVER 500 — any failure degrades
to a plain branded card.

Fonts: DejaVu Sans Mono when present (shipped in the prod image via fonts-dejavu-core), else
platform monos (Consolas / Menlo), else Pillow's built-in scalable default.
"""
from __future__ import annotations

import io
import os
import threading
import time
import urllib.request

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 630
BG = (7, 7, 13)
PANEL = (13, 13, 21)
CYAN = (0, 240, 255)
CYAN_DIM = (0, 240, 255, 46)
TEXT = (227, 227, 238)
MUTED = (148, 148, 184)
FAINT = (110, 110, 146)
GOOD = (61, 220, 133)
MAGENTA = (255, 46, 151)
RANK_METALS = {1: (232, 194, 104), 2: (170, 180, 200), 3: (201, 138, 94)}

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]


def _font(size: int, bold: bool = True):
    order = _FONT_CANDIDATES if bold else _FONT_CANDIDATES[1:] + _FONT_CANDIDATES[:1]
    for p in order:
        if os.path.exists(p) and (("Bold" in p or p.endswith("b.ttf")) == bold or True):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)          # Pillow >=10.1 scalable fallback
    except Exception:                                  # pragma: no cover
        return ImageFont.load_default()


def _tw(draw, txt, font):
    return draw.textbbox((0, 0), txt, font=font)[2]


def _chamfer(x0, y0, x1, y1, cut):
    """Corner-cut octagon (the site's machined-plate silhouette)."""
    return [(x0 + cut, y0), (x1 - cut, y0), (x1, y0 + cut), (x1, y1 - cut),
            (x1 - cut, y1), (x0 + cut, y1), (x0, y1 - cut), (x0, y0 + cut)]


def _ground(d: ImageDraw.ImageDraw):
    """Scanlines + perspective grid floor + horizon bloom — the calibrated-signal backdrop."""
    for y in range(0, H, 4):                                   # scanlines
        d.line([(0, y), (W, y)], fill=(255, 255, 255, 4))
    horizon = 470
    for i in range(9):                                         # receding floor lines
        y = horizon + int((H - horizon) * (i / 8) ** 1.7)
        d.line([(0, y), (W, y)], fill=(0, 240, 255, 14))
    for k in range(-8, 9):                                     # vanishing verticals
        d.line([(W // 2 + k * 40, H), (W // 2 + k * 300, horizon)], fill=(0, 240, 255, 8))
    d.line([(0, horizon), (W, horizon)], fill=(0, 240, 255, 40))


def _frame(d: ImageDraw.ImageDraw):
    pts = _chamfer(18, 18, W - 18, H - 18, 30)
    d.polygon(pts, outline=(0, 240, 255, 150))
    d.polygon(_chamfer(21, 21, W - 21, H - 21, 30), outline=(0, 240, 255, 45))
    for (x, y, dx, dy) in ((44, 44, 1, 1), (W - 44, 44, -1, 1),
                           (44, H - 44, 1, -1), (W - 44, H - 44, -1, -1)):
        d.line([(x, y), (x + 26 * dx, y)], fill=(0, 240, 255, 110), width=2)   # corner reticles
        d.line([(x, y), (x, y + 26 * dy)], fill=(0, 240, 255, 110), width=2)


def _chip(d, x, y, label, value, color, pad=16, fsize=30, check=False):
    fl, fv = _font(15), _font(fsize)
    ck = 30 if check else 0                       # vector checkmark (font-safe: ✓ is tofu in many monos)
    wl, wv = _tw(d, label, fl), _tw(d, value, fv) + ck
    w = max(wl, wv) + pad * 2
    h = 78
    d.polygon(_chamfer(x, y, x + w, y + h, 10), fill=(255, 255, 255, 7), outline=color + (140,))
    d.text((x + pad, y + 12), label, font=fl, fill=FAINT)
    if check:
        d.line([(x + pad, y + 52), (x + pad + 8, y + 61), (x + pad + 22, y + 40)], fill=color, width=4)
    d.text((x + pad + ck, y + 34), value, font=fv, fill=color)
    return x + w + 14


def _avatar(url: str | None, size: int = 128) -> Image.Image:
    """Circular owner avatar (the HF account image, same as the boards); any failure ->
    branded Æ disc (never blocks the card). Accepts a remote URL or a local site path
    like /static/aeon-avatar.png (own-org models resolve to a local asset)."""
    im = None
    if url and url.startswith("http"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aeon-bench/og"})
            with urllib.request.urlopen(req, timeout=4) as r:
                im = Image.open(io.BytesIO(r.read())).convert("RGB").resize((size, size), Image.LANCZOS)
        except Exception:
            im = None
    elif url and url.startswith("/") and not url.endswith(".svg"):
        try:
            p = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "web",
                                              url.lstrip("/")))
            im = Image.open(p).convert("RGB").resize((size, size), Image.LANCZOS)
        except Exception:
            im = None
    if im is None:
        im = Image.new("RGB", (size, size), PANEL)
        d = ImageDraw.Draw(im)
        f = _font(int(size * .5))
        d.text(((size - _tw(d, "Æ", f)) // 2, int(size * .18)), "Æ", font=f, fill=CYAN)
    mask = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 2, size * 2), fill=255)
    im.putalpha(mask.resize((size, size), Image.LANCZOS))
    return im


def render_model_card(info: dict) -> bytes:
    """1200×630 PNG for one benchmark row: {model, org, name, rank, composite, peak_tps,
    trust, hardware, suite, avatar_url}."""
    base = Image.new("RGB", (W, H), BG)
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    _ground(d)

    # horizon bloom (soft magenta/cyan) — blurred separately so lines above stay crisp
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dg = ImageDraw.Draw(glow)
    dg.ellipse((W // 2 - 430, 430, W // 2 + 430, 560), fill=(0, 240, 255, 34))
    dg.ellipse((W // 2 - 260, 452, W // 2 + 260, 540), fill=(255, 46, 151, 22))
    glow = glow.filter(ImageFilter.GaussianBlur(46))
    lay = Image.alpha_composite(glow, lay)
    d = ImageDraw.Draw(lay)
    _frame(d)

    # header: brand + suite readout
    d.text((56, 46), "▲ AEON//BENCH", font=_font(30), fill=CYAN)
    tag = (info.get("suite") or "AEON BENCH").upper()
    ftag = _font(16)
    d.text((W - 58 - _tw(d, tag, ftag), 54), tag, font=ftag, fill=FAINT)

    # rank watermark + chip (podium metals)
    rank = info.get("rank")
    if rank:
        metal = RANK_METALS.get(rank, (110, 110, 146))
        rs = f"{rank:02d}"
        frk = _font(300)
        d.text((W - 92 - _tw(d, rs, frk), 96), rs, font=frk, fill=metal + (34,))
        fr2 = _font(22)
        chip = f"RANK {rs}"
        cw = _tw(d, chip, fr2) + 30
        d.polygon(_chamfer(W - 70 - cw, 108, W - 70, 152, 8), fill=(255, 255, 255, 10),
                  outline=metal + (170,))
        d.text((W - 55 - cw + 15 - 15 + 15, 118), chip, font=fr2, fill=metal)

    # owner avatar + identity
    lay.alpha_composite(_avatar(info.get("avatar_url")), (60, 150))
    d.ellipse((56, 146, 60 + 128 + 4, 150 + 128 + 4), outline=(0, 240, 255, 130), width=2)
    org = (info.get("org") or "").rstrip("/")
    if org:
        d.text((216, 168), org + " /", font=_font(26), fill=MUTED)
    name = info.get("name") or info.get("model") or "model"
    fn = _font(54)
    while _tw(d, name, fn) > 900 and fn.size > 30:                      # shrink-to-fit
        fn = _font(fn.size - 4)
    d.text((214, 202), name, font=fn, fill=TEXT)

    # metric chips
    x = 216
    comp = info.get("composite")
    if comp is not None:
        x = _chip(d, x, 330, "COMPOSITE", f"{comp:.1f}", RANK_METALS[1])
    tps = info.get("peak_tps")
    if tps:
        x = _chip(d, x, 330, "PEAK CONCURRENT", f"{tps:.0f} TOK/S", CYAN)
    if (info.get("trust") or "") == "attested":
        x = _chip(d, x, 330, "TRUST", "ATTESTED", GOOD, check=True)

    # footer: hardware + site
    hw = info.get("hardware")
    if hw:
        d.text((58, H - 76), str(hw).upper(), font=_font(18), fill=MUTED)
    site = "aeon-bench.com"
    fs = _font(20)
    d.text((W - 58 - _tw(d, site, fs), H - 78), site, font=fs, fill=CYAN)

    out = Image.alpha_composite(base.convert("RGBA"), lay).convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def render_fallback_card(title: str = "AEON BENCH") -> bytes:
    """Plain branded card — the degrade path when a model key can't be resolved."""
    return render_model_card({"name": title, "org": "", "suite": "open · attested · local LLM benchmarks"})


# ---- tiny in-memory cache (cards change only when the board does) ----------------------------
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, bytes]] = {}
_TTL = 900


def cached(key: str, builder) -> bytes:
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    png = builder()
    with _LOCK:
        _CACHE[key] = (now, png)
        if len(_CACHE) > 200:
            _CACHE.pop(next(iter(_CACHE)))
    return png
