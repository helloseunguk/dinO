@echo off
cd /d "%~dp0"

rem Use the project venv Python if present, else fall back to system pythonw.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
) else (
    echo [WARN] .venv not found. Install packages first:
    echo        python -m venv .venv
    echo        .venv\Scripts\python -m pip install -r requirements.txt
    pause
    start "" pythonw.exe "%~dp0main.py"
)
