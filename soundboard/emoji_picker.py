"""
PyQt6-based emoji picker with colored emoji support.

This module provides a standalone emoji picker dialog that renders
colored emojis properly on Windows. It can be called from the main
Tkinter application via subprocess to avoid event loop conflicts.
"""

import sys
import subprocess
import os
from typing import Optional, cast

# Try to import PyQt6
try:
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QVBoxLayout,
        QHBoxLayout,
        QGridLayout,
        QScrollArea,
        QWidget,
        QPushButton,
        QLabel,
        QFrame,
    )
    from PyQt6.QtCore import Qt, QSize
    from PyQt6.QtGui import QFont, QColor, QPalette

    PYQT_AVAILABLE = True
except ImportError:
    PYQT_AVAILABLE = False


# Discord-style colors
COLORS = {
    "bg_dark": "#1e1f22",
    "bg_medium": "#2b2d31",
    "bg_light": "#383a40",
    "text_primary": "#f2f3f5",
    "text_muted": "#949ba4",
    "blurple": "#5865f2",
    "red": "#da373c",
    "green": "#23a55a",
}

# Full emoji set
EMOJIS = [
    # Faces & Emotions
    "😀",
    "😃",
    "😄",
    "😁",
    "😆",
    "😅",
    "🤣",
    "😂",
    "🙂",
    "😊",
    "😇",
    "🥰",
    "😍",
    "🤩",
    "😘",
    "😗",
    "😚",
    "😙",
    "🥲",
    "😋",
    "😛",
    "😜",
    "🤪",
    "😝",
    "🤑",
    "🤗",
    "🤭",
    "🤫",
    "🤔",
    "🤐",
    "🤨",
    "😐",
    "😑",
    "😶",
    "😏",
    "😒",
    "🙄",
    "😬",
    "😮‍💨",
    "🤥",
    "😌",
    "😔",
    "😪",
    "🤤",
    "😴",
    "😷",
    "🤒",
    "🤕",
    "🤢",
    "🤮",
    "🤧",
    "🥵",
    "🥶",
    "🥴",
    "😵",
    "🤯",
    "🤠",
    "🥳",
    "🥸",
    "😎",
    "🤓",
    "🧐",
    "😕",
    "😟",
    "🙁",
    "😮",
    "😯",
    "😲",
    "😳",
    "🥺",
    "😦",
    "😧",
    "😨",
    "😰",
    "😥",
    "😢",
    "😭",
    "😱",
    "😖",
    "😣",
    "😞",
    "😓",
    "😩",
    "😫",
    "🥱",
    "😤",
    "😡",
    "😠",
    "🤬",
    "😈",
    "👿",
    "💀",
    "☠️",
    "💩",
    "🤡",
    "👹",
    "👺",
    "👻",
    "👽",
    "👾",
    # Gestures & Body
    "💪",
    "👍",
    "👎",
    "👊",
    "✊",
    "🤛",
    "🤜",
    "👏",
    "🙌",
    "👐",
    "🤲",
    "🤝",
    "🙏",
    "✌️",
    "🤞",
    "🤟",
    "🤘",
    "🤙",
    "👌",
    "🤌",
    "👈",
    "👉",
    "👆",
    "👇",
    "☝️",
    "✋",
    "🤚",
    "🖐️",
    "🖖",
    "👋",
    "🤏",
    "✍️",
    "🦾",
    "🦿",
    "🦵",
    "🦶",
    "👂",
    "🦻",
    "👃",
    "🧠",
    # Hearts & Love
    "❤️",
    "🧡",
    "💛",
    "💚",
    "💙",
    "💜",
    "🖤",
    "🤍",
    "🤎",
    "💔",
    "❣️",
    "💕",
    "💞",
    "💓",
    "💗",
    "💖",
    "💘",
    "💝",
    "💟",
    "♥️",
    # Music & Sound
    "🎵",
    "🎶",
    "🎤",
    "🎧",
    "🎷",
    "🎸",
    "🎹",
    "🎺",
    "🎻",
    "🥁",
    "🔔",
    "🔕",
    "🔊",
    "🔉",
    "🔈",
    "🔇",
    "📢",
    "📣",
    "💬",
    "💭",
    # Activities & Sports
    "⚽",
    "🏀",
    "🏈",
    "⚾",
    "🥎",
    "🎾",
    "🏐",
    "🏉",
    "🥏",
    "🎱",
    "🏓",
    "🏸",
    "🏒",
    "🏑",
    "🥍",
    "🏏",
    "🥅",
    "⛳",
    "🏹",
    "🎣",
    "🎮",
    "🕹️",
    "🎲",
    "🧩",
    "♟️",
    "🎯",
    "🎳",
    "🎰",
    "🃏",
    "🀄",
    # Stars & Effects
    "⭐",
    "🌟",
    "✨",
    "💫",
    "🔥",
    "💥",
    "💢",
    "💦",
    "💨",
    "🕳️",
    "💣",
    "💬",
    "👁️‍🗨️",
    "🗨️",
    "🗯️",
    "💭",
    "💤",
    "🎉",
    "🎊",
    "🎈",
    # Animals
    "🐶",
    "🐱",
    "🐭",
    "🐹",
    "🐰",
    "🦊",
    "🐻",
    "🐼",
    "🐨",
    "🐯",
    "🦁",
    "🐮",
    "🐷",
    "🐸",
    "🐵",
    "🐔",
    "🐧",
    "🐦",
    "🐤",
    "🦆",
    "🦅",
    "🦉",
    "🦇",
    "🐺",
    "🐗",
    "🐴",
    "🦄",
    "🐝",
    "🐛",
    "🦋",
    "🐌",
    "🐞",
    "🐜",
    "🦟",
    "🦗",
    "🕷️",
    "🦂",
    "🐢",
    "🐍",
    "🦎",
    # Food & Drink
    "🍎",
    "🍐",
    "🍊",
    "🍋",
    "🍌",
    "🍉",
    "🍇",
    "🍓",
    "🫐",
    "🍈",
    "🍒",
    "🍑",
    "🥭",
    "🍍",
    "🥥",
    "🥝",
    "🍅",
    "🥑",
    "🍆",
    "🌽",
    "🍕",
    "🍔",
    "🍟",
    "🌭",
    "🍿",
    "🧂",
    "🥓",
    "🥚",
    "🍳",
    "🧇",
    "☕",
    "🍵",
    "🧃",
    "🥤",
    "🍶",
    "🍺",
    "🍻",
    "🥂",
    "🍷",
    "🍸",
    # Symbols & Signs
    "✅",
    "❌",
    "⭕",
    "🚫",
    "⛔",
    "❓",
    "❗",
    "‼️",
    "⁉️",
    "💯",
    "🔴",
    "🟠",
    "🟡",
    "🟢",
    "🔵",
    "🟣",
    "🟤",
    "⚫",
    "⚪",
    "🟥",
    "▶️",
    "⏸️",
    "⏹️",
    "⏺️",
    "⏭️",
    "⏮️",
    "⏩",
    "⏪",
    "🔀",
    "🔁",
    "🔂",
    "🔄",
    "🔃",
    "⬆️",
    "⬇️",
    "⬅️",
    "➡️",
    "↗️",
    "↘️",
    "↙️",
    # Objects
    "🔑",
    "🗝️",
    "🔨",
    "🪓",
    "⛏️",
    "🔧",
    "🔩",
    "🗡️",
    "⚔️",
    "🛡️",
    "💎",
    "💰",
    "💵",
    "💴",
    "💶",
    "💷",
    "💳",
    "🏆",
    "🥇",
    "🥈",
    "🥉",
    "🎖️",
    "🏅",
    "📱",
    "💻",
    "🖥️",
    "🖨️",
    "⌨️",
    "🖱️",
    "💿",
    "📷",
    "📸",
    "📹",
    "🎥",
    "📽️",
    "🎬",
    "📺",
    "📻",
    "🎙️",
    "🔦",
]


# Only define PyQt6 classes if available
if PYQT_AVAILABLE:

    class EmojiButton(QPushButton):
        """Custom button for emoji display with hover effects."""

        def __init__(self, emoji: str, parent=None):
            super().__init__(emoji, parent)
            self.emoji = emoji
            self.setFixedSize(44, 44)
            self.setFont(QFont("Segoe UI Emoji", 22))
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self._update_style(False)

        def _update_style(self, hovered: bool):
            bg = COLORS["bg_light"] if hovered else COLORS["bg_dark"]
            self.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {bg};
                    border: none;
                    border-radius: 6px;
                    padding: 2px;
                }}
            """
            )

        def enterEvent(self, event):
            self._update_style(True)
            super().enterEvent(event)

        def leaveEvent(self, event):
            self._update_style(False)
            super().leaveEvent(event)

    class EmojiPickerDialog(QDialog):
        """PyQt6 emoji picker dialog with colored emoji support."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.selected_emoji: Optional[str] = None
            self._setup_ui()

        def _setup_ui(self):
            self.setWindowTitle("Choose Emoji")
            self.setFixedSize(580, 520)
            self.setStyleSheet(
                f"""
                QDialog {{
                    background-color: {COLORS["bg_dark"]};
                }}
                QLabel {{
                    color: {COLORS["text_primary"]};
                }}
                QScrollArea {{
                    border: 1px solid {COLORS["bg_medium"]};
                    border-radius: 8px;
                    background-color: {COLORS["bg_dark"]};
                }}
                QScrollBar:vertical {{
                    background-color: {COLORS["bg_medium"]};
                    width: 12px;
                    border-radius: 6px;
                }}
                QScrollBar::handle:vertical {{
                    background-color: {COLORS["bg_light"]};
                    border-radius: 6px;
                    min-height: 30px;
                }}
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                    height: 0px;
                }}
            """
            )

            layout = QVBoxLayout(self)
            layout.setContentsMargins(15, 15, 15, 15)
            layout.setSpacing(10)

            # Title
            title = QLabel("🎨 Select an Emoji")
            title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            layout.addWidget(title)

            # Scroll area for emojis
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

            # Container for emoji grid
            container = QWidget()
            container.setStyleSheet(f"background-color: {COLORS['bg_dark']};")
            grid = QGridLayout(container)
            grid.setSpacing(4)
            grid.setContentsMargins(8, 8, 8, 8)

            # Add emoji buttons
            cols = 10
            for idx, emoji in enumerate(EMOJIS):
                row = idx // cols
                col = idx % cols
                btn = EmojiButton(emoji)
                btn.clicked.connect(lambda checked, e=emoji: self._select_emoji(e))
                grid.addWidget(btn, row, col)

            scroll.setWidget(container)
            layout.addWidget(scroll, 1)

            # Button row
            btn_layout = QHBoxLayout()
            btn_layout.setSpacing(10)

            clear_btn = QPushButton("Clear Emoji")
            clear_btn.setFont(QFont("Segoe UI", 10))
            clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            clear_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {COLORS["red"]};
                    color: white;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 16px;
                }}
                QPushButton:hover {{
                    background-color: #c92f34;
                }}
            """
            )
            clear_btn.clicked.connect(lambda: self._select_emoji(""))
            btn_layout.addWidget(clear_btn)

            btn_layout.addStretch()

            cancel_btn = QPushButton("Cancel")
            cancel_btn.setFont(QFont("Segoe UI", 10))
            cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel_btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: {COLORS["bg_medium"]};
                    color: white;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 16px;
                }}
                QPushButton:hover {{
                    background-color: {COLORS["bg_light"]};
                }}
            """
            )
            cancel_btn.clicked.connect(self.reject)
            btn_layout.addWidget(cancel_btn)

            layout.addLayout(btn_layout)

        def _select_emoji(self, emoji: str):
            self.selected_emoji = emoji
            self.accept()

    def get_qapp() -> QApplication:
        """Get or create the QApplication instance."""
        global _qapp
        if _qapp is None:
            # Check if QApplication already exists (e.g., in some environments)
            existing = QApplication.instance()
            if existing is not None and isinstance(existing, QApplication):
                _qapp = cast(QApplication, existing)
            else:
                _qapp = QApplication(sys.argv)
        return _qapp

    def _run_picker_dialog() -> Optional[str]:
        """Run the picker dialog (called in subprocess or standalone)."""
        app = get_qapp()
        dialog = EmojiPickerDialog()
        result = dialog.exec()

        if result == QDialog.DialogCode.Accepted:
            return dialog.selected_emoji
        return None


# Global QApplication instance
_qapp: Optional["QApplication"] = None


def pick_emoji() -> Optional[str]:
    """
    Show the emoji picker dialog and return the selected emoji.

    Runs the PyQt6 picker in a subprocess to avoid event loop conflicts
    with Tkinter.

    Returns:
        The selected emoji string, empty string if cleared, or None if cancelled.
    """
    if not PYQT_AVAILABLE:
        return None

    # Run this module as a subprocess to avoid Tkinter/Qt event loop conflicts
    # Get the path to this module
    module_path = os.path.abspath(__file__)

    # When running as a frozen exe, sys.executable points to the .exe, not Python.
    # In that case, the emoji_picker.py script is bundled alongside and we need
    # a real Python interpreter. Fall back gracefully if unavailable.
    if getattr(sys, 'frozen', False):
        # Frozen exe - try to find a Python interpreter
        # The emoji picker won't work in frozen mode without a system Python
        # For now, return None to skip the emoji picker gracefully
        return None

    python_exe = sys.executable

    try:
        # Run the picker as a subprocess
        result = subprocess.run(
            [python_exe, module_path],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        # Parse the output
        output = result.stdout.strip()
        if output.startswith("EMOJI:"):
            emoji = output[6:]  # Remove "EMOJI:" prefix
            return emoji  # Can be empty string (cleared) or emoji
        elif output == "CANCELLED":
            return None
        else:
            return None
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        print(f"Emoji picker error: {e}")
        return None


# Run as standalone subprocess for emoji picking
if __name__ == "__main__":
    if PYQT_AVAILABLE:
        result = _run_picker_dialog()
        if result is not None:
            # Output format: EMOJI:<emoji> or EMOJI: (empty for cleared)
            print(f"EMOJI:{result}")
        else:
            print("CANCELLED")
    else:
        print("ERROR:PyQt6 not installed")
