@echo off
rem ============================================================
rem  scripts\build_exe.bat - non-interactive EXE build (Hub/CI).
rem  No pauses; exit code 0 = artifact produced.
rem
rem  Builds into dist_new\ (staging), then mirrors into dist\
rem  with robocopy. If SoundBoard.exe is currently running the
rem  mirror is skipped and the build stays staged - close the
rem  app and run update_soundboard.bat to finish the update.
rem ============================================================
cd /d "%~dp0.."

if not exist ".venv\Scripts\pyinstaller.exe" (
    echo ERROR: .venv\Scripts\pyinstaller.exe not found - create the venv first.
    exit /b 1
)

.venv\Scripts\pyinstaller.exe soundboard.spec --noconfirm --distpath dist_new
if errorlevel 1 (
    echo ERROR: PyInstaller failed.
    exit /b 1
)
if not exist "dist_new\SoundBoard\SoundBoard.exe" (
    echo ERROR: build produced no dist_new\SoundBoard\SoundBoard.exe
    exit /b 1
)

tasklist /FI "IMAGENAME eq SoundBoard.exe" 2>NUL | find /I "SoundBoard.exe" >NUL
if not errorlevel 1 (
    echo SoundBoard.exe is running - new build STAGED in dist_new\.
    echo Close the app and run update_soundboard.bat to mirror it into dist\.
    exit /b 0
)

robocopy "dist_new\SoundBoard" "dist\SoundBoard" /MIR /R:3 /W:2 /NFL /NDL /NP >NUL
if errorlevel 8 (
    echo ERROR: mirror into dist\ failed - files locked?
    exit /b 1
)
echo Build OK: dist\SoundBoard\SoundBoard.exe
exit /b 0
