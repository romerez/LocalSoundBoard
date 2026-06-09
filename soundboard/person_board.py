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
import os
import time
import uuid
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)


def _new_gid() -> str:
    """Fresh stable id for a shared group."""
    return uuid.uuid4().hex[:8]

import customtkinter as ctk

from . import emoji_render
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
_OUTLINE_SS = 2          # supersample factor (render 2x, display 1x) for crisp text
_OUTLINE_TEXT_PX = 15    # logical glyph height
# HAIRLINE borders — just enough that the text never blends into the chip, NOT a
# thick cartoon outline. These are SUPERSAMPLED px (÷_OUTLINE_SS on screen), so
# wb=2 → ~1px dark edge, ww=1 → a faint ~0.5px light halo on screen.
_OUTLINE_WB = 2          # black inner border width (in supersampled px)
_OUTLINE_WW = 1          # white outer border width (in supersampled px)
_OUTLINE_FONTS = ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf")


@functools.lru_cache(maxsize=8)
def _outline_font(px: int):
    from PIL import ImageFont
    for path in _OUTLINE_FONTS:
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


@functools.lru_cache(maxsize=2048)
def _outlined_text_pil(text: str, px: int, wb: int, ww: int):
    """RGBA PIL image of ``text`` as white glyphs + black inner + white outer
    border (transparent elsewhere). ``text`` is LOGICAL. Returns None on failure."""
    try:
        from PIL import Image, ImageDraw
        font = _outline_font(px)
        if font is None or not text:
            return None
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = probe.textbbox((0, 0), text, font=font, stroke_width=wb + ww)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = wb + ww + 2
        img = Image.new("RGBA", (max(1, w + 2 * pad), max(1, h + 2 * pad)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ox, oy = pad - bbox[0], pad - bbox[1]
        white, black = (255, 255, 255, 255), (0, 0, 0, 255)
        # Concentric: widest white blob, then black blob, then the glyph — leaves
        # a black ring (wb) hugging the text and a white ring (ww) outside it.
        d.text((ox, oy), text, font=font, fill=white, stroke_width=wb + ww, stroke_fill=white)
        d.text((ox, oy), text, font=font, fill=black, stroke_width=wb, stroke_fill=black)
        d.text((ox, oy), text, font=font, fill=white, stroke_width=0)
        return img
    except Exception:
        return None


def _chip_label_image(name: str, emoji: Optional[str], px_logical: int = _OUTLINE_TEXT_PX):
    """A CTkImage of ``[emoji] [outlined name]`` for a sound chip, or None to let
    the caller fall back to a plain text button."""
    try:
        from PIL import Image
        ss = _OUTLINE_SS
        txt = _outlined_text_pil(name, px_logical * ss, _OUTLINE_WB, _OUTLINE_WW)
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
        return ctk.CTkImage(light_image=canvas, dark_image=canvas,
                            size=(max(1, W // ss), max(1, H // ss)))
    except Exception:
        return None


class _SpeedScrollableFrame(ctk.CTkScrollableFrame):
    """A ``CTkScrollableFrame`` whose mouse-wheel speed follows the app-wide
    scroll-speed multiplier, so the People board scrolls exactly as fast as the
    main soundboard (CTk's default is a fixed ~20 units/notch, which felt slow).

    ``units_per_notch`` is a 0-arg callable returning the desired Tk scroll units
    per wheel notch (the app's ``_get_scroll_units_per_notch``); None → CTk default.
    """

    def __init__(self, *args, units_per_notch=None, **kwargs):
        self._units_per_notch = units_per_notch
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
    ):
        self.root = root
        self.persons = persons
        self.play = play
        self.is_running = is_running
        self.get_main_sounds = get_main_sounds
        self.persist = persist
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
        # bound as panels rebuild.
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
                        panel.rebuild()
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
def _font(size_key: str = "size_sm", *, bold: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(
        family=FONTS["family"], size=FONTS[size_key], weight="bold" if bold else "normal"
    )


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


def emoji_image(emoji: Optional[str], size: int = 20):
    """A CTkImage of the colour emoji glyph, or None (falls back to text)."""
    if not emoji:
        return None
    try:
        pil = emoji_render.get_pil_image(emoji, size)
        if pil is not None:
            return ctk.CTkImage(light_image=pil, dark_image=pil, size=(size, size))
    except Exception:
        pass
    return None


# Cache the expensive circle-cropped avatar PIL by (path, mtime, size) so the
# hub doesn't re-decode/crop/mask the same picture from disk on every rebuild
# and every sidebar row. A fresh CTkImage is wrapped per call (never shared).
_avatar_pil_cache: dict = {}


def circle_avatar(image_path: Optional[str], size: int = 36):
    """A CTkImage of ``image_path`` cropped to a circle, or None."""
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        mtime = os.path.getmtime(image_path)
    except OSError:
        return None
    key = (image_path, mtime, size)
    out = _avatar_pil_cache.get(key)
    if out is None:
        try:
            from PIL import Image, ImageDraw
            img = Image.open(image_path).convert("RGBA")
            # Center-crop to a square, then resize.
            w, h = img.size
            s = min(w, h)
            img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
            img = img.resize((size, size), Image.LANCZOS)
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
        "L": {"cols": 1, "chip_w": 230, "trunc": 30, "height": 46},
        "M": {"cols": 2, "chip_w": 150, "trunc": 18, "height": 40},
        "S": {"cols": 3, "chip_w": 110, "trunc": 12, "height": 34},
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
        # Cancel pending single-shot timers (reflow/nudge).
        for attr in ("_reflow_after", "_nudge_after"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.after_cancel(aid)
                except Exception:
                    pass
                setattr(self, attr, None)
        # Cancel per-sound preview-progress + reset timers.
        for timers in (getattr(self, "_progress_timers", None),
                       getattr(self, "_reset_timers", None)):
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
            units_per_notch=getattr(self.ctx, "scroll_units", None))
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
        if self._body is None or not self._body.winfo_exists():
            return
        if event.widget is not self._win:
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
        self._reflow_after = self.after(120, self.rebuild)

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
        heights = [0] * gcols
        for group in visible:
            ci = heights.index(min(heights))
            card = self._build_group(col_frames[ci], group, chip_cols)
            card.pack(fill=tk.X, pady=(0, 8))
            n = len(group.sounds) if not group.collapsed else 0
            heights[ci] += 2 + (n + chip_cols - 1) // max(1, chip_cols)  # rough row estimate

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
            if time.time() >= float(_RESIZE_STATE.get("until", 0.0)):
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
        name_lbl = ctk.CTkLabel(head, text=_disp(f"{group.name}  ({len(group.sounds)})"),
                                font=_font("size_md", bold=True), text_color=COLORS["text_primary"])
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
            self._expand_group_inplace(entry)
        return card

    def _expand_group_inplace(self, entry: dict):
        """Build (once) then show a group's chips — no panel rebuild."""
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
                _CHUNK = 6

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
                        try:
                            self.after(1, lambda: _build_chunk(end))
                        except Exception:
                            pass

                _build_chunk(0)   # first few synchronously for instant feedback
            entry["built"] = True
        try:
            if not holder.winfo_ismapped():
                holder.pack(fill=tk.X, padx=8, pady=(0, 8))
            entry["caret"].configure(text="▼")
        except Exception:
            pass

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
        trunc = p["trunc"]
        # A person's colour themes ALL of their sound tiles (set it via Edit
        # person -> Colour). It takes precedence so picking a person colour makes
        # every one of their sounds that colour; clear it to fall back to per-
        # sound / per-group colours.
        bg = self.person.color or slot.color or group.color or COLORS["blurple"]
        fg = get_text_color_for_bg(bg)
        label = slot.name if len(slot.name) <= trunc else slot.name[: trunc - 1] + "…"
        cimg = emoji_image(slot.emoji, 18)

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
        # (or previews). Always present (track = chip colour so it's invisible
        # when idle) to avoid layout jitter when it appears/disappears.
        prog = ctk.CTkProgressBar(chip, height=4, corner_radius=2,
                                  fg_color=bg, progress_color=fg)
        prog.set(0.0)
        prog.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4, pady=(1, 3))

        self._chip_widgets[id(slot)] = {
            "play": play, "menu": menu_btn, "chip": chip, "prog": prog,
            "bg": bg, "fg": fg, "label": label, "cimg": cimg, "slot": slot,
            "outlined": outlined,
        }

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
            dst_group.sounds.append(SoundSlot.from_dict(d["slot"].to_dict()))
            self.ctx.changed(person)
            self.ctx.changed(self.person)
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
            except Exception:
                pass

    def _open_sound_settings(self, group: PersonGroup, slot: SoundSlot):
        sound_settings_dialog(self.winfo_toplevel(), slot, on_change=lambda: self.ctx.changed(self.person))

    # -- person edits ------------------------------------------------------
    def _edit_person(self):
        def save(name, emoji, color, image_path):
            self.person.name = name
            self.person.emoji = emoji
            self.person.color = color
            self.person.image_path = image_path
            self.ctx.changed(self.person)
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

        def add(slot: SoundSlot):
            # Deep-copy so later edits don't mutate the original main-board slot.
            copy = SoundSlot.from_dict(slot.to_dict())
            group.sounds.append(copy)
            self.ctx.changed(self.person)

        for label, slot in sounds:
            bg = slot.color or COLORS["bg_light"]
            ctk.CTkButton(body, text=_disp(label), anchor="w", fg_color=COLORS["bg_medium"],
                          hover_color=COLORS["bg_light"], text_color=COLORS["text_primary"],
                          font=_font(), corner_radius=6,
                          command=lambda s=slot: add(s)).pack(fill=tk.X, padx=4, pady=3)

        ctk.CTkButton(dlg, text="Done", command=dlg.destroy, fg_color=COLORS["blurple"],
                      hover_color=COLORS["blurple_hover"], font=_font(bold=True)).pack(
            padx=10, pady=(0, 12), fill=tk.X)
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


# ---------------------------------------------------------------------------
# Pop-out: a single person in a floating, always-on-top window
# ---------------------------------------------------------------------------
class PersonPopout(ctk.CTkToplevel):
    def __init__(self, master, person: Person, ctx: PersonContext):
        super().__init__(master)
        self.person = person
        self.ctx = ctx
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
        # Unregister the panel before the window tears it down at the C level.
        try:
            self.panel.destroy()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# The hub: manage all people + a panel for the selected one
# ---------------------------------------------------------------------------
class PersonHub(ctk.CTkToplevel):
    def __init__(self, master, ctx: PersonContext, on_popout: Callable[[Person], None]):
        super().__init__(master)
        self.ctx = ctx
        self.on_popout = on_popout
        self.selected: Optional[Person] = None
        self._panel: Optional[PersonPanel] = None
        # One PersonPanel per person, built on first view and kept alive (just
        # pack_forget()'d when hidden) so switching people is instant instead of
        # tearing down and rebuilding ~200 CTk widgets every click. Keyed by
        # id(person) (Person dataclasses aren't hashable).
        self._panel_cache: dict = {}
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
        # Only auto-place beside the main window when we have NO remembered
        # position (otherwise honour where the user left it). Keep the size/pos
        # fresh in the config as the user moves/resizes (debounced write).
        if not self._restored_geometry:
            self._position_beside(master)
        self.bind("<Configure>", self._remember_geometry, add="+")
        self.refresh_people()
        if ctx.persons:
            self.select(ctx.persons[0])
        else:
            self._show_empty_hint()

    def _remember_geometry(self, event=None):
        # Persist the hub's geometry whenever it changes (debounced downstream).
        # Ignore child-widget <Configure> and the pre-mapped 1x1 state.
        if event is not None and event.widget is not self:
            return
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
        exactly the row for ``self.selected`` is highlighted, always."""
        for rw in list(self._row_widgets.values()):
            is_sel = rw["person"] is self.selected
            try:
                if not rw["row"].winfo_exists():
                    continue
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
        panel._showing = True
        q = self._search_var.get().strip().lower() if hasattr(self, "_search_var") else ""
        panel.set_filter(q)     # no-op if unchanged
        panel.ensure_fresh()    # rebuild only if it changed while hidden
        panel.tkraise()
        self._panel = panel
        self._update_selection_highlight(prev, person)

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
        # Remember where/how big it was so it reopens the same.
        self._remember_geometry()
        # Tear down every cached panel (each unregisters from the context).
        for panel in list(self._panel_cache.values()):
            try:
                panel.destroy()
            except Exception:
                pass
        self._panel_cache.clear()
        self._panel = None
        self.destroy()
