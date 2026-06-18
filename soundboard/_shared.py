"""Cross-module shared state for performance coordination.

Lives in its own module to avoid circular imports between `gui` and
`slot_widget`. Both modules read and write `RESIZE_STATE['until']` to
coordinate deferring expensive redraws while the user is actively
resizing or moving the window.
"""

import time
from typing import Any, Dict

# When `time.time() < RESIZE_STATE['until']`, widgets should skip
# expensive draw work and reschedule for after the resize ends.
#   until      — main-window defer window (armed by gui._on_root_configure)
#   root       — the main Tk root (so the CTk patch can tell toplevels apart)
#   dirty      — main-window widgets skipped during the defer (gui sweeps it)
#   tops       — {toplevel: until} for OTHER windows (People hub / pop-outs)
#                that arm their own defer via arm_toplevel_resize_defer()
#   dirty_tops — {toplevel: set(widgets)} skipped per non-main toplevel,
#                swept by that toplevel's own _sweep_toplevel timer
RESIZE_STATE: Dict[str, Any] = {"until": 0.0, "tops": {}, "dirty_tops": {}}

_SWEEP_CHUNK = 24


def arm_toplevel_resize_defer(top: Any, hold: float = 0.15) -> None:
    """Defer CTk per-widget redraws inside `top` for ~`hold`s.

    The main window's defer is swept by gui._post_resize_sweep alone, which is
    why the CTk patch originally drew non-main toplevels immediately (deferring
    them with no sweep left them PERMANENTLY blank). This gives each toplevel
    its own until-window AND its own sweep, so the People hub / pop-outs can
    skip the per-pixel CTk redraw cascade safely. Call it from the toplevel's
    own <Configure> handler, only on true SIZE changes (never pure moves).
    """
    RESIZE_STATE["tops"][top] = time.time() + hold
    aid = getattr(top, "_lsb_sweep_after", None)
    if aid is not None:
        try:
            top.after_cancel(aid)
        except Exception:
            pass
    try:
        top._lsb_sweep_after = top.after(int(hold * 1000) + 10, lambda: _sweep_toplevel(top))
    except Exception:
        top._lsb_sweep_after = None
        RESIZE_STATE["tops"].pop(top, None)
        RESIZE_STATE["dirty_tops"].pop(top, None)


def _sweep_toplevel(top: Any) -> None:
    """Repaint the widgets that were skipped while `top` was resizing."""
    top._lsb_sweep_after = None
    try:
        alive = bool(top.winfo_exists())
    except Exception:
        alive = False
    if not alive:
        RESIZE_STATE["tops"].pop(top, None)
        RESIZE_STATE["dirty_tops"].pop(top, None)
        return
    # Still resizing? Check again shortly.
    if time.time() < RESIZE_STATE["tops"].get(top, 0.0):
        try:
            top._lsb_sweep_after = top.after(60, lambda: _sweep_toplevel(top))
            return
        except Exception:
            pass
    RESIZE_STATE["tops"].pop(top, None)
    dirty = RESIZE_STATE["dirty_tops"].pop(top, None)
    if dirty:
        _drain_toplevel(top, list(dirty))


def _drain_toplevel(top: Any, widgets: list) -> None:
    """Chunked redraw so the post-resize repaint never blocks a full frame."""
    # A NEW resize started mid-drain: re-park the remainder (they'd otherwise
    # be lost — stale/blank widgets after overlapping drags) and let the new
    # sweep handle everything once it settles.
    if time.time() < RESIZE_STATE["tops"].get(top, 0.0):
        if widgets:
            RESIZE_STATE["dirty_tops"].setdefault(top, set()).update(widgets)
        return
    for w in widgets[:_SWEEP_CHUNK]:
        try:
            if w.winfo_exists():
                w._draw(no_color_updates=True)
        except Exception:
            pass
    rest = widgets[_SWEEP_CHUNK:]
    if rest:
        try:
            top.after(0, lambda: _drain_toplevel(top, rest))
        except Exception:
            pass
