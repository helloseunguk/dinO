"""
게임 화면 자동 캡처 + OCR 기반 보스 타이머 동기화 모듈

방식: 위치 기반 보스 판별 + 시간 텍스트만 개별 크롭 OCR
좌표: boss_config.TAB_TIME_COORDS에서 관리
"""

import ctypes
import logging
import re
import time
from datetime import datetime, timedelta

import mss
import numpy as np
from PIL import Image

from boss_config import TAB_BOSS_MAP, TAB_TIME_COORDS
# 좌표·마우스 공용 헬퍼 (game_coords.py로 일원화)
from game_coords import (
    BASE_W, BASE_H,
    find_game_window as _find_game_window,
    scale_xy as _scale_xy,
    scale_w as _scale_w,
    scale_h as _scale_h,
    click_game as _click_game,
    client_size as _client_size,
)
from ocr_engine import ocr_lines

logger = logging.getLogger(__name__)

SCHEDULE_ICON = (29, 346)
SCHEDULE_CLOSE = (1596, 247)

TAB_COORDS: dict[str, tuple[int, int]] = {
    # "미드가르드": (621, 307),  # 비활성화 — 동기화 시 탭 돌지 않음
    "요툰하임":   (727, 307),
    "니다벨리르": (833, 311),
    "알브하임":   (931, 314),
    "무스펠하임": (1061, 314),
    "아스가르드": (1160, 305),
    "니플하임":   (1272, 310),
    "던전":       (1364, 308),
}

# 시간 텍스트 크롭 크기 (중심 기준, 기준 해상도에서)
TIME_CROP_W = 250
TIME_CROP_H = 60

# --- 시간 파싱 ---

# OCR이 한국어를 잘못 읽는 케이스 대비:
#   분 ↔ 문  |  초 ↔ 쵸  |  시간 ↔ 시  |  남음 ↔ 남움
_TIME_PATTERNS = [
    (re.compile(r"(\d+)\s*일\s*(\d+)\s*시간?\s*남[음움]"),
     lambda m: timedelta(days=int(m.group(1)), hours=int(m.group(2)))),
    (re.compile(r"(\d+)\s*시간?\s*(\d+)\s*[분문]\s*남[음움]"),
     lambda m: timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))),
    (re.compile(r"(\d+)\s*[분문]\s*(\d+)\s*[초쵸]\s*남[음움]"),
     lambda m: timedelta(minutes=int(m.group(1)), seconds=int(m.group(2)))),
]

# "출현 중" 감지용
_SPAWNED_PATTERN = re.compile(r"출현\s*중")

# 반환값: timedelta=남은시간, "SPAWNED"=출현중, None=인식실패
SPAWNED = "SPAWNED"


def _parse_time(text: str) -> timedelta | str | None:
    if _SPAWNED_PATTERN.search(text):
        return SPAWNED
    for pattern, converter in _TIME_PATTERNS:
        m = pattern.search(text)
        if m:
            return converter(m)
    return None


class ScreenSync:
    """게임 화면 캡처 + 위치 기반 보스 판별 + 시간 텍스트 OCR"""

    def __init__(self, tab_delay: float = 0.3):
        self.tab_delay = tab_delay
        self._sct: mss.mss | None = None  # sync_all 실행 중에만 활성

    def open_schedule(self, hwnd: int, bx: int, by: int, cw: int, ch: int):
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        gx, gy = _scale_xy(SCHEDULE_ICON, cw, ch)
        _click_game(gx, gy, bx, by)
        time.sleep(1.5)

    def close_schedule(self, bx: int, by: int, cw: int, ch: int):
        gx, gy = _scale_xy(SCHEDULE_CLOSE, cw, ch)
        _click_game(gx, gy, bx, by)
        time.sleep(0.3)

    def capture_tab(self, tab_name: str, bx: int, by: int,
                    cw: int, ch: int) -> dict[str, object]:
        """
        탭 클릭 → 전체 영역 1회 캡처 → numpy 슬라이싱 →
        세로로 이어붙여 1회 OCR → bbox Y좌표로 보스 행 역매핑.

        반환: {boss_name: datetime | SPAWNED} — 캡처 시점 기준 절대 스폰 시각.
        OCR 적용 지연이 반영되도록 datetime = capture_time + remaining.
        """
        tx, ty = _scale_xy(TAB_COORDS[tab_name], cw, ch)
        _click_game(tx, ty, bx, by)
        time.sleep(self.tab_delay)

        bosses = TAB_BOSS_MAP.get(tab_name, [])
        coords = TAB_TIME_COORDS.get(tab_name, [])
        crop_w = _scale_w(TIME_CROP_W, cw)
        crop_h = _scale_h(TIME_CROP_H, ch)
        n = min(len(bosses), len(coords))
        if n == 0:
            return {}

        # ─── 1) 각 보스 크롭의 화면 좌표 계산 ───
        crop_rects: list[tuple[int, int]] = []
        for i in range(n):
            cx, cy = _scale_xy(coords[i], cw, ch)
            crop_rects.append((
                bx + cx - crop_w // 2,
                by + cy - crop_h // 2,
            ))

        # ─── 2) 전체 bounding rect 1회 캡처 ───
        t0 = time.perf_counter()
        min_x = min(r[0] for r in crop_rects)
        min_y = min(r[1] for r in crop_rects)
        max_x = max(r[0] + crop_w for r in crop_rects)
        max_y = max(r[1] + crop_h for r in crop_rects)
        region = {"left": min_x, "top": min_y,
                  "width": max_x - min_x, "height": max_y - min_y}
        # 절대 스폰 시각 계산 기준 — 스크린샷 찍기 직전 시각
        capture_time = datetime.now()
        screenshot = self._sct.grab(region)
        full_img = np.array(Image.frombytes(
            "RGB", (screenshot.width, screenshot.height), screenshot.rgb))
        t_cap = time.perf_counter() - t0

        # ─── 3) 세로로 gap과 함께 이어붙이기 (black gap → detection 분리) ───
        GAP = 30
        stitched_h = n * crop_h + (n - 1) * GAP
        stitched = np.zeros((stitched_h, crop_w, 3), dtype=np.uint8)
        for i, (sx, sy) in enumerate(crop_rects):
            lx, ly = sx - min_x, sy - min_y
            stitched[i * (crop_h + GAP):i * (crop_h + GAP) + crop_h] = \
                full_img[ly:ly + crop_h, lx:lx + crop_w]

        # ─── 4) OCR 1회 호출 (Windows OCR) ───
        t1 = time.perf_counter()
        lines = ocr_lines(stitched)
        t_ocr = time.perf_counter() - t1

        # ─── 5) bbox Y 좌표로 보스 행 역매핑 ───
        row_h = crop_h + GAP
        row_texts: list[list[str]] = [[] for _ in range(n)]
        for line in lines:
            idx = int(line.y_center // row_h)
            if 0 <= idx < n:
                row_texts[idx].append(line.text)

        result: dict[str, object] = {}
        for i in range(n):
            boss_cfg = bosses[i]
            full_text = " ".join(row_texts[i])
            parsed = _parse_time(full_text)
            if parsed == SPAWNED:
                result[boss_cfg.name] = SPAWNED
                logger.debug("[OCR] %s → 출현 중", boss_cfg.name)
            elif isinstance(parsed, timedelta):
                # 캡처 시점 기준 절대 스폰 시각으로 변환 (지연 보정)
                absolute_spawn = capture_time + parsed
                result[boss_cfg.name] = absolute_spawn
                logger.debug("[OCR] %s → %s → 스폰=%s (%s)",
                             boss_cfg.name, parsed,
                             absolute_spawn.strftime("%H:%M:%S"), full_text)
            else:
                logger.warning("[OCR] %s → 인식 실패 (원문='%s')",
                               boss_cfg.name, full_text)

        logger.info("[타이밍] %s: 캡처 %dms, OCR %dms (탭대기 %dms 제외)",
                    tab_name, int(t_cap * 1000), int(t_ocr * 1000),
                    int(self.tab_delay * 1000))
        return result

    def sync_all(self) -> dict[str, object]:
        """시간표 열기 → 모든 탭 순회 → 개별 OCR → 시간표 닫기.

        반환: {boss_name: datetime | SPAWNED} — 각 보스의 절대 스폰 시각.
        """
        hwnd, bx, by, cw, ch = _find_game_window()
        scale = cw / BASE_W if BASE_W else 1.0
        logger.info("[동기화] 클라이언트 크기 %dx%d (기준 %dx%d, 스케일 %.3fx)",
                    cw, ch, BASE_W, BASE_H, scale)
        self.open_schedule(hwnd, bx, by, cw, ch)

        all_data: dict[str, object] = {}
        t_total_start = time.perf_counter()
        with mss.mss() as sct:
            self._sct = sct
            for tab_name in TAB_COORDS:
                try:
                    data = self.capture_tab(tab_name, bx, by, cw, ch)
                    all_data.update(data)
                    bosses = TAB_BOSS_MAP.get(tab_name, [])
                    logger.info("[동기화] %s: %d/%d 보스 인식",
                                tab_name, len(data), len(bosses))
                except Exception as e:
                    logger.error("[동기화] %s 탭 처리 실패: %s", tab_name, e)
            self._sct = None

        self.close_schedule(bx, by, cw, ch)
        total_ms = int((time.perf_counter() - t_total_start) * 1000)
        logger.info("[동기화] 총 %d개 보스, 총 소요 %dms (탭 %d개)",
                    len(all_data), total_ms, len(TAB_COORDS))
        return all_data
