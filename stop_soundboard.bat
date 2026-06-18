@echo off
REM ============================================================
REM  stop_soundboard.bat
REM  Force-closes the soundboard app and ANY other Python
REM  process. Use this when the window is gone but python.exe
REM  is still running (an orphaned process keeps auto-saving
REM  the config and can re-wipe data).
REM
REM  Just double-click it, or run it from a terminal.
REM ============================================================

echo.
echo === Python processes BEFORE ===
tasklist /FI "IMAGENAME eq python.exe"  2>nul | findstr /I "python.exe"  || echo   (no python.exe running)
tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | findstr /I "pythonw.exe" || echo   (no pythonw.exe running)

echo.
echo Killing python.exe / pythonw.exe ...
taskkill /F /IM python.exe  >nul 2>&1
taskkill /F /IM pythonw.exe >nul 2>&1

echo.
echo === Python processes AFTER ===
tasklist /FI "IMAGENAME eq python.exe"  2>nul | findstr /I "python.exe"  || echo   (none - all closed)
tasklist /FI "IMAGENAME eq pythonw.exe" 2>nul | findstr /I "pythonw.exe" || echo   (none - all closed)

echo.
echo Done. Safe to restore / relaunch now.
pause
