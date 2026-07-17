# Copilot Instructions - Discord Soundboard Project

> **Last Updated:** 2026-07-17 (People window: persistent hub + hidden startup prebuild + empty-group stubs + measured pump/flush policy — open/reopen is now near-instant)
> **Status:** Active Development
> **Language:** Python 3.x
ALWAYS EDIT THIS FILE FIRST when adding features or making changes. This is the source of truth for the project and helps maintain consistency.
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
| Config Storage | JSON (atomic writes via `os.replace`) |
| Image Processing | `pillow` |
| Audio Format Conversion | `pydub` + `imageio-ffmpeg` |
| Pitch-Preserving Speed | `librosa` |
| Emoji Data | `emoji-data-python` |
| Color Utilities | `colour` |
| Noise Suppression | **DeepFilterNet3** via `onnxruntime` (default, Krisp-class DNN, 48 kHz) + `pyrnnoise` (RNNoise, light fallback); mic-only, pluggable backend, replaces Krisp |
| Voice Effects | pure-numpy DSP + `scipy.signal` (real-time mic voice changer) |

### Dependencies

```
sounddevice>=0.4.6
soundfile>=0.12.1
numpy>=1.24.0
keyboard>=0.13.5
mouse>=0.7.1
pynput>=1.7.0
pillow>=10.0.0
pydub>=0.25.1
imageio-ffmpeg>=0.6.0
librosa>=0.10.0
scipy>=1.10.0
customtkinter>=5.2.0
emoji-data-python>=1.6.0
colour>=0.1.5
noisereduce>=3.0.0
```
> Note: the live mic **Voice Changer** lives in `soundboard/voice_fx.py` (pure-numpy DSP + `scipy.signal` for its radio/megaphone bandpass). See the full `requirements.txt` for the authoritative, annotated dependency list (`pyrnnoise`, `pystray`, `yt-dlp`, `soundcard`, etc.).

---

## File Structure

```
LocalSoundBoardProject/
├── main.py                 # Application entry point (configures logging)
├── soundboard/             # Main package
│   ├── __init__.py         # Package exports (SoundboardApp, AudioMixer, SoundCache, SoundSlot)
│   ├── constants.py        # Colors, audio settings, UI config, lazy emoji loading
│   ├── models.py           # SoundSlot, SoundTab dataclasses
│   ├── audio.py            # AudioMixer, SoundCache, audio utilities (uses logging module)
│   ├── voice_fx.py         # VoiceChanger — real-time mic voice effects (pure-numpy DSP + scipy bandpass)
│   ├── editor.py           # SoundEditor with waveform visualization
│   └── gui.py              # SoundboardApp GUI class
├── sounds/                 # Local sound storage folder (auto-created)
├── images/                 # Local image storage folder (auto-created)
├── soundboard.py           # Legacy entry point (deprecated)
├── soundboard_config.json  # Auto-generated user config (sounds, hotkeys, volumes)
├── debug.log               # Runtime debug log (via Python logging module)
├── copilot-instructions.md # This file - project specification
└── requirements.txt        # Python dependencies
```

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `constants.py` | All magic numbers, colors, config values, lazy-loaded emoji categories |
| `models.py` | Data structures (SoundSlot, SoundTab) |
| `audio.py` | Audio I/O, mixing logic, sound caching, resampling utilities |
| `editor.py` | Sound editor with waveform visualization and trimming |
| `gui.py` | CustomTkinter UI components, configuration I/O |

---

## CRITICAL: Slot Button Click & Drag Architecture

> **DO NOT MODIFY this system without reading this section first.**
> This was solved after extensive debugging — every design choice has a reason.

### The Problem

CTkButton's internal `_draw()` method re-binds `<Button-1>` on its internal canvas
**without `add`**, which **wipes any raw `canvas.bind()` handlers** we add. This means:

- `canvas.bind("<ButtonPress-1>", handler)` — **WILL BE WIPED** by `_draw()`
- `canvas.bind("<ButtonPress-1>", handler, add="+")` — **WILL ALSO BE WIPED** by `_draw()`
- `_draw()` runs on **every** `btn.configure()` call (color changes, text updates, etc.)

### The Solution: Separated Click and Drag

| Mechanism | Purpose | Why it works |
|-----------|---------|--------------|
| `command=` callback | **Click-to-play** (on main button) | Stored as `CTkButton._command` property — immune to `_draw()` |
| Drag handle (⋮⋮) | **Drag detection** (separate widget) | Uses CTkLabel at top of slot — not affected by CTkButton issues |

### Event Flow

```
USER CLICKS A SLOT (normal mode):
  1. <Button-1> fires on internal canvas
  2. CTkButton._clicked() calls self._command → _on_slot_command(slot_idx)
  3. _handle_slot_click → debounce check → _play_slot(slot_idx)

USER MOVES A SLOT (edit mode):
  1. User clicks "↔ Move" button to enter edit mode
  2. Slots start pulsing animation to indicate edit mode
  3. User clicks a slot to select it (turns green)
  4. User clicks another slot to swap, or clicks a tab to move there
  5. Click "✓ Done" to exit edit mode
```

### State Machine

```
Click state (reset via _reset_click_state()):
  _click_active   : bool          — a press is being tracked
  _click_slot     : Optional[int] — slot index that was pressed
  _click_tab      : Optional[int] — tab that was active at press time
  _click_dragging : bool          — drag mode active

Edit mode state (reset via _exit_edit_mode()):
  _edit_mode      : bool          — edit/move mode is enabled
  _dragging_slot  : Optional[int] — slot selected for moving

Persistent (NOT reset per-click):
  _last_play_time      : float         — debounce rapid clicks (< 150ms)
  _just_stopped_slot   : Optional[int] — slot just stopped (prevent re-play < 200ms)
  _just_stopped_at     : float         — when it was stopped
```

### Edit Mode UX

- **Activation**: Click "↔ Move" button in header to enter edit mode
- **Visual feedback**: Filled slots get a colored border to indicate edit mode
- **Cursor**: Changes to "fleur" (move cursor) in edit mode
- **Select slot**: Click a slot to select it (turns green)
- **Swap slots**: Click another slot to swap positions
- **Move to tab**: Click a different tab to move the slot there
- **Deselect**: Click the same slot again to deselect
- **Exit**: Click "✓ Done" button to exit edit mode

### What NOT To Do

| Approach | Why it fails |
|----------|-------------|
| `canvas.bind(event, handler)` on CTkButton's internal canvas | **Wiped by `_draw()`** on every `btn.configure()` call |
| `canvas.bind(event, handler, add="+")` | Same — `_draw()` replaces ALL `<Button-1>` bindings |
| Using main button for both click and drag | Confusing UX — users accidentally drag when trying to play |
| Hold-to-drag with timer | Complex state management, unreliable event bindings |
| Real-time drag highlighting all slots | Too expensive, causes performance issues |

### Stop Button

- `command=` set **once** at creation time in `_create_slot_widgets` (via `make_stop_handler`)
- `_show_stop_button()` just packs/unpacks — **no re-binding**
- `_stop_slot_with_flag()` records `_just_stopped_slot` + timestamp; `_handle_slot_click` checks this to prevent re-play within 200ms

### Tab Integration

- `_switch_tab()` calls `_reset_click_state()` and `_reset_drag_handle_state()` to cancel any in-progress interaction
- If a drag was active, cursor and highlights are also reset

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
- [x] Comprehensive emoji library (1800+ emojis from emoji-data-python)
- [x] Colored emoji display (Segoe UI Emoji font)
- [x] Neon color palette (12 vibrant colors)
- [x] 24 customizable slot colors (standard + neon)
- [x] Color utility functions (lighten, darken, saturate, gradient)
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
- [x] Edit/Move mode for slot reordering (click "↔ Move" button, phone-like UI)
- [x] Move sounds between tabs (select slot in edit mode, click target tab)
- [x] Unlimited sounds per tab with scrolling support
- [x] Preview progress bar with distinct green color
- [x] Different colors for play modes (orange=Discord, green=preview)
- [x] Edit button (✏️) on sound slots for quick access to settings
- [x] Right-click popup for quick volume/speed adjustment
- [x] Playback speed adjustment per sound (0.5x to 2x)
- [x] Custom color selection for sound slots (12 color palette)
- [x] Per-slot stop button (■) appears while sound is playing
- [x] Global "Stop All Sounds" button in audio options
- [x] Pitch preservation option for speed changes (uses librosa time-stretch)
- [x] Modern UI with CustomTkinter (rounded corners, modern styling)
- [x] Performance optimization: cached fonts (pre-created CTkFont objects)
- [x] Performance optimization: cached slot images (avoids disk reads on updates)
- [x] Optimized animation loop (20fps instead of 60fps for progress bars)
- [x] Performance optimization: background sound preloading (non-blocking startup)
- [x] Performance optimization: in-place fade-out (eliminates redundant array copy per playback)
- [x] Performance optimization: lazy emoji category loading (deferred to first picker open)
- [x] Performance optimization: editor playhead-only redraws (no full waveform redraw during preview)
- [x] Structured debug logging via Python `logging` module (replaces raw file I/O)
- [x] Atomic config saves via `os.replace()` (prevents data loss on crash)
- [x] Scoped mousewheel scrolling (only active when hovering over soundboard area)
- [x] High-quality audio resampling in sound editor (librosa/scipy, was nearest-neighbor)
- [x] Hotkey playback progress tracked across all tabs (not just current tab)
- [x] Performance optimization: tab bar updates existing buttons instead of recreating (faster tab switching)
- [x] Performance optimization: slot filled-state cache skips redundant preview/edit button updates
- [x] Emoji picker with scrolling support and 200+ categorized emojis
- [x] Hebrew/Arabic RTL text display support for slot labels using natural text order (no pre-reversal)
- [x] Sound editor pause/stop without UI freeze (background thread stream cleanup)
- [x] Zoom centered on cursor position (mouse wheel zooms to cursor location)
- [x] Improved sound editor UI (larger window, modern styling, better layout)
- [x] Delete sound button with confirmation popup (🗑️ in quick popup + Clear button confirmation)
- [x] Unlimited tabs with horizontal scrolling (no more 4-tab limit)
- [x] Dynamic tab bar scroll buttons (◀ ▶ appear when needed)
- [x] Mousewheel scrolling for tab bar (scroll tabs left/right)
- [x] Auto-scroll to active tab when switching
- [x] Move button relocated to tab bar (left side, next to scroll buttons)
- [x] Right-click quick edit popup captures pointer/keyboard focus so edits cannot click through to sound slots
- [x] Faster soundboard mousewheel scrolling
- [x] Configurable hover preview binding to play the sound under the cursor locally
- [x] Multi-cut workflow captures a title for each cut and uses it for the created sound slot/file
- [x] Action-bar shortcut to open the local `sounds/` folder
- [x] Recordings use date/time/duration-aware filenames
- [x] Quick Record to Sound workflow: press once to record, press again to stop and open the new recording as a sound
- [x] Long recordings are handed to the sound editor via a background prepare step so the UI stays responsive
- [x] Tab manager dialog for reordering tabs
- [x] Shift + mousewheel over a sound adjusts that sound's volume without opening edit
- [x] Configurable app-wide mousewheel scroll speed multiplier
- [x] Quick edit popup suppresses the next slot click after closing so Apply/Cancel cannot click through
- [x] Right-click menu includes explicit Edit action
- [x] Soundboard scroll speed multiplier works (configurable 1x-30x with weighted scroll units for stronger movement)
- [x] Audio options panel scroll isolated from soundboard scroll (no cross-panel scrolling)
- [x] Shift+scroll adjusts per-slot volume without scrolling soundboard
- [x] Volume adjustment shows visual bar indicator (green for normal, yellow for boost >100%)
- [x] Volume adjustment displays live percentage in visual bar (🔊 X%)
- [x] Muted state detected in status bar (🔇 instead of 🔊)
- [x] **Real-time mic Voice Changer** — modulates your LIVE microphone before it hits the virtual cable, so the Discord call hears the effect. Pure-numpy DSP chain (`soundboard/voice_fx.py` → `VoiceChanger`) applied in `AudioMixer._output_callback` right where the mic is scaled, after RNNoise and before the soft-clip. Effects: pitch shift (deep↔chipmunk, crossfading delay-line shifter), drive/distortion, bitcrush (lo-fi/8-bit), ring-mod (robot/metallic), bandpass (radio/megaphone/telephone) via `scipy.signal` biquad, chorus/vibrato (modulated delay), echo (feedback delay), reverb (Schroeder comb bank), tremolo, output gain. Lock-free (GUI sets attrs, audio thread reads), crash-proof (`process()` never raises, always returns len(input)), cheap (vectorised, well under one 1024-block).
- [x] **17 one-tap voice presets** — Clean, Deep, Demon, Chipmunk, Helium, Robot, Cylon, Radio, Megaphone, Telephone, Alien, Underwater, Cave, Ghost, Drunk, 8-Bit, Stadium. Voice Changer card in Audio Options (after Mic Processing): master Enable, preset grid, collapsible ⚙ Advanced drawer (pitch slider −12..+12 st, per-effect on/off toggles, output level). Persisted in config under `voice_changer`; applied to the mixer on stream start and mirrored live while streaming.
- [x] **Dynamic grid density** — live ⊞ Columns −/+ control in the soundboard header lets the user pick how many sound slots appear per row (2–12). Replaces the fixed `UI["grid_columns"]` constant with `self.grid_columns`; changing it rebuilds every tab's grid (`_rebuild_all_tab_grids` via `_cleanup_tab_widgets` + `_build_all_tab_widgets`) and persists under `grid_columns`. Denser = more, smaller slots; roomier = fewer, larger.
- [x] **Persistent decoded-audio cache** — decoded + 48 kHz-resampled PCM is saved to `audio_cache/*.npy` and reloaded on later launches, so warming skips decode/ffmpeg/resample (`SoundCache._load_into_cache`). Keyed by path+mtime+sr; auto-invalidates on edit; safe fallback to decode.
- [x] **Data-safety: persons save-guard + rotating backups** — an empty in-memory persons list can't overwrite people on disk; every launch snapshots the good config to `config_backups/` (keeps 20).
- [x] **HiDPI window-size persistence fixed** — window restores at its saved size instead of growing ~1.5× each launch (restore via raw `wm_geometry`).
- [x] **Faster startup** — removed the per-tab synchronous layout-flush warm-up; first tab paints, others lazily on switch.
- [x] **People-hub smoothness** — pop-out chips build incrementally (`after()`-chunked) so a sound-heavy person never freezes the UI; per-person avatar render cached. Search is debounced; the per-person panel cache is LRU-bounded; chip-label composites + person sounds (decoded cache) are warmed/cached.
- [x] **Two-line sound chips** — sound-chip labels use a smaller font and wrap a long title onto up to two lines (RTL-correct) instead of clipping it with a single `…`.
- [x] **Coloured person names** — names in the People list render in each person's assigned colour (white if none) with a thin black+white outline for legibility on dark/selected rows.
- [x] **Shift+wheel volume on People chips** — Shift + mouse-wheel over a person's sound adjusts that sound's volume (no edit dialog), mirroring the main board: inverted (up=quieter, down=louder), capped 0–150%, live-updates a currently-playing copy, persists, and flashes the chip's length bar as a volume gauge (green / yellow when boosted).
- [x] **Persistent People hub (close = hide, open = instant)** — `PersonHub._on_close` WITHDRAWS the window instead of destroying it (panels + caches survive); `reopen()` deiconifies + reconciles staleness (`refresh_people`, `ensure_fresh`, covered-dirty drain, chip-rewrap flush). Pop-outs do the same (`PersonPopout.reopen()`). Real teardown only via `PersonHub.shutdown()` on app exit (`_real_quit`). Esc also hides the hub.
- [x] **People hub prebuilt hidden at startup** — `_prebuild_person_hub` (gui.py, `after(4500)`) constructs the hub withdrawn (CTk's deferred deiconify honours an immediate `withdraw()`), so the FIRST click on 👥 People is a ~50ms deiconify. While withdrawn, panels build with covered-panel semantics (`select()` derives `_showing` from `wm_state() != "withdrawn"`).
- [x] **Empty-group stub rows** — shared groups with no sounds for a person render as a 3-4 widget stub row (`_build_group_stub`: caret + emoji/name, droppable, right-click menu) instead of a full ~9-widget card; the real card builds lazily on click/expand (`_upgrade_stub`, `pack(before=stub)`). In the real config 149 of 220 cards were empty — two-thirds of all card-build work skipped.
- [x] **Measured build-pump policy** — pump ticks carry ~14-16ms of single-widget jobs (`_CHUNK=1`; `_build_group` enqueues its expand step instead of running it inline) and flush `update_idletasks()` on accumulated-work debt (~100ms visible / ~300ms covered / always on drain). This kills BOTH failure modes measured by probe: the ~1.6s idle-starvation cliff (no flushing) and the quadratic ~350-420ms-per-tick masonry re-layout (flushing every tick). Chip label wrap width is seeded from the real window width (`_chip_avail_base`) and `_flush_chip_rewrap` defers while the pump streams, so the post-layout rewrap is one cheap pass.
- [x] **Prewarm gating + last-person memory** — background panel warm starts only once the active panel's pump drains (`_maybe_start_prewarm`); the hub opens on the last-selected person (`last_person` via the ctx geometry store) and the chip size (L/M/S) + group-columns choices persist across launches (`chip_size`/`gcols`).
- [x] **Avatar master-decode cache** — `circle_avatar` decodes each picture ONCE per `(path, mtime)` into a ≤256px square master (JPEG `draft()` fast-path) and derives every requested size from it; sidebar 26px / title 28px / dialog 36px no longer each pay a full photo decode.
- [x] **CTk perf patch #4** — `CTkToplevel._update_dimensions_event` early-returns for child-widget `<Configure>` events (it only tracks the toplevel's own size), removing hundreds of pointless Tcl round-trips per panel build.

---

## Planned Features / Backlog

### High Priority
- [ ] Fix volume above 100% not making sounds louder (soft clipping needs work)

### Medium Priority
- [ ] Drag-and-drop sound file import (from file explorer)
- [ ] Search/filter sounds
- [ ] Add a sound group/type so we can later filter it or search
- [ ] Looping sounds option
- [ ] Fade in/out effects
- [ ] Add stream deck integration
- [ ] YouTube to MP3 trimmer
- [x] ~~Add sound filters for voice modulation (pitch shift, robot, echo, etc.)~~ — done: real-time mic **Voice Changer** (see Current Features)
### Low Priority
- [ ] Import/export config profiles (sounds, images, tabs)
- [ ] System tray minimization
- [ ] Auto-start with Windows
- [ ] Discord Rich Presence
- [ ] Emoji search by description in picker
- [ ] Convert sound editor to CustomTkinter

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
```

#### `AudioMixer`
Handles real-time audio processing.
- Captures microphone input
- Queues and mixes sound effects
- Outputs to virtual audio device
- Manages playback state and PTT integration
- Uses SoundCache for fast cached playback
- Uses Python `logging` module for all debug output

**Key Methods:**
- `start()` - Begin audio stream
- `stop()` - End audio stream
- `play_sound(file_path, volume, speed, preserve_pitch, sound_id)` - Queue a sound
- `stop_sound(sound_id)` - Stop a specific sound by ID
- `stop_all_sounds()` - Clear playback queue
- `_output_callback()` - Real-time mixing (called by sounddevice)

**Class Constants:**
- `PTT_BUTTON_MAP` - Maps mouse button names to Windows API button names

#### `SoundCache`
Manages local sound storage and in-memory caching.
- Copies sounds to `sounds/` folder for persistence
- Pre-loads audio data at target sample rate (48kHz)
- Provides O(1) lookup for cached audio
- **Persistent decoded-PCM disk cache (`audio_cache/`):** the decoded + resampled float32 PCM is also saved to `audio_cache/<key>.npy` and reloaded on later launches, so warming/first-play skips decode + ffmpeg + resample. Key = `md5(abspath|mtime|sample_rate)`, so editing/replacing a file auto-invalidates it. Falls back to decoding if the cache is missing/corrupt — never breaks playback. The sound editor reads source files directly (not this cache), so edit/clone/trim are unaffected.

**Key Methods:**
- `add_sound(source_path)` - Copy sound to local storage, cache it, return local path
- `get_sound_data(file_path)` - Get pre-loaded audio data (fast)
- `preload_sounds(paths)` - Pre-load multiple sounds at startup
- `remove_sound(file_path)` - Remove from cache and optionally delete file
- `_load_into_cache(file_path)` - Decode+resample (or `np.load` from `audio_cache/`), store in RAM + persist to disk
- `_disk_cache_path(file_path)` - Resolve the `audio_cache/<key>.npy` path for a sound (key = abspath+mtime+sr)

#### `SoundboardApp`
Main GUI application.
- Tkinter-based interface
- Device selection
- Sound slot grid management
- Configuration persistence
- Integrates with SoundCache for local storage

---

## Configuration Format

`soundboard_config.json`:
```json
{
  "hover_preview_key": "mouse3",
  "scroll_speed_multiplier": 10,
  "ptt_enabled": true,
  "ptt_key": "mouse4",
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
2. Run: `pip install -r requirements.txt`
3. Run: `python main.py`
4. Select your mic as Input, "CABLE Input" as Output
5. In Discord: Set input device to "CABLE Output"

### For Developers

```bash
# Clone/create project
cd LocalSoundBoardProject

# Install dependencies
pip install -r requirements.txt

# Run with admin (for global hotkeys)
python main.py
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

### Version 1.3.x (People-Window Launch Performance — 2026-07-17)

**Problem:** opening the People window was "extra slow" every time despite earlier passes. A 7-agent audit + live benchmark (real config: 10 persons / 220 group cards / 165 sounds) measured: window maps fast (~190ms) but true time-to-interactive was ~2.9s (1.3s pump + a hidden ~1.6s idle-starved geometry backlog), the background prewarm then hammered the UI for ~19s with repeated ~1.3-1.7s freezes, closing destroyed everything (~4s), and every reopen re-paid the entire cost.

**Fixes (all measured before/after with the same harness):**
- **Persistent hub:** close = `withdraw()`, reopen = `reopen()` (~270ms measured, vs seconds). Pop-outs identical. Real destroy only on app exit (`shutdown()`).
- **Hidden startup prebuild:** `_prebuild_person_hub` builds the hub withdrawn at app idle (`after(4500)`), so even the first open is the reopen path.
- **Empty-group stubs:** 149/220 cards in the real config were placeholders for empty shared groups — now 3-4 widget stub rows that stay droppable/menu-able and upgrade to real cards on demand.
- **Pump flush policy:** probed three alternatives (never flush = one ~1.6s cliff; every tick = quadratic, 632ms of jobs vs 3,457ms of flushes; bounded `dooneevent` = useless, single reflow atom is 240-400ms). Landed: debt-based `update_idletasks()` (~100ms visible / ~300ms covered / on drain). Jobs right-sized (`_CHUNK=1`, expand step enqueued) under a 14-16ms tick.
- **Rewrap discipline:** `_flush_chip_rewrap` waits for the pump to drain; wrap width pre-seeded from real window width — the old ~85-120ms post-launch PIL re-render wave is now usually a no-op.
- **Avatar master cache** (one decode per photo, sizes derived) + fallback-only 18px chip emoji + CTk patch #4 (toplevel `<Configure>` child-event guard).
- **UX:** hub opens on the last-used person; chip size + group columns persist; Esc hides the hub.

**Result (same benchmark):** first-panel stream 3.4s fully progressive with worst stall ~400ms (was 2.9s mostly frozen), prewarm-all 14.8s with ~400ms max background stalls (was ~19s with 1.3-1.7s freezes), close 17ms (was ~4s destroy), reopen 270ms (was full cold rebuild) — and with the prebuild, the user-visible open is always the 270ms path. Verified: 21/21 unit tests, live open/close/reopen benchmark, 15-check gui-glue smoke test (prebuild stays withdrawn, reopen states, popout lifecycle, shutdown).

### Version 1.3.0 (Performance, Data-Safety & Window-Size — 2026-06-09)

**Data recovery & safety (after a people-data wipe):**
- **Root cause found & fixed:** `_save_config_now()` guarded *tabs* against being wiped but **not persons** — so when `self.persons` went empty in memory (a transient state after the pop-out crash below), the auto-save wrote `persons: []`, and because each save copies the live file over `.bak` first, a second save destroyed the only backup too. Added a **persons data-safety guard**: an empty in-memory persons list can no longer overwrite people that still exist on disk (it preserves the on-disk people instead).
- **Rotating config backups:** `_snapshot_good_config()` runs once at startup right after a successful load and copies the known-good config to `config_backups/` (keeps the last 20). Unlike `.bak` (overwritten every save), this folder is write-once per launch, so a session can never destroy the only good copy again.
- **Recovered 8 people / 83 person-sounds** from git's object store (dangling blobs) + a deep per-person merge across all backups, and surfaced **80 orphaned audio files** (lost slot-sounds still on disk) as a **"♻ Recovered" tab** for re-filing.
- **Pop-out crash fix:** `PersonPanel._cleanup()` now cancels every pending `after()` timer (preview-progress tick, reset, reflow, nudge) on close, so a callback can't fire on a destroyed window (`TclError: invalid command name`).

**Performance:**
- **Decoded-audio disk cache (`audio_cache/`):** `SoundCache._load_into_cache()` saves the fully decoded + 48 kHz-resampled float32 PCM to `audio_cache/<key>.npy` and loads it back on later launches, **skipping decode/ffmpeg/resample entirely** (warming hundreds of sounds becomes `np.load` instead of re-decoding every launch). Keyed by abspath + mtime + sample-rate so an edited/replaced sound auto-invalidates; falls back to decoding if a cache file is missing/corrupt, so it can never break playback. The sound editor reads source files directly, so **edit / clone / trim are unaffected**.
- **Startup freeze removed:** dropped the per-tab synchronous `update_idletasks()` warm-up in `_build_tab_widgets` — it forced a full-window layout+paint flush for **every** background tab at startup (18–19 blocking flushes on a large config). Virtualization already repaints a tab on first show, so it was redundant; first switch now lays out lazily.
- **People-hub hang fixed:** `PersonPanel._expand_group_inplace()` now builds chips in **chunks of 6 across event-loop ticks** instead of all at once, so a sound-heavy person no longer freezes the UI thread. (A frozen UI thread also stalls the app's global keyboard hook → system-wide key lag, which is what made Shift stop working while a person window was open.)
- **People-hub search debounce (#37):** `PersonHub._on_search()` is now debounced (`SEARCH_DEBOUNCE_MS = 180`, same pattern as the emoji picker) so a fast typist no longer tears down + rebuilds the whole sidebar **and** re-filters the active panel on every keystroke — bursts collapse to a single rebuild. The pending timer is cancelled on hub close so a callback can't fire on a destroyed window.
- **People-hub panel-cache eviction (#39):** the per-person `PersonPanel` cache is now an LRU `OrderedDict` bounded to `MAX_CACHED_PANELS = 5` — clicking through everyone destroys the least-recently-viewed panels (each ~200 CTk widgets) instead of keeping all of them resident forever. The on-screen panel is always kept; eviction calls `PersonPanel.destroy()` so its after-timers are cancelled too.
- **Chip-label PIL cache (#36-lite):** `_chip_label_image()` was re-compositing emoji + outlined text **and** rebuilding a `CTkImage` for every chip on every rebuild/search/density change. The composited PIL canvas is now cached by `(name, emoji, px)` in `_chip_label_pil_cache` (compose split into `_compose_chip_label_pil()`), wrapping a **fresh** `CTkImage` per call — the same "cache the PIL, never share the CTkImage" contract as the avatar cache. (Full per-chip *viewport virtualization* — the heavier #36 — was deliberately **deferred**: the chunked build already removed the freeze and real people carry ~10–30 sounds, so virtualizing the fragile masonry layout is high regression-risk for little real gain. Revisit only for hundreds-of-sounds people.)
- **People sounds warmed into the decoded cache:** the startup warm-up (`_preload_sounds`) only walked the main-board tabs — person sounds were never pre-decoded, so the first click on one paid a decode. It now also walks `self.persons[*].groups[*].sounds`, so People's sounds are decode-cached to `audio_cache/` at launch like everything else (deduped against main-board files via the same `seen` set). They already shared the same `SoundCache`/disk cache on playback; this just removes the cold first-play.
- **Readable two-line sound chips (light & dark backgrounds):** chip labels wrap a long name onto up to 2 lines instead of clipping with one ellipsis, and are rendered **bold white + a solid ~2px black outline** (subtitle style) so they stay legible on ANY chip colour/state. This matters because the chip **recolours on play** (idle person-colour → amber "playing" → green "preview"), so the baked label must read on all three — a hairline edge wasn't enough on light colours (yellow/near-white) or the amber state. `_outlined_text_pil()` was generalised to render multiline (`multiline_textbbox`/`multiline_text`, per-line BiDi so Hebrew stays correct), a `fill` colour, `align`, and `bold` (loads `segoeuib.ttf`). Constants: `_OUTLINE_SS` 2→3 (crisper), `_OUTLINE_TEXT_PX`=14, `_OUTLINE_WB`=6 (~2px), `_OUTLINE_WW`=0 (dropped the fuzzy white ring), `_OUTLINE_BOLD`.
  - **Wrap by measured PIXEL width, not char count** (`_wrap_label(name, max_px)`): a char-count budget never triggered for bold/Hebrew (a "short" 20-char Hebrew name is wider than a narrow chip). It measures each candidate line with an `ImageDraw.textbbox` using the **same font + stroke** the label is drawn with (`font.getbbox` under-measured and never wrapped), greedy word-wrap with char-break + `…` on the last line, and subtracts the canvas pad so a line+outline fits.
  - **Re-wrap to the chip's REAL width** (`_on_chip_configure`): the masonry can make a tile much narrower than `chip_w` (varies with window/columns/DPI), so a build-time estimate clips. The play button's `<Configure>` re-wraps to its actual `winfo_width()` (÷ window scaling → logical) and re-renders the label only when the wrapped text changes. **Gotcha:** `CTkButton.bind("<Configure>")` forwards to the button's internal canvas, so `event.widget` is NOT the button — capture the button in a closure and read `winfo_width()`.
  - **Each wrapped line is rendered SEPARATELY and stacked**, NOT via PIL `multiline_text`: `multiline_text` *with a stroke* leaves a solid **black band under the lower line(s)** (1-line chips were fine, every 2-line chip had a black rectangle behind the bottom line). `_outlined_line_pil()` renders one line (the clean single-line path); `_outlined_text_pil()` stacks the lines aligned per `align` (right for RTL). Person/group names are single-line so they pass straight through.
  - RTL note: `textbbox` returns **float** coords for RTL, which crashed `Image.new` (silent blank) — dims are `math.ceil`'d to ints. `_SIZES` heights bumped (L52/M48/S44) to fit two lines. Verified by rendering chips over green/amber/preview/light-yellow/near-white/dark-blue, at narrow/wide widths, and zoomed to confirm no black band.
- **Chip length bar is invisible when idle (no "shadow"):** the per-chip length/progress bar used `progress_color=fg`, so its idle 0% cap showed as a stray dot/dark mark at the bottom. It's now built with `progress_color=bg` (fully blended). `_start_progress()` switches the fill to `fg` for the duration; `_cancel_progress()` and the `_show_chip_volume` restore set it back to `bg`. So the bar only appears while playing/previewing or during a Shift+wheel volume change.
- **Person + group/tab names coloured & thin-outlined:** person names in the hub list render in **their assigned colour** (white if none); group/tab headers render white — both **bold with a HAIRLINE black+white outline** so they're legible on dark and lighter rows, deliberately much thinner than the bold sound-chip outline. Shared helper `_thin_outlined_image(text, colour, px, bold)` (used by `_person_name_image()` and `_group_name_image()`) → `_outlined_text_pil(fill=…, bold=True)` with thin `_NAME_WB/_NAME_WW`, cached by `(text, colour, px, bold)` in `_thin_label_pil_cache` (PIL cached, fresh CTkImage per call). Both fall back to the previous plain text label if the render fails.
- **Shift+wheel volume on People chips:** Shift + mouse-wheel over a chip now adjusts that sound's volume (the gesture the main board already had, which People was missing). `_SpeedScrollableFrame` gained an `on_shift_wheel(event)` hook; `PersonPanel._shift_wheel_volume()` finds the hovered chip by walking `.master` up to the tagged `chip._slot_ref`, adjusts `slot.volume` (inverted: up=quieter, down=louder; cap 0–1.5 to match the mixer/main board), live-updates a playing copy via the new `PersonContext.set_volume` callback (`SoundboardApp._set_person_sound_volume` → `mixer.set_sound_volume("person_{id}", v)`), persists (debounced), and flashes the chip length bar as a volume gauge (`_show_chip_volume`, green / yellow when >100%). Shift is detected from the raw `event.state & 0x0001` OR CTk's `_shift_pressed` (the raw modifier is reliable even when Tk lacks keyboard focus, e.g. focus on Discord). Volume-bar timers are cancelled in `_cleanup`.
- **Image caches:** slot-image decode cached by `(path, mtime, size)` (reused across tabs/search/density rebuilds); person-hub avatar decode/crop/mask cached the same way.

**Window / HiDPI:**
- **Window no longer grows huge each launch:** restore geometry via `wm_geometry()` (raw Tk) instead of CustomTkinter's `geometry()`. We save `winfo_geometry()` in physical px, but CTk's `geometry()` setter re-multiplies the size by the window scaling, so at 150 % DPI the window grew ~1.5× every launch. `wm_geometry()` round-trips `winfo_geometry()` identically.

**Housekeeping / docs:**
- gitignore + untrack `debug.log` / `perf.log` / `importtime.txt`; gitignore `config_backups/`, `audio_cache/`, `_recovery/`.
- Added `docs/PERFORMANCE_PLAN.md` (full bottom-to-top perf audit + 3-plan roadmap) and `docs/SESSION_BACKLOG.md` (open items + perf backlog).
- Added `stop_soundboard.bat` (force-close orphaned app processes) and `soundboard/perf_probe.py` (opt-in `LSB_PERF=1` timing harness; inert otherwise).

### Version 1.2.0 (Voice Changer & Dynamic Grid - 2026-06-03)
- **Real-time mic Voice Changer** (`soundboard/voice_fx.py` → `VoiceChanger`): pure-numpy effect chain applied to the live mic inside `AudioMixer._output_callback` (after RNNoise, before soft-clip → virtual cable), so the Discord call hears the effect. Effects: pitch shift (crossfading delay-line shifter), drive, bitcrush, ring-mod (robot), `scipy.signal` bandpass (radio/megaphone/telephone), chorus/vibrato, echo, Schroeder-comb reverb, tremolo, output gain. Lock-free reads in the audio thread; `process()` never raises and always returns len(input).
- **17 one-tap presets** + Voice Changer card in Audio Options (master Enable, preset grid, collapsible ⚙ Advanced drawer with pitch slider / per-effect toggles / output level). Persisted under `voice_changer`; applied on stream start and mirrored live.
- **Dynamic grid density**: header ⊞ Columns −/+ control (2–12 slots per row). `self.grid_columns` replaces the `UI["grid_columns"]` constant across `_build_tab_widgets` and the search overlay; changing it rebuilds all tab grids and persists under `grid_columns`.
- Added explicit `scipy>=1.10.0` to requirements (used directly by the radio effect; already a librosa dep).
- Perf: `_hide_search_results` now clears `_search_slot_widgets` so the animation loop stops doing per-frame `.set()` on destroyed overlay widgets.
- Verified: headless DSP tests (pitch ±12 st moves dominant freq; all 17 presets stable; reverb+echo feedback decays with no runaway), all 21 existing unit tests pass, full package imports clean.

### Version 1.2.3 (Input & Editing Polish - 2026-06-01)
- Added Shift+mousewheel hover volume control, app-wide scroll-speed setting, hardened quick-popup click-through suppression, and restored explicit Edit in right-click options
- Increased scroll-speed slider weight so each configured step scrolls several Tk units instead of feeling 1:1/minor
- Added action-bar Sounds folder shortcut, Quick Record to Sound, duration-aware recording filenames, long-record editor background preparation, and tab reordering manager
- Fixed quick edit popup click-through by using transient/grab focus and routing popup mousewheel events to its controls
- Increased soundboard scroll speed for large tabs
- Added configurable hover preview binding for locally previewing the filled slot under the cursor
- Multi-cut now asks for each cut title and uses those titles when saving generated cuts and creating slots
- PTT playback path validated: the sound is queued and PTT is pressed immediately afterward, with delayed release after playback; audible physical mouse clicks usually come from microphone pickup, not from switching the mic to sound a second later

### Version 1.2.0 (Refactor & Performance - 2026-02-19)
- Replaced all raw `debug.log` file writes with Python `logging` module (buffered, configurable)
- Fixed non-atomic config save: `os.remove()`+`os.rename()` → single `os.replace()`
- Fixed `preserve_pitch` silently resetting to `True` when saving from full config dialog
- Fixed hotkey playback not tracking progress for non-current tabs
- Fixed image hash reading entire file into memory (`f.read()[:4096]` → `f.read(4096)`)
- Fixed `bind_all("<MouseWheel>")` stealing scroll events globally → scoped to canvas area
- Fixed sound editor using low-quality nearest-neighbor resampling → uses `_resample_audio()` (librosa/scipy)
- Removed dead/identical code branch in editor `_on_save`
- Removed duplicate pydub/ffmpeg initialization from `editor.py` (delegates to `audio.py`)
- Extracted duplicated PTT button map to `AudioMixer.PTT_BUTTON_MAP` class constant
- Made `_apply_fade_out` work in-place (eliminates redundant array copy)
- Background thread for sound preloading at startup (non-blocking UI)
- Lazy-loaded emoji categories (deferred from import time to first picker open)
- Editor playhead updates only redraw the playhead line, not the entire waveform
- Logging configured in `main.py` before package imports
- Fixed emoji picker freeze: categories now load async via `after()` (one at a time)
- Fixed colorless emojis: use `tk.Label` instead of `CTkButton` for proper Windows emoji rendering

### Version 1.2.2 (Infinite Tabs & Delete Feature - 2026-02-20)
- **Delete sound with confirmation**: Added 🗑️ delete button to quick popup, confirmation dialog for Clear button
- **Unlimited tabs**: Removed 4-tab limit, tab bar now scrolls horizontally
- **Scrollable tab bar**: Tab bar uses `tk.Canvas` with horizontal scrolling
- **Dynamic scroll buttons**: ◀ ▶ buttons appear only when tabs overflow
- **Tab mousewheel scrolling**: Scroll wheel works on tab bar to navigate tabs
- **Auto-scroll to active tab**: Active tab scrolls into view when selected
- **Move button relocated**: Moved from soundboard header to tab bar (left side)
- **Fixed move functionality**: `_swap_slots` and `_move_slot_to_tab` now use per-tab widget update methods
- Added `_delete_slot()` helper for sound deletion with cache cleanup
- Added `_ensure_slots_for_tab()` to rebuild target tab widgets when moving sounds
- Added `_scroll_tab_into_view()`, `_update_tabs_scroll_buttons()` for tab bar management

### Version 1.2.1 (Tab Switching Performance - 2026-02-20)
- **Zero-delay tab switching**: Implemented per-tab widget frames architecture
- Each tab now has its own pre-built widget grid stored in `tab_grid_frames`
- Switching tabs uses `tkraise()` (single Tkinter call) instead of updating widgets
- All tab widgets built upfront via `_build_all_tab_widgets()` at config load
- Added per-tab widget storage: `tab_slot_buttons`, `tab_slot_frames`, etc.
- Legacy widget aliases (`slot_buttons`, `slot_frames`) point to current tab's widgets
- New tabs get widgets built immediately via `_build_tab_widgets(tab_idx)`
- Tab switching works instantly even with 100+ sounds per tab

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

### CRITICAL Lessons — 2026-06-09 (data loss, DPI, freeze→keyboard)

| Issue | Cause | Fix / Rule |
|-------|-------|-----|
| **Entire `persons` list silently wiped** | `_save_config_now()` guarded `tabs` against an empty-overwrite but **not `persons`**; a transient empty `self.persons` (after the pop-out crash) was auto-saved as `persons: []`, and the next save copied that over `.bak` too — destroying the only backup. | Guard EVERY critical config section against an empty-overwrite (now: if in-memory persons is empty but disk has people, preserve the disk people). Keep **rotating, write-once-per-launch backups** (`config_backups/`, via `_snapshot_good_config()`), never just a single `.bak` that the app overwrites. Recovery: decoded-PCM and slot files survive in `sounds/`; old configs survive as git dangling blobs (`git cat-file --batch-all-objects` + parse for `persons`). |
| **Window grows ~1.5× bigger every launch (HiDPI)** | We SAVE geometry via `winfo_geometry()` (PHYSICAL px) but RESTORE via CustomTkinter's `geometry()`, which **re-multiplies** size by the window scaling. At 150 % DPI the window balloons each launch. | Restore with **`self.root.wm_geometry(geo)`** (raw Tk, no CTk scaling) so it round-trips `winfo_geometry()` exactly. NEVER restore a `winfo_geometry()` value through CTk's `geometry()`. |
| **Shift / keyboard stops working system-wide** | The app holds a global `keyboard` hook; when the UI thread **freezes** (e.g. the People-hub built all chips synchronously), the hook stalls → Windows blocks keyboard input app-wide. Force-killing the app clears it; a stuck modifier is released by re-sending key-up. | Never let the UI thread freeze — build heavy widget sets **incrementally** (`after()`-chunked). If a kill leaves a modifier stuck, inject key-up via `keybd_event`. |
| **Startup "stuck" for ~2 min** | Two synchronous costs: per-tab `update_idletasks()` warm-up flush for all 18–19 tabs, AND re-decoding ~500+ sounds every launch (OGG/MP3 each spawn ffmpeg). Re-launching during the wait spawned **6 app instances** ("3 windows"). | Removed the per-tab warm-up flush; added the **decoded-audio disk cache** (`audio_cache/`). (Backlog: a single-instance guard so re-launching can't stack instances.) |
| **Decoded-audio cache & editing** | — | `audio_cache/<key>.npy` is keyed by abspath+mtime+sr, so editing/replacing a sound auto-invalidates it; the editor reads source files directly (never the cache); a missing/corrupt entry falls back to decoding. Safe with edit/clone/trim/record. |

### Performance Lessons — 2026-06-10 ("laggy / smudgy moving" session)

| Issue | Cause | Fix / Rule |
|-------|-------|-----|
| **System-wide mouse-cursor lag** | The hover-preview mouse binding installed the `mouse` library's WH_MOUSE_LL hook, which runs Python for EVERY system mouse event — every pixel of pointer travel, in every app. Whenever the Tk thread held the GIL through a redraw burst, Windows delayed the whole pointer. | NEVER keep a persistent `mouse.hook()`. Hover-preview now edge-detects the button via a 30 ms `GetAsyncKeyState` `after()`-poll (`_poll_hover_preview_button`). The `mouse` library remains only for the transient 5 s "Record Key" capture. |
| **Window MOVE treated as resize** | A pure title-bar drag fires root `<Configure>` with unchanged w×h; arming the resize-defer + post-resize sweep on moves paused the progress animation and queued a useless chunked redraw wave after every drag. | `_on_root_configure` early-returns when `(event.width, event.height) == self._last_root_size`. Only real size changes arm `_SHARED_RESIZE_STATE`. |
| **`tab_slot_buttons` holds ButtonProxy facades, NOT SlotWidgets** | `hasattr(btn, '_volume_display')` guards silently failed, so the slot volume gauge never rendered. | Unwrap with `getattr(x, "slot_widget", x)` before touching SlotWidget internals from the per-tab stores. |
| **Canvas delete+recreate on hot paths** | Hover Enter/Leave did `delete("all")` + full rebuild per slot crossed; the progress bar deleted + recreated its rects every animation tick. | Tk canvas golden rule: persistent items + `itemconfigure`/`coords()`. Hover recolours the `bg`-tagged rect (`_apply_hover_fill`); progress moves a persistent fill rect (`_prog_fill_id`, reset to `None` wherever `delete("all")` runs). Keep the full-redraw fallback. |
| **Root logger at DEBUG = disk I/O per PNG decode** | PIL logs several DEBUG lines per PNG open and numba ~31k lines per JIT warm-up; `debug.log` hit 6 MB+, written synchronously during redraw storms. | Root → WARNING, `soundboard.*` → INFO (`LSB_DEBUG=1` for DEBUG), PIL/numba/pydub/filelock pinned to WARNING, `RotatingFileHandler` 1 MB × 3. Never `open("debug.log")` directly — route through `logging` (its FileHandler flushes every record). |
| **Debounced cull vs fast wheel** | One wheel notch can jump past the 3-row virtualization buffer; a 40 ms-debounced cull left blank rows visible, then a burst regrid. | The grid wheel handler culls synchronously after `yview_scroll` (cheap now that `_apply_row_minsizes` memoizes per `(grid, cols, rows, row_height)`). |
| **Per-pixel `<Configure>` work in dialogs** | People-chip title re-wrap ran PIL text measurement per chip per resize pixel; both editor waveform canvases fully redrew per resize pixel. | Collect-and-flush: chips re-wrap once per 120 ms (`_flush_chip_rewrap`, scaling resolved once per pass, timer in `_cleanup`); editor canvases debounce redraw 80 ms with a `winfo_exists()` guard. |
| **People windows resized with the full CTk redraw cascade** | The CTk `_draw()` defer patch only deferred MAIN-window widgets — deferring another toplevel's widgets with no sweep had left them permanently blank, so hub/pop-outs drew every widget per resize pixel instead. | Per-toplevel defer: `_shared.arm_toplevel_resize_defer(top)` (own until-window in `RESIZE_STATE["tops"]`, own chunked sweep draining `RESIZE_STATE["dirty_tops"][top]`). Arm it from the toplevel's own `<Configure>` on TRUE size changes only — never pure moves, never the first map (baseline `_lsb_last_size`). Any new always-open toplevel should arm this too. |
| **First click on a long sound froze the app** | Sounds ≥4 MB were deliberately excluded from the RAM warmer (memory), which left them fully cold — first play decoded + resampled synchronously on the GUI thread. | `SoundCache.warm_disk_cache(fp)`: decode once to `audio_cache/<key>.npy` WITHOUT retaining in RAM (skip ≥10-min files — those stream). The warmer calls it for long files; first play is then a single `np.load`. |
| **Destroying CTk widgets is O(all widgets in the app)** | CTk's `AppearanceModeTracker.callback_list` + `ScalingTracker.window_widgets_dict` are GLOBAL lists removed from with `list.remove()` (linear equality scan over thousands of bound methods). One ~300-widget panel destroy ≈ 1M+ comparisons. | gui.py "perf patch #2": values replaced with `_CallbackBag` (dict keyed `(id(self), __func__)` → O(1)). Don't write code that destroys big CTk trees synchronously in click handlers anyway — chunk via `after_idle`. |
| **CTkScrollableFrame leaks 5 `bind_all` handlers forever** | Its `__init__` does `bind_all` for wheel + 4 Shift sequences; `destroy()` never unbinds. Every evicted panel/closed dialog permanently slowed every wheel notch + Shift press. | gui.py "perf patch #3": record the funcids at init, splice them out of the `all` bindtag + `deletecommand` on destroy. |
| **CTkImage CAN and SHOULD be shared (contract change)** | The old "cache the PIL, wrap a fresh CTkImage per call" rule discarded CTkImage's per-instance scaled-PhotoImage cache — every rebuild re-ran a BICUBIC resize + PIL→Tk conversion per chip (~1ms each, 100+/panel). | Cache the CTkImage itself (bounded LRU, same keys as the PIL caches — see `_ctk_cache_get` in person_board.py). CTkImage tracks consumers and reuses the per-size PhotoImage; just never `.configure(size=)` a shared one. |
| **tkraise-stacked panels all resize together** | All cached People panels stay mapped in one grid cell, so a hub resize Configures ~1500 widgets and the sweep repainted the covered (invisible) ~80%. | The defer patch parks covered panels' widgets on `panel._covered_dirty` (detected via the `_showing is False` master-walk); `select()` drains it chunked right after `tkraise`. Any new stacked-panel design needs the same repaint-on-raise contract. |
| **Mid-drag rebuilds = "design keeps breaking"** | A column-change rebuild firing while the resize drag was still active froze the drag AND its fresh widgets landed inside the defer window (popcorn repaint). | Gate deferred rebuild/re-wrap work on BOTH `RESIZE_STATE["until"]` and `RESIZE_STATE["tops"][win]` and re-arm until the drag settles (`_reflow_when_settled`, `_flush_chip_rewrap`). Interrupted sweeps must RE-PARK their remainder, never drop it. |

### Config & File Handling

| Issue | Cause | Fix |
|-------|-------|-----|
| Config deleted on startup | Missing color constant (e.g., `bg_light`) used in UI before being added to `COLORS` dict | Always add new color constants to `constants.py` BEFORE using them in GUI code |
| Config file corruption (duplicate JSON) | Non-atomic writes - crash during save corrupts file | Use atomic writes: write to temp file, then `os.replace()` to target. NEVER use `os.remove()`+`os.rename()` — there's a window where the file doesn't exist |
| Sound paths break when editing slot | Path comparison failed for relative paths like `sounds/file.mp3` vs absolute | Check both relative AND absolute paths when detecting if sound is already in local storage |

### Audio

| Issue | Cause | Fix |
|-------|-------|-----|
| OGG files fail with "malformed" error | `soundfile` claims OGG support but fails on some files | Use shared `read_audio_file()` function with pydub fallback. Don't include `.ogg` in soundfile_formats list. |
| pydub can't load OGG/M4A/AAC | pydub requires ffmpeg binary which isn't bundled | Install `imageio-ffmpeg` (bundles ffmpeg), set `AudioSegment.converter` to `imageio_ffmpeg.get_ffmpeg_exe()` |
| PTT releases too early | PTT released immediately when audio callback returns | Add debounce delay (5 callback cycles ~100ms) before releasing PTT |
| Sounds don't play with rapid clicks | Lock contention and duplicate cache lookups | Queue sound before taking locks, use single cache lookup |
| Direct sf.read() fails for OGG | Multiple code paths used sf.read directly without fallback | Consolidated audio loading to shared `read_audio_file()` function in audio.py |
| PTT gets stuck when user presses PTT during playback | `mouse` library's `press()`/`release()` has internal state tracking that can conflict with physical mouse input; queue-based release can be lost or reordered | Replace `mouse` library simulation with direct Windows `SendInput` API for mouse buttons; use `_force_release_ptt()` (direct, not queued) for stop operations; drain PTT queue before force-releasing |
| **"PTT locked" while user holds the key + mic audible during sounds (2026-06-10)** | We inject the user's OWN Discord PTT key for auto-PTT. (a) Sound ends while the user physically holds the key → our injected key-UP kills Discord transmit and a held key sends no new key-downs → dead until re-press. (b) Auto-PTT transmits the whole cable → everyone heard the live mic with every sound. | Mixer tracks the PHYSICAL key via keyboard hook (`ptt_user_physical`); our SendInput moments are excluded via `_inject_window_until` windows (injected keys don't auto-repeat, physical ones do). ALL release paths skip the key-up while the user holds; press skips injection when already physically down. Mic is ducked in the mix while `ptt_active and not ptt_user_physical and not manual_ptt_hold`, behind `duck_mic_during_sounds` (persisted, checkbox, default ON). Mouse PTT keys have no physical signal — flag stays False. |
| **RNNoise too weak vs Krisp → DeepFilterNet3 (2026-06-11)** | RNNoise is a tiny 2017 model, weak on non-stationary noise (keyboard/voices). Krisp can't be used because we mix mic+sounds into ONE cable stream (it would eat the sound FX) — local mic-only denoise is the only architecture. | `NoiseSuppressor` is now pluggable (`_DenoiseBackend`: `deepfilternet`\|`rnnoise`\|none), default DeepFilterNet3 via onnxruntime (NO PyTorch), 48 kHz, model at `soundboard/models/denoiser_model.onnx`. Reuses the existing 480-frame ring + priming; warm up the backend (5 dummy frames) on build or the first real inference glitches (~10× slower). Pick a 48 kHz-native model — 16 kHz models (GTCRN/NSNet2/Picovoice) sound telephone-muffled. onnxruntime in PyInstaller needs `collect_dynamic_libs`+`collect_data_files` (else `onnxruntime_providers_shared.dll` load error). Fallback chain DeepFilterNet→RNNoise→passthrough. Weights-license caveat for the paid build: keep BSD RNNoise as the clean default or regenerate the ONNX from Rikorose official split models. |
| Preview sounds can't be stopped | Preview uses `sd.play()` directly with no stop mechanism; stop button only stops mixer sounds | Added `_stop_preview()` and `_stop_all_previews()` methods; preview button toggles play/stop; stop button and "Stop All" also stop previews; show stop button during previews |

### GUI / Tkinter

| Issue | Cause | Fix |
|-------|-------|-----|
| Progress bar shows on wrong tab | Playing state not tracking which tab the sound belongs to | Store `tab_idx` in `playing_slots` dict, only update UI if current tab matches |
| Preview button not visible | Button hidden by expanding main button | Pack bottom frame FIRST at bottom, then main button expands into remaining space |
| UI elements resize unexpectedly | Grid weights and pack expand options | Use `grid_propagate(False)` and `pack_propagate(False)` to lock sizes; set `resizable(False, False)` on window |
| Sound plays twice on click | Button had both `command=` and raw canvas bindings calling `_play_slot` | **CURRENT FIX:** Use ONLY `command=` for click-to-play. Raw canvas bindings are wiped by `_draw()`. See "Slot Button Click & Drag Architecture" section. |
| Colors reset when dragging | `_reset_drag_highlights()` set all slots to `bg_medium` instead of proper color | Call `_update_slot_button()` to restore full button appearance |
| Playing color shows on wrong tab after switch | `_update_slot_button` didn't check if playing state was for current tab | Check `playing_slots[idx].get("tab_idx") == current_tab_idx` before applying playing color |
| Scrollbar shows when not needed | Scrollbar always visible even with few slots | Add `_update_scrollbar_visibility()` to show/hide based on content height vs canvas height |
| Scrollbar visibility false positive | `winfo_height()` returns 1 before widget is mapped | Check `canvas_height <= 1` and return early; use `winfo_ismapped()` before pack/pack_forget |
| Editor crashes on OGG import | `_update_info_labels()` called before `selection_label` created | Add `hasattr()` check before updating `selection_label` in `_update_info_labels()` |
| Mouse wheel scrolls when no scrollbar | `_on_mousewheel` always scrolls regardless of scrollbar state | Check `slots_scrollbar.winfo_ismapped()` before allowing scroll |
| No new slots after filling all | `save` only called `_update_slot_button` not `_refresh_slot_buttons` | Call `_refresh_slot_buttons()` after save/clear to create new empty slots |
| Sound replays when switching tabs | Click/drag state not cleared on tab switch | `_switch_tab()` calls `_reset_click_state()` to atomically clear all interaction state. See "Tab Integration" in architecture section. |
| Empty slot '+' button doesn't open dialog | Empty slot needs special handling since there's no sound to play | Empty slot `command=` callback opens the file dialog directly instead of calling `_play_slot` |
| Sound plays when clicking tab button | Slot's release event fires even when releasing on a different widget | **CURRENT FIX:** Click-to-play uses `command=` (fires only on the bound widget). Drag uses `winfo_containing()` only for drop target detection. |
| Tab buttons unresponsive while sound plays | Slot event handlers don't properly isolate from other widgets | **CURRENT FIX:** `command=` is widget-scoped; `_on_slot_release` only acts if `_click_active` was set by that slot's `_on_slot_press`. |
| Stop button needed double-click | CTkButton's internal canvas/label children get recreated on first pack, losing bindings. `stop_button_clicked` was a `bool` blocking ALL slots. | **CURRENT FIX:** Stop button uses `command=` set once at creation (immune to `_draw()`). `_show_stop_button()` only packs/unpacks, no re-binding. `_just_stopped_slot` + `_just_stopped_at` for slot-specific, time-based replay prevention. |
| Hotkey playback doesn't show stop button | `_play_slot_from_tab` didn't pack the stop button like `_play_slot` does | Add stop button packing logic to `_play_slot_from_tab` in the `update_ui` lambda |
| Clicking other sounds while one plays needs double-click | Old approach used `ButtonRelease-1` for play, which `_draw()` wiped after any `btn.configure()`. Mass `_reset_drag_highlights()` on every release also triggered redraws. | **CURRENT FIX:** Play uses `command=` (immune to `_draw()`). Drag highlights only reset when `was_dragging` is True. See architecture section above. |
| No hover cursor on slot buttons | CTkButton has no default cursor; slot buttons were created without `cursor=` | Added `cursor="hand2"` to slot and stop button creation in `_create_slot_widgets` |
| Dragging plays sound accidentally | Previous drag used main button with 5px threshold — sound played on every drag start | **CURRENT FIX:** Use separate "↔ Move" edit mode. In edit mode, clicks select/swap; in normal mode, clicks play sounds. |
| Emoji picker freezes UI | Creating thousands of `CTkButton` widgets synchronously blocks the main thread | Use `after()` to load categories one at a time asynchronously; limit emojis per category (96 max) |
| Emojis display as colorless/black | `CTkButton` doesn't render colored emojis properly on Windows | Use native `tk.Label` with "Segoe UI Emoji" font instead of `CTkButton`; add hover/click bindings manually |
| Hebrew/Arabic text displays backwards | Tkinter doesn't handle some RTL text consistently in legacy canvas rendering paths | Display Hebrew/Arabic slot labels naturally without pre-reversing text; `_fix_rtl_text()` is now a no-op for slot buttons and should only remain for future RTL-specific refinements. |
| Emoji picker can't scroll | Simple grid layout doesn't support scrolling | Use `tk.Canvas` with scrollbar and `create_window()` to embed scrollable frame; bind mousewheel to canvas |
| Move slot broken after per-tab widgets | `_swap_slots` and `_move_slot_to_tab` called `_refresh_slot_buttons()` which doesn't exist in per-tab architecture | Use `_update_slot_button_for_tab(tab_idx, slot_idx)` for affected slots; use `_ensure_slots_for_tab(target_tab)` before moving to that tab |

### Sound Editor Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Editor pause causes UI freeze | `stream.abort()` and `stream.close()` block the main thread | Set `is_playing = False` first, then close stream in a background thread: `threading.Thread(target=close_stream, daemon=True).start()` |
| Zoom resets to beginning | `_zoom_in()` and `_zoom_out()` didn't preserve cursor position | Track cursor X position in `_on_mouse_wheel()`, calculate sample under cursor, adjust `view_start` after zoom to keep same sample under cursor |
| Editor window looks unprofessional | Mix of ttk.Frame and tk widgets with inconsistent styling | Use consistent tk.Frame with explicit bg colors, flat relief buttons, cursor="hand2", proper padding and spacing |

### CustomTkinter-Specific Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Sounds don't play when clicking slot | CTkButton's `_draw()` method re-binds `<Button-1>` on its internal canvas WITHOUT `add`, wiping any custom canvas bindings. `_draw()` runs on EVERY `btn.configure()` call. | **CURRENT FIX:** Use `command=` for click-to-play (stored as `_command` property, immune to `_draw()`). Use `CTkButton.bind()` for drag detection (CTkButton stores and re-applies these). NEVER bind directly to internal canvas children. See "Slot Button Click & Drag Architecture" section. |
| Tab switching extremely slow | Creating new `CTkFont()` objects on every button update is expensive | Pre-create cached font objects (`_font_sm`, `_font_xl_bold`, etc.) once in `__init__` and reuse everywhere |
| UI lag after tab switch | Image reloading from disk on every `_update_slot_button` call | Track `slot_image_paths` dict to cache already-loaded images; only reload if path changed |
| CTkButton `command=` is the ONLY reliable click handler | `command=` is stored as `_command` property and survives `_draw()` resets. Raw bindings on the internal canvas are wiped. | **Use `command=` for ALL click-to-play and stop-button actions.** Do NOT add redundant `ButtonRelease-1` bindings — they will be wiped and cause confusion. |
| CTkProgressBar has no delete() | Tkinter Canvas `.delete("tag")` doesn't exist on CTkProgressBar | Use `.set(0)` to reset CTkProgressBar instead of `.delete("progress")` |
| Animation loop causes lag | 60fps (16ms interval) animation loop with `.configure()` calls every frame | Reduce to 20fps (50ms); set progress bar color once when playback starts, not every frame |
| Tab bar recreation on every switch | `_refresh_tab_bar()` destroyed and recreated ALL tab buttons on every tab switch | Check if tab count changed; if not, just `configure()` existing buttons with new colors/fonts instead of destroying them |
| Redundant preview/edit button updates | Preview/edit buttons always got `configure()` called even when appearance didn't change | Track `_slot_filled_cache` dict; only call `configure()` on preview/edit buttons when filled/empty state actually changes |
| 2-second tab switching delay | Updating slot widget appearances on every tab switch via `configure()` calls, which triggers CTkButton's expensive `_draw()` method | **Per-tab widget frames architecture:** Each tab has its own pre-built frame with widgets. Tab switch = `tkraise()` (single call). Widgets built upfront via `_build_all_tab_widgets()`. Legacy aliases (`slot_buttons`, etc.) point to current tab's widgets. |
| Stop button stuck visible after sound ends on non-active tab | `_animate_progress()` only updated widgets on `current_tab_idx`, not the tab where the sound was actually playing | **Always update the actual tab's widgets using per-tab storage** (`tab_slot_stop_buttons[tab_idx]`), not the legacy aliases that point to current tab. See "Per-Tab Widget Architecture" below. |

### Per-Tab Widget Architecture (CRITICAL)

> **READ THIS BEFORE MODIFYING ANY WIDGET UPDATE CODE**

Each tab has its own independent widget set. When updating widgets for a sound/slot, you MUST use the correct tab's storage.

#### Widget Storage Structure
```python
# Per-tab storage: tab_idx -> slot_idx -> widget
tab_grid_frames: Dict[int, Frame]           # One grid frame per tab
tab_slot_buttons: Dict[int, Dict[int, CTkButton]]
tab_slot_frames: Dict[int, Dict[int, CTkFrame]]
tab_slot_progress: Dict[int, Dict[int, CTkProgressBar]]
tab_slot_stop_buttons: Dict[int, Dict[int, CTkButton]]
tab_slot_preview_buttons: Dict[int, Dict[int, CTkButton]]
tab_slot_edit_buttons: Dict[int, Dict[int, CTkButton]]
tab_slot_emoji_labels: Dict[int, Dict[int, CTkLabel]]
tab_slot_images: Dict[int, Dict[int, CTkImage]]
tab_slot_image_paths: Dict[int, Dict[int, str]]
_tab_slot_filled_cache: Dict[int, Dict[int, bool]]

# Legacy aliases (point to CURRENT tab only)
slot_buttons = tab_slot_buttons[current_tab_idx]
slot_frames = tab_slot_frames[current_tab_idx]
# etc.
```

#### CORRECT: Updating widgets for a specific tab
```python
# When a sound finishes playing on tab 3 while viewing tab 1:
tab_idx = playing_slots[slot_idx]["tab_idx"]  # This is 3

# ✅ CORRECT: Use per-tab storage directly
if tab_idx in self.tab_slot_stop_buttons:
    if slot_idx in self.tab_slot_stop_buttons[tab_idx]:
        self.tab_slot_stop_buttons[tab_idx][slot_idx].pack_forget()

self._update_slot_button_for_tab(tab_idx, slot_idx)  # Updates tab 3's widgets
```

#### WRONG: Using legacy aliases
```python
# ❌ WRONG: Legacy aliases only point to current tab
if slot_idx in self.slot_stop_buttons:  # This is tab 1's widgets!
    self.slot_stop_buttons[slot_idx].pack_forget()  # Updates wrong tab
```

#### When to Use What
| Scenario | Use | Method |
|----------|-----|--------|
| User clicks slot on current tab | Legacy aliases OK | `_update_slot_button()` |
| Sound finishes on ANY tab | Per-tab storage | `_update_slot_button_for_tab(tab_idx, slot_idx)` |
| Building tab widgets | Per-tab storage | Store to `tab_slot_*[tab_idx][slot_idx]` |
| Tab switch | Just `tkraise()` | `tab_grid_frames[new_idx].tkraise()` |

### Refactoring & Performance Lessons

| Issue | Cause | Fix |
|-------|-------|-----|
| Debug logging causes file handle churn | `with open("debug.log", "a")` called 20+ times in hot audio paths | Use Python `logging` module with `FileHandler` — buffered, single handle, configurable levels |
| Double array copy per playback | `get_sound_data()` returns `.copy()`, then `_apply_fade_out()` also calls `.copy()` | Make `_apply_fade_out` work in-place — caller already has a copy from cache |
| UI blocks on startup with many sounds | `preload_sounds()` runs synchronously on main thread | Run in `threading.Thread(daemon=True)`, update status via `root.after(0, ...)` |
| Editor redraws entire waveform every 50ms | `_update_playhead()` called `_draw_waveform()` which deletes+redraws all canvas items | Only delete+redraw the playhead tag, not the entire waveform |
| Emoji categories computed at import time | `EMOJI_CATEGORIES = _build_emoji_categories()` runs on every import | Use lazy `get_emoji_categories()` function that delegates to `@lru_cache`-decorated builder |
| `bind_all("<MouseWheel>")` steals events | Global binding intercepts scroll events in emoji picker and other dialogs | Use `<Enter>`/`<Leave>` on canvas to `bind_all`/`unbind_all` only when hovering |
| `preserve_pitch` lost on full dialog save | `SoundSlot()` created without `preserve_pitch` kwarg, defaults to `True` | Always pass `existing.preserve_pitch if existing else True` when constructing new `SoundSlot` |
| Duplicate code across modules | PTT button map copy-pasted in `_press_ptt`/`_release_ptt`; pydub init duplicated in `editor.py` | Extract to class constant (`PTT_BUTTON_MAP`); remove dead code from editor (it delegates to `audio.py`) |
| Editor uses low-quality resampling | Nearest-neighbor index-based resampling causes aliasing | Import and use `_resample_audio()` from `audio.py` which has librosa/scipy fallbacks |

### General Rules

1. **Always test after adding new UI elements** - layout issues are common with Tkinter
2. **Never use a new color constant without adding it to COLORS first**
3. **Path handling must support both forward and backslashes on Windows**
4. **Audio file format support varies** - always have pydub fallback ready
5. **Atomic writes for all config/state files** - use `os.replace()` (never `os.remove()`+`os.rename()`)
6. **CTkButton click handling** - ALWAYS use `command=` for click actions on CTkButton. NEVER rely on raw `<Button-1>` or `<ButtonRelease-1>` canvas bindings — `CTkButton._draw()` wipes them on every `btn.configure()`. Use `CTkButton.bind()` only for drag detection (`<B1-Motion>`, `<ButtonRelease-1>`) as CTkButton re-applies these internally. See "Slot Button Click & Drag Architecture" section.
7. **CTkButton widget hierarchy** - CTkButton contains internal canvas/label children; `winfo_containing()` returns these internals, not the CTkButton. Use `_is_widget_inside()` helper for drag-drop target detection only. For click-to-play, trust `command=` — it always fires on the correct widget.
8. **Font creation is expensive** - Pre-create `CTkFont` objects once and reuse them; never create fonts in animation loops or update functions
9. **Use Python `logging` module** - Never use raw `open("debug.log", "a")` writes; configure logging once in `main.py`, use `logger = logging.getLogger(__name__)` in modules
10. **Avoid unnecessary array copies** - When audio data is already a copy (from cache or speed adjustment), apply fade-out and other transforms in-place
11. **Background-thread heavy I/O** - Sound preloading, file hashing, and other blocking I/O should run in daemon threads; update UI via `root.after(0, callback)`
12. **Lazy-load expensive resources** - Emoji categories, large data structures should be built on first access, not at import time
13. **NEVER bind directly to CTkButton internal children** - `canvas.bind()` on a CTkButton's internal canvas WILL be wiped by `_draw()`. This applies to both `add` and non-`add` variants. Use `command=` for clicks, `CTkButton.bind()` for motion/release. Read the "Slot Button Click & Drag Architecture" section before touching any slot event handling code.
14. **Per-tab widget updates** - When updating widgets for a sound/slot that may be on ANY tab (not just current), use per-tab storage directly (`tab_slot_*[tab_idx][slot_idx]`). NEVER use legacy aliases (`slot_buttons`, `slot_frames`) for cross-tab updates. See "Per-Tab Widget Architecture" section.
