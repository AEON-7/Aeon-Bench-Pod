"""aeon/sharecard.py — server-rendered SOCIAL CARDS (OG images) for shared benchmarks.

Social scrapers (X, iMessage, Discord, Slack, WhatsApp) read OG meta tags and fetch a static
image — they never execute JS — so each shareable benchmark gets a Pillow-rendered 1200×630 PNG
in the site's NIGHT CITY grammar: near-black ground, chamfered cyan frame, scanlines + horizon
bloom, the model's name + owner avatar, and the two heroes: the podium-metal GLOBAL rank plate
("#3 GLOBAL") and the OVERALL AEON SCORE numeral. A supporting chip line carries the component
scores (intelligence / agentic / performance), the MAX CTX the benchmark was served at, and the
trust tier; peak concurrent tok/s rides the hardware footer. Cards are cached in-memory (the
board moves slowly) and NEVER 500 — any failure degrades to a plain branded card.

Fonts: DejaVu Sans Mono when present (shipped in the prod image via fonts-dejavu-core), else
platform monos (Consolas / Menlo), else Pillow's built-in scalable default.
"""
from __future__ import annotations

import hashlib
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
# GOD MODE accent: the podium gold, because a god result IS the top tier — it replaces
# cyan throughout that variant so the card reads as a different class at a glance.
GOLD = (232, 194, 104)

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


def _chip(d, x, y, label, value, color, pad=16, fsize=30, check=False, h=78):
    fl, fv = _font(15), _font(fsize)
    ck = fsize if check else 0                    # vector checkmark (font-safe: ✓ is tofu in many monos)
    wl, wv = _tw(d, label, fl), _tw(d, value, fv) + ck
    w = max(wl, wv) + pad * 2
    d.polygon(_chamfer(x, y, x + w, y + h, 10), fill=(255, 255, 255, 7), outline=color + (140,))
    d.text((x + pad, y + 12), label, font=fl, fill=FAINT)
    vy = y + h - fsize - 14                       # value baseline scales with the chip height
    if check:
        s = fsize / 30.0
        d.line([(x + pad, vy + 18 * s), (x + pad + 8 * s, vy + 27 * s), (x + pad + 22 * s, vy + 6 * s)],
               fill=color, width=max(3, round(4 * s)))
    d.text((x + pad + ck, vy), value, font=fv, fill=color)
    return x + w + 14


def _fmt_tps(n):
    """Throughput, without lying about precision: a 9.96 tok/s serve read as a flat "10" in the
    footer. Sub-10 keeps a decimal; above that the integer is the honest resolution."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    return f"{n:.1f}" if n < 10 else f"{n:,.0f}"


def _fmt_ctx(n):
    """65536 -> '64K' (the boards' context grammar); sub-1K windows stay literal."""
    return f"{round(n / 1024)}K" if n >= 1024 else str(n)


def _initials_disc(seed: str, size: int) -> Image.Image:
    """A deterministic initials disc for creators whose avatar we cannot rasterise.

    HF generates an SVG identicon for accounts with no uploaded picture, and Pillow cannot render
    SVG — so those creators all collapsed to the same branded Æ, which reads as "unknown model"
    on someone's own result. This at least gives each creator a stable, distinct mark: their
    initial on a hue derived from the name, the same idea HF's own identicon expresses."""
    h = int(hashlib.sha256((seed or "?").encode("utf-8")).hexdigest()[:8], 16)
    hue = h % 360
    # cheap HSV->RGB at fixed S/V, kept muted so it sits inside the card's palette
    c, x = 96, int(96 * (1 - abs(((hue / 60.0) % 2) - 1)))
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(hue // 60) % 6]
    im = Image.new("RGB", (size, size), (r + 26, g + 26, b + 34))
    d = ImageDraw.Draw(im)
    ch = (seed or "?").strip()[:1].upper() or "?"
    f = _font(int(size * .52))
    d.text(((size - _tw(d, ch, f)) // 2, int(size * .16)), ch, font=f, fill=(240, 240, 250))
    return im


def _avatar(url: str | None, size: int = 128, seed: str = "") -> Image.Image:
    """Circular owner avatar (the HF account image, same as the boards); any failure ->
    an initials disc, or the branded Æ when we have no name either (never blocks the card).
    Accepts a remote URL or a local site path like /static/aeon-avatar.png (own-org models
    resolve to a local asset). SVG cannot be rasterised by Pillow, so those take the disc."""
    im = None
    if url and url.lower().endswith(".svg"):
        im = None                       # HF identicon / local svg: unrasterisable, use the disc
    elif url and url.startswith("http"):
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
        if seed:
            im = _initials_disc(seed, size)
        else:
            im = Image.new("RGB", (size, size), PANEL)
            d = ImageDraw.Draw(im)
            f = _font(int(size * .5))
            d.text(((size - _tw(d, "Æ", f)) // 2, int(size * .18)), "Æ", font=f, fill=CYAN)
    mask = Image.new("L", (size * 2, size * 2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 2, size * 2), fill=255)
    im.putalpha(mask.resize((size, size), Image.LANCZOS))
    return im


def render_model_card(info: dict) -> bytes:
    """1200×630 PNG for one benchmark row: {model, org, name, rank, aeon, provisional,
    components, ctx_len, composite, peak_tps, trust, hardware, suite, avatar_url}.
    Heroes: the GLOBAL rank plate + the OVERALL AEON SCORE numeral. Supporting chip line:
    component scores · MAX CTX · trust. Old payloads (no aeon/ctx) degrade to the composite
    headline with the same geometry — nothing faked, nothing crammed.

    `info["god"]` renders the GOD MODE variant: identical geometry (a god result should look like
    a first-class benchmark card, not a different product), with the GOD SCORE as the headline,
    the god components on the chip line, and a GOD MODE badge. Before this, sharing a god row
    produced the generic fallback card, because a god-only run is not on the global board."""
    god = bool(info.get("god"))
    accent = GOLD if god else CYAN
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
    d.text((56, 46), "▲ AEON//BENCH", font=_font(30), fill=accent)
    tag = (info.get("suite") or "AEON BENCH").upper()
    ftag = _font(16)
    d.text((W - 58 - _tw(d, tag, ftag), 54), tag, font=ftag, fill=FAINT)
    if god:
        # the badge: unmistakable, and placed where the eye lands after the brand
        fb = _font(22)
        btxt = "GOD MODE"
        bw = _tw(d, btxt, fb) + 34
        d.polygon(_chamfer(300, 42, 300 + bw, 84, 10), fill=GOLD + (26,), outline=GOLD + (210,))
        d.text((317, 50), btxt, font=fb, fill=GOLD)

    # HERO 1 — global-leaderboard placement: watermark numerals + the "#3 GLOBAL" plate,
    # podium-metal-hued for the top three
    rank = info.get("rank")
    if rank:
        metal = GOLD if god else RANK_METALS.get(rank, (110, 110, 146))
        rs = f"{rank:02d}"
        frk = _font(300)
        d.text((W - 92 - _tw(d, rs, frk), 96), rs, font=frk, fill=metal + (34,))
        flbl = _font(15)
        lab = "GOD MODE BENCH" if god else "GLOBAL LEADERBOARD"
        d.text((W - 70 - _tw(d, lab, flbl), 300), lab, font=flbl, fill=FAINT)
        fr2 = _font(42)
        plate = f"#{rank} GOD" if god else f"#{rank} GLOBAL"
        cw = _tw(d, plate, fr2) + 44
        d.polygon(_chamfer(W - 70 - cw, 324, W - 70, 400, 12), fill=(255, 255, 255, 10),
                  outline=metal + (180,))
        d.text((W - 70 - cw + 22, 338), plate, font=fr2, fill=metal)

    # owner avatar + identity
    lay.alpha_composite(
        _avatar(info.get("avatar_url"), seed=(info.get("org") or info.get("name") or "")),
        (60, 150))
    d.ellipse((56, 146, 60 + 128 + 4, 150 + 128 + 4), outline=accent + (130,), width=2)
    org = (info.get("org") or "").rstrip("/")
    if org:
        d.text((216, 168), org + " /", font=_font(26), fill=MUTED)
    name = info.get("name") or info.get("model") or "model"
    fn = _font(54)
    NAME_W = 926                                                        # 214 -> the frame's edge
    while _tw(d, name, fn) > NAME_W and fn.size > 30:                   # shrink-to-fit
        fn = _font(fn.size - 4)
    if _tw(d, name, fn) > NAME_W:
        # Shrinking bottoms out at 30pt and community names get long ("keys-latest-GLM-5.2-
        # Quantrio-INT4-INT8-Mixed-Abliterated-DFlash"), so past that point the text simply ran
        # off the card. Truncate instead: a clipped name reads as a bug, an ellipsis reads as a
        # long name.
        while name and _tw(d, name + "…", fn) > NAME_W:
            name = name[:-1]
        name += "…"
    d.text((214, 202), name, font=fn, fill=TEXT)

    # HERO 2 — the OVERALL AEON SCORE numeral (the headline number; old payloads without an
    # aeon score keep the slot with the composite, labelled honestly)
    aeon = info.get("aeon")
    headline = aeon if aeon is not None else info.get("composite")
    if headline is not None:
        fl = _font(20)
        x = 218
        head_lab = ("GOD SCORE" if god else "AEON SCORE") if aeon is not None else "COMPOSITE"
        d.text((x, 296), head_lab, font=fl, fill=FAINT)
        x += _tw(d, head_lab, fl) + 16
        if aeon is not None:
            sub = "BEYOND FRONTIER" if god else "OVERALL"
            d.text((x, 296), sub, font=fl, fill=accent)
            x += _tw(d, sub, fl) + 16
            if info.get("provisional"):
                d.text((x, 296), "· PROVISIONAL", font=fl, fill=MUTED)
        d.text((212, 322), f"{headline:.1f}", font=_font(108), fill=TEXT)

    # supporting chip line: component scores + the served context + trust
    x, cy = 60, 466
    comps = info.get("components") or {}
    fields = ((("sentinels", "SENTINELS"), ("agentic", "AGENTIC")) if god
              else (("intelligence", "INTELLIGENCE"), ("agentic", "AGENTIC"),
                    ("performance", "PERFORMANCE")))
    for key, lab in fields:
        v = comps.get(key)
        if v is not None:
            x = _chip(d, x, cy, lab, f"{v:.1f}", RANK_METALS[1], fsize=24, h=64, pad=14)
        elif key == "agentic" and info.get("agentic_not_counted"):
            # an untested component is stated, never silently dropped and never shown as 0
            x = _chip(d, x, cy, lab, "UNTESTED", MUTED, fsize=24, h=64, pad=14)
    # PEAK THROUGHPUT rides the chip line, not just the footer: it is a headline measurement of
    # the model+rig, and on a god card (where a component may be untested) it is often the most
    # concrete number on the image.
    tps = info.get("peak_tps")
    if tps:
        x = _chip(d, x, cy, "PEAK TOK/S", _fmt_tps(tps), CYAN, fsize=24, h=64, pad=14)
    ctx = info.get("ctx_len")
    if ctx:
        x = _chip(d, x, cy, "MAX CTX", _fmt_ctx(ctx), CYAN, fsize=24, h=64, pad=14)
    if (info.get("trust") or "") == "attested":
        x = _chip(d, x, cy, "TRUST", "ATTESTED", GOOD, check=True, fsize=24, h=64, pad=14)
    if god:
        x = _chip(d, x, cy, "BENCH", "GOD MODE", GOLD, fsize=24, h=64, pad=14)

    # footer: hardware + peak concurrent throughput, site
    foot = []
    hw = info.get("hardware")
    if hw:
        foot.append(str(hw).upper())
    tps = info.get("peak_tps")
    if tps:
        foot.append(f"PEAK {_fmt_tps(tps)} TOK/S CONCURRENT")
    if foot:
        d.text((58, H - 76), " · ".join(foot), font=_font(18), fill=MUTED)
    site = "aeon-bench.com"
    fs = _font(20)
    d.text((W - 58 - _tw(d, site, fs), H - 78), site, font=fs, fill=accent)

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
