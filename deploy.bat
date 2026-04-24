@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ========================================
echo   SoundBoard Deploy Pipeline
echo ========================================
echo.

REM Activate virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo ERROR: Virtual environment not found. Run from workspace root.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat

REM Bump version
echo [1/3] Bumping patch version...
for /f "delims=" %%v in ('python scripts\bump_version.py') do set NEW_VERSION=%%v
if "!NEW_VERSION!"=="" (
    echo ERROR: Version bump failed.
    pause
    exit /b 1
)
echo        New version: !NEW_VERSION!
echo.

REM Build EXE
echo [2/3] Building executable...
echo.
pyinstaller soundboard.spec --noconfirm
echo.

REM Verify output
echo [3/3] Verifying build...
if exist "dist\SoundBoard\SoundBoard.exe" (
    echo.
    echo ========================================
    echo   DEPLOY SUCCESSFUL
    echo   Version : !NEW_VERSION!
    echo   EXE     : dist\SoundBoard\SoundBoard.exe
    echo.
    echo   Run the app:  launch.bat
    echo   Dev mode  :   run.bat
    echo   (Both share the same config + sounds)
    echo ========================================
) else (
    echo ERROR: Build failed - SoundBoard.exe not found in dist\SoundBoard\
    pause
    exit /b 1
)

echo.
pause
