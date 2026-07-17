"""
Per-person mini-soundboards: the "People" hub plus floating pop-out windows.

A *Person* owns named *groups* of sounds (e.g. "Hi", "Bye", "lol"). Sounds can
be added from a file or copied in from an existing main-board slot. The same
``PersonPanel`` renders a person both inside the hub's right pane and inside a
standalone always-on-top pop-out window, so you can keep several people's boards
open next to your game.

The module talks to the app only through a small ``PersonContext`` so it stays
decoupled and testable.
"""

import functools
import logging
import math
import os
import textwrap
import time
import uuid
import tkinter as tk
from collections import OrderedDict
from tkinter import filedialog, messagebox
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


def _new_gid() -> str:
    """Fresh stable id for a shared group."""
    return uuid.uuid4().hex[:8]

import customtkinter as ctk

from . import emoji_render
from ._shared import arm_toplevel_resize_defer
from .constants import COLORS, FONTS, SUPPORTED_FORMATS, get_text_color_for_bg
from .emoji_picker import pick_emoji
from .models import Person, PersonGroup, SoundSlot

try:
    # Shared with the main board's resize-defer perf patch. While
    # ``time.time() < RESIZE_STATE["until"]`` CTk skips per-widget draws, so a
    # rebuild landing in that window leaves chips blank — the ONLY case where the
    # forced redraw nudge is needed (see _schedule_nudge).
    from ._shared import RESIZE_STATE as _RESIZE_STATE
except Exception:  # pragma: no cover - isolated test imports
    _RESIZE_STATE = {"until": 0.0}


def _disp(text):
    """Identity pass-through.

    Tk renders RTL (Hebrew/Arabic) correctly in DISPLAY widgets (CTkLabel /
    CTkButton / Canvas) on this platform, so reordering here would mirror text
    that is already correct. Kept as a hook around every name-bearing label in
    case a future platform needs it.
    """
    return text

# A compact, pleasant palette for colouring people / groups / sounds.
PALETTE = [
    COLORS["blurple"], COLORS["green"], COLORS["red"], COLORS["yellow"],
    "#E67E22", "#9B59B6", "#1ABC9C", "#E91E63",
    "#3498DB", "#2ECC71", "#F39C12", "#95A5A6",
]

# ---------------------------------------------------------------------------
# Outlined chip text — white glyphs wrapped in a black inner border + a white
# outer border, so a sound name is legible on ANY chip colour / state, light or
# dark. Rendered with PIL (CTk/Tk labels can't stroke text). LOGICAL text is fed
# straight in: PIL+raqm applies BiDi exactly like CTk's own labels, so Hebrew is
# correct — DO NOT pre-reorder with get_display (that double-reverses → mirrored).
# Everything is best-effort: any failure returns None and the chip falls back to
# a normal text button, so this can never break the board.
# ---------------------------------------------------------------------------
_OUTLINE_SS = 3          # supersample factor (render 3x, display 1x) for crisp text
_OUTLINE_TEXT_PX = 14    # logical glyph height
_CHIP_MAX_LINES = 2      # wrap a long sound name onto up to this many lines
# Subtitle-style: BOLD white glyphs + a solid black outline thick enough to stay
# readable on ANY chip colour/state — light person colours, the amber "playing"
# and green "preview" states — because the chip recolours on play, so the baked
# label must read on all of them (white-on-light needs a real dark halo, not a
# hairline). No outer white ring (it only adds fuzz). Widths are SUPERSAMPLED px
# (÷_OUTLINE_SS on screen): wb=6 → ~2px black edge.
_OUTLINE_WB = 6          # black outline width (supersampled px)
_OUTLINE_WW = 0          # no outer white ring
_OUTLINE_BOLD = True     # render chip labels bold for readability
_OUTLINE_FONTS = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf")
_OUTLINE_FONTS_BOLD = ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf")


@functools.lru_cache(maxsize=16)
def _outline_font(px: int, bold: bool = False):
    from PIL import ImageFont
    for path in (_OUTLINE_FONTS_BOLD if bold else _OUTLINE_FONTS):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _is_rtl(text: str) -> bool:
    """True if the string contains Hebrew/Arabic characters (so a multi-line
    label should right-align, matching natural reading)."""
    for ch in text:
        o = ord(ch)
        if 0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF:
            return True
    return False


@functools.lru_cache(maxsize=4096)
def _outlined_line_pil(text: str, px: int, wb: int, ww: int,
                       fill: tuple = (255, 255, 255, 255), bold: bool = False):
    """RGBA PIL image of a SINGLE line of ``text``: ``fill`` glyphs + black inner
    + white outer border (transparent elsewhere). ``text`` is LOGICAL (PIL/raqm
    applies BiDi, so Hebrew is correct — DO NOT pre-reorder). None on failure."""
    try:
        from PIL import Image, ImageDraw
        font = _outline_font(px, bold)
        if font is None or not text:
            return None
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = probe.textbbox((0, 0), text, font=font, stroke_width=wb + ww)
        # RTL returns sub-pixel FLOAT coords; Image.new needs ints.
        w, h = int(math.ceil(bbox[2] - bbox[0])), int(math.ceil(bbox[3] - bbox[1]))
        pad = wb + ww + 2
        img = Image.new("RGBA", (max(1, w + 2 * pad), max(1, h + 2 * pad)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ox, oy = pad - bbox[0], pad - bbox[1]
        white, black = (255, 255, 255, 255), (0, 0, 0, 255)
        # Concentric: widest white blob, then black blob, then the glyph — leaves
        # a black ring (wb) hugging the text and a white ring (ww) outside it.
        d.text((ox, oy), text, font=font, fill=white, stroke_width=wb + ww, stroke_fill=white)
        d.text((ox, oy), text, font=font, fill=black, stroke_width=wb, stroke_fill=black)
        d.text((ox, oy), text, font=font, fill=fill, stroke_width=0)
        return img
    except Exception:
        return None


@functools.lru_cache(maxsize=2048)
def _outlined_text_pil(text: str, px: int, wb: int, ww: int,
                       fill: tuple = (255, 255, 255, 255),
                       align: str = "left", spacing: int = 2, bold: bool = False):
    """RGBA PIL image of ``text`` (may contain newlines). Each line is rendered
    SEPARATELY and stacked — NOT via PIL ``multiline_text``, which with a stroke
    leaves a solid black band under the lower lines. Lines are aligned per
    ``align`` (right for RTL). Returns None on failure."""
    try:
        from PIL import Image
        if not text:
            return None
        line_imgs = [_outlined_line_pil(ln, px, wb, ww, fill, bold)
                     for ln in text.split("\n")]
        line_imgs = [im for im in line_imgs if im is not None]
        if not line_imgs:
            return None
        if len(line_imgs) == 1:
            return line_imgs[0]
        W = max(im.width for im in line_imgs)
        H = sum(im.height for im in line_imgs) + spacing * (len(line_imgs) - 1)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        y = 0
        for im in line_imgs:
            if align == "right":
                x = W - im.width
            elif align == "center":
                x = (W - im.width) // 2
            else:
                x = 0
            canvas.alpha_composite(im, (x, y))
            y += im.height + spacing
        return canvas
    except Exception:
        return None


# Cache the composited chip-label PIL (emoji + outlined text) by
# (name, emoji, px) so rebuilds / search / density changes don't re-composite
# and re-rasterize the same label every chip every time. The expensive glyph
# raster underneath (_outlined_text_pil) is already lru_cached.
_chip_label_pil_cache: dict = {}
# ...and cache the CTkImage WRAPPER too. CTkImage keeps its scaled
# ImageTk.PhotoImage per-instance, so wrapping a fresh one per call (the old
# "never share" rule) re-ran a BICUBIC resize of the 3x-supersampled canvas +
# a PIL→Tk PhotoImage conversion per chip on EVERY rebuild (~1ms each, ~100+
# per panel). Sharing one CTkImage across widgets is supported by CTk — it
# tracks consumers and reuses the per-size PhotoImage. LRU-bounded.
_chip_label_ctk_cache: "OrderedDict" = OrderedDict()
_CTK_IMG_CACHE_MAX = 2048


def _ctk_cache_get(cache: "OrderedDict", key, factory):
    """Shared bounded-LRU lookup for CTkImage caches."""
    img = cache.get(key)
    if img is not None:
        cache.move_to_end(key)
        return img
    img = factory()
    if img is not None:
        cache[key] = img
        while len(cache) > _CTK_IMG_CACHE_MAX:
            cache.popitem(last=False)
    return img


def _compose_chip_label_pil(name: str, emoji: Optional[str], px_logical: int):
    """The supersampled RGBA PIL canvas for a chip label, or None to let the
    caller fall back to a plain text button. Pure (no Tk), so it's cacheable."""
    try:
        from PIL import Image
        ss = _OUTLINE_SS
        align = "right" if _is_rtl(name) else "left"
        txt = _outlined_text_pil(name, px_logical * ss, _OUTLINE_WB, _OUTLINE_WW,
                                 align=align, spacing=2 * ss, bold=_OUTLINE_BOLD)
        emo = None
        if emoji:
            try:
                emo = emoji_render.get_pil_image(emoji, px_logical * ss)
            except Exception:
                emo = None
        # If the sound HAS a name but the outlined text failed to render, return
        # None so the caller falls back to a normal text button (name preserved).
        # Otherwise an emoji-only image would silently drop the name.
        if txt is None and name:
            return None
        parts = [p for p in (emo, txt) if p is not None]
        if not parts:
            return None
        gap = 4 * ss if len(parts) > 1 else 0
        H = max(p.height for p in parts)
        W = sum(p.width for p in parts) + gap * (len(parts) - 1)
        if W <= 0 or H <= 0:
            return None
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        x = 0
        for im in parts:
            canvas.alpha_composite(im.convert("RGBA"), (x, (H - im.height) // 2))
            x += im.width + gap
        return canvas
    except Exception:
        return None


def _chip_label_image(name: str, emoji: Optional[str], px_logical: int = _OUTLINE_TEXT_PX):
    """A (shared, cached) CTkImage of ``[emoji] [outlined name]`` for a sound
    chip, or None to let the caller fall back to a plain text button."""
    key = (name, emoji or "", px_logical)

    def _make():
        canvas = _chip_label_pil_cache.get(key)
        if canvas is None:
            canvas = _compose_chip_label_pil(name, emoji, px_logical)
            if canvas is None:
                return None  # failure stays uncached (cheap, may be transient)
            if len(_chip_label_pil_cache) > 1024:
                _chip_label_pil_cache.clear()
            _chip_label_pil_cache[key] = canvas
        try:
            ss = _OUTLINE_SS
            W, H = canvas.size
            return ctk.CTkImage(light_image=canvas, dark_image=canvas,
                                size=(max(1, W // ss), max(1, H // ss)))
        except Exception:
            return None

    return _ctk_cache_get(_chip_label_ctk_cache, key, _make)


@functools.lru_cache(maxsize=4096)
def _wrap_label(name: str, max_px_logical: int, max_lines: int = _CHIP_MAX_LINES) -> str:
    """Wrap a sound name onto up to ``max_lines`` lines that each FIT within
    ``max_px_logical`` (the chip's usable text width), so a long title shows in
    full across two lines instead of being clipped. Wrapping by measured pixel
    width — not character count — is what makes this work for bold + Hebrew
    glyphs (a "short" 20-char Hebrew name is still wider than the chip). Returns
    the name unchanged when it already fits on one line; ellipsises the last
    line if even ``max_lines`` lines can't hold it."""
    name = (name or "").strip()
    if not name:
        return name
    ss = _OUTLINE_SS
    font = _outline_font(_OUTLINE_TEXT_PX * ss, _OUTLINE_BOLD)
    if font is None:
        return name
    # _outlined_text_pil pads the canvas by (wb+ww+2) on every side, so the
    # rendered image is wider than the glyph run; subtract that so a wrapped line
    # (image + pad) actually fits the chip instead of clipping by a few px.
    pad2 = 2 * (_OUTLINE_WB + _OUTLINE_WW + 2)
    max_px = max(1, int(max_px_logical * ss) - pad2)

    # Measure with the SAME engine the label is drawn with — an ImageDraw bbox
    # WITH the stroke — not font.getbbox(), which omits the stroke and the raqm
    # shaping and so under-measures Hebrew (the wrap then never triggered).
    try:
        from PIL import Image, ImageDraw
        _probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    except Exception:
        return name

    def w(s: str) -> int:
        try:
            b = _probe.textbbox((0, 0), s, font=font, stroke_width=_OUTLINE_WB)
            return b[2] - b[0]
        except Exception:
            return len(s) * _OUTLINE_TEXT_PX * ss

    if w(name) <= max_px:
        return name

    # Greedy word-wrap; char-break a single word that's wider than a line.
    lines: list = []
    cur = ""
    for word in name.split(" "):
        cand = word if not cur else cur + " " + word
        if w(cand) <= max_px:
            cur = cand
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if w(word) <= max_px:
            cur = word
        else:
            piece = ""
            for ch in word:
                if w(piece + ch) <= max_px or not piece:
                    piece += ch
                else:
                    lines.append(piece)
                    piece = ch
            cur = piece
    if cur:
        lines.append(cur)

    if len(lines) <= max_lines:
        return "\n".join(lines)

    # Too long for max_lines: keep the first lines, ellipsise the last to fit.
    kept = lines[:max_lines]
    last, ell = kept[-1], "…"
    while last and w(last + ell) > max_px:
        last = last[:-1]
    kept[-1] = (last + ell) if last else ell
    return "\n".join(kept)


# -- person name as coloured, thin-outlined text (sidebar / tab list) ---------
# Their assigned colour fills the glyph; a HAIRLINE black+white border keeps it
# legible on the dark sidebar and the lighter selected row — deliberately much
# thinner than the bold sound-chip outline (the user's "don't make it bulky").
_NAME_SS = 3           # supersample for crispness
_NAME_TEXT_PX = 14     # person-name glyph height
_GROUP_NAME_PX = 15    # group/tab-name glyph height (a touch larger, it's a header)
_NAME_WB = 3           # black inner border (supersampled px ≈ 1px on screen)
_NAME_WW = 2           # white outer border (supersampled px ≈ 0.7px on screen)
_thin_label_pil_cache: dict = {}
_thin_label_ctk_cache: "OrderedDict" = OrderedDict()


def _hex_to_rgba(hex_color: Optional[str]) -> tuple:
    try:
        h = (hex_color or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    except Exception:
        return (255, 255, 255, 255)


def _thin_outlined_image(text: str, color_hex: Optional[str], px_logical: int, bold: bool = True):
    """A CTkImage of ``text`` in ``color_hex`` (white if None) with a HAIRLINE
    black+white outline — legible on dark and lighter backgrounds without the
    bold halo of the sound chips. Used for person names and group/tab headers.
    Caches the PIL canvas AND the shared CTkImage. None on failure."""
    if not text:
        return None
    key = (text, color_hex or "", px_logical, bold)

    def _make():
        canvas = _thin_label_pil_cache.get(key)
        if canvas is None:
            fill = _hex_to_rgba(color_hex) if color_hex else (255, 255, 255, 255)
            align = "right" if _is_rtl(text) else "left"
            canvas = _outlined_text_pil(text, px_logical * _NAME_SS, _NAME_WB, _NAME_WW,
                                        fill=fill, align=align, bold=bold)
            if canvas is None:
                return None
            if len(_thin_label_pil_cache) > 512:
                _thin_label_pil_cache.clear()
            _thin_label_pil_cache[key] = canvas
        try:
            W, H = canvas.size
            return ctk.CTkImage(light_image=canvas, dark_image=canvas,
                                size=(max(1, W // _NAME_SS), max(1, H // _NAME_SS)))
        except Exception:
            return None

    return _ctk_cache_get(_thin_label_ctk_cache, key, _make)


def _person_name_image(name: str, color_hex: Optional[str], px_logical: int = _NAME_TEXT_PX):
    """Person name in their colour (or white) with a thin outline; bold."""
    return _thin_outlined_image(name, color_hex, px_logical, bold=True)


def _group_name_image(text: str, color_hex: Optional[str] = None):
    """Group/tab header in white (or its colour) with a thin outline; bold."""
    return _thin_outlined_image(text, color_hex, _GROUP_NAME_PX, bold=True)


class _SpeedScrollableFrame(ctk.CTkScrollableFrame):
    """A ``CTkScrollableFrame`` whose mouse-wheel speed follows the app-wide
    scroll-speed multiplier, so the People board scrolls exactly as fast as the
    main soundboard (CTk's default is a fixed ~20 units/notch, which felt slow).

    ``units_per_notch`` is a 0-arg callable returning the desired Tk scroll units
    per wheel notch (the app's ``_get_scroll_units_per_notch``); None → CTk default.
    """

    def __init__(self, *args, units_per_notch=None, on_shift_wheel=None, **kwargs):
        self._units_per_notch = units_per_notch
        # Optional callback(event) -> bool. When Shift is held and the wheel is
        # over this frame, it's offered the event first; returning True means it
        # handled it (e.g. adjusted a sound's volume) so we DON'T also scroll.
        self._on_shift_wheel = on_shift_wheel
        super().__init__(*args, **kwargs)

    def _mouse_wheel_all(self, event):  # overrides CTk's fixed-speed handler
        try:
            # Only act when the wheel is over THIS frame's canvas (CTk binds the
            # handler app-wide; this guard stops every scrollable frame reacting).
            if not self.check_if_master_is_canvas(event.widget):
                return
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            # Shift + wheel over a sound chip adjusts THAT sound's volume instead
            # of scrolling — same gesture as the main board. Check the raw event
            # modifier (Shift mask = 0x0001 on Windows; reliable even when Tk
            # doesn't hold keyboard focus) OR CTk's tracked flag, then offer it to
            # the callback, which returns True when it handled the event.
            shift = bool(getattr(event, "state", 0) & 0x0001) or getattr(self, "_shift_pressed", False)
            if shift and callable(self._on_shift_wheel):
                try:
                    if self._on_shift_wheel(event):
                        return "break"
                except Exception:
                    pass
            upn = None
            if callable(self._units_per_notch):
                try:
                    upn = int(self._units_per_notch())
                except Exception:
                    upn = None
            if upn is None:
                upn = 20  # CTk's Windows default (≈ delta/6 per notch)
            direction = -1 if delta > 0 else 1
            notches = max(1, abs(delta) // 120)
            amount = direction * notches * upn
            canvas = self._parent_canvas
            if getattr(self, "_shift_pressed", False):
                if canvas.xview() != (0.0, 1.0):
                    canvas.xview("scroll", amount, "units")
            elif canvas.yview() != (0.0, 1.0):
                canvas.yview("scroll", amount, "units")
        except Exception:
            # Never let a wheel event raise — fall back to CTk's own handling.
            try:
                super()._mouse_wheel_all(event)
            except Exception:
                pass


class PersonContext:
    """Everything ``person_board`` needs from the main app, passed as callables
    so the UI module never reaches into ``SoundboardApp`` internals."""

    def __init__(
        self,
        *,
        root: tk.Misc,
        persons: List[Person],
        play: Callable[[SoundSlot], float],
        is_running: Callable[[], bool],
        get_main_sounds: Callable[[], List[tuple]],
        persist: Callable[[], None],
        choose_color: Optional[Callable] = None,
        stop: Optional[Callable[[SoundSlot], None]] = None,
        preview: Optional[Callable[[SoundSlot], float]] = None,
        stop_preview: Optional[Callable[[], None]] = None,
        scroll_units: Optional[Callable[[], int]] = None,
        get_geometry: Optional[Callable[[str], Optional[str]]] = None,
        set_geometry: Optional[Callable[[str, str], None]] = None,
        set_volume: Optional[Callable[[SoundSlot], None]] = None,
    ):
        self.root = root
        self.persons = persons
        self.play = play
        self.is_running = is_running
        self.get_main_sounds = get_main_sounds
        self.persist = persist
        # Live-update the mixer volume of a currently-playing person sound when
        # the user Shift+wheels a chip (None → only the stored value changes).
        self.set_volume = set_volume
        # 0-arg callable → Tk scroll units per wheel notch (the app's scroll-speed
        # multiplier), so People scroll areas match the main board. None → default.
        self.scroll_units = scroll_units
        # Persist People window sizes/positions across sessions so the hub /
        # pop-outs reopen where you left them (keyed by a string like "hub").
        self._get_geometry = get_geometry
        self._set_geometry = set_geometry
        self._stop = stop
        self._preview = preview
        self._stop_preview = stop_preview
        # id(slot) of sounds currently playing to Discord (for the stop affordance).
        self.playing_ids: set = set()
        # Optional richer colour picker (the app's gradient studio). Signature:
        # choose_color(parent, initial_hex_or_None, on_pick). Falls back to the
        # built-in swatch popup when not provided (e.g. in tests).
        self._choose_color = choose_color
        # Live registry of open panels so an edit in one view refreshes the
        # other view of the same person (hub <-> pop-out).
        self._panels: List["PersonPanel"] = []
        # Drop-target registry for cross-window drag (main board -> group).
        # Maps a group's frame widget -> (person, group).
        self._drop_targets: dict = {}

        # Groups are SHARED across everyone. Consolidate existing per-person
        # groups into one canonical set on startup (dedupe by name, keep each
        # person's sounds, give every person the full set). Persists if changed.
        self.consolidate_groups()

    # -- shared groups (one set for everyone; only the sounds differ) -------
    def shared_groups(self) -> List[dict]:
        """Canonical ordered group definitions (id/name/color/emoji)."""
        for p in self.persons:
            return [{"id": g.id, "name": g.name, "color": g.color, "emoji": g.emoji}
                    for g in p.groups]
        return []

    def consolidate_groups(self, persist: bool = True) -> None:
        """Make every person share ONE group set (dedup by name), preserving the
        sounds each person already had in each group."""
        persons = self.persons
        if not persons:
            return
        # Match names case-insensitively (trimmed) so "Hi"/"HI" become ONE group
        # (the user's "no duplicates"); the first-seen casing is kept as the
        # display name. Genuinely different words stay separate.
        order: List[str] = []          # normalized keys, first-seen order
        canon: dict = {}               # key -> {id, name, color, emoji}
        for p in persons:
            for g in p.groups:
                nm = (g.name or "").strip() or "Group"
                key = nm.lower()
                if key not in canon:
                    canon[key] = {"id": g.id or _new_gid(), "name": nm,
                                  "color": g.color, "emoji": g.emoji}
                    order.append(key)
                else:
                    c = canon[key]
                    if not c["id"] and g.id:
                        c["id"] = g.id
                    if c["color"] is None and g.color is not None:
                        c["color"] = g.color
                    if c["emoji"] is None and g.emoji is not None:
                        c["emoji"] = g.emoji
        changed = False
        for p in persons:
            sounds_by_key: dict = {}
            collapsed_by_key: dict = {}
            for g in p.groups:
                key = ((g.name or "").strip() or "Group").lower()
                sounds_by_key.setdefault(key, []).extend(g.sounds)
                collapsed_by_key[key] = collapsed_by_key.get(key, False) or bool(g.collapsed)
            new_groups = [
                PersonGroup(
                    id=canon[key]["id"], name=canon[key]["name"], color=canon[key]["color"],
                    emoji=canon[key]["emoji"], collapsed=collapsed_by_key.get(key, False),
                    sounds=sounds_by_key.get(key, []),
                )
                for key in order
            ]
            # Detect a real change (count / ids / order) so we only persist when needed.
            if [g.id for g in p.groups] != [g.id for g in new_groups] or \
               len(p.groups) != len(new_groups):
                changed = True
            p.groups = new_groups
        if changed and persist:
            try:
                self.persist()
            except Exception:
                pass

    def ensure_groups_for(self, person: Person) -> None:
        """Give a (new) person the full shared group set (empty), in canonical
        order, keeping any sounds they already have per group."""
        defs = self.shared_groups()
        if not defs:
            return
        existing = {g.id: g for g in person.groups}
        person.groups = [
            existing.get(d["id"]) or PersonGroup(
                id=d["id"], name=d["name"], color=d["color"], emoji=d["emoji"], sounds=[])
            for d in defs
        ]

    def add_shared_group(self, name: str, emoji=None, color=None) -> None:
        name = (name or "").strip()
        if not name:
            return
        # Don't create a duplicate (case-insensitive) of an existing shared group.
        existing = {(g.name or "").strip().lower() for p in self.persons for g in p.groups}
        if name.lower() in existing:
            self.changed()  # already there — just refresh
            return
        gid = _new_gid()
        for p in self.persons:
            p.groups.append(PersonGroup(id=gid, name=name, emoji=emoji, color=color, sounds=[]))
        self.changed()

    def edit_shared_group(self, gid: str, name: str, emoji, color) -> None:
        for p in self.persons:
            for g in p.groups:
                if g.id == gid:
                    g.name, g.emoji, g.color = name, emoji, color
        self.changed()

    def delete_shared_group(self, gid: str) -> None:
        for p in self.persons:
            p.groups = [g for g in p.groups if g.id != gid]
        self.changed()

    def move_shared_group(self, gid: str, delta: int) -> None:
        for p in self.persons:
            groups = p.groups
            idx = next((i for i, g in enumerate(groups) if g.id == gid), None)
            if idx is None:
                continue
            j = idx + delta
            if 0 <= j < len(groups):
                groups[idx], groups[j] = groups[j], groups[idx]
        self.changed()

    # -- playback helpers --------------------------------------------------
    def stop(self, slot: SoundSlot):
        if self._stop is not None:
            try:
                self._stop(slot)
            except Exception:
                pass
        self.playing_ids.discard(id(slot))

    def preview(self, slot: SoundSlot) -> float:
        if self._preview is not None:
            try:
                return float(self._preview(slot) or 0.0)
            except Exception:
                return 0.0
        return 0.0

    def stop_preview(self):
        if self._stop_preview is not None:
            try:
                self._stop_preview()
            except Exception:
                pass

    def is_playing(self, slot: SoundSlot) -> bool:
        return id(slot) in self.playing_ids

    # -- colour picker (gradient studio if available, else swatches) -------
    def choose_color(self, parent, initial, on_pick):
        if self._choose_color is not None:
            try:
                self._choose_color(parent, initial, on_pick)
                return
            except Exception:
                pass
        pick_color(parent, initial, on_pick)

    # -- drop-target registry (cross-window drag) --------------------------
    def register_drop_target(self, widget, person: Person, group: PersonGroup):
        # Opportunistically drop dead frames so the registry can't grow without
        # bound as panels rebuild — but at most ~1x/second: the sweep does a
        # Tcl winfo_exists per entry and this is called once per group card
        # per rebuild (O(N²) Tcl calls during rebuild storms otherwise).
        now = time.time()
        if now - getattr(self, "_drop_sweep_at", 0.0) > 1.0:
            self._drop_sweep_at = now
            for w in [w for w in self._drop_targets if not _alive(w)]:
                self._drop_targets.pop(w, None)
        self._drop_targets[widget] = (person, group)

    def clear_drop_targets_for(self, person: Person):
        for w in [w for w, (p, _g) in self._drop_targets.items() if p is person]:
            self._drop_targets.pop(w, None)

    def find_drop_target(self, widget):
        """Walk up from ``widget`` to find a registered group drop frame."""
        cur = widget
        while cur is not None:
            hit = self._drop_targets.get(cur)
            if hit is not None:
                # Make sure the frame is still alive.
                try:
                    if cur.winfo_exists():
                        return hit
                except Exception:
                    return None
                self._drop_targets.pop(cur, None)
                return None
            try:
                cur = cur.master
            except Exception:
                break
        return None

    # -- window geometry persistence --------------------------------------
    def load_geometry(self, key: str) -> Optional[str]:
        """Last-saved Tk geometry string for window ``key`` ("hub"/"popout"), or None."""
        if self._get_geometry is not None:
            try:
                return self._get_geometry(key)
            except Exception:
                return None
        return None

    def save_geometry(self, key: str, geom: Optional[str]):
        if self._set_geometry is not None and geom:
            try:
                self._set_geometry(key, geom)
            except Exception:
                pass

    # -- observer plumbing -------------------------------------------------
    def register(self, panel: "PersonPanel"):
        self._panels.append(panel)

    def unregister(self, panel: "PersonPanel"):
        try:
            self._panels.remove(panel)
        except ValueError:
            pass

    def changed(self, person: Optional[Person] = None):
        """Persist and refresh every open panel showing ``person`` (or all).

        Also self-heals the registry by dropping panels whose widgets were
        destroyed (e.g. a hub/pop-out that closed), so it can't grow unbounded.
        """
        try:
            self.persist()
        except Exception:
            pass
        rebuilt = 0
        for panel in list(self._panels):
            try:
                alive = bool(panel.winfo_exists())
            except Exception:
                alive = False
            if not alive:
                self.unregister(panel)
                continue
            if person is None or panel.person is person:
                # Only the VISIBLE panel needs to repaint right now. The hub keeps
                # a cached (covered) panel per person; rebuilding all of them on
                # every shared-group edit would cost N * full-rebuild for nothing.
                # Hidden panels just get flagged and rebuild lazily the next time
                # they're shown (ensure_fresh).
                try:
                    if getattr(panel, "_showing", True):
                        # Several windows can show the same data (hub panel +
                        # pop-outs). Rebuild the FIRST one now for instant
                        # feedback; stagger the rest across event-loop ticks —
                        # N synchronous full rebuilds in one click was a
                        # primary "everything freezes" path.
                        if rebuilt == 0:
                            panel.rebuild()
                        else:
                            panel.after(30 * rebuilt, panel.rebuild)
                        rebuilt += 1
                    else:
                        panel.mark_dirty()
                except Exception:
                    # Never let one panel's failure break the others, but DO
                    # record it — a silently-swallowed rebuild error here looks
                    # exactly like "my groups disappeared / nothing happens".
                    logger.exception("PersonPanel.rebuild() failed in changed()")

    def collapse_changed(self, person: Person, group: PersonGroup):
        """A group's collapse state flipped — a VIEW-only change.

        Persist (debounced upstream) and apply the show/hide in place to every
        live panel of this person, WITHOUT a rebuild. Panels that haven't built
        this group yet (hidden/dirty) just get re-flagged.
        """
        try:
            self.persist()
        except Exception:
            pass
        for panel in list(self._panels):
            try:
                if not panel.winfo_exists():
                    self.unregister(panel)
                    continue
            except Exception:
                continue
            if panel.person is person:
                try:
                    panel.apply_collapse_state(group)
                except Exception:
                    logger.exception("apply_collapse_state failed in collapse_changed()")


# ---------------------------------------------------------------------------
# Small reusable dialogs
# ---------------------------------------------------------------------------
_FONT_CACHE: dict = {}


def _font(size_key: str = "size_sm", *, bold: bool = False) -> ctk.CTkFont:
    """Shared CTkFont per (size, weight). Every CTkFont registers a NAMED Tcl
    font that is never garbage-collected — creating one per widget per
    rebuild() grew the interpreter's font table without bound and slowed every
    font operation as the session aged. CTkFont objects are safely shareable
    across widgets (one Tk interpreter)."""
    key = (size_key, bold)
    f = _FONT_CACHE.get(key)
    if f is None:
        f = _FONT_CACHE[key] = ctk.CTkFont(
            family=FONTS["family"], size=FONTS[size_key], weight="bold" if bold else "normal"
        )
    return f


def prompt_text(parent: tk.Misc, title: str, label: str, initial: str = "") -> Optional[str]:
    """Modal single-line text prompt. Returns the text or None if cancelled."""
    dlg = ctk.CTkToplevel(parent)
    dlg.title(title)
    dlg.configure(fg_color=COLORS["bg_dark"])
    dlg.transient(parent)
    dlg.resizable(False, False)
    result: List[Optional[str]] = [None]

    ctk.CTkLabel(dlg, text=label, font=_font("size_md", bold=True),
                 text_color=COLORS["text_primary"]).pack(padx=20, pady=(18, 8), anchor="w")
    var = tk.StringVar(value=initial)
    entry = ctk.CTkEntry(dlg, textvariable=var, width=300, font=_font("size_md"),
                         fg_color=COLORS["bg_medium"], border_color=COLORS["border"])
    entry.pack(padx=20, pady=(0, 14), fill=tk.X)

    row = ctk.CTkFrame(dlg, fg_color="transparent")
    row.pack(padx=20, pady=(0, 16), anchor="e")

    def ok():
        txt = var.get().strip()
        if txt:
            result[0] = txt
        dlg.destroy()

    def cancel():
        result[0] = None
        dlg.destroy()

    ctk.CTkButton(row, text="Cancel", command=cancel, width=90, fg_color=COLORS["bg_light"],
                  hover_color=COLORS["bg_lighter"], font=_font()).pack(side=tk.LEFT, padx=(0, 8))
    ctk.CTkButton(row, text="OK", command=ok, width=90, fg_color=COLORS["blurple"],
                  hover_color=COLORS["blurple_hover"], font=_font(bold=True)).pack(side=tk.LEFT)

    dlg.bind("<Return>", lambda _e: ok())
    dlg.bind("<Escape>", lambda _e: cancel())
    _center_over(dlg, parent)
    entry.focus_set()
    entry.select_range(0, "end")
    dlg.grab_set()
    parent.wait_window(dlg)
    return result[0]


def pick_color(parent: tk.Misc, initial: Optional[str], on_pick: Callable[[Optional[str]], None]):
    """Tiny swatch popup (presets + 'Default'). Calls ``on_pick`` with a hex or None."""
    dlg = ctk.CTkToplevel(parent)
    dlg.title("Pick a colour")
    dlg.configure(fg_color=COLORS["bg_dark"])
    dlg.transient(parent)
    dlg.resizable(False, False)

    grid = ctk.CTkFrame(dlg, fg_color="transparent")
    grid.pack(padx=16, pady=16)

    def choose(c: Optional[str]):
        on_pick(c)
        dlg.destroy()

    cols = 4
    for i, col in enumerate(PALETTE):
        b = ctk.CTkButton(grid, text="", width=46, height=36, fg_color=col,
                          hover_color=col, corner_radius=8, command=lambda c=col: choose(c))
        b.grid(row=i // cols, column=i % cols, padx=5, pady=5)
        if initial and initial.lower() == col.lower():
            b.configure(border_width=3, border_color="white")
    ctk.CTkButton(dlg, text="Default colour", command=lambda: choose(None),
                  fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
                  font=_font()).pack(padx=16, pady=(0, 16), fill=tk.X)
    _center_over(dlg, parent)
    dlg.grab_set()


def _center_over(win: tk.Misc, parent: tk.Misc):
    win.update_idletasks()
    try:
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        ww, wh = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{px + (pw - ww) // 2}+{py + max(0, (ph - wh) // 3)}")
    except Exception:
        pass


def _alive(widget) -> bool:
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


_emoji_ctk_cache: "OrderedDict" = OrderedDict()


def emoji_image(emoji: Optional[str], size: int = 20):
    """A (shared, cached) CTkImage of the colour emoji glyph, or None."""
    if not emoji:
        return None

    def _make():
        try:
            pil = emoji_render.get_pil_image(emoji, size)
            if pil is not None:
                return ctk.CTkImage(light_image=pil, dark_image=pil, size=(size, size))
        except Exception:
            pass
        return None

    return _ctk_cache_get(_emoji_ctk_cache, (emoji, size), _make)


# Cache the expensive circle-cropped avatar PIL by (path, mtime, size) so the
# hub doesn't re-decode/crop/mask the same picture from disk on every rebuild
# and every sidebar row; the CTkImage wrapper is cached on the same key (the
# mtime in the key invalidates both when the picture file changes).
# The FULL decode happens once per (path, mtime) into a small square "master"
# (_avatar_master_cache); every requested size is a cheap resize of that.
# Before this, a phone-camera photo was fully decoded + LANCZOS'd once PER
# SIZE (sidebar row 26px, panel title 28px, dialogs 36px…) — ~50-200ms each,
# on the Tk thread, while the hub window was still invisible.
_avatar_pil_cache: dict = {}
_avatar_master_cache: dict = {}
_avatar_ctk_cache: "OrderedDict" = OrderedDict()
_AVATAR_MASTER_PX = 256  # ≥2x the largest requested size, so derived sizes stay crisp


def circle_avatar(image_path: Optional[str], size: int = 36):
    """A CTkImage of ``image_path`` cropped to a circle, or None."""
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        mtime = os.path.getmtime(image_path)
    except OSError:
        return None
    key = (image_path, mtime, size)

    def _make():
        out = _avatar_pil_cache.get(key)
        if out is None:
            try:
                from PIL import Image, ImageDraw
                mkey = (image_path, mtime)
                master = _avatar_master_cache.get(mkey)
                if master is None:
                    img = Image.open(image_path)
                    try:
                        # JPEG fast-path: decode at reduced resolution (huge
                        # photos drop ~8x in decode cost; final quality is set
                        # by the LANCZOS resize below, not the draft).
                        img.draft("RGB", (_AVATAR_MASTER_PX * 2, _AVATAR_MASTER_PX * 2))
                    except Exception:
                        pass
                    img = img.convert("RGBA")
                    # Center-crop to a square.
                    w, h = img.size
                    s = min(w, h)
                    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
                    if s > _AVATAR_MASTER_PX:
                        img = img.resize((_AVATAR_MASTER_PX, _AVATAR_MASTER_PX), Image.LANCZOS)
                    master = img
                    if len(_avatar_master_cache) > 64:
                        _avatar_master_cache.clear()
                    _avatar_master_cache[mkey] = master
                img = master.resize((size, size), Image.LANCZOS)
                mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
                out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                out.paste(img, (0, 0), mask)
            except Exception:
                return None
            if len(_avatar_pil_cache) > 256:
                _avatar_pil_cache.clear()
            _avatar_pil_cache[key] = out
        try:
            return ctk.CTkImage(light_image=out, dark_image=out, size=(size, size))
        except Exception:
            return None

    return _ctk_cache_get(_avatar_ctk_cache, key, _make)


def edit_entity_dialog(parent, ctx, title, *, name, emoji, color, image_path=None,
                       allow_picture=False, on_save):
    """Combined name + icon + colour (+ optional picture) editor for People/Groups.

    ``on_save`` is always called as ``on_save(name, emoji, color, image_path)``.
    """
    dlg = ctk.CTkToplevel(parent)
    dlg.title(title)
    dlg.configure(fg_color=COLORS["bg_dark"])
    dlg.transient(parent)
    dlg.resizable(False, False)
    state = {"emoji": emoji, "color": color, "img": None, "image_path": image_path}

    ctk.CTkLabel(dlg, text=_disp(title), font=_font("size_lg", bold=True),
                 text_color=COLORS["text_primary"]).pack(padx=20, pady=(16, 10), anchor="w")

    form = ctk.CTkFrame(dlg, fg_color="transparent")
    form.pack(padx=20, pady=(0, 6), fill=tk.X)

    ctk.CTkLabel(form, text="Name", width=64, anchor="w", font=_font(),
                 text_color=COLORS["text_secondary"]).grid(row=0, column=0, sticky="w", pady=6)
    name_var = tk.StringVar(value=name or "")
    name_entry = ctk.CTkEntry(form, textvariable=name_var, width=260, font=_font("size_md"),
                              fg_color=COLORS["bg_medium"], border_color=COLORS["border"])
    name_entry.grid(row=0, column=1, sticky="w", pady=6)

    # Icon row
    ctk.CTkLabel(form, text="Icon", width=64, anchor="w", font=_font(),
                 text_color=COLORS["text_secondary"]).grid(row=1, column=0, sticky="w", pady=6)
    icon_btn = ctk.CTkButton(form, text="", width=260, height=34, fg_color=COLORS["bg_medium"],
                             hover_color=COLORS["bg_light"], text_color=COLORS["text_primary"],
                             font=_font(), anchor="w")
    icon_btn.grid(row=1, column=1, sticky="w", pady=6)

    def refresh_icon():
        state["img"] = emoji_image(state["emoji"], 22)
        if state["img"] is not None:
            icon_btn.configure(image=state["img"], text="  Change icon")
        else:
            icon_btn.configure(image=None,
                               text=(f"  {state['emoji']}  Change icon" if state["emoji"]
                                     else "＋ Choose icon"))

    def choose_icon():
        res = pick_emoji(dlg)
        if res is not None:  # "" = cleared, None = cancelled
            state["emoji"] = res or None
            refresh_icon()
    icon_btn.configure(command=choose_icon)
    refresh_icon()

    # Colour row
    ctk.CTkLabel(form, text="Colour", width=64, anchor="w", font=_font(),
                 text_color=COLORS["text_secondary"]).grid(row=2, column=0, sticky="w", pady=6)
    swatch = ctk.CTkButton(form, text="Choose colour", width=260, height=34,
                           fg_color=state["color"] or COLORS["bg_medium"],
                           hover_color=state["color"] or COLORS["bg_light"],
                           text_color=(get_text_color_for_bg(state["color"]) if state["color"]
                                       else COLORS["text_primary"]),
                           font=_font())
    swatch.grid(row=2, column=1, sticky="w", pady=6)

    def on_color(c):
        state["color"] = c
        swatch.configure(fg_color=c or COLORS["bg_medium"], hover_color=c or COLORS["bg_light"],
                         text_color=(get_text_color_for_bg(c) if c else COLORS["text_primary"]))
    swatch.configure(command=lambda: ctx.choose_color(dlg, state["color"], on_color))

    # Picture row (people only): pick an image shown as a circular avatar.
    if allow_picture:
        ctk.CTkLabel(form, text="Picture", width=64, anchor="w", font=_font(),
                     text_color=COLORS["text_secondary"]).grid(row=3, column=0, sticky="w", pady=6)
        pic_btn = ctk.CTkButton(form, text="", width=260, height=40,
                                fg_color=COLORS["bg_medium"], hover_color=COLORS["bg_light"],
                                text_color=COLORS["text_primary"], font=_font(), anchor="w",
                                compound="left")
        pic_btn.grid(row=3, column=1, sticky="w", pady=6)

        def refresh_pic():
            state["avatar"] = circle_avatar(state["image_path"], 28)
            if state["avatar"] is not None:
                pic_btn.configure(image=state["avatar"], text="  Change picture")
            else:
                pic_btn.configure(image=None, text="＋ Choose picture")

        def choose_pic():
            from tkinter import filedialog
            p = filedialog.askopenfilename(
                parent=dlg, title="Choose a picture",
                filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")],
            )
            if p:
                state["image_path"] = p
                refresh_pic()

        pic_btn.configure(command=choose_pic)
        refresh_pic()
        # Right-click the picture button to clear it.
        pic_btn.bind("<Button-3>", lambda _e: (state.update(image_path=None), refresh_pic()))

    row = ctk.CTkFrame(dlg, fg_color="transparent")
    row.pack(padx=20, pady=(10, 16), anchor="e")

    def save():
        nm = name_var.get().strip()
        if not nm:
            messagebox.showwarning(title, "Please enter a name.", parent=dlg)
            return
        on_save(nm, state["emoji"], state["color"], state["image_path"])
        dlg.destroy()

    ctk.CTkButton(row, text="Cancel", command=dlg.destroy, width=90, fg_color=COLORS["bg_light"],
                  hover_color=COLORS["bg_lighter"], font=_font()).pack(side=tk.LEFT, padx=(0, 8))
    ctk.CTkButton(row, text="Save", command=save, width=90, fg_color=COLORS["blurple"],
                  hover_color=COLORS["blurple_hover"], font=_font(bold=True)).pack(side=tk.LEFT)

    dlg.bind("<Return>", lambda _e: save())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    _center_over(dlg, parent)
    name_entry.focus_set()
    dlg.grab_set()


def sound_settings_dialog(parent, slot: SoundSlot, on_change: Callable[[], None]):
    """Per-sound settings: volume / speed / loop — mirrors the main-board slot."""
    dlg = ctk.CTkToplevel(parent)
    dlg.title(f"Settings — {slot.name}")
    dlg.configure(fg_color=COLORS["bg_dark"])
    dlg.transient(parent)
    dlg.resizable(False, False)

    ctk.CTkLabel(dlg, text=_disp(f"⚙ {slot.name}"), font=_font("size_lg", bold=True),
                 text_color=COLORS["text_primary"]).pack(padx=20, pady=(16, 10), anchor="w")
    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(padx=20, pady=(0, 6), fill=tk.X)

    # Volume 0–200 %
    vol_var = tk.DoubleVar(value=max(0.0, min(2.0, slot.volume)) * 100)
    vol_lbl = ctk.CTkLabel(body, text=f"Volume: {int(vol_var.get())}%", font=_font(),
                           text_color=COLORS["text_secondary"], anchor="w")
    vol_lbl.pack(fill=tk.X)

    def on_vol(v):
        slot.volume = float(v) / 100.0
        vol_lbl.configure(text=f"Volume: {int(float(v))}%")
    ctk.CTkSlider(body, from_=0, to=200, variable=vol_var, command=on_vol,
                  progress_color=COLORS["blurple"]).pack(fill=tk.X, pady=(0, 10))

    # Speed 0.5–2.0×
    spd_var = tk.DoubleVar(value=max(0.5, min(2.0, slot.speed)))
    spd_lbl = ctk.CTkLabel(body, text=f"Speed: {spd_var.get():.2f}×", font=_font(),
                           text_color=COLORS["text_secondary"], anchor="w")
    spd_lbl.pack(fill=tk.X)

    def on_spd(v):
        slot.speed = float(v)
        spd_lbl.configure(text=f"Speed: {float(v):.2f}×")
    ctk.CTkSlider(body, from_=0.5, to=2.0, variable=spd_var, command=on_spd,
                  progress_color=COLORS["blurple"]).pack(fill=tk.X, pady=(0, 10))

    pitch_var = tk.BooleanVar(value=slot.preserve_pitch)
    ctk.CTkSwitch(body, text="Preserve pitch when changing speed", variable=pitch_var,
                  command=lambda: setattr(slot, "preserve_pitch", bool(pitch_var.get())),
                  font=_font(), progress_color=COLORS["blurple"]).pack(fill=tk.X, pady=(0, 8))

    loop_var = tk.BooleanVar(value=slot.loop)
    ctk.CTkSwitch(body, text="Loop until stopped", variable=loop_var,
                  command=lambda: setattr(slot, "loop", bool(loop_var.get())),
                  font=_font(), progress_color=COLORS["blurple"]).pack(fill=tk.X, pady=(0, 8))

    def close():
        try:
            on_change()
        finally:
            dlg.destroy()

    ctk.CTkButton(dlg, text="Done", command=close, fg_color=COLORS["blurple"],
                  hover_color=COLORS["blurple_hover"], font=_font(bold=True)).pack(
        padx=20, pady=(6, 16), fill=tk.X)
    dlg.protocol("WM_DELETE_WINDOW", close)
    _center_over(dlg, parent)
    dlg.grab_set()


# ---------------------------------------------------------------------------
# The reusable person panel (groups + sound chips)
# ---------------------------------------------------------------------------
class PersonPanel(ctk.CTkFrame):
    """Renders one Person's groups and sound buttons. Reused by the hub and the
    pop-out. Pass ``compact=True`` for the narrower floating window."""

    # Sound-chip size presets the user cycles with the "⤢ size" header button.
    # Larger = wider chips, fewer per row, more of the title visible; Smaller =
    # more chips per row. `_shared_size` is session-wide so the choice sticks as
    # you switch between people / open pop-outs.
    _SIZES = {
        # cols = sound tiles per row inside a group card (the real readability
        # knob). chip_w/trunc/height size each tile. All values are LOGICAL px
        # (DPI-independent — see _win_width, which converts the physical window
        # width to logical so these thresholds are correct at 125/150/175 %).
        # chip_w drives the wrap width (see _build_chip); height fits two lines
        # of the bold label + the length bar without a dark gap under the text.
        "L": {"cols": 1, "chip_w": 230, "height": 52},
        "M": {"cols": 2, "chip_w": 150, "height": 48},
        "S": {"cols": 3, "chip_w": 110, "height": 44},
    }
    _SIZE_ORDER = ["L", "M", "S"]
    _SIZE_LABEL = {"L": "Large", "M": "Medium", "S": "Small"}
    _shared_size = "L"

    # Group-card columns the user cycles with the "▦" header button: 0 = Auto
    # (fit as many as the window allows), or a forced 1-4. Fewer columns = wider
    # group cards ("resize groups"). Session-wide like the chip size.
    _GCOLS_ORDER = [0, 1, 2, 3, 4]
    _shared_gcols = 0

    @property
    def CHIP_W(self) -> int:
        return self._SIZES[getattr(self, "_size", "L")]["chip_w"]

    def _sz(self) -> dict:
        return self._SIZES[getattr(self, "_size", "L")]

    def __init__(self, master, person: Person, ctx: PersonContext, *, compact: bool = False):
        super().__init__(master, fg_color="transparent")
        self.person = person
        self.ctx = ctx
        self.compact = compact
        self._size = PersonPanel._shared_size  # current chip-size preset (L/M/S)
        self._gcols = PersonPanel._shared_gcols  # forced group columns (0 = Auto)
        self._nudge_after: Optional[str] = None
        self._body: Optional[ctk.CTkScrollableFrame] = None
        self._reflow_after: Optional[str] = None
        self._last_cols: int = -1  # chip columns last laid out (reflow guard)
        self._cols: int = 3        # current column count for the chip grid
        self._win = None           # toplevel we bound <Configure> to
        self._win_bind_id = None
        self._title_img = None
        # Chip drag state (drag a sound between groups / windows).
        self._drag = None
        self._drag_ghost = None
        self._suppress_click = False
        # Filtering: free-text (from the hub search box) + a single-group filter.
        self._filter_text = ""
        self._group_filter = "All groups"
        # Per-sound chip widgets (id(slot) -> dict) + transient preview state.
        self._chip_widgets: dict = {}
        self._previewing_id = None
        self._reset_timers: dict = {}
        self._progress_timers: dict = {}  # id(slot) -> after id for the length bar
        self._vol_timers: dict = {}       # id(slot) -> after id to clear the volume bar
        # Per-group widget registry (id(group) -> {card, caret, holder, built,
        # group, chip_cols}) so collapse/expand can show/hide a single group's
        # chips IN PLACE instead of rebuilding the whole panel.
        self._group_widgets: dict = {}
        # Set when the panel changed while hidden (hub cache); rebuilt lazily by
        # ensure_fresh() the next time it's shown.
        self._dirty = False
        # Whether this panel is the front/active view. The hub stacks all cached
        # panels in one cell and raises the active one (no unmap → no redraw →
        # instant switch), so winfo_ismapped() is True even for covered panels —
        # this explicit flag is the real "is the user looking at me" signal.
        # Pop-outs (own window) and standalone use stay True for their lifetime.
        self._showing = True
        # Widgets of this panel that skipped their resize redraw while COVERED
        # (the CTk defer patch parks them here instead of the toplevel's sweep
        # — repainting invisible panels was ~80% of the post-resize stall).
        # Drained by drain_covered_dirty() when the hub raises this panel.
        self._covered_dirty: set = set()
        # Panel-wide chip-build queue (one time-budgeted after() pump instead
        # of every group building its first chunk synchronously in rebuild()).
        self._build_jobs: list = []
        self._build_pump_after = None

        ctx.register(self)
        self.bind("<Destroy>", self._on_destroy)
        self._build_shell()
        self.rebuild()
        # Bind reflow to the WINDOW (after it exists). Window width never flaps
        # with the inner scrollbar, so this can't feed back into a rebuild loop.
        self.after(0, self._bind_window_resize)

    # -- lifecycle ---------------------------------------------------------
    def _bind_window_resize(self):
        try:
            self._win = self.winfo_toplevel()
            self._win_bind_id = self._win.bind("<Configure>", self._on_win_resize, add="+")
            real_cols = self._cols_for_width(self._win.winfo_width())
            # The first rebuild() in __init__ can run before the toplevel has its
            # real size (a pop-out is created then mapped → winfo_width()==1 → the
            # masonry collapses to one squished column, which reads as "my groups
            # disappeared"). Only if the real width now implies a DIFFERENT column
            # count do we re-lay-out once. When the panel was built into an
            # already-mapped host (the hub), the counts match and we skip the
            # second rebuild entirely — halving every person's first-view cost.
            if real_cols != self._cols:
                self._last_cols = real_cols
                self.rebuild()
            else:
                self._last_cols = real_cols
        except Exception:
            pass

    # -- hub cache plumbing ------------------------------------------------
    def mark_dirty(self):
        """Flag that this (hidden) panel's data changed; rebuild on next show."""
        self._dirty = True

    def ensure_fresh(self):
        """Rebuild only if flagged dirty while hidden — a cheap no-op otherwise."""
        if self._dirty:
            self.rebuild()
            # The rebuild replaced every widget — nothing parked remains valid.
            self._covered_dirty.clear()

    def drain_covered_dirty(self):
        """Repaint widgets whose resize redraw was skipped while this panel was
        covered (parked by the CTk defer patch). Called right after the hub
        raises this panel; chunked so the click never pays one big stall."""
        if not self._covered_dirty:
            return
        widgets = [w for w in self._covered_dirty]
        self._covered_dirty.clear()

        def _drain(ws=widgets):
            for w in ws[:24]:
                try:
                    if w.winfo_exists():
                        w._draw(no_color_updates=True)  # type: ignore[attr-defined]
                except Exception:
                    pass
            rest = ws[24:]
            if rest:
                try:
                    self.after(0, lambda: _drain(rest))
                except Exception:
                    pass

        _drain()

    def _on_destroy(self, event):
        if event.widget is self:
            self._cleanup()

    def _cleanup(self):
        """Unregister from the context + drop the window binding + cancel every
        pending after() timer (idempotent).

        Without the timer cancellation, a preview-progress tick (reschedules
        every 60ms), a reset/reflow/nudge callback could fire AFTER the pop-out
        is destroyed and crash with 'invalid command name' on the dead widget.
        """
        self.ctx.unregister(self)
        if self._win is not None and self._win_bind_id:
            try:
                self._win.unbind("<Configure>", self._win_bind_id)
            except Exception:
                pass
            self._win_bind_id = None
        # Cancel pending single-shot timers (reflow/nudge/chip-rewrap/build pump).
        for attr in ("_reflow_after", "_nudge_after", "_rewrap_after", "_build_pump_after"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        # Cancel per-sound preview-progress + reset + volume-bar timers.
        for timers in (getattr(self, "_progress_timers", None),
                       getattr(self, "_reset_timers", None),
                       getattr(self, "_vol_timers", None)):
            if not timers:
                continue
            for aid in list(timers.values()):
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
            timers.clear()

    def destroy(self):
        # Direct .destroy() (hub swap, explicit close) → unregister reliably;
        # the <Destroy> backstop covers parent-driven teardown.
        self._cleanup()
        super().destroy()

    # -- static shell (header + scroll body) -------------------------------
    def _build_shell(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill=tk.X, padx=10, pady=(8, 4))

        self._title = ctk.CTkLabel(header, text="", font=_font("size_lg", bold=True),
                                   text_color=COLORS["text_primary"])
        self._title.pack(side=tk.LEFT)

        ctk.CTkButton(header, text="＋ Group", width=80, height=28,
                      command=self._add_group, fg_color=COLORS["blurple"],
                      hover_color=COLORS["blurple_hover"], font=_font(bold=True),
                      corner_radius=8).pack(side=tk.RIGHT)
        ctk.CTkButton(header, text="✎", width=34, height=28, command=self._edit_person,
                      fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
                      font=_font(), corner_radius=8).pack(side=tk.RIGHT, padx=(0, 6))

        # Sound size: cycle Large / Medium / Small (wider chips show more of the
        # title; smaller packs more per row). Lets the user "enlarge / shorten"
        # the sound tiles to taste.
        self._size_btn = ctk.CTkButton(
            header, text=f"⤢ {self._SIZE_LABEL[self._size]}", width=92, height=28,
            command=self._cycle_size, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], font=_font(), corner_radius=8)
        self._size_btn.pack(side=tk.RIGHT, padx=(0, 6))

        # Group columns: cycle Auto / 1 / 2 / 3 / 4 — fewer columns = wider group
        # cards ("resize the groups"). Pairs with the chip-size control above to
        # let the user lay the board out however they like.
        self._gcols_btn = ctk.CTkButton(
            header, text=self._gcols_label(), width=74, height=28,
            command=self._cycle_gcols, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], font=_font(), corner_radius=8)
        self._gcols_btn.pack(side=tk.RIGHT, padx=(0, 6))

        # Per-group filter (show one group, or all).
        self._group_filter_var = tk.StringVar(value="All groups")
        self._group_menu = ctk.CTkOptionMenu(
            header, variable=self._group_filter_var, values=["All groups"], width=130, height=28,
            fg_color=COLORS["bg_medium"], button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"], font=_font(),
            command=self._on_group_filter,
        )
        self._group_menu.pack(side=tk.RIGHT, padx=(0, 6))

        self._body = _SpeedScrollableFrame(
            self, fg_color=COLORS["bg_darkest"],
            units_per_notch=getattr(self.ctx, "scroll_units", None),
            on_shift_wheel=self._shift_wheel_volume)
        self._body.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

    def set_filter(self, text: str):
        text = (text or "").strip().lower()
        if text == self._filter_text:
            return  # unchanged — don't pay a rebuild (hub calls this on every switch)
        self._filter_text = text
        self.rebuild()

    def _cycle_size(self):
        order = self._SIZE_ORDER
        self._size = order[(order.index(self._size) + 1) % len(order)]
        PersonPanel._shared_size = self._size  # remember for other panels this session
        try:
            self.ctx.save_geometry("chip_size", self._size)  # …and across launches
        except Exception:
            pass
        try:
            self._size_btn.configure(text=f"⤢ {self._SIZE_LABEL[self._size]}")
        except Exception:
            pass
        self.rebuild()
        # The size preset is session-wide. Push it to the other open/cached
        # panels: repaint the visible ones now, flag the hidden ones to re-lay-out
        # at the new size when next shown.
        label = f"⤢ {self._SIZE_LABEL[self._size]}"
        for panel in list(self.ctx._panels):
            if panel is self:
                continue
            try:
                panel._size = self._size
                try:
                    panel._size_btn.configure(text=label)
                except Exception:
                    pass
                if getattr(panel, "_showing", True):
                    panel.rebuild()
                else:
                    panel.mark_dirty()
            except Exception:
                pass

    def _gcols_label(self) -> str:
        return "▦ Auto" if not getattr(self, "_gcols", 0) else f"▦ {self._gcols}"

    def _cycle_gcols(self):
        order = self._GCOLS_ORDER
        cur = self._gcols if self._gcols in order else 0
        self._gcols = order[(order.index(cur) + 1) % len(order)]
        PersonPanel._shared_gcols = self._gcols  # session-wide, like chip size
        try:
            self.ctx.save_geometry("gcols", str(self._gcols))  # persist across launches
        except Exception:
            pass
        try:
            self._gcols_btn.configure(text=self._gcols_label())
        except Exception:
            pass
        self.rebuild()
        # Push to the other open/cached panels (repaint visible, flag hidden).
        label = self._gcols_label()
        for panel in list(self.ctx._panels):
            if panel is self:
                continue
            try:
                panel._gcols = self._gcols
                try:
                    panel._gcols_btn.configure(text=label)
                except Exception:
                    pass
                if getattr(panel, "_showing", True):
                    panel.rebuild()
                else:
                    panel.mark_dirty()
            except Exception:
                pass

    def _on_group_filter(self, value: str):
        self._group_filter = value
        self.rebuild()

    def _cols_for_width(self, width: int) -> int:
        # The reflow guard tracks the GROUP column count (what actually changes
        # the masonry). Derived from the live window width + size preset, so the
        # passed physical width is only a trigger, not the source of truth.
        return self._group_cols()

    def _on_win_resize(self, event):
        # Fires for any window geometry change. Only re-wrap when the user
        # actually changed the width enough to change the column COUNT.
        # Bindtags deliver every CHILD widget's <Configure> here too, so the
        # cheap pure-Python event.widget reject must run before any Tcl call.
        if event.widget is not self._win:
            return
        if self._body is None or not self._body.winfo_exists():
            return
        cols = self._cols_for_width(event.width)
        if cols == self._last_cols:
            return
        self._last_cols = cols
        # A covered (cached) panel must not re-lay-out on every resize of the hub
        # window — just remember it needs one and do it when next shown.
        if not getattr(self, "_showing", True):
            self.mark_dirty()
            return
        if self._reflow_after:
            try:
                self.after_cancel(self._reflow_after)
            except Exception:
                pass
        self._reflow_after = self.after(150, self._reflow_when_settled)

    def _reflow_when_settled(self):
        """Run the column-count rebuild only once the resize drag truly ended.

        Rebuilding mid-drag froze the drag for the full teardown+rebuild and
        its fresh widgets landed inside the active defer window (the popcorn
        repaint that read as "the design keeps breaking")."""
        self._reflow_after = None
        try:
            until = float(_RESIZE_STATE.get("until", 0.0))
            tops = _RESIZE_STATE.get("tops") or {}
            until = max(until, float(tops.get(self._win, 0.0)))
        except Exception:
            until = 0.0
        if time.time() < until:
            try:
                self._reflow_after = self.after(120, self._reflow_when_settled)
            except Exception:
                self._reflow_after = None
            return
        self.rebuild()

    # -- full redraw of the dynamic body -----------------------------------
    def rebuild(self):
        if self._body is None or not self._body.winfo_exists():
            return
        self._reflow_after = None
        self._dirty = False
        accent = self.person.color or COLORS["blurple"]
        self._title_img = circle_avatar(self.person.image_path, 28) or emoji_image(
            self.person.emoji, 24)
        if self._title_img is not None:
            self._title.configure(image=self._title_img, text=_disp(f" {self.person.name}"),
                                  compound="left")
        else:
            pref = f"{self.person.emoji} " if self.person.emoji else ""
            self._title.configure(image=None, text=_disp(f"{pref}{self.person.name}"))
        self._title.configure(text_color=accent if self.person.color else COLORS["text_primary"])

        # Column count is derived from the WINDOW width (stable), not the inner
        # scrollable frame (which flaps as its scrollbar toggles).
        try:
            self._cols = self._cols_for_width(self.winfo_toplevel().winfo_width())
            self._last_cols = self._cols
        except Exception:
            pass

        # Keep the group-filter dropdown in sync with the person's groups.
        try:
            values = ["All groups"] + [g.name for g in self.person.groups]
            self._group_menu.configure(values=values)
            if self._group_filter not in values:
                self._group_filter = "All groups"
                self._group_filter_var.set("All groups")
        except Exception:
            pass

        for w in self._body.winfo_children():
            w.destroy()
        # The old widgets are gone — drop their stale registry entries so paint /
        # progress callbacks can't touch destroyed widgets and the dicts can't
        # grow unbounded across rebuilds.
        self._chip_widgets = {}
        self._group_widgets = {}
        # Stale queued chip-build jobs reference destroyed holders (they no-op
        # via the gen/liveness checks) — drop them so the pump doesn't churn.
        self._build_jobs = []
        self._covered_dirty.clear()

        if not self.person.groups:
            ctk.CTkLabel(
                self._body,
                text="No groups yet.\nClick ＋ Group to add one like “Hi”, “Bye” or “lol”.",
                font=_font("size_md"), text_color=COLORS["text_muted"], justify="center",
            ).grid(row=0, column=0, pady=40)
            return

        # Which groups are visible (respecting the group + text filters).
        visible = []
        for group in self.person.groups:
            if self._group_filter != "All groups" and group.name != self._group_filter:
                continue
            if self._filter_text and not any(
                self._filter_text in s.name.lower() for s in group.sounds
            ):
                continue
            visible.append(group)

        # Groups this person has NO sounds in sink to the bottom (stable sort
        # keeps the shared base order within each section). Empty shared groups
        # still show — just last — so every person sees the same set.
        visible.sort(key=lambda g: 1 if not g.sounds else 0)

        if not visible:
            ctk.CTkLabel(
                self._body, text="No sounds match your search.",
                font=_font("size_md"), text_color=COLORS["text_muted"],
            ).grid(row=0, column=0, pady=30)
            return

        # Masonry: pack group cards into N responsive columns so the width is
        # used and the board stays compact (instead of one tall sparse column).
        gcols = self._group_cols()
        chip_cols = self._chip_cols_in_card(gcols)
        # Estimated LOGICAL play-button text width for this layout, derived
        # from the real window width (sidebar/margins → columns → tiles → the
        # play button minus its padding). Chips wrap their titles against this
        # up-front, so the post-layout <Configure> rewrap pass — which used to
        # re-measure + re-render every label ~120ms after launch — almost
        # always lands inside its ±4px skip band. The rewrap stays as the
        # corrector for whatever this estimate gets wrong (odd DPI, tiny
        # windows), so a bad estimate costs one extra render, never clipping.
        try:
            margins = 42 if self.compact else 264  # popout vs hub chrome+sidebar
            col_w = (self._win_width() - margins) // max(1, gcols) - 6
            tile_w = (col_w - 16) // max(1, chip_cols) - 6
            self._chip_avail_base = max(0, tile_w - 26 - 16)
        except Exception:
            self._chip_avail_base = 0
        col_frames = []
        for i in range(gcols):
            cf = ctk.CTkFrame(self._body, fg_color="transparent")
            cf.grid(row=0, column=i, sticky="new", padx=3)
            col_frames.append(cf)
        # Column weights for the masonry. CRITICAL: only the USED columns may
        # share the "grpcol" uniform group. Putting the empty trailing columns
        # (weight 0) in the SAME uniform group forces every uniform column to
        # the same width — which collapses the real columns to 1px so the whole
        # group grid stays UNMAPPED (the "my groups disappeared / nothing shows"
        # bug). Reset the unused columns with no uniform group at all.
        for i in range(8):
            try:
                if i < gcols:
                    self._body.grid_columnconfigure(i, weight=1, uniform="grpcol")
                else:
                    self._body.grid_columnconfigure(i, weight=0, uniform="")
            except Exception:
                pass

        # Shortest-column placement keeps the columns balanced in height.
        # Card builds STREAM through the panel-wide pump: building ~20 cards
        # (~8 CTk widgets each) synchronously was the bulk of the 1-2s
        # "groups load" stall when showing an uncached/dirty person.
        # Placement is computed up-front from the DATA, and the queue is
        # FIFO, so async card arrival can't disturb the masonry order.
        heights = [0] * gcols
        for group in visible:
            ci = heights.index(min(heights))
            if not group.sounds:
                # EMPTY shared group → cheap 3-4 widget stub row instead of the
                # full ~9-widget card. In the real config 149 of 220 cards
                # across a full warm-up were empty placeholders — two-thirds of
                # all card-build work. The stub stays visible, droppable and
                # clickable; the real card is built lazily on first interaction.
                heights[ci] += 1

                def _build_stub(group=group, parent=col_frames[ci]):
                    if not _alive(parent):
                        return
                    stub = self._build_group_stub(parent, group, chip_cols)
                    stub.pack(fill=tk.X, pady=(0, 8))

                self._enqueue_build(_build_stub)
                continue
            n = len(group.sounds) if not group.collapsed else 0
            heights[ci] += 2 + (n + chip_cols - 1) // max(1, chip_cols)  # rough row estimate

            def _build_card(group=group, parent=col_frames[ci]):
                if not _alive(parent):
                    return
                card = self._build_group(parent, group, chip_cols)
                card.pack(fill=tk.X, pady=(0, 8))

            self._enqueue_build(_build_card)

        # Force a redraw of the freshly built chips shortly after layout. CTk
        # defers per-widget _draw while a resize is in flight (the main-window
        # perf patch); a rebuild that lands in that window can leave chips blank
        # ("empty boxes"). This nudge guarantees they paint regardless.
        self._schedule_nudge()

    def _schedule_nudge(self):
        # The forced per-chip _draw() is ONLY needed when a rebuild lands inside
        # the window-resize defer window (CTk skips draws then → blank "empty
        # box" chips). Click/edit/collapse-driven rebuilds happen outside any
        # resize, so we skip the nudge there — that forced second draw pass was a
        # big chunk of the old per-rebuild cost.
        try:
            until = float(_RESIZE_STATE.get("until", 0.0))
            tops = _RESIZE_STATE.get("tops") or {}
            try:
                until = max(until, float(tops.get(self.winfo_toplevel(), 0.0)))
            except Exception:
                pass
            if time.time() >= until:
                return
        except Exception:
            return
        if self._nudge_after is not None:
            try:
                self.after_cancel(self._nudge_after)
            except Exception:
                pass
        self._nudge_after = self.after(40, self._nudge_redraw)

    def _nudge_redraw(self):
        self._nudge_after = None
        for w in list(self._chip_widgets.values()):
            for key in ("chip", "play", "menu"):
                widget = w.get(key)
                if widget is None:
                    continue
                try:
                    if widget.winfo_exists():
                        widget._draw(no_color_updates=False)  # type: ignore[attr-defined]
                except Exception:
                    pass

    # -- group layout helpers ---------------------------------------------
    GROUP_MIN_W = 300

    def _win_width(self) -> int:
        """LOGICAL width of the panel's window (physical px / CTk scaling).

        ``winfo_width()`` returns PHYSICAL pixels (e.g. 2175 for a 1450-logical
        window at 150 % DPI), but CTk widget sizes (chip_w etc.) are LOGICAL, so
        comparing them directly packed far too many tiny columns. Convert to
        logical here so every threshold lines up regardless of display scaling.
        """
        try:
            top = self.winfo_toplevel()
            phys = max(top.winfo_width(), 1)
            if phys <= 1:
                # PRE-MAP (hub/pop-out __init__ builds the first panel before
                # the window is on screen): winfo_width()==1 collapsed the
                # masonry to one squished column AND forced a second full
                # rebuild after mapping. The wm geometry request (set by the
                # window right before building us) already knows the real
                # width — and CTkToplevel.geometry() is LOGICAL units, which
                # is exactly what this function returns.
                try:
                    req = int(str(top.geometry()).split("x")[0])
                except Exception:
                    req = 0
                if req > 1:
                    return req
                return 900  # sane default — never lay out against width 1
            try:
                scaling = ctk.ScalingTracker.get_window_scaling(top)
            except Exception:
                scaling = max(1.0, top.winfo_fpixels("1i") / 96.0)
            return max(int(phys / max(scaling, 0.5)), 1)
        except Exception:
            return 900

    def _group_cols(self) -> int:
        # Forced column count ("resize groups"): fewer = wider cards. Never wider
        # than what the window can actually hold, so it can't push cards off-edge.
        forced = getattr(self, "_gcols", 0)
        if forced:
            return max(1, min(forced, self._auto_group_cols()))
        return self._auto_group_cols()

    def _auto_group_cols(self) -> int:
        # Fit as many group cards across the window as keeps each card wide
        # enough for `cols` readable tiles. Capped so it never gets silly.
        p = self._sz()
        card_target = p["cols"] * (p["chip_w"] + 10) + 40
        return max(1, min(6, (self._win_width() - 40) // card_target))

    def _chip_cols_in_card(self, gcols: int) -> int:
        # The size preset directly dictates tiles-per-row; the card is sized to
        # hold exactly that many, so titles stay readable.
        return max(1, self._sz()["cols"])

    def _build_group(self, parent, group: PersonGroup, chip_cols: int) -> ctk.CTkFrame:
        # A search match always expands the group so the hits are visible.
        collapsed = bool(group.collapsed) and not self._filter_text

        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_dark"], corner_radius=10)
        self.ctx.register_drop_target(card, self.person, group)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill=tk.X, padx=8, pady=(6, 4))

        caret = ctk.CTkLabel(head, text=("▶" if collapsed else "▼"), width=14,
                             font=_font(bold=True), text_color=COLORS["text_muted"])
        caret.pack(side=tk.LEFT)
        gimg = emoji_image(group.emoji, 18)
        if gimg is not None:
            il = ctk.CTkLabel(head, image=gimg, text="")
            il._gimg = gimg
            il.pack(side=tk.LEFT, padx=(2, 4))
        else:
            ctk.CTkLabel(head, text="●", text_color=group.color or COLORS["text_muted"],
                         font=_font("size_md", bold=True)).pack(side=tk.LEFT, padx=(2, 4))
        gname_txt = f"{group.name}  ({len(group.sounds)})"
        gname_img = _group_name_image(gname_txt)
        if gname_img is not None:
            name_lbl = ctk.CTkLabel(head, image=gname_img, text="")
            name_lbl._gname_img = gname_img  # keep a ref so it isn't GC'd
        else:
            name_lbl = ctk.CTkLabel(head, text=_disp(gname_txt),
                                    font=_font("size_md", bold=True),
                                    text_color=COLORS["text_primary"])
        name_lbl.pack(side=tk.LEFT)
        # Clicking the header toggles collapse.
        for w in (head, caret, name_lbl):
            w.bind("<Button-1>", lambda _e, g=group: self._toggle_group_collapse(g))

        menu_btn = ctk.CTkButton(head, text="⋮", width=26, height=26,
                                 fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
                                 font=_font(), corner_radius=6)
        # NOTE: call _open_group_menu, NOT _group_menu — the latter name is the
        # group-filter CTkOptionMenu instance attribute (set in _build_shell),
        # which would shadow a same-named method and make the ⋮ button raise
        # "'CTkOptionMenu' object is not callable" (silent dead button).
        menu_btn.configure(command=lambda g=group, w=menu_btn: self._open_group_menu(g, w))
        menu_btn.pack(side=tk.RIGHT)
        ctk.CTkButton(head, text="＋", width=26, height=26,
                      command=lambda g=group: self._add_sound_menu(g),
                      fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
                      font=_font(bold=True), corner_radius=6).pack(side=tk.RIGHT, padx=(0, 6))

        # Register the group so collapse/expand can show/hide its chips IN PLACE
        # (no panel rebuild). Chips are built lazily on first expand; subsequent
        # toggles just pack_forget()/pack() the cached holder, so a collapsed
        # group also costs nothing to build until the user opens it.
        entry = {"card": card, "group": group, "caret": caret,
                 "holder": None, "built": False, "chip_cols": max(1, chip_cols)}
        self._group_widgets[id(group)] = entry
        if not collapsed:
            # Through the pump, NOT synchronously: the card job alone is ~8ms;
            # stacking the holder + first chips on top blew the tick budget.
            # FIFO keeps order sane (all shells land, then holders/chips fill),
            # and _expand_group_inplace no-ops if the card died in between.
            self._enqueue_build(lambda e=entry: self._expand_group_inplace(e))
        return card

    def _build_group_stub(self, parent, group: PersonGroup, chip_cols: int) -> ctk.CTkFrame:
        """A cheap stub row standing in for an EMPTY group's full card.

        Keeps the group visible (name + caret + colour/emoji), droppable
        (registered drop target) and clickable (left-click expands into the
        real card, right-click opens the group menu) — but skips the two
        ~4ms CTkButtons and the holder/hint widgets until actually needed.
        """
        stub = ctk.CTkFrame(parent, fg_color=COLORS["bg_dark"], corner_radius=10)
        self.ctx.register_drop_target(stub, self.person, group)
        caret = ctk.CTkLabel(stub, text="▶", width=14, font=_font(bold=True),
                             text_color=COLORS["text_muted"])
        caret.pack(side=tk.LEFT, padx=(8, 0), pady=5)
        gimg = emoji_image(group.emoji, 18)
        if gimg is not None:
            il = ctk.CTkLabel(stub, image=gimg, text="")
            il._gimg = gimg
            il.pack(side=tk.LEFT, padx=(2, 4), pady=5)
        gname_txt = f"{group.name}  (0)"
        gname_img = _group_name_image(gname_txt)
        if gname_img is not None:
            name_lbl = ctk.CTkLabel(stub, image=gname_img, text="")
            name_lbl._gname_img = gname_img  # keep a ref so it isn't GC'd
        else:
            name_lbl = ctk.CTkLabel(stub, text=_disp(gname_txt),
                                    font=_font("size_md", bold=True),
                                    text_color=COLORS["text_primary"])
        name_lbl.pack(side=tk.LEFT, padx=(4, 8), pady=5)
        entry = {"card": stub, "group": group, "caret": caret, "holder": None,
                 "built": False, "chip_cols": max(1, chip_cols), "stub": True}
        self._group_widgets[id(group)] = entry
        widgets = (stub, caret, name_lbl) if gimg is None else (stub, caret, il, name_lbl)
        for w in widgets:
            w.bind("<Button-1>", lambda _e, e=entry: self._stub_clicked(e))
            w.bind("<Button-3>", lambda _e, g=group, s=stub: self._open_group_menu(g, s))
        return stub

    def _stub_clicked(self, entry: dict):
        group = entry["group"]
        if group.collapsed:
            # Match the full-card header: expanding un-collapses (persisted,
            # for every view of this person). collapse_changed routes back
            # through apply_collapse_state → _expand_group_inplace, which
            # upgrades the stub to a real card.
            self._toggle_group_collapse(group)
        else:
            self._expand_group_inplace(entry)

    def _upgrade_stub(self, entry: dict):
        """Replace an empty-group stub row with the full card, in place."""
        stub = entry.get("card")
        if not _alive(stub):
            return
        group = entry["group"]
        parent = stub.master
        try:
            # _build_group replaces our _group_widgets entry with a full-card
            # one and (when expanded) enqueues its own chip build.
            card = self._build_group(parent, group, entry.get("chip_cols", 1))
            card.pack(fill=tk.X, pady=(0, 8), before=stub)
        except Exception:
            return
        try:
            stub.destroy()
        except Exception:
            pass

    def _expand_group_inplace(self, entry: dict):
        """Build (once) then show a group's chips — no panel rebuild."""
        if entry.get("stub"):
            # Empty-group stub → build the real card in place. _build_group
            # schedules the holder/hint expansion itself when un-collapsed,
            # so there is nothing further to do here.
            self._upgrade_stub(entry)
            return
        group = entry["group"]
        card = entry["card"]
        if not _alive(card):
            return
        holder = entry.get("holder")
        if holder is None or not _alive(holder):
            holder = ctk.CTkFrame(card, fg_color="transparent")
            entry["holder"] = holder
            entry["built"] = False
        if not entry["built"]:
            sounds = (
                [s for s in group.sounds if self._filter_text in s.name.lower()]
                if self._filter_text else list(group.sounds)
            )
            if not sounds:
                ctk.CTkLabel(holder, text="No sounds — ＋ or drag one here",
                             font=_font("size_sm"), text_color=COLORS["text_muted"]).pack(anchor="w")
            else:
                cols = max(1, entry["chip_cols"])
                for c in range(cols):
                    holder.grid_columnconfigure(c, weight=1, uniform="chip")
                # Build chips in small chunks across event-loop ticks so a group
                # with many sounds can't freeze the UI thread. A frozen UI thread
                # also stalls the app's global keyboard hook -> system-wide key
                # lag (this is what made Shift stop working while a person was
                # open). A generation token + liveness check cancel an in-flight
                # build if the panel rebuilds or the holder is destroyed. Masonry
                # placement is COUNT-based (see rebuild), so async chip arrival
                # does not disturb the column layout.
                gen = entry.get("_chip_gen", 0) + 1
                entry["_chip_gen"] = gen
                # 1 per job: the pump's budget is only checked BETWEEN jobs, so
                # the job itself must fit the ~8ms tick. A warm chip is ~4-9ms
                # but a COLD one (first open: PIL wrap-measure + supersampled
                # label render) is ~25-40ms — 3-chip jobs hit 75-120ms ticks on
                # launch (measured p90 28ms, max 71ms). One chip per job keeps
                # every tick honest; the after(1) round-trip overhead is noise.
                _CHUNK = 1

                def _build_chunk(start, _gen=gen):
                    if entry.get("_chip_gen") != _gen or not _alive(holder):
                        return
                    end = min(start + _CHUNK, len(sounds))
                    for i in range(start, end):
                        try:
                            self._build_chip(holder, group, sounds[i]).grid(
                                row=i // cols, column=i % cols,
                                padx=3, pady=3, sticky="ew")
                        except Exception:
                            pass
                    if end < len(sounds):
                        self._enqueue_build(lambda: _build_chunk(end))

                # Through the panel-wide pump — rebuild() calls this for EVERY
                # expanded group, and the old synchronous first chunk meant
                # "6 chips × N groups in one tick" (the real first-view freeze;
                # the per-group after(1) pacing only helped within one group).
                self._enqueue_build(lambda: _build_chunk(0))
            entry["built"] = True
        try:
            if not holder.winfo_ismapped():
                holder.pack(fill=tk.X, padx=8, pady=(0, 8))
            entry["caret"].configure(text="▼")
        except Exception:
            pass

    def _enqueue_build(self, job) -> None:
        """Add a chip-build job to the panel-wide queue and ensure the pump runs."""
        self._build_jobs.append(job)
        if self._build_pump_after is None:
            try:
                self._build_pump_after = self.after(1, self._drain_build_jobs)
            except Exception:
                self._build_pump_after = None

    def _drain_build_jobs(self):
        """Run queued chip-build jobs for one ~14-16ms frame, then yield.

        Keeps every tick under a frame budget no matter how many groups a
        person has — the UI (and the global keyboard hook) never stalls.
        Geometry/first-draw backlog is flushed on accumulated-work debt (see
        the inline policy note below). update_idletasks only runs idle
        callbacks (geometry + deferred draws); it can NOT re-enter this
        timer-driven pump. Never update() here."""
        self._build_pump_after = None
        jobs = self._build_jobs
        if not jobs:
            return
        # Windows' default timer granularity makes every after(1) round-trip
        # cost up to ~15ms of dead latency, so ticks must carry a meaningful
        # batch of (single-widget-sized) jobs — ~14-16ms ≈ one frame.
        showing = getattr(self, "_showing", True)
        t0 = time.perf_counter()
        deadline = t0 + (0.014 if showing else 0.016)
        while jobs and time.perf_counter() < deadline:
            job = jobs.pop(0)
            try:
                job()
            except Exception:
                pass
        # Flush policy (measured, not guessed). Three probed alternatives:
        # (a) never flush → the geometry + CTk first-draw backlog of every
        #     widget the pump created lands as ONE giant stall when something
        #     finally pumps idletasks (~1.6s after the first panel, ~1.5s per
        #     prewarmed panel — the original "extra slow on launch");
        # (b) flush every tick → quadratic: each update_idletasks() re-runs
        #     the scrollable-frame reflow cascade over the whole masonry so
        #     far (probe: 632ms of jobs vs 3,457ms of flush);
        # (c) bounded dooneevent(IDLE) draining → useless here, a SINGLE idle
        #     atom (the reflow cascade) is itself 240-400ms.
        # So: flush on accumulated-work debt. The visible panel flushes every
        # ~100ms of build work (progressive paint, worst stall ~400ms instead
        # of 1.6s); covered prewarm panels every ~300ms (nobody sees them —
        # only total background churn matters); and always once at the end.
        debt = getattr(self, "_flush_debt", 0.0) + (time.perf_counter() - t0)
        if not jobs or debt >= (0.10 if showing else 0.30):
            self._flush_debt = 0.0
            try:
                self.update_idletasks()
            except Exception:
                pass
        else:
            self._flush_debt = debt
        if jobs:
            try:
                self._build_pump_after = self.after(1, self._drain_build_jobs)
            except Exception:
                self._build_pump_after = None

    def _collapse_group_inplace(self, entry: dict):
        """Hide a group's chips in place (keep them for an instant re-expand)."""
        holder = entry.get("holder")
        if holder is not None and _alive(holder):
            try:
                holder.pack_forget()
            except Exception:
                pass
        try:
            entry["caret"].configure(text="▶")
        except Exception:
            pass

    def apply_collapse_state(self, group: PersonGroup):
        """Sync this panel to ``group.collapsed`` in place (the toggle may have
        happened in another open view of the same person)."""
        entry = self._group_widgets.get(id(group))
        # Bail (and rebuild later) when: the group isn't built here, this panel is
        # covered, OR this panel has an active text filter. Under a filter, groups
        # are FORCE-expanded to show their hits (_build_group line ~1096); honoring
        # an external collapse here would hide the very matches the user searched
        # for. mark_dirty() lets it rebuild correctly on next show / filter change.
        if entry is None or not getattr(self, "_showing", True) or self._filter_text:
            self.mark_dirty()
            return
        if group.collapsed:
            self._collapse_group_inplace(entry)
        else:
            self._expand_group_inplace(entry)

    # -- group collapse / menu / reorder ----------------------------------
    def _toggle_group_collapse(self, group: PersonGroup):
        # Under an active text filter, groups are force-expanded to show their
        # search hits, so a collapse toggle has no visible effect. Ignore it
        # rather than silently flipping + persisting a collapsed state the user
        # can't see (which would then surprise them once the search is cleared).
        if self._filter_text:
            return
        group.collapsed = not group.collapsed
        # View-only change: persist (debounced) + apply the show/hide in place to
        # every live view of this person. No panel rebuild → instant.
        self.ctx.collapse_changed(self.person, group)

    def _open_group_menu(self, group: PersonGroup, widget):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["bg_medium"], fg=COLORS["text_primary"],
                       activebackground=COLORS["blurple"], activeforeground="white", bd=0)
        menu.add_command(label=("Expand" if group.collapsed else "Collapse"),
                         command=lambda: self._toggle_group_collapse(group))
        menu.add_command(label="Edit (name/icon/colour)", command=lambda: self._edit_group(group))
        menu.add_command(label="＋ Add sound", command=lambda: self._add_sound_menu(group))
        menu.add_separator()
        idx = self.person.groups.index(group)
        if idx > 0:
            menu.add_command(label="Move up", command=lambda: self._move_group(group, -1))
        if idx < len(self.person.groups) - 1:
            menu.add_command(label="Move down", command=lambda: self._move_group(group, +1))
        menu.add_separator()
        menu.add_command(label="Delete group", command=lambda: self._delete_group(group))
        try:
            menu.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())
        finally:
            menu.grab_release()

    def _move_group(self, group: PersonGroup, delta: int):
        # Reorder the shared group for EVERYONE (groups are shared).
        self.ctx.move_shared_group(group.id, delta)

    def _build_chip(self, parent, group: PersonGroup, slot: SoundSlot) -> ctk.CTkFrame:
        p = self._sz()
        h = p["height"]
        # A person's colour themes ALL of their sound tiles (set it via Edit
        # person -> Colour). It takes precedence so picking a person colour makes
        # every one of their sounds that colour; clear it to fall back to per-
        # sound / per-group colours.
        bg = self.person.color or slot.color or group.color or COLORS["blurple"]
        fg = get_text_color_for_bg(bg)
        # Wrap a long name onto up to 2 lines so more of the title is readable.
        # Budget = estimated real tile width minus the ⋮ menu button + paddings
        # (and a leading emoji). rebuild() derives _chip_avail_base from the
        # actual window width so this first wrap usually already matches the
        # laid-out width and the +120ms rewrap pass becomes a no-op; fall back
        # to the preset's nominal chip_w when no estimate is available.
        # Measured in real pixels by _wrap_label so wrapping actually triggers
        # for bold/Hebrew titles a char-count never caught.
        base = getattr(self, "_chip_avail_base", 0)
        if base > 36:
            avail = max(36, base - (24 if slot.emoji else 0))
        else:
            avail = max(36, p["chip_w"] - 36 - (24 if slot.emoji else 0))
        label = _wrap_label(slot.name, avail)
        # The 18px emoji is only used by the NON-outlined fallback label — the
        # outlined path bakes the emoji into the label image at render size, so
        # rasterizing a second 18px variant per chip was pure launch waste.
        cimg = None

        # NOTE: no grid_propagate(False). The old fixed-size chip relied on the
        # parent grid for its width and on propagate-off for height; that combo
        # could leave a chip mis-sized to ~1/3 width (titles clipped) or, on a
        # rebuild, render blank ("empty boxes"). Instead the chip sizes to the
        # buttons' fixed height and stretches horizontally via the column weight.
        chip = ctk.CTkFrame(parent, fg_color=bg, corner_radius=8)
        chip.grid_columnconfigure(0, weight=1)

        # Label as an OUTLINED image (white text + black inner / white outer
        # border) so it's legible on any chip colour and any play state. If the
        # render is unavailable for any reason, fall back to a normal text button.
        label_img = _chip_label_image(label, slot.emoji)
        outlined = label_img is not None
        if outlined:
            play = ctk.CTkButton(
                chip, text="", image=label_img, compound="left", height=h,
                fg_color=bg, hover_color=bg, text_color=fg, corner_radius=8, anchor="w",
                command=lambda: self._chip_clicked(group, slot),
            )
            play._label_img = label_img  # keep a ref so it isn't GC'd
        else:
            cimg = emoji_image(slot.emoji, 18)  # fallback-only (see above)
            play = ctk.CTkButton(
                chip, text=(f"  {_disp(label)}" if cimg else _disp(label)), image=cimg,
                compound="left", height=h, fg_color=bg, hover_color=bg, text_color=fg,
                font=_font(bold=True), corner_radius=8, anchor="w",
                command=lambda: self._chip_clicked(group, slot),
            )
            if cimg is not None:
                play._cimg = cimg  # keep ref
        play.grid(row=0, column=0, sticky="ew", padx=(2, 0), pady=(2, 0))
        menu_btn = ctk.CTkButton(
            chip, text="⋮", width=24, height=h, fg_color=bg, hover_color=bg, text_color=fg,
            font=_font(bold=True), corner_radius=8,
            command=lambda: self._chip_menu(group, slot, menu_btn),
        )
        menu_btn.grid(row=0, column=1, sticky="ew", padx=(0, 2), pady=(2, 0))
        # Length bar — a thin track at the bottom that fills as the sound plays
        # (or previews). Always gridded (so there's no layout jitter) but fully
        # INVISIBLE when idle: BOTH the track and the fill are the chip colour,
        # so there's no stray dot/shadow. _start_progress / _show_chip_volume
        # switch the fill to a visible colour while active, then back to bg.
        prog = ctk.CTkProgressBar(chip, height=4, corner_radius=2,
                                  fg_color=bg, progress_color=bg)
        prog.set(0.0)
        prog.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(1, 3))

        self._chip_widgets[id(slot)] = {
            "play": play, "menu": menu_btn, "chip": chip, "prog": prog,
            "bg": bg, "fg": fg, "label": label, "cimg": cimg, "slot": slot,
            "outlined": outlined,
        }
        # Tag the chip so a Shift+wheel event (delivered to any child widget) can
        # walk up .master and find which sound it's over (see _shift_wheel_volume).
        chip._slot_ref = slot

        # Re-wrap the title to the play button's ACTUAL width once it's laid out.
        # A build-time estimate from chip_w can't match the masonry's real tile
        # width (which varies with window size / column count / DPI), so a long
        # name clipped instead of wrapping. This is cheap (cached render; only
        # re-renders when the wrapped text actually changes) and loop-free (the
        # tile width is fixed by the grid, not by the label image).
        if outlined:
            play._wrap_state = {"slot": slot, "emoji": slot.emoji, "label": label}
            # Seed the rewrap's change-detector with the width we just wrapped
            # for: when the real laid-out width lands within its ±4px band the
            # post-layout rewrap skips the PIL re-measure entirely.
            play._last_avail = avail
            # NOTE: CTkButton.bind() forwards <Configure> to its internal canvas,
            # so event.widget is NOT the button — capture the button in a closure
            # and read its real width via winfo_width() instead.
            play.bind("<Configure>", lambda _e, pb=play: self._on_chip_configure(pb), add="+")

        # Click model (uses the app's mouseN naming: right-click = "mouse2",
        # middle-click = "mouse3"):
        #   left-click  -> play / stop (the button command)
        #   right-click -> edit menu (settings/rename/colour/move/remove)
        #   middle-click-> preview locally, or stop if it's already playing
        for w in (play, menu_btn):
            w.bind("<Button-3>", lambda e, g=group, s=slot: self._chip_menu(g, s, e.widget) or "break")
            w.bind("<Button-2>", lambda e, g=group, s=slot: self._chip_secondary(g, s))
        # Left-drag moves the sound between groups (press/motion/release).
        play.bind("<ButtonPress-1>", lambda e, s=slot, g=group: self._chip_press(e, g, s), add="+")
        play.bind("<B1-Motion>", self._chip_motion, add="+")
        play.bind("<ButtonRelease-1>", self._chip_release, add="+")

        if self.ctx.is_playing(slot):
            self._paint_chip(slot, "playing")
        return chip

    # -- chip menu (settings / rename / colour / icon / move / remove) -----
    def _chip_menu(self, group: PersonGroup, slot: SoundSlot, widget):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["bg_medium"], fg=COLORS["text_primary"],
                       activebackground=COLORS["blurple"], activeforeground="white", bd=0)
        menu.add_command(label="⚙ Settings (volume/speed/loop)",
                         command=lambda: self._open_sound_settings(group, slot))
        menu.add_command(label="Rename", command=lambda: self._rename_sound(group, slot))
        menu.add_command(label="Colour", command=lambda: self._recolor_sound(group, slot))
        menu.add_command(label="Icon", command=lambda: self._icon_sound(group, slot))
        others = [g for g in self.person.groups if g is not group]
        if others:
            move = tk.Menu(menu, tearoff=0, bg=COLORS["bg_medium"], fg=COLORS["text_primary"],
                           activebackground=COLORS["blurple"], activeforeground="white", bd=0)
            for tgt in others:
                move.add_command(label=_disp(tgt.name),
                                 command=lambda s=slot, src=group, dst=tgt: self._move_sound(src, dst, s))
            menu.add_cascade(label="Move to group", menu=move)
        menu.add_separator()
        menu.add_command(label="Remove", command=lambda: self._remove_sound(group, slot))
        try:
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _icon_sound(self, group: PersonGroup, slot: SoundSlot):
        res = pick_emoji(self.winfo_toplevel())
        if res is not None:
            slot.emoji = res or None
            self.ctx.changed(self.person)

    # -- chip click vs drag ------------------------------------------------
    def _chip_clicked(self, group: PersonGroup, slot: SoundSlot):
        if self._suppress_click:
            self._suppress_click = False
            return
        # Toggle: click a playing sound to stop it.
        if self.ctx.is_playing(slot):
            self.ctx.stop(slot)
            self._cancel_reset(slot)
            self._cancel_progress(slot)
            self._paint_chip(slot, "idle")
            return
        self._play(group, slot)

    def _chip_press(self, event, group: PersonGroup, slot: SoundSlot):
        self._drag = {"group": group, "slot": slot, "x": event.x_root, "y": event.y_root}
        self._suppress_click = False
        self._drag_ghost = None

    def _chip_motion(self, event):
        d = getattr(self, "_drag", None)
        if not d:
            return
        if abs(event.x_root - d["x"]) + abs(event.y_root - d["y"]) < 8:
            return
        self._suppress_click = True  # movement => treat the release as a drag, not a play
        self._show_drag_ghost(d["slot"].name, event.x_root, event.y_root)

    def _chip_release(self, event):
        d = getattr(self, "_drag", None)
        self._drag = None
        self._hide_drag_ghost()
        if not d or not self._suppress_click:
            return  # was a click, handled by command
        # Find the group frame under the cursor (any open hub/pop-out window).
        try:
            target = self.winfo_containing(event.x_root, event.y_root)
        except Exception:
            target = None
        hit = self.ctx.find_drop_target(target)
        if hit is None:
            return
        person, dst_group = hit
        if person is not self.person:
            # Dropped onto a different person's group: copy a deep copy over.
            # Only the TARGET person changed — the drop copies, so the source
            # person's data is untouched (the old second changed(self.person)
            # ran a pointless full rebuild of every source-person panel).
            dst_group.sounds.append(SoundSlot.from_dict(d["slot"].to_dict()))
            self.ctx.changed(person)
            return
        if dst_group is not d["group"]:
            self._move_sound(d["group"], dst_group, d["slot"])

    def _show_drag_ghost(self, text: str, x: int, y: int):
        if self._drag_ghost is None:
            g = tk.Toplevel(self.winfo_toplevel())
            g.overrideredirect(True)
            g.attributes("-topmost", True)
            try:
                g.attributes("-alpha", 0.85)
            except Exception:
                pass
            tk.Label(g, text=_disp(text), bg=COLORS["blurple"], fg="white",
                     font=("Segoe UI", 9, "bold"), padx=10, pady=4).pack()
            self._drag_ghost = g
            self._ghost_last_move = 0.0
        # Throttle to ~60fps: Tk repositions the override-redirect Toplevel
        # synchronously, and motion events arrive far faster than the screen
        # refreshes — per-pixel .geometry() calls made the drag feel smeary.
        now = time.time()
        if now - getattr(self, "_ghost_last_move", 0.0) < 0.015:
            return
        self._ghost_last_move = now
        self._drag_ghost.geometry(f"+{x + 12}+{y + 12}")

    def _hide_drag_ghost(self):
        if getattr(self, "_drag_ghost", None) is not None:
            try:
                self._drag_ghost.destroy()
            except Exception:
                pass
            self._drag_ghost = None

    def _move_sound(self, src: PersonGroup, dst: PersonGroup, slot: SoundSlot):
        if slot in src.sounds:
            src.sounds.remove(slot)
        dst.sounds.append(slot)
        self.ctx.changed(self.person)

    # -- playback ----------------------------------------------------------
    def _play(self, group: PersonGroup, slot: SoundSlot):
        if not self.ctx.is_running():
            messagebox.showinfo("Soundboard", "Start the audio stream first!",
                                parent=self.winfo_toplevel())
            return
        try:
            dur = self.ctx.play(slot) or 0.0
        except Exception as e:
            messagebox.showerror("Playback", f"Could not play:\n{e}",
                                 parent=self.winfo_toplevel())
            return
        self.ctx.playing_ids.add(id(slot))
        self._paint_chip(slot, "playing")
        if dur > 0 and not slot.loop:
            self._schedule_reset(slot, dur)
            self._start_progress(slot, dur)

    def _chip_secondary(self, group: PersonGroup, slot: SoundSlot):
        """middle-click: stop if playing to Discord, else preview locally (toggle)."""
        if self.ctx.is_playing(slot):
            self.ctx.stop(slot)
            self._cancel_reset(slot)
            self._cancel_progress(slot)
            self._paint_chip(slot, "idle")
            return "break"
        if self._previewing_id == id(slot):
            self.ctx.stop_preview()
            self._cancel_progress(slot)
            self._set_preview(None)
            return "break"
        # Switch preview to this sound.
        self.ctx.stop_preview()
        self._set_preview(None)
        dur = self.ctx.preview(slot) or 0.0
        self._set_preview(id(slot))
        self._paint_chip(slot, "preview")
        if dur > 0:
            self._start_progress(slot, dur)
            self.after(int(dur * 1000) + 150, lambda sid=id(slot): self._auto_clear_preview(sid))
        return "break"

    def _set_preview(self, sid):
        old = self._previewing_id
        self._previewing_id = sid
        if old is not None and old != sid:
            w = self._chip_widgets.get(old)
            if w and w["play"].winfo_exists():
                self._cancel_progress(w["slot"])
                self._paint_chip(w["slot"], "idle")

    def _auto_clear_preview(self, sid):
        if self._previewing_id == sid:
            w = self._chip_widgets.get(sid)
            self._previewing_id = None
            if w and w["play"].winfo_exists():
                self._cancel_progress(w["slot"])
                self._paint_chip(w["slot"], "idle")

    def _on_chip_configure(self, play):
        """<Configure> fires for every visible chip on every pixel of a live
        People-window resize. The actual re-wrap does PIL text measurement +
        a supersampled label re-render per chip, which froze resizes — so this
        handler only collects the dirty chip and a single shared ~120ms timer
        flushes them in one pass."""
        pending = getattr(self, "_rewrap_pending", None)
        if pending is None:
            pending = self._rewrap_pending = set()
        pending.add(play)
        if getattr(self, "_rewrap_after", None) is None:
            try:
                self._rewrap_after = self.after(120, self._flush_chip_rewrap)
            except Exception:
                self._rewrap_after = None

    def _flush_chip_rewrap(self):
        self._rewrap_after = None
        pending = getattr(self, "_rewrap_pending", None)
        if not pending:
            return
        # Mid-drag the PIL measure + supersampled re-render of every visible
        # chip every ~120ms was exactly the resize stutter the defer patch
        # exists to kill — wait until the drag settles. Covered panels keep
        # their pending set untouched; select() flushes it on raise.
        if not getattr(self, "_showing", True):
            return
        # While the build pump is still streaming cards, the masonry shifts on
        # every tick's geometry flush — chips would re-wrap (PIL measure +
        # supersampled re-render) over and over against transient widths.
        # Wait for the pump to drain; the final layout then gets ONE pass.
        if self._build_jobs or self._build_pump_after:
            try:
                self._rewrap_after = self.after(180, self._flush_chip_rewrap)
            except Exception:
                self._rewrap_after = None
            return
        try:
            until = float(_RESIZE_STATE.get("until", 0.0))
            tops = _RESIZE_STATE.get("tops") or {}
            until = max(until, float(tops.get(self.winfo_toplevel(), 0.0)))
        except Exception:
            until = 0.0
        if time.time() < until:
            try:
                self._rewrap_after = self.after(120, self._flush_chip_rewrap)
            except Exception:
                self._rewrap_after = None
            return
        chips = [p for p in pending if _alive(p)]
        pending.clear()
        if not chips:
            return
        # All chips share this panel's toplevel — resolve scaling once.
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self.winfo_toplevel())
        except Exception:
            try:
                scaling = max(1.0, self.winfo_fpixels("1i") / 96.0)
            except Exception:
                scaling = 1.0
        for play in chips:
            self._rewrap_chip(play, scaling)

    def _rewrap_chip(self, play, scaling: float):
        """Re-wrap a chip's title to the play button's real width (set by the
        grid once laid out), re-rendering the outlined label only when the
        wrapped text actually changes — so long names wrap to 2 lines instead
        of clipping, at whatever width the masonry/DPI produced."""
        st = getattr(play, "_wrap_state", None)
        if not st or not _alive(play):
            return
        width_px = play.winfo_width()
        if width_px <= 1:
            return  # not laid out yet
        # Convert the physical button width to logical, then drop the button's
        # internal padding (and room for a leading emoji) to get the text width.
        # Leave a comfortable margin so RTL text doesn't butt the right edge.
        width_logical = width_px / max(scaling, 0.5)
        avail = int(width_logical - 16 - (24 if st["emoji"] else 0))
        if avail < 30:
            return
        last = getattr(play, "_last_avail", -1)
        if abs(avail - last) < 4:
            return  # negligible change — skip the re-measure
        play._last_avail = avail
        new_label = _wrap_label(st["slot"].name, avail)
        if new_label == st["label"]:
            return
        st["label"] = new_label
        img = _chip_label_image(new_label, st["emoji"])
        if img is None:
            return
        try:
            play.configure(image=img)
            play._label_img = img  # keep a ref so it isn't GC'd
            w = self._chip_widgets.get(id(st["slot"]))
            if w is not None:
                w["label"] = new_label
        except Exception:
            pass

    # -- Shift + wheel volume (mirrors the main board) ---------------------
    def _shift_wheel_volume(self, event) -> bool:
        """Shift + wheel over a sound chip changes THAT sound's volume (no edit
        dialog). Returns True when handled so the scroll frame doesn't also
        scroll. Inverted like the main board: scroll UP lowers, DOWN raises."""
        # Find which chip (its slot) the wheel is over by walking up from the
        # widget that received the event to the tagged chip frame.
        slot = None
        w = getattr(event, "widget", None)
        for _ in range(8):
            if w is None:
                break
            ref = getattr(w, "_slot_ref", None)
            if ref is not None:
                slot = ref
                break
            w = getattr(w, "master", None)
        if slot is None:
            return False
        delta = getattr(event, "delta", 0)
        if not delta:
            return False
        notches = max(1, abs(delta) // 120)
        change = (-0.05 if delta > 0 else 0.05) * notches
        # Cap at 1.5 — the mixer clamps live volume there and the main board uses
        # the same ceiling, so going higher wouldn't actually get louder.
        slot.volume = max(0.0, min(1.5, slot.volume + change))
        # Live-update a currently-playing copy, then persist (debounced).
        try:
            setter = getattr(self.ctx, "set_volume", None)
            if callable(setter):
                setter(slot)
        except Exception:
            pass
        try:
            self.ctx.persist()
        except Exception:
            pass
        self._show_chip_volume(slot)
        return True

    def _show_chip_volume(self, slot: SoundSlot):
        """Briefly turn the chip's length bar into a volume gauge (green normal,
        yellow when boosted >100%), then restore it to its idle/playing look."""
        w = self._chip_widgets.get(id(slot))
        if not w:
            return
        prog = w.get("prog")
        if prog is None or not _alive(prog):
            return
        vol = slot.volume
        try:
            prog.configure(fg_color=COLORS["bg_darkest"],
                           progress_color=COLORS["yellow"] if vol > 1.0 else COLORS["green"])
            prog.set(max(0.0, min(1.0, vol / 1.5)))
        except Exception:
            return
        sid = id(slot)
        old = self._vol_timers.pop(sid, None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:
                pass

        def _restore():
            self._vol_timers.pop(sid, None)
            ww = self._chip_widgets.get(sid)
            if not ww:
                return
            p = ww.get("prog")
            if p is None or not _alive(p):
                return
            # If it's playing/previewing, that path owns the bar — leave it.
            if sid in self.ctx.playing_ids or self._previewing_id == sid:
                return
            try:
                p.configure(fg_color=ww["bg"], progress_color=ww["bg"])  # back to invisible
                p.set(0.0)
            except Exception:
                pass

        try:
            self._vol_timers[sid] = self.after(1200, _restore)
        except Exception:
            pass

    def _paint_chip(self, slot: SoundSlot, state: str):
        w = self._chip_widgets.get(id(slot))
        if not w:
            return
        play = w["play"]
        try:
            if not play.winfo_exists():
                return
            color = {"playing": COLORS["playing"], "preview": COLORS["preview"]}.get(state, w["bg"])
            if w.get("outlined"):
                # The outlined white label stays legible on every state colour, so
                # state is conveyed by the chip colour + the length bar — no text
                # rewrite needed (and the label is an image, not editable text).
                play.configure(fg_color=color, hover_color=color)
            else:
                prefix = "⏹ " if state == "playing" else ("🎧 " if state == "preview" else "")
                disp = _disp(w["label"])  # name reordered for RTL; icon stays on the left
                text = (f"  {prefix}{disp}" if w["cimg"] else f"{prefix}{disp}")
                play.configure(fg_color=color, hover_color=color, text=text)
        except Exception:
            pass

    def _schedule_reset(self, slot: SoundSlot, dur: float):
        self._cancel_reset(slot)
        sid = id(slot)

        def _reset():
            self._reset_timers.pop(sid, None)
            self.ctx.playing_ids.discard(sid)
            self._cancel_progress(slot)
            w = self._chip_widgets.get(sid)
            if w and w["play"].winfo_exists():
                self._paint_chip(w["slot"], "idle")

        try:
            self._reset_timers[sid] = self.after(int(dur * 1000) + 200, _reset)
        except Exception:
            pass

    def _cancel_reset(self, slot: SoundSlot):
        aid = self._reset_timers.pop(id(slot), None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                pass

    # -- length bar (fills as a sound plays / previews) --------------------
    def _start_progress(self, slot: SoundSlot, dur: float):
        if dur <= 0:
            return
        sid = id(slot)
        self._cancel_progress(slot)
        start = time.time()
        # Make the (idle-invisible) length bar visible for the duration.
        w = self._chip_widgets.get(sid)
        if w and _alive(w.get("prog")):
            try:
                w["prog"].configure(fg_color=w["bg"], progress_color=w["fg"])
            except Exception:
                pass

        def tick():
            w = self._chip_widgets.get(sid)
            if not w or "prog" not in w:
                self._progress_timers.pop(sid, None)
                return
            try:
                if not w["prog"].winfo_exists():
                    self._progress_timers.pop(sid, None)
                    return
                frac = (time.time() - start) / dur
                w["prog"].set(min(1.0, max(0.0, frac)))
            except Exception:
                self._progress_timers.pop(sid, None)
                return
            if frac < 1.0:
                self._progress_timers[sid] = self.after(60, tick)
            else:
                self._progress_timers.pop(sid, None)

        self._progress_timers[sid] = self.after(60, tick)

    def _cancel_progress(self, slot: SoundSlot):
        sid = id(slot)
        aid = self._progress_timers.pop(sid, None)
        if aid is not None:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        w = self._chip_widgets.get(sid)
        if w and "prog" in w:
            try:
                if w["prog"].winfo_exists():
                    w["prog"].set(0.0)
                    # Re-hide the bar (fill = chip colour) so no idle dot remains.
                    w["prog"].configure(fg_color=w["bg"], progress_color=w["bg"])
            except Exception:
                pass

    def _open_sound_settings(self, group: PersonGroup, slot: SoundSlot):
        # Volume/speed/loop/pitch are not rendered on the chip — persist only.
        # The old changed() here ran a full destroy+rebuild of every visible
        # panel of this person per settings tweak, for zero visual difference.
        sound_settings_dialog(self.winfo_toplevel(), slot,
                              on_change=lambda: self.ctx.persist())

    # -- person edits ------------------------------------------------------
    def _edit_person(self):
        def save(name, emoji, color, image_path):
            self.person.name = name
            self.person.emoji = emoji
            self.person.color = color
            self.person.image_path = image_path
            self.ctx.changed(self.person)
            # The hub sidebar renders name/colour/avatar too — refresh it, or
            # it shows the OLD identity until the hub is reopened (the hub
            # registers this hook; None when no hub is open).
            refresh = getattr(self.ctx, "refresh_sidebar", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
        edit_entity_dialog(self.winfo_toplevel(), self.ctx, "Edit person",
                           name=self.person.name, emoji=self.person.emoji,
                           color=self.person.color, image_path=self.person.image_path,
                           allow_picture=True, on_save=save)

    # -- group edits -------------------------------------------------------
    def _add_group(self):
        # New groups are created for EVERYONE (groups are shared).
        def save(name, emoji, color, _image):
            self.ctx.add_shared_group(name, emoji, color)
        edit_entity_dialog(self.winfo_toplevel(), self.ctx, "New group (for everyone)",
                           name="", emoji=None, color=None, on_save=save)

    def _edit_group(self, group: PersonGroup):
        # Name/icon/colour are shared — editing updates the group for everyone.
        def save(name, emoji, color, _image):
            self.ctx.edit_shared_group(group.id, name, emoji, color)
        edit_entity_dialog(self.winfo_toplevel(), self.ctx, "Edit group (for everyone)",
                           name=group.name, emoji=group.emoji, color=group.color, on_save=save)

    def _delete_group(self, group: PersonGroup):
        # Deleting removes the group (and its sounds) from EVERYONE. Warn with
        # the total sound count across all people.
        total = sum(
            len(g.sounds) for p in self.ctx.persons for g in p.groups if g.id == group.id
        )
        if not messagebox.askyesno(
            "Delete group for everyone",
            f"Delete “{group.name}” for ALL people"
            + (f" and its {total} sound(s)?" if total else "?"),
            parent=self.winfo_toplevel(),
        ):
            return
        self.ctx.delete_shared_group(group.id)

    # -- sound edits -------------------------------------------------------
    def _add_sound_menu(self, group: PersonGroup):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["bg_medium"], fg=COLORS["text_primary"],
                       activebackground=COLORS["blurple"], activeforeground="white", bd=0)
        menu.add_command(label="From file…", command=lambda: self._add_sound_from_file(group))
        menu.add_command(label="From main board…", command=lambda: self._add_sound_from_board(group))
        try:
            x = self.winfo_pointerx()
            y = self.winfo_pointery()
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _add_sound_from_file(self, group: PersonGroup):
        types = [("Audio", " ".join(SUPPORTED_FORMATS)), ("All files", "*.*")]
        path = filedialog.askopenfilename(parent=self.winfo_toplevel(),
                                          title="Add sound", filetypes=types)
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        group.sounds.append(SoundSlot(name=name, file_path=path, source_file_path=path))
        self.ctx.changed(self.person)

    def _add_sound_from_board(self, group: PersonGroup):
        sounds = []
        try:
            sounds = self.ctx.get_main_sounds()
        except Exception:
            sounds = []
        if not sounds:
            messagebox.showinfo("Add from board", "No sounds on the main board yet.",
                                parent=self.winfo_toplevel())
            return
        self._board_picker(group, sounds)

    def _board_picker(self, group: PersonGroup, sounds: List[tuple]):
        top = self.winfo_toplevel()
        dlg = ctk.CTkToplevel(top)
        dlg.title("Add from main board")
        dlg.configure(fg_color=COLORS["bg_dark"])
        dlg.geometry("420x520")
        dlg.transient(top)

        ctk.CTkLabel(dlg, text="Pick sounds to copy in", font=_font("size_md", bold=True),
                     text_color=COLORS["text_primary"]).pack(padx=14, pady=(14, 6), anchor="w")
        body = ctk.CTkScrollableFrame(dlg, fg_color=COLORS["bg_darkest"])
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Batch: mutate per click, but rebuild the panel ONCE when the dialog
        # closes — the panel sits BEHIND this grab_set modal, so per-click
        # full rebuilds (6 picks = 6 destroy-everything passes) were invisible
        # pure cost.
        added = {"n": 0}

        def add(slot: SoundSlot):
            # Deep-copy so later edits don't mutate the original main-board slot.
            copy = SoundSlot.from_dict(slot.to_dict())
            group.sounds.append(copy)
            added["n"] += 1

        def _close():
            try:
                dlg.destroy()
            except Exception:
                pass
            if added["n"]:
                self.ctx.changed(self.person)

        for label, slot in sounds:
            bg = slot.color or COLORS["bg_light"]
            ctk.CTkButton(body, text=_disp(label), anchor="w", fg_color=COLORS["bg_medium"],
                          hover_color=COLORS["bg_light"], text_color=COLORS["text_primary"],
                          font=_font(), corner_radius=6,
                          command=lambda s=slot: add(s)).pack(fill=tk.X, padx=4, pady=3)

        ctk.CTkButton(dlg, text="Done", command=_close, fg_color=COLORS["blurple"],
                      hover_color=COLORS["blurple_hover"], font=_font(bold=True)).pack(
            padx=10, pady=(0, 12), fill=tk.X)
        dlg.protocol("WM_DELETE_WINDOW", _close)
        _center_over(dlg, top)
        dlg.grab_set()

    def _rename_sound(self, group: PersonGroup, slot: SoundSlot):
        name = prompt_text(self.winfo_toplevel(), "Rename sound", "Name:", slot.name)
        if name:
            slot.name = name
            self.ctx.changed(self.person)

    def _recolor_sound(self, group: PersonGroup, slot: SoundSlot):
        self.ctx.choose_color(self.winfo_toplevel(), slot.color,
                              lambda c: (setattr(slot, "color", c), self.ctx.changed(self.person)))

    def _remove_sound(self, group: PersonGroup, slot: SoundSlot):
        try:
            group.sounds.remove(slot)
        except ValueError:
            pass
        self.ctx.changed(self.person)


def _restore_ui_prefs(ctx: PersonContext):
    """Apply persisted People-UI preferences (chip size / group columns) to the
    session-wide PersonPanel defaults before any panel is built. The values are
    stored through the same generic per-key store as the window geometries."""
    try:
        s = ctx.load_geometry("chip_size")
        if s in PersonPanel._SIZES:
            PersonPanel._shared_size = s
    except Exception:
        pass
    try:
        g = ctx.load_geometry("gcols")
        if g is not None and str(g).lstrip("-").isdigit() and int(g) in PersonPanel._GCOLS_ORDER:
            PersonPanel._shared_gcols = int(g)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pop-out: a single person in a floating, always-on-top window
# ---------------------------------------------------------------------------
class PersonPopout(ctk.CTkToplevel):
    def __init__(self, master, person: Person, ctx: PersonContext):
        super().__init__(master)
        self.person = person
        self.ctx = ctx
        _restore_ui_prefs(ctx)  # honour the saved chip-size/columns choice
        self.title(person.name)
        self.configure(fg_color=COLORS["bg_dark"])
        # Reopen pop-outs at the last size you used (positioned beside the hub).
        saved = ctx.load_geometry("popout")
        self.geometry(saved if (saved and "x" in saved) else "300x460")
        self.minsize(240, 320)

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill=tk.X, padx=8, pady=(8, 0))
        self._top_var = tk.BooleanVar(value=True)
        ctk.CTkSwitch(bar, text="On top", variable=self._top_var, command=self._toggle_top,
                      font=_font("size_xs"), progress_color=COLORS["blurple"]).pack(side=tk.RIGHT)

        self.panel = PersonPanel(self, person, ctx, compact=True)
        self.panel.pack(fill=tk.BOTH, expand=True)

        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._position_beside(master)
        self.bind("<Configure>", self._remember_size, add="+")

    def _remember_size(self, event=None):
        # Persist the pop-out SIZE (shared) so the next one opens the same size;
        # position stays the cascade-beside-the-hub placement. Use self.geometry()
        # (CTk-LOGICAL units) like the hub does — winfo_* is PHYSICAL px and would
        # get re-scaled on restore (opening ever-bigger at >100% DPI).
        if event is not None and event.widget is not self:
            return
        # Real SIZE change (not a move, not the first map) → defer this
        # window's CTk redraws for the duration of the drag (smudge fix).
        if event is not None:
            prev = getattr(self, "_lsb_last_size", None)
            size = (event.width, event.height)
            if size != prev:
                self._lsb_last_size = size
                if prev is not None:
                    arm_toplevel_resize_defer(self)
        try:
            size = self.geometry().split("+")[0].split("-")[0]  # "WxH" from "WxH+X+Y"
            if "x" in size and not size.startswith("1x1"):
                self.ctx.save_geometry("popout", size)
        except Exception:
            pass

    def _toggle_top(self):
        self.attributes("-topmost", bool(self._top_var.get()))

    def _position_beside(self, master):
        try:
            self.update_idletasks()
            x = master.winfo_rootx() + master.winfo_width() + 12
            y = master.winfo_rooty() + (len(self.ctx._panels) * 28)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _on_close(self):
        self._remember_size()
        # Hide, don't destroy — reopening the same person's pop-out is then a
        # ~50ms deiconify (gui.py's reuse path) instead of a full panel build.
        # While hidden, _showing=False parks rebuilds (changed() marks dirty).
        try:
            self.panel._showing = False
        except Exception:
            pass
        self.withdraw()

    def reopen(self):
        """Re-show a hidden pop-out and settle anything that changed meanwhile."""
        self.deiconify()
        try:
            if self.panel.winfo_exists():
                self.panel._showing = True
                self.panel.ensure_fresh()
                self.panel.drain_covered_dirty()
                self.panel._flush_chip_rewrap()
        except Exception:
            pass
        try:
            self.attributes("-topmost", bool(self._top_var.get()))
            self.lift()
            self.focus_force()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The hub: manage all people + a panel for the selected one
# ---------------------------------------------------------------------------
class PersonHub(ctk.CTkToplevel):
    # Typing in the search box fires a trace per keystroke; rebuilding the whole
    # sidebar + re-filtering the panel on every one is wasteful. Coalesce bursts.
    SEARCH_DEBOUNCE_MS = 180
    # Keep at most this many person panels alive at once (LRU). A panel is
    # ~200-400 CTk widgets; the cap exists only as a runaway guard. It MUST
    # comfortably exceed the number of people: at the old cap of 5 with 10
    # people, clicking person-to-person was a constant evict+full-rebuild
    # cycle — the "groups load for 1-2s on every switch" complaint. All
    # panels are also pre-built in the background (see _prewarm_panels) so
    # switching is a pure tkraise.
    MAX_CACHED_PANELS = 32

    def __init__(self, master, ctx: PersonContext, on_popout: Callable[[Person], None]):
        super().__init__(master)
        self.ctx = ctx
        _restore_ui_prefs(ctx)  # honour the saved chip-size/columns choice
        self.on_popout = on_popout
        self.selected: Optional[Person] = None
        self._panel: Optional[PersonPanel] = None
        # One PersonPanel per person, built on first view and kept alive (just
        # pack_forget()'d when hidden) so switching people is instant instead of
        # tearing down and rebuilding ~200 CTk widgets every click. Keyed by
        # id(person) (Person dataclasses aren't hashable). LRU-ordered: the most
        # recently selected sits at the end; least-recent get evicted past
        # MAX_CACHED_PANELS so memory stays bounded.
        self._panel_cache: "OrderedDict" = OrderedDict()
        # Pending debounced-search timer handle (after id), so we can cancel it.
        self._search_after: Optional[str] = None
        # Sidebar person rows, so selecting only restyles the two affected rows
        # rather than rebuilding the whole list.
        self._row_widgets: dict = {}

        self.title("People")
        self.configure(fg_color=COLORS["bg_dark"])
        # Reopen at the exact size/position it was last closed at (a sensible
        # default the first time) so it stops opening "big" and you don't have to
        # resize it every session.
        saved = ctx.load_geometry("hub")
        self._restored_geometry = bool(saved)
        self.geometry(saved or "900x620")
        self.minsize(560, 400)

        # Left: people list
        self.sidebar = ctk.CTkFrame(self, fg_color=COLORS["bg_darkest"], width=210)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=8)
        self.sidebar.pack_propagate(False)

        head = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        head.pack(fill=tk.X, padx=8, pady=(8, 4))
        ctk.CTkLabel(head, text="People", font=_font("size_md", bold=True),
                     text_color=COLORS["text_secondary"]).pack(side=tk.LEFT)
        ctk.CTkButton(head, text="＋ Add", width=66, height=26, command=self._add_person,
                      fg_color=COLORS["blurple"], hover_color=COLORS["blurple_hover"],
                      font=_font(bold=True), corner_radius=8).pack(side=tk.RIGHT)

        # Search filters BOTH the people list and the selected person's sounds.
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        ctk.CTkEntry(self.sidebar, textvariable=self._search_var,
                     placeholder_text="🔍 Search people & sounds", height=30,
                     fg_color=COLORS["bg_medium"], border_color=COLORS["border"],
                     font=_font()).pack(fill=tk.X, padx=8, pady=(0, 6))

        self.people_list = _SpeedScrollableFrame(
            self.sidebar, fg_color="transparent",
            units_per_notch=getattr(self.ctx, "scroll_units", None))
        self.people_list.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        # Right: header + person panel host
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=8)

        rhead = ctk.CTkFrame(right, fg_color="transparent")
        rhead.pack(fill=tk.X)
        self._pop_btn = ctk.CTkButton(rhead, text="⧉ Pop out", width=100, height=28,
                                     command=self._popout_selected, fg_color=COLORS["bg_light"],
                                     hover_color=COLORS["bg_lighter"], font=_font(),
                                     corner_radius=8)
        self._pop_btn.pack(side=tk.RIGHT)

        self.host = ctk.CTkFrame(right, fg_color=COLORS["bg_dark"], corner_radius=10)
        self.host.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        # All cached person panels share this one grid cell; selecting raises the
        # active one (tkraise) instead of unmap/remap, so switching never redraws.
        self.host.grid_rowconfigure(0, weight=1)
        self.host.grid_columnconfigure(0, weight=1)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Esc hides the hub too (same park-don't-destroy path as the ✕ button).
        self.bind("<Escape>", lambda _e: self._on_close())
        # Only auto-place beside the main window when we have NO remembered
        # position (otherwise honour where the user left it). Keep the size/pos
        # fresh in the config as the user moves/resizes (debounced write).
        if not self._restored_geometry:
            self._position_beside(master)
        self.bind("<Configure>", self._remember_geometry, add="+")
        # Panels (incl. pop-outs) call this after person-meta edits so the
        # sidebar can't go stale; stays wired while the hub is merely hidden,
        # cleared only in shutdown().
        self.ctx.refresh_sidebar = self.refresh_people
        self.refresh_people()
        if ctx.persons:
            # after(40), NOT after_idle: after_idle ran BEFORE the map/expose
            # redraw idle-callbacks, so the hub sat as an empty dark frame
            # until person #1's whole panel had streamed in. With a short
            # timer pending and nothing due, Tk goes idle right after CTk's
            # deferred deiconify — the sidebar/header PAINT first, then the
            # panel starts building at the real window width.
            self.after(40, self._select_initial)
            # Then pre-build EVERYONE ELSE's panel in the background (one at a
            # time, through each panel's ~8ms build pump) so clicking any
            # person is a pure tkraise — near-instant. RAM trade explicitly
            # chosen by the user. Start is gated on the ACTIVE panel's pump
            # having drained, so warming never competes with the first paint.
            self.after(800, self._maybe_start_prewarm)
        else:
            self._show_empty_hint()

    def _select_initial(self):
        """Select the person the user last had open (falls back to the first)."""
        if not self.ctx.persons:
            return
        target = self.ctx.persons[0]
        try:
            last = self.ctx.load_geometry("last_person")
        except Exception:
            last = None
        if last:
            for p in self.ctx.persons:
                if p.name == last:
                    target = p
                    break
        self.select(target)

    def _maybe_start_prewarm(self):
        """Start the background warm only once the active panel has finished
        building — a fixed 1500ms start used to land mid-stream on cold opens
        and stretched the first paint."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        panel = self._panel
        try:
            busy = panel is not None and _alive(panel) and (
                panel._build_jobs or panel._build_pump_after)
        except Exception:
            busy = False
        if busy:
            self.after(250, self._maybe_start_prewarm)
        else:
            self._prewarm_panels()

    def _remember_geometry(self, event=None):
        # Persist the hub's geometry whenever it changes (debounced downstream).
        # Ignore child-widget <Configure> and the pre-mapped 1x1 state.
        if event is not None and event.widget is not self:
            return
        # Real SIZE change (not a move, not the first map) → defer this
        # window's CTk redraws for the duration of the drag (smudge fix).
        if event is not None:
            prev = getattr(self, "_lsb_last_size", None)
            size = (event.width, event.height)
            if size != prev:
                self._lsb_last_size = size
                if prev is not None:
                    arm_toplevel_resize_defer(self)
        try:
            if self.winfo_width() > 1 and self.winfo_height() > 1:
                self.ctx.save_geometry("hub", self.geometry())
        except Exception:
            pass

    # -- positioning -------------------------------------------------------
    def _position_beside(self, master):
        try:
            self.update_idletasks()
            x = master.winfo_rootx() + 60
            y = master.winfo_rooty() + 40
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # -- search ------------------------------------------------------------
    def _on_search(self):
        # Debounced: coalesce a burst of keystrokes into one rebuild+filter so a
        # fast typist doesn't tear down/rebuild the sidebar on every character.
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.after(self.SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self):
        self._search_after = None
        q = self._search_var.get().strip().lower()
        self.refresh_people()
        if self._panel is not None and self._panel.winfo_exists():
            self._panel.set_filter(q)

    def _person_matches(self, person: Person, q: str) -> bool:
        if not q:
            return True
        if q in person.name.lower():
            return True
        for g in person.groups:
            if q in g.name.lower() or any(q in s.name.lower() for s in g.sounds):
                return True
        return False

    # -- people sidebar ----------------------------------------------------
    def refresh_people(self):
        for w in self.people_list.winfo_children():
            w.destroy()
        self._row_widgets = {}
        q = self._search_var.get().strip().lower() if hasattr(self, "_search_var") else ""
        for person in self.ctx.persons:
            if self._person_matches(person, q):
                self._build_person_row(person)
        # Guarantee exactly the selected row is highlighted after any rebuild.
        self._update_selection_highlight()

    def _update_selection_highlight(self, prev: Optional[Person] = None, new: Optional[Person] = None):
        """Reconcile EVERY row to the current selection (a handful of rows, so
        cheap and flicker-free). Touching only the prev/new rows left stale
        highlight boxes whenever a row had been rebuilt or a redraw was dropped —
        the 'several people highlighted at once' bug. This is self-correcting:
        exactly the row for ``self.selected`` is highlighted, always.

        Each configure() forces a full CTk redraw of that row, so rows whose
        state did NOT change are skipped (tracked on the row widget) — the
        reconcile stays bulletproof but a click repaints at most 2 rows."""
        for rw in list(self._row_widgets.values()):
            is_sel = rw["person"] is self.selected
            try:
                if not rw["row"].winfo_exists():
                    continue
                if getattr(rw["row"], "_lsb_sel", None) is is_sel:
                    continue  # already styled for this state — skip the redraw
                rw["row"]._lsb_sel = is_sel
                rw["row"].configure(
                    fg_color=COLORS["bg_light"] if is_sel else "transparent")
                rw["name_btn"].configure(font=_font(bold=is_sel))
            except Exception:
                pass

    def _build_person_row(self, person: Person):
        is_sel = person is self.selected
        row = ctk.CTkFrame(self.people_list,
                           fg_color=COLORS["bg_light"] if is_sel else "transparent",
                           corner_radius=8)
        row.pack(fill=tk.X, padx=2, pady=2)

        # Avatar: circular picture if set, else colour emoji, else a colour dot.
        avatar = circle_avatar(person.image_path, 26)
        if avatar is None:
            avatar = emoji_image(person.emoji, 22)
        if avatar is not None:
            lbl = ctk.CTkLabel(row, image=avatar, text="", width=28)
            lbl._img = avatar
            lbl.pack(side=tk.LEFT, padx=(8, 0))
        else:
            dot = person.color or COLORS["text_muted"]
            ctk.CTkLabel(row, text="●", text_color=dot, font=_font(bold=True),
                         fg_color="transparent", width=16).pack(side=tk.LEFT, padx=(8, 0))

        # Name in the person's assigned colour (white if none) with a HAIRLINE
        # black+white outline so it's legible on the dark list and the lighter
        # selected row — a thin version of the chip outline, not the bulky one.
        name_img = _person_name_image(person.name, person.color)
        if name_img is not None:
            name_btn = ctk.CTkButton(
                row, text="", image=name_img, anchor="w", height=34,
                fg_color="transparent", hover_color=COLORS["bg_lighter"],
                command=lambda p=person: self.select(p),
            )
            name_btn._name_img = name_img  # keep a ref so it isn't GC'd
        else:
            name_btn = ctk.CTkButton(
                row, text=_disp(person.name), anchor="w", height=34,
                fg_color="transparent", hover_color=COLORS["bg_lighter"],
                text_color=COLORS["text_primary"], font=_font(bold=is_sel),
                command=lambda p=person: self.select(p),
            )
        name_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        # Right-click a person → manage menu (edit / move / pop out / delete).
        for w in (row, name_btn):
            w.bind("<Button-3>", lambda e, p=person: self._person_menu(p, e.x_root, e.y_root))

        ctk.CTkButton(row, text="⋮", width=26, height=28,
                      command=lambda p=person, w=row: self._person_menu(
                          p, w.winfo_rootx() + 20, w.winfo_rooty() + 30),
                      fg_color="transparent", hover_color=COLORS["bg_lighter"],
                      text_color=COLORS["text_muted"], font=_font()).pack(side=tk.RIGHT, padx=(0, 2))

        # Remember the row so selection can restyle it without a full list rebuild.
        self._row_widgets[id(person)] = {"row": row, "name_btn": name_btn, "person": person}

    def _person_menu(self, person: Person, x: int, y: int):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["bg_medium"], fg=COLORS["text_primary"],
                       activebackground=COLORS["blurple"], activeforeground="white", bd=0)
        menu.add_command(label="Edit (name/icon/colour/picture)",
                         command=lambda: self._edit_person_dialog(person))
        menu.add_command(label="Pop out ⧉", command=lambda: self.on_popout(person))
        menu.add_separator()
        i = self.ctx.persons.index(person)
        if i > 0:
            menu.add_command(label="Move up", command=lambda: self._move_person(person, -1))
        if i < len(self.ctx.persons) - 1:
            menu.add_command(label="Move down", command=lambda: self._move_person(person, +1))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete_person(person))
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _move_person(self, person: Person, delta: int):
        ps = self.ctx.persons
        i = ps.index(person)
        j = i + delta
        if 0 <= j < len(ps):
            ps[i], ps[j] = ps[j], ps[i]
            self.ctx.changed()
            self.refresh_people()

    def _edit_person_dialog(self, person: Person):
        def save(name, emoji, color, image_path):
            person.name = name
            person.emoji = emoji
            person.color = color
            person.image_path = image_path
            self.ctx.changed(person)
            self.refresh_people()
        edit_entity_dialog(self, self.ctx, "Edit person", name=person.name, emoji=person.emoji,
                           color=person.color, image_path=person.image_path,
                           allow_picture=True, on_save=save)

    def _show_empty_hint(self):
        for w in self.host.winfo_children():
            w.destroy()
        # Host children include every cached panel — they're gone now, so drop
        # the (dead) cache entries too. Only reached with zero people.
        self._panel_cache.clear()
        self._panel = None
        ctk.CTkLabel(
            self.host,
            text="No people yet.\nClick ＋ Add to create someone,\nthen give them groups of sounds.",
            font=_font("size_md"), text_color=COLORS["text_muted"], justify="center",
        ).pack(expand=True)

    # -- selection ---------------------------------------------------------
    def select(self, person: Person):
        prev = self.selected
        if person is prev and self._panel is not None and _alive(self._panel):
            return
        self.selected = person
        # Remember for next launch so the hub opens on the person you use.
        try:
            self.ctx.save_geometry("last_person", person.name)
        except Exception:
            pass
        # Mark the panel we're leaving as covered + cancel any pending resize
        # reflow on it (a covered panel must not run a full rebuild; keep the
        # pending column change as a dirty flag so it reflows when next shown).
        if self._panel is not None and _alive(self._panel):
            self._panel._showing = False
            if self._panel._reflow_after:
                try:
                    self._panel.after_cancel(self._panel._reflow_after)
                except Exception:
                    pass
                self._panel._reflow_after = None
                self._panel.mark_dirty()
        # Reuse this person's cached panel, or build it once on first view. All
        # panels share ONE grid cell; raising swaps them with no unmap/redraw.
        # (Panels paint correctly on build now that the resize-defer patch leaves
        # non-main toplevels alone, so no hidden-build / update_idletasks dance.)
        panel = self._panel_cache.get(id(person))
        if panel is None or not _alive(panel):
            panel = PersonPanel(self.host, person, self.ctx, compact=False)
            panel.grid(row=0, column=0, sticky="nsew")
            self._panel_cache[id(person)] = panel
        # A select while the hub is WITHDRAWN (startup prebuild / hidden-hub
        # bookkeeping) gets covered-panel semantics: repaints park, the pump
        # uses its background flush cadence. reopen() flips it back showing.
        # wm_state is read synchronously, so a user click in a visible hub can
        # never be misclassified (deiconify sets 'normal' before any click).
        try:
            panel._showing = self.wm_state() != "withdrawn"
        except Exception:
            panel._showing = True
        # Mark this panel most-recently-used, then drop any stale panels past the
        # cap so the cache can't grow without bound as you click through people.
        self._panel_cache.move_to_end(id(person))
        self._evict_panels()
        q = self._search_var.get().strip().lower() if hasattr(self, "_search_var") else ""
        panel.set_filter(q)     # no-op if unchanged
        panel.ensure_fresh()    # rebuild only if it changed while hidden
        panel.tkraise()
        # Repaint anything that skipped its resize redraw while covered, and
        # finish any chip-label re-wraps that were parked for the same reason.
        panel.drain_covered_dirty()
        try:
            panel._flush_chip_rewrap()
        except Exception:
            pass
        self._panel = panel
        self._update_selection_highlight(prev, person)

    def _prewarm_panels(self):
        """Build the next missing person panel covered in the cache.

        Sequential: the next panel starts only once the previous one's build
        pump has drained, so background warming never overlaps the active
        panel's work for more than one ~8ms tick. After the warm-up, every
        person switch is a pure tkraise."""
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        target = None
        for person in self.ctx.persons:
            cached = self._panel_cache.get(id(person))
            if cached is None or not _alive(cached):
                target = person
                break
        if target is None:
            return  # everyone is cached — warm-up complete
        try:
            panel = PersonPanel(self.host, target, self.ctx, compact=False)
            panel._showing = False  # covered; select() flips it when raised
            panel.grid(row=0, column=0, sticky="nsew")
            self._panel_cache[id(target)] = panel
            # Least-recently-used end: a pre-warmed panel must never push a
            # genuinely-used one out of the cache.
            self._panel_cache.move_to_end(id(target), last=False)
            # Gridding stacked the new panel on TOP — keep the active one front.
            if self._panel is not None and _alive(self._panel):
                self._panel.tkraise()
        except Exception:
            logger.exception("panel pre-warm failed")
            return

        def _next_when_drained():
            try:
                if not self.winfo_exists():
                    return
                busy = _alive(panel) and (panel._build_jobs or panel._build_pump_after)
            except Exception:
                busy = False
            if busy:
                self.after(150, _next_when_drained)
            else:
                # Settle THIS panel's deferred layout work now, in the warm-up
                # slot. The pumps run on after(1) timers, which starve Tk's
                # idle queue — without this flush, 9 panels' pending geometry
                # piled up and the user's FIRST click paid ~2.4s for all of it.
                # (update_idletasks is the safe pump — never update().)
                try:
                    self.update_idletasks()
                except Exception:
                    pass
                self.after(80, self._prewarm_panels)

        self.after(150, _next_when_drained)

    def _evict_panels(self):
        """Drop least-recently-used cached panels beyond MAX_CACHED_PANELS.
        The active (on-screen) panel was just moved to the end, so it's never the
        eviction candidate at the front; the break is a belt-and-suspenders guard
        for the degenerate single-entry case. The actual widget destruction is
        deferred to after_idle — destroying a ~300-widget panel synchronously
        made the CLICK that selected a person visibly hitch."""
        doomed = []
        while len(self._panel_cache) > self.MAX_CACHED_PANELS:
            key, panel = next(iter(self._panel_cache.items()))
            if panel is self._panel:
                break
            self._panel_cache.pop(key, None)
            doomed.append(panel)
        if not doomed:
            return

        def _destroy_later():
            for panel in doomed:
                try:
                    panel.destroy()
                except Exception:
                    pass

        try:
            self.after_idle(_destroy_later)
        except Exception:
            _destroy_later()

    # -- person CRUD -------------------------------------------------------
    def _add_person(self):
        def save(name, emoji, color, image_path):
            person = Person(name=name, emoji=emoji, color=color, image_path=image_path)
            self.ctx.persons.append(person)
            self.ctx.ensure_groups_for(person)  # new person gets the shared groups
            self.ctx.changed()
            self.refresh_people()
            self.select(person)
        edit_entity_dialog(self, self.ctx, "New person", name="", emoji=None, color=None,
                           allow_picture=True, on_save=save)

    def _delete_person(self, person: Person):
        if not messagebox.askyesno("Delete person",
                                   f"Delete “{person.name}” and all their sounds?", parent=self):
            return
        # Drop this person's cached panel first so the follow-up changed()/select
        # can't rebuild a doomed panel.
        panel = self._panel_cache.pop(id(person), None)
        if panel is not None:
            if panel is self._panel:
                self._panel = None
            try:
                panel.destroy()
            except Exception:
                pass
        try:
            self.ctx.persons.remove(person)
        except ValueError:
            pass
        if self.selected is person:
            self.selected = None
        self.ctx.changed()
        self.refresh_people()
        if self.ctx.persons:
            self.select(self.ctx.persons[0])
        else:
            self._show_empty_hint()

    def _popout_selected(self):
        if self.selected is not None:
            self.on_popout(self.selected)

    def _on_close(self):
        """Hide, don't destroy. Rebuilding the hub from scratch cost seconds on
        EVERY open (window + sidebar + first panel + the long background warm),
        and destroying ~10 cached panels wasn't free either — parking the hub
        withdrawn makes every reopen a ~50ms deiconify (see reopen()). Panels
        stay registered with the context, so edits made while hidden flow in
        as usual: changed() marks non-showing panels dirty and ensure_fresh()
        settles them on reopen. Real destruction happens with the app root."""
        # Remember where/how big it was so it reopens the same.
        self._remember_geometry()
        # Cancel a pending debounced search so it can't fire while hidden.
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except Exception:
                pass
            self._search_after = None
        # The active panel is no longer what the user is looking at — park its
        # repaint/reflow work exactly like a covered cached panel.
        if self._panel is not None and _alive(self._panel):
            self._panel._showing = False
        self.withdraw()

    def reopen(self):
        """Bring the hidden hub back — the fast path for every open after the
        first. Reconciles anything that changed while it was withdrawn."""
        self.deiconify()
        # Sidebar first (people may have been added/renamed/recoloured from
        # pop-outs or the recording-editor tagging flow while hidden).
        self.refresh_people()
        panel = self._panel
        if panel is not None and _alive(panel):
            panel._showing = True
            panel.ensure_fresh()        # rebuild only if flagged dirty
            panel.drain_covered_dirty()
            try:
                panel._flush_chip_rewrap()
            except Exception:
                pass
        elif self.ctx.persons:
            self._select_initial()
        else:
            self._show_empty_hint()
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def shutdown(self):
        """Really tear the hub down (app exit): destroy every cached panel so
        their after-timers are cancelled, then the window itself."""
        for panel in list(self._panel_cache.values()):
            try:
                panel.destroy()
            except Exception:
                pass
        self._panel_cache.clear()
        self._panel = None
        try:
            if getattr(self.ctx, "refresh_sidebar", None) == self.refresh_people:
                self.ctx.refresh_sidebar = None
        except Exception:
            pass
        self.destroy()
