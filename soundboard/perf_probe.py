"""Lightweight, opt-in performance probe — TEMPORARY profiling harness.

Goal: find the *real* felt-lag source (opening windows, tab switching, grid
build/scroll, resize) with hard wall-clock numbers, instead of guessing from a
static code read.

Design:
- ZERO effect unless the environment variable ``LSB_PERF`` is set to a truthy
  value (1/true/yes/on). When off, :func:`install` returns immediately and the
  real app code is never touched.
- When on, it monkeypatches a curated set of methods at runtime to time them.
  All instrumentation lives in THIS file; the only edit to real code is one
  guarded ``perf_probe.install()`` call in ``main.py``.
- "Action" probes (low-frequency, user-visible: open editor, switch tab, ...)
  print a live line every call so you can correlate with what you just did.
- "Hot" probes (high-frequency: per-image decode, per-chip build, cull) only
  print when a single call is slow (>= HOT_THRESHOLD_MS); they always feed the
  aggregate table.
- On exit (and on demand via :func:`dump`) a sorted summary table is written so
  you can see exactly where the milliseconds went.

Output goes to ``perf.log`` (fresh each run) AND to stderr so you see it live.
The perf logger does NOT propagate, so it never pollutes ``debug.log``.

To remove later: delete this file and the small guarded block in main.py.
"""

from __future__ import annotations

import atexit
import functools
import logging
import os
import sys
from time import perf_counter

# A single slow hot-path call (e.g. one big LANCZOS image decode) >= this many
# ms gets its own live line; otherwise hot calls are aggregated silently.
HOT_THRESHOLD_MS = 6.0

_ACTION = "action"
_HOT = "hot"

# (module_attr_on_soundboard, class_name, method_name, kind)
# Curated to cover the user's stated pain: opening windows, tabs/grid, resize.
_TARGETS = [
    # ---- Tabs / grid build + switch + density + resize sweep -------------
    ("gui", "SoundboardApp", "_switch_tab", _ACTION),
    ("gui", "SoundboardApp", "_show_tab_only", _ACTION),
    ("gui", "SoundboardApp", "_build_tab_widgets", _ACTION),
    ("gui", "SoundboardApp", "_build_all_tab_widgets", _ACTION),
    ("gui", "SoundboardApp", "_apply_grid_columns", _ACTION),
    ("gui", "SoundboardApp", "_rebuild_all_tab_grids", _ACTION),
    ("gui", "SoundboardApp", "_post_resize_sweep", _ACTION),
    ("gui", "SoundboardApp", "_load_slot_image", _HOT),
    ("gui", "SoundboardApp", "_cull_slots", _HOT),
    ("gui", "SoundboardApp", "_sweep_drain", _HOT),
    # ---- Opening windows: settings / popups / person hub ----------------
    ("gui", "SoundboardApp", "_toggle_audio_options", _ACTION),
    ("gui", "SoundboardApp", "_show_voice_popup", _ACTION),
    ("gui", "SoundboardApp", "_show_quick_popup", _ACTION),
    ("gui", "SoundboardApp", "_open_person_hub", _ACTION),
    # ---- Sound editor open path -----------------------------------------
    ("editor", "SoundEditor", "__init__", _ACTION),
    ("editor", "SoundEditor", "_load_audio", _ACTION),
    ("editor", "SoundEditor", "_create_dialog", _ACTION),
    ("editor", "SoundEditor", "_draw_waveform", _HOT),
    # ---- People hub + panels --------------------------------------------
    ("person_board", "PersonHub", "__init__", _ACTION),
    ("person_board", "PersonHub", "select", _ACTION),
    ("person_board", "PersonHub", "_on_search", _ACTION),
    ("person_board", "PersonHub", "refresh_people", _ACTION),
    ("person_board", "PersonPanel", "rebuild", _ACTION),
    ("person_board", "PersonPanel", "_expand_group_inplace", _ACTION),
    ("person_board", "PersonPanel", "_build_chip", _HOT),
]

# label -> {"count", "total_ms", "max_ms", "kind"}
_stats: dict = {}
_log = logging.getLogger("lsb.perf")
_installed = False


def _enabled() -> bool:
    return str(os.environ.get("LSB_PERF", "")).strip().lower() in ("1", "true", "yes", "on")


def _setup_logger() -> None:
    _log.setLevel(logging.DEBUG)
    _log.propagate = False  # keep perf output OUT of debug.log
    for h in list(_log.handlers):
        _log.removeHandler(h)
    fmt = logging.Formatter("%(message)s")
    try:
        fh = logging.FileHandler("perf.log", mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        _log.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    _log.addHandler(sh)


def _ctx(args) -> str:
    """Cheap context tag from the first real argument (tab idx / slot idx / path)."""
    if len(args) >= 2:
        a = args[1]
        if isinstance(a, bool):
            return ""
        if isinstance(a, int):
            return f"[{a}]"
        if isinstance(a, str) and a:
            return f"[{os.path.basename(a)[:24]}]"
    return ""


def _record(label: str, kind: str, dt_ms: float, args) -> None:
    s = _stats.get(label)
    if s is None:
        s = {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "kind": kind}
        _stats[label] = s
    s["count"] += 1
    s["total_ms"] += dt_ms
    if dt_ms > s["max_ms"]:
        s["max_ms"] = dt_ms
    if kind == _ACTION or dt_ms >= HOT_THRESHOLD_MS:
        try:
            _log.debug("[PERF] %8.1f ms  %s%s", dt_ms, label, _ctx(args))
        except Exception:
            pass


def _wrap(func, label: str, kind: str):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            _record(label, kind, (perf_counter() - t0) * 1000.0, args)

    wrapper.__lsb_probe__ = True  # type: ignore[attr-defined]
    return wrapper


def install() -> bool:
    """Patch the timing probes in. No-op unless LSB_PERF is set. Returns True if active."""
    global _installed
    if _installed or not _enabled():
        return False

    _setup_logger()

    import importlib

    patched, missing = 0, []
    for mod_attr, cls_name, meth_name, kind in _TARGETS:
        try:
            mod = importlib.import_module(f"soundboard.{mod_attr}")
            cls = getattr(mod, cls_name, None)
            if cls is None:
                missing.append(f"{mod_attr}.{cls_name}")
                continue
            func = cls.__dict__.get(meth_name)
            if func is None:
                missing.append(f"{cls_name}.{meth_name}")
                continue
            if getattr(func, "__lsb_probe__", False):
                continue
            label = f"{cls_name}.{meth_name}"
            setattr(cls, meth_name, _wrap(func, label, kind))
            patched += 1
        except Exception as e:  # never let profiling break the app
            missing.append(f"{cls_name}.{meth_name}({e})")

    _installed = True
    atexit.register(dump)
    _log.debug("=" * 64)
    _log.debug("[PERF] probe ACTIVE - %d methods instrumented (missing: %s)", patched, missing or "none")
    _log.debug("[PERF] live lines below; full table on exit. perf.log = this session.")
    _log.debug("=" * 64)
    return True


def reset() -> None:
    _stats.clear()
    _log.debug("[PERF] stats reset")


def dump() -> None:
    """Write the sorted summary table (total time descending)."""
    if not _stats:
        return
    rows = sorted(_stats.items(), key=lambda kv: kv[1]["total_ms"], reverse=True)
    _log.debug("")
    _log.debug("=" * 78)
    _log.debug("[PERF] SUMMARY (sorted by total time)")
    _log.debug("%-42s %7s %11s %9s %9s", "method", "calls", "total ms", "avg ms", "max ms")
    _log.debug("-" * 78)
    for label, s in rows:
        cnt = s["count"]
        tot = s["total_ms"]
        avg = tot / cnt if cnt else 0.0
        _log.debug("%-42s %7d %11.1f %9.2f %9.1f", label[:42], cnt, tot, avg, s["max_ms"])
    _log.debug("=" * 78)
