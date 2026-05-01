@echo off
chcp 65001 >nul
cd /d "%~dp0"
python main.py
echo.
echo === 프로그램이 종료되었습니다. 에러가 있으면 위 내용을 확인하세요. ===
pause
