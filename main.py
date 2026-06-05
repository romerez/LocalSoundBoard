#!/usr/bin/env python3
"""
Discord Soundboard - Main Entry Point

Run this file to start the application:
    python main.py
"""

import logging
import os
import sys

# When running as a frozen exe, anchor the working directory to the workspace
# root (two levels up from the exe inside dist\SoundBoard\) so that
# soundboard_config.json, sounds\, and images\ are shared with dev mode.
if getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(sys.executable)            # ...\dist\SoundBoard
    workspace_root = os.path.dirname(os.path.dirname(exe_dir))  # ...\(workspace)
    os.chdir(workspace_root)


def _enable_dpi_awareness():
    """Make the process per-monitor-**v2** DPI aware before any Tk window exists.

    CustomTkinter only requests the older per-monitor-**v1** mode, which on a
    HiDPI / mixed-DPI multi-monitor setup leaves the UI looking soft ("smudged")
    — especially child windows and after dragging between monitors. v2 lets
    Windows hand DPI changes to the window/non-client area cleanly so everything
    renders crisp. Must run *before* customtkinter/Tk initialises; falls back
    gracefully on older Windows.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (Win10 1703+).
        ctx = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v1
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # system-DPI aware (oldest)
    except Exception:
        pass


_enable_dpi_awareness()

logging.basicConfig(
    filename="debug.log",
    filemode="a",
    level=logging.DEBUG,
    format="[%(name)s] %(message)s",
)

from soundboard import SoundboardApp


def main():
    app = SoundboardApp()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass
