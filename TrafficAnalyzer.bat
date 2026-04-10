@echo off
REM Traffic Analyzer GUI Launcher
REM Double-click this file to start the desktop app

cd /d "%~dp0"

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    echo.
    echo Please install Python 3.11+ from https://www.python.org/
    echo When installing, make sure to check "Add Python to PATH"
    pause
    exit /b 1
)

REM Run the GUI app
python traffic_gui.py

REM If exits with error, show message
if errorlevel 1 (
    echo.
    echo An error occurred. Press any key to close this window.
    pause
)
