#!/usr/bin/env python3
"""
Discord Soundboard - Main Entry Point

Run this file to start the application:
    python main.py
"""

import logging

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
    main()
