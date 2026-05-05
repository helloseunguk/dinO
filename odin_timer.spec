# -*- mode: python ; coding: utf-8 -*-
"""
Odin Boss Timer — PyInstaller spec (one-folder, windowed, UAC admin).

빌드:
    pyinstaller --clean odin_timer.spec

결과:
    dist/OdinBossTimer/OdinBossTimer.exe  (+ 의존 파일)

런타임 데이터 (exe 옆에 생성/보존):
    - boss_times.json          : 보스 리젠 시각
    - automation_targets.json  : GUI에서 편집한 우선순위

OCR:
    Windows 내장 OCR (winsdk) 사용 — 별도 모델 번들 불필요.
    한국어 언어팩이 OS에 설치돼 있어야 함.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PROJECT_DIR = Path(SPECPATH)

# ── 번들할 자산 ───────────────────────────────────────────────
datas = [
    (str(PROJECT_DIR / "images" / "boss_icon.png"), "images"),
    (str(PROJECT_DIR / "images" / "boss.ico"), "images"),
    (str(PROJECT_DIR / "images" / "auto.png"), "images"),
    (str(PROJECT_DIR / "discord_secret.txt"), "."),
]

# ── hidden imports ──────────────────────────────────────────
hiddenimports = []
hiddenimports += collect_submodules("winsdk")
hiddenimports += [
    "PIL.Image",
    "PIL.ImageGrab",
]

# ── Analysis ────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "notebook",
        "jupyter",
        "IPython",
        "unittest",
        "test",
        # EasyOCR 제거 후 이 모듈들은 더 이상 필요 없음
        "torch",
        "torchvision",
        "easyocr",
        "scipy",
        "pandas",
        "sympy",
        "networkx",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OdinBossTimer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    uac_admin=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_DIR / "images" / "boss.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OdinBossTimer",
)
