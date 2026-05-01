"""Cross-module shared state for performance coordination.

Lives in its own module to avoid circular imports between `gui` and
`slot_widget`. Both modules read and write `RESIZE_STATE['until']` to
coordinate deferring expensive redraws while the user is actively
resizing or moving the window.
"""

from typing import Any, Dict

# When `time.time() < RESIZE_STATE['until']`, widgets should skip
# expensive draw work and reschedule for after the resize ends.
RESIZE_STATE: Dict[str, Any] = {"until": 0.0}
