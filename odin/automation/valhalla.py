"""발할라 대전 자동 참가.

근거 자료: recordings/20261018_181512 (18:15 참가 → 18:19 필드 복귀, 4분 14초).

순서:
  1) 왼쪽 '참가 등록 진행' 아래 아이콘 클릭 → "발할라 대전에 참가 하시겠습니까?" 확인
  2) 적대 범위 공격 바로가기가 꺼져 있으면 켠다 (꺼짐 = 회색, 켜짐 = 금색)
  3) 매칭 대기 — 그동안은 하던 사냥 그대로. 녹화에선 약 95초
  4) 경기장 입장(오른쪽 위 주황 띠 '… 팀 순위') → 발할라 프리셋 (옵션, 기본 D).
     그 뒤로는 자동 전투만 — AUTO 가 꺼져 있으면 G. 입장하면 AUTO 가 꺼진 채로 시작한다.
     사망 화면에선 아무것도 누르지 않는다. 부활은 저절로 되고, '나가기' 는 이탈이다.
  5) 결과 창(OCR 로 '보상'·'패배'·'승리') → 창을 클릭해 닫기
  6) 필드 복귀 → 아이들 프리셋 (옵션, 지금 C)

화면 판정은 템플릿보다 **색**을 주로 쓴다. 경기장 배경이 계속 바뀌고, 켜짐/꺼짐은
모양이 같고 색만 다르다. 기준값은 모두 위 녹화에서 잰 것이다 (BASE 1920×1009).
"""

import ctypes
import logging
import re
import time

import cv2
import mss
import numpy as np
from PIL import Image

from odin.automation.automator import (
    SKILL_PRESET_KEYS, _VK_G, _cancel_event, _check_cancel, _find_and_click,
    _match_region, _press_key, _sleep_cancellable, AutomationCancelled)
from odin.game.game_coords import ensure_game_foreground
from odin.game.game_coords import (
    center_to_abs_region, click_at_screen, click_base, find_game_window, grab_base,
    scale_h, scale_w, ui)
from odin.core.paths import BUNDLED_DIR
from odin.game.ocr_engine import ocr_text

logger = logging.getLogger(__name__)

# ── 1) 참가 ─────────────────────────────────────────────
# 왼쪽 위 퀘스트 목록의 '참가 등록 진행 05:44' 아래 아이콘 — 녹화에선 (262, 197).
# 퀘스트 목록에 다른 항목이 끼면 위아래로 밀릴 수 있어 세로로 넓게 찾는다.
JOIN_IMAGE = "valhalla_join.png"
JOIN_CENTER = (262, 230)
JOIN_W = 200
JOIN_H = 300
JOIN_THRESHOLD = 0.6        # 녹화에서 참가 전 0.88~0.94, 경기 후(아이콘 없음) 0.41~0.46
JOIN_TIMEOUT_SEC = 10.0
# 아이콘 아래 글자 — 참가 전엔 흰 카운트다운 '04:45초', 참가 후엔 노란 '매칭중 X'.
# 아이콘은 참가 후에도 그대로 있어서(빛만 빠짐) 아이콘만 보고 누르면 이미 참가한
# 상태에서 한 번 더 누르게 된다. 흰 글자가 보일 때만 누른다.
# 흰 비율 참가 전 0.044 / 참가 후 0, 노랑 참가 후 0.03~0.17 (깜빡임 — 0 인 순간도 있다).
JOIN_LABEL_OFFSET = (-2, 30)  # 아이콘 중심 기준 (BASE)
JOIN_LABEL_W = 70
JOIN_LABEL_H = 16
JOIN_WHITE_MIN = 0.02
JOIN_YELLOW_MIN = 0.02
# 참가 확인 창의 '확인' 버튼 — 녹화에선 (1028, 586)
CONFIRM_IMAGE = "valhalla_confirm.png"
CONFIRM_CENTER = (1028, 586)
CONFIRM_W = 260
CONFIRM_H = 90
CONFIRM_TIMEOUT_SEC = 5.0

# ── 2) 적대 범위 공격 바로가기 (왼쪽 아이콘 열) ──────────────
# 금색 픽셀 비율 — 꺼짐 0.00 (필드 4장), 켜짐 0.07~0.28 (필드·경기장 20장).
HOSTILE_CENTER = ui(33, 396, "TL")   # 왼쪽 아이콘열 — 위쪽 기준
HOSTILE_SIZE = 28
HOSTILE_GOLD_MIN = 0.04
HOSTILE_MAX_CLICKS = 2     # 누를 때마다 뒤집히므로 오판이면 두 번째 클릭이 되돌린다
# 같은 자리에 칼 아이콘 대신 번개 배터리 아이콘이 있을 때가 있다(10/30 새벽 이후 4K·QHD 화면).
# 그때도 '꺼짐' 으로 보고 두 번씩 눌렀다 — 칼 모양(회색·금색 모두)일 때만 누른다.
# 칼 템플릿 점수: 회색 0.87 · 금색 0.73 / 배터리 0.16~0.28 · 로딩 0.13.
HOSTILE_ICON_PATH = BUNDLED_DIR / "images" / "hostile_swords.png"
HOSTILE_ICON_MIN = 0.55
_hostile_icon_cache: np.ndarray | None = None

# ── 4) 경기장 판정 ─────────────────────────────────────
# 오른쪽 위 '… 팀 순위' 띠가 주황이면 바로 경기장으로 본다 (카운트다운 중에도 보인다).
# 띠 중앙값 색이 경기장 BGR (9~35, 56~99, 112~185), 필드·사망 화면은 R < 80.
#
# 하지만 색만으로는 부족했다 — 10-18 21:00 회차에 실제로 경기를 치렀는데 한 번도
# 경기장으로 못 봤다. 녹화(18:15)에선 우리가 '바니르(아군)' 이었고, 주황은 팀/아군
# 표시 색이라 편이 바뀌면 띠 색도 바뀌는 것으로 보인다.
# 그래서 왼쪽 킬 현황판의 팀 이름('에시르  바니르(아군)')과 남은 시간('4분 46초 남음')을
# OCR 로 함께 본다. 어느 편이든 같은 자리·같은 글자다. 녹화에서 살아 있는 경기 화면
# 61장 모두 읽혔고, 필드·사망 화면 51장은 한 장도 안 읽혔다.
ARENA_BAR_CENTER = (1635, 96)
ARENA_BAR_W = 76
ARENA_BAR_H = 14
ARENA_TEAMS_CENTER = ui(160, 343, "TL")
ARENA_TEAMS_W, ARENA_TEAMS_H = 200, 26
ARENA_TIMER_CENTER = (150, 310)
ARENA_TIMER_W, ARENA_TIMER_H = 200, 26
_ARENA_TEAMS_RE = re.compile(r"에시|바니")
_ARENA_TIMER_RE = re.compile(r"남음|\d+\s*초")
# 입장 때 누를 프리셋은 옵션 — automation_config.VALHALLA_PRESET (기본 D)

# AUTO 링 — 버튼 둘레에서 도는 밝은 화살표. 글자(반지름 14 안쪽)는 뺀다.
# 링 자리의 밝은 픽셀 비율이 꺼짐 0.00, 켜짐 0.09~0.17.
AUTO_CENTER = (1855, 840)
AUTO_BOX = 56
AUTO_RING_R = (17, 26)
AUTO_RING_MIN = 0.04
AUTO_OFF_CONFIRM = 2       # 이만큼 연속으로 꺼짐이어야 G — 한 번 깜빡임에 끄지 않게
AUTO_KEY_COOLDOWN_SEC = 3.0

# ── 5) 결과 창 ───────────────────────────────────────────
# 가운데 패널을 OCR — "에시르 80 … 패배 제단 활성화 목표 킬수 80 보상 바니르(아군) 39 …".
# 사망 화면은 같은 자리에서 아무것도 안 읽힌다 (녹화 4장).
RESULT_CENTER = (960, 500)
RESULT_W = 560
RESULT_H = 400
RESULT_KEYWORDS = ("보상", "패배", "승리")
RESULT_CLICK = (972, 539)   # 녹화에서 누른 자리 (패널 가운데)

POLL_SEC = 1.0
MATCH_TIMEOUT_SEC = 600.0   # 매칭 대기 상한 — 녹화에선 약 95초
BATTLE_MAX_SEC = 720.0      # 입장부터 결과까지 상한 — 경기 5분 + 카운트다운·부활
FIELD_TIMEOUT_SEC = 60.0    # 결과 창 닫은 뒤 필드 HUD 가 보일 때까지
# 결과 창을 못 읽었을 때의 대비 — 주황 띠 없이 HUD 가 이만큼 보이면 이미 필드다
FIELD_FALLBACK_SEC = 10.0


def _grab(center, w, h, win) -> np.ndarray:
    """BASE 중심·크기 영역을 BASE 크기의 BGR 로 캡처."""
    _hwnd, bx, by, cw, ch = win
    region = center_to_abs_region(center, w, h, bx, by, cw, ch)
    return cv2.cvtColor(grab_base(region, cw, ch), cv2.COLOR_RGB2BGR)


def _ocr_box(center, w, h, win) -> str:
    img = _grab(center, w, h, win)
    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return ocr_text(cv2.cvtColor(big, cv2.COLOR_BGR2RGB))


def _in_arena(win) -> bool:
    b, g, r = (float(v) for v in np.median(
        _grab(ARENA_BAR_CENTER, ARENA_BAR_W, ARENA_BAR_H, win).reshape(-1, 3), 0))
    if r >= 100 and r - b >= 70 and 0.3 * r <= g <= 0.7 * r:
        return True
    if _ARENA_TEAMS_RE.search(_ocr_box(ARENA_TEAMS_CENTER, ARENA_TEAMS_W, ARENA_TEAMS_H, win)):
        return True
    return bool(_ARENA_TIMER_RE.search(
        _ocr_box(ARENA_TIMER_CENTER, ARENA_TIMER_W, ARENA_TIMER_H, win)))


def _auto_ratio(win) -> float:
    img = _grab(AUTO_CENTER, AUTO_BOX, AUTO_BOX, win).astype(int)
    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    k = w / AUTO_BOX   # 창 크기가 BASE 와 다르면 반지름도 같이
    d = np.hypot(xx - w / 2, yy - h / 2)
    ring = (d > AUTO_RING_R[0] * k) & (d < AUTO_RING_R[1] * k)
    return float((img[ring].min(axis=1) > 190).mean())


def _hostile_ratio(win) -> float:
    img = _grab(HOSTILE_CENTER, HOSTILE_SIZE, HOSTILE_SIZE, win).astype(int)
    b, g, r = img[..., 0], img[..., 1], img[..., 2]
    return float(((r > 170) & (g > 130) & (r - b > 60)).mean())


def _hostile_icon_score(win) -> float:
    """그 자리가 적대 범위 공격(칼) 아이콘인가 — 회색·금색 모두 모양으로 본다."""
    global _hostile_icon_cache
    if _hostile_icon_cache is None:
        _hostile_icon_cache = cv2.cvtColor(cv2.imread(str(HOSTILE_ICON_PATH)), cv2.COLOR_BGR2GRAY)
    img = cv2.cvtColor(_grab(HOSTILE_CENTER, HOSTILE_SIZE * 2, HOSTILE_SIZE * 2, win),
                       cv2.COLOR_BGR2GRAY)
    return float(cv2.matchTemplate(img, _hostile_icon_cache, cv2.TM_CCOEFF_NORMED).max())


def _hud_visible(win) -> bool:
    """사망·로딩 화면이 아닌가. 둘 다 HUD 가 통째로 사라진다."""
    return _hostile_ratio(win) >= HOSTILE_GOLD_MIN or _auto_ratio(win) >= AUTO_RING_MIN


def _result_text(win) -> str:
    img = _grab(RESULT_CENTER, RESULT_W, RESULT_H, win)
    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    text = ocr_text(cv2.cvtColor(big, cv2.COLOR_BGR2RGB))
    return text if any(k in text for k in RESULT_KEYWORDS) else ""


def _press_preset(preset: str, why: str):
    vk = SKILL_PRESET_KEYS.get(preset)
    if vk is None:
        logger.warning("[발할라] 알 수 없는 프리셋 '%s' — 키 입력 생략", preset)
        return
    _press_key(vk)
    logger.info("[발할라] 프리셋 %s (%s)", preset, why)


def _ensure_hostile_on(win):
    """적대 범위 공격 켜기. 누를 때마다 뒤집히므로 눌러 보고 다시 확인한다."""
    _hwnd, bx, by, cw, ch = win
    for i in range(HOSTILE_MAX_CLICKS + 1):
        ratio = _hostile_ratio(win)
        if ratio >= HOSTILE_GOLD_MIN:
            logger.info("[발할라] 적대 범위 공격 켜짐 (금색 %.2f)", ratio)
            return
        if i == HOSTILE_MAX_CLICKS:
            break
        icon = _hostile_icon_score(win)
        if icon < HOSTILE_ICON_MIN:
            logger.warning("[발할라] 그 자리에 적대 범위 공격(칼) 아이콘이 없음 (모양 %.2f) — 누르지 않음",
                           icon)
            return
        logger.info("[발할라] 적대 범위 공격 꺼짐 (금색 %.2f, 모양 %.2f) → 클릭", ratio, icon)
        click_base(HOSTILE_CENTER, bx, by, cw, ch)
        _sleep_cancellable(1.0)
    logger.warning("[발할라] 적대 범위 공격을 켰는지 확인 못 함 — 그대로 진행")


def _wait_for_arena(win) -> bool:
    logger.info("[발할라] 매칭 대기 (최대 %.0f분)", MATCH_TIMEOUT_SEC / 60)
    start = time.time()
    while time.time() - start < MATCH_TIMEOUT_SEC:
        _check_cancel()
        if _in_arena(win):
            logger.info("[발할라] 경기장 입장 (매칭 %.0fs)", time.time() - start)
            return True
        _sleep_cancellable(POLL_SEC)
    return False


def _fight_until_result(win) -> bool:
    """자동 전투만 유지하다가 결과 창이 뜨면 닫는다.
    반환: True=결과 창 닫음, False=결과 창 없이 필드로 돌아온 것 같음(대비 경로)."""
    _hwnd, bx, by, cw, ch = win
    start = time.time()
    off_streak = 0
    last_key = 0.0
    field_since: float | None = None
    while time.time() - start < BATTLE_MAX_SEC:
        _check_cancel()
        if _in_arena(win):
            field_since = None
            ratio = _auto_ratio(win)
            # 입장 직후 카운트다운 동안은 HUD 가 숨어 링도 0 으로 보인다(녹화 100~110초).
            # 그때 G 를 누르면 켜 놓고도 안 보여서 한 번 더 눌러 꺼 버린다 — HUD 가
            # 보일 때(켜 둔 적대 범위 공격이 금색으로 보일 때)만 판정한다.
            if _hostile_ratio(win) < HOSTILE_GOLD_MIN:
                off_streak = 0
            else:
                off_streak = off_streak + 1 if ratio < AUTO_RING_MIN else 0
            if (off_streak >= AUTO_OFF_CONFIRM
                    and time.time() - last_key >= AUTO_KEY_COOLDOWN_SEC):
                _press_key(_VK_G)
                last_key = time.time()
                off_streak = 0
                logger.info("[발할라] AUTO 꺼짐 (링 %.2f) → G", ratio)
        else:
            off_streak = 0
            text = _result_text(win)
            if text:
                logger.info("[발할라] 결과 창: %s", text)
                click_base(RESULT_CLICK, bx, by, cw, ch)
                _sleep_cancellable(2.0)
                if not _result_text(win):
                    return True
                continue   # 안 닫혔으면 다음 바퀴에 다시 누른다
            # 결과 창을 못 읽고 지나갔을 때 — 주황 띠 없이 HUD 가 계속 보이면 필드다.
            # 사망 화면은 HUD 가 없어 여기 걸리지 않는다.
            if _hud_visible(win):
                field_since = field_since or time.time()
                if time.time() - field_since >= FIELD_FALLBACK_SEC:
                    logger.warning("[발할라] 결과 창을 못 봤지만 필드로 돌아온 것 같음")
                    return False
            else:
                field_since = None
        _sleep_cancellable(POLL_SEC)
    logger.warning("[발할라] %.0f분 안에 결과 창 없음 — 종료 처리", BATTLE_MAX_SEC / 60)
    return False


def _wait_for_field(win) -> bool:
    start = time.time()
    seen = 0
    while time.time() - start < FIELD_TIMEOUT_SEC:
        _check_cancel()
        seen = seen + 1 if (not _in_arena(win) and _hud_visible(win)) else 0
        if seen >= 3:
            logger.info("[발할라] 필드 복귀 (%.0fs)", time.time() - start)
            return True
        _sleep_cancellable(POLL_SEC)
    return False


def _join_label(abs_xy, win) -> tuple[float, float]:
    """참가 아이콘 아래 글자의 (흰 비율, 노랑 비율)."""
    _hwnd, bx, by, cw, ch = win
    w = max(1, scale_w(JOIN_LABEL_W, cw))
    h = max(1, scale_h(JOIN_LABEL_H, ch))
    cx = abs_xy[0] + scale_w(JOIN_LABEL_OFFSET[0], cw)
    cy = abs_xy[1] + scale_h(JOIN_LABEL_OFFSET[1], ch)
    with mss.mss() as sct:
        shot = sct.grab({"left": cx - w // 2, "top": cy - h // 2, "width": w, "height": h})
    p = np.array(Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)).astype(int)
    r, g, b = p[..., 0], p[..., 1], p[..., 2]
    white = float((p.min(axis=-1) > 150).mean())
    yellow = float(((r > 120) & (g > 100) & (r - b > 70) & (g - b > 50)).mean())
    return white, yellow


class RegistrationClosed(Exception):
    """참가 아이콘 자체가 없다 — 이 회차는 더 시도해도 소용없다."""


def _join(win) -> bool:
    """참가 등록. 반환: True=지금 참가함, False=이미 매칭 중이라 누르지 않음."""
    _hwnd, bx, by, cw, ch = win
    start = time.time()
    icon_seen = matching_seen = False
    while time.time() - start < JOIN_TIMEOUT_SEC:
        _check_cancel()
        present, score, abs_xy = _match_region(
            JOIN_IMAGE, JOIN_CENTER, JOIN_W, JOIN_H, bx, by, cw, ch, JOIN_THRESHOLD)
        if present:
            icon_seen = True
            white, yellow = _join_label(abs_xy, win)
            if white >= JOIN_WHITE_MIN:
                click_at_screen(*abs_xy)
                logger.info("[발할라] 참가 아이콘 클릭 @%s (score=%.2f, 흰 글자 %.3f)",
                            abs_xy, score, white)
                if not _find_and_click(CONFIRM_IMAGE, CONFIRM_CENTER, CONFIRM_W,
                                       CONFIRM_H, bx, by, cw, ch,
                                       timeout=CONFIRM_TIMEOUT_SEC):
                    raise TimeoutError("참가 확인 창의 '확인' 버튼 없음")
                return True
            if yellow >= JOIN_YELLOW_MIN:
                matching_seen = True
                break
        _sleep_cancellable(0.5)
    if matching_seen:
        logger.info("[발할라] 이미 매칭 중 ('매칭중' 표시) — 참가 클릭 없이 입장 대기")
        return False
    if icon_seen:
        raise TimeoutError("참가 아이콘은 있지만 등록 가능 표시(흰 카운트다운)도 '매칭중' 도 없음")
    raise RegistrationClosed("참가 등록 아이콘 없음 — 등록이 끝났거나 이미 치렀음")


def run_valhalla():
    """발할라 대전 한 판 — 공용 큐 워커 스레드에서 동기 실행."""
    _cancel_event.clear()
    logger.info("[발할라] 시작 (Ctrl+Esc 로 중단 가능)")
    try:
        win = find_game_window()
        hwnd, bx, by, cw, ch = win
        ensure_game_foreground()
        _sleep_cancellable(0.5)

        try:
            if _join(win):
                _sleep_cancellable(1.0)
        except RegistrationClosed as e:
            # 실패로 끝내면 2분마다 다시 들어와 창을 잡아챈다. 정상 종료로 회차를 닫는다.
            logger.info("[발할라] %s — 이번 회차는 끝", e)
            return

        _ensure_hostile_on(win)

        if not _wait_for_arena(win):
            raise TimeoutError(f"{MATCH_TIMEOUT_SEC / 60:.0f}분 안에 매칭 안 됨")
        from odin.automation.automation_config import get_valhalla_preset
        _press_preset(get_valhalla_preset(), "경기장 입장")

        if _fight_until_result(win):
            if not _wait_for_field(win):
                logger.warning("[발할라] 필드 복귀 확인 못 함 — 프리셋만 바꾸고 끝")

        from odin.automation.automation_config import get_idle_preset
        _press_preset(get_idle_preset(), "필드 복귀")
        logger.info("[발할라] 완료")
    except AutomationCancelled:
        logger.info("[발할라] ⏹ Ctrl+Esc 로 중단됨")
        raise
