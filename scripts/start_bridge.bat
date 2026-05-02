@echo off
chcp 65001 >nul
title AI Paimon — ClawBot Bridge

echo ========================================
echo     ClawBot Bridge Server
echo ========================================
echo.

set "ROOT=%~dp0.."
cd /d "%ROOT%"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

echo Starting ClawBot bridge on port 5001 ...
echo.
python src/clawbot_bridge.py %*

pause
