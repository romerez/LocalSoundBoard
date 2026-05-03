"""Native Tkinter emoji picker with true-color emoji rendering.

This replaces the previous PyQt6-subprocess-based picker, which had
recurring problems: subprocess startup latency, venv-vs-system-Python
mismatches, frozen-EXE failures, and occasional event-loop deadlocks
on the parent Tk window.

The new picker is a plain `tk.Toplevel` (so no Qt at all) that uses
`soundboard.emoji_render` to rasterise each glyph through Pillow +
`seguiemj.ttf`, then displays the resulting PhotoImages on `tk.Button`
widgets. A category bar sits at the top, plus Clear / Cancel.

Public API (kept compatible with the old module):
    pick_emoji(parent=None) -> Optional[str]
        Returns the chosen emoji ("" if cleared, None if cancelled).
    PYQT_AVAILABLE: bool   # legacy flag, now always True (picker works
                            # without PyQt6).
"""

from __future__ import annotations

import tkinter as tk
from typing import Dict, List, Optional, Tuple

from . import emoji_render

# Legacy compat flag — gui.py checks this; the new picker has no Qt
# dependency so we always advertise "available" as long as Tk is.
PYQT_AVAILABLE = True


# ---------------------------------------------------------------------------
# Emoji set, grouped by category for tabbing.
# ---------------------------------------------------------------------------

EMOJI_CATEGORIES: List[Tuple[str, List[str]]] = [
    (
        "😀 Faces",
        [
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
            "🤖",
            "😺",
        ],
    ),
    (
        "👍 Gestures",
        [
            "👋",
            "🤚",
            "🖐",
            "✋",
            "🖖",
            "👌",
            "🤌",
            "🤏",
            "✌️",
            "🤞",
            "🤟",
            "🤘",
            "🤙",
            "👈",
            "👉",
            "👆",
            "🖕",
            "👇",
            "☝️",
            "👍",
            "👎",
            "✊",
            "👊",
            "🤛",
            "🤜",
            "👏",
            "🙌",
            "👐",
            "🤲",
            "🤝",
            "🙏",
            "✍️",
            "💅",
            "🤳",
            "💪",
            "🦾",
            "🦵",
            "🦿",
            "🦶",
            "👂",
            "🦻",
            "👃",
            "🧠",
            "🫀",
            "🫁",
            "🦷",
            "🦴",
            "👀",
            "👁",
            "👅",
            "👄",
            "💋",
            "🩸",
        ],
    ),
    (
        "🐶 Animals",
        [
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
            "🙈",
            "🙉",
            "🙊",
            "🐒",
            "🐔",
            "🐧",
            "🐦",
            "🐤",
            "🐣",
            "🐥",
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
            "🕷",
            "🦂",
            "🐢",
            "🐍",
            "🦎",
            "🦖",
            "🦕",
            "🐙",
            "🦑",
            "🦐",
            "🦞",
            "🦀",
            "🐡",
            "🐠",
            "🐟",
            "🐬",
            "🐳",
            "🐋",
            "🦈",
        ],
    ),
    (
        "🍔 Food",
        [
            "🍏",
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
            "🍆",
            "🥑",
            "🥦",
            "🥬",
            "🥒",
            "🌶",
            "🌽",
            "🥕",
            "🧄",
            "🧅",
            "🥔",
            "🍠",
            "🥐",
            "🥯",
            "🍞",
            "🥖",
            "🥨",
            "🧀",
            "🥚",
            "🍳",
            "🧈",
            "🥞",
            "🧇",
            "🥓",
            "🥩",
            "🍗",
            "🍖",
            "🌭",
            "🍔",
            "🍟",
            "🍕",
            "🥪",
            "🥙",
            "🧆",
            "🌮",
            "🌯",
            "🥗",
            "🥘",
            "🥫",
            "🍝",
            "🍜",
            "🍲",
            "🍛",
            "🍣",
            "🍱",
            "🥟",
            "🦪",
            "🍤",
            "🍙",
            "🍚",
            "🍘",
            "🍥",
            "🍦",
            "🍧",
            "🍨",
            "🍩",
            "🍪",
            "🎂",
            "🍰",
            "🧁",
            "🥧",
            "🍫",
            "🍬",
            "🍭",
            "🍮",
            "🍯",
            "🍼",
            "🥛",
            "☕",
            "🍵",
            "🍶",
            "🍾",
            "🍷",
            "🍸",
            "🍹",
            "🍺",
            "🍻",
            "🥂",
            "🥃",
            "🥤",
            "🧃",
            "🧉",
        ],
    ),
    (
        "⚽ Activities",
        [
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
            "🪀",
            "🏓",
            "🏸",
            "🏒",
            "🏑",
            "🥍",
            "🏏",
            "🥅",
            "⛳",
            "🪁",
            "🏹",
            "🎣",
            "🤿",
            "🥊",
            "🥋",
            "🎽",
            "🛹",
            "🛼",
            "🛷",
            "⛸",
            "🥌",
            "🎿",
            "⛷",
            "🏂",
            "🪂",
            "🏋️",
            "🤼",
            "🤸",
            "⛹️",
            "🤺",
            "🤾",
            "🏌️",
            "🏇",
            "🧘",
            "🏄",
            "🏊",
            "🤽",
            "🚣",
            "🧗",
            "🚵",
            "🚴",
            "🏆",
            "🥇",
            "🥈",
            "🥉",
            "🏅",
            "🎖",
            "🏵",
            "🎗",
            "🎫",
            "🎟",
            "🎪",
            "🤹",
            "🎭",
            "🩰",
            "🎨",
            "🎬",
            "🎤",
            "🎧",
            "🎼",
            "🎹",
            "🥁",
            "🪘",
            "🎷",
            "🎺",
            "🪗",
            "🎸",
            "🪕",
            "🎻",
            "🎲",
            "♟",
            "🎯",
            "🎳",
            "🎮",
            "🎰",
            "🧩",
        ],
    ),
    (
        "❤️ Symbols",
        [
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
            "✨",
            "⭐",
            "🌟",
            "💫",
            "💥",
            "🔥",
            "💧",
            "🌊",
            "💯",
            "💢",
            "💤",
            "✅",
            "❌",
            "⭕",
            "🛑",
            "⛔",
            "🚫",
            "❗",
            "❕",
            "❓",
            "❔",
            "‼️",
            "⁉️",
            "♻️",
            "🆕",
            "🆗",
            "🆙",
            "🆒",
            "🔔",
            "🔕",
            "🔆",
            "🔅",
            "📶",
            "📵",
            "🔞",
            "0️⃣",
            "1️⃣",
            "2️⃣",
            "3️⃣",
            "4️⃣",
            "5️⃣",
            "6️⃣",
            "7️⃣",
            "8️⃣",
            "9️⃣",
            "🔟",
        ],
    ),
    (
        "🎵 Objects",
        [
            "🎶",
            "🎵",
            "🎙️",
            "🎚",
            "🎛",
            "🎤",
            "🎧",
            "📻",
            "🎷",
            "🎸",
            "🎹",
            "🎺",
            "🎻",
            "🥁",
            "📞",
            "☎️",
            "💻",
            "🖥",
            "🖨",
            "⌨️",
            "🖱",
            "💽",
            "💾",
            "💿",
            "📀",
            "🎥",
            "🎞",
            "📽",
            "📺",
            "📷",
            "📸",
            "📹",
            "📼",
            "🔍",
            "🔎",
            "🕯",
            "💡",
            "🔦",
            "📔",
            "📕",
            "📖",
            "📗",
            "📘",
            "📙",
            "📚",
            "📒",
            "📃",
            "📜",
            "📄",
            "📰",
            "📑",
            "🔖",
            "🏷",
            "💰",
            "💵",
            "💸",
            "💳",
            "✉️",
            "📧",
            "📦",
            "✏️",
            "✒️",
            "🖋",
            "🖊",
            "📝",
            "💼",
            "📁",
            "📂",
            "📅",
            "📆",
            "📈",
            "📉",
            "📊",
            "📋",
            "📌",
            "📍",
            "📎",
            "📏",
            "📐",
            "✂️",
            "🗑",
            "🔒",
            "🔓",
            "🔑",
            "🔨",
            "🛠",
            "⚙️",
            "🔧",
            "🔩",
            "⛓",
            "🧰",
            "🧲",
            "🧪",
            "🔬",
            "🔭",
            "📡",
            "💉",
            "💊",
            "🚪",
            "🛏",
            "🛋",
            "🪑",
            "🚽",
            "🚿",
            "🛁",
            "🧴",
            "🧹",
            "🧺",
            "🧻",
            "🧼",
            "🧽",
            "🧯",
            "🛒",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Picker dialog
# ---------------------------------------------------------------------------


# Discord-ish dark palette so the dialog matches the rest of the UI.
_BG_DARK = "#1e1f22"
_BG_MEDIUM = "#2b2d31"
_BG_LIGHT = "#383a40"
_BG_HOVER = "#42454d"
_TEXT = "#f2f3f5"
_BLURPLE = "#5865f2"
_RED = "#da373c"


class _EmojiPickerDialog:
    """Modal Tk emoji picker. Use `pick_emoji(parent)` instead of this directly."""

    GRID_COLS = 10
    BUTTON_SIZE = 36  # pixels per emoji button (square-ish)
    EMOJI_PIXELS = 26  # rasterised emoji image side length

    def __init__(self, parent: Optional[tk.Misc] = None) -> None:
        # Stand-alone Tk root if no parent provided (e.g. tests).
        self._owns_root = False
        if parent is None:
            self._root = tk.Tk()
            self._owns_root = True
            parent = self._root
        else:
            self._root = parent.winfo_toplevel()  # type: ignore[union-attr]

        self.result: Optional[str] = None
        self._image_refs: List = []  # keep PhotoImages alive

        self.win = tk.Toplevel(parent)
        self.win.title("Choose Emoji")
        self.win.configure(bg=_BG_DARK)
        self.win.geometry("520x540")
        self.win.minsize(420, 380)
        try:
            self.win.transient(parent.winfo_toplevel())  # type: ignore[union-attr]
        except Exception:
            pass
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._cat_buttons: List[tk.Button] = []
        self._build_ui()

        # Render first category up front; switching renders on demand.
        self._show_category(0)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Header
        header = tk.Frame(self.win, bg=_BG_DARK)
        header.pack(fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            header,
            text="Select an emoji",
            bg=_BG_DARK,
            fg=_TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT)

        # Category tabs
        cat_bar = tk.Frame(self.win, bg=_BG_DARK)
        cat_bar.pack(fill=tk.X, padx=12, pady=(0, 6))
        for i, (label, _emojis) in enumerate(EMOJI_CATEGORIES):
            btn = tk.Button(
                cat_bar,
                text=label,
                bg=_BG_MEDIUM,
                fg=_TEXT,
                activebackground=_BG_HOVER,
                activeforeground=_TEXT,
                bd=0,
                relief="flat",
                font=("Segoe UI Emoji", 9),
                padx=8,
                pady=3,
                cursor="hand2",
                command=lambda idx=i: self._show_category(idx),
            )
            btn.pack(side=tk.LEFT, padx=2)
            self._cat_buttons.append(btn)

        # Scrollable grid area
        body = tk.Frame(self.win, bg=_BG_DARK, highlightthickness=1, highlightbackground=_BG_MEDIUM)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self._canvas = tk.Canvas(body, bg=_BG_DARK, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(body, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Inner frame that holds the emoji buttons; gets re-built per category.
        self._grid_frame = tk.Frame(self._canvas, bg=_BG_DARK)
        self._grid_window = self._canvas.create_window((0, 0), window=self._grid_frame, anchor="nw")
        self._grid_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._grid_window, width=e.width),
        )

        # Mousewheel scrolling
        def _on_wheel(event: tk.Event) -> None:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._canvas.bind("<MouseWheel>", _on_wheel)
        self._grid_frame.bind("<MouseWheel>", _on_wheel)

        # Footer with Clear / Cancel
        footer = tk.Frame(self.win, bg=_BG_DARK)
        footer.pack(fill=tk.X, padx=12, pady=(6, 12))

        tk.Button(
            footer,
            text="Clear emoji",
            bg=_RED,
            fg="white",
            activebackground="#bf2e33",
            activeforeground="white",
            bd=0,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            padx=12,
            pady=6,
            cursor="hand2",
            command=lambda: self._select(""),
        ).pack(side=tk.LEFT)

        tk.Button(
            footer,
            text="Cancel",
            bg=_BG_MEDIUM,
            fg=_TEXT,
            activebackground=_BG_HOVER,
            activeforeground=_TEXT,
            bd=0,
            relief="flat",
            font=("Segoe UI", 9),
            padx=12,
            pady=6,
            cursor="hand2",
            command=self._on_cancel,
        ).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Category switching
    # ------------------------------------------------------------------

    def _show_category(self, idx: int) -> None:
        # Style active tab
        for i, btn in enumerate(self._cat_buttons):
            if i == idx:
                btn.configure(bg=_BLURPLE, fg="white")
            else:
                btn.configure(bg=_BG_MEDIUM, fg=_TEXT)

        # Clear current grid
        for child in self._grid_frame.winfo_children():
            child.destroy()
        self._image_refs.clear()

        emojis = EMOJI_CATEGORIES[idx][1]
        for i, emoji in enumerate(emojis):
            row = i // self.GRID_COLS
            col = i % self.GRID_COLS
            self._make_emoji_button(emoji).grid(row=row, column=col, padx=2, pady=2)

        # Equal column weights so they spread out a little.
        for c in range(self.GRID_COLS):
            self._grid_frame.grid_columnconfigure(c, weight=1)

        # Reset scroll to top.
        self._canvas.yview_moveto(0.0)

    def _make_emoji_button(self, emoji: str) -> tk.Button:
        img = emoji_render.get_tk_image(emoji, self.EMOJI_PIXELS)
        if img is not None:
            self._image_refs.append(img)
            btn = tk.Button(
                self._grid_frame,
                image=img,
                bg=_BG_DARK,
                activebackground=_BG_LIGHT,
                bd=0,
                relief="flat",
                cursor="hand2",
                width=self.BUTTON_SIZE,
                height=self.BUTTON_SIZE,
                highlightthickness=0,
                command=lambda e=emoji: self._select(e),
            )
        else:
            # Fallback: text-only (monochrome).
            btn = tk.Button(
                self._grid_frame,
                text=emoji,
                bg=_BG_DARK,
                fg=_TEXT,
                activebackground=_BG_LIGHT,
                activeforeground=_TEXT,
                bd=0,
                relief="flat",
                cursor="hand2",
                font=("Segoe UI Emoji", 16),
                width=2,
                command=lambda e=emoji: self._select(e),
            )

        # Hover tint
        def _enter(_e: tk.Event, b: tk.Button = btn) -> None:
            b.configure(bg=_BG_HOVER)

        def _leave(_e: tk.Event, b: tk.Button = btn) -> None:
            b.configure(bg=_BG_DARK)

        btn.bind("<Enter>", _enter)
        btn.bind("<Leave>", _leave)
        return btn

    # ------------------------------------------------------------------
    # Result handling
    # ------------------------------------------------------------------

    def _select(self, emoji: str) -> None:
        self.result = emoji
        self._close()

    def _on_cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        try:
            self.win.grab_release()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        if self._owns_root:
            try:
                self._root.destroy()
            except Exception:
                pass

    def show(self) -> Optional[str]:
        try:
            self.win.wait_window()
        except Exception:
            pass
        return self.result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def pick_emoji(parent: Optional[tk.Misc] = None) -> Optional[str]:
    """Show the emoji picker and return the chosen emoji.

    Returns:
        The emoji string, "" if the user clicked Clear, or None if
        cancelled.

    The `parent` argument is the parent Tk widget (any widget will do —
    the picker uses `winfo_toplevel()` to find the proper parent).
    """
    dlg = _EmojiPickerDialog(parent)
    return dlg.show()


# Standalone entry point — kept so existing scripts that still launch
# this module as a subprocess don't break. New callers should just
# `from soundboard.emoji_picker import pick_emoji`.
if __name__ == "__main__":
    import sys

    result = pick_emoji(None)
    if result is not None:
        print(f"EMOJI:{result}")
    else:
        print("CANCELLED")
    sys.exit(0)
