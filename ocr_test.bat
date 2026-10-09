@echo off
rem 서버 OCR 테스트 — 시간표를 열어 캡처한 뒤 /api/bosses/ocr/test 로 전송.
rem 의존성(mss, Pillow)은 프로젝트 venv 에만 있으므로 반드시 venv 파이썬으로 실행한다.
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [WARN] .venv not found. Install packages first:
    echo        python -m venv .venv
    echo        .venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

"%~dp0.venv\Scripts\python.exe" -m odin.sync.ocr_sync --test %*
pause
