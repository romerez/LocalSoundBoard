"""
Discord Soundboard Package

A local Windows application that plays sound effects through Discord
by mixing microphone input with audio files and routing the output
through a virtual audio cable.
"""

from .audio import AudioMixer, SoundCache
from .editor import SoundEditor, edit_sound_file
from .gui import SoundboardApp
from .models import SoundSlot, SoundTab

__all__ = [
    "AudioMixer",
    "SoundCache",
    "SoundboardApp",
    "SoundSlot",
    "SoundTab",
    "SoundEditor",
    "edit_sound_file",
]
__version__ = "1.1.0"
