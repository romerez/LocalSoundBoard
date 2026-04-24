#!/usr/bin/env python3
"""
Discord Soundboard - Main Entry Point

Run this file to start the application:
    python main.py
"""

import logging
import os
import sys

# When running as a frozen exe, set the working directory to the exe's folder
# so that sounds/, images/, and config files are found correctly.
if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))

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
