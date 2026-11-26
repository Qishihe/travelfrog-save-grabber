@echo off
chcp 65001 >nul
setlocal
set "PKG=%~dp0"
title TravelFrog Official Server Save Grabber (Portable)

echo ============================================================
echo   TravelFrog - One-Click Official / Mi Server Save Grabber
echo   Portable package. No Python installation needed.
echo ============================================================
echo.
echo   BEFORE you run:
echo     1. Start MuMu / LDPlayer emulator
echo     2. Open TravelFrog (official or Mi server) and LOG IN
echo     3. Keep the emulator running
echo.
echo   The script will restart the game, grab the save, and print
echo   the output path below. No manual clicks needed.
echo   If BOTH servers are installed, both are grabbed in turn.
echo   After it finishes, restart the game once to restore network.
echo.

set "PY=%PKG%runtime\python\python.exe"
if exist "%PY%" goto :run

echo [!] Bundled Python not found, trying system Python...
where python >nul 2>&1
if not errorlevel 1 goto :use_system
where py >nul 2>&1
if not errorlevel 1 goto :use_launcher
echo [X] No Python found. Put python.exe in runtime\python\.
pause
exit /b 1

:use_system
set "PY=python"
goto :run

:use_launcher
set "PY=py"

:run
"%PY%" -u "%PKG%scripts\grab_official.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%" NEQ "0" goto :fail
echo Done. Check output folder timestamped subdir for the save.
goto :end

:fail
echo ------------------------------------------------------------
echo  Failed. Common causes:
echo    1. Emulator not running / game not installed / not logged in
echo    2. Wrong ADB port:  add argument  --port 16384
echo    3. Pick one server:  add argument  --pkg mi   or   --pkg official
echo    4. Missing big protos: add argument  --clear  (re-login)
echo    5. Cannot tap Enter:   add argument  --tap X Y
echo    6. Emulator root denied: enable ADB/root in emulator settings
echo ------------------------------------------------------------

:end
echo.
pause
