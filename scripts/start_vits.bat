@echo off
chcp 65001 >nul
title AI Paimon — VITS TTS Server

echo ========================================
echo     Paimon VITS TTS Server
echo ========================================
echo.

REM Resolve project root (parent of scripts/)
set "ROOT=%~dp0.."
cd /d "%ROOT%"

REM Activate venv if present
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

echo Starting VITS server on port 8020 ...
echo.
python src/vits_server/server.py %*

pause
