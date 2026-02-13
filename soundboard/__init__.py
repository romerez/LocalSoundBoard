"""
Discord Soundboard Package

A local Windows application that plays sound effects through Discord
by mixing microphone input with audio files and routing the output
through a virtual audio cable.
"""

from .audio import AudioMixer
from .gui import SoundboardApp
from .models import SoundSlot

__all__ = ["AudioMixer", "SoundboardApp", "SoundSlot"]
__version__ = "1.0.0"
