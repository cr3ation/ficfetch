"""Deterministic cover-image generation for downloaded EPUBs.

AO3 works ship without cover art. When a user's chosen style is not ``none`` and
an EPUB has no embedded cover, the download worker renders one from the work's
metadata (title, author, fandom, rating, word count). Everything is offline and
deterministic: the palette is seeded from the title, so the same work always
produces the same cover and re-downloads stay stable.

Four styles, all with a subtle film grain that kills gradient banding:
  - ``hybrid``     mesh colour-cloud + editorial serif typography (the default)
  - ``art``        generative mesh with a legibility panel
  - ``editorial``  flat deep tone + serif, à la Standard Ebooks
  - ``bold``       vivid linear gradient + heavy sans

Rendering is CPU-only and single-threaded by contract: the sole caller in the
download worker runs on the event loop, and the preview route is ``async`` too,
so the font cache is never touched from two threads at once.
"""
from __future__ import annotations

import colorsys
import hashlib
import io
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .models import Work

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_SERIF = str(FONT_DIR / "EBGaramond-VF.ttf")
_SANS = str(FONT_DIR / "Inter-VF.ttf")

# 'none' is handled by callers (skip generation); these are the drawable styles.
# 'paper'/'framed' are light, high-contrast styles for black-and-white e-readers.
ALLOWED_STYLES: tuple[str, ...] = ("hybrid", "art", "editorial", "bold", "paper", "framed")
DEFAULT_STYLE = "hybrid"
STYLE_CHOICES: tuple[str, ...] = ("hybrid", "art", "editorial", "bold", "paper", "framed", "none")
# Light styles skip the film grain — it speckles a light background and muddies e-ink.
_LIGHT_STYLES = frozenset({"paper", "framed"})

# Classic e-book cover ratio (5:8).
W, H = 1600, 2560

# Fixed sample rendered by the /preferences preview gallery.
SAMPLE_WORK = Work(
    work_id="0",
    title="Magic In His Eyes",
    authors=["MissYuki1990"],
    fandoms=["Harry Potter", "Teen Wolf"],
    rating="Explicit",
    word_count=120000,
)


# ---------------------------------------------------------------- fonts

_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def _font(path: str, size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """Load a named weight of a variable font, cached and never mutated after."""
    key = (path, size, weight)
    fnt = _font_cache.get(key)
    if fnt is None:
        fnt = ImageFont.truetype(path, size)
        try:
            fnt.set_variation_by_name(weight)
        except Exception:
            pass  # fall back to the default instance rather than fail a download
        _font_cache[key] = fnt
    return fnt


def _serif(size: int, weight: str = "Medium") -> ImageFont.FreeTypeFont:
    return _font(_SERIF, size, weight)


def _sans(size: int, weight: str = "Medium") -> ImageFont.FreeTypeFont:
    return _font(_SANS, size, weight)


# ---------------------------------------------------------------- colour

def _hsl(h: float, s: float, lig: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, lig, s)
    return (int(r * 255), int(g * 255), int(b * 255))


def _seed(title: str) -> int:
    return int(hashlib.sha256(title.encode("utf-8")).hexdigest()[:8], 16)


def _palette(title: str) -> dict:
    s = _seed(title)
    h = s % 360
    return {
        "h": h,
        "bg1": _hsl(h, 0.45, 0.16),
        "bg2": _hsl(h + 22, 0.55, 0.08),
        "flat": _hsl(h, 0.30, 0.13),
        "grad_top": _hsl(h, 0.55, 0.30),
        "grad_bot": _hsl(h + 35, 0.65, 0.13),
        "accent": _hsl(h + 18, 0.68, 0.62),
        "accent2": _hsl(h + 160, 0.60, 0.55),
        "line": _hsl(h, 0.35, 0.38),
        "text": _hsl(h, 0.16, 0.95),
        "muted": _hsl(h, 0.28, 0.72),
        # Light, e-ink-friendly scheme: near-white paper, near-black ink, a
        # saturated medium-dark accent that keeps contrast even in grayscale.
        "paper": _hsl(h, 0.10, 0.965),
        "ink": _hsl(h, 0.28, 0.11),
        "accent_dark": _hsl(h, 0.62, 0.40),
        "muted_ink": _hsl(h, 0.10, 0.45),
    }


def _lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


# ---------------------------------------------------------------- text

def _wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _fit_title(draw, text, role_font, max_w, max_h, smax, smin, max_lines):
    """Shrink the title until it fits `max_lines` and `max_h`. role_font(size)."""
    for size in range(smax, smin - 1, -6):
        fnt = role_font(size)
        lines = _wrap(draw, text, fnt, max_w)
        asc, desc = fnt.getmetrics()
        lh = (asc + desc) * 1.12
        if len(lines) <= max_lines and lh * len(lines) <= max_h:
            return fnt, lines, lh
    fnt = role_font(smin)
    lines = _wrap(draw, text, fnt, max_w)[:max_lines]
    asc, desc = fnt.getmetrics()
    return fnt, lines, (asc + desc) * 1.12


def _draw_lines(draw, cx, cy, lines, fnt, lh, fill, shadow=None):
    y = cy - lh * len(lines) / 2 + lh / 2
    for ln in lines:
        if shadow:
            draw.text((cx + 3, y + 4), ln, font=fnt, fill=shadow, anchor="mm")
        draw.text((cx, y), ln, font=fnt, fill=fill, anchor="mm")
        y += lh


def _tracked(draw, cx, y, text, fnt, fill, tracking):
    """Draw centred small-caps with letter-spacing (Pillow has no tracking)."""
    text = text.upper()
    widths = [draw.textlength(ch, font=fnt) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=fnt, fill=fill, anchor="lm")
        x += w + tracking


def _fandom_line(work: Work) -> str:
    return " · ".join(work.fandoms[:2]) if work.fandoms else ""


def _author(work: Work) -> str:
    return work.authors[0] if work.authors else "Anonymous"


def _meta_line(work: Work) -> str:
    parts = []
    if work.rating:
        parts.append(work.rating)
    if work.word_count:
        parts.append(f"{work.word_count:,} WORDS".replace(",", " "))
    return "   ·   ".join(parts)


# ---------------------------------------------------------------- backgrounds

def _vgrad(c1, c2):
    g = Image.new("RGB", (1, H))
    for y in range(H):
        g.putpixel((0, y), _lerp(c1, c2, y / (H - 1)))
    return g.resize((W, H))


def _mesh(title: str, pal: dict) -> Image.Image:
    """Soft colour-cloud background. Rendered at half-res — the big Gaussian
    blur is the cost, and downscaling it 4x is invisible once upscaled."""
    sw, sh = W // 2, H // 2
    rng = random.Random(_seed(title) ^ 0x9E3779B9)
    base = Image.new("RGB", (sw, sh), _hsl(pal["h"], 0.5, 0.07))
    layer = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    cols = [pal["accent"], _hsl(pal["h"] + 30, 0.55, 0.32),
            _hsl(pal["h"] - 25, 0.5, 0.28), pal["bg1"]]
    for i in range(5):
        cx = rng.randint(-80, sw + 80)
        cy = rng.randint(-80, sh + 80)
        r = rng.randint(300, 600)
        ld.ellipse([cx - r, cy - r, cx + r, cy + r], fill=cols[i % len(cols)] + (120,))
    layer = layer.filter(ImageFilter.GaussianBlur(130))
    base = Image.alpha_composite(base.convert("RGBA"), layer)
    base = Image.alpha_composite(base, Image.new("RGBA", (sw, sh), (6, 8, 14, 120)))
    return base.convert("RGB").resize((W, H), Image.LANCZOS)


def _grain(img, alpha=0.05):
    """Subtle film grain so gradients don't band on real screens."""
    noise = Image.effect_noise(img.size, 26).convert("RGB")
    return Image.blend(img, ImageChops.overlay(img, noise), alpha)


# ---------------------------------------------------------------- styles

def _render_editorial(work: Work, pal: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), pal["flat"])
    d = ImageDraw.Draw(img)
    d.rectangle([64, 64, W - 64, H - 64], outline=pal["line"], width=3)
    cx = W // 2
    if _fandom_line(work):
        _tracked(d, cx, 440, _fandom_line(work), _sans(40), pal["muted"], 10)
    d.line([cx - 90, 520, cx + 90, 520], fill=pal["line"], width=3)
    fnt, lines, lh = _fit_title(d, work.title, _serif, W - 460, 620, 168, 74, 4)
    _draw_lines(d, cx, int(H * 0.44), lines, fnt, lh, pal["text"])
    d.line([cx - 90, int(H * 0.73), cx + 90, int(H * 0.73)], fill=pal["line"], width=3)
    _tracked(d, cx, int(H * 0.78), _author(work), _sans(48), pal["muted"], 8)
    if _meta_line(work):
        _tracked(d, cx, H - 150, _meta_line(work), _sans(30), pal["line"], 6)
    return img


def _render_bold(work: Work, pal: dict) -> Image.Image:
    img = _vgrad(pal["grad_top"], pal["grad_bot"])
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 700, -400, W + 500, 800], fill=pal["accent"] + (90,))
    glow = glow.filter(ImageFilter.GaussianBlur(260))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)
    cx = W // 2
    white = (245, 245, 250)
    if _fandom_line(work):
        _tracked(d, cx, 360, _fandom_line(work), _sans(40, "Bold"), (255, 255, 255), 10)
    fnt, lines, lh = _fit_title(d, work.title, lambda s: _sans(s, "Bold"), W - 300, 900, 190, 92, 4)
    _draw_lines(d, cx, int(H * 0.46), lines, fnt, lh, white, shadow=(0, 0, 0))
    d.line([cx - 70, int(H * 0.80), cx + 70, int(H * 0.80)], fill=(255, 255, 255), width=4)
    _tracked(d, cx, int(H * 0.85), _author(work), _sans(46, "Bold"), (235, 235, 240), 8)
    return img


def _render_art(work: Work, pal: dict) -> Image.Image:
    base = _mesh(work.title, pal)
    # scattered translucent rings for texture
    rd = ImageDraw.Draw(base, "RGBA")
    rng = random.Random(_seed(work.title))
    for _ in range(3):
        rx, ry = rng.randint(0, W), rng.randint(0, H)
        rr = rng.randint(220, 520)
        rd.ellipse([rx - rr, ry - rr, rx + rr, ry + rr], outline=pal["accent"] + (45,), width=3)
    # legibility panel
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(
        [150, int(H * 0.34), W - 150, int(H * 0.66)], radius=36, fill=(8, 10, 16, 165))
    base = Image.alpha_composite(base.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(base)
    cx = W // 2
    if _fandom_line(work):
        _tracked(d, cx, int(H * 0.30), _fandom_line(work), _sans(38), pal["muted"], 10)
    fnt, lines, lh = _fit_title(d, work.title, _serif, W - 420, 380, 150, 70, 3)
    _draw_lines(d, cx, int(H * 0.47), lines, fnt, lh, (245, 245, 250))
    _tracked(d, cx, int(H * 0.61), _author(work), _sans(42), pal["muted"], 8)
    return base


def _render_hybrid(work: Work, pal: dict) -> Image.Image:
    img = _mesh(work.title, pal)
    # soft dark glow behind the title band for legibility over the mesh
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [W * 0.12, H * 0.30, W * 0.88, H * 0.60], fill=(4, 6, 10, 140))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img)
    d.rectangle([70, 70, W - 70, H - 70], outline=pal["line"], width=2)
    cx = W // 2
    if _fandom_line(work):
        _tracked(d, cx, 450, _fandom_line(work), _sans(38), pal["muted"], 10)
    d.line([cx - 80, 525, cx + 80, 525], fill=pal["line"], width=2)
    fnt, lines, lh = _fit_title(d, work.title, _serif, W - 420, 560, 158, 72, 4)
    _draw_lines(d, cx, int(H * 0.43), lines, fnt, lh, (245, 246, 250))
    # minimalist diamond accent between title and author
    ay, ac = int(H * 0.63), pal["accent"]
    d.line([cx - 170, ay, cx - 40, ay], fill=ac, width=2)
    d.line([cx + 40, ay, cx + 170, ay], fill=ac, width=2)
    d.polygon([(cx, ay - 16), (cx + 16, ay), (cx, ay + 16), (cx - 16, ay)], outline=ac, width=2)
    _tracked(d, cx, int(H * 0.72), _author(work), _sans(46), pal["muted"], 8)
    if _meta_line(work):
        _tracked(d, cx, H - 155, _meta_line(work), _sans(30), pal["line"], 6)
    return img


# ---------------------------------------------------------------- light styles

# Seeded ornament corners for the 'framed' style. (x, y) is a frame corner and
# (qx, qy) the inward direction; everything is drawn as thin accent lines so it
# stays crisp — a thin grey line — on a black-and-white e-reader.
_QANG = {(1, 1): 0, (-1, 1): 90, (-1, -1): 180, (1, -1): 270}


def _c_arcs(d, x, y, qx, qy, col, w):
    a = _QANG[(qx, qy)]
    for rr in (150, 102, 63):
        d.arc([x - rr, y - rr, x + rr, y + rr], a, a + 90, fill=col, width=w)
    d.ellipse([x - 7, y - 7, x + 7, y + 7], fill=col)


def _c_bracket(d, x, y, qx, qy, col, w):
    for g, ln in ((28, 150), (50, 118)):
        d.line([(x + qx * g, y + qy * g), (x + qx * g + qx * ln, y + qy * g)], fill=col, width=w)
        d.line([(x + qx * g, y + qy * g), (x + qx * g, y + qy * g + qy * ln)], fill=col, width=w)
    d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=col)


def _c_steps(d, x, y, qx, qy, col, w):
    for i in range(3):
        o, ln = 24 + i * 26, 150 - i * 36
        d.line([(x + qx * o, y + qy * o), (x + qx * o + qx * ln, y + qy * o)], fill=col, width=w)
        d.line([(x + qx * o, y + qy * o), (x + qx * o, y + qy * o + qy * ln)], fill=col, width=w)


def _c_rays(d, x, y, qx, qy, col, w):
    px, py = x + qx * 18, y + qy * 18
    base = _QANG[(qx, qy)]
    for a in range(0, 91, 15):
        ang = math.radians(base + a)
        d.line([(px, py), (px + math.cos(ang) * 130, py + math.sin(ang) * 130)], fill=col, width=w)
    d.ellipse([px - 6, py - 6, px + 6, py + 6], fill=col)


_CORNERS = (_c_arcs, _c_bracket, _c_steps, _c_rays)


def _top_orn(d, cx, ty, kind, col, w):
    if kind == 0:
        d.polygon([(cx, ty - 22), (cx + 22, ty), (cx, ty + 22), (cx - 22, ty)], outline=col, width=w)
    elif kind == 1:
        for dx in (-46, 0, 46):
            d.ellipse([cx + dx - 6, ty - 6, cx + dx + 6, ty + 6], fill=col)
    elif kind == 2:
        d.ellipse([cx - 20, ty - 20, cx + 20, ty + 20], outline=col, width=w)
        d.ellipse([cx - 5, ty - 5, cx + 5, ty + 5], fill=col)
    else:
        for a in range(0, 360, 45):
            ang = math.radians(a)
            d.line([(cx, ty), (cx + math.cos(ang) * 24, ty + math.sin(ang) * 24)], fill=col, width=w)


def _paper_base(work: Work, pal: dict, ornate: bool) -> Image.Image:
    """Light, high-contrast cover: near-black serif on near-white paper. `ornate`
    adds a seeded decorative frame; otherwise a plain thin double rule."""
    img = Image.new("RGB", (W, H), pal["paper"])
    d = ImageDraw.Draw(img)
    acc, ink, mut = pal["accent_dark"], pal["ink"], pal["muted_ink"]
    cx = W // 2
    if ornate:
        rng = random.Random(_seed(work.title))
        corner = rng.choice(_CORNERS)
        topk = rng.randrange(4)
        double = rng.random() < 0.7
        m, m2 = 96, 116
        d.rectangle([m, m, W - m, H - m], outline=acc, width=4)
        if double:
            d.rectangle([m2, m2, W - m2, H - m2], outline=acc, width=1)
        ci = m2 if double else m
        for qx, qy, ox, oy in ((1, 1, ci, ci), (-1, 1, W - ci, ci),
                               (1, -1, ci, H - ci), (-1, -1, W - ci, H - ci)):
            corner(d, ox, oy, qx, qy, acc, 3)
        _top_orn(d, cx, 300, topk, acc, 3)
    else:
        d.rectangle([90, 90, W - 90, H - 90], outline=acc, width=4)
        d.rectangle([112, 112, W - 112, H - 112], outline=acc, width=1)
    if _fandom_line(work):
        _tracked(d, cx, 384, _fandom_line(work), _sans(46, "SemiBold"), acc, 10)
    d.line([cx - 120, 500, cx + 120, 500], fill=acc, width=4)
    fnt, lines, lh = _fit_title(d, work.title, lambda s: _serif(s, "SemiBold"), W - 380, 720, 210, 112, 3)
    _draw_lines(d, cx, int(H * 0.44), lines, fnt, lh, ink)
    d.line([cx - 120, int(H * 0.66), cx + 120, int(H * 0.66)], fill=acc, width=4)
    _tracked(d, cx, int(H * 0.715), _author(work), _sans(62, "SemiBold"), ink, 8)
    if _meta_line(work):
        _tracked(d, cx, H - 200, _meta_line(work), _sans(34), mut, 6)
    return img


def _render_paper(work: Work, pal: dict) -> Image.Image:
    return _paper_base(work, pal, ornate=False)


def _render_framed(work: Work, pal: dict) -> Image.Image:
    return _paper_base(work, pal, ornate=True)


_RENDERERS = {
    "hybrid": _render_hybrid,
    "art": _render_art,
    "editorial": _render_editorial,
    "bold": _render_bold,
    "paper": _render_paper,
    "framed": _render_framed,
}


# ---------------------------------------------------------------- public API

def generate_cover(work: Work, style: str = DEFAULT_STYLE) -> bytes:
    """Render a JPEG cover for `work` in `style`. Raises on unknown style."""
    renderer = _RENDERERS.get(style)
    if renderer is None:
        raise ValueError(f"Unknown cover style: {style!r}")
    img = renderer(work, _palette(work.title))
    if style not in _LIGHT_STYLES:
        img = _grain(img)  # film grain only for the textured dark styles
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


_preview_cache: dict[str, bytes] = {}


def generate_preview(style: str) -> bytes:
    """Cover for the fixed SAMPLE_WORK, memoised per style for the gallery."""
    if style not in _preview_cache:
        _preview_cache[style] = generate_cover(SAMPLE_WORK, style)
    return _preview_cache[style]
