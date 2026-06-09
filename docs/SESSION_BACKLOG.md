# Session Backlog & Open Items

> **Date:** 2026-06-09
> **Purpose:** Everything still to figure out / fix from this session — the performance work we started with, the data-loss emergency, the keyboard issue, and recovery follow-ups. Cross-references [docs/PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md) for the full perf detail.

---

## Status snapshot (what is SAFE right now)

- ✅ **People data recovered & committed.** 8 people / 83 person-sounds restored from git's object store, deep-merged across all backups, applied to `soundboard_config.json` + `.bak`. Commits: `f713d46` (77-sound floor) and `923c67b` (83-sound merge). Frozen copy at `_recovery/GOOD_config_8people.json`.
- ✅ **Save path hardened** — an empty in-memory persons list can no longer overwrite people that exist on disk ([gui.py `_save_config_now`](../soundboard/gui.py)).
- ✅ **Pop-up crash fixed** — `PersonPanel._cleanup()` now cancels pending `after()` timers so a preview/reflow callback can't fire on a destroyed window.
- ✅ **Perf quick wins landed** — slot-image decode cache (#3) + person-hub avatar cache (#38).
- ✅ **Shift key** — was a side effect of the frozen app stalling the global keyboard hook; resolved on app shutdown.

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
