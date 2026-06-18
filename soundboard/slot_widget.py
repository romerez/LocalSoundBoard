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

import time
import tkinter as tk
from typing import Any, Callable, Optional

from ._shared import RESIZE_STATE as _SHARED_RESIZE_STATE
from .constants import COLORS

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


def _resolve_font(f: Any) -> Any:
    """Resolve a font value to something Tk's create_text accepts.

    CTkFont stores its actual values via .actual(); reach into that so
    Canvas text renders at the configured family/size/weight rather than
    Tk's default.
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
    """

    BOTTOM_STRIP = 30  # reserved height at bottom for stop+menu buttons
    OVERLAY_BTN_W = 28
    OVERLAY_BTN_H = 22
    OVERLAY_PAD = 5
    PROGRESS_H = 6  # thicker bar — closer to original CTkProgressBar look

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
        super().__init__(
            parent,
            highlightthickness=0,
            bd=0,
            bg=COLORS["bg_medium"],
            height=height,
            cursor="hand2",
        )

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
        self._font: Any = ("Segoe UI Emoji", 11)
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
        # treated as a drag-drop instead of a click.
        self._drag_threshold: int = 8
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
        self._redraw_full()

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
        """Return a Tk-usable image from a CTkImage / PhotoImage / None."""
        if image is None:
            return None
        # CTkImage
        try:
            from customtkinter import CTkImage

            if isinstance(image, CTkImage):
                # Use the dark-mode PIL image to make a PhotoImage.
                try:
                    from PIL import ImageTk  # type: ignore

                    pil = image._dark_image or image._light_image  # type: ignore[attr-defined]
                    if pil is not None:
                        # Resize to the configured size if available.
                        size = getattr(image, "_size", None)
                        if size:
                            try:
                                pil = pil.resize(size)
                            except Exception:
                                pass
                        return ImageTk.PhotoImage(pil)
                except Exception:
                    return None
        except Exception:
            pass
        # Already a Tk PhotoImage
        return image

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

        # Outer frame (canvas bg already set; draw border if any)
        if self._border_width > 0:
            self.create_rectangle(
                1,
                1,
                w - 1,
                h - 1,
                outline=self._border_color,
                width=self._border_width,
                fill="",
                tags="border",
            )

        # Inner button area (the "real" slot button bg)
        inset = max(self._border_width, 4)
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

        content_h = h - self.BOTTOM_STRIP

        # Image (centered above text if present)
        text_cy = content_h // 2 + 6
        if self._image_tk is not None:
            try:
                self.create_image(
                    w // 2,
                    content_h // 2 - 14,
                    image=self._image_tk,
                    tags="image",
                )
                text_cy = content_h - 22
            except tk.TclError:
                pass

        # Main text
        if self._text:
            try:
                self.create_text(
                    w // 2,
                    text_cy,
                    text=self._text,
                    fill=self._text_color,
                    font=_resolve_font(self._font),
                    anchor="center",
                    width=max(20, w - 16),
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
                    width=max(20, w - 16),
                    justify="center",
                    tags="text",
                )

        # Emoji top-left — try rendering as a true-color image first,
        # fall back to Tk's monochrome glyph if PIL/the font is unavailable.
        if self._emoji:
            emoji_img = None
            try:
                from . import emoji_render as _er

                emoji_img = _er.get_tk_image(self._emoji, 22)
            except Exception:
                emoji_img = None
            if emoji_img is not None:
                # Stash a ref on the widget too — the renderer cache holds
                # the canonical reference but this also protects against
                # accidental cache flushes mid-frame.
                self._emoji_img = emoji_img
                try:
                    self.create_image(
                        10,
                        10,
                        image=emoji_img,
                        anchor="nw",
                        tags="emoji",
                    )
                except tk.TclError:
                    self.create_text(
                        10,
                        10,
                        text=self._emoji,
                        fill=COLORS["text_primary"],
                        font=("Segoe UI Emoji", 14),
                        anchor="nw",
                        tags="emoji",
                    )
            else:
                self.create_text(
                    10,
                    10,
                    text=self._emoji,
                    fill=COLORS["text_primary"],
                    font=("Segoe UI Emoji", 14),
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
            bar_pad_left = self.OVERLAY_PAD
            bar_pad_right = self.OVERLAY_PAD
            y_bot = h - self.BOTTOM_STRIP - 2
            y_top = y_bot - self.PROGRESS_H
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
                y_top - 12,
                text=f"🔊 {volume_pct}%",
                fill="#44ff44",
                font=("Segoe UI", 9, "bold"),
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
        bar_pad_left = self.OVERLAY_PAD
        bar_pad_right = self.OVERLAY_PAD
        y_bot = h - self.BOTTOM_STRIP - 2
        y_top = y_bot - self.PROGRESS_H
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

        btn_y_top = h - self.OVERLAY_BTN_H - self.OVERLAY_PAD
        btn_y_bot = h - self.OVERLAY_PAD

        # ⋯ menu button (always visible) — bottom-right
        mx1 = w - self.OVERLAY_PAD
        mx0 = mx1 - self.OVERLAY_BTN_W
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
            font=("Segoe UI", 11, "bold"),
            tags=("overlay", "menu_btn"),
        )

        # Stop button (only when sound is playing) — bottom-left
        if self._stop_visible:
            sx0 = self.OVERLAY_PAD
            sx1 = sx0 + self.OVERLAY_BTN_W
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
                font=("Segoe UI", 11),
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

        btn_y_top = h - self.OVERLAY_BTN_H - self.OVERLAY_PAD
        btn_y_bot = h - self.OVERLAY_PAD
        if btn_y_top <= y <= btn_y_bot:
            mx1 = w - self.OVERLAY_PAD
            mx0 = mx1 - self.OVERLAY_BTN_W
            if mx0 <= x <= mx1:
                return "menu"
            if self._stop_visible:
                sx0 = self.OVERLAY_PAD
                sx1 = sx0 + self.OVERLAY_BTN_W
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
