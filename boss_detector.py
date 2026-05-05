"""
보스 아이콘 템플릿 매칭으로 보스 존재 유무 판단.

중심 좌표(게임 내부 BASE) 기준 ±half 범위를 캡처해서
images/boss_icon.png 와 비교.
"""

import logging

import cv2
import mss
import numpy as np
from PIL import Image

from paths import BUNDLED_DIR
from screen_sync import BASE_W, BASE_H, _find_game_window, _scale_xy, _scale_w, _scale_h

logger = logging.getLogger(__name__)

# 탐색 영역 (게임 내부 BASE 1920×1009 기준)
BOSS_ICON_CENTER = (907, 19)
BOSS_ICON_HALF_W = 200
BOSS_ICON_HALF_H = 30

# 템플릿 매칭 임계값 (TM_CCOEFF_NORMED, 0.0~1.0 — 높을수록 엄격)
MATCH_THRESHOLD = 0.55

_TEMPLATE_PATH = BUNDLED_DIR / "images" / "boss_icon.png"
_template_cache: np.ndarray | None = None


def _load_template() -> np.ndarray:
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    if not _TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"보스 아이콘 템플릿 없음: {_TEMPLATE_PATH}")
    img = cv2.imread(str(_TEMPLATE_PATH), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"템플릿 로드 실패: {_TEMPLATE_PATH}")
    _template_cache = img
    logger.info("[보스탐지] 템플릿 로드: %s (%dx%d)",
                _TEMPLATE_PATH.name, img.shape[1], img.shape[0])
    return _template_cache


def _capture_search_region(bx: int, by: int, cw: int, ch: int) -> tuple[np.ndarray, dict]:
    """게임 내부 중심/반경 → 절대 좌표 region 으로 변환 후 캡처."""
    cx, cy = _scale_xy(BOSS_ICON_CENTER, cw, ch)
    hw = _scale_w(BOSS_ICON_HALF_W, cw)
    hh = _scale_h(BOSS_ICON_HALF_H, ch)
    left = bx + cx - hw
    top = by + cy - hh
    region = {"left": left, "top": top, "width": hw * 2, "height": hh * 2}
    with mss.mss() as sct:
        shot = sct.grab(region)
        img_rgb = np.array(Image.frombytes(
            "RGB", (shot.width, shot.height), shot.rgb))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    return img_bgr, region


def _scale_template(template: np.ndarray, cw: int, ch: int) -> np.ndarray:
    """클라이언트 크기가 BASE와 다르면 템플릿도 같은 비율로 리사이즈."""
    sw, sh = cw / BASE_W, ch / BASE_H
    if abs(sw - 1.0) < 0.02 and abs(sh - 1.0) < 0.02:
        return template
    new_w = max(1, int(template.shape[1] * sw))
    new_h = max(1, int(template.shape[0] * sh))
    return cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_LINEAR)


def detect_boss(threshold: float = MATCH_THRESHOLD) -> tuple[bool, float, dict]:
    """
    게임 화면에서 보스 아이콘 템플릿 매칭.

    반환: (present: bool, max_confidence: float, debug_info: dict)
      debug_info: {'region': ..., 'template_size': (w,h), 'max_loc': (x,y)}
    """
    template = _load_template()
    _hwnd, bx, by, cw, ch = _find_game_window()
    search_img, region = _capture_search_region(bx, by, cw, ch)
    template_scaled = _scale_template(template, cw, ch)

    if (search_img.shape[0] < template_scaled.shape[0]
            or search_img.shape[1] < template_scaled.shape[1]):
        logger.warning("[보스탐지] 탐색 영역(%dx%d)이 템플릿(%dx%d)보다 작음",
                       search_img.shape[1], search_img.shape[0],
                       template_scaled.shape[1], template_scaled.shape[0])
        return False, 0.0, {"region": region}

    result = cv2.matchTemplate(search_img, template_scaled, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    present = max_val >= threshold
    debug = {
        "region": region,
        "template_size": (template_scaled.shape[1], template_scaled.shape[0]),
        "max_loc": max_loc,
        "score": max_val,
    }
    # per-poll 로그는 DEBUG — 상태 전환 순간만 INFO로 presence_monitor에서 로깅
    logger.debug("[보스탐지] 점수=%.3f (임계값=%.2f) → %s | 매칭위치=%s",
                 max_val, threshold,
                 "있음" if present else "없음", max_loc)
    return present, max_val, debug
