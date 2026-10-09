"""마우스 좌표 모니터링 + 단축키로 좌표 저장 (터미널 버전).

앱 안에서 쓰려면 GUI 툴바의 "🖱 좌표 수집…" 버튼을 쓰면 된다 — 저장 파일
(macro_points.json)과 좌표 환산은 macro_points 모듈로 공유하므로 양쪽 결과가 같다.

사용법:
    python mouse_monitor.py           # 모니터링 시작
    python mouse_monitor.py --once    # 현재 좌표 1회만 출력하고 종료

단축키 (게임 창에 포커스가 있어도 동작):
    F8   현재 좌표 저장 (이름 입력 — 빈 값이면 자동 이름)
    F9   마지막 저장 취소
    F10  저장 목록 출력
    Ctrl+C 종료
"""

import ctypes
import sys
import time

# DPI awareness — 스케일된 디스플레이에서 좌표가 어긋나지 않도록 가장 먼저 설정
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

import macro_points as mp
from game_coords import BASE_H, BASE_W

# 좌표 표시와 단축키 감시를 같은 루프에서 처리
POLL_INTERVAL_SEC = 0.05


def print_points(points: list[dict]) -> None:
    if not points:
        print("\n저장된 좌표가 없습니다.")
        return
    print(f"\n── 저장된 좌표 {len(points)}개 ({mp.POINTS_PATH}) ──")
    for i, p in enumerate(points, 1):
        print(f"{i:2d}. {p['name'] or '(이름 없음)':<20} {mp.format_line(p)}")
    print("── 코드에 붙여넣기 ──")
    print(mp.code_snippet(points))
    print()


def main() -> None:
    gx, gy, cw, ch = mp.window_origin()
    if cw:
        print(f"게임 창: 원점 ({gx}, {gy}), 클라이언트 {cw}x{ch} "
              f"(BASE {BASE_W}x{BASE_H}, 스케일 {cw / BASE_W:.3f}x)")
    else:
        print("게임 창을 찾지 못했습니다 — 절대 좌표만 표시합니다.")

    if "--once" in sys.argv:
        print(mp.format_line(mp.snapshot(gx, gy, cw, ch)))
        return

    points = mp.load_points()
    print(f"기존 저장 좌표 {len(points)}개 ← {mp.POINTS_PATH.name}")
    print("=" * 72)
    print("F8 저장 · F9 마지막 취소 · F10 목록 · Ctrl+C 종료")
    print("=" * 72)

    watched = (mp.VK_F8, mp.VK_F9, mp.VK_F10)
    prev = {vk: False for vk in watched}
    try:
        while True:
            now = {vk: mp.key_down(vk) for vk in watched}

            # F8 — 누른 순간의 좌표를 먼저 찍어두고 이름을 물어본다
            #      (이름 입력 중에 마우스가 움직여도 저장값은 바뀌지 않음)
            if now[mp.VK_F8] and not prev[mp.VK_F8]:
                point = mp.snapshot(gx, gy, cw, ch)
                print(f"\n[저장] {mp.format_line(point)}")
                try:
                    name = input("       이름 (엔터=자동): ").strip()
                except EOFError:
                    name = ""
                point["name"] = name or mp.auto_name(points)
                points.append(point)
                mp.save_points(points)
                print(f"       → '{point['name']}' 저장됨 (총 {len(points)}개)")

            if now[mp.VK_F9] and not prev[mp.VK_F9]:
                if points:
                    removed = points.pop()
                    mp.save_points(points)
                    print(f"\n[취소] '{removed['name']}' 삭제 "
                          f"(남은 {len(points)}개)")
                else:
                    print("\n[취소] 저장된 좌표가 없습니다.")

            if now[mp.VK_F10] and not prev[mp.VK_F10]:
                print_points(points)

            prev = now

            ax, ay = mp.cursor_pos()
            lx, ly = ax - gx, ay - gy
            bx, by = mp.to_base(lx, ly, cw, ch)
            mark = " ⚠ 창 밖" if mp.outside_client(lx, ly, cw, ch) else "       "
            print(f"\rBASE ({bx:5d}, {by:5d}) | 게임 ({lx:6d}, {ly:6d}) "
                  f"| 절대 ({ax:5d}, {ay:5d}) | 저장 {len(points)}개{mark}",
                  end="", flush=True)
            time.sleep(POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        print_points(points)
        print("종료")


if __name__ == "__main__":
    main()
