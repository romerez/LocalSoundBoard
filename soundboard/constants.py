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

# Editor settings
EDITOR = {
    "max_duration_warning": 5.0,  # Warn if sound is longer than 5 seconds
    "default_zoom": 1.0,
    "max_zoom": 50.0,
    "min_zoom": 1.0,
}

# File settings
CONFIG_FILE = "soundboard_config.json"
SOUNDS_DIR = "sounds"  # Local folder for storing sound files
SUPPORTED_FORMATS = (
    "*.mp3",  # MP3 audio
    "*.wav",  # WAV audio
    "*.ogg",  # OGG Vorbis
    "*.flac",  # FLAC lossless
    "*.m4a",  # AAC/MPEG-4 audio
    "*.aac",  # AAC audio
    "*.wma",  # Windows Media Audio
    "*.aiff",  # Apple audio
    "*.aif",  # Apple audio (alt extension)
    "*.opus",  # Opus codec
    "*.webm",  # WebM audio
    "*.mp4",  # MPEG-4 (audio track)
    "*.wv",  # WavPack
    "*.ape",  # Monkey's Audio
)
