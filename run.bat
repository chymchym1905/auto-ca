@echo off
REM Launch the ToF Window Capturer as Administrator.
REM Running elevated makes the global hotkey work even when ToF runs as admin.

REM Self-elevate: if not already admin, relaunch this script via UAC.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM Run from this script's own directory.
cd /d "%~dp0"

".venv\Scripts\pythonw.exe" main.py
set EXITCODE=%errorlevel%

REM Keep the window open if it exited with an error.
if %EXITCODE% neq 0 (
    echo.
    echo Program exited with code %EXITCODE%.
    pause
)
