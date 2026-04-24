@echo off
cd /d "%~dp0"

if not exist "dist\SoundBoard\SoundBoard.exe" (
    echo SoundBoard.exe not found.
    echo Run deploy.bat first to build the application.
    pause
    exit /b 1
)

REM Launch EXE with workspace root as working directory.
REM This makes it share soundboard_config.json, sounds\ and images\ with dev mode.
start "" /D "%~dp0" "%~dp0dist\SoundBoard\SoundBoard.exe"
