@echo off
REM RoValid launcher for Windows
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" checker.py %*
) else (
    echo No .venv found - using system Python.
    echo Tip: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -r requirements.txt
    echo.
    python checker.py %*
)

pause
