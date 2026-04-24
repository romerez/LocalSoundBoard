@echo off
echo ========================================
echo   Building SoundBoard Executable
echo ========================================
echo.

REM Activate virtual environment
call .venv\Scripts\activate.bat

echo Installing PyInstaller if needed...
pip install pyinstaller >nul 2>&1

echo.
echo Building executable (this may take a few minutes)...
echo.
pyinstaller soundboard.spec --noconfirm

echo.
if exist "dist\SoundBoard\SoundBoard.exe" (
    echo ========================================
    echo   BUILD SUCCESSFUL!
    echo   Output: dist\SoundBoard\SoundBoard.exe
    echo ========================================
    echo.
    echo You can copy the entire dist\SoundBoard folder
    echo to any location and run SoundBoard.exe
) else (
    echo ========================================
    echo   BUILD FAILED - Check output above
    echo ========================================
)
echo.
pause
