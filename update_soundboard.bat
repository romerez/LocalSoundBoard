@echo off
rem Finish updating SoundBoard: mirror the staged build (dist_new) over dist.
rem Safe to run any time AFTER closing the running SoundBoard.
cd /d "%~dp0"

tasklist /FI "IMAGENAME eq SoundBoard.exe" 2>NUL | find /I "SoundBoard.exe" >NUL
if not errorlevel 1 (
    echo SoundBoard.exe is still running - close it first ^(tray icon ^> Quit^).
    pause
    exit /b 1
)

if not exist "dist_new\SoundBoard\SoundBoard.exe" (
    echo No staged build found at dist_new\SoundBoard - nothing to do.
    pause
    exit /b 1
)

echo Mirroring new build into dist\ ...
robocopy "dist_new\SoundBoard" "dist\SoundBoard" /MIR /R:3 /W:2 /NFL /NDL /NP >NUL
if errorlevel 8 (
    echo Some files could not be copied - is the app really closed?
    pause
    exit /b 1
)
echo Done. Starting the new SoundBoard...
start "" "dist\SoundBoard\SoundBoard.exe"
