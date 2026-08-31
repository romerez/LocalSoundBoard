"""Process-wide CustomTkinter fixes for layout-storm stalls (imported once by gui.py).

Measured 2026-08-31 with a main-thread stall profiler on the People hub (10
people, ~250 CTk widgets per panel, 150 % DPI): the single biggest source of
0.8–4 s UI freezes was CustomTkinter's own drawing code, not the app:

* ``CTkScrollbar._draw`` and ``CTkOptionMenu._draw`` end with
  ``self._canvas.update_idletasks()``. ``CTkScrollableFrame`` binds every inner
  ``<Configure>`` to ``configure(scrollregion=bbox("all"))``, which changes the
  canvas view, which calls the scrollbar's ``set()`` → ``_draw()`` →
  ``update_idletasks()``.  So EVERY widget geometry change inside a scrollable
  frame forced a FULL flush of all pending idle layout/redraw work, nested
  inside the flush that was already running.  ``CTkScrollbar.set`` alone
  measured 170–670 ms per call (2.6–3.0 s per People-panel build), and one
  ``CTkOptionMenu._draw`` took 2.06 s.
* ``CTkScrollbar.set`` redraws even when the thumb did not move.

The three patches below remove exactly that: the scrollbar/option-menu draws no
longer pump the idle queue (Tk repaints them at idle anyway), the scroll
region is refreshed once per 40 ms burst instead of once per widget, and the
scrollbar only redraws when its values changed.  Behaviour is otherwise
identical; the main board's scrollable frames benefit too.
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk

import customtkinter as ctk

_NOOP = lambda *a, **k: None  # noqa: E731

# Win32 RedrawWindow flags
_RDW_INVALIDATE = 0x0001
_RDW_ERASE = 0x0004
_RDW_ALLCHILDREN = 0x0080
_RDW_UPDATENOW = 0x0100


def redraw_window(top, update_now: bool = False) -> None:
    """Force Windows to repaint ``top`` and every child from Tk's CURRENT state.

    Widgets created/recoloured while their toplevel was WITHDRAWN come up on
    screen with STALE pixels after deiconify — the prebuilt People hub showed
    chips as blue slabs and empty sidebar rows, and every CTkToplevel dialog
    showed a blank header (its title/subtitle labels are built during the
    withdraw→deiconify title-bar cycle below) — even though every widget's Tk
    state was correct. Invalidating the window (RDW_INVALIDATE | RDW_ERASE |
    RDW_ALLCHILDREN) makes Windows send WM_PAINT to every child HWND; Tk then
    turns those into queued <Expose> events and repaints from the state it
    already has. ``update_now`` delivers the WM_PAINTs synchronously (Tk still
    paints from its idle queue — see person_board._after_reopen). No-op off
    Windows, on an unmapped window, or on any failure."""
    if not sys.platform.startswith("win"):
        return
    try:
        if not top.winfo_exists() or not top.winfo_viewable():
            return
        hwnd = ctypes.c_void_p(int(top.winfo_id()))
        flags = _RDW_INVALIDATE | _RDW_ERASE | _RDW_ALLCHILDREN
        if update_now:
            flags |= _RDW_UPDATENOW
        ctypes.windll.user32.RedrawWindow(hwnd, None, None, flags)
    except Exception:
        pass


def _arm_redraw_on_map(top) -> None:
    """Repaint ``top`` once it is really mapped (a <Map> is coming, or it is
    already on screen). Idempotent per window; safe to call repeatedly."""
    top._lsb_rdw_pending = True
    if not getattr(top, "_lsb_rdw_bound", False):
        top._lsb_rdw_bound = True

        def _on_map(event, top=top):
            # Children's <Map> events reach the toplevel's bindtag too.
            if getattr(event, "widget", None) is not top:
                return
            if not getattr(top, "_lsb_rdw_pending", False):
                return
            top._lsb_rdw_pending = False
            _do_reveal_redraw(top)

        try:
            top.bind("<Map>", _on_map, add="+")
        except Exception:
            pass

    def _fallback(top=top):
        # Already visible (no <Map> will come): repaint now.
        if not getattr(top, "_lsb_rdw_pending", False):
            return
        try:
            if top.winfo_exists() and top.winfo_viewable():
                top._lsb_rdw_pending = False
                _do_reveal_redraw(top)
        except Exception:
            pass

    try:
        top.after(60, _fallback)
    except Exception:
        pass


def _do_reveal_redraw(top) -> None:
    """Invalidate the window once it is mapped so content built while it was
    withdrawn (CTk's own title-bar withdraw/deiconify cycle) can't linger with
    stale pixels — the same cure the People hub uses on reopen."""
    def _do():
        try:
            if not top.winfo_exists():
                return
        except Exception:
            return
        redraw_window(top, update_now=True)
        top._lsb_rdw_done = getattr(top, "_lsb_rdw_done", 0) + 1

    try:
        top.after(1, _do)
    except Exception:
        pass


def _wrap_titlebar_color(cls) -> None:
    """CTkToplevel/CTk ``_windows_set_titlebar_color`` does ``withdraw();
    update()`` — a FULL nested event pump inside every toplevel CONSTRUCTOR.
    Measured: a quick popup's CTkToplevel.__init__ took 5.0 s and the
    configure dialog's 6.4 s, because that pump ran the whole backlog of
    pending work (People pre-warm panels — 1791 CTk widgets — built inside
    the popup's constructor). Tk's own idle flush is all the recolour needs."""
    orig = getattr(cls, "_windows_set_titlebar_color", None)
    if orig is None:
        return

    def _patched(self, color_mode):
        saved = tk.Misc.update
        tk.Misc.update = tk.Misc.update_idletasks  # `super().update()` resolves here
        try:
            return orig(self, color_mode)
        finally:
            tk.Misc.update = saved

    cls._windows_set_titlebar_color = _patched


def _wrap_titlebar_revert(cls) -> None:
    """After CTk re-shows the window (5 ms after the recolour withdraw — i.e.
    after the constructor's caller finished building the dialog while it was
    withdrawn), repaint it once mapped: without this, content built during
    that window came up stale (the blank dialog header)."""
    orig = getattr(cls, "_revert_withdraw_after_windows_set_titlebar_color", None)
    if orig is None:
        return

    def _patched(self):
        result = orig(self)
        try:
            if self.wm_state() == "normal":
                _arm_redraw_on_map(self)
        except Exception:
            pass
        return result

    cls._revert_withdraw_after_windows_set_titlebar_color = _patched


def _wrap_draw_without_idle_pump(cls) -> None:
    orig = cls._draw

    def _draw(self, *a, **k):
        canvas = getattr(self, "_canvas", None)
        # Shadow the instance method for the duration of this draw only.
        self.update_idletasks = _NOOP
        if canvas is not None:
            canvas.update_idletasks = _NOOP
        try:
            return orig(self, *a, **k)
        finally:
            for obj in (self, canvas):
                try:
                    del obj.update_idletasks
                except Exception:
                    pass

    cls._draw = _draw


def install() -> None:
    """Idempotent."""
    if getattr(ctk, "_lsb_patched", False):
        return
    ctk._lsb_patched = True

    # 1) No nested idle pump from CTk widget draws.
    for cls in (ctk.CTkScrollbar, ctk.CTkOptionMenu):
        try:
            _wrap_draw_without_idle_pump(cls)
        except Exception:
            pass

    # 2) Scrollbar redraw only on real change.
    try:
        _orig_set = ctk.CTkScrollbar.set

        def _set(self, start_value, end_value):
            try:
                s, e = float(start_value), float(end_value)
                if abs(s - self._start_value) < 1e-4 and abs(e - self._end_value) < 1e-4:
                    return
            except Exception:
                pass
            return _orig_set(self, start_value, end_value)

        ctk.CTkScrollbar.set = _set
    except Exception:
        pass

    # 4) No nested full event pump inside toplevel constructors, and a repaint
    #    of every toplevel once it is shown after CTk's title-bar withdraw cycle.
    for cls in (ctk.CTkToplevel, ctk.CTk):
        try:
            _wrap_titlebar_color(cls)
            _wrap_titlebar_revert(cls)
        except Exception:
            pass

    # 3) Debounced scrollregion refresh for CTkScrollableFrame.
    try:
        _orig_init = ctk.CTkScrollableFrame.__init__

        def _init(self, *a, **k):
            _orig_init(self, *a, **k)

            def _update_region():
                self._lsb_sr_after = None
                try:
                    self._parent_canvas.configure(scrollregion=self._parent_canvas.bbox("all"))
                except Exception:
                    pass

            def _on_configure(_event=None):
                if getattr(self, "_lsb_sr_after", None) is None:
                    try:
                        self._lsb_sr_after = self.after(40, _update_region)
                    except Exception:
                        self._lsb_sr_after = None

            self._lsb_sr_after = None
            # Replaces CTk's immediate lambda binding on the same tag/sequence.
            self.bind("<Configure>", _on_configure)

        ctk.CTkScrollableFrame.__init__ = _init
    except Exception:
        pass
