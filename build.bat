@echo off
cd /d "%~dp0"

rem Use venv Python if present, else fall back to system python.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [1/3] Checking PyInstaller...
"%PY%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    "%PY%" -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed.
        echo Try manually: "%PY%" -m pip install pyinstaller
        pause
        exit /b 1
    )
)

echo.
echo [2/3] Cleaning and building...
"%PY%" -m PyInstaller --clean --noconfirm odin_timer.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo [3/3] Done. Output: dist\OdinBossTimer\OdinBossTimer.exe
explorer "dist\OdinBossTimer"
pause
