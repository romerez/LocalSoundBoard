@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  scripts\deploy_new_version.bat - promote DEV (source) to
rem  PROD (dist\SoundBoard\SoundBoard.exe).
rem
rem  Why this exists: launch.bat runs the PACKAGED EXE, so source
rem  changes are invisible until the EXE is rebuilt. A stale EXE
rem  once wiped config fields written by newer code (see
rem  docs/SESSION_BACKLOG.md 2026-08-08) - so ALWAYS deploy after
rem  changing desktop code.
rem
rem  Safe while the app is running: the build goes to dist_new\
rem  staging first and only the final mirror needs the app closed.
rem  Non-interactive; exit 0 = prod updated, 2 = staged (close the
rem  app and re-run), 1 = build failed.
rem ============================================================
cd /d "%~dp0.."

echo [1/5] Refreshing yt-dlp (YouTube breaks stale versions)...
.venv\Scripts\python.exe -m pip install -q -U yt-dlp
if errorlevel 1 (
    echo        WARNING: yt-dlp update failed ^(offline?^) - continuing with the bundled version.
) else (
    for /f "tokens=2" %%v in ('.venv\Scripts\python.exe -m pip show yt-dlp ^| findstr /B "Version:"') do echo        yt-dlp %%v
)

echo [2/5] Bumping the app version...
for /f "delims=" %%v in ('.venv\Scripts\python.exe scripts\bump_version.py') do set NEW_VERSION=%%v
if "!NEW_VERSION!"=="" (
    echo ERROR: version bump failed - soundboard\__init__.py unchanged.
    exit /b 1
)
echo        v!NEW_VERSION! ^(shown in the app's status bar^)

echo [3/5] Building the EXE into dist_new\ ...
if not exist ".venv\Scripts\pyinstaller.exe" (
    echo ERROR: .venv\Scripts\pyinstaller.exe not found - create the venv first.
    exit /b 1
)
rem --clean + wiping the workpath is MANDATORY, not tidiness: PyInstaller
rem reuses cached analysis keyed on module PATHS, so an upgraded dependency
rem that kept its paths (exactly what `pip install -U yt-dlp` does) gets
rem silently re-bundled at its OLD version. That shipped a fresh EXE with a
rem five-month-old yt-dlp and looked like "the fix didn't work".
if exist "build_new" rmdir /s /q "build_new"
.venv\Scripts\pyinstaller.exe soundboard.spec --noconfirm --clean --distpath dist_new --workpath build_new >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller failed. Re-run without ^>nul to see the log.
    exit /b 1
)
if not exist "dist_new\SoundBoard\SoundBoard.exe" (
    echo ERROR: build produced no dist_new\SoundBoard\SoundBoard.exe
    exit /b 1
)

echo [4/5] Checking whether PROD is running...
tasklist /FI "IMAGENAME eq SoundBoard.exe" 2>NUL | find /I "SoundBoard.exe" >NUL
if not errorlevel 1 (
    echo.
    echo   ============================================================
    echo    NEW VERSION STAGED - but SoundBoard.exe is still running.
    echo    Close it ^(tray icon ^> Quit^) and run this command again
    echo    to finish promoting it to prod.
    echo   ============================================================
    exit /b 2
)

echo [5/5] Promoting dist_new -^> dist ...
robocopy "dist_new\SoundBoard" "dist\SoundBoard" /MIR /R:3 /W:2 /NFL /NDL /NP >NUL
if errorlevel 8 (
    echo ERROR: mirror into dist\ failed - files locked?
    exit /b 1
)

for %%F in ("dist\SoundBoard\SoundBoard.exe") do set BUILT=%%~tF
echo.
echo   ============================================================
echo    DEPLOYED v!NEW_VERSION! -^> dist\SoundBoard\SoundBoard.exe
echo    Built: !BUILT!
echo    The app's status bar shows v!NEW_VERSION! ^(no DEV badge^).
echo    Start it with the "Run prod" command ^(or launch.bat^).
echo   ============================================================
exit /b 0
