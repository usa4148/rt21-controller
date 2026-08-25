@echo off
REM Launcher for the RT-21 Rotator Controller, web edition (Windows).
REM Standard library only - no virtual environment, nothing to install.

setlocal
set "APP_DIR=%~dp0"
set "APP_SCRIPT=%APP_DIR%rt21_web.py"

if not exist "%APP_SCRIPT%" (
    echo Error: rt21_web.py not found next to this script.
    pause
    exit /b 1
)

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%APP_SCRIPT%" %*
) else (
    python "%APP_SCRIPT%" %*
)
endlocal
