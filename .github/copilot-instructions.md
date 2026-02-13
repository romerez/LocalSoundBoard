# Copilot Instructions - Discord Soundboard Project

> **Last Updated:** 2024
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
```

---

## File Structure

```
LocalSoundBoardProject/
├── soundboard.py           # Main application entry point
├── soundboard_config.json  # Auto-generated user config (sounds, hotkeys, volumes)
├── copilot-instructions.md # This file - project specification
└── requirements.txt        # Python dependencies
```

---

## Current Features

- [x] Real-time microphone passthrough
- [x] Mix multiple sounds simultaneously
- [x] 12 configurable sound slots (4x3 grid)
- [x] Per-slot volume control
- [x] Global hotkey support (e.g., `ctrl+1`, `F1`)
- [x] Mic volume slider (0-150%)
- [x] Mic mute toggle
- [x] Stop all sounds button
- [x] Auto-save/load configuration
- [x] Discord-style dark theme UI
- [x] Device selection dropdowns (input/output)
- [x] Support for MP3, WAV, OGG, FLAC formats

---

## Planned Features / Backlog

- [ ] Drag-and-drop sound file import
- [ ] Sound preview before adding to slot
- [ ] Adjustable grid size (more/fewer slots)
- [ ] Sound categories/tabs
- [ ] Search/filter sounds
- [ ] Waveform visualization
- [ ] Looping sounds option
- [ ] Fade in/out effects
- [ ] Sound trimming/editing
- [ ] Import/export config profiles
- [ ] System tray minimization
- [ ] Auto-start with Windows
- [ ] Push-to-talk integration
- [ ] Discord Rich Presence

---

## Code Architecture

### Main Classes

#### `SoundSlot` (dataclass)
Represents a single sound button configuration.
```python
@dataclass
class SoundSlot:
    name: str           # Display name
    file_path: str      # Path to audio file
    hotkey: str | None  # Global hotkey (e.g., "ctrl+1")
    volume: float       # 0.0 to 1.5
```

#### `AudioMixer`
Handles real-time audio processing.
- Captures microphone input
- Queues and mixes sound effects
- Outputs to virtual audio device
- Manages playback state

**Key Methods:**
- `start()` - Begin audio stream
- `stop()` - End audio stream
- `play_sound(file_path, volume)` - Queue a sound
- `stop_all_sounds()` - Clear playback queue
- `_audio_callback()` - Real-time mixing (called by sounddevice)

#### `SoundboardApp`
Main GUI application.
- Tkinter-based interface
- Device selection
- Sound slot grid management
- Configuration persistence

---

## Configuration Format

`soundboard_config.json`:
```json
{
  "slots": {
    "0": {
      "name": "Air Horn",
      "file_path": "C:/sounds/airhorn.mp3",
      "hotkey": "ctrl+1",
      "volume": 1.0
    },
    "1": {
      "name": "Sad Trombone",
      "file_path": "C:/sounds/sadtrombone.wav",
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