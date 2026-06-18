# Session Backlog & Open Items

> **Date:** 2026-06-09 (updated 2026-06-10)
> **Purpose:** Everything still to figure out / fix from this session — the performance work we started with, the data-loss emergency, the keyboard issue, and recovery follow-ups. Cross-references [docs/PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md) for the full perf detail.

---

## 2026-06-10 — "laggy / smudgy moving" session (DONE, uncommitted)

Felt-lag fixes, all smoke-tested via a real app launch (window, search, hover/progress fast paths, hover-preview poll):

- ✅ **Global mouse hook removed** — hover-preview's persistent `mouse.hook` (WH_MOUSE_LL) ran Python per system mouse-move and stalled the OS cursor whenever the Tk thread was busy. Now a 30 ms `GetAsyncKeyState` poll (`_poll_hover_preview_button`); the `mouse` lib remains only for the 5 s Record-Key capture.
- ✅ **Window MOVE no longer arms the resize-defer** — pure title-bar drags (w×h unchanged) skip the defer + post-resize sweep entirely (`_last_root_size`); the post-move redraw wave is gone.
- ✅ **#49/#50/#51 logging** — root→WARNING, `soundboard.*`→INFO (`LSB_DEBUG=1` for DEBUG), PIL/numba/pydub/filelock silenced, RotatingFileHandler 1 MB×3; `_drop_log` routed through the logger.
- ✅ **#8 hover** — Enter/Leave recolours the persistent `bg` rect (`_apply_hover_fill`); no more `delete("all")` per slot crossed.
- ✅ **#10 progress bar** — persistent fill rect moved via `coords()` per tick (`_prog_fill_id`); volume-gauge path still full-rebuilds (rare).
- ✅ **#17 shift+wheel volume** — progress-strip-only repaint; dropped the per-notch `_update_slot_button_for_tab`. Also FIXED the main-board volume gauge, which had never rendered (`tab_slot_buttons` holds ButtonProxy facades — now unwrapped via `.slot_widget`).
- ✅ **#16 `_app_is_active` cached 50 ms** (Win32 foreground query shared across the stacked wheel handlers).
- ✅ **#20 (lite) main search debounced 180 ms** + shared footer CTkFont (was leaking a named Tcl font per result per keystroke). Pooled-widget overlay rebuild (#20 full) still open.
- ✅ **Scroll blank-row smudge** — grid wheel handler culls synchronously after `yview_scroll`; `_apply_row_minsizes` memoized per `(grid, cols, rows, row_height)` (#18).
- ✅ **Resize smear shortened** — SlotWidget defer 220→100 ms, sweep 180→120 ms, sweep chunk 16→24.
- ✅ **People-window resize** — chip title re-wrap throttled to one shared 120 ms pass (`_flush_chip_rewrap`, scaling resolved once per pass, timer cancelled in `_cleanup`).
- ✅ **#41 (+ range-picker) editor resize debounce** — both waveform canvases redraw 80 ms after the size settles, with `winfo_exists()` guards.
- ✅ **Voice level meter** — `progress_color` reconfigured only when the colour zone changes, not every tick.
- ✅ **People-window resize smudge** — the CTk `_draw()` defer patch only covered the MAIN window (deferring other toplevels with no sweep left them permanently blank, so they drew per pixel). Now `_shared.arm_toplevel_resize_defer()` gives the People hub + pop-outs their own per-toplevel defer window AND their own chunked sweep (`RESIZE_STATE["tops"]`/`["dirty_tops"]`), armed from `_remember_geometry`/`_remember_size` on true size changes only (never moves, never first map). Verified live: arms on resize, sweeps clean.
- ✅ **People chip drag ghost throttled to ~60fps** — was a synchronous `Toplevel.geometry()` per motion pixel.
- ✅ **Big-sound first-play freeze** — long (≥4 MB) files were deliberately never warmed, so the FIRST click decoded + resampled on the GUI thread (multi-second hang on long person sounds). New `SoundCache.warm_disk_cache()`: the startup warmer now decodes long files ONCE into `audio_cache/` without keeping them in RAM — first play becomes one `np.load`. Streaming-sized files (≥10 min) are skipped (playback streams them).
- ✅ **Adversarial review pass (4 agents)** caught + fixed before landing: (1) cross-slot stale volume gauge — the shared 1s clear-timer orphaned slot A's gauge when slot B was wheeled within 1s (now tracks `_volume_indicator_widget` and clears the previous slot immediately); (2) a pending search debounce could re-show the results overlay 180 ms after a tab switch (now cancelled in `_hide_search_results` + both filter handlers); (3) sub-30 ms mouse taps missed by the hover-preview poll (now also triggers on `GetAsyncKeyState`'s 0x0001 "pressed since last call" bit, flushed at registration).

## 2026-06-10 (later) — People-windows deep fix ("really really laggy / design keeps breaking")

A 4-agent deep-dive mapped every cost + breakage path in the People windows; all fixes below smoke-tested live (hub open → cycle 7 people → resize → raise covered panel):

- ✅ **CTk tracker patch (gui.py "perf patch #2")** — destroying ANY CTk widget did a linear equality scan over two GLOBAL callback lists (every live widget in the app); destroying a ~300-widget panel ≈ 1M+ bound-method comparisons. `AppearanceModeTracker.callback_list` and `ScalingTracker.window_widgets_dict` values are now `_CallbackBag`s (dict keyed on `(id(self), func)`) → O(1) add/remove. Speeds every rebuild/eviction/dialog-close app-wide.
- ✅ **CTkScrollableFrame bind_all leak (gui.py "perf patch #3")** — every scrollable frame ever created left its 5 `bind_all` handlers (wheel + 4 Shift) installed forever; scrolling got slower all session. Funcids are recorded at init and spliced out of the `all` bindtag on destroy.
- ✅ **Covered panels no longer resize-repaint** — all 5 tkraise-stacked cached panels are mapped, so a hub resize fanned out to ~1500 widgets and the sweep repainted the ~80% that were invisible. The defer patch now parks covered panels' widgets on `panel._covered_dirty`; `select()` drains it (chunked) right after `tkraise` + flushes parked chip re-wraps.
- ✅ **Async config writer** — the ~390KB serialize + `.bak` copy + atomic write moved off the Tk thread (single serialized `ConfigWriter`, newest-snapshot coalescing, `sync=True` flush on shutdown under `_config_io_lock`). Every window move/drag/slider used to cost a visible frame ~0.4s later.
- ✅ **Open/pop-out double build killed** — panels were built pre-map at `winfo_width()==1` (1-column squish) then fully rebuilt at real width. `_win_width()` now falls back to the wm-geometry request pre-map, and the hub defers its initial `select()` one tick (window appears instantly).
- ✅ **Panel-wide chip build pump** — rebuild built every card + first 6 chips of EVERY group in one tick (per-group `after(1)` chunking only paced within a group). All chip chunks now drain through one ~8ms-budget `after()` pump per panel.
- ✅ **Reflow waits for the drag to end** — the 120ms column-change rebuild fired MID-drag (300–600ms freeze + popcorn repaint inside the defer = "design keeps breaking"); now re-defers until the per-toplevel defer expires. Chip re-wrap flush does the same (and skips covered panels).
- ✅ **CTkImage caches** — chip labels / thin name images / emoji / avatars cached as SHARED CTkImages (bounded LRU, keyed like the PIL caches). The old "fresh CTkImage per call" rule re-ran a BICUBIC resize + PIL→PhotoImage conversion per chip per rebuild (~100–200ms/panel). CTkImage is designed to be shared (per-size PhotoImage + consumer tracking). `_wrap_label` is now `lru_cache`d (PIL textbbox per word per chip per rebuild before).
- ✅ **`_font()` memoized** — was a fresh named Tcl font per widget per rebuild, never freed (font table grew all session). 4 entries now cover the board.
- ✅ **changed() fan-out tamed** — multi-window rebuilds staggered (first now, rest `after(30·i)`); cross-person copy-drop no longer rebuilds the SOURCE person; "Add from main board" batches to ONE rebuild on Done; sound settings (volume/speed/loop/pitch) persist-only (nothing rendered changes); LRU eviction destroys panels in `after_idle`, not inside the click.
- ✅ **Breakage hardening** — interrupted sweeps now RE-PARK their remainder (main window + per-toplevel) instead of dropping widgets stale; `_schedule_nudge`/`_reflow` are per-toplevel-defer aware; sidebar refreshes after person-meta edits (`ctx.refresh_sidebar` hook); selection highlight reconciles all rows but repaints only changed ones; drop-target sweep throttled to ~1/s.
- ✅ **Near-instant person switching (follow-up: "groups load 1-2s tab to tab")** — root causes: cache cap 5 < 10 people (constant evict+rebuild thrash) and group CARDS still built synchronously in rebuild(). Fixes: `MAX_CACHED_PANELS` 5→32; cards stream through the build pump (placement precomputed, FIFO keeps masonry order); chip chunk 6→3 (a CTkButton ≈4ms to create; 6-chip jobs overran the 8ms tick budget); **`_prewarm_panels`** builds every person's panel in the background after the hub opens (sequential, one panel at a time, `update_idletasks()` flush per panel — the after(1) pumps starve Tk's idle queue and the backlog otherwise hits the first click). Measured: warm ~7s in background, then every switch ≤138ms (most 54–95ms — the tkraise expose repaint).

## 2026-06-10 (evening) — PTT lock "again" + mic audible during sounds

Root causes found and fixed in `audio.py` (+ GUI wiring):

- **"PTT locked"** — the app injects the user's own Discord PTT key (F9) for auto-PTT. When a sound ended while the user was PHYSICALLY holding F9 to talk, the injected key-UP cut Discord off mid-sentence — and a held key sends no further key-downs, so transmission stayed dead until they re-pressed. **Fix:** the mixer now tracks the user's real finger via a keyboard hook (`_register_ptt_physical_watch`; our own SendInput moments are excluded via `_inject_window_until` ~50ms windows — injected keys don't auto-repeat, physical ones do). Every release path (`_do_ptt_release`, `_force_release_ptt`) skips the key-up injection while `ptt_user_physical` is True; the user's own release ends the transmission. Press also skips injection when the key is already physically down.
- **"People hear my mic when I play a sound"** — during auto-PTT Discord transmits the whole cable, and the live mic was always mixed in. **Fix:** new `duck_mic_during_sounds` (default ON, persisted as `duck_mic_during_sounds`, checkbox "🤫 Mic muted during sounds" next to Mute Mic): the mic is zeroed in the output mix while `ptt_active and not ptt_user_physical and not manual_ptt_hold` — so sounds transmit clean, but holding F9 to talk over a sound still works, and Test Output's manual hold still carries the mic.
- Mouse-button PTT keys aren't physically watchable (no auto-repeat signal) — `ptt_user_physical` stays False for them; the duck + injected release behave as before for mouse PTT.
- Verified: VK cache (f9→120), watch register/unregister on key change, release-skip leaves no stuck state, app boots with stream auto-start + watcher active + duck applied.

### Still open after this pass (next pain-ranked candidates)
- ⏳ **#15 one mousewheel dispatcher** — handlers still stack per notch (the 50 ms active-cache removes most of the cost; full consolidation deferred as M-effort/CTk-internals risk).
- ⏳ **Editor marker-drag** still full-redraws per pixel (#40) — needs persistent bars + `coords()` markers.
- ⏳ **First play of a long sound still np.loads on the GUI thread** (tens–hundreds of ms for very large PCM; was seconds of decode). Fully async play-path decode remains the follow-up.
- ⏳ **Async config save** (#22) — serialize+write still on the Tk thread (~12–26 ms per debounced flush).
- ⏳ Plan A remainders: #1 splash-before-imports, #6 lazy constants imports, #7 splash floor, #22, #47.

---

## Status snapshot — DONE & committed (2026-06-09)

- ✅ **People data recovered & committed.** 8 people / 83 person-sounds restored from git's object store, deep-merged across all backups. Commits `f713d46` (77-sound floor), `923c67b` (83-sound merge). Frozen copy at `_recovery/GOOD_config_8people.json`.
- ✅ **80 orphaned sounds recovered** into a "♻ Recovered" tab (`8ccec74`) — includes Yair's lost sounds; re-file them onto people once you've confirmed.
- ✅ **Save path hardened** — an empty in-memory persons list can no longer overwrite people that exist on disk (`_save_config_now`).
- ✅ **Rotating config backups** — `config_backups/` snapshot at every launch (keeps 20), so `.bak` getting overwritten can't cause total loss again.
- ✅ **Pop-up crash fixed** — `PersonPanel._cleanup()` cancels pending `after()` timers.
- ✅ **People-hub HANG fixed** (`9995bcb`) — chips build incrementally (`after()`-chunked); no UI freeze, no keyboard-stall.
- ✅ **Window-size growth fixed** (`6368aa2`) — restore via raw `wm_geometry` (HiDPI no longer balloons the window).
- ✅ **Startup freeze fixed** (`b14f9a7`) — dropped the per-tab layout-flush warm-up.
- ✅ **Decoded-audio disk cache** (`26faff8`) — `audio_cache/`; decode once, `np.load` thereafter. Edit/clone safe.
- ✅ **Image caches** — slot-image (#3) + avatar (#38) decode cached.
- ✅ **Shift key** — was the frozen app stalling the global keyboard hook; resolved + the hang that caused it is fixed.

### Still open
- ⏳ **Single-instance guard** — slow startup once led to 6 stacked instances ("3 windows"); add a guard so re-launching focuses the existing window.
- ✅ **People search debounce (#37)** — `PersonHub._on_search` debounced 180 ms; pending timer cancelled on close.
- ✅ **People panel-cache eviction (#39)** — panel cache is now an LRU `OrderedDict` bounded to 5; least-recent panels destroyed.
- ✅ **Chip-label PIL cache (#36-lite)** — `_chip_label_image` composite cached by `(name, emoji, px)`; rebuilds/search/density stop re-rasterizing every chip. Fresh CTkImage per call (avatar-cache contract).
- ✅ **People sounds warmed into the decoded cache** — `_preload_sounds` now also walks `persons[*].groups[*].sounds`, so person sounds are decode-cached to `audio_cache/` at launch (no cold first-play). They already shared the same `SoundCache` on playback.
- ✅ **Readable 2-line sound chips** — bold white + solid ~2px black outline (reads on any colour/state). `_wrap_label` wraps by **measured pixel width** (same font+stroke as the render; char-count never triggered for Hebrew) and **re-wraps to the chip's REAL width** via the play button's `<Configure>` (masonry tiles are narrower than `chip_w`; CTkButton forwards Configure to its canvas → capture the button in a closure). Heights L52/M48/S44. RTL float-bbox crash fixed (`math.ceil`). Verified narrow+wide, 6 backgrounds.
- ✅ **Chip length bar invisible when idle** — was showing a stray dot/dark "shadow" at the chip bottom; now `progress_color=bg` at idle, switched to `fg` only while playing/previewing, and the volume gauge restores to invisible.
- ✅ **Coloured person names + readable group/tab names** — shared `_thin_outlined_image` helper (bold, hairline outline): person names in their colour (white if none), group/tab headers in white. Used in `_build_person_row` + `_build_group`.
- ✅ **Shift+wheel volume on People chips** — was main-board-only; now `_SpeedScrollableFrame.on_shift_wheel` → `PersonPanel._shift_wheel_volume` (cap 0–1.5, inverted, live mixer update via `PersonContext.set_volume`/`_set_person_sound_volume`, debounced persist, length-bar volume gauge). Shift via raw `event.state` OR CTk `_shift_pressed`. Smoke-tested end-to-end.
- ⏸️ **True chip viewport virtualization (#36 full)** — DEFERRED by decision. The freeze is fixed (chunked build) and real people carry ~10–30 sounds; virtualizing the fragile masonry (home of the empty-boxes / groups-disappeared / RTL bugs) is high-risk for little real gain. Opt-in only if someone loads hundreds of sounds.
- ⏳ **Re-file Yair's sounds** from the ♻ Recovered tab once verified.
- ⏳ **Perf backlog** — #42 editor off-thread, C1 canvas-per-tab; re-run profiler for real numbers.

---

## P0 — BLOCKING (do first)

### 1. People hub HANG (#36) — freezes the app AND stalls the keyboard
- **Symptom:** opening the People hub / a pop-out freezes the whole app (black window + spinner). Because the app holds a global keyboard hook, a freeze also **stalls keyboard input system-wide** (this is what broke shift).
- **Cause:** a person builds **all** its chips synchronously on the UI thread (Keren 27, Oz 19, …); no virtualization. In `person_board.py` `rebuild()` / `_expand_group_inplace()` / `_build_chip()`.
- **Fix:** build chips **incrementally** (`after()`-chunked, like the emoji picker) and/or **virtualize** (only build visible chips). Port the main board's cull primitive.
- **Until fixed:** DO NOT open the People hub. The main soundboard is fine.
- **Bonus hardening:** make the global keyboard hook resilient so a UI freeze can't stall system input (watchdog / run hotkeys off the UI thread).

### 2. Orphan-sound recovery (Yair + the 2 lost people)
- **Found:** **125 orphaned audio files** in `sounds/` (590 on disk, 465 referenced). The ~30 Hebrew-named `…_50xxxxxx.wav` files dated **2026-06-08** (wipe day) are almost certainly **Yair's lost sounds + the 2 missing people**. Full list: `_recovery/orphans.txt`; script: `_recovery/find_orphans.py`.
- **Note:** git CANNOT attribute these to Yair (he was empty in every backup), so re-filing needs the user to recognize them.
- **DECISION NEEDED:** build a **"♻ Recovered" tab** with (a) just the ~30 wipe-day sounds, (b) all 125, or (c) just hand over the list. Then user drags Yair's onto Yair.
- **Also try:** Windows **Previous Versions** on `soundboard_config.json` (right-click → Properties → Previous Versions) — could surface the original 10-person config with Yair's sounds already filed.

---

## P1 — Data-safety follow-ups

- [ ] **Confirm the wipe trigger.** The pop-up crash is the prime suspect (fixed). Verify no other path can empty `self.persons` at runtime.
- [ ] **Better backup strategy.** Root problem was: a bad save copied the wiped config over `.bak`, destroying the only backup. Add **rotating, timestamped backups** (e.g. keep last N good configs) and **never overwrite a good `.bak` with a smaller/empty one**.
- [ ] **Extend the empty-overwrite guard** to other critical sections if needed (tabs already guarded; persons now guarded).
- [ ] **The 2 lost people** were never git-saved → only recoverable via orphans / Previous Versions. Need the user's recollection of their names.

---

## P2 — Performance (the original goal) — see [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md)

**Felt-lag reality (from user):** the pain is **opening windows, tabs/grid, and window drag-resize** — NOT startup/search/audio. So prioritize accordingly.

### Done this session
- [x] #3 slot-image decode cache (path+mtime+size)
- [x] #38 person-hub avatar cache

### Open — mapped to the user's actual pain
- [ ] **#36 person-board virtualization** — fixes the People hang (P0 above) AND the "opening People is slow" lag. *Same work item.*
- [ ] **#42 editor off-thread load** — opening the sound editor does a synchronous full-file resample on the UI thread (clone path [gui.py:10355](../soundboard/gui.py)). Move decode to a worker.
- [ ] **C1 — canvas-per-tab** — the real cure for **drag-resize** lag and dense-tab scrolling. Folds in #11 (drop per-slot footer), #8 (hover redraw), #12 (range-diff cull). Bigger rewrite; decide after measuring.
- [ ] **Decision:** incremental (#11/#8/#12) vs structural **C1**. C1 subsumes them. Measure drag-resize first, then commit to one.

### Plan A quick wins (real but lower felt-impact — from the plan)
- [ ] Logging: root→WARNING, silence PIL/numba, RotatingFileHandler (kills the 6 MB `debug.log` write-storm)
- [ ] Splash-before-imports + lazy-import librosa/scipy/pyrnnoise (~4.4 s blank launch → ~1 s)
- [ ] Search debounce + image LRU (~340 ms/keystroke freeze) — only if search is actually used
- [ ] Progress bar → `coords()`/`itemconfigure()`; single mousewheel dispatcher; async config save

### Profiling
- [ ] **Re-run the profiler** (`LSB_PERF=1`) once the People hang is fixed — the first run got interrupted by the crash, so we still have **no hard numbers** on the real bottleneck. Harness: `soundboard/perf_probe.py` (inert unless `LSB_PERF=1`).

---

## Open questions / decisions needed (from the user)

1. **Recovered-sounds tab:** focused ~30, all 125, or just the list? (P0.2)
2. **The 2 lost people** — do you remember their names? (helps re-filing orphans)
3. **Windows Previous Versions** available for this drive? (could auto-restore Yair)
4. **Perf approach:** OK to fix the People hang (#36) next? It clears the freeze + the keyboard-stall in one go.
5. **Commit/branch policy** for the bigger perf work (C1) — branch off `main`?

---

## Housekeeping (low priority)

- [ ] Stop tracking junk that got committed/created: `debug.log` (6 MB), `perf.log`, `importtime.txt` → add to `.gitignore`.
- [ ] `_recovery/` folder — keep until recovery fully done, then remove (or keep `GOOD_config_8people.json`).
- [ ] `stop_soundboard.bat`, `soundboard/perf_probe.py` — temporary tools; remove when done.
- [ ] Still-uncommitted WIP in `audio.py` and others — commit or stash so future changes are isolated.
- [ ] Lesson learned: commit/checkpoint **before** big sessions so app-state vs code-changes stay separable.
