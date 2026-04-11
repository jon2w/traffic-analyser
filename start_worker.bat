@echo off
REM Traffic Analyser Worker Launcher
REM Double-click to start processing recordings

cd /d "%~dp0"

REM Activate virtual environment
if exist "%~dp0venv\Scripts\activate.bat" (
    call "%~dp0venv\Scripts\activate.bat"
) else (
    echo Error: venv not found.
    echo Run: python -m venv venv ^&^& venv\Scripts\activate ^&^& pip install -r requirements.txt
    pause
    exit /b 1
)

python worker.py --server http://192.168.1.99:5002 --user-id 2

if errorlevel 1 (
    echo.
    echo Worker exited with an error. Press any key to close.
    pause
)
