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
_TEXT_MUTED = "#a3a6aa"
_BLURPLE = "#5865f2"
_RED = "#da373c"


# ---------------------------------------------------------------------------
# Emoji library (emoji-data-python → ~1900 categorised, named emoji).
#
# When the `emoji_data_python` package is available we build BOTH the
# browse-by-category tabs and the search keyword index from it, giving a large,
# properly categorised, searchable set. If the package is ever missing we fall
# back to the curated EMOJI_CATEGORIES above so the picker still works (the
# "won't break" requirement).
# ---------------------------------------------------------------------------

# Library category name -> short tab label (with a representative icon). Order
# here is the order the tabs appear in. "Component" (skin-tone modifiers) is
# intentionally excluded — they aren't standalone emoji.
_CATEGORY_ORDER: List[Tuple[str, str]] = [
    ("Smileys & Emotion", "😀 Smileys"),
    ("People & Body", "👋 People"),
    ("Animals & Nature", "🐶 Animals"),
    ("Food & Drink", "🍔 Food"),
    ("Travel & Places", "✈️ Travel"),
    ("Activities", "⚽ Activities"),
    ("Objects", "💡 Objects"),
    ("Symbols", "❤️ Symbols"),
    ("Flags", "🏳️ Flags"),
]

# Cap search results so a broad query (e.g. "a") doesn't rasterise the whole set.
_MAX_SEARCH_RESULTS = 240

_CATEGORIES: Optional[List[Tuple[str, List[str]]]] = None
_SEARCH_INDEX: Optional[List[Tuple[str, str]]] = None


def _load_library() -> Tuple[Optional[List[Tuple[str, List[str]]]], Optional[List[Tuple[str, str]]]]:
    """Build (categories, search_index) from emoji-data-python, or (None, None)."""
    try:
        import emoji_data_python as edp
    except Exception:
        return None, None
    try:
        by_cat: Dict[str, List[str]] = {}
        index: List[Tuple[str, str]] = []
        seen: set = set()
        for em in sorted(edp.emoji_data, key=lambda e: getattr(e, "sort_order", 0) or 0):
            char = getattr(em, "char", None)
            cat = getattr(em, "category", None)
            if not char or cat in (None, "Component") or char in seen:
                continue
            seen.add(char)
            by_cat.setdefault(cat, []).append(char)
            names = [getattr(em, "name", "") or ""]
            names.extend(getattr(em, "short_names", []) or [])
            kw = " ".join(names).lower().replace("_", " ").replace("-", " ")
            index.append((char, kw))
        categories: List[Tuple[str, List[str]]] = []
        for cat_name, label in _CATEGORY_ORDER:
            chars = by_cat.get(cat_name)
            if chars:
                categories.append((label, chars))
        if not categories:
            return None, None
        return categories, index
    except Exception:
        return None, None


def _ensure_loaded() -> None:
    global _CATEGORIES, _SEARCH_INDEX
    if _CATEGORIES is not None:
        return
    cats, index = _load_library()
    if cats is not None:
        _CATEGORIES = cats
        _SEARCH_INDEX = index
    else:
        # Fallback: curated set drives both browse and search.
        _CATEGORIES = EMOJI_CATEGORIES
        fallback_index: List[Tuple[str, str]] = []
        seen: set = set()
        for label, emojis in EMOJI_CATEGORIES:
            cat_kw = label.split(" ", 1)[-1].lower()
            for c in emojis:
                if c in seen:
                    continue
                seen.add(c)
                fallback_index.append((c, cat_kw))
        _SEARCH_INDEX = fallback_index


def _categories() -> List[Tuple[str, List[str]]]:
    _ensure_loaded()
    return _CATEGORIES or EMOJI_CATEGORIES


def _build_search_index() -> List[Tuple[str, str]]:
    _ensure_loaded()
    return _SEARCH_INDEX or []


def _search_emojis(query: str) -> List[str]:
    """Return emoji chars whose keywords match every whitespace-separated term."""
    q = query.strip().lower()
    if not q:
        return []
    terms = q.split()
    out: List[str] = []
    for char, kw in _build_search_index():
        if all(t in kw for t in terms):
            out.append(char)
            if len(out) >= _MAX_SEARCH_RESULTS:
                break
    return out


class _EmojiPickerDialog:
    """Modal Tk emoji picker. Use `pick_emoji(parent)` instead of this directly.

    Two modes share one scrollable grid:
      * **Browse** — click a category tab to see that curated set.
      * **Search** — type in the box to query the full (~4000-emoji) library
        by name/keyword; results replace the grid live (debounced).

    The grid is *responsive*: column count is derived from the canvas width so
    the window fits its content at any size and never needs a manual resize.
    """

    BUTTON_SIZE = 46   # pixels per emoji button (square-ish)
    EMOJI_PIXELS = 32  # rasterised emoji image side length (bigger = easier to read)
    CELL = 54          # button + padding footprint used to compute columns
    SEARCH_DEBOUNCE_MS = 200
    _PLACEHOLDER = "🔍  Search emoji by name (e.g. fire, heart, cat)…"

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
        self._buttons: List[tk.Button] = []  # currently displayed emoji buttons
        self._cols = 1
        self._active_cat = 0
        self._mode = "category"  # or "search"
        self._search_after: Optional[str] = None
        self._placeholder_on = False

        self.win = tk.Toplevel(parent)
        self.win.title("Choose Emoji")
        self.win.configure(bg=_BG_DARK)
        self.win.geometry("560x560")
        self.win.minsize(360, 360)
        try:
            self.win.transient(parent.winfo_toplevel())  # type: ignore[union-attr]
        except Exception:
            pass
        self.win.grab_set()
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._cat_buttons: List[tk.Button] = []
        self._build_ui()

        # Show first category up front; switching/searching re-renders on demand.
        self._show_category(0)
        self.win.after(60, lambda: self._search_entry.focus_set())

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- Header: title + live search box -------------------------------
        header = tk.Frame(self.win, bg=_BG_DARK)
        header.pack(fill=tk.X, padx=12, pady=(12, 6))
        tk.Label(
            header,
            text="Select an emoji",
            bg=_BG_DARK,
            fg=_TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT)

        search_wrap = tk.Frame(self.win, bg=_BG_MEDIUM, highlightthickness=1,
                               highlightbackground=_BG_LIGHT)
        search_wrap.pack(fill=tk.X, padx=12, pady=(0, 6))
        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            search_wrap,
            textvariable=self._search_var,
            bg=_BG_MEDIUM,
            fg=_TEXT_MUTED,
            insertbackground=_TEXT,
            disabledbackground=_BG_MEDIUM,
            bd=0,
            relief="flat",
            font=("Segoe UI", 11),
        )
        self._search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10, pady=7)
        self._set_placeholder()
        self._search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self._search_entry.bind("<FocusOut>", self._on_search_focus_out)
        self._search_entry.bind("<KeyRelease>", self._on_search_key)
        self._search_entry.bind("<Escape>", lambda _e: self._clear_search())
        # A small clear-search "✕" button.
        self._clear_btn = tk.Button(
            search_wrap, text="✕", bg=_BG_MEDIUM, fg=_TEXT_MUTED, activebackground=_BG_HOVER,
            activeforeground=_TEXT, bd=0, relief="flat", font=("Segoe UI", 10), cursor="hand2",
            command=self._clear_search,
        )
        self._clear_btn.pack(side=tk.RIGHT, padx=(0, 8))

        # --- Category tabs: colour icon + short label, wrapped into a grid so
        # every category is clearly named AND the row always fits the window
        # (a single pack row of 9 labelled tabs would clip — the "needs
        # resizing" bug). 5 per row → two tidy rows for the 9 categories.
        cat_bar = tk.Frame(self.win, bg=_BG_DARK)
        cat_bar.pack(fill=tk.X, padx=12, pady=(0, 6))
        self._tab_img_refs: List = []
        cats = _categories()
        per_row = 5
        for c in range(per_row):
            cat_bar.grid_columnconfigure(c, weight=1, uniform="cat")
        for i, (label, _emojis) in enumerate(cats):
            parts = label.split(" ", 1)
            icon = parts[0]
            name = parts[1] if len(parts) > 1 else label
            icon_img = emoji_render.get_tk_image(icon, 18)
            btn = tk.Button(
                cat_bar,
                text=f" {name}",
                image=icon_img if icon_img is not None else "",
                compound=tk.LEFT if icon_img is not None else tk.NONE,
                bg=_BG_MEDIUM,
                fg=_TEXT,
                activebackground=_BG_HOVER,
                activeforeground=_TEXT,
                bd=0,
                relief="flat",
                padx=6,
                pady=4,
                font=("Segoe UI", 9, "bold"),
                anchor="w",
                cursor="hand2",
                command=lambda idx=i: self._show_category(idx),
            )
            if icon_img is not None:
                self._tab_img_refs.append(icon_img)
            btn.grid(row=i // per_row, column=i % per_row, padx=2, pady=2, sticky="ew")
            self._cat_buttons.append(btn)

        # --- Scrollable grid area ------------------------------------------
        body = tk.Frame(self.win, bg=_BG_DARK, highlightthickness=1, highlightbackground=_BG_MEDIUM)
        body.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)

        self._canvas = tk.Canvas(body, bg=_BG_DARK, highlightthickness=0, bd=0)
        # Scrollbar is ALWAYS present so the canvas width is stable — this
        # prevents the show/hide-scrollbar width flap that would otherwise
        # bounce the responsive column count back and forth.
        scrollbar = tk.Scrollbar(body, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._grid_frame = tk.Frame(self._canvas, bg=_BG_DARK)
        self._grid_window = self._canvas.create_window((0, 0), window=self._grid_frame, anchor="nw")
        self._grid_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        def _on_wheel(event: tk.Event) -> None:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._canvas.bind("<MouseWheel>", _on_wheel)
        self._grid_frame.bind("<MouseWheel>", _on_wheel)

        # --- Footer: status count + Clear / Cancel -------------------------
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

        self._status = tk.Label(
            footer, text="", bg=_BG_DARK, fg=_TEXT_MUTED, font=("Segoe UI", 9)
        )
        self._status.pack(side=tk.LEFT, padx=12)

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
    # Search box: placeholder + debounced querying
    # ------------------------------------------------------------------

    def _set_placeholder(self) -> None:
        self._placeholder_on = True
        self._search_entry.delete(0, tk.END)
        self._search_entry.insert(0, self._PLACEHOLDER)
        self._search_entry.configure(fg=_TEXT_MUTED)

    def _on_search_focus_in(self, _e: tk.Event) -> None:
        if self._placeholder_on:
            self._placeholder_on = False
            self._search_entry.delete(0, tk.END)
            self._search_entry.configure(fg=_TEXT)

    def _on_search_focus_out(self, _e: tk.Event) -> None:
        if not self._search_entry.get().strip():
            self._set_placeholder()

    def _on_search_key(self, event: tk.Event) -> None:
        if self._placeholder_on:
            return
        # Right-align live for Hebrew/Arabic queries (cosmetic; keywords are
        # English so results stay empty, but the field shouldn't misbehave).
        try:
            from .rtl import is_rtl_dominant

            self._search_entry.configure(
                justify="right" if is_rtl_dominant(self._search_var.get()) else "left"
            )
        except Exception:
            pass
        if self._search_after is not None:
            try:
                self.win.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.win.after(self.SEARCH_DEBOUNCE_MS, self._do_search)

    def _do_search(self) -> None:
        self._search_after = None
        query = "" if self._placeholder_on else self._search_var.get().strip()
        if not query:
            # Empty query → fall back to the active category browse view.
            self._show_category(self._active_cat)
            return
        self._mode = "search"
        for btn in self._cat_buttons:
            btn.configure(bg=_BG_MEDIUM, fg=_TEXT)
        results = _search_emojis(query)
        self._populate(results)
        if not results:
            self._status.configure(text=f'No emoji match "{query}"')
        else:
            capped = " (showing first %d)" % _MAX_SEARCH_RESULTS if len(results) >= _MAX_SEARCH_RESULTS else ""
            self._status.configure(text=f"{len(results)} result(s){capped}")

    def _clear_search(self) -> None:
        self._placeholder_on = False
        self._search_entry.delete(0, tk.END)
        self._search_entry.configure(justify="left")
        if self.win.focus_get() is not self._search_entry:
            self._set_placeholder()
        self._show_category(self._active_cat)

    # ------------------------------------------------------------------
    # Category browse
    # ------------------------------------------------------------------

    def _restore_status(self) -> None:
        """Re-show the status line for the current view (after a hover preview)."""
        cats = _categories()
        if self._mode == "category" and 0 <= self._active_cat < len(cats):
            label, emojis = cats[self._active_cat]
            self._status.configure(text=f"{label} · {len(emojis)} emoji")
        else:
            self._status.configure(text=f"{len(self._buttons)} result(s)")

    def _show_category(self, idx: int) -> None:
        self._mode = "category"
        self._active_cat = idx
        for i, btn in enumerate(self._cat_buttons):
            if i == idx:
                btn.configure(bg=_BLURPLE, fg="white")
            else:
                btn.configure(bg=_BG_MEDIUM, fg=_TEXT)
        cats = _categories()
        idx = max(0, min(idx, len(cats) - 1))
        label, emojis = cats[idx]
        self._populate(list(emojis))
        self._status.configure(text=f"{label} · {len(emojis)} emoji")

    # ------------------------------------------------------------------
    # Grid build (create widgets) + layout (responsive re-grid)
    # ------------------------------------------------------------------

    def _populate(self, emojis: List[str]) -> None:
        """Swap in a freshly built grid for a new emoji set, flicker-free.

        The new buttons are built and laid out in an OFF-SCREEN frame first,
        then swapped in for the old one in a single step (old frame destroyed
        only after the new one is shown). This removes the destroy→empty→rebuild
        "refresh break" (grey boxes) the user saw when switching/searching.
        """
        try:
            cw = self._canvas.winfo_width()
        except Exception:
            cw = 0
        if cw <= 1:
            cw = 560
        cols = max(1, self._cols_for_width(cw))

        new_frame = tk.Frame(self._canvas, bg=_BG_DARK)
        new_refs: List = []
        new_buttons: List[tk.Button] = []
        for emoji in emojis:
            new_buttons.append(self._make_emoji_button(emoji, new_frame, new_refs))
        for i, btn in enumerate(new_buttons):
            btn.grid(row=i // cols, column=i % cols, padx=2, pady=2)
        new_frame.grid_columnconfigure(cols, weight=1)
        try:
            new_frame.update_idletasks()  # fully laid out before it is ever shown
        except Exception:
            pass

        old_frame = self._grid_frame
        old_window = self._grid_window
        self._grid_frame = new_frame
        self._buttons = new_buttons
        self._image_refs = new_refs
        self._cols = cols
        self._grid_window = self._canvas.create_window(
            (0, 0), window=new_frame, anchor="nw", width=cw
        )
        new_frame.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all") or (0, 0, 0, 0)),
        )
        try:
            self._canvas.delete(old_window)
        except Exception:
            pass
        if old_frame is not None:
            try:
                old_frame.destroy()
            except Exception:
                pass
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all") or (0, 0, 0, 0))
            self._canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _cols_for_width(self, width: int) -> int:
        return max(1, (width - 6) // self.CELL)

    def _on_canvas_configure(self, event: tk.Event) -> None:
        # Keep the inner frame the width of the canvas, then re-grid only if the
        # column count actually changed (avoids needless relayout churn).
        self._canvas.itemconfigure(self._grid_window, width=event.width)
        cols = self._cols_for_width(event.width)
        if cols != self._cols:
            self._layout(cols)

    def _layout(self, cols: Optional[int] = None, reset_scroll: bool = False) -> None:
        if cols is None:
            try:
                cols = self._cols_for_width(self._canvas.winfo_width())
            except Exception:
                cols = 10
        cols = max(1, cols)
        self._cols = cols
        for i, btn in enumerate(self._buttons):
            btn.grid(row=i // cols, column=i % cols, padx=2, pady=2)
        # Clear stale column weights, then give the trailing column weight so the
        # block of buttons sits flush left and never inherits spacing from a
        # previous (wider) category.
        for c in range(64):
            self._grid_frame.grid_columnconfigure(c, weight=0)
        self._grid_frame.grid_columnconfigure(cols, weight=1)
        # CRITICAL: flush the geometry NOW so bbox("all") reflects the new
        # button set, then pin the scrollregion to it. Without the explicit
        # update the scrollregion can lag a frame behind on a debounced search,
        # leaving a too-tall region (phantom empty space you can scroll into).
        try:
            self._grid_frame.update_idletasks()
            bbox = self._canvas.bbox("all") or (0, 0, 0, 0)
            self._canvas.configure(scrollregion=bbox)
        except Exception:
            pass
        if reset_scroll:
            try:
                self._canvas.yview_moveto(0.0)
            except Exception:
                pass

    def _make_emoji_button(self, emoji: str, parent: tk.Misc, refs: List) -> tk.Button:
        img = emoji_render.get_tk_image(emoji, self.EMOJI_PIXELS)
        if img is not None:
            refs.append(img)
            btn = tk.Button(
                parent,
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
            btn = tk.Button(
                parent,
                text=emoji,
                bg=_BG_DARK,
                fg=_TEXT,
                activebackground=_BG_LIGHT,
                activeforeground=_TEXT,
                bd=0,
                relief="flat",
                cursor="hand2",
                highlightthickness=0,
                font=("Segoe UI Emoji", 18),
                width=2,
                height=1,
                command=lambda e=emoji: self._select(e),
            )

        def _enter(_e: tk.Event, b: tk.Button = btn) -> None:
            b.configure(bg=_BG_HOVER)

        def _leave(_e: tk.Event, b: tk.Button = btn) -> None:
            b.configure(bg=_BG_DARK)

        btn.bind("<Enter>", _enter)
        btn.bind("<Leave>", _leave)
        # Let wheel events over a button still scroll the canvas.
        btn.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
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
        if self._search_after is not None:
            try:
                self.win.after_cancel(self._search_after)
            except Exception:
                pass
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
