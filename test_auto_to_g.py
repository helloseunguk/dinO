"""auto 인식 → AUTO 위치 클릭 end-to-end 테스트.

automator._run_idle_sequence 의 마지막 단계만 따로 실행.
이미 게임 화면에 AUTO 아이콘이 떠 있다고 가정.

사용법:
    1) 게임에서 AUTO 아이콘이 화면에 보이는 상태로 만든다
    2) python test_auto_to_g.py
    3) 스크립트가 게임 포커스 → AUTO 폴링 → 감지되면 1.5s 후 AUTO 위치 클릭
"""

import ctypes
import logging
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

from automator import (
    AUTO_IMAGE_CENTER, _wait_for_auto_image, _sleep_cancellable,
)
from game_coords import click_base as _click_game_base
from screen_sync import _find_game_window


def main():
    print("=" * 60)
    print("AUTO 인식 → AUTO 위치 클릭 end-to-end 테스트")
    print("=" * 60)
    print("3초 후 시작합니다. 게임 화면에 AUTO 아이콘이 보이도록 준비하세요.")
    for i in range(3, 0, -1):
        print(f"  {i}...")
        time.sleep(1)

    hwnd, bx, by, cw, ch = _find_game_window()
    print(f"\n[윈도우] hwnd={hwnd} bx={bx} by={by} cw={cw} ch={ch}")

    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)

    auto_detected = _wait_for_auto_image(bx, by, cw, ch)

    if auto_detected:
        print(f"\n>>> 감지됨. 1.5s 안정화 → AUTO 위치 클릭 BASE={AUTO_IMAGE_CENTER}")
        _sleep_cancellable(1.5)
        _click_game_base(AUTO_IMAGE_CENTER, bx, by, cw, ch)
        print(">>> 클릭 완료")
    else:
        print("\n>>> 미감지 — 종료")

    print("\n[완료] 게임에서 AUTO 토글이 적용됐는지 확인하세요.")


if __name__ == "__main__":
    main()
