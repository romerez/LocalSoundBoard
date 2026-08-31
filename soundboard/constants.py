"""
Constants and configuration values for the Discord Soundboard.

Uses emoji-data-python for emoji categories and defines Discord-style theme colors.
"""

from functools import lru_cache
from typing import Dict, List, Tuple, Optional

from colour import Color  # type: ignore[import-untyped]
from emoji_data_python import emoji_data, EmojiChar  # type: ignore[import-untyped]


# =============================================================================
# EMOJI SUPPORT (from emoji-data-python library)
# =============================================================================

# Category name mappings with emoji prefixes for display
EMOJI_CATEGORY_DISPLAY_NAMES = {
    "Smileys & Emotion": "😀 Faces & Emotions",
    "People & Body": "👍 Gestures & Body",
    "Animals & Nature": "🐾 Animals & Nature",
    "Food & Drink": "🍕 Food & Drink",
    "Travel & Places": "🚗 Travel & Places",
    "Activities": "🎉 Activities",
    "Objects": "🏠 Objects",
    "Symbols": "⬆️ Symbols",
    "Flags": "🏳️ Flags",
}

# Categories to exclude from picker (skin tone modifiers, etc.)
EXCLUDED_CATEGORIES = {"Component"}


@lru_cache(maxsize=1)
def _build_emoji_categories() -> Dict[str, List[str]]:
    """
    Build emoji categories from emoji-data-python library.
    Results are cached for performance.
    """
    categories: Dict[str, List[str]] = {}

    for emoji_obj in emoji_data:
        category = emoji_obj.category
        if category in EXCLUDED_CATEGORIES:
            continue

        # Use display name if available, otherwise raw category
        display_name = EMOJI_CATEGORY_DISPLAY_NAMES.get(category, category)
        if display_name is None:
            continue

        if display_name not in categories:
            categories[display_name] = []

        # Get the actual emoji character
        emoji_char = emoji_obj.char
        if emoji_char and emoji_char not in categories[display_name]:
            categories[display_name].append(emoji_char)

    # Sort categories for consistent display order
    sorted_categories = {}
    preferred_order = [
        "😀 Faces & Emotions",
        "👍 Gestures & Body",
        "🐾 Animals & Nature",
        "🍕 Food & Drink",
        "🚗 Travel & Places",
        "🎉 Activities",
        "🏠 Objects",
        "⬆️ Symbols",
        "🏳️ Flags",
    ]

    for name in preferred_order:
        if name in categories:
            sorted_categories[name] = categories[name]

    # Add any remaining categories not in preferred order
    for name, emojis in categories.items():
        if name not in sorted_categories:
            sorted_categories[name] = emojis

    return sorted_categories


def get_emoji_categories() -> Dict[str, List[str]]:
    """Get emoji categories (lazy-loaded on first call, then cached via lru_cache)."""
    return _build_emoji_categories()


def get_default_emojis() -> List[str]:
    """Get flat list of all emojis (lazy-loaded)."""
    result: List[str] = []
    for emojis in get_emoji_categories().values():
        result.extend(emojis)
    return result


# =============================================================================
# DISCORD-STYLE COLOR PALETTE
# =============================================================================


class DiscordColors:
    """Discord-style color palette with semantic naming."""

    # Background layers (darkest to lightest)
    BG_DARKEST = "#1E1F22"  # Deepest background
    BG_DARK = "#2B2D31"  # Main background
    BG_MEDIUM = "#313338"  # Card/panel background
    BG_LIGHT = "#3F4147"  # Elevated elements
    BG_LIGHTER = "#4E5058"  # Hover states

    # Accent colors
    BLURPLE = "#5865F2"  # Primary Discord brand color
    BLURPLE_HOVER = "#4752C4"
    GREEN = "#23A559"  # Success/positive
    GREEN_HOVER = "#1E8E4D"
    RED = "#DA373C"  # Danger/stop
    RED_HOVER = "#B62D31"
    YELLOW = "#F0B232"  # Warning

    # Playback states
    PLAYING = "#F5A623"  # Orange for playing to Discord
    PLAYING_GLOW = "#F5A62333"  # With alpha for glow effect
    PREVIEW = "#23A559"  # Green for preview playback

    # Interactive states
    DRAG_TARGET = "#5865F2"  # Bright blurple for drop target
    DRAG_TARGET_GLOW = "#5865F233"

    # Text colors
    TEXT_PRIMARY = "#F2F3F5"
    TEXT_SECONDARY = "#B5BAC1"
    TEXT_MUTED = "#80848E"
    TEXT_LINK = "#00AFF4"

    # Borders and separators
    BORDER = "#3F4147"
    BORDER_STRONG = "#4E5058"


# Dict-style access for backward compatibility
COLORS = {
    "bg_darkest": DiscordColors.BG_DARKEST,
    "bg_dark": DiscordColors.BG_DARK,
    "bg_medium": DiscordColors.BG_MEDIUM,
    "bg_light": DiscordColors.BG_LIGHT,
    "bg_lighter": DiscordColors.BG_LIGHTER,
    "blurple": DiscordColors.BLURPLE,
    "blurple_hover": DiscordColors.BLURPLE_HOVER,
    "green": DiscordColors.GREEN,
    "green_hover": DiscordColors.GREEN_HOVER,
    "red": DiscordColors.RED,
    "red_hover": DiscordColors.RED_HOVER,
    "yellow": DiscordColors.YELLOW,
    "playing": DiscordColors.PLAYING,
    "playing_glow": DiscordColors.PLAYING_GLOW,
    "preview": DiscordColors.PREVIEW,
    "drag_target": DiscordColors.DRAG_TARGET,
    "drag_target_glow": DiscordColors.DRAG_TARGET_GLOW,
    "text_primary": DiscordColors.TEXT_PRIMARY,
    "text_secondary": DiscordColors.TEXT_SECONDARY,
    "text_muted": DiscordColors.TEXT_MUTED,
    "text_link": DiscordColors.TEXT_LINK,
    "border": DiscordColors.BORDER,
    "border_strong": DiscordColors.BORDER_STRONG,
}


# =============================================================================
# SLOT COLOR PALETTE (for customization)
# =============================================================================

# The slot color palette is organised into themed families. Each family is its
# own dict so the picker can group/label them; ``ALL_SLOT_COLORS`` is the flat
# merge the UI iterates over. Names are the dict keys (must stay unique); hex
# values should stay unique too because the config dialog reverse-maps a slot's
# stored hex back to a name (first exact match wins).

# Standard colors — the original Discord-flavoured core set.
SLOT_COLORS = {
    "Default": DiscordColors.BLURPLE,
    "Red": "#DA373C",
    "Orange": "#F5A623",
    "Yellow": "#F0B232",
    "Green": "#23A559",
    "Teal": "#1ABC9C",
    "Cyan": "#00AFF4",
    "Blue": "#3498DB",
    "Purple": "#9B59B6",
    "Pink": "#E91E8C",
    "Magenta": "#EB459E",
    "Gray": "#5C6370",
}

# Neon colors (vibrant, high-saturation)
NEON_COLORS = {
    "Neon Pink": "#FF10F0",
    "Neon Purple": "#BC13FE",
    "Neon Blue": "#04D9FF",
    "Neon Cyan": "#00FFFF",
    "Neon Green": "#39FF14",
    "Neon Lime": "#CCFF00",
    "Neon Yellow": "#FFFF00",
    "Neon Orange": "#FF6600",
    "Neon Red": "#FF0040",
    "Neon Coral": "#FF355E",
    "Electric Blue": "#0066FF",
    "Hot Magenta": "#FF00CC",
}

# Pastel colors (soft, low-saturation — easy on the eyes, great for grouping)
PASTEL_COLORS = {
    "Pastel Red": "#FF8A80",
    "Pastel Coral": "#FFAB91",
    "Pastel Orange": "#FFB870",
    "Pastel Yellow": "#FFE082",
    "Pastel Lime": "#C5E1A5",
    "Pastel Green": "#A5D6A7",
    "Pastel Mint": "#A7E8D2",
    "Pastel Teal": "#80CBC4",
    "Pastel Sky": "#90CAF9",
    "Pastel Blue": "#9FA8DA",
    "Pastel Lavender": "#B39DDB",
    "Pastel Purple": "#CE93D8",
    "Pastel Pink": "#F8BBD0",
    "Pastel Rose": "#F4A7C0",
}

# Vivid colors (rich, fully-saturated jewel-ish tones)
VIVID_COLORS = {
    "Crimson": "#DC143C",
    "Scarlet": "#FF2400",
    "Tangerine": "#F28500",
    "Amber": "#FFBF00",
    "Gold": "#FFD700",
    "Chartreuse": "#7FFF00",
    "Emerald": "#2ECC71",
    "Jade": "#00A86B",
    "Turquoise": "#06BFB4",
    "Aqua": "#19D3DA",
    "Azure": "#1E90FF",
    "Cobalt": "#2849D8",
    "Indigo": "#4B0082",
    "Violet": "#8F00FF",
    "Orchid": "#DA70D6",
    "Fuchsia": "#FF1493",
    "Rose": "#FF407A",
    "Salmon": "#FA8072",
}

# Earth tones (warm, natural browns / greens / sand)
EARTH_COLORS = {
    "Brown": "#8B5A2B",
    "Chocolate": "#7B3F00",
    "Coffee": "#6F4E37",
    "Sienna": "#A0522D",
    "Rust": "#B7410E",
    "Terracotta": "#E2725B",
    "Sand": "#C2B280",
    "Khaki": "#BDB76B",
    "Olive": "#808000",
    "Moss": "#8A9A5B",
    "Forest": "#228B22",
    "Pine": "#01796F",
}

# Deep / dark jewel tones (great for low-key, moody slots)
DEEP_COLORS = {
    "Maroon": "#800000",
    "Wine": "#722F37",
    "Burgundy": "#8D021F",
    "Plum": "#5A2A5A",
    "Eggplant": "#3D2352",
    "Navy": "#1F3A93",
    "Midnight": "#191970",
    "Deep Teal": "#014D4E",
    "Deep Green": "#0B5345",
    "Slate": "#4A5568",
    "Steel Blue": "#3A6EA5",
    "Royal": "#3B2F8F",
}

# Monochrome (grayscale ramp for neutral / labelling slots)
MONO_COLORS = {
    "Onyx": "#1B1B1F",
    "Graphite": "#2F3136",
    "Charcoal": "#36393F",
    "Steel": "#71797E",
    "Stone": "#9095A0",
    "Silver": "#B9BDC6",
    "Cloud": "#D3D7DE",
    "White": "#F5F6F8",
}

# Grouped families in display order (the picker may use this to add headings).
SLOT_COLOR_GROUPS = {
    "Standard": SLOT_COLORS,
    "Neon": NEON_COLORS,
    "Pastel": PASTEL_COLORS,
    "Vivid": VIVID_COLORS,
    "Earth": EARTH_COLORS,
    "Deep": DEEP_COLORS,
    "Mono": MONO_COLORS,
}

# Combined palette for slot customization (flat name -> hex, in family order)
ALL_SLOT_COLORS = {
    **SLOT_COLORS,
    **NEON_COLORS,
    **PASTEL_COLORS,
    **VIVID_COLORS,
    **EARTH_COLORS,
    **DEEP_COLORS,
    **MONO_COLORS,
}


# =============================================================================
# COLOR UTILITIES (using colour library)
# =============================================================================


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """Convert hex color to RGB tuple (0-255)."""
    c = Color(hex_color)
    return (int(c.red * 255), int(c.green * 255), int(c.blue * 255))


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB values (0-255) to hex color."""
    return f"#{r:02x}{g:02x}{b:02x}"


def lighten_color(hex_color: str, amount: float = 0.2) -> str:
    """Lighten a color by the given amount (0.0-1.0)."""
    c = Color(hex_color)
    c.luminance = min(1.0, c.luminance + amount)
    return c.hex_l


def darken_color(hex_color: str, amount: float = 0.2) -> str:
    """Darken a color by the given amount (0.0-1.0)."""
    c = Color(hex_color)
    c.luminance = max(0.0, c.luminance - amount)
    return c.hex_l


def saturate_color(hex_color: str, amount: float = 0.2) -> str:
    """Increase saturation of a color."""
    c = Color(hex_color)
    c.saturation = min(1.0, c.saturation + amount)
    return c.hex_l


def desaturate_color(hex_color: str, amount: float = 0.2) -> str:
    """Decrease saturation of a color."""
    c = Color(hex_color)
    c.saturation = max(0.0, c.saturation - amount)
    return c.hex_l


def get_complementary_color(hex_color: str) -> str:
    """Get the complementary (opposite) color."""
    c = Color(hex_color)
    c.hue = (c.hue + 0.5) % 1.0
    return c.hex_l


def generate_color_gradient(start_hex: str, end_hex: str, steps: int = 5) -> List[str]:
    """Generate a gradient between two colors."""
    start = Color(start_hex)
    end = Color(end_hex)
    return [c.hex_l for c in start.range_to(end, steps)]


def _srgb_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _hex_to_rgb(hex_color: str) -> tuple:
    h = (hex_color or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return (88, 101, 242)  # blurple


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance (0 = black, 1 = white) of an #rrggbb colour."""
    r, g, b = _hex_to_rgb(hex_color)
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG contrast ratio between two colours (1 .. 21)."""
    la, lb = relative_luminance(hex_a), relative_luminance(hex_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_light_color(hex_color: str) -> bool:
    """True when DARK text reads better on this background than light text.

    Uses WCAG relative luminance, NOT HSL lightness: the old ``Color.luminance``
    (= HSL L) called saturated mid-tones such as blurple #5865F2, red #DA373C,
    purple #9B59B6 and pink #E91E63 "light" and put near-black text on them,
    which is why a blurple chip had a dark ⋮ next to a white title.
    """
    text_light = DiscordColors.TEXT_PRIMARY
    text_dark = "#1E1F22"
    return contrast_ratio(hex_color, text_dark) > contrast_ratio(hex_color, text_light)


def get_text_color_for_bg(hex_color: str) -> str:
    """The text colour (light or dark) with the higher WCAG contrast on ``hex_color``."""
    return "#1E1F22" if is_light_color(hex_color) else DiscordColors.TEXT_PRIMARY


def tint_for_dark_bg(hex_color: str, min_ratio: float = 4.5, against: str = None) -> str:
    """Lighten ``hex_color`` (mix toward white) until it reaches ``min_ratio``
    contrast on a dark background — for coloured TEXT on the dark UI, where a
    user-picked navy/black would otherwise vanish. Unchanged if already legible."""
    against = against or DiscordColors.BG_LIGHT
    r, g, b = _hex_to_rgb(hex_color)
    for step in range(0, 21):
        t = step / 20.0
        rr, gg, bb = (round(r + (255 - r) * t), round(g + (255 - g) * t), round(b + (255 - b) * t))
        cand = f"#{rr:02X}{gg:02X}{bb:02X}"
        if contrast_ratio(cand, against) >= min_ratio:
            return cand
    return DiscordColors.TEXT_PRIMARY


# =============================================================================
# FONT CONFIGURATION
# =============================================================================


class FontConfig:
    """Font family and size configuration."""

    # Segoe UI Emoji supports colored emojis while still rendering regular text well
    FAMILY = "Segoe UI Emoji"
    FAMILY_TEXT = "Segoe UI"  # For pure text without emojis
    FAMILY_MONO = "JetBrains Mono"

    SIZE_XS = 10
    SIZE_SM = 11
    SIZE_MD = 13
    SIZE_LG = 15
    SIZE_XL = 18
    SIZE_XXL = 24


# Dict-style access for backward compatibility
FONTS = {
    "family": FontConfig.FAMILY,
    "family_text": FontConfig.FAMILY_TEXT,
    "family_mono": FontConfig.FAMILY_MONO,
    "size_xs": FontConfig.SIZE_XS,
    "size_sm": FontConfig.SIZE_SM,
    "size_md": FontConfig.SIZE_MD,
    "size_lg": FontConfig.SIZE_LG,
    "size_xl": FontConfig.SIZE_XL,
    "size_xxl": FontConfig.SIZE_XXL,
}


# =============================================================================
# SOUND GROUPS / TYPES (for filtering and organization)
# =============================================================================
# AUDIO SETTINGS
# =============================================================================

AUDIO = {
    "sample_rate": 48000,  # Discord standard
    "block_size": 1024,
    "channels": 2,
}


# =============================================================================
# UI SETTINGS
# =============================================================================

UI = {
    "window_title": "Discord Soundboard",
    "grid_columns": 4,
    "grid_rows": 3,
    "total_slots": 12,
    "corner_radius": 8,
    "slot_corner_radius": 10,
    "button_corner_radius": 6,
    "padding": 12,
    "slot_padding": 4,
    # Control-size tokens (logical px). Chosen so 1.5x (the user's 150 % DPI)
    # lands on an EVEN device size — CTk floors scaled canvases to even px, so
    # 26/30/34-tall controls leave a 1 px strip of parent colour along one edge.
    "control_height": 28,        # inline buttons / entries / option menus
    "toolbar_height": 32,        # top action bar buttons
    "compact_height": 24,        # dense rows (DJ list, status bar, in-card ＋/⋮)
    "footer_button_height": 36,  # dialog footer buttons
    "icon_button": 28,           # square single-glyph buttons (✎ ⋮ ✕ ⚙ ＋)
    "pill_width": 4,             # identity colour pill next to a name
    # Main-board slot tiles: False keeps the on-screen tile height / text size
    # the user is used to (the canvas ignores DPI for its geometry, as it has
    # since the single-canvas refactor); True makes the tile DPI-correct
    # (152 logical → 228 device px at 150 %, text -22 px) — bigger, fewer rows.
    # Thumbnails and emoji are decoded at device pixels (crisp) either way.
    "slot_scale_geometry": False,
    "slot_width": 180,
    "slot_height": 152,  # 120 main + 32 bottom bar
    # Now Playing / DJ Looper panel settings
    "now_playing_width": 360,
    "now_playing_item_height": 140,
}


# =============================================================================
# EDITOR SETTINGS
# =============================================================================

EDITOR = {
    "max_duration_warning": 5.0,  # Warn if sound is longer than 5 seconds
    "default_zoom": 1.0,
    "max_zoom": 50.0,
    "min_zoom": 1.0,
}


# =============================================================================
# FILE/PATH SETTINGS
# =============================================================================

CONFIG_FILE = "soundboard_config.json"
SOUNDS_DIR = "sounds"
IMAGES_DIR = "images"

# Supported audio formats (used in file dialogs)
SUPPORTED_FORMATS = (
    "*.mp3",
    "*.wav",
    "*.ogg",
    "*.flac",
    "*.m4a",
    "*.aac",
    "*.wma",
    "*.aiff",
    "*.aif",
    "*.opus",
    "*.webm",
    "*.mp4",
    "*.wv",
    "*.ape",
)

# Supported image formats (used in file dialogs)
SUPPORTED_IMAGE_FORMATS = (
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.jfif",
    "*.gif",
    "*.bmp",
    "*.ico",
)
