"""System tray icon integration via pystray.

Runs a tray icon in a background thread. The tray menu lets the user
restore the hidden window or quit the application.

Usage:
    tray = SystemTray(
        on_show=lambda: app.root.deiconify(),
        on_quit=lambda: app.root.event_generate("<<TrayQuit>>"),
        title="Discord Soundboard",
    )
    tray.start()
    ...
    tray.stop()

Notes:
* `pystray` callbacks fire on its own thread. Don't touch Tk directly
  from them — instead, schedule the work onto the Tk thread (e.g. via
  `root.after(0, ...)` or `event_generate`).
* The icon is generated procedurally if no .ico is bundled, so the tray
  always has *something* visible.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import pystray
    from pystray import Menu, MenuItem
    from PIL import Image, ImageDraw

    _AVAILABLE = True
except Exception:  # pragma: no cover - pystray/PIL missing
    _AVAILABLE = False


def is_available() -> bool:
    return _AVAILABLE


def _make_default_icon() -> "Image.Image":
    """Create a simple Discord-blurple speaker icon as fallback."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Rounded square background
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(88, 101, 242, 255))
    # Speaker body (rectangle)
    draw.rectangle((20, 26, 30, 38), fill=(255, 255, 255, 255))
    # Speaker cone (triangle)
    draw.polygon([(30, 22), (44, 14), (44, 50), (30, 42)], fill=(255, 255, 255, 255))
    # Sound waves
    draw.arc((42, 18, 54, 46), start=300, end=60, fill=(255, 255, 255, 255), width=2)
    return img


class SystemTray:
    """Tray icon manager. Safe no-op if pystray is unavailable."""

    def __init__(
        self,
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "Discord Soundboard",
        icon_image: Optional["Image.Image"] = None,
    ) -> None:
        self._on_show = on_show
        self._on_quit = on_quit
        self._title = title
        self._icon: Optional["pystray.Icon"] = None
        self._thread: Optional[threading.Thread] = None

        if not _AVAILABLE:
            return

        image = icon_image if icon_image is not None else _make_default_icon()

        def _menu_show(icon: "pystray.Icon", item: "MenuItem") -> None:
            try:
                self._on_show()
            except Exception:
                pass

        def _menu_quit(icon: "pystray.Icon", item: "MenuItem") -> None:
            try:
                self._on_quit()
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass

        menu = Menu(
            MenuItem("Show Soundboard", _menu_show, default=True),
            Menu.SEPARATOR,
            MenuItem("Quit", _menu_quit),
        )
        self._icon = pystray.Icon(
            "soundboard", image, title, menu
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the tray icon on a background thread (idempotent)."""
        if not _AVAILABLE or self._icon is None:
            return
        if self._thread is not None and self._thread.is_alive():
            return

        def _run() -> None:
            try:
                # Blocking — runs the platform tray loop until stop().
                self._icon.run()  # type: ignore[union-attr]
            except Exception:
                pass

        self._thread = threading.Thread(target=_run, daemon=True, name="tray-icon")
        self._thread.start()

    def stop(self) -> None:
        """Tear the tray icon down."""
        if self._icon is not None:
            try:
                self._icon.visible = False
            except Exception:
                pass
            try:
                self._icon.stop()
            except Exception:
                pass
        self._thread = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def show_notification(self, message: str, title: Optional[str] = None) -> None:
        """Show a balloon/toast notification (best-effort)."""
        if self._icon is None:
            return
        try:
            self._icon.notify(message, title or self._title)
        except Exception:
            pass
