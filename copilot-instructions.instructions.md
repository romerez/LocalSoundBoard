# Copilot Instructions - Discord Soundboard Project

> **Last Updated:** 2026-02-19 (major: Slot Button Click & Drag Architecture documented)
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
| Config Storage | JSON (atomic writes via `os.replace`) |
| Image Processing | `pillow` |
| Audio Format Conversion | `pydub` + `imageio-ffmpeg` |
| Pitch-Preserving Speed | `librosa` |
| Emoji Data | `emoji-data-python` |
| Color Utilities | `colour` |

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
customtkinter>=5.2.0
emoji-data-python>=1.6.0
colour>=0.1.5
```

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

### The Solution: Two-Mechanism Split

| Mechanism | Purpose | Why it works |
|-----------|---------|--------------|
| `command=` callback | **Click-to-play** (primary) | Stored as `CTkButton._command` property — immune to `_draw()` |
| `CTkButton.bind()` | **Drag detection** (motion/release) | CTkButton stores bindings internally and re-applies after `_draw()` |

### Event Flow

```
USER CLICKS A SLOT:
  1. <Button-1> fires on internal canvas
  2. CTkButton._clicked() calls self._command → _on_slot_command(slot_idx)
  3. _on_slot_command checks _click_dragging → False → calls _handle_slot_click
  4. _handle_slot_click: debounce check → just-stopped check → _play_slot(slot_idx)
  5. (simultaneously) _on_slot_press records position for potential drag

USER DRAGS A SLOT:
  1. <Button-1>: command= fires → _on_slot_command → _click_dragging is False → play fires
     (this is acceptable: drag operations are rare, and the play is instant)
  2. <B1-Motion>: _on_slot_motion detects movement > 5px → sets _click_dragging = True
  3. <ButtonRelease-1>: _on_slot_release sees was_dragging → handles drop
```

### State Machine

```
State fields (reset atomically via _reset_click_state()):
  _click_active   : bool          — a press is being tracked
  _click_slot     : Optional[int] — slot index that was pressed
  _click_tab      : Optional[int] — tab that was active at press time
  _click_start_x/y: int           — screen position of press
  _click_dragging : bool          — drag threshold exceeded

Persistent (NOT reset per-click):
  _last_play_time      : float         — debounce rapid clicks (< 150ms)
  _just_stopped_slot   : Optional[int] — slot just stopped (prevent re-play < 200ms)
  _just_stopped_at     : float         — when it was stopped
```

### What NOT To Do

| Approach | Why it fails |
|----------|-------------|
| `canvas.bind(event, handler)` on CTkButton's internal canvas | **Wiped by `_draw()`** on every `btn.configure()` call |
| `canvas.bind(event, handler, add="+")` | Same — `_draw()` replaces ALL `<Button-1>` bindings |
| `_rebind_slot_internals()` (deferred re-binding after configure) | Creates race conditions; replaces CTkButton's own bindings without `add`; causes wrong-slot-index bugs |
| Using `winfo_containing()` to verify click target | Returns stale/mismatched widgets after CTkButton redraws; caused "first click ignored" bugs |
| Using `<ButtonRelease-1>` for click-to-play | Unreliable — CTkButton's `_draw()` can wipe the release binding between press and release |

### Stop Button

- `command=` set **once** at creation time in `_create_slot_widgets` (via `make_stop_handler`)
- `_show_stop_button()` just packs/unpacks — **no re-binding**
- `_stop_slot_with_flag()` records `_just_stopped_slot` + timestamp; `_handle_slot_click` checks this to prevent re-play within 200ms

### Tab Integration

- `_switch_tab()` calls `_reset_click_state()` to cancel any in-progress interaction
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
- [x] Drag-and-drop slot reordering (within same tab)
- [x] Drag-and-drop to move sounds between tabs
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
|| Emoji picker freezes UI | Creating thousands of `CTkButton` widgets synchronously blocks the main thread | Use `after()` to load categories one at a time asynchronously; limit emojis per category (96 max) |
|| Emojis display as colorless/black | `CTkButton` doesn't render colored emojis properly on Windows | Use native `tk.Label` with "Segoe UI Emoji" font instead of `CTkButton`; add hover/click bindings manually |

### CustomTkinter-Specific Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Sounds don't play when clicking slot | CTkButton's `_draw()` method re-binds `<Button-1>` on its internal canvas WITHOUT `add`, wiping any custom canvas bindings. `_draw()` runs on EVERY `btn.configure()` call. | **CURRENT FIX:** Use `command=` for click-to-play (stored as `_command` property, immune to `_draw()`). Use `CTkButton.bind()` for drag detection (CTkButton stores and re-applies these). NEVER bind directly to internal canvas children. See "Slot Button Click & Drag Architecture" section. |
| Tab switching extremely slow | Creating new `CTkFont()` objects on every button update is expensive | Pre-create cached font objects (`_font_sm`, `_font_xl_bold`, etc.) once in `__init__` and reuse everywhere |
| UI lag after tab switch | Image reloading from disk on every `_update_slot_button` call | Track `slot_image_paths` dict to cache already-loaded images; only reload if path changed |
| CTkButton `command=` is the ONLY reliable click handler | `command=` is stored as `_command` property and survives `_draw()` resets. Raw bindings on the internal canvas are wiped. | **Use `command=` for ALL click-to-play and stop-button actions.** Do NOT add redundant `ButtonRelease-1` bindings — they will be wiped and cause confusion. |
| CTkProgressBar has no delete() | Tkinter Canvas `.delete("tag")` doesn't exist on CTkProgressBar | Use `.set(0)` to reset CTkProgressBar instead of `.delete("progress")` |
| Animation loop causes lag | 60fps (16ms interval) animation loop with `.configure()` calls every frame | Reduce to 20fps (50ms); set progress bar color once when playback starts, not every frame |

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