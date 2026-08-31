"""Color emoji rendering for Tkinter.

Tk's built-in `create_text` / `tk.Label` can render emoji glyphs but only
with platform-default monochrome fallback (the slot widget's Canvas was
showing emojis in white-on-color, not their real colored form). To get
true colored emoji on the slot grid AND in the picker, we rasterise the
glyph through Pillow using the COLR/CPAL `seguiemj.ttf` font that ships
with Windows, then wrap the result as a `tk.PhotoImage` (via PIL's
`ImageTk`) and cache it.

Notes:
* `seguiemj.ttf` is a bitmap-style color font; Pillow can render it when
  `embedded_color=True` is passed and the build of FreeType in use
  supports color tables (Pillow >= 9.1 does).
* The font's bitmaps are baked at large sizes (109px), so we render at a
  large size and then resize to the requested target with LANCZOS.
* All operations are done lazily and cached by `(emoji, size)`.
* `size` is a raw PIXEL count — the cache does no DPI handling. Callers
  must pass DEVICE pixels (``round(logical * window_scaling)``) whenever the
  result is drawn 1:1 on a raw Tk canvas / `tk.Button` (PhotoImage) or is
  wrapped as ``CTkImage(size=(logical, logical))``: CTk resizes the PIL image
  to ``round(logical * scaling)`` and Pillow skips resampling only when the
  raster already has exactly that size. Passing the logical size instead
  gives a bicubic 1.5x upscale on a 150 % display (the "smudgy" emoji).
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk

    _PIL_OK = True
except Exception:
    _PIL_OK = False


# ---------------------------------------------------------------------------
# Font discovery
# ---------------------------------------------------------------------------

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\seguiemj.ttf",
    "seguiemj.ttf",  # PIL search path
)

# Native bitmap size of seguiemj — render at this then downscale.
_NATIVE_SIZE = 109

# Cache: (emoji, size) -> ImageTk.PhotoImage. Must be kept alive as long
# as Tk references the image, so we hold strong refs here.
_image_cache: Dict[Tuple[str, int], "ImageTk.PhotoImage"] = {}
_pil_cache: Dict[Tuple[str, int], "Image.Image"] = {}
_font: Optional["ImageFont.FreeTypeFont"] = None
_font_failed: bool = False


def is_available() -> bool:
    """Return True if PIL + the color emoji font are usable."""
    if not _PIL_OK:
        return False
    return _get_font() is not None


def _get_font() -> Optional["ImageFont.FreeTypeFont"]:
    global _font, _font_failed
    if _font is not None:
        return _font
    if _font_failed or not _PIL_OK:
        return None
    for path in _FONT_CANDIDATES:
        try:
            _font = ImageFont.truetype(path, _NATIVE_SIZE)
            return _font
        except Exception:
            continue
    _font_failed = True
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_pil(emoji: str, size: int) -> Optional["Image.Image"]:
    """Render emoji to a square RGBA PIL image of the requested size."""
    key = (emoji, size)
    if key in _pil_cache:
        return _pil_cache[key]

    font = _get_font()
    if font is None:
        return None

    try:
        # Render at native size (much higher quality than scaling the font
        # itself, since seguiemj's bitmaps live at this size). Use a generous
        # margin on ALL sides: some color-emoji bitmaps (e.g. 🦉, 🏟️) extend
        # well past the nominal em box, so a tight canvas clipped their tops /
        # bottoms. The bbox crop below trims the slack back off, so the only
        # cost of the extra padding is a few cheap transparent pixels.
        _PAD = 48
        canvas = _NATIVE_SIZE + _PAD * 2
        big = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        draw = ImageDraw.Draw(big)
        try:
            draw.text((_PAD, _PAD), emoji, font=font, embedded_color=True)
        except TypeError:
            # Older Pillow — fall back to monochrome.
            draw.text((_PAD, _PAD), emoji, font=font, fill=(255, 255, 255, 255))

        # Crop tight to non-empty bbox so different emoji are visually centred.
        bbox = big.getbbox()
        if bbox:
            big = big.crop(bbox)

        # Pad to square so the resize keeps aspect intact.
        w, h = big.size
        side = max(w, h)
        sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        sq.paste(big, ((side - w) // 2, (side - h) // 2), big)

        if size != side:
            sq = sq.resize((size, size), Image.LANCZOS)

        _pil_cache[key] = sq
        return sq
    except Exception:
        return None


def get_tk_image(emoji: str, size: int = 32) -> Optional["ImageTk.PhotoImage"]:
    """Return a cached `ImageTk.PhotoImage` for the given emoji and size.

    ``size`` is DEVICE pixels: a PhotoImage is blitted 1:1, so pass
    ``round(logical * window_scaling)`` (see the module docstring).

    Returns None if PIL or the emoji font are unavailable, or if the glyph
    cannot be rasterised. The returned object is owned by the cache; do
    not let the caller's ref drop without keeping it alive elsewhere — Tk
    will GC the underlying photo otherwise.
    """
    if not emoji or not _PIL_OK:
        return None

    key = (emoji, size)
    cached = _image_cache.get(key)
    if cached is not None:
        return cached

    pil_img = _render_pil(emoji, size)
    if pil_img is None:
        return None

    try:
        photo = ImageTk.PhotoImage(pil_img)
    except Exception:
        return None

    _image_cache[key] = photo
    return photo


def get_pil_image(emoji: str, size: int = 32) -> Optional["Image.Image"]:
    """Return the cached PIL image for the emoji, or None.

    Useful when a consumer wants to wrap the rasterised glyph in something
    other than a `tk.PhotoImage` — e.g. CTk's `CTkImage`. Render at the
    DEVICE size (``round(logical * scaling)``) and pass the LOGICAL size to
    ``CTkImage(size=...)``: CTk then finds the raster already at its target
    size and shows it without resampling.
    """
    if not emoji or not _PIL_OK:
        return None
    return _render_pil(emoji, size)


def clear_cache() -> None:
    """Drop all cached PhotoImages (used when shutting down)."""
    _image_cache.clear()
    _pil_cache.clear()
