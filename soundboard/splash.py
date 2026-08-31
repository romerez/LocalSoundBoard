"""A lightweight, self-animating startup splash screen.

Shown the instant the app launches (while the main window is still being built
and withdrawn) so startup feels instant and professional instead of a blank
gap. Pure Tk — no CustomTkinter, no images required — so it can appear before
the heavier UI exists and never blocks launch if anything goes wrong (every
operation is wrapped defensively by the caller).

The indeterminate "comet" bar animates on its own ``after`` loop, so once the
Tk main loop is running it sweeps smoothly. During the synchronous parts of
construction the caller can pump frames manually via :meth:`pump`.
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Optional

# Discord-ish dark palette, matching the rest of the app.
_BG = "#1e1f22"
_CARD = "#2b2d31"
_TRACK = "#383a40"
_TEXT = "#f2f3f5"
_MUTED = "#a3a6aa"
_BLURPLE = "#5865f2"
_BLURPLE_DIM = "#4954c9"
_BLURPLE_FAINT = "#3a429e"


class SplashScreen:
    """Borderless, centered splash with a title, status line and animated bar."""

    def __init__(self, root: tk.Misc, title: str, subtitle: str = "Loading…") -> None:
        self.root = root
        self._running = True
        self._frame = 0
        self._after: Optional[str] = None
        self._closed = False

        # DPI scale so the splash is crisp/comfortable on HiDPI displays. The
        # root is normally a CTk window: use CTk's factor so the splash is
        # sized exactly like the app. (A per-monitor-DPI-aware Tk reports
        # 96 dpi through winfo_fpixels, so that alone reads 1.0 at 150 %.)
        scale = 0.0
        if "customtkinter" in sys.modules:
            try:
                from customtkinter import ScalingTracker  # type: ignore

                scale = float(ScalingTracker.get_window_scaling(root.winfo_toplevel()))
            except Exception:
                scale = 0.0
        if not (0.4 <= scale <= 8.0):
            try:
                scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
            except Exception:
                scale = 1.0
        self._s = scale
        W, H = int(440 * scale), int(240 * scale)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
        except Exception:
            pass
        self.win.configure(bg=_BG)

        # Center on the screen the pointer/primary is on.
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x = (sw - W) // 2
            y = (sh - H) // 2
            self.win.geometry(f"{W}x{H}+{x}+{y}")
        except Exception:
            self.win.geometry(f"{W}x{H}")

        # Fade in (Windows supports per-window alpha).
        try:
            self.win.attributes("-alpha", 0.0)
        except Exception:
            pass

        card = tk.Frame(self.win, bg=_CARD, highlightthickness=1, highlightbackground=_TRACK)
        card.pack(fill=tk.BOTH, expand=True, padx=int(2 * scale), pady=int(2 * scale))

        # Raw-Tk fonts: a NEGATIVE size is device pixels (a positive one is
        # points that Tk scales by its own factor, not CTk's) — so these match
        # CTk text of the same logical size.
        def _px(px: float) -> int:
            return -max(1, round(px * scale))

        def _f(px: int, bold: bool = False) -> tuple:
            return ("Segoe UI", _px(px), "bold" if bold else "normal")

        tk.Label(card, text="🎵", bg=_CARD, fg=_BLURPLE, font=("Segoe UI Emoji", _px(34))).pack(
            pady=(int(26 * scale), int(4 * scale))
        )
        tk.Label(card, text=title, bg=_CARD, fg=_TEXT, font=_f(17, True)).pack()

        self._status_var = tk.StringVar(value=subtitle)
        tk.Label(card, textvariable=self._status_var, bg=_CARD, fg=_MUTED, font=_f(10)).pack(
            pady=(int(8 * scale), int(14 * scale))
        )

        # Indeterminate "comet" track.
        self._track_w = int(320 * scale)
        self._track_h = int(6 * scale)
        self._seg_w = int(90 * scale)
        self._canvas = tk.Canvas(
            card,
            width=self._track_w,
            height=self._track_h,
            bg=_TRACK,
            highlightthickness=0,
            bd=0,
        )
        self._canvas.pack()

        self._draw_comet()
        self._fade_in()
        self._animate()

    # -- animation ------------------------------------------------------
    def _draw_comet(self) -> None:
        try:
            self._canvas.delete("comet")
        except Exception:
            return
        span = self._track_w + self._seg_w
        # Position sweeps from off-left to off-right, then wraps.
        pos = (self._frame * max(3, int(4 * self._s))) % span - self._seg_w
        h = self._track_h
        # Trailing glow: three stacked segments, brightest leading edge.
        for off, col in ((0, _BLURPLE), (int(14 * self._s), _BLURPLE_DIM), (int(28 * self._s), _BLURPLE_FAINT)):
            x0 = pos - off
            x1 = x0 + self._seg_w
            self._canvas.create_rectangle(
                max(0, x0), 0, min(self._track_w, x1), h, fill=col, outline="", tags="comet"
            )

    def _animate(self) -> None:
        if not self._running:
            return
        self._frame += 1
        self._draw_comet()
        try:
            self._after = self.win.after(16, self._animate)
        except Exception:
            self._after = None

    def _fade_in(self, step: int = 0) -> None:
        try:
            a = min(1.0, step * 0.12)
            self.win.attributes("-alpha", a)
            if a < 1.0:
                self.win.after(12, lambda: self._fade_in(step + 1))
        except Exception:
            pass

    # -- public API -----------------------------------------------------
    def set_status(self, text: str) -> None:
        try:
            self._status_var.set(text)
        except Exception:
            pass

    def pump(self) -> None:
        """Advance + render a frame during synchronous work (no main loop yet).

        Uses ``update_idletasks`` (NOT ``update``) on purpose: it maps and
        redraws the splash but does NOT process the global ``after``-timer
        queue, so it can't prematurely fire the host app's deferred startup
        callbacks (which expect to run only once the main loop is up and the
        rest of construction has finished).
        """
        if self._closed:
            return
        try:
            self._frame += 2
            self._draw_comet()
            self.win.update_idletasks()
        except Exception:
            pass

    def close(self) -> None:
        """Fade out and destroy. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._running = False
        if self._after is not None:
            try:
                self.win.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        self._fade_out()

    def _fade_out(self, step: int = 8) -> None:
        try:
            a = step * 0.12
            if a > 0:
                self.win.attributes("-alpha", a)
                self.win.after(12, lambda: self._fade_out(step - 1))
                return
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
