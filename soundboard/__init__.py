"""
Discord Soundboard Package

A local Windows application that plays sound effects through Discord
by mixing microphone input with audio files and routing the output
through a virtual audio cable.
"""

from .audio import AudioMixer, SoundCache
from .editor import SoundEditor, edit_sound_file
from .gui import SoundboardApp
from .models import SoundSlot

__all__ = [
    "AudioMixer",
    "SoundCache",
    "SoundboardApp",
    "SoundSlot",
    "SoundEditor",
    "edit_sound_file",
]
__version__ = "1.0.0"
