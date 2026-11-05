@echo off
REM OpenPLC Runtime - Windows Launcher
REM This script starts the OpenPLC Runtime inside the MSYS2 environment

setlocal EnableDelayedExpansion

REM Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

REM Set MSYS2 root directory (relative to this script)
set "MSYS2_ROOT=%SCRIPT_DIR%\msys64"

REM Check if MSYS2 exists
if not exist "%MSYS2_ROOT%\usr\bin\bash.exe" (
    echo ERROR: MSYS2 installation not found at %MSYS2_ROOT%
    echo Please reinstall OpenPLC Runtime.
    pause
    exit /b 1
)

REM Set environment variables for MSYS2
set "CHERE_INVOKING=1"
set "MSYSTEM=MSYS"
set "HOME=/home/openplc"

REM Convert Windows path to MSYS2 path
set "OPENPLC_WIN_PATH=%SCRIPT_DIR%\openplc-runtime"
set "OPENPLC_MSYS_PATH=%OPENPLC_WIN_PATH:\=/%"
set "OPENPLC_MSYS_PATH=%OPENPLC_MSYS_PATH:C:=/c%"
set "OPENPLC_MSYS_PATH=%OPENPLC_MSYS_PATH:D:=/d%"
set "OPENPLC_MSYS_PATH=%OPENPLC_MSYS_PATH:E:=/e%"

echo ==========================================
echo OpenPLC Runtime for Windows
echo ==========================================
echo.
echo Starting OpenPLC Runtime...
echo MSYS2 Root: %MSYS2_ROOT%
echo OpenPLC Dir: %OPENPLC_WIN_PATH%
echo.

REM Create runtime directory if it doesn't exist
if not exist "%MSYS2_ROOT%\run\runtime" (
    mkdir "%MSYS2_ROOT%\run\runtime" 2>nul
)

REM Start the OpenPLC Runtime
"%MSYS2_ROOT%\usr\bin\bash.exe" -lc "cd '%OPENPLC_MSYS_PATH%' && exec ./venvs/runtime/bin/python3 -m webserver.app"

if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: OpenPLC Runtime exited with error code %ERRORLEVEL%
    pause
)

endlocal
