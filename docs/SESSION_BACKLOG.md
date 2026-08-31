# Session Backlog & Open Items

> **Date:** 2026-06-09 (updated 2026-08-31)

---

## 2026-08-31 (evening) — UI polish (preview/search/popup/dialogs) + DJ Looper reimagined

**Preview parity (bug).** Local preview ignored a slot's speed/pitch — you auditioned a
different sound than Discord heard. New module-level `audio.apply_speed(...)` is the ONE
transform used by both the Discord path and preview; `_render_preview_audio` /
`_start_preview_render` (gui.py) render at slot speed × master volume + soft-clip, a slow
librosa stretch runs off-thread with a generation counter so a superseding preview/stop
drops it. Covers main-board AND People previews. Returns the rate-adjusted duration so the
progress strip is right.

**Search overlay (bugs).** (1) Results showed the file basename, not the slot NAME — a slot
named "Trexon Key" read as `yt_KWvtoxjrBPw_0fa…`. `_paint_search_slot` now uses `slot.name`
(falls back to the file stem only when unnamed). (2) A preview started from the search
overlay had no way to stop — the overlay slot now shows the ⏹ button + a green progress bar
while previewing, not only while playing.

**Quick-edit popup restyle.** The slot ⋯ → quick popup (the "amateur"-looking one) was a
ragged 380 px, 3 button rows, 120 px sliders. Rebuilt at 424×262 on a grid: header + ✕,
Volume/Speed sliders with live value read-outs (`65%`, `1.00×`) + reset buttons, Preserve
pitch / Loop checkboxes, a divider, an even action row (✓ Apply green / Edit / Clone / 🗑),
and a quiet outlined file-utility row (Show file / Copy file / Copy path / Suno). Uses the
app's `UI`/`CHECKBOX_KW`/font tokens; geometric glyphs that didn't render (⧉) dropped.

**Dialogs opened in ~5 s (perf, root cause).** CustomTkinter's `_windows_set_titlebar_color`
does `withdraw(); update()` inside EVERY CTkToplevel constructor — that `update()` is a full
nested event pump that ran the entire pending backlog (e.g. the People pre-warm building
~1,800 widgets) *inside* the popup's `__init__` (measured 5.0 s popup, 6.4 s configure
dialog). `ctk_patches.py` now shadows `update`→`update_idletasks` for the duration of that
call, and repaints each toplevel once mapped after CTk's withdraw/deiconify cycle
(`redraw_window` moved into ctk_patches, shared with person_board). Popup/dialog opens are
now fast; blank-label renders under a concurrent prewarm storm were proven a harness
artifact (crisp with prewarm off; the user's own screenshots render fine).

**DJ Looper — reimagined (design) + 2 live bugs fixed.** A 4-concept / 3-judge / 36-agent
workflow reviewed the panel and picked (unanimous, 147/180) **"Air Deck"**: an always-on-top
canvas deck for the ONE long/looping sound, global ctrl+alt hotkeys as the real interface,
short memes get no UI; engine gains fades / seek-scrub / A-B loop / bed-duck / a clean event
stream; the 1,500-line `NowPlayingPanel` is deleted. Full implementable spec (engine API,
mockups, hotkey table, 4 parallel work packages, test plan, risks) in
[docs/DJ_AIR_DECK_PLAN.md](DJ_AIR_DECK_PLAN.md); pitch artifact:
https://claude.ai/code/artifact/6b2dcc7a-a10f-4497-9e32-94eabbd8a63d . **Awaiting the user's
A/B call (full floating deck vs docked strip) before the big build.**
Two HIGH bugs the diagnosis found are ALREADY FIXED at the engine (so every surface benefits):
- **Pause→Resume went silent to Discord**: the PTT release countdown ignores paused sounds,
  so pausing released the F9 key ~300 ms later; `resume_sound`/`restart_sound` flipped the
  flag but never re-pressed → the resumed sound hit the cable with Discord not transmitting.
  Both now `_press_ptt()` + reset the countdown (no-op when there's no PTT key / it's held).
- **DJ cards labelled with the file hash, not the slot name** (518/518 mismatch). `play_sound`
  now threads `display_name` + `emoji` → stored in the sound entry → returned by
  `get_playing_sounds`; every play site passes `slot.name`/`slot.emoji`; the card shows
  `emoji + name` (RTL-safe).
- 🧪 `test_dj_engine.py` (5 tests): resume/restart re-press PTT, unknown id is safe,
  get_playing_sounds exposes name+emoji, signature guard.
- ✅ **WP-A step 1 (engine) DONE + tested.** `AudioMixer` gained clickless fade
  stop/pause/resume (`fade_out_sound` / `pause_sound(fade_ms)` / `resume_sound(fade_ms)` /
  `stop_sound(fade_ms)` / `stop_all_sounds(fade_ms)` — `fade_ms=0` = today's exact behaviour),
  `seek_sound` (works while paused), `handle_retrigger` (layer/restart/toggle) + instance cap,
  `set_effective_rate` (honours a slot's baked speed), a `pop_events()` stream
  (started/ended/loop/paused emitted by the callback), and `on_air_state()` / `get_sound_peaks()`.
  Render-path math untouched behind a fast-path guard (a steady 1.0 sound runs byte-identical —
  asserted). `test_deck_engine.py` = 13 tests driving `_output_callback` directly.
  NEXT: WP-A step 2 (A-B loop + bed-duck), then WP-B deck UI (float + dock), WP-C wiring/deletion.

## 2026-08-31 (later) — People window "smudge" root-caused + UI consistency pass

User: "check, test and fix the smudginess when opening People and using it; make the
font/border of people look better, it's hard to read; and make all the buttons / ticks /
bars coherent — some places got messy."

Method: a visual harness (`scratchpad ui_harness.py`: launches the app from source in an
ISOLATED CWD — copied config with auto-start/PTT/hotkeys off, junctions to sounds/images —
opens People and screenshots it over time), a main-thread stall profiler with 50 ms stack
sampling, and a 5-reader review workflow with pixel-level probes. NOTE: the harness windows
land on the user's saved geometry, i.e. exactly on top of the running PROD windows — park
them elsewhere (`window_geometry` / `person_windows` in the copy) or the grabs blend both.

What was actually wrong (all measured, none of it was "blur"):
- ✅ **Stale pixels after deiconify.** Widgets created/recoloured while the hub was WITHDRAWN
  (the startup prebuild, pre-warmed panels) came up on screen with the pixels they had at
  creation — chips as bare colour slabs with dark boxes where the label sits, EMPTY sidebar
  rows, the selected row as dark squares — although every widget's Tk state was correct
  (probed: label fg/bg/image all right). One Win32 `RedrawWindow(RDW_INVALIDATE |
  RDW_ALLCHILDREN)` repaints everything from that state (~200 ms). `reopen()` now
  reconciles BEFORE `deiconify()`, maps with `-alpha 0`, reveals on the first `<Map>` /
  `<Expose>` (700 ms timer as safety net) after a synchronous repaint, then `-alpha 1` — no
  white frame, no stale frame. `select()` repaints synchronously the first time a panel is
  exposed. (`person_board._redraw_window`, `_after_reopen`, `_reveal`.)
- ✅ **Every first open rebuilt the whole board.** `_win_width()` for a withdrawn window fell
  through to `winfo_width()` (Tk reports the 200-px placeholder for a never-mapped
  CTkToplevel) → the prebuilt panel was laid out for ~133 logical px (1 column,
  `_last_cols=1`), and the real `<Configure>` on map triggered `_reflow_when_settled` → full
  teardown + rebuild (~1 s of destroys + seconds of streaming) on EVERY open. Now the
  unmapped branch uses CTk's `_current_width` (set by its geometry() setter) →
  `_lsb_req_w` → 900. Measured: open 349 → **~30 ms**, first person switch 1128 → **~70 ms**,
  no COLS DRIFT.
- ✅ **CustomTkinter's own nested idle pumps.** `CTkScrollbar._draw` and
  `CTkOptionMenu._draw` end with `update_idletasks()`; `CTkScrollableFrame` binds every
  inner `<Configure>` to `configure(scrollregion=bbox("all"))` → `scrollbar.set()` →
  `_draw()` → `update_idletasks()`. Profiled: `CTkScrollbar.set` cost 170–670 ms PER CALL
  (2.6–3.0 s per panel build), one `CTkOptionMenu._draw` 2.06 s. New
  `soundboard/ctk_patches.py` (gui "perf patch #5"): no idle pump inside those draws,
  change-only scrollbar redraw, 40 ms-debounced scrollregion refresh. The remaining
  1–3 s stalls are intrinsic CTk widget construction (~4 CTk widgets per chip); the pump
  streams them, and **prewarm now pauses while the hub is visible** so background panel
  builds can no longer freeze the hub while it is being used.
- ✅ **Readability.** Person names and group headers were PIL bitmaps with black+white
  hairline rings, downscaled ~2:1 by CTkImage — a grey fuzzy aura, 2 px of true colour per
  stem; chip titles had a 3-device-px black stroke that filled every letter counter
  ("bubble letters"); every emoji/avatar was rasterised at LOGICAL px and bicubic-UPSCALED
  1.5x. Now: names/headers are native ClearType (white, `size_md`), identity colour is a
  4 px pill (+ avatar), the panel title is always white; chip labels are rendered at exact
  DEVICE pixels (13 logical → 20 px, 2 px edge, no white ring) and wrapped in a CTkImage
  whose logical size is `raster/scaling` so CTk never resamples; `emoji_image(…, scaling)`
  / `circle_avatar(…, scaling)` build at device px (AA rim). `constants.is_light_color`
  now uses WCAG relative luminance (blurple/red/purple/pink → white glyphs, was dark) and
  `_paint_chip` recolours the ⋮ + progress for the playing/preview colours.
- ✅ **Sidebar rows overflowed**: avatar 28 + name button (CTk min width 140) + ⋮ 26 > 176
  → the ⋮ rendered 10 px wide or unmapped. Name button `width=40`, ⋮ 28 px, row 36 px.
- ✅ **Consistency pass** (agents, per the converged style spec): tokens
  `UI[control_height=28 / toolbar_height=32 / compact_height=24 / footer_button_height=36
  / icon_button=28]` (even device px at 150 % — 26/30/34 left 1 px strips), font tokens,
  `DROPDOWN_KW/ENTRY_KW/CHECKBOX_KW`, green = confirm/create, blurple = selection, icon
  buttons 28x28, checkbox labels de-emoji'd (hints → tooltips), single-spaced card titles,
  status bar right cluster packed first + truncated device names with tooltips, tab emoji
  and slot thumbnails/emoji at device px (thumbnails decoded off-thread, aspect kept),
  colour picker / splash / emoji picker / editor / QR at device px, footer buttons 36 px.
  `UI["slot_scale_geometry"]` (default False) keeps the main-board tile height/text the
  user is used to; True = DPI-correct taller tiles.
- 🧪 `test_people_ui.py` (12 tests, real withdrawn CTk windows, after()-stepped): prebuilt
  layout width, reopen reveals (state/alpha/via/latency), incremental `refresh_people`,
  row shape, device-exact rasters, WCAG contrast rule. All other suites still pass.
- ⏳ Not smoke-tested by the user yet (they were live on a call during the session; the
  deploy could only stage). Follow-ups: masonry re-grid on resize without destroy
  (resize still rebuilds), canvas-per-panel chips (the remaining per-widget floor), the
  board grid overflowing narrow windows (pre-existing).

---

### Adversarial review of the pass (same day) — 14 confirmed findings → 8 fixes

36-agent review (6 lenses × finder → 2 independent refuters each, Tk probes on this
machine). 15 raw findings, 14 confirmed, 1 refuted (DPI factor sampled once — cosmetic
only, and slots don't scale by default). All fixed; `test_people_ui.py` is now 12 tests.

- ✅ **`reopen()` on an already-visible hub / pop-out / Favorites window blanked it for
  ~0.7 s** — a regression from the reveal state machine: `deiconify()` on a MAPPED window
  emits no `<Map>`/`<Expose>`, so alpha stayed 0 until the 700 ms safety timer (invisible
  window holding focus). Both `reopen()`s now short-circuit when `winfo_viewable()`
  (lift/focus only; pop-outs re-assert topmost) and return while a reveal is in flight.
- ✅ **Zero-people hint was `pack()`ed where the panels are `grid()`ed** → the first
  `select()` after it raised TclError ("cannot use geometry manager grid inside … pack"):
  the Add-person dialog stayed open and the panel never appeared; with the persistent hub
  the same hit `reopen()` after adding someone from the recording flow. Hint is now
  `grid()`ed and `_clear_empty_hint()` runs before the first panel is built;
  `reopen()`'s `_select_initial()` branch also flips `_showing=True` like the cached one.
- ✅ **`RedrawWindow(RDW_UPDATENOW)` is NOT a synchronous Tk paint**: Tk turns each
  WM_PAINT into a queued `<Expose>` and repaints Frames/Canvases in idle callbacks, so
  alpha was flipped before any repaint ran. `_after_reopen`/`_reveal` now flip alpha from
  a nested `after_idle` (outer runs before the display procs, inner in the next idle
  pass); the 700 ms timer still reveals regardless (`_lsb_reveal_armed`). Reveal: 60 ms.
- ✅ **Slot thumbnail poll was armed on a transient SlotWidget**: tkinter deletes a
  widget's pending `after` commands on destroy, so a Columns ± / tab delete / search
  re-render inside the 40 ms window left `_poll_scheduled` stuck True for the session (no
  thumbnail ever delivered again, queues growing). The poll now lives on the Tk root and
  self-heals if the host dies; the old "a destroyed one raises" comment was backwards.
- ✅ **Slot ⋯ menu: `self._fix_rtl_text` doesn't exist** (it's a module function) → once
  a Favorites folder existed the loop raised AttributeError, the bare `except` swallowed
  it, and every item after "Add to Queue" silently vanished. Fixed; AttributeError logged.
- ✅ `root.update()` / `top.update()` in the two Copy-path handlers removed (banned
  re-entrant pump; Tk owns the clipboard as soon as `clipboard_append` returns).
- ✅ `_build_person_row` records `row._lsb_sel`, so the post-rebuild reconcile really
  skips unchanged rows (every row was reconfigured + redrawn after each rebuild).
- ✅ Edit person/group dialog icon + avatar previews rasterised at device px too.
- ✅ **PIL BiDi depends on a fribidi DLL Pillow doesn't ship** — on this PC raqm only
  works because Meld/Tesseract put `libfribidi-0.dll` on PATH; anywhere else Hebrew chip
  labels would render MIRRORED (BASIC layout: no BiDi, no error). `_RAQM_OK` probe at
  import; RTL names fall back to native Tk text (which BiDis correctly) without raqm.
- 🧪 New tests: reopen-while-visible keeps alpha 1 / no pending state; first person after
  the empty hint (grid manager, select OK); thumb poll survives its widget's destroy;
  static guard against `self._fix_rtl_text`.

---

## 2026-08-31 — Mic noise suppression: works in the EXE again, strength bug fixed, 5 engines

User: "look again at the sound filter (background sounds removal for mic) — make it work,
improve it, add alternatives."

- ✅ **ROOT CAUSE #1 — RNNoise never worked in the EXE.** `from pyrnnoise.rnnoise import …`
  executes `pyrnnoise/__init__.py` first, which imports `audiolab → av` (libav). `audiolab`
  was never in the bundle, so in prod `RNNOISE_AVAILABLE` was False: with the saved config
  (`backend: rnnoise`, strength 100, enabled) the mic passed through **RAW**, and the lazy
  loader retried + wrote a warning to debug.log on EVERY audio block — 36,351 lines across
  the four rotating logs, i.e. disk I/O inside the PortAudio callback. Dev worked (the venv
  has audiolab), which is why it was never caught. Fix: `audio.py` binds `rnnoise.dll`
  directly via ctypes (`_load_rnnoise`; candidates `_MEIPASS/pyrnnoise/` and the package dir
  found with `find_spec` WITHOUT executing it) — `pyrnnoise` is never imported. The spec now
  excludes `pyrnnoise/audiolab/av` (−65 MB of `av.libs`) and bundles only the DLL. A test
  simulates the frozen layout (package unfindable, DLL only under `_MEIPASS`).
- ✅ **ROOT CAUSE #2 — DeepFilterNet at the GUI default strength (85) sounded doubled /
  robotic.** The June mapping drove the ONNX `atten_lim_db = (1−s)·40`. Measured on real voice
  clips, this export blends the raw input back **misaligned with its 30 ms lookahead** for any
  finite limit (output delay jumps 1440 → 1920 samples): clean-speech SI-SDR **21.8 dB at
  limit 0 vs 0.3 dB at limit 6 (= strength 85)**, 6.3 at 10, 17.3 at 20. Fix: the model always
  runs unlimited; strength is a **delay-compensated wet/dry mix** in `NoiseSuppressor` (dry
  path delayed by the engine's measured latency — DFN/Max 1440, RNNoise 960, Classic 480,
  Gate 0 samples; RNNoise's old mix was misaligned too), residual dB-linear (−40 dB·s). At
  0.85 clean speech now measures 21 dB (was 0.3).
- ✅ **Engine builds moved off the audio thread + one-shot fallback.** Before: the ONNX
  session (~150 ms) + warm-up was built lazily INSIDE the input callback, and a failed engine
  was rebuilt on every block. Now: background builder thread with a generation counter (stale
  builds discarded), failures remembered with a human note, fallback routes
  (`_NS_FALLBACKS`: best → classic → rnnoise → gate), explicit dropdown pick = retry.
  `process()` never builds; it passes through until an engine lands.
- ✅ **Denoising runs on a dedicated mic-processing thread** (`AudioMixer._ns_worker`, Pro
  Audio MMCSS / highest priority): the input callback hands raw blocks to a queue and returns;
  blocks keep routing through the worker until it has drained after NS is switched off, so no
  block can overtake another (tested). The NS-off path is byte-identical to before.
- ✅ **New engines** (`NS_BACKEND_LABELS`, labels shared with the GUI):
  **Max** = DeepFilterNet + residual gate keyed on the model's own `lsnr` output (open > −7 dB,
  close < −12 dB, ~160 ms release, floor −10…−25 dB) → pauses ~25 dB quieter than plain DFN for
  ≤ 0.2 dB speech cost. **Classic** = MCRA noise tracking + decision-directed Wiener gain,
  960/480 sqrt-Hann OLA, gain floor −8…−32 dB, 3-bin smoothing, 1 s warm start — no AI,
  0.2 ms/block, 20 ms total latency, the most transparent voice (clean-speech SI-SDR 15–48 dB
  on the People clips). **Gate only** (the expander, now exposed; its detector "high-pass" was
  an 8-sample box = ~3 kHz cutoff, so broadband hiss closed it ON speech — now 128 samples ≈
  165 Hz; floor −6…−36 dB; floor seeds from the first frame and rises faster while closed).
  Plus **Low-cut 80 Hz** (2nd-order Butterworth, `noise_suppression_lowcut`) ahead of any engine.
- ✅ **GUI**: 5-engine dropdown built from the audio catalogue (only engines that load on this
  machine), low-cut checkbox, a **live status line** ("✓ Best (DeepFilterNet) active ·
  7.8 ms/block · 40 ms delay", fallback notes in yellow, ⚠ heavy-CPU hint) polled once a
  second while Audio Options is open; the card subtitle no longer claims NS is "bypassed when
  using a virtual cable" (it never was).
- 📊 **Objective A/B** — real Discord voice clip (`sounds/discord_20260531_093144…`), 4 s
  noise adaptation, strength 1.0; SI-SDR gain vs the noisy input at 5 dB SNR / steady-noise
  attenuation: DFN white **+9.0** / −47 dB, keyboard **+14.6** / −58, other voice **+4.6** /
  −48, fan +8.7 / −23; Max: same speech, pauses −72 / −83 / −52 dB; Classic white +3.8 / −27,
  fan +3.0 / −20, keyboard +1.3 (not its job); RNNoise white +3.4 / −35, fan +6.2 / −46,
  keyboard +9.3 / −20. Cost per 21 ms block: DFN/Max ≈ 7.5 ms, RNNoise 2.5 ms, Classic 0.2 ms.
  Evaluation gotcha: the `recording_…` call-recording clips are NOT clean speech (DFN's own
  lsnr median −3.8 dB) — they made every DNN look broken until a real voice clip was used.
- 🧪 `test_noise_suppression.py` — 17 headless tests: DLL loader never imports pyrnnoise,
  frozen-EXE simulation, ring = exact 480-sample delay, one-shot fallback + no log spam, async
  build, worker order/drain, declared-vs-measured latency, partial-strength fidelity on real
  speech, residual law, low-cut, catalogue consistency. `test_soundboard.py` +
  `test_mobile_sync.py` still pass.
- ⏳ **Not smoke-tested live** (the EXE was running during the session, so the deploy could
  only stage). Next launch: open ▶ Audio Options → the status line should read
  "✓ Light (RNNoise) active · … ms/block" with the saved config; then try Best / Max at the
  default 85 and Classic for fan/PC noise.

---

## 2026-08-25 (later) — right-click: Copy full path + Prep for Suno upload

- **📄 Copy full path** on the slot ⋯/right-click menu AND the People/Favorites chip menu.
  Stored paths are relative (`sounds\x.wav`), so it resolves to an ABSOLUTE path — what's
  actually useful in Explorer/terminal/chat. Copies even when the file is missing (that's
  when you most want it) and says so in the status bar. Person boards can reference files
  outside `sounds/` ("Add from file…"), hence resolve rather than assume.
- **🎼 Prep for Suno upload**: re-encodes the sound to a bare PCM_16 WAV in
  `Desktop\Suno upload\`, named Suno-safe. Rationale (user-supplied): Suno's copyright
  false-positives are triggered by embedded cover art / ID3 tags, and WAV avoids them.
  **Verified at chunk level** — the export contains ONLY `fmt ` + `data`, no LIST/ID3/iXML;
  re-decoding through soundfile makes surviving metadata structurally impossible. (A naive
  byte scan "finds" JPEG markers in raw PCM — coincidental sample bytes, not art.)
  Naming keeps letters/digits in ANY script so Hebrew titles stay readable, strips
  brackets/quotes/punctuation, collapses to dashes, caps at 60 chars, falls back to
  `suno-upload`; collisions get `-2`, `-3`. Desktop is resolved via the shell known-folder
  API (OneDrive-redirect safe; verified → `C:\Users\User\Desktop`). Runs on a worker
  thread; refuses >12 min with a "trim it first" message (float32 @48k is ~23 MB/min, and
  Suno only takes short uploads anyway). Reveals the folder in Explorer once per session.
- Both actions exposed on the People boards via a new `PersonContext.export_suno` hook.

---

## 2026-08-25 — YouTube downloads fixed + 3-action Hub commands

User hit two ⬇ Web errors: "Could not copy Chrome cookie database (yt-dlp #7271)" on probe,
then "[youtube] v4KDY5iIK2c: This video is not available".

- ✅ **Root cause of "not available" = STALE yt-dlp** (2026.3.17, five months old). Updated to
  2026.8.19 and the user's exact failing video ID now resolves **with no cookies at all**
  (verified: title + 62s duration extracted). YouTube routinely breaks old releases and
  reports it as an availability error — misleading but not a bug in our code.
- ✅ **Cookie errors are no longer fatal:** new `_ydl_run()` helper wraps probe AND download —
  if reading BROWSER cookies fails it retries once without them (most videos need none), and
  the status line says so. `_is_browser_cookie_error()` classifies by message; unit-checked
  against 7 real strings incl. both of the user's errors (True) vs. genuine failures like
  "video is not available" / bot-check / 403 (False, so they still surface). Chrome/Edge
  v127+ app-bound encryption means those cookies can NEVER be read — cookies.txt or Firefox
  stay the path for age-restricted content; the dialog's warning now says exactly that.
- ✅ `requirements.txt` pins `yt-dlp>=2026.8.19` with a keep-it-fresh comment. **The EXE
  BUNDLES yt-dlp**, so a pip upgrade only reaches the user after a re-deploy — hence:
- ✅ **New `scripts\deploy_new_version.bat`** (dev → prod): refresh yt-dlp → PyInstaller into
  `dist_new\` → promote into `dist\`; exit 2 = staged because the app is still running.
  `.projecthub/project.json` now proposes the three contract actions (§16 keys
  `launch`/`dev`/`build`) relabelled as RUN PROD / RUN DEV / DEPLOY NEW VERSION.
  **Hub commands are proposals — the user must adopt/approve them in the Hub UI.**
- ⚠️ **FIRST DEPLOY DIDN'T ACTUALLY FIX IT — PyInstaller shipped the OLD yt-dlp.** The user
  re-ran ⬇ Web and got the same "video is not available" on a fresh EXE (process start
  13:20:45 vs EXE build 13:20:28 — definitely the new binary). Diagnosis: `build_new\` was
  dated **Aug 8** after an Aug 25 build → PyInstaller reused cached analysis keyed on module
  PATHS, and `pip install -U yt-dlp` replaces files at the SAME paths, so the bundle kept
  2026.3.17. Meanwhile the venv resolved both failing IDs fine. **Fix: deploy now does
  `rmdir /s /q build_new` + `--clean` every time** (recorded in .projecthub/NOTES.md — never
  remove). General rule: after upgrading any bundled dependency, a cached PyInstaller build
  is not proof of anything.
- ✅ **Version indicator (user request):** status-bar badge, leftmost so device names can't
  push it off — `v1.2.9` muted in PROD, `v1.2.9 DEV` on a yellow chip when running from
  source (`sys.frozen` decides). Tooltip explains prod-vs-dev and that deploy bumps it.
  Deploy now auto-runs `scripts/bump_version.py`, so the number always tracks the build.
- ✅ **REAL ROOT CAUSE, found via the new debug.log instrumentation** (three wrong theories
  preceded it — stale yt-dlp bundle, stale cookies.txt, throttling; all disproved by
  reproducing outside the app, incl. a full 3.87 MB download with the user's own cookies):
  1. **Doubled URL.** The log showed
     `url=…start_radio=1https://www.youtube.com/watch?v=…start_radio=1` — the dialog
     auto-prefills the URL box from the clipboard, and pasting again appends instead of
     replacing (prefill selects the text, but clicking into the box clears the selection).
  2. **Radio/mix playlist.** The pasted link carried `&list=RD…&start_radio=1`, so with
     `noplaylist: False` yt-dlp walked an auto-generated infinite MIX
     (traceback: `__process_playlist` → `__process_iterable_entry`).
  3. **YouTube bot-check → no formats.** The failure line is
     `common.py raise_no_formats` — YouTube returned a playability status with ZERO formats
     and yt-dlp renders that as the misleading "This video is not available". **Verified
     live: player_client `['tv','ios']` FAILS on this video while
     `['android','ios','tv','web']` returns 4 formats** — client acceptance is independent,
     which is why the same video worked in every isolated test.
  Fixes: `_sanitize_media_url()` (de-duplicates a doubled paste; strips `list=RD…/UL…` +
  `start_radio` for watch URLs while leaving REAL `PL…` playlists intact — unit-checked over
  6 cases) and a retry ladder in `_ydl_run()`: as-configured → cookie-free → explicit
  player clients, keeping the FIRST error if all rungs fail. The box is rewritten in place
  so the user sees the cleaned URL rather than a silent change.
- ✅ **yt-dlp version is now visible in the ⬇ Web dialog header** ("engine: yt-dlp X · if
  downloads start failing, Deploy new version") — this exact class of failure is invisible
  otherwise, and it's the fastest way to tell a stale bundle from a genuinely dead video.

---

## 2026-08-08 (later) — Phase 3: build the library ON the phone (BUILT, APK v3/0.3.0)

- ✅ **Phone tab CRUD:** ＋ chip in the 📱 section; chip long-press → rename/delete (delete
  refcounts files via shared `ConfigRefs` §5.1 walker; PC chips → "managed on the PC").
- ✅ **Add sounds on phone:** tap an empty slot in a phone tab → SAF picker; "Share to LSB
  Mobile" from any app (SEND/SEND_MULTIPLE, singleTask + pendingShare flow) → first phone
  tab (auto-creates "Shared"). `MediaImporter` copies with the DESKTOP naming convention
  `{stem}_{md5-8}{ext}` (content-dedupe, two-way-sync-ready).
- ✅ **Slot action sheet** (long-press): Edit / ✂ Trim / 🗑 Delete (phone tabs only).
- ✅ **Trim editor:** MediaExtractor+MediaCodec → PCM16 (float fallback), ≤10 min +
  `largeHeap`; Canvas waveform + drag handles (green start/red end), preview via temp WAV
  through the pool, save-as-new `{stem}_{ts8}.wav` + `source_file_path` (re-trim opens the
  ORIGINAL — desktop Clone semantics). Cuts from PC sounds land in a phone tab ("Cuts").
- ✅ **Web/YouTube download:** youtubedl-android (Seal fork, arm64-only), board ⋮ → 🌐;
  `bestaudio[ext=m4a]/bestaudio`, NO ffmpeg (m4a/webm kept, §7.7); into current phone tab
  or auto-created "Web". First run unpacks the engine (~15s).
- ✅ Repo `mutateConfig` publishes synchronously + single-lane saves (create-tab→add-slot
  can't race); slice A+B compiled clean on first checkpoint.
- ⏭ On-device: install v3 via 📷 update flow → checklist: create tab, add from Files,
  share from WhatsApp, trim a cut (Hebrew name), delete slot/tab, yt-dlp first-run, then
  📷 re-sync to confirm phone content survives (§5.4).

---

## 2026-08-08 — section-wipe incident: root cause + hardening (FIXED)

User's 4 phone-section tabs (Kids/TV Shows/Memes/Fart) reverted to PC and the Phone view
showed a stale PC grid. Backup forensics (config_backups timestamps): a **17:14 launch of the
OLD EXE** (dist\ predates sections; `launch.bat` runs the EXE, not source!) saved the config
and dropped every `section` key — models' to_dict/from_dict silently discarded unknown fields.

- ✅ **models.py hardened:** SoundSlot/SoundTab/Person/PersonGroup now carry `extra`
  (unknown-key round-trip, mirrors the phone's raw-JsonObject rule; legacy slot `group`
  deliberately excluded so migration can't resurrect it). Old/new build mixes can no longer
  wipe post-hoc fields. Round-trip + both test suites pass.
- ✅ **Stale-grid bug:** switching to (or starting in) an EMPTY section now shows a hint
  placeholder in the board area instead of the other section's grid
  (`_show_section_placeholder`, `_reconcile_section_view` after(1500) post-load).
- ✅ **soundboard.spec:** added `soundboard.gui/audio/editor/models/mobile_sync/perf_probe`
  hiddenimports — the PEP-562 lazy `__init__` had hidden them from PyInstaller's static
  analysis (frozen EXE would have crashed on import).
- ✅ **EXE rebuilt** into `dist_new\` staging (safe while app runs); mirror into `dist\` after
  the app closes. **STANDING RULE: after desktop code changes, rebuild + mirror the EXE —
  `launch.bat` users otherwise run stale code.**
- ✅ Section restore script prepared (from backup 17:13, by index+name) — run with app closed.

---

## 2026-08-07 (latest) — in-app updater + 📷 quick sync + long-press edit (BUILT, APK v2)

User: "only missing sounds or changes; also update the app; don't re-download everything" +
"long press on a sound can edit basic stuff". Delta-sound sync already existed (md5 diff);
net-new:

- ✅ **In-app APK self-update:** manifest gains an `app` block (desktop `read_apk_info` /
  `attach_app_info` — versionCode/Name from Gradle's output-metadata.json + apk md5/size;
  tested). Phone compares `BuildConfig.VERSION_CODE`; when the PC is newer, the sync SKIPS
  `/v1/complete` (server session stays alive), shows "⬆ Update now" → md5-verified APK
  download → FileProvider + `REQUEST_INSTALL_PACKAGES` → OS installer (data survives).
  **RELEASE RULE: bump `versionCode` in app/build.gradle.kts every phone release** (now 2 /
  0.2.0; `buildFeatures.buildConfig = true` enabled).
- ✅ **📷 board button** → `sync_scan` route → scanner auto-opens (shared `startQrScan`).
- ✅ **Long-press tile → basic edit** (name RTL/volume 0-200%/speed/pitch/loop count+gap):
  `LibraryRepository.updateSlot` overlays onto the slot's raw JsonObject (unknown fields
  round-trip); §5.4 warning row when the tab is desktop-owned. Works from search results too.
- Desktop suite: 7 groups ALL PASS (new `test_app_info`). Real library now 591 audio refs —
  the user is actively adding sounds through the pipeline.
- ⏭ To get v2 on the phone: ONE last browser install (scan with camera → download app);
  from then on updates ride the 📷 sync. On-device checks: update card appears only when
  PC has newer versionCode; long-press edit persists after app restart; edited desktop-tab
  slot reverts on next sync (expected §5.4).

---

## 2026-08-07 (later) — 💻/📱 master sections + Phase 2 Wi-Fi QR sync (BUILT)

User confirmed the browser bootstrap works on the S24U, then asked for: (1) PC/Phone master
sections managed on the PC, phone shows phone-section first; (2) scan-QR → auto-update.

- ✅ **Schema:** `SoundTab.section` ("pc" default | "phone") in `models.py` (tolerant,
  round-trips; contract updated in mobile/README.md §5.1). Orthogonal to phone-side `origin`.
- ✅ **Desktop:** CTkSegmentedButton section switcher in the tabs sidebar; rows are
  pack_forget-hidden (NEVER destroyed — `tab_buttons` must stay 1:1 with `tabs`; reorder mode
  force-shows all rows since drag math assumes every row packed); tab edit dialog gained a
  "📱 Phone section" switch; new tabs land in the active section; `_switch_tab` auto-flips
  section for cross-section jumps (search); `active_section` persisted in config.
  **NOT yet smoke-tested live** — next launch: flip sections, drag-reorder in/out of filter,
  edit-dialog toggle, search-jump across sections.
- ✅ **Phone:** section switcher above the tab chips (defaults to 📱 Phone; empty-state hint
  points at the PC toggle); Wi-Fi sync card: 📷 scan (play-services-code-scanner + ML Kit
  module pre-download meta-data, manual URL fallback), `WifiPuller` (manifest pull → md5+
  on-disk diff → by-index downloads with 3× retry → shared SyncApplier → /complete),
  keep-screen-on while syncing (FGS hardening deferred), INTERNET permission added.
- 📌 Together with the earlier landing-page change, one QR now serves both flows: camera scan
  → browser (install/first pack), in-app scan → delta sync.

---

## 2026-08-07 — 📱 Mobile companion app: full plan written (PLANNED, no code yet)

- ✅ **`mobile/README.md`** — complete project plan for an Android sibling app (user's Galaxy
  S24 Ultra): playback-only soundboard (tabs/grid/search/per-sound settings/trim editor/web
  download), **no mic features by design**. Decisions locked with the user: Kotlin + Jetpack
  Compose · Wi-Fi + QR transfer now, Supabase cloud sync later · trim editor + yt-dlp download
  in v1 (People/Favorites + Queue/DJ deferred to Phase 4) · one-way desktop→phone sync,
  two-way-ready format.
- ✅ Library audited for transfer: 611 files/773 MB (~583 referenced ≈ 630 MB), all
  wav(PCM16-48k)/mp3/ogg = Android-native; 301 Hebrew filenames verified NFC + byte-exact;
  2 dangling file_paths + 1 dangling source_file_path (importer must tolerate); 2 person
  avatars use absolute out-of-tree paths (exporter must rewrite); 77 paths multi-referenced
  (refcount on delete); `audio_cache/` (2.6 GB) must never transfer. **No export/sync code
  exists anywhere today — Phase 0 builds it.**
- ✅ **Phase 0 SHIPPED (same session):** `soundboard/mobile_sync.py` — reference-set walker
  (tabs+persons+favorites, all 3 path fields), manifest builder with md5 hash cache
  (660 MB: 2.6s cold / 0.33s warm), absolute-path rewrite (avatars → `images/avatars/`,
  external audio → `sounds/external/`), `.zip` export (UTF-8 names), token-protected LAN
  server serving files **by manifest index** (Hebrew/bracket safety), idle self-stop, and the
  📱 action-bar button + QR dialog (`qrcode` in requirements + spec; `_flush_save_config`
  before build; dead-dialog auto-recreate). `soundboard/__init__.py` made lazy (PEP 562) so
  headless tests don't drag in the GUI. **Adversarially reviewed (3-agent workflow, 22
  findings applied)** — notable: per-occurrence rewrite bug (dual rel+abs ref), Windows
  SO_REUSEADDR silent double-bind, LoudnessEnhancer-style mB token issues, keep-alive
  Content-Length hangs. `test_mobile_sync.py`: 5 suites ALL PASS incl. real-library walk
  (583 audio + 137 image refs, 2 external avatars, 3 dangling tolerated; real export =
  720 files / 689 MB, zero audio_cache). Real full-manifest build verified.
- ⏭ **Smoke-test checklist for next real app launch** (code not yet exercised inside the
  running GUI): click 📱 → dialog opens without UI freeze → QR shows the right LAN IP
  (dropdown if VPN/Hyper-V adapters present) → allow the Windows Firewall prompt (Private) →
  "Open in browser" self-test returns the manifest JSON → Export .zip writes ~689 MB →
  close/reopen recreates cleanly after idle timeout. Known cosmetic: action bar can clip at
  narrow window widths (pre-existing debt, 📱 adds ~40 px; overflow-menu refactor parked).
- ✅ **Phase 1 BUILT (same session):** `mobile/android/` — complete Kotlin/Compose app,
  **compiles to a working debug APK** (35 MB, `app/build/outputs/apk/debug/app-debug.apk`).
  Data layer keeps the desktop JSON schema verbatim (raw-JsonObject round-trip, §5.2
  tolerance), ConfigStore ports the §7.5 safety pattern + a `saveAuthoritative` sync path,
  zip importer is staged + md5-verified + kill-safe (deletes run after config persist),
  ExoPlayer pool (12 voices, LoudnessEnhancer mB gain, loop sentinel 0→−1 **verified against
  desktop audio.py replay loop**), Discord-dark theme, board (tab chips + sparse grid +
  RTL tiles + search) / now-playing / sync screens. Logic-reviewed by agent (6 findings
  fixed: authoritative-save vs clobber guards, corrupt-config recovery, current_tab
  read-modify-write + debounce, delete-after-persist ordering, tab clamp). Build toolchain
  found ON this machine: Android Studio + SDK; build with
  `JAVA_HOME="C:/Program Files/Android/openjdk/jdk-21.0.8" gradlew.bat :app:assembleDebug`.
- ✅ **USB-free bootstrap added (user request, same session):** the 📱 LAN server now also
  serves a token-protected **browser landing page** (`GET /?token=`) with two buttons —
  `GET /v1/apk` (the built debug APK, `application/vnd.android.package-archive` content type
  so Android offers install) and `GET /v1/zip` (SoundPack baked once per session into %TEMP%,
  deleted on server stop; bake starts on first landing hit). QR now encodes the LANDING URL —
  scan with the phone's **camera app**, no in-app scanner needed; Phase 2's scanner will
  accept the same URL. Dialog shows download/bake events. Tests extended
  (`test_browser_bootstrap`) — ALL PASS.
- ⏭ **Phase 1 acceptance on-device (needs the user + phone, NO USB now):** desktop 📱 →
  camera-scan QR → install APK from page → download pack → import → then verify: 19 tabs/485
  slots render (Hebrew/colors/emoji/images), 104-slot tab at 120 Hz, 8-sound overlap, mono
  WAV, 150% louder than 100% (LoudnessEnhancer!), loop 3×/1.5s vs desktop side-by-side,
  dangling refs shown, kill-mid-import safe. Then Phase 2 (Wi-Fi QR sync client).
- 📌 New standing rule (recorded in copilot-instructions § Mobile Companion App): schema or
  file-naming changes must update `mobile/README.md` §6 + bump pack `format_version`.

---

## 2026-07-18 — ⭐ Favorites + 🔍 Pick-from-title (DONE, uncommitted)

- ✅ **⭐ Favorites board**: folders of favorite sounds — a persistent `FavoritesWindow` reusing the WHOLE PersonPanel feature set via a solo `PersonContext` (groups = personal folders, "folder" wording, no shared-group semantics, ✎ hidden). Add via slot ⋯ menu cascade / People chip ⋮ menu / drag onto the window (always copies). Move between folders = existing drag + "Move to folder". Persisted `favorites_board`; warmed at startup; `fav_*` namespaced geometry/UI prefs. 19/19 smoke checks.
- ✅ **🔍 Pick from title**: slot ⋯ menu + chip ⋮ menus → popup with the title in a selectable entry; select a part → right-click or buttons → Search this app / Google / custom engine (`custom_search_url` config key, `%s` placeholder, hidden when unset). 8/8 smoke checks.
- ⏭ **qBittorrent search** was requested "if not problematic" — it is: qBittorrent has no external trigger for its in-app search (WebUI API needs the user to enable WebUI + auth; desktop app has no search IPC/URI). Skipped; `custom_search_url` lets the user wire any search site themselves. Revisit only if the user runs qBittorrent WebUI and wants a real integration.
- ❓ **User screenshot "I keep getting this"** (black video pane + 5×4 grid of time-labeled blank tiles, ~34min span): does NOT match any soundboard surface (checked LongAudioPicker, editor, YouTube dialog — nothing draws a timestamp tile grid). Looks like another program's video preview/storyboard grid with failed thumbnails. Awaiting user context: what were they doing / which window is it?

---

## 2026-07-17 — People-window launch: persistent hub + measured pump fixes (DONE, uncommitted)

The user: People window "extra slow on launch". Audited with a 7-agent workflow + a LIVE timed benchmark harness (`scratchpad bench_people_hub.py` pattern — real config, 10 persons / 220 group cards / 165 sounds). Measured root causes, in order of impact:

1. **Every open was a cold launch** — `_on_close` destroyed the hub + all ~10 prewarmed panels (~4s), and gui.py's deiconify fast path was dead code.
2. **Idle starvation** — the after(1) build pump starved Tk's idle queue: first panel "drained" at 1.3s but a hidden ~1.6s geometry/first-draw backlog made true time-to-interactive ~2.9s; each prewarmed panel landed a ~1.3-1.7s freeze (prewarm-all ~19s, NOT the ~7s previously believed).
3. **68% waste** — 149/220 group cards were full 9-widget cards for EMPTY shared groups.
4. Duplicate avatar decodes (26px + 28px = 2 full photo decodes/person), unconditional 18px chip emoji raster, post-launch rewrap burst.

Fixes shipped (all in `person_board.py` + small gui.py glue; each verified by re-running the benchmark):
- ✅ **Persistent hub**: close=withdraw, `reopen()` reconciles (refresh_people + ensure_fresh + covered-dirty drain + rewrap flush); popouts same; `shutdown()` on real app exit. Reopen measured **270ms** (close 17ms).
- ✅ **Hidden startup prebuild** (`_prebuild_person_hub`, after(4500)) → first open is also the 270ms path. CTk quirk relied on: immediate `withdraw()` after construction sticks through the deferred deiconify. `select()` derives `_showing` from `wm_state()`.
- ✅ **Empty-group stub rows** (3-4 widgets, droppable, right-click menu, lazy `_upgrade_stub` with `pack(before=)`).
- ✅ **Pump policy — PROBED, not guessed**: no-flush = one giant cliff; flush-per-tick = quadratic masonry re-layout (632ms jobs vs 3,457ms flushes; ~350-420ms per flush regardless of delta); bounded `dooneevent(IDLE)` = dead end (single reflow atom is 240-400ms). Landed: debt-based flush (0.10s showing / 0.30s covered / on drain), `_CHUNK=1`, `_build_group` enqueues its expand step, 14-16ms tick budget.
- ✅ **Rewrap gated while pump busy** + wrap width seeded from real window width (`_chip_avail_base`) + `play._last_avail` seeded → the +120ms full-board PIL re-render wave after launch is now usually a no-op.
- ✅ **Avatar master-decode cache** ((path,mtime) → ≤256px master, JPEG draft) + fallback-only chip `cimg` + **CTk patch #4** (CTkToplevel `<Configure>` child-event guard).
- ✅ **UX**: opens on last-used person (`last_person`), chip size (L/M/S) + ▦ group-columns persist across launches, Esc hides the hub.

**Before → after (same harness):** true interactive ~2.9s (mostly frozen) → 3.4s fully-progressive/responsive with ≤~400ms stalls; prewarm-all 19s w/ 1.3-1.7s freezes → 14.8s w/ ≤~400ms background stalls; close ~4s → 17ms; reopen seconds → **270ms**; user-visible open with prebuild: **always ~270ms**.

Verified: 21/21 unit tests; benchmark before/after; 15-check gui-glue smoke (prebuild stays withdrawn through CTk's deiconify timer, reopen/popout state machine, shutdown teardown). NOT verified live in the running app (the built EXE was running — no second instance risked); **smoke-test checklist for next real launch**: open People (should appear instantly after ~5s uptime), close+reopen, click through people, drag a sound onto an EMPTY group's stub row, right-click a stub, collapse/expand a stub group from a pop-out, Esc closes, Hebrew chip labels wrap correctly at 150% DPI.

### Foundation decision (next steps, ranked)
- **NOW (done)**: persistent hub + stubs + pump policy — removed ~90% of felt launch cost, near-zero regression surface.
- **NEXT (recommended, behind a flag)**: canvas-chip — replace each chip's CTkFrame+2 CTkButton+CTkProgressBar (~10 Tk widgets, ~9ms) with ONE tk.Canvas modeled on the main board's SlotWidget (~0.5ms): label is already a pre-rendered PIL image; drag/shift-wheel/`_slot_ref` tagging port directly. Kills the remaining per-widget floor (first panel < 200ms cold, prewarm ~2s).
- **LATER/probably never at this scale**: canvas-per-panel (masonry as canvas items) — the true ceiling-raiser but re-implements drop targets/menus/collapse; only if someone loads hundreds of sounds per person.
- **Data hygiene worth doing**: the shared-group list carries dead groups (`E2E_NEWGROUP`, duplicate מפתח/מפתחות, חיובי/חיוב, שלילה/שלילי) — deleting them cuts every panel's card count by ~a third.
> **Purpose:** Everything still to figure out / fix from this session — the performance work we started with, the data-loss emergency, the keyboard issue, and recovery follow-ups. Cross-references [docs/PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md) for the full perf detail.

---

## 2026-06-11 — top-tier noise suppression (DeepFilterNet3) — DONE, uncommitted

The user: our RNNoise filter is "shit" vs Discord's Krisp. Correct — RNNoise is a tiny 2017 model, weak on non-stationary noise (keyboard, other voices). Researched (4-track workflow) and built a **Krisp-class upgrade**:

- ✅ **DeepFilterNet3 added as the default denoiser** — a 48 kHz-native DNN run via **onnxruntime (no PyTorch)**. Big jump over RNNoise on keyboard/voices; preserves full voice band (most alternatives — GTCRN/NSNet2/Picovoice — are 16 kHz telephone-band and were rejected for that). Model: single combined raw-in/raw-out ONNX `soundboard/models/denoiser_model.onnx` (16 MB; 480-sample frames = same as RNNoise; 45304-float recurrent state; `atten_lim_db` scalar). Validated against the source repo's reference output: **correlation 1.0000**. Cost: **~3.3 ms/frame, ~8 ms/block — 3× real-time on CPU**, runs inline in the input callback with a multi-frame warm-up so enabling mid-call doesn't glitch.
- ✅ **Pluggable backend design** — `NoiseSuppressor` now holds a `_DenoiseBackend` (`deepfilternet` | `rnnoise` | none); reuses the existing 480-frame ring buffer + one-frame priming (the buzz fix). Graceful fallback: DeepFilterNet → RNNoise → passthrough on any load/inference failure. Wet/dry `strength` is backend-agnostic.
- ✅ **GUI**: engine dropdown ("Best (DeepFilterNet)" / "Light (RNNoise)") next to the NS checkbox + strength slider; persisted as `noise_suppression_backend` (default `deepfilternet`, only restored if available). NVIDIA Broadcast hint label (detects the Broadcast mic by name; shows "select it as Input" when present, else an install tip).
- ✅ **NVIDIA Broadcast routing** — the user has an RTX GPU but the Broadcast APP isn't installed (not in the device list), so this is the detect-and-recommend hint for now; once installed, its mic just appears in the Input picker and the hint goes green.
- ✅ **PyInstaller spec** bundles onnxruntime DLLs (`collect_dynamic_libs`/`collect_data_files` — avoids the `onnxruntime_providers_shared.dll` load error) + the ONNX model; `onnxruntime` already in the venv (1.27.0).

### NS follow-ups / open
- ⏳ **Latency**: default DeepFilterNet adds ~40 ms algorithmic vs RNNoise's ~10 ms. Fine for voice chat; if the user wants RNNoise-low latency there's a ~10 ms 0-lookahead "LL" variant (split ONNX only — would need merging). Add a "low latency" toggle later if asked.
- ⏳ **Weights license for the paid product**: DeepFilterNet code is MIT/Apache and the weights ship permissively, BUT the combined ONNX came from a wrapper repo (yuyun2000/SpeechDenoiser) with no LICENSE file; underlying weights trained on the MS DNS dataset (mixed provenance). For commercialization: regenerate the combined model from Rikorose's official split ONNX (explicit MIT/Apache) via the deepfilter-rt merge script, or get written confirmation. RNNoise (BSD) remains the unambiguously-clean default-shippable fallback. Keep DeepFilterNet attribution (Hendrik Schröter, MIT/Apache).
- ⏳ **If glitches under heavy CPU load**: move inference to a worker thread off the input callback (the research-recommended design; deferred since steady-state cost is well within budget).
- ⏳ **Optional**: ai-coustics SDK (48 kHz, CPU, clean embeddable license, $) or NVIDIA Maxine in-app (RTX, royalty-free, heavy CUDA bundle) as a future "even better" tier; Krisp SDK is enterprise-sales-gated.

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
