"""Album art pipeline.

The phone cannot fetch art directly from i.scdn.co: Gingerbread's TLS stack
predates SNI and modern cipher suites, so every HTTPS request to Spotify's CDN
fails. The laptop downloads the image, shrinks it, pre-renders a blurred
backdrop, and serves both over plain HTTP on the LAN.

Blurring here rather than in CSS is not an optimization, it is the only
option: Android 2.3 has no backdrop-filter and no filter: blur().
"""

import colorsys
import hashlib
import io
import os

import requests
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(PROJECT_ROOT, "var", "cache")
COVER_PX = 300     # phone is 320 wide; 300 leaves a margin and halves the bytes
BACKDROP_PX = 96   # upscaled by the browser, so it can be tiny
TIMEOUT = 4        # the phone is already showing the new title by now

# The 1-bit variant is dithered at exactly the size it is displayed at, and the
# eink theme sets those same pixel dimensions in CSS. Any scaling in the browser
# turns the dither pattern into grey mush, so these two numbers must agree:
# static/themes/eink.css, #cover-dither.
#
# Sized for landscape (480x320), which is the orientation this runs in. Portrait
# shows the same file at the same pixel size rather than a second variant —
# one dither per track, never rescaled. Changing this needs the cached
# *-1bit.png files deleted, or old covers keep being served at the old size.
#
# 176 rather than 200: the theme was redrawn for a 1 metre viewing distance,
# and the right column needed the width back for 34px type.
DITHER_PX = 176

# What white becomes in the dimmed variant. The dark theme puts the cover on a
# black field, where a #ffffff dither is the brightest thing in the room at
# night. Inverting it is not the answer — a negative of album art stops reading
# as the artwork — so the pattern is left exactly as it is and only the lit
# pixels are turned down. Slightly warm, to sit with the paper tone the theme
# uses for ink.
DIM_INK = (138, 138, 132)


def _key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def prepare(url):
    """Download and process one album cover. Returns a dict of paths + palette.

    Results are cached on disk by URL hash, so repeats of the same track cost
    nothing and the phone's HTTP cache keeps working (filenames are stable).
    """
    if not url:
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    key = _key(url)
    cover_name = "%s.jpg" % key
    backdrop_name = "%s-bg.jpg" % key
    # PNG, not JPEG. JPEG's DCT smears a 1-bit dither into grey fringing —
    # the pattern is the image here, and it has to survive byte for byte.
    dither_name = "%s-1bit.png" % key
    dim_name = "%s-dim.png" % key
    cover_path = os.path.join(CACHE_DIR, cover_name)
    backdrop_path = os.path.join(CACHE_DIR, backdrop_name)
    dither_path = os.path.join(CACHE_DIR, dither_name)
    dim_path = os.path.join(CACHE_DIR, dim_name)
    palette_path = os.path.join(CACHE_DIR, "%s.pal" % key)

    # Both dither paths are part of the test on purpose: entries cached before
    # a variant existed must fall through and be rebuilt, not served forever
    # without it.
    if (os.path.exists(cover_path) and os.path.exists(dither_path)
            and os.path.exists(dim_path) and os.path.exists(palette_path)):
        with open(palette_path) as fh:
            colors = fh.read().strip().split(",")
        return _result(cover_name, backdrop_name, dither_name, dim_name, colors)

    try:
        raw = requests.get(url, timeout=TIMEOUT).content
        src = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        print("[art] fetch failed: %s" % exc)
        return None

    cover = src.resize((COVER_PX, COVER_PX), Image.LANCZOS)
    # Quality 78 keeps a 300px cover around 20-25 KB. The wire is not the
    # constraint on Wi-Fi; the 512 MB phone decoding the JPEG is.
    cover.save(cover_path, "JPEG", quality=78, optimize=True)

    backdrop = src.resize((BACKDROP_PX, BACKDROP_PX), Image.LANCZOS)
    backdrop = backdrop.filter(ImageFilter.GaussianBlur(radius=8))
    backdrop = ImageEnhance.Brightness(backdrop).enhance(0.45)
    backdrop = ImageEnhance.Color(backdrop).enhance(1.25)
    backdrop.save(backdrop_path, "JPEG", quality=60, optimize=True)

    # One dither pass, two files: the bright one and the dimmed one. Both are
    # produced for every cover regardless of which theme is loaded — the
    # backend has never known that themes exist and does not start now.
    mono = _dither(src)
    mono.save(dither_path, "PNG", optimize=True)
    _dim(mono).save(dim_path, "PNG", optimize=True)

    colors = _palette(src)
    with open(palette_path, "w") as fh:
        fh.write(",".join(colors))

    return _result(cover_name, backdrop_name, dither_name, dim_name, colors)


def _dither(img):
    """1-bit Floyd-Steinberg cover for the e-ink theme.

    Adapted from process_cover_for_eink() in the deleted inky_album_display
    pipeline (see 0e3e557), which quantized to the Inky Impression's 7-color
    palette. Same idea, two colors.

    Order matters: resize first, dither second. Dithering at source resolution
    and then resampling averages neighbouring black and white pixels into grey,
    which is the exact mush the pattern exists to avoid. Pillow's convert("1")
    applies Floyd-Steinberg by default.
    """
    grey = img.convert("L").resize((DITHER_PX, DITHER_PX), Image.LANCZOS)
    # Covers that are mostly midtones dither into flat noise. Stretching the
    # range first gives the error diffusion something to work with.
    grey = ImageOps.autocontrast(grey, cutoff=1)

    # convert("1") does the Floyd-Steinberg pass; convert("L") then widens the
    # container to 8-bit without touching a pixel — every value is still 0 or
    # 255. Costs a few KB over the LAN and sidesteps having to find out on the
    # device whether Gingerbread decodes 1-bit-depth PNGs.
    return grey.convert("1").convert("L")


def _dim(mono):
    """The same dithered image with its white turned down to DIM_INK.

    Takes the already-dithered picture rather than re-dithering: the pattern
    must be pixel-for-pixel identical to the bright variant, because it is the
    same photograph, only quieter. Nothing is inverted and no pixel moves.

    A two-entry palette PNG, so the exact tone survives — an 8-bit greyscale
    file could only carry a neutral grey, and this one is deliberately warm.
    Two colours also make it smaller than the bright variant, not larger.
    """
    out = Image.new("P", mono.size)
    palette = [0, 0, 0] + list(DIM_INK) + [0] * (768 - 6)
    out.putpalette(palette)
    # mono carries only 0 and 255; index 1 is the lit pixel.
    out.putdata([1 if value else 0 for value in mono.getdata()])
    return out


def _result(cover_name, backdrop_name, dither_name, dim_name, colors):
    # Every variant is offered on every payload. The backend does not know
    # which theme is loaded — that is the whole point — so it produces both
    # covers and lets the CSS pick. Themes stay a frontend-only concern.
    return {
        "cover": "/art/%s" % cover_name,
        "cover_dither": "/art/%s" % dither_name,
        "cover_dither_dim": "/art/%s" % dim_name,
        "backdrop": "/art/%s" % backdrop_name,
        "accent": colors[0],
        "bg": colors[1],
        "text": colors[2],
    }


def _palette(img):
    """Pick an accent, a background, and a readable text color.

    Median-cut quantization over a thumbnail. Cheap, deterministic, and good
    enough — a k-means pass costs 50x more for a difference nobody sees at
    320x480.
    """
    small = img.resize((64, 64), Image.LANCZOS).quantize(colors=8, method=Image.MEDIANCUT)
    pal = small.getpalette()
    counts = sorted(small.getcolors(), reverse=True)

    swatches = []
    for count, idx in counts:
        r, g, b = pal[idx * 3:idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        swatches.append({"rgb": (r, g, b), "count": count, "l": l, "s": s})

    # Accent is the pop color, not the dominant one — a cover that is 80%
    # teal with an orange stripe should give an orange progress bar. Rather
    # than weighting saturation against area (which just reintroduces the
    # dominant color under a different name), apply a hard area floor and
    # then take the most saturated survivor.
    total = float(sum(s["count"] for s in swatches))
    candidates = [
        s for s in swatches
        if 0.18 < s["l"] < 0.85 and s["s"] > 0.25 and s["count"] / total > 0.04
    ]
    if candidates:
        accent = max(candidates, key=lambda s: s["s"])
    else:
        # Nothing cleared the saturation floor — a near-monochrome cover.
        # Falling back to the most common swatch returns white here, which
        # makes the accent invisible on exactly the covers where it matters.
        # Take the most saturated mid-lightness swatch instead, still behind
        # the area floor so a few stray pixels cannot become the accent.
        mid = [s for s in swatches
               if 0.18 < s["l"] < 0.85 and s["count"] / total > 0.04]
        accent = max(mid or swatches, key=lambda s: s["s"])

    # Background comes from the dominant swatch instead, darkened hard. Using
    # the accent here made every screen look like a single flat wash.
    dominant = max(swatches, key=lambda s: s["count"])
    dr, dg, db = dominant["rgb"]
    h, l, s = colorsys.rgb_to_hls(dr / 255.0, dg / 255.0, db / 255.0)
    bg_lightness = min(l * 0.28, 0.14)
    br, bg_, bb = colorsys.hls_to_rgb(h, bg_lightness, min(s, 0.6))
    background = (int(br * 255), int(bg_ * 255), int(bb * 255))

    # Contrast against the background we just computed, not against the
    # dominant swatch it came from. Those are different numbers: the
    # background is deliberately crushed to l<=0.14, so keying off the
    # dominant lightness put near-black text on a near-black panel for any
    # predominantly light cover.
    text = (255, 255, 255) if bg_lightness < 0.65 else (12, 12, 12)

    return [_hex(accent["rgb"]), _hex(background), _hex(text)]


def _hex(rgb):
    return "#%02x%02x%02x" % rgb
