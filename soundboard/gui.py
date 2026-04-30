"""
GUI components for the Discord Soundboard.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, List, Optional, Union

import customtkinter as ctk
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont, ImageTk

from .audio import AudioMixer, SoundCache
from .constants import (
    ALL_SLOT_COLORS,
    COLORS,
    CONFIG_FILE,
    FONTS,
    IMAGES_DIR,
    SLOT_COLORS,
    SOUNDS_DIR,
    SUPPORTED_FORMATS,
    SUPPORTED_IMAGE_FORMATS,
    UI,
    get_text_color_for_bg,
)
from .editor import SoundEditor
from .models import SoundSlot, SoundTab
from .slot_widget import (
    ButtonProxy,
    EmojiLabelProxy,
    FrameProxy,
    MenuButtonProxy,
    ProgressProxy,
    SlotWidget,
    StopButtonProxy,
)


# ---------------------------------------------------------------------------
# CustomTkinter performance patch: defer per-widget Canvas redraws during
# active window resize/move.
#
# Every CTk widget binds <Configure> to `_update_dimensions_event`, which
# calls `_draw()` whenever its size changes. With dozens of slot widgets
# (frames + buttons + progress bars), every pixel of resize triggers a
# cascade of expensive Canvas redraws (rounded corners, gradients, etc.)
# that stutters the window during a drag.
#
# This patch makes `_update_dimensions_event` skip the `_draw()` call while
# `SoundboardApp._resize_active_until` is in the future. The widget still
# records its new dimensions; we just suppress the visual redraw until the
# resize settles. A single deferred sweep then redraws everything once.
# ---------------------------------------------------------------------------
try:
    from customtkinter.windows.widgets.core_widget_classes import ctk_base_class as _ctk_base

    # Shared with slot_widget via _shared module to avoid circular import.
    from ._shared import RESIZE_STATE as _SHARED_RESIZE_STATE

    _orig_update_dimensions_event = _ctk_base.CTkBaseClass._update_dimensions_event

    def _patched_update_dimensions_event(self, event):  # type: ignore[no-redef]
        # Replicate the dimension-change check from the original, but skip the
        # _draw() call when a resize is in progress. The next _draw on this
        # widget (triggered by the post-resize sweep, theme change, or any
        # state change) will pick up the recorded dimensions.
        try:
            new_w = self._reverse_widget_scaling(event.width)
            new_h = self._reverse_widget_scaling(event.height)
            if round(self._current_width) != round(new_w) or round(self._current_height) != round(
                new_h
            ):
                self._current_width = new_w
                self._current_height = new_h
                if time.time() < _SHARED_RESIZE_STATE["until"]:
                    # Defer the redraw: tag widget for later sweep.
                    _SHARED_RESIZE_STATE.setdefault("dirty", set()).add(self)  # type: ignore[arg-type]
                    return
                self._draw(no_color_updates=True)
        except Exception:
            # Fall back to the original behavior on any unexpected issue
            # so we never break CTk's drawing.
            try:
                _orig_update_dimensions_event(self, event)
            except Exception:
                pass

    _ctk_base.CTkBaseClass._update_dimensions_event = _patched_update_dimensions_event
except Exception:
    # If CTk internals change, silently fall back to default behavior.
    from ._shared import RESIZE_STATE as _SHARED_RESIZE_STATE  # type: ignore[no-redef]


# Regex matching Hebrew, Arabic, Persian RTL characters
_RTL_PATTERN = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0750-\u077F]")


def _is_rtl_dominant(text: str) -> bool:
    """Return True if the text is primarily RTL (more Hebrew/Arabic than Latin chars)."""
    if not text:
        return False
    rtl_count = len(_RTL_PATTERN.findall(text))
    ltr_count = len(re.findall(r"[a-zA-Z]", text))
    return rtl_count > 0 and rtl_count >= ltr_count


def _fix_rtl_text(text: str) -> str:
    """
    Prepare RTL-dominant text for Tkinter Canvas / CTkButton on Windows.

    Tkinter Canvas uses Win32 GDI which applies BiDi: it reverses word order
    for RTL paragraphs. We pre-reverse word order so the two reversals cancel
    out and the text displays in the same left-to-right logical order as typed.

    Brackets and punctuation (!, ', geresh, gershayim) are part of their
    adjacent token and survive unchanged — Tkinter Canvas does NOT apply BiDi
    bracket mirroring so no pre-mirroring is needed or wanted.
    LTR-dominant lines are passed through unchanged.
    """
    if not text or not _RTL_PATTERN.search(text):
        return text
    lines = text.split("\n")
    result = []
    for line in lines:
        if _is_rtl_dominant(line):
            result.append(" ".join(reversed(line.split(" "))))
        else:
            result.append(line)
    return "\n".join(result)


def _bind_clipboard_shortcuts(entry_widget) -> None:
    """
    Make Ctrl+V/C/X/A/Z/Y work regardless of the active keyboard layout.

    Tk's default class bindings are tied to the *keysym* (`v`, `c`, ...).
    On non-Latin layouts (Hebrew, Russian, Arabic, Greek, ...) Ctrl+V emits
    a different keysym (e.g. `ה`), so the built-in <<Paste>> binding never
    fires and the entry stays empty. We bind on the physical *keycode*
    instead, which is layout-independent on Windows.

    Also handles right-click → Paste/Copy/Cut context menu.

    Accepts either a CTkEntry (uses its inner ._entry) or a raw tk.Entry.
    Safe to call multiple times; safe if widget is None.
    """
    if entry_widget is None:
        return
    # Resolve the underlying tk.Entry
    inner = getattr(entry_widget, "_entry", entry_widget)
    if inner is None:
        return

    # Windows virtual-key codes (also match Tk's event.keycode on Windows)
    KC_A, KC_C, KC_V, KC_X, KC_Y, KC_Z = 65, 67, 86, 88, 89, 90

    def _do_paste(w):
        try:
            # Replace selection if any
            try:
                if w.selection_present():
                    w.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            try:
                clip = w.clipboard_get()
            except tk.TclError:
                return
            if clip:
                w.insert("insert", clip)
        except Exception:
            pass

    def _do_copy(w):
        try:
            if w.selection_present():
                text = w.selection_get()
                w.clipboard_clear()
                w.clipboard_append(text)
        except Exception:
            pass

    def _do_cut(w):
        try:
            if w.selection_present():
                text = w.selection_get()
                w.clipboard_clear()
                w.clipboard_append(text)
                w.delete("sel.first", "sel.last")
        except Exception:
            pass

    def _do_select_all(w):
        try:
            w.select_range(0, "end")
            w.icursor("end")
        except Exception:
            pass

    def _on_ctrl_key(event):
        # On Windows, event.state bit 0x4 = Control. Tk also fires this
        # handler only for Control-KeyPress, so trust the binding.
        kc = getattr(event, "keycode", 0)
        w = event.widget
        if kc == KC_V:
            _do_paste(w)
            return "break"
        if kc == KC_C:
            _do_copy(w)
            return "break"
        if kc == KC_X:
            _do_cut(w)
            return "break"
        if kc == KC_A:
            _do_select_all(w)
            return "break"
        # Z/Y: let Tk's built-in undo/redo run
        return None

    try:
        inner.bind("<Control-KeyPress>", _on_ctrl_key, add="+")
        # Some IMEs / RTL layouts emit Control-Shift combos; cover those too
        inner.bind("<Control-Shift-KeyPress>", _on_ctrl_key, add="+")
    except Exception:
        pass

    # Right-click context menu (Paste / Copy / Cut / Select All)
    def _show_context_menu(event):
        try:
            menu = tk.Menu(inner, tearoff=0)
            menu.add_command(label="Cut", command=lambda: _do_cut(inner))
            menu.add_command(label="Copy", command=lambda: _do_copy(inner))
            menu.add_command(label="Paste", command=lambda: _do_paste(inner))
            menu.add_separator()
            menu.add_command(label="Select All", command=lambda: _do_select_all(inner))
            menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass
        finally:
            try:
                menu.grab_release()  # type: ignore[name-defined]
            except Exception:
                pass

    try:
        inner.bind("<Button-3>", _show_context_menu, add="+")
    except Exception:
        pass


def _bind_rtl_entry(entry_widget: "ctk.CTkEntry", str_var: "tk.StringVar") -> None:
    """
    Force LTR display in a CTkEntry, even when the text contains Hebrew.

    Win32 Edit controls auto-detect paragraph direction from the first strong
    BiDi character. For Hebrew text that first char is RTL, causing the whole
    field to display right-to-left. Setting justify='left' (ES_LEFT style)
    keeps the paragraph in LTR mode so the text appears in logical order,
    matching the button display and what the user typed.

    Also installs layout-independent clipboard shortcuts so paste works on
    Hebrew / Russian / etc. keyboard layouts.
    """

    def _set_ltr(*_args: object) -> None:
        try:
            entry_widget._entry.configure(justify="left")  # type: ignore[attr-defined]
        except Exception:
            pass

    str_var.trace_add("write", _set_ltr)
    _set_ltr()  # Apply immediately
    _bind_clipboard_shortcuts(entry_widget)


# Configure CustomTkinter appearance
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Try to import keyboard for global hotkeys
try:
    import keyboard

    HOTKEYS_AVAILABLE = True
except ImportError:
    HOTKEYS_AVAILABLE = False

# Try to import PIL for image support
try:
    from PIL import Image, ImageTk

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Try to import windnd for drag-and-drop file support (Windows only)
try:
    import windnd

    WINDND_AVAILABLE = True
except ImportError:
    WINDND_AVAILABLE = False


class NowPlayingPanel:
    """DJ Looper side panel showing currently playing sounds with full DJ controls.

    Features:
    - List of currently playing sounds with progress bars
    - Play/pause button for each sound
    - Loop indicator and toggle for looping sounds
    - Speed slider for real-time speed adjustment
    - Volume slider for real-time volume adjustment
    - Loop count and delay controls
    - Restart button per sound
    - Elapsed time / total duration display
    - Colored state indicator border (orange=playing, yellow=delay, gray=paused)
    - Stop button per sound (prominent)
    """

    def __init__(
        self,
        parent: Any,
        mixer_ref: Union[Callable[[], Optional[AudioMixer]], AudioMixer],
        on_stop_callback: Optional[Callable[[str], None]] = None,
    ):
        self.parent = parent
        self.mixer_ref = mixer_ref
        self.on_stop_callback = on_stop_callback
        self.is_visible = False
        self.panel_side = "right"

        self.frame: Optional[ctk.CTkFrame] = None
        self.items_frame: Optional[ctk.CTkFrame] = None
        self.sound_items: Dict[str, Dict[str, Any]] = {}

        self._create_panel()

    # ----- helpers -----

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        """Format seconds as M:SS."""
        m, s = divmod(max(0, int(seconds)), 60)
        return f"{m}:{s:02d}"

    def _get_mixer(self) -> Optional[AudioMixer]:
        mixer = self.mixer_ref() if callable(self.mixer_ref) else self.mixer_ref
        if isinstance(mixer, AudioMixer):
            return mixer
        return None

    def _state_color(self, sound_info: dict) -> str:
        """Return the accent color for the current playback state."""
        if sound_info.get("paused"):
            return COLORS["text_muted"]
        if sound_info.get("in_delay"):
            return COLORS["yellow"]
        return COLORS["playing"]

    # ----- panel chrome -----

    def _create_panel(self):
        self.frame = ctk.CTkFrame(
            self.parent,
            fg_color=COLORS["bg_dark"],
            corner_radius=UI["corner_radius"],
            width=UI["now_playing_width"],
        )

        # Header
        header_frame = ctk.CTkFrame(self.frame, fg_color="transparent", height=40)
        header_frame.pack(fill=tk.X, padx=12, pady=(12, 8))
        header_frame.pack_propagate(False)

        header_label = ctk.CTkLabel(
            header_frame,
            text="🎧 DJ Looper",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_lg"], weight="bold"),
            text_color=COLORS["text_primary"],
        )
        header_label.pack(side=tk.LEFT)

        self.side_btn = ctk.CTkButton(
            header_frame,
            text="◀",
            command=self._toggle_side,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
            width=28,
            height=24,
        )
        self.side_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # Scrollable items container
        self.items_canvas = tk.Canvas(
            self.frame,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
            width=UI["now_playing_width"] - 24,
        )
        self.items_scrollbar = ctk.CTkScrollbar(
            self.frame,
            orientation="vertical",
            command=self.items_canvas.yview,
            fg_color=COLORS["bg_medium"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            width=10,
        )
        self.items_canvas.configure(yscrollcommand=self.items_scrollbar.set)
        self.items_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(0, 12))
        self.items_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 12), pady=(0, 12))

        self.items_frame = ctk.CTkFrame(self.items_canvas, fg_color="transparent")
        self.items_canvas_window = self.items_canvas.create_window(
            (0, 0), window=self.items_frame, anchor="nw"
        )
        self.items_frame.bind("<Configure>", self._on_items_configure)
        self.items_canvas.bind("<Configure>", self._on_canvas_configure)

        # Empty state
        self.empty_label = ctk.CTkLabel(
            self.items_frame,
            text="No sounds playing",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_muted"],
        )
        self.empty_label.pack(pady=20)

    def _on_items_configure(self, event):
        # Debounce: window resize/move fires this many times per drag.
        # Coalesce into a single update ~80ms later.
        if getattr(self, "_items_cfg_after", None):
            try:
                self.items_frame.after_cancel(self._items_cfg_after)  # type: ignore[arg-type]
            except Exception:
                pass
        self._items_cfg_after = self.items_frame.after(80, self._apply_items_configure)  # type: ignore[union-attr]

    def _apply_items_configure(self):
        self._items_cfg_after = None
        try:
            self.items_canvas.configure(scrollregion=self.items_canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event):
        # Debounce: width sync on canvas item is expensive during resize drags.
        self._pending_canvas_width = event.width
        if getattr(self, "_canvas_cfg_after", None):
            try:
                self.items_canvas.after_cancel(self._canvas_cfg_after)  # type: ignore[arg-type]
            except Exception:
                pass
        self._canvas_cfg_after = self.items_canvas.after(80, self._apply_canvas_configure)

    def _apply_canvas_configure(self):
        self._canvas_cfg_after = None
        width = getattr(self, "_pending_canvas_width", None)
        if width is None:
            return
        try:
            self.items_canvas.itemconfig(self.items_canvas_window, width=width)
        except Exception:
            pass

    # ----- visibility / positioning -----

    def _toggle_side(self):
        self.panel_side = "left" if self.panel_side == "right" else "right"
        self.side_btn.configure(text="▶" if self.panel_side == "left" else "◀")
        if self.is_visible:
            self._repack_panel()

    def _repack_panel(self):
        if self.frame is None:
            return
        self.frame.pack_forget()
        if self.is_visible:
            side = tk.RIGHT if self.panel_side == "right" else tk.LEFT
            self.frame.pack(
                side=side,
                fill=tk.Y,
                padx=(
                    8 if self.panel_side == "right" else 0,
                    0 if self.panel_side == "right" else 8,
                ),
            )

    def show(self):
        if not self.is_visible:
            self.is_visible = True
            self._repack_panel()

    def hide(self):
        if self.is_visible:
            self.is_visible = False
            if self.frame is not None:
                self.frame.pack_forget()

    def toggle(self):
        if self.is_visible:
            self.hide()
        else:
            self.show()

    def set_side(self, side: str):
        if side in ("left", "right") and side != self.panel_side:
            self.panel_side = side
            self.side_btn.configure(text="▶" if side == "left" else "◀")
            if self.is_visible:
                self._repack_panel()

    # ----- update loop -----

    def update(self, playing_sounds: list, playing_slots: Optional[dict] = None):
        current_ids = set()

        if playing_sounds:
            self.empty_label.pack_forget()
        else:
            self.empty_label.pack(pady=20)
            for sound_id in list(self.sound_items.keys()):
                self._remove_item(sound_id)
            return

        for sound_info in playing_sounds:
            sound_id = sound_info.get("sound_id")
            if not sound_id:
                continue
            current_ids.add(sound_id)
            if sound_id in self.sound_items:
                self._update_item(sound_id, sound_info)
            else:
                self._create_item(sound_id, sound_info)

        for sound_id in list(self.sound_items.keys()):
            if sound_id not in current_ids:
                self._remove_item(sound_id)

    # ----- item creation -----

    def _create_item(self, sound_id: str, sound_info: dict):
        """Create a new DJ-style sound item widget."""
        state_color = self._state_color(sound_info)

        # Outer frame with colored left border indicator
        outer_frame = ctk.CTkFrame(
            self.items_frame,
            fg_color=COLORS["bg_medium"],
            corner_radius=8,
        )
        outer_frame.pack(fill=tk.X, pady=4)

        # Colored state bar on the left
        state_bar = ctk.CTkFrame(
            outer_frame,
            fg_color=state_color,
            corner_radius=4,
            width=4,
        )
        state_bar.pack(side=tk.LEFT, fill=tk.Y, padx=(4, 0), pady=6)

        # Content container
        content = ctk.CTkFrame(outer_frame, fg_color="transparent")
        content.pack(fill=tk.BOTH, expand=True, padx=(8, 10), pady=8)

        # ── Row 1: Name + Stop button ──
        row1 = ctk.CTkFrame(content, fg_color="transparent")
        row1.pack(fill=tk.X)

        name = sound_info.get("name", "Unknown")[:22]
        if len(sound_info.get("name", "")) > 22:
            name += "…"

        name_label = ctk.CTkLabel(
            row1,
            text=name,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        )
        name_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Large stop button
        stop_btn = ctk.CTkButton(
            row1,
            text="✕",
            command=lambda: self._on_stop_click(sound_id),
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            corner_radius=6,
            width=34,
            height=28,
        )
        stop_btn.pack(side=tk.RIGHT, padx=(4, 0))

        # ── Row 2: Time + Loop indicator ──
        row2 = ctk.CTkFrame(content, fg_color="transparent")
        row2.pack(fill=tk.X, pady=(2, 0))

        elapsed = sound_info.get("elapsed_seconds", 0)
        total = sound_info.get("total_seconds", 0)
        time_label = ctk.CTkLabel(
            row2,
            text=f"{self._fmt_time(elapsed)} / {self._fmt_time(total)}",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_secondary"],
            anchor="w",
        )
        time_label.pack(side=tk.LEFT)

        # Loop indicator
        loops = sound_info.get("loops_remaining", -1)
        is_looping = sound_info.get("loop", False)
        loop_text = ""
        if is_looping:
            loop_text = "🔁 ∞" if loops < 0 else f"🔁 {loops}"
        loop_label = ctk.CTkLabel(
            row2,
            text=loop_text,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["green"] if is_looping else COLORS["text_muted"],
        )
        loop_label.pack(side=tk.RIGHT)

        # ── Row 3: Progress bar (full width) ──
        progress = ctk.CTkProgressBar(
            content,
            height=10,
            fg_color=COLORS["bg_light"],
            progress_color=state_color,
            corner_radius=5,
        )
        progress.set(sound_info.get("progress", 0))
        progress.pack(fill=tk.X, pady=(4, 0))

        # ── Row 4: Transport controls ──
        row4 = ctk.CTkFrame(content, fg_color="transparent")
        row4.pack(fill=tk.X, pady=(6, 0))

        btn_font = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"])
        btn_w, btn_h = 28, 24

        # Play/Pause
        is_paused = sound_info.get("paused", False)
        play_pause_btn = ctk.CTkButton(
            row4,
            text="▶" if is_paused else "⏸",
            command=lambda: self._on_play_pause_click(sound_id),
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=btn_font,
            corner_radius=4,
            width=btn_w,
            height=btn_h,
        )
        play_pause_btn.pack(side=tk.LEFT, padx=(0, 3))

        # Loop toggle
        loop_btn = ctk.CTkButton(
            row4,
            text="🔁" if is_looping else "➡",
            command=lambda: self._on_loop_toggle_click(sound_id),
            fg_color=COLORS["green"] if is_looping else COLORS["bg_light"],
            hover_color=COLORS["green_hover"] if is_looping else COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=btn_w,
            height=btn_h,
        )
        loop_btn.pack(side=tk.LEFT, padx=(0, 3))

        # Restart
        restart_btn = ctk.CTkButton(
            row4,
            text="↺",
            command=lambda: self._on_restart_click(sound_id),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=btn_w,
            height=btn_h,
        )
        restart_btn.pack(side=tk.LEFT)

        # ── Row 4b: Speed + Pitch + Reset ──
        row4b = ctk.CTkFrame(content, fg_color="transparent")
        row4b.pack(fill=tk.X, pady=(4, 0))

        ctk.CTkLabel(
            row4b,
            text="⚡",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=14,
        ).pack(side=tk.LEFT)

        current_speed = sound_info.get("speed", 1.0)
        # NOTE: Do NOT use variable=tk.IntVar with CTkSlider — IntVar quantizes
        # the float value the slider tries to write, which on some Tk builds
        # causes feedback loops where the slider visually doesn't move and
        # `command=` never fires reliably. Track value via closure instead.
        speed_current: List[float] = [float(int(current_speed * 100))]
        preserve_pitch_state: List[bool] = [True]
        # Override flag prevents _update_item from snapping the value label
        # back to the mixer's stale speed while the user is interacting.
        speed_user_override: List[bool] = [False]
        speed_apply_after: List[Optional[str]] = [None]

        # Create the value label FIRST so the slider's initial set() (which
        # may fire `command`) doesn't NameError on a not-yet-created widget.
        speed_value_label = ctk.CTkLabel(
            row4b,
            text=f"{current_speed:.1f}x",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=30,
        )

        def _apply_speed():
            speed_apply_after[0] = None
            mixer = self._get_mixer()
            if mixer:
                new_speed = speed_current[0] / 100.0
                threading.Thread(
                    target=mixer.set_sound_speed,
                    args=(sound_id, new_speed, preserve_pitch_state[0]),
                    daemon=True,
                ).start()
            # Keep override briefly so the label doesn't snap before the
            # background thread finishes publishing the new speed.
            if self.frame is not None:
                self.frame.after(400, lambda: speed_user_override.__setitem__(0, False))

        def _on_speed_change(val):
            # Fires on every drag tick. Update label live, debounce apply.
            speed_user_override[0] = True
            try:
                v = float(val)
            except (TypeError, ValueError):
                return
            speed_current[0] = v
            speed_value_label.configure(text=f"{v / 100.0:.1f}x")
            # Debounce: apply 250 ms after the last drag event.
            if speed_apply_after[0] is not None and self.frame is not None:
                try:
                    self.frame.after_cancel(speed_apply_after[0])
                except Exception:
                    pass
            if self.frame is not None:
                speed_apply_after[0] = self.frame.after(250, _apply_speed)

        speed_slider = ctk.CTkSlider(
            row4b,
            from_=50,
            to=200,
            number_of_steps=150,
            command=_on_speed_change,
            width=90,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        speed_slider.set(speed_current[0])
        speed_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)

        speed_value_label.pack(side=tk.LEFT)

        # Pitch preservation toggle
        def _toggle_pitch():
            preserve_pitch_state[0] = not preserve_pitch_state[0]
            pitch_btn.configure(
                text="🎵" if preserve_pitch_state[0] else "🐿",
                fg_color=COLORS["green"] if preserve_pitch_state[0] else COLORS["bg_light"],
                hover_color=(
                    COLORS["green_hover"] if preserve_pitch_state[0] else COLORS["bg_lighter"]
                ),
            )
            # Re-apply current speed with new pitch setting
            if int(speed_current[0]) != 100:
                speed_user_override[0] = True
                _apply_speed()

        pitch_btn = ctk.CTkButton(
            row4b,
            text="🎵",
            command=_toggle_pitch,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=btn_font,
            corner_radius=4,
            width=28,
            height=btn_h,
        )
        pitch_btn.pack(side=tk.LEFT, padx=(4, 2))

        # Reset speed
        def _reset_speed():
            speed_user_override[0] = True
            speed_current[0] = 100.0
            speed_slider.set(100)
            speed_value_label.configure(text="1.0x")
            mixer = self._get_mixer()
            if mixer:
                threading.Thread(
                    target=mixer.set_sound_speed,
                    args=(sound_id, 1.0, preserve_pitch_state[0]),
                    daemon=True,
                ).start()
            if self.frame is not None:
                self.frame.after(400, lambda: speed_user_override.__setitem__(0, False))

        reset_speed_btn = ctk.CTkButton(
            row4b,
            text="↺",
            command=_reset_speed,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=24,
            height=btn_h,
        )
        reset_speed_btn.pack(side=tk.LEFT, padx=(2, 0))

        # ── Row 5: Volume slider ──
        row5 = ctk.CTkFrame(content, fg_color="transparent")
        row5.pack(fill=tk.X, pady=(4, 0))

        ctk.CTkLabel(
            row5,
            text="🔊",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=18,
        ).pack(side=tk.LEFT)

        current_volume = sound_info.get("volume", 1.0)
        volume_var = tk.IntVar(value=int(current_volume * 100))
        volume_debounce_id: List[Optional[str]] = [None]
        # Prevents _update_item from snapping the value label back to the
        # mixer's stale volume during the 50 ms debounce window.
        volume_user_override: List[bool] = [False]

        def _apply_volume():
            mixer = self._get_mixer()
            if mixer:
                new_vol = volume_var.get() / 100.0
                mixer.set_sound_volume(sound_id, new_vol)
            volume_user_override[0] = False

        def _on_volume_change(val):
            volume_user_override[0] = True
            new_vol = float(val) / 100.0
            vol_value_label.configure(text=f"{int(new_vol * 100)}%")
            if volume_debounce_id[0] is not None and self.items_frame is not None:
                try:
                    self.items_frame.after_cancel(volume_debounce_id[0])
                except Exception:
                    pass
            if self.items_frame is not None:
                volume_debounce_id[0] = self.items_frame.after(50, _apply_volume)

        volume_slider = ctk.CTkSlider(
            row5,
            from_=0,
            to=150,
            variable=volume_var,
            command=_on_volume_change,
            width=100,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["green"],
            button_color=COLORS["green"],
            button_hover_color=COLORS["green_hover"],
        )
        volume_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)

        vol_value_label = ctk.CTkLabel(
            row5,
            text=f"{int(current_volume * 100)}%",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=34,
        )
        vol_value_label.pack(side=tk.LEFT)

        # Reset volume
        def _reset_volume():
            volume_user_override[0] = True
            volume_var.set(100)
            vol_value_label.configure(text="100%")
            mixer = self._get_mixer()
            if mixer:
                mixer.set_sound_volume(sound_id, 1.0)
            volume_user_override[0] = False

        reset_vol_btn = ctk.CTkButton(
            row5,
            text="↺",
            command=_reset_volume,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=22,
            height=btn_h,
        )
        reset_vol_btn.pack(side=tk.LEFT, padx=(2, 0))

        # ── Row 6: Loop delay slider (only shown when looping) ──
        row6 = ctk.CTkFrame(content, fg_color="transparent")
        is_looping_now = sound_info.get("loop", False)
        if is_looping_now:
            row6.pack(fill=tk.X, pady=(4, 0))

        ctk.CTkLabel(
            row6,
            text="⏱",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=18,
        ).pack(side=tk.LEFT)

        delay_label_prefix = ctk.CTkLabel(
            row6,
            text="Delay",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=34,
        )
        delay_label_prefix.pack(side=tk.LEFT, padx=(0, 4))

        current_delay = sound_info.get("loop_delay", 0.0)
        delay_var = tk.IntVar(value=int(current_delay * 10))  # 0-100 → 0.0-10.0s
        delay_debounce_id: List[Optional[str]] = [None]
        delay_user_override: List[bool] = [False]

        def _apply_delay():
            mixer = self._get_mixer()
            if mixer:
                new_delay = delay_var.get() / 10.0
                mixer.set_sound_loop_delay(sound_id, new_delay)
            delay_user_override[0] = False

        def _on_delay_change(val):
            delay_user_override[0] = True
            new_delay = float(val) / 10.0
            delay_value_label.configure(text=f"{new_delay:.1f}s")
            if delay_debounce_id[0] is not None and self.items_frame is not None:
                try:
                    self.items_frame.after_cancel(delay_debounce_id[0])
                except Exception:
                    pass
            if self.items_frame is not None:
                delay_debounce_id[0] = self.items_frame.after(200, _apply_delay)

        delay_slider = ctk.CTkSlider(
            row6,
            from_=0,
            to=100,
            variable=delay_var,
            command=_on_delay_change,
            width=80,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["yellow"],
            button_color=COLORS["yellow"],
            button_hover_color=COLORS["yellow"],
        )
        delay_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)

        delay_value_label = ctk.CTkLabel(
            row6,
            text=f"{current_delay:.1f}s",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=30,
        )
        delay_value_label.pack(side=tk.LEFT)

        # ── Row 7: Loop count (only when looping) ──
        row7 = ctk.CTkFrame(content, fg_color="transparent")
        if is_looping_now:
            row7.pack(fill=tk.X, pady=(2, 0))

        ctk.CTkLabel(
            row7,
            text="🔢",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=18,
        ).pack(side=tk.LEFT)

        loop_count_label = ctk.CTkLabel(
            row7,
            text="Loops",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=36,
        )
        loop_count_label.pack(side=tk.LEFT, padx=(0, 4))

        # Infinite loop btn
        def _set_infinite_loop():
            mixer = self._get_mixer()
            if mixer:
                mixer.set_sound_loop_count(sound_id, -1)

        inf_btn = ctk.CTkButton(
            row7,
            text="∞",
            command=_set_infinite_loop,
            fg_color=COLORS["blurple"] if loops < 0 else COLORS["bg_light"],
            hover_color=COLORS["blurple_hover"],
            font=btn_font,
            corner_radius=4,
            width=26,
            height=22,
        )
        inf_btn.pack(side=tk.LEFT, padx=(0, 3))

        # Preset loop counts (collected so _update_item can re-style the active one)
        preset_loop_buttons: Dict[int, Any] = {}
        for cnt in (2, 5, 10):

            def _set_loop_count(c=cnt):
                mixer = self._get_mixer()
                if mixer:
                    mixer.set_sound_loop_count(sound_id, c)

            is_active = loops == cnt
            btn = ctk.CTkButton(
                row7,
                text=str(cnt),
                command=_set_loop_count,
                fg_color=COLORS["blurple"] if is_active else COLORS["bg_light"],
                hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_lighter"],
                font=btn_font,
                corner_radius=4,
                width=26,
                height=22,
            )
            btn.pack(side=tk.LEFT, padx=(0, 3))
            preset_loop_buttons[cnt] = btn

        # Store widget references
        self.sound_items[sound_id] = {
            "outer_frame": outer_frame,
            "state_bar": state_bar,
            "progress": progress,
            "name_label": name_label,
            "time_label": time_label,
            "loop_label": loop_label,
            "play_pause_btn": play_pause_btn,
            "loop_btn": loop_btn,
            "restart_btn": restart_btn,
            "speed_slider": speed_slider,
            "speed_current": speed_current,
            "speed_value_label": speed_value_label,
            "speed_user_override": speed_user_override,
            "reset_speed_btn": reset_speed_btn,
            "pitch_btn": pitch_btn,
            "preserve_pitch_state": preserve_pitch_state,
            "volume_slider": volume_slider,
            "volume_var": volume_var,
            "vol_value_label": vol_value_label,
            "volume_user_override": volume_user_override,
            "reset_vol_btn": reset_vol_btn,
            "stop_btn": stop_btn,
            "row6": row6,
            "row7": row7,
            "delay_slider": delay_slider,
            "delay_var": delay_var,
            "delay_value_label": delay_value_label,
            "delay_user_override": delay_user_override,
            "inf_btn": inf_btn,
            "preset_loop_buttons": preset_loop_buttons,
            "sound_info": sound_info,
        }

    # ----- item update -----

    def _update_item(self, sound_id: str, sound_info: dict):
        item = self.sound_items.get(sound_id)
        if not item:
            return

        item["sound_info"] = sound_info
        state_color = self._state_color(sound_info)

        # State bar
        if item.get("state_bar"):
            item["state_bar"].configure(fg_color=state_color)

        # Progress
        item["progress"].set(sound_info.get("progress", 0))
        item["progress"].configure(progress_color=state_color)

        # Time
        if item.get("time_label"):
            elapsed = sound_info.get("elapsed_seconds", 0)
            total = sound_info.get("total_seconds", 0)
            item["time_label"].configure(
                text=f"{self._fmt_time(elapsed)} / {self._fmt_time(total)}"
            )

        # Loop indicator
        if item.get("loop_label"):
            loops = sound_info.get("loops_remaining", -1)
            is_looping = sound_info.get("loop", False)
            loop_text = ""
            if is_looping:
                loop_text = "🔁 ∞" if loops < 0 else f"🔁 {loops}"
            item["loop_label"].configure(
                text=loop_text,
                text_color=COLORS["green"] if is_looping else COLORS["text_muted"],
            )

        # Play/pause
        if item.get("play_pause_btn"):
            is_paused = sound_info.get("paused", False)
            item["play_pause_btn"].configure(text="▶" if is_paused else "⏸")

        # Loop toggle
        if item.get("loop_btn"):
            is_looping = sound_info.get("loop", False)
            item["loop_btn"].configure(
                text="🔁" if is_looping else "➡",
                fg_color=COLORS["green"] if is_looping else COLORS["bg_light"],
                hover_color=COLORS["green_hover"] if is_looping else COLORS["bg_lighter"],
            )

        # Speed display — skip if user is actively changing speed to prevent flicker
        if item.get("speed_value_label") and not item.get("speed_user_override", [False])[0]:
            current_speed = sound_info.get("speed", 1.0)
            item["speed_value_label"].configure(text=f"{current_speed:.1f}x")

        # Volume display — skip if user is actively changing volume
        if item.get("vol_value_label") and not item.get("volume_user_override", [False])[0]:
            current_vol = sound_info.get("volume", 1.0)
            item["vol_value_label"].configure(text=f"{int(current_vol * 100)}%")

        # Show/hide loop rows based on loop state
        is_looping = sound_info.get("loop", False)
        if item.get("row6"):
            if is_looping:
                if not item["row6"].winfo_ismapped():
                    item["row6"].pack(fill=tk.X, pady=(4, 0))
            else:
                item["row6"].pack_forget()

        if item.get("row7"):
            if is_looping:
                if not item["row7"].winfo_ismapped():
                    item["row7"].pack(fill=tk.X, pady=(2, 0))
            else:
                item["row7"].pack_forget()

        # Update loop delay display — skip if user is actively dragging
        if item.get("delay_value_label") and not item.get("delay_user_override", [False])[0]:
            current_delay = sound_info.get("loop_delay", 0.0)
            item["delay_value_label"].configure(text=f"{current_delay:.1f}s")

        # Update loop-count preset button highlights
        loops_remaining = sound_info.get("loops_remaining", -1)
        if item.get("inf_btn"):
            is_inf = loops_remaining < 0
            item["inf_btn"].configure(
                fg_color=COLORS["blurple"] if is_inf else COLORS["bg_light"],
                hover_color=COLORS["blurple_hover"] if is_inf else COLORS["bg_lighter"],
            )
        for cnt, btn in (item.get("preset_loop_buttons") or {}).items():
            is_active = loops_remaining == cnt
            btn.configure(
                fg_color=COLORS["blurple"] if is_active else COLORS["bg_light"],
                hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_lighter"],
            )

    # ----- item removal -----

    def _remove_item(self, sound_id: str):
        item = self.sound_items.pop(sound_id, None)
        if item and item.get("outer_frame"):
            item["outer_frame"].destroy()

    # ----- button handlers -----

    def _on_play_pause_click(self, sound_id: str):
        mixer = self._get_mixer()
        if not mixer:
            return
        item = self.sound_items.get(sound_id)
        if item:
            is_paused = item.get("sound_info", {}).get("paused", False)
            if is_paused:
                mixer.resume_sound(sound_id)
            else:
                mixer.pause_sound(sound_id)

    def _on_loop_toggle_click(self, sound_id: str):
        mixer = self._get_mixer()
        if mixer:
            mixer.toggle_sound_loop(sound_id)

    def _on_restart_click(self, sound_id: str):
        mixer = self._get_mixer()
        if mixer:
            mixer.restart_sound(sound_id)

    def _on_stop_click(self, sound_id: str):
        if self.on_stop_callback:
            self.on_stop_callback(sound_id)
        else:
            mixer = self._get_mixer()
            if mixer:
                mixer.stop_sound(sound_id)


class SoundboardApp:
    """Main GUI application for the soundboard."""

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title(UI["window_title"])
        self.root.resizable(True, True)  # Allow resizing
        self.root.configure(fg_color=COLORS["bg_darkest"])

        self.mixer: Optional[AudioMixer] = None
        self.sound_cache = SoundCache()  # Local sound storage with caching
        self.tabs: List[SoundTab] = []  # List of all tabs
        self.current_tab_idx = 0  # Currently active tab index

        # Per-tab widget storage for instant tab switching
        # Structure: tab_idx -> slot_idx -> widget
        self.tab_grid_frames: Dict[int, Any] = {}  # tab_idx -> grid frame for that tab
        self.tab_slot_buttons: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_frames: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_progress: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_preview_buttons: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_edit_buttons: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_stop_buttons: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_bottom_frames: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_emoji_labels: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_images: Dict[int, Dict[int, Any]] = {}
        self.tab_slot_image_paths: Dict[int, Dict[int, str]] = {}
        self._tab_slot_filled_cache: Dict[int, Dict[int, bool]] = {}
        self._tab_built: Dict[int, bool] = {}  # Track which tabs have been built

        # Legacy aliases for compatibility (point to current tab's widgets)
        self.slot_buttons: Dict[int, Any] = {}
        self.slot_frames: Dict[int, Any] = {}
        self.slot_progress: Dict[int, Any] = {}
        self.slot_preview_buttons: Dict[int, Any] = {}
        self.slot_edit_buttons: Dict[int, Any] = {}
        self.slot_stop_buttons: Dict[int, Any] = {}
        self.slot_bottom_frames: Dict[int, Any] = {}
        self.slot_images: Dict[int, Any] = {}
        self.slot_image_paths: Dict[int, str] = {}
        self.slot_emoji_labels: Dict[int, Any] = {}
        self._slot_filled_cache: Dict[int, bool] = {}

        self._last_active_tab_idx: int = 0  # Track last active tab for tab bar optimization
        self.registered_hotkeys: list = []

        # Debounced save handle (see _save_config / _save_config_now)
        self._save_after_id: Optional[str] = None

        # Pre-create cached fonts for performance
        self._font_sm = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        self._font_sm_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_sm"], weight="bold"
        )
        self._font_xs = ctk.CTkFont(size=FONTS["size_xs"])
        self._font_xl_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_xl"], weight="bold"
        )
        self.tab_buttons: List[Any] = []  # CTkButton instances

        # Playing state tracking: slot_idx -> {start_time, duration, tab_idx}
        self.playing_slots: Dict[int, Dict] = {}

        # Preview state tracking: slot_idx -> {start_time, duration}
        self.preview_slots: Dict[int, Dict] = {}

        # Animation-loop micro-caches: skip work when nothing changed visibly.
        # `_last_progress_values` maps slot_idx (or ("preview", slot_idx)) -> last
        # rounded ratio set on the progress bar. `_last_panel_update` is the wall
        # time of the most recent NowPlayingPanel.update() call (throttle).
        self._last_progress_values: Dict[Any, float] = {}
        self._last_panel_update: float = 0.0
        # Last text written to the status bar — skip StringVar.set when unchanged
        # to avoid the trace-callback chain firing for identical values.
        self._last_status_text: str = ""

        # Click / drag state machine  (IDLE → PRESSED → DRAGGING | click)
        # All fields are reset together via _reset_click_state().
        self._click_active: bool = False  # a press is being tracked
        self._click_slot: Optional[int] = None  # slot that was pressed
        self._click_tab: Optional[int] = None  # tab that was active at press
        self._click_start_x: int = 0  # screen-x of press
        self._click_start_y: int = 0  # screen-y of press
        self._click_dragging: bool = False  # drag threshold exceeded

        # Edit mode state (rearrange mode)
        self._edit_mode: bool = False
        self._dragging_slot: Optional[int] = None

        # Search / filter state
        self._search_query: str = ""
        self._filter_group: Optional[str] = None  # None = "All Groups"
        self._search_results: Optional[List[Dict]] = None  # None = not searching
        # ^ Each result dict: {tab_idx, slot_idx, slot}
        self._custom_groups: List[str] = []  # User-created groups (persisted in config)

        # Persistent across clicks (not reset per-click)
        self._last_play_time: float = 0.0
        self._just_stopped_slot: Optional[int] = None
        self._just_stopped_at: float = 0.0

        # Ensure images directory exists
        Path(IMAGES_DIR).mkdir(exist_ok=True)

        self._setup_styles()
        self._create_ui()

        # Force the chrome (window frame, tab sidebar, action bar, status bar)
        # to paint NOW before we spend time building the slot grid. Without this
        # the window stays invisible until __init__ returns and mainloop pumps
        # the first idle queue, which makes the app feel like it has a long
        # "Generate" startup. update_idletasks only flushes layout/paint, not
        # user events, so it's safe.
        try:
            self.root.update_idletasks()
        except Exception:
            pass

        self._load_config()
        self._preload_sounds()  # Preload all sounds into memory

        # Let window size itself based on content, then set minimum size
        self.root.after(50, self._finalize_window_size)

        # Start animation loop
        self._animate_progress()

        # Track active window resize/move so the animation loop and other
        # periodic work back off while geometry is changing. Without this,
        # progress-bar updates compound with CTk's per-widget Canvas redraws
        # on every Configure event during a drag and stutter the resize.
        self._resize_active_until: float = 0.0
        self._resize_sweep_after_id: Optional[str] = None
        self.root.bind("<Configure>", self._on_root_configure, add="+")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_root_configure(self, event):
        """Mark the window as 'currently resizing' for ~150ms after each event."""
        # Only react to events on the root window itself, not children.
        if event.widget is not self.root:
            return
        until = time.time() + 0.15
        self._resize_active_until = until
        # Share with the CTk monkeypatch so per-widget redraws are deferred.
        _SHARED_RESIZE_STATE["until"] = until
        # Schedule a one-shot post-resize sweep that redraws everything once.
        if self._resize_sweep_after_id is not None:
            try:
                self.root.after_cancel(self._resize_sweep_after_id)  # type: ignore[arg-type]
            except Exception:
                pass
        self._resize_sweep_after_id = self.root.after(180, self._post_resize_sweep)

    def _post_resize_sweep(self):
        """Redraw CTk widgets that were skipped during the resize.

        Done in chunks via after(0) so we don't block the UI for one big stall
        right after the user releases the mouse. With many slot widgets visible
        a single-shot sweep produced a visible ~50ms hitch; chunking keeps each
        tick under a single frame budget.
        """
        self._resize_sweep_after_id = None
        # Bail if another resize started in the meantime; the next sweep will catch it.
        if time.time() < self._resize_active_until:
            self._resize_sweep_after_id = self.root.after(60, self._post_resize_sweep)
            return
        dirty = (
            _SHARED_RESIZE_STATE.pop("dirty", None)
            if isinstance(_SHARED_RESIZE_STATE, dict)
            else None
        )
        if not dirty:
            return
        # Drain in chunks to keep each tick's work bounded.
        self._sweep_drain(list(dirty))

    def _sweep_drain(self, widgets: list, chunk: int = 16):
        """Redraw up to `chunk` widgets, then yield to the event loop."""
        # If a new resize started while we were draining, abandon this sweep —
        # the new sweep will pick up the (re-populated) dirty set.
        if time.time() < self._resize_active_until:
            return
        end = min(chunk, len(widgets))
        for widget in widgets[:end]:
            try:
                if widget.winfo_exists():
                    widget._draw(no_color_updates=True)  # type: ignore[attr-defined]
            except Exception:
                pass
        rest = widgets[end:]
        if rest:
            self.root.after(0, lambda: self._sweep_drain(rest, chunk))

    def _setup_styles(self):
        """Configure ttk styles for Discord-like appearance (legacy support)."""
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg_dark"])
        style.configure("TLabel", background=COLORS["bg_dark"], foreground=COLORS["text_primary"])
        style.configure("TButton", background=COLORS["blurple"], foreground=COLORS["text_primary"])

    def _finalize_window_size(self):
        """Set window size to a comfortable default that fits the layout."""
        self.root.update_idletasks()
        req_width = self.root.winfo_reqwidth()
        req_height = self.root.winfo_reqheight()

        # Ensure minimum comfortable size
        min_w = max(req_width, 800)
        min_h = max(req_height, 600)
        self.root.minsize(min_w, min_h)

        # Set initial size if window is too small
        cur_w = self.root.winfo_width()
        cur_h = self.root.winfo_height()
        if cur_w < min_w or cur_h < min_h:
            self.root.geometry(f"{max(cur_w, min_w)}x{max(cur_h, min_h)}")

    def _create_ui(self):
        """Build the main user interface."""
        # Top-level frame that can include side panels
        self.outer_frame = ctk.CTkFrame(self.root, fg_color=COLORS["bg_darkest"], corner_radius=0)
        self.outer_frame.pack(fill=tk.BOTH, expand=True, padx=UI["padding"], pady=UI["padding"])

        # Left sidebar for tabs (vertical)
        self._create_tabs_sidebar(self.outer_frame)

        # Main content frame (soundboard area)
        main_frame = ctk.CTkFrame(self.outer_frame, fg_color=COLORS["bg_darkest"], corner_radius=0)
        main_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.main_frame = main_frame

        self._create_device_section(main_frame)
        self._create_action_bar(main_frame)
        self._create_soundboard_section(main_frame)
        self._create_status_bar(main_frame)

        # Create Now Playing panel (hidden by default)
        self.now_playing_panel = NowPlayingPanel(
            self.outer_frame,
            mixer_ref=lambda: self.mixer,
            on_stop_callback=self._on_panel_stop_sound,
        )

        # Hook drag-and-drop for image/sound files from file explorer
        if WINDND_AVAILABLE:
            windnd.hook_dropfiles(self.root, func=self._on_files_dropped)

    def _create_device_section(self, parent):
        """Create the collapsible audio device selection and PTT section."""
        # Header frame for collapse toggle
        header_frame = ctk.CTkFrame(parent, fg_color="transparent")
        header_frame.pack(fill=tk.X, pady=(0, 8))

        self.audio_options_expanded = tk.BooleanVar(value=False)

        self.toggle_audio_btn = ctk.CTkButton(
            header_frame,
            text="▶ Audio Options",
            command=self._toggle_audio_options,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            text_color=COLORS["text_secondary"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=32,
            anchor="w",
            width=140,
        )
        self.toggle_audio_btn.pack(side=tk.LEFT)

        # Collapsible content frame
        self.audio_options_frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_dark"], corner_radius=UI["corner_radius"]
        )
        # Hidden by default

        # Inner container with padding
        device_frame = ctk.CTkFrame(self.audio_options_frame, fg_color="transparent")
        device_frame.pack(fill=tk.X, padx=12, pady=12)

        # Get available devices
        devices = sd.query_devices()
        input_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_input_channels"] > 0
        ]
        output_devices = [
            (i, d["name"]) for i, d in enumerate(devices) if d["max_output_channels"] > 0
        ]

        # Device selection row
        device_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        device_row.pack(fill=tk.X, pady=(0, 10))

        # Input device selector
        input_frame = ctk.CTkFrame(device_row, fg_color="transparent")
        input_frame.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        ctk.CTkLabel(
            input_frame,
            text="🎤 Microphone",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w")

        self.input_var = tk.StringVar()
        self.input_combo = ctk.CTkComboBox(
            input_frame,
            variable=self.input_var,
            values=[f"{i}: {name}" for i, name in input_devices],
            width=280,
            height=32,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )
        if input_devices:
            self.input_combo.set(f"{input_devices[0][0]}: {input_devices[0][1]}")
        self.input_combo.pack(anchor="w", pady=(4, 0))

        # Output device selector
        output_frame = ctk.CTkFrame(device_row, fg_color="transparent")
        output_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ctk.CTkLabel(
            output_frame,
            text="🔊 Virtual Cable Output",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w")

        self.output_var = tk.StringVar()
        self.output_combo = ctk.CTkComboBox(
            output_frame,
            variable=self.output_var,
            values=[f"{i}: {name}" for i, name in output_devices],
            width=280,
            height=32,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )

        # Auto-select virtual cable if found
        selected_output = None
        for idx, (i, name) in enumerate(output_devices):
            if "cable" in name.lower() or "virtual" in name.lower():
                selected_output = f"{i}: {name}"
                break
        if selected_output:
            self.output_combo.set(selected_output)
        elif output_devices:
            self.output_combo.set(f"{output_devices[0][0]}: {output_devices[0][1]}")
        self.output_combo.pack(anchor="w", pady=(4, 0))

        # Controls row (Start button, PTT, etc.)
        controls_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        controls_row.pack(fill=tk.X, pady=(0, 10))

        # Start/Stop button
        self.toggle_btn = ctk.CTkButton(
            controls_row,
            text="▶ Start Stream",
            command=self._toggle_stream,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=36,
            width=140,
        )
        self.toggle_btn.pack(side=tk.LEFT, padx=(0, 15))

        # PTT checkbox
        self.ptt_enabled_var = tk.BooleanVar(value=False)
        self.ptt_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Push-to-Talk",
            variable=self.ptt_enabled_var,
            command=self._toggle_ptt_visibility,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.ptt_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Mic mute
        self.mic_mute_var = tk.BooleanVar(value=False)
        self.mic_mute_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Mute Mic",
            variable=self.mic_mute_var,
            command=self._toggle_mic_mute,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.mic_mute_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Monitor
        self.monitor_var = tk.BooleanVar(value=True)
        self.monitor_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="🔊 Monitor",
            variable=self.monitor_var,
            command=self._toggle_monitor,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.monitor_checkbox.pack(side=tk.LEFT, padx=(0, 15))

        # Auto-start
        self.auto_start_var = tk.BooleanVar(value=True)
        self.auto_start_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="Auto-Start",
            variable=self.auto_start_var,
            command=self._save_config,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.auto_start_checkbox.pack(side=tk.LEFT)

        # Noise suppression (replaces Discord's Krisp - which is bypassed
        # when routing through the virtual cable)
        self.noise_suppress_var = tk.BooleanVar(value=False)
        self.noise_suppress_checkbox = ctk.CTkCheckBox(
            controls_row,
            text="🛡 Noise Suppression",
            variable=self.noise_suppress_var,
            command=self._toggle_noise_suppression,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.noise_suppress_checkbox.pack(side=tk.LEFT, padx=(15, 8))

        # Strength slider for noise suppression
        self.ns_strength_var = tk.DoubleVar(value=85)
        self.ns_strength_slider = ctk.CTkSlider(
            controls_row,
            from_=0,
            to=100,
            variable=self.ns_strength_var,
            command=self._update_ns_strength,
            width=110,
            height=14,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.ns_strength_slider.pack(side=tk.LEFT, padx=(0, 0))

        # PTT key frame (hidden by default)
        self.ptt_frame = ctk.CTkFrame(device_frame, fg_color="transparent")
        # Hidden initially - will be shown via pack when needed

        ctk.CTkLabel(
            self.ptt_frame,
            text="PTT Key:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_key_var = tk.StringVar(value="")
        self.ptt_entry = ctk.CTkEntry(
            self.ptt_frame,
            textvariable=self.ptt_key_var,
            width=100,
            height=28,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )
        self.ptt_entry.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_record_btn = ctk.CTkButton(
            self.ptt_frame,
            text="⏺ Record Key",
            command=self._record_ptt_key,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=100,
        )
        self.ptt_record_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_clear_btn = ctk.CTkButton(
            self.ptt_frame,
            text="Clear",
            command=self._clear_ptt_key,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=60,
        )
        self.ptt_clear_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.ptt_status_label = ctk.CTkLabel(
            self.ptt_frame,
            text="",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        )
        self.ptt_status_label.pack(side=tk.LEFT, padx=10)

        # Mic volume row
        mic_row = ctk.CTkFrame(device_frame, fg_color="transparent")
        mic_row.pack(fill=tk.X, pady=(5, 0))

        ctk.CTkLabel(
            mic_row,
            text="Mic Volume:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.mic_volume_var = tk.DoubleVar(value=100)
        self.mic_volume_slider = ctk.CTkSlider(
            mic_row,
            from_=0,
            to=150,
            variable=self.mic_volume_var,
            command=self._update_mic_volume,
            width=200,
            height=16,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.mic_volume_slider.pack(side=tk.LEFT, padx=(0, 10))

    def _toggle_audio_options(self):
        """Toggle the audio options visibility."""
        # Pre-arm resize state so the cascade of CTk child redraws triggered by
        # mapping/unmapping ~15 widgets is batched into a single post-sweep
        # instead of N per-widget Canvas redraws. This makes the toggle feel
        # instant even on slower machines.
        until = time.time() + 0.20
        self._resize_active_until = until
        _SHARED_RESIZE_STATE["until"] = until
        if self._resize_sweep_after_id is not None:
            try:
                self.root.after_cancel(self._resize_sweep_after_id)  # type: ignore[arg-type]
            except Exception:
                pass
        self._resize_sweep_after_id = self.root.after(220, self._post_resize_sweep)

        if self.audio_options_expanded.get():
            self.audio_options_frame.pack_forget()
            self.toggle_audio_btn.configure(text="▶ Audio Options")
            self.audio_options_expanded.set(False)
        else:
            self.audio_options_frame.pack(
                fill=tk.X, pady=(0, 8), after=self.toggle_audio_btn.master
            )
            self.toggle_audio_btn.configure(text="▼ Audio Options")
            self.audio_options_expanded.set(True)

    def _create_tabs_sidebar(self, parent):
        """Create vertical tabs sidebar on the left side."""
        # Sidebar container - natural width based on content
        self.tabs_sidebar = ctk.CTkFrame(parent, fg_color=COLORS["bg_dark"], corner_radius=8)
        self.tabs_sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8), pady=0)

        # Header with title and add button
        header_frame = ctk.CTkFrame(self.tabs_sidebar, fg_color="transparent")
        header_frame.pack(fill=tk.X, padx=8, pady=(8, 4))

        ctk.CTkLabel(
            header_frame,
            text="Tabs",
            font=self._font_sm_bold,
            text_color=COLORS["text_primary"],
        ).pack(side=tk.LEFT)

        self.add_tab_btn = ctk.CTkButton(
            header_frame,
            text="+",
            command=self._add_new_tab,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=self._font_sm_bold,
            corner_radius=6,
            height=26,
            width=26,
        )
        self.add_tab_btn.pack(side=tk.RIGHT, padx=(8, 0))

        # Scrollable area for tab buttons
        self.tabs_canvas = tk.Canvas(
            self.tabs_sidebar,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
            width=130,
        )
        self.tabs_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))

        # Scrollbar for tabs (only shown when needed)
        self.tabs_scrollbar = ctk.CTkScrollbar(
            self.tabs_sidebar,
            command=self.tabs_canvas.yview,
            fg_color=COLORS["bg_dark"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            width=12,
        )
        self.tabs_canvas.configure(yscrollcommand=self.tabs_scrollbar.set)

        # Frame inside canvas to hold tab buttons
        self.tabs_container = ctk.CTkFrame(
            self.tabs_canvas, fg_color="transparent", corner_radius=0
        )
        self.tabs_canvas_window = self.tabs_canvas.create_window(
            (0, 0), window=self.tabs_container, anchor="nw"
        )

        # Bind events for scrolling
        self.tabs_canvas.bind("<Configure>", self._on_tabs_canvas_configure)
        self.tabs_container.bind("<Configure>", self._on_tabs_container_configure)
        self.tabs_canvas.bind("<Enter>", self._bind_tabs_mousewheel)
        self.tabs_canvas.bind("<Leave>", self._unbind_tabs_mousewheel)
        # Use bind_all with add="+" so it doesn't clobber CTkScrollableFrame's binding.
        # The handler checks _tabs_mousewheel_bound flag to only scroll when hovering.
        self._tabs_mousewheel_bound = False
        self.tabs_canvas.bind_all("<MouseWheel>", self._on_tabs_mousewheel, add="+")

    def _create_action_bar(self, parent):
        """Create the action bar with Move, Stop All, and Playing buttons."""
        # Action bar container
        self.action_bar_frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_medium"], height=48, corner_radius=8
        )
        self.action_bar_frame.pack(fill=tk.X, pady=(0, 10), padx=4)
        self.action_bar_frame.pack_propagate(False)

        # LEFT: Edit mode button
        left_section = ctk.CTkFrame(self.action_bar_frame, fg_color="transparent")
        left_section.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)

        self.edit_mode_btn = ctk.CTkButton(
            left_section,
            text="↔ Move",
            command=self._toggle_edit_mode,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=65,
        )
        self.edit_mode_btn.pack(side=tk.LEFT, pady=8)

        # YouTube → MP3 download button
        self.youtube_btn = ctk.CTkButton(
            left_section,
            text="⬇ YouTube",
            command=self._show_youtube_download_dialog,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=90,
        )
        self.youtube_btn.pack(side=tk.LEFT, padx=(6, 0), pady=8)

        # RIGHT: Stop All and Playing buttons
        right_section = ctk.CTkFrame(self.action_bar_frame, fg_color="transparent")
        right_section.pack(side=tk.RIGHT, padx=(0, 8), fill=tk.Y)

        # Stop All button
        self.stop_all_btn = ctk.CTkButton(
            right_section,
            text="⏹ Stop All",
            command=self._stop_all_sounds,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold"),
            corner_radius=6,
            height=32,
            width=80,
        )
        self.stop_all_btn.pack(side=tk.LEFT, padx=(0, 6), pady=8)

        # DJ Looper toggle button
        self.now_playing_btn = ctk.CTkButton(
            right_section,
            text="🎧 DJ Looper",
            command=self._toggle_now_playing_panel,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=90,
        )
        self.now_playing_btn.pack(side=tk.LEFT, pady=8)

    def _create_tab_bar(self, parent):
        """DEPRECATED: Use _create_tabs_sidebar() and _create_action_bar() instead."""
        pass  # Keep for backwards compatibility, but no longer used

    def _bind_tabs_mousewheel(self, event=None):
        """Bind mousewheel to tabs scrolling when hovering over tabs area."""
        self._tabs_mousewheel_bound = True

    def _unbind_tabs_mousewheel(self, event=None):
        """Unbind mousewheel from tabs scrolling."""
        self._tabs_mousewheel_bound = False

    def _on_tabs_mousewheel(self, event):
        """Handle mousewheel scrolling on tabs canvas (vertical)."""
        if not getattr(self, "_tabs_mousewheel_bound", False):
            return
        # Only scroll if there's overflow
        canvas_height = self.tabs_canvas.winfo_height()
        content_height = self.tabs_container.winfo_reqheight()
        if content_height <= canvas_height:
            return
        # Scroll vertically
        self.tabs_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _scroll_tabs(self, direction: int):
        """Scroll tabs up (-1) or down (1)."""
        self.tabs_canvas.yview_scroll(direction * 3, "units")

    def _on_tabs_canvas_configure(self, event=None):
        """Handle tabs canvas resize (debounced)."""
        # Immediately match inner window width to canvas width (cheap).
        if event is not None:
            try:
                self.tabs_canvas.itemconfig(self.tabs_canvas_window, width=event.width)
            except Exception:
                pass
        # Defer the expensive scrollbar show/hide + bbox work.
        self._schedule_tabs_scroll_update()

    def _on_tabs_container_configure(self, event=None):
        """Handle tabs container content change (debounced)."""
        self._schedule_tabs_scroll_update()

    def _schedule_tabs_scroll_update(self):
        """Coalesce tabs scroll-region updates fired by Configure during resize."""
        if getattr(self, "_tabs_scroll_after", None):
            try:
                self.root.after_cancel(self._tabs_scroll_after)  # type: ignore[arg-type]
            except Exception:
                pass
        self._tabs_scroll_after = self.root.after(80, self._apply_tabs_scroll_update)

    def _apply_tabs_scroll_update(self):
        self._tabs_scroll_after = None
        try:
            bbox = self.tabs_canvas.bbox("all")
            if bbox:
                self.tabs_canvas.configure(scrollregion=(0, 0, bbox[2], bbox[3] + 4))
        except Exception:
            pass
        self._update_tabs_scrollbar()

    def _update_tabs_scrollbar(self):
        """Show/hide scrollbar based on whether tabs overflow vertically."""
        canvas_height = self.tabs_canvas.winfo_height()
        content_height = self.tabs_container.winfo_reqheight()

        if content_height > canvas_height and canvas_height > 1:
            # Show scrollbar on the right side of sidebar
            if not self.tabs_scrollbar.winfo_ismapped():
                self.tabs_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 8))
        else:
            # Hide scrollbar and reset scroll position to start
            if self.tabs_scrollbar.winfo_ismapped():
                self.tabs_scrollbar.pack_forget()
            # Reset scroll to beginning when no overflow
            self.tabs_canvas.yview_moveto(0)

    def _refresh_tab_bar(self):
        """Refresh the tab bar buttons.

        Optimization: If the number of tabs hasn't changed, just update existing
        button properties instead of destroying and recreating all buttons.
        This significantly reduces lag when switching tabs.
        """
        # Check if we need to recreate buttons (tab count changed)
        if len(self.tab_buttons) != len(self.tabs):
            # Tab count changed - must recreate all buttons
            for btn in self.tab_buttons:
                btn.destroy()
            self.tab_buttons.clear()

            for idx, tab in enumerate(self.tabs):
                display_name = f"{tab.emoji} {tab.name}" if tab.emoji else tab.name
                is_active = idx == self.current_tab_idx
                tab_anchor = "e" if _is_rtl_dominant(tab.name) else "w"

                btn = ctk.CTkButton(
                    self.tabs_container,
                    text=_fix_rtl_text(display_name),
                    command=lambda i=idx: self._switch_tab(i),
                    fg_color=COLORS["blurple"] if is_active else COLORS["bg_medium"],
                    hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_light"],
                    text_color=COLORS["text_primary"],
                    font=self._font_sm_bold if is_active else self._font_sm,
                    corner_radius=6,
                    height=36,
                    anchor=tab_anchor,
                )
                btn.pack(side=tk.TOP, fill=tk.X, pady=(0, 4))
                btn.bind("<Button-3>", lambda e, i=idx: self._configure_tab(i))
                self.tab_buttons.append(btn)

            # Reset scroll to beginning when tabs are recreated
            self.tabs_canvas.yview_moveto(0)
        else:
            # Same tab count - just update existing buttons (much faster)
            # Only update the tabs that need visual changes (previously active and newly active)
            for idx, (btn, tab) in enumerate(zip(self.tab_buttons, self.tabs)):
                is_active = idx == self.current_tab_idx
                was_active = getattr(self, "_last_active_tab_idx", -1) == idx

                # Only reconfigure if this tab's active state changed
                if is_active or was_active:
                    display_name = f"{tab.emoji} {tab.name}" if tab.emoji else tab.name
                    tab_anchor = "e" if _is_rtl_dominant(tab.name) else "w"
                    btn.configure(
                        text=_fix_rtl_text(display_name),
                        fg_color=COLORS["blurple"] if is_active else COLORS["bg_medium"],
                        hover_color=COLORS["blurple_hover"] if is_active else COLORS["bg_light"],
                        font=self._font_sm_bold if is_active else self._font_sm,
                        anchor=tab_anchor,
                    )

            # Track which tab was active for next comparison
            self._last_active_tab_idx = self.current_tab_idx

        # Only recompute scroll region when tab count actually changed
        # (active-state-only updates do not affect the canvas bbox).
        if len(self.tab_buttons) != len(self.tabs) or not getattr(
            self, "_tabs_scroll_initialized", False
        ):
            self._tabs_scroll_initialized = True
            self._schedule_tabs_scroll_update()

    def _show_tab_only(self, tab_idx: int):
        """Make `tab_idx` the only visible/managed tab grid frame.

        Only the previously-shown tab is grid_remove()'d (tracked via
        `_currently_shown_tab`). Iterating ALL tabs every switch is expensive
        because each grid_remove triggers Tk geometry recomputation. Hidden
        tabs stop receiving Configure/Map events on resize/move/minimize.
        Widgets are kept alive so re-show is instant.
        """
        target = self.tab_grid_frames.get(tab_idx)
        if target is None:
            return

        prev_idx = getattr(self, "_currently_shown_tab", None)
        if prev_idx is not None and prev_idx != tab_idx:
            prev_frame = self.tab_grid_frames.get(prev_idx)
            if prev_frame is not None:
                try:
                    prev_frame.grid_remove()
                except Exception:
                    pass
        try:
            target.grid()
            # No tkraise() needed: with all other tab grids grid_remove()'d
            # the target is already the only managed child at row=0,col=0.
            # Calling tkraise() forces extra CTk state updates for nothing.
        except Exception:
            pass
        self._currently_shown_tab = tab_idx

    def _switch_tab(self, tab_idx: int):
        """Switch to a different tab using tkraise() for instant switching."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        # Check if we're in edit mode with a selected slot - move it to this tab
        if self._edit_mode and self._dragging_slot is not None and tab_idx != self.current_tab_idx:
            source_idx = self._dragging_slot
            source_tab = self._click_tab
            self._dragging_slot = None

            if source_idx is not None and source_tab is not None:
                self._move_slot_to_tab(source_idx, source_tab, tab_idx)
            return

        # Don't switch to current tab
        if tab_idx == self.current_tab_idx:
            return

        # Clear search results if active (return to normal tab view)
        if self._search_results is not None:
            self._hide_search_results()

        # Cancel any ongoing interaction
        self._reset_click_state()
        if self._edit_mode:
            self._exit_edit_mode(skip_refresh=True)

        # Clear progress bars on OLD tab before switching
        old_tab_idx = self.current_tab_idx
        if old_tab_idx in self.tab_slot_progress:
            for slot_idx in self.tab_slot_progress[old_tab_idx]:
                self.tab_slot_progress[old_tab_idx][slot_idx].set(0)

        # Update current tab index
        self.current_tab_idx = tab_idx

        # Ensure target tab is built (lazy build on first visit)
        self._ensure_tab_built(tab_idx)

        # NOTE: do NOT pre-arm _SHARED_RESIZE_STATE here. Doing so used to
        # batch CTk per-widget redraws during the tab swap, but with the
        # SlotWidget refactor each slot is a single tk.Canvas that needs to
        # redraw IMMEDIATELY at its new size when the tab is shown — otherwise
        # users see slots flash at the previous window's size for ~200ms after
        # switching tabs post-resize.

        # INSTANT SWITCH: hide other tabs entirely (grid_remove) so they
        # stop receiving Configure/Map events on resize/move/minimize.
        self._show_tab_only(tab_idx)

        # Scroll the sound grid back to the top so the first sounds are visible
        try:
            self.scrollable_grid._parent_canvas.yview_moveto(0)  # type: ignore[attr-defined]
        except Exception:
            pass

        # Update aliases to point at new tab's widgets
        self._update_current_tab_aliases()

        # Update tab bar appearance (already optimized)
        self._refresh_tab_bar()

        # Scroll the new active tab into view
        self._scroll_tab_into_view(tab_idx)

    def _scroll_tab_into_view(self, tab_idx: int):
        """Scroll the tabs canvas to make the specified tab visible."""
        if tab_idx < 0 or tab_idx >= len(self.tab_buttons):
            return

        btn = self.tab_buttons[tab_idx]

        # Get the button position relative to the tabs_container
        btn_x = btn.winfo_x()
        btn_width = btn.winfo_width()

        # Get canvas viewport
        canvas_width = self.tabs_canvas.winfo_width()
        content_width = self.tabs_container.winfo_reqwidth()

        # If content fits, no scrolling needed
        if content_width <= canvas_width:
            return

        # Get current scroll position
        scroll_pos = self.tabs_canvas.xview()
        visible_left = scroll_pos[0] * content_width
        visible_right = scroll_pos[1] * content_width

        # Check if button is already fully visible
        if btn_x >= visible_left and (btn_x + btn_width) <= visible_right:
            return

        # Calculate scroll position to center the button (or at least make it visible)
        target_x = max(0, btn_x - (canvas_width / 2) + (btn_width / 2))
        scroll_fraction = target_x / content_width
        scroll_fraction = max(0, min(1, scroll_fraction))

        self.tabs_canvas.xview_moveto(scroll_fraction)

    def _add_new_tab(self):
        """Add a new tab."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("New Tab")
        dialog.geometry("400x220")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Name field
        ctk.CTkLabel(frame, text="Tab Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=10
        )
        name_var = tk.StringVar(value=f"Tab {len(self.tabs) + 1}")
        tab_name_entry = ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        )
        tab_name_entry.grid(row=0, column=1, pady=10)
        _bind_rtl_entry(tab_name_entry, name_var)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=10
        )
        emoji_var = tk.StringVar(value="")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=1, column=1, sticky="w", pady=10)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=1, column=2, padx=10)

        def save():
            name = name_var.get().strip() or f"Tab {len(self.tabs) + 1}"
            emoji = emoji_var.get().strip() or None
            new_tab = SoundTab(name=name, emoji=emoji)
            self.tabs.append(new_tab)
            new_tab_idx = len(self.tabs) - 1
            # Build widgets for the new tab
            self._build_tab_widgets(new_tab_idx)
            self.current_tab_idx = new_tab_idx
            self._show_tab_only(new_tab_idx)
            self._update_current_tab_aliases()
            self._refresh_tab_bar()
            self._save_config()
            dialog.destroy()

        # Buttons
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Create",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _configure_tab(self, tab_idx: int):
        """Configure or delete a tab."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]

        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Edit Tab: {tab.name}")
        dialog.geometry("400x250")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # Name field
        ctk.CTkLabel(frame, text="Tab Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=10
        )
        name_var = tk.StringVar(value=tab.name)
        edit_tab_name_entry = ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        )
        edit_tab_name_entry.grid(row=0, column=1, pady=10)
        _bind_rtl_entry(edit_tab_name_entry, name_var)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=10
        )
        emoji_var = tk.StringVar(value=tab.emoji or "")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=1, column=1, sticky="w", pady=10)

        # Emoji picker button
        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=1, column=2, padx=10)

        def save():
            tab.name = name_var.get().strip() or f"Tab {tab_idx + 1}"
            tab.emoji = emoji_var.get().strip() or None
            self._refresh_tab_bar()
            self._save_config()
            dialog.destroy()

        def delete():
            # Prevent deleting the last tab
            if len(self.tabs) <= 1:
                messagebox.showwarning("Cannot Delete", "You must have at least one tab.")
                return

            if messagebox.askyesno(
                "Delete Tab",
                f"Are you sure you want to delete '{tab.name}'?\nAll sounds in this tab will be lost.",
            ):
                # Clean up the deleted tab's widgets
                self._cleanup_tab_widgets(tab_idx)

                self.tabs.pop(tab_idx)

                # Reindex per-tab storage to fill the gap
                self._reindex_tab_storage(tab_idx)

                # Adjust current tab index if needed
                if self.current_tab_idx >= len(self.tabs):
                    self.current_tab_idx = len(self.tabs) - 1

                self._refresh_tab_bar()
                self._ensure_tab_built(self.current_tab_idx)
                self._show_tab_only(self.current_tab_idx)
                self._update_current_tab_aliases()
                self._register_hotkeys()
                self._save_config()
                dialog.destroy()

        # Buttons
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Save",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Delete Tab",
            command=delete,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _show_emoji_picker(self, target_var: tk.StringVar, parent):
        """Show emoji picker dialog with colored emojis using PyQt6."""
        # Import the PyQt6-based emoji picker
        from .emoji_picker import pick_emoji, PYQT_AVAILABLE

        if not PYQT_AVAILABLE:
            # Fallback: show a message that PyQt6 is required
            messagebox.showinfo(
                "PyQt6 Required", "For colored emojis, install PyQt6:\npip install PyQt6"
            )
            return

        # Run the PyQt6 picker
        result = pick_emoji()

        if result is not None:
            target_var.set(result)

    def _toggle_ptt_visibility(self):
        """Show or hide PTT settings based on checkbox."""
        if self.ptt_enabled_var.get():
            self.ptt_frame.pack(fill=tk.X, pady=(8, 0))
            # Re-enable PTT in mixer if running
            if self.mixer:
                ptt_key = self.ptt_key_var.get().strip()
                if ptt_key:
                    self.mixer.set_ptt_key(ptt_key)
        else:
            self.ptt_frame.pack_forget()
            # Disable PTT in mixer if running
            if self.mixer:
                self.mixer.set_ptt_key(None)

        # Save config
        self._save_config()

    def _create_soundboard_section(self, parent):
        """Create the soundboard grid section with scrolling.

        Uses CTkScrollableFrame which handles scroll + width natively without
        manual canvas width syncing (the old approach caused massive resize lag).
        """
        # Modern card-style container
        board_frame = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_dark"],
            corner_radius=UI["corner_radius"],
        )
        board_frame.pack(fill=tk.BOTH, expand=True)

        # Header with label
        header_frame = ctk.CTkFrame(board_frame, fg_color="transparent")
        header_frame.pack(fill=tk.X, padx=12, pady=(8, 4))

        header_label = ctk.CTkLabel(
            header_frame,
            text="Soundboard",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_secondary"],
        )
        header_label.pack(side=tk.LEFT)

        # Search & filter bar
        search_bar = ctk.CTkFrame(board_frame, fg_color="transparent")
        search_bar.pack(fill=tk.X, padx=12, pady=(0, 4))

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search_changed())
        self._search_entry = ctk.CTkEntry(
            search_bar,
            textvariable=self._search_var,
            placeholder_text="🔍 Search sounds across all tabs...",
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            placeholder_text_color=COLORS["text_muted"],
            font=self._font_sm,
            height=30,
            corner_radius=6,
        )
        self._search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        _bind_rtl_entry(self._search_entry, self._search_var)

        self._filter_group_var = tk.StringVar(value="All Groups")
        group_values = ["All Groups"]  # Updated after config load via _refresh_group_combo
        self._group_combo = ctk.CTkComboBox(
            search_bar,
            variable=self._filter_group_var,
            values=group_values,
            width=130,
            height=30,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["border"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            font=self._font_xs,
            corner_radius=6,
            state="readonly",
            command=self._on_filter_changed,
        )
        self._group_combo.pack(side=tk.LEFT, padx=(0, 6))

        self._clear_search_btn = ctk.CTkButton(
            search_bar,
            text="✕",
            command=self._clear_search,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            width=30,
            height=30,
        )
        self._clear_search_btn.pack(side=tk.LEFT, padx=(0, 6))

        ctk.CTkButton(
            search_bar,
            text="⚙",
            command=lambda: self._show_manage_groups_dialog(),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            width=30,
            height=30,
        ).pack(side=tk.LEFT)

        # Search results frame (shown when searching, replaces normal tab grid)
        self._search_results_frame: Optional[ctk.CTkFrame] = None
        self._search_result_widgets: List[Dict] = []

        # Scrollable frame — handles scroll + width natively
        self.scrollable_grid = ctk.CTkScrollableFrame(
            board_frame,
            fg_color=COLORS["bg_dark"],
            scrollbar_button_color=COLORS["bg_light"],
            scrollbar_button_hover_color=COLORS["bg_lighter"],
            corner_radius=0,
        )
        self.scrollable_grid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # The grid_frame is the scrollable frame's inner container
        # Tab grid frames are stacked inside it at position (0,0)
        self.grid_frame = self.scrollable_grid
        self.grid_frame.grid_columnconfigure(0, weight=1)
        self.grid_frame.grid_rowconfigure(0, weight=1)

        # Don't create slot widgets here - they're created per-tab lazily
        # After config loads, _build_all_tab_widgets() will create them

    def _create_slot_widgets(self):
        """Legacy method - now builds widgets for current tab only."""
        self._build_tab_widgets(self.current_tab_idx)
        self._update_current_tab_aliases()

    def _build_tab_widgets(self, tab_idx: int):
        """Build all slot widgets for a specific tab.

        Creates a new grid frame for the tab and populates it with slot widgets.
        Each tab has its own isolated set of widgets for instant switching.
        """
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        # If already built, skip
        if self._tab_built.get(tab_idx, False):
            return

        tab = self.tabs[tab_idx]

        # Initialize per-tab storage
        self.tab_slot_buttons[tab_idx] = {}
        self.tab_slot_frames[tab_idx] = {}
        self.tab_slot_progress[tab_idx] = {}
        self.tab_slot_preview_buttons[tab_idx] = {}
        self.tab_slot_edit_buttons[tab_idx] = {}
        self.tab_slot_stop_buttons[tab_idx] = {}
        self.tab_slot_bottom_frames[tab_idx] = {}
        self.tab_slot_emoji_labels[tab_idx] = {}
        self.tab_slot_images[tab_idx] = {}
        self.tab_slot_image_paths[tab_idx] = {}
        self._tab_slot_filled_cache[tab_idx] = {}

        # Create grid frame for this tab, stacked with others at position (0,0).
        # IMPORTANT: only the active tab is left grid()'d at any moment;
        # all other tabs are grid_remove()'d to stop Tk from sending Configure /
        # Map / Unmap events to their entire widget subtrees on every window
        # resize/move/minimize. With many tabs/slots that's a HUGE perf win.
        tab_grid = ctk.CTkFrame(self.grid_frame, fg_color=COLORS["bg_dark"])
        tab_grid.grid(row=0, column=0, sticky="nsew")
        if tab_idx != self.current_tab_idx:
            # Hide non-current tabs immediately so they never receive layout events.
            tab_grid.grid_remove()
        self.tab_grid_frames[tab_idx] = tab_grid

        # Configure columns for even distribution (flex layout)
        for c in range(UI["grid_columns"]):
            tab_grid.grid_columnconfigure(c, weight=1, uniform="slot")

        # Calculate slots needed
        max_idx = max(tab.slots.keys()) if tab.slots else -1
        num_slots = max(max_idx + 2, UI["total_slots"])

        BOTTOM_HEIGHT = 32

        for i in range(num_slots):
            row, col = divmod(i, UI["grid_columns"])

            # ---- Unified single-Canvas slot widget --------------------------
            # Replaces the previous stack of (slot_frame + bottom_frame +
            # main_button + stop_button + menu_button + progress_bar +
            # emoji_label) — 7 Tk widgets per slot — with ONE tk.Canvas that
            # paints all of these itself. With 60+ slots per tab this drops
            # Tk's per-resize layout cost by an order of magnitude and is the
            # single biggest UI perf win in the project.
            #
            # The various tab_slot_* dicts below all point at proxy objects
            # that route legacy `.configure()` / `.set()` / `.pack()` /
            # `.lift()` calls back to the right setter on the SlotWidget, so
            # the rest of the codebase keeps working without changes.
            slot_widget = SlotWidget(
                tab_grid,
                on_click=lambda t=tab_idx, idx=i: self._on_slot_command_for_tab(t, idx),
                on_right_click=lambda e, t=tab_idx, idx=i: self._show_quick_popup_for_tab(
                    e, t, idx
                ),
                on_menu=lambda t=tab_idx, idx=i: self._show_slot_menu(t, idx),
                on_stop=lambda t=tab_idx, idx=i: self._stop_slot_with_flag_for_tab(t, idx),
                height=UI["slot_height"],
            )
            slot_widget.grid(
                row=row,
                column=col,
                padx=UI["slot_padding"],
                pady=UI["slot_padding"],
                sticky="nsew",
            )

            # Proxies — same SlotWidget instance, different facets of the API.
            slot_frame = FrameProxy(slot_widget)
            btn = ButtonProxy(slot_widget)
            progress = ProgressProxy(slot_widget)
            stop_btn = StopButtonProxy(slot_widget)
            menu_btn = MenuButtonProxy(slot_widget)
            preview_btn = menu_btn  # menu replaced both preview + edit
            edit_btn = menu_btn
            emoji_label = EmojiLabelProxy(slot_widget)
            bottom_frame = menu_btn  # legacy ref; never poked directly

            self.tab_slot_frames[tab_idx][i] = slot_frame
            self.tab_slot_buttons[tab_idx][i] = btn
            self.tab_slot_progress[tab_idx][i] = progress
            self.tab_slot_preview_buttons[tab_idx][i] = preview_btn
            self.tab_slot_edit_buttons[tab_idx][i] = edit_btn
            self.tab_slot_stop_buttons[tab_idx][i] = stop_btn
            self.tab_slot_bottom_frames[tab_idx][i] = bottom_frame
            self.tab_slot_emoji_labels[tab_idx][i] = emoji_label

        self._tab_built[tab_idx] = True

        # Update slot appearances
        for i in range(num_slots):
            self._update_slot_button_for_tab(tab_idx, i)

        # Warm-up: force Tk to lay out and CTk to draw all widgets NOW so the
        # first switch to this tab doesn't trigger a layout/draw cascade.
        # Only needed for non-current tabs (current tab is already visible).
        # SKIP entirely if the user is currently moving/resizing the window —
        # update_idletasks() is synchronous and would stall the resize. The
        # tab will warm up lazily on first visit instead (acceptable trade-off).
        if tab_idx != self.current_tab_idx and time.time() >= getattr(
            self, "_resize_active_until", 0.0
        ):
            try:
                # The grid is currently grid_remove()'d; re-add briefly to
                # force layout, flush, then remove again. This pre-warms
                # widget sizes and CTk Canvas renders.
                tab_grid.grid()
                tab_grid.lower()  # Ensure it stays beneath the visible tab
                self.root.update_idletasks()
                tab_grid.grid_remove()
            except Exception:
                pass

    def _show_slot_menu(self, tab_idx: int, slot_idx: int):
        """Show a popup menu with Preview and Edit options for a slot.

        Replaces the previous separate preview/edit buttons to reduce per-slot
        widget count (4 widgets per slot instead of 5).
        """
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label="🔊 Preview",
            command=lambda: self._preview_slot_for_tab(tab_idx, slot_idx),
        )
        menu.add_command(
            label="✏️ Edit",
            command=lambda: self._configure_slot_for_tab(tab_idx, slot_idx),
        )
        # Position the menu just below/right of the menu button.
        try:
            btn = self.tab_slot_preview_buttons.get(tab_idx, {}).get(slot_idx)
            if btn is not None and btn.winfo_exists():
                x = btn.winfo_rootx()
                y = btn.winfo_rooty() + btn.winfo_height()
                menu.tk_popup(x, y)
            else:
                # Fallback: pointer position
                menu.tk_popup(self.root.winfo_pointerx(), self.root.winfo_pointery())
        finally:
            menu.grab_release()

    def _ensure_tab_built(self, tab_idx: int):
        """Ensure a tab's widgets are built. Builds lazily if needed."""
        if not self._tab_built.get(tab_idx, False):
            self._build_tab_widgets(tab_idx)

    def _update_current_tab_aliases(self):
        """Update legacy widget aliases to point at current tab's widgets."""
        tab_idx = self.current_tab_idx
        if tab_idx not in self.tab_slot_buttons:
            return
        self.slot_buttons = self.tab_slot_buttons.get(tab_idx, {})
        self.slot_frames = self.tab_slot_frames.get(tab_idx, {})
        self.slot_progress = self.tab_slot_progress.get(tab_idx, {})
        self.slot_preview_buttons = self.tab_slot_preview_buttons.get(tab_idx, {})
        self.slot_edit_buttons = self.tab_slot_edit_buttons.get(tab_idx, {})
        self.slot_stop_buttons = self.tab_slot_stop_buttons.get(tab_idx, {})
        self.slot_bottom_frames = self.tab_slot_bottom_frames.get(tab_idx, {})
        self.slot_images = self.tab_slot_images.get(tab_idx, {})
        self.slot_image_paths = self.tab_slot_image_paths.get(tab_idx, {})
        self.slot_emoji_labels = self.tab_slot_emoji_labels.get(tab_idx, {})
        self._slot_filled_cache = self._tab_slot_filled_cache.get(tab_idx, {})

    def _cleanup_tab_widgets(self, tab_idx: int):
        """Clean up all widget storage for a tab that's being deleted."""
        # Destroy the grid frame (destroys all child widgets)
        if tab_idx in self.tab_grid_frames:
            self.tab_grid_frames[tab_idx].destroy()
            del self.tab_grid_frames[tab_idx]

        # Clean up per-tab storage
        for storage in [
            self.tab_slot_buttons,
            self.tab_slot_frames,
            self.tab_slot_progress,
            self.tab_slot_preview_buttons,
            self.tab_slot_edit_buttons,
            self.tab_slot_stop_buttons,
            self.tab_slot_bottom_frames,
            self.tab_slot_emoji_labels,
            self.tab_slot_images,
            self.tab_slot_image_paths,
            self._tab_slot_filled_cache,
        ]:
            if tab_idx in storage:
                del storage[tab_idx]

        if tab_idx in self._tab_built:
            del self._tab_built[tab_idx]

    def _reindex_tab_storage(self, deleted_idx: int):
        """Reindex per-tab storage after a tab is deleted.

        Shifts all tab indices > deleted_idx down by 1.
        """
        storages = [
            self.tab_grid_frames,
            self.tab_slot_buttons,
            self.tab_slot_frames,
            self.tab_slot_progress,
            self.tab_slot_preview_buttons,
            self.tab_slot_edit_buttons,
            self.tab_slot_stop_buttons,
            self.tab_slot_bottom_frames,
            self.tab_slot_emoji_labels,
            self.tab_slot_images,
            self.tab_slot_image_paths,
            self._tab_slot_filled_cache,
            self._tab_built,
        ]

        for storage in storages:
            # Find all keys greater than deleted_idx and shift them down
            keys_to_shift = [k for k in storage.keys() if isinstance(k, int) and k > deleted_idx]
            keys_to_shift.sort()  # Process in order
            for old_key in keys_to_shift:
                storage[old_key - 1] = storage[old_key]
                del storage[old_key]

    def _build_all_tab_widgets(self):
        """Build current tab immediately, then build remaining tabs in background.

        This keeps startup fast (only current tab blocks) while ensuring
        tab switching is instant (other tabs are pre-built before user clicks them).
        """
        self._build_tab_widgets(self.current_tab_idx)
        self._show_tab_only(self.current_tab_idx)
        self._update_current_tab_aliases()

        # Build remaining tabs in background.
        # Use after(15ms) instead of after_idle so the queue actually drains
        # in a few hundred ms — after_idle can stall indefinitely if the user
        # is interacting, leaving first-time tab switches laggy.
        remaining = [i for i in range(len(self.tabs)) if i != self.current_tab_idx]
        if remaining:
            self.root.after(50, lambda: self._build_tabs_incrementally(remaining))

    def _build_tabs_incrementally(self, remaining: list):
        """Build one tab per ~15ms tick to avoid blocking the UI.

        Uses after(15) instead of after_idle so the queue drains predictably
        even while the user is moving/resizing the window. This guarantees
        all tabs are pre-built within ~N*15ms of startup, so first-time tab
        switches don't trigger a synchronous build on the UI thread.
        """
        if not remaining:
            return
        # Don't fight resize/move; defer until window is settled.
        if time.time() < getattr(self, "_resize_active_until", 0.0):
            self.root.after(150, lambda: self._build_tabs_incrementally(remaining))
            return
        tab_idx = remaining.pop(0)
        if not self._tab_built.get(tab_idx, False):
            self._build_tab_widgets(tab_idx)
        if remaining:
            self.root.after(15, lambda: self._build_tabs_incrementally(remaining))

    def _prioritize_tab_build(self, tab_idx: int):
        """If `tab_idx` is in the background-build queue, build it now.

        Called from `_switch_tab` so first-time switches aren't laggy.
        """
        if not self._tab_built.get(tab_idx, False):
            self._build_tab_widgets(tab_idx)

    def _animate_progress(self):
        """Update progress bars for playing sounds.

        Performance: When nothing is playing and the DJ Looper panel has nothing
        to show, sleep for 250ms instead of 50ms. This drops idle CPU usage to
        near zero and removes the per-frame widget work that used to compound
        with Configure events during window resize/move/minimize.
        """
        # Resize/move backoff: while the user is dragging the window, skip
        # all per-frame widget work and reschedule far in the future. This
        # prevents progress-bar `.set()` calls from interleaving with CTk's
        # Canvas redraws on every Configure event.
        if time.time() < getattr(self, "_resize_active_until", 0.0):
            self.root.after(150, self._animate_progress)
            return

        # Idle fast-path: no sounds playing anywhere, nothing to animate.
        if not self.playing_slots and not self.preview_slots:
            # Make sure the DJ Looper panel reflects the empty state, but only
            # once (not every 250ms) — only if it still thinks sounds are playing.
            panel = getattr(self, "now_playing_panel", None)
            if panel is not None and panel.is_visible and panel.sound_items:
                panel.update([], {})
            self.root.after(250, self._animate_progress)
            return

        current_time = time.time()
        finished = []

        # Cache of last-set progress values (per slot_idx). Only call .set()
        # when the rounded value has actually changed, otherwise CTk does a
        # full Canvas redraw of the progress bar 20× per second for nothing.
        # 1% granularity is invisible to the eye and cuts redraws by ~80%.
        last_progress = self._last_progress_values  # alias for speed

        for slot_idx, play_info in self.playing_slots.items():
            elapsed = current_time - play_info["start_time"]
            duration = play_info["duration"]
            progress_ratio = min(elapsed / duration, 1.0) if duration > 0 else 1.0

            # Only update progress bar if this slot's sound is from the current tab
            if play_info["tab_idx"] == self.current_tab_idx:
                pb = self.slot_progress.get(slot_idx)
                if pb is not None:
                    rounded = round(progress_ratio, 2)
                    if last_progress.get(slot_idx) != rounded:
                        pb.set(progress_ratio)
                        last_progress[slot_idx] = rounded

            # Check if finished
            if progress_ratio >= 1.0:
                finished.append(slot_idx)

        # Reset finished playing slots
        for slot_idx in finished:
            tab_idx = self.playing_slots[slot_idx]["tab_idx"]
            del self.playing_slots[slot_idx]
            last_progress.pop(slot_idx, None)
            # Cheap visual reset: color + hide stop button + reset progress.
            # Avoids disk I/O (image reload) that full _update_slot_button_for_tab does.
            self._reset_slot_visual(tab_idx, slot_idx)

        # Handle preview slots (same logic but with green color)
        preview_finished = []

        for slot_idx, play_info in self.preview_slots.items():
            elapsed = current_time - play_info["start_time"]
            duration = play_info["duration"]
            progress_ratio = min(elapsed / duration, 1.0) if duration > 0 else 1.0

            # Only update progress bar if this slot's sound is from the current tab
            if play_info["tab_idx"] == self.current_tab_idx:
                pb = self.slot_progress.get(slot_idx)
                if pb is not None:
                    rounded = round(progress_ratio, 2)
                    # Use a separate key to not collide with playing-slots cache
                    cache_key = ("preview", slot_idx)
                    if last_progress.get(cache_key) != rounded:
                        pb.set(progress_ratio)
                        last_progress[cache_key] = rounded

            # Check if finished
            if progress_ratio >= 1.0:
                preview_finished.append(slot_idx)

        # Reset finished preview slots
        for slot_idx in preview_finished:
            tab_idx = self.preview_slots[slot_idx]["tab_idx"]
            del self.preview_slots[slot_idx]
            last_progress.pop(("preview", slot_idx), None)
            self._reset_slot_visual(tab_idx, slot_idx)

        # Update Now Playing panel if visible — but throttled to ~5fps because
        # _update_item reconfigures ~15 CTk widgets per playing sound and each
        # .configure() triggers a Canvas redraw. At 20fps with 3 sounds open
        # that's 900 widget redraws/sec on top of CTk's own resize redraws.
        panel = getattr(self, "now_playing_panel", None)
        if panel is not None and panel.is_visible:
            if current_time - self._last_panel_update >= 0.2:
                if self.mixer and self.mixer.running and self.playing_slots:
                    playing_sounds = self.mixer.get_playing_sounds()
                    panel.update(playing_sounds, self.playing_slots)
                elif panel.sound_items:
                    panel.update([], {})
                self._last_panel_update = current_time

        # Schedule next frame (20fps is enough for progress bars)
        self.root.after(50, self._animate_progress)

    def _reset_slot_visual(self, tab_idx: int, slot_idx: int):
        """Lightweight visual reset for a slot that just finished playing.

        Cheaper than a full `_update_slot_button_for_tab()` call because it only
        touches color/progress/stop-button instead of reloading the slot image
        and reconfiguring every property. Called from the animation loop.
        """
        try:
            if tab_idx < 0 or tab_idx >= len(self.tabs):
                return
            tab = self.tabs[tab_idx]
            slot = tab.slots.get(slot_idx)

            # Reset button color to its default (filled = slot color, empty = transparent)
            btn = self.tab_slot_buttons.get(tab_idx, {}).get(slot_idx)
            if btn is not None:
                if slot is not None:
                    fg = slot.color if slot.color else COLORS["blurple"]
                else:
                    fg = "transparent"
                try:
                    btn.configure(fg_color=fg)
                except Exception:
                    pass

            # Reset progress bar to 0
            pb = self.tab_slot_progress.get(tab_idx, {}).get(slot_idx)
            if pb is not None:
                try:
                    pb.set(0)
                except Exception:
                    pass

            # Hide stop button if present
            stop_btn = self.tab_slot_stop_buttons.get(tab_idx, {}).get(slot_idx)
            if stop_btn is not None:
                try:
                    stop_btn.pack_forget()
                except Exception:
                    pass
        except Exception:
            pass

    def _calculate_slots_for_tab(self, tab: SoundTab) -> int:
        """Calculate how many slots a tab needs (max slot index + 2, minimum 12)."""
        max_idx = max(tab.slots.keys()) if tab.slots else -1
        return max(max_idx + 2, UI["total_slots"])

    # =================================================================
    # Search / Filter / Group Management
    # =================================================================

    def _get_all_groups(self) -> List[str]:
        """Get all available groups (user-managed)."""
        return list(self._custom_groups)

    def _refresh_group_combo(self):
        """Refresh the group filter dropdown with current available groups."""
        if hasattr(self, "_group_combo"):
            values = ["All Groups"] + self._get_all_groups()
            self._group_combo.configure(values=values)

    def _show_manage_groups_dialog(self, rebuild_callback=None):
        """Show a dialog to add and remove custom groups."""
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Manage Groups")
        dialog.geometry("400x500")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, True)

        # Center on parent
        dialog.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width() - 400) // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - 500) // 2
        dialog.geometry(f"+{x}+{y}")

        # Title
        ctk.CTkLabel(
            dialog,
            text="Manage Sound Groups",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(padx=16, pady=(16, 4))

        ctk.CTkLabel(
            dialog,
            text="Built-in groups cannot be removed. Custom groups can be added or removed.",
            font=self._font_xs,
            text_color=COLORS["text_muted"],
            wraplength=360,
        ).pack(padx=16, pady=(0, 8))

        # Scrollable list of groups
        list_frame = ctk.CTkScrollableFrame(
            dialog,
            fg_color=COLORS["bg_medium"],
            corner_radius=8,
        )
        list_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        def _rebuild_list():
            for w in list_frame.winfo_children():
                w.destroy()

            if not self._custom_groups:
                ctk.CTkLabel(
                    list_frame,
                    text="No groups yet. Add one below.",
                    font=self._font_xs,
                    text_color=COLORS["text_muted"],
                ).pack(pady=20)
                return

            for g in list(self._custom_groups):
                row = ctk.CTkFrame(list_frame, fg_color="transparent")
                row.pack(fill=tk.X, padx=4, pady=2)
                ctk.CTkLabel(
                    row,
                    text=g,
                    font=self._font_sm,
                    text_color=COLORS["text_primary"],
                    anchor="w",
                ).pack(side=tk.LEFT, fill=tk.X, expand=True)

                def _remove(group_name=g):
                    self._remove_custom_group(group_name)
                    _rebuild_list()
                    self._refresh_group_combo()
                    if rebuild_callback:
                        rebuild_callback()

                ctk.CTkButton(
                    row,
                    text="✕",
                    command=_remove,
                    fg_color=COLORS["red"],
                    hover_color=COLORS["red_hover"],
                    font=self._font_xs,
                    corner_radius=4,
                    width=28,
                    height=24,
                ).pack(side=tk.RIGHT, padx=(0, 4))

        _rebuild_list()

        # Add new group section
        add_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        add_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        new_var = tk.StringVar()
        new_entry = ctk.CTkEntry(
            add_frame,
            textvariable=new_var,
            placeholder_text="New group name...",
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
            font=self._font_sm,
            height=32,
            corner_radius=6,
        )
        new_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        _bind_rtl_entry(new_entry, new_var)

        def _add():
            name = new_var.get().strip()
            if not name:
                return
            if name not in self._custom_groups:
                self._custom_groups.append(name)
                _rebuild_list()
                self._refresh_group_combo()
                if rebuild_callback:
                    rebuild_callback()
            new_var.set("")

        ctk.CTkButton(
            add_frame,
            text="+ Add",
            command=_add,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=self._font_sm,
            corner_radius=6,
            width=70,
            height=32,
        ).pack(side=tk.LEFT)

        # Close button
        ctk.CTkButton(
            dialog,
            text="Done",
            command=dialog.destroy,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=self._font_sm,
            corner_radius=6,
            height=34,
        ).pack(padx=16, pady=(0, 16))

        new_entry.focus_set()

    def _remove_custom_group(self, group_name: str):
        """Remove a custom group and clean it from all slots."""
        if group_name in self._custom_groups:
            self._custom_groups.remove(group_name)
        # Remove from all slots across all tabs
        for tab in self.tabs:
            for slot in tab.slots.values():
                if group_name in slot.groups:
                    slot.groups.remove(group_name)
        self._save_config()

    def _on_search_changed(self):
        """Called when the search entry text changes."""
        self._search_query = self._search_var.get().strip().lower()
        self._run_search()

    def _on_filter_changed(self, _value: str = ""):
        """Called when the group filter combobox changes."""
        selected = self._filter_group_var.get()
        self._filter_group = None if selected == "All Groups" else selected
        self._run_search()

    def _clear_search(self):
        """Clear search query and group filter, return to normal view."""
        self._search_var.set("")
        self._filter_group_var.set("All Groups")
        self._filter_group = None
        self._search_query = ""
        self._hide_search_results()

    def _run_search(self):
        """Execute search across all tabs and show results."""
        query = self._search_query
        group = self._filter_group

        # If both empty, hide results and show normal grid
        if not query and not group:
            self._hide_search_results()
            return

        results: List[Dict] = []
        for tab_idx, tab in enumerate(self.tabs):
            for slot_idx, slot in tab.slots.items():
                # Match query against name, hotkey, emoji, groups
                if query:
                    groups_str = " ".join(g.lower() for g in slot.groups)
                    searchable = " ".join(
                        [
                            slot.name.lower(),
                            (slot.hotkey or "").lower(),
                            (slot.emoji or ""),
                            groups_str,
                        ]
                    )
                    if query not in searchable:
                        continue

                # Match group filter
                if group and group not in slot.groups:
                    continue

                results.append(
                    {
                        "tab_idx": tab_idx,
                        "tab_name": tab.name,
                        "tab_emoji": tab.emoji or "",
                        "slot_idx": slot_idx,
                        "slot": slot,
                    }
                )

        self._search_results = results
        self._show_search_results()

    def _show_search_results(self):
        """Show search results overlay, hiding normal tab grids."""
        # Hide all tab grid frames
        for frame in self.tab_grid_frames.values():
            frame.grid_remove()

        # Destroy old results frame if exists
        if self._search_results_frame is not None:
            self._search_results_frame.destroy()

        self._search_results_frame = ctk.CTkFrame(self.grid_frame, fg_color=COLORS["bg_dark"])
        self._search_results_frame.grid(row=0, column=0, sticky="nsew")
        self._search_results_frame.tkraise()

        for c in range(UI["grid_columns"]):
            self._search_results_frame.grid_columnconfigure(c, weight=1, uniform="slot")

        results = self._search_results or []

        if not results:
            no_results = ctk.CTkLabel(
                self._search_results_frame,
                text="No sounds found",
                font=self._font_sm,
                text_color=COLORS["text_muted"],
            )
            no_results.grid(row=0, column=0, columnspan=UI["grid_columns"], pady=40)
            return

        # Header showing result count
        count_label = ctk.CTkLabel(
            self._search_results_frame,
            text=f"Found {len(results)} sound{'s' if len(results) != 1 else ''}",
            font=self._font_xs,
            text_color=COLORS["text_muted"],
        )
        count_label.grid(
            row=0, column=0, columnspan=UI["grid_columns"], sticky="w", padx=8, pady=(4, 2)
        )

        self._search_result_widgets = []

        for i, result in enumerate(results):
            row = (i // UI["grid_columns"]) + 1  # +1 for count label row
            col = i % UI["grid_columns"]
            slot: SoundSlot = result["slot"]

            slot_color = slot.color or COLORS["blurple"]

            frame = ctk.CTkFrame(
                self._search_results_frame,
                fg_color=COLORS["bg_medium"],
                corner_radius=UI["slot_corner_radius"],
                height=UI["slot_height"],
            )
            frame.grid(
                row=row,
                column=col,
                padx=UI["slot_padding"],
                pady=UI["slot_padding"],
                sticky="nsew",
            )
            frame.pack_propagate(False)

            # Bottom frame with tab source info
            bottom = ctk.CTkFrame(frame, fg_color="transparent", height=32)
            bottom.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 4))

            tab_info = f"{result['tab_emoji']} {result['tab_name']}"
            group_text = f" · {', '.join(slot.groups)}" if slot.groups else ""
            ctk.CTkLabel(
                bottom,
                text=f"{tab_info}{group_text}",
                font=ctk.CTkFont(family=FONTS["family"], size=9),
                text_color=COLORS["text_muted"],
                anchor="w",
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

            # Main button
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""
            max_name_len = 32
            display_name = (
                slot.name[:max_name_len] + "…" if len(slot.name) > max_name_len else slot.name
            )
            display_text = _fix_rtl_text(f"{display_name}{hk}")

            # Load image if available
            photo = None
            if slot.image_path and os.path.exists(slot.image_path):
                photo = self._load_slot_image(slot.image_path)

            tab_idx = result["tab_idx"]
            slot_idx = result["slot_idx"]

            btn = ctk.CTkButton(
                frame,
                text=display_text,
                image=photo,
                fg_color=slot_color,
                hover_color=COLORS["bg_lighter"],
                text_color=COLORS["text_primary"],
                font=self._font_sm,
                corner_radius=UI["slot_corner_radius"],
                anchor="center",
                cursor="hand2",
                compound="top",
                command=lambda t=tab_idx, s=slot_idx: self._play_slot_from_search(t, s),
            )
            btn.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4)

            # Emoji label
            if slot.emoji:
                emoji_lbl = ctk.CTkLabel(
                    frame,
                    text=slot.emoji,
                    font=ctk.CTkFont(family="Segoe UI Emoji", size=18),
                    text_color=COLORS["text_primary"],
                    fg_color="transparent",
                    width=24,
                    height=24,
                )
                emoji_lbl.place(x=6, y=4)

            self._search_result_widgets.append(
                {
                    "frame": frame,
                    "btn": btn,
                    "tab_idx": tab_idx,
                    "slot_idx": slot_idx,
                    "photo": photo,
                }
            )

    def _hide_search_results(self):
        """Hide search results and restore normal tab grid view."""
        self._search_results = None

        if self._search_results_frame is not None:
            self._search_results_frame.destroy()
            self._search_results_frame = None

        self._search_result_widgets = []

        # Re-show only the current tab (hides everything else)
        self._show_tab_only(self.current_tab_idx)

    def _play_slot_from_search(self, tab_idx: int, slot_idx: int):
        """Play a sound from a search result (may be on a different tab)."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        tab = self.tabs[tab_idx]
        slot = tab.slots.get(slot_idx)
        if not slot or not self.mixer or not self.mixer.running:
            return

        try:
            duration = self.mixer.play_sound(
                slot.file_path,
                volume=slot.volume,
                speed=slot.speed,
                preserve_pitch=slot.preserve_pitch,
                loop=slot.loop,
                loop_count=slot.loop_count,
                loop_delay=slot.loop_delay,
            )
            self.playing_slots[slot_idx] = {
                "start_time": time.time(),
                "duration": duration,
                "tab_idx": tab_idx,
            }
            # Update the slot on its own tab if built
            if tab_idx in self.tab_slot_buttons and slot_idx in self.tab_slot_buttons.get(
                tab_idx, {}
            ):
                self._update_slot_button_for_tab(tab_idx, slot_idx)
                if (
                    tab_idx in self.tab_slot_stop_buttons
                    and slot_idx in self.tab_slot_stop_buttons.get(tab_idx, {})
                ):
                    self.tab_slot_stop_buttons[tab_idx][slot_idx].pack(side=tk.LEFT, padx=(0, 2))
            self.status_var.set(f"Playing: {slot.name}")
        except Exception as e:
            self.status_var.set(f"Error: {e}")

    def _refresh_slot_buttons(self):
        """Refresh slot buttons for current tab.

        With per-tab frames, this just ensures the current tab is built and aliases are updated.
        No widget updates needed - per-tab widgets are pre-built.
        """
        # Ensure tab is built
        self._ensure_tab_built(self.current_tab_idx)
        self._update_current_tab_aliases()

    def _refresh_current_tab_slots(self):
        """Update all slot appearances for the current tab (after content changes)."""
        tab_idx = self.current_tab_idx
        if tab_idx not in self.tab_slot_buttons:
            return
        for slot_idx in self.tab_slot_buttons[tab_idx]:
            self._update_slot_button_for_tab(tab_idx, slot_idx)

    def _ensure_slots_for_tab(self, tab_idx: int):
        """Ensure a tab has enough slot widgets for its content + empty slots.

        Rebuilds the tab if needed (e.g., after adding content to the last empty slot).
        """
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]
        needed_slots = self._calculate_slots_for_tab(tab)

        # Check if we have enough widgets
        current_slots = len(self.tab_slot_buttons.get(tab_idx, {}))

        if current_slots < needed_slots:
            # Need more slots - rebuild this tab
            self._tab_built[tab_idx] = False
            if tab_idx in self.tab_grid_frames:
                self.tab_grid_frames[tab_idx].destroy()
                del self.tab_grid_frames[tab_idx]
            self._build_tab_widgets(tab_idx)

            # Re-show only the current tab (hides everything else)
            self._show_tab_only(self.current_tab_idx)

            # Update aliases if this is the current tab
            if tab_idx == self.current_tab_idx:
                self._update_current_tab_aliases()

    def _create_status_bar(self, parent):
        """Create the status bar at the bottom."""
        status_frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_dark"], corner_radius=UI["button_corner_radius"], height=28
        )
        status_frame.pack(fill=tk.X, pady=(8, 0))
        status_frame.pack_propagate(False)

        self.status_var = tk.StringVar(value="Ready - Select devices and click Start")
        self._last_status_text = self.status_var.get()
        # Skip StringVar.set() when text hasn't changed — Tk still fires write
        # traces and the bound CTkLabel redraws even if the value is identical.
        # With many _play_slot/_stop_slot calls this adds up. Wrap once.
        _orig_status_set = self.status_var.set

        def _dedup_status_set(value, *a, **kw):
            if value == self._last_status_text:
                return
            self._last_status_text = value
            _orig_status_set(value, *a, **kw)

        self.status_var.set = _dedup_status_set  # type: ignore[method-assign]
        self.status_label = ctk.CTkLabel(
            status_frame,
            textvariable=self.status_var,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self.status_label.pack(fill=tk.X, padx=10, pady=4)

    def _toggle_stream(self):
        """Start or stop the audio stream."""
        if self.mixer and self.mixer.running:
            self.mixer.stop()
            self.toggle_btn.configure(
                text="▶ Start Stream", fg_color=COLORS["green"], hover_color=COLORS["green_hover"]
            )
            self.status_var.set("Stopped")
        else:
            try:
                input_idx = int(self.input_var.get().split(":")[0])
                output_idx = int(self.output_var.get().split(":")[0])
                self.mixer = AudioMixer(input_idx, output_idx, sound_cache=self.sound_cache)
                # Apply PTT key if enabled and configured
                ptt_key = None
                if self.ptt_enabled_var.get():
                    ptt_key = self.ptt_key_var.get().strip()
                    if ptt_key:
                        self.mixer.set_ptt_key(ptt_key)
                # Apply noise suppression settings
                if hasattr(self, "noise_suppress_var"):
                    self.mixer.noise_suppressor.enabled = self.noise_suppress_var.get()
                    self.mixer.noise_suppressor.set_strength(self.ns_strength_var.get() / 100.0)
                self.mixer.start()
                # Save device selection
                self._save_config()
                self.toggle_btn.configure(
                    text="⏹ Stop Stream", fg_color=COLORS["red"], hover_color=COLORS["red_hover"]
                )
                ptt_status = f" (PTT: {ptt_key})" if ptt_key else ""
                self.status_var.set(f"Running - Mic → Virtual Cable{ptt_status}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start:\n{e}")

    def _update_mic_volume(self, _=None):
        """Update microphone volume from slider."""
        if self.mixer:
            self.mixer.mic_volume = self.mic_volume_var.get() / 100.0

    def _toggle_mic_mute(self):
        """Toggle microphone mute state."""
        if self.mixer:
            self.mixer.mic_muted = self.mic_mute_var.get()

    def _toggle_monitor(self):
        """Toggle local speaker monitoring (hear sounds through speakers)."""
        if self.mixer:
            self.mixer.set_monitor_enabled(self.monitor_var.get())

    def _toggle_noise_suppression(self):
        """Toggle mic noise suppression (Krisp replacement)."""
        enabled = self.noise_suppress_var.get()
        if self.mixer:
            self.mixer.noise_suppressor.enabled = enabled
            self.mixer.noise_suppressor.set_strength(self.ns_strength_var.get() / 100.0)
            # Reset context buffer so we don't carry stale audio when toggling
            self.mixer.noise_suppressor.reset()
        self._save_config()

    def _update_ns_strength(self, _=None):
        """Update noise suppression strength from slider (0-100 → 0.0-1.0)."""
        if self.mixer:
            self.mixer.noise_suppressor.set_strength(self.ns_strength_var.get() / 100.0)
        # Debounced save (slider drag triggers many calls)
        self._save_config()

    def _toggle_now_playing_panel(self):
        """Toggle the DJ Looper side panel visibility."""
        if self.now_playing_panel.is_visible:
            self.now_playing_panel.hide()
            self.now_playing_btn.configure(
                fg_color=COLORS["bg_light"],
                hover_color=COLORS["bg_lighter"],
            )
        else:
            self.now_playing_panel.show()
            self.now_playing_btn.configure(
                fg_color=COLORS["blurple"],
                hover_color=COLORS["blurple_hover"],
            )
        # Let window resize to fit new content
        self.root.after(50, self._finalize_window_size)

    def _on_panel_stop_sound(self, sound_id: str):
        """Handle stop button click from the Now Playing panel."""
        if self.mixer:
            self.mixer.stop_sound(sound_id)

        # Update GUI state - parse sound_id to get tab_idx and slot_idx
        # Format is "tab_idx_slot_idx"
        try:
            parts = sound_id.split("_")
            if len(parts) >= 2:
                tab_idx = int(parts[0])
                slot_idx = int(parts[1])

                # Remove from playing_slots
                if slot_idx in self.playing_slots:
                    del self.playing_slots[slot_idx]

                # Update UI
                if tab_idx in self.tab_slot_buttons and slot_idx in self.tab_slot_buttons.get(
                    tab_idx, {}
                ):
                    self._update_slot_button_for_tab(tab_idx, slot_idx)
                if tab_idx in self.tab_slot_progress and slot_idx in self.tab_slot_progress.get(
                    tab_idx, {}
                ):
                    self.tab_slot_progress[tab_idx][slot_idx].set(0)
                if (
                    tab_idx in self.tab_slot_stop_buttons
                    and slot_idx in self.tab_slot_stop_buttons.get(tab_idx, {})
                ):
                    self.tab_slot_stop_buttons[tab_idx][slot_idx].pack_forget()
        except (ValueError, IndexError):
            pass

        self.status_var.set("Stopped sound")

    def _stop_all_sounds(self):
        """Stop all currently playing sounds (Discord and preview)."""
        if self.mixer:
            self.mixer.stop_all_sounds()

        # Stop any previews
        self._stop_all_previews()

        # Clear playing state and hide stop buttons
        for slot_idx in list(self.playing_slots.keys()):
            tab_idx = self.playing_slots[slot_idx].get("tab_idx")
            del self.playing_slots[slot_idx]
            if tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].set(0)
                if slot_idx in self.slot_stop_buttons:
                    self.slot_stop_buttons[slot_idx].pack_forget()

        self.status_var.set("Stopped all sounds")

    def _stop_slot(self, slot_idx: int):
        """Stop the sound playing in a specific slot (both Discord play and preview)."""
        # Stop preview if this slot is previewing
        if slot_idx in self.preview_slots:
            self._stop_preview(slot_idx)

        # Get the tab this slot belongs to for sound_id
        if slot_idx in self.playing_slots:
            tab_idx = self.playing_slots[slot_idx].get("tab_idx", self.current_tab_idx)
        else:
            tab_idx = self.current_tab_idx

        sound_id = f"{tab_idx}_{slot_idx}"

        # Stop only this specific sound in the mixer
        if self.mixer:
            self.mixer.stop_sound(sound_id)

        # Clear this slot's playing state
        if slot_idx in self.playing_slots:
            del self.playing_slots[slot_idx]

        # Update UI for this slot
        if tab_idx == self.current_tab_idx:
            self._update_slot_button(slot_idx)
            if slot_idx in self.slot_progress:
                self.slot_progress[slot_idx].set(0)
            if slot_idx in self.slot_stop_buttons:
                self.slot_stop_buttons[slot_idx].pack_forget()

        self.status_var.set("Stopped")

    def _stop_slot_with_flag(self, slot_idx: int):
        """Stop a slot and record the time so _handle_slot_click ignores re-plays."""
        self._just_stopped_slot = slot_idx
        self._just_stopped_at = time.time()
        self._stop_slot(slot_idx)
        # If a press is active on this slot, cancel it
        if self._click_active and self._click_slot == slot_idx:
            self._reset_click_state()

    def _delete_slot(self, slot_idx: int):
        """Delete a sound from a slot (assumes confirmation already done)."""
        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            return

        slot = tab.slots[slot_idx]

        # Check if any other slot uses this sound before removing from cache
        other_uses = any(
            s.file_path == slot.file_path
            for t in self.tabs
            for idx, s in t.slots.items()
            if not (t == tab and idx == slot_idx)
        )
        if not other_uses:
            self.sound_cache.remove_sound(slot.file_path, delete_file=True)

        del tab.slots[slot_idx]
        self._update_slot_button_for_tab(self.current_tab_idx, slot_idx)
        self._register_hotkeys()
        self._save_config()
        self.status_var.set(f"Deleted: {slot.name}")

    def _record_ptt_key(self):
        """Record a key or mouse button press to use as PTT key."""
        if not HOTKEYS_AVAILABLE:
            messagebox.showwarning(
                "Keyboard Module Required",
                "The keyboard module is required for PTT functionality.\n"
                "Install it with: pip install keyboard",
            )
            return

        self.ptt_record_btn.configure(text="Press key...", fg_color=COLORS["red"])
        self.ptt_status_label.configure(
            text="Press key or mouse button (5s)...", text_color=COLORS["blurple"]
        )
        self.root.update()

        # Store hook references as instance variables so we can unhook them
        self._ptt_hook = None
        self._ptt_mouse_hook = None
        self._ptt_recording = True
        self._ptt_timeout_id = None

        def cleanup_hooks():
            """Clean up all hooks."""
            if self._ptt_hook:
                try:
                    keyboard.unhook(self._ptt_hook)
                except Exception:
                    pass
            if self._ptt_mouse_hook:
                try:
                    import mouse  # type: ignore[import-untyped]

                    mouse.unhook(self._ptt_mouse_hook)
                except Exception:
                    pass

        def cancel_recording():
            """Cancel recording after timeout."""
            if self._ptt_recording:
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_record_btn.configure(text="⏺ Record Key", fg_color=COLORS["blurple"])
                self.ptt_status_label.configure(
                    text="Recording timed out", text_color=COLORS["red"]
                )

        def set_ptt_key(key_name: str):
            """Set the PTT key and update UI."""
            if not self._ptt_recording:
                return

            # Cancel the timeout
            if self._ptt_timeout_id:
                try:
                    self.root.after_cancel(self._ptt_timeout_id)
                except RuntimeError:
                    pass

            # Schedule UI updates on main thread (Tkinter is not thread-safe)
            def update_ui():
                self._ptt_recording = False
                cleanup_hooks()
                self.ptt_key_var.set(key_name)
                self.ptt_record_btn.configure(text="⏺ Record Key", fg_color=COLORS["blurple"])
                self.ptt_status_label.configure(text=f"PTT: {key_name}", text_color=COLORS["green"])

                # Update mixer if running
                if self.mixer:
                    self.mixer.set_ptt_key(key_name)

                # Save config
                self._save_config()

            try:
                self.root.after(0, update_ui)
            except RuntimeError:
                pass

        def on_key(event):
            set_ptt_key(event.name)
            return False  # Stop propagation

        def on_mouse_event(event):
            """Handle mouse button events using mouse library."""
            # Check if it's a button event (has event_type and button attributes)
            event_type = getattr(event, "event_type", None)
            button = getattr(event, "button", None)

            if event_type == "down" and button:
                # Map mouse button names: x = mouse5, x2 = mouse4 (to match Discord)
                if button == "x":
                    button_name = "mouse5"
                elif button == "x2":
                    button_name = "mouse4"
                elif button == "left":
                    button_name = "mouse1"
                elif button == "right":
                    button_name = "mouse2"
                elif button == "middle":
                    button_name = "mouse3"
                else:
                    button_name = f"mouse_{button}"
                set_ptt_key(button_name)

        # Hook keyboard (no suppress to avoid blocking keyboard)
        self._ptt_hook = keyboard.on_press(on_key)

        # Try to hook mouse buttons using mouse library
        try:
            import mouse  # type: ignore[import-untyped]

            self._ptt_mouse_hook = mouse.hook(on_mouse_event)
        except ImportError:
            pass  # mouse module not available
        except Exception:
            pass

        # Set a 5 second timeout
        self._ptt_timeout_id = self.root.after(5000, cancel_recording)

    def _clear_ptt_key(self):
        """Clear the PTT key setting."""
        self.ptt_key_var.set("")
        self.ptt_status_label.configure(text="PTT disabled", text_color=COLORS["text_muted"])

        # Update mixer if running
        if self.mixer:
            self.mixer.set_ptt_key(None)

        # Save config
        self._save_config()

    def _get_current_tab(self) -> SoundTab:
        """Get the currently active tab."""
        if not self.tabs:
            # Create default tab if none exist
            self.tabs.append(SoundTab(name="Main", emoji="🎵"))
        return self.tabs[self.current_tab_idx]

    def _show_stop_button(self, slot_idx: int):
        """Show the stop button for a playing slot.

        command= was already set at creation time — just pack/unpack.
        """
        if slot_idx not in self.slot_stop_buttons or slot_idx not in self.slot_progress:
            return
        stop_btn = self.slot_stop_buttons[slot_idx]
        progress = self.slot_progress[slot_idx]
        stop_btn.pack_forget()
        stop_btn.pack(side=tk.LEFT, padx=(0, 4), before=progress)

    def _is_widget_inside(self, widget, parent) -> bool:
        """Check if *widget* is *parent* or a descendant of *parent*."""
        if widget is None or parent is None:
            return False
        current = widget
        while current is not None:
            if current == parent:
                return True
            try:
                current = current.master
            except Exception:
                break
        return False

    # ─────────────────────────────────────────────────────────
    # Slot click / drag state machine
    #
    # Click-to-play: via command= callback (survives CTkButton._draw())
    # Drag: via drag handle at top of slot (hold 2 seconds to enable)
    #
    # The drag handle approach separates click-to-play from drag,
    # making it clear that holding the grip icon enables reordering.
    # ─────────────────────────────────────────────────────────

    _DRAG_THRESHOLD = 5  # pixels before actual drag movement starts

    def _reset_click_state(self):
        """Return to IDLE — cancel any press/drag in progress."""
        self._click_active = False
        self._click_slot = None
        self._click_tab = None
        self._click_start_x = 0
        self._click_start_y = 0
        self._click_dragging = False

    # ---------- Edit Mode (phone-like rearrange with wiggle animation) ----------

    def _toggle_edit_mode(self):
        """Toggle edit/rearrange mode - slots wiggle and can be dragged."""
        if self._edit_mode:
            self._exit_edit_mode()
        else:
            self._enter_edit_mode()

    def _enter_edit_mode(self):
        """Enter edit mode - show edit indicators on slots."""
        self._edit_mode = True
        self._dragging_slot = None
        self._edit_mode_frames: set = set()  # Track which frames have edit indicator

        # Update edit mode button
        if hasattr(self, "edit_mode_btn"):
            self.edit_mode_btn.configure(
                text="✓ Done",
                fg_color=COLORS["green"],
            )

        # Change cursor
        self.root.configure(cursor="fleur")

        # Show edit mode indicator on all filled slots
        tab = self._get_current_tab()
        for slot_idx, frame in self.slot_frames.items():
            if slot_idx in tab.slots:
                frame.configure(
                    fg_color=COLORS["blurple"],
                    border_width=2,
                    border_color=COLORS["text_muted"],
                )
                self._edit_mode_frames.add(slot_idx)

    def _exit_edit_mode(self, skip_refresh: bool = False):
        """Exit edit mode - reset visuals."""
        self._edit_mode = False
        self._dragging_slot = None

        # Update edit mode button
        if hasattr(self, "edit_mode_btn"):
            self.edit_mode_btn.configure(
                text="↔ Move",
                fg_color=COLORS["bg_light"],
            )

        # Reset cursor
        self.root.configure(cursor="")

        # Reset only frames that had edit indicator (optimization)
        frames_to_reset = getattr(self, "_edit_mode_frames", set())
        for slot_idx in frames_to_reset:
            if slot_idx in self.slot_frames:
                self.slot_frames[slot_idx].configure(
                    fg_color=COLORS["bg_medium"],
                    border_width=0,
                )
        self._edit_mode_frames = set()

        # Reset slot appearances (skip if caller will refresh anyway)
        if not skip_refresh:
            self._refresh_current_tab_slots()

    # ---------- Per-tab callback wrappers ----------
    # These are used by per-tab widgets. They delegate to the main methods
    # since the tab must be active (raised) for its widgets to be clickable.

    def _on_slot_command_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab slot command callback."""
        # Tab should already be current since its frame is raised
        self._on_slot_command(slot_idx)

    def _preview_slot_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab preview callback."""
        self._preview_slot(slot_idx)

    def _configure_slot_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab configure callback."""
        self._configure_slot(slot_idx)

    def _show_quick_popup_for_tab(self, event, tab_idx: int, slot_idx: int):
        """Per-tab quick popup callback."""
        self._show_quick_popup(event, slot_idx)

    def _stop_slot_with_flag_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab stop callback."""
        self._stop_slot_with_flag(slot_idx)

    # ---------- command= callback (PRIMARY click mechanism) ----------

    def _on_slot_command(self, slot_idx: int):
        """CTkButton command= callback — fires on press, survives _draw().

        This is the ONLY reliable click callback for CTkButton because
        _draw() re-binds <Button-1> on the internal canvas, wiping any
        raw canvas.bind() handlers.  command= is stored as a property
        and is immune to _draw().
        """
        # In edit mode, clicking a slot selects it for dragging
        if self._edit_mode:
            if self._dragging_slot is None:
                # Select this slot for dragging
                tab = self._get_current_tab()
                if slot_idx in tab.slots:
                    self._dragging_slot = slot_idx
                    self._click_tab = self.current_tab_idx
                    # Highlight the selected slot
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["green"])
            elif self._dragging_slot == slot_idx:
                # Clicking same slot - deselect
                self._dragging_slot = None
                self._refresh_current_tab_slots()
            else:
                # Clicking different slot - swap them
                source_idx = self._dragging_slot
                self._dragging_slot = None
                self._swap_slots(source_idx, slot_idx)
            return

        self._handle_slot_click(slot_idx)

    # ---------- high-level action handlers ----------

    def _handle_slot_click(self, slot_idx: int):
        """A confirmed click on a slot — play or configure."""
        now = time.time()

        if now - self._last_play_time < 0.15:
            return

        if self._just_stopped_slot == slot_idx and now - self._just_stopped_at < 0.2:
            self._just_stopped_slot = None
            return
        self._just_stopped_slot = None

        self._last_play_time = now
        self._play_slot(slot_idx)

    def _handle_slot_drop(self, event, source_idx: int, source_tab: int):
        """A confirmed drag-drop — swap slots or move across tabs."""
        widget = self.root.winfo_containing(event.x_root, event.y_root)

        for idx, tab_btn in enumerate(self.tab_buttons):
            if self._is_widget_inside(widget, tab_btn):
                if idx != source_tab:
                    self._move_slot_to_tab(source_idx, source_tab, idx)
                return

        for slot_idx, btn in self.slot_buttons.items():
            if self._is_widget_inside(widget, btn):
                if slot_idx != source_idx and source_tab == self.current_tab_idx:
                    self._swap_slots(source_idx, slot_idx)
                return

    def _swap_slots(self, idx1: int, idx2: int):
        """Swap two slots within the current tab."""
        tab = self._get_current_tab()
        slot1 = tab.slots.get(idx1)
        slot2 = tab.slots.get(idx2)

        if slot1 is not None and slot2 is not None:
            # Both have content - swap
            tab.slots[idx1] = slot2
            tab.slots[idx2] = slot1
        elif slot1 is not None:
            # Only slot1 has content - move to slot2
            tab.slots[idx2] = slot1
            del tab.slots[idx1]
        # else: slot1 is empty, nothing to move

        self._save_config()
        # Update the two affected slots' appearances (per-tab architecture)
        tab_idx = self.current_tab_idx
        self._update_slot_button_for_tab(tab_idx, idx1)
        self._update_slot_button_for_tab(tab_idx, idx2)

    def _move_slot_to_tab(self, slot_idx: int, from_tab_idx: int, to_tab_idx: int):
        """Move a slot from one tab to another."""
        if from_tab_idx >= len(self.tabs) or to_tab_idx >= len(self.tabs):
            return

        from_tab = self.tabs[from_tab_idx]
        to_tab = self.tabs[to_tab_idx]

        if slot_idx not in from_tab.slots:
            return

        slot = from_tab.slots[slot_idx]

        # Find first empty slot in target tab
        target_idx = 0
        while target_idx in to_tab.slots:
            target_idx += 1

        # Move the slot
        to_tab.slots[target_idx] = slot
        del from_tab.slots[slot_idx]

        self._save_config()

        # Update the source tab's old slot (now empty) - per-tab architecture
        self._ensure_tab_built(from_tab_idx)
        self._update_slot_button_for_tab(from_tab_idx, slot_idx)

        # Update the destination tab's new slot - per-tab architecture
        # Ensure destination tab has enough slots for the new content
        self._ensure_slots_for_tab(to_tab_idx)
        self._update_slot_button_for_tab(to_tab_idx, target_idx)

        self.status_var.set(f"Moved '{slot.name}' to {to_tab.emoji or ''} {to_tab.name}")

    def _play_slot(self, slot_idx: int):
        """Play the sound assigned to a slot, or open config for empty slots."""
        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            # Empty slot - open configuration to add a sound
            self._configure_slot(slot_idx)
            return

        slot = tab.slots[slot_idx]

        if self.mixer and self.mixer.running:
            # Create unique sound_id combining tab and slot
            sound_id = f"{self.current_tab_idx}_{slot_idx}"
            # play_sound returns the duration, avoiding a second cache lookup
            duration = self.mixer.play_sound(
                slot.file_path,
                slot.volume,
                slot.speed,
                slot.preserve_pitch,
                sound_id,
                loop=slot.loop,
                loop_count=slot.loop_count,
                loop_delay=slot.loop_delay,
            )
            loop_text = " (looping)" if slot.loop else ""
            self.status_var.set(f"Playing: {slot.name}{loop_text}")

            # Start progress tracking
            if duration > 0:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": self.current_tab_idx,
                }
                # Change button color to playing state
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["playing"])
                # Set progress bar color for playing state
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].configure(progress_color=COLORS["playing"])
                # Show stop button (on left side, before progress bar)
                self._show_stop_button(slot_idx)
        else:
            self.status_var.set("Start the audio stream first!")

    def _preview_slot(self, slot_idx: int):
        """Preview a sound through default speakers (without streaming to Discord).

        Clicking preview again while already previewing this slot stops the preview.
        """
        # If this slot is already previewing, stop it
        if slot_idx in self.preview_slots:
            self._stop_preview(slot_idx)
            return

        tab = self._get_current_tab()
        if slot_idx not in tab.slots:
            self.status_var.set("No sound in this slot")
            return

        slot = tab.slots[slot_idx]

        # Get cached audio data
        data = self.sound_cache.get_sound_data(slot.file_path)
        if data is None:
            self.status_var.set("Failed to load sound for preview")
            return

        try:
            # Stop any currently playing preview on other slots
            self._stop_all_previews()

            # Calculate duration
            duration = len(data) / self.sound_cache.sample_rate

            # Play through default speakers (not the virtual cable)
            sd.play(data * slot.volume, samplerate=self.sound_cache.sample_rate, device=None)
            self.status_var.set(f"Preview: {slot.name}")

            # Track preview progress
            if duration > 0:
                self.preview_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": self.current_tab_idx,
                }
                # Change button color to preview state (green)
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["preview"])
                # Set progress bar color for preview state
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].configure(progress_color=COLORS["preview"])
                # Show stop button for preview
                self._show_stop_button(slot_idx)
        except Exception as e:
            self.status_var.set(f"Preview error: {e}")

    def _stop_preview(self, slot_idx: int):
        """Stop a specific preview sound and reset its UI state."""
        sd.stop()

        if slot_idx in self.preview_slots:
            tab_idx = self.preview_slots[slot_idx].get("tab_idx", self.current_tab_idx)
            del self.preview_slots[slot_idx]

            # Reset UI for this slot
            if tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].set(0)
                if slot_idx in self.slot_stop_buttons:
                    self.slot_stop_buttons[slot_idx].pack_forget()

        self.status_var.set("Preview stopped")

    def _stop_all_previews(self):
        """Stop all currently playing previews and reset their UI state."""
        sd.stop()

        for slot_idx in list(self.preview_slots.keys()):
            tab_idx = self.preview_slots[slot_idx].get("tab_idx", self.current_tab_idx)
            del self.preview_slots[slot_idx]

            if tab_idx == self.current_tab_idx:
                self._update_slot_button(slot_idx)
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].set(0)
                if slot_idx in self.slot_stop_buttons:
                    self.slot_stop_buttons[slot_idx].pack_forget()

    def _show_quick_popup(self, event, slot_idx: int):
        """Show a quick popup for volume/speed adjustment next to the clicked slot."""
        tab = self._get_current_tab()

        # If slot is empty, open the full configure dialog instead
        if slot_idx not in tab.slots:
            self._configure_slot(slot_idx)
            return

        slot = tab.slots[slot_idx]

        # Create popup window positioned near the click
        popup = ctk.CTkToplevel(self.root)
        popup.title("Quick Edit")
        popup.overrideredirect(True)  # Remove window decorations
        popup.attributes("-topmost", True)

        # Position popup near the click location
        x = event.x_root + 10
        y = event.y_root + 10
        popup.geometry(f"340x260+{x}+{y}")

        # Main frame with rounded corners
        main_frame = ctk.CTkFrame(popup, fg_color=COLORS["bg_medium"], corner_radius=12)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Header with slot name
        header = ctk.CTkLabel(
            main_frame,
            text=slot.name[:20] + "…" if len(slot.name) > 20 else slot.name,
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        header.pack(fill=tk.X, padx=12, pady=(12, 8))

        # Volume control
        vol_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        vol_frame.pack(fill=tk.X, padx=12, pady=4)

        ctk.CTkLabel(
            vol_frame,
            text="🔊 Volume:",
            text_color=COLORS["text_primary"],
            width=80,
            anchor="w",
        ).pack(side=tk.LEFT)

        volume_var = tk.IntVar(value=int(slot.volume * 100))
        volume_slider = ctk.CTkSlider(
            vol_frame,
            from_=0,
            to=150,
            variable=volume_var,
            width=120,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        volume_slider.pack(side=tk.LEFT, padx=5)

        ctk.CTkButton(
            vol_frame,
            text="↺",
            command=lambda: volume_var.set(100),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=28,
            height=28,
        ).pack(side=tk.RIGHT)

        # Speed control
        speed_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        speed_frame.pack(fill=tk.X, padx=12, pady=4)

        ctk.CTkLabel(
            speed_frame,
            text="⚡ Speed:",
            text_color=COLORS["text_primary"],
            width=80,
            anchor="w",
        ).pack(side=tk.LEFT)

        speed_var = tk.IntVar(value=int(slot.speed * 100))
        speed_slider = ctk.CTkSlider(
            speed_frame,
            from_=50,
            to=200,
            variable=speed_var,
            width=120,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        speed_slider.pack(side=tk.LEFT, padx=5)

        ctk.CTkButton(
            speed_frame,
            text="↺",
            command=lambda: speed_var.set(100),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=28,
            height=28,
        ).pack(side=tk.RIGHT)

        # Preserve pitch checkbox
        pitch_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        pitch_frame.pack(fill=tk.X, padx=12, pady=4)

        preserve_pitch_var = tk.BooleanVar(value=slot.preserve_pitch)
        pitch_check = ctk.CTkCheckBox(
            pitch_frame,
            text="🎵 Preserve pitch",
            variable=preserve_pitch_var,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            text_color=COLORS["text_primary"],
        )
        pitch_check.pack(side=tk.LEFT)

        # Loop checkbox (DJ-style quick toggle)
        loop_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        loop_frame.pack(fill=tk.X, padx=12, pady=4)

        loop_var = tk.BooleanVar(value=slot.loop)
        loop_check = ctk.CTkCheckBox(
            loop_frame,
            text="🔁 Loop",
            variable=loop_var,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            text_color=COLORS["text_primary"],
        )
        loop_check.pack(side=tk.LEFT)

        # Button frame
        btn_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_frame.pack(fill=tk.X, padx=12, pady=(8, 12))

        def apply_changes():
            """Apply the volume/speed/pitch/loop changes."""
            slot.volume = volume_var.get() / 100.0
            slot.speed = speed_var.get() / 100.0
            slot.preserve_pitch = preserve_pitch_var.get()
            slot.loop = loop_var.get()
            self._save_config()
            self._update_slot_button_for_tab(self.current_tab_idx, slot_idx)
            popup.destroy()

        def open_full_edit():
            """Open the full configure dialog."""
            popup.destroy()
            self._configure_slot(slot_idx)

        def clone_for_retrim():
            """Clone slot to next empty slot, opening editor for a new cut."""
            popup.destroy()
            self._clone_slot_for_retrim(slot_idx)

        def delete_sound():
            """Delete the sound with confirmation."""
            popup.destroy()
            if messagebox.askyesno(
                "Delete Sound",
                f"Are you sure you want to delete '{slot.name}'?",
                icon="warning",
            ):
                self._delete_slot(slot_idx)

        ctk.CTkButton(
            btn_frame,
            text="Apply",
            command=apply_changes,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="More...",
            command=open_full_edit,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="📋 Clone",
            command=clone_for_retrim,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="🗑️",
            command=delete_sound,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=32,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            btn_frame,
            text="✕",
            command=popup.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=32,
        ).pack(side=tk.RIGHT, padx=2)

        # Close popup when clicking outside
        def on_focus_out(event):
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except tk.TclError:
                pass

        popup.bind("<FocusOut>", on_focus_out)
        popup.focus_set()

    def _clone_slot_for_retrim(self, slot_idx: int):
        """Clone an existing slot into a new slot with a different cut.

        Opens the sound editor on the original (un-trimmed) source if one was
        tracked, otherwise on the slot's current file. Saves the new cut as
        an independent file, then creates a new slot that copies the original
        slot's settings (color, emoji, image, volume, speed, loop, groups).
        Hotkey is intentionally NOT copied to avoid duplicate bindings.
        """
        tab = self._get_current_tab()
        original = tab.slots.get(slot_idx)
        if not original:
            return

        # Pick the best source to re-trim from. Prefer the tracked original
        # source so the user can pick any range from the full file. Fall back
        # to the current (already-trimmed) file if no source was tracked.
        candidate_paths = []
        if original.source_file_path:
            candidate_paths.append(original.source_file_path)
        candidate_paths.append(original.file_path)

        source_for_editor: Optional[str] = None
        for p in candidate_paths:
            try:
                if p and os.path.isfile(p):
                    source_for_editor = p
                    break
            except Exception:
                continue

        if not source_for_editor:
            messagebox.showwarning(
                "Clone",
                "Cannot find the original audio file for this sound.",
            )
            return

        # Open the editor and let the user pick a new cut.
        try:
            editor = SoundEditor(self.root, source_for_editor, output_device=None)
            result = editor.show()
        except Exception as e:
            messagebox.showerror("Editor Error", f"Failed to open sound editor:\n{e}")
            return

        if result is None:
            return  # User cancelled

        audio_data, sample_rate = result

        # Save the new cut as an independent file in the local sounds folder.
        try:
            new_file_path = self.sound_cache.add_sound_data(
                audio_data,
                sample_rate,
                Path(source_for_editor).name,
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save cloned sound:\n{e}")
            return

        # Find the next empty slot in the current tab.
        new_slot_idx = 0
        while new_slot_idx in tab.slots:
            new_slot_idx += 1

        # Build a unique-ish name: "<original> (copy)", "(copy 2)", ...
        existing_names = {s.name for s in tab.slots.values()}
        base_name = original.name or Path(source_for_editor).stem
        candidate = f"{base_name} (copy)"
        n = 2
        while candidate in existing_names:
            candidate = f"{base_name} (copy {n})"
            n += 1

        tab.slots[new_slot_idx] = SoundSlot(
            name=candidate,
            file_path=new_file_path,
            hotkey=None,  # don't copy hotkey to avoid duplicate bindings
            volume=original.volume,
            emoji=original.emoji,
            image_path=original.image_path,
            color=original.color,
            speed=original.speed,
            preserve_pitch=original.preserve_pitch,
            loop=original.loop,
            loop_count=original.loop_count,
            loop_delay=original.loop_delay,
            groups=list(original.groups),
            source_file_path=source_for_editor,
        )

        self._ensure_slots_for_tab(self.current_tab_idx)
        self._update_slot_button_for_tab(self.current_tab_idx, new_slot_idx)
        self._save_config()

    def _configure_slot(self, slot_idx: int):
        """Open configuration dialog for a slot."""
        tab = self._get_current_tab()

        dialog = ctk.CTkToplevel(self.root)
        dialog.title(f"Configure Slot {slot_idx + 1}")
        dialog.geometry("550x780")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        existing = tab.slots.get(slot_idx)

        # State for edited audio
        edited_audio_data = {"data": None, "sample_rate": None, "original_name": None}

        # Name field
        ctk.CTkLabel(frame, text="Name:", text_color=COLORS["text_primary"]).grid(
            row=0, column=0, sticky="w", pady=8
        )
        name_var = tk.StringVar(value=existing.name if existing else "")
        name_entry = ctk.CTkEntry(
            frame,
            textvariable=name_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        )
        name_entry.grid(row=0, column=1, pady=8)
        _bind_rtl_entry(name_entry, name_var)

        # File path field
        ctk.CTkLabel(frame, text="Sound File:", text_color=COLORS["text_primary"]).grid(
            row=1, column=0, sticky="w", pady=8
        )
        path_var = tk.StringVar(value=existing.file_path if existing else "")
        path_entry = ctk.CTkEntry(
            frame,
            textvariable=path_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        )
        path_entry.grid(row=1, column=1, pady=8)
        _bind_rtl_entry(path_entry, path_var)

        # Edit status label
        edit_status_var = tk.StringVar(value="")
        edit_status_label = ctk.CTkLabel(
            frame,
            textvariable=edit_status_var,
            text_color=COLORS["green"],
            font=ctk.CTkFont(size=11),
        )
        edit_status_label.grid(row=2, column=1, sticky="w")

        def browse():
            filetypes = [("Audio", " ".join(SUPPORTED_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                path_var.set(fp)
                if not name_var.get():
                    name_var.set(Path(fp).stem)
                # Open the sound editor
                self._open_sound_editor(fp, edited_audio_data, edit_status_var, dialog)

        def edit_current():
            """Edit the currently selected sound file."""
            current_path = path_var.get()
            if current_path and os.path.exists(current_path):
                self._open_sound_editor(current_path, edited_audio_data, edit_status_var, dialog)
            else:
                messagebox.showwarning("No File", "Please select a sound file first.")

        btn_frame_browse = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame_browse.grid(row=1, column=2, padx=10)

        ctk.CTkButton(
            btn_frame_browse,
            text="Browse",
            command=browse,
            width=70,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
        ).pack(pady=2)
        ctk.CTkButton(
            btn_frame_browse,
            text="Edit",
            command=edit_current,
            width=70,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
        ).pack(pady=2)

        # Emoji field
        ctk.CTkLabel(frame, text="Emoji:", text_color=COLORS["text_primary"]).grid(
            row=3, column=0, sticky="w", pady=8
        )
        emoji_var = tk.StringVar(value=existing.emoji if existing and existing.emoji else "")
        ctk.CTkEntry(
            frame,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=3, column=1, sticky="w", pady=8)

        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            frame,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=100,
        ).grid(row=3, column=2, padx=10, sticky="w")

        # Image field
        ctk.CTkLabel(frame, text="Image:", text_color=COLORS["text_primary"]).grid(
            row=4, column=0, sticky="w", pady=8
        )
        image_var = tk.StringVar(
            value=existing.image_path if existing and existing.image_path else ""
        )
        ctk.CTkEntry(
            frame,
            textvariable=image_var,
            width=250,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=4, column=1, pady=8)

        def browse_image():
            filetypes = [("Images", " ".join(SUPPORTED_IMAGE_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                # Copy image to local storage
                local_path = self._copy_image_to_storage(fp)
                image_var.set(local_path)

        ctk.CTkButton(
            frame,
            text="Browse",
            command=browse_image,
            width=70,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
        ).grid(row=4, column=2, padx=10)

        # Volume slider
        ctk.CTkLabel(frame, text="Volume:", text_color=COLORS["text_primary"]).grid(
            row=5, column=0, sticky="w", pady=8
        )
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        ctk.CTkSlider(
            frame,
            from_=0,
            to=150,
            variable=volume_var,
            width=200,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).grid(row=5, column=1, sticky="w")

        # Hotkey field
        ctk.CTkLabel(frame, text="Hotkey:", text_color=COLORS["text_primary"]).grid(
            row=6, column=0, sticky="w", pady=8
        )
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ctk.CTkEntry(
            frame,
            textvariable=hotkey_var,
            width=150,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        ).grid(row=6, column=1, sticky="w")

        # Speed slider
        ctk.CTkLabel(frame, text="Speed:", text_color=COLORS["text_primary"]).grid(
            row=7, column=0, sticky="w", pady=8
        )
        speed_var = tk.DoubleVar(value=(existing.speed * 100) if existing else 100)
        speed_frame = ctk.CTkFrame(frame, fg_color="transparent")
        speed_frame.grid(row=7, column=1, sticky="w")
        ctk.CTkSlider(
            speed_frame,
            from_=50,
            to=200,
            variable=speed_var,
            width=150,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).pack(side=tk.LEFT)
        speed_label = ctk.CTkLabel(
            speed_frame, text="100%", width=50, text_color=COLORS["text_primary"]
        )
        speed_label.pack(side=tk.LEFT, padx=5)

        def update_speed_label(*args):
            speed_label.configure(text=f"{int(speed_var.get())}%")

        speed_var.trace("w", update_speed_label)
        update_speed_label()

        # Color dropdown
        ctk.CTkLabel(frame, text="Color:", text_color=COLORS["text_primary"]).grid(
            row=8, column=0, sticky="w", pady=8
        )
        color_names = list(ALL_SLOT_COLORS.keys())

        # Find existing color name
        existing_color_name = "Default"
        if existing and existing.color:
            for name, hex_val in ALL_SLOT_COLORS.items():
                if hex_val.lower() == existing.color.lower():
                    existing_color_name = name
                    break

        color_var = tk.StringVar(value=existing_color_name)
        color_dropdown = ctk.CTkComboBox(
            frame,
            variable=color_var,
            values=color_names,
            width=150,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            dropdown_hover_color=COLORS["bg_light"],
            state="readonly",
        )
        color_dropdown.grid(row=8, column=1, sticky="w")

        # Color preview (using a small CTkFrame as color swatch)
        color_preview = ctk.CTkFrame(
            frame,
            width=30,
            height=20,
            fg_color=ALL_SLOT_COLORS[existing_color_name],
            corner_radius=4,
        )
        color_preview.grid(row=8, column=2, padx=10, sticky="w")

        def update_color_preview(*args):
            selected = color_var.get()
            if selected in ALL_SLOT_COLORS:
                color_preview.configure(fg_color=ALL_SLOT_COLORS[selected])

        color_var.trace("w", update_color_preview)

        # Groups / Types selector (multi-select with checkboxes)
        ctk.CTkLabel(frame, text="Groups:", text_color=COLORS["text_primary"]).grid(
            row=9, column=0, sticky="nw", pady=8
        )
        groups_outer = ctk.CTkFrame(frame, fg_color="transparent")
        groups_outer.grid(row=9, column=1, columnspan=2, sticky="w", pady=8)

        existing_groups = existing.groups if existing else []
        group_check_vars: Dict[str, tk.BooleanVar] = {}
        group_checks_frame = ctk.CTkFrame(groups_outer, fg_color="transparent")
        group_checks_frame.pack(fill=tk.X)

        all_groups = self._get_all_groups()

        def _rebuild_group_checks():
            """Rebuild the group checkboxes from current all_groups list."""
            for w in group_checks_frame.winfo_children():
                w.destroy()
            group_check_vars.clear()

            current_all = self._get_all_groups()
            col = 0
            row_g = 0
            for g in current_all:
                var = tk.BooleanVar(value=g in existing_groups)
                group_check_vars[g] = var
                cb = ctk.CTkCheckBox(
                    group_checks_frame,
                    text=g,
                    variable=var,
                    fg_color=COLORS["blurple"],
                    hover_color=COLORS["blurple_hover"],
                    font=self._font_xs,
                    height=22,
                    checkbox_width=16,
                    checkbox_height=16,
                )
                cb.grid(row=row_g, column=col, sticky="w", padx=(0, 10), pady=1)
                col += 1
                if col >= 3:
                    col = 0
                    row_g += 1

        _rebuild_group_checks()

        # Manage groups button (opens dialog to add/remove groups)
        manage_btn_frame = ctk.CTkFrame(groups_outer, fg_color="transparent")
        manage_btn_frame.pack(fill=tk.X, pady=(4, 0))

        def _open_manage_groups():
            # Remember currently checked groups before opening dialog
            nonlocal existing_groups
            existing_groups = [g for g, v in group_check_vars.items() if v.get()]
            self._show_manage_groups_dialog(rebuild_callback=_rebuild_group_checks)
            # After dialog closes, re-check the groups that were selected
            for g in existing_groups:
                if g in group_check_vars:
                    group_check_vars[g].set(True)

        ctk.CTkButton(
            manage_btn_frame,
            text="⚙ Manage Groups",
            command=_open_manage_groups,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=4,
            width=120,
            height=26,
        ).pack(side=tk.LEFT)

        # Loop checkbox
        ctk.CTkLabel(frame, text="Loop:", text_color=COLORS["text_primary"]).grid(
            row=10, column=0, sticky="w", pady=8
        )
        loop_frame = ctk.CTkFrame(frame, fg_color="transparent")
        loop_frame.grid(row=10, column=1, columnspan=2, sticky="w")

        loop_var = tk.BooleanVar(value=existing.loop if existing else False)
        loop_checkbox = ctk.CTkCheckBox(
            loop_frame,
            text="Enable looping",
            variable=loop_var,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
        )
        loop_checkbox.pack(side=tk.LEFT)

        # Loop count (0 = infinite)
        ctk.CTkLabel(loop_frame, text="  Count:", text_color=COLORS["text_secondary"]).pack(
            side=tk.LEFT, padx=(20, 5)
        )
        loop_count_var = tk.IntVar(value=existing.loop_count if existing else 0)
        loop_count_entry = ctk.CTkEntry(
            loop_frame,
            textvariable=loop_count_var,
            width=60,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
        )
        loop_count_entry.pack(side=tk.LEFT)
        ctk.CTkLabel(
            loop_frame,
            text="(0=∞)",
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(size=10),
        ).pack(side=tk.LEFT, padx=5)

        # Loop delay
        ctk.CTkLabel(frame, text="Loop Delay:", text_color=COLORS["text_primary"]).grid(
            row=11, column=0, sticky="w", pady=8
        )
        delay_frame = ctk.CTkFrame(frame, fg_color="transparent")
        delay_frame.grid(row=11, column=1, sticky="w")

        loop_delay_var = tk.DoubleVar(value=existing.loop_delay if existing else 0.0)
        loop_delay_slider = ctk.CTkSlider(
            delay_frame,
            from_=0,
            to=5,
            variable=loop_delay_var,
            width=150,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        )
        loop_delay_slider.pack(side=tk.LEFT)
        delay_label = ctk.CTkLabel(
            delay_frame, text="0.0s", width=50, text_color=COLORS["text_primary"]
        )
        delay_label.pack(side=tk.LEFT, padx=5)

        def update_delay_label(*args):
            delay_label.configure(text=f"{loop_delay_var.get():.1f}s")

        loop_delay_var.trace("w", update_delay_label)
        update_delay_label()

        def save():
            if not path_var.get() and edited_audio_data["data"] is None:
                dialog.destroy()
                return

            source_path = path_var.get()
            local_path = source_path

            try:
                # If we have edited audio data, save it as a new file
                if (
                    edited_audio_data["data"] is not None
                    and edited_audio_data["sample_rate"] is not None
                ):
                    local_path = self.sound_cache.add_sound_data(
                        edited_audio_data["data"],
                        edited_audio_data["sample_rate"],
                        edited_audio_data["original_name"] or Path(source_path).name,
                    )
                # Otherwise copy original sound to local storage if not already there
                else:
                    # Check if already in sounds folder (handle both relative and absolute paths)
                    source_abs = str(Path(source_path).absolute())
                    sounds_abs = str(Path(SOUNDS_DIR).absolute())
                    is_already_local = (
                        source_path.startswith(SOUNDS_DIR + "/")
                        or source_path.startswith(SOUNDS_DIR + "\\")
                        or source_abs.startswith(sounds_abs)
                    )
                    if not is_already_local:
                        local_path = self.sound_cache.add_sound(source_path)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to add sound:\n{e}")
                return

            # Determine source_file_path: track the original un-trimmed file
            # so that "Clone (re-trim)" can re-cut from the original.
            #  - If we just trimmed via the editor, persist the original full
            #    file in our local sounds/ folder so it can never be lost
            #    (user might delete the file from Downloads, etc).
            #  - Prefer an already-tracked source from the existing slot.
            new_source_file_path: Optional[str] = None
            if (
                edited_audio_data["data"] is not None
                and edited_audio_data["sample_rate"] is not None
            ):
                if existing and existing.source_file_path \
                        and os.path.isfile(existing.source_file_path):
                    # Existing slot already has a tracked original — keep it.
                    new_source_file_path = existing.source_file_path
                elif source_path:
                    # Whatever path the user picked / the slot was pointing
                    # at BEFORE this save IS the original we want to keep.
                    # (If they re-trimmed an existing slot's file, source_path
                    # equals existing.file_path which is the original full
                    # file we previously copied to sounds/.)
                    try:
                        src_abs = str(Path(source_path).absolute())
                        sounds_abs = str(Path(SOUNDS_DIR).absolute())
                        already_local = (
                            source_path.startswith(SOUNDS_DIR + "/")
                            or source_path.startswith(SOUNDS_DIR + "\\")
                            or src_abs.startswith(sounds_abs)
                        )
                        if already_local:
                            # Already in sounds/, just reference it.
                            new_source_file_path = source_path
                        else:
                            # Copy the original full-length source into local
                            # storage so it survives even if the user deletes
                            # the original from Downloads/etc.
                            new_source_file_path = self.sound_cache.add_sound(source_path)
                    except Exception:
                        # If we can't persist the source, fall back to the
                        # raw path so Clone may still work while the file
                        # remains where the user picked it.
                        new_source_file_path = source_path

            tab.slots[slot_idx] = SoundSlot(
                name=name_var.get() or Path(source_path).stem,
                file_path=local_path,
                hotkey=hotkey_var.get() or None,
                volume=volume_var.get() / 100.0,
                emoji=emoji_var.get() or None,
                image_path=image_var.get() or None,
                color=ALL_SLOT_COLORS.get(color_var.get()),
                speed=speed_var.get() / 100.0,
                preserve_pitch=existing.preserve_pitch if existing else True,
                loop=loop_var.get(),
                loop_count=loop_count_var.get(),
                loop_delay=loop_delay_var.get(),
                groups=[g for g, v in group_check_vars.items() if v.get()],
                source_file_path=new_source_file_path,
            )
            self._update_slot_button_for_tab(
                self.current_tab_idx, slot_idx
            )  # Update the slot appearance
            # Ensure we have enough empty slots after adding this one
            self._ensure_slots_for_tab(self.current_tab_idx)
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        def clear():
            """Delete the sound with confirmation."""
            slot_to_delete = tab.slots.get(slot_idx)
            if not slot_to_delete:
                dialog.destroy()
                return

            if not messagebox.askyesno(
                "Delete Sound",
                f"Are you sure you want to delete '{slot_to_delete.name}'?",
                icon="warning",
            ):
                return

            # Check if any other slot uses this sound before removing from cache
            other_uses = any(
                s.file_path == slot_to_delete.file_path
                for t in self.tabs
                for idx, s in t.slots.items()
                if not (t == tab and idx == slot_idx)
            )
            if not other_uses:
                self.sound_cache.remove_sound(slot_to_delete.file_path, delete_file=True)
            del tab.slots[slot_idx]
            self._update_slot_button_for_tab(
                self.current_tab_idx, slot_idx
            )  # Update the slot appearance
            self._register_hotkeys()
            self._save_config()
            dialog.destroy()

        # Button row
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=12, column=0, columnspan=3, pady=25)

        ctk.CTkButton(
            btn_frame,
            text="Save",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Clear",
            command=clear,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_medium"],
            hover_color=COLORS["bg_light"],
            width=100,
        ).pack(side=tk.LEFT, padx=5)

    def _on_files_dropped(self, files):
        """Handle files dropped from file explorer onto the main window.

        Determines which slot is under the cursor and applies the image to it.
        Accepts image files dropped onto filled sound slots.
        """
        # Determine cursor position
        try:
            cursor_x = self.root.winfo_pointerx()
            cursor_y = self.root.winfo_pointery()
        except Exception:
            return

        # Decode file paths (windnd passes bytes on some versions)
        file_paths = []
        for f in files:
            if isinstance(f, bytes):
                try:
                    file_paths.append(f.decode("utf-8"))
                except UnicodeDecodeError:
                    try:
                        file_paths.append(f.decode("gbk"))
                    except UnicodeDecodeError:
                        continue
            else:
                file_paths.append(str(f))

        if not file_paths:
            return

        # Find image files among the dropped files
        image_exts = {".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".ico"}
        image_files = [
            f for f in file_paths if Path(f).suffix.lower() in image_exts and os.path.isfile(f)
        ]

        if not image_files:
            self.status_var.set("Drop an image file onto a sound slot")
            return

        image_path = image_files[0]  # Use first image

        # Find which slot is under the cursor
        target_slot = self._find_slot_at_position(cursor_x, cursor_y)
        if target_slot is None:
            self.status_var.set("Drop the image onto a sound slot")
            return

        tab_idx, slot_idx = target_slot
        tab = self.tabs[tab_idx]
        slot = tab.slots.get(slot_idx)

        if not slot:
            self.status_var.set("Drop the image onto a filled sound slot")
            return

        # Copy image to local storage and assign to slot
        local_path = self._copy_image_to_storage(image_path)
        slot.image_path = local_path

        # Update the slot appearance
        self._update_slot_button_for_tab(tab_idx, slot_idx)
        if tab_idx == self.current_tab_idx:
            self._update_slot_button(slot_idx)

        self._save_config()
        self.status_var.set(f"Image set for: {slot.name}")

    def _find_slot_at_position(self, screen_x: int, screen_y: int):
        """Find slot under screen coordinates. Returns (tab_idx, slot_idx) or None."""
        tab_idx = self.current_tab_idx
        if tab_idx not in self.tab_slot_frames:
            return None

        for slot_idx, frame in self.tab_slot_frames[tab_idx].items():
            try:
                fx = frame.winfo_rootx()
                fy = frame.winfo_rooty()
                fw = frame.winfo_width()
                fh = frame.winfo_height()
                if fx <= screen_x <= fx + fw and fy <= screen_y <= fy + fh:
                    return (tab_idx, slot_idx)
            except Exception:
                continue

        return None

    def _copy_image_to_storage(self, source_path: str) -> str:
        """Copy an image to local storage and return the local path."""
        Path(IMAGES_DIR).mkdir(exist_ok=True)

        # Generate unique filename using hash
        with open(source_path, "rb") as f:
            file_hash = hashlib.md5(f.read(4096)).hexdigest()[:8]

        original_name = Path(source_path).stem
        extension = Path(source_path).suffix
        new_filename = f"{original_name}_{file_hash}{extension}"
        local_path = str(Path(IMAGES_DIR) / new_filename)

        if not os.path.exists(local_path):
            shutil.copy2(source_path, local_path)

        return local_path

    def _open_sound_editor(
        self,
        file_path: str,
        edited_audio_data: dict,
        status_var: tk.StringVar,
        parent_dialog: tk.Toplevel,
    ):
        """Open the sound editor dialog for a file."""
        try:
            # Use default system output device for preview (speakers/headphones)
            # NOT the virtual cable which routes to Discord
            output_device = None  # None = default system output

            # Create and show editor
            editor = SoundEditor(
                self.root,
                file_path,
                output_device=output_device,
            )
            result = editor.show()

            if result is not None:
                audio_data, sample_rate = result
                edited_audio_data["data"] = audio_data
                edited_audio_data["sample_rate"] = sample_rate
                edited_audio_data["original_name"] = Path(file_path).name

                # Calculate duration
                duration = len(audio_data) / sample_rate
                status_var.set(f"✓ Edited ({duration:.2f}s)")
            else:
                # User cancelled - clear edited data if any
                status_var.set("")

        except Exception as e:
            messagebox.showerror("Editor Error", f"Failed to open sound editor:\n{e}")

    def _load_slot_image(self, image_path: str, size: tuple = (70, 55)) -> Optional[ctk.CTkImage]:
        """Load and resize an image for a slot button using CTkImage."""
        if not PIL_AVAILABLE:
            return None

        try:
            img = Image.open(image_path)
            img.thumbnail(size, Image.Resampling.LANCZOS)
            # Use CTkImage for proper scaling on HighDPI displays
            return ctk.CTkImage(light_image=img, dark_image=img, size=size)
        except Exception:
            return None

    def _update_slot_button(self, slot_idx: int):
        """Update the appearance of a slot button.

        Optimization: Preview/edit buttons only change appearance based on whether
        the slot is filled or empty. We cache this state and skip their configure()
        calls when the filled state hasn't changed, reducing redundant redraws.
        """
        btn = self.slot_buttons[slot_idx]
        slot_frame = self.slot_frames[slot_idx]
        tab = self._get_current_tab()

        # Determine background color (playing/preview state takes precedence, but only for current tab)
        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == self.current_tab_idx
        )

        # Get custom slot color or default
        slot = tab.slots.get(slot_idx)
        default_color = slot.color if slot and slot.color else COLORS["blurple"]

        if is_playing:
            bg_color = COLORS["playing"]
            frame_color = COLORS["bg_light"]
        elif is_previewing:
            bg_color = COLORS["preview"]
            frame_color = COLORS["bg_light"]
        else:
            bg_color = default_color if slot_idx in tab.slots else "transparent"
            frame_color = COLORS["bg_medium"]

        # Update frame color
        slot_frame.configure(fg_color=frame_color)

        # Check if filled state changed (for preview/edit button optimization)
        is_filled = slot_idx in tab.slots
        was_filled = self._slot_filled_cache.get(slot_idx)
        filled_state_changed = was_filled != is_filled
        self._slot_filled_cache[slot_idx] = is_filled

        if is_filled:
            slot = tab.slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""

            # Truncate name if too long (max ~18 chars per line, 2 lines)
            max_name_len = 32
            display_name = (
                slot.name[:max_name_len] + "…" if len(slot.name) > max_name_len else slot.name
            )

            # Build display text (no emoji - it's shown separately) and fix RTL text (Hebrew, Arabic)
            display_text = _fix_rtl_text(f"{display_name}{hk}")

            # Update emoji label (separate from button text for proper rendering)
            if slot_idx in self.slot_emoji_labels:
                emoji_label = self.slot_emoji_labels[slot_idx]
                if slot.emoji:
                    emoji_label.configure(text=slot.emoji)
                    emoji_label.lift()  # Bring to front when there's an emoji
                else:
                    emoji_label.configure(text="")
                    emoji_label.lower()  # Hide when no emoji

            # Use cached image if available and path hasn't changed
            photo = None
            image_path = (
                slot.image_path if slot.image_path and os.path.exists(slot.image_path) else None
            )
            if image_path:
                # Check if we already have this image cached for this slot
                if (
                    slot_idx in self.slot_images
                    and self.slot_image_paths.get(slot_idx) == image_path
                ):
                    photo = self.slot_images[slot_idx]
                else:
                    # Load and cache the image
                    photo = self._load_slot_image(image_path)
                    if photo:
                        self.slot_images[slot_idx] = photo
                        self.slot_image_paths[slot_idx] = image_path

            hover_color = COLORS["bg_lighter"] if not is_playing and not is_previewing else bg_color

            btn.configure(
                text=display_text,
                image=photo,
                fg_color=bg_color,
                hover_color=hover_color,
                text_color=COLORS["text_primary"],
                font=self._font_sm,
                anchor="center",
            )

            # Only update preview/edit buttons if filled state changed (optimization)
            if filled_state_changed:
                if slot_idx in self.slot_preview_buttons:
                    self.slot_preview_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
                if slot_idx in self.slot_edit_buttons:
                    self.slot_edit_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
        else:
            # Clear image reference if exists
            if slot_idx in self.slot_images:
                del self.slot_images[slot_idx]
            if slot_idx in self.slot_image_paths:
                del self.slot_image_paths[slot_idx]

            # Clear emoji label for empty slots
            if slot_idx in self.slot_emoji_labels:
                self.slot_emoji_labels[slot_idx].configure(text="")
                self.slot_emoji_labels[slot_idx].lower()

            btn.configure(
                text="+",
                image=None,
                fg_color="transparent",
                hover_color=COLORS["bg_light"],
                text_color=COLORS["text_muted"],
                font=self._font_xl_bold,
            )

            # Only update preview/edit buttons if filled state changed (optimization)
            if filled_state_changed:
                if slot_idx in self.slot_preview_buttons:
                    self.slot_preview_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
                    )
                if slot_idx in self.slot_edit_buttons:
                    self.slot_edit_buttons[slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
                    )

    def _update_slot_button_for_tab(self, tab_idx: int, slot_idx: int):
        """Update slot appearance for a specific tab (used during tab building)."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        if tab_idx not in self.tab_slot_buttons:
            return
        if slot_idx not in self.tab_slot_buttons[tab_idx]:
            return

        tab = self.tabs[tab_idx]
        btn = self.tab_slot_buttons[tab_idx][slot_idx]
        slot_frame = self.tab_slot_frames[tab_idx][slot_idx]

        # For initial build, no playing/preview state
        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == tab_idx
        )
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == tab_idx
        )

        slot = tab.slots.get(slot_idx)
        default_color = slot.color if slot and slot.color else COLORS["blurple"]

        if is_playing:
            bg_color = COLORS["playing"]
            frame_color = COLORS["bg_light"]
        elif is_previewing:
            bg_color = COLORS["preview"]
            frame_color = COLORS["bg_light"]
        else:
            bg_color = default_color if slot_idx in tab.slots else "transparent"
            frame_color = COLORS["bg_medium"]

        slot_frame.configure(fg_color=frame_color)

        # Track filled state
        is_filled = slot_idx in tab.slots
        if tab_idx not in self._tab_slot_filled_cache:
            self._tab_slot_filled_cache[tab_idx] = {}
        was_filled = self._tab_slot_filled_cache[tab_idx].get(slot_idx)
        filled_state_changed = was_filled != is_filled
        self._tab_slot_filled_cache[tab_idx][slot_idx] = is_filled

        if is_filled:
            slot = tab.slots[slot_idx]
            hk = f"\n[{slot.hotkey}]" if slot.hotkey else ""
            loop_indicator = " 🔁" if slot.loop else ""
            max_name_len = 28 if slot.loop else 32
            display_name = (
                slot.name[:max_name_len] + "…" if len(slot.name) > max_name_len else slot.name
            )
            display_text = _fix_rtl_text(f"{display_name}{loop_indicator}{hk}")

            # Update emoji label
            if slot_idx in self.tab_slot_emoji_labels.get(tab_idx, {}):
                emoji_label = self.tab_slot_emoji_labels[tab_idx][slot_idx]
                if slot.emoji:
                    emoji_label.configure(text=slot.emoji)
                    emoji_label.lift()
                else:
                    emoji_label.configure(text="")
                    emoji_label.lower()

            # Load image
            photo = None
            image_path = (
                slot.image_path if slot.image_path and os.path.exists(slot.image_path) else None
            )
            if image_path:
                if tab_idx not in self.tab_slot_images:
                    self.tab_slot_images[tab_idx] = {}
                if tab_idx not in self.tab_slot_image_paths:
                    self.tab_slot_image_paths[tab_idx] = {}

                if (
                    slot_idx in self.tab_slot_images[tab_idx]
                    and self.tab_slot_image_paths[tab_idx].get(slot_idx) == image_path
                ):
                    photo = self.tab_slot_images[tab_idx][slot_idx]
                else:
                    photo = self._load_slot_image(image_path)
                    if photo:
                        self.tab_slot_images[tab_idx][slot_idx] = photo
                        self.tab_slot_image_paths[tab_idx][slot_idx] = image_path

            hover_color = COLORS["bg_lighter"] if not is_playing and not is_previewing else bg_color

            btn.configure(
                text=display_text,
                image=photo,
                fg_color=bg_color,
                hover_color=hover_color,
                text_color=COLORS["text_primary"],
                font=self._font_sm,
                anchor="center",
            )

            if filled_state_changed:
                if slot_idx in self.tab_slot_preview_buttons.get(tab_idx, {}):
                    self.tab_slot_preview_buttons[tab_idx][slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
                if slot_idx in self.tab_slot_edit_buttons.get(tab_idx, {}):
                    self.tab_slot_edit_buttons[tab_idx][slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_primary"],
                    )
        else:
            # Clear image reference
            if tab_idx in self.tab_slot_images and slot_idx in self.tab_slot_images[tab_idx]:
                del self.tab_slot_images[tab_idx][slot_idx]
            if (
                tab_idx in self.tab_slot_image_paths
                and slot_idx in self.tab_slot_image_paths[tab_idx]
            ):
                del self.tab_slot_image_paths[tab_idx][slot_idx]

            # Clear emoji
            if slot_idx in self.tab_slot_emoji_labels.get(tab_idx, {}):
                self.tab_slot_emoji_labels[tab_idx][slot_idx].configure(text="")
                self.tab_slot_emoji_labels[tab_idx][slot_idx].lower()

            btn.configure(
                text="+",
                image=None,
                fg_color="transparent",
                hover_color=COLORS["bg_light"],
                text_color=COLORS["text_muted"],
                font=self._font_xl_bold,
            )

            if filled_state_changed:
                if slot_idx in self.tab_slot_preview_buttons.get(tab_idx, {}):
                    self.tab_slot_preview_buttons[tab_idx][slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
                    )
                if slot_idx in self.tab_slot_edit_buttons.get(tab_idx, {}):
                    self.tab_slot_edit_buttons[tab_idx][slot_idx].configure(
                        fg_color=COLORS["bg_light"],
                        text_color=COLORS["text_muted"],
                    )

    def _register_hotkeys(self):
        """Register global hotkeys for all slots across all tabs.

        Diff-based: only unregister hotkeys that went away and only register
        hotkeys that are new. Called frequently (on every save), so avoiding
        unnecessary keyboard.add_hotkey / remove_hotkey system calls is a
        meaningful perf win.
        """
        if not HOTKEYS_AVAILABLE:
            return

        # Build desired hotkey -> (tab_idx, slot_idx) map from current config
        desired: Dict[str, tuple] = {}
        for tab_idx, tab in enumerate(self.tabs):
            for slot_idx, slot in tab.slots.items():
                if slot.hotkey:
                    # Last-wins if a hotkey is duplicated across slots.
                    desired[slot.hotkey] = (tab_idx, slot_idx)

        # Normalise existing registrations into a dict for easy comparison.
        # Older code stored a flat list of hotkey strings; preserve that shape.
        current: Dict[str, tuple] = getattr(self, "_hotkey_map", {})

        # Unregister hotkeys that are gone or whose target changed
        for hk, target in list(current.items()):
            if desired.get(hk) != target:
                try:
                    keyboard.remove_hotkey(hk)  # type: ignore
                except Exception:
                    pass
                current.pop(hk, None)

        # Register hotkeys that are new or changed
        def make_hotkey_handler(t: int, s: int):
            # CRITICAL: use root.after() to schedule playback on main thread;
            # running it inline in the keyboard hook freezes Windows input.
            def handler():
                try:
                    self.root.after(0, lambda: self._play_slot_from_tab(t, s))
                except Exception:
                    pass

            return handler

        for hk, (tab_idx, slot_idx) in desired.items():
            if current.get(hk) == (tab_idx, slot_idx):
                continue
            try:
                keyboard.add_hotkey(hk, make_hotkey_handler(tab_idx, slot_idx))
                current[hk] = (tab_idx, slot_idx)
            except Exception:
                pass

        # Persist the new map; keep the legacy list in sync for any callers
        # that still inspect `registered_hotkeys`.
        self._hotkey_map = current
        self.registered_hotkeys = list(current.keys())

    def _play_slot_from_tab(self, tab_idx: int, slot_idx: int):
        """Play a sound from a specific tab (for hotkeys)."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        tab = self.tabs[tab_idx]
        if slot_idx not in tab.slots:
            return

        slot = tab.slots[slot_idx]
        if self.mixer and self.mixer.running:
            # Create unique sound_id combining tab and slot
            sound_id = f"{tab_idx}_{slot_idx}"
            # play_sound returns the duration, avoiding a second cache lookup
            duration = self.mixer.play_sound(
                slot.file_path,
                slot.volume,
                slot.speed,
                slot.preserve_pitch,
                sound_id,
                loop=slot.loop,
                loop_count=slot.loop_count,
                loop_delay=slot.loop_delay,
            )

            # Always track playing state so progress shows when switching tabs
            if duration > 0:
                self.playing_slots[slot_idx] = {
                    "start_time": time.time(),
                    "duration": duration,
                    "tab_idx": tab_idx,
                    "loop": slot.loop,
                }

                loop_text = " (looping)" if slot.loop else ""

                def update_ui():
                    self.status_var.set(f"Playing: {slot.name}{loop_text}")
                    if tab_idx == self.current_tab_idx:
                        if slot_idx in self.slot_buttons:
                            self.slot_buttons[slot_idx].configure(fg_color=COLORS["playing"])
                        self._show_stop_button(slot_idx)

                try:
                    self.root.after(0, update_ui)
                except RuntimeError:
                    pass
            else:
                try:
                    self.root.after(0, lambda: self.status_var.set(f"Playing: {slot.name}"))
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------
    # YouTube → MP3 download
    # ------------------------------------------------------------------
    def _show_youtube_download_dialog(self):
        """Prompt for a YouTube URL and start a download in the background.

        Auto-pastes from the clipboard if the clipboard contains what looks
        like a YouTube URL, so the common case is just one click → Enter.
        """
        # Try to auto-fill from clipboard
        prefill = ""
        try:
            clip = self.root.clipboard_get()
            if isinstance(clip, str) and ("youtube.com" in clip or "youtu.be" in clip):
                prefill = clip.strip()
        except Exception:
            pass

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Download from YouTube")
        dialog.geometry("560x260")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        ctk.CTkLabel(
            frame,
            text="YouTube URL:",
            text_color=COLORS["text_primary"],
            font=self._font_sm,
        ).pack(anchor="w")

        url_var = tk.StringVar(value=prefill)
        url_entry = ctk.CTkEntry(
            frame,
            textvariable=url_var,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
            height=34,
        )
        url_entry.pack(fill=tk.X, pady=(4, 8))
        _bind_clipboard_shortcuts(url_entry)
        url_entry.focus_set()
        if prefill:
            url_entry.select_range(0, "end")

        # Optional cookies file (for age-restricted / login-only videos)
        ctk.CTkLabel(
            frame,
            text="Cookies file (optional, for age-restricted videos):",
            text_color=COLORS["text_secondary"],
            font=ctk.CTkFont(family=FONTS["family"], size=11),
        ).pack(anchor="w")

        cookies_row = ctk.CTkFrame(frame, fg_color="transparent")
        cookies_row.pack(fill=tk.X, pady=(2, 10))

        cookies_var = tk.StringVar(value=getattr(self, "_youtube_cookies_path", "") or "")
        cookies_entry = ctk.CTkEntry(
            cookies_row,
            textvariable=cookies_var,
            fg_color=COLORS["bg_medium"],
            border_color=COLORS["bg_light"],
            height=28,
        )
        cookies_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        _bind_clipboard_shortcuts(cookies_entry)

        def browse_cookies():
            fp = filedialog.askopenfilename(
                title="Select cookies.txt",
                filetypes=[("Cookies file", "*.txt"), ("All files", "*.*")],
            )
            if fp:
                cookies_var.set(fp)

        ctk.CTkButton(
            cookies_row,
            text="...",
            width=32,
            height=28,
            command=browse_cookies,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
        ).pack(side=tk.LEFT, padx=(4, 0))

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill=tk.X)

        def start():
            url = url_var.get().strip()
            if not url:
                messagebox.showwarning("YouTube", "Please paste a YouTube URL.")
                return
            cookies_path = cookies_var.get().strip() or None
            if cookies_path and not os.path.isfile(cookies_path):
                messagebox.showwarning("YouTube", "Cookies file not found.")
                return
            # Persist cookies path for next time
            self._youtube_cookies_path = cookies_path or ""
            self._save_config()
            dialog.destroy()
            self._start_youtube_download(url, cookies_path, self.current_tab_idx)

        ctk.CTkButton(
            btn_row,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=90,
            height=32,
        ).pack(side=tk.RIGHT, padx=(6, 0))

        ctk.CTkButton(
            btn_row,
            text="Download",
            command=start,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=110,
            height=32,
        ).pack(side=tk.RIGHT)

        url_entry.bind("<Return>", lambda e: start())

    def _start_youtube_download(self, url: str, cookies_path: Optional[str], target_tab_idx: int):
        """Show progress dialog and download a single YouTube video as MP3."""
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            messagebox.showerror(
                "yt-dlp missing",
                "yt-dlp is not installed.\n\nRun:\n  pip install yt-dlp",
            )
            return

        try:
            from static_ffmpeg import run as _sff_run  # type: ignore

            ffmpeg_exe, _ffprobe_exe = _sff_run.get_or_fetch_platform_executables_else_raise()
            ffmpeg_dir = os.path.dirname(ffmpeg_exe)
        except Exception:
            ffmpeg_dir = None

        # When running as a PyInstaller EXE, prefer the bundled ffmpeg+ffprobe
        # under sys._MEIPASS/ffmpeg_bin (see soundboard.spec). static-ffmpeg's
        # auto-download path may be unwritable inside the frozen bundle.
        try:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                bundled = os.path.join(meipass, "ffmpeg_bin")
                if os.path.isdir(bundled) and (
                    os.path.exists(os.path.join(bundled, "ffmpeg.exe"))
                    or os.path.exists(os.path.join(bundled, "ffmpeg"))
                ):
                    ffmpeg_dir = bundled
        except Exception:
            pass

        if not ffmpeg_dir:
            # Last-resort fallback: imageio-ffmpeg ships ffmpeg only (no ffprobe),
            # so the MP3 postprocessor will fail. Try anyway and surface the error.
            try:
                import imageio_ffmpeg  # type: ignore

                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                ffmpeg_dir = os.path.dirname(ffmpeg_path)
            except Exception:
                ffmpeg_dir = None

        # Progress dialog
        prog = ctk.CTkToplevel(self.root)
        prog.title("Downloading...")
        prog.geometry("420x150")
        prog.transient(self.root)
        prog.grab_set()
        prog.protocol("WM_DELETE_WINDOW", lambda: None)  # disable close

        pframe = ctk.CTkFrame(prog, fg_color=COLORS["bg_dark"], corner_radius=0)
        pframe.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)

        status_var = tk.StringVar(value="Fetching info...")
        ctk.CTkLabel(
            pframe,
            textvariable=status_var,
            text_color=COLORS["text_primary"],
            font=self._font_sm,
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 8))

        bar = ctk.CTkProgressBar(
            pframe,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"],
            height=14,
        )
        bar.pack(fill=tk.X)
        bar.set(0)

        cancel_flag = {"cancel": False}

        def on_cancel():
            cancel_flag["cancel"] = True
            status_var.set("Cancelling...")

        ctk.CTkButton(
            pframe,
            text="Cancel",
            command=on_cancel,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=90,
            height=28,
        ).pack(pady=(10, 0))

        result: Dict[str, Any] = {"path": None, "title": None, "error": None}

        # Output template — yt-dlp will replace .ext with .mp3 after postprocessing
        out_template = str(Path(SOUNDS_DIR).absolute() / "yt_%(id)s.%(ext)s")
        os.makedirs(SOUNDS_DIR, exist_ok=True)

        def hook(d):
            if cancel_flag["cancel"]:
                raise Exception("Cancelled by user")
            try:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    pct = (downloaded / total) if total else 0
                    title = (d.get("info_dict") or {}).get("title", "")
                    msg = f"Downloading: {int(pct * 100)}%"
                    if title:
                        short = title if len(title) <= 50 else title[:47] + "..."
                        msg = f"{short}\n{msg}"
                    self.root.after(0, lambda m=msg, p=pct: (status_var.set(m), bar.set(p)))
                elif d.get("status") == "finished":
                    self.root.after(
                        0, lambda: (status_var.set("Converting to MP3..."), bar.set(1.0))
                    )
            except Exception:
                pass

        def worker():
            ydl_opts: Dict[str, Any] = {
                "format": "bestaudio/best",
                "outtmpl": out_template,
                "noplaylist": True,  # critical: only download single video
                "quiet": True,
                "no_warnings": True,
                "progress_hooks": [hook],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
            if ffmpeg_dir:
                ydl_opts["ffmpeg_location"] = ffmpeg_dir
            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=True)
                    # If a playlist URL slipped through, take the first entry
                    if info and "entries" in info:
                        entries = list(info.get("entries") or [])
                        info = entries[0] if entries else None
                    if not info:
                        result["error"] = "No video info returned."
                        return
                    video_id = info.get("id", "")
                    title = info.get("title", "Sound") or "Sound"
                    final_path = str(Path(SOUNDS_DIR).absolute() / f"yt_{video_id}.mp3")
                    if not os.path.exists(final_path):
                        # Some versions name the postprocessed file differently;
                        # fall back to scanning sounds/ for the freshest yt_<id>.* file.
                        candidates = list(Path(SOUNDS_DIR).glob(f"yt_{video_id}.*"))
                        if candidates:
                            final_path = str(candidates[0].absolute())
                    result["path"] = final_path
                    result["title"] = title
            except Exception as e:
                if cancel_flag["cancel"]:
                    result["error"] = "Cancelled."
                else:
                    # Strip ANSI color codes from yt-dlp error messages
                    msg = re.sub(r"\x1b?\[[0-9;]*m", "", str(e))
                    result["error"] = msg

        def on_done():
            try:
                prog.grab_release()
            except Exception:
                pass
            try:
                prog.destroy()
            except Exception:
                pass

            if result["error"]:
                if result["error"] != "Cancelled.":
                    messagebox.showerror("Download failed", result["error"])
                # Clean up partial yt_*.* files for cancelled downloads
                return

            path = result["path"]
            title = result["title"] or "Sound"
            if not path or not os.path.exists(path):
                messagebox.showerror(
                    "Download failed",
                    "The download finished but the MP3 file could not be found.",
                )
                return

            self._create_slot_from_downloaded_file(path, title, target_tab_idx)

        def thread_target():
            try:
                worker()
            finally:
                self.root.after(0, on_done)

        threading.Thread(target=thread_target, daemon=True).start()

    def _create_slot_from_downloaded_file(self, file_path: str, title: str, target_tab_idx: int):
        """Add the downloaded MP3 to the cache and open the configure dialog."""
        try:
            local_path = self.sound_cache.add_sound(file_path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to add downloaded sound:\n{e}")
            return

        # Remove the original yt_*.mp3 file (add_sound copies to a hashed name)
        try:
            if os.path.abspath(file_path) != os.path.abspath(local_path):
                os.remove(file_path)
        except Exception:
            pass

        if target_tab_idx < 0 or target_tab_idx >= len(self.tabs):
            target_tab_idx = self.current_tab_idx
        tab = self.tabs[target_tab_idx]

        # Find first empty slot in the target tab (or grow)
        slot_idx = 0
        while slot_idx in tab.slots:
            slot_idx += 1

        tab.slots[slot_idx] = SoundSlot(
            name=title,
            file_path=local_path,
            hotkey=None,
            volume=1.0,
        )

        # Switch to the target tab if not already there
        if self.current_tab_idx != target_tab_idx:
            self._switch_tab(target_tab_idx)

        self._ensure_slots_for_tab(target_tab_idx)
        self._update_slot_button_for_tab(target_tab_idx, slot_idx)
        self._save_config()

        # Open configure dialog so user can tweak name/hotkey/trim
        if target_tab_idx == self.current_tab_idx:
            self._configure_slot(slot_idx)

    def _save_config(self):
        """Debounced save. Actual disk I/O happens in `_save_config_now()`.

        Many UI interactions (slider drags, drag-reorder, speed/volume
        changes) call `_save_config()` dozens of times per second. Writing
        JSON to disk on every call stalls the UI thread. Debouncing
        coalesces bursts into a single write ~400ms after activity stops.
        """
        # If we are shutting down or root is gone, save immediately and exit.
        if not getattr(self, "root", None):
            self._save_config_now()
            return
        if getattr(self, "_save_after_id", None):
            try:
                self.root.after_cancel(self._save_after_id)  # type: ignore[arg-type]
            except Exception:
                pass
        self._save_after_id = self.root.after(400, self._save_config_now)

    def _flush_save_config(self):
        """Force an immediate save (used on shutdown). Cancels any pending debounce."""
        if getattr(self, "_save_after_id", None):
            try:
                self.root.after_cancel(self._save_after_id)  # type: ignore[arg-type]
            except Exception:
                pass
            self._save_after_id = None
        self._save_config_now()

    def _save_config_now(self):
        """Write configuration to JSON file using atomic write to prevent corruption."""
        self._save_after_id = None
        config = {
            "tabs": [t.to_dict() for t in self.tabs],
            "current_tab": self.current_tab_idx,
            "ptt_enabled": self.ptt_enabled_var.get(),
            "ptt_key": self.ptt_key_var.get().strip() if self.ptt_key_var.get().strip() else None,
            "input_device": self.input_var.get() if self.input_var.get() else None,
            "output_device": self.output_var.get() if self.output_var.get() else None,
            "auto_start": self.auto_start_var.get() if hasattr(self, "auto_start_var") else True,
            "monitor_enabled": self.monitor_var.get() if hasattr(self, "monitor_var") else True,
            "now_playing_visible": (
                self.now_playing_panel.is_visible if hasattr(self, "now_playing_panel") else False
            ),
            "now_playing_side": (
                self.now_playing_panel.panel_side if hasattr(self, "now_playing_panel") else "right"
            ),
            "custom_groups": self._custom_groups,
            "youtube_cookies_path": getattr(self, "_youtube_cookies_path", "") or "",
            "noise_suppression": (
                self.noise_suppress_var.get() if hasattr(self, "noise_suppress_var") else False
            ),
            "noise_suppression_strength": (
                self.ns_strength_var.get() if hasattr(self, "ns_strength_var") else 85
            ),
        }

        # Atomic write: write to temp file first, then rename
        temp_file = CONFIG_FILE + ".tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            os.replace(temp_file, CONFIG_FILE)
        except Exception as e:
            print(f"Error saving config: {e}")
            # Clean up temp file if it exists
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    def _load_config(self):
        """Load configuration from JSON file."""
        if not os.path.exists(CONFIG_FILE):
            # Create default tab
            self.tabs = [SoundTab(name="Main", emoji="🎵")]
            # Defer the slot-grid build so the window can paint first.
            self.root.after(1, self._build_all_tab_widgets)
            self._refresh_tab_bar()
            return

        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)

            # Check if using new tab format or old format
            if "tabs" in config:
                # New format with tabs
                self.tabs = [SoundTab.from_dict(t) for t in config.get("tabs", [])]
                # Always start on first tab (index 0) for consistent behavior
                self.current_tab_idx = 0
            elif "slots" in config:
                # Old format - migrate to new format
                default_tab = SoundTab(name="Main", emoji="🎵")
                for idx, data in config.get("slots", {}).items():
                    default_tab.slots[int(idx)] = SoundSlot.from_dict(data)
                self.tabs = [default_tab]
                self.current_tab_idx = 0

            # Ensure at least one tab exists
            if not self.tabs:
                self.tabs = [SoundTab(name="Main", emoji="🎵")]

            # Ensure current_tab_idx is valid
            if self.current_tab_idx >= len(self.tabs):
                self.current_tab_idx = 0

            # Load PTT settings
            ptt_key = config.get("ptt_key")
            ptt_enabled = config.get("ptt_enabled", ptt_key is not None)  # Backward compat

            if ptt_key:
                self.ptt_key_var.set(ptt_key)
                self.ptt_status_label.configure(text=f"PTT: {ptt_key}", text_color=COLORS["green"])

            if ptt_enabled:
                self.ptt_enabled_var.set(True)
                self.ptt_frame.pack(fill=tk.X, pady=(8, 0))  # Show PTT settings

            # Load saved device selections
            saved_input = config.get("input_device")
            saved_output = config.get("output_device")

            if saved_input:
                # Check if the saved device is still in the available devices list
                current_values = self.input_combo.cget("values")
                if saved_input in current_values:
                    self.input_combo.set(saved_input)

            if saved_output:
                current_values = self.output_combo.cget("values")
                if saved_output in current_values:
                    self.output_combo.set(saved_output)

            # Load auto-start and monitor settings (default to True for new users)
            auto_start = config.get("auto_start", True)
            monitor_enabled = config.get("monitor_enabled", True)

            self.auto_start_var.set(auto_start)
            self.monitor_var.set(monitor_enabled)

            # Load noise suppression settings
            ns_enabled = bool(config.get("noise_suppression", False))
            ns_strength = float(config.get("noise_suppression_strength", 85))
            if hasattr(self, "noise_suppress_var"):
                self.noise_suppress_var.set(ns_enabled)
            if hasattr(self, "ns_strength_var"):
                self.ns_strength_var.set(ns_strength)

            # Load Now Playing panel settings
            now_playing_visible = config.get("now_playing_visible", False)
            now_playing_side = config.get("now_playing_side", "right")

            # Load custom groups
            self._custom_groups = config.get("custom_groups", [])
            self._refresh_group_combo()

            # Load YouTube downloader cookies path
            self._youtube_cookies_path = config.get("youtube_cookies_path", "") or ""

            if hasattr(self, "now_playing_panel"):
                self.now_playing_panel.set_side(now_playing_side)
                if now_playing_visible:
                    self.now_playing_panel.show()
                    self.now_playing_btn.configure(
                        fg_color=COLORS["blurple"],
                        hover_color=COLORS["blurple_hover"],
                    )

            # Build widgets for ALL tabs upfront (for instant tab switching).
            # Deferred via after(1) so the chrome can paint first — building
            # the slot grid synchronously here adds visible "Generate" lag at
            # startup. The grid will appear ~1 frame after the window opens.
            self.root.after(1, self._build_all_tab_widgets)
            self._refresh_tab_bar()
            self._register_hotkeys()

            # Auto-start the stream if enabled and devices are selected
            if auto_start and self.input_var.get() and self.output_var.get():
                self.root.after(100, self._auto_start_stream)

        except Exception as e:
            print(f"Error loading config: {e}")
            # Create default tab on error
            self.tabs = [SoundTab(name="Main", emoji="🎵")]
            self.root.after(1, self._build_all_tab_widgets)
            self._refresh_tab_bar()

    def _auto_start_stream(self):
        """Auto-start the audio stream after config load."""
        if not self.mixer or not self.mixer.running:
            self._toggle_stream()
            # Apply monitor setting after stream starts
            if hasattr(self, "monitor_var") and self.mixer:
                self.mixer.set_monitor_enabled(self.monitor_var.get())

    def _preload_sounds(self):
        """Preload all configured sounds into memory cache in a background thread."""
        sound_paths = []
        for tab in self.tabs:
            for slot in tab.slots.values():
                if slot.file_path:
                    sound_paths.append(slot.file_path)

        if sound_paths:
            self.status_var.set(f"Loading {len(sound_paths)} sounds...")

            def _do_preload():
                self.sound_cache.preload_sounds(sound_paths)
                try:
                    self.root.after(
                        0, lambda: self.status_var.set(f"Ready - {len(sound_paths)} sounds cached")
                    )
                except RuntimeError:
                    pass  # Main loop not running (app closing or not started yet)

            threading.Thread(target=_do_preload, daemon=True).start()
        else:
            self.status_var.set("Ready")

    def _on_close(self):
        """Handle application close. BULLETPROOF - guarantees process termination."""
        # CRITICAL: Release PTT key FIRST to prevent Windows UI freeze
        if self.mixer:
            try:
                self.mixer._force_release_ptt()
            except Exception:
                pass

        # Schedule force-kill as absolute last resort (500ms timeout)
        # This runs in a daemon thread so it won't block
        def force_kill():
            import time

            time.sleep(0.5)  # Give normal shutdown 500ms
            os._exit(0)  # Nuclear option - kills process immediately

        kill_thread = threading.Thread(target=force_kill, daemon=True)
        kill_thread.start()

        # Save config (quick operation) — force a synchronous write so any
        # pending debounced save is not lost on shutdown.
        try:
            self._flush_save_config()
        except Exception:
            pass

        # Signal mixer to shut down (sets _shutting_down flag and releases PTT)
        if self.mixer:
            try:
                self.mixer.stop()
            except Exception:
                pass

        # Destroy window immediately
        try:
            self.root.destroy()
        except Exception:
            pass

        # If we get here, exit cleanly before force_kill triggers
        try:
            os._exit(0)
        except Exception:
            pass

    def run(self):
        """Start the application main loop."""
        try:
            self.root.mainloop()
        except (KeyboardInterrupt, SystemExit):
            pass
