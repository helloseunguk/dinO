"""보스 아이콘 감지 실시간 모니터.

사용법:
    python test_detect_live.py

1초마다 detect_boss() 호출 → score + 판정 + 매칭 위치 출력.
Ctrl+C 로 종료.
"""

import time
from boss_detector import detect_boss, BOSS_ICON_CENTER, MATCH_THRESHOLD


def main():
    print(f"감지 시작 (ROI 중심={BOSS_ICON_CENTER}, 임계값={MATCH_THRESHOLD})")
    print(f"{'시각':10} {'score':>7} {'판정':>8} {'위치':>12}")
    print("-" * 45)
    try:
        while True:
            try:
                present, score, debug = detect_boss()
            except Exception as e:
                print(f"ERROR: {e}")
                time.sleep(1)
                continue
            mark = "★있음" if present else "  없음"
            loc = debug.get("max_loc", ("?", "?"))
            now = time.strftime("%H:%M:%S")
            print(f"{now:10} {score:7.3f} {mark:>8} {str(loc):>12}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료")


if __name__ == "__main__":
    main()
