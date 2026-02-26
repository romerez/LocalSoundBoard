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

# Standard colors
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

# Combined palette for slot customization
ALL_SLOT_COLORS = {**SLOT_COLORS, **NEON_COLORS}


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


def is_light_color(hex_color: str) -> bool:
    """Check if a color is light (for text contrast decisions)."""
    c = Color(hex_color)
    return c.luminance > 0.5


def get_text_color_for_bg(hex_color: str) -> str:
    """Get appropriate text color (light or dark) for a background."""
    return DiscordColors.TEXT_PRIMARY if not is_light_color(hex_color) else "#1E1F22"


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
    "window_size": "800x700",
    "window_size_with_panel": "1100x700",  # With side panel open
    "grid_columns": 4,
    "grid_rows": 3,
    "total_slots": 12,
    "corner_radius": 8,
    "slot_corner_radius": 10,
    "button_corner_radius": 6,
    "padding": 12,
    "slot_padding": 8,
    # Now Playing panel settings
    "now_playing_width": 280,
    "now_playing_item_height": 60,
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
