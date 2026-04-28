@echo off
cd /d "%~dp0"

if not exist "dist\SoundBoard\SoundBoard.exe" (
    echo SoundBoard.exe not found.
    echo Run deploy.bat first to build the application.
    pause
    exit /b 1
)

REM Spawn the EXE detached so this cmd window can close immediately.
REM /B = no new console window, cwd is inherited from this script (workspace root).
start "" /B "%~dp0dist\SoundBoard\SoundBoard.exe"
exit
