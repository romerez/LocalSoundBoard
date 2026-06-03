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
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import customtkinter as ctk
import sounddevice as sd
from PIL import Image, ImageDraw, ImageFont, ImageTk

from .audio import AudioMixer, SoundCache, Recorder
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
import subprocess
import shutil

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
    Return text unchanged.

    Previous code attempted to pre-reverse RTL text for Tkinter Canvas BiDi
    behavior, but Hebrew slot labels are now displayed correctly without
    special reversal. This helper remains for future RTL adjustments.
    """
    return text


def _split_slot_text_into_two_lines(text: str, max_chars: int = 18) -> tuple[str, str]:
    text = text.strip()
    if len(text) <= max_chars:
        return text, ""
    if " " in text:
        split_at = text.rfind(" ", 0, max_chars)
        if split_at == -1:
            split_at = max_chars
        first = text[:split_at].rstrip()
        second = text[split_at:].strip()
    else:
        first = text[:max_chars]
        second = text[max_chars:].strip()
    if len(second) > max_chars:
        second = second[: max_chars - 1].rstrip() + "…"
    return first, second


def _format_slot_display_text(name: str, hotkey: Optional[str], loop: bool) -> str:
    name = (name or "").strip()
    hotkey_text = f"[{hotkey}]" if hotkey else ""
    loop_indicator = " 🔁" if loop else ""

    if hotkey_text:
        if len(name) <= 20:
            first_line = f"{name}{loop_indicator}".strip()
        else:
            first_line, _ = _split_slot_text_into_two_lines(name, 18)
            first_line = f"{first_line}{loop_indicator}".strip()
        return _fix_rtl_text(f"{first_line}\n{hotkey_text}")

    if len(name) <= 18:
        return _fix_rtl_text(f"{name}{loop_indicator}".strip())

    first_line, second_line = _split_slot_text_into_two_lines(name, 18)
    if second_line:
        return _fix_rtl_text(f"{first_line}\n{second_line}{loop_indicator}".strip())
    return _fix_rtl_text(f"{first_line}{loop_indicator}".strip())


def _mouse_button_to_key_name(button: Any) -> str:
    """Normalize mouse-library button names to the app's mouseN format."""
    if button == "x":
        return "mouse5"
    if button == "x2":
        return "mouse4"
    if button == "left":
        return "mouse1"
    if button == "right":
        return "mouse2"
    if button == "middle":
        return "mouse3"
    return f"mouse_{button}"


def _unpack_multicut_result(item: Any, fallback_title: str) -> tuple[Any, int, str]:
    """Accept old/new multi-cut tuple shapes and return audio, sample rate, title."""
    try:
        audio_data = item[0]
        sample_rate = int(item[1])
        title = str(item[2]).strip() if len(item) >= 3 else ""
    except Exception:
        audio_data, sample_rate, title = item
    return audio_data, sample_rate, title or fallback_title


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


# ---------------------------------------------------------------------------
# Windows "Start with Windows" helpers (registry-based, no extra deps).
#
# We write to HKCU\Software\Microsoft\Windows\CurrentVersion\Run, which adds
# the app to the user's startup list without requiring admin rights.
# ---------------------------------------------------------------------------

_STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_VALUE = "LocalSoundBoard"


def _windows_startup_command() -> str:
    """Return the command line to launch the app on Windows boot."""
    # Frozen (PyInstaller build) — just launch the EXE itself.
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'

    # Dev mode: prefer launch.bat / run.bat next to the workspace root so the
    # CWD matches what the user normally launches with (shared config / sounds).
    workspace_root = Path(__file__).resolve().parents[1]
    for candidate in ("launch.bat", "run.bat"):
        path = workspace_root / candidate
        if path.exists():
            return f'"{path}"'

    # Fallback: pythonw.exe main.py (no console window)
    main_py = workspace_root / "main.py"
    pyw = sys.executable
    if pyw.lower().endswith("python.exe"):
        candidate = pyw[:-10] + "pythonw.exe"
        if Path(candidate).exists():
            pyw = candidate
    return f'"{pyw}" "{main_py}"'


def _windows_startup_set(enabled: bool) -> bool:
    """Add or remove the auto-start registry entry. Returns True on success."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        if enabled:
            cmd = _windows_startup_command()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
            ) as k:
                winreg.SetValueEx(k, _STARTUP_REG_VALUE, 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
                ) as k:
                    winreg.DeleteValue(k, _STARTUP_REG_VALUE)
            except FileNotFoundError:
                pass  # already absent
        return True
    except Exception as e:
        print(f"[startup] Failed to {'set' if enabled else 'clear'} registry entry: {e}")
        return False


def _windows_startup_is_enabled() -> bool:
    """Return True if the auto-start registry entry exists."""
    if sys.platform != "win32":
        return False
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY) as k:
            winreg.QueryValueEx(k, _STARTUP_REG_VALUE)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tooltip helper
# ---------------------------------------------------------------------------
class _Tooltip:
    """Small, non-annoying tooltip shown after a hover delay.

    Call `_Tooltip.attach(widget, "text")` (or pass `delay_ms=`) to wire
    one up. The popup is a borderless `tk.Toplevel` with a single label —
    cheap to create and torn down on `<Leave>` / motion / click.
    """

    _instance: Optional["_Tooltip"] = None  # singleton — only one shows at a time

    def __init__(self, widget: Any, text: str, delay_ms: int = 3000):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: Optional[str] = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")
        widget.bind("<Motion>", self._on_motion, add="+")

    @classmethod
    def attach(cls, widget: Any, text: str, delay_ms: int = 3000) -> "_Tooltip":
        return cls(widget, text, delay_ms)

    def _on_enter(self, _event=None):
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _on_motion(self, _event=None):
        # Reset the timer when the cursor moves so casual hovers don't
        # trigger; only true "I'm pausing on this control" shows the tip.
        if self._tip is None:
            self._on_enter()

    def _on_leave(self, _event=None):
        self._cancel()
        self._hide()

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        self._after_id = None
        # Hide any other tooltip currently on screen — only one at a time.
        other = _Tooltip._instance
        if other is not None and other is not self:
            try:
                other._hide()
            except Exception:
                pass
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        except Exception:
            return
        try:
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            try:
                tip.attributes("-topmost", True)
            except Exception:
                pass
            label = tk.Label(
                tip,
                text=self.text,
                background="#1e1f22",
                foreground="#dbdee1",
                font=("Segoe UI", 8),
                padx=6,
                pady=2,
                bd=1,
                relief="solid",
                highlightthickness=0,
            )
            label.pack()
            self._tip = tip
            _Tooltip._instance = self
        except Exception:
            self._tip = None

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
        if _Tooltip._instance is self:
            _Tooltip._instance = None


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
        on_play_staged: Optional[Callable[[int, int], None]] = None,
        on_edit_staged: Optional[Callable[[int, int], None]] = None,
        on_get_slot: Optional[Callable[[int, int], Optional[SoundSlot]]] = None,
        on_slot_changed: Optional[Callable[[int, int], None]] = None,
    ):
        self.parent = parent
        self.mixer_ref = mixer_ref
        self.on_stop_callback = on_stop_callback
        # Called when the user clicks the ▶ button on a staged (drag-dropped)
        # item: receives (tab_idx, slot_idx). Wired up by SoundboardApp.
        self.on_play_staged = on_play_staged
        # Called when the user clicks the ✏ Edit button on a staged item.
        self.on_edit_staged = on_edit_staged
        # Returns the SoundSlot for (tab_idx, slot_idx). Used by the staged
        # item's inline volume/speed/loop controls so they can read+write
        # the underlying slot config before play.
        self.on_get_slot = on_get_slot
        # Called after the staged item edits a SoundSlot field, so the app
        # can persist the change (debounced inside _save_config).
        self.on_slot_changed = on_slot_changed
        self.is_visible = False

        self.frame: Optional[ctk.CTkFrame] = None
        self.items_frame: Optional[ctk.CTkFrame] = None
        self.sound_items: Dict[str, Dict[str, Any]] = {}
        # Staged items: sounds dragged onto the panel that haven't been
        # played yet. Key is "{tab_idx}_{slot_idx}" so the same slot can
        # only be staged once at a time.
        self.staged_items: Dict[str, Dict[str, Any]] = {}

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

        # Pause/Play All toggle — affects every currently-playing sound in
        # the panel. Label flips between ⏸ and ▶ depending on aggregate
        # state (see _refresh_pause_all_button).
        self.pause_all_btn = ctk.CTkButton(
            header_frame,
            text="⏸ Pause All",
            command=self._on_pause_all_click,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold"),
            corner_radius=6,
            width=92,
            height=26,
        )
        self.pause_all_btn.pack(side=tk.RIGHT)
        _Tooltip.attach(self.pause_all_btn, "Pause/resume every active sound at once")

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

    def _repack_panel(self):
        if self.frame is None:
            return
        self.frame.pack_forget()
        if self.is_visible:
            self.frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))

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

    # ----- update loop -----

    def update(self, playing_sounds: list, playing_slots: Optional[dict] = None):
        current_ids = set()

        if playing_sounds:
            self.empty_label.pack_forget()
        else:
            # Only show empty label if there are no staged items either.
            if not self.staged_items:
                self.empty_label.pack(pady=20)
            else:
                self.empty_label.pack_forget()
            for sound_id in list(self.sound_items.keys()):
                self._remove_item(sound_id)
            self._refresh_pause_all_button()
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

        self._refresh_pause_all_button()

    # ----- staged items (drag-dropped, not playing yet) -----

    def add_staged(
        self,
        tab_idx: int,
        slot_idx: int,
        name: str,
        emoji: str = "",
        color: Optional[str] = None,
    ) -> None:
        """Show a staged (not-yet-playing) sound item at the top of the panel.

        Triggered by dragging a slot onto the panel. The item shows a
        ▶ Play button (calls `on_play_staged` callback) and a ✕ Remove
        button. Staged items persist across `update()` calls.
        """
        if self.items_frame is None:
            return
        staged_id = f"{tab_idx}_{slot_idx}"
        # If this slot is already staged, briefly highlight it instead.
        if staged_id in self.staged_items:
            try:
                outer = self.staged_items[staged_id].get("outer_frame")
                if outer is not None:
                    outer.configure(fg_color=COLORS.get("blurple", "#5865F2"))
                    self.items_frame.after(
                        300,
                        lambda o=outer: o.configure(fg_color=COLORS["bg_medium"]),
                    )
            except Exception:
                pass
            return

        # Hide empty label if it's showing.
        try:
            self.empty_label.pack_forget()
        except Exception:
            pass

        outer_frame = ctk.CTkFrame(
            self.items_frame,
            fg_color=COLORS["bg_medium"],
            corner_radius=8,
            border_width=2,
            border_color=color or COLORS.get("blurple", "#5865F2"),
        )
        outer_frame.pack(fill=tk.X, pady=4)
        try:
            outer_frame.lift()
        except Exception:
            pass

        # Resolve the underlying SoundSlot so the inline controls can
        # read+write its volume / speed / loop config directly.
        slot = self.on_get_slot(tab_idx, slot_idx) if self.on_get_slot else None

        def _notify_changed():
            if self.on_slot_changed is not None:
                try:
                    self.on_slot_changed(tab_idx, slot_idx)
                except Exception:
                    pass

        # ── Title row: name + Edit + ✕ Remove + ▶ Play ──
        title_row = ctk.CTkFrame(outer_frame, fg_color="transparent")
        title_row.pack(fill=tk.X, padx=10, pady=(8, 2))

        display_name = (emoji + " " if emoji else "") + (name or "Unknown")
        if len(display_name) > 26:
            display_name = display_name[:25] + "…"

        name_label = ctk.CTkLabel(
            title_row,
            text=display_name,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        )
        name_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def _do_play():
            cb = self.on_play_staged
            self.remove_staged(staged_id)
            if cb is not None:
                try:
                    cb(tab_idx, slot_idx)
                except Exception:
                    pass

        def _do_edit():
            cb = self.on_edit_staged
            if cb is not None:
                try:
                    cb(tab_idx, slot_idx)
                except Exception:
                    pass

        btn_font = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold")

        remove_btn = ctk.CTkButton(
            title_row,
            text="✕",
            command=lambda sid=staged_id: self.remove_staged(sid),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=6,
            width=26,
            height=24,
        )
        remove_btn.pack(side=tk.RIGHT, padx=(3, 0))

        play_btn = ctk.CTkButton(
            title_row,
            text="▶",
            command=_do_play,
            fg_color=COLORS["green"],
            hover_color=COLORS.get("green_hover", COLORS["green"]),
            font=btn_font,
            corner_radius=6,
            width=30,
            height=24,
        )
        play_btn.pack(side=tk.RIGHT, padx=(3, 0))

        edit_btn = ctk.CTkButton(
            title_row,
            text="✏",
            command=_do_edit,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=6,
            width=26,
            height=24,
        )
        edit_btn.pack(side=tk.RIGHT, padx=(3, 0))

        _Tooltip.attach(remove_btn, "Remove from staging")
        _Tooltip.attach(play_btn, "Play this sound")
        _Tooltip.attach(edit_btn, "Open full slot editor")

        small_font = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"])
        mono_font = ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"])
        ctrl_btn_font = small_font

        # If we don't have a slot reference, just show the subtitle and stop.
        if slot is None:
            sub_label = ctk.CTkLabel(
                outer_frame,
                text="Staged · ✏ edit · ▶ play · ✕ remove",
                font=small_font,
                text_color=COLORS["text_muted"],
                anchor="w",
            )
            sub_label.pack(fill=tk.X, padx=10, pady=(0, 8))
            self.staged_items[staged_id] = {
                "outer_frame": outer_frame,
                "tab_idx": tab_idx,
                "slot_idx": slot_idx,
            }
            return

        # ── Volume row ──
        vol_row = ctk.CTkFrame(outer_frame, fg_color="transparent")
        vol_row.pack(fill=tk.X, padx=10, pady=(4, 0))
        ctk.CTkLabel(
            vol_row, text="🔊", font=small_font, text_color=COLORS["text_muted"], width=18
        ).pack(side=tk.LEFT)

        vol_var = tk.IntVar(value=int(slot.volume * 100))
        vol_value_label = ctk.CTkLabel(
            vol_row,
            text=f"{int(slot.volume * 100)}%",
            font=mono_font,
            text_color=COLORS["text_muted"],
            width=34,
        )

        def _on_vol_change(val):
            v = float(val) / 100.0
            slot.volume = v
            vol_value_label.configure(text=f"{int(v * 100)}%")
            _notify_changed()

        vol_slider = ctk.CTkSlider(
            vol_row,
            from_=0,
            to=150,
            variable=vol_var,
            command=_on_vol_change,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["green"],
            button_color=COLORS["green"],
            button_hover_color=COLORS["green_hover"],
        )
        vol_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)
        vol_value_label.pack(side=tk.LEFT)
        _Tooltip.attach(vol_slider, "Volume (0–150%)")

        def _reset_vol():
            vol_var.set(100)
            slot.volume = 1.0
            vol_value_label.configure(text="100%")
            _notify_changed()

        ctk.CTkButton(
            vol_row,
            text="↺",
            command=_reset_vol,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctrl_btn_font,
            corner_radius=4,
            width=22,
            height=22,
        ).pack(side=tk.LEFT, padx=(2, 0))

        # ── Speed row ──
        spd_row = ctk.CTkFrame(outer_frame, fg_color="transparent")
        spd_row.pack(fill=tk.X, padx=10, pady=(4, 0))
        ctk.CTkLabel(
            spd_row, text="⚡", font=small_font, text_color=COLORS["text_muted"], width=18
        ).pack(side=tk.LEFT)

        spd_var = tk.DoubleVar(value=float(slot.speed * 100))
        spd_value_label = ctk.CTkLabel(
            spd_row,
            text=f"{slot.speed:.1f}x",
            font=mono_font,
            text_color=COLORS["text_muted"],
            width=34,
        )

        def _on_spd_change(val):
            try:
                v = float(val) / 100.0
            except (TypeError, ValueError):
                return
            slot.speed = v
            spd_value_label.configure(text=f"{v:.1f}x")
            _notify_changed()

        spd_slider = ctk.CTkSlider(
            spd_row,
            from_=50,
            to=200,
            variable=spd_var,
            command=_on_spd_change,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        spd_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)
        spd_value_label.pack(side=tk.LEFT)
        _Tooltip.attach(spd_slider, "Playback speed (0.5x–2x)")

        # Pitch toggle (🎵 preserve / 🐿 chipmunk)
        pitch_state: List[bool] = [bool(slot.preserve_pitch)]

        def _toggle_pitch():
            pitch_state[0] = not pitch_state[0]
            slot.preserve_pitch = pitch_state[0]
            pitch_btn.configure(
                text="🎵" if pitch_state[0] else "🐿",
                fg_color=COLORS["green"] if pitch_state[0] else COLORS["bg_light"],
                hover_color=(
                    COLORS["green_hover"] if pitch_state[0] else COLORS["bg_lighter"]
                ),
            )
            _notify_changed()

        pitch_btn = ctk.CTkButton(
            spd_row,
            text="🎵" if pitch_state[0] else "🐿",
            command=_toggle_pitch,
            fg_color=COLORS["green"] if pitch_state[0] else COLORS["bg_light"],
            hover_color=(
                COLORS["green_hover"] if pitch_state[0] else COLORS["bg_lighter"]
            ),
            font=ctrl_btn_font,
            corner_radius=4,
            width=24,
            height=22,
        )
        pitch_btn.pack(side=tk.LEFT, padx=(2, 2))
        _Tooltip.attach(pitch_btn, "🎵 keep original pitch · 🐿 chipmunk/deep voice")

        def _reset_spd():
            spd_var.set(100)
            slot.speed = 1.0
            spd_value_label.configure(text="1.0x")
            _notify_changed()

        ctk.CTkButton(
            spd_row,
            text="↺",
            command=_reset_spd,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctrl_btn_font,
            corner_radius=4,
            width=22,
            height=22,
        ).pack(side=tk.LEFT, padx=(2, 0))

        # ── Loop row: single explicit selector [OFF] [∞] [2x] [5x] [10x] ──
        # We deliberately fold loop on/off + count into ONE row so there's
        # no ambiguous "toggle is on but count is also persisted" state to
        # get stuck in. Click OFF to disable looping; click any count to
        # enable looping with that count.
        loop_row = ctk.CTkFrame(outer_frame, fg_color="transparent")
        loop_row.pack(fill=tk.X, padx=10, pady=(6, 0))

        ctk.CTkLabel(
            loop_row,
            text="🔁",
            font=small_font,
            text_color=COLORS["text_muted"],
            width=18,
        ).pack(side=tk.LEFT)

        loop_preset_btns: Dict[str, Any] = {}
        delay_row_ref: List[Any] = [None]

        def _current_loop_key() -> str:
            if not slot.loop:
                return "off"
            return "inf" if (slot.loop_count or 0) == 0 else str(slot.loop_count)

        def _set_loop_visibility():
            row = delay_row_ref[0]
            if row is None:
                return
            try:
                if slot.loop:
                    row.pack(fill=tk.X, padx=10, pady=(4, 0))
                else:
                    row.pack_forget()
            except Exception:
                pass

        def _restyle_loop_presets():
            active_key = _current_loop_key()
            for key, b in loop_preset_btns.items():
                active = key == active_key
                # OFF gets red-when-active so it's visually distinct from
                # the "enable looping" buttons.
                if key == "off":
                    fg = COLORS["red"] if active else COLORS["bg_light"]
                    hv = COLORS.get("red_hover", COLORS["red"]) if active else COLORS["bg_lighter"]
                else:
                    fg = COLORS["green"] if active else COLORS["bg_light"]
                    hv = COLORS.get("green_hover", COLORS["green"]) if active else COLORS["bg_lighter"]
                try:
                    b.configure(fg_color=fg, hover_color=hv)
                except Exception:
                    pass

        def _make_set_loop(key: str, loop_on: bool, count: int):
            def _do():
                slot.loop = loop_on
                slot.loop_count = count
                _restyle_loop_presets()
                _set_loop_visibility()
                _notify_changed()

            return _do

        # (label, key, loop_on, count)
        loop_choices = (
            ("OFF", "off", False, 0),
            ("∞", "inf", True, 0),
            ("2x", "2", True, 2),
            ("5x", "5", True, 5),
            ("10x", "10", True, 10),
        )
        loop_tips = {
            "off": "Stop looping (play once)",
            "inf": "Loop forever until stopped",
            "2": "Play 2 times",
            "5": "Play 5 times",
            "10": "Play 10 times",
        }
        for label_text, key, loop_on, count in loop_choices:
            b = ctk.CTkButton(
                loop_row,
                text=label_text,
                command=_make_set_loop(key, loop_on, count),
                fg_color=COLORS["bg_light"],
                hover_color=COLORS["bg_lighter"],
                font=ctrl_btn_font,
                corner_radius=4,
                width=34 if key == "off" else 28,
                height=22,
            )
            b.pack(side=tk.LEFT, padx=(2, 0))
            loop_preset_btns[key] = b
            _Tooltip.attach(b, loop_tips[key])
        _restyle_loop_presets()

        # ── Loop delay row (only visible when looping) ──
        delay_row = ctk.CTkFrame(outer_frame, fg_color="transparent")
        delay_row_ref[0] = delay_row
        ctk.CTkLabel(
            delay_row, text="⏱", font=small_font, text_color=COLORS["text_muted"], width=18
        ).pack(side=tk.LEFT)

        delay_var = tk.IntVar(value=int((slot.loop_delay or 0.0) * 10))
        delay_value_label = ctk.CTkLabel(
            delay_row,
            text=f"{slot.loop_delay or 0.0:.1f}s",
            font=mono_font,
            text_color=COLORS["text_muted"],
            width=34,
        )

        def _on_delay_change(val):
            v = float(val) / 10.0
            slot.loop_delay = v
            delay_value_label.configure(text=f"{v:.1f}s")
            _notify_changed()

        delay_slider = ctk.CTkSlider(
            delay_row,
            from_=0,
            to=100,
            variable=delay_var,
            command=_on_delay_change,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["yellow"],
            button_color=COLORS["yellow"],
            button_hover_color=COLORS["yellow"],
        )
        delay_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)
        delay_value_label.pack(side=tk.LEFT)
        _Tooltip.attach(delay_slider, "Pause between loops (0–10s)")

        _set_loop_visibility()

        # Subtitle row at the bottom
        sub_label = ctk.CTkLabel(
            outer_frame,
            text="Staged · changes save to slot · ▶ to play",
            font=small_font,
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        sub_label.pack(fill=tk.X, padx=10, pady=(4, 8))

        self.staged_items[staged_id] = {
            "outer_frame": outer_frame,
            "tab_idx": tab_idx,
            "slot_idx": slot_idx,
        }

    def remove_staged(self, staged_id: str) -> None:
        item = self.staged_items.pop(staged_id, None)
        if item and item.get("outer_frame"):
            try:
                item["outer_frame"].destroy()
            except Exception:
                pass
        # Restore empty label if everything is gone now.
        if not self.staged_items and not self.sound_items:
            try:
                self.empty_label.pack(pady=20)
            except Exception:
                pass

    def clear_staged(self) -> None:
        for sid in list(self.staged_items.keys()):
            self.remove_staged(sid)

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
        _Tooltip.attach(stop_btn, "Stop this sound")

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
        _Tooltip.attach(play_pause_btn, "Pause / resume this sound")

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
        _Tooltip.attach(loop_btn, "Toggle looping for this playback")

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
        _Tooltip.attach(restart_btn, "Restart from the beginning")

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
        # DoubleVar (not IntVar) — IntVar quantizes the slider's float write
        # which on some Tk builds creates a feedback loop where the slider
        # silently refuses to move below the previous value (e.g. <100).
        speed_var = tk.DoubleVar(value=float(current_speed * 100))
        preserve_pitch_state: List[bool] = [True]
        # Override flag prevents _update_item from snapping the value label
        # back to the mixer's stale speed while the user is interacting.
        speed_user_override: List[bool] = [False]

        # Create the value label FIRST so the slider's initial set() (which
        # may fire `command`) doesn't NameError on a not-yet-created widget.
        speed_value_label = ctk.CTkLabel(
            row4b,
            text=f"{current_speed:.1f}x",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=30,
        )

        # Default-enable real-time pitch-preserving stretch on the mixer
        # side. At 1.0 this is free (fast int path); only kicks in when
        # the user moves the slider away from 100%.
        _initial_mixer = self._get_mixer()
        if _initial_mixer:
            _initial_mixer.set_pitch_preserve_live(sound_id, True)

        def _on_speed_change(val):
            # Instant, zero-delay speed change. The mixer applies the new
            # rate on the very next audio callback (~21ms). When
            # preserve-pitch is ON (🎵, default), the callback uses WSOLA
            # OLA so pitch stays correct. When OFF (🐿), it uses cheap
            # linear interpolation so pitch shifts (chipmunk/deep voice).
            speed_user_override[0] = True
            try:
                v = float(val)
            except (TypeError, ValueError):
                return
            new_speed = v / 100.0
            speed_value_label.configure(text=f"{new_speed:.1f}x")
            mixer = self._get_mixer()
            if mixer:
                mixer.set_playback_rate(sound_id, new_speed)
            # Release override after a short window so _update_item can
            # resync to the mixer's value if it diverges.
            if self.items_frame is not None:
                self.items_frame.after(600, lambda: speed_user_override.__setitem__(0, False))

        speed_slider = ctk.CTkSlider(
            row4b,
            from_=50,
            to=200,
            variable=speed_var,
            command=_on_speed_change,
            width=90,
            height=14,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
            button_hover_color=COLORS["blurple_hover"],
        )
        speed_slider.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)

        speed_value_label.pack(side=tk.LEFT)
        _Tooltip.attach(speed_slider, "Live speed (0.5x–2x)")

        # Pitch preservation toggle. Flips the mixer's per-sound
        # `pitch_preserve_live` flag, which switches the audio callback
        # between the WSOLA OLA path (🎵 ON, pitch stays original) and
        # the linear-interp resample path (🐿 OFF, chipmunk/deep voice).
        # The change takes effect on the very next audio callback.
        def _toggle_pitch():
            preserve_pitch_state[0] = not preserve_pitch_state[0]
            pitch_btn.configure(
                text="🎵" if preserve_pitch_state[0] else "🐿",
                fg_color=COLORS["green"] if preserve_pitch_state[0] else COLORS["bg_light"],
                hover_color=(
                    COLORS["green_hover"] if preserve_pitch_state[0] else COLORS["bg_lighter"]
                ),
            )
            mixer = self._get_mixer()
            if mixer:
                mixer.set_pitch_preserve_live(sound_id, preserve_pitch_state[0])

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
        _Tooltip.attach(pitch_btn, "🎵 keep pitch · 🐿 chipmunk/deep voice")

        # Reset speed
        def _reset_speed():
            speed_user_override[0] = True
            speed_var.set(100)
            speed_value_label.configure(text="1.0x")
            mixer = self._get_mixer()
            if mixer:
                # Just clear the live rate — the WSOLA path detects rate==1.0
                # via the fast int path automatically. No librosa needed.
                mixer.set_playback_rate(sound_id, 1.0)
            if self.items_frame is not None:
                self.items_frame.after(400, lambda: speed_user_override.__setitem__(0, False))

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
        _Tooltip.attach(reset_speed_btn, "Reset speed to 1.0x")

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
        _Tooltip.attach(volume_slider, "Live volume (0–150%)")
        _Tooltip.attach(reset_vol_btn, "Reset volume to 100%")

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
        _Tooltip.attach(delay_slider, "Pause between loops (0–10s)")
        _Tooltip.attach(inf_btn, "Loop forever")

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
            _Tooltip.attach(btn, f"Loop {cnt} times")

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
            "speed_var": speed_var,
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

    def _on_pause_all_click(self):
        """Pause every active sound — or, if all are paused, resume them all."""
        mixer = self._get_mixer()
        if not mixer or not self.sound_items:
            return
        # If at least one sound is currently playing (not paused), pause
        # everything. Otherwise resume everything.
        any_playing = any(
            not item.get("sound_info", {}).get("paused", False)
            for item in self.sound_items.values()
        )
        for sid in list(self.sound_items.keys()):
            try:
                if any_playing:
                    mixer.pause_sound(sid)
                else:
                    mixer.resume_sound(sid)
            except Exception:
                pass
        self._refresh_pause_all_button(force_paused=any_playing)

    def _refresh_pause_all_button(self, force_paused: Optional[bool] = None):
        """Sync the Pause/Play All button label + enabled state."""
        btn = getattr(self, "pause_all_btn", None)
        if btn is None:
            return
        try:
            if not self.sound_items:
                btn.configure(
                    text="⏸ Pause All",
                    state="disabled",
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                )
                return

            if force_paused is not None:
                all_paused = force_paused
            else:
                all_paused = all(
                    item.get("sound_info", {}).get("paused", False)
                    for item in self.sound_items.values()
                )

            if all_paused:
                btn.configure(
                    text="▶ Play All",
                    state="normal",
                    fg_color=COLORS["green"],
                    hover_color=COLORS.get("green_hover", COLORS["green"]),
                )
            else:
                btn.configure(
                    text="⏸ Pause All",
                    state="normal",
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                )
        except Exception:
            pass

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

        # Remove the panel card synchronously. Otherwise a sound that was
        # paused (and whose visual duration timer already expired in
        # _animate_progress) leaves the card on screen — the next animation
        # tick won't reap it because playing_slots no longer contains it.
        try:
            self._remove_item(sound_id)
            self._refresh_pause_all_button()
            # If nothing's left at all, restore the empty-state label.
            if not self.sound_items and not self.staged_items and self.empty_label is not None:
                self.empty_label.pack(pady=20)
        except Exception:
            pass


class SoundboardApp:
    """Main GUI application for the soundboard."""

    def __init__(self):
        self.root = ctk.CTk()
        self.root.title(UI["window_title"])
        self.root.resizable(True, True)  # Allow resizing
        self.root.configure(fg_color=COLORS["bg_darkest"])

        self.mixer: Optional[AudioMixer] = None
        self.sound_cache = SoundCache()  # Local sound storage with caching
        # Call recorder (WASAPI loopback). Lazy-created when user clicks Record.
        self.recorder: Optional[Recorder] = None
        self._recording_after_id: Optional[str] = None
        self.quick_sound_recorder: Optional[Recorder] = None
        self._quick_record_after_id: Optional[str] = None
        self._editor_prepare_jobs: set[str] = set()
        # System tray icon (lazy — only started when window is hidden).
        self._tray = None  # type: ignore[assignment]
        self._tray_hidden: bool = False
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
        self.tab_slot_group_labels: Dict[int, Dict[int, Any]] = {}  # footer label below each slot showing groups
        self.tab_slot_wrappers: Dict[int, Dict[int, Any]] = {}  # wrapper frame containing slot + footer
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
        self.hover_preview_key_var = tk.StringVar(value="mouse3")
        self.scroll_speed_var = tk.IntVar(value=10)
        self._scroll_speed_label: Optional[Any] = None
        self._hover_preview_keyboard_key: Optional[str] = None
        self._hover_preview_keyboard_handle: Optional[Any] = None
        self._hover_preview_mouse_hook: Optional[Any] = None
        self._hover_preview_mouse_key: Optional[str] = None
        self._hover_preview_last_at: float = 0.0
        self._hovered_slot: Optional[tuple[int, int]] = None
        self._quick_popup: Optional[Any] = None
        self._suppress_slot_click_until: float = 0.0
        self._soundboard_mousewheel_active: bool = False
        self._volume_indicator_after_id: Optional[str] = None  # Temporary volume display timeout

        # Debounced save handle (see _save_config / _save_config_now)
        self._save_after_id: Optional[str] = None

        # AFK Mode: auto-replay a chosen slot every N seconds.
        self._afk_enabled: bool = False
        self._afk_tab_idx: int = 0
        self._afk_slot_idx: int = 0
        self._afk_interval_seconds: int = 60
        self._afk_after_id: Optional[str] = None
        self._afk_countdown_after_id: Optional[str] = None
        self._afk_next_play_time: float = 0.0
        # (tab_idx, slot_idx) -> "Tab · Slot" label, for the picker.
        self._afk_slot_options: Dict[str, tuple] = {}
        # Popup widget refs (created on demand by _show_afk_popup).
        self._afk_popup: Optional[Any] = None
        self._afk_popup_toggle_btn: Optional[Any] = None
        self._afk_popup_status_label: Optional[Any] = None

        # Pre-create cached fonts for performance
        self._font_sm = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        self._font_sm_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_sm"], weight="bold"
        )
        self._font_slot = ctk.CTkFont(
            family=FONTS["family_text"], size=FONTS["size_lg"], weight="bold"
        )
        self._font_xs = ctk.CTkFont(size=FONTS["size_xs"])
        self._font_xl_bold = ctk.CTkFont(
            family=FONTS["family"], size=FONTS["size_xl"], weight="bold"
        )
        # Tab labels keep the same font size as before; the sidebar itself
        # is wider so titles fit without ellipsization on most names.
        self._font_tab = self._font_sm
        self._font_tab_bold = self._font_sm_bold
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

        # ---- Sound Scheduler ----
        # Ordered list of {tab_idx, slot_idx} entries. Played in sequence
        # by `_scheduler_play_next` which uses root.after to wait for each
        # sound's duration (+ delay_between_sounds) before triggering the
        # next one. Not persisted to config — it's a session-only queue.
        self._scheduler_queue: List[Dict[str, Any]] = []
        self._scheduler_running: bool = False
        self._scheduler_after_id: Optional[str] = None
        self._scheduler_dialog: Optional[ctk.CTkToplevel] = None
        self._scheduler_list_frame: Optional[Any] = None
        self._scheduler_status_var: Optional[tk.StringVar] = None
        self._scheduler_play_btn: Optional[Any] = None
        self._scheduler_delay_var: Optional[tk.DoubleVar] = None
        # Drag-reorder state for the scheduler dialog
        self._scheduler_row_widgets: List[Any] = []
        self._scheduler_drag_index: Optional[int] = None
        self._scheduler_drag_target: Optional[int] = None

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
            on_play_staged=self._play_staged_slot,
            on_edit_staged=self._edit_staged_slot,
            on_get_slot=self._get_slot_for_panel,
            on_slot_changed=self._on_staged_slot_changed,
        )

        # Hook drag-and-drop for image/sound files from file explorer
        if WINDND_AVAILABLE:
            # When the app runs as Administrator (we do, for global hotkeys),
            # Windows UIPI silently blocks WM_DROPFILES from lower-integrity
            # processes like Explorer. We must allow those messages explicitly
            # via ChangeWindowMessageFilterEx, otherwise drops do nothing.
            try:
                import ctypes
                from ctypes import wintypes

                # Resolve the real top-level HWND (winfo_id can return a child)
                try:
                    hwnd = int(self.root.frame(), 16)
                except Exception:
                    hwnd = self.root.winfo_id()

                user32 = ctypes.windll.user32
                MSGFLT_ALLOW = 1
                for msg in (0x0233, 0x004A, 0x0049):  # DROPFILES, COPYDATA, COPYGLOBALDATA
                    try:
                        user32.ChangeWindowMessageFilterEx(
                            wintypes.HWND(hwnd),
                            wintypes.UINT(msg),
                            wintypes.DWORD(MSGFLT_ALLOW),
                            None,
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            windnd.hook_dropfiles(self.root, func=self._on_files_dropped)

        # Ctrl+V on the main window: paste a clipboard image onto the slot
        # under the mouse cursor. Bound at the root level so it works no
        # matter which non-entry widget has focus.
        self.root.bind_all("<Control-v>", self._on_paste_image_to_slot, add="+")
        self.root.bind_all("<Control-V>", self._on_paste_image_to_slot, add="+")
        self.root.bind_all("<MouseWheel>", self._on_global_mousewheel, add="+")

    def _get_scroll_speed_multiplier(self) -> int:
        """Return the current app-wide wheel speed multiplier."""
        try:
            value = int(self.scroll_speed_var.get())
        except Exception:
            value = 10
        return max(1, min(30, value))

    def _get_scroll_units_per_notch(self) -> int:
        """Translate the user-facing scroll speed into stronger Tk scroll units."""
        return self._get_scroll_speed_multiplier() * 5

    def _update_scroll_speed_label(self, _value=None, save: bool = True):
        """Refresh the scroll speed label and persist the setting."""
        value = self._get_scroll_speed_multiplier()
        if self._scroll_speed_label is not None:
            try:
                self._scroll_speed_label.configure(text=f"{value}x")
            except Exception:
                pass
        if save:
            self._save_config()

    @staticmethod
    def _mousewheel_direction_and_notches(event) -> tuple[int, int]:
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return 0, 0
        return (-1 if delta > 0 else 1), max(1, abs(delta) // 120)

    def _find_scroll_canvas_for_widget(self, widget):
        """Find the nearest canvas-backed scroll target for a widget."""
        seen = set()
        cur = widget
        while cur is not None and cur not in seen:
            seen.add(cur)
            try:
                if cur is self.tabs_canvas:
                    return cur
                if hasattr(self, "scrollable_grid") and cur is getattr(
                    self.scrollable_grid,
                    "_parent_canvas",
                    None,
                ):
                    return cur
                parent_canvas = getattr(cur, "_parent_canvas", None)
                if parent_canvas is not None and hasattr(parent_canvas, "yview_scroll"):
                    return parent_canvas
                if isinstance(cur, tk.Canvas) and cur.__class__.__name__ != "SlotWidget":
                    try:
                        if cur.cget("scrollregion"):
                            return cur
                    except Exception:
                        pass
                cur = cur.master
            except Exception:
                break
        return None

    def _on_global_mousewheel(self, event):
        """Apply the app-wide scroll speed to scrollable panels not handled elsewhere.

        Specifically handles audio options panel. The soundboard has its own
        faster handler (_on_soundboard_mousewheel).
        """
        if self._is_quick_popup_open():
            return None

        # Check if we're over the audio options frame (if it's expanded)
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            if hasattr(self, 'audio_options_frame') and self.audio_options_frame.winfo_ismapped():
                ax = self.audio_options_frame.winfo_rootx()
                ay = self.audio_options_frame.winfo_rooty()
                aw = self.audio_options_frame.winfo_width()
                ah = self.audio_options_frame.winfo_height()
                if ax <= x <= ax + aw and ay <= y <= ay + ah:
                    # We're over the audio options - find its scrollable canvas
                    widget = self.root.winfo_containing(x, y)
                    canvas = self._find_scroll_canvas_for_widget(widget)
                    if canvas is not None:
                        direction, notches = self._mousewheel_direction_and_notches(event)
                        if direction != 0:
                            try:
                                canvas.yview_scroll(
                                    direction * notches * self._get_scroll_units_per_notch(),
                                    "units",
                                )
                                return "break"
                            except Exception:
                                pass
                    return None
        except Exception:
            pass

        # Default: don't scroll (soundboard has its own handler)
        return None

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

        # Collapsible content frame - fixed height so the soundboard grid
        # below stays visible. Inside it lives a CTkScrollableFrame so the
        # user can scroll through all the option cards.
        self.audio_options_frame = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_dark"],
            corner_radius=UI["corner_radius"],
            height=380,
        )
        self.audio_options_frame.pack_propagate(False)
        # Hidden by default

        # Scrollable inner container - holds all the option cards
        device_frame = ctk.CTkScrollableFrame(
            self.audio_options_frame,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_light"],
            scrollbar_button_hover_color=COLORS["bg_lighter"],
        )
        device_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

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
        self.input_combo.pack(anchor="w", pady=(4, 0), fill=tk.X)

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
        self.output_combo.pack(anchor="w", pady=(4, 0), fill=tk.X)

        # Live-update the status bar whenever the user changes a device.
        self.input_var.trace_add("write", lambda *_: self._update_status_bar())
        self.output_var.trace_add("write", lambda *_: self._update_status_bar())

        # ==============================================================
        # Card helper - tiny inline factory so every section has the same
        # look (rounded card, header with emoji + bold title, optional
        # subtitle). Returns the inner body frame to pack rows into.
        # ==============================================================
        def _make_card(parent, title, subtitle="", title_color=None):
            card = ctk.CTkFrame(
                parent,
                fg_color=COLORS["bg_medium"],
                corner_radius=UI["corner_radius"],
            )
            card.pack(fill=tk.X, pady=(10, 0))
            header = ctk.CTkFrame(card, fg_color="transparent")
            header.pack(fill=tk.X, padx=12, pady=(8, 2))
            ctk.CTkLabel(
                header,
                text=title,
                font=ctk.CTkFont(
                    family=FONTS["family"], size=FONTS["size_sm"], weight="bold"
                ),
                text_color=title_color or COLORS["text_primary"],
            ).pack(side=tk.LEFT)
            if subtitle:
                ctk.CTkLabel(
                    header,
                    text=subtitle,
                    font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
                    text_color=COLORS["text_muted"],
                ).pack(side=tk.LEFT, padx=(8, 0))
            body = ctk.CTkFrame(card, fg_color="transparent")
            body.pack(fill=tk.X, padx=12, pady=(2, 10))
            return body

        # ============================================================
        # CARD: Stream control - the big start/stop button + quick
        # mic/monitor toggles. The most-used controls live up top.
        # ============================================================
        stream_card = _make_card(
            device_frame,
            "🎚  Stream",
            "(start the audio stream to Discord)",
        )
        stream_row = ctk.CTkFrame(stream_card, fg_color="transparent")
        stream_row.pack(fill=tk.X)

        self.toggle_btn = ctk.CTkButton(
            stream_row,
            text="▶ Start Stream",
            command=self._toggle_stream,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=36,
            width=140,
        )
        self.toggle_btn.pack(side=tk.LEFT, padx=(0, 16))

        self.mic_mute_var = tk.BooleanVar(value=False)
        self.mic_mute_checkbox = ctk.CTkCheckBox(
            stream_row,
            text="Mute Mic",
            variable=self.mic_mute_var,
            command=self._toggle_mic_mute,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.mic_mute_checkbox.pack(side=tk.LEFT, padx=(0, 16))

        self.monitor_var = tk.BooleanVar(value=True)
        self.monitor_checkbox = ctk.CTkCheckBox(
            stream_row,
            text="🔊 Monitor (hear sounds locally)",
            variable=self.monitor_var,
            command=self._toggle_monitor,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.monitor_checkbox.pack(side=tk.LEFT)

        # ============================================================
        # CARD: Test Output - hear/record what Discord actually receives
        # (placed near the top because it's the most useful diagnostic)
        # ============================================================
        test_body = _make_card(
            device_frame,
            "🎧  Test Output (Mic Test)",
            "(plays / records the EXACT signal Discord receives)",
            title_color=COLORS["blurple"],
        )

        # Row 1: Live test + PTT-hold toggles
        test_row1 = ctk.CTkFrame(test_body, fg_color="transparent")
        test_row1.pack(fill=tk.X, pady=(0, 6))

        self.test_live_var = tk.BooleanVar(value=False)
        self.test_live_checkbox = ctk.CTkCheckBox(
            test_row1,
            text="🎧 Live Test (hear what Discord hears)",
            variable=self.test_live_var,
            command=self._toggle_test_live,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.test_live_checkbox.pack(side=tk.LEFT, padx=(0, 16))

        self.test_ptt_var = tk.BooleanVar(value=True)
        self.test_ptt_checkbox = ctk.CTkCheckBox(
            test_row1,
            text="🎙 Hold PTT during test",
            variable=self.test_ptt_var,
            command=self._toggle_test_ptt,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.test_ptt_checkbox.pack(side=tk.LEFT)

        # Row 2: Record-and-play with duration
        test_row2 = ctk.CTkFrame(test_body, fg_color="transparent")
        test_row2.pack(fill=tk.X)

        ctk.CTkLabel(
            test_row2,
            text="Duration:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.test_duration_var = tk.StringVar(value="5s")
        self.test_duration_menu = ctk.CTkOptionMenu(
            test_row2,
            values=["3s", "5s", "10s", "15s", "30s"],
            variable=self.test_duration_var,
            width=70,
            height=28,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            fg_color=COLORS["bg_dark"],
            button_color=COLORS["bg_dark"],
            button_hover_color=COLORS["bg_light"],
        )
        self.test_duration_menu.pack(side=tk.LEFT, padx=(0, 10))

        self.test_record_btn = ctk.CTkButton(
            test_row2,
            text="⏺ Record & Play",
            command=self._toggle_test_record,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=140,
        )
        self.test_record_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.test_status_label = ctk.CTkLabel(
            test_row2,
            text="",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_muted"],
        )
        self.test_status_label.pack(side=tk.LEFT)

        # Test playback state (used by _toggle_test_record)
        self._test_playback_stream: Optional[sd.OutputStream] = None
        self._test_record_pending: bool = False

        # ============================================================
        # CARD: Volume - mic + master sliders side by side
        # ============================================================
        vol_body = _make_card(device_frame, "🎚  Volume")

        # Mic volume row
        mic_row = ctk.CTkFrame(vol_body, fg_color="transparent")
        mic_row.pack(fill=tk.X, pady=(2, 4))

        ctk.CTkLabel(
            mic_row,
            text="🎤 Mic",
            width=70,
            anchor="w",
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
            height=16,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.mic_volume_slider.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)

        self.mic_volume_label = ctk.CTkLabel(
            mic_row,
            text="100%",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=40,
        )
        self.mic_volume_label.pack(side=tk.LEFT)

        # Master (sounds) volume row — affects every playing sound,
        # independent of the mic.
        master_row = ctk.CTkFrame(vol_body, fg_color="transparent")
        master_row.pack(fill=tk.X, pady=(2, 0))

        ctk.CTkLabel(
            master_row,
            text="🎵 Sounds",
            width=70,
            anchor="w",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.master_volume_var = tk.DoubleVar(value=100)
        self.master_volume_slider = ctk.CTkSlider(
            master_row,
            from_=0,
            to=150,
            variable=self.master_volume_var,
            command=self._update_master_volume,
            height=16,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["green"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["green"],
        )
        self.master_volume_slider.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)

        self.master_volume_label = ctk.CTkLabel(
            master_row,
            text="100%",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=40,
        )
        self.master_volume_label.pack(side=tk.LEFT)

        # ============================================================
        # CARD: Push-to-Talk - enable + key picker (key picker collapsed
        # by default; revealed by the checkbox via _toggle_ptt_visibility)
        # ============================================================
        ptt_body = _make_card(
            device_frame,
            "⌨  Push-to-Talk",
            "(auto-presses Discord's PTT key while sounds play)",
        )

        ptt_top = ctk.CTkFrame(ptt_body, fg_color="transparent")
        ptt_top.pack(fill=tk.X)

        self.ptt_enabled_var = tk.BooleanVar(value=False)
        self.ptt_checkbox = ctk.CTkCheckBox(
            ptt_top,
            text="Enable Push-to-Talk",
            variable=self.ptt_enabled_var,
            command=self._toggle_ptt_visibility,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.ptt_checkbox.pack(side=tk.LEFT)

        # PTT key configuration row (hidden until checkbox enabled)
        self.ptt_frame = ctk.CTkFrame(ptt_body, fg_color="transparent")
        # Hidden initially - shown via _toggle_ptt_visibility

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
            width=120,
            height=28,
            fg_color=COLORS["bg_dark"],
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
            width=120,
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

        # ============================================================
        # CARD: Hover Preview - press one binding while hovering a slot
        # to preview it locally without sending audio to Discord.
        # ============================================================
        hover_body = _make_card(
            device_frame,
            "Hover Preview",
            "(press the binding while hovering a sound to play it locally)",
        )

        hover_row = ctk.CTkFrame(hover_body, fg_color="transparent")
        hover_row.pack(fill=tk.X)

        ctk.CTkLabel(
            hover_row,
            text="Binding:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.hover_preview_entry = ctk.CTkEntry(
            hover_row,
            textvariable=self.hover_preview_key_var,
            width=120,
            height=28,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["border"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
        )
        self.hover_preview_entry.pack(side=tk.LEFT, padx=(0, 8))
        self.hover_preview_entry.bind("<FocusOut>", lambda _e: self._apply_hover_preview_key())
        self.hover_preview_entry.bind("<Return>", lambda _e: self._apply_hover_preview_key())

        self.hover_preview_record_btn = ctk.CTkButton(
            hover_row,
            text="Record Key",
            command=self._record_hover_preview_key,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=110,
        )
        self.hover_preview_record_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.hover_preview_clear_btn = ctk.CTkButton(
            hover_row,
            text="Clear",
            command=self._clear_hover_preview_key,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=60,
        )
        self.hover_preview_clear_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.hover_preview_status_label = ctk.CTkLabel(
            hover_row,
            text="",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        )
        self.hover_preview_status_label.pack(side=tk.LEFT, padx=10)

        # ============================================================
        # CARD: Mic processing - noise suppression
        # (replaces Discord's Krisp, which is bypassed by virtual cable)
        # ============================================================
        ns_body = _make_card(
            device_frame,
            "🛡  Mic Processing",
            "(Krisp replacement - bypassed when using a virtual cable)",
        )

        ns_row = ctk.CTkFrame(ns_body, fg_color="transparent")
        ns_row.pack(fill=tk.X)

        self.noise_suppress_var = tk.BooleanVar(value=False)
        self.noise_suppress_checkbox = ctk.CTkCheckBox(
            ns_row,
            text="Noise Suppression",
            variable=self.noise_suppress_var,
            command=self._toggle_noise_suppression,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.noise_suppress_checkbox.pack(side=tk.LEFT, padx=(0, 12))

        ctk.CTkLabel(
            ns_row,
            text="Strength:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 6))

        self.ns_strength_var = tk.DoubleVar(value=85)
        self.ns_strength_slider = ctk.CTkSlider(
            ns_row,
            from_=0,
            to=100,
            variable=self.ns_strength_var,
            command=self._update_ns_strength,
            height=14,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.ns_strength_slider.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ============================================================
        # CARD: Call Recording - persistent settings (start button is in
        # the action bar)
        # ============================================================
        rec_body = _make_card(
            device_frame,
            "🔴  Call Recording",
            "(use the ● Rec button in the action bar to start)",
            title_color=COLORS["red"],
        )

        rec_path_row = ctk.CTkFrame(rec_body, fg_color="transparent")
        rec_path_row.pack(fill=tk.X)

        self.recording_include_mic_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            rec_path_row,
            text="Include mic",
            variable=self.recording_include_mic_var,
            command=self._save_config,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
            checkbox_height=18,
            checkbox_width=18,
        ).pack(side=tk.LEFT, padx=(0, 12))

        ctk.CTkLabel(
            rec_path_row,
            text="Save to:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 6))

        default_rec_dir = os.path.join(os.path.expanduser("~"), "Documents", "DiscordRecordings")
        self.recording_dir_var = tk.StringVar(value=default_rec_dir)
        self.recording_dir_entry = ctk.CTkEntry(
            rec_path_row,
            textvariable=self.recording_dir_var,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            height=28,
        )
        self.recording_dir_entry.pack(side=tk.LEFT, padx=(0, 6), fill=tk.X, expand=True)
        self.recording_dir_entry.bind("<FocusOut>", lambda e: self._save_config())

        ctk.CTkButton(
            rec_path_row,
            text="Browse…",
            command=self._browse_recording_dir,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=80,
        ).pack(side=tk.LEFT, padx=(0, 6))

        ctk.CTkButton(
            rec_path_row,
            text="Open",
            command=self._open_recording_dir,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=60,
        ).pack(side=tk.LEFT)

        ctk.CTkButton(
            rec_path_row,
            text="Open Sounds",
            command=self._open_sounds_dir,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=UI["button_corner_radius"],
            height=28,
            width=110,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # ============================================================
        # AFK Mode state vars (UI lives in the action-bar popup, not here)
        # ============================================================
        self.afk_slot_var = tk.StringVar(value="(no sound chosen)")
        self.afk_min_var = tk.StringVar(value="1")
        self.afk_sec_var = tk.StringVar(value="0")
        # Build the initial slot list now so the popup has data on first open.
        self._refresh_afk_slot_options()

        # ============================================================
        # CARD: App preferences
        # ============================================================
        app_body = _make_card(device_frame, "⚙  App")
        app_row = ctk.CTkFrame(app_body, fg_color="transparent")
        app_row.pack(fill=tk.X)

        self.auto_start_var = tk.BooleanVar(value=True)
        self.auto_start_checkbox = ctk.CTkCheckBox(
            app_row,
            text="Auto-start stream on launch",
            variable=self.auto_start_var,
            command=self._save_config,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.auto_start_checkbox.pack(side=tk.LEFT, padx=(0, 16))

        self.minimize_to_tray_var = tk.BooleanVar(value=False)
        self.minimize_to_tray_checkbox = ctk.CTkCheckBox(
            app_row,
            text="🔻 Minimize to tray",
            variable=self.minimize_to_tray_var,
            command=self._on_toggle_tray_setting,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.minimize_to_tray_checkbox.pack(side=tk.LEFT)

        # Start with Windows — writes a per-user registry entry under
        # HKCU\...\Run pointing at launch.bat (dev) or the EXE (deployed).
        self.start_with_windows_var = tk.BooleanVar(value=_windows_startup_is_enabled())
        self.start_with_windows_checkbox = ctk.CTkCheckBox(
            app_row,
            text="🪟 Start with Windows",
            variable=self.start_with_windows_var,
            command=self._on_toggle_start_with_windows,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=4,
        )
        self.start_with_windows_checkbox.pack(side=tk.LEFT, padx=(16, 0))

        scroll_row = ctk.CTkFrame(app_body, fg_color="transparent")
        scroll_row.pack(fill=tk.X, pady=(12, 0))

        ctk.CTkLabel(
            scroll_row,
            text="Scroll speed",
            width=90,
            anchor="w",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.scroll_speed_slider = ctk.CTkSlider(
            scroll_row,
            from_=1,
            to=30,
            number_of_steps=29,
            variable=self.scroll_speed_var,
            command=self._update_scroll_speed_label,
            height=16,
            fg_color=COLORS["bg_light"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["text_primary"],
            button_hover_color=COLORS["blurple"],
        )
        self.scroll_speed_slider.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)

        self._scroll_speed_label = ctk.CTkLabel(
            scroll_row,
            text=f"{self._get_scroll_speed_multiplier()}x",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            width=36,
        )
        self._scroll_speed_label.pack(side=tk.LEFT)

    def _browse_recording_dir(self):
        """Pick a folder for saved recordings."""
        current = self.recording_dir_var.get() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="Choose folder for recordings",
            initialdir=current if os.path.isdir(current) else os.path.expanduser("~"),
        )
        if chosen:
            self.recording_dir_var.set(chosen)
            self._save_config()

    def _open_recording_dir(self):
        """Open the recordings folder in Explorer."""
        path = self.recording_dir_var.get()
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("Open Folder", f"Could not open folder:\n{e}")

    def _open_sounds_dir(self):
        """Open the local sounds folder in Explorer."""
        try:
            os.makedirs(SOUNDS_DIR, exist_ok=True)
            os.startfile(str(Path(SOUNDS_DIR).absolute()))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("Open Folder", f"Could not open folder:\n{e}")

    def _is_long_audio_file(self, file_path: str) -> bool:
        """Best-effort check for files that should be decoded off the UI thread."""
        try:
            return os.path.getsize(file_path) >= 4 * 1024 * 1024
        except Exception:
            return False

    def _preload_sound_in_background(self, file_path: str):
        """Warm the sound cache without freezing the UI."""
        if not file_path:
            return

        def _worker():
            try:
                self.sound_cache.preload_sounds([file_path])
            except Exception:
                pass

        threading.Thread(target=_worker, name="SoundPreload", daemon=True).start()

    def _toggle_recording(self):
        """Start or stop call recording."""
        # If recording, stop it
        if self.recorder is not None and self.recorder.recording:
            try:
                recorder = self.recorder

                def _on_saved(final_path: Optional[str]):
                    def _ui_update():
                        if final_path:
                            self.status_var.set(
                                f"Saved recording: {os.path.basename(final_path)}"
                            )
                            self.root.after(4000, self._update_status_bar)
                        else:
                            self.status_var.set("Recording stopped, but saving failed.")
                            self.root.after(4000, self._update_status_bar)

                    try:
                        self.root.after(0, _ui_update)
                    except Exception:
                        pass

                saved_path = recorder.stop(on_saved=_on_saved)
            except Exception as e:
                messagebox.showerror("Recording", f"Error stopping recording:\n{e}")
                saved_path = None
            finally:
                self.recorder = None
                if self._recording_after_id is not None:
                    try:
                        self.root.after_cancel(self._recording_after_id)
                    except Exception:
                        pass
                    self._recording_after_id = None
                self.record_btn.configure(
                    text="● Rec",
                    fg_color=COLORS["red"],
                    hover_color=COLORS["red_hover"],
                )
                self.recording_timer_label.configure(text="0:00", text_color=COLORS["text_muted"])
            if saved_path:
                self.status_var.set(f"Saving recording: {os.path.basename(saved_path)}")
            else:
                self._update_status_bar()
            return

        # Start a new recording
        if self.quick_sound_recorder is not None and self.quick_sound_recorder.recording:
            messagebox.showwarning(
                "Recording",
                "Quick Sound is already recording. Stop it before starting a normal recording.",
            )
            return

        out_dir = self.recording_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("Recording", "Please choose a folder to save recordings.")
            return
        include_mic = bool(self.recording_include_mic_var.get())

        try:
            self.recorder = Recorder()
            self.recorder.start(
                output_dir=out_dir,
                include_mic=include_mic,
                mixer=self.mixer if include_mic else None,
            )
        except Exception as e:
            self.recorder = None
            messagebox.showerror(
                "Recording",
                f"Could not start recording:\n{e}\n\n"
                "Make sure audio is playing through your default Windows playback device.",
            )
            return

        self.record_btn.configure(
            text="■ Stop",
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
        )
        self.recording_timer_label.configure(text="● 0:00", text_color=COLORS["red"])
        self._update_recording_timer()
        self._update_status_bar()
        self._save_config()

    def _toggle_quick_record_to_sound(self):
        """Record once, then add the saved recording as a new sound."""
        if self.quick_sound_recorder is not None and self.quick_sound_recorder.recording:
            recorder = self.quick_sound_recorder

            def _on_saved(final_path: Optional[str]):
                def _finish():
                    if final_path and os.path.exists(final_path):
                        self.status_var.set(f"Adding quick sound: {os.path.basename(final_path)}")
                        self._create_slot_from_audio_file(
                            final_path,
                            Path(final_path).stem,
                            self.current_tab_idx,
                            remove_source=False,
                            open_config=True,
                        )
                        self.root.after(4000, self._update_status_bar)
                    else:
                        messagebox.showerror(
                            "Quick Sound",
                            "The recording stopped, but the audio file was not saved.",
                        )
                        self._update_status_bar()

                try:
                    self.root.after(0, _finish)
                except Exception:
                    pass

            try:
                planned_path = recorder.stop(on_saved=_on_saved)
            except Exception as e:
                messagebox.showerror("Quick Sound", f"Error stopping recording:\n{e}")
                planned_path = None
            finally:
                self.quick_sound_recorder = None
                self.quick_record_btn.configure(
                    text="Rec Sound",
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                )

            if planned_path:
                self.status_var.set(f"Saving quick sound: {os.path.basename(planned_path)}")
            return

        if self.recorder is not None and self.recorder.recording:
            messagebox.showwarning(
                "Quick Sound",
                "A normal recording is already running. Stop it before using Quick Sound.",
            )
            return

        out_dir = self.recording_dir_var.get().strip()
        if not out_dir:
            messagebox.showwarning("Quick Sound", "Please choose a folder to save recordings.")
            return
        include_mic = bool(self.recording_include_mic_var.get())

        try:
            self.quick_sound_recorder = Recorder()
            self.quick_sound_recorder.start(
                output_dir=out_dir,
                include_mic=include_mic,
                mixer=self.mixer if include_mic else None,
            )
        except Exception as e:
            self.quick_sound_recorder = None
            messagebox.showerror(
                "Quick Sound",
                f"Could not start recording:\n{e}\n\n"
                "Make sure audio is playing through your default Windows playback device.",
            )
            return

        self.quick_record_btn.configure(
            text="Stop Sound",
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
        )
        self.status_var.set("Quick Sound recording...")
        self._save_config()

    def _update_recording_timer(self):
        """Tick the elapsed-time label in the action bar while recording."""
        if self.recorder is None or not self.recorder.recording:
            self._recording_after_id = None
            return
        elapsed = int(self.recorder.get_elapsed())
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        if h > 0:
            txt = f"● {h}:{m:02d}:{s:02d}"
        else:
            txt = f"● {m}:{s:02d}"
        try:
            self.recording_timer_label.configure(text=txt, text_color=COLORS["red"])
            # Refresh status-bar recording portion (cheap; deduped via StringVar)
            self._update_status_bar()
        except Exception:
            return
        self._recording_after_id = self.root.after(500, self._update_recording_timer)

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
        _Tooltip.attach(self.add_tab_btn, "Create a new tab")

        self.manage_tabs_btn = ctk.CTkButton(
            header_frame,
            text="...",
            command=self._show_tab_manager,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_sm_bold,
            corner_radius=6,
            height=26,
            width=30,
        )
        self.manage_tabs_btn.pack(side=tk.RIGHT, padx=(6, 0))
        _Tooltip.attach(self.manage_tabs_btn, "Manage tab order")

        # Scrollable area for tab buttons
        self.tabs_canvas = tk.Canvas(
            self.tabs_sidebar,
            bg=COLORS["bg_dark"],
            highlightthickness=0,
            width=180,
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

        # Web → MP3 download button (YouTube, Vimeo, Twitter, TikTok, ~1000 sites)
        self.youtube_btn = ctk.CTkButton(
            left_section,
            text="⬇ Web Audio",
            command=self._show_youtube_download_dialog,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=90,
        )
        self.youtube_btn.pack(side=tk.LEFT, padx=(6, 0), pady=8)

        self.open_sounds_btn = ctk.CTkButton(
            left_section,
            text="Sounds",
            command=self._open_sounds_dir,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=72,
        )
        self.open_sounds_btn.pack(side=tk.LEFT, padx=(6, 0), pady=8)

        # ---- Call Recorder cluster (button + live timer) ----
        # Visually grouped so the timer reads as belonging to the record btn.
        rec_cluster = ctk.CTkFrame(
            left_section,
            fg_color=COLORS["bg_dark"],
            corner_radius=6,
        )
        rec_cluster.pack(side=tk.LEFT, padx=(8, 0), pady=8)

        self.record_btn = ctk.CTkButton(
            rec_cluster,
            text="● Rec",
            command=self._toggle_recording,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold"),
            corner_radius=6,
            height=28,
            width=66,
        )
        self.record_btn.pack(side=tk.LEFT, padx=(3, 4), pady=2)

        self.recording_timer_label = ctk.CTkLabel(
            rec_cluster,
            text="0:00",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_muted"],
            width=58,
        )
        self.recording_timer_label.pack(side=tk.LEFT, padx=(0, 4), pady=2)

        self.quick_record_btn = ctk.CTkButton(
            rec_cluster,
            text="Rec Sound",
            command=self._toggle_quick_record_to_sound,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold"),
            corner_radius=6,
            height=28,
            width=86,
        )
        self.quick_record_btn.pack(side=tk.LEFT, padx=(0, 3), pady=2)

        # Kept as alias so the legacy code path that wrote to
        # `recording_status_label` still works without touching it.
        self.recording_status_label = self.recording_timer_label

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

        # Sound Scheduler / Queue button
        self.scheduler_btn = ctk.CTkButton(
            right_section,
            text="📋 Queue",
            command=self._open_scheduler_dialog,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=78,
        )
        self.scheduler_btn.pack(side=tk.LEFT, padx=(0, 6), pady=8)

        # AFK Mode button — opens a popup to pick slot, set interval, start/stop
        self.afk_btn = ctk.CTkButton(
            right_section,
            text="💤 AFK",
            command=self._show_afk_popup,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_xs,
            corner_radius=6,
            height=32,
            width=70,
        )
        self.afk_btn.pack(side=tk.LEFT, padx=(0, 6), pady=8)

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

        # Hover tooltips (3s delay) — small, non-intrusive hints for every
        # action-bar button. Reuses the singleton _Tooltip class.
        _Tooltip.attach(self.edit_mode_btn, "Drag-to-rearrange slots in the current tab")
        _Tooltip.attach(self.youtube_btn, "Download audio from a web page (YouTube, Vimeo, Twitter, TikTok, ...) as MP3")
        _Tooltip.attach(self.open_sounds_btn, "Open the local sounds folder")
        _Tooltip.attach(self.record_btn, "Record a Discord call (others + optional mic) to MP3")
        _Tooltip.attach(self.quick_record_btn, "Record once, then add it as a new sound")
        _Tooltip.attach(self.stop_all_btn, "Stop every playing sound, preview and queue")
        _Tooltip.attach(self.scheduler_btn, "Open the Sound Queue / scheduler")
        _Tooltip.attach(self.afk_btn, "AFK Mode — auto-play a sound every X min:sec")
        _Tooltip.attach(self.now_playing_btn, "Show / hide the DJ Looper side panel")

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
        direction, notches = self._mousewheel_direction_and_notches(event)
        if direction == 0:
            return
        self.tabs_canvas.yview_scroll(
            direction * notches * self._get_scroll_units_per_notch(),
            "units",
        )
        return "break"

    def _scroll_tabs(self, direction: int):
        """Scroll tabs up (-1) or down (1)."""
        self.tabs_canvas.yview_scroll(direction * max(8, self._get_scroll_units_per_notch()), "units")

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
        """        # Lazy import — avoids touching PIL on platforms without color emoji.
        from . import emoji_render as _er
        from PIL import Image as _PILImage

        # Build a single transparent 22×22 placeholder so tabs without an
        # emoji still reserve the same horizontal slot — keeps all tab
        # titles vertically aligned in a column instead of jumping left
        # whenever an emoji is missing.
        if not hasattr(self, "_tab_emoji_placeholder"):
            try:
                _blank_pil = _PILImage.new("RGBA", (22, 22), (0, 0, 0, 0))
                self._tab_emoji_placeholder = ctk.CTkImage(
                    light_image=_blank_pil, dark_image=_blank_pil, size=(22, 22)
                )
            except Exception:
                self._tab_emoji_placeholder = None

        def _tab_emoji_image(em: Optional[str]):
            """Return a CTkImage for the tab's emoji, or None if no emoji."""
            if not em:
                return None
            try:
                pil = _er.get_pil_image(em, 22)
                if pil is None:
                    return None
                return ctk.CTkImage(light_image=pil, dark_image=pil, size=(22, 22))
            except Exception:
                return None

        # ------------------------------------------------------------------
        # Each tab is a CTkFrame with a real 2-column grid:
        #   column 0  → name label (sticky="ew") — gets ellipsized
        #   column 1  → emoji label (fixed 26px) — always reserves space
        # This is the only reliable way to get the emojis to line up in
        # a single column AND have predictable text truncation, since
        # CTkButton with compound="right" + transparent placeholder image
        # doesn't reserve consistent horizontal space.
        # ------------------------------------------------------------------

        EMOJI_COL_W = 28  # px — width reserved for the emoji column

        def _measure_text_width(text: str, font) -> int:
            """Best-effort pixel width of `text` rendered in `font`."""
            if not text:
                return 0
            try:
                # CTkFont subclasses tkfont.Font, so .measure() works.
                return int(font.measure(text))
            except Exception:
                # Conservative fallback so we OVER-estimate (better to
                # ellipsize too aggressively than not at all).
                return len(text) * 10

        def _ellipsize_to_width(full: str, font, max_px: int) -> str:
            """Trim `full` with a trailing ellipsis so it fits in `max_px`."""
            if max_px <= 0 or not full:
                return full
            if _measure_text_width(full, font) <= max_px:
                return full
            ell = "…"
            ell_w = _measure_text_width(ell, font)
            lo, hi, best = 0, len(full), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                w = _measure_text_width(full[:mid], font) + ell_w
                if w <= max_px:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return full[:best].rstrip() + ell if best > 0 else ell

        def _attach_ellipsis(tab_frame, name_lbl):
            """Re-flow the tab name to fit inside its column.

            We bind to the FRAME's <Configure> rather than the label's,
            because Tk only fires <Configure> on the label when its actual
            allocated width changes. With grid sticky="ew" + column weight=1,
            the label's natural request width is its TEXT width, so when the
            text is too long the column squeezes it but no Configure fires
            on the label itself — the text just gets center-clipped.
            The wrapper frame, however, always reports its real width.
            """
            def _avail_px():
                fw = tab_frame.winfo_width()
                if fw <= 1:
                    return 0
                # padx for name = (10, 4) → 14, padx for emoji = (0, 6) → 6
                return max(0, fw - EMOJI_COL_W - 14 - 6 - 4)

            def _font_for(lbl):
                return self._font_tab_bold if getattr(lbl, "_is_active_tab", False) else self._font_tab

            def _reflow(_event=None):
                full = getattr(name_lbl, "_full_tab_name", "") or ""
                if not full:
                    return
                avail = _avail_px()
                if avail == 0:
                    name_lbl.after(30, _reflow)
                    return
                fnt = _font_for(name_lbl)
                new_text = _ellipsize_to_width(full, fnt, avail)
                name_lbl._truncated_text = new_text  # type: ignore[attr-defined]
                name_lbl._is_truncated = (new_text != full)  # type: ignore[attr-defined]
                # Don't fight the marquee — it owns the text while hovering.
                if getattr(name_lbl, "_marquee_after", None) is None:
                    if name_lbl.cget("text") != new_text:
                        try:
                            name_lbl.configure(text=new_text)
                        except Exception:
                            pass

            tab_frame.bind("<Configure>", _reflow, add="+")
            name_lbl.after(30, _reflow)
            name_lbl._reflow = _reflow  # type: ignore[attr-defined]

            # ---- Hover-marquee: scroll long titles so the user can read them.
            def _start_marquee(_event=None):
                if not getattr(name_lbl, "_is_truncated", False):
                    return
                full = getattr(name_lbl, "_full_tab_name", "") or ""
                if not full:
                    return
                # Cancel any in-flight marquee.
                _stop_marquee()
                fnt = _font_for(name_lbl)
                avail = _avail_px()
                if avail <= 0:
                    return
                # Use a scrolling string with a clear separator between repeats.
                pad = "   •   "
                scroll_src = full + pad + full
                state = {"offset": 0, "src": scroll_src, "full_len": len(full) + len(pad)}

                def _tick():
                    # Show a substring window starting at offset, ellipsized to fit.
                    s = state["src"][state["offset"]:]
                    visible = _ellipsize_to_width(s, fnt, avail)
                    try:
                        name_lbl.configure(text=visible)
                    except Exception:
                        return
                    state["offset"] = (state["offset"] + 1) % state["full_len"]
                    name_lbl._marquee_after = name_lbl.after(180, _tick)  # type: ignore[attr-defined]

                # Brief pause before scrolling starts (gives a glance at the original).
                name_lbl._marquee_after = name_lbl.after(450, _tick)  # type: ignore[attr-defined]

            def _stop_marquee(_event=None):
                aid = getattr(name_lbl, "_marquee_after", None)
                if aid is not None:
                    try:
                        name_lbl.after_cancel(aid)
                    except Exception:
                        pass
                    name_lbl._marquee_after = None  # type: ignore[attr-defined]
                # Restore truncated text.
                trunc = getattr(name_lbl, "_truncated_text", None)
                if trunc is not None:
                    try:
                        name_lbl.configure(text=trunc)
                    except Exception:
                        pass

            # Bind on the frame AND the label so hover works whether the
            # cursor enters the padding area or the label itself.
            for w in (tab_frame, name_lbl):
                w.bind("<Enter>", _start_marquee, add="+")
                w.bind("<Leave>", _stop_marquee, add="+")

        def _apply_tab_visual(tab_frame, name_lbl, emoji_lbl, tab, is_active: bool):
            bg = COLORS["blurple"] if is_active else COLORS["bg_medium"]
            try:
                tab_frame.configure(fg_color=bg)
            except Exception:
                pass
            full = tab.name or ""
            name_lbl._full_tab_name = full  # type: ignore[attr-defined]
            name_lbl._is_active_tab = is_active  # type: ignore[attr-defined]
            try:
                name_lbl.configure(
                    font=self._font_tab_bold if is_active else self._font_tab,
                    fg_color=bg,
                    text_color=COLORS["text_primary"],
                )
            except Exception:
                pass
            # Re-flow against current width via the cached helper.
            reflow = getattr(name_lbl, "_reflow", None)
            if callable(reflow):
                try:
                    reflow()
                except Exception:
                    try:
                        name_lbl.configure(text=full)
                    except Exception:
                        pass
            emoji_img = _tab_emoji_image(tab.emoji)
            try:
                emoji_lbl.configure(image=emoji_img, fg_color=bg)
                emoji_lbl._emoji_image_ref = emoji_img  # type: ignore[attr-defined]
            except Exception:
                pass

        def _bind_tab_click(widget, idx: int):
            widget.bind("<Button-1>", lambda _e, i=idx: self._switch_tab(i))
            widget.bind("<Button-3>", lambda _e, i=idx: self._configure_tab(i))
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

        # Check if we need to recreate buttons (tab count changed)
        if len(self.tab_buttons) != len(self.tabs):
            for w in self.tab_buttons:
                try:
                    w.destroy()
                except Exception:
                    pass
            self.tab_buttons.clear()

            for idx, tab in enumerate(self.tabs):
                is_active = idx == self.current_tab_idx
                bg = COLORS["blurple"] if is_active else COLORS["bg_medium"]

                tab_frame = ctk.CTkFrame(
                    self.tabs_container,
                    fg_color=bg,
                    corner_radius=6,
                    height=32,
                )
                tab_frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 3))
                tab_frame.pack_propagate(False)
                tab_frame.grid_columnconfigure(0, weight=1, minsize=10)
                tab_frame.grid_columnconfigure(1, weight=0, minsize=EMOJI_COL_W)

                name_lbl = ctk.CTkLabel(
                    tab_frame,
                    text=tab.name or "",
                    font=self._font_tab_bold if is_active else self._font_tab,
                    text_color=COLORS["text_primary"],
                    fg_color=bg,
                    anchor="w",
                    justify="left",
                )
                name_lbl._full_tab_name = tab.name or ""  # type: ignore[attr-defined]
                name_lbl._is_active_tab = is_active  # type: ignore[attr-defined]
                name_lbl.grid(row=0, column=0, sticky="ew", padx=(10, 4), pady=4)
                _attach_ellipsis(tab_frame, name_lbl)

                emoji_img = _tab_emoji_image(tab.emoji)
                emoji_lbl = ctk.CTkLabel(
                    tab_frame,
                    text="",
                    image=emoji_img,
                    fg_color=bg,
                    width=EMOJI_COL_W,
                )
                emoji_lbl._emoji_image_ref = emoji_img  # type: ignore[attr-defined]
                emoji_lbl.grid(row=0, column=1, sticky="e", padx=(0, 6), pady=2)

                # Bind click + right-click to ALL three widgets so the
                # whole row is hit-testable.
                _bind_tab_click(tab_frame, idx)
                _bind_tab_click(name_lbl, idx)
                _bind_tab_click(emoji_lbl, idx)

                # Stash refs on the frame so the update branch can find them.
                tab_frame._name_lbl = name_lbl  # type: ignore[attr-defined]
                tab_frame._emoji_lbl = emoji_lbl  # type: ignore[attr-defined]

                self.tab_buttons.append(tab_frame)

            self.tabs_canvas.yview_moveto(0)
        else:
            # Same tab count — only reskin tabs whose active state changed.
            for idx, (tab_frame, tab) in enumerate(zip(self.tab_buttons, self.tabs)):
                is_active = idx == self.current_tab_idx
                was_active = getattr(self, "_last_active_tab_idx", -1) == idx
                if not (is_active or was_active):
                    continue
                name_lbl = getattr(tab_frame, "_name_lbl", None)
                emoji_lbl = getattr(tab_frame, "_emoji_lbl", None)
                if name_lbl is None or emoji_lbl is None:
                    continue
                _apply_tab_visual(tab_frame, name_lbl, emoji_lbl, tab, is_active)

            self._last_active_tab_idx = self.current_tab_idx

        # Only recompute scroll region when tab count actually changed
        # (active-state-only updates do not affect the canvas bbox).
        if len(self.tab_buttons) != len(self.tabs) or not getattr(
            self, "_tabs_scroll_initialized", False
        ):
            self._tabs_scroll_initialized = True
            self._schedule_tabs_scroll_update()

    @staticmethod
    def _truncate_tab_label(text: str, max_chars: int = 13) -> str:
        """Trim a tab label so the button text width stays predictable.

        The sidebar canvas is only ~130px wide and the emoji eats ~22px on
        the right. CTk doesn't ellipsize itself — without this, long names
        get center-clipped to garbage like "nemy Territor".
        """
        if not text:
            return ""
        if len(text) > max_chars:
            return text[: max_chars - 1].rstrip() + "…"
        return text

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

                # CRITICAL: every tab whose index just shifted down has slot
                # widgets whose `on_click`/`on_menu`/`on_stop` lambdas baked
                # the OLD tab_idx into their closures. After reindexing the
                # storage dicts, those lambdas now point at the wrong tab —
                # which is what made "Edit" sometimes open an empty dialog
                # for a slot that lives on a totally different tab.
                #
                # The cheapest reliable fix is to drop and rebuild ALL the
                # remaining per-tab widgets. Closures get fresh tab indices,
                # everything lines up again.
                for shifted_idx in [
                    i for i in range(len(self.tabs) + 1)  # include old higher
                    if i != tab_idx and i in self.tab_grid_frames
                ]:
                    self._cleanup_tab_widgets(shifted_idx)

                # Reindex per-tab storage to fill the gap
                self._reindex_tab_storage(tab_idx)

                # Adjust current tab index if needed
                if self.current_tab_idx >= len(self.tabs):
                    self.current_tab_idx = len(self.tabs) - 1

                self._refresh_tab_bar()
                # Rebuild current tab synchronously so the user sees no flicker;
                # other tabs are rebuilt lazily on first switch (or via the
                # background incremental builder below).
                self._ensure_tab_built(self.current_tab_idx)
                self._show_tab_only(self.current_tab_idx)
                self._update_current_tab_aliases()
                # Kick off background rebuild for all the other tabs that we
                # just torn down, so their first switch is instant too.
                remaining = [
                    i for i in range(len(self.tabs))
                    if i != self.current_tab_idx and not self._tab_built.get(i, False)
                ]
                if remaining:
                    self.root.after(50, lambda r=remaining: self._build_tabs_incrementally(r))
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

    def _show_tab_manager(self):
        """Open a modal dialog for reordering tabs."""
        if not self.tabs:
            return

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Manage Tabs")
        dialog.geometry("460x560")
        dialog.minsize(400, 420)
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        header = ctk.CTkFrame(dialog, fg_color=COLORS["bg_medium"], corner_radius=0)
        header.pack(fill=tk.X)
        ctk.CTkLabel(
            header,
            text="Manage Tabs",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_lg"], weight="bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        ).pack(fill=tk.X, padx=16, pady=(12, 2))
        ctk.CTkLabel(
            header,
            text="Change the order shown in the left sidebar.",
            font=self._font_xs,
            text_color=COLORS["text_muted"],
            anchor="w",
        ).pack(fill=tk.X, padx=16, pady=(0, 12))

        list_frame = ctk.CTkScrollableFrame(
            dialog,
            fg_color=COLORS["bg_dark"],
            corner_radius=0,
        )
        list_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        order = list(range(len(self.tabs)))

        def _move(pos: int, delta: int):
            new_pos = pos + delta
            if new_pos < 0 or new_pos >= len(order):
                return
            order[pos], order[new_pos] = order[new_pos], order[pos]
            _render()

        def _to_edge(pos: int, first: bool):
            idx = order.pop(pos)
            if first:
                order.insert(0, idx)
            else:
                order.append(idx)
            _render()

        def _render():
            for child in list_frame.winfo_children():
                child.destroy()

            for pos, tab_idx in enumerate(order):
                tab = self.tabs[tab_idx]
                row = ctk.CTkFrame(list_frame, fg_color=COLORS["bg_medium"], corner_radius=6)
                row.pack(fill=tk.X, pady=4)
                row.grid_columnconfigure(0, weight=1)

                label_text = f"{pos + 1}. "
                if tab.emoji:
                    label_text += f"{tab.emoji} "
                label_text += tab.name or f"Tab {tab_idx + 1}"
                ctk.CTkLabel(
                    row,
                    text=label_text,
                    text_color=COLORS["text_primary"],
                    font=self._font_sm_bold if tab_idx == self.current_tab_idx else self._font_sm,
                    anchor="w",
                ).grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=8)

                ctk.CTkButton(
                    row,
                    text="Top",
                    command=lambda p=pos: _to_edge(p, True),
                    width=46,
                    height=26,
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                    font=self._font_xs,
                    state=(tk.DISABLED if pos == 0 else tk.NORMAL),
                ).grid(row=0, column=1, padx=(0, 4), pady=6)
                ctk.CTkButton(
                    row,
                    text="Up",
                    command=lambda p=pos: _move(p, -1),
                    width=42,
                    height=26,
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                    font=self._font_xs,
                    state=(tk.DISABLED if pos == 0 else tk.NORMAL),
                ).grid(row=0, column=2, padx=(0, 4), pady=6)
                ctk.CTkButton(
                    row,
                    text="Down",
                    command=lambda p=pos: _move(p, 1),
                    width=52,
                    height=26,
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                    font=self._font_xs,
                    state=(tk.DISABLED if pos == len(order) - 1 else tk.NORMAL),
                ).grid(row=0, column=3, padx=(0, 4), pady=6)
                ctk.CTkButton(
                    row,
                    text="End",
                    command=lambda p=pos: _to_edge(p, False),
                    width=46,
                    height=26,
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                    font=self._font_xs,
                    state=(tk.DISABLED if pos == len(order) - 1 else tk.NORMAL),
                ).grid(row=0, column=4, padx=(0, 8), pady=6)

        def _apply():
            self._apply_tab_order(order)
            dialog.destroy()

        footer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_medium"], corner_radius=0)
        footer.pack(fill=tk.X)
        ctk.CTkButton(
            footer,
            text="Apply",
            command=_apply,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=100,
        ).pack(side=tk.RIGHT, padx=(6, 12), pady=12)
        ctk.CTkButton(
            footer,
            text="Close",
            command=dialog.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=100,
        ).pack(side=tk.RIGHT, padx=6, pady=12)

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        _render()

    def _apply_tab_order(self, order: List[int]):
        """Apply a new tab ordering and rebuild tab widgets with fresh indices."""
        if sorted(order) != list(range(len(self.tabs))):
            return
        if order == list(range(len(self.tabs))):
            return

        current_tab = self.tabs[self.current_tab_idx] if self.tabs else None

        if self.playing_slots or self.preview_slots:
            self._stop_all_sounds()

        try:
            if self._search_results is not None:
                self._clear_search()
        except Exception:
            pass

        for tab_idx in list(self.tab_grid_frames.keys()):
            self._cleanup_tab_widgets(tab_idx)

        for tab_button in self.tab_buttons:
            try:
                tab_button.destroy()
            except Exception:
                pass
        self.tab_buttons.clear()
        self._tabs_scroll_initialized = False

        old_tabs = self.tabs
        self.tabs = [old_tabs[i] for i in order]
        if current_tab is not None:
            try:
                self.current_tab_idx = self.tabs.index(current_tab)
            except ValueError:
                self.current_tab_idx = min(self.current_tab_idx, len(self.tabs) - 1)

        self._last_active_tab_idx = -1
        self._refresh_tab_bar()
        self._build_all_tab_widgets()
        self._register_hotkeys()
        self._save_config()
        self.status_var.set("Tab order saved")

    def _show_emoji_picker(self, target_var: tk.StringVar, parent):
        """Show the native Tk emoji picker (no PyQt6, no subprocess)."""
        from .emoji_picker import pick_emoji

        # Run the native Tk picker — modal, blocks until closed.
        try:
            result = pick_emoji(parent if parent is not None else self.root)
        except Exception as e:
            messagebox.showerror("Emoji picker error", str(e))
            return

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
        self._update_status_bar()

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
        # (tab_idx, slot_idx) -> dict with widget refs for the SlotWidget
        # rendered in the search/filter overlay. Used by _animate_progress
        # and _update_slot_button_for_tab to keep the overlay slots in sync
        # with playing state, just like the normal per-tab grid.
        self._search_slot_widgets: Dict[tuple, Dict] = {}

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
        self._install_soundboard_mousewheel_acceleration()

        # Don't create slot widgets here - they're created per-tab lazily
        # After config loads, _build_all_tab_widgets() will create them

    def _install_soundboard_mousewheel_acceleration(self):
        """Add a faster scoped mousewheel handler for the soundboard grid."""
        canvas = getattr(self.scrollable_grid, "_parent_canvas", None)
        targets = [self.scrollable_grid]
        if canvas is not None:
            targets.append(canvas)

        for widget in targets:
            try:
                widget.bind("<Enter>", self._bind_soundboard_mousewheel, add="+")
                widget.bind("<Leave>", self._unbind_soundboard_mousewheel, add="+")
            except Exception:
                pass

        if canvas is not None:
            try:
                canvas.bind_all("<MouseWheel>", self._on_soundboard_mousewheel, add="+")
            except Exception:
                pass

    def _bind_soundboard_mousewheel(self, _event=None):
        self._soundboard_mousewheel_active = True

    def _unbind_soundboard_mousewheel(self, _event=None):
        self._soundboard_mousewheel_active = False

    def _on_soundboard_mousewheel(self, event):
        """Scroll the soundboard faster while the pointer is over it.

        Shift+wheel adjusts the hovered slot's volume instead of scrolling.
        """
        if not getattr(self, "_soundboard_mousewheel_active", False):
            try:
                x = self.root.winfo_pointerx()
                y = self.root.winfo_pointery()
                gx = self.scrollable_grid.winfo_rootx()
                gy = self.scrollable_grid.winfo_rooty()
                gw = self.scrollable_grid.winfo_width()
                gh = self.scrollable_grid.winfo_height()
                if not (gx <= x <= gx + gw and gy <= y <= gy + gh):
                    return None
            except Exception:
                return None
        if self._is_quick_popup_open():
            return "break"

        # Check for Shift key (0x0001 is Shift in Tk event.state)
        is_shift = bool(getattr(event, "state", 0) & 0x0001)

        if is_shift:
            # Shift+wheel: adjust hovered slot volume, don't scroll soundboard
            result = self._adjust_hovered_slot_volume_from_wheel(event)
            return result if result is not None else "break"

        # Normal wheel: scroll soundboard with speed multiplier
        canvas = getattr(self.scrollable_grid, "_parent_canvas", None)
        if canvas is None:
            return None

        direction, notches = self._mousewheel_direction_and_notches(event)
        if direction == 0:
            return None
        try:
            canvas.yview_scroll(
                direction * notches * self._get_scroll_units_per_notch(),
                "units",
            )
            return "break"
        except Exception:
            return None

    def _adjust_hovered_slot_volume_from_wheel(self, event):
        """Shift + wheel over a filled slot changes its per-slot volume.

        Displays a visual volume indicator on the slot with progress bar-like visualization.
        """
        direction, notches = self._mousewheel_direction_and_notches(event)
        if direction == 0:
            return None

        target = self._hovered_slot
        if target is None:
            try:
                target = self._find_slot_at_position(
                    self.root.winfo_pointerx(),
                    self.root.winfo_pointery(),
                )
            except Exception:
                target = None
        if target is None:
            return "break"

        tab_idx, slot_idx = target
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return "break"
        slot = self.tabs[tab_idx].slots.get(slot_idx)
        if slot is None:
            return "break"

        # The requested gesture is intentionally inverted: Shift + scroll up
        # lowers volume, Shift + scroll down raises it.
        change = (-0.05 if direction < 0 else 0.05) * notches
        slot.volume = max(0.0, min(1.5, slot.volume + change))

        if (
            self.mixer
            and slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == tab_idx
        ):
            try:
                self.mixer.set_sound_volume(f"{tab_idx}_{slot_idx}", slot.volume)
            except Exception:
                pass

        # Temporarily show a visual volume indicator on the slot
        self._show_slot_volume_indicator(tab_idx, slot_idx, slot.volume)

        self._update_slot_button_for_tab(tab_idx, slot_idx)
        self._save_config()
        volume_pct = int(round(slot.volume * 100))
        status_msg = f"🔊 {slot.name}: volume {volume_pct}%"
        if volume_pct == 0:
            status_msg = f"🔇 {slot.name}: muted"
        self.status_var.set(status_msg)
        return "break"

    def _show_slot_volume_indicator(self, tab_idx: int, slot_idx: int, volume: float):
        """Temporarily display a volume bar overlay on a slot."""
        try:
            if tab_idx not in self.tab_slot_buttons or slot_idx not in self.tab_slot_buttons[tab_idx]:
                return

            # Get the slot widget (SlotWidget is a tk.Canvas)
            slot_widget = self.tab_slot_buttons[tab_idx][slot_idx]

            # Add a temporary visual indicator by updating the slot's progress-like display
            # This uses the same mechanism as playback progress but shows volume level
            # We'll set a temporary visual property on the slot widget

            # Cancel any pending volume indicator clear
            if hasattr(self, '_volume_indicator_after_id') and self._volume_indicator_after_id:
                try:
                    self.root.after_cancel(self._volume_indicator_after_id)
                except Exception:
                    pass

            # Mark the slot as showing volume (if it's a SlotWidget with custom rendering)
            if hasattr(slot_widget, '_volume_display'):
                slot_widget._volume_display = volume
                # Force a redraw
                if hasattr(slot_widget, '_redraw_full'):
                    slot_widget._redraw_full()

            # Clear the volume indicator after 1 second
            def clear_volume_display():
                try:
                    if hasattr(slot_widget, '_volume_display'):
                        slot_widget._volume_display = None
                    if hasattr(slot_widget, '_redraw_full'):
                        slot_widget._redraw_full()
                except Exception:
                    pass
                self._volume_indicator_after_id = None

            self._volume_indicator_after_id = self.root.after(1000, clear_volume_display)
        except Exception:
            # Silently ignore any issues with volume display
            pass

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
        self.tab_slot_group_labels[tab_idx] = {}
        self.tab_slot_wrappers[tab_idx] = {}
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

            # Wrapper frame holds the slot widget + a small footer label
            # listing the slot's groups. Footer is always present (height
            # reserved) so the grid stays uniform whether or not a slot has
            # groups assigned.
            wrapper = ctk.CTkFrame(tab_grid, fg_color="transparent", corner_radius=0)
            wrapper.grid(
                row=row,
                column=col,
                padx=UI["slot_padding"],
                pady=UI["slot_padding"],
                sticky="nsew",
            )
            wrapper.grid_columnconfigure(0, weight=1)
            wrapper.grid_rowconfigure(0, weight=1)
            wrapper.grid_rowconfigure(1, weight=0)

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
                wrapper,
                on_click=lambda t=tab_idx, idx=i: self._on_slot_command_for_tab(t, idx),
                on_right_click=lambda e, t=tab_idx, idx=i: self._show_quick_popup_for_tab(
                    e, t, idx
                ),
                on_menu=lambda t=tab_idx, idx=i: self._show_slot_menu(t, idx),
                on_stop=lambda t=tab_idx, idx=i: self._stop_slot_with_flag_for_tab(t, idx),
                on_drag_drop=lambda xr, yr, t=tab_idx, idx=i: self._on_slot_drag_drop(
                    t, idx, xr, yr
                ),
                height=UI["slot_height"],
            )
            slot_widget.grid(row=0, column=0, sticky="nsew")
            slot_widget.bind(
                "<Enter>",
                lambda _e, t=tab_idx, idx=i: self._set_hovered_slot(t, idx),
                add="+",
            )
            slot_widget.bind(
                "<Leave>",
                lambda _e, t=tab_idx, idx=i: self._clear_hovered_slot(t, idx),
                add="+",
            )

            # Footer label: shows the groups this slot belongs to. Empty
            # when the slot has none, but the row stays sized so the grid
            # doesn't shift around when slots gain/lose groups.
            # Clicking the label filters the soundboard to that group (or
            # pops a menu when the slot has multiple groups).
            group_lbl = ctk.CTkLabel(
                wrapper,
                text="",
                font=self._font_xs,
                text_color=COLORS["text_muted"],
                anchor="center",
                justify="center",
                height=14,
                cursor="hand2",
            )
            group_lbl.grid(row=1, column=0, sticky="ew", padx=2, pady=(2, 0))
            group_lbl.bind(
                "<Button-1>",
                lambda e, t=tab_idx, idx=i: self._on_group_label_click(e, t, idx),
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
            self.tab_slot_group_labels[tab_idx][i] = group_lbl
            self.tab_slot_wrappers[tab_idx][i] = wrapper

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
        # Only show queue option if the slot actually has a sound.
        try:
            tab = self.tabs[tab_idx]
            if slot_idx in tab.slots:
                menu.add_separator()
                menu.add_command(
                    label="📋 Add to Queue",
                    command=lambda: self._scheduler_add(tab_idx, slot_idx),
                )
                menu.add_separator()
                menu.add_command(
                    label="📋 Paste Image from Clipboard",
                    command=lambda: self._apply_clipboard_image_to_slot(
                        tab_idx, slot_idx, show_errors=True
                    ),
                )
                if tab.slots[slot_idx].image_path:
                    menu.add_command(
                        label="🗑 Clear Image",
                        command=lambda: self._clear_slot_image(tab_idx, slot_idx),
                    )
        except (IndexError, AttributeError):
            pass
        # Position the menu at the current mouse pointer. The slot's ⋯
        # button is drawn directly on the SlotWidget Canvas (not a real
        # widget), so `winfo_rootx/y` of the proxy returns the canvas
        # origin, not the click position — which made the menu pop up in
        # the wrong place. Pointer position is what the user actually
        # wants.
        try:
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
            self.tab_slot_group_labels,
            self.tab_slot_wrappers,
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
            self.tab_slot_group_labels,
            self.tab_slot_wrappers,
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
            # once (not every 250ms) — only if the MIXER also has nothing.
            # Paused sounds stay in mixer.currently_playing past the visual
            # duration timer, and their cards must remain visible until the
            # user explicitly presses ✕.
            panel = getattr(self, "now_playing_panel", None)
            if panel is not None and panel.is_visible:
                mixer_sounds: list = []
                if self.mixer and self.mixer.running:
                    try:
                        mixer_sounds = self.mixer.get_playing_sounds()
                    except Exception:
                        mixer_sounds = []
                if mixer_sounds:
                    # Throttle this slow-path too (0.5s is plenty since
                    # nothing is changing visibly except pause state).
                    if time.time() - self._last_panel_update >= 0.5:
                        panel.update(mixer_sounds, {})
                        self._last_panel_update = time.time()
                elif panel.sound_items:
                    panel.update([], {})
            self.root.after(250, self._animate_progress)
            return

        current_time = time.time()
        finished = []
        # Lazily fetched once per animate tick if we hit a "visually finished"
        # looping sound — used to decide whether to reap or restart the timer.
        mixer_active_ids: Optional[set] = None

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

            # Also push progress to the search-overlay slot (if visible) so a
            # filter view shows the same orange playing bar in real time —
            # regardless of which tab the sound actually lives on.
            if self._search_slot_widgets:
                entry = self._search_slot_widgets.get(
                    (play_info["tab_idx"], slot_idx)
                )
                if entry is not None:
                    try:
                        entry["progress"].set(progress_ratio)
                    except Exception:
                        pass

            # Check if finished
            if progress_ratio >= 1.0:
                # For LOOPING sounds, the mixer restarts the audio
                # automatically — don't reap the UI entry just because our
                # one-shot duration timer hit the end. Otherwise the slot
                # vanishes from the DJ Looper while the mixer keeps playing,
                # which makes "Stop All" look like it didn't stop anything
                # (the next loop iteration sounds "random" to the user).
                if play_info.get("loop"):
                    if mixer_active_ids is None and self.mixer:
                        try:
                            mixer_active_ids = {
                                s.get("sound_id")
                                for s in self.mixer.get_playing_sounds()
                            }
                        except Exception:
                            mixer_active_ids = set()
                    sound_id = f"{play_info['tab_idx']}_{slot_idx}"
                    if mixer_active_ids and sound_id in mixer_active_ids:
                        # Mixer is still playing — reset our visual timer so
                        # the progress bar restarts and the item stays in
                        # the panel.
                        play_info["start_time"] = current_time
                        last_progress.pop(slot_idx, None)
                        continue
                    # Else: mixer ran out of loops — fall through and reap.
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

            # Mirror onto search-overlay slot if visible (preview = green)
            if self._search_slot_widgets:
                entry = self._search_slot_widgets.get(
                    (play_info["tab_idx"], slot_idx)
                )
                if entry is not None:
                    try:
                        entry["progress"].set(progress_ratio)
                    except Exception:
                        pass

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
                if self.mixer and self.mixer.running:
                    playing_sounds = self.mixer.get_playing_sounds()
                    # Only clear cards if the mixer ALSO has nothing.
                    # Paused sounds linger in mixer.currently_playing
                    # past the visual timer — keep their cards visible.
                    if playing_sounds or panel.sound_items:
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

            # Mirror onto the search overlay if visible
            if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
                try:
                    self._paint_search_slot(tab_idx, slot_idx)
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

    def _filter_by_group(self, group: str):
        """Apply a group filter and show the search overlay.

        Used by the clickable group footer labels under each slot.
        """
        if not group:
            return
        try:
            self._filter_group_var.set(group)
        except Exception:
            pass
        self._filter_group = group
        # Don't clobber the user's text query.
        self._run_search()

    def _on_group_label_click(self, event, tab_idx: int, slot_idx: int):
        """Click on a slot's group footer → filter by that group.

        If the slot has one group, filter immediately. If multiple, pop a
        small menu so the user can pick which one to filter by.
        """
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        slot = self.tabs[tab_idx].slots.get(slot_idx)
        if slot is None or not slot.groups:
            return
        groups = list(slot.groups)
        if len(groups) == 1:
            self._filter_by_group(groups[0])
            return
        menu = tk.Menu(self.root, tearoff=0)
        for g in groups:
            menu.add_command(label=g, command=lambda gg=g: self._filter_by_group(gg))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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

    def _paint_search_slot(self, tab_idx: int, slot_idx: int) -> None:
        """Paint the search-overlay SlotWidget for (tab_idx, slot_idx) to
        match the slot's current state (filled/empty, color, image, emoji,
        playing/preview tint, hotkey label).

        Mirrors the relevant subset of `_update_slot_button_for_tab` so the
        overlay slot looks identical to its per-tab counterpart.
        """
        entry = self._search_slot_widgets.get((tab_idx, slot_idx))
        if entry is None:
            return
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        tab = self.tabs[tab_idx]
        slot = tab.slots.get(slot_idx)

        is_playing = (
            slot_idx in self.playing_slots
            and self.playing_slots[slot_idx].get("tab_idx") == tab_idx
        )
        is_previewing = (
            slot_idx in self.preview_slots
            and self.preview_slots[slot_idx].get("tab_idx") == tab_idx
        )

        default_color = slot.color if (slot and slot.color) else COLORS["blurple"]
        if is_playing:
            bg_color = COLORS["playing"]
            frame_color = COLORS["bg_light"]
        elif is_previewing:
            bg_color = COLORS["preview"]
            frame_color = COLORS["bg_light"]
        else:
            bg_color = default_color if slot is not None else "transparent"
            frame_color = COLORS["bg_medium"]

        try:
            entry["frame"].configure(fg_color=frame_color)
        except Exception:
            pass

        btn = entry["btn"]
        emoji_label = entry["emoji_label"]
        progress = entry["progress"]
        stop_btn = entry["stop_btn"]

        if slot is not None:
            # Prefer showing the file's basename so names match the actual file
            try:
                display_name = Path(slot.file_path).stem if slot.file_path else slot.name
            except Exception:
                display_name = slot.name
            display_text = _format_slot_display_text(display_name, slot.hotkey, slot.loop)
            photo = None
            if slot.image_path and os.path.exists(slot.image_path):
                photo = self._load_slot_image(slot.image_path)

            try:
                btn.configure(
                    text=display_text,
                    image=photo,
                    fg_color=bg_color,
                    hover_color=COLORS["bg_lighter"]
                    if not (is_playing or is_previewing)
                    else bg_color,
                    text_color=COLORS["text_primary"],
                    font=self._font_slot,
                    anchor="center",
                )
            except Exception:
                pass

            try:
                if slot.emoji:
                    emoji_label.configure(text=slot.emoji)
                    emoji_label.lift()
                else:
                    emoji_label.configure(text="")
                    emoji_label.lower()
            except Exception:
                pass

            # Stop button: visible while playing; progress reflects elapsed
            try:
                if is_playing:
                    stop_btn.pack(side=tk.LEFT, padx=(0, 2))
                    play_info = self.playing_slots[slot_idx]
                    duration = play_info.get("duration", 0) or 0
                    if duration > 0:
                        elapsed = time.time() - play_info["start_time"]
                        progress.set(min(elapsed / duration, 1.0))
                else:
                    stop_btn.pack_forget()
                    progress.set(0)
            except Exception:
                pass
        else:
            try:
                btn.configure(
                    text="+",
                    image=None,
                    fg_color="transparent",
                    hover_color=COLORS["bg_light"],
                    text_color=COLORS["text_muted"],
                    font=self._font_xl_bold,
                )
                emoji_label.configure(text="")
                emoji_label.lower()
                stop_btn.pack_forget()
                progress.set(0)
            except Exception:
                pass

    def _show_search_results(self):
        """Show search results overlay, hiding normal tab grids.

        Each result is rendered as a real `SlotWidget` (same as the main
        per-tab grid) so the playing progress bar, ⋯ menu and stop button
        all light up exactly like in the normal view. A small label below
        the slot shows the source tab + the slot's groups.
        """
        # Hide all tab grid frames
        for frame in self.tab_grid_frames.values():
            frame.grid_remove()

        # Destroy old results frame if exists (also drops _search_slot_widgets)
        if self._search_results_frame is not None:
            self._search_results_frame.destroy()
        self._search_slot_widgets = {}

        self._search_results_frame = ctk.CTkFrame(self.grid_frame, fg_color=COLORS["bg_dark"])
        self._search_slot_widgets = {}
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

        # Each result occupies a small wrapper frame containing:
        #   row 0 : SlotWidget (full slot rendering — playing/progress/menu/stop)
        #   row 1 : footer label (tab source + groups)
        for i, result in enumerate(results):
            row = (i // UI["grid_columns"]) + 1  # +1 for count label
            col = i % UI["grid_columns"]
            tab_idx: int = result["tab_idx"]
            slot_idx: int = result["slot_idx"]

            wrapper = ctk.CTkFrame(
                self._search_results_frame,
                fg_color="transparent",
                corner_radius=0,
            )
            wrapper.grid(
                row=row,
                column=col,
                padx=UI["slot_padding"],
                pady=UI["slot_padding"],
                sticky="nsew",
            )
            wrapper.grid_columnconfigure(0, weight=1)
            wrapper.grid_rowconfigure(0, weight=1)

            slot_widget = SlotWidget(
                wrapper,
                # Direct routes — DON'T switch tabs from the overlay,
                # otherwise clicking a result hides the filter view.
                # All four targets are tab-aware (`tab_idx` argument).
                on_click=lambda t=tab_idx, idx=slot_idx: self._play_slot_from_tab(t, idx),
                on_right_click=lambda e, t=tab_idx, idx=slot_idx: self._show_slot_menu(t, idx),
                on_menu=lambda t=tab_idx, idx=slot_idx: self._show_slot_menu(t, idx),
                on_stop=lambda t=tab_idx, idx=slot_idx: self._stop_slot_with_flag(idx),
                on_drag_drop=None,  # no reordering inside the search overlay
                height=UI["slot_height"],
            )
            slot_widget.grid(row=0, column=0, sticky="nsew")
            slot_widget.bind(
                "<Enter>",
                lambda _e, t=tab_idx, idx=slot_idx: self._set_hovered_slot(t, idx),
                add="+",
            )
            slot_widget.bind(
                "<Leave>",
                lambda _e, t=tab_idx, idx=slot_idx: self._clear_hovered_slot(t, idx),
                add="+",
            )

            slot_frame = FrameProxy(slot_widget)
            btn = ButtonProxy(slot_widget)
            progress = ProgressProxy(slot_widget)
            stop_btn = StopButtonProxy(slot_widget)
            emoji_label = EmojiLabelProxy(slot_widget)

            # Footer: source tab + groups (always shown so the user can tell
            # where the sound lives and which groups it belongs to). Rendered
            # as a small chip below the slot with a background so it visually
            # reads as part of the result, not as floating text.
            tab_info = f"{result['tab_emoji']} {result['tab_name']}".strip()
            slot_obj: SoundSlot = result["slot"]
            if slot_obj.groups:
                group_text = "  •  " + ", ".join(slot_obj.groups)
            else:
                group_text = "  •  (no groups)"
            footer = ctk.CTkLabel(
                wrapper,
                text=f"{tab_info}{group_text}",
                font=ctk.CTkFont(family=FONTS["family"], size=11),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["bg_medium"],
                corner_radius=4,
                anchor="w",
                justify="left",
                height=20,
            )
            footer.grid(row=1, column=0, sticky="ew", padx=2, pady=(3, 0), ipadx=4)

            entry = {
                "wrapper": wrapper,
                "widget": slot_widget,
                "frame": slot_frame,
                "btn": btn,
                "progress": progress,
                "stop_btn": stop_btn,
                "emoji_label": emoji_label,
                "footer": footer,
                "tab_idx": tab_idx,
                "slot_idx": slot_idx,
            }
            self._search_result_widgets.append(entry)
            self._search_slot_widgets[(tab_idx, slot_idx)] = entry

            # Initial paint to match the slot's normal appearance + reflect
            # any currently-playing state so the user sees the orange/green
            # tint the moment they open the filter view.
            self._paint_search_slot(tab_idx, slot_idx)

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
        """Create the status bar at the bottom.

        Shows live system state at a glance:
          [● Stream]  🎤 Mic Name  →  🔊 Output Name  |  PTT key  |  🔴 Rec timer
        plus a free-form right-hand status message ("Ready", "Saved …", etc.).
        Always visible — gives the user the same info the audio-options card
        does even when that card is collapsed.
        """
        status_frame = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_dark"],
            corner_radius=UI["button_corner_radius"],
            height=30,
        )
        status_frame.pack(fill=tk.X, pady=(8, 0))
        status_frame.pack_propagate(False)

        font_xs = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"])
        font_xs_bold = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold")

        # Stream indicator (colored dot + label)
        self.status_stream_label = ctk.CTkLabel(
            status_frame,
            text="● Stopped",
            font=font_xs_bold,
            text_color=COLORS["text_muted"],
        )
        self.status_stream_label.pack(side=tk.LEFT, padx=(10, 8), pady=4)

        # Mic device
        self.status_mic_label = ctk.CTkLabel(
            status_frame,
            text="🎤 —",
            font=font_xs,
            text_color=COLORS["text_secondary"],
        )
        self.status_mic_label.pack(side=tk.LEFT, padx=(0, 6), pady=4)

        ctk.CTkLabel(
            status_frame,
            text="→",
            font=font_xs,
            text_color=COLORS["text_muted"],
        ).pack(side=tk.LEFT, padx=(0, 6), pady=4)

        # Output device
        self.status_output_label = ctk.CTkLabel(
            status_frame,
            text="🔊 —",
            font=font_xs,
            text_color=COLORS["text_secondary"],
        )
        self.status_output_label.pack(side=tk.LEFT, padx=(0, 12), pady=4)

        # PTT info
        self.status_ptt_label = ctk.CTkLabel(
            status_frame,
            text="",
            font=font_xs,
            text_color=COLORS["text_muted"],
        )
        self.status_ptt_label.pack(side=tk.LEFT, padx=(0, 12), pady=4)

        # Recording info (separate from action-bar timer; shown only while recording)
        self.status_rec_label = ctk.CTkLabel(
            status_frame,
            text="",
            font=font_xs_bold,
            text_color=COLORS["red"],
        )
        self.status_rec_label.pack(side=tk.LEFT, padx=(0, 12), pady=4)

        # Right-hand free-form status message
        self.status_var = tk.StringVar(value="Ready")
        self._last_status_text = self.status_var.get()
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
            font=font_xs,
            text_color=COLORS["text_muted"],
            anchor="e",
        )
        self.status_label.pack(side=tk.RIGHT, padx=10, pady=4)

    def _short_device_name(self, raw: str, max_len: int = 28) -> str:
        """Strip the leading 'NN: ' device-index prefix and trim to max_len."""
        if not raw:
            return "—"
        name = raw.split(":", 1)[1].strip() if ":" in raw else raw
        # Drop trailing API tag like " (MME)" / " (Windows DirectSound)"
        if " (" in name and name.endswith(")"):
            name = name.rsplit(" (", 1)[0]
        if len(name) > max_len:
            name = name[: max_len - 1].rstrip() + "…"
        return name

    def _update_status_bar(self):
        """Refresh the live indicators in the status bar.

        Safe to call from any state change (stream toggle, device change,
        recording start/stop, PTT change). Cheap — just reconfigures a few
        CTkLabel text/colors; the StringVar is dedup'd.
        """
        if not hasattr(self, "status_stream_label"):
            return  # Status bar not built yet (called during init)

        # --- Stream state ---
        running = bool(self.mixer and self.mixer.running)
        if running:
            self.status_stream_label.configure(text="● Live", text_color=COLORS["green"])
        else:
            self.status_stream_label.configure(text="○ Stopped", text_color=COLORS["text_muted"])

        # --- Devices ---
        mic_raw = self.input_var.get() if hasattr(self, "input_var") else ""
        out_raw = self.output_var.get() if hasattr(self, "output_var") else ""
        mic_short = self._short_device_name(mic_raw)
        out_short = self._short_device_name(out_raw)

        muted = bool(self.mixer and self.mixer.mic_muted)
        mic_text = f"🔇 {mic_short}" if muted else f"🎤 {mic_short}"
        self.status_mic_label.configure(
            text=mic_text,
            text_color=(
                COLORS["red"]
                if muted
                else (COLORS["text_secondary"] if running else COLORS["text_muted"])
            ),
        )
        self.status_output_label.configure(
            text=f"🔊 {out_short}",
            text_color=(COLORS["text_secondary"] if running else COLORS["text_muted"]),
        )

        # --- PTT ---
        ptt_enabled = bool(getattr(self, "ptt_enabled_var", None) and self.ptt_enabled_var.get())
        ptt_key = self.ptt_key_var.get().strip() if hasattr(self, "ptt_key_var") else ""
        if ptt_enabled and ptt_key:
            self.status_ptt_label.configure(
                text=f"🎙 PTT: {ptt_key}",
                text_color=COLORS["blurple"],
            )
        else:
            self.status_ptt_label.configure(text="", text_color=COLORS["text_muted"])

        # --- Recording ---
        if self.recorder is not None and self.recorder.recording:
            elapsed = int(self.recorder.get_elapsed())
            m, s = divmod(elapsed, 60)
            h, m = divmod(m, 60)
            if h > 0:
                t = f"{h}:{m:02d}:{s:02d}"
            else:
                t = f"{m}:{s:02d}"
            self.status_rec_label.configure(text=f"🔴 REC {t}", text_color=COLORS["red"])
        else:
            self.status_rec_label.configure(text="")

    def _toggle_stream(self):
        """Start or stop the audio stream."""
        if self.mixer and self.mixer.running:
            self.mixer.stop()
            self.toggle_btn.configure(
                text="▶ Start Stream", fg_color=COLORS["green"], hover_color=COLORS["green_hover"]
            )
            self.status_var.set("Stream stopped")
            self._update_status_bar()
        else:
            try:
                input_idx = int(self.input_var.get().split(":")[0])
                output_idx = int(self.output_var.get().split(":")[0])
                self.mixer = AudioMixer(input_idx, output_idx, sound_cache=self.sound_cache)
                # Apply persisted volume settings
                self.mixer.mic_volume = self.mic_volume_var.get() / 100.0
                if hasattr(self, "master_volume_var"):
                    self.mixer.master_volume = self.master_volume_var.get() / 100.0
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
                self.status_var.set(f"Streaming{ptt_status}")
                self._update_status_bar()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to start:\n{e}")
                self._update_status_bar()

    def _update_mic_volume(self, _=None):
        """Update microphone volume from slider."""
        val = self.mic_volume_var.get()
        if hasattr(self, "mic_volume_label"):
            self.mic_volume_label.configure(text=f"{int(val)}%")
        if self.mixer:
            self.mixer.mic_volume = val / 100.0

    def _update_master_volume(self, _=None):
        """Update master (sounds) volume from slider. Affects all playing sounds."""
        val = self.master_volume_var.get()
        if hasattr(self, "master_volume_label"):
            self.master_volume_label.configure(text=f"{int(val)}%")
        if self.mixer:
            self.mixer.master_volume = val / 100.0
        # Persist (debounced).
        self._save_config()

    def _toggle_mic_mute(self):
        """Toggle microphone mute state."""
        if self.mixer:
            self.mixer.mic_muted = self.mic_mute_var.get()
        self._update_status_bar()

    def _toggle_monitor(self):
        """Toggle local speaker monitoring (hear sounds through speakers)."""
        if self.mixer:
            self.mixer.set_monitor_enabled(self.monitor_var.get())

    # ------------------------------------------------------------------
    # Test Output (hear what Discord hears)
    # ------------------------------------------------------------------

    def _ensure_test_ptt_key(self) -> bool:
        """Make sure the mixer has a PTT key for the test, even if the main
        PTT checkbox isn't enabled.

        The user might want to test PTT without permanently turning on
        Push-to-Talk for normal sound playback. If a key is recorded in
        `ptt_key_var` but the mixer doesn't have one, push it in for the
        duration of the test. Returns True if a key is available.
        """
        if not self.mixer:
            return False
        if self.mixer.ptt_key:
            return True
        # Borrow the configured key (if any) from the GUI state.
        key = self.ptt_key_var.get().strip() if hasattr(self, "ptt_key_var") else ""
        if not key:
            return False
        try:
            self.mixer.set_ptt_key(key)
        except Exception as e:
            print(f"[gui] Could not sync PTT key for test: {e}")
            return False
        return bool(self.mixer.ptt_key)

    def _toggle_test_live(self):
        """Toggle live test output - pipes the Discord-bound mix to speakers."""
        enabled = self.test_live_var.get()
        if not self.mixer or not self.mixer.running:
            self.test_live_var.set(False)
            self.test_status_label.configure(
                text="Start the stream first.", text_color=COLORS["red"]
            )
            self.root.after(2500, lambda: self.test_status_label.configure(text=""))
            return
        try:
            self.mixer.set_test_output_enabled(enabled)
        except Exception as e:
            print(f"[gui] Test live toggle failed: {e}")
            self.test_live_var.set(False)
            self.test_status_label.configure(text=f"Error: {e}", text_color=COLORS["red"])
            return
        if enabled:
            # Optionally also press & hold PTT so Discord actually transmits
            # the test signal (full end-to-end verification).
            ptt_held = False
            if self.test_ptt_var.get():
                if self._ensure_test_ptt_key():
                    ptt_held = self.mixer.hold_ptt()
            msg = "● LIVE - hearing what Discord hears"
            if ptt_held:
                msg += "  •  🎙 PTT held"
            elif self.test_ptt_var.get() and self.mixer.ptt_key is None:
                msg += "  •  (set a PTT key first)"
            self.test_status_label.configure(text=msg, text_color=COLORS["green"])
        else:
            # Always release PTT hold when disabling Live Test
            try:
                self.mixer.release_ptt_hold()
            except Exception:
                pass
            self.test_status_label.configure(text="")

    def _toggle_test_ptt(self):
        """Toggle PTT-hold during the active Live Test."""
        if not self.mixer:
            return
        # Only act if Live Test is currently running - otherwise this is just
        # a preference for the next time the user enables it.
        if not self.test_live_var.get():
            return
        if self.test_ptt_var.get():
            held = False
            if self._ensure_test_ptt_key():
                held = self.mixer.hold_ptt()
            if held:
                self.test_status_label.configure(
                    text="● LIVE - hearing what Discord hears  •  🎙 PTT held",
                    text_color=COLORS["green"],
                )
            else:
                self.test_status_label.configure(
                    text="● LIVE - hearing what Discord hears  •  (set a PTT key first)",
                    text_color=COLORS["green"],
                )
        else:
            try:
                self.mixer.release_ptt_hold()
            except Exception:
                pass
            self.test_status_label.configure(
                text="● LIVE - hearing what Discord hears", text_color=COLORS["green"]
            )

    def _toggle_test_record(self):
        """Record N seconds of the Discord-bound mix, then play it back."""
        if not self.mixer or not self.mixer.running:
            self.test_status_label.configure(
                text="Start the stream first.", text_color=COLORS["red"]
            )
            self.root.after(2500, lambda: self.test_status_label.configure(text=""))
            return

        # If currently playing back a previous test, stop it
        if self._test_playback_stream is not None:
            try:
                sd.stop()
            except Exception:
                pass
            self._test_playback_stream = None
            # Drop the PTT hold if we grabbed one for this test
            try:
                self.mixer.release_ptt_hold()
            except Exception:
                pass
            self.test_record_btn.configure(text="⏺ Record & Play", fg_color=COLORS["red"])
            self.test_status_label.configure(text="")
            return

        # If currently recording, abort
        if self._test_record_pending:
            self.mixer.stop_test_recording()
            self._test_record_pending = False
            try:
                self.mixer.release_ptt_hold()
            except Exception:
                pass
            self.test_record_btn.configure(text="⏺ Record & Play", fg_color=COLORS["red"])
            self.test_status_label.configure(text="Cancelled.", text_color=COLORS["text_muted"])
            return

        # Parse duration ("5s" -> 5.0)
        try:
            seconds = float(self.test_duration_var.get().rstrip("sS"))
        except ValueError:
            seconds = 5.0

        ok = self.mixer.start_test_recording(
            seconds,
            on_done=lambda: self.root.after(0, self._on_test_record_done),
        )
        if not ok:
            self.test_status_label.configure(text="Could not start.", text_color=COLORS["red"])
            return

        # Hold PTT for the duration of the recording so Discord actually
        # transmits while we capture (true end-to-end test). Released in
        # `_on_test_playback_done` after playback finishes.
        if self.test_ptt_var.get():
            if self._ensure_test_ptt_key():
                self.mixer.hold_ptt()

        self._test_record_pending = True
        self.test_record_btn.configure(text="■ Stop", fg_color=COLORS["bg_lighter"])
        # Live countdown
        self._test_record_remaining = seconds
        self._tick_test_record_countdown()

    def _tick_test_record_countdown(self):
        """Update the recording status label every 250ms."""
        if not self._test_record_pending:
            return
        self.test_status_label.configure(
            text=f"🔴 Recording… {self._test_record_remaining:.1f}s",
            text_color=COLORS["red"],
        )
        self._test_record_remaining -= 0.25
        if self._test_record_remaining > 0 and self._test_record_pending:
            self.root.after(250, self._tick_test_record_countdown)

    def _on_test_record_done(self):
        """Mixer finished capturing - grab the buffer and play it back."""
        if not self._test_record_pending or not self.mixer:
            return
        self._test_record_pending = False
        # Capture is done - release the PTT hold immediately. Local playback
        # below goes to speakers only (NOT through the mixer / virtual cable),
        # so there's no reason to keep Discord transmitting.
        try:
            self.mixer.release_ptt_hold()
        except Exception:
            pass
        audio = self.mixer.stop_test_recording()
        if audio is None or len(audio) == 0:
            self.test_record_btn.configure(text="⏺ Record & Play", fg_color=COLORS["red"])
            self.test_status_label.configure(text="No audio captured.", text_color=COLORS["red"])
            self.root.after(2500, lambda: self.test_status_label.configure(text=""))
            return

        # Play back through the default speakers using sounddevice's
        # convenience API - non-blocking, no manual stream management needed.
        try:
            sd.play(audio, samplerate=self.mixer.sample_rate, blocking=False)
            # Track that playback is in progress so the button can stop it
            self._test_playback_stream = True  # type: ignore[assignment]
        except Exception as e:
            print(f"[gui] Test playback failed: {e}")
            self.test_record_btn.configure(text="⏺ Record & Play", fg_color=COLORS["red"])
            self.test_status_label.configure(text=f"Playback error: {e}", text_color=COLORS["red"])
            return

        duration = len(audio) / float(self.mixer.sample_rate)
        self.test_record_btn.configure(text="■ Stop Playback", fg_color=COLORS["bg_lighter"])
        self.test_status_label.configure(
            text=f"▶ Playing back ({duration:.1f}s)…", text_color=COLORS["blurple"]
        )
        # Reset UI when playback finishes
        self.root.after(int(duration * 1000) + 100, self._on_test_playback_done)

    def _on_test_playback_done(self):
        """Reset the test record button after playback finishes."""
        if self._test_playback_stream is None:
            return
        self._test_playback_stream = None
        self.test_record_btn.configure(text="⏺ Record & Play", fg_color=COLORS["red"])
        self.test_status_label.configure(text="Done.", text_color=COLORS["text_muted"])
        self.root.after(1500, lambda: self.test_status_label.configure(text=""))

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

    # ------------------------------------------------------------------
    # Drag-to-DJ-Looper (stage a sound without playing it)
    # ------------------------------------------------------------------

    def _on_slot_drag_drop(self, tab_idx: int, slot_idx: int, x_root: int, y_root: int):
        """Called when the user drags a slot and releases the mouse.

        If the release lands inside the DJ Looper panel, the sound is
        added to the panel as a STAGED item (not played yet). Otherwise
        the drop is ignored and the slot continues to behave normally.
        """
        # Only meaningful for filled slots.
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        tab = self.tabs[tab_idx]
        if slot_idx not in tab.slots:
            return
        panel = getattr(self, "now_playing_panel", None)
        if panel is None or panel.frame is None:
            return

        # Walk up parents from the widget under the cursor and check whether
        # the panel's frame is in the chain.
        try:
            target = self.root.winfo_containing(x_root, y_root)
        except Exception:
            target = None
        cursor = target
        on_panel = False
        while cursor is not None:
            if cursor is panel.frame:
                on_panel = True
                break
            try:
                cursor = cursor.master  # type: ignore[attr-defined]
            except Exception:
                break

        if not on_panel:
            return

        # Auto-show panel if it was somehow hidden.
        if not panel.is_visible:
            panel.show()
            try:
                self.now_playing_btn.configure(
                    fg_color=COLORS["blurple"],
                    hover_color=COLORS["blurple_hover"],
                )
            except Exception:
                pass

        slot = tab.slots[slot_idx]
        panel.add_staged(
            tab_idx=tab_idx,
            slot_idx=slot_idx,
            name=slot.name,
            emoji=slot.emoji or "",
            color=slot.color,
        )
        self.status_var.set(f"Staged: {slot.name}")

    def _play_staged_slot(self, tab_idx: int, slot_idx: int):
        """Play a slot that was staged in the DJ Looper panel."""
        self._play_slot_from_tab(tab_idx, slot_idx)

    def _edit_staged_slot(self, tab_idx: int, slot_idx: int):
        """Open the slot config dialog for a staged item so the user can
        tweak volume / speed / loop / etc. before clicking ▶ to play."""
        try:
            self._configure_slot_for_tab(tab_idx, slot_idx)
        except Exception as e:
            self.status_var.set(f"Edit failed: {e}")

    def _get_slot_for_panel(self, tab_idx: int, slot_idx: int) -> Optional[SoundSlot]:
        """Look up a SoundSlot by tab + slot index. Used by the DJ Looper
        panel's staged items so they can show inline volume/speed/loop
        controls bound to the underlying slot."""
        if 0 <= tab_idx < len(self.tabs):
            return self.tabs[tab_idx].slots.get(slot_idx)
        return None

    def _on_staged_slot_changed(self, tab_idx: int, slot_idx: int):
        """Persist any inline edit the user made on a staged item."""
        try:
            self._save_config()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sound Scheduler — queue sounds and play them in sequence
    # ------------------------------------------------------------------

    def _scheduler_add(self, tab_idx: int, slot_idx: int):
        """Append a slot to the scheduler queue and update UI."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        tab = self.tabs[tab_idx]
        if slot_idx not in tab.slots:
            return
        slot = tab.slots[slot_idx]
        self._scheduler_queue.append({"tab_idx": tab_idx, "slot_idx": slot_idx})
        self._update_scheduler_button_label()
        self._refresh_scheduler_dialog()
        self.status_var.set(f"Queued: {slot.name} (queue: {len(self._scheduler_queue)})")

    def _scheduler_remove(self, index: int):
        if 0 <= index < len(self._scheduler_queue):
            self._scheduler_queue.pop(index)
            self._update_scheduler_button_label()
            self._refresh_scheduler_dialog()

    def _scheduler_move(self, index: int, delta: int):
        new_index = index + delta
        if 0 <= index < len(self._scheduler_queue) and 0 <= new_index < len(self._scheduler_queue):
            q = self._scheduler_queue
            q[index], q[new_index] = q[new_index], q[index]
            self._refresh_scheduler_dialog()

    def _scheduler_clear(self):
        self._scheduler_queue.clear()
        self._update_scheduler_button_label()
        self._refresh_scheduler_dialog()

    def _scheduler_start(self):
        """Begin playing the queue from the top."""
        if not self._scheduler_queue or self._scheduler_running:
            return
        if not self.mixer or not self.mixer.running:
            self.status_var.set("Start the audio stream first!")
            return
        self._scheduler_running = True
        self._refresh_scheduler_dialog()
        self._scheduler_play_next()

    def _scheduler_stop(self):
        """Halt queue playback (the currently playing sound finishes)."""
        self._scheduler_running = False
        if self._scheduler_after_id is not None:
            try:
                self.root.after_cancel(self._scheduler_after_id)
            except Exception:
                pass
            self._scheduler_after_id = None
        self._refresh_scheduler_dialog()
        self.status_var.set("Queue stopped")

    def _scheduler_play_next(self):
        """Play the next queued sound and schedule the one after it."""
        self._scheduler_after_id = None
        if not self._scheduler_running or not self._scheduler_queue:
            self._scheduler_running = False
            self._refresh_scheduler_dialog()
            return

        item = self._scheduler_queue.pop(0)
        self._update_scheduler_button_label()
        tab_idx = item["tab_idx"]
        slot_idx = item["slot_idx"]

        # Play through the existing path so progress UI / PTT / DJ Looper
        # all get updated naturally.
        try:
            if tab_idx == self.current_tab_idx:
                self._play_slot(slot_idx)
            else:
                self._play_slot_from_tab(tab_idx, slot_idx)
        except Exception as e:
            self.status_var.set(f"Queue error: {e}")
            self._scheduler_running = False
            self._refresh_scheduler_dialog()
            return

        # Compute when to fire the next one. The mixer.play_sound returns
        # duration; we already stored it in playing_slots.
        info = self.playing_slots.get(slot_idx)
        if info and info.get("tab_idx") == tab_idx:
            duration_ms = max(100, int(info["duration"] * 1000))
        else:
            duration_ms = 1000  # Fallback if we couldn't read the duration

        delay_s = (
            float(self._scheduler_delay_var.get()) if self._scheduler_delay_var is not None else 0.0
        )
        gap_ms = max(0, int(delay_s * 1000))
        wait_ms = duration_ms + gap_ms

        if self._scheduler_queue:
            self._scheduler_after_id = self.root.after(wait_ms, self._scheduler_play_next)
        else:
            # Last sound — let it finish, then mark queue stopped.
            self._scheduler_after_id = self.root.after(wait_ms, self._scheduler_finished)

        self._refresh_scheduler_dialog()

    def _scheduler_finished(self):
        self._scheduler_after_id = None
        self._scheduler_running = False
        self._refresh_scheduler_dialog()
        self.status_var.set("Queue finished")

    def _update_scheduler_button_label(self):
        """Show queue length on the scheduler button (e.g. '📋 Queue (3)')."""
        if not hasattr(self, "scheduler_btn"):
            return
        n = len(self._scheduler_queue)
        text = "📋 Queue" if n == 0 else f"📋 Queue ({n})"
        try:
            self.scheduler_btn.configure(text=text)
        except Exception:
            pass

    def _open_scheduler_dialog(self):
        """Open (or focus) the scheduler dialog."""
        if self._scheduler_dialog is not None:
            try:
                if self._scheduler_dialog.winfo_exists():
                    self._scheduler_dialog.deiconify()
                    self._scheduler_dialog.lift()
                    self._scheduler_dialog.focus_force()
                    return
            except Exception:
                pass
            self._scheduler_dialog = None

        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Sound Queue")
        dlg.geometry("420x520")
        dlg.configure(fg_color=COLORS["bg_dark"])
        dlg.transient(self.root)

        def _on_close():
            self._scheduler_dialog = None
            self._scheduler_list_frame = None
            self._scheduler_status_var = None
            self._scheduler_play_btn = None
            try:
                dlg.destroy()
            except Exception:
                pass

        dlg.protocol("WM_DELETE_WINDOW", _on_close)
        self._scheduler_dialog = dlg

        # Header
        header = ctk.CTkFrame(dlg, fg_color="transparent")
        header.pack(fill=tk.X, padx=12, pady=(12, 4))
        ctk.CTkLabel(
            header,
            text="📋 Sound Queue",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_lg"], weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(side=tk.LEFT)

        # Status text (e.g. "3 queued · running")
        self._scheduler_status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            header,
            textvariable=self._scheduler_status_var,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        ).pack(side=tk.RIGHT)

        # Inter-sound delay
        delay_row = ctk.CTkFrame(dlg, fg_color="transparent")
        delay_row.pack(fill=tk.X, padx=12, pady=(4, 4))
        ctk.CTkLabel(
            delay_row,
            text="Delay between sounds:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
        ).pack(side=tk.LEFT)

        if self._scheduler_delay_var is None:
            self._scheduler_delay_var = tk.DoubleVar(value=0.0)
        delay_value_label = ctk.CTkLabel(
            delay_row,
            text=f"{self._scheduler_delay_var.get():.1f}s",
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_sm"]),
            text_color=COLORS["text_primary"],
            width=44,
        )
        delay_value_label.pack(side=tk.RIGHT)

        def _on_delay_change(v):
            try:
                delay_value_label.configure(text=f"{float(v):.1f}s")
            except Exception:
                pass

        delay_slider = ctk.CTkSlider(
            dlg,
            from_=0,
            to=10,
            variable=self._scheduler_delay_var,
            command=_on_delay_change,
            fg_color=COLORS["bg_medium"],
            progress_color=COLORS["yellow"],
            button_color=COLORS["yellow"],
            button_hover_color=COLORS["yellow"],
        )
        delay_slider.pack(fill=tk.X, padx=12, pady=(0, 8))

        # Scrollable list of queued sounds
        list_container = ctk.CTkScrollableFrame(
            dlg,
            fg_color=COLORS["bg_medium"],
            corner_radius=8,
        )
        list_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        self._scheduler_list_frame = list_container

        # Action buttons
        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 12))

        self._scheduler_play_btn = ctk.CTkButton(
            btn_row,
            text="▶ Play Queue",
            command=self._scheduler_toggle_play,
            fg_color=COLORS["green"],
            hover_color=COLORS.get("green_hover", COLORS["green"]),
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            corner_radius=6,
            height=34,
        )
        self._scheduler_play_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        clear_btn = ctk.CTkButton(
            btn_row,
            text="🗑 Clear",
            command=self._scheduler_clear,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=6,
            height=34,
            width=80,
        )
        clear_btn.pack(side=tk.LEFT)

        self._refresh_scheduler_dialog()

    def _scheduler_toggle_play(self):
        if self._scheduler_running:
            self._scheduler_stop()
        else:
            self._scheduler_start()

    def _refresh_scheduler_dialog(self):
        """Rebuild the queue list inside the open scheduler dialog."""
        frame = self._scheduler_list_frame
        if frame is None:
            return
        try:
            if not frame.winfo_exists():
                self._scheduler_list_frame = None
                return
        except Exception:
            return

        # Wipe existing children
        for child in frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        self._scheduler_row_widgets = []

        if not self._scheduler_queue:
            ctk.CTkLabel(
                frame,
                text="Queue is empty.\nUse a slot's ⋯ menu → 'Add to Queue'.",
                font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
                text_color=COLORS["text_muted"],
                justify="center",
            ).pack(pady=24)
        else:
            for i, item in enumerate(list(self._scheduler_queue)):
                self._build_scheduler_row(frame, i, item)

        # Status text + play-button label
        if self._scheduler_status_var is not None:
            n = len(self._scheduler_queue)
            state = " · running" if self._scheduler_running else ""
            self._scheduler_status_var.set(
                f"{n} queued{state}" if n else ("running" if self._scheduler_running else "")
            )
        if self._scheduler_play_btn is not None:
            try:
                self._scheduler_play_btn.configure(
                    text="⏹ Stop Queue" if self._scheduler_running else "▶ Play Queue",
                    fg_color=COLORS["red"] if self._scheduler_running else COLORS["green"],
                    hover_color=(
                        COLORS["red_hover"] if self._scheduler_running else COLORS["green_hover"]
                    ),
                )
            except Exception:
                pass

    # ---- Drag-and-drop reordering inside the scheduler dialog ----

    def _scheduler_drag_start(self, event, index: int):
        self._scheduler_drag_index = index
        self._scheduler_drag_target = index
        self._scheduler_highlight_target(index)

    def _scheduler_drag_motion(self, event):
        if self._scheduler_drag_index is None or not self._scheduler_row_widgets:
            return
        # event.x_root / y_root are screen coords; compute which row the
        # cursor is currently over by checking each row's screen rect.
        y = event.y_root
        target = self._scheduler_drag_index  # default: no change
        for i, row in enumerate(self._scheduler_row_widgets):
            try:
                if not row.winfo_exists():
                    continue
                top = row.winfo_rooty()
                bot = top + row.winfo_height()
                if top <= y <= bot:
                    target = i
                    break
                # Above the first row → drop at start
                if i == 0 and y < top:
                    target = 0
                    break
            except Exception:
                continue
        else:
            # Below the last row → drop at end
            try:
                last = self._scheduler_row_widgets[-1]
                if y > last.winfo_rooty() + last.winfo_height():
                    target = len(self._scheduler_row_widgets) - 1
            except Exception:
                pass
        if target != self._scheduler_drag_target:
            self._scheduler_drag_target = target
            self._scheduler_highlight_target(target)

    def _scheduler_drag_release(self, event):
        src = self._scheduler_drag_index
        dst = self._scheduler_drag_target
        self._scheduler_drag_index = None
        self._scheduler_drag_target = None
        self._scheduler_clear_highlights()
        if src is None or dst is None or src == dst:
            return
        if not (0 <= src < len(self._scheduler_queue)) or not (
            0 <= dst < len(self._scheduler_queue)
        ):
            return
        item = self._scheduler_queue.pop(src)
        self._scheduler_queue.insert(dst, item)
        self._refresh_scheduler_dialog()

    def _scheduler_highlight_target(self, index: int):
        for i, row in enumerate(self._scheduler_row_widgets):
            try:
                if not row.winfo_exists():
                    continue
                if i == index and i != self._scheduler_drag_index:
                    row.configure(border_color=COLORS["blurple"])
                elif i == self._scheduler_drag_index:
                    row.configure(border_color=COLORS["bg_lighter"])
                else:
                    row.configure(border_color=COLORS["bg_dark"])
            except Exception:
                pass

    def _scheduler_clear_highlights(self):
        for row in self._scheduler_row_widgets:
            try:
                if row.winfo_exists():
                    row.configure(border_color=COLORS["bg_dark"])
            except Exception:
                pass

    def _build_scheduler_row(self, parent: Any, index: int, item: Dict[str, Any]):
        tab_idx = item["tab_idx"]
        slot_idx = item["slot_idx"]
        try:
            tab = self.tabs[tab_idx]
            slot = tab.slots.get(slot_idx)
        except IndexError:
            slot = None

        row = ctk.CTkFrame(parent, fg_color=COLORS["bg_dark"], corner_radius=6, border_width=2, border_color=COLORS["bg_dark"])
        row.pack(fill=tk.X, pady=3, padx=2)
        # Track for drag-target hit-testing
        self._scheduler_row_widgets.append(row)

        # Drag handle (grip) — also doubles as the order number tile
        grip = ctk.CTkLabel(
            row,
            text=f"\u2630  {index + 1}",  # ≡ + number
            font=ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_muted"],
            width=46,
            cursor="fleur",
        )
        grip.pack(side=tk.LEFT, padx=(8, 4), pady=6)
        # Bind drag handlers to the grip AND the row body so users can
        # grab anywhere on the row (except the action buttons) to reorder.
        for w in (row, grip):
            w.bind("<ButtonPress-1>", lambda e, i=index: self._scheduler_drag_start(e, i))
            w.bind("<B1-Motion>", self._scheduler_drag_motion)
            w.bind("<ButtonRelease-1>", self._scheduler_drag_release)

        # Sound name + tab info
        if slot is not None:
            label_text = (slot.emoji + " " if slot.emoji else "") + slot.name
            sub_text = f"  ·  {tab.emoji or ''} {tab.name}".rstrip()
        else:
            label_text = "(missing slot)"
            sub_text = ""

        text_box = ctk.CTkFrame(row, fg_color="transparent")
        text_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        name_lbl = ctk.CTkLabel(
            text_box,
            text=label_text,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
            cursor="fleur",
        )
        name_lbl.pack(fill=tk.X)
        sub_lbl = None
        if sub_text:
            sub_lbl = ctk.CTkLabel(
                text_box,
                text=sub_text,
                font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
                text_color=COLORS["text_muted"],
                anchor="w",
                cursor="fleur",
            )
            sub_lbl.pack(fill=tk.X)

        # Make the text area also draggable
        for w in (text_box, name_lbl) + ((sub_lbl,) if sub_lbl is not None else ()):
            w.bind("<ButtonPress-1>", lambda e, i=index: self._scheduler_drag_start(e, i))
            w.bind("<B1-Motion>", self._scheduler_drag_motion)
            w.bind("<ButtonRelease-1>", self._scheduler_drag_release)

        btn_font = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"])

        up_btn = ctk.CTkButton(
            row,
            text="▲",
            command=lambda i=index: self._scheduler_move(i, -1),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=26,
            height=24,
        )
        up_btn.pack(side=tk.LEFT, padx=2, pady=4)

        down_btn = ctk.CTkButton(
            row,
            text="▼",
            command=lambda i=index: self._scheduler_move(i, 1),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=btn_font,
            corner_radius=4,
            width=26,
            height=24,
        )
        down_btn.pack(side=tk.LEFT, padx=2, pady=4)

        del_btn = ctk.CTkButton(
            row,
            text="✕",
            command=lambda i=index: self._scheduler_remove(i),
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            font=btn_font,
            corner_radius=4,
            width=26,
            height=24,
        )
        del_btn.pack(side=tk.LEFT, padx=(2, 8), pady=4)

    def _stop_all_sounds(self):
        """Stop all currently playing sounds (Discord and preview)."""
        # Halt the scheduler too — Stop All should mean STOP everything.
        if self._scheduler_running or self._scheduler_after_id is not None:
            self._scheduler_stop()
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

        # Mirror onto the search overlay (works regardless of current tab)
        if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
            try:
                self._paint_search_slot(tab_idx, slot_idx)
            except Exception:
                pass

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
                set_ptt_key(_mouse_button_to_key_name(button))

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

    def _apply_hover_preview_key(self):
        """Apply the hover-preview binding typed in the entry."""
        key = self.hover_preview_key_var.get().strip().lower()
        self.hover_preview_key_var.set(key)
        self._register_hover_preview_binding()
        self._save_config()

    def _clear_hover_preview_key(self):
        """Disable the hover-preview binding."""
        self.hover_preview_key_var.set("")
        self._register_hover_preview_binding()
        if hasattr(self, "hover_preview_status_label"):
            self.hover_preview_status_label.configure(
                text="Hover preview disabled",
                text_color=COLORS["text_muted"],
            )
        self._save_config()

    def _record_hover_preview_key(self):
        """Record a key or mouse button for hover preview."""
        self.hover_preview_record_btn.configure(text="Press key...", fg_color=COLORS["red"])
        self.hover_preview_status_label.configure(
            text="Press key or mouse button (5s)...",
            text_color=COLORS["blurple"],
        )
        self.root.update()

        hook_refs = {"keyboard": None, "mouse": None, "recording": True, "timeout": None}

        def cleanup_hooks():
            if hook_refs["keyboard"] is not None and HOTKEYS_AVAILABLE:
                try:
                    keyboard.unhook(hook_refs["keyboard"])
                except Exception:
                    pass
            if hook_refs["mouse"] is not None:
                try:
                    import mouse  # type: ignore[import-untyped]

                    mouse.unhook(hook_refs["mouse"])
                except Exception:
                    pass

        def finish(key_name: Optional[str]):
            if not hook_refs["recording"]:
                return
            hook_refs["recording"] = False
            timeout_id = hook_refs.get("timeout")
            if timeout_id is not None:
                try:
                    self.root.after_cancel(timeout_id)  # type: ignore[arg-type]
                except Exception:
                    pass
            cleanup_hooks()

            def update_ui():
                self.hover_preview_record_btn.configure(
                    text="Record Key",
                    fg_color=COLORS["blurple"],
                )
                if not key_name:
                    self.hover_preview_status_label.configure(
                        text="Recording timed out",
                        text_color=COLORS["red"],
                    )
                    return
                self.hover_preview_key_var.set(key_name)
                self.hover_preview_status_label.configure(
                    text=f"Hover preview: {key_name}",
                    text_color=COLORS["green"],
                )
                self._register_hover_preview_binding()
                self._save_config()

            try:
                self.root.after(0, update_ui)
            except RuntimeError:
                pass

        def on_key(event):
            finish(str(event.name).lower())
            return False

        def on_mouse_event(event):
            event_type = getattr(event, "event_type", None)
            button = getattr(event, "button", None)
            if event_type == "down" and button:
                finish(_mouse_button_to_key_name(button))

        if HOTKEYS_AVAILABLE:
            try:
                hook_refs["keyboard"] = keyboard.on_press(on_key)
            except Exception:
                hook_refs["keyboard"] = None

        try:
            import mouse  # type: ignore[import-untyped]

            hook_refs["mouse"] = mouse.hook(on_mouse_event)
        except Exception:
            hook_refs["mouse"] = None

        if hook_refs["keyboard"] is None and hook_refs["mouse"] is None:
            self.hover_preview_record_btn.configure(
                text="Record Key",
                fg_color=COLORS["blurple"],
            )
            self.hover_preview_status_label.configure(
                text="Input hooks unavailable",
                text_color=COLORS["red"],
            )
            return

        hook_refs["timeout"] = self.root.after(5000, lambda: finish(None))

    def _unregister_hover_preview_binding(self):
        """Remove the current hover-preview keyboard/mouse hooks."""
        if self._hover_preview_keyboard_handle is not None and HOTKEYS_AVAILABLE:
            try:
                keyboard.remove_hotkey(self._hover_preview_keyboard_handle)
            except Exception:
                pass
        self._hover_preview_keyboard_handle = None
        self._hover_preview_keyboard_key = None

        if self._hover_preview_mouse_hook is not None:
            try:
                import mouse  # type: ignore[import-untyped]

                mouse.unhook(self._hover_preview_mouse_hook)
            except Exception:
                pass
        self._hover_preview_mouse_hook = None
        self._hover_preview_mouse_key = None

    def _register_hover_preview_binding(self):
        """Register the configured hover-preview binding."""
        self._unregister_hover_preview_binding()

        key = self.hover_preview_key_var.get().strip().lower()
        self.hover_preview_key_var.set(key)
        if not key:
            if hasattr(self, "hover_preview_status_label"):
                self.hover_preview_status_label.configure(
                    text="Hover preview disabled",
                    text_color=COLORS["text_muted"],
                )
            return

        def trigger_preview():
            try:
                self.root.after(0, self._preview_hovered_slot)
            except RuntimeError:
                pass

        if key.startswith("mouse"):
            self._hover_preview_mouse_key = key

            def on_mouse_event(event):
                event_type = getattr(event, "event_type", None)
                button = getattr(event, "button", None)
                if event_type != "down" or not button:
                    return
                if _mouse_button_to_key_name(button) == self._hover_preview_mouse_key:
                    trigger_preview()

            try:
                import mouse  # type: ignore[import-untyped]

                self._hover_preview_mouse_hook = mouse.hook(on_mouse_event)
                if hasattr(self, "hover_preview_status_label"):
                    self.hover_preview_status_label.configure(
                        text=f"Hover preview: {key}",
                        text_color=COLORS["green"],
                    )
            except Exception:
                self._hover_preview_mouse_hook = None
                if hasattr(self, "hover_preview_status_label"):
                    self.hover_preview_status_label.configure(
                        text="Mouse hook unavailable",
                        text_color=COLORS["red"],
                    )
            return

        if not HOTKEYS_AVAILABLE:
            if hasattr(self, "hover_preview_status_label"):
                self.hover_preview_status_label.configure(
                    text="Keyboard module unavailable",
                    text_color=COLORS["red"],
                )
            return

        try:
            self._hover_preview_keyboard_handle = keyboard.add_hotkey(key, trigger_preview)
            self._hover_preview_keyboard_key = key
            if hasattr(self, "hover_preview_status_label"):
                self.hover_preview_status_label.configure(
                    text=f"Hover preview: {key}",
                    text_color=COLORS["green"],
                )
        except Exception:
            self._hover_preview_keyboard_key = None
            if hasattr(self, "hover_preview_status_label"):
                self.hover_preview_status_label.configure(
                    text="Could not register binding",
                    text_color=COLORS["red"],
                )

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

    def _set_hovered_slot(self, tab_idx: int, slot_idx: int):
        """Track which slot is under the pointer for hover-preview binding."""
        self._hovered_slot = (tab_idx, slot_idx)

    def _clear_hovered_slot(self, tab_idx: int, slot_idx: int):
        """Clear the hover-preview target when the pointer leaves the slot."""
        if self._hovered_slot == (tab_idx, slot_idx):
            self._hovered_slot = None

    def _is_quick_popup_open(self) -> bool:
        popup = getattr(self, "_quick_popup", None)
        if popup is None:
            return False
        try:
            return bool(popup.winfo_exists())
        except Exception:
            return False

    def _preview_hovered_slot(self):
        """Preview the slot currently under the pointer via the hover binding."""
        if self._is_quick_popup_open():
            return

        now = time.time()
        if now - self._hover_preview_last_at < 0.15:
            return
        self._hover_preview_last_at = now

        target = self._hovered_slot
        if target is None:
            try:
                target = self._find_slot_at_position(
                    self.root.winfo_pointerx(),
                    self.root.winfo_pointery(),
                )
            except Exception:
                target = None
        if target is None:
            self.status_var.set("Hover a sound slot to preview")
            return

        tab_idx, slot_idx = target
        self._preview_slot_by_tab(tab_idx, slot_idx)

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
        if 0 <= tab_idx < len(self.tabs) and tab_idx != self.current_tab_idx:
            try:
                self._switch_tab(tab_idx)
            except Exception:
                pass
        self._on_slot_command(slot_idx)

    def _preview_slot_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab preview callback."""
        self._preview_slot_by_tab(tab_idx, slot_idx)

    def _configure_slot_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab configure callback.

        IMPORTANT: switch to `tab_idx` first if it isn't current. Otherwise
        `_configure_slot(slot_idx)` uses `_get_current_tab()` and ends up
        opening the configure dialog for the WRONG tab — typically an
        empty slot at the same index, which looks like "a new configure
        window for a new file randomly opened".
        """
        if 0 <= tab_idx < len(self.tabs) and tab_idx != self.current_tab_idx:
            try:
                self._switch_tab(tab_idx)
            except Exception:
                pass
        self._configure_slot(slot_idx)

    def _show_quick_popup_for_tab(self, event, tab_idx: int, slot_idx: int):
        """Per-tab quick popup callback."""
        if 0 <= tab_idx < len(self.tabs) and tab_idx != self.current_tab_idx:
            try:
                self._switch_tab(tab_idx)
            except Exception:
                pass
        self._show_quick_popup(event, slot_idx)

    def _stop_slot_with_flag_for_tab(self, tab_idx: int, slot_idx: int):
        """Per-tab stop callback."""
        if 0 <= tab_idx < len(self.tabs) and tab_idx != self.current_tab_idx:
            try:
                self._switch_tab(tab_idx)
            except Exception:
                pass
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

        if self._is_quick_popup_open():
            return

        if now - self._last_play_time < 0.15:
            return

        if now < getattr(self, "_suppress_slot_click_until", 0.0):
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
                    "loop": slot.loop,
                }
                # Change button color to playing state
                if slot_idx in self.slot_buttons:
                    self.slot_buttons[slot_idx].configure(fg_color=COLORS["playing"])
                # Set progress bar color for playing state
                if slot_idx in self.slot_progress:
                    self.slot_progress[slot_idx].configure(progress_color=COLORS["playing"])
                # Show stop button (on left side, before progress bar)
                self._show_stop_button(slot_idx)
                # Mirror onto the search-overlay slot if visible — without
                # this the filter view's slot keeps showing the resting color
                # and never reveals the stop button while the sound plays.
                if self._search_slot_widgets and (
                    self.current_tab_idx,
                    slot_idx,
                ) in self._search_slot_widgets:
                    try:
                        self._paint_search_slot(self.current_tab_idx, slot_idx)
                    except Exception:
                        pass
                # Auto-open the DJ Looper panel for looping sounds — gives
                # the user immediate visibility + a Stop button for runaway
                # loops they may have configured by accident.
                if slot.loop:
                    panel = getattr(self, "now_playing_panel", None)
                    if panel is not None and not panel.is_visible:
                        try:
                            panel.show()
                        except Exception:
                            pass
        else:
            self.status_var.set("Start the audio stream first!")

    def _preview_slot(self, slot_idx: int):
        """Preview a sound through default speakers (without streaming to Discord).

        Clicking preview again while already previewing this slot stops the preview.
        """
        self._preview_slot_by_tab(self.current_tab_idx, slot_idx)

    def _preview_slot_by_tab(self, tab_idx: int, slot_idx: int):
        """Preview a sound from a specific tab without switching tabs."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return

        # If this exact slot is already previewing, stop it.
        if slot_idx in self.preview_slots:
            preview_tab = self.preview_slots[slot_idx].get("tab_idx", self.current_tab_idx)
            if preview_tab == tab_idx:
                self._stop_preview(slot_idx)
                return
            self._stop_preview(slot_idx)

        tab = self.tabs[tab_idx]
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
                    "tab_idx": tab_idx,
                }
                # Change button color to preview state (green)
                if slot_idx in self.tab_slot_buttons.get(tab_idx, {}):
                    self.tab_slot_buttons[tab_idx][slot_idx].configure(fg_color=COLORS["preview"])
                # Set progress bar color for preview state
                if slot_idx in self.tab_slot_progress.get(tab_idx, {}):
                    self.tab_slot_progress[tab_idx][slot_idx].configure(
                        progress_color=COLORS["preview"]
                    )
                # Show stop button for preview
                if slot_idx in self.tab_slot_stop_buttons.get(tab_idx, {}):
                    self.tab_slot_stop_buttons[tab_idx][slot_idx].pack()
                if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
                    self._paint_search_slot(tab_idx, slot_idx)
        except Exception as e:
            self.status_var.set(f"Preview error: {e}")

    def _stop_preview(self, slot_idx: int):
        """Stop a specific preview sound and reset its UI state."""
        sd.stop()

        if slot_idx in self.preview_slots:
            tab_idx = self.preview_slots[slot_idx].get("tab_idx", self.current_tab_idx)
            del self.preview_slots[slot_idx]

            self._update_slot_button_for_tab(tab_idx, slot_idx)
            if slot_idx in self.tab_slot_progress.get(tab_idx, {}):
                self.tab_slot_progress[tab_idx][slot_idx].set(0)
            if slot_idx in self.tab_slot_stop_buttons.get(tab_idx, {}):
                self.tab_slot_stop_buttons[tab_idx][slot_idx].pack_forget()
            if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
                self._paint_search_slot(tab_idx, slot_idx)

        self.status_var.set("Preview stopped")

    def _stop_all_previews(self):
        """Stop all currently playing previews and reset their UI state."""
        sd.stop()

        for slot_idx in list(self.preview_slots.keys()):
            tab_idx = self.preview_slots[slot_idx].get("tab_idx", self.current_tab_idx)
            del self.preview_slots[slot_idx]

            self._update_slot_button_for_tab(tab_idx, slot_idx)
            if slot_idx in self.tab_slot_progress.get(tab_idx, {}):
                self.tab_slot_progress[tab_idx][slot_idx].set(0)
            if slot_idx in self.tab_slot_stop_buttons.get(tab_idx, {}):
                self.tab_slot_stop_buttons[tab_idx][slot_idx].pack_forget()
            if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
                self._paint_search_slot(tab_idx, slot_idx)

    def _show_quick_popup(self, event, slot_idx: int):
        """Show a quick popup for volume/speed adjustment next to the clicked slot."""
        tab = self._get_current_tab()

        # If slot is empty, open the full configure dialog instead
        if slot_idx not in tab.slots:
            self._configure_slot(slot_idx)
            return

        slot = tab.slots[slot_idx]

        old_popup = getattr(self, "_quick_popup", None)
        if old_popup is not None:
            try:
                if old_popup.winfo_exists():
                    old_popup.grab_release()
                    old_popup.destroy()
            except Exception:
                pass
            self._quick_popup = None

        # Create popup window positioned near the click
        popup = ctk.CTkToplevel(self.root)
        popup.title("Quick Edit")
        popup.overrideredirect(True)  # Remove window decorations
        popup.attributes("-topmost", True)
        popup.transient(self.root)
        self._quick_popup = popup

        def close_popup():
            if getattr(self, "_quick_popup", None) is popup:
                self._quick_popup = None
            self._suppress_slot_click_until = time.time() + 0.35
            try:
                popup.grab_release()
            except Exception:
                pass
            try:
                if popup.winfo_exists():
                    popup.destroy()
            except tk.TclError:
                pass

        def activate_popup():
            try:
                popup.grab_set()
                popup.focus_force()
            except tk.TclError:
                pass

        popup.after(10, activate_popup)

        # Position popup near the click location
        x = event.x_root + 10
        y = event.y_root + 10
        popup.geometry(f"380x320+{x}+{y}")

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
        primary_btn_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        primary_btn_row.pack(fill=tk.X)
        secondary_btn_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        secondary_btn_row.pack(fill=tk.X, pady=(6, 0))

        def apply_changes():
            """Apply the volume/speed/pitch/loop changes."""
            slot.volume = volume_var.get() / 100.0
            slot.speed = speed_var.get() / 100.0
            slot.preserve_pitch = preserve_pitch_var.get()
            slot.loop = loop_var.get()
            self._save_config()
            self._update_slot_button_for_tab(self.current_tab_idx, slot_idx)
            close_popup()

        def open_full_edit():
            """Open the full configure dialog."""
            close_popup()
            self._configure_slot(slot_idx)

        def clone_for_retrim():
            """Clone slot to next empty slot, opening editor for a new cut."""
            close_popup()
            self._clone_slot_for_retrim(slot_idx)

        def delete_sound():
            """Delete the sound with confirmation."""
            close_popup()
            if messagebox.askyesno(
                "Delete Sound",
                f"Are you sure you want to delete '{slot.name}'?",
                icon="warning",
            ):
                self._delete_slot(slot_idx)

        def open_file_location():
            """Open the folder containing this sound and select the file."""
            try:
                fp = slot.file_path
                if not fp or not os.path.exists(fp):
                    messagebox.showwarning("Open Location", "File not found on disk.")
                    return
                # Use explorer /select to highlight the file
                subprocess.run(["explorer", "/select,", str(Path(fp).absolute())])
            except Exception as e:
                messagebox.showerror("Open Location", f"Could not open location:\n{e}")

        def copy_file_to_folder():
            """Copy this sound file to a user-chosen folder."""
            try:
                fp = slot.file_path
                if not fp or not os.path.exists(fp):
                    messagebox.showwarning("Copy File", "File not found on disk.")
                    return
                dest_dir = filedialog.askdirectory(title="Choose destination folder")
                if not dest_dir:
                    return
                dest_path = Path(dest_dir) / Path(fp).name
                shutil.copy2(fp, str(dest_path))
                messagebox.showinfo("Copy File", f"Copied to: {dest_path}")
            except Exception as e:
                messagebox.showerror("Copy File", f"Could not copy file:\n{e}")

        ctk.CTkButton(
            primary_btn_row,
            text="Apply",
            command=apply_changes,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            secondary_btn_row,
            text="Open Location",
            command=open_file_location,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=110,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            secondary_btn_row,
            text="Copy File",
            command=copy_file_to_folder,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=90,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            primary_btn_row,
            text="Edit",
            command=open_full_edit,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            primary_btn_row,
            text="📋 Clone",
            command=clone_for_retrim,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=70,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            primary_btn_row,
            text="🗑️",
            command=delete_sound,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=32,
        ).pack(side=tk.LEFT, padx=2)

        ctk.CTkButton(
            primary_btn_row,
            text="✕",
            command=close_popup,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=32,
        ).pack(side=tk.RIGHT, padx=2)

        popup.bind("<Escape>", lambda _e: close_popup())

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

        # Multi-cut: if the user used the Multi-Cut workflow, create one new
        # slot per captured segment and skip the single-result path entirely.
        multi_results = getattr(editor, "multi_results", None) or []
        if multi_results:
            base_name = original.name or Path(source_for_editor).stem
            existing_names = {s.name for s in tab.slots.values()}

            def _next_unique_name(candidate: str) -> str:
                if candidate not in existing_names:
                    existing_names.add(candidate)
                    return candidate
                k = 2
                while f"{candidate} ({k})" in existing_names:
                    k += 1
                final = f"{candidate} ({k})"
                existing_names.add(final)
                return final

            created_any = False
            for i, item in enumerate(multi_results, start=1):
                audio_data, sample_rate, title = _unpack_multicut_result(
                    item,
                    f"{base_name} {i}",
                )
                try:
                    new_file_path = self.sound_cache.add_sound_data(
                        audio_data,
                        sample_rate,
                        f"{title}.wav",
                    )
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to save cut {i}:\n{e}")
                    continue

                # Find next empty slot
                new_slot_idx = 0
                while new_slot_idx in tab.slots:
                    new_slot_idx += 1

                tab.slots[new_slot_idx] = SoundSlot(
                    name=_next_unique_name(title),
                    file_path=new_file_path,
                    hotkey=None,
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
                created_any = True

            if created_any:
                self._save_config()
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
        existing = tab.slots.get(slot_idx)

        dialog = ctk.CTkToplevel(self.root)
        title_suffix = f" — {existing.name}" if existing and existing.name else ""
        dialog.title(f"Configure Slot {slot_idx + 1}{title_suffix}")
        dialog.geometry("640x780")
        dialog.minsize(560, 600)
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.after(10, lambda: dialog.focus_force())

        # Layout: header (top) + scrollable body (middle) + sticky footer (bottom)
        dialog.grid_rowconfigure(1, weight=1)
        dialog.grid_columnconfigure(0, weight=1)

        # ---- Header --------------------------------------------------------
        header = ctk.CTkFrame(dialog, fg_color=COLORS["bg_medium"], corner_radius=0, height=56)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        # Color swatch reflecting the slot's color (or default blurple).
        header_color = (existing.color if existing and existing.color else COLORS["blurple"])
        accent = ctk.CTkFrame(header, fg_color=header_color, corner_radius=4, width=8)
        accent.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(16, 12), pady=10)

        ctk.CTkLabel(
            header,
            text=f"Slot {slot_idx + 1}",
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_lg"], weight="bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="sw", pady=(8, 0))
        subtitle_text = existing.name if existing and existing.name else "Empty slot"
        ctk.CTkLabel(
            header,
            text=subtitle_text,
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            anchor="w",
        ).grid(row=1, column=1, sticky="nw", pady=(0, 8))

        # ---- Scrollable body ----------------------------------------------
        body = ctk.CTkScrollableFrame(
            dialog, fg_color=COLORS["bg_dark"], corner_radius=0
        )
        body.grid(row=1, column=0, sticky="nsew", padx=14, pady=(10, 6))
        body.grid_columnconfigure(0, weight=1)

        # State for edited audio
        edited_audio_data = {
            "data": None,
            "sample_rate": None,
            "original_name": None,
            "title": None,
        }

        # ---- Card builder --------------------------------------------------
        def _card(title: str) -> ctk.CTkFrame:
            """Create a titled card section inside the scrollable body."""
            card = ctk.CTkFrame(
                body, fg_color=COLORS["bg_medium"], corner_radius=8
            )
            card.pack(fill=tk.X, expand=False, pady=(0, 10))
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                card,
                text=title,
                text_color=COLORS["text_secondary"],
                font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"], weight="bold"),
                anchor="w",
            ).grid(row=0, column=0, columnspan=4, sticky="ew", padx=14, pady=(10, 6))
            return card

        def _row_label(card: ctk.CTkFrame, text: str, row: int) -> None:
            ctk.CTkLabel(
                card,
                text=text,
                text_color=COLORS["text_primary"],
                font=self._font_sm,
                anchor="w",
                width=88,
            ).grid(row=row, column=0, sticky="w", padx=(14, 8), pady=6)

        # ====================================================================
        # CARD 1 — Sound source
        # ====================================================================
        sound_card = _card("🔊  SOUND SOURCE")

        _row_label(sound_card, "Name", 1)
        name_var = tk.StringVar(value=existing.name if existing else "")
        name_entry = ctk.CTkEntry(
            sound_card,
            textvariable=name_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            placeholder_text="Display name (defaults to file name)",
        )
        name_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 14), pady=6)
        _bind_rtl_entry(name_entry, name_var)

        _row_label(sound_card, "File", 2)
        path_var = tk.StringVar(value=existing.file_path if existing else "")
        path_entry = ctk.CTkEntry(
            sound_card,
            textvariable=path_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            placeholder_text="Pick a sound file…",
        )
        path_entry.grid(row=2, column=1, sticky="ew", padx=(0, 6), pady=6)
        _bind_rtl_entry(path_entry, path_var)

        # File action buttons (Browse + Edit) sit on the same row as the entry.
        path_btns = ctk.CTkFrame(sound_card, fg_color="transparent")
        path_btns.grid(row=2, column=2, columnspan=2, sticky="e", padx=(0, 14))

        def browse():
            filetypes = [("Audio", " ".join(SUPPORTED_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                path_var.set(fp)
                if not name_var.get():
                    name_var.set(Path(fp).stem)
                self._open_sound_editor(fp, edited_audio_data, edit_status_var, dialog, name_var)

        def edit_current():
            current_path = path_var.get()
            source_path: Optional[str] = None
            if existing and existing.source_file_path:
                try:
                    if os.path.isfile(existing.source_file_path):
                        source_path = existing.source_file_path
                except Exception:
                    source_path = None
            if source_path is None and current_path and os.path.exists(current_path):
                source_path = current_path
            if source_path is None:
                messagebox.showwarning("No File", "Please select a sound file first.")
                return
            self._open_sound_editor(source_path, edited_audio_data, edit_status_var, dialog, name_var)

        ctk.CTkButton(
            path_btns,
            text="Browse",
            command=browse,
            width=72,
            height=28,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=self._font_sm,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ctk.CTkButton(
            path_btns,
            text="✂ Edit",
            command=edit_current,
            width=72,
            height=28,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_sm,
        ).pack(side=tk.LEFT)

        # Edit status label (shown when audio has been trimmed in this session)
        edit_status_var = tk.StringVar(value="")
        edit_status_label = ctk.CTkLabel(
            sound_card,
            textvariable=edit_status_var,
            text_color=COLORS["green"],
            font=self._font_xs,
            anchor="w",
        )
        edit_status_label.grid(row=3, column=1, columnspan=3, sticky="w", padx=(0, 14), pady=(0, 10))

        # ====================================================================
        # CARD 2 — Appearance (emoji, image, color)
        # ====================================================================
        appearance_card = _card("🎨  APPEARANCE")

        _row_label(appearance_card, "Emoji", 1)
        emoji_var = tk.StringVar(value=existing.emoji if existing and existing.emoji else "")
        emoji_entry = ctk.CTkEntry(
            appearance_card,
            textvariable=emoji_var,
            width=80,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
        )
        emoji_entry.grid(row=1, column=1, sticky="w", padx=(0, 6), pady=6)

        def pick_emoji():
            self._show_emoji_picker(emoji_var, dialog)

        ctk.CTkButton(
            appearance_card,
            text="Choose Emoji",
            command=pick_emoji,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=120,
            height=28,
            font=self._font_sm,
        ).grid(row=1, column=2, columnspan=2, sticky="w", padx=(0, 14), pady=6)

        _row_label(appearance_card, "Image", 2)
        image_var = tk.StringVar(
            value=existing.image_path if existing and existing.image_path else ""
        )
        image_entry = ctk.CTkEntry(
            appearance_card,
            textvariable=image_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            placeholder_text="Optional thumbnail (drag-and-drop also works)",
        )
        image_entry.grid(row=2, column=1, sticky="ew", padx=(0, 6), pady=6)

        def browse_image():
            filetypes = [("Images", " ".join(SUPPORTED_IMAGE_FORMATS))]
            fp = filedialog.askopenfilename(filetypes=filetypes)
            if fp:
                local_path = self._copy_image_to_storage(fp)
                image_var.set(local_path)

        image_btns = ctk.CTkFrame(appearance_card, fg_color="transparent")
        image_btns.grid(row=2, column=2, columnspan=2, sticky="e", padx=(0, 14))
        ctk.CTkButton(
            image_btns,
            text="Browse",
            command=browse_image,
            width=72,
            height=28,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_sm,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ctk.CTkButton(
            image_btns,
            text="Clear",
            command=lambda: image_var.set(""),
            width=60,
            height=28,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_sm,
        ).pack(side=tk.LEFT)

        # Color picker — visual swatch palette
        _row_label(appearance_card, "Color", 3)
        color_names = list(ALL_SLOT_COLORS.keys())
        existing_color_name = "Default"
        if existing and existing.color:
            for name, hex_val in ALL_SLOT_COLORS.items():
                if hex_val.lower() == existing.color.lower():
                    existing_color_name = name
                    break
        color_var = tk.StringVar(value=existing_color_name)

        palette_outer = ctk.CTkFrame(
            appearance_card, fg_color=COLORS["bg_dark"], corner_radius=6
        )
        palette_outer.grid(
            row=3, column=1, columnspan=3, sticky="ew", padx=(0, 14), pady=(6, 12)
        )
        selected_lbl = ctk.CTkLabel(
            palette_outer,
            text=existing_color_name,
            text_color=COLORS["text_secondary"],
            font=self._font_xs,
            anchor="w",
        )
        selected_lbl.pack(fill=tk.X, padx=10, pady=(8, 4))
        palette_grid = ctk.CTkFrame(palette_outer, fg_color="transparent")
        palette_grid.pack(fill=tk.X, padx=8, pady=(0, 8))

        SWATCH_SIZE = 26
        SWATCH_PER_ROW = 8
        swatches: Dict[str, tk.Canvas] = {}

        def _select_color(name: str):
            color_var.set(name)

        def _draw_swatch(canvas: tk.Canvas, fill: str, selected: bool):
            canvas.delete("all")
            w = SWATCH_SIZE
            if selected:
                canvas.create_rectangle(
                    0, 0, w - 1, w - 1,
                    outline=COLORS["text_primary"], width=2, fill="",
                )
                canvas.create_rectangle(3, 3, w - 4, w - 4, outline="", fill=fill)
            else:
                canvas.create_rectangle(
                    1, 1, w - 2, w - 2,
                    outline=COLORS["bg_light"], width=1, fill=fill,
                )

        for i, name in enumerate(color_names):
            r, c = divmod(i, SWATCH_PER_ROW)
            hex_val = ALL_SLOT_COLORS[name]
            cv = tk.Canvas(
                palette_grid,
                width=SWATCH_SIZE,
                height=SWATCH_SIZE,
                bg=COLORS["bg_dark"],
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            cv.grid(row=r, column=c, padx=2, pady=2)
            _draw_swatch(cv, hex_val, name == existing_color_name)
            cv.bind("<Button-1>", lambda _e, n=name: _select_color(n))
            try:
                _Tooltip.attach(cv, name)
            except Exception:
                pass
            swatches[name] = cv

        def update_color_preview(*_args):
            selected = color_var.get()
            if selected not in ALL_SLOT_COLORS:
                return
            selected_lbl.configure(text=selected)
            try:
                accent.configure(fg_color=ALL_SLOT_COLORS[selected])
            except Exception:
                pass
            for name, cv in swatches.items():
                _draw_swatch(cv, ALL_SLOT_COLORS[name], name == selected)

        color_var.trace("w", update_color_preview)

        # ====================================================================
        # CARD 3 — Playback (volume, speed, hotkey)
        # ====================================================================
        playback_card = _card("🎚️  PLAYBACK")

        _row_label(playback_card, "Volume", 1)
        volume_var = tk.DoubleVar(value=(existing.volume * 100) if existing else 100)
        vol_value_lbl = ctk.CTkLabel(
            playback_card, text="100%", width=48,
            text_color=COLORS["text_primary"], font=self._font_sm, anchor="e",
        )
        vol_value_lbl.grid(row=1, column=2, sticky="e", padx=(0, 14), pady=6)
        ctk.CTkSlider(
            playback_card,
            from_=0, to=150,
            variable=volume_var,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=6)
        volume_var.trace("w", lambda *_: vol_value_lbl.configure(text=f"{int(volume_var.get())}%"))
        vol_value_lbl.configure(text=f"{int(volume_var.get())}%")

        _row_label(playback_card, "Speed", 2)
        speed_var = tk.DoubleVar(value=(existing.speed * 100) if existing else 100)
        speed_label = ctk.CTkLabel(
            playback_card, text="100%", width=48,
            text_color=COLORS["text_primary"], font=self._font_sm, anchor="e",
        )
        speed_label.grid(row=2, column=2, sticky="e", padx=(0, 14), pady=6)
        ctk.CTkSlider(
            playback_card,
            from_=50, to=200,
            variable=speed_var,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).grid(row=2, column=1, sticky="ew", padx=(0, 8), pady=6)

        def update_speed_label(*args):
            speed_label.configure(text=f"{int(speed_var.get())}%")

        speed_var.trace("w", update_speed_label)
        update_speed_label()

        _row_label(playback_card, "Hotkey", 3)
        hotkey_var = tk.StringVar(value=existing.hotkey if existing and existing.hotkey else "")
        ctk.CTkEntry(
            playback_card,
            textvariable=hotkey_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            placeholder_text="e.g. ctrl+1, F5, alt+m",
        ).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(0, 14), pady=(6, 12))

        # ====================================================================
        # CARD 4 — Groups
        # ====================================================================
        groups_card = _card("🏷️  GROUPS")

        existing_groups = existing.groups if existing else []
        group_check_vars: Dict[str, tk.BooleanVar] = {}
        group_checks_frame = ctk.CTkFrame(groups_card, fg_color="transparent")
        group_checks_frame.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 6))

        def _rebuild_group_checks():
            for w in group_checks_frame.winfo_children():
                w.destroy()
            group_check_vars.clear()
            current_all = self._get_all_groups()
            if not current_all:
                ctk.CTkLabel(
                    group_checks_frame,
                    text="No groups defined yet — create one with Manage Groups below.",
                    text_color=COLORS["text_muted"],
                    font=self._font_xs,
                    anchor="w",
                ).pack(fill=tk.X, pady=4)
                return
            col, row_g = 0, 0
            for g in current_all:
                var = tk.BooleanVar(value=g in existing_groups)
                group_check_vars[g] = var
                cb = ctk.CTkCheckBox(
                    group_checks_frame,
                    text=g,
                    variable=var,
                    fg_color=COLORS["blurple"],
                    hover_color=COLORS["blurple_hover"],
                    font=self._font_sm,
                    height=22,
                    checkbox_width=16,
                    checkbox_height=16,
                )
                cb.grid(row=row_g, column=col, sticky="w", padx=(0, 14), pady=2)
                col += 1
                if col >= 3:
                    col = 0
                    row_g += 1

        _rebuild_group_checks()

        def _open_manage_groups():
            nonlocal existing_groups
            existing_groups = [g for g, v in group_check_vars.items() if v.get()]
            self._show_manage_groups_dialog(rebuild_callback=_rebuild_group_checks)
            for g in existing_groups:
                if g in group_check_vars:
                    group_check_vars[g].set(True)

        ctk.CTkButton(
            groups_card,
            text="⚙ Manage Groups",
            command=_open_manage_groups,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=self._font_sm,
            corner_radius=4,
            width=140,
            height=28,
        ).grid(row=2, column=0, columnspan=4, sticky="w", padx=14, pady=(2, 12))

        # ====================================================================
        # CARD 5 — Looping
        # ====================================================================
        loop_card = _card("🔁  LOOPING")

        loop_var = tk.BooleanVar(value=existing.loop if existing else False)
        ctk.CTkCheckBox(
            loop_card,
            text="Enable looping",
            variable=loop_var,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            font=self._font_sm,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 6))

        ctk.CTkLabel(
            loop_card, text="Count", text_color=COLORS["text_primary"],
            font=self._font_sm, width=88, anchor="w",
        ).grid(row=2, column=0, sticky="w", padx=(14, 8), pady=6)
        loop_count_var = tk.IntVar(value=existing.loop_count if existing else 0)
        loop_count_row = ctk.CTkFrame(loop_card, fg_color="transparent")
        loop_count_row.grid(row=2, column=1, columnspan=3, sticky="w", pady=6)
        ctk.CTkEntry(
            loop_count_row,
            textvariable=loop_count_var,
            width=72,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
        ).pack(side=tk.LEFT)
        ctk.CTkLabel(
            loop_count_row, text="  0 = play forever",
            text_color=COLORS["text_muted"], font=self._font_xs,
        ).pack(side=tk.LEFT, padx=8)

        ctk.CTkLabel(
            loop_card, text="Delay", text_color=COLORS["text_primary"],
            font=self._font_sm, width=88, anchor="w",
        ).grid(row=3, column=0, sticky="w", padx=(14, 8), pady=(6, 12))
        loop_delay_var = tk.DoubleVar(value=existing.loop_delay if existing else 0.0)
        delay_label = ctk.CTkLabel(
            loop_card, text="0.0s", width=48,
            text_color=COLORS["text_primary"], font=self._font_sm, anchor="e",
        )
        delay_label.grid(row=3, column=2, sticky="e", padx=(0, 14), pady=(6, 12))
        ctk.CTkSlider(
            loop_card,
            from_=0, to=5,
            variable=loop_delay_var,
            fg_color=COLORS["bg_dark"],
            progress_color=COLORS["blurple"],
            button_color=COLORS["blurple"],
        ).grid(row=3, column=1, sticky="ew", padx=(0, 8), pady=(6, 12))

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
                        edited_audio_data.get("title")
                        or edited_audio_data["original_name"]
                        or Path(source_path).name,
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
                        preload_now = not self._is_long_audio_file(source_path)
                        local_path = self.sound_cache.add_sound(source_path, preload=preload_now)
                        if not preload_now:
                            self._preload_sound_in_background(local_path)
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
                if (
                    existing
                    and existing.source_file_path
                    and os.path.isfile(existing.source_file_path)
                ):
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
                            new_source_file_path = self.sound_cache.add_sound(
                                source_path,
                                preload=False,
                            )
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

        # ---- Sticky footer (always visible regardless of scroll) -----------
        footer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_medium"], corner_radius=0, height=64)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        footer.grid_columnconfigure(0, weight=1)

        btn_frame = ctk.CTkFrame(footer, fg_color="transparent")
        btn_frame.grid(row=0, column=0, sticky="e", padx=18, pady=12)

        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=100,
            height=36,
            font=self._font_sm,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="🗑 Clear",
            command=clear,
            fg_color=COLORS["red"],
            hover_color=COLORS["red_hover"],
            width=110,
            height=36,
            font=self._font_sm,
        ).pack(side=tk.LEFT, padx=(0, 8))
        ctk.CTkButton(
            btn_frame,
            text="✓ Save",
            command=save,
            fg_color=COLORS["green"],
            hover_color=COLORS["green_hover"],
            width=120,
            height=36,
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
        ).pack(side=tk.LEFT)

    def _clear_slot_image(self, tab_idx: int, slot_idx: int):
        """Remove the custom image from a slot (the file on disk is left alone)."""
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return
        slot = self.tabs[tab_idx].slots.get(slot_idx)
        if not slot or not slot.image_path:
            return
        slot.image_path = None
        self._update_slot_button_for_tab(tab_idx, slot_idx)
        if tab_idx == self.current_tab_idx:
            self._update_slot_button(slot_idx)
        self._save_config()
        self.status_var.set(f"Image cleared for: {slot.name}")

    def _apply_clipboard_image_to_slot(self, tab_idx: int, slot_idx: int,
                                       show_errors: bool = False) -> bool:
        """Read the clipboard and apply any image found to the given slot.

        Returns True on success. If `show_errors` is True (menu invocation)
        a messagebox is shown explaining why nothing happened; otherwise
        we just update the status bar (silent paths used by Ctrl+V).
        """
        if tab_idx < 0 or tab_idx >= len(self.tabs):
            return False
        slot = self.tabs[tab_idx].slots.get(slot_idx)
        if not slot:
            if show_errors:
                messagebox.showinfo("Paste Image", "This slot is empty — add a sound first.")
            return False

        try:
            from PIL import ImageGrab
            img = ImageGrab.grabclipboard()
        except Exception as e:
            if show_errors:
                messagebox.showerror("Paste Image", f"Could not read clipboard:\n{e}")
            return False

        if img is None:
            if show_errors:
                messagebox.showinfo(
                    "Paste Image",
                    "No image on the clipboard.\n\n"
                    "Copy a screenshot (Win+Shift+S) or an image file in "
                    "Explorer first, then try again.",
                )
            else:
                self.status_var.set("No image on the clipboard")
            return False

        local_path: Optional[str] = None

        # Case 1: clipboard contains file path(s) (e.g. copied from Explorer)
        if isinstance(img, list):
            image_exts = {".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".ico", ".webp"}
            for f in img:
                p = str(f)
                if Path(p).suffix.lower() in image_exts and os.path.isfile(p):
                    local_path = self._copy_image_to_storage(p)
                    break
            if local_path is None:
                if show_errors:
                    messagebox.showinfo(
                        "Paste Image",
                        "Clipboard has files but none of them are images.",
                    )
                return False
        else:
            # Case 2: raw bitmap (screenshot, browser image-copy, etc.)
            try:
                Path(IMAGES_DIR).mkdir(exist_ok=True)
                try:
                    raw = img.tobytes()
                    file_hash = hashlib.md5(raw[:4096]).hexdigest()[:8]
                except Exception:
                    file_hash = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
                local_path = str(Path(IMAGES_DIR) / f"clipboard_{file_hash}.png")
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                img.save(local_path, "PNG")
            except Exception as e:
                msg = f"Failed to save clipboard image:\n{e}"
                if show_errors:
                    messagebox.showerror("Paste Image", msg)
                else:
                    self.status_var.set(f"Failed to paste image: {e}")
                return False

        slot.image_path = local_path
        self._update_slot_button_for_tab(tab_idx, slot_idx)
        if tab_idx == self.current_tab_idx:
            self._update_slot_button(slot_idx)
        self._save_config()
        self.status_var.set(f"Image set for: {slot.name}")
        return True

    def _on_paste_image_to_slot(self, event=None):
        """Ctrl+V: paste a clipboard image onto the slot under the cursor.

        Ignored when the focus is on a text entry (so normal paste still
        works). Uses PIL.ImageGrab to read the clipboard bitmap; if no
        image is on the clipboard this is a no-op.
        """
        # Don't hijack paste when typing into an entry / textbox
        try:
            focused = self.root.focus_get()
            if focused is not None:
                cls = focused.winfo_class()
                if cls in ("Entry", "Text", "TEntry", "TCombobox", "CTkEntry"):
                    return None
                if hasattr(focused, "master") and focused.master is not None:
                    mcls = focused.master.winfo_class()
                    if "Entry" in mcls or "Combobox" in mcls:
                        return None
        except Exception:
            pass

        try:
            cursor_x = self.root.winfo_pointerx()
            cursor_y = self.root.winfo_pointery()
        except Exception:
            return None
        target = self._find_slot_at_position(cursor_x, cursor_y)
        if target is None:
            self.status_var.set("Hover a sound slot to paste an image")
            return None
        tab_idx, slot_idx = target
        if self._apply_clipboard_image_to_slot(tab_idx, slot_idx, show_errors=False):
            return "break"
        return None

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

    def _create_extra_slots_from_multicut(self, segments, source_name: str):
        """Create new slots in the current tab for extra Multi-Cut segments.

        Used by the configure-dialog edit flow: the FIRST cut is loaded into
        the dialog being edited; segments 2..N are saved here as brand-new
        empty slots so the user gets all cuts in one shot.
        """
        if not segments:
            return
        tab = self._get_current_tab()
        existing_names = {s.name for s in tab.slots.values()}
        base_name = Path(source_name).stem

        def _next_unique_name(candidate: str) -> str:
            if candidate not in existing_names:
                existing_names.add(candidate)
                return candidate
            k = 2
            while f"{candidate} ({k})" in existing_names:
                k += 1
            final = f"{candidate} ({k})"
            existing_names.add(final)
            return final

        created_any = False
        for i, item in enumerate(segments, start=2):
            audio_data, sample_rate, title = _unpack_multicut_result(
                item,
                f"{base_name} {i}",
            )
            try:
                new_file_path = self.sound_cache.add_sound_data(
                    audio_data,
                    sample_rate,
                    f"{title}.wav",
                )
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save cut {i}:\n{e}")
                continue

            new_slot_idx = 0
            while new_slot_idx in tab.slots:
                new_slot_idx += 1

            tab.slots[new_slot_idx] = SoundSlot(
                name=_next_unique_name(title),
                file_path=new_file_path,
            )
            self._ensure_slots_for_tab(self.current_tab_idx)
            self._update_slot_button_for_tab(self.current_tab_idx, new_slot_idx)
            created_any = True

        if created_any:
            self._save_config()

    def _open_prepared_sound_editor(
        self,
        file_path: str,
        edited_audio_data: dict,
        status_var: tk.StringVar,
        parent_dialog: tk.Toplevel,
        name_var: Optional[tk.StringVar],
        preloaded_audio: Tuple[Any, int],
    ):
        """Open the editor using audio decoded on a worker thread."""
        try:
            editor = SoundEditor(
                self.root,
                file_path,
                output_device=None,
                preloaded_audio=preloaded_audio,
            )
            result = editor.show()

            multi_results = getattr(editor, "multi_results", None) or []
            if multi_results:
                base_title = Path(file_path).stem
                first_audio, first_sr, first_title = _unpack_multicut_result(
                    multi_results[0],
                    f"{base_title} 1",
                )
                edited_audio_data["data"] = first_audio
                edited_audio_data["sample_rate"] = first_sr
                edited_audio_data["original_name"] = Path(file_path).name
                edited_audio_data["title"] = first_title
                if name_var is not None:
                    name_var.set(first_title)
                duration = len(first_audio) / first_sr
                status_var.set(
                    f"Multi-Cut: cut 1 of {len(multi_results)} loaded ({duration:.2f}s)"
                )

                if len(multi_results) > 1:
                    self._create_extra_slots_from_multicut(
                        multi_results[1:],
                        Path(file_path).name,
                    )
                return

            if result is not None:
                audio_data, sample_rate = result
                edited_audio_data["data"] = audio_data
                edited_audio_data["sample_rate"] = sample_rate
                edited_audio_data["original_name"] = Path(file_path).name
                edited_audio_data["title"] = None
                duration = len(audio_data) / sample_rate
                status_var.set(f"Edited ({duration:.2f}s)")
            else:
                status_var.set("")
        except Exception as e:
            messagebox.showerror("Editor Error", f"Failed to open sound editor:\n{e}")

    def _open_sound_editor(
        self,
        file_path: str,
        edited_audio_data: dict,
        status_var: tk.StringVar,
        parent_dialog: tk.Toplevel,
        name_var: Optional[tk.StringVar] = None,
    ):
        """Open the sound editor dialog for a file."""
        if self._is_long_audio_file(file_path):
            job_key = os.path.abspath(file_path)
            if job_key in self._editor_prepare_jobs:
                status_var.set("Preparing audio...")
                return
            self._editor_prepare_jobs.add(job_key)
            status_var.set("Preparing long audio...")

            def _worker():
                prepared_audio: Optional[Tuple[Any, int]] = None
                error: Optional[Exception] = None
                try:
                    prepared_audio = SoundEditor.prepare_audio(file_path)
                except Exception as exc:
                    error = exc

                def _done():
                    self._editor_prepare_jobs.discard(job_key)
                    try:
                        if not parent_dialog.winfo_exists():
                            return
                    except Exception:
                        return
                    if error is not None or prepared_audio is None:
                        status_var.set("")
                        messagebox.showerror(
                            "Editor Error",
                            f"Failed to prepare audio:\n{error}",
                        )
                        return
                    status_var.set("")
                    self._open_prepared_sound_editor(
                        file_path,
                        edited_audio_data,
                        status_var,
                        parent_dialog,
                        name_var,
                        prepared_audio,
                    )

                try:
                    self.root.after(0, _done)
                except Exception:
                    pass

            threading.Thread(target=_worker, name="SoundEditorPrepare", daemon=True).start()
            return

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

            # Multi-cut: if the user used Multi-Cut, the FIRST captured segment
            # becomes the "edited audio" for the slot being configured (so the
            # configure dialog's normal Save flow handles it), and the
            # REMAINING segments are auto-saved as brand-new slots in the
            # current tab.
            multi_results = getattr(editor, "multi_results", None) or []
            if multi_results:
                base_title = Path(file_path).stem
                first_audio, first_sr, first_title = _unpack_multicut_result(
                    multi_results[0],
                    f"{base_title} 1",
                )
                edited_audio_data["data"] = first_audio
                edited_audio_data["sample_rate"] = first_sr
                edited_audio_data["original_name"] = Path(file_path).name
                edited_audio_data["title"] = first_title
                if name_var is not None:
                    name_var.set(first_title)
                duration = len(first_audio) / first_sr
                status_var.set(
                    f"✓ Multi-Cut: cut 1 of {len(multi_results)} loaded ({duration:.2f}s)"
                )

                if len(multi_results) > 1:
                    self._create_extra_slots_from_multicut(
                        multi_results[1:],
                        Path(file_path).name,
                    )
                return

            if result is not None:
                audio_data, sample_rate = result
                edited_audio_data["data"] = audio_data
                edited_audio_data["sample_rate"] = sample_rate
                edited_audio_data["original_name"] = Path(file_path).name
                edited_audio_data["title"] = None

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

        # Refresh the group footer label below the slot.
        group_lbl = self.tab_slot_group_labels.get(tab_idx, {}).get(slot_idx)
        if group_lbl is not None:
            slot_obj = tab.slots.get(slot_idx)
            groups = list(slot_obj.groups) if (slot_obj and slot_obj.groups) else []
            footer_text = ", ".join(groups) if groups else ""
            try:
                if group_lbl.cget("text") != footer_text:
                    group_lbl.configure(text=footer_text)
            except Exception:
                pass

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

        # Mirror the change onto the search-overlay slot if the search/filter
        # view is currently showing this slot — keeps the playing tint, image,
        # name etc. in sync across both views with a single call.
        if self._search_slot_widgets and (tab_idx, slot_idx) in self._search_slot_widgets:
            self._paint_search_slot(tab_idx, slot_idx)

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
            display_text = _format_slot_display_text(slot.name, slot.hotkey, slot.loop)

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
                font=self._font_slot,
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
                    # Auto-open the DJ Looper panel for looping sounds so
                    # the user can always see + stop runaway loops.
                    if slot.loop:
                        panel = getattr(self, "now_playing_panel", None)
                        if panel is not None and not panel.is_visible:
                            try:
                                panel.show()
                            except Exception:
                                pass
                    # Mirror onto search-overlay slot — works regardless of
                    # which tab the sound actually lives on.
                    if self._search_slot_widgets and (
                        tab_idx,
                        slot_idx,
                    ) in self._search_slot_widgets:
                        try:
                            self._paint_search_slot(tab_idx, slot_idx)
                        except Exception:
                            pass

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
        # Try to auto-fill from clipboard if it looks like a URL
        prefill = ""
        try:
            clip = self.root.clipboard_get()
            if isinstance(clip, str):
                s = clip.strip()
                if s.startswith("http://") or s.startswith("https://"):
                    prefill = s
        except Exception:
            pass

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Download Web Audio")
        dialog.geometry("620x520")
        dialog.minsize(560, 480)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.after(10, lambda: dialog.focus_force())

        # Outer container
        outer = ctk.CTkFrame(dialog, fg_color=COLORS["bg_dark"], corner_radius=0)
        outer.pack(fill=tk.BOTH, expand=True, padx=22, pady=20)

        # Header
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill=tk.X, pady=(0, 14))
        ctk.CTkLabel(
            header,
            text="▶  Download Web Audio",
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family=FONTS["family"], size=16, weight="bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text=(
                "YouTube, Vimeo, Twitter, TikTok, SoundCloud, Facebook + ~1000 other sites.\n"
                "If the page has several videos you'll get to pick which ones to grab."
            ),
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(family=FONTS["family"], size=11),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))

        def _section_card(parent, title: str) -> ctk.CTkFrame:
            card = ctk.CTkFrame(parent, fg_color=COLORS["bg_medium"], corner_radius=8)
            card.pack(fill=tk.X, pady=(0, 10))
            ctk.CTkLabel(
                card,
                text=title,
                text_color=COLORS["text_secondary"],
                font=ctk.CTkFont(family=FONTS["family"], size=11, weight="bold"),
                anchor="w",
            ).pack(anchor="w", padx=14, pady=(10, 4))
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill=tk.X, padx=14, pady=(0, 12))
            return inner

        # --- URL card ---
        url_card = _section_card(outer, "PAGE URL")
        url_var = tk.StringVar(value=prefill)
        url_entry = ctk.CTkEntry(
            url_card,
            textvariable=url_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            height=36,
            placeholder_text="https://vimeo.com/...   |   https://youtube.com/watch?v=...",
        )
        url_entry.pack(fill=tk.X)
        _bind_clipboard_shortcuts(url_entry)
        url_entry.focus_set()
        if prefill:
            url_entry.select_range(0, "end")

        # --- Cookies card (combined: file OR browser) ---
        cookies_card = _section_card(outer, "COOKIES  (optional, for age-restricted videos)")

        # Browser cookies row
        browser_row = ctk.CTkFrame(cookies_card, fg_color="transparent")
        browser_row.pack(fill=tk.X)
        ctk.CTkLabel(
            browser_row,
            text="Live from browser:",
            text_color=COLORS["text_secondary"],
            font=ctk.CTkFont(family=FONTS["family"], size=11),
            width=140,
            anchor="w",
        ).pack(side=tk.LEFT)
        browser_choices = [
            "None",
            "chrome",
            "firefox",
            "edge",
            "brave",
            "opera",
            "vivaldi",
            "chromium",
        ]
        browser_var = tk.StringVar(
            value=getattr(self, "_youtube_cookies_browser", "None") or "None"
        )
        browser_menu = ctk.CTkOptionMenu(
            browser_row,
            variable=browser_var,
            values=browser_choices,
            width=160,
            height=30,
            fg_color=COLORS["bg_dark"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
        )
        browser_menu.pack(side=tk.LEFT)

        ctk.CTkLabel(
            cookies_card,
            text=(
                "⚠  Chrome v127+ / recent Edge / Brave use app-bound encryption — "
                "yt-dlp can't read them. Use Firefox, or export a cookies.txt below."
            ),
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(family=FONTS["family"], size=10),
            justify="left",
            wraplength=540,
            anchor="w",
        ).pack(anchor="w", pady=(8, 10), fill=tk.X)

        # Divider "or"
        divider = ctk.CTkFrame(cookies_card, fg_color="transparent")
        divider.pack(fill=tk.X, pady=(0, 8))
        ctk.CTkFrame(divider, fg_color=COLORS["bg_light"], height=1).pack(
            side=tk.LEFT, fill=tk.X, expand=True, pady=8
        )
        ctk.CTkLabel(
            divider,
            text="  or  ",
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(family=FONTS["family"], size=10),
        ).pack(side=tk.LEFT)
        ctk.CTkFrame(divider, fg_color=COLORS["bg_light"], height=1).pack(
            side=tk.LEFT, fill=tk.X, expand=True, pady=8
        )

        # File row
        file_row = ctk.CTkFrame(cookies_card, fg_color="transparent")
        file_row.pack(fill=tk.X)
        ctk.CTkLabel(
            file_row,
            text="Cookies file:",
            text_color=COLORS["text_secondary"],
            font=ctk.CTkFont(family=FONTS["family"], size=11),
            width=140,
            anchor="w",
        ).pack(side=tk.LEFT)

        cookies_var = tk.StringVar(value=getattr(self, "_youtube_cookies_path", "") or "")
        cookies_entry = ctk.CTkEntry(
            file_row,
            textvariable=cookies_var,
            fg_color=COLORS["bg_dark"],
            border_color=COLORS["bg_light"],
            height=30,
            placeholder_text="Path to cookies.txt",
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
            file_row,
            text="Browse…",
            width=80,
            height=30,
            command=browse_cookies,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
        ).pack(side=tk.LEFT, padx=(6, 0))

        # --- Action buttons (pinned to bottom) ---
        btn_row = ctk.CTkFrame(outer, fg_color="transparent")
        btn_row.pack(fill=tk.X, side=tk.BOTTOM, pady=(8, 0))

        def start():
            url = url_var.get().strip()
            if not url:
                messagebox.showwarning("Download", "Please paste a page URL.")
                return
            cookies_path = cookies_var.get().strip() or None
            if cookies_path and not os.path.isfile(cookies_path):
                messagebox.showwarning("Download", "Cookies file not found.")
                return
            browser = browser_var.get().strip()
            if browser == "None":
                browser = ""
            # Persist for next time
            self._youtube_cookies_path = cookies_path or ""
            self._youtube_cookies_browser = browser
            self._save_config()
            dialog.destroy()
            self._start_youtube_download(url, cookies_path, self.current_tab_idx, browser or None)

        ctk.CTkButton(
            btn_row,
            text="⬇  Download",
            command=start,
            fg_color=COLORS["blurple"],
            hover_color=COLORS["blurple_hover"],
            width=140,
            height=36,
            font=ctk.CTkFont(family=FONTS["family"], size=12, weight="bold"),
        ).pack(side=tk.RIGHT)

        ctk.CTkButton(
            btn_row,
            text="Cancel",
            command=dialog.destroy,
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            width=90,
            height=36,
        ).pack(side=tk.RIGHT, padx=(0, 8))

        url_entry.bind("<Return>", lambda e: start())

    def _start_youtube_download(
        self,
        url: str,
        cookies_path: Optional[str],
        target_tab_idx: int,
        cookies_browser: Optional[str] = None,
    ):
        """Probe URL → if multiple videos, show picker → download selected as MP3.

        Works for any site yt-dlp supports (YouTube, Vimeo, Twitter, TikTok,
        SoundCloud, Facebook, ~1000 others). For pages that contain a single
        video the picker is skipped automatically.
        """
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            messagebox.showerror(
                "yt-dlp missing",
                "yt-dlp is not installed.\n\nRun:\n  pip install yt-dlp",
            )
            return

        # Quick probe dialog (indeterminate)
        probe = ctk.CTkToplevel(self.root)
        probe.title("Inspecting page…")
        probe.geometry("360x110")
        probe.transient(self.root)
        probe.grab_set()
        probe.protocol("WM_DELETE_WINDOW", lambda: None)
        pf = ctk.CTkFrame(probe, fg_color=COLORS["bg_dark"], corner_radius=0)
        pf.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)
        ctk.CTkLabel(
            pf,
            text="Inspecting page for videos…",
            text_color=COLORS["text_primary"],
            font=self._font_sm,
        ).pack(anchor="w", pady=(0, 8))
        pbar = ctk.CTkProgressBar(
            pf, mode="indeterminate", fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"], height=12,
        )
        pbar.pack(fill=tk.X)
        pbar.start()

        probe_result: Dict[str, Any] = {"entries": None, "error": None, "title": None}

        def _probe_worker():
            opts: Dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist",  # fast: don't resolve each entry
                "noplaylist": False,
            }
            if cookies_path:
                opts["cookiefile"] = cookies_path
            if cookies_browser:
                opts["cookiesfrombrowser"] = (cookies_browser,)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[arg-type]
                    info = ydl.extract_info(url, download=False)
                if info is None:
                    probe_result["error"] = "No video info returned."
                    return
                probe_result["title"] = info.get("title")
                if "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]  # type: ignore[union-attr]
                    probe_result["entries"] = entries
                else:
                    probe_result["entries"] = [info]
            except Exception as e:
                probe_result["error"] = re.sub(r"\x1b?\[[0-9;]*m", "", str(e))

        def _on_probe_done():
            try:
                pbar.stop()
                probe.grab_release()
                probe.destroy()
            except Exception:
                pass

            if probe_result["error"]:
                messagebox.showerror("Inspection failed", probe_result["error"])
                return

            entries = probe_result["entries"] or []
            if not entries:
                messagebox.showerror("Nothing found", "No downloadable video was found on that page.")
                return

            if len(entries) == 1:
                # Single video → go straight to download
                self._download_audio_entries(
                    url, [1], cookies_path, cookies_browser, target_tab_idx
                )
            else:
                # Multiple videos → show picker
                self._show_video_picker_dialog(
                    url, entries, cookies_path, cookies_browser, target_tab_idx
                )

        def _probe_thread():
            try:
                _probe_worker()
            finally:
                self.root.after(0, _on_probe_done)

        threading.Thread(target=_probe_thread, daemon=True).start()

    def _show_video_picker_dialog(
        self,
        url: str,
        entries: List[Dict[str, Any]],
        cookies_path: Optional[str],
        cookies_browser: Optional[str],
        target_tab_idx: int,
    ):
        """Show a checkbox list of videos found on the page; user picks which to grab."""
        dlg = ctk.CTkToplevel(self.root)
        dlg.title(f"{len(entries)} videos found")
        dlg.geometry("560x520")
        dlg.minsize(480, 360)
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(fg_color=COLORS["bg_dark"])
        dlg.after(10, lambda: dlg.focus_force())

        outer = ctk.CTkFrame(dlg, fg_color=COLORS["bg_dark"], corner_radius=0)
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=18)

        ctk.CTkLabel(
            outer,
            text=f"Found {len(entries)} videos — pick which ones to download",
            text_color=COLORS["text_primary"],
            font=ctk.CTkFont(family=FONTS["family"], size=14, weight="bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 4))
        ctk.CTkLabel(
            outer,
            text="Each selected video becomes its own MP3 slot in the current tab.",
            text_color=COLORS["text_muted"],
            font=ctk.CTkFont(family=FONTS["family"], size=11),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 10))

        # Select all / none row
        sel_row = ctk.CTkFrame(outer, fg_color="transparent")
        sel_row.pack(fill=tk.X, pady=(0, 6))
        check_vars: List[tk.BooleanVar] = [tk.BooleanVar(value=True) for _ in entries]

        def _set_all(value: bool):
            for v in check_vars:
                v.set(value)

        ctk.CTkButton(
            sel_row, text="Select all", command=lambda: _set_all(True),
            width=90, height=26, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], font=self._font_xs,
        ).pack(side=tk.LEFT)
        ctk.CTkButton(
            sel_row, text="None", command=lambda: _set_all(False),
            width=70, height=26, fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"], font=self._font_xs,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # Scrollable list of checkboxes
        list_frame = ctk.CTkScrollableFrame(
            outer,
            fg_color=COLORS["bg_medium"],
            scrollbar_button_color=COLORS["bg_light"],
            scrollbar_button_hover_color=COLORS["bg_lighter"],
        )
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        for idx, entry in enumerate(entries):
            title = entry.get("title") or entry.get("id") or f"Video {idx + 1}"
            dur = entry.get("duration")
            if isinstance(dur, (int, float)) and dur > 0:
                m, s = divmod(int(dur), 60)
                title = f"{title}  ({m}:{s:02d})"
            cb = ctk.CTkCheckBox(
                list_frame,
                text=f"{idx + 1}. {title}",
                variable=check_vars[idx],
                font=self._font_sm,
                text_color=COLORS["text_primary"],
                hover_color=COLORS["bg_lighter"],
                checkbox_width=18,
                checkbox_height=18,
            )
            cb.pack(anchor="w", padx=10, pady=4, fill=tk.X)

        # Action buttons
        btns = ctk.CTkFrame(outer, fg_color="transparent")
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        def _confirm():
            picked = [i + 1 for i, v in enumerate(check_vars) if v.get()]  # 1-indexed
            if not picked:
                messagebox.showwarning("Nothing selected", "Pick at least one video.")
                return
            dlg.destroy()
            self._download_audio_entries(
                url, picked, cookies_path, cookies_browser, target_tab_idx
            )

        ctk.CTkButton(
            btns, text="Cancel", command=dlg.destroy,
            fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
            width=90, height=34,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ctk.CTkButton(
            btns, text="⬇  Download selected", command=_confirm,
            fg_color=COLORS["blurple"], hover_color=COLORS["blurple_hover"],
            width=180, height=34,
            font=ctk.CTkFont(family=FONTS["family"], size=12, weight="bold"),
        ).pack(side=tk.RIGHT)

    def _download_audio_entries(
        self,
        url: str,
        playlist_items: List[int],
        cookies_path: Optional[str],
        cookies_browser: Optional[str],
        target_tab_idx: int,
    ):
        """Download one or more audio tracks from `url` and create slots for each.

        `playlist_items` is a list of 1-indexed positions within the page's
        entry list. For a single-video page just pass `[1]`.
        """
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
            try:
                import imageio_ffmpeg  # type: ignore

                ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
                ffmpeg_dir = os.path.dirname(ffmpeg_path)
            except Exception:
                ffmpeg_dir = None

        # Progress dialog
        prog = ctk.CTkToplevel(self.root)
        prog.title("Downloading...")
        prog.geometry("440x170")
        prog.transient(self.root)
        prog.grab_set()
        prog.protocol("WM_DELETE_WINDOW", lambda: None)

        pframe = ctk.CTkFrame(prog, fg_color=COLORS["bg_dark"], corner_radius=0)
        pframe.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)

        total_count = len(playlist_items)
        status_var = tk.StringVar(
            value=f"Preparing download (1 of {total_count})..."
            if total_count > 1
            else "Preparing download..."
        )
        ctk.CTkLabel(
            pframe, textvariable=status_var, text_color=COLORS["text_primary"],
            font=self._font_sm, anchor="w", wraplength=400, justify="left",
        ).pack(fill=tk.X, pady=(0, 8))

        bar = ctk.CTkProgressBar(
            pframe, fg_color=COLORS["bg_medium"],
            progress_color=COLORS["blurple"], height=14,
        )
        bar.pack(fill=tk.X)
        bar.set(0)

        cancel_flag = {"cancel": False, "current": 0}

        def on_cancel():
            cancel_flag["cancel"] = True
            status_var.set("Cancelling...")

        ctk.CTkButton(
            pframe, text="Cancel", command=on_cancel,
            fg_color=COLORS["red"], hover_color=COLORS["red_hover"],
            width=90, height=28,
        ).pack(pady=(10, 0))

        downloaded: List[Dict[str, str]] = []  # [{path, title}, ...]
        errors: List[str] = []

        out_template = str(Path(SOUNDS_DIR).absolute() / "yt_%(id)s.%(ext)s")
        os.makedirs(SOUNDS_DIR, exist_ok=True)

        def hook(d):
            if cancel_flag["cancel"]:
                raise Exception("Cancelled by user")
            try:
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    db = d.get("downloaded_bytes") or 0
                    pct = (db / total) if total else 0
                    title = (d.get("info_dict") or {}).get("title", "")
                    cur = cancel_flag["current"] + 1
                    prefix = f"[{cur}/{total_count}] " if total_count > 1 else ""
                    msg = f"{prefix}Downloading: {int(pct * 100)}%"
                    if title:
                        short = title if len(title) <= 50 else title[:47] + "..."
                        msg = f"{prefix}{short}\n{int(pct * 100)}%"
                    self.root.after(0, lambda m=msg, p=pct: (status_var.set(m), bar.set(p)))
                elif d.get("status") == "finished":
                    self.root.after(0, lambda: (status_var.set("Converting to MP3..."), bar.set(1.0)))
            except Exception:
                pass

        def worker():
            for i, item_no in enumerate(playlist_items):
                if cancel_flag["cancel"]:
                    break
                cancel_flag["current"] = i
                ydl_opts: Dict[str, Any] = {
                    "format": "bestaudio/best",
                    "outtmpl": out_template,
                    "noplaylist": False,
                    "playlist_items": str(item_no),
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
                if cookies_browser:
                    ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                        info = ydl.extract_info(url, download=True)
                        if info and "entries" in info:
                            ents = [e for e in (info.get("entries") or []) if e]  # type: ignore[union-attr]
                            info = ents[0] if ents else None
                        if not info:
                            errors.append(f"Item {item_no}: no info returned")
                            continue
                        vid = info.get("id", "")
                        title = info.get("title", "Sound") or "Sound"
                        final_path = str(Path(SOUNDS_DIR).absolute() / f"yt_{vid}.mp3")
                        if not os.path.exists(final_path):
                            cands = list(Path(SOUNDS_DIR).glob(f"yt_{vid}.*"))
                            if cands:
                                final_path = str(cands[0].absolute())
                        downloaded.append({"path": final_path, "title": title})
                except Exception as e:
                    if cancel_flag["cancel"]:
                        break
                    msg = re.sub(r"\x1b?\[[0-9;]*m", "", str(e))
                    low = msg.lower()
                    if "failed to decrypt with dpapi" in low or (
                        "decrypt" in low and "cookie" in low
                    ):
                        msg = (
                            f"Can't decrypt {cookies_browser or 'browser'} cookies (Chrome v127+ "
                            "uses app-bound encryption). Switch to Firefox in the dropdown, or "
                            "export a cookies.txt with the 'Get cookies.txt LOCALLY' extension."
                        )
                    elif "could not copy" in low and "cookie" in low:
                        msg = (
                            f"Can't read cookies from {cookies_browser or 'the selected browser'} "
                            "while it's running. Quit it fully (check tray) or use Firefox / a "
                            "cookies.txt file."
                        )
                    elif "sign in to confirm your age" in low:
                        msg = (
                            "Site is asking for sign-in to confirm your age. Pick a browser you're "
                            "logged into in the dropdown, or supply a fresh cookies.txt."
                        )
                    errors.append(f"Item {item_no}: {msg}")

        def on_done():
            try:
                prog.grab_release()
            except Exception:
                pass
            try:
                prog.destroy()
            except Exception:
                pass

            for d in downloaded:
                p, t = d["path"], d["title"]
                if p and os.path.exists(p):
                    self._create_slot_from_downloaded_file(p, t, target_tab_idx)

            if errors and not cancel_flag["cancel"]:
                preview = "\n\n".join(errors[:3])
                more = f"\n\n(+ {len(errors) - 3} more errors)" if len(errors) > 3 else ""
                messagebox.showerror(
                    "Some downloads failed",
                    f"Downloaded {len(downloaded)} of {total_count}.\n\n{preview}{more}",
                )

        def thread_target():
            try:
                worker()
            finally:
                self.root.after(0, on_done)

        threading.Thread(target=thread_target, daemon=True).start()

    def _create_slot_from_audio_file(
        self,
        file_path: str,
        title: str,
        target_tab_idx: int,
        remove_source: bool = False,
        open_config: bool = True,
    ):
        """Add an audio file to the cache and optionally open the configure dialog."""
        try:
            preload_now = not self._is_long_audio_file(file_path)
            local_path = self.sound_cache.add_sound(file_path, preload=preload_now)
            if not preload_now:
                self._preload_sound_in_background(local_path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to add sound:\n{e}")
            return

        if remove_source:
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

        if open_config and target_tab_idx == self.current_tab_idx:
            self._configure_slot(slot_idx)

    def _create_slot_from_downloaded_file(self, file_path: str, title: str, target_tab_idx: int):
        """Add the downloaded MP3 to the cache and open the configure dialog."""
        self._create_slot_from_audio_file(
            file_path,
            title,
            target_tab_idx,
            remove_source=True,
            open_config=True,
        )

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
            "hover_preview_key": (
                self.hover_preview_key_var.get().strip()
                if self.hover_preview_key_var.get().strip()
                else None
            ),
            "scroll_speed_multiplier": self._get_scroll_speed_multiplier(),
            "input_device": self.input_var.get() if self.input_var.get() else None,
            "output_device": self.output_var.get() if self.output_var.get() else None,
            "auto_start": self.auto_start_var.get() if hasattr(self, "auto_start_var") else True,
            "monitor_enabled": self.monitor_var.get() if hasattr(self, "monitor_var") else True,
            "minimize_to_tray": (
                self.minimize_to_tray_var.get() if hasattr(self, "minimize_to_tray_var") else False
            ),
            "now_playing_visible": (
                self.now_playing_panel.is_visible if hasattr(self, "now_playing_panel") else False
            ),
            "master_volume": (
                self.master_volume_var.get() if hasattr(self, "master_volume_var") else 100
            ),
            "custom_groups": self._custom_groups,
            "youtube_cookies_path": getattr(self, "_youtube_cookies_path", "") or "",
            "youtube_cookies_browser": getattr(self, "_youtube_cookies_browser", "") or "",
            "noise_suppression": (
                self.noise_suppress_var.get() if hasattr(self, "noise_suppress_var") else False
            ),
            "noise_suppression_strength": (
                self.ns_strength_var.get() if hasattr(self, "ns_strength_var") else 85
            ),
            "recording_dir": (
                self.recording_dir_var.get() if hasattr(self, "recording_dir_var") else ""
            ),
            "recording_include_mic": (
                self.recording_include_mic_var.get()
                if hasattr(self, "recording_include_mic_var")
                else True
            ),
            # AFK Mode
            "afk_enabled": self._afk_enabled,
            "afk_tab_idx": self._afk_tab_idx,
            "afk_slot_idx": self._afk_slot_idx,
            "afk_interval_seconds": self._afk_interval_seconds,
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
            self._register_hover_preview_binding()
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

            hover_preview_key = config.get("hover_preview_key", "mouse3") or ""
            self.hover_preview_key_var.set(str(hover_preview_key).strip().lower())

            try:
                self.scroll_speed_var.set(
                    max(1, min(30, int(config.get("scroll_speed_multiplier", 10))))
                )
            except Exception:
                self.scroll_speed_var.set(10)
            self._update_scroll_speed_label(save=False)

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

            # Load minimize-to-tray preference
            if hasattr(self, "minimize_to_tray_var"):
                self.minimize_to_tray_var.set(bool(config.get("minimize_to_tray", False)))

            # Load noise suppression settings
            ns_enabled = bool(config.get("noise_suppression", False))
            ns_strength = float(config.get("noise_suppression_strength", 85))
            if hasattr(self, "noise_suppress_var"):
                self.noise_suppress_var.set(ns_enabled)
            if hasattr(self, "ns_strength_var"):
                self.ns_strength_var.set(ns_strength)

            # Load Now Playing panel settings
            now_playing_visible = config.get("now_playing_visible", False)

            # Load master (sounds) volume
            master_volume = float(config.get("master_volume", 100))
            if hasattr(self, "master_volume_var"):
                self.master_volume_var.set(master_volume)
                if hasattr(self, "master_volume_label"):
                    self.master_volume_label.configure(text=f"{int(master_volume)}%")

            # Load custom groups
            self._custom_groups = config.get("custom_groups", [])
            self._refresh_group_combo()

            # Load YouTube downloader cookies path
            self._youtube_cookies_path = config.get("youtube_cookies_path", "") or ""
            self._youtube_cookies_browser = config.get("youtube_cookies_browser", "") or ""

            # Load recording settings
            rec_dir = config.get("recording_dir", "") or ""
            if rec_dir and hasattr(self, "recording_dir_var"):
                self.recording_dir_var.set(rec_dir)
            if hasattr(self, "recording_include_mic_var"):
                self.recording_include_mic_var.set(bool(config.get("recording_include_mic", True)))

            # AFK Mode — restore persisted selection + interval; start later if enabled.
            self._afk_tab_idx = int(config.get("afk_tab_idx", 0) or 0)
            self._afk_slot_idx = int(config.get("afk_slot_idx", 0) or 0)
            self._afk_interval_seconds = max(1, int(config.get("afk_interval_seconds", 60) or 60))
            if hasattr(self, "afk_min_var"):
                m, s = divmod(self._afk_interval_seconds, 60)
                self.afk_min_var.set(str(m))
                self.afk_sec_var.set(str(s))
            # Refresh picker so the saved slot can be selected.
            if hasattr(self, "afk_slot_combo"):
                self._refresh_afk_slot_options()
            _afk_was_enabled = bool(config.get("afk_enabled", False))

            if hasattr(self, "now_playing_panel"):
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
            self._register_hover_preview_binding()

            # Auto-start the stream if enabled and devices are selected
            if auto_start and self.input_var.get() and self.output_var.get():
                self.root.after(100, self._auto_start_stream)

            # Auto-start AFK if it was previously running.
            if _afk_was_enabled:
                self.root.after(300, self._start_afk)

            # Initial paint of the rich status bar (devices/PTT/etc.)
            self.root.after(50, self._update_status_bar)

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
        """Handle application close. BULLETPROOF - guarantees process termination.

        If the user has enabled "minimize to tray", clicking the window's X
        hides the window to the tray instead of quitting. The tray's own
        Quit menu item performs the real shutdown.
        """
        # Honor minimize-to-tray preference (if checkbox is checked AND
        # we're not in the middle of a real shutdown via the tray menu).
        if (
            getattr(self, "minimize_to_tray_var", None) is not None
            and self.minimize_to_tray_var.get()
            and not getattr(self, "_force_quit", False)
        ):
            self._minimize_to_tray()
            return

        self._real_quit()

    def _real_quit(self):
        """Actual shutdown — releases PTT, stops mixer, destroys window."""
        # Cancel any pending AFK timers so they can't fire post-shutdown.
        for _attr in ("_afk_after_id", "_afk_countdown_after_id"):
            _aid = getattr(self, _attr, None)
            if _aid is not None:
                try:
                    self.root.after_cancel(_aid)
                except Exception:
                    pass
                setattr(self, _attr, None)

        try:
            self._unregister_hover_preview_binding()
        except Exception:
            pass

        # Tear down the tray icon if it's running.
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None

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

        # Stop any active recording so the file is flushed to disk before
        # we shut down the audio streams it depends on.
        if self.recorder is not None and self.recorder.recording:
            try:
                self.recorder.stop()
            except Exception:
                pass
        if self.quick_sound_recorder is not None and self.quick_sound_recorder.recording:
            try:
                self.quick_sound_recorder.stop()
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

    # ------------------------------------------------------------------
    # AFK Mode — auto-play a chosen slot every N min:sec
    # ------------------------------------------------------------------

    def _refresh_afk_slot_options(self):
        """Rebuild the AFK slot picker mapping from currently filled slots.

        The picker widget itself lives in the popup (created on demand);
        this just refreshes the label→(tab_idx, slot_idx) map and the
        currently selected `afk_slot_var` value.
        """
        if not hasattr(self, "afk_slot_var"):
            return
        mapping: Dict[str, tuple] = {}
        options: list = []
        for tab_idx, tab in enumerate(self.tabs):
            for slot_idx, slot in tab.slots.items():
                if not slot or not slot.file_path:
                    continue
                tab_label = tab.name or f"Tab {tab_idx + 1}"
                slot_label = slot.name or f"Slot {slot_idx + 1}"
                label = f"{tab_label} · {slot_label}"
                if label in mapping:
                    label = f"{label} #{slot_idx}"
                mapping[label] = (tab_idx, slot_idx)
                options.append(label)

        self._afk_slot_options = mapping

        if not options:
            self.afk_slot_var.set("(no filled slots)")
            return

        # Keep the previously selected slot if it still exists.
        target_label = None
        for label, (t, s) in mapping.items():
            if t == self._afk_tab_idx and s == self._afk_slot_idx:
                target_label = label
                break
        if target_label is None:
            target_label = options[0]
            t, s = mapping[target_label]
            self._afk_tab_idx, self._afk_slot_idx = t, s
        self.afk_slot_var.set(target_label)

    def _on_afk_slot_changed(self):
        """Selected slot changed in the picker."""
        label = self.afk_slot_var.get()
        if label in self._afk_slot_options:
            self._afk_tab_idx, self._afk_slot_idx = self._afk_slot_options[label]
            self._save_config()

    def _on_afk_interval_changed(self):
        """Minutes/seconds spinbox changed."""
        try:
            mins = int(self.afk_min_var.get() or "0")
        except ValueError:
            mins = 0
        try:
            secs = int(self.afk_sec_var.get() or "0")
        except ValueError:
            secs = 0
        mins = max(0, min(mins, 180))
        secs = max(0, min(secs, 59))
        total = mins * 60 + secs
        if total == 0:
            total = 1  # Floor at 1s to avoid runaway loops.
        self._afk_interval_seconds = total
        self._save_config()
        # If running, reschedule with the new interval.
        if self._afk_enabled:
            self._stop_afk(_keep_flag=True)
            self._start_afk()

    def _toggle_afk(self):
        if self._afk_enabled:
            self._stop_afk()
        else:
            self._start_afk()

    def _start_afk(self):
        """Begin AFK auto-play."""
        # Resolve current selection (in case it never fired _on_afk_slot_changed).
        label = self.afk_slot_var.get() if hasattr(self, "afk_slot_var") else ""
        if label in self._afk_slot_options:
            self._afk_tab_idx, self._afk_slot_idx = self._afk_slot_options[label]

        # Validate selection is a real filled slot.
        if (
            self._afk_tab_idx < 0
            or self._afk_tab_idx >= len(self.tabs)
            or self._afk_slot_idx not in self.tabs[self._afk_tab_idx].slots
            or not self.tabs[self._afk_tab_idx].slots[self._afk_slot_idx].file_path
        ):
            self._afk_popup_status("⚠ Pick a filled slot first", warn=True)
            return

        # Pull current interval from the spinboxes (in case user typed but
        # never blurred the field).
        self._on_afk_interval_changed()

        self._afk_enabled = True
        self._update_afk_button_visual()
        self._update_afk_popup_widgets()
        self._schedule_next_afk_tick()
        self._afk_tick_status_countdown()
        self._save_config()

    def _stop_afk(self, _keep_flag: bool = False):
        """Stop AFK auto-play. _keep_flag is for internal reschedules."""
        if self._afk_after_id is not None:
            try:
                self.root.after_cancel(self._afk_after_id)
            except Exception:
                pass
            self._afk_after_id = None
        if self._afk_countdown_after_id is not None:
            try:
                self.root.after_cancel(self._afk_countdown_after_id)
            except Exception:
                pass
            self._afk_countdown_after_id = None

        if not _keep_flag:
            self._afk_enabled = False
            self._update_afk_button_visual()
            self._update_afk_popup_widgets()
            self._afk_popup_status("Idle")
            self._save_config()

    def _schedule_next_afk_tick(self):
        interval_ms = max(1, self._afk_interval_seconds) * 1000
        self._afk_next_play_time = time.time() + interval_ms / 1000.0
        self._afk_after_id = self.root.after(interval_ms, self._afk_tick)

    def _afk_tick(self):
        self._afk_after_id = None
        if not self._afk_enabled or getattr(self, "_shutting_down", False):
            return
        # Silently skip if stream isn't running (matches hotkey behavior).
        if self.mixer and self.mixer.running:
            try:
                self._play_slot_from_tab(self._afk_tab_idx, self._afk_slot_idx)
            except Exception:
                pass
        # Reschedule regardless so AFK keeps the same cadence.
        self._schedule_next_afk_tick()

    def _afk_tick_status_countdown(self):
        """1Hz refresher for the popup's 'Next play in M:SS' label."""
        self._afk_countdown_after_id = None
        if not self._afk_enabled:
            return
        remaining = max(0, int(self._afk_next_play_time - time.time()))
        m, s = divmod(remaining, 60)
        if self.mixer and self.mixer.running:
            self._afk_popup_status(f"Next play in {m}:{s:02d}")
        else:
            self._afk_popup_status(
                f"Next in {m}:{s:02d} (stream stopped — will skip)", warn=True
            )
        self._afk_countdown_after_id = self.root.after(1000, self._afk_tick_status_countdown)

    # ----- action-bar AFK button + popup -----

    def _update_afk_button_visual(self):
        """Reflect AFK running state on the action-bar button."""
        btn = getattr(self, "afk_btn", None)
        if btn is None:
            return
        try:
            if self._afk_enabled:
                btn.configure(
                    text="💤 AFK ⏵",
                    fg_color=COLORS["green"],
                    hover_color=COLORS["green_hover"],
                )
            else:
                btn.configure(
                    text="💤 AFK",
                    fg_color=COLORS["bg_light"],
                    hover_color=COLORS["bg_lighter"],
                )
        except Exception:
            pass

    def _afk_popup_status(self, text: str, warn: bool = False):
        """Update the popup's status label, if open."""
        lbl = getattr(self, "_afk_popup_status_label", None)
        if lbl is None:
            return
        try:
            lbl.configure(
                text=text,
                text_color=(
                    COLORS.get("yellow", "#F0B232") if warn else COLORS["text_muted"]
                ),
            )
        except Exception:
            pass

    def _update_afk_popup_widgets(self):
        """Refresh popup toggle button / status when the popup is open."""
        toggle = getattr(self, "_afk_popup_toggle_btn", None)
        if toggle is None:
            return
        try:
            if self._afk_enabled:
                toggle.configure(
                    text="⏹ Stop AFK",
                    fg_color=COLORS["red"],
                    hover_color=COLORS["red_hover"],
                )
            else:
                toggle.configure(
                    text="▶ Start AFK",
                    fg_color=COLORS["green"],
                    hover_color=COLORS["green_hover"],
                )
        except Exception:
            pass

    def _show_afk_popup(self):
        """Open a small popup with AFK slot picker, interval, and start/stop."""
        # If a popup is already open, just focus it.
        existing = getattr(self, "_afk_popup", None)
        if existing is not None:
            try:
                if existing.winfo_exists():
                    existing.lift()
                    existing.focus_force()
                    return
            except Exception:
                pass
            self._afk_popup = None

        # Refresh slot list (may have changed since last open).
        self._refresh_afk_slot_options()
        # Sync min/sec vars from persisted seconds.
        m, s = divmod(self._afk_interval_seconds, 60)
        self.afk_min_var.set(str(m))
        self.afk_sec_var.set(str(s))

        popup = ctk.CTkToplevel(self.root)
        popup.title("AFK Mode")
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.geometry("380x230")
        popup.configure(fg_color=COLORS["bg_dark"])
        # Position roughly under the action bar AFK button.
        try:
            x = self.afk_btn.winfo_rootx()
            y = self.afk_btn.winfo_rooty() + self.afk_btn.winfo_height() + 6
            popup.geometry(f"380x230+{x}+{y}")
        except Exception:
            pass

        self._afk_popup = popup

        def _on_close():
            self._afk_popup = None
            self._afk_popup_toggle_btn = None
            self._afk_popup_status_label = None
            try:
                popup.destroy()
            except Exception:
                pass

        popup.protocol("WM_DELETE_WINDOW", _on_close)

        body = ctk.CTkFrame(popup, fg_color=COLORS["bg_medium"], corner_radius=10)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        ctk.CTkLabel(
            body,
            text="💤 AFK Mode",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_md"], weight="bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        ).pack(fill=tk.X, padx=12, pady=(10, 6))

        # ── Slot picker row
        pick_row = ctk.CTkFrame(body, fg_color="transparent")
        pick_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        ctk.CTkLabel(
            pick_row,
            text="Sound:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
            width=56,
            anchor="w",
        ).pack(side=tk.LEFT)
        values = list(self._afk_slot_options.keys()) or ["(no filled slots)"]
        slot_combo = ctk.CTkOptionMenu(
            pick_row,
            values=values,
            variable=self.afk_slot_var,
            command=lambda _v: self._on_afk_slot_changed(),
            fg_color=COLORS["bg_light"],
            button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"],
            dropdown_fg_color=COLORS["bg_medium"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            dropdown_font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            corner_radius=6,
            height=28,
        )
        slot_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))

        refresh_btn = ctk.CTkButton(
            pick_row,
            text="⟳",
            command=lambda: (
                self._refresh_afk_slot_options(),
                slot_combo.configure(
                    values=list(self._afk_slot_options.keys()) or ["(no filled slots)"]
                ),
            ),
            fg_color=COLORS["bg_light"],
            hover_color=COLORS["bg_lighter"],
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            corner_radius=6,
            width=28,
            height=28,
        )
        refresh_btn.pack(side=tk.LEFT)
        _Tooltip.attach(refresh_btn, "Refresh slot list")

        # ── Interval row
        int_row = ctk.CTkFrame(body, fg_color="transparent")
        int_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        ctk.CTkLabel(
            int_row,
            text="Every:",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"]),
            text_color=COLORS["text_secondary"],
            width=56,
            anchor="w",
        ).pack(side=tk.LEFT)

        min_spin = tk.Spinbox(
            int_row,
            from_=0,
            to=180,
            width=4,
            textvariable=self.afk_min_var,
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            bg=COLORS["bg_light"],
            fg=COLORS["text_primary"],
            buttonbackground=COLORS["bg_light"],
            relief="flat",
            highlightthickness=0,
            command=self._on_afk_interval_changed,
        )
        min_spin.pack(side=tk.LEFT, padx=(0, 2))
        ctk.CTkLabel(
            int_row,
            text="min",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        sec_spin = tk.Spinbox(
            int_row,
            from_=0,
            to=59,
            width=4,
            textvariable=self.afk_sec_var,
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            bg=COLORS["bg_light"],
            fg=COLORS["text_primary"],
            buttonbackground=COLORS["bg_light"],
            relief="flat",
            highlightthickness=0,
            command=self._on_afk_interval_changed,
        )
        sec_spin.pack(side=tk.LEFT, padx=(0, 2))
        ctk.CTkLabel(
            int_row,
            text="sec",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        ).pack(side=tk.LEFT)

        # Pull live values when user blurs / closes.
        self.afk_min_var.trace_add("write", lambda *_: self._on_afk_interval_changed())
        self.afk_sec_var.trace_add("write", lambda *_: self._on_afk_interval_changed())

        # ── Toggle button + status
        toggle_row = ctk.CTkFrame(body, fg_color="transparent")
        toggle_row.pack(fill=tk.X, padx=12, pady=(4, 8))

        toggle_btn = ctk.CTkButton(
            toggle_row,
            text="⏹ Stop AFK" if self._afk_enabled else "▶ Start AFK",
            command=self._toggle_afk,
            fg_color=COLORS["red"] if self._afk_enabled else COLORS["green"],
            hover_color=(
                COLORS["red_hover"] if self._afk_enabled else COLORS["green_hover"]
            ),
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold"),
            corner_radius=6,
            height=30,
        )
        toggle_btn.pack(fill=tk.X)
        self._afk_popup_toggle_btn = toggle_btn

        status_lbl = ctk.CTkLabel(
            body,
            text="Running…" if self._afk_enabled else "Idle",
            font=ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        status_lbl.pack(fill=tk.X, padx=12, pady=(0, 10))
        self._afk_popup_status_label = status_lbl

        # If AFK is currently running, kick the countdown so the label updates.
        if self._afk_enabled and self._afk_countdown_after_id is None:
            self._afk_tick_status_countdown()

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    def _on_toggle_tray_setting(self):
        """Persist the minimize-to-tray preference."""
        self._save_config()

    def _on_toggle_start_with_windows(self):
        """Add or remove the Windows auto-start registry entry."""
        enabled = bool(self.start_with_windows_var.get())
        ok = _windows_startup_set(enabled)
        if not ok and enabled:
            # Roll back the checkbox if the registry write failed.
            self.start_with_windows_var.set(False)
            try:
                messagebox.showerror(
                    "Start with Windows",
                    "Could not write to the Windows registry.\n\n"
                    "This feature is only supported on Windows.",
                )
            except Exception:
                pass
        # No need to persist in JSON — the registry IS the source of truth.

    def _minimize_to_tray(self):
        """Hide the main window and show a tray icon."""
        # Lazy-create the tray on first hide.
        if self._tray is None:
            try:
                from .tray import SystemTray, is_available

                if not is_available():
                    # pystray missing — fall back to normal close.
                    self._real_quit()
                    return

                self._tray = SystemTray(
                    on_show=self._tray_request_show,
                    on_quit=self._tray_request_quit,
                    title=UI["window_title"],
                )
                self._tray.start()
            except Exception:
                # Tray failed for any reason — just quit normally.
                self._real_quit()
                return

        # Hide the window.
        try:
            self.root.withdraw()
        except Exception:
            pass
        self._tray_hidden = True

        # First-time hint so the user knows where the app went.
        if not getattr(self, "_tray_first_hide_notified", False):
            try:
                if self._tray is not None:
                    self._tray.show_notification(
                        "The soundboard is still running in the system tray. "
                        "Right-click the icon to restore or quit."
                    )
            except Exception:
                pass
            self._tray_first_hide_notified = True

    def _tray_request_show(self):
        """Tray menu 'Show' clicked — schedule restore on the Tk thread."""
        try:
            self.root.after(0, self._restore_from_tray)
        except Exception:
            pass

    def _tray_request_quit(self):
        """Tray menu 'Quit' clicked — schedule a real shutdown on the Tk thread."""
        self._force_quit = True
        try:
            self.root.after(0, self._real_quit)
        except Exception:
            # If the Tk loop is already gone, just exit hard.
            try:
                os._exit(0)
            except Exception:
                pass

    def _restore_from_tray(self):
        """Restore the main window from a tray-hidden state."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        self._tray_hidden = False

    def run(self):
        """Start the application main loop."""
        try:
            self.root.mainloop()
        except (KeyboardInterrupt, SystemExit):
            pass
