# Copilot Instructions - Discord Soundboard Project

> **Last Updated:** 2026-02
> **Status:** Active Development
> **Language:** Python 3.x

---

## Project Overview

A local Windows application that plays sound effects through Discord by mixing microphone input with audio files and routing the output through a virtual audio cable.

### Architecture

```
[Physical Microphone] ──┐
                        ├──► [Python App (AudioMixer)] ──► [VB-Audio Virtual Cable] ──► Discord Input
[Sound Files (.mp3/.wav)] ──┘
```

### Core Purpose

Replace Discord's built-in soundboard with a standalone, local solution that:
- Runs independently of Discord
- Supports global hotkeys
- Mixes real mic audio with sound effects in real-time
- Outputs to a virtual audio device that Discord reads as a microphone

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.x |
| GUI | Tkinter |
| Audio I/O | `sounddevice` |
| Audio Files | `soundfile` |
| Audio Processing | `numpy` |
| Global Hotkeys | `keyboard` |
| Config Storage | JSON |

### Dependencies

```
sounddevice>=0.4.6
soundfile>=0.12.1
numpy>=1.24.0
keyboard>=0.13.5
librosa>=0.10.0
```

---

## File Structure

```
LocalSoundBoardProject/
├── main.py                 # Application entry point
├── soundboard/             # Main package
│   ├── __init__.py         # Package exports (SoundboardApp, AudioMixer, SoundCache, SoundSlot, SoundTab, SoundEditor)
│   ├── constants.py        # Colors, audio settings, UI config
│   ├── models.py           # SoundSlot and SoundTab dataclasses
│   ├── audio.py            # AudioMixer and SoundCache classes
│   ├── editor.py           # SoundEditor class (waveform, trimming, preview)
│   └── gui.py              # SoundboardApp GUI class
├── sounds/                 # Local sound storage folder (auto-created)
├── images/                 # Local image storage folder (auto-created)
├── soundboard.py           # Legacy entry point (deprecated)
├── soundboard_config.json  # Auto-generated user config (sounds, hotkeys, volumes)
├── copilot-instructions.md # This file - project specification
└── requirements.txt        # Python dependencies
```

---

## Current Features

- [x] Real-time microphone passthrough
- [x] Mix multiple sounds simultaneously
- [x] Unlimited configurable sound slots (4-column grid with scrolling)
- [x] Per-slot volume control
- [x] Global hotkey support (e.g., `ctrl+1`, `F1`)
- [x] Mic volume slider (0-150%)
- [x] Mic mute toggle
- [x] Stop all sounds button
- [x] Auto-save/load configuration
- [x] Discord-style dark theme UI
- [x] Device selection dropdowns (input/output)
- [x] Support for MP3, WAV, OGG, FLAC, M4A, AAC, WMA, AIFF, Opus, WebM, MP4, WavPack, APE formats
- [x] Local sound storage (`sounds/` folder)
- [x] In-memory sound caching for instant playback
- [x] Pre-loaded audio data at startup
- [x] Sound editing with waveform visualization
- [x] Sound trimming (cut from start/end points)
- [x] Zoom in/out for precise editing
- [x] Preview playback of selected portion
- [x] Warning for sounds longer than 5 seconds
- [x] Push-to-Talk integration (auto-press Discord PTT key when sounds play)
- [x] Tabbed sound organization (create/rename/delete tabs)
- [x] Tab emojis for visual identification
- [x] Slot emojis for quick sound identification
- [x] Custom images/pictures on sound slots
- [x] Emoji picker dialog with categories and scrolling
- [x] Comprehensive emoji library (500+ emojis in 13 categories)
- [x] Backward-compatible config migration
- [x] Visual playback progress bar on sound slots
- [x] Playing state color indicator (orange/amber)
- [x] Preview play/pause/resume/stop controls in sound editor
- [x] Local speaker monitoring (hear sounds through your speakers while playing to Discord)
- [x] Per-slot preview button (🔊) to test sounds locally before streaming
- [x] Tab-aware progress bars (no cross-tab visual issues when switching)
- [x] PTT release debounce (prevents premature release with rapid sound clicks)
- [x] Collapsible audio options panel (closed by default)
- [x] Auto-start stream on launch (configurable)
- [x] "+" button on empty slots to quickly add sounds
- [x] Fixed-size UI elements (locked window and slot dimensions)
- [x] Tab-aware progress bars (no cross-tab visual issues when switching)
- [x] Drag-and-drop slot reordering (within same tab)
- [x] Drag-and-drop to move sounds between tabs
- [x] Unlimited sounds per tab with scrolling support
- [x] Preview progress bar with distinct green color
- [x] Different colors for play modes (orange=Discord, green=preview)
- [x] Improved OGG file loading via pydub fallback
- [x] Better scrollbar visibility (only shows when needed)
- [x] Improved slot sizing (larger slots for better text visibility)
- [x] Edit button (✏️) on sound slots for quick access to settings
- [x] Right-click popup for quick volume/speed adjustment
- [x] Playback speed adjustment per sound (0.5x to 2x)
- [x] Custom color selection for sound slots (12 color palette)
- [x] Per-slot stop button (■) appears while sound is playing
- [x] Global "Stop All Sounds" button in audio options
- [x] Pitch preservation option for speed changes (uses librosa time-stretch)

---

## Planned Features / Backlog

### High Priority
- [ ] Fix volume above 100% not making sounds louder (soft clipping needs work)
- [ ] UI design overhaul (modernize look and feel)

### Medium Priority
- [ ] Drag-and-drop sound file import (from file explorer)
- [ ] Search/filter sounds
- [ ] Add a sound group/type so we can later filter it or search
- [ ] Looping sounds option
- [ ] Fade in/out effects
- [ ] Add stream deck integration

### Low Priority
- [ ] Import/export config profiles (sounds, images, tabs)
- [ ] System tray minimization
- [ ] Auto-start with Windows
- [ ] Discord Rich Presence
- [ ] Emoji search by description in picker

---

## Code Architecture

### Main Classes

#### `SoundSlot` (dataclass)
Represents a single sound button configuration.
```python
@dataclass
class SoundSlot:
    name: str                    # Display name
    file_path: str               # Path to audio file
    hotkey: str | None           # Global hotkey (e.g., "ctrl+1")
    volume: float                # 0.0 to 1.5
    emoji: str | None            # Emoji character to display
    image_path: str | None       # Path to custom image/gif
    color: str | None            # Custom background color (hex)
    speed: float                 # Playback speed (0.5 to 2.0)
    preserve_pitch: bool         # True = natural sound (librosa), False = chipmunk/deep voice
```

#### `SoundTab` (dataclass)
Represents a tab containing sound slots.
```python
@dataclass
class SoundTab:
    name: str                              # Tab display name
    emoji: str | None                      # Emoji for tab
    slots: Dict[int, SoundSlot]            # Sound slots in this tab
```

#### `AudioMixer`
Handles real-time audio processing.
- Captures microphone input
- Queues and mixes sound effects
- Outputs to virtual audio device
- Manages playback state
- Uses SoundCache for fast cached playback
- Optional local speaker monitoring (sounds only, no mic)

**Key Methods:**
- `start()` - Begin audio stream
- `stop()` - End audio stream
- `play_sound(file_path, volume)` - Queue a sound (uses cache if available)
- `stop_all_sounds()` - Clear playback queue
- `set_monitor_enabled(enabled)` - Enable/disable local speaker monitoring
- `_output_callback()` - Real-time mixing (called by sounddevice)

#### `SoundCache`
Manages local sound storage and in-memory caching.
- Copies sounds to `sounds/` folder for persistence
- Pre-loads audio data at target sample rate (48kHz)
- Provides O(1) lookup for cached audio

**Key Methods:**
- `add_sound(source_path)` - Copy sound to local storage, cache it, return local path
- `get_sound_data(file_path)` - Get pre-loaded audio data (fast)
- `preload_sounds(paths)` - Pre-load multiple sounds at startup
- `remove_sound(file_path)` - Remove from cache and optionally delete file

#### `SoundboardApp`
Main GUI application.
- Tkinter-based interface
- Device selection
- Sound slot grid management
- Configuration persistence
- Integrates with SoundCache for local storage

#### `SoundEditor`
Sound editing dialog with waveform visualization.
- Visual waveform display showing audio amplitude
- Draggable start/end markers for trimming
- Zoom in/out for precise editing
- Preview playback of selected portion
- Warning for sounds > 5 seconds

**Key Methods:**
- `show()` - Display the editor dialog and return trimmed audio or None
- `_draw_waveform()` - Render waveform visualization
- `_start_playback()` - Preview the selected portion
- `_on_save()` - Save trimmed audio and close

---

## Configuration Format

`soundboard_config.json`:
```json
{
  "slots": {
    "0": {
      "name": "Air Horn",
      "file_path": "sounds/airhorn_8f3a2b1c.mp3",
      "hotkey": "ctrl+1",
      "volume": 1.0
    },
    "1": {
      "name": "Sad Trombone",
      "file_path": "sounds/sadtrombone_4e5f6a7b.wav",
      "hotkey": "ctrl+2",
      "volume": 0.8
    }
  }
}
```

---

## Development Guidelines

### Adding New Features

1. Update this `copilot-instructions.md` first
2. Move item from "Planned Features" to "Current Features" when complete
3. Document any new classes/methods in "Code Architecture"
4. Test with VB-Audio Virtual Cable + Discord

### Code Style

- Use type hints where possible
- Keep audio callback (`_audio_callback`) minimal - no blocking operations
- Handle exceptions gracefully in audio code
- Use threading locks for shared state between GUI and audio threads

### Audio Processing Rules

- Sample rate: 48000 Hz (Discord standard)
- Block size: 1024 samples
- Format: Float32
- Channels: Mono input → Stereo output
- Always clip output to [-1.0, 1.0] to prevent distortion

---

## Setup Instructions

### For Users

1. Install VB-Audio Virtual Cable: https://vb-audio.com/Cable/
2. Run: `pip install sounddevice soundfile numpy keyboard`
3. Run: `python soundboard.py`
4. Select your mic as Input, "CABLE Input" as Output
5. In Discord: Set input device to "CABLE Output"

### For Developers

```bash
# Clone/create project
cd LocalSoundBoardProject

# Install dependencies
pip install -r requirements.txt

# Run with admin (for global hotkeys)
python soundboard.py
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No sound in Discord | Ensure Discord input = "CABLE Output" |
| Hotkeys not working | Run as Administrator |
| Audio crackling | Close other audio apps, check CPU usage |
| "No module named X" | Run `pip install <module>` |
| Virtual cable not showing | Reinstall VB-Audio, restart PC |

---

## Change Log

### Version 1.0.0 (Initial)
- Basic soundboard with 12 slots
- Mic passthrough and mixing
- Global hotkey support
- JSON config persistence
- Discord-style dark theme

---

## Notes for AI Assistants

When modifying this project:

1. **Always update this file** when adding/changing features
2. **Preserve backward compatibility** with existing `soundboard_config.json`
3. **Keep the audio callback lightweight** - offload work to other threads
4. **Test audio changes carefully** - bugs can cause loud noises or crashes
5. **Maintain the Discord aesthetic** - colors: `#2C2F33`, `#7289DA`, `#43B581`, `#F04747`

When asked to add a feature:
1. Add it to "Planned Features" if discussing
2. Move to "Current Features" once implemented
3. Update "Code Architecture" if adding new classes
4. Update "Configuration Format" if adding new settings

---

## Known Gotchas & Lessons Learned

> **IMPORTANT:** Add to this section whenever you encounter a bug or learn something the hard way. This prevents repeating mistakes.

### Config & File Handling

| Issue | Cause | Fix |
|-------|-------|-----|
| Config deleted on startup | Missing color constant (e.g., `bg_light`) used in UI before being added to `COLORS` dict | Always add new color constants to `constants.py` BEFORE using them in GUI code |
| Config file corruption (duplicate JSON) | Non-atomic writes - crash during save corrupts file | Use atomic writes: write to temp file, then `os.replace()` to target |
| Sound paths break when editing slot | Path comparison failed for relative paths like `sounds/file.mp3` vs absolute | Check both relative AND absolute paths when detecting if sound is already in local storage |

### Audio

| Issue | Cause | Fix |
|-------|-------|-----|
| OGG files fail with "malformed" error | `soundfile` claims OGG support but fails on some files | Use shared `read_audio_file()` function with pydub fallback. Don't include `.ogg` in soundfile_formats list. |
| pydub can't load OGG/M4A/AAC | pydub requires ffmpeg binary which isn't bundled | Install `imageio-ffmpeg` (bundles ffmpeg), set `AudioSegment.converter` to `imageio_ffmpeg.get_ffmpeg_exe()` |
| PTT releases too early | PTT released immediately when audio callback returns | Add debounce delay (5 callback cycles ~100ms) before releasing PTT |
| Sounds don't play with rapid clicks | Lock contention and duplicate cache lookups | Queue sound before taking locks, use single cache lookup |
| Direct sf.read() fails for OGG | Multiple code paths used sf.read directly without fallback | Consolidated audio loading to shared `read_audio_file()` function in audio.py |

### GUI / Tkinter

| Issue | Cause | Fix |
|-------|-------|-----|
| Progress bar shows on wrong tab | Playing state not tracking which tab the sound belongs to | Store `tab_idx` in `playing_slots` dict, only update UI if current tab matches |
| Preview button not visible | Button hidden by expanding main button | Pack bottom frame FIRST at bottom, then main button expands into remaining space |
| UI elements resize unexpectedly | Grid weights and pack expand options | Use `grid_propagate(False)` and `pack_propagate(False)` to lock sizes; set `resizable(False, False)` on window |
| Sound plays twice on click | Button had both `command=` and drag bindings that call `_play_slot` | Remove `command=` from button, let drag `_on_drag_end` handle click-to-play |
| Colors reset when dragging | `_reset_drag_highlights()` set all slots to `bg_medium` instead of proper color | Call `_update_slot_button()` to restore full button appearance |
| Playing color shows on wrong tab after switch | `_update_slot_button` didn't check if playing state was for current tab | Check `playing_slots[idx].get("tab_idx") == current_tab_idx` before applying playing color |
| Scrollbar shows when not needed | Scrollbar always visible even with few slots | Add `_update_scrollbar_visibility()` to show/hide based on content height vs canvas height |
| Scrollbar visibility false positive | `winfo_height()` returns 1 before widget is mapped | Check `canvas_height <= 1` and return early; use `winfo_ismapped()` before pack/pack_forget |
| Editor crashes on OGG import | `_update_info_labels()` called before `selection_label` created | Add `hasattr()` check before updating `selection_label` in `_update_info_labels()` |
| Mouse wheel scrolls when no scrollbar | `_on_mousewheel` always scrolls regardless of scrollbar state | Check `slots_scrollbar.winfo_ismapped()` before allowing scroll |
| No new slots after filling all | `save` only called `_update_slot_button` not `_refresh_slot_buttons` | Call `_refresh_slot_buttons()` after save/clear to create new empty slots |
| Sound replays when switching tabs | Drag state not cleared on tab switch, or double-event race condition | Clear `drag_source_idx`/`is_dragging` FIRST in `_on_drag_end`, and also clear in `_switch_tab` |
| Empty slot '+' button doesn't open dialog | For empty slots, `drag_source_idx=None` causes `_on_drag_end` to return early | Track empty slot clicks separately with `empty_slot_clicked` variable, check it in `_on_drag_end` |
| Sound plays when clicking tab button | Slot's `ButtonRelease-1` fires even when releasing on a different widget (tab) | Check `winfo_containing()` to verify release is on same slot button; use `click_in_progress` flag and return `"break"` to stop event propagation |
| Tab buttons unresponsive while sound plays | Slot drag handlers don't properly isolate their events from other widgets | Add `click_in_progress` flag set on `_on_drag_start`, check it in `_on_drag_end`; clear flag on tab switch; return `"break"` from all drag handlers |

### General Rules

1. **Always test after adding new UI elements** - layout issues are common with Tkinter
2. **Never use a new color constant without adding it to COLORS first**
3. **Path handling must support both forward and backslashes on Windows**
4. **Audio file format support varies** - always have pydub fallback ready
5. **Atomic writes for all config/state files** - prevents corruption on crash
6. **Tkinter event handling** - return `"break"` to stop event propagation; use `winfo_containing()` to check actual widget under cursor on release; track click state with flags to prevent stray events