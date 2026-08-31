# Final Design Spec — "Air Deck" (reimagined DJ Looper)

Verified against the working tree on 2026-08-31 (gui.py 17,086 lines, audio.py 4,013, constants.py 572, models.py 243, person_board.py 4,089, slot_widget.py 1,388). All line references below were read, not guessed. Note: **local-preview speed/pitch parity is ALREADY FIXED in the working tree** (`_render_preview_audio` gui.py:11634, `_start_preview_render` gui.py:11664, used by both `_preview_slot_by_tab` gui.py:11572 and `_preview_person_sound` gui.py:13511) — the spec keeps it and adds nothing there except a shared engine helper.

---

## 1. Concept & name

**Air Deck** replaces the DJ Looper side panel. Short meme sounds (1–3 s, ~99% of presses) get **no UI at all** beyond the slot tile's existing orange progress bar and stop overlay — no card is ever built for them. When a **long sound (≥ 8 s) or a loop** starts, a small **always-on-top floating window** (the Air Deck) appears on top of whatever you're doing — without stealing focus from your game — showing ONE hero sound (its slot name, emoji and colour, a scrubbable waveform, and rate-aware time remaining) plus small chips for anything else playing, an **ON AIR LED** that tells you whether Discord is actually hearing the sound (and whether your own mic is muted by auto-PTT), and four fat buttons you can hit blind: **STOP / PAUSE / AGAIN / FADE**. A set of configurable **ctrl+alt global hotkeys** drives the same four verbs (plus seek, volume, A-B loop, bed-duck) while the game has keyboard focus. The engine learns to fade instead of hard-cut, to seek, to loop a region, to duck a music bed under your voice, and to **re-press the Discord PTT key on resume** — so every control is truthful in the call. The main window never resizes: the on-air state is mirrored as a compact segment inside the **existing status bar**, not a new row.

---

## 2. What the user sees & does

### 2.1 Main-window surface — On-Air segment in the existing status bar

No new row. `_create_status_bar` (gui.py:8367) gains one canvas-drawn segment (`OnAirSegment`, a `tk.Canvas` of height `UI["compact_height"]`=24 logical px) packed in the LEFT cluster after the device labels:

```
│ v1.9 │ 🎤 HyperX → 🔊 CABLE Input │ ● ON AIR  😂 שם הסאונד  -1:07  [⏬] [🎧] │ F9 │ Ready │
```

- **LED**: red = mixer is auto-holding the PTT key (`ptt_active and not ptt_user_physical`), green = user's own voice is live (`ptt_user_physical or universal_ptt_open or manual_ptt_hold`), grey = idle. Tooltip (existing `_Tooltip`, 3 s): "Red = the app is holding your PTT key (F9). While red, your own mic is muted (duck-mic). Green = your voice is going out."
- Focused sound's emoji + name (logical Hebrew string, pixel-ellipsised via `font.measure`) + rate-aware remaining time.
- `[⏬]` = fade-all (250 ms); `[🎧]` = show/hide the Air Deck window (replaces the action-bar "🎧 DJ" button behaviour; the action-bar button at gui.py:4970 is relabelled "🎧 Deck" and calls the same toggle — its colour is driven by `AirDeckWindow.on_visibility(cb)`, never set independently, so it cannot desync).
- Idle state: segment shows only the grey LED — no text, no reserved space beyond ~20 px.
- Clicking the segment body when something plays = show the Deck.

### 2.2 Floating surface — AirDeckWindow

- **Base implementation (v1, mandatory)**: a `ctk.CTkToplevel` following the proven `PersonPopout` pattern (person_board.py:3082): `attributes("-topmost", True)` with an "On top" pin, geometry persisted (see §6) via the same store `PersonContext.load_geometry/save_geometry` use (person_board.py:745/754) under key `"deck"`, created once at startup **withdrawn**, `withdraw()` on close, reopen short-circuits on `winfo_viewable()`, reveal via `_redraw_window` (person_board.py:119) + `arm_toplevel_resize_defer` (from `soundboard._shared`). Geometry clamped to the virtual desktop on restore (memory: 150% multi-monitor).
- **No-activate enhancement (flag-guarded, `deck_noactivate=True`)**: before `deiconify()`, apply `GWL_EXSTYLE |= WS_EX_NOACTIVATE` and show via ctypes `ShowWindow(hwnd, SW_SHOWNOACTIVATE)` + `SetWindowPos(HWND_TOPMOST, SWP_NOACTIVATE|SWP_NOMOVE|SWP_NOSIZE)`. **This is prototyped on day 1** (see §9 R1); if it misbehaves at 150% DPI, the flag ships False and the base path (which briefly activates) is the behaviour.
- Body is **one `tk.Canvas`** (SlotWidget technique — canvas items created once, updated with `coords`/`itemconfigure`; zero CTk children below the titlebar row). All drawing at device px (logical × CTk scaling factor); rasterised emoji via `emoji_render` helpers at device px.
- Default size 340×150 logical; double-click header toggles **compact** mode (340×40, hero line + STOP/PAUSE/FADE only), persisted.
- **Auto-show**: on the mixer `started` event for a sound that qualifies (duration ≥ `deck_long_threshold_s`=8, or `loop=True`) when `deck_auto_show="long_or_loop"` (default). **Auto-hide**: 4 s after the last qualifying sound ends, only if it was auto-shown, never while pinned, never while the pointer is over the deck, and never within 2 s of another qualifying sound starting.
- Refresh: 10 Hz while visible and something plays; 0 Hz hidden.

### 2.3 Slot-level behaviour

- Tile progress is now **mixer-position-driven**: `_animate_progress` (gui.py:7429) takes ONE `get_playing_sounds()` snapshot per tick and sets each playing tile's bar from `snapshot["progress"]` (position-based, truthful under pause/seek/rate). `playing_slots` (currently `{slot_idx: {start_time, duration, tab_idx, loop}}`, written at gui.py:14638) becomes `{slot_idx: {tab_idx, sound_id, loop, armed_at}}`; the wall-clock timer survives ONLY as a 1.5 s grace (`armed_at`) for the librosa background-stretch window (play_sound's threaded path, audio.py:2874-2887) — a slot whose `sound_id` has not yet appeared in the snapshot keeps its orange paint until `started` arrives or the grace expires.
- Tile ⏹ overlay on a sound ≥ 8 s = **60 ms fade-stop**; Shift+⏹ = hard cut. Short sounds: unchanged (hard-ish 20 ms fade, inaudible).
- The slot **right-click menu** (`_show_slot_menu` gui.py:7104) gets a deck block **prepended only while that slot's `sound_id` is in the mixer** (tk.Menu — costs nothing until opened): `⏸ Pause / ▶ Resume`, `|◀ Again (to cue)`, `⏬ Fade out`, `■ Stop now`, `Loop ▸ Off / ∞ / 2 / 5 / 10 / Finish this pass`, `Speed ▸ 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0` (sets **effective** rate; honest for speed-baked slots), `Bed mode ✓`, `Load to deck (focus)`.
- **Retrigger policy** (pressing a tile/hotkey while that `sound_id` is playing): per-slot `retrigger` field, default `"auto"` = `"layer"` for sounds < 8 s (spam is a feature, capped at `max_instances=3` — oldest instance recycled with a 20 ms fade), `"restart"` for ≥ 8 s (seek to cue, no stacking — the forgiving default per judge 3; `"toggle"` = press-again-stops available per slot). The status bar shows "Restarted: <name>" / "(stopped)" for 1 s so the change is legible.
- Drag-to-stage is **deleted**. Drag to Favorites/People stays (the `_on_slot_drag_drop` panel branch at gui.py:10006+ is removed, the rest kept).

### 2.4 Hotkeys (global, `keyboard` lib, all configurable)

Registered by `DeckHotkeys` (new module) using the same diff-map pattern as `_register_hotkeys` (gui.py:14555); every handler does `self.root.after(0, ...)` only. Stored in config `deck_hotkeys` (action → combo string, `""` = unbound). Validation at save time rejects any combo equal to or containing `ptt_key`, the Universal PTT key, or any slot hotkey. Defaults deliberately avoid `ctrl+alt+tab` (Windows persistent task switcher), `ctrl+alt+arrows` (Intel iGPU rotate), F-keys, digits and WASD:

| Action | Default | Effect |
|---|---|---|
| `stop_all` | `ctrl+alt+end` | Fade ALL 250 ms + halt Queue scheduler + previews (panic key) |
| `pause_resume` | `ctrl+alt+space` | Pause (30 ms fade → PTT releases ~300 ms later so you can talk) / Resume (re-presses PTT) on FOCUSED |
| `fade_stop` | `ctrl+alt+backspace` | Fade FOCUSED 800 ms; second press within 400 ms = hard cut |
| `again` | `ctrl+alt+home` | Seek FOCUSED to its cue (0:00 if none) |
| `seek_back` / `seek_fwd` | `ctrl+alt+,` / `ctrl+alt+.` | −5 s / +5 s (prev/next cue when the slot has cues) |
| `vol_down` / `vol_up` | `ctrl+alt+-` / `ctrl+alt+=` | Focused volume ∓10% (live, not persisted) |
| `loop_a` / `loop_b` | `ctrl+alt+[` / `ctrl+alt+]` | Set A / set B & engage region; `loop_b` again = release |
| `loop_clear` | `ctrl+alt+'` | Clear A-B (restore prior loop state) |
| `loop_finish` | `ctrl+alt+;` | Finish this pass then stop (loops) |
| `bed` | `ctrl+alt+b` | Toggle BED (duck to 35% under voice/other sounds) on FOCUSED |
| `cycle_focus` | `ctrl+alt+pageup` / `ctrl+alt+pagedown` | Cycle deck focus across playing sounds |
| `toggle_deck` | `ctrl+alt+d` | Show/hide the Air Deck window |

**Focus rule** (resolves judge concern): focused = **most recently started** qualifying sound; clicking a chip sets sticky manual focus until that sound ends; focus is **frozen for 1 s after any deck hotkey/button press** so a new sound can't hijack an in-flight STOP; every focus change flashes the hero row and the hotkey feedback names its target in the status bar ("Paused: <name>").

In-app (deck window has focus, no Entry focused): Space = pause/resume, Esc = fade all, Home = again, `[`/`]` = A/B.

### 2.5 Short-sound flow (1–3 s meme)

Tile click / slot hotkey → `SoundboardApp._start_sound(origin, tab_idx, slot_idx, slot)` (ONE new helper replacing the three copy-pasted `play_sound` call sites: `_play_slot` gui.py:11498, `_play_slot_from_tab` gui.py:14613, `_play_person_sound` gui.py:13473) → `mixer.handle_retrigger(sound_id, policy)` returns `None` (nothing playing / layer) → `mixer.play_sound(..., label=slot.name, meta={emoji,color,origin,tab,slot}, fade_in_ms=3, max_instances=3)`. Tile turns orange, PTT pressed — unchanged. If a BED sound is playing, the engine ducks it to 35% with a 30 ms attack, restoring over 400 ms after the meme ends. **Nothing else happens**: no card, no deck change (below threshold, not looping), the deck header's one-line "⚡ <name> 0.8s" last-shot ticker updates if the deck happens to be visible. On the `ended` event the tile resets. Tk-thread cost per press: ~0 (vs 230–430 ms card build today).

### 2.6 Long-sound flow (1:30 bit)

Press → `handle_retrigger` (`restart` default: if already playing, seek to cue + status message, done) → else `play_sound(...)` → `started` event within one tick → deck shows (no focus steal), hero row paints pill/emoji/name, waveform polygon fills in when `get_sound_peaks` returns (worker thread, memoised), LED red, rate-aware countdown runs. During play: **talk** = physically hold F9 → LED green, BED sounds dip to 35%; **PAUSE** = 30 ms fade → `paused=True` → PTT releases ~300 ms later (intended: you paused to talk); **RESUME** = `resume_sound` re-presses PTT *before* unpausing, callback safety net backs it up; **scrub** = click/drag the waveform (60 ms throttle, 10 ms splice fades, works while paused); **AGAIN** = seek to cue (right-click AGAIN = "set cue here", persisted to `slot.cues`); **A-B** = trap the good bar (seam crossfade at rate 1.0); **STOP** = 60 ms fade; **FADE** = 800 ms musical fade — PTT is held through every fade tail (fading entries count as active). Speed menu sets `live_rate = wanted/base_speed` so a 1.5×-baked slot reading "1.5×" really plays 1.5× (fixes the compounding bug at audio.py:3195-3200); pitch mode starts from `slot.preserve_pitch` — the panel's forced `set_pitch_preserve_live(id, True)` is gone. End → `ended` event → tile reset, People chip cleared via registry, deck auto-hides after 4 s.

### 2.7 Looping flow

Loops always qualify for the deck and auto-show it (runaway-loop safety, kept from gui.py:14655's instinct — but now on a topmost window that cannot resize the board). Hero badge shows **"pass 2/5"** or **∞** from new snapshot fields `loop_pass`/`loop_total` (fixes the rotting `loops_remaining == cnt` highlight). One loop model everywhere: **total passes** — badge click / `ctrl+alt+;` = "finish this pass" (`set_sound_loop_count(id, 0)`, audio.py:3331, finally exposed); right-click Loop radio = Off/∞/2/5/10 total. Loop wrap gets a 10 ms seam fade (sample crossfade on the rate==1.0 path, gain dip-out/in on WSOLA/resample paths); the baked `_apply_fade_out` (audio.py:1609, applied at 2926/2984) is **removed** — the runtime tail envelope replaces it, so toggling loop on mid-play no longer dips at every seam. `loop_delay` keeps the `in_delay` state machine (audio.py:2614-2632); deck shows a yellow "delay 1.4 s" countdown. A-B regions: engage live, wrap A↔B, seek outside the region clears it (CDJ loop exit), releasing continues to the natural end.

### 2.8 Hebrew/RTL & 150% DPI notes

- All names reach `create_text` as **logical** strings (canvas text is BiDi-aware; `_fix_rtl_text` gui.py stays a no-op). Ellipsis by `font.measure` on progressively shorter **logical prefixes**, ellipsis appended once — never slice inside a combining sequence, never pre-reorder.
- Every canvas coordinate = logical × `self._apply_widget_scaling`-equivalent factor read once per redraw; emoji rasterised at device px via `emoji_render`; min hit target 28×28 logical (42×42 device); the four transport buttons are 66×36 logical (99×54 device).
- Deck reveal reuses the People smudge fixes verbatim: `reopen()` short-circuits on `winfo_viewable` (deiconify on a mapped window gives no `<Map>`), `_redraw_window(update_now=True)` with the nested `after_idle` expose flush, `after()` timers armed on `root`, never `root.update()`.

---

## 3. ASCII mockups (logical px, app tokens)

**AirDeckWindow, full mode — 340×150 logical (510×225 device @150%)**
```
┌ 🎧 Air Deck ──────────────────────────── [📌] [–] ┐  24  header (compact_height)
│ ● ON AIR   ⚡ 😂 חחח בוא הנה 0.8s        pass 2/5 │  20  LED + last-shot ticker + loop badge
│ ▌😂 (שיחת טלפון עם המשטרה) רמיקס   ×1.00  -0:49  │  20  hero: pill_width=4 colour pill, emoji,
│ 0:42 ▁▂▄▆█▇▅▃▂▁▂▃▅▇█▆▄▂▁▁▂▄▆█▇▅▃▂▁▂▃▅ 1:31      │  26  name, eff. rate, remaining
│        A═════════^═════════B   ▲cue                │       waveform polygon + playhead + A/B + cues
│ [ chip: ⏸ ביט לופ -0:12 ✕ ][ chip: 🎵 wow ✕ ][+1] │  24  chips (max 3 + overflow), h=24
│ [  STOP  ] [ PAUSE ] [ AGAIN ] [  FADE  ]          │  36  4 buttons 66×36, gaps 8
└────────────────────────────────────────────────────┘
STOP red · PAUSE grey/orange (▶ green when paused) · AGAIN blurple · FADE yellow
Waveform: click=seek, drag=scrub, shift+click=set A, ctrl+click=set B, right-click=cue menu
Chips: click=focus, middle=restart, wheel=vol ±5%, ✕=60ms fade-stop, right-click=deck menu
```

**Compact mode — 340×40**
```
┌ ● -1:07  😂 (שיחת טלפון עם המשטרה)…  [STOP][⏸][⏬] ┐
└────────────────────────────────────────────────────┘
```

**Status-bar On-Air segment (inside existing 32-px status bar, gui.py:8367)**
```
│ v1.9 │ 🎤 HyperX… → 🔊 CABLE… │ ● ON AIR 😂 שם -1:07 [⏬][🎧] │ 🌐 F9 │ Ready │
                                  └── OnAirSegment canvas, h=24 (compact_height) ──┘
```
Colours from `COLORS` only: playing=orange, in-delay=yellow, paused=text_muted, fading=blurple pulse, red/green/blurple as above. Fonts from `FONTS` tokens; corner radius `UI["button_corner_radius"]`=6 for canvas buttons.

---

## 4. Audio engine additions (soundboard/audio.py, class AudioMixer)

### 4.1 Entry fields (added in `_play_sound_sync`, audio.py:2896, both dict literals — 2930 and 2995)

```python
"label": label or Path(file_path).stem,   # also mirrored into "name"
"meta": meta or {},                       # {emoji, color, origin, tab, slot} echoed verbatim
"instance": next(self._instance_counter), # itertools.count()
"base_speed": speed,                      # the pre-baked factor
# playback_rate (existing) is now documented as live_rate; "speed" is never overwritten again
"pitch_preserve_live": preserve_pitch,    # seed from slot; never forced by UI
"gain": 0.0 if fade_in_ms else 1.0, "gain_target": 1.0,
"gain_step": ...,                         # per-sample delta
"after_fade": None,                       # None | "stop" | "pause"
"bed": bed, "duck": 1.0, "duck_target": 1.0,
"loop_start": int(loop_start_s*sr), "loop_end": int(loop_end_s*sr) if loop_end_s else None,
"loop_prev": None,                        # (loop, loops_remaining) saved by set_loop_region
"loop_total": loop_count, "loop_pass": 1,
"cue": int(cue_s*sr),
"pending_seek": None,                     # sample index, applied next block
"started": False,                         # for the 'started' event
```

### 4.2 Public API (exact signatures)

```python
def play_sound(self, file_path, volume=1.0, speed=1.0, preserve_pitch=True,
               sound_id=None, loop=False, loop_count=0, loop_delay=0.0, *,
               label: Optional[str] = None, meta: Optional[dict] = None,
               fade_in_ms: int = 3, cue_s: float = 0.0,
               loop_start_s: float = 0.0, loop_end_s: Optional[float] = None,
               bed: bool = False, max_instances: int = 0) -> float:
    """As today, plus deck metadata. max_instances>0: if that many entries with
    this sound_id already exist (counting a _pending_ids set that covers the
    librosa background-stretch window), the OLDEST gets fade_out_sound(20ms)
    before the new one queues. Return value unchanged (duration estimate).
    The baked _apply_fade_out call is REMOVED (runtime tail envelope replaces it)."""

def handle_retrigger(self, sound_id: str, policy: str) -> Optional[str]:
    """Pre-check for a re-press of a playing sound_id. Under self.lock (plus
    _pending_ids). policy 'layer' or no live entry -> None (caller proceeds to
    play_sound). 'restart' -> seek newest entry to its cue, re-press PTT,
    return 'restarted'. 'toggle' -> fade_out_sound(id, 250), return 'stopped'.
    No float sentinels (explicitly avoids the -1.0 return judges flagged)."""

def stop_sound(self, sound_id: str, fade_ms: int = 0) -> None:
    """fade_ms==0 keeps TODAY'S exact semantics (hard removal, queue drain,
    _force_release_ptt when empty — audio.py:3064) so the Queue scheduler,
    AFK, shutdown and existing tests are untouched. fade_ms>0 delegates to
    fade_out_sound (PTT then releases via the normal countdown ~300ms after
    the tail). GUI callers opt IN to fades explicitly."""

def stop_all_sounds(self, fade_ms: int = 0) -> None:
    """fade_ms==0 = today's behaviour exactly (audio.py:3107). fade_ms>0:
    fade every entry, drain the queue, let the countdown release PTT."""

def fade_out_sound(self, sound_id: str, ms: int = 800, then: str = "stop") -> None:
    """gain_target=0, gain_step=1/(ms*sr/1000), after_fade=then. Fading entries
    count as ACTIVE for PTT (key held through the tail)."""

def pause_sound(self, sound_id: str, fade_ms: int = 30) -> None:
    """fade_out_sound(id, fade_ms, then='pause'); callback sets paused=True at
    gain 0. Replaces the instant flag flip (audio.py:3122)."""

def resume_sound(self, sound_id: str, fade_ms: int = 30) -> None:
    """paused=False, gain=0, gain_target=1 over fade_ms, after_fade=None,
    self._ptt_release_countdown = 0, self._press_ptt().  <-- THE BUG FIX."""

def restart_sound(self, sound_id: str) -> None:
    """pending_seek=cue, clears in_delay/WSOLA, 10ms fade-in, _press_ptt()."""

def seek_sound(self, sound_id: str, seconds: float, *, relative: bool = False) -> bool:
    """Sets pending_seek = clamp(target_sample, 0, end-1) under self.lock; the
    callback applies it at the next block boundary with a splice fade (4.3).
    Works while paused (position moves, stays paused). If an A-B region is set
    and the target falls outside it, clears the region (restoring loop_prev)."""

def set_cue(self, sound_id: str, seconds: float) -> None
def set_loop_region(self, sound_id: str, start_s: float, end_s: float) -> None:
    """Validates end-start >= 0.05s. On a non-loop entry saves loop_prev and
    sets loop=True, loops_remaining=-1."""
def clear_loop_region(self, sound_id: str) -> None   # restores loop_prev
def set_loop_total(self, sound_id: str, total: int) -> None:
    """loop_total=total; loops_remaining = -1 if total==0 else max(0, total - loop_pass)."""
def set_sound_bed(self, sound_id: str, enabled: bool) -> None
def set_effective_rate(self, sound_id: str, rate: float,
                       preserve_pitch: Optional[bool] = None) -> None:
    """playback_rate = clamp(rate / base_speed, 0.5, 2.0). set_playback_rate
    (audio.py:3183) keeps working but NO LONGER writes 'speed' (lines 3196-3200
    deleted). preserve_pitch not None -> set_pitch_preserve_live + WSOLA reset."""

def get_sound_peaks(self, sound_id: str, buckets: int = 160) -> Optional[np.ndarray]:
    """Grabs the entry's data ref under self.lock, computes mono abs-max per
    bucket OUTSIDE the lock, memoised by (id(data), buckets). Deck calls it
    from a worker thread; ~15ms for 90s of audio."""

def pop_events(self) -> List[Tuple[str, str, dict]]:
    """Drains self._events (collections.deque(maxlen=256), appended by the
    callback with no lock): ('started', id, {instance}), ('ended', id,
    {instance, reason: 'finished'|'stopped'|'faded'}), ('loop', id,
    {loop_pass}), ('paused', id, {}). GUI drains once per _animate_progress
    tick, including the 250ms idle path. Nothing ever calls back on the
    audio thread. Burst-safe: O(1) dict work per event in the drain handler,
    and 'ended' tile resets go through _reset_slot_visual (gui.py:7614),
    NEVER _update_slot_button_for_tab (the image-reloading path gui.py:14396)."""

def on_air_state(self) -> Tuple[str, bool]:
    """('app'|'user'|'off', mic_ducked) computed from ptt_active,
    ptt_user_physical, universal_ptt_open, manual_ptt_hold,
    duck_mic_during_sounds — for the LED."""
```

### 4.3 `_output_callback` changes (audio.py:2526, per block, per non-paused entry, in order)

Inside the existing `with self.lock` loop:

1. **Pending seek**: if `pending_seek is not None` — this block first renders 5 ms of the *old* position multiplied by a 1→0 ramp (or simply sets `gain=0` with a 10 ms fade-in step for simplicity — implementer's choice, test only asserts no discontinuity > threshold), then `position = pending_seek; pending_seek = None`, pop `wsola_buf`, zero `wsola_read/write`, clear `in_delay`.
2. **End bound**: `end = loop_end or data_len`; all three render paths (int fast path line ~2668, WSOLA `_wsola_render` guard at 3270 `i0 + WSOLA_FRAME >= data_len` → `>= end`, resample `max_out`) read against `end` instead of `data_len`.
3. **Wrap** (`remaining <= 0` branch, ~2643): wrap target becomes `loop_start` (not 0); `loop_pass += 1`; `loops_remaining` decrement unchanged; append `('loop', id, {...})`. **Seam**: on the rate==1.0 path, if the chunk ends exactly at `end`, blend its last `min(480, chunk)` samples with `data[loop_start : loop_start+480]` via linear ramps and set `position = loop_start + blended` (true crossfade); on WSOLA/resample paths, arm `gain=0` with a 5 ms fade-in step (documented approximation). `loop_delay` path unchanged except it resumes at `loop_start`.
4. **Natural tail** (replaces baked `_apply_fade_out`): if not looping and `remaining <= tail_samples` (30 ms) and `gain_target == 1.0` → `gain_target = 0.0`, step = 1/tail_samples, `after_fade='stop'`.
5. **Gain envelope**: only when `gain != gain_target`: `n = chunk_size`; ramp = `np.linspace(gain, gain ± min(|Δ|, gain_step*n), n, dtype=np.float32)`; `chunk *= ramp[:, None]` (mono: `ramp`); `gain = ramp[-1]`; at `gain == 0.0` with target 0: `after_fade 'stop'` → `finished.append(i)` + event `('ended', id, {'reason':'faded'})`; `'pause'` → `paused=True, gain=0, gain_target=1, after_fade=None` + event.
6. **Duck envelope** (bed): compute once per block before the loop: `talking = ptt_user_physical or (universal_ptt_enabled and universal_ptt_open) or manual_ptt_hold`; `foreground = self._last_nonbed_active` (set at end of previous block — one block latency, fine). For `bed` entries: `duck_target = self.bed_level if (talking or foreground) else 1.0`; ramp `duck → duck_target` at attack 30 ms / release 400 ms; `chunk *= duck_ramp`.
7. **Fast-path guard (mandatory, tested)**: when `gain == gain_target == 1.0`, `duck == duck_target == 1.0`, `pending_seek is None`, `loop_end is None`, `rate == 1.0` — the per-entry code path performs **zero new numpy operations** vs today (the 99% meme case pays nothing).
8. **After the loop**: `self._last_nonbed_active = any(...)`. **PTT re-press safety net**: `if active_sounds > 0 and self.ptt_key and not self.ptt_active and not self.manual_ptt_hold and self._ptt_repress_cooldown <= 0: self._press_ptt(); self._ptt_repress_cooldown = self._ptt_release_delay` (decrement each block; the cooldown prevents press/release ping-pong with the queue-driven worker — resolves judge 2's race concern). Append `('started', id, ...)` for entries mixing their first block; `('ended', ..., 'finished')` for naturally finished ones.

**Thread-safety**: every public mutator acquires `self.lock` exactly as the existing ones do (audio.py:3064-3360 pattern); `_events` is an unlocked deque (append from callback, swap-drain from Tk thread — deque append/popleft are atomic); peaks computed outside the lock on a data ref; `get_playing_sounds` stays **locked** and is called **once per GUI tick** (the per-block lock-free snapshot graft is *rejected* — it adds allocation to the callback to solve contention that a single 50 ms locked read does not have).

**Interactions**: `pause` still releases PTT ~300 ms after the fade (audio.py:2749-2754 countdown counts only non-paused entries — unchanged and now intended); fading entries are NOT paused so they hold PTT; the safety timeout (audio.py:2764+) still only counts while nothing plays; WSOLA state (`wsola_buf/read/write`) is popped on every seek/wrap/mode change exactly as `set_pitch_preserve_live` does (audio.py:3218-3222); A-B points on the WSOLA path snap within one 2048-sample grain (documented).

### 4.4 `get_playing_sounds()` new fields (audio.py:3450)

Added to each dict (existing keys untouched so nothing else breaks): `label, meta, instance, base_speed, live_rate` (=playback_rate), `effective_rate` (=base_speed×live_rate), `pitch_preserve_live, gain, duck, fading` (gain_target<gain), `bed, loop_total, loop_pass, loop_start_s, loop_end_s, cue_s, remaining_wall_seconds` (=(end−pos)/(sr×live_rate), + delay remainder when `in_delay`), `started`.

### 4.5 Preview parity

Already shipped: `_render_preview_audio` (gui.py:11634) applies `apply_speed` + master volume + soft-clip, used by both preview paths via `_start_preview_render` (gui.py:11664). **No new work** except: extract the transform into `audio.render_preview_buffer(data, speed, preserve_pitch, volume, master, sr)` so gui.py and any future deck "audition" call one function, and add a regression test that a `speed=1.5, preserve_pitch=False` slot previews shorter than realtime.

---

## 5. GUI architecture

### 5.1 New modules

**`soundboard/deck_model.py`** (no Tk imports — pure logic, unit-testable headless):
- `OnAirEntry` (dataclass): `sound_id, slot, origin('board'|'person'), tab_idx, slot_idx, label, emoji, color, started_wall`.
- `OnAirRegistry`: `register(entry)`, `pop(sound_id) -> Optional[OnAirEntry]`, `get(sound_id)`, `by_origin(...)`. Populated by `_start_sound`; the single source that maps mixer ids back to slots — **fixes the stuck People chip** (`person_` ids currently swallowed at gui.py:9990) because `ended/stopped` events route through it to `PersonContext.playing_ids.discard(id(slot))` (person_board.py:541/677).
- `DeckModel.compute(snapshot, registry, now) -> DeckView`: focus rule (§2.4), hero fields, ≤3 chips + overflow count, LED state via `mixer.on_air_state()`, qualifying test (`total ≥ threshold or loop`), auto-show/auto-hide decisions.
- `DeckActions(mixer, registry, app_callbacks)`: `stop_focused, stop_all, pause_resume, again, fade, seek, volume, loop_finish, loop_ab, bed, cycle_focus` — thin wrappers shared by hotkeys, deck buttons, chips, tile menu and the status-bar segment.

**`soundboard/deck_ui.py`**:
- `DeckPainter(canvas, mode: 'full'|'compact'|'segment', scaling)`: builds canvas items once; `update(view: DeckView)` moves/recolours with change detection; rectangle hit-testing à la `SlotWidget._hit_test` (slot_widget.py:1102); `font.measure` ellipsis; waveform polygon from peaks.
- `AirDeckWindow(ctk.CTkToplevel)`: PersonPopout pattern (§2.2); `toggle()`, `reopen()`, `on_visibility(cb)`, `set_pinned(b)`; geometry via injected `load_geometry/save_geometry` callables (same store as People, key `"deck"`).
- `OnAirSegment(tk.Canvas)`: the status-bar segment; same `DeckPainter` in `segment` mode.

**`soundboard/deck_hotkeys.py`**:
- `DECK_ACTIONS`: table of `(action_id, label, default_combo)` (§2.4).
- `DeckHotkeys(actions: DeckActions, root)`: `load(config)`, `register()`/`unregister()` diff-based clone of `_register_hotkeys`; `validate(combo, ptt_key, universal_key, slot_hotkeys) -> Optional[str]` (error text); settings UI section built with `_scaffold_dialog` (gui.py:15584) reusing the `_record_hover_preview_key` capture pattern (gui.py:10868).

### 5.2 SoundboardApp wiring

- Construction: after the mixer, `self.deck_registry = OnAirRegistry()`, `self.deck_model = DeckModel(config)`, `self.deck_actions = DeckActions(...)`, `self.on_air_segment` in `_create_status_bar`, `self.deck_window` created **withdrawn at startup** (cheap: one toplevel + one canvas), `self.deck_hotkeys.register()` alongside `_register_hotkeys`.
- `_start_sound(origin, tab_idx, slot_idx, slot)`: computes `sound_id` (`f"{tab}_{slot}"` / `f"person_{id(slot)}"`), resolves retrigger policy (`slot.retrigger`, `'auto'` via `sound_cache.get_sound_duration`), calls `mixer.handle_retrigger` then `mixer.play_sound(..., label, meta, ...)`, registers with `OnAirRegistry`, arms `playing_slots` (new shape §2.3). Replaces the bodies of the three call sites.
- `_animate_progress` rewrite of the active path (gui.py:7429): keep resize backoff, `_ptt_watchdog_check`, voice meter, 250 ms idle path, 1% quantisation; then per tick — `events = mixer.pop_events()` (also drained on the idle path), ONE `snapshot = mixer.get_playing_sounds()`, tile progress from snapshot by `sound_id` (grace 1.5 s via `armed_at`), event dispatch (tile reset via `_reset_slot_visual`, People chip clear, deck auto-show/hide), `view = deck_model.compute(...)` pushed to `OnAirSegment` (on change) and `AirDeckWindow` (when visible, 10 Hz cap). The double-snapshot (7529 + panel path) and `_last_panel_update` go away.
- Stop paths: tile ⏹ / chip ✕ / hotkeys → `deck_actions`; action-bar Stop All keeps `_stop_all_sounds` (gui.py:10634) but calls `mixer.stop_all_sounds(fade_ms=250)`; the scheduler/AFK/shutdown paths keep `fade_ms=0`.

### 5.3 Deleted

- `class NowPlayingPanel` (gui.py:792–~2295) entirely: cards, staged cards, per-card sliders, loop presets, Pause All header, items canvas/scrollbar.
- `_toggle_now_playing_panel` (9949) incl. its `root.after(50, _finalize_window_size)` (the function itself stays — startup only), `_on_panel_stop_sound` (9966), the panel branch + auto-show branch of `_on_slot_drag_drop` (10006+), staged callbacks, the panel auto-show closures in `_play_slot` and `_play_slot_from_tab` (14655-14663), the panel blocks in `_animate_progress` (7456-7473).
- `set_playback_rate`'s `sound["speed"] = rate` mirror (audio.py:3196-3200); the baked `_apply_fade_out` calls at play time (audio.py:2926, 2984; the function stays for the editor if referenced).
- `constants.py`: `now_playing_width`, `now_playing_item_height` (520-521). Added: `UI["deck_window_size"]=(340,150)`, `UI["deck_compact_size"]=(340,40)`, `UI["deck_button"]=(66,36)`, `UI["deck_chip_height"]=24`, `UI["deck_wave_height"]=26`.
- Config: `now_playing_visible` — load (16213/16291) ignores it; save (15899) drops it (one-way migration).
- Docs: the phantom `now_playing_side` / left-right positioning paragraphs in `.github/copilot-instructions.md` (~425-437, ~797-810, ~1076) replaced by an Air Deck section.

### 5.4 models.py

`SoundSlot` gains `cues: List[float] = []`, `retrigger: str = "auto"` (`auto|layer|restart|toggle`), `bed: bool = False` — added to `_KNOWN` (models.py:52), `to_dict`, `from_dict` (the `extra` round-trip guarantees old EXEs don't wipe them; mobile exporter ignores them). Slot config dialog gains an "On re-press" option row and a "Bed" checkbox.

---

## 6. Persisted config keys

| Key | Default | Meaning |
|---|---|---|
| `deck_geometry` | `None` | CTk-logical geometry string, via the People geometry store, key `"deck"`; clamped to virtual desktop on restore |
| `deck_pinned` | `false` | Pinned = never auto-hide |
| `deck_compact` | `false` | Compact mode |
| `deck_noactivate` | `true` | Use the SW_SHOWNOACTIVATE path (auto-falls back if the day-1 probe fails) |
| `deck_hotkeys` | `{}` → defaults §2.4 | action → combo; `""` = unbound |
| `deck_long_threshold_s` | `8` | Qualifies for deck / restart-retrigger |
| `deck_auto_show` | `"long_or_loop"` | `always` / `long_or_loop` / `never` |
| `deck_auto_hide_s` | `4` | After last qualifying sound ends |
| `deck_bed_level` | `0.35` | Bed duck level (0.10–0.60; tooltip: keep above Discord's noise gate) |
| `deck_shot_max_instances` | `3` | Layer cap for short sounds |
| `now_playing_visible` | — | **Migration**: ignored on load, dropped on save |

---

## 7. Parallel work packages (disjoint files)

**WP-A — Engine (soundboard/audio.py only).** All of §4. *Acceptance*: `test_deck_engine.py` green (§8); a `speed=1, no-fade, no-bed, no-region` entry executes the byte-identical fast path (asserted by op-count/monkeypatch test); `resume_sound` enqueues `"press"` on `_ptt_queue`; fading entry holds PTT until gain 0 + countdown; `stop_sound(fade_ms=0)` behaviour byte-identical to today (existing callers unaffected); `handle_retrigger` covers the librosa `_pending_ids` window.

**WP-B — Deck modules (new files: deck_model.py, deck_ui.py, deck_hotkeys.py).** Built against the §4 API on paper (a `FakeMixer` stub with the same signatures lets WP-B start before WP-A lands). *Acceptance*: `DeckModel` unit tests (focus rule incl. sticky/freeze, qualify, auto-show/hide) pass without Tk; `DeckPainter.update` ≤ 1 ms per tick and `AirDeckWindow` first-show ≤ 20 ms measured in a withdrawn CTk window at scaling 1.5 (bench script in scratchpad); the day-1 no-activate probe script reports pass/fail and the fallback works; hotkey validation rejects PTT/slot collisions.

**WP-C — gui.py + models.py + constants.py wiring & deletion.** §5.2–5.4. Lands **last**, after WP-A tests are green, as its own commit (revert point). *Acceptance*: `grep -c "NowPlayingPanel\|now_playing_\|add_staged\|_on_panel_stop_sound"` over soundboard/ returns 0 (except the migration line and constants removal diff); app boots, plays, stops, People chips reset from deck stops; window never resizes on play/stop; DJ→Deck button colour tracks `on_visibility`.

**WP-D — Tests + docs (test_deck_engine.py, test_deck_ui.py, .github/copilot-instructions.md, docs/SESSION_BACKLOG.md, .projecthub map, memory).** *Acceptance*: both test files runnable headless per §8; copilot-instructions has no `now_playing_side` and documents the Deck (window, hotkeys, engine API, config keys); .projecthub map lists the three new modules and the deletion.

---

## 8. Test plan

**`test_deck_engine.py`** (repo root, numpy-only, no audio device — construct `AudioMixer` without starting streams and drive `_output_callback(outdata, 1024, None, None)` directly, feeding entries via `sound_queue`):
1. Fade-out reaches exactly 0 within `ms`, entry removed, `('ended', id, faded)` event emitted, output has no sample step > 0.05 across the fade.
2. `pause_sound` → gain hits 0 → `paused=True` → PTT release countdown fires after `_ptt_release_delay` blocks; `resume_sound` → `"press"` appears on `_ptt_queue` and audio resumes with a ramp.
3. Callback safety net: entry active + `ptt_active=False` → exactly one `"press"` per cooldown window (no ping-pong across 30 blocks).
4. `seek_sound` lands within one block; works while paused; seek outside an A-B region clears it and restores `loop_prev`.
5. A-B region: wraps at `loop_end` to `loop_start`, `loop_pass` increments once per wrap, rate==1.0 seam is sample-continuous on a sine (max discontinuity < 0.02), WSOLA path emits no NaNs.
6. `handle_retrigger`: `restart` seeks (no new entry), `toggle` fades, `layer`+`max_instances=3` recycles the oldest.
7. Bed duck: ramps to `bed_level` within attack when a non-bed entry mixes, back within release after it ends; `ptt_user_physical=True` alone also ducks.
8. Fast-path guard: steady 1.0/1.0 entry — spy on `np.linspace` (monkeypatch) asserts zero envelope calls.
9. Loop delay + region interplay, `set_loop_total` pass arithmetic, natural tail fade replaces baked fade (loop toggled on mid-play has no dip).

**`test_deck_model.py`**: pure-python focus rule, sticky focus, 1 s freeze, qualify threshold, chip overflow, auto-show/hide state machine (fake clock).

**`test_deck_ui.py`** (pattern of test_people_ui.py: withdrawn CTk windows, after()-scheduled steps under mainloop, never `root.update()`): AirDeckWindow builds withdrawn without smudge-pattern violations; `reopen()` short-circuits on `winfo_viewable`; Hebrew hero name ellipsised without pre-reordering (assert the canvas text item's string equals a logical prefix + ellipsis); geometry save/restore round-trip; painter update-only (no item creation) across 20 ticks.

**Live smoke matrix (manual, user's rig)**: pause→resume audible in the Discord call (the headline bug); fade-stop clickless on the cable; A-B seam on a music bit; bed duck under physical F9; deck over a borderless game on the second monitor at 150%; ctrl+alt chords in the PyInstaller EXE under the Hebrew layout; spam 20 memes with deck visible (Tk tick < 2 ms).

---

## 9. Open risks & mitigations

- **R1 — No-activate floating window.** `WS_EX_NOACTIVATE`/`SW_SHOWNOACTIVATE` on a Tk toplevel at 150% DPI is the only unproven piece. *Mitigation*: day-1 standalone probe script; flag `deck_noactivate` auto-disables on probe failure; the base PersonPopout path (briefly activates) is fully acceptable v1 behaviour; all reveal code reuses the People smudge fixes.
- **R2 — Callback regressions.** The envelope/seek/region code lives in the function that must never raise, alongside DeepFilterNet. *Mitigation*: WP-A test harness drives the callback directly; the fast-path guard is a tested invariant, so the 99% case executes today's exact code; try/except around only the new steps degrading to pass-through.
- **R3 — `stop_sound` semantics.** *Resolved by design*: `fade_ms=0` default keeps today's behaviour for every existing caller (scheduler, AFK, shutdown, watchdog, tests); only deck-routed calls opt into fades.
- **R4 — ctrl+alt chords**: AltGr aliasing, elevated games, anti-cheat hooks. *Mitigation*: defaults avoid tab/arrows/AltGr-heavy letters where possible, all rebindable with live capture, EXE smoke in week 1; hotkeys are additive — tiles and the deck always work without them.
- **R5 — Retrigger default change** (re-press restarts a ≥8 s sound instead of layering). *Mitigation*: `restart` (not `toggle`) chosen as the forgiving default; per-slot override; 1 s status feedback; threshold configurable.
- **R6 — librosa late-start window.** *Mitigation*: `_pending_ids` in `handle_retrigger`, `started` event + 1.5 s `armed_at` grace on tiles; deck shows "loading…" until the id appears.
- **R7 — Exclusive-fullscreen games hide topmost windows.** *Mitigation*: hotkeys are the primary interface; documented; user's borderless/second-monitor setup is fine.
- **R8 — LED truthfulness.** It reports "app is holding F9", not Discord's transmit state. *Mitigation*: tooltip wording says exactly that; the existing PTT watchdog is unchanged.
- **R9 — Big deletion in a dirty tree** (gui.py already modified). *Mitigation*: WP ordering — engine first (additive, green tests), deletion last as one commit; grep-zero acceptance gate in WP-C.
- **R10 — Effort.** Judges called 7 days optimistic; realistic total is **10–12 days**. v1 ship line = WP-A + WP-B + WP-C core (deck, hotkeys, PTT fix, fades, seek, A-B, bed). Second pass = editor cue markers, BRAKE tape-stop (right-click FADE easter egg: `set_effective_rate` ramp to 0.05 + fade, ~20 lines once envelopes exist), crossfade-into (`deck_crossfade_ms`), compact-mode polish.