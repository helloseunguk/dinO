"""
보스 아이콘 템플릿 매칭 독립 테스트 스크립트.

용도:
  - boss_detector.py의 BOSS_ICON_CENTER / MATCH_THRESHOLD 튜닝
  - 매칭 안 되면 캡처 이미지를 디스크에 저장해서 시각 확인

실행:
  python test_boss_presence.py                 # 기본 폴링 (2s)
  python test_boss_presence.py --save          # 매 캡처마다 저장
  python test_boss_presence.py --center 908 19 --half 50 50
  python test_boss_presence.py --threshold 0.7

종료: Ctrl+C
"""

import argparse
import ctypes
import time
from pathlib import Path

import cv2
import mss
import numpy as np
from PIL import Image

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from boss_detector import (
    BOSS_ICON_CENTER, BOSS_ICON_HALF_W, BOSS_ICON_HALF_H,
    MATCH_THRESHOLD, _load_template,
)
from screen_sync import BASE_W, BASE_H, _find_game_window, _scale_xy, _scale_w, _scale_h

OUT_DIR = Path(__file__).parent / "boss_debug"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--center", nargs=2, type=int, metavar=("X", "Y"),
                   default=None, help="중심 좌표 (게임 내부 BASE)")
    p.add_argument("--half", nargs=2, type=int, metavar=("HW", "HH"),
                   default=None, help="반경 (게임 내부 BASE)")
    p.add_argument("--threshold", type=float, default=MATCH_THRESHOLD,
                   help="매칭 임계값")
    p.add_argument("--interval", type=float, default=2.0,
                   help="폴링 간격 (초)")
    p.add_argument("--save", action="store_true",
                   help="매 캡처마다 이미지 저장 (boss_debug/)")
    p.add_argument("--save-miss", action="store_true",
                   help="매칭 실패 시에만 저장")
    return p.parse_args()


def main():
    args = parse_args()
    center = tuple(args.center) if args.center else BOSS_ICON_CENTER
    hw_base = args.half[0] if args.half else BOSS_ICON_HALF_W
    hh_base = args.half[1] if args.half else BOSS_ICON_HALF_H

    template = _load_template()
    print(f"[템플릿] {template.shape[1]}x{template.shape[0]}")
    print(f"[설정] center={center} (BASE) | half={hw_base}x{hh_base} (BASE)")
    print(f"[설정] threshold={args.threshold} | interval={args.interval}s")

    if args.save or args.save_miss:
        OUT_DIR.mkdir(exist_ok=True)
        print(f"[저장] {OUT_DIR}")

    print("폴링 시작 (Ctrl+C로 종료)")
    print("-" * 80)

    count = 0
    try:
        with mss.mss() as sct:
            while True:
                count += 1
                t0 = time.time()

                hwnd, bx, by, cw, ch = _find_game_window()
                cx, cy = _scale_xy(center, cw, ch)
                hw = _scale_w(hw_base, cw)
                hh = _scale_h(hh_base, ch)
                region = {
                    "left": bx + cx - hw, "top": by + cy - hh,
                    "width": hw * 2, "height": hh * 2,
                }

                shot = sct.grab(region)
                img_rgb = np.array(Image.frombytes(
                    "RGB", (shot.width, shot.height), shot.rgb))
                img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

                # 템플릿 비율 스케일
                sw, sh = cw / BASE_W, ch / BASE_H
                tpl = template
                if abs(sw - 1.0) > 0.02 or abs(sh - 1.0) > 0.02:
                    tpl = cv2.resize(
                        template,
                        (max(1, int(template.shape[1] * sw)),
                         max(1, int(template.shape[0] * sh))),
                        interpolation=cv2.INTER_LINEAR,
                    )

                if (img_bgr.shape[0] < tpl.shape[0]
                        or img_bgr.shape[1] < tpl.shape[1]):
                    print(f"[#{count}] 영역이 템플릿보다 작음 — 건너뜀")
                    time.sleep(args.interval)
                    continue

                result = cv2.matchTemplate(img_bgr, tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                present = max_val >= args.threshold
                dt = (time.time() - t0) * 1000
                mark = "🔴" if present else "  "
                ts = time.strftime("%H:%M:%S")
                print(f"[#{count} {ts}] ({dt:.0f}ms) {mark} score={max_val:.3f} "
                      f"loc={max_loc} | region={region}")

                if args.save or (args.save_miss and not present):
                    fname = f"{count:04d}_{ts.replace(':', '-')}_{'P' if present else '_'}_s{max_val:.2f}.png"
                    # 원본 영역 + 매칭 박스 시각화
                    vis = img_bgr.copy()
                    top_left = max_loc
                    br = (top_left[0] + tpl.shape[1], top_left[1] + tpl.shape[0])
                    color = (0, 255, 0) if present else (0, 0, 255)
                    cv2.rectangle(vis, top_left, br, color, 2)
                    cv2.imwrite(str(OUT_DIR / fname), vis)

                time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\n[종료] 총 {count}회 폴링")


if __name__ == "__main__":
    main()
