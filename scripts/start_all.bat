@echo off
chcp 65001 >nul
title AI Paimon — Full Stack Launcher

echo ================================================
echo   AI Paimon — One-Click Launcher
echo ================================================
echo.

set "ROOT=%~dp0.."
cd /d "%ROOT%"

REM --- Load Open-LLM-VTuber path from .env or use default ---
if exist "%ROOT%\.env" (
    for /f "usebackq tokens=1,2 delims==" %%a in ("%ROOT%\.env") do (
        if "%%a"=="OPEN_LLM_VTUBER_DIR" set "VTUBER_DIR=%%b"
    )
)
if not defined VTUBER_DIR (
    echo.
    echo   ERROR: OPEN_LLM_VTUBER_DIR is not set.
    echo   Please add the following line to your .env file:
    echo     OPEN_LLM_VTUBER_DIR=C:\path\to\Open-LLM-VTuber
    echo.
    pause
    exit /b 1
)

if not exist "%VTUBER_DIR%\run_server.py" (
    echo.
    echo   ERROR: Cannot find run_server.py in: %VTUBER_DIR%
    echo   Please check your OPEN_LLM_VTUBER_DIR setting in .env
    echo.
    pause
    exit /b 1
)

REM --- Step 1: Check OpenClaw Gateway ---
echo [1/3] Checking ClawBot Gateway ...
powershell -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:18789/' -TimeoutSec 3 -UseBasicParsing; Write-Host '  OK - Gateway is running' } catch { Write-Host '  WARNING: Gateway not running. Start it with: openclaw gateway'; pause; exit 1 }"

REM --- Step 2: Start VITS TTS server in background ---
echo.
echo [2/3] Starting VITS TTS server (port 8020) ...
start "Paimon VITS" cmd /c "%ROOT%\scripts\start_vits.bat"
timeout /t 5 /nobreak >nul

REM --- Step 3: Start Open-LLM-VTuber ---
echo.
echo [3/3] Starting Open-LLM-VTuber ...
echo.
echo   Open your browser: http://localhost:12393
echo   Press Ctrl+C to stop
echo ================================================
echo.

cd /d "%VTUBER_DIR%"
uv run run_server.py

pause
