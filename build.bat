@echo off
cd /d "%~dp0"

echo [1/3] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo.
        echo [ERROR] pip install failed.
        echo Try manually: python -m pip install pyinstaller
        pause
        exit /b 1
    )
)

echo.
echo [2/3] Cleaning and building...
python -m PyInstaller --clean --noconfirm odin_timer.spec
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
