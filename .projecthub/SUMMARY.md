# LocalSoundBoard

A Windows Discord soundboard built for one power user: play sounds and music
into a Discord call by mixing them — together with a noise-suppressed live
mic — into a VB-Cable virtual microphone, with automatic push-to-talk so
sounds transmit hands-free. The UI is Hebrew-first (full RTL + colored
emoji). An Android companion app (in `mobile/`) mirrors the library over
Wi-Fi QR sync and can now build its own library on the phone.

## Current state

Working and in daily use. Desktop runs from source (`main.py`) or as a
PyInstaller EXE (`dist\SoundBoard\`). The July–August work — ⭐ Favorites,
🌐 Universal PTT, DeepFilterNet3 GUI wiring, and the whole mobile companion
(desktop `mobile_sync.py` + `mobile/android/`, APK v3/0.3.0) — is **built and
tested but largely uncommitted** in the working tree. Known standing rule:
after desktop code changes, rebuild + mirror the EXE (`launch.bat` users run
the EXE, not source — a stale EXE once wiped newer config fields).

## Architecture

One Python process (CustomTkinter UI + sounddevice/WASAPI audio):

- `soundboard/gui.py` — SoundboardApp: config/state owner, main board
  (virtualized slot grid, tabs with 💻/📱 sections, search), imports
  (drag & drop, yt-dlp), perf patches for CTk internals. ~700 KB, the hub.
- `soundboard/audio.py` — AudioMixer: duplex 48 kHz stream; SoundCache with
  a decoded-PCM disk cache (`audio_cache/`); per-sound volume/speed/pitch/
  loop; mic pipeline with five noise-suppression engines (DeepFilterNet3
  ONNX, Max = DFN + residual gate, Classic spectral, RNNoise via a direct
  rnnoise.dll ctypes binding, Gate) on a dedicated worker thread, 80 Hz
  low-cut and voice FX; auto-PTT key injection with a physical-key watch;
  mic duck.
- `soundboard/person_board.py` — People hub + pop-outs + ⭐ Favorites:
  per-person mini-boards; persistent hub (~30 ms reopen, alpha-masked
  RedrawWindow reveal), chip build pump, device-pixel chip labels, native
  names with colour pills. `soundboard/ctk_patches.py` removes CustomTkinter's
  nested idle pumps app-wide.
- `soundboard/mobile_sync.py` — SoundPack manifest (md5 + hash cache),
  zip export, token-protected LAN HTTP server (files by manifest index),
  QR dialog; also serves the APK + browser bootstrap landing page.
- `soundboard/universal_ptt.py` — hook-free VK-polled mic gate for any app.
- `soundboard/models.py` — tolerant dataclasses (unknown JSON keys
  round-trip, so old/new builds can't wipe each other's fields).
- `mobile/android/` — Kotlin + Jetpack Compose playback app; keeps the
  desktop JSON schema verbatim; ExoPlayer pool; Wi-Fi delta sync; in-app
  APK self-update; phone-side tabs/trim/share-import/yt-dlp (v3).

Data: one `soundboard_config.json` (guarded: async atomic writes, .bak,
20 rotating launch backups, empty-people refusal) + plain files in
`sounds/`, `images/`, `audio_cache/`. No database server.

## How it runs

Three actions cover the whole loop: **run prod** = `dist\SoundBoard\SoundBoard.exe`
(what the user actually uses), **run dev** = `.venv\Scripts\python.exe main.py`
(source, same config + sounds — never both at once), **deploy new version** =
`scripts\deploy_new_version.bat`, which promotes dev → prod: refresh yt-dlp →
PyInstaller into `dist_new\` staging → mirror into `dist\`. It exits 2
("staged") when the EXE is still running, because the mirror can't replace a
locked binary — close the app from the tray and re-run. Older equivalents kept:
`scripts\build_exe.bat` (build only), `update_soundboard.bat` (mirror only),
interactive `build.bat`/`deploy.bat` (pause; deploy also bumps the version).
`launch.bat` starts the EXE detached; `stop_soundboard.bat` kills stray
*python* processes only — it does NOT stop the EXE (Task-kill does). Tests:
`test_soundboard.py` and `test_mobile_sync.py` run headless (the package
imports lazily via PEP 562). Android: Gradle in `mobile/android/` (needs
JAVA_HOME → the Android Studio JDK; see NOTES.md). Executable command
definitions live in `project.json` as proposals for the Hub.
