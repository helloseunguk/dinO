"""
보스 임박(SOON) 시 자동 클릭 시퀀스.

플로우:
  1) 시간표 아이콘 클릭 (게임 내)
  2) 해당 지역 탭 클릭 (게임 내)
  3) 보스의 시간 셀 클릭 (게임 내)
  4) 절대 좌표 (4402, 967) 클릭
  5) 절대 좌표 (3595, 585) 클릭
  6) OCR로 (3494, 669, 96x29) 영역에서 'm' 사라질 때까지 폴링 (최대 대기시간)
  7) 절대 좌표 (4411, 841) 클릭
"""

import ctypes
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import cv2
import mss
import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Signal, Slot

from automation_config import REGION_PRIORITY, is_automation_target, priority_key
from boss_config import TAB_BOSS_MAP, TAB_TIME_COORDS
from game_coords import (
    find_game_window, scale_xy,
    click_game, click_base, click_at_screen, center_to_abs_region,
    scroll_wheel_down,
)
from models import BossState
from ocr_engine import ocr_text
from paths import BUNDLED_DIR, DATA_DIR
from screen_sync import SCHEDULE_ICON, TAB_COORDS

# 모듈 내부 alias (기존 코드 호환)
_find_game_window = find_game_window
_scale_xy = scale_xy
_click_game = click_game
_click_game_base = click_base
_center_to_abs_region = center_to_abs_region
_scroll_wheel_down = scroll_wheel_down

logger = logging.getLogger(__name__)

# boss.config.region → 시간표 탭 이름
_REGION_TO_TAB = {
    "요툰":   "요툰하임",
    "니다벨": "니다벨리르",
    "알브":   "알브하임",
    "무스펠": "무스펠하임",
    "아스":   "아스가르드",
    "니플":   "니플하임",
    "던전":   "던전",
    "미드":   "미드가르드",
}

# === 게임 내부 좌표 (BASE 1920×1009 기준) ===
# 런타임에 _scale_xy + bx/by 오프셋 적용 → 실제 화면 좌표로 변환됨
# screen_sync.TAB_TIME_COORDS와 동일한 좌표 시스템.

# Step 4-5: 이미지 매칭으로 버튼 위치 찾아 클릭. 탐색 영역(BASE 중심 + W×H)
# 안에서 템플릿과 매칭되는 지점의 중심을 절대좌표로 클릭.
BOSS_MOVE_4_IMAGE = "boss_move_4.png"
BOSS_MOVE_4_CENTER = (1840, 960)
BOSS_MOVE_4_W = 200
BOSS_MOVE_4_H = 100

BOSS_MOVE_5_IMAGE = "boss_move_5.png"
BOSS_MOVE_5_CENTER = (990, 585)
BOSS_MOVE_5_W = 120
BOSS_MOVE_5_H = 80

MOVE_IMAGE_THRESHOLD = 0.7
MOVE_IMAGE_TIMEOUT = 10.0      # 이미지 나타날 때까지 최대 대기 (초)
MOVE_IMAGE_POLL_INTERVAL = 0.3

# 'm' 감시 영역 — 중심 좌표 + 크롭 크기 (TAB_COORDS와 동일한 방식)
OCR_CENTER = (966, 600)    # 게임 내부 좌표 (BASE 1920×1009)
OCR_CROP_W = 150
OCR_CROP_H = 50

# 각 액션 사이 딜레이
STEP_DELAY = 0.5
# 게임 로딩 완료 감지 — camera2.png 가 노출되면 로딩 끝났다고 판정
# (CAMERA_CHECK_* 상수는 _run_camera_check 정의부 근처에 위치)
LOADING_DETECT_TIMEOUT_SEC = 15.0    # 그 안에 안 뜨면 폴백으로 그냥 진행
LOADING_DETECT_INTERVAL = 0.3
LOADING_DETECT_PRE_DELAY = 0.5       # 로딩 화면이 뜰 시간 확보용 짧은 settle
POST_LOADING_DELAY_SEC = 2.0         # camera2 감지 직후 settle — 이동 텍스트 안정화 대기

# === Ctrl+Esc 키로 중단 기능 ===
_VK_ESCAPE = 0x1B
_VK_CONTROL = 0x11
_cancel_event = threading.Event()
# Ctrl+Esc 감지 시 추가로 호출할 콜백 (자동화 인스턴스가 본인 정리 로직 등록용)
_on_esc_callbacks: list = []


def register_esc_callback(fn):
    """Ctrl+Esc 눌린 순간 호출할 콜백 등록. 예외는 무시됨."""
    _on_esc_callbacks.append(fn)


def cancel_current():
    """진행 중인 자동화 시퀀스에 중단 신호 — _run_sequence 가 다음
    _check_cancel 시점에서 AutomationCancelled 로 종료됨 (Ctrl+Esc 와 동일).
    큐/추적 상태 초기화는 별도로 BossAutomator.reset() 호출할 것.
    """
    _cancel_event.set()


class AutomationCancelled(Exception):
    """Ctrl+Esc 로 자동화가 중단됨을 알리는 예외."""
    pass


def _esc_watcher_loop():
    """백그라운드에서 Ctrl+Esc 조합 감시 — 둘 다 눌린 순간(rising edge) 취소 플래그 set."""
    prev_pressed = False
    while True:
        try:
            esc_down = bool(ctypes.windll.user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000)
            ctrl_down = bool(ctypes.windll.user32.GetAsyncKeyState(_VK_CONTROL) & 0x8000)
            pressed = esc_down and ctrl_down
            if pressed and not prev_pressed:
                _cancel_event.set()
                logger.info("[자동화] Ctrl+Esc 감지 → 모든 작업 중단 요청")
                for cb in _on_esc_callbacks:
                    try:
                        cb()
                    except Exception as e:
                        logger.warning("[자동화] Ctrl+Esc 콜백 실행 실패: %s", e)
            prev_pressed = pressed
        except Exception:
            pass
        time.sleep(0.05)


# 모듈 로드 시 한 번만 watcher 스레드 시작
_watcher_thread = threading.Thread(
    target=_esc_watcher_loop, daemon=True, name="EscWatcher")
_watcher_thread.start()


def _check_cancel():
    if _cancel_event.is_set():
        raise AutomationCancelled("Ctrl+Esc 로 중단됨")


def _sleep_cancellable(duration: float):
    """time.sleep을 더 짧은 chunk로 쪼개서 중단 감지."""
    end = time.time() + duration
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        _check_cancel()
        time.sleep(min(0.05, remaining))

# 폴링 설정
POLL_INTERVAL_SEC = 0.5
MAX_WAIT_SECONDS = 60           # 전체 대기 최대 시간 (도착까지)
EMPTY_CONFIRM_COUNT = 10        # 'm' 사라진 뒤 빈 상태 연속 관측 횟수 (0.5s × 10 = 5초)
# 'B' 전략: Phase 1 → Phase 2 전환 규칙
#   1. 빠른 경로: 마지막 관측 거리 ≤ NEAR_ARRIVAL_DIST (= 10m) → 즉시 Phase 2
#   2. 폴백 경로: m 이 M_ABSENCE_FORCE_PHASE2_SEC (= 15s) 이상 연속 안 보임 → Phase 2
#      (도착 후 거리 표시가 먼 값에서 바로 사라지는 UI 변화 대응)
NEAR_ARRIVAL_DIST = 10
M_ABSENCE_FORCE_PHASE2_SEC = 15.0
# 'A' 전략: Phase 2 에서 EMPTY_CONFIRM_COUNT 달성 후 이 시간 동안 2차 확인.
# 이 동안 m 이 다시 나타나면 도착 판정 취소하고 Phase 2 계속.
FINAL_CONFIRM_WAIT_SEC = 3.0

# === 미니맵 정지 감지 (도착 빠른 판정 보조) ===
# 미니맵을 매 폴링마다 캡처해 직전 프레임과 cv2.absdiff → 이진화 → 변경 픽셀 비율로
# '정지'를 판정. m_seen_once 인 상태에서 미니맵이 N회 연속 정지 + (m 사라짐 또는 ≤10m)
# 이면 Phase 2/2차 확인 건너뛰고 즉시 도착 확정.
USE_MINIMAP_FAST_PATH = True
MINIMAP_CENTER = (137, 209)
MINIMAP_W = 200
MINIMAP_H = 200
MINIMAP_PIXEL_DIFF_THRESHOLD = 30      # 0~255 — 이 이상 차이 픽셀만 '변경'으로 인정
MINIMAP_CHANGE_PCT_THRESHOLD = 5.0     # 변경 픽셀 비율(%) 미만이면 '정지'
MINIMAP_STATIC_STREAK_THRESHOLD = 10   # 연속 정지 횟수 (0.5s × 10 = 5.0초)
# 빠른 판정에서 'm 사라짐' 으로 간주하기 전 연속 미검출 필요 횟수.
# OCR 한 번 글리치로 사라졌다고 즉시 도착 처리되는 걸 방지.
M_ABSENT_STREAK_FOR_FAST_PATH = 5      # 0.5s × 5 = 2.5초 연속 미검출

# === OCR 디버그 저장 ===
# True 면 _wait_until_arrival 폴링마다 'm' OCR 영역 이미지를 DATA_DIR/ocr_debug 에 저장.
# 파일명 = {sequence}_{HH-MM-SS-ms}_m={value}.png
# 디스크 채우기 방지를 위해 새 시퀀스 시작 시 디렉토리 정리.
OCR_DEBUG_SAVE = False
OCR_DEBUG_KEEP_LATEST = 500            # 시퀀스 시작 시 가장 오래된 것부터 잘라냄


# _click_game_base / _center_to_abs_region 은 game_coords 로 이전 (파일 상단 alias)


# Virtual-Key Code (Windows)
_VK_F = 0x46
_VK_G = 0x47
_VK_C = 0x43
_VK_DELETE = 0x2E
_VK_NEXT = 0x22          # PageDown
# G → F 사이 딜레이
KEY_GF_DELAY = 0.3

# === IDLE 진입 시 카메라 뷰 보정 ===
# camera2.png 와 매칭 점수가 임계값에 도달할 때까지 1초 간격으로 C 키 입력.
# 최대 CAMERA_CHECK_MAX_ITERS 회 시도 후 타임아웃.
CAMERA_CHECK_IMAGE = "camera2.png"
CAMERA_CHECK_CENTER = (30, 441)
CAMERA_CHECK_W = 50
CAMERA_CHECK_H = 50
CAMERA_CHECK_THRESHOLD = 0.9
CAMERA_CHECK_INTERVAL = 1.0
CAMERA_CHECK_MAX_ITERS = 30

# 보스별 스킬 프리셋 → Virtual-Key 매핑.
# 도착 시 G+F 다음에 해당 키를 눌러 게임 내 스킬셋을 전환.
# IDLE 복귀 시에는 'A' 키를 눌러 기본 프리셋으로 되돌림.
SKILL_PRESET_KEYS = {
    "A": _VK_DELETE,     # Delete
    "C": _VK_NEXT,       # PageDown
}


def _press_key(vk_code: int):
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)          # key down
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(vk_code, 0, 0x0002, 0)     # key up


# 줌아웃 (게임 중앙 휠 스크롤 다운) — m이 안 보이면 1초마다 10단계씩
ZOOM_OUT_SCROLLS = 10
ZOOM_INTERVAL_SEC = 1.0
# _scroll_wheel_down 은 game_coords 로 이전 (파일 상단 alias)


# ─── 이미지 템플릿 매칭 공용 헬퍼 ───────────────────────────
# 파일명 기반 캐시 → 여러 단계에서 재사용. auto.png 는 기존 _auto_template_cache 유지.
_image_template_cache: dict[str, np.ndarray] = {}


def _load_image_template(filename: str) -> np.ndarray:
    """images/{filename} 템플릿 1회 로드 후 캐시."""
    if filename in _image_template_cache:
        return _image_template_cache[filename]
    path = BUNDLED_DIR / "images" / filename
    if not path.exists():
        raise FileNotFoundError(f"템플릿 없음: {path}")
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"템플릿 로드 실패: {path}")
    _image_template_cache[filename] = img
    logger.info("[자동화] 템플릿 로드: %s (%dx%d)",
                filename, img.shape[1], img.shape[0])
    return img


def _match_region(filename: str,
                  center_base: tuple[int, int], w_base: int, h_base: int,
                  bx: int, by: int, cw: int, ch: int,
                  threshold: float
                  ) -> tuple[bool, float, tuple[int, int]]:
    """BASE 중심/크기 영역 캡처 + 템플릿 매칭.
    반환: (존재여부, max_score, 매칭 중심의 절대 스크린 좌표)
    """
    template = _load_image_template(filename)
    region = _center_to_abs_region(
        center_base, w_base, h_base, bx, by, cw, ch)
    with mss.mss() as sct:
        shot = sct.grab(region)
    img_rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    if (img_bgr.shape[0] < template.shape[0]
            or img_bgr.shape[1] < template.shape[1]):
        logger.warning("[자동화] '%s' 탐색영역(%dx%d) < 템플릿(%dx%d)",
                       filename,
                       img_bgr.shape[1], img_bgr.shape[0],
                       template.shape[1], template.shape[0])
        return False, 0.0, (0, 0)
    result = cv2.matchTemplate(img_bgr, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    abs_cx = region["left"] + max_loc[0] + template.shape[1] // 2
    abs_cy = region["top"] + max_loc[1] + template.shape[0] // 2
    return max_val >= threshold, max_val, (abs_cx, abs_cy)


def _find_and_click(filename: str,
                    center_base: tuple[int, int], w_base: int, h_base: int,
                    bx: int, by: int, cw: int, ch: int,
                    threshold: float = MOVE_IMAGE_THRESHOLD,
                    timeout: float = MOVE_IMAGE_TIMEOUT,
                    interval: float = MOVE_IMAGE_POLL_INTERVAL) -> bool:
    """이미지 찾아 매칭 중심을 절대좌표로 클릭.
    내부 폴링으로 이미지 등장 대기 → 등장 즉시 클릭 (암묵적 단계 검증).
    성공=True / 타임아웃=False.
    """
    start = time.time()
    best = 0.0
    while time.time() - start < timeout:
        _check_cancel()
        present, score, abs_xy = _match_region(
            filename, center_base, w_base, h_base, bx, by, cw, ch, threshold)
        if score > best:
            best = score
        if present:
            click_at_screen(abs_xy[0], abs_xy[1])
            logger.info("[자동화] '%s' 클릭 @%s (score=%.2f, 경과 %.1fs)",
                        filename, abs_xy, score, time.time() - start)
            return True
        _sleep_cancellable(interval)
    logger.warning("[자동화] '%s' 미감지 → 클릭 실패 (%0.fs, 최고 score=%.2f)",
                   filename, timeout, best)
    return False


def _run_sequence(boss: BossState, screen_sync):
    """자동화 시퀀스 — 동기 실행 (공용 큐 워커 스레드에서 호출됨)."""
    # 이전 Ctrl+Esc 요청 리셋 — 이번 시퀀스의 Ctrl+Esc 만 반영
    _cancel_event.clear()
    logger.info("[자동화] 시작: %s (Ctrl+Esc 로 중단 가능)",
                boss.config.display_name)

    try:
        hwnd, bx, by, cw, ch = _find_game_window()
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        _sleep_cancellable(STEP_DELAY)

        # 1) 시간표 열기
        gx, gy = _scale_xy(SCHEDULE_ICON, cw, ch)
        _click_game(gx, gy, bx, by)
        _sleep_cancellable(STEP_DELAY)

        # 2) 지역 탭 클릭
        tab_name = _REGION_TO_TAB.get(boss.config.region)
        if tab_name is None or tab_name not in TAB_COORDS:
            raise ValueError(f"탭 매핑 실패: region='{boss.config.region}'")
        tx, ty = _scale_xy(TAB_COORDS[tab_name], cw, ch)
        _click_game(tx, ty, bx, by)
        _sleep_cancellable(STEP_DELAY)

        # 3) 보스 시간 셀 클릭
        configs = TAB_BOSS_MAP.get(tab_name, [])
        coords = TAB_TIME_COORDS.get(tab_name, [])
        idx = next(
            (i for i, c in enumerate(configs) if c.name == boss.config.name), None
        )
        if idx is None:
            raise ValueError(f"탭 '{tab_name}'에 보스 '{boss.config.name}' 없음")
        if idx >= len(coords):
            raise ValueError(f"시간 좌표 부족: idx={idx}, len={len(coords)}")
        tcx, tcy = _scale_xy(coords[idx], cw, ch)
        _click_game(tcx, tcy, bx, by)
        _sleep_cancellable(STEP_DELAY)

        # 4) boss_move_4 버튼 이미지 매칭 → 클릭 (폴링 대기 = 암묵적 검증)
        if not _find_and_click(
                BOSS_MOVE_4_IMAGE,
                BOSS_MOVE_4_CENTER, BOSS_MOVE_4_W, BOSS_MOVE_4_H,
                bx, by, cw, ch):
            raise TimeoutError(f"'{BOSS_MOVE_4_IMAGE}' 미감지 — 시퀀스 중단")

        # 5) boss_move_5 버튼 이미지 매칭 → 클릭
        # (_find_and_click 의 폴링이 step 4 → 5 전환 대기 역할을 겸함)
        if not _find_and_click(
                BOSS_MOVE_5_IMAGE,
                BOSS_MOVE_5_CENTER, BOSS_MOVE_5_W, BOSS_MOVE_5_H,
                bx, by, cw, ch):
            raise TimeoutError(f"'{BOSS_MOVE_5_IMAGE}' 미감지 — 시퀀스 중단")
        _sleep_cancellable(STEP_DELAY)

        # 6) 게임 로딩 완료 감지 — camera2.png 가 노출될 때까지 대기 후 도착 판정 시작
        _wait_for_loading_complete(bx, by, cw, ch)
        # 카메라 노출 직후 캐릭터·UI 가 settle 되도록 추가 대기 (이동 거리 텍스트 안정화)
        _sleep_cancellable(POST_LOADING_DELAY_SEC)

        abs_region = _center_to_abs_region(
            OCR_CENTER, OCR_CROP_W, OCR_CROP_H, bx, by, cw, ch)
        logger.info("[자동화] 도착 대기 시작 (최대 %d초) region=%s",
                    MAX_WAIT_SECONDS, abs_region)
        _wait_until_arrival(abs_region, bx, by, cw, ch)

        # 도착 직후 settle — 게임이 G/F 입력을 안정적으로 받도록 짧은 대기
        _sleep_cancellable(0.5)

        # 7) G 키 입력
        _press_key(_VK_G)
        _sleep_cancellable(KEY_GF_DELAY)

        # 8) F 키 입력
        _press_key(_VK_F)
        logger.info("[자동화] G + F 키 입력 완료")

        # 9) 스킬 프리셋 키 — 보스별 설정 (기본 A=Delete, C=PageDown)
        from automation_config import get_preset
        preset = get_preset(boss.config.name)
        preset_vk = SKILL_PRESET_KEYS.get(preset)
        if preset_vk is not None:
            _sleep_cancellable(KEY_GF_DELAY)
            _press_key(preset_vk)
            logger.info("[자동화] 스킬 프리셋 '%s' 키 입력", preset)
        else:
            logger.warning("[자동화] 알 수 없는 프리셋 '%s' — 키 입력 생략", preset)
    except AutomationCancelled:
        logger.info("[자동화] ⏹ Ctrl+Esc 로 중단됨: %s", boss.config.display_name)
        raise


_OCR_DEBUG_DIR = DATA_DIR / "ocr_debug"
_ocr_debug_seq = 0


def _ocr_debug_prepare():
    """디버그 디렉토리 보장 + 가장 오래된 파일들부터 잘라내 KEEP_LATEST 만 유지."""
    if not OCR_DEBUG_SAVE:
        return
    try:
        _OCR_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(_OCR_DEBUG_DIR.glob("*.png"),
                       key=lambda p: p.stat().st_mtime)
        excess = len(files) - OCR_DEBUG_KEEP_LATEST
        for f in files[:max(0, excess)]:
            try:
                f.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.warning("[OCR디버그] 정리 실패: %s", e)


def _ocr_debug_save(img_gray: np.ndarray, m_val: int | None, text: str):
    """OCR 영역 그레이스케일 이미지를 PNG 로 저장. 파일명에 m값 + 정리된 텍스트 포함."""
    if not OCR_DEBUG_SAVE:
        return
    global _ocr_debug_seq
    _ocr_debug_seq += 1
    try:
        ts = time.strftime("%H-%M-%S") + f"-{int((time.time() % 1) * 1000):03d}"
        val = "NONE" if m_val is None else str(m_val)
        sanitized = re.sub(r"[^a-zA-Z0-9가-힣]", "_", text or "")[:30]
        path = (_OCR_DEBUG_DIR
                / f"{_ocr_debug_seq:05d}_{ts}_m={val}_{sanitized}.png")
        cv2.imwrite(str(path), img_gray)
    except Exception as e:
        logger.debug("[OCR디버그] 저장 실패: %s", e)


def _capture_gray(sct, abs_region: dict) -> np.ndarray:
    shot = sct.grab(abs_region)
    img_rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb))
    return np.dot(img_rgb[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)


def _extract_m_distance(text: str) -> int | None:
    """OCR 결과에서 거리값(정수 m) 추출. 매칭 없으면 None.
    공백만 제거 후 'm 바로 앞에 연속된 숫자' 만 매칭. 다른 문자(한글/기호)가 끼면
    그 앞 숫자는 무시. 예:
      '3 7m' → '37m' → 37 (공백만 제거되어 연속 숫자)
      '12 후 145m' → '12후145m' → 145 ('m' 직전 연속 숫자만)
    """
    cleaned = re.sub(r"\s+", "", text.lower())
    m = re.search(r"(\d+)m\b", cleaned)
    return int(m.group(1)) if m else None


def _capture_minimap_gray(sct, bx: int, by: int, cw: int, ch: int) -> np.ndarray:
    """미니맵 영역 캡처 → 그레이스케일 ndarray."""
    region = _center_to_abs_region(
        MINIMAP_CENTER, MINIMAP_W, MINIMAP_H, bx, by, cw, ch)
    shot = sct.grab(region)
    img_rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb))
    return np.dot(img_rgb[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)


def _is_minimap_static(prev: np.ndarray, curr: np.ndarray) -> tuple[bool, float]:
    """이전/현재 미니맵 그레이스케일 비교.
    반환: (정지 여부, 변경 픽셀 비율 %).
    """
    diff = cv2.absdiff(prev, curr)
    _, binary = cv2.threshold(
        diff, MINIMAP_PIXEL_DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
    change_pct = (cv2.countNonZero(binary) / binary.size) * 100
    return change_pct < MINIMAP_CHANGE_PCT_THRESHOLD, change_pct


def _secondary_confirm(sct, abs_region: dict) -> bool:
    """Phase 2 확정 후 FINAL_CONFIRM_WAIT_SEC 동안 추가 검증.
    그 동안 m 이 한 번이라도 재등장하면 False 반환 (도착 취소).
    """
    end = time.time() + FINAL_CONFIRM_WAIT_SEC
    while time.time() < end:
        _check_cancel()
        img_gray = _capture_gray(sct, abs_region)
        text = _read_m_region(img_gray)
        if _extract_m_distance(text) is not None:
            logger.info("[자동화] 2차 확인 — m 재등장 (OCR='%s') → 도착 취소", text)
            return False
        _sleep_cancellable(POLL_INTERVAL_SEC)
    return True


def _wait_until_arrival(abs_region: dict,
                        bx: int, by: int, cw: int, ch: int):
    """
    2단계 + 2차 확인 폴링으로 도착을 확정.

    Phase 1: 'm' 출현 → 사라짐 대기
             - m 아직 안 보임: 1초마다 휠다운 줌아웃
             - m 한 번 보이면: 마지막 거리 추적(last_m_value)
             - m 사라짐: last_m_value ≤ NEAR_ARRIVAL_DIST 일 때만 Phase 2 진입
               (거리가 아직 멀었을 때 OCR 글리치로 인한 오진입 방지)
    Phase 2: m 없음 EMPTY_CONFIRM_COUNT 회 연속 → FINAL_CONFIRM_WAIT_SEC 2차 확인
             2차 확인 중 m 재등장 시 Phase 2 재시작, 통과 시 도착 확정.
    """
    start = time.time()
    phase = 1
    m_seen_once = False
    last_m_value: int | None = None      # 마지막으로 관측된 거리값 (m)
    last_m_seen_at: float | None = None  # 마지막으로 m 을 본 시각 (time.time)
    empty_streak = 0
    last_zoom = 0.0
    prev_minimap: np.ndarray | None = None
    minimap_static_streak = 0
    m_absent_streak = 0    # 빠른 판정에서 OCR 글리치 방지용 연속 미검출 카운터
    _ocr_debug_prepare()   # 디버그 디렉토리 준비 + 오래된 파일 정리

    with mss.mss() as sct:
        while True:
            _check_cancel()
            elapsed = time.time() - start
            if elapsed > MAX_WAIT_SECONDS:
                # Phase 1 에서 m 을 한 번도 못 본 경우 = 근거리 도착으로 간주
                if phase == 1 and not m_seen_once:
                    logger.info(
                        "[자동화] m 미감지 상태로 %ds 경과 → 근거리 도착으로 간주",
                        MAX_WAIT_SECONDS)
                    return
                raise TimeoutError(
                    f"도착 대기 {MAX_WAIT_SECONDS}초 초과 (phase={phase})"
                )
            img_gray = _capture_gray(sct, abs_region)
            text = _read_m_region(img_gray)
            m_val = _extract_m_distance(text)
            has_m = m_val is not None
            _ocr_debug_save(img_gray, m_val, text)

            # 'm 사라짐' 연속 카운터 — 한 번 보이면 리셋, 미검출이면 +1
            if has_m:
                m_absent_streak = 0
            else:
                m_absent_streak += 1

            # === 미니맵 정지 빠른 판정 ===
            # m 한 번이라도 본 후(이동 시작 확정) + 미니맵 N회 연속 정지 +
            # (m N회 연속 미검출 OR m ≤ 10) 만족 시 Phase 2/2차 확인 건너뛰고 즉시 도착 확정.
            if USE_MINIMAP_FAST_PATH:
                try:
                    curr_minimap = _capture_minimap_gray(sct, bx, by, cw, ch)
                except Exception as e:
                    logger.debug("[미니맵] 캡처 실패 — 빠른 판정 스킵: %s", e)
                    curr_minimap = None
                if curr_minimap is not None and prev_minimap is not None:
                    is_static, change_pct = _is_minimap_static(prev_minimap, curr_minimap)
                    if is_static:
                        minimap_static_streak += 1
                    else:
                        minimap_static_streak = 0
                    logger.debug(
                        "[미니맵] change=%.2f%% static_streak=%d/%d "
                        "m_absent_streak=%d/%d",
                        change_pct, minimap_static_streak,
                        MINIMAP_STATIC_STREAK_THRESHOLD,
                        m_absent_streak, M_ABSENT_STREAK_FOR_FAST_PATH)
                    m_gone_confirmed = m_absent_streak >= M_ABSENT_STREAK_FOR_FAST_PATH
                    m_near = m_val is not None and m_val <= NEAR_ARRIVAL_DIST
                    # 미니맵 정지 + (m 연속 미검출 OR m 근거리) → m_seen_once 무관하게 즉시 도착
                    if (minimap_static_streak >= MINIMAP_STATIC_STREAK_THRESHOLD
                            and (m_gone_confirmed or m_near)):
                        logger.info(
                            "[자동화] 도착 빠른 판정 — 미니맵 %d회 정지 + m %s",
                            minimap_static_streak,
                            f"{m_absent_streak}회 미검출" if m_gone_confirmed
                            else f"{m_val}m")
                        return
                if curr_minimap is not None:
                    prev_minimap = curr_minimap

            if phase == 1:
                logger.debug("[폴링 P1] (%.1fs) OCR='%s' | m값=%s | last=%s | m본적=%s",
                             elapsed, text or "(없음)", m_val, last_m_value, m_seen_once)
                if has_m:
                    last_m_value = m_val
                    last_m_seen_at = time.time()
                    if not m_seen_once:
                        logger.info("[자동화] 'm' 첫 감지 (OCR='%s', 경과 %.1fs)",
                                    text, elapsed)
                    m_seen_once = True
                elif not m_seen_once:
                    # m 아직 못 봄 — 1초마다 줌아웃
                    now = time.time()
                    if now - last_zoom >= ZOOM_INTERVAL_SEC:
                        zx = bx + cw // 2
                        zy = by + ch // 2
                        logger.debug("[자동화] m 미감지 → 게임 중앙(%d,%d) 휠다운 %d단계 (줌아웃)",
                                     zx, zy, ZOOM_OUT_SCROLLS)
                        _scroll_wheel_down(zx, zy, ZOOM_OUT_SCROLLS)
                        last_zoom = now
                else:
                    # m 을 이전에 봤는데 지금은 없음 — 두 경로 중 하나 통과 시 Phase 2
                    absence = (time.time() - last_m_seen_at) if last_m_seen_at else 0.0
                    near = last_m_value is not None and last_m_value <= NEAR_ARRIVAL_DIST
                    timeout_fallback = absence >= M_ABSENCE_FORCE_PHASE2_SEC
                    if near:
                        logger.info(
                            "[자동화] 'm' 사라짐 (마지막 거리 %dm ≤ %dm) → Phase 2",
                            last_m_value, NEAR_ARRIVAL_DIST)
                        phase = 2
                        empty_streak = 0
                    elif timeout_fallback:
                        logger.info(
                            "[자동화] 'm' %0.1fs 이상 미감지 (마지막 %sm) → Phase 2 (폴백)",
                            absence, last_m_value)
                        phase = 2
                        empty_streak = 0
                    else:
                        logger.debug(
                            "[자동화] 'm' 일시 사라짐 (마지막 %sm, 경과 %.1fs) → Phase 1 유지",
                            last_m_value, absence)
            else:  # phase 2 — "m 패턴 없음" 이 연속 N회면 2차 확인
                empty_streak = empty_streak + 1 if not has_m else 0
                logger.debug("[폴링 P2] (%.1fs) OCR='%s' | m없음연속=%d/%d",
                             elapsed, text or "(없음)",
                             empty_streak, EMPTY_CONFIRM_COUNT)
                if empty_streak >= EMPTY_CONFIRM_COUNT:
                    logger.info(
                        "[자동화] Phase 2 통과 — 2차 확인 시작 (%.1fs 추가 검증)",
                        FINAL_CONFIRM_WAIT_SEC)
                    if _secondary_confirm(sct, abs_region):
                        logger.info("[자동화] 2차 확인 통과 → 도착 확정")
                        return
                    # 2차 확인 실패 → Phase 2 계속
                    empty_streak = 0
            _sleep_cancellable(POLL_INTERVAL_SEC)


def _read_m_region(img_gray: np.ndarray) -> str:
    """OCR 전체 텍스트 반환 (Windows OCR 기반)."""
    return ocr_text(img_gray)


# === 스폰 지난 자동화 스킵 규칙 ===
# "무스펠하임 이전" (REGION_PRIORITY 상 무스펠보다 앞) 지역의 보스만,
# 스폰 시간이 이미 지났으면 자동화 스킵.
def _is_stale_high_priority(boss: BossState) -> bool:
    """스폰 시간이 지났고, 무스펠 이전 우선순위 지역이면 True."""
    if boss.next_spawn_at is None:
        return False
    if boss.next_spawn_at > datetime.now():
        return False  # 아직 스폰 시간 안 지남
    try:
        region_idx = REGION_PRIORITY.index(boss.config.region)
        muspel_idx = REGION_PRIORITY.index("무스펠")
    except ValueError:
        return False  # 지역이 목록에 없으면 판단 불가 — skip 안 함
    return region_idx < muspel_idx


# ─── 아이들 액션: 다음 보스까지 N분 이상 남았을 때 실행 ───
IDLE_ACTION_THRESHOLD_SEC = 300.0   # 5분
IDLE_CLICK_1 = (32, 164)            # 게임 내부 BASE 좌표 (인벤토리/메뉴 공통 클릭)

# G 키 누르기 전 auto 아이콘 템플릿 매칭으로 준비 상태 확인
AUTO_IMAGE_CENTER = (1853, 845)     # BASE 좌표
AUTO_IMAGE_W = 70                   # 탐색 박스 폭
AUTO_IMAGE_H = 70                   # 탐색 박스 높이
AUTO_IMAGE_THRESHOLD = 0.7
AUTO_IMAGE_MAX_WAIT_SEC = 30.0      # 최대 대기
AUTO_IMAGE_POLL_INTERVAL = 0.5
_AUTO_TEMPLATE_PATH = BUNDLED_DIR / "images" / "auto.png"
_auto_template_cache: np.ndarray | None = None

# 아이들 모드에서 사용할 아이템 후보 — name → BASE 좌표
IDLE_ITEMS: dict[str, tuple[int, int]] = {
    "마석팔찌": (322, 455),
    "태숭장":   (322, 510),
    "태숭활":   (322, 565),
    "불사망":   (322, 734),
}
DEFAULT_IDLE_ITEM = "태숭장"

# 현재 선택된 아이템 (GUI에서 set_idle_item 으로 변경)
_selected_idle_item: str = DEFAULT_IDLE_ITEM


def set_idle_item(name: str) -> bool:
    """GUI에서 아이들 아이템 선택을 변경. 성공 시 True."""
    global _selected_idle_item
    if name not in IDLE_ITEMS:
        logger.warning("[아이들] 알 수 없는 아이템: %s", name)
        return False
    _selected_idle_item = name
    logger.info("[아이들] 아이템 변경: %s = %s", name, IDLE_ITEMS[name])
    return True


def get_idle_item() -> str:
    return _selected_idle_item


def _load_auto_template() -> np.ndarray:
    """auto.png 템플릿 1회 로드 후 캐시."""
    global _auto_template_cache
    if _auto_template_cache is not None:
        return _auto_template_cache
    if not _AUTO_TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"auto 템플릿 없음: {_AUTO_TEMPLATE_PATH}")
    img = cv2.imread(str(_AUTO_TEMPLATE_PATH), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"auto 템플릿 로드 실패: {_AUTO_TEMPLATE_PATH}")
    _auto_template_cache = img
    logger.info("[아이들] auto 템플릿 로드: %s (%dx%d)",
                _AUTO_TEMPLATE_PATH.name, img.shape[1], img.shape[0])
    return img


def _auto_image_present(bx: int, by: int, cw: int, ch: int) -> tuple[bool, float]:
    """AUTO_IMAGE 영역 1회 캡처 + 템플릿 매칭 → (존재여부, score)."""
    template = _load_auto_template()
    region = _center_to_abs_region(
        AUTO_IMAGE_CENTER, AUTO_IMAGE_W, AUTO_IMAGE_H, bx, by, cw, ch)
    with mss.mss() as sct:
        shot = sct.grab(region)
    img_rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb))
    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    if (img_bgr.shape[0] < template.shape[0]
            or img_bgr.shape[1] < template.shape[1]):
        logger.warning("[아이들] 탐색 영역(%dx%d)이 템플릿(%dx%d)보다 작음",
                       img_bgr.shape[1], img_bgr.shape[0],
                       template.shape[1], template.shape[0])
        return False, 0.0
    result = cv2.matchTemplate(img_bgr, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, _ = cv2.minMaxLoc(result)
    return max_val >= AUTO_IMAGE_THRESHOLD, max_val


def _wait_for_auto_image(bx: int, by: int, cw: int, ch: int) -> bool:
    """auto 이미지가 나타날 때까지 폴링. 반환: True=감지, False=타임아웃.
    템플릿 로드 후 1초 안정화 대기 — 아이템 클릭 직후 UI 가 아직 렌더 중일 수 있음.
    """
    _load_auto_template()       # 캐시되지 않았다면 여기서 로드
    _sleep_cancellable(1.0)     # UI 안정화
    logger.info("[아이들] auto 이미지 대기 (최대 %.0fs)", AUTO_IMAGE_MAX_WAIT_SEC)
    start = time.time()
    while True:
        _check_cancel()
        elapsed = time.time() - start
        if elapsed > AUTO_IMAGE_MAX_WAIT_SEC:
            logger.warning("[아이들] auto 이미지 %0.fs 내 미감지 — 타임아웃",
                           AUTO_IMAGE_MAX_WAIT_SEC)
            return False
        present, score = _auto_image_present(bx, by, cw, ch)
        if present:
            logger.info("[아이들] auto 이미지 감지 (score=%.2f, 경과 %.1fs)",
                        score, elapsed)
            return True
        _sleep_cancellable(AUTO_IMAGE_POLL_INTERVAL)


def _run_idle_sequence(screen_sync=None):
    """여유 시간에 실행할 아이들 시퀀스.
    1) (IDLE_CLICK_1) 클릭 → 2) 1s → 3) 선택된 아이템 클릭
    → 4) auto 이미지 감지 대기 → 5) 감지되면 AUTO 위치 클릭.
    """
    # 이전 Ctrl+Esc 요청 리셋 — 이번 시퀀스의 Ctrl+Esc 만 반영
    _cancel_event.clear()

    item_name = _selected_idle_item
    item_xy = IDLE_ITEMS.get(item_name)
    if item_xy is None:
        logger.warning("[아이들] 선택된 아이템 없음 (%s) — 기본값으로 폴백", item_name)
        item_xy = IDLE_ITEMS[DEFAULT_IDLE_ITEM]

    logger.info("[아이들] 시퀀스 시작 (아이템: %s)", item_name)
    hwnd, bx, by, cw, ch = _find_game_window()
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    _sleep_cancellable(0.3)

    _click_game_base(IDLE_CLICK_1, bx, by, cw, ch)
    _sleep_cancellable(1.0)
    _click_game_base(item_xy, bx, by, cw, ch)

    # auto 아이콘 등장 대기 — 뜨면 UI 안정화 후 AUTO 위치 클릭, 타임아웃이면 그냥 종료
    auto_detected = _wait_for_auto_image(bx, by, cw, ch)
    if auto_detected:
        _sleep_cancellable(1.5)              # UI 트랜지션 안정화
        _click_game_base(AUTO_IMAGE_CENTER, bx, by, cw, ch)
        logger.info("[아이들] AUTO 위치 클릭 완료 (BASE %s)", AUTO_IMAGE_CENTER)
    else:
        logger.warning("[아이들] auto 미감지 → 시퀀스 종료")
    logger.info("[아이들] 시퀀스 완료")


def _wait_for_loading_complete(bx: int, by: int, cw: int, ch: int) -> bool:
    """게임 로딩 완료 감지 — camera2.png 가 화면에 나타날 때까지 폴링.
    클릭/키 입력 없이 패시브로 매칭만 수행 (idle 의 _run_camera_check 와 다름).
    반환: True=감지, False=타임아웃 폴백.
    """
    # 로딩 화면이 올라올 시간을 잠깐 줌 — 이전 화면의 camera2 가 아직 떠 있을 수 있음
    _sleep_cancellable(LOADING_DETECT_PRE_DELAY)
    logger.info("[자동화] 게임 로딩 완료 대기 (camera2.png 노출 감시, 최대 %.0fs)",
                LOADING_DETECT_TIMEOUT_SEC)
    start = time.time()
    best = 0.0
    while time.time() - start < LOADING_DETECT_TIMEOUT_SEC:
        _check_cancel()
        try:
            present, score, _ = _match_region(
                CAMERA_CHECK_IMAGE,
                CAMERA_CHECK_CENTER, CAMERA_CHECK_W, CAMERA_CHECK_H,
                bx, by, cw, ch,
                CAMERA_CHECK_THRESHOLD)
        except Exception as e:
            logger.debug("[로딩감지] 매칭 실패: %s", e)
            _sleep_cancellable(LOADING_DETECT_INTERVAL)
            continue
        if score > best:
            best = score
        if present:
            logger.info("[자동화] 로딩 완료 감지 (camera2 score=%.2f, %.1fs 경과)",
                        score, time.time() - start)
            return True
        _sleep_cancellable(LOADING_DETECT_INTERVAL)
    logger.warning(
        "[자동화] 로딩 완료 미감지 — %.0fs 타임아웃 (최고 score=%.2f) → 그대로 도착 대기 진입",
        LOADING_DETECT_TIMEOUT_SEC, best)
    return False


def _run_camera_check(screen_sync=None):
    """IDLE 진입 시 카메라 뷰 보정.
    camera2.png 매칭 점수 >= CAMERA_CHECK_THRESHOLD 될 때까지
    CAMERA_CHECK_INTERVAL 간격으로 C 키 반복. 최대 CAMERA_CHECK_MAX_ITERS 회.
    """
    _cancel_event.clear()
    logger.info("[카메라] 뷰 보정 시작 (임계값 %.2f)", CAMERA_CHECK_THRESHOLD)
    try:
        _hwnd, bx, by, cw, ch = _find_game_window()
    except Exception as e:
        logger.warning("[카메라] 게임 창 탐색 실패 — 스킵: %s", e)
        return
    for i in range(CAMERA_CHECK_MAX_ITERS):
        _check_cancel()
        try:
            present, score, _ = _match_region(
                CAMERA_CHECK_IMAGE, CAMERA_CHECK_CENTER,
                CAMERA_CHECK_W, CAMERA_CHECK_H,
                bx, by, cw, ch,
                CAMERA_CHECK_THRESHOLD)
        except Exception as e:
            logger.warning("[카메라] 매칭 실패 — 중단: %s", e)
            return
        if present:
            logger.info("[카메라] 뷰 일치 (score=%.2f, %d회차) — 완료",
                        score, i + 1)
            return
        logger.debug("[카메라] 미일치 score=%.2f < %.2f → C 키 (%d회차)",
                     score, CAMERA_CHECK_THRESHOLD, i + 1)
        _press_key(_VK_C)
        _sleep_cancellable(CAMERA_CHECK_INTERVAL)
    logger.warning("[카메라] %d회 시도 후 점수 %.2f 미달 — 타임아웃",
                   CAMERA_CHECK_MAX_ITERS, CAMERA_CHECK_THRESHOLD)


# 대기열에서 꺼낼 때 이 시간 이하로 남았으면 스킵 (이동 완료 전에 스폰 지남)
QUEUE_SKIP_MIN_REMAINING_SEC = 40.0
# 시작 전 스왑 체크 임계 — run_for 호출 시 더 높은 우선순위 보스가 이 시간 이하로
# 남아 있으면 원래 타겟 대신 그 보스로 먼저 이동.
SWAP_CHECK_THRESHOLD_SEC = 240.0   # 4분


class BossAutomator(QObject):
    """SOON 이벤트 시 자동화 시퀀스를 공용 큐에 적재.

    직렬 처리 보장: 한 번에 최대 한 개의 자동화만 실행되고,
    그 보스의 presence 추적이 끝날 때까지(ended 시그널) 다음은 _pending 에 보류.

    상태 머신:
      IDLE      — 아무 작업 없음. run_for 호출 시 즉시 큐 submit 후 RUNNING 전환
      RUNNING   — 자동화 task가 큐에 적재/실행 중. task_finished 시 TRACKING,
                  task_failed 시 IDLE 복귀
      TRACKING  — 자동화 완료 후 presence 추적 중. ended 시그널 오면 IDLE 복귀

    IDLE 이 아닐 때 run_for 가 불리면 _pending 에 보류. IDLE 전환 시 다음 보류 꺼내 submit.

    스폰 시간 지났고 고우선순위 지역(무스펠 이전)이면 submit 전에 스킵.
    """

    STATE_IDLE = "IDLE"
    STATE_RUNNING = "RUNNING"
    STATE_TRACKING = "TRACKING"

    # 보스 이동 자동화 시작 시점 emit — GUI 방문 기록 탭 업데이트용. arg: 저장된 엔트리(dict).
    boss_visited = Signal(dict)

    def __init__(self, screen_sync, game_queue,
                 presence_monitor=None,
                 bosses: list[BossState] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.screen_sync = screen_sync
        self.game_queue = game_queue
        self.presence_monitor = presence_monitor
        # 전체 보스 리스트 — 시작 전 스왑 체크(_find_better_target)에서 스캔 대상.
        # main.py 에서 주입 (없으면 빈 리스트 → 스왑 체크는 항상 None 반환).
        self.all_bosses: list[BossState] = bosses or []
        self.enabled = True
        # 대기열에서 꺼낼 때 남은 시간이 QUEUE_SKIP_MIN_REMAINING_SEC 이하면 스킵할지.
        # GUI 체크박스로 on/off. 기본 켜짐 (촉박한 보스는 건너뛰는 게 평균 이득).
        self.skip_short_remaining = True
        self._pending: list[BossState] = []
        self._state = self.STATE_IDLE
        self._current_boss: BossState | None = None
        self._idle_action_submitted = False  # 현재 IDLE 기간에 아이들 액션 이미 돌렸는지

        if presence_monitor is not None:
            presence_monitor.ended.connect(self._on_boss_ended)
        if game_queue is not None:
            game_queue.task_finished.connect(self._on_task_finished)
            game_queue.task_failed.connect(self._on_task_failed)

        # Ctrl+Esc 로 대기열·추적·상태 전부 초기화
        register_esc_callback(self._on_esc_pressed)

    def reset(self):
        """상태 강제 초기화 — 꼬인 상태(RUNNING/TRACKING 에 갇힘 등) 해제용.
        _pending 비움 + _state=IDLE + presence 추적 중단 + _current_boss=None.
        외부 GUI "초기화" 버튼에서도 호출.
        """
        old_state = self._state
        old_boss_name = (self._current_boss.config.display_name
                         if self._current_boss else "-")
        pending_count = len(self._pending)
        self._pending.clear()
        self._state = self.STATE_IDLE
        self._current_boss = None
        self._idle_action_submitted = False
        if self.presence_monitor is not None:
            try:
                self.presence_monitor.stop_tracking()
            except Exception as e:
                logger.warning("[자동화] reset: stop_tracking 실패: %s", e)
        logger.info(
            "[자동화] 상태 초기화: %s('%s') → IDLE (대기열 %d개 비움)",
            old_state, old_boss_name, pending_count)

    def _on_esc_pressed(self):
        """Ctrl+Esc → 상태 강제 초기화."""
        self.reset()

    def run_for(self, boss: BossState):
        if not self.enabled:
            return
        if self.game_queue is None:
            logger.warning("[자동화] game_queue 없음 — 스킵")
            return

        # IDLE 이 아니면 보류 (우선순위 순으로 삽입)
        if self._state != self.STATE_IDLE:
            if boss in self._pending:
                return  # 중복 방지
            self._pending.append(boss)
            # priority_key 작을수록 높은 우선순위 → ASC 정렬로 앞쪽이 고우선순위
            self._pending.sort(key=lambda b: priority_key(b.config))
            current_name = (self._current_boss.config.display_name
                            if self._current_boss else "?")
            pos = self._pending.index(boss) + 1
            logger.info(
                "[자동화] 상태=%s ('%s' 진행 중) — '%s' 대기열 보류 (%d번째 / 총 %d개)",
                self._state, current_name,
                boss.config.display_name, pos, len(self._pending))
            return

        # 시작 전 스왑 체크 — 우선순위가 더 높고 5분 이내 남은 보스가 있으면 그쪽 먼저
        better = self._find_better_target(boss)
        if better is not None:
            logger.info(
                "[자동화] 시작 전 스왑: '%s' → '%s' (우선순위 %s < %s, 남은 %.1f분)",
                boss.config.display_name,
                better.config.display_name,
                priority_key(better.config),
                priority_key(boss.config),
                better.remaining_time.total_seconds() / 60)
            # 원래 보스는 pending 으로 — 스왑 대상이 끝난 뒤 재시도
            if boss not in self._pending:
                self._pending.append(boss)
                self._pending.sort(key=lambda b: priority_key(b.config))
            boss = better

        self._submit(boss)

    def _find_better_target(self, current: BossState) -> BossState | None:
        """all_bosses 중 current 보다 우선순위가 높고 <5분 남은 자동화 타겟을 탐색.
        여러 개면 최고 우선순위 하나 반환. 없으면 None.
        """
        if not self.all_bosses:
            return None
        cur_key = priority_key(current.config)
        best: BossState | None = None
        best_key = cur_key
        for b in self.all_bosses:
            if b is current:
                continue
            if not is_automation_target(b.config):
                continue
            rem = b.remaining_time
            if rem is None:
                continue
            sec = rem.total_seconds()
            if sec <= 0 or sec >= SWAP_CHECK_THRESHOLD_SEC:
                continue
            b_key = priority_key(b.config)
            if b_key < best_key:
                best = b
                best_key = b_key
        return best

    def _submit(self, boss: BossState):
        # 스폰 지나고 고우선순위 지역이면 스킵 (아무도 안 잡은 상태로 간주)
        if _is_stale_high_priority(boss):
            logger.info(
                "[자동화] 스킵 — '%s' 스폰 시간 지남 (무스펠 이전 고우선순위 지역)",
                boss.config.display_name)
            self._process_next_pending()
            return

        self._state = self.STATE_RUNNING
        self._current_boss = boss
        self._idle_action_submitted = False   # 자동화 시작 → 다음 IDLE 기간엔 다시 가능

        name = f"automation:{boss.config.display_name}"
        screen_sync = self.screen_sync
        presence_monitor = self.presence_monitor

        def task():
            _run_sequence(boss, screen_sync)
            # G+F 완료 시점엔 이미 보스 앞 → pre-spawn wait 우회하고 즉시 폴링.
            # 실제 보스 없으면 10초 후 stale clear 로 자동 IDLE.
            if presence_monitor is not None:
                try:
                    presence_monitor.start_tracking(boss, detect_now=True)
                except Exception as e:
                    logger.warning("[자동화] presence start_tracking 실패: %s", e)

        self.game_queue.submit(name, task)

    @Slot(str, float)
    def _on_task_finished(self, name: str, duration: float):
        """자동화 task 완료 → 무조건 TRACKING 진입.
        실제 보스 여부는 presence_monitor 가 판단 (STALE_CLEAR_SEC=10s).
        """
        if name == "idle_action":
            logger.info("[아이들] 시퀀스 완료 (%.1fs)", duration)
            # 아이들 시퀀스가 끝난 뒤 카메라 뷰 보정 작업 적재 (IDLE 유지 중일 때만)
            if self._state == self.STATE_IDLE and self.game_queue is not None:
                try:
                    self.game_queue.submit("camera_check", _run_camera_check)
                except Exception as e:
                    logger.warning("[카메라] 작업 적재 실패: %s", e)
            return
        if not name.startswith("automation:"):
            return  # sync 등 다른 task 는 무관
        if self._state != self.STATE_RUNNING:
            return

        boss = self._current_boss
        self._state = self.STATE_TRACKING
        logger.info(
            "[자동화] 상태 RUNNING → TRACKING ('%s' 출현 감지 대기)",
            boss.config.display_name if boss else "?")

        # 방문 기록 — 도착 성공(시퀀스 완료) 시점에 1건 추가
        if boss is not None:
            try:
                import boss_visits
                entry = boss_visits.append(boss.config.name, boss.config.region)
                self.boss_visited.emit(entry)
            except Exception as e:
                logger.warning("[방문기록] 저장 실패: %s", e)

    @Slot(str, str)
    def _on_task_failed(self, name: str, error: str):
        """자동화 task 실패(Esc/타임아웃 등) → 즉시 IDLE, 다음 보류 꺼내기."""
        if name == "idle_action":
            logger.info("[아이들] 시퀀스 실패: %s", error)
            return
        if not name.startswith("automation:"):
            return
        if self._state == self.STATE_RUNNING:
            logger.info("[자동화] 상태 RUNNING → IDLE (실패: %s)", error)
            self._state = self.STATE_IDLE
            self._current_boss = None
            self._idle_action_submitted = False
            self._process_next_pending()

    @Slot(object)
    def _on_boss_ended(self, boss):
        """presence 추적 종료 → IDLE 복귀, 다음 보류 꺼내기."""
        if self._state == self.STATE_TRACKING:
            logger.info(
                "[자동화] 상태 TRACKING → IDLE ('%s' 종료)",
                boss.config.display_name)
            # 스킬 프리셋 기본값(A)으로 복귀 — 전투 끝났으니 기본 스킬셋으로
            vk_a = SKILL_PRESET_KEYS.get("A")
            if vk_a is not None:
                try:
                    _press_key(vk_a)
                    logger.info("[자동화] 스킬 프리셋 'A' 로 복귀 (IDLE 전환)")
                except Exception as e:
                    logger.warning("[자동화] 프리셋 A 키 입력 실패: %s", e)
            self._state = self.STATE_IDLE
            self._current_boss = None
            self._idle_action_submitted = False
            self._process_next_pending()

    def trigger_idle_action_now(self):
        """GUI에서 아이들 아이템 변경 시 즉시 재실행용."""
        if self._state != self.STATE_IDLE:
            logger.info("[아이들] 재실행 스킵 — 상태 %s (IDLE 아님)", self._state)
            return
        if self.game_queue is None:
            return
        self._idle_action_submitted = True  # check_idle_action 중복 방지
        logger.info("[아이들] 아이템 변경 → 즉시 재실행")
        self.game_queue.submit("idle_action", _run_idle_sequence)

    def check_idle_action(self, next_spawn_sec):
        """다음 보스까지 여유(>10분)가 있으면 아이들 시퀀스 1회 실행.
        스케줄러가 tick마다 호출. 같은 IDLE 기간엔 한 번만 실행.
        """
        if self._state != self.STATE_IDLE:
            logger.debug("[아이들] 스킵 — 상태 %s (IDLE 아님)", self._state)
            return
        if self._idle_action_submitted:
            logger.debug("[아이들] 스킵 — 이번 IDLE 기간에 이미 1회 실행됨")
            return
        if self.game_queue is None:
            logger.debug("[아이들] 스킵 — game_queue 없음")
            return
        if next_spawn_sec is None:
            logger.debug("[아이들] 스킵 — 자동화 타겟 중 미래 스폰 보스 없음")
            return
        if next_spawn_sec < IDLE_ACTION_THRESHOLD_SEC:
            logger.debug(
                "[아이들] 스킵 — 다음 보스까지 %.1f분 < 임계 %.1f분",
                next_spawn_sec / 60, IDLE_ACTION_THRESHOLD_SEC / 60)
            return
        self._idle_action_submitted = True
        logger.info(
            "[아이들] 다음 보스까지 %.1f분 여유 → 아이들 시퀀스 적재",
            next_spawn_sec / 60)
        self.game_queue.submit("idle_action", _run_idle_sequence)

    def _process_next_pending(self):
        """보류된 보스 중 다음 실행 가능한 것을 꺼내 submit."""
        while self._pending:
            next_boss = self._pending.pop(0)
            if _is_stale_high_priority(next_boss):
                logger.info(
                    "[자동화] 보류 해제 중 스킵 — '%s' 스폰 지남 (남은 대기 %d개)",
                    next_boss.config.display_name, len(self._pending))
                continue
            rem = next_boss.remaining_time
            # 대기 중에 스폰 지나면서 advance_to_next_spawn 이 돌아
            # next_spawn_at 이 다음 주기로 밀려버린 케이스 → 이미 잡힌 걸로 간주, 스킵
            #   판정: 남은 시간이 alert_before + 1분 을 초과하면 SOON 범위를 벗어남
            advance_threshold = next_boss.config.alert_before + timedelta(minutes=1)
            if rem is not None and rem > advance_threshold:
                logger.info(
                    "[자동화] 보류 해제 중 스킵 — '%s' 남은 %.1f분 > %.1f분 (스폰 주기 넘어감, 이미 잡힘 추정)",
                    next_boss.config.display_name,
                    rem.total_seconds() / 60, advance_threshold.total_seconds() / 60)
                continue
            # 남은 시간 ≤ 40s면 스킵 (이동 완료 전에 스폰 지날 확률 높음) — skip_short_remaining 토글로 끌 수 있음
            if (self.skip_short_remaining
                    and rem is not None
                    and rem.total_seconds() <= QUEUE_SKIP_MIN_REMAINING_SEC):
                logger.info(
                    "[자동화] 보류 해제 중 스킵 — '%s' 남은 %.1fs ≤ %.0fs (촉박) (남은 대기 %d개)",
                    next_boss.config.display_name, rem.total_seconds(),
                    QUEUE_SKIP_MIN_REMAINING_SEC, len(self._pending))
                continue
            logger.info(
                "[자동화] 보류 해제 → '%s' 실행 (남은 대기 %d개)",
                next_boss.config.display_name, len(self._pending))
            self._submit(next_boss)
            return
        logger.debug("[자동화] 보류 대기열 비었음")
