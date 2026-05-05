"""빌드/개발 환경에 따른 경로 해석.

frozen (PyInstaller) 실행 환경:
  - BUNDLED_DIR: 읽기 전용 자산 (sys._MEIPASS — one-folder 는 실행 폴더, one-file 은 temp)
  - DATA_DIR:    쓰기 가능한 사용자 데이터 (exe 옆 폴더 — 재실행 간 보존)
개발 환경: 둘 다 프로젝트 루트.
"""

import sys
from pathlib import Path

_PROJECT_DIR = Path(__file__).parent

if getattr(sys, "frozen", False):
    BUNDLED_DIR = Path(getattr(sys, "_MEIPASS", _PROJECT_DIR))
    DATA_DIR = Path(sys.executable).parent
else:
    BUNDLED_DIR = _PROJECT_DIR
    DATA_DIR = _PROJECT_DIR
