"""Single-canvas slot widget.

Replaces the previous per-slot stack of 5 CTk widgets (slot_frame +
main_button + bottom_frame + stop_button + menu_button + progress_bar)
with ONE tk.Canvas that paints itself. With ~60 slots visible per tab
this drops Tk's geometry-recompute work on every <Configure> from ~300
widgets down to ~60 — the dominant cause of resize / tab-switch lag.

The widget exposes thin proxy objects that mimic the original widgets'
APIs (`.configure()`, `.set()`, `.pack()`, `.pack_forget()`, `.lift()`,
`.lower()`) so the rest of `gui.py` keeps working without code changes.
Each proxy routes its calls to the appropriate setter on the underlying
`SlotWidget`.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from typing import Any, Callable, Optional

from ._shared import RESIZE_STATE as _SHARED_RESIZE_STATE
from .constants import COLORS, UI

# ---------------------------------------------------------------------------
# DPI helpers
# ---------------------------------------------------------------------------
#
# A tk.Canvas is a RAW Tk widget: its coordinates, PhotoImages and negative
# font sizes are all DEVICE pixels, while every number handed to us by gui.py
# (heights, font sizes, image sizes) is LOGICAL px that CTk would multiply by
# the window's DPI factor. The widget resolves that factor once per instance.
#
# Two modes, selected by UI["slot_scale_geometry"] at construction time:
# * False (default): the tile keeps its ORIGINAL on-screen size — the logical
#   numbers are used directly as device px and text uses the CTkFont's
#   .actual() point size / the original positive point sizes, exactly as the
#   canvas widget always drew them — so the main board does not grow.
# * True: geometry and fonts are multiplied by the DPI factor like CTk
#   widgets (152 logical → 228 device px, -22 px main text at 150 %).
# In BOTH modes the rasters are crisp: slot thumbnails are decoded at the
# real device-pixel box (see below) and emoji glyphs at the device pixel
# size of the (mode-dependent) box they are drawn in — never upscaled.


def _window_scaling(widget: Any) -> float:
    """CTk's DPI factor for the window *widget* lives in (1.0 fallback)."""
    try:
        from customtkinter import ScalingTracker  # type: ignore

        top = widget.winfo_toplevel()
        try:
            return float(ScalingTracker.get_window_scaling(top))
        except Exception:
            # Window not (yet) registered with CTk — ask the OS directly.
            return float(ScalingTracker.get_window_dpi_scaling(top)) * float(
                getattr(ScalingTracker, "window_scaling", 1.0) or 1.0
            )
    except Exception:
        pass
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# Device-size thumbnail decoding
# ---------------------------------------------------------------------------
#
# gui.py's loader hands us a CTkImage whose PIL raster was thumbnailed at the
# LOGICAL size (70x55); a canvas PhotoImage is blitted 1:1 in device px, so at
# 150 % that raster would have to be upscaled (blurry) or drawn small. Instead
# the slot re-decodes the file straight to DEVICE size. That costs ~0.5 s for
# a 150-image library and every tab is prebuilt at startup, so the decode runs
# on ONE daemon worker thread (pure PIL, never touches Tk); the slot paints an
# immediate placeholder (the logical raster fitted to the device box — exactly
# what CTk would show) and swaps in the crisp raster when it lands. Results
# come back through a queue drained by a main-thread `after` poll, so there
# are no cross-thread Tk calls (which raise if the mainloop isn't dispatching,
# e.g. during the synchronous startup build).
#
# Cache: (path, mtime, dev_w, dev_h) -> ImageTk.PhotoImage (or _THUMB_FAILED).
# Bounded; every widget holds its own ref in `_image_tk`, so a clear() can
# never drop an image that is still on screen. Main thread only.
_THUMB_CACHE: dict = {}
_THUMB_CACHE_MAX = 512
_THUMB_FAILED = object()  # negative cache marker: don't retry a broken file
_PENDING: dict = {}  # key -> [SlotWidget, ...] waiting for that decode
_PENDING_LOCK = threading.Lock()
_DECODE_QUEUE: "queue.Queue" = queue.Queue()  # (key, path, dev) → worker
_RESULT_QUEUE: "queue.Queue" = queue.Queue()  # (key, pil | None) ← worker
_decoder: Optional[threading.Thread] = None
_poll_scheduled = False
_poll_host: Any = None  # the Tk ROOT the poll timer is armed on (outlives every slot)


def _decode_device_thumb(path: str, dev: tuple) -> Any:
    """Pure PIL: decode *path* to an aspect-kept raster fitting *dev* (device px).

    ``draft()`` lets JPEGs decode at a reduced DCT scale (2x the target keeps
    LANCZOS quality); ``thumbnail()`` never stretches. A source smaller than
    the box in both dimensions is fitted up so it isn't a speck.
    """
    from PIL import Image, ImageOps  # type: ignore

    src = Image.open(path)
    try:
        src.draft(None, (dev[0] * 2, dev[1] * 2))
    except Exception:
        pass
    src.thumbnail(dev, Image.Resampling.LANCZOS)
    src.load()
    if src.size[0] < dev[0] and src.size[1] < dev[1]:
        src = ImageOps.contain(src, dev, Image.Resampling.LANCZOS)
    return src


def _decode_worker() -> None:
    while True:
        key, path, dev = _DECODE_QUEUE.get()
        try:
            pil = _decode_device_thumb(path, dev)
        except Exception:
            pil = None
        _RESULT_QUEUE.put((key, pil))


def _ensure_decoder() -> None:
    global _decoder
    if _decoder is None or not _decoder.is_alive():
        _decoder = threading.Thread(target=_decode_worker, name="SlotThumbDecoder", daemon=True)
        _decoder.start()


def _poll_host_alive() -> bool:
    try:
        return _poll_host is not None and bool(_poll_host.winfo_exists())
    except Exception:
        return False


def _arm_poll() -> None:
    global _poll_scheduled
    try:
        _poll_host.after(40, _poll_results)
        _poll_scheduled = True
    except Exception:
        _poll_scheduled = False


def _schedule_poll(widget: Any) -> None:
    """Arm the main-thread result poll (idempotent; re-armed while work is in flight).

    Armed on the Tk ROOT, never on a slot: tkinter deletes a widget's pending
    ``after`` commands when that widget is destroyed, so a poll armed on a
    slot that is rebuilt within 40 ms (Columns ±, tab delete, the search
    overlay re-render) silently never fired and left ``_poll_scheduled`` stuck
    True — no thumbnail was delivered again for the rest of the session.
    (after() on an ALREADY-destroyed widget does not raise; destruction AFTER
    arming is the hazard.) The host is re-resolved if it ever dies.
    """
    global _poll_scheduled, _poll_host
    if not _poll_host_alive():
        try:
            _poll_host = widget._root()
        except Exception:
            try:
                _poll_host = widget.winfo_toplevel()
            except Exception:
                _poll_host = widget
        _poll_scheduled = False  # a timer armed on a dead host died with it
    if _poll_scheduled:
        return
    _arm_poll()


def _poll_results() -> None:
    global _poll_scheduled
    _poll_scheduled = False
    while True:
        try:
            key, pil = _RESULT_QUEUE.get_nowait()
        except queue.Empty:
            break
        _deliver_thumb(key, pil)
    # Keep polling while decodes are still in flight — always from the root.
    with _PENDING_LOCK:
        busy = bool(_PENDING)
    if busy and _poll_host_alive():
        _arm_poll()


def _deliver_thumb(key: tuple, pil: Any) -> None:
    """Main thread: wrap the decoded raster ONCE, cache it, hand it to every
    slot that asked for it (duplicates across tabs share the PhotoImage)."""
    with _PENDING_LOCK:
        waiters = _PENDING.pop(key, [])
    photo: Any = _THUMB_FAILED
    if pil is not None:
        try:
            from PIL import ImageTk  # type: ignore

            photo = ImageTk.PhotoImage(pil)
        except Exception:
            photo = _THUMB_FAILED
    if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[key] = photo
    if photo is _THUMB_FAILED:
        return  # slots keep their placeholder
    for w in waiters:
        try:
            w._apply_thumb(key, photo)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _hex_to_rgb(c: str) -> tuple:
    c = c.lstrip("#")
    if len(c) != 6:
        return (50, 50, 50)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except ValueError:
        return (50, 50, 50)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _lighten(color: str, amount: float = 0.08) -> str:
    """Return a slightly lighter version of a hex color (for hover)."""
    if not color or not color.startswith("#"):
        return color
    r, g, b = _hex_to_rgb(color)
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return _rgb_to_hex(r, g, b)


def _resolve_color(c: Any, fallback: str) -> str:
    """Resolve a color value (handles 'transparent' and CTk tuples)."""
    if c is None:
        return fallback
    if isinstance(c, (list, tuple)):
        # CTk uses (light_mode_color, dark_mode_color); pick dark.
        if len(c) >= 2:
            return str(c[1])
        if len(c) == 1:
            return str(c[0])
        return fallback
    if c == "transparent":
        return fallback
    return str(c)


def _resolve_font_legacy(f: Any) -> Any:
    """Original font resolution (UI["slot_scale_geometry"] == False).

    Verbatim pre-DPI behaviour so the rendered text is pixel-identical to
    before: a CTkFont is read through .actual(), whose size is a POSITIVE
    point size that Tk converts with its own scaling (≈15 px for the size-15
    slot font at 150 %); plain tuples are handed to Tk untouched.
    """
    if f is None:
        return ("Segoe UI", 11)
    # CTkFont path
    try:
        actual = getattr(f, "actual", None)
        if callable(actual):
            d = actual()
            family = d.get("family", "Segoe UI")
            size = d.get("size", 11)
            weight = d.get("weight", "normal")
            slant = d.get("slant", "roman")
            parts = [family, size]
            if weight == "bold":
                parts.append("bold")
            if slant == "italic":
                parts.append("italic")
            return tuple(parts)
    except Exception:
        pass
    return f


def _resolve_font(f: Any, scaling: float = 1.0) -> Any:
    """Resolve a font value to a Tk font tuple sized in DEVICE pixels
    (UI["slot_scale_geometry"] == True).

    CTkFont keeps its LOGICAL pixel size in ``.cget("size")``; its
    ``.actual()`` reports a *point* size instead, which Tk then multiplies by
    its own ``tk scaling`` — not CTk's DPI factor — so the canvas text used to
    come out ~30 % smaller than the very same CTkFont on a CTkButton. A
    NEGATIVE Tk font size is pixels, so ``-round(size * scaling)`` renders the
    text exactly as CTk would. Plain ``(family, size, *style)`` tuples with a
    positive (logical) size are converted the same way; a negative size is
    taken as already-device px and passed through.
    """
    if f is None:
        return ("Segoe UI", -max(1, round(11 * scaling)))
    # CTkFont path (duck-typed: only CTkFont has create_scaled_tuple)
    try:
        if hasattr(f, "create_scaled_tuple") and callable(getattr(f, "cget", None)):
            family = f.cget("family") or "Segoe UI"
            size = abs(int(f.cget("size") or 11))
            parts: list = [family, -max(1, round(size * scaling))]
            if f.cget("weight") == "bold":
                parts.append("bold")
            if f.cget("slant") == "italic":
                parts.append("italic")
            return tuple(parts)
    except Exception:
        pass
    # Plain tuple / list: (family, size, *style)
    if isinstance(f, (tuple, list)) and len(f) >= 2:
        try:
            size = int(f[1])
        except (TypeError, ValueError):
            return f
        if size > 0:
            return (f[0], -max(1, round(size * scaling)), *f[2:])
        return tuple(f)
    # Anything else (named font, tkinter.font.Font) — hand to Tk untouched.
    return f


# ---------------------------------------------------------------------------
# SlotWidget — the unified single-Canvas slot
# ---------------------------------------------------------------------------


class SlotWidget(tk.Canvas):
    """One Tk widget that draws the entire slot.

    Layout (top to bottom):
        +--------------------------------------+
        | [emoji]                              |
        |                                      |
        |          [optional image]            |
        |               name                   |
        |             [hotkey]                 |
        |                                      |
        |  ====progress-bar=====               |
        |  [stop?] [⋯ menu]                    |
        +--------------------------------------+

    The bottom 32px is reserved for the progress bar and the stop / menu
    overlay buttons. Click hit-testing is done in `_on_click` based on
    pointer coordinates (cheaper than embedding child widgets).

    All class constants and the ``height`` argument are in the tile's own
    px units. With ``UI["slot_scale_geometry"]`` False (default) they are
    used directly as device px — the original look; with it True they are
    multiplied by the window's DPI factor once in ``__init__``. Either way
    the resolved values (``self._bottom_strip`` …) are what every
    ``create_*`` call and the hit-test use.
    """

    BOTTOM_STRIP = 30  # reserved height at bottom for stop+menu buttons
    OVERLAY_BTN_W = 28
    OVERLAY_BTN_H = 22
    OVERLAY_PAD = 5
    PROGRESS_H = 6  # thicker bar — closer to original CTkProgressBar look
    EMOJI_PX = 22  # emoji glyph side (top-left badge)

    def __init__(
        self,
        parent: Any,
        on_click: Callable[[], None],
        on_right_click: Callable[[Any], None],
        on_menu: Callable[[], None],
        on_stop: Callable[[], None],
        on_drag_drop: Optional[Callable[[int, int], None]] = None,
        height: int = 152,
    ) -> None:
        # Real DPI factor of the window — always used for the thumbnail box
        # (a canvas PhotoImage is blitted 1:1, so that raster must be device px).
        s = _window_scaling(parent)
        if not (0.4 <= s <= 8.0):
            s = 1.0
        self._s: float = s
        # Geometry/font mode (see module notes). Default False keeps the
        # original on-screen tile size; True scales like CTk widgets.
        self._scale_geometry: bool = bool(UI.get("slot_scale_geometry", False))
        sg = s if self._scale_geometry else 1.0
        self._sg: float = sg

        def _d(px: float) -> int:
            return max(1, round(px * sg))

        super().__init__(
            parent,
            highlightthickness=0,
            bd=0,
            bg=COLORS["bg_medium"],
            height=_d(height),
            cursor="hand2",
        )

        # Device-pixel geometry, computed once (no per-frame arithmetic /
        # allocations — the progress animation redraws every playing slot
        # each tick).
        self._bottom_strip: int = _d(self.BOTTOM_STRIP)
        self._ov_w: int = _d(self.OVERLAY_BTN_W)
        self._ov_h: int = _d(self.OVERLAY_BTN_H)
        self._ov_pad: int = _d(self.OVERLAY_PAD)
        self._prog_h: int = _d(self.PROGRESS_H)
        self._inset_min: int = _d(4)
        self._img_dy: int = _d(14)  # image centre lift above content middle
        self._text_dy: int = _d(6)  # text centre drop below content middle
        self._text_img_dy: int = _d(22)  # text baseline offset when an image is shown
        self._text_margin: int = _d(16)  # wrap width margin
        self._emoji_px: int = _d(self.EMOJI_PX)
        self._emoji_xy: int = _d(10)
        self._vol_text_dy: int = _d(12)
        self._font_emoji_fallback: tuple
        self._font_vol: tuple
        self._font_menu: tuple
        self._font_stop: tuple
        if self._scale_geometry:
            # Raw-Tk fonts: NEGATIVE = device px, so they match CTk's sizing.
            self._font_emoji_fallback = ("Segoe UI Emoji", -_d(14))
            self._font_vol = ("Segoe UI", -_d(9), "bold")
            self._font_menu = ("Segoe UI", -_d(11), "bold")
            self._font_stop = ("Segoe UI", -_d(11))
        else:
            # Original positive point sizes — pixel-identical to before.
            self._font_emoji_fallback = ("Segoe UI Emoji", 14)
            self._font_vol = ("Segoe UI", 9, "bold")
            self._font_menu = ("Segoe UI", 11, "bold")
            self._font_stop = ("Segoe UI", 11)

        self._on_click_cb = on_click
        self._on_right_click_cb = on_right_click
        self._on_menu_cb = on_menu
        self._on_stop_cb = on_stop
        self._on_drag_drop_cb = on_drag_drop

        # Visual state
        self._frame_color: str = COLORS["bg_medium"]
        self._button_color: str = COLORS["bg_medium"]  # "transparent" → frame_color
        self._button_color_raw: Any = "transparent"  # original value for hover calc
        self._text: str = "+"
        self._text_color: str = COLORS["text_muted"]
        self._image: Any = None  # CTkImage or PhotoImage
        self._image_tk: Any = None  # Resolved Tk image (cached)
        self._image_key: Optional[tuple] = None  # thumb-cache key of _image (None = n/a)
        self._font: Any = ("Segoe UI Emoji", 11)
        self._font_tk: Any = self._resolve_font_mode(self._font)  # resolved once per set_font
        self._emoji: str = ""
        self._emoji_img: Any = None  # strong ref to current emoji PhotoImage
        self._progress: float = 0.0
        self._progress_color: str = COLORS["playing"]
        self._stop_visible: bool = False
        self._border_width: int = 0
        self._border_color: str = COLORS["text_muted"]
        self._hover: bool = False
        self._volume_display: Optional[float] = None  # temporary volume indicator (0.0-1.5)

        # Avoid full redraws on every event by tracking dirty regions.
        # For now `_redraw_full` is what we call most; progress + overlays
        # have cheaper partial redraws via tagged item deletion.
        self._last_w: int = 0
        self._last_h: int = 0
        # Resize-deferral state: when a Configure arrives during an active
        # window resize, we mark the widget dirty and schedule a single
        # debounced redraw instead of redrawing on every pixel.
        self._dirty: bool = False
        self._redraw_after_id: Optional[str] = None
        # Persistent progress-bar items: the playing animation ticks
        # _redraw_progress every frame, so the fill rect is moved via
        # coords() instead of delete()+create_rectangle() churn.
        self._prog_fill_id: Optional[int] = None
        self._prog_size: tuple = (0, 0)

        # Bindings
        self.bind("<Configure>", self._on_resize, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<B1-Motion>", self._on_motion, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.bind("<Button-3>", self._handle_right_click, add="+")
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")

        # Click state (for press-vs-drag distinction; we treat any release
        # within the widget as a click for simplicity).
        self._press_x: int = 0
        self._press_y: int = 0
        self._press_active: bool = False
        # Drag detection: once motion exceeds threshold, the next release is
        # treated as a drag-drop instead of a click. Pointer deltas are device
        # px, so the threshold is scaled like everything else.
        self._drag_threshold: int = _d(8)
        self._drag_active: bool = False

    # ------------------------------------------------------------------
    # Setters — used by proxies
    # ------------------------------------------------------------------

    def set_frame_color(self, color: Any) -> None:
        c = _resolve_color(color, COLORS["bg_medium"])
        if c != self._frame_color:
            self._frame_color = c
            try:
                self.config(bg=c)  # also paints transparent areas
            except tk.TclError:
                pass
            self._redraw_full()

    def set_button_color(self, color: Any) -> None:
        self._button_color_raw = color
        c = _resolve_color(color, self._frame_color)
        # If "transparent" was passed, we want the frame color to show through.
        if color == "transparent":
            c = self._frame_color
        if c != self._button_color:
            self._button_color = c
            self._redraw_full()

    def set_text(self, text: Any) -> None:
        t = "" if text is None else str(text)
        if t != self._text:
            self._text = t
            self._redraw_full()

    def set_text_color(self, color: Any) -> None:
        c = _resolve_color(color, COLORS["text_primary"])
        if c != self._text_color:
            self._text_color = c
            self._redraw_full()

    def set_image(self, image: Any) -> None:
        if image is self._image:
            return
        self._image = image
        # Resolve to a Tk-compatible image once.
        self._image_tk = self._resolve_image(image)
        self._redraw_full()

    def set_font(self, font: Any) -> None:
        if font is self._font:
            return
        self._font = font
        self._font_tk = self._resolve_font_mode(font)
        self._redraw_full()

    def _resolve_font_mode(self, f: Any) -> Any:
        """Mode-aware font resolution: original point-size path by default,
        DPI-scaled negative-px path when UI["slot_scale_geometry"] is True."""
        if self._scale_geometry:
            return _resolve_font(f, self._s)
        return _resolve_font_legacy(f)

    def set_emoji(self, emoji: str) -> None:
        e = emoji or ""
        if e != self._emoji:
            self._emoji = e
            self._redraw_full()

    def set_progress(self, value: float) -> None:
        v = max(0.0, min(1.0, float(value)))
        if abs(v - self._progress) < 0.005:
            return
        self._progress = v
        self._redraw_progress()

    def set_progress_color(self, color: Any) -> None:
        c = _resolve_color(color, COLORS["playing"])
        if c != self._progress_color:
            self._progress_color = c
            self._redraw_progress()

    def set_stop_visible(self, visible: bool) -> None:
        if visible != self._stop_visible:
            self._stop_visible = visible
            self._redraw_overlays()

    def set_border(self, width: Optional[int] = None, color: Optional[str] = None) -> None:
        dirty = False
        if width is not None and width != self._border_width:
            self._border_width = int(width)
            dirty = True
        if color is not None:
            c = _resolve_color(color, COLORS["text_muted"])
            if c != self._border_color:
                self._border_color = c
                dirty = True
        if dirty:
            self._redraw_full()

    # ------------------------------------------------------------------
    # Image resolution
    # ------------------------------------------------------------------

    def _resolve_image(self, image: Any) -> Any:
        """Return a Tk PhotoImage for a CTkImage / PhotoImage / None, rasterised
        at DEVICE size.

        A PhotoImage on a tk.Canvas is drawn 1:1 in device pixels, so the
        raster has to be ``round(logical * scaling)`` px: the old
        ``pil.resize(size)`` drew the 70x55 LOGICAL raster (47x37 logical at
        150 %) and stretched its aspect ratio on top. When the loader's raster
        is smaller than the device box, the file (``pil.filename``) is
        re-decoded at device size on the background worker (see module notes)
        and this returns an immediate placeholder — the raster fitted to the
        box, aspect kept — which ``_apply_thumb`` replaces once the crisp
        version lands. Results are cached by (path, mtime, device size), so a
        rebuild / tab switch never decodes twice.
        """
        self._image_key = None
        if image is None:
            return None
        try:
            from customtkinter import CTkImage
        except Exception:
            return image
        if not isinstance(image, CTkImage):
            return image  # already a Tk PhotoImage
        try:
            from PIL import Image, ImageOps, ImageTk  # type: ignore
        except Exception:
            return None
        try:
            pil = image._dark_image or image._light_image  # type: ignore[attr-defined]
        except Exception:
            pil = None
        if pil is None:
            return None
        size = getattr(image, "_size", None) or pil.size
        # Box in DEVICE px. In the default (legacy-geometry) mode the box is the
        # same 70x55 device px the tile has always reserved — decoded crisply
        # and aspect-correct now, but no bigger, so a tall picture can't grow
        # into the two-line name. Scaled mode uses the DPI-correct box.
        s = self._s if self._scale_geometry else 1.0
        dev = (max(1, round(size[0] * s)), max(1, round(size[1] * s)))

        path = getattr(pil, "filename", None) or ""
        key = None
        if path:
            try:
                key = (path, os.path.getmtime(path), dev[0], dev[1])
            except OSError:
                key = None
        failed = False
        if key is not None:
            cached = _THUMB_CACHE.get(key)
            if cached is _THUMB_FAILED:
                failed = True
            elif cached is not None:
                self._image_key = key
                return cached

        # Placeholder / direct result: fit the raster we already have into the
        # device box (aspect kept — never stretched). Cheap (~0.1 ms).
        try:
            src = pil.copy()
            src.thumbnail(dev, Image.Resampling.LANCZOS)
            if src.size[0] < dev[0] and src.size[1] < dev[1]:
                src = ImageOps.contain(src, dev, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(src)
        except Exception:
            return None

        if key is None or failed:
            return photo
        self._image_key = key
        if pil.size[0] < dev[0] and pil.size[1] < dev[1]:
            # Loader raster is LOGICAL-sized (too small): queue a crisp
            # device-size decode; the placeholder shows meanwhile.
            with _PENDING_LOCK:
                waiters = _PENDING.get(key)
                if waiters is None:
                    _PENDING[key] = [self]
                    _DECODE_QUEUE.put((key, path, dev))
                else:
                    waiters.append(self)
            _ensure_decoder()
            _schedule_poll(self)
        else:
            # Raster already covers the device box (loader gave device px, or
            # scaling is 1.0): the fitted copy IS the final result — cache it.
            if len(_THUMB_CACHE) >= _THUMB_CACHE_MAX:
                _THUMB_CACHE.clear()
            _THUMB_CACHE[key] = photo
        return photo

    def _apply_thumb(self, key: tuple, photo: Any) -> None:
        """Main thread: swap in the crisp device-size raster if this slot still
        shows the image it was decoded for. Only the image item is touched —
        no full redraw."""
        if key != self._image_key:
            return  # image changed / cleared while the decode was in flight
        self._image_tk = photo
        try:
            if self.find_withtag("image"):
                self.itemconfigure("image", image=photo)
            elif self.winfo_width() > 1 and self.winfo_height() > 1:
                self._redraw_full()
            # else: not laid out yet — the Configure redraw will use _image_tk
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _on_resize(self, event: Any) -> None:
        # Skip if size didn't actually change (Tk fires Configure for
        # position changes too).
        if event.width == self._last_w and event.height == self._last_h:
            return
        self._last_w = event.width
        self._last_h = event.height
        # During an active window resize, do NOT redraw on every pixel —
        # mark dirty and schedule a single debounced redraw. This is the
        # main reason resize feels smooth even with 60 SlotWidgets.
        now = time.time()
        if now < _SHARED_RESIZE_STATE.get("until", 0.0):
            self._dirty = True
            # 100ms: long enough to skip every intermediate pixel of a drag,
            # short enough that the stale stretched canvas (the "smudge")
            # disappears almost as soon as the user pauses.
            self._schedule_debounced_redraw(100)
            return
        # Outside an active resize (e.g. tab switch, initial layout, audio
        # options toggle), redraw IMMEDIATELY so the user never sees a stale
        # canvas at the old size. The cost of one redraw per slot on a
        # one-shot layout pass is fine; what we wanted to avoid was redrawing
        # 60 slots × every pixel of a continuous drag.
        if self._redraw_after_id is not None:
            try:
                self.after_cancel(self._redraw_after_id)
            except Exception:
                pass
            self._redraw_after_id = None
        self._dirty = False
        self._redraw_full()

    def _schedule_debounced_redraw(self, ms: int) -> None:
        if self._redraw_after_id is not None:
            try:
                self.after_cancel(self._redraw_after_id)
            except Exception:
                pass
            self._redraw_after_id = None
        try:
            self._redraw_after_id = self.after(ms, self._do_debounced_redraw)
        except Exception:
            self._redraw_after_id = None

    def _do_debounced_redraw(self) -> None:
        self._redraw_after_id = None
        # If a fresh resize started while we were waiting, defer further.
        if time.time() < _SHARED_RESIZE_STATE.get("until", 0.0):
            self._schedule_debounced_redraw(100)
            return
        if self._dirty:
            self._dirty = False
            self._redraw_full()

    def _redraw_full(self) -> None:
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return
        if w <= 1 or h <= 1:
            return

        self.delete("all")
        self._prog_fill_id = None  # all canvas items are gone

        # Outer frame (canvas bg already set; draw border if any). Default
        # mode: the original 1 px-inset rectangle at the given width. Scaled
        # mode: width × DPI, outline centred on half its width so none of it
        # is clipped.
        bw = 0
        o: float = 1
        if self._border_width > 0:
            if self._scale_geometry:
                bw = max(1, round(self._border_width * self._s))
                o = bw / 2
            else:
                bw = int(self._border_width)
        if bw > 0:
            self.create_rectangle(
                o,
                o,
                w - o,
                h - o,
                outline=self._border_color,
                width=bw,
                fill="",
                tags="border",
            )

        # Inner button area (the "real" slot button bg)
        inset = max(bw, self._inset_min)
        bx0, by0 = inset, inset
        bx1, by1 = w - inset, h - inset

        # Hover lighten
        button_fill = self._button_color
        if self._hover and self._button_color and self._button_color != self._frame_color:
            button_fill = _lighten(self._button_color, 0.06)

        if button_fill and button_fill != self._frame_color:
            self.create_rectangle(
                bx0,
                by0,
                bx1,
                by1,
                outline="",
                fill=button_fill,
                tags="bg",
            )

        content_h = h - self._bottom_strip

        # Image (centered above text if present)
        text_cy = content_h // 2 + self._text_dy
        if self._image_tk is not None:
            try:
                self.create_image(
                    w // 2,
                    content_h // 2 - self._img_dy,
                    image=self._image_tk,
                    tags="image",
                )
                text_cy = content_h - self._text_img_dy
            except tk.TclError:
                pass

        # Main text
        if self._text:
            wrap_w = max(self._text_margin + 4, w - self._text_margin)
            try:
                self.create_text(
                    w // 2,
                    text_cy,
                    text=self._text,
                    fill=self._text_color,
                    font=self._font_tk,
                    anchor="center",
                    width=wrap_w,
                    justify="center",
                    tags="text",
                )
            except tk.TclError:
                # Some font objects may be invalid in edge cases; fall back.
                self.create_text(
                    w // 2,
                    text_cy,
                    text=self._text,
                    fill=self._text_color,
                    anchor="center",
                    width=wrap_w,
                    justify="center",
                    tags="text",
                )

        # Emoji top-left — try rendering as a true-color image first,
        # fall back to Tk's monochrome glyph if PIL/the font is unavailable.
        # The glyph is rasterised at DEVICE px (a canvas PhotoImage is 1:1).
        if self._emoji:
            emoji_img = None
            try:
                from . import emoji_render as _er

                emoji_img = _er.get_tk_image(self._emoji, self._emoji_px)
            except Exception:
                emoji_img = None
            exy = self._emoji_xy
            if emoji_img is not None:
                # Stash a ref on the widget too — the renderer cache holds
                # the canonical reference but this also protects against
                # accidental cache flushes mid-frame.
                self._emoji_img = emoji_img
                try:
                    self.create_image(
                        exy,
                        exy,
                        image=emoji_img,
                        anchor="nw",
                        tags="emoji",
                    )
                except tk.TclError:
                    self.create_text(
                        exy,
                        exy,
                        text=self._emoji,
                        fill=COLORS["text_primary"],
                        font=self._font_emoji_fallback,
                        anchor="nw",
                        tags="emoji",
                    )
            else:
                self.create_text(
                    exy,
                    exy,
                    text=self._emoji,
                    fill=COLORS["text_primary"],
                    font=self._font_emoji_fallback,
                    anchor="nw",
                    tags="emoji",
                )

        # Progress bar + overlays at the bottom
        self._redraw_progress()
        self._redraw_overlays()

    def _redraw_progress(self) -> None:
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return

        # If volume display is active, show volume bar instead of progress
        if self._volume_display is not None:
            self.delete("progress")
            self._prog_fill_id = None
            volume = max(0.0, min(1.5, self._volume_display))
            # Bar sits ABOVE the overlay button row, with a small gap.
            bar_pad_left = self._ov_pad
            bar_pad_right = self._ov_pad
            y_bot = h - self._bottom_strip - 2
            y_top = y_bot - self._prog_h
            track_x0 = bar_pad_left
            track_x1 = w - bar_pad_right
            # Track (background) — dark with a white border for visibility
            self.create_rectangle(
                track_x0,
                y_top,
                track_x1,
                y_bot,
                outline="#44ff44",  # Green outline for volume
                width=1,
                fill=COLORS["bg_dark"],
                tags="progress",
            )
            # Fill — green for volume (0-1.0) then yellow/orange for boosted (1.0-1.5)
            fill_w = (track_x1 - track_x0 - 2) * (volume / 1.5)  # scale to 1.5 max
            if fill_w > 1:
                # Color changes: green for normal, yellow for boost
                fill_color = "#44ff44" if volume <= 1.0 else "#ffcc00"
                self.create_rectangle(
                    track_x0 + 1,
                    y_top + 1,
                    track_x0 + 1 + fill_w,
                    y_bot - 1,
                    outline="",
                    fill=fill_color,
                    tags="progress",
                )
            # Add volume percentage text
            volume_pct = int(round(volume * 100))
            self.create_text(
                (track_x0 + track_x1) // 2,
                y_top - self._vol_text_dy,
                text=f"🔊 {volume_pct}%",
                fill="#44ff44",
                font=self._font_vol,
                anchor="center",
                tags="progress",
            )
            return
        
        # Normal progress display
        if w <= 1 or self._progress <= 0.0:
            # find_withtag too: the volume gauge leaves "progress"-tagged
            # items behind with _prog_fill_id already None — they must still
            # be cleared when the display returns to idle.
            if self._prog_fill_id is not None or self.find_withtag("progress"):
                self.delete("progress")
                self._prog_fill_id = None
            return
        # Bar sits ABOVE the overlay button row, with a small gap.
        bar_pad_left = self._ov_pad
        bar_pad_right = self._ov_pad
        y_bot = h - self._bottom_strip - 2
        y_top = y_bot - self._prog_h
        track_x0 = bar_pad_left
        track_x1 = w - bar_pad_right
        fill_w = max(0.0, (track_x1 - track_x0 - 2) * self._progress)

        # Fast path — the playing animation ticks here every frame for every
        # playing slot, so just move the existing fill rect's right edge.
        # NB: coords() on a deleted item is a SILENT no-op (returns []), so
        # existence is checked positively rather than via TclError.
        if self._prog_fill_id is not None and (w, h) == self._prog_size:
            try:
                if self.coords(self._prog_fill_id):
                    self.coords(
                        self._prog_fill_id,
                        track_x0 + 1,
                        y_top + 1,
                        track_x0 + 1 + fill_w,
                        y_bot - 1,
                    )
                    return
                self._prog_fill_id = None  # item vanished — full rebuild
            except tk.TclError:
                self._prog_fill_id = None  # fall through to a full rebuild

        self.delete("progress")
        # Track (background) — dark with a white border for visibility
        self.create_rectangle(
            track_x0,
            y_top,
            track_x1,
            y_bot,
            outline="#ffffff",
            width=1,
            fill=COLORS["bg_dark"],
            tags="progress",
        )
        # Fill — red, inset 1px so the white border stays visible. Created even
        # at zero width so the per-tick fast path always has an item to move.
        self._prog_fill_id = self.create_rectangle(
            track_x0 + 1,
            y_top + 1,
            track_x0 + 1 + fill_w,
            y_bot - 1,
            outline="",
            fill=COLORS["red"],
            tags="progress",
        )
        self._prog_size = (w, h)

    def _redraw_overlays(self) -> None:
        self.delete("overlay")
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return
        if w <= 1:
            return

        btn_y_top = h - self._ov_h - self._ov_pad
        btn_y_bot = h - self._ov_pad

        # ⋯ menu button (always visible) — bottom-right
        mx1 = w - self._ov_pad
        mx0 = mx1 - self._ov_w
        self.create_rectangle(
            mx0,
            btn_y_top,
            mx1,
            btn_y_bot,
            outline="",
            fill=COLORS["bg_light"],
            tags=("overlay", "menu_btn"),
        )
        self.create_text(
            (mx0 + mx1) // 2,
            (btn_y_top + btn_y_bot) // 2,
            text="⋯",
            fill=COLORS["text_primary"],
            font=self._font_menu,
            tags=("overlay", "menu_btn"),
        )

        # Stop button (only when sound is playing) — bottom-left
        if self._stop_visible:
            sx0 = self._ov_pad
            sx1 = sx0 + self._ov_w
            self.create_rectangle(
                sx0,
                btn_y_top,
                sx1,
                btn_y_bot,
                outline="",
                fill=COLORS["red"],
                tags=("overlay", "stop_btn"),
            )
            self.create_text(
                (sx0 + sx1) // 2,
                (btn_y_top + btn_y_bot) // 2,
                text="⏹",
                fill="white",
                font=self._font_stop,
                tags=("overlay", "stop_btn"),
            )

    # ------------------------------------------------------------------
    # Hit-testing & event handlers
    # ------------------------------------------------------------------

    def _hit_test(self, x: int, y: int) -> str:
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return "main"
        if w <= 1 or h <= 1:
            return "main"

        btn_y_top = h - self._ov_h - self._ov_pad
        btn_y_bot = h - self._ov_pad
        if btn_y_top <= y <= btn_y_bot:
            mx1 = w - self._ov_pad
            mx0 = mx1 - self._ov_w
            if mx0 <= x <= mx1:
                return "menu"
            if self._stop_visible:
                sx0 = self._ov_pad
                sx1 = sx0 + self._ov_w
                if sx0 <= x <= sx1:
                    return "stop"
        return "main"

    def _on_press(self, event: Any) -> None:
        self._press_x = event.x
        self._press_y = event.y
        self._press_active = True
        self._drag_active = False

    def _on_motion(self, event: Any) -> None:
        # Only meaningful if the consumer wants drag events.
        if self._on_drag_drop_cb is None or self._drag_active:
            return
        dx = event.x - self._press_x
        dy = event.y - self._press_y
        if (dx * dx + dy * dy) >= (self._drag_threshold * self._drag_threshold):
            self._drag_active = True
            try:
                self.config(cursor="exchange")
            except tk.TclError:
                pass

    def _on_release(self, event: Any) -> None:
        if not self._press_active:
            return
        self._press_active = False

        # Restore cursor regardless of where release happened.
        if self._drag_active:
            try:
                self.config(cursor="hand2")
            except tk.TclError:
                pass

        # If a drag was in progress, fire the drag-drop callback with screen
        # coordinates so the host can decide what was hit. Suppress click.
        if self._drag_active and self._on_drag_drop_cb is not None:
            self._drag_active = False
            try:
                self._on_drag_drop_cb(event.x_root, event.y_root)
            except Exception:
                pass
            return

        # Only fire click if release happened inside the widget.
        try:
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return
        if not (0 <= event.x <= w and 0 <= event.y <= h):
            return
        target = self._hit_test(event.x, event.y)
        if target == "stop":
            try:
                self._on_stop_cb()
            except Exception:
                pass
        elif target == "menu":
            try:
                self._on_menu_cb()
            except Exception:
                pass
        else:
            try:
                self._on_click_cb()
            except Exception:
                pass

    def _handle_right_click(self, event: Any) -> None:
        try:
            self._on_right_click_cb(event)
        except Exception:
            pass

    def _on_enter(self, event: Any) -> None:
        if not self._hover:
            self._hover = True
            self._apply_hover_fill()

    def _on_leave(self, event: Any) -> None:
        if self._hover:
            self._hover = False
            self._apply_hover_fill()

    def _apply_hover_fill(self) -> None:
        """Recolour the existing bg rect in place. Hover is a 6% lighten of the
        button fill — sweeping the mouse across a dense tab must not pay a full
        delete("all") + rebuild per slot crossed."""
        if not self._button_color or self._button_color == self._frame_color:
            # No bg rect exists in this state and the lighten would be a
            # no-op anyway (matches the condition in _redraw_full).
            return
        fill = _lighten(self._button_color, 0.06) if self._hover else self._button_color
        try:
            if self.find_withtag("bg"):
                self.itemconfigure("bg", fill=fill)
            else:
                self._redraw_full()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Proxies — adapt the SlotWidget setters to the legacy widget APIs that
# are referenced throughout gui.py.
# ---------------------------------------------------------------------------


class _BaseProxy:
    """Common no-ops so legacy code that calls .bind() / .winfo_exists() etc.
    on these aliases doesn't crash."""

    def __init__(self, slot_widget: SlotWidget) -> None:
        self.slot_widget = slot_widget

    def bind(self, *a: Any, **k: Any) -> None:
        pass

    def unbind(self, *a: Any, **k: Any) -> None:
        pass

    def winfo_exists(self) -> bool:
        try:
            return bool(self.slot_widget.winfo_exists())
        except Exception:
            return False

    def winfo_ismapped(self) -> bool:
        return self.winfo_exists()

    def winfo_rootx(self) -> int:
        try:
            return int(self.slot_widget.winfo_rootx())
        except Exception:
            return 0

    def winfo_rooty(self) -> int:
        try:
            return int(self.slot_widget.winfo_rooty())
        except Exception:
            return 0

    def winfo_width(self) -> int:
        try:
            return int(self.slot_widget.winfo_width())
        except Exception:
            return 0

    def winfo_height(self) -> int:
        try:
            return int(self.slot_widget.winfo_height())
        except Exception:
            return 0

    def lift(self) -> None:
        pass

    def lower(self) -> None:
        pass

    def place(self, *a: Any, **k: Any) -> None:
        pass

    def grid(self, *a: Any, **k: Any) -> None:
        pass

    def grid_remove(self) -> None:
        pass


class FrameProxy(_BaseProxy):
    """Routes slot_frames[idx].configure(fg_color=..., border_*=...) calls."""

    def configure(self, **kwargs: Any) -> None:
        if "fg_color" in kwargs:
            self.slot_widget.set_frame_color(kwargs["fg_color"])
        if "border_color" in kwargs or "border_width" in kwargs:
            self.slot_widget.set_border(
                width=kwargs.get("border_width"),
                color=kwargs.get("border_color"),
            )

    config = configure  # alias


class ButtonProxy(_BaseProxy):
    """Routes slot_buttons[idx].configure(...) calls."""

    def configure(self, **kwargs: Any) -> None:
        if "fg_color" in kwargs:
            self.slot_widget.set_button_color(kwargs["fg_color"])
        if "text" in kwargs:
            self.slot_widget.set_text(kwargs["text"])
        if "text_color" in kwargs:
            self.slot_widget.set_text_color(kwargs["text_color"])
        if "image" in kwargs:
            self.slot_widget.set_image(kwargs["image"])
        if "font" in kwargs:
            self.slot_widget.set_font(kwargs["font"])
        # silently drop hover_color, anchor, compound, etc.

    config = configure


class ProgressProxy(_BaseProxy):
    """Routes slot_progress[idx].set(v) and .configure(progress_color=...)."""

    def set(self, value: float) -> None:
        self.slot_widget.set_progress(value)

    def get(self) -> float:
        return self.slot_widget._progress

    def configure(self, **kwargs: Any) -> None:
        if "progress_color" in kwargs:
            self.slot_widget.set_progress_color(kwargs["progress_color"])

    config = configure


class StopButtonProxy(_BaseProxy):
    """Routes stop button visibility via .pack() / .pack_forget()."""

    def pack(self, *args: Any, **kwargs: Any) -> None:
        self.slot_widget.set_stop_visible(True)

    def pack_forget(self) -> None:
        self.slot_widget.set_stop_visible(False)

    def configure(self, **kwargs: Any) -> None:
        pass

    config = configure


class MenuButtonProxy(_BaseProxy):
    """Routes ⋯ menu button. Visible permanently — color updates are no-ops."""

    def configure(self, **kwargs: Any) -> None:
        pass

    config = configure

    def pack(self, *a: Any, **k: Any) -> None:
        pass

    def pack_forget(self) -> None:
        pass


class EmojiLabelProxy(_BaseProxy):
    """Routes emoji_label.configure(text=...) and lift/lower visibility."""

    def configure(self, **kwargs: Any) -> None:
        if "text" in kwargs:
            self.slot_widget.set_emoji(kwargs.get("text") or "")

    config = configure

    def lift(self) -> None:
        # Emoji is shown when set; nothing to do here.
        pass

    def lower(self) -> None:
        # Hide emoji.
        self.slot_widget.set_emoji("")
