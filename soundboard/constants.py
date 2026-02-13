"""
Constants and configuration values for the Discord Soundboard.
"""

# Discord-style colors
COLORS = {
    "bg_dark": "#2C2F33",
    "bg_medium": "#40444B",
    "blurple": "#7289DA",
    "green": "#43B581",
    "red": "#F04747",
    "text_primary": "#FFFFFF",
    "text_muted": "#8E9297",
}

# Audio settings
AUDIO = {
    "sample_rate": 48000,  # Discord standard
    "block_size": 1024,
    "channels": 2,
}

# UI settings
UI = {
    "window_title": "Discord Soundboard",
    "window_size": "800x600",
    "grid_columns": 4,
    "grid_rows": 3,
    "total_slots": 12,
}

# File settings
CONFIG_FILE = "soundboard_config.json"
SUPPORTED_FORMATS = ("*.mp3", "*.wav", "*.ogg", "*.flac")
