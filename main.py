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
