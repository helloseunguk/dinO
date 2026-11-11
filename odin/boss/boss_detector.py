"""
보스 존재 유무 판단.

presence_monitor 가 두 신호를 함께 본다.
1) detect_boss_engaged — 우측 '피해량' 패널 헤더 OCR
   보스와 교전 중이면 헤더가 "피해량 - <보스명>", 아니면 "피해량" 만 뜬다.
   보스 이름까지 확인할 수 있어 연탐 때 다른 보스와 헷갈리지 않지만,
   보스가 죽은 뒤에도 한동안 남아 있어 처치 시점은 알 수 없다.
2) detect_boss — 상단 중앙 보스 아이콘 템플릿 매칭
   처치 시점 판단에 쓴다. 단독으로는 2026-09-19 이후 추적 세션 대부분이
   미감지로 끝났다 (점수 0.2~0.4, 임계값 0.55).
"""

import difflib
import logging

import cv2
import numpy as np

from odin.core.paths import BUNDLED_DIR
from odin.game.game_coords import grab_base
from odin.sync.screen_sync import BASE_W, BASE_H, _find_game_window, _scale_xy, _scale_w, _scale_h

logger = logging.getLogger(__name__)

# '피해량' 패널 헤더 영역 (게임 내부 BASE 1920×1009 기준)
DAMAGE_PANEL_CENTER = (1732, 96)
DAMAGE_PANEL_HALF_W = 148
DAMAGE_PANEL_HALF_H = 13
# 원본 크기로는 Windows OCR 이 글자를 못 읽는다 — 3배 확대 + 흰 글씨 이진화
_PANEL_UPSCALE = 3
_PANEL_TEXT_THRESHOLD = 180
# OCR 이 한두 글자 틀려도 같은 보스로 보는 유사도 하한
_NAME_MATCH_RATIO = 0.6

# 탐색 영역 (게임 내부 BASE 1920×1009 기준)
BOSS_ICON_CENTER = (907, 19)
BOSS_ICON_HALF_W = 200
BOSS_ICON_HALF_H = 30

# 템플릿 매칭 임계값 (TM_CCOEFF_NORMED, 0.0~1.0 — 높을수록 엄격)
MATCH_THRESHOLD = 0.55

_TEMPLATE_PATH = BUNDLED_DIR / "images" / "boss_icon.png"
_template_cache: tuple[np.ndarray, np.ndarray] | None = None
# 마스크 = 붉은 아이콘 픽셀(채도·명도 높음)의 바깥 윤곽을 채운 영역.
# 템플릿 모서리에 찍힌 어두운 배경까지 비교하면 수르트처럼 배경이 밝은 불길인 곳에선
# 아이콘이 그대로 있어도 점수가 0.3 대로 떨어져 "잡힘" 으로 오판했다 (10/24·10/27).
_MASK_MIN_SAT = 120
_MASK_MIN_VAL = 70


def _build_mask(template: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
    red = ((hsv[:, :, 1] >= _MASK_MIN_SAT)
           & (hsv[:, :, 2] >= _MASK_MIN_VAL)).astype(np.uint8) * 255
    # 눈·입 같은 안쪽 검은 무늬는 배경과 상관없으니 윤곽째 채워 포함한다
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    mask = np.zeros_like(red)
    if contours:
        cv2.drawContours(mask, [max(contours, key=cv2.contourArea)], -1, 255, cv2.FILLED)
    # 가장자리 1px 은 배경과 섞인 픽셀이라 뺀다
    return cv2.erode(mask, np.ones((2, 2), np.uint8))


def _load_template() -> tuple[np.ndarray, np.ndarray]:
    """(템플릿, 아이콘 마스크) 반환."""
    global _template_cache
    if _template_cache is not None:
        return _template_cache
    if not _TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"보스 아이콘 템플릿 없음: {_TEMPLATE_PATH}")
    img = cv2.imread(str(_TEMPLATE_PATH), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"템플릿 로드 실패: {_TEMPLATE_PATH}")
    mask = _build_mask(img)
    _template_cache = (img, mask)
    logger.info("[보스탐지] 템플릿 로드: %s (%dx%d, 마스크 %d/%d px)",
                _TEMPLATE_PATH.name, img.shape[1], img.shape[0],
                int((mask > 0).sum()), mask.size)
    return _template_cache


def _capture_search_region(bx: int, by: int, cw: int, ch: int) -> tuple[np.ndarray, dict]:
    """게임 내부 중심/반경 → 절대 좌표 region 으로 변환 후 캡처 (BASE 크기로 줄임)."""
    cx, cy = _scale_xy(BOSS_ICON_CENTER, cw, ch)
    hw = _scale_w(BOSS_ICON_HALF_W, cw)
    hh = _scale_h(BOSS_ICON_HALF_H, ch)
    left = bx + cx - hw
    top = by + cy - hh
    region = {"left": left, "top": top, "width": hw * 2, "height": hh * 2}
    img_bgr = cv2.cvtColor(grab_base(region, cw, ch), cv2.COLOR_RGB2BGR)
    return img_bgr, region


def _scale_template(template: np.ndarray, mask: np.ndarray,
                    cw: int, ch: int) -> tuple[np.ndarray, np.ndarray]:
    """클라이언트 크기가 BASE와 다르면 템플릿·마스크도 같은 비율로 리사이즈.
    tools/ 의 원본 크기 캡처용 — detect_boss 는 캡처를 BASE 로 줄이므로 쓰지 않는다."""
    sw, sh = cw / BASE_W, ch / BASE_H
    if abs(sw - 1.0) < 0.02 and abs(sh - 1.0) < 0.02:
        return template, mask
    new_w = max(1, int(template.shape[1] * sw))
    new_h = max(1, int(template.shape[0] * sh))
    return (cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_LINEAR),
            cv2.resize(mask, (new_w, new_h), interpolation=cv2.INTER_NEAREST))


def detect_boss(threshold: float = MATCH_THRESHOLD) -> tuple[bool, float, dict]:
    """
    게임 화면에서 보스 아이콘 템플릿 매칭.

    반환: (present: bool, max_confidence: float, debug_info: dict)
      debug_info: {'region': ..., 'template_size': (w,h), 'max_loc': (x,y)}
    """
    template, mask = _load_template()
    _hwnd, bx, by, cw, ch = _find_game_window()
    search_img, region = _capture_search_region(bx, by, cw, ch)

    if (search_img.shape[0] < template.shape[0]
            or search_img.shape[1] < template.shape[1]):
        logger.warning("[보스탐지] 탐색 영역(%dx%d)이 템플릿(%dx%d)보다 작음",
                       search_img.shape[1], search_img.shape[0],
                       template.shape[1], template.shape[0])
        return False, 0.0, {"region": region}

    result = cv2.matchTemplate(search_img, template, cv2.TM_CCOEFF_NORMED,
                               mask=mask)
    # 마스크 매칭은 단색 영역에서 분모가 0 이 돼 nan/inf 가 나온다 — 0 으로 본다
    result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    present = max_val >= threshold
    debug = {
        "region": region,
        "template_size": (template.shape[1], template.shape[0]),
        "max_loc": max_loc,
        "score": max_val,
    }
    # per-poll 로그는 DEBUG — 상태 전환 순간만 INFO로 presence_monitor에서 로깅
    logger.debug("[보스탐지] 점수=%.3f (임계값=%.2f) → %s | 매칭위치=%s",
                 max_val, threshold,
                 "있음" if present else "없음", max_loc)
    return present, max_val, debug


def save_icon_region(path) -> None:
    """아이콘 탐색 영역을 지금 캡처해 저장 (오판 진단용)."""
    _hwnd, bx, by, cw, ch = _find_game_window()
    img, _ = _capture_search_region(bx, by, cw, ch)
    path.parent.mkdir(parents=True, exist_ok=True)
    # cv2.imwrite 는 한글 경로를 못 쓴다 — 인코딩 후 직접 기록
    ok, buf = cv2.imencode(".png", img)
    if ok:
        path.write_bytes(buf.tobytes())


def read_damage_panel() -> str:
    """'피해량' 패널 헤더 OCR. 교전 중인 보스 이름을 반환, 없으면 빈 문자열."""
    from odin.game.ocr_engine import ocr_text

    _hwnd, bx, by, cw, ch = _find_game_window()
    cx, cy = _scale_xy(DAMAGE_PANEL_CENTER, cw, ch)
    hw = _scale_w(DAMAGE_PANEL_HALF_W, cw)
    hh = _scale_h(DAMAGE_PANEL_HALF_H, ch)
    region = {"left": bx + cx - hw, "top": by + cy - hh,
              "width": hw * 2, "height": hh * 2}
    gray = cv2.cvtColor(grab_base(region, cw, ch), cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=_PANEL_UPSCALE, fy=_PANEL_UPSCALE,
                      interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, _PANEL_TEXT_THRESHOLD, 255,
                              cv2.THRESH_BINARY_INV)
    text = ocr_text(binary)
    if "-" not in text:
        return ""
    return text.split("-", 1)[1].replace(" ", "")


def detect_boss_engaged(boss_name: str) -> tuple[bool, str]:
    """'피해량 - <boss_name>' 이 떠 있으면 True.

    반환: (present, 헤더에서 읽은 보스 이름 — 다른 보스 이름일 수도 있음)
    """
    seen = read_damage_panel()
    target = boss_name.replace(" ", "")
    present = bool(seen) and (
        target in seen
        or difflib.SequenceMatcher(None, target, seen).ratio() >= _NAME_MATCH_RATIO)
    logger.debug("[보스탐지] 피해량 패널='%s' (대상=%s) → %s",
                 seen, boss_name, "교전 중" if present else "없음")
    return present, seen
