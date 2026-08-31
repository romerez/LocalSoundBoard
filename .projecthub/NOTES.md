# Notes

- **EXE staleness is dangerous.** `launch.bat` runs `dist\SoundBoard\SoundBoard.exe`,
  not source. A stale EXE once stripped newer config fields (the 2026-08-08
  section-wipe). After desktop code changes run the **deploy new version** action
  (`scripts\deploy_new_version.bat`) — it stages into `dist_new\` and promotes to
  `dist\`, exiting 2 if the app is still running.
- **PyInstaller silently re-bundles STALE dependencies.** Its cached analysis is keyed on
  module PATHS, so a `pip install -U <pkg>` that keeps the same paths (yt-dlp does) gets
  rebuilt at the OLD version — you ship a brand-new EXE containing a months-old library and
  conclude "the fix didn't work". `scripts\deploy_new_version.bat` therefore wipes
  `build_new\` and passes `--clean` every time. Never remove that.
- **yt-dlp goes stale and YouTube lies about it.** An out-of-date yt-dlp reports
  `This video is not available` for perfectly fine videos (seen 2026-08-09 with the
  March build; updating fixed the exact failing ID). The EXE BUNDLES yt-dlp, so a
  `pip install -U yt-dlp` only reaches users after a re-deploy — which is why the
  deploy action refreshes it first. Separately, Chrome/Edge v127+ app-bound
  encryption makes `--cookies-from-browser chrome` permanently unreadable
  (yt-dlp #7271); the app now auto-retries without cookies, and cookies.txt or
  Firefox remain the options for genuinely age-restricted videos.
- **Machine-specific paths.** Commands use `.venv\Scripts\...` inside the repo.
  The Android build needs `JAVA_HOME = C:/Program Files/Android/openjdk/jdk-21.0.8`
  (Android Studio's JDK) — configure as an env var at Hub approval time.
- **Schema contract with the phone.** Any change to the config schema or file
  naming must update `mobile/README.md` §6 and bump the SoundPack
  `format_version`. Phone releases must bump `versionCode` in
  `mobile/android/app/build.gradle.kts`.
- **Windows keeps STALE pixels for Tk content built while a toplevel is withdrawn.** The
  prebuilt People hub / pre-warmed panels came up blank or as bare colour slabs even though
  every widget's state was right. `person_board._redraw_window()` (Win32 RedrawWindow,
  RDW_ALLCHILDREN) after deiconify — done behind `-alpha 0` on the first <Map>/<Expose> —
  is the cure; forcing `_draw()` on every widget is the slow way. Also: a never-mapped
  CTkToplevel reports `winfo_width()==200`, so pre-map layout must use CTk's
  `_current_width` (see `_win_width`). And CustomTkinter's scrollbar/option-menu draws
  call `update_idletasks()` (nested full flushes) — `soundboard/ctk_patches.py` removes
  that; keep it imported first thing in gui.py.
- **CTkToplevel constructors run a full nested `update()`** inside CustomTkinter's
  `_windows_set_titlebar_color` (`withdraw(); update()`), which pumps the entire pending
  backlog — a dialog opened while the People hub pre-warms took ~5 s to appear.
  `ctk_patches.install()` shadows `update`→`update_idletasks` for that call and repaints
  each toplevel once mapped. Keep it imported first thing in gui.py.
- **Auto-PTT + pause:** the PTT release countdown ignores PAUSED sounds, so pausing every
  sound releases the Discord key ~300 ms later. `resume_sound`/`restart_sound` MUST
  `_press_ptt()` again or the resumed audio reaches the cable with Discord not
  transmitting (was inaudible). Fixed 2026-08-31.
- **Tk/Win32 reveal gotchas (2026-08-31 review, Tk-probed).** `deiconify()` on a MAPPED
  window emits no <Map>/<Expose> — an alpha-0 reveal must short-circuit on `winfo_viewable()`.
  `RedrawWindow(RDW_UPDATENOW)` only makes Tk QUEUE <Expose>s; Frames/Canvases repaint in idle
  callbacks, so "show after repaint" = nested `after_idle`. A pending `after()` dies with the
  widget it was armed on (arm session-long timers on the root — `slot_widget._poll_host`). Tk
  refuses `grid()` into a master that has `pack()` slaves. Pillow's raqm BiDi needs a fribidi
  DLL Pillow doesn't ship (here: Meld/Tesseract on PATH) — `person_board._RAQM_OK` gates the
  PIL Hebrew path. `_fix_rtl_text` is a gui.py MODULE function, not a method.
- **Never `import pyrnnoise`.** Its `__init__` pulls `audiolab → av` (libav), which the EXE
  never had — so for months "Light (RNNoise)" silently passed the raw mic through in prod
  while logging a warning per audio block. `audio.py` binds `rnnoise.dll` via ctypes
  (`_load_rnnoise`) and the spec excludes `pyrnnoise/audiolab/av`; only the DLL ships.
  Related: never drive DeepFilterNet's `atten_lim_db` for the strength slider — any finite
  limit comb-filters the voice (measured 2026-08-31); strength is a delay-compensated
  wet/dry in `NoiseSuppressor`. `test_noise_suppression.py` guards both.
- **Audio routing assumptions.** VB-Cable set to 48 kHz both directions;
  Discord noise suppression (Krisp) = None; Discord input = the cable.
  Mismatched sample rates present as "robotic voice" (playbook in memory +
  docs).
- **audio_cache/ (~2.6 GB) and debug logs are disposable**; never ship or sync
  them. `soundboard_config.json` + `sounds/` + `images/` are the real data.
- Live smoke-test checklists for the not-yet-exercised UI paths live in
  `docs/SESSION_BACKLOG.md` (People-hub launch checks, 📱 dialog checks,
  phone v3 on-device acceptance).
