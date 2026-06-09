"""App-wide right-to-left (Hebrew / Arabic) text support.

Tkinter 8.6 implements **no** BiDi algorithm: it lays every character out in
logical order, left-to-right. For Hebrew/Arabic that means text renders
*reversed* (the first typed letter ends up on the far left instead of the far
right). We therefore convert stored/logical text into *visual* order with
``python-bidi`` before handing it to any non-editable Tk widget (Label, Button,
Canvas, Menu, tab chips, ...). Tk then paints that visual string left-to-right
and the user sees correct RTL.

Rules of thumb:
  * DISPLAY text (labels, buttons, menu entries, canvas) -> ``to_display()``.
  * EDITABLE text (Entry/Text the user types into) must stay in *logical*
    order so it can be stored/round-tripped; only its *alignment* is adjusted
    (``justify`` right for RTL-dominant). Never feed entry contents through
    ``to_display()`` or the saved value would be reversed.

If ``python-bidi`` is unavailable the helpers degrade to a no-op so the app
still runs (text just renders as before).
"""

from __future__ import annotations

import re

# Strong right-to-left script ranges (Hebrew, Arabic, Syriac, supplements) plus
# the Hebrew/Arabic presentation-form blocks. Explicit \u escapes so the source
# stays ASCII and unambiguous.
RTL_PATTERN = re.compile(
    "["
    "֐-׿"  # Hebrew
    "؀-ۿ"  # Arabic
    "܀-ݏ"  # Syriac
    "ݐ-ݿ"  # Arabic Supplement
    "ހ-޿"  # Thaana
    "ࢠ-ࣿ"  # Arabic Extended-A
    "יִ-ﭏ"  # Hebrew presentation forms
    "ﭐ-﷿"  # Arabic presentation forms-A
    "ﹰ-﻿"  # Arabic presentation forms-B
    "]"
)

try:  # python-bidi is the only hard dependency for correct RTL.
    from bidi.algorithm import get_display as _get_display

    _BIDI_OK = True
except Exception:  # pragma: no cover - fallback path
    _get_display = None  # type: ignore[assignment]
    _BIDI_OK = False


def has_rtl(text: str) -> bool:
    """True if the text contains any strong right-to-left character."""
    return bool(text) and bool(RTL_PATTERN.search(text))


def is_rtl_dominant(text: str) -> bool:
    """True when the text is primarily RTL (>= as many RTL as Latin letters)."""
    if not text:
        return False
    rtl_count = len(RTL_PATTERN.findall(text))
    ltr_count = len(re.findall(r"[A-Za-z]", text))
    return rtl_count > 0 and rtl_count >= ltr_count


def to_display(text: str) -> str:
    """Convert logical text to visual order for a non-editable Tk widget.

    Pure-LTR strings (English, numbers, symbols) are returned unchanged, so
    this is safe to call indiscriminately on any label text. Newlines are
    preserved and each line is reordered independently so multi-line labels
    keep their line structure and per-line base direction.
    """
    if not text or not _BIDI_OK:
        return text
    # Fast path: nothing RTL -> BiDi is a no-op, skip the work entirely.
    if not RTL_PATTERN.search(text):
        return text
    try:
        if "\n" in text:
            return "\n".join(
                _get_display(ln) if ln else ln for ln in text.split("\n")  # type: ignore[misc]
            )
        return _get_display(text)  # type: ignore[misc]
    except Exception:
        return text


def entry_justify(text: str) -> str:
    """Return the ``justify`` value an editable field should use for ``text``."""
    return "right" if is_rtl_dominant(text) else "left"
