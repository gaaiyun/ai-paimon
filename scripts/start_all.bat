@echo off
chcp 65001 >nul
title AI Paimon — Full Stack Launcher

echo ================================================
echo   AI Paimon — One-Click Launcher
echo ================================================
echo.

set "ROOT=%~dp0.."
cd /d "%ROOT%"

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

cd /d "%ROOT%\Open-LLM-VTuber"
uv run run_server.py

pause
