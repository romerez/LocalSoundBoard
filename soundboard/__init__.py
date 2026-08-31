"""
Discord Soundboard Package

A local Windows application that plays sound effects through Discord
by mixing microphone input with audio files and routing the output
through a virtual audio cable.

Exports are lazy (PEP 562): ``from soundboard import SoundboardApp`` still
works, but importing a light submodule (e.g. ``soundboard.mobile_sync`` in
headless tests) no longer drags in the whole GUI + audio dependency stack.
"""

_LAZY_EXPORTS = {
    "AudioMixer": ("soundboard.audio", "AudioMixer"),
    "SoundCache": ("soundboard.audio", "SoundCache"),
    "Recorder": ("soundboard.audio", "Recorder"),
    "SoundboardApp": ("soundboard.gui", "SoundboardApp"),
    "SoundSlot": ("soundboard.models", "SoundSlot"),
    "SoundTab": ("soundboard.models", "SoundTab"),
    "SoundEditor": ("soundboard.editor", "SoundEditor"),
    "edit_sound_file": ("soundboard.editor", "edit_sound_file"),
}

__all__ = list(_LAZY_EXPORTS)
__version__ = "1.2.21"


def __getattr__(name):
    try:
        module_name, attr = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value  # cache so the import machinery runs once
    return value
