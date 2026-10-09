"""경험치(EXP %) 추이로 100% 까지 남은 시간 추정.

IDLE(사냥 중)일 때만 SAMPLE_INTERVAL_SEC 마다 화면 왼쪽 아래 'EXP 29.0355%' 를
읽고, ESTIMATE_INTERVAL_SEC 마다 현재 사냥 구간의 기울기(%/h)로 100% 도달
시간을 계산한다.

사냥 구간:
  보스 자동화·사냥터 이동(idle_action)이 끝나고 IDLE 로 돌아온 뒤부터.
  다른 곳에서 오른 경험치가 섞이지 않게 이동이 생기면 구간을 새로 시작한다.
  경험치가 줄면(레벨업·사망) 그때도 새로 시작한다.

게임 조작이 없는 순수 캡처라 game_queue 를 타지 않는다. 다만 큐 태스크가
실행 중이면 팝업이 숫자를 가릴 수 있어 그 회차는 건너뛴다.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import cv2
import mss
import numpy as np
from PIL import Image
from PySide6.QtCore import QObject

from screen_sync import _find_game_window, _scale_xy, _scale_w, _scale_h

logger = logging.getLogger(__name__)

TARGET_PERCENT = 100.0
# 경험치 읽기 간격 / 추정 계산 간격
SAMPLE_INTERVAL_SEC = 30.0
ESTIMATE_INTERVAL_SEC = 300.0
# 사냥 구간 기록이 이만큼 쌓여야 첫 계산을 한다
MIN_SPAN_SEC = 300.0
MIN_SAMPLES = 5
# 한 회차(30초) 사이에 이보다 많이 오르면 오인식 의심
MAX_JUMP_PERCENT = 1.0
# 감소·급등 같은 이상값은 이 횟수만큼 연속으로 나와야 사실로 받아들인다
# (한 번은 숫자 한 자리를 빠뜨린 오인식일 수 있다 — 29.0355 → 29.035)
ANOMALY_CONFIRM = 2

# 화면 왼쪽 아래 경험치 숫자 영역 (게임 내부 BASE 1920×1009).
# 숫자 바로 아래(약 8px)에 연두색 경험치 바가 있어 세로를 넉넉히 잡으면 안 된다
EXP_CENTER = (131, 990)
EXP_HALF_W = 42
EXP_HALF_H = 10
# 숫자는 연두색 — 색으로 걸러야 배경과 'EXP' 라벨, 옆 시계가 빠진다
_UPSCALE = 4
# OCR 은 소수점을 공백으로 읽는다 ("29 0355零&") — 정수부와 소수부를 따로 잡는다
_NUM_RE = re.compile(r"(\d{1,3})\D+(\d{1,4})")


def read_exp_percent() -> float | None:
    """게임 화면의 경험치 % 를 읽는다. 못 읽으면 None."""
    from ocr_engine import ocr_text

    _hwnd, bx, by, cw, ch = _find_game_window()
    cx, cy = _scale_xy(EXP_CENTER, cw, ch)
    hw = _scale_w(EXP_HALF_W, cw)
    hh = _scale_h(EXP_HALF_H, ch)
    region = {"left": bx + cx - hw, "top": by + cy - hh,
              "width": hw * 2, "height": hh * 2}
    with mss.mss() as sct:
        shot = sct.grab(region)
    rgb = np.array(Image.frombytes(
        "RGB", (shot.width, shot.height), shot.rgb)).astype(int)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    # 숫자 = 밝은 연두 (R 160~240, G 220~240, B ~0). 뒤로 지나가는 초록 빛줄기는
    # 더 어둡거나 푸른 기가 있어 대부분 빠진다
    mask = (g > 190) & (b < 70) & (r > 130)
    img = np.where(mask, 0, 255).astype(np.uint8)
    img = cv2.resize(img, None, fx=_UPSCALE, fy=_UPSCALE,
                     interpolation=cv2.INTER_NEAREST)
    img = cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)

    text = ocr_text(img)
    m = _NUM_RE.search(text)
    # 게임은 소수 4자리를 보여주고 끝의 0 만 생략한다 (29.0360 → 29.036).
    # 2자리 이하는 대부분 빛 효과에 가려 뒷자리를 놓친 것 (33.3796 → 33.3)
    if not m or len(m.group(2)) < 3:
        logger.debug("[경험치] 읽기 실패: '%s'", text)
        return None
    value = float(f"{m.group(1)}.{m.group(2)}")
    return value if 0.0 <= value < TARGET_PERCENT else None


def read_exp_stable(attempts: int = 5, gap_sec: float = 0.4) -> float | None:
    """여러 번 읽어 두 번 연속 같은 값이 나오면 채택.

    숫자 뒤로 빛 효과가 지나가는 프레임은 못 읽거나(None) 자릿수를 빠뜨린다
    (33.3763 → 3.763). 깨끗한 프레임 두 장이면 충분하다.
    """
    prev = None
    for i in range(attempts):
        if i:
            time.sleep(gap_sec)
        value = read_exp_percent()
        if value is not None and value == prev:
            return value
        prev = value
    return None


def format_duration(hours: float) -> str:
    total_min = int(round(hours * 60))
    days, rem = divmod(total_min, 24 * 60)
    h, m = divmod(rem, 60)
    if days:
        return f"{days}일 {h}시간"
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


@dataclass
class ExpEstimate:
    current: float               # 계산 시점 경험치 %
    rate_per_hour: float         # 시간당 오르는 %
    span_min: float              # 계산에 쓴 사냥 구간 길이 (분)
    hours_left: float | None     # 100% 까지 남은 시간 — 오르지 않으면 None
    eta: datetime | None
    computed_at: datetime


class ExpTracker(QObject):
    """IDLE 사냥 구간의 경험치 추이 기록 + 5분마다 100% 도달 시간 추정."""

    def __init__(self, automator, game_queue, parent: QObject | None = None):
        super().__init__(parent)
        self.automator = automator
        self.game_queue = game_queue
        self._lock = threading.Lock()
        self._samples: list[tuple[float, float]] = []    # (time.time(), %)
        self._anomalies: list[tuple[float, float]] = []  # 확인 대기 중인 이상값
        self._last_estimate_at = 0.0
        self._current: float | None = None
        self._estimate: ExpEstimate | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        if game_queue is not None:
            game_queue.task_started.connect(self._on_task_started)

    # ─── 외부 API ───────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="ExpTracker")
        self._thread.start()
        logger.info("[경험치] 추적 시작 (%.0f초마다 읽기, %.0f분마다 계산)",
                    SAMPLE_INTERVAL_SEC, ESTIMATE_INTERVAL_SEC / 60)

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> dict:
        """GUI 표시용 현재 상태."""
        with self._lock:
            span = (self._samples[-1][0] - self._samples[0][0]
                    if len(self._samples) >= 2 else 0.0)
            return {"current": self._current,
                    "estimate": self._estimate,
                    "segment_min": span / 60}

    # ─── 구간 관리 ───────────────────────────────────
    def _on_task_started(self, name: str):
        # 캐릭터가 자리를 옮기는 태스크 — 지금까지의 기록은 다른 사냥터 것
        if name == "idle_action" or name.startswith("automation:"):
            self._reset(f"이동 ({name})")

    def _reset(self, reason: str, keep: list[tuple[float, float]] | None = None):
        with self._lock:
            had = len(self._samples)
            self._samples = list(keep or [])
            self._anomalies.clear()
            # 직전 추정은 남겨 둔다 — 보스 다녀오는 동안에도 GUI 에 보이도록
            self._last_estimate_at = 0.0
        if had:
            logger.info("[경험치] 사냥 구간 새로 시작 — %s (이전 기록 %d개 버림)",
                        reason, had)

    def _add_sample(self, t: float, value: float):
        with self._lock:
            last = self._samples[-1][1] if self._samples else None
            anomalous = last is not None and (
                value < last or value - last > MAX_JUMP_PERCENT)
            if not anomalous:
                self._anomalies.clear()
                self._samples.append((t, value))
                self._current = value
                return
            self._anomalies.append((t, value))
            if len(self._anomalies) < ANOMALY_CONFIRM:
                return   # 한 번은 오인식일 수 있다 — 다음 회차로 확인
            confirmed = list(self._anomalies)
            self._current = value
        if value < last - 20:   # 사망 패널티는 몇 % 수준 — 크게 줄면 레벨업
            reason = f"레벨업 ({last:.4f}% → {value:.4f}%)"
        elif value < last:
            reason = f"경험치 감소 ({last:.4f}% → {value:.4f}%)"
        else:
            reason = f"경험치 급등 ({last:.4f}% → {value:.4f}%)"
        self._reset(reason, keep=confirmed)

    # ─── 추정 ───────────────────────────────────
    def _maybe_estimate(self, now: float):
        with self._lock:
            samples = list(self._samples)
            due = now - self._last_estimate_at >= ESTIMATE_INTERVAL_SEC
        if not due or len(samples) < MIN_SAMPLES:
            return
        ts = np.array([s[0] for s in samples])
        vs = np.array([s[1] for s in samples])
        span = ts[-1] - ts[0]
        if span < MIN_SPAN_SEC:
            return
        # 처음·끝 두 점이 아니라 구간 전체에 맞춘 직선 — 한두 번의 오인식에 덜 흔들린다
        slope_per_sec = np.polyfit(ts - ts[0], vs, 1)[0]
        rate = float(slope_per_sec * 3600)
        current = float(vs[-1])
        computed_at = datetime.now()
        if rate > 0:
            hours_left = (TARGET_PERCENT - current) / rate
            eta = computed_at + timedelta(hours=hours_left)
        else:
            hours_left = eta = None
        estimate = ExpEstimate(current, rate, span / 60, hours_left, eta, computed_at)
        with self._lock:
            self._estimate = estimate
            self._last_estimate_at = now
        if hours_left is None:
            logger.info("[경험치] 현재 %.4f%% | 최근 %.0f분 동안 오르지 않음",
                        current, span / 60)
        else:
            logger.info("[경험치] 현재 %.4f%% | 최근 %.0f분 기준 %+.4f%%/h | "
                        "%.0f%%까지 약 %s (%s 예상)",
                        current, span / 60, rate, TARGET_PERCENT,
                        format_duration(hours_left), eta.strftime("%m/%d %H:%M"))

    # ─── 루프 ───────────────────────────────────
    def _loop(self):
        while not self._stop_event.wait(SAMPLE_INTERVAL_SEC):
            try:
                if self.automator is not None and self.automator.state != "IDLE":
                    self._reset(f"보스 자동화 ({self.automator.state})")
                    continue
                if self.game_queue is not None and self.game_queue.current_task:
                    continue
                value = read_exp_stable()
                if value is None:
                    continue
                now = time.time()
                self._add_sample(now, value)
                self._maybe_estimate(now)
            except Exception as e:
                # 게임 창이 없을 때 등 — 다음 회차에 다시 시도
                logger.debug("[경험치] 측정 실패: %s", e)
