"""보스 탐지 범위 1회 캡처 — 시각 확인용.

사용법: python capture_boss_region.py

저장: boss_debug/region_HHMMSS.png  (탐색 영역 + 매칭 위치 박스)
출력: 절대좌표, 매칭 점수
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

from boss_detector import (
    BOSS_ICON_CENTER, BOSS_ICON_HALF_W, BOSS_ICON_HALF_H,
    MATCH_THRESHOLD, _load_template,
)
from screen_sync import BASE_W, BASE_H, _find_game_window, _scale_xy, _scale_w, _scale_h

OUT_DIR = Path(__file__).parent / "boss_debug"


def main():
    OUT_DIR.mkdir(exist_ok=True)
    template = _load_template()
    hwnd, bx, by, cw, ch = _find_game_window()

    cx, cy = _scale_xy(BOSS_ICON_CENTER, cw, ch)
    hw = _scale_w(BOSS_ICON_HALF_W, cw)
    hh = _scale_h(BOSS_ICON_HALF_H, ch)
    region = {
        "left": bx + cx - hw, "top": by + cy - hh,
        "width": hw * 2, "height": hh * 2,
    }

    print(f"[게임창] base=({bx},{by}) size={cw}x{ch}")
    print(f"[BASE]   center={BOSS_ICON_CENTER} half={BOSS_ICON_HALF_W}x{BOSS_ICON_HALF_H}")
    print(f"[절대]   left={region['left']} top={region['top']} "
          f"size={region['width']}x{region['height']}")

    with mss.mss() as sct:
        shot = sct.grab(region)
    img_rgb = np.array(Image.frombytes("RGB", (shot.width, shot.height), shot.rgb))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

    sw, sh = cw / BASE_W, ch / BASE_H
    tpl = template
    if abs(sw - 1.0) > 0.02 or abs(sh - 1.0) > 0.02:
        tpl = cv2.resize(
            template,
            (max(1, int(template.shape[1] * sw)),
             max(1, int(template.shape[0] * sh))),
            interpolation=cv2.INTER_LINEAR,
        )

    result = cv2.matchTemplate(img_bgr, tpl, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    present = max_val >= MATCH_THRESHOLD

    print(f"[매칭]   score={max_val:.3f} (임계값 {MATCH_THRESHOLD}) → "
          f"{'★있음' if present else '없음'} | loc={max_loc}")

    vis = img_bgr.copy()
    color = (0, 255, 0) if present else (0, 0, 255)
    cv2.rectangle(vis, max_loc,
                  (max_loc[0] + tpl.shape[1], max_loc[1] + tpl.shape[0]),
                  color, 2)

    ts = time.strftime("%H-%M-%S")
    fname = f"region_{ts}_s{max_val:.2f}_{'P' if present else 'N'}.png"
    out_path = OUT_DIR / fname
    cv2.imwrite(str(out_path), vis)
    print(f"[저장]   {out_path}")


if __name__ == "__main__":
    main()
