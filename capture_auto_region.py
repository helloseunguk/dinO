"""auto 아이콘 인식 영역 1회 캡처 → 디스크 저장.

용도: AUTO_IMAGE_CENTER / W / H / threshold 튜닝 시 실제로 어떤 픽셀이
캡처되는지 시각 확인.

저장:
  auto_debug/auto_capture.png        ← 캡처된 영역 원본
  auto_debug/auto_match_vis.png      ← 매칭 박스 시각화 (template 위치 표시)
  auto_debug/auto_template.png       ← 현재 템플릿 (비교용)
"""

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

from automator import (
    AUTO_IMAGE_CENTER, AUTO_IMAGE_W, AUTO_IMAGE_H,
    AUTO_IMAGE_THRESHOLD, _load_auto_template,
)
from game_coords import center_to_abs_region
from screen_sync import _find_game_window

OUT_DIR = Path(__file__).parent / "auto_debug"


def main():
    OUT_DIR.mkdir(exist_ok=True)
    template = _load_auto_template()
    print(f"[템플릿] {template.shape[1]}x{template.shape[0]}")
    print(f"[설정] center={AUTO_IMAGE_CENTER} (BASE) | "
          f"w={AUTO_IMAGE_W} h={AUTO_IMAGE_H} | threshold={AUTO_IMAGE_THRESHOLD}")

    hwnd, bx, by, cw, ch = _find_game_window()
    print(f"[윈도우] hwnd={hwnd} bx={bx} by={by} cw={cw} ch={ch}")

    # 게임 윈도우 포커스 후 잠시 대기 — UI가 활성화된 상태로 캡처
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)

    region = center_to_abs_region(
        AUTO_IMAGE_CENTER, AUTO_IMAGE_W, AUTO_IMAGE_H, bx, by, cw, ch)
    print(f"[캡처영역] {region}")

    with mss.mss() as sct:
        shot = sct.grab(region)
    img_rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

    # 원본 캡처
    cap_path = OUT_DIR / "auto_capture.png"
    cv2.imwrite(str(cap_path), img_bgr)

    # 템플릿 복사 (비교용)
    tpl_path = OUT_DIR / "auto_template.png"
    cv2.imwrite(str(tpl_path), template)

    # 템플릿 매칭 + 시각화
    if (img_bgr.shape[0] >= template.shape[0]
            and img_bgr.shape[1] >= template.shape[1]):
        result = cv2.matchTemplate(img_bgr, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        present = max_val >= AUTO_IMAGE_THRESHOLD
        print(f"[매칭] score={max_val:.3f} → {'있음' if present else '없음'} "
              f"(임계값={AUTO_IMAGE_THRESHOLD}) | loc={max_loc}")

        vis = img_bgr.copy()
        br = (max_loc[0] + template.shape[1],
              max_loc[1] + template.shape[0])
        color = (0, 255, 0) if present else (0, 0, 255)
        cv2.rectangle(vis, max_loc, br, color, 2)
        vis_path = OUT_DIR / "auto_match_vis.png"
        cv2.imwrite(str(vis_path), vis)
    else:
        print(f"[경고] 캡처 영역({img_bgr.shape[1]}x{img_bgr.shape[0]})이 "
              f"템플릿({template.shape[1]}x{template.shape[0]})보다 작음")

    print(f"\n[저장] {OUT_DIR}")
    for p in OUT_DIR.iterdir():
        print(f"  {p.name}")


if __name__ == "__main__":
    main()
