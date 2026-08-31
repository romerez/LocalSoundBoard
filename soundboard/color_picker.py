"""Modern, reusable colour picker for slots and tabs.

Provides :class:`SlickColorPicker`, a self-contained CustomTkinter widget built
around a real **2D gradient studio** (the kind you'd see in Photoshop / Discord)
rather than rows of dots:

* **Gradient studio** — a live saturation/value square plus a hue strip you can
  click & drag, with a hex field, an RGB readout and ``Use`` / ``＋ Save``
  actions. This is the primary, "create any colour" surface.
* **Palette** — the themed library (``SLOT_COLOR_GROUPS``) shown one family at a
  time via a dropdown, as compact round chips, for fast picks.
* **Saved** — colours the user pressed ``＋ Save`` on, persisted in the app
  config (``app._custom_colors``) so they're reusable on any slot/tab.

Design goals: *no internal scrollbar* (the hosting dialog scrolls if needed),
a fixed, predictable natural height, and graceful degradation to a swatch-only
picker if Pillow/NumPy aren't importable.

Public API (kept stable for both call sites):
``SlickColorPicker(parent, app, initial=None, on_change=None, allow_none=True,
default_hex=None)`` with ``.get()`` and ``.set(value)``.
"""

from __future__ import annotations

import colorsys
import tkinter as tk
from typing import Callable, List, Optional

import customtkinter as ctk

from .constants import COLORS, FONTS, SLOT_COLOR_GROUPS, UI, get_text_color_for_bg

# Pillow + NumPy power the live gradient. They're hard deps of the app, but the
# picker degrades to swatches-only if either is somehow unavailable.
try:
    import numpy as np
    from PIL import Image, ImageTk

    _IMG_OK = True
except Exception:  # pragma: no cover - defensive
    _IMG_OK = False


# ---------------------------------------------------------------------------
# colour helpers
# ---------------------------------------------------------------------------
def _norm_hex(value: str) -> Optional[str]:
    """Normalise ``#rgb``/``#rrggbb`` to lowercase ``#rrggbb``; else None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v.startswith("#"):
        v = "#" + v
    body = v[1:]
    if len(body) == 3 and all(c in "0123456789abcdef" for c in body):
        body = "".join(c * 2 for c in body)
    if len(body) == 6 and all(c in "0123456789abcdef" for c in body):
        return "#" + body
    return None


def _hex_to_hsv(hex_color: str):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    return colorsys.rgb_to_hsv(r, g, b)


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return f"#{int(round(r * 255)):02x}{int(round(g * 255)):02x}{int(round(b * 255)):02x}"


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _window_scaling(widget) -> float:
    """CTk's DPI factor for *widget*'s window (1.0 fallback).

    The gradient square, hue strip, preview ring and swatch chips are RAW
    ``tk.Canvas`` widgets: their sizes, coordinates and PhotoImages are DEVICE
    pixels. Everything below is therefore rendered at ``logical * scaling`` and
    drawn 1:1 — no CTk/Tk resample, so the gradients stay crisp at 150 %.
    """
    try:
        return float(ctk.ScalingTracker.get_window_scaling(widget.winfo_toplevel()))
    except Exception:
        pass
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:
        return 1.0


if _IMG_OK:

    def _hsv_to_rgb_np(h, s, v):
        """Vectorised HSV→RGB for float arrays in [0, 1]. Returns (r, g, b)."""
        i = np.floor(h * 6.0).astype(int) % 6
        f = h * 6.0 - np.floor(h * 6.0)
        p = v * (1.0 - s)
        q = v * (1.0 - f * s)
        t = v * (1.0 - (1.0 - f) * s)
        conds = [i == k for k in range(6)]
        r = np.select(conds, [v, q, p, p, t, v])
        g = np.select(conds, [t, v, v, q, p, p])
        b = np.select(conds, [p, p, t, v, v, q])
        return r, g, b

    def _sv_image(hue: float, w: int, h: int) -> "Image.Image":
        """Saturation (x, 0→1) × Value (y, top=1→bottom=0) square for *hue*."""
        s_row = np.linspace(0.0, 1.0, w, dtype=np.float32)
        v_col = np.linspace(1.0, 0.0, h, dtype=np.float32)
        s = np.tile(s_row, (h, 1))
        v = np.repeat(v_col[:, None], w, axis=1)
        hh = np.full((h, w), hue, dtype=np.float32)
        r, g, b = _hsv_to_rgb_np(hh, s, v)
        arr = (np.stack([r, g, b], axis=-1) * 255.0).astype(np.uint8)
        return Image.fromarray(arr, "RGB")

    def _hue_image(w: int, h: int) -> "Image.Image":
        """Vertical hue bar (top 0° → bottom 360°)."""
        hue_col = np.linspace(0.0, 1.0, h, dtype=np.float32)
        hh = np.repeat(hue_col[:, None], w, axis=1)
        ones = np.ones((h, w), dtype=np.float32)
        r, g, b = _hsv_to_rgb_np(hh, ones, ones)
        arr = (np.stack([r, g, b], axis=-1) * 255.0).astype(np.uint8)
        return Image.fromarray(arr, "RGB")


class SlickColorPicker(ctk.CTkFrame):
    """A gradient-studio + themed-swatch + saved-colours colour picker."""

    SWATCH = 24          # chip diameter (px)
    SV_W, SV_H = 184, 132   # saturation/value square size
    HUE_W = 18           # hue strip width (same height as SV square)
    SWATCH_PER_ROW = 9   # chips per row in the palette

    def __init__(
        self,
        parent,
        app,
        initial: Optional[str] = None,
        on_change: Optional[Callable[[Optional[str]], None]] = None,
        allow_none: bool = True,
        default_hex: Optional[str] = None,
    ):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self._on_change = on_change
        self._allow_none = allow_none
        self._default_hex = _norm_hex(default_hex or "") or COLORS["blurple"]

        self._selected: Optional[str] = _norm_hex(initial) if initial else None
        # Studio source-of-truth (always a concrete colour, even when Default).
        seed = self._selected or self._default_hex
        self._h, self._s, self._v = _hex_to_hsv(seed)

        self._chips: List[tuple] = []        # current family swatches
        self._saved_chips: List[tuple] = []  # saved-colour swatches
        self._sv_img_id = None
        self._sv_hue_cached: Optional[float] = None
        self._sv_photo = None
        self._hue_photo = None

        # Device-pixel sizes for the raw-Tk canvases (see _window_scaling).
        # NB: ``self._s`` is the SATURATION channel, hence ``_dpi`` here.
        dpi = _window_scaling(self)
        if not (0.4 <= dpi <= 8.0):
            dpi = 1.0
        self._dpi = dpi
        self._sv_dw = max(1, round(self.SV_W * dpi))
        self._sv_dh = max(1, round(self.SV_H * dpi))
        self._hue_dw = max(1, round(self.HUE_W * dpi))
        self._sw_d = max(1, round(self.SWATCH * dpi))
        self._prev_d = max(1, round(40 * dpi))
        self._cur_r = max(2, round(6 * dpi))
        self._w2 = max(1, round(2 * dpi))
        # Raw-Tk font: NEGATIVE size = device px, matching CTk's sizing.
        self._font_chip = (FONTS["family"], -max(1, round(9 * dpi)), "bold")

        self._font_xs = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_xs"])
        self._font_sm = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"])
        self._font_sm_b = ctk.CTkFont(family=FONTS["family"], size=FONTS["size_sm"], weight="bold")
        self._font_mono = ctk.CTkFont(family=FONTS["family_mono"], size=FONTS["size_sm"])

        self._build_header()
        if _IMG_OK:
            self._build_studio()
        self._build_palette()
        self._build_saved_row()

        if _IMG_OK:
            self._render_sv(force=True)
        self._refresh_all(notify=False)

    # ------------------------------------------------------------- value API
    def get(self) -> Optional[str]:
        """Return the chosen hex (lowercase ``#rrggbb``) or None for default."""
        return self._selected

    def set(self, value: Optional[str]):
        hx = _norm_hex(value) if value else None
        if hx:
            self._h, self._s, self._v = _hex_to_hsv(hx)
            self._selected = hx
        else:
            self._selected = None
        if _IMG_OK:
            self._render_sv(force=True)
        self._refresh_all(notify=False)

    # ----------------------------------------------------------------- header
    def _build_header(self):
        bar = ctk.CTkFrame(self, fg_color=COLORS["bg_dark"], corner_radius=8)
        bar.pack(fill=tk.X, pady=(0, 8))
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill=tk.X, padx=10, pady=8)

        self._preview = tk.Canvas(
            inner, width=self._prev_d, height=self._prev_d, highlightthickness=0, bd=0,
            bg=COLORS["bg_dark"], cursor="arrow",
        )
        self._preview.pack(side=tk.LEFT)

        info = ctk.CTkFrame(inner, fg_color="transparent")
        info.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))
        self._name_lbl = ctk.CTkLabel(
            info, text="", font=self._font_sm_b, text_color=COLORS["text_primary"],
            anchor="w",
        )
        self._name_lbl.pack(fill=tk.X, anchor="w")
        self._hex_lbl = ctk.CTkLabel(
            info, text="", font=self._font_xs, text_color=COLORS["text_muted"],
            anchor="w",
        )
        self._hex_lbl.pack(fill=tk.X, anchor="w")

        if self._allow_none:
            ctk.CTkButton(
                inner, text="Default", width=74, height=UI["control_height"],
                command=self._choose_default,
                fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
                font=self._font_xs, corner_radius=UI["button_corner_radius"],
            ).pack(side=tk.RIGHT)

    # ----------------------------------------------------------------- studio
    def _build_studio(self):
        studio = ctk.CTkFrame(self, fg_color=COLORS["bg_dark"], corner_radius=8)
        studio.pack(fill=tk.X, pady=(0, 8))
        row = ctk.CTkFrame(studio, fg_color="transparent")
        row.pack(fill=tk.X, padx=10, pady=10)

        # SV square (left).
        self._sv_canvas = tk.Canvas(
            row, width=self._sv_dw, height=self._sv_dh, highlightthickness=0, bd=0,
            bg=COLORS["bg_dark"], cursor="crosshair",
        )
        self._sv_canvas.pack(side=tk.LEFT)
        self._sv_canvas.bind("<Button-1>", self._on_sv_pointer)
        self._sv_canvas.bind("<B1-Motion>", self._on_sv_pointer)

        # Hue strip (middle).
        self._hue_canvas = tk.Canvas(
            row, width=self._hue_dw, height=self._sv_dh, highlightthickness=0, bd=0,
            bg=COLORS["bg_dark"], cursor="sb_v_double_arrow",
        )
        self._hue_canvas.pack(side=tk.LEFT, padx=(10, 0))
        self._hue_canvas.bind("<Button-1>", self._on_hue_pointer)
        self._hue_canvas.bind("<B1-Motion>", self._on_hue_pointer)
        # Rendered at DEVICE size and drawn 1:1 → crisp on HiDPI.
        self._hue_photo = ImageTk.PhotoImage(_hue_image(self._hue_dw, self._sv_dh))
        self._hue_canvas.create_image(0, 0, anchor="nw", image=self._hue_photo)

        # Controls column (right).
        ctrl = ctk.CTkFrame(row, fg_color="transparent")
        ctrl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        hexrow = ctk.CTkFrame(ctrl, fg_color="transparent")
        hexrow.pack(fill=tk.X)
        ctk.CTkLabel(hexrow, text="HEX", font=self._font_xs,
                     text_color=COLORS["text_muted"], width=30, anchor="w").pack(side=tk.LEFT)
        self._hex_var = tk.StringVar(value="#ffffff")
        hex_entry = ctk.CTkEntry(
            hexrow, textvariable=self._hex_var, height=30,
            fg_color=COLORS["bg_medium"], border_color=COLORS["bg_light"],
            font=self._font_mono,
        )
        hex_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        hex_entry.bind("<Return>", lambda _e: self._apply_hex_entry())
        hex_entry.bind("<FocusOut>", lambda _e: self._apply_hex_entry())

        self._rgb_lbl = ctk.CTkLabel(
            ctrl, text="", font=self._font_xs, text_color=COLORS["text_secondary"],
            anchor="w",
        )
        self._rgb_lbl.pack(fill=tk.X, anchor="w", pady=(8, 0))

        btns = ctk.CTkFrame(ctrl, fg_color="transparent")
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        # Primary (green, bold) commits the studio colour; Save is secondary.
        ctk.CTkButton(
            btns, text="✓ Use", height=UI["control_height"], command=self._use_studio_color,
            fg_color=COLORS["green"], hover_color=COLORS["green_hover"],
            font=self._font_sm_b, corner_radius=UI["button_corner_radius"],
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ctk.CTkButton(
            btns, text="＋ Save", height=UI["control_height"], command=self._save_studio_color,
            fg_color=COLORS["bg_light"], hover_color=COLORS["bg_lighter"],
            font=self._font_sm, corner_radius=UI["button_corner_radius"],
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

    # ---------------------------------------------------------------- palette
    def _build_palette(self):
        wrap = ctk.CTkFrame(self, fg_color=COLORS["bg_dark"], corner_radius=8)
        wrap.pack(fill=tk.X, pady=(0, 8))

        head = ctk.CTkFrame(wrap, fg_color="transparent")
        head.pack(fill=tk.X, padx=10, pady=(8, 2))
        ctk.CTkLabel(head, text="PALETTE", font=self._font_xs,
                     text_color=COLORS["text_muted"], anchor="w").pack(side=tk.LEFT)
        self._family_var = tk.StringVar(value=self._initial_family())
        ctk.CTkOptionMenu(
            head, values=list(SLOT_COLOR_GROUPS.keys()), variable=self._family_var,
            command=lambda _v: self._render_family(), width=120,
            height=UI["control_height"],
            fg_color=COLORS["bg_medium"], button_color=COLORS["bg_light"],
            button_hover_color=COLORS["bg_lighter"], font=self._font_sm,
            dropdown_font=self._font_sm, corner_radius=UI["button_corner_radius"],
        ).pack(side=tk.RIGHT)

        self._palette_grid = ctk.CTkFrame(wrap, fg_color="transparent")
        self._palette_grid.pack(fill=tk.X, padx=10, pady=(0, 10))
        self._render_family()

    def _initial_family(self) -> str:
        if self._selected:
            for fam, colors in SLOT_COLOR_GROUPS.items():
                if any(_norm_hex(h) == self._selected for h in colors.values()):
                    return fam
        return next(iter(SLOT_COLOR_GROUPS.keys()))

    def _render_family(self):
        for child in self._palette_grid.winfo_children():
            child.destroy()
        self._chips = []
        family = self._family_var.get()
        colors = SLOT_COLOR_GROUPS.get(family, {})
        for i, hex_color in enumerate(colors.values()):
            r, c = divmod(i, self.SWATCH_PER_ROW)
            h = _norm_hex(hex_color)
            cv = self._make_chip(self._palette_grid, h, c, r)
            self._chips.append((cv, h))
        self._refresh_chip_rings()

    # ------------------------------------------------------------ saved colors
    def _build_saved_row(self):
        self._saved_wrap = ctk.CTkFrame(self, fg_color=COLORS["bg_dark"], corner_radius=8)
        self._saved_header = ctk.CTkLabel(
            self._saved_wrap, text="SAVED  ·  right-click to remove", font=self._font_xs,
            text_color=COLORS["text_muted"], anchor="w",
        )
        self._saved_grid = ctk.CTkFrame(self._saved_wrap, fg_color="transparent")
        self._refresh_saved_row()

    def _saved_colors(self) -> List[str]:
        return [c for c in (getattr(self.app, "_custom_colors", []) or []) if _norm_hex(c)]

    def _refresh_saved_row(self):
        for child in self._saved_grid.winfo_children():
            child.destroy()
        self._saved_chips = []
        saved = self._saved_colors()
        if not saved:
            self._saved_wrap.pack_forget()
            return
        self._saved_wrap.pack(fill=tk.X, pady=(0, 8))
        self._saved_header.pack(fill=tk.X, anchor="w", padx=10, pady=(8, 2))
        self._saved_grid.pack(fill=tk.X, padx=10, pady=(0, 10))
        for i, hex_color in enumerate(saved):
            r, c = divmod(i, self.SWATCH_PER_ROW)
            h = _norm_hex(hex_color)
            cv = tk.Canvas(
                self._saved_grid, width=self._sw_d, height=self._sw_d,
                highlightthickness=0, bd=0, bg=COLORS["bg_dark"], cursor="hand2",
            )
            cv.grid(row=r, column=c, padx=3, pady=3)
            self._draw_chip(cv, h, h == self._selected)
            cv.bind("<Button-1>", lambda _e, x=h: self._pick_swatch(x))
            cv.bind("<Button-3>", lambda _e, x=h: self._forget_saved(x))
            self._saved_chips.append((cv, h))

    def _forget_saved(self, hex_color: str):
        lst = getattr(self.app, "_custom_colors", None)
        if isinstance(lst, list) and hex_color in lst:
            lst.remove(hex_color)
            self._persist_app()
            self._refresh_saved_row()

    # --------------------------------------------------------------- chips
    def _make_chip(self, parent, hex_color: str, col: int, row: int):
        cv = tk.Canvas(
            parent, width=self._sw_d, height=self._sw_d, highlightthickness=0,
            bd=0, bg=COLORS["bg_dark"], cursor="hand2",
        )
        cv.grid(row=row, column=col, padx=3, pady=3)
        self._draw_chip(cv, hex_color, hex_color == self._selected)
        cv.bind("<Button-1>", lambda _e, h=hex_color: self._pick_swatch(h))
        return cv

    def _draw_chip(self, canvas: tk.Canvas, fill: str, selected: bool):
        canvas.delete("all")
        d = self._sw_d
        pad = self._w2
        if selected:
            canvas.create_oval(0, 0, d - 1, d - 1, fill="", outline=COLORS["text_primary"],
                               width=self._w2)
            canvas.create_oval(pad + 1, pad + 1, d - pad - 2, d - pad - 2, fill=fill, outline="")
            tc = get_text_color_for_bg(fill)
            canvas.create_text(d / 2, d / 2 - 1, text="✓", fill=tc, font=self._font_chip)
        else:
            canvas.create_oval(pad, pad, d - pad - 1, d - pad - 1, fill=fill,
                               outline=COLORS["bg_light"], width=1)

    # ------------------------------------------------------------ interactions
    def _pick_swatch(self, hex_color: str):
        h = _norm_hex(hex_color)
        if not h:
            return
        self._h, self._s, self._v = _hex_to_hsv(h)
        self._selected = h
        if _IMG_OK:
            self._render_sv(force=False)
        self._refresh_all(notify=True)

    def _choose_default(self):
        self._selected = None
        self._refresh_all(notify=True)

    def _on_sv_pointer(self, event):
        # Pointer coords are device px — the same space as the device-sized canvas.
        x = min(max(event.x, 0), self._sv_dw - 1)
        y = min(max(event.y, 0), self._sv_dh - 1)
        self._s = x / max(1, self._sv_dw - 1)
        self._v = 1.0 - y / max(1, self._sv_dh - 1)
        self._selected = _hsv_to_hex(self._h, self._s, self._v)
        self._refresh_all(notify=True)

    def _on_hue_pointer(self, event):
        y = min(max(event.y, 0), self._sv_dh - 1)
        self._h = y / max(1, self._sv_dh - 1)
        self._selected = _hsv_to_hex(self._h, self._s, self._v)
        self._render_sv(force=False)
        self._refresh_all(notify=True)

    def _apply_hex_entry(self):
        h = _norm_hex(self._hex_var.get())
        if not h:
            self._hex_var.set(self._selected or self._default_hex)
            return
        self._h, self._s, self._v = _hex_to_hsv(h)
        self._selected = h
        if _IMG_OK:
            self._render_sv(force=False)
        self._refresh_all(notify=True)

    def _use_studio_color(self):
        self._selected = _hsv_to_hex(self._h, self._s, self._v)
        self._refresh_all(notify=True)

    def _save_studio_color(self):
        h = self._selected or _hsv_to_hex(self._h, self._s, self._v)
        lst = getattr(self.app, "_custom_colors", None)
        if not isinstance(lst, list):
            lst = []
            self.app._custom_colors = lst
        if h not in lst:
            lst.append(h)
            self._persist_app()
        self._selected = h
        self._refresh_saved_row()
        self._refresh_all(notify=True)

    def _persist_app(self):
        try:
            self.app._save_config()
        except Exception:
            pass

    # --------------------------------------------------------------- rendering
    def _render_sv(self, force: bool):
        """(Re)draw the SV square image; only regenerated when the hue moves."""
        if not _IMG_OK:
            return
        if force or self._sv_hue_cached is None or abs(self._sv_hue_cached - self._h) > 1e-4:
            self._sv_photo = ImageTk.PhotoImage(_sv_image(self._h, self._sv_dw, self._sv_dh))
            if self._sv_img_id is None:
                self._sv_img_id = self._sv_canvas.create_image(
                    0, 0, anchor="nw", image=self._sv_photo, tags=("svimg",)
                )
            else:
                self._sv_canvas.itemconfigure(self._sv_img_id, image=self._sv_photo)
            self._sv_hue_cached = self._h
        self._draw_sv_cursor()
        self._draw_hue_cursor()

    def _draw_sv_cursor(self):
        if not _IMG_OK:
            return
        self._sv_canvas.delete("svcur")
        x = self._s * (self._sv_dw - 1)
        y = (1.0 - self._v) * (self._sv_dh - 1)
        rr = self._cur_r
        # White ring with a dark inner ring so it reads on any background.
        self._sv_canvas.create_oval(x - rr, y - rr, x + rr, y + rr, outline="#ffffff",
                                    width=self._w2, tags=("svcur",))
        self._sv_canvas.create_oval(x - rr - 1, y - rr - 1, x + rr + 1, y + rr + 1,
                                    outline="#000000", width=1, tags=("svcur",))

    def _draw_hue_cursor(self):
        if not _IMG_OK:
            return
        self._hue_canvas.delete("huecur")
        y = self._h * (self._sv_dh - 1)
        hh = self._w2
        self._hue_canvas.create_rectangle(0, y - hh, self._hue_dw - 1, y + hh,
                                          outline="#ffffff", width=self._w2, tags=("huecur",))

    def _draw_ring(self, canvas: tk.Canvas, fill: str):
        canvas.delete("all")
        w = int(canvas.cget("width"))
        h = int(canvas.cget("height"))
        d = min(w, h)
        p = self._w2
        canvas.create_oval(p, p, d - p - 1, d - p - 1, fill=fill, outline=COLORS["bg_light"],
                           width=p)

    def _color_name(self, hex_color: str) -> str:
        for colors in SLOT_COLOR_GROUPS.values():
            for name, hx in colors.items():
                if _norm_hex(hx) == hex_color:
                    return name
        return "Custom"

    def _refresh_chip_rings(self):
        for cv, hex_color in (self._chips + self._saved_chips):
            try:
                if cv.winfo_exists():
                    self._draw_chip(cv, hex_color, hex_color == self._selected)
            except Exception:
                pass

    def _refresh_all(self, notify: bool):
        shown = self._selected or self._default_hex
        self._draw_ring(self._preview, shown)
        if self._selected is None:
            self._name_lbl.configure(text="Default")
            self._hex_lbl.configure(text=self._default_hex.lower())
        else:
            self._name_lbl.configure(text=self._color_name(self._selected))
            self._hex_lbl.configure(text=self._selected)
        if _IMG_OK:
            studio_hex = self._selected or _hsv_to_hex(self._h, self._s, self._v)
            self._hex_var.set(studio_hex)
            r, g, b = _hex_to_rgb(studio_hex)
            self._rgb_lbl.configure(text=f"R {r:>3}    G {g:>3}    B {b:>3}")
            self._draw_sv_cursor()
            self._draw_hue_cursor()
        self._refresh_chip_rings()
        if notify and self._on_change:
            try:
                self._on_change(self._selected)
            except Exception:
                pass
