# LocalSoundBoard — Lightning Performance Plan

> **Status:** Planning document (no code changes yet)
> **Date:** 2026-06-08
> **Scope:** Bottom-to-top performance audit of the whole app — startup, GUI, tabs, slot grid, scrolling, audio engine, voice FX, person board, editor, emoji picker, config, logging — plus 3 strategic plans.
> **Hard constraint:** We can rewire and refactor freely, but **every feature that works today must keep working.** No regressions.
> **Method:** 15 parallel deep-read agents over the real source + 3 research tracks (GUI toolkits, Tkinter techniques, audio/startup libs), cross-checked against the live config (18 tabs / 437 slots / 10 persons) and the 6 MB `debug.log`.

---

## 0. TL;DR — Where the lag actually is

The app is **already well-optimized in the places the team has focused on**, and the felt lag is concentrated in a handful of very specific, very fixable spots. The headline numbers:

| Symptom | Root cause | Measured cost | Fix tier |
|---|---|---|---|
| **~4.4 s blank screen on launch** | Heavy libs (`sounddevice` 2.6 s, `scipy.signal` 0.8 s, `pyrnnoise` 0.44 s) imported at module top **before the splash window even exists** | ~4.4 s warm, more cold | Quick win |
| **Search freezes while typing** | Cross-tab search has **no debounce** and **destroys + rebuilds every result widget on every keystroke** | **~340 ms / keypress** | Quick win |
| **6 MB `debug.log`, disk I/O during the snappy moments** | Root logger at `DEBUG` captures **PIL + numba** internals | 100,819 lines, **~80% third-party spam** | Quick win |
| **Hover micro-jank across a dense tab** | Mouse Enter/Leave does a **full canvas teardown + rebuild** (`delete("all")`) per slot for a 6 % colour change | N create-calls per pixel of mouse travel | Quick win |
| **Progress animation churn** | `_redraw_progress` does `delete("progress")` + `create_rectangle()` **every frame, every playing slot** | per-frame alloc churn | Quick win |
| **Startup widget storm + micro-stalls** | All 18 tabs' grids (~523 cells, ~1,600 widgets) built up-front, each background tab force-laid-out with a synchronous `update_idletasks()` (17×) | ~1–2 s of main-thread work after the window appears | Architectural |
| **Save hitch after slider/drag** | Each debounced save re-serializes the **whole** config + `shutil.copy2` of a 330 KB `.bak`, **on the Tk thread** | ~12–26 ms per flush | Architectural |

**What's already good (do not "fix" these):** the slot is already a single self-painting `tk.Canvas` (`SlotWidget`) instead of a CTkButton stack; the grid already has **row virtualization / occlusion culling**; there's a **monkeypatch that suppresses CTk `_draw()` during window resize** + a chunked deferred sweep; tab switching is genuinely **O(1)** (`grid_remove`/`grid` swap, not rebuild); the audio callback and animation loops contain **no per-frame logging**; the voice-FX biquad **caches its coefficients**; config save is **already debounced 400 ms** and atomic; huge (>10 min) audio files **already stream** instead of fully decoding.

The implication: we are **not** starting from stock CustomTkinter. The two cheapest big wins (single-canvas slot, virtualization) are already shipped. So the plans below are graded against *that* baseline.

---

## 1. Full bottom-to-top findings

Each finding: **impact** (how much felt lag it causes) · **effort** (S/M/L/XL) · **risk** (what could break). Locations are clickable.

### 1.1 Startup / cold launch — *the biggest felt problem*

1. **[CRITICAL] Heavy libs imported before the splash exists.** `import librosa`, `from pyrnnoise.rnnoise import …`, and `from scipy.signal import …` run at module import — pulled in by [main.py:63](main.py#L63) `from soundboard import SoundboardApp` **before** `__init__` builds the splash. Measured: `sounddevice` 2.6 s, `scipy.signal` 0.8 s, `pyrnnoise`/audiolab 0.44 s = **~4.4 s of blank desktop**. Worse: `scipy.signal` and `pyrnnoise` are loaded even though their features (radio voice FX, noise suppression) default to **off**. — effort **L** · risk: must preserve `*_AVAILABLE` flags + try/except fallbacks; never trigger a first-time import inside the audio callback.
2. **[HIGH] All 18 tabs built up front (~1,600 widgets).** [_build_all_tab_widgets (gui.py:6339)](soundboard/gui.py#L6339) builds the current tab, then `_build_tabs_incrementally` drains the other 17 one-per-15 ms. 18 tabs / 437 slots → **523 cells × ~3 Tk widgets**. — effort **M** · risk: first switch to an unbuilt tab pays a one-time build; keep `_prioritize_tab_build` on switch.
3. **[HIGH] 220 slot images decoded + LANCZOS-resized on the Tk thread.** [_load_slot_image (gui.py:12235)](soundboard/gui.py#L12235) does `Image.open` + `thumbnail(LANCZOS)` + `CTkImage` with **no cross-tab cache**. Current tab's 7 images = 34 ms; **all 220 = ~963 ms** of main-thread decode during build, repeated every launch and every density change. — effort **M** · risk: `CTkImage` must be built on the main thread (only decode can move off-thread); keep strong refs.
4. **[MEDIUM] Per-background-tab synchronous `update_idletasks()` warm-up (17×).** [gui.py:6110-6122](soundboard/gui.py#L6110-L6122) grids each hidden tab, forces a **full-window layout+paint flush**, then ungrids — 17 full relayouts shortly after launch, largely **redundant** because virtualization re-culls on first show anyway. — effort **S** · risk: first switch may show a one-frame settle; warm only top rows or rely on on-show cull.
5. **[MEDIUM] Audio Options panel built on the first-paint path though hidden by default.** [_create_device_section (gui.py:2859)](soundboard/gui.py#L2859) eagerly builds a `CTkScrollableFrame` + device combos + PTT/NS/voice cards during `_create_ui`, even though the panel is collapsed. — effort **M** · risk: config restore reads combo values; stage saved selections into plain vars and apply on first expand.
6. **[MEDIUM] `constants.py` pays ~42 ms import cost for `emoji_data_python` (35 ms) + `colour` (7 ms)** at module load, for data only needed when the emoji picker opens. [constants.py:10-11](soundboard/constants.py#L10-L11). — effort **S** · risk: low; move imports inside the lazy builder/util functions.
7. **[MEDIUM] Splash min-show floor (0.9 s) is added *after* the slow part.** [gui.py:2571-2577](soundboard/gui.py#L2571). The splash can't paint during the imports (the worst part), then adds an artificial 0.9 s once content is ready. — effort **S** · risk: keep a small floor (~250 ms) to avoid a flash; keep the 6 s safety dismiss.

### 1.2 Slot grid rendering & virtualization

8. **[HIGH] Hover repaints the entire slot canvas.** [_on_enter/_on_leave (slot_widget.py:762-770)](soundboard/slot_widget.py#L762-L770) → `_redraw_full()` does `delete("all")` + recreate every item, just to lighten the fill 6 %. Moving the mouse across a tab tears down/rebuilds two canvases per slot crossed. — effort **M** · risk: keep a persistent tagged `bg` rect and `itemconfigure` its fill; fall back to full redraw if missing.
9. **[HIGH] `_redraw_full` is the only update path — one logical update fires 3–5 full redraws.** [slot_widget.py:381](soundboard/slot_widget.py#L381). Every setter ends in `_redraw_full()`; `ButtonProxy.configure` fans one `btn.configure(...)` into up to 5 setter calls, each a full teardown. — effort **M** · risk: add a `_suspend_redraw` counter so a batched configure redraws once; keep early-return equality checks.
10. **[HIGH] Progress bar churns canvas items every frame.** [_redraw_progress (slot_widget.py:517)](soundboard/slot_widget.py#L517) does `delete("progress")` + `create_rectangle()` per tick per playing slot. The Tk canvas golden rule is *reuse + `coords()`/`itemconfigure()`, never delete+recreate*. — effort **S** · risk: low; persist the fill-rect id, update geometry on resize only.
11. **[MEDIUM] Per-slot group footer is a `CTkLabel` (= CTkFrame + tk.Label = 2 widgets) on *every* slot, even the ~95 % with no groups.** [gui.py:6060](soundboard/gui.py#L6060). Doubles surviving per-slot widget count (~2,176 live Tk widgets after build). — effort **M** · risk: draw the footer text on the SlotWidget canvas, or use a plain `tk.Label`, or create lazily only when a slot has groups (reserve row minsize).
12. **[MEDIUM] Range-diff the cull instead of scanning all wrappers.** [_cull_slots (gui.py:2481)](soundboard/gui.py#L2481) iterates every wrapper calling `winfo_ismapped()` (a Tcl round-trip) + `bbox("all")` per scroll settle. Track the mapped `(first,last)` range and only touch entered/exited rows. — effort **M** · risk: re-sync the cached range after rebuild/density change; keep a full-scan fallback.
13. **[LOW] `os.path.exists()` disk stat per image slot on every update**, even on cache hit. [gui.py:12472](soundboard/gui.py#L12472). — effort **S** · risk: stat only on cache miss / path change.
14. **[LOW/Architecture] "One big canvas for the whole grid" — assessed and NOT recommended now.** Would force re-implementing hit-testing, drag-drop, scrollregion, z-order; current per-slot canvas + culling already pays the worst cost (item creation) only for visible slots. Revisit only if profiling demands it. (See Plan C1 for the *middle* option: one canvas per **tab**.)

### 1.3 Animation loop & scrolling

15. **[HIGH] Three stacked `bind_all("<MouseWheel>")` handlers all fire every notch.** [gui.py:2685](soundboard/gui.py#L2685), [4183](soundboard/gui.py#L4183), [5765](soundboard/gui.py#L5765). Because they use `add="+"`, returning `"break"` does **not** stop the siblings — so each notch runs all three, each calling `_app_is_active()` (a Win32 `GetForegroundWindow`/`GetWindowThreadProcessId` pair that **re-runs `import ctypes`**) + a `winfo_containing` ancestry walk. ~3–6 Win32 calls per notch. — effort **M** · risk: collapse to one dispatcher; preserve region precedence and the active-window gate; keep returning `"break"`.
16. **[MEDIUM] `_app_is_active()` re-imports ctypes + 2 Win32 syscalls per call, no caching.** [gui.py:2753](soundboard/gui.py#L2753). Foreground state can't change mid-gesture — cache it for ~50 ms and hoist the import. — effort **S** · risk: short TTL so focus loss still stops scroll within a frame.
17. **[MEDIUM] Shift+wheel volume does a full slot repaint (incl. the `os.path.exists` stat) per notch** [gui.py:5881](soundboard/gui.py#L5881) — but volume changes no colour/emoji/image; the overlay already shows feedback. — effort **S** · risk: keep the live-mixer volume update; just drop the repaint.
18. **[LOW] Cull re-reserves every row's minsize each settle** [gui.py:2499](soundboard/gui.py#L2499) — memoize per `(tab, cols, row_height)`. — effort **S**.
19. **[NEGATIVE/Good] The animation loop is healthy:** idles to 250 ms when nothing plays, work is proportional to *playing* slots only, caches progress at 1 % granularity, only touches the current tab. No change needed beyond #10.

### 1.4 Search overlay / dialogs / config persistence

20. **[CRITICAL] Search overlay: no debounce + destroy/rebuild all result widgets per keystroke = ~340 ms/keypress.** [_on_search_changed (gui.py:6832)](soundboard/gui.py#L6832) → [_show_search_results (gui.py:7067)](soundboard/gui.py#L7067). First letter is worst (broadest match, ~70 results). The emoji picker **already** debounces ([emoji_picker.py:1027](soundboard/emoji_picker.py#L1027)) — same pattern simply was never applied here. — effort **M** · risk: keep the per-result lambdas + `_search_slot_widgets` keys identical so playing-state mirroring keeps working.
21. **[HIGH] `_load_slot_image` is uncached and hammered during search rebuilds** — every keystroke re-reads + LANCZOS-resizes each result's image. [gui.py:12235](soundboard/gui.py#L12235). An LRU keyed on `(path, mtime, size)` fixes this **and** speeds normal builds/tab-switch. — effort **S** · risk: key on mtime so replaced images refresh.
22. **[MEDIUM] `_save_config_now` runs ~12–26 ms of synchronous disk I/O on the Tk thread per flush** — full re-serialize of all tabs+persons + `shutil.copy2` of the 330 KB `.bak` + atomic write. [gui.py:13748](soundboard/gui.py#L13748). It's already debounced 400 ms (good) and has **50+ call sites**, but each flush is still a missed frame, and the `.bak` byte-copy is pure redundant disk traffic. — effort **M** · risk: data-safety-critical; build the dict on the main thread, hand bytes to a single serialized worker; keep atomic `os.replace` + shutdown flush. Cheaper `.bak`: rename instead of `copy2`.
23. **[LOW] `_run_search` re-scans all 437 slots with per-slot string joins each keystroke** [gui.py:6907](soundboard/gui.py#L6907) — precompute a per-slot lowercased blob; folds into the debounce. — effort **S**.
24. **[LOW] Voice popup re-renders all preset emoji icons from scratch on every open** [gui.py:7964](soundboard/gui.py#L7964) — cache rendered icons at app scope. — effort **S**.

### 1.5 Audio engine (real-time)

25. **[HIGH] Output callback holds `self.lock` across the entire per-sound mix loop.** [audio.py:1409](soundboard/audio.py#L1409). The same lock is taken by the GUI's 20 fps `get_playing_sounds()` and by every stop/volume call, so **GUI and audio thread serialize** — micro-jank under load, underrun risk. — effort **L** · risk: concurrency-sensitive. Make the audio thread the sole owner of `currently_playing`; route all external changes through a command queue; publish an atomic snapshot for the GUI. Test with mainloop+after.
26. **[MEDIUM] Per-block heap allocations in the callback** (`np.zeros`, `column_stack`, padding, `_soft_clip` `np.copy`/double `abs`). [audio.py:1382+](soundboard/audio.py#L1382). PortAudio's rule is "no allocation in the callback." Pre-allocate scratch buffers, `arr.fill(0)`, accumulate with `out=`. — effort **M** · risk: keep gain/stereo semantics bit-identical; sounddevice copies `outdata` so in-place is safe.
27. **[MEDIUM] No cache eviction — every triggered/preloaded sound stays decoded in RAM forever.** [audio.py:702](soundboard/audio.py#L702). Float32 @48 kHz stereo ≈ 384 KB/s/ch; ~1,000 configured sounds grow unbounded. Add an LRU byte cap (e.g. 150–250 MB) + store as **int16** (~half the RAM). — effort **M** · risk: don't evict a buffer mid-copy; play path already copies so playing sounds are independent.
28. **[LOW] Full-buffer `.copy()` on every play** [audio.py:816](soundboard/audio.py#L816) — multi-MB memcpy on the click path for long clips; apply fade virtually so the cached buffer can be shared read-only. — effort **M**.
29. **[LOW] RNNoise input ring uses per-block `np.concatenate`** (3–4 allocations/block on the input RT thread). [audio.py:181](soundboard/audio.py#L181) — replace with a fixed preallocated ring. — effort **M** · risk: keep the documented one-frame priming (the buzz fix).
30. **[LOW] Peak metering does two full-block `np.max(np.abs())` every block** purely for a ~5 fps GUI meter [audio.py:1596](soundboard/audio.py#L1596) — reuse `_soft_clip`'s max or compute every Nth block. — effort **S**.
31. **[NEGATIVE/Verdict] Do NOT move to `python-rtmixer`.** Its docs explicitly exclude in-callback signal processing + per-sound gain — but this app's voice changer, RNNoise, WSOLA, soft-clip, PTT all run *inside* the callback. Keep the Python callback and optimize it. numba/Cython are scalpels for one profiled scalar kernel (e.g. WSOLA), not strategy.

### 1.6 Voice Changer (voice_fx.py)

> Verdict: **correctly written for the audio thread** — fully vectorized, zero per-sample Python loops, biquad coefficients cached ([voice_fx.py:421](soundboard/voice_fx.py#L421)), `process()` never raises. The cost is **allocation churn**, not danger.

32. **[MEDIUM] Reverb runs a Python loop over 8 comb filters, each a full `np.mod` + fancy-index gather/scatter** [voice_fx.py:493](soundboard/voice_fx.py#L493) — the heaviest enabled effect. Replace the modulo gather with slice + at-most-one wrap. — effort **M** · risk: null-test tail timing.
33. **[LOW] `np.arange(n)` / `np.mod` index arrays rebuilt every effect every block** [voice_fx.py:356+](soundboard/voice_fx.py#L356) — cache `self._arange = np.arange(block_size)`. — effort **M**.
34. **[MEDIUM] Pitch shifter calls `np.cos` twice per block over n** [voice_fx.py:347](soundboard/voice_fx.py#L347) — precompute a Hann lookup table. — effort **M**.
35. **[LOW] Pervasive redundant `.astype(np.float32)` copies** even when already float32 [voice_fx.py:330+](soundboard/voice_fx.py#L330) — `copy=False` / `out=`. — effort **S**.

### 1.7 Person board (person_board.py)

> The earlier PERF rewrite (panel cache + `tkraise` + in-place collapse) is genuinely good. Two real gaps remain:

36. **[HIGH] No viewport virtualization** — `_expand_group_inplace` builds **every chip** in a group (CTkFrame + 2 CTkButton + CTkProgressBar + a PIL outlined-text render each), all resident. [person_board.py:1349](soundboard/person_board.py#L1349). A 200-sound person = ~800 widgets + 200 PIL renders on first expand. Port the main board's cull primitive. — effort **L** · risk: gate behind a `_virtualize` flag; respect collapse/expand + masonry height.
37. **[HIGH] `rebuild()` full teardown fires on every search keystroke** (sidebar *and* panel). [person_board.py:1111](soundboard/person_board.py#L1111) / [_on_search:2059](soundboard/person_board.py#L2059) — debounce ~150 ms (same fix as #20). — effort **S**.
38. **[MEDIUM] `circle_avatar()` re-reads the avatar from disk on every rebuild + every sidebar row, uncached** [person_board.py:618](soundboard/person_board.py#L618) — `lru_cache` on `(path, mtime, size)`. — effort **S**.
39. **[MEDIUM] Cached person panels never evicted** [person_board.py:1985](soundboard/person_board.py#L1985) — bound to last K=4–6 viewed (rebuild is already fast). — effort **M** · risk: evict via `PersonPanel.destroy()` so after-timers are cancelled.

### 1.8 Sound editor (editor.py)

> Waveform is a downsampled bar plot (right approach) and the playhead is correctly isolated. The cost is **redraw thrash**.

40. **[HIGH] Full `_draw_waveform` (delete-all + ~900 `create_line`) on every marker drag / scroll / zoom / pan — no debounce.** [editor.py:556](soundboard/editor.py#L556), driven by `<B1-Motion>` [editor.py:774](soundboard/editor.py#L774). Cache per-pixel bar amplitudes (depend only on data+zoom+view); on marker drag move only the two marker lines via `coords()`. — effort **M**.
41. **[MEDIUM] Canvas resize redraws the full waveform on every intermediate pixel** [editor.py:795](soundboard/editor.py#L795) — debounce with `after_cancel`/`after(60,…)` (the repo already has this "resize-defer" pattern elsewhere; this dialog never got it). — effort **S**.
42. **[MEDIUM] Synchronous audio load + full-file resample on the Tk thread when opening the editor** [editor.py:101](soundboard/editor.py#L101); the clone path [gui.py:10355](soundboard/gui.py#L10355) skips the existing `preloaded_audio` contract. Run `prepare_audio` (explicitly no Tk) in a worker. — effort **M**.
43. **[LOW] `LongAudioPicker._build_columns` does ~width separate `.max()` calls in a Python loop** [editor.py:1587](soundboard/editor.py#L1587) — vectorize with `np.maximum.reduceat`. — effort **S**.

### 1.9 Emoji picker (emoji_picker.py)

44. **[HIGH] No virtualization — every emoji in a category is a real `tk.Button` up-front (~385 for People & Body; only ~60 visible).** [_populate (emoji_picker.py:1086)](soundboard/emoji_picker.py#L1086). Measured cold open of the largest category = **~434 ms** main-thread block. The documented "async chunked `after()` loading" fix is **gone** from the current code. — effort **L** (virtualize) or **M** (restore chunked build) · risk: keep responsive column recompute + wheel + hover.
45. **[MEDIUM] Every category switch + search destroys/rebuilds all widgets** [emoji_picker.py:1086](soundboard/emoji_picker.py#L1086) — cache one frame per category (`tkraise`) or fold into virtualization. — effort **M**.
46. **[LOW] `_layout` re-grids all buttons + loops `grid_columnconfigure` over `range(64)` per column change** [emoji_picker.py:1154](soundboard/emoji_picker.py#L1154) — gate + debounce; bound the range. — effort **S**.

### 1.10 Data / config

47. **[MEDIUM] 55 % of the config is default-valued fields `to_dict` always serializes.** [models.py:35](soundboard/models.py#L35). `hotkey` null in 437/437, `loop`/`loop_count` default in 437/437, etc. Omitting defaults shrinks slot bytes 143 KB → 64 KB. `from_dict` already uses `.get(key, default)` so it's lossless + no migration. (The file is also **pretty-printed**, which roughly doubles its on-disk size.) — effort **S** · risk: always keep `name`+`file_path`.
48. **[LOW] Person sounds are full `SoundSlot` copies of main-board slots (100 % duplication) + every person carries all 21 empty groups (16 KB of placeholders).** [models.py:92](soundboard/models.py#L92). Skip empty groups now (zero behaviour change); reference-by-key later. — effort **M**.

### 1.11 Logging / infra — *cheap, high-value*

49. **[HIGH] Root logger at `DEBUG` captures all third-party spam into a 6 MB file.** [main.py:56-61](main.py#L56-L61). Breakdown: **`PIL.PngImagePlugin` ~49,400 lines** (one burst per PNG decode — icons, emoji, slot images, tray) + **numba JIT ~31,000 lines** (librosa) = **~80 %** of 100,819 lines. The app's own `[soundboard.*]` is only ~2,227 lines. Every PNG load + JIT compile does synchronous formatting + a disk write during exactly the image-heavy startup. **Fix:** root → `WARNING`, `setLevel(WARNING)` on `PIL`/`numba`/`pydub`/`filelock`, gate true DEBUG behind an env var. — effort **S** · risk: none (app diagnostics stay under their own logger).
50. **[MEDIUM] Plain append `FileHandler`, no rotation — log grows unbounded.** [main.py:56](main.py#L56) — use `RotatingFileHandler(maxBytes=1_000_000, backupCount=3)`. — effort **S**.
51. **[LOW] `_drop_log` opens/writes/flushes/closes `debug.log` per drag-drop step** — a second OS handle racing the logging handler, on the UI thread mid-drop. [gui.py:11252](soundboard/gui.py#L11252) — route through the standard logger. — effort **S**.
52. **[NEGATIVE/Good] No logging in any per-block/per-frame loop** — confirmed for `_output_callback`, `_input_callback`, `NoiseSuppressor.process`, `_animate_progress`, `_update_voice_level_meter`. The I/O storm is purely the third-party DEBUG capture (#49), so the fix never touches real-time code.

---

## 2. The three plans

The plans are **cumulative postures**, not mutually exclusive menus: A is the floor everyone should take; B re-engineers within the current stack; C changes the foundation. Each lists **pros / cons / benefits / problems** as requested. None of them break features when executed per the safety rules in §3.

---

### PLAN A — "Surgical Strike" (Quick Wins)

*Ship the highest-impact, lowest-risk fixes. No architecture change. Days, not weeks.*

**Scope (in priority order):**
1. **Logging** (#49–51): root→WARNING, silence PIL/numba/pydub/filelock, `RotatingFileHandler`, route `_drop_log` through the logger. *(Kills the 6 MB log + the per-PNG/JIT write storm.)*
2. **Splash-before-imports + lazy heavy imports** (#1, #6, librosa/scipy/pyrnnoise/soundcard/yt_dlp + `constants` emoji/colour): show the pure-Tk splash in `main.py` first, then import; defer optional libs to first use; set `NUMBA_CACHE_DIR`. *(Cuts ~4.4 s blank → ~1 s with a visible splash.)*
3. **Search debounce + image cache** (#20, #21, #23): ~120–180 ms debounce + `_load_slot_image` LRU + precomputed search blob. *(Kills the ~340 ms/keystroke freeze — biggest single felt-lag item.)*
4. **Slot redraw micro-fixes** (#8, #10, #17): hover = `itemconfigure` not full redraw; progress = persistent rect via `coords()`; shift+wheel drops the full repaint.
5. **Mousewheel dedup** (#15, #16): one dispatcher, cache `_app_is_active`, hoist `ctypes`.
6. **Async config save + default-omission** (#22, #47): move serialize/backup/write to a worker; `.bak` rename instead of `copy2`; `to_dict` omits defaults (config ~2.3× smaller).
7. **Warm-up trim** (#4): drop or scope the per-background-tab `update_idletasks()`.
8. **Editor resize debounce + person-board search debounce + avatar cache** (#41, #37, #38).

**Pros**
- Tiny diffs, mostly localized; each item independently shippable + testable.
- Hits the *actual* top complaints (startup, search, hover, scroll, log) head-on.
- Almost zero feature-regression surface; several are pure no-op-equivalence changes.
- Several wins are reusing patterns the codebase already proved (emoji-picker debounce, resize-defer).

**Cons**
- Doesn't raise the architectural ceiling — startup widget count and audio-thread lock contention remain.
- A grab-bag of small changes rather than one coherent story; needs discipline to land + verify each.
- The lazy-import work (#1) is "L" effort despite low risk — the most code of the set.

**Benefits (outcomes)**
- **Startup:** ~4.4 s blank → ~1 s to a visible, animated splash; window interactive much sooner.
- **Search:** typing becomes smooth (debounced, cached) instead of a per-keystroke freeze.
- **Everyday feel:** hover, scroll, and progress animation stop micro-stuttering on dense tabs.
- **Disk/log:** 6 MB → small rotating logs; no I/O churn during the snappy moments.
- **Config:** save hitch removed from the UI thread.

**Problems (risks & how to contain)**
- Lazy imports must preserve `*_AVAILABLE` detection + never import inside the audio callback (do it on stream-start / first-use, warm numba off-thread).
- Async save is data-critical: snapshot the dict on the UI thread, serialize one write at a time, keep atomic `os.replace` + synchronous shutdown flush.
- Hover/progress canvas changes must keep persistent item ids in sync on resize; keep a full-redraw fallback.

---

### PLAN B — "Architectural Tune-up" (Re-engineer within CustomTkinter)

*Everything in A, plus the structural changes that make the app stay lightning at 50+ tabs / thousands of slots / many people. 1–2 weeks. Stays on the current stack — no toolkit change.*

**Additional scope:**
1. **Truly lazy per-tab build** (#2): build the current tab + immediate neighbours eagerly, the rest on first switch via the existing `_prioritize_tab_build`. Decouples startup cost from tab/slot count. Pre-warm neighbours in `after_idle`.
2. **Off-thread image pipeline + on-disk thumbnail cache** (#3): worker decodes/resizes; main thread only builds `CTkImage`; persistent cache keyed by `(path, mtime, size)` so repeat launches + density changes skip decode. Visible-slots-first.
3. **Range-diff culling + memoized row minsizes** (#12, #18): O(visible) instead of O(slots) per scroll settle.
4. **SlotWidget redraw coalescing + drop the per-slot footer CTkLabel** (#9, #11): one redraw per logical update; footer drawn on the canvas (or lazy) → ~halve surviving widget count.
5. **Audio callback hardening** (#25–30): snapshot-then-mix outside the lock + command-queue mutations; pre-allocated scratch buffers; RNNoise ring buffer; int16 + LRU cache; throttled metering.
6. **Voice-FX allocation cuts** (#32–35): cached `arange`, slice-based reverb, Hann LUT, `copy=False`.
7. **Person board virtualization + bounded panel cache** (#36, #39).
8. **Emoji picker virtualization (or restored chunked build)** (#44, #45).
9. **Editor amplitude cache + off-thread load** (#40, #42, #43).
10. **Config: orjson load + reference/empty-group trimming for persons** (#48).

**Pros**
- Attacks the *causes* (widget count, lock contention, eager build) not just symptoms — the gains hold as the user's board grows.
- Reuses proven in-repo patterns (virtualization, debounce, panel cache, resize-defer) ported to the places that lack them (person board, emoji picker, editor).
- Bounds memory (int16 + LRU + evictable panels) and removes audio-thread/GUI serialization → fewer dropouts under load.
- Still 100 % CustomTkinter → RTL, emoji, tray, hotkeys, drag-drop all keep working untouched.

**Cons**
- Touches the **real-time audio threading model** (#25) — the single most delicate change; needs careful, test-driven work.
- Larger surface area → more verification; several items are "M/L" effort.
- Hits a ceiling: even one CTk widget re-runs `_draw()` on `configure()`; you can reduce calls, not the per-widget floor.
- Off-thread image + thumbnail cache adds a cache-invalidation contract to maintain.

**Benefits (outcomes)**
- Startup time becomes ~constant regardless of how many tabs/slots are configured (lazy build).
- Dense tabs, person pop-outs, the emoji picker, and the editor all become viewport-bounded → smooth at any size.
- Lower, bounded RAM; steadier audio latency (safe to lower block size for less Discord delay).
- Repeat launches skip image decode entirely (thumbnail cache).

**Problems (risks & how to contain)**
- **Audio refactor:** make the audio thread the sole owner of `currently_playing`; route stop/volume/pause through a command queue; publish an immutable snapshot for the GUI; A/B with the built-in test-output/record tools; add mainloop+after tests.
- **Lazy build:** anything that scans "all slots" (global search, hotkey registration) must read the **data model**, not live widgets — verify those paths first.
- **Recycling is explicitly out of scope** here (XL, high-risk); culling already caps painted widgets. Only consider it if a single tab grows to thousands of slots.
- Virtualizing the person board must respect the masonry height estimate + collapse/expand state.

---

### PLAN C — "Foundation" (Step-change / long-horizon)

*The ceiling-raisers. Two independent sub-paths; pick by appetite. Both keep the audio + global-hotkey core as a GUI-agnostic engine.*

#### C1 — One `tk.Canvas` per **tab** (near-term step-change, still Tkinter) — effort **M–L**
Fold the N per-slot `SlotWidget` canvases of a tab into **one** `tk.Canvas` that draws all slots as canvas items (`create_rectangle`/`create_text`/`create_image`). Hit-testing via tags + `<Button>`/`<Motion>`; hover/press = recolour a couple of items; scrolling uses the Canvas's native `yview` (no `grid()`/`grid_remove` churn). The logical next step of the work `slot_widget.py` already started.

- **Pros:** removes Tk's geometry manager from the hot path entirely (a tab = **1 widget**, so tab-switch/resize `<Configure>` work collapses to O(1)); scrolling is native canvas viewport; reuses the existing colour/font/proxy helpers; lowest feature-risk of any "big" move — **nothing leaves the Tk process**, so audio/hotkeys/tray/drag-drop/RTL/emoji are untouched.
- **Cons:** you hand-build every per-slot interaction (hover, press, right-click hit zones, in-canvas rename overlay); text/emoji metrics, ellipsizing, and RTL alignment computed manually per item; image items need persisted `PhotoImage` refs; doesn't raise the ceiling beyond Tk's single-threaded Tcl loop.
- **Benefits:** the remaining geometry-manager bottleneck disappears; best effort-to-payoff if a full rewrite is off the table.
- **Problems:** re-implementing per-slot interaction is where regressions hide — port click/menu/stop/drag-drop/rename one at a time behind a feature flag, keep the current per-slot path as fallback until parity is proven.

#### C2 — Migrate the GUI to **PySide6** (decade rebuild) — effort **XL**
Rebuild the front end on Qt for Python (LGPL). Render the grid as a `QListView`/`QGraphicsView` in IconMode with a custom `QStyledItemDelegate` over a `QAbstractListModel`; style with QSS. Keep audio/hotkeys/tray as a **headless engine** the Qt UI drives. **Phased: headless-engine-first, not a big-bang cutover.**

- **Pros:** categorical step-change in the exact bottleneck — model-view renders "millions of rows in a single widget"; off-screen items aren't widgets at all (no hand-rolled culling). **Native RTL/bidi** (`layoutDirection` + automatic natural-direction alignment) would **permanently retire the recurring Hebrew bug** (see [docs/HEBREW_RTL.md](docs/HEBREW_RTL.md)). LGPL fits the commercialization plan; native `QSystemTrayIcon`, `QMimeData` drag-drop, and `QThread` integrate cleanly with a real-time engine.
- **Cons:** XL rewrite of the ~15k-line GUI — every dialog, the editor, person_board, color_picker, emoji_picker re-authored in Qt idioms (only the audio/hotkey core ports directly); full-colour emoji may need the existing `emoji_render` image path as delegate-painted `QPixmap`; larger installer (Qt DLLs); steeper API (signals/slots/models/delegates).
- **Benefits:** a genuinely fast, professional, commercial-grade app whose architecture is the textbook answer to "huge grid of slots across many tabs"; RTL whack-a-mole ends for good.
- **Problems:** long migration window = regression risk; mitigate with the phased headless-engine-first plan, a feature-parity checklist, and keeping the Tk app shippable until Qt reaches parity. `windnd` Windows drag-drop is replaced by Qt DnD (re-plumb + validate Explorer drops).

#### Explicitly rejected foundations (research verdicts)
- **Dear PyGui — disqualified.** No RTL/Hebrew (issue #320) and **cannot render Unicode > U+FFFF** (issue #1092) where most emoji live. Both are mandatory here. Best raw FPS, wrong tool.
- **Flet / webview — poor fit.** UI runs in a separate Flutter process behind IPC; every progress-bar/VU-meter update crosses a process boundary (latency exactly where this app is hot), and global hotkeys / tray / native drag-drop aren't first-class.
- **Kivy — weak.** Incomplete RTL (needs bidi + reshaper hacks; TextInput RTL unfinished). **Toga — immature** for a feature-dense production app.

**Plan C as a whole**
- **Pros:** the only paths that change the performance *ceiling*; C2 also future-proofs the app and solves RTL natively.
- **Cons:** C1 trades widget conveniences for manual canvas work; C2 is a multi-week rewrite.
- **Benefits:** C1 = remove the last Tk geometry bottleneck cheaply; C2 = a renderer that simply doesn't have this class of problem.
- **Problems:** highest regression risk of the three plans; must be gated behind feature flags / phased parity so the shipping app never breaks.

---

## 3. Cross-cutting safety rules (so we don't break shit)

1. **Feature-parity checklist before/after each change:** mic passthrough + mix, per-slot volume/speed/pitch-preserve, global hotkeys, PTT (press/debounced-release/force-release), tabs (create/rename/delete/reorder/move-between), edit/move mode, search, recording + quick-record, voice changer + 17 presets, noise suppression, person hub + pop-outs, editor (trim/zoom/multi-cut), emoji/image pickers, color picker, RTL/Hebrew, tray, drag-drop, YouTube→MP3, monitoring, auto-start.
2. **Never bind to CTkButton internal canvases** (wiped by `_draw()`); `command=` for clicks, `CTkButton.bind` for motion/release only. (Per the project's hard-won rule.)
3. **Per-tab widget updates** must use `tab_slot_*[tab_idx][slot_idx]`, never the legacy current-tab aliases, for cross-tab updates.
4. **Audio callback stays allocation-light and never does first-time imports or blocking I/O.** Any lazy import happens on stream-start / first feature use, with numba warmed off-thread.
5. **Config writes stay atomic** (`os.replace`) with the existing `_config_loaded_ok` / non-empty-tabs guards and a synchronous shutdown flush, even when moved off-thread.
6. **RTL stays display-natural** — never pre-reverse text (see [docs/HEBREW_RTL.md](docs/HEBREW_RTL.md)); any new canvas-drawn text must keep Label/Canvas BiDi behaviour.
7. **Land behind flags + verify in the real app** (use `/run` and `/verify`): one finding per commit where possible, with an A/B for any audio change.

---

## 4. Recommended sequencing (master backlog)

| # | Fix | Impact | Effort | Risk | Plan |
|---|---|---|---|---|---|
| 49 | Logging: root→WARNING, silence PIL/numba, rotation | High | S | None | A |
| 20 | Search debounce | **Critical** | S | Low | A |
| 21 | `_load_slot_image` LRU cache | High | S | Low | A |
| 1 | Splash-before-imports + lazy librosa/scipy/pyrnnoise | **Critical** | L | Low | A |
| 6 | Lazy `emoji_data_python` + `colour` in constants | Medium | S | Low | A |
| 10 | Progress bar → `coords()`/`itemconfigure()` | High | S | Low | A |
| 8 | Hover → `itemconfigure`, not full redraw | High | M | Low | A |
| 15/16 | One mousewheel dispatcher + cache `_app_is_active` | High | M | Low | A |
| 17 | Shift+wheel: drop full slot repaint | Medium | S | Low | A |
| 22 | Async / off-thread config save + cheaper `.bak` | Medium | M | Med | A |
| 47 | `to_dict` omit default fields | Medium | S | Low | A |
| 4 | Trim per-tab `update_idletasks` warm-up | Medium | S | Low | A |
| 41/37/38 | Editor resize debounce · person search debounce · avatar cache | Med | S | Low | A |
| 2 | Truly lazy per-tab build | High | M | Med | B |
| 3 | Off-thread image decode + thumbnail cache | High | M | Med | B |
| 9/11 | Redraw coalescing + drop footer CTkLabel | High | M | Med | B |
| 25 | Audio: snapshot-then-mix, command-queue mutations | High | L | Med | B |
| 26/29/30 | Pre-alloc scratch · RNNoise ring · throttle meter | Medium | M | Med | B |
| 27 | int16 cache + LRU eviction | Medium | M | Low | B |
| 12/18 | Range-diff culling + memoized minsizes | Medium | M | Low | B |
| 36/39 | Person board virtualization + bounded cache | High | L | Med | B |
| 44/45 | Emoji picker virtualization / chunked build | High | L | Med | B |
| 40/42 | Editor amplitude cache + off-thread load | High | M | Low | B |
| 32-35 | Voice-FX allocation cuts (reverb/arange/Hann/astype) | Medium | M | Low | B |
| 48 | Person sounds: skip empty groups / reference model | Low | M | Med | B |
| C1 | One canvas per tab | High | M–L | Med | C |
| C2 | PySide6 headless-engine-first migration | High | XL | Med | C |

**Suggested path:** do **all of Plan A** first (it removes the felt lag the user is actually complaining about, at near-zero risk), measure, then take **Plan B** items in backlog order. Treat **C1** as an optional later step-change and **C2** only as a deliberate commercial-grade rebuild decision.

---

## 5. Appendix — measured facts grounding this plan

- **Config:** 18 tabs · **437 slots** · 10 persons. 330 KB on disk is mostly **pretty-printing** (indent=2 ≈ 2×) + ~55 % default-valued fields; no base64 — images are external paths. `json.load` is only ~4 ms (parsing is *not* the bottleneck; the downstream widget build it feeds is).
- **`debug.log`:** 6,123,008 bytes / 100,819 lines; ~49,400 PIL lines + ~31,000 numba lines = ~80 % third-party; app's own `[soundboard.*]` ≈ 2,227 lines.
- **Imports (fresh):** `sounddevice` 2.6 s, `scipy.signal` 0.8 s, `pyrnnoise`/audiolab 0.44 s, `emoji_data_python` 35 ms, `colour` 7 ms — all paid before first paint today.
- **Search:** ~70 results for a single common letter; destroy+rebuild ≈ **340 ms/keystroke**, no debounce.
- **Images:** current tab 7 imgs = 34 ms; all 220 = **~963 ms** of main-thread LANCZOS decode during build.
- **Save:** `to_dict`+dumps ~1.8–2.3 ms, `.bak` copy ~1.2 ms, write+replace ~8.3 ms → **~12–26 ms** on the Tk thread per flush (debounced 400 ms, 50+ call sites).
- **Already shipped (baseline):** single-canvas `SlotWidget`; row virtualization/occlusion culling; resize `_draw()` suppression + chunked sweep; O(1) tab switch; debounced cull; global wheel dispatcher; debounced atomic config save; huge-file streaming; cached voice-FX biquad.

*Full per-subsystem findings (evidence quotes, exact line refs, effort/risk for all 52 items) were produced by the audit and condensed in §1 above.*
