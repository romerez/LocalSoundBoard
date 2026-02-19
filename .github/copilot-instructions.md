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
| GUI | CustomTkinter (modern Tkinter extension) |
| Audio I/O | `sounddevice` |
| Audio Files | `soundfile` |
| Audio Processing | `numpy` |
| Global Hotkeys | `keyboard` |
| Mouse Input | `mouse`, `pynput` |
| Config Storage | JSON |
| Image Processing | `pillow` |
| Audio Format Conversion | `pydub` + `imageio-ffmpeg` |
| Pitch-Preserving Speed | `librosa` |
| Emoji Data | `emoji-data-python` |
| Color Utilities | `colour` |

### Dependencies with Explanations

| Library | Version | Purpose |
|---------|---------|---------|
| `customtkinter` | >=5.2.0 | Modern Tkinter extension with rounded corners, dark mode support, and modern styling. |
| `sounddevice` | >=0.4.6 | Real-time audio I/O using PortAudio. Provides low-latency audio streams for mic capture and output to virtual cable. |
| `soundfile` | >=0.12.1 | Read/write audio files (WAV, FLAC, AIFF). Primary audio file loader for reliable formats. |
| `numpy` | >=1.24.0 | Audio buffer manipulation, waveform processing, sample mixing, resampling operations. |
| `keyboard` | >=0.13.5 | Global keyboard hotkey registration and PTT key simulation. Works system-wide without window focus. |
| `mouse` | >=0.7.1 | Mouse button detection for PTT recording. Captures mouse4/mouse5 button for Discord PTT. |
| `pynput` | >=1.7.0 | Backup mouse listener for PTT key capture. Used alongside Windows API. |
| `pillow` | >=10.0.0 | Image loading and resizing for custom slot thumbnails. Supports PNG, JPG, GIF, BMP, ICO. |
| `pydub` | >=0.25.1 | Extended audio format support (MP3, OGG, M4A, AAC, WMA, Opus, WebM). Fallback when soundfile fails. |
| `imageio-ffmpeg` | >=0.6.0 | Bundled ffmpeg binary for pydub. No separate ffmpeg installation required. |
| `librosa` | >=0.10.0 | Time-stretching for pitch-preserving speed changes. Allows 0.5x-2x speed without chipmunk voice. |
| `emoji-data-python` | >=1.6.0 | Emoji database with categories. Provides 1800+ emojis organized by category for emoji picker. |
| `colour` | >=0.1.5 | Color manipulation utilities. Lighten, darken, saturate, generate gradients, complementary colors. |

### Core Python Modules Used

| Module | Purpose |
|--------|---------|
| `customtkinter` | Modern GUI widgets - CTkFrame, CTkButton, CTkSlider, etc. |
| `tkinter` | Base GUI framework - used by CustomTkinter and for dialogs |
| `threading` | Background audio processing, non-blocking operations |
| `queue` | Thread-safe audio data transfer between callbacks |
| `ctypes` | Windows API calls for mouse button simulation (PTT) |
| `hashlib` | Generate unique filenames for cached sounds |
| `dataclasses` | Clean data models for SoundSlot and SoundTab |
| `json` | Config file persistence |
| `pathlib` | Cross-platform file path handling |
| `io` | In-memory audio buffer operations |
| `shutil` | File copying for local sound storage |

---

## File Structure (Detailed)

```
LocalSoundBoardProject/
├── main.py                     # Application entry point
├── requirements.txt            # Python dependencies
├── soundboard_config.json      # User configuration (auto-generated)
├── test_soundboard.py          # Unit tests
├── soundboard/                 # Main package
│   ├── __init__.py             # Package exports
│   ├── constants.py            # All configuration values
│   ├── models.py               # Data structures
│   ├── audio.py                # Audio engine
│   ├── editor.py               # Sound trimmer
│   └── gui.py                  # Main UI
├── sounds/                     # Sound file storage (auto-created)
├── images/                     # Custom images (auto-created)
└── .github/
    └── copilot-instructions.md # This documentation
```

### File Explanations

#### `main.py`
**Purpose:** Application entry point  
**Lines:** ~20  
**Description:** Simple launcher that imports `SoundboardApp` from the package and calls `run()`. Keeps entry point clean and testable.

```python
from soundboard import SoundboardApp
def main():
    app = SoundboardApp()
    app.run()
```

---

#### `soundboard/__init__.py`
**Purpose:** Package initialization and public API exports  
**Lines:** ~25  
**Description:** Defines what gets exported when importing the soundboard package. Sets package version.

**Exports:**
- `AudioMixer` - Audio mixing engine
- `SoundCache` - Sound file caching
- `SoundboardApp` - Main GUI application
- `SoundSlot` - Sound button configuration
- `SoundTab` - Tab container for slots
- `SoundEditor` - Audio trimming dialog
- `edit_sound_file()` - Convenience function for editing

---

#### `soundboard/constants.py`
**Purpose:** All magic numbers, colors, settings, and emoji library  
**Lines:** ~900  
**Description:** Single source of truth for all configuration values. Prevents magic numbers in code.

**Key Constants:**

| Constant | Description |
|----------|-------------|
| `COLORS` | Discord-style color palette (bg_dark, blurple, green, red, playing, preview, etc.) |
| `SLOT_COLORS` | 12-color standard palette for slot backgrounds |
| `NEON_COLORS` | 12-color vibrant neon palette (Neon Pink, Electric Blue, etc.) |
| `ALL_SLOT_COLORS` | Combined standard + neon colors (24 total) |
| `AUDIO` | Sample rate (48000), block size (1024), channels (2) |
| `UI` | Window title, size (740x650), grid columns (4), rows (3) |
| `EDITOR` | Max duration warning (5s), zoom limits (1x-50x) |
| `CONFIG_FILE` | "soundboard_config.json" |
| `SOUNDS_DIR` | "sounds" folder name |
| `IMAGES_DIR` | "images" folder name |
| `SUPPORTED_FORMATS` | Audio formats tuple (*.mp3, *.wav, *.ogg, etc.) |
| `SUPPORTED_IMAGE_FORMATS` | Image formats tuple (*.png, *.jpg, *.gif, etc.) |
| `EMOJI_CATEGORIES` | Dict of 9 categories with 1800+ emojis (from emoji-data-python) |
| `FONTS` | Font configuration with "Segoe UI Emoji" for colored emoji support |

**Color Utility Functions:**
| Function | Purpose |
|----------|--------|
| `hex_to_rgb()` | Convert hex color to RGB tuple |
| `rgb_to_hex()` | Convert RGB to hex |
| `lighten_color()` | Make color lighter |
| `darken_color()` | Make color darker |
| `saturate_color()` | Increase color saturation |
| `desaturate_color()` | Decrease color saturation |
| `get_complementary_color()` | Get opposite color on color wheel |
| `generate_color_gradient()` | Create gradient between two colors |
| `is_light_color()` | Check if color is light (for contrast) |
| `get_text_color_for_bg()` | Auto-select text color for readability |

---

#### `soundboard/models.py`
**Purpose:** Data structures for sounds and tabs  
**Lines:** ~75  
**Description:** Dataclasses that define the shape of configuration data. Handles JSON serialization.

**Classes:**
- `SoundSlot` - Individual sound button config
- `SoundTab` - Tab containing multiple slots

---

#### `soundboard/audio.py`
**Purpose:** Real-time audio mixing engine  
**Lines:** ~880  
**Description:** Core audio functionality. Handles mic capture, sound playback, PTT integration, and output to virtual cable. Uses separate input/output streams for device compatibility.

**Key Components:**
- `_simulate_mouse_button()` - Windows API for mouse button PTT
- `read_audio_file()` - Unified audio loader with pydub fallback
- `SoundCache` - In-memory sound caching for instant playback
- `AudioMixer` - Real-time audio mixing with PTT support

---

#### `soundboard/editor.py`
**Purpose:** Sound editing dialog with waveform visualization  
**Lines:** ~850  
**Description:** Modal dialog for trimming sounds. Shows visual waveform, allows selecting portions with draggable markers, includes zoom and preview playback.

**Key Features:**
- Waveform visualization using canvas
- Draggable start/end trim markers
- Zoom in/out (1x to 50x)
- Preview playback of selected portion
- Warning for sounds > 5 seconds

---

#### `soundboard/gui.py`
**Purpose:** Main GUI application  
**Lines:** ~2360  
**Description:** Tkinter-based user interface. Manages all visual components, user interactions, hotkey registration, and configuration persistence. Largest file in the project.

**Key Components:**
- Device selection dropdowns
- Tab bar with add/rename/delete
- Sound slot grid with drag-and-drop
- Audio options panel (collapsible)
- Status bar with stream toggle
- Configuration save/load

---

#### `sounds/` (folder)
**Purpose:** Local storage for sound files  
**Created:** Automatically on first sound add  
**Description:** When users add sounds, they're copied here with unique hashed filenames to prevent conflicts. Keeps original files safe.

**Naming Convention:** `{original_name}_{8char_hash}.{ext}`
Example: `airhorn_8f3a2b1c.mp3`

---

#### `images/` (folder)
**Purpose:** Local storage for custom slot images  
**Created:** Automatically when user adds custom image  
**Description:** Stores thumbnail images for sound slots. Images are copied here to ensure they persist.

---

#### `soundboard_config.json`
**Purpose:** User configuration persistence  
**Created:** Automatically on first save  
**Description:** Stores all user settings including sounds, hotkeys, volumes, tabs, device selections. JSON format for easy editing.

**Schema (simplified):**
```json
{
  "tabs": [
    {
      "name": "Main",
      "emoji": "🎵",
      "slots": {
        "0": {
          "name": "Air Horn",
          "file_path": "sounds/airhorn_8f3a2b1c.mp3",
          "hotkey": "ctrl+1",
          "volume": 1.0,
          "emoji": "📢",
          "image_path": null,
          "color": "#7289DA",
          "speed": 1.0,
          "preserve_pitch": true
        }
      }
    }
  ],
  "input_device": "Microphone (Realtek)",
  "output_device": "CABLE Input (VB-Audio)",
  "mic_volume": 1.0,
  "mic_muted": false,
  "ptt_key": "mouse4",
  "monitor_enabled": false,
  "auto_start": true
}
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
- [x] Modern UI with CustomTkinter (rounded corners, modern styling)

---

## Planned Features / Backlog

### High Priority
- [ ] Fix volume above 100% not making sounds louder (soft clipping needs work)
- [ ] Fix emoji picker (PyQt6 subprocess approach has issues - freezing, venv conflicts, etc.)
- [ ] Emoji display on slot buttons (Tkinter can't render colored emojis - need alternative approach)

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
- [ ] Convert sound editor to CustomTkinter

---

## Code Architecture (Complete)

### Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Application Startup                             │
│  main.py → SoundboardApp.__init__() → _load_config() → _preload_sounds()    │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            Runtime Data Flow                                 │
│                                                                              │
│  User clicks slot → _play_slot() → AudioMixer.play_sound()                  │
│                                            │                                 │
│                                            ▼                                 │
│  ┌────────────────┐    ┌─────────────────────────────────────┐              │
│  │   SoundCache   │───►│  _output_callback() [Real-time]     │              │
│  │ (cached audio) │    │  - Get mic from _mic_queue          │              │
│  └────────────────┘    │  - Mix with currently_playing       │              │
│                        │  - Apply soft clipping              │              │
│  ┌────────────────┐    │  - Output to virtual cable          │              │
│  │  Microphone    │───►│  - Queue to monitor (if enabled)    │              │
│  │ _input_callback│    └─────────────────────────────────────┘              │
│  └────────────────┘                    │                                     │
│                                        ▼                                     │
│                         ┌──────────────────────────────┐                     │
│                         │  VB-Audio Virtual Cable      │──► Discord Input    │
│                         └──────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Class: `SoundSlot` (dataclass)
**File:** `models.py`  
**Purpose:** Represents a single sound button configuration.

#### Fields
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | required | Display name shown on button |
| `file_path` | `str` | required | Path to audio file (relative to sounds/) |
| `hotkey` | `str \| None` | `None` | Global hotkey (e.g., "ctrl+1", "F1") |
| `volume` | `float` | `1.0` | Playback volume (0.0 to 1.5) |
| `emoji` | `str \| None` | `None` | Emoji character displayed on button |
| `image_path` | `str \| None` | `None` | Path to custom thumbnail image |
| `color` | `str \| None` | `None` | Custom background color (hex, e.g., "#7289DA") |
| `speed` | `float` | `1.0` | Playback speed multiplier (0.5x to 2.0x) |
| `preserve_pitch` | `bool` | `True` | If True, use time-stretch; if False, chipmunk/deep voice |

#### Methods
| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `Dict[str, Any]` | Serialize to dictionary for JSON storage |
| `from_dict(data)` | `SoundSlot` | Class method to create instance from dictionary |

---

### Class: `SoundTab` (dataclass)
**File:** `models.py`  
**Purpose:** Represents a tab containing multiple sound slots.

#### Fields
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | required | Tab display name |
| `emoji` | `str \| None` | `None` | Emoji shown in tab button |
| `slots` | `Dict[int, SoundSlot]` | `{}` | Map of slot index to SoundSlot |

#### Methods
| Method | Returns | Description |
|--------|---------|-------------|
| `to_dict()` | `Dict[str, Any]` | Serialize tab and all slots to dictionary |
| `from_dict(data)` | `SoundTab` | Class method to create instance from dictionary |

---

### Class: `SoundCache`
**File:** `audio.py`  
**Purpose:** Manages local sound storage and in-memory caching for instant playback.

**Why it exists:** Reading audio files from disk on every click causes noticeable delay. SoundCache pre-loads all sounds into memory at startup, providing O(1) access time.

#### Constructor
```python
SoundCache(sample_rate: Optional[int] = None)
```
- `sample_rate`: Target sample rate (default: 48000 Hz)

#### Instance Variables
| Variable | Type | Description |
|----------|------|-------------|
| `sample_rate` | `int` | Target sample rate for all cached audio |
| `sounds_dir` | `Path` | Path to local sounds folder |
| `_cache` | `Dict[str, np.ndarray]` | In-memory cache: filepath → audio data |
| `_lock` | `threading.Lock` | Thread-safe access to cache |

#### Methods
| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `add_sound` | `(source_path: str)` | `str` | Copy sound to local storage, cache it, return new path |
| `add_sound_data` | `(audio_data, sample_rate, original_name)` | `str` | Save numpy array as WAV file and cache it |
| `get_sound_data` | `(file_path: str)` | `np.ndarray \| None` | Get cached audio data (loads if not cached) |
| `preload_sounds` | `(file_paths: List[str])` | `None` | Pre-load multiple sounds at startup |
| `remove_sound` | `(file_path: str, delete_file: bool)` | `None` | Remove from cache and optionally delete file |
| `clear_cache` | `()` | `None` | Clear in-memory cache (files remain on disk) |
| `is_cached` | `(file_path: str)` | `bool` | Check if sound is in cache |
| `get_sound_duration` | `(file_path: str)` | `float` | Get duration in seconds |
| `_hash_file` | `(file_path: str)` | `str` | Generate MD5 hash for unique naming |
| `_load_into_cache` | `(file_path: str)` | `np.ndarray` | Load, resample, and cache audio file |
| `_read_audio_file` | `(file_path: str)` | `Tuple[np.ndarray, int]` | Read audio with pydub fallback |

---

### Class: `AudioMixer`
**File:** `audio.py`  
**Purpose:** Real-time audio mixing engine. Captures mic, mixes with sounds, outputs to virtual cable.

**Why separate streams:** Using a single duplex stream often fails when input/output devices have different capabilities. Separate streams provide better device compatibility.

#### Constructor
```python
AudioMixer(
    input_device: int,
    output_device: int,
    sample_rate: Optional[int] = None,
    block_size: Optional[int] = None,
    sound_cache: Optional[SoundCache] = None
)
```

#### Instance Variables
| Variable | Type | Description |
|----------|------|-------------|
| `input_device` | `int` | Device index for microphone |
| `output_device` | `int` | Device index for virtual cable |
| `sample_rate` | `int` | Audio sample rate (48000 Hz) |
| `block_size` | `int` | Samples per callback (1024) |
| `channels` | `int` | Output channels (2 = stereo) |
| `sound_cache` | `SoundCache` | Reference to sound cache |
| `running` | `bool` | Stream active flag |
| `input_stream` | `sd.InputStream` | Mic capture stream |
| `output_stream` | `sd.OutputStream` | Virtual cable output stream |
| `sound_queue` | `queue.Queue` | Newly triggered sounds |
| `currently_playing` | `List[Dict]` | Sounds being mixed |
| `lock` | `threading.Lock` | Thread-safe access |
| `_mic_queue` | `queue.Queue` | Mic samples between callbacks |
| `_last_mic_data` | `np.ndarray` | Fallback mic buffer |
| `mic_volume` | `float` | Microphone volume (0.0-1.5) |
| `mic_muted` | `bool` | Mic mute state |
| `ptt_key` | `str \| None` | Push-to-Talk key name |
| `ptt_active` | `bool` | PTT currently pressed |
| `_ptt_lock` | `threading.Lock` | Thread-safe PTT state |
| `_ptt_release_delay` | `int` | Callback cycles before PTT release |
| `_ptt_release_countdown` | `int` | Current release countdown |
| `monitor_enabled` | `bool` | Local speaker monitoring state |
| `monitor_stream` | `sd.OutputStream` | Local speaker output stream |
| `_monitor_queue` | `queue.Queue` | Audio data for local speakers |

#### Public Methods
| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `start` | `()` | `None` | Begin audio streams (input + output) |
| `stop` | `()` | `None` | Stop all audio streams |
| `play_sound` | `(file_path, volume, speed, preserve_pitch)` | `float` | Queue sound for playback, returns duration |
| `stop_all_sounds` | `()` | `None` | Clear queue and stop all playing sounds |
| `set_ptt_key` | `(key: Optional[str])` | `None` | Set Push-to-Talk key |
| `set_monitor_enabled` | `(enabled: bool)` | `None` | Enable/disable local speaker monitoring |

#### Private/Callback Methods
| Method | Signature | Description |
|--------|-----------|-------------|
| `_input_callback` | `(indata, frames, time, status)` | sounddevice callback for mic capture |
| `_output_callback` | `(outdata, frames, time, status)` | sounddevice callback for real-time mixing |
| `_monitor_callback` | `(outdata, frames, time, status)` | sounddevice callback for local speakers |
| `_apply_speed` | `(data, speed, preserve_pitch)` | Apply speed change with optional pitch preservation |
| `_soft_clip` | `(x: np.ndarray)` | Soft limiting to allow volume > 100% |
| `_press_ptt` | `()` | Press PTT key (keyboard or mouse) |
| `_release_ptt` | `()` | Release PTT key |
| `_check_ptt_release` | `()` | Check if all sounds finished, release PTT |

---

### Class: `SoundEditor`
**File:** `editor.py`  
**Purpose:** Modal dialog for sound trimming with waveform visualization.

**Key Features:**
- Visual waveform showing amplitude over time
- Draggable green/red markers for start/end trim points
- Zoom from 1x to 50x for precise editing
- Preview playback of selected portion
- Warning banner for sounds > 5 seconds

#### Constructor
```python
SoundEditor(
    parent: tk.Tk,
    file_path: str,
    on_save: Optional[Callable[[np.ndarray, int], None]] = None,
    output_device: Optional[int] = None
)
```

#### Instance Variables
| Variable | Type | Description |
|----------|------|-------------|
| `parent` | `tk.Tk` | Parent window |
| `file_path` | `str` | Path to audio file being edited |
| `on_save` | `Callable` | Callback when save is clicked |
| `output_device` | `int \| None` | Device for preview playback |
| `audio_data` | `np.ndarray` | Full audio samples |
| `waveform_data` | `np.ndarray` | Mono data for visualization |
| `sample_rate` | `int` | Audio sample rate |
| `duration` | `float` | Total duration in seconds |
| `trim_start` | `int` | Start trim point (samples) |
| `trim_end` | `int` | End trim point (samples) |
| `zoom_level` | `float` | Current zoom (1.0 to 50.0) |
| `view_start` | `float` | View position (0.0 to 1.0) |
| `is_playing` | `bool` | Preview playback active |
| `is_paused` | `bool` | Preview paused |
| `play_stream` | `sd.OutputStream` | Preview playback stream |
| `play_position` | `int` | Current playback sample position |
| `canvas_width` | `int` | Waveform canvas width |
| `canvas_height` | `int` | Waveform canvas height |
| `dragging` | `str \| None` | "start", "end", or None |
| `result` | `Tuple \| None` | Trimmed audio result |

#### UI Creation Methods
| Method | Description |
|--------|-------------|
| `_create_dialog` | Create main dialog window |
| `_create_info_bar` | Create duration/selection info labels |
| `_create_waveform_canvas` | Create waveform visualization canvas |
| `_create_timeline` | Create time ruler below waveform |
| `_create_zoom_controls` | Create zoom buttons and scroll slider |
| `_create_playback_controls` | Create play/pause/stop buttons |
| `_create_action_buttons` | Create save/cancel buttons |

#### Drawing Methods
| Method | Description |
|--------|-------------|
| `_draw_waveform` | Render waveform visualization |
| `_draw_trim_markers` | Draw green (start) and red (end) markers |
| `_draw_playback_position` | Draw yellow playhead during preview |
| `_draw_timeline` | Draw time labels below waveform |
| `_update_info_labels` | Update duration and selection text |

#### Interaction Methods
| Method | Description |
|--------|-------------|
| `_on_canvas_click` | Handle click on waveform (start dragging marker) |
| `_on_canvas_drag` | Handle drag to move marker |
| `_on_canvas_release` | Handle mouse release (stop dragging) |
| `_update_marker_position` | Update trim point based on mouse X |
| `_on_canvas_resize` | Handle window resize |
| `_on_mouse_wheel` | Handle scroll wheel for zoom |

#### Zoom Methods
| Method | Description |
|--------|-------------|
| `_zoom_in` | Increase zoom level |
| `_zoom_out` | Decrease zoom level |
| `_zoom_fit` | Reset to full view (1x zoom) |
| `_update_scroll_range` | Update scroll slider range |
| `_on_scroll` | Handle scroll slider change |

#### Playback Methods
| Method | Description |
|--------|-------------|
| `_toggle_playback` | Play or resume preview |
| `_pause_playback` | Pause preview |
| `_prepare_audio_for_playback` | Prepare selected portion for preview |
| `_start_playback` | Start audio stream for preview |
| `_update_playhead` | Update playhead position during playback |
| `_stop_playback` | Stop preview and close stream |
| `_reset_selection` | Reset trim markers to full audio |

#### Action Methods
| Method | Description |
|--------|-------------|
| `_on_save` | Trim audio and close dialog |
| `_on_cancel` | Close without saving |
| `show` | Display dialog and wait for result |
| `_load_audio` | Load and prepare audio file |
| `_read_audio_file` | Read audio with pydub fallback |

---

### Class: `SoundboardApp`
**File:** `gui.py`  
**Purpose:** Main GUI application with all user interface components.

**Largest class in the project.** Manages the entire UI, user interactions, configuration, and coordinates between AudioMixer and SoundCache.

#### Constructor
```python
SoundboardApp()
```
Creates root Tkinter window, initializes all components, loads configuration, and sets up auto-start.

#### Instance Variables (Key)
| Variable | Type | Description |
|----------|------|-------------|
| `root` | `tk.Tk` | Main window |
| `mixer` | `AudioMixer \| None` | Audio engine |
| `sound_cache` | `SoundCache` | Sound caching |
| `tabs` | `List[SoundTab]` | All sound tabs |
| `current_tab_idx` | `int` | Active tab index |
| `playing_slots` | `Dict` | Currently playing sounds |
| `slot_widgets` | `List[Dict]` | UI widgets for each slot |
| `tab_buttons` | `List[tk.Button]` | Tab bar buttons |
| `drag_source_idx` | `int \| None` | Drag-and-drop source |
| `is_dragging` | `bool` | Drag in progress |

#### UI Creation Methods
| Method | Description |
|--------|-------------|
| `_create_ui` | Create all UI sections |
| `_create_device_section` | Device dropdowns and audio options |
| `_create_tab_bar` | Tab buttons and add tab button |
| `_create_soundboard_section` | Scrollable slot grid |
| `_create_slot_widgets` | Individual slot buttons |
| `_create_status_bar` | Bottom status bar |
| `_setup_styles` | Configure ttk styles |

#### Tab Management Methods
| Method | Description |
|--------|-------------|
| `_refresh_tab_bar` | Rebuild tab button widgets |
| `_switch_tab` | Switch to different tab |
| `_add_new_tab` | Create new tab dialog |
| `_configure_tab` | Edit/delete tab dialog |
| `_show_emoji_picker` | Emoji selection dialog |

#### Sound Slot Methods
| Method | Description |
|--------|-------------|
| `_refresh_slot_buttons` | Rebuild all slot widgets |
| `_update_slot_button` | Update single slot appearance |
| `_configure_slot` | Edit slot dialog (sound, hotkey, volume, etc.) |
| `_play_slot` | Play sound to Discord (current tab) |
| `_play_slot_from_tab` | Play sound from specific tab (for hotkeys) |
| `_preview_slot` | Preview sound locally |
| `_stop_slot` | Stop specific sound |
| `_stop_slot_with_flag` | Stop with flag to prevent click-through |
| `_open_sound_editor` | Open trimming dialog |
| `_show_quick_popup` | Right-click volume/speed popup |

#### Drag and Drop Methods
| Method | Description |
|--------|-------------|
| `_on_drag_start` | Begin drag operation |
| `_on_drag_motion` | Update drag visuals |
| `_on_drag_end` | Complete drag (play, swap, or move to tab) |
| `_reset_drag_highlights` | Clear drag target highlighting |
| `_swap_slots` | Swap two slots within same tab |
| `_move_slot_to_tab` | Move slot to different tab |

#### Audio Control Methods
| Method | Description |
|--------|-------------|
| `_toggle_stream` | Start/stop audio streaming |
| `_toggle_audio_options` | Expand/collapse audio options panel |
| `_update_mic_volume` | Handle mic volume slider change |
| `_toggle_mic_mute` | Toggle mic mute |
| `_toggle_monitor` | Toggle local speaker monitoring |
| `_stop_all_sounds` | Stop all playing sounds |

#### PTT Methods
| Method | Description |
|--------|-------------|
| `_toggle_ptt_visibility` | Show/hide PTT configuration |
| `_record_ptt_key` | Listen for keyboard/mouse PTT key |
| `_clear_ptt_key` | Clear configured PTT key |

#### Animation Methods
| Method | Description |
|--------|-------------|
| `_animate_progress` | Update progress bars for playing sounds |

#### Canvas/Scroll Methods
| Method | Description |
|--------|-------------|
| `_on_grid_configure` | Handle grid resize |
| `_on_canvas_configure` | Handle canvas resize |
| `_update_scrollbar_visibility` | Show/hide scrollbar as needed |
| `_on_mousewheel` | Handle scroll wheel |

#### Configuration Methods
| Method | Description |
|--------|-------------|
| `_save_config` | Save settings to JSON (atomic write) |
| `_load_config` | Load settings from JSON |
| `_auto_start_stream` | Optionally start stream on launch |
| `_preload_sounds` | Pre-load all sounds into cache |
| `_register_hotkeys` | Register global hotkeys |

#### Utility Methods
| Method | Description |
|--------|-------------|
| `_get_current_tab` | Get current SoundTab object |
| `_copy_image_to_storage` | Copy custom image to images/ folder |
| `_load_slot_image` | Load and resize slot thumbnail |
| `_clear_stop_flag` | Clear stop button click flag |
| `_on_close` | Handle window close (clean up) |
| `run` | Start the main event loop |

---

### Standalone Functions

#### `_simulate_mouse_button(button, press)`
**File:** `audio.py`  
**Purpose:** Simulate mouse button press/release using Windows API (ctypes).

```python
_simulate_mouse_button(button: str, press: bool = True)
```

**Parameters:**
- `button`: "left", "right", "middle", "x" (mouse4), "x2" (mouse5)
- `press`: True = press down, False = release

**Why Windows API:** The `mouse` library doesn't reliably handle mouse4/mouse5 buttons. Direct Windows API calls via ctypes work consistently for Discord PTT.

---

#### `read_audio_file(file_path)`
**File:** `audio.py`  
**Purpose:** Universal audio file reader with pydub fallback.

```python
read_audio_file(file_path: str) -> Tuple[np.ndarray, int]
```

**Returns:** (audio_data, sample_rate)

**Loading Strategy:**
1. Try `soundfile` for WAV, FLAC, AIFF (reliable formats)
2. Try `soundfile` for other formats (may work for some MP3s)
3. Fallback to `pydub` for OGG, M4A, AAC, WMA, Opus, WebM, etc.

**Why this exists:** soundfile claims to support OGG but fails on many files. pydub with ffmpeg handles these cases.

---

#### `edit_sound_file(parent, file_path, on_save, output_device)`
**File:** `editor.py`  
**Purpose:** Convenience function to open SoundEditor dialog.

```python
edit_sound_file(
    parent: tk.Tk,
    file_path: str,
    on_save: Optional[Callable[[np.ndarray, int], None]] = None,
    output_device: Optional[int] = None
) -> Optional[Tuple[np.ndarray, int]]
```

**Returns:** Trimmed (audio_data, sample_rate) or None if cancelled

---

## Configuration Format (Complete)

### `soundboard_config.json` Schema

```json
{
  "tabs": [
    {
      "name": "Main",
      "emoji": "🎵",
      "slots": {
        "0": {
          "name": "Air Horn",
          "file_path": "sounds/airhorn_8f3a2b1c.mp3",
          "hotkey": "ctrl+1",
          "volume": 1.0,
          "emoji": "📢",
          "image_path": null,
          "color": "#7289DA",
          "speed": 1.0,
          "preserve_pitch": true
        }
      }
    }
  ],
  "input_device": "Microphone (Realtek High Definition Audio)",
  "output_device": "CABLE Input (VB-Audio Virtual Cable)",
  "mic_volume": 1.0,
  "mic_muted": false,
  "ptt_key": "mouse4",
  "monitor_enabled": false,
  "auto_start": true
}
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tabs` | `Array<SoundTab>` | `[{name:"Main", slots:{}}]` | List of all tabs |
| `input_device` | `string \| null` | `null` | Selected microphone name |
| `output_device` | `string \| null` | `null` | Selected output device name (virtual cable) |
| `mic_volume` | `float` | `1.0` | Microphone volume multiplier (0.0 to 1.5) |
| `mic_muted` | `bool` | `false` | Microphone mute state |
| `ptt_key` | `string \| null` | `null` | Push-to-Talk key (e.g., "ctrl", "mouse4", "F1") |
| `monitor_enabled` | `bool` | `false` | Local speaker monitoring enabled |
| `auto_start` | `bool` | `true` | Auto-start audio stream on launch |

### SoundTab Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `string` | required | Tab display name |
| `emoji` | `string \| null` | `null` | Tab emoji |
| `slots` | `Dict[string, SoundSlot]` | `{}` | Map of slot index (as string) to slot data |

### SoundSlot Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `string` | required | Display name |
| `file_path` | `string` | required | Path to audio file |
| `hotkey` | `string \| null` | `null` | Global hotkey binding |
| `volume` | `float` | `1.0` | Volume multiplier |
| `emoji` | `string \| null` | `null` | Slot emoji |
| `image_path` | `string \| null` | `null` | Custom thumbnail path |
| `color` | `string \| null` | `null` | Background color (hex) |
| `speed` | `float` | `1.0` | Playback speed (0.5 to 2.0) |
| `preserve_pitch` | `bool` | `true` | Pitch preservation for speed changes |

### Migration Notes

The config format has evolved. Old configs are auto-migrated:
- **v1.0 → v1.1:** Added `tabs` array (old `slots` dict moved into default tab)
- **v1.1 → v1.2:** Added `speed` and `color` to SoundSlot
- **v1.2 → v1.3:** Added `preserve_pitch` to SoundSlot
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

## Setup Instructions (Complete)

### For Users

#### Prerequisites
1. **Python 3.8+** - Download from https://python.org
2. **VB-Audio Virtual Cable** - Download from https://vb-audio.com/Cable/
   - Install and restart PC
   - This creates "CABLE Input" and "CABLE Output" devices

#### Installation
```bash
# Navigate to project folder
cd LocalSoundBoardProject

# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py
```

#### First-Time Setup
1. **Input Device:** Select your physical microphone
2. **Output Device:** Select "CABLE Input (VB-Audio Virtual Cable)"
3. **Discord Settings:** 
   - Go to Settings → Voice & Video
   - Set Input Device to "CABLE Output (VB-Audio Virtual Cable)"
4. **Click "Start Stream"** to begin

#### Push-to-Talk Setup (Optional)
1. Expand "Audio Options" panel
2. Click "⏺ Record PTT Key"
3. Press your Discord PTT key (e.g., mouse4, ctrl)
4. Now when sounds play, they'll automatically trigger Discord PTT

### For Developers

#### Development Setup
```bash
# Clone/create project
cd LocalSoundBoardProject

# Create virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Run with admin (required for global hotkeys on Windows)
python main.py
```

#### Running Tests
```bash
python test_soundboard.py
```

#### Debug Mode
The application writes debug info to `debug.log` for troubleshooting audio issues.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No sound in Discord | Ensure Discord input = "CABLE Output" |
| Hotkeys not working | Run as Administrator |
| Audio crackling | Close other audio apps, check CPU usage |
| "No module named X" | Run `pip install <module>` |
| Virtual cable not showing | Reinstall VB-Audio, restart PC |
| "PyQt6 Required" message even after installing | You're running with system Python instead of venv. Use `run.bat` or `.venv\Scripts\python.exe main.py` |

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
| Audio sounds distorted/poor quality in Discord | Naive linear interpolation resampling causes aliasing and distortion when audio files have different sample rates than 48kHz | Use `librosa.resample()` for high-quality resampling with anti-aliasing; added `_resample_audio()` helper function |
| Audio sounds "cut off" in Discord | PTT releasing too early + abrupt sound endings | Increased PTT debounce delay from 5 to 15 cycles (~300ms); added `_apply_fade_out()` to apply 30ms fade at end of sounds |
| OGG files fail with "malformed" error | `soundfile` claims OGG support but fails on some files | Use shared `read_audio_file()` function with pydub fallback. Don't include `.ogg` in soundfile_formats list. |
| pydub can't load OGG/M4A/AAC | pydub requires ffmpeg binary which isn't bundled | Install `imageio-ffmpeg` (bundles ffmpeg), set `AudioSegment.converter` to `imageio_ffmpeg.get_ffmpeg_exe()` |
| PTT releases too early | PTT released immediately when audio callback returns | Add debounce delay (15 callback cycles ~300ms) before releasing PTT |
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
| Stop button needs double-click | Stop button used `<Button-1>` binding, hides itself mid-click causing release event to land on slot button | Use standard `command=` attribute for tk.Button; add `stop_button_clicked` flag to block slot reactions for 100ms after stop; bind both `<ButtonPress-1>` and `<ButtonRelease-1>` to return `"break"` |
| Hotkey playback doesn't show stop button | `_play_slot_from_tab` didn't pack the stop button like `_play_slot` does | Add stop button packing logic to `_play_slot_from_tab` in the `update_ui` lambda |

### Environment & Python

| Issue | Cause | Fix |
|-------|-------|-----|
| "PyQt6 Required" dialog appears | Running with system Python instead of venv Python | Always use `run.bat` or `.venv\Scripts\python.exe main.py`. PyQt6 is installed in venv only. |
| Subprocess can't find PyQt6 | `sys.executable` returns wrong Python | The emoji picker runs as subprocess using `sys.executable` - if main app uses wrong Python, subprocess will too |
| Tkinter + PyQt6 event loop freeze | Running PyQt6 dialog directly in Tkinter process | Run PyQt6 dialogs as **subprocess** to avoid event loop conflicts. Use `subprocess.run()` to launch picker. |

### General Rules

1. **Always test after adding new UI elements** - layout issues are common with Tkinter
2. **Never use a new color constant without adding it to COLORS first**
3. **Path handling must support both forward and backslashes on Windows**
4. **Audio file format support varies** - always have pydub fallback ready
5. **Atomic writes for all config/state files** - prevents corruption on crash
6. **Tkinter event handling** - return `"break"` to stop event propagation; use `winfo_containing()` to check actual widget under cursor on release; track click state with flags to prevent stray events
7. **ALWAYS use venv Python** - run with `run.bat` or `.venv\Scripts\python.exe main.py`, never bare `python main.py`
8. **PyQt6 + Tkinter coexistence** - PyQt6 dialogs must run as subprocess to avoid event loop conflicts