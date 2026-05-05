"""
보스 임박(SOON) 이후 출현→종료 추적 모니터.

동작 규칙 (단순화 버전):
1. 보스 스폰 시각까지 감지 홀드 (pre-spawn wait)
2. 스폰 시각 지난 순간부터 POLL_INTERVAL_SEC (1s) 간격으로 detect_boss 폴링
3. detect_boss 가 연속 CONSECUTIVE_FALSE_FOR_KILL(5) 번 False 나오면 "잡힘"으로 간주
   → boss.set_next_spawn(now + 주기), ended emit, 추적 중단

부가 동작:
- 게임 큐가 태스크 실행 중이면 폴링 일시정지 (schedule 팝업 등이 아이콘 가림 방지)
- True 가 감지되면 detected 시그널 emit (GUI/로그 용도), 연속 False 카운터 리셋
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal

from boss_detector import detect_boss
from models import BossState

logger = logging.getLogger(__name__)

# 폴링 주기 — 0.5초
POLL_INTERVAL_SEC = 0.5
# True 를 한 번도 못 본 상태에서 연속 False 가 이만큼 쌓이면 "이미 잡힘" 판정.
# 스폰 예측 drift(게임 UI가 "1일 X시간" 단위로만 표시) 흡수용.
# 60 × 0.5s = 30초
CONSECUTIVE_FALSE_BEFORE_EVER_SEEN = 60
# True 를 본 적 있는 상태(전투 중/직후) 에서 연속 False 이면 "잡힘" 확정.
# 20 × 0.5s = 10초
CONSECUTIVE_FALSE_AFTER_EVER_SEEN = 20
# heartbeat 로그 간격 (폴링 횟수 기준) — 10초마다 1회 (20 × 0.5s)
HEARTBEAT_LOG_EVERY_N_POLLS = 20
# detect_now 시 "근시일 스폰" 판정 임계값 = boss.config.alert_before + 이 버퍼.
# 자동화가 SOON 시점(alert_before 남음)부터 도착까지 소요되는 시간을 흡수.
# 이 안이면 pre-spawn wait 유지, 초과(=advance 됨)면 지금을 감지 기준으로 override.
DETECT_NOW_BUFFER = timedelta(minutes=1)


class PresenceMonitor(QObject):
    # 외부 이벤트 시그널
    detected = Signal(object)                 # BossState — 첫 True 감지 시점
    ended = Signal(object)                    # BossState — 잡힘 판정 시점
    presence_changed = Signal(object, bool)   # (boss, flag) — GUI 구독용

    def __init__(self, game_queue=None, parent: QObject | None = None):
        super().__init__(parent)
        self._tracked: BossState | None = None
        self._flag = False             # 한 번이라도 True 가 떴는지 (GUI 표시용)
        self._paused = False
        self._consec_false = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._poll_count = 0
        self._first_poll_logged = False
        self._expected_spawn: datetime | None = None
        self._tracking_started_at: datetime | None = None

        if game_queue is not None:
            game_queue.task_started.connect(self._on_queue_start)
            game_queue.task_finished.connect(self._on_queue_done)
            game_queue.task_failed.connect(self._on_queue_fail)

    # ─── 게임 큐 상태 동기화 ───────────────────────────────────
    def _on_queue_start(self, name: str):
        self._paused = True
        logger.debug("[출현감지] 큐 태스크 '%s' 시작 → 감지 일시정지", name)

    def _on_queue_done(self, name: str, duration: float):
        self._paused = False
        self._consec_false = 0

    def _on_queue_fail(self, name: str, err: str):
        self._paused = False
        self._consec_false = 0

    # ─── 외부 API ───────────────────────────────────
    def start_tracking(self, boss: BossState, detect_now: bool = False):
        """추적 시작.

        detect_now=True: 자동화로 이미 보스 앞에 도착한 상태일 때.
            - next_spawn_at 이 근시일(≤ alert_before + DETECT_NOW_BUFFER) 미래면
              그 시각까지 pre-spawn wait (조기 도착 케이스)
            - 근시일 초과(advance 됨) 또는 과거면 즉시 감지 시작
        detect_now=False: next_spawn_at 그대로 사용 (표준 SOON 경로)
        """
        now = datetime.now()
        if detect_now and boss.next_spawn_at is not None:
            gap = boss.next_spawn_at - now
            max_early = boss.config.alert_before + DETECT_NOW_BUFFER
            if timedelta(0) < gap <= max_early:
                # 조기 도착 — 아직 스폰 전이지만 근시일 → 스폰까지 대기
                effective_spawn = boss.next_spawn_at
                mode_note = f" — 조기 도착, 스폰까지 {gap.total_seconds():.0f}s 대기"
            else:
                # advance됐거나 이미 과거 → 지금을 기준으로 감지 시작
                effective_spawn = now
                mode_note = " — 즉시 감지 모드"
        else:
            effective_spawn = boss.next_spawn_at
            mode_note = ""

        with self._lock:
            if self._tracked is not None and self._tracked is not boss:
                logger.info("[출현감지] 기존 추적 중단: %s → 신규: %s",
                            self._tracked.config.display_name,
                            boss.config.display_name)
            self._tracked = boss
            self._flag = False
            self._consec_false = 0
            self._poll_count = 0
            self._first_poll_logged = False
            self._expected_spawn = effective_spawn
            self._tracking_started_at = now
        logger.info(
            "[출현감지] 추적 시작: %s (감지 기준 = %s%s)",
            boss.config.display_name,
            effective_spawn.strftime("%m/%d %H:%M:%S") if effective_spawn else "?",
            mode_note,
        )
        self._ensure_thread()

    def stop_tracking(self):
        with self._lock:
            if self._tracked is not None:
                logger.info("[출현감지] 추적 중단: %s",
                            self._tracked.config.display_name)
            self._tracked = None
            self._flag = False

    @property
    def flag(self) -> bool:
        return self._flag

    @property
    def tracked(self) -> BossState | None:
        return self._tracked

    # ─── 폴링 루프 ───────────────────────────────────
    def _ensure_thread(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="PresenceMonitor")
        self._thread.start()

    def _loop(self):
        pre_spawn_logged = False
        while not self._stop_event.is_set():
            time.sleep(POLL_INTERVAL_SEC)
            with self._lock:
                boss = self._tracked
                paused = self._paused
                expected = self._expected_spawn
            if boss is None or paused:
                pre_spawn_logged = False
                continue

            # 1) 스폰 시각 이전이면 감지 스킵 (pre-spawn wait)
            if expected is not None and datetime.now() < expected:
                if not pre_spawn_logged:
                    wait_s = (expected - datetime.now()).total_seconds()
                    logger.info(
                        "[출현감지] %s: 스폰까지 %.1fs 대기 — 도착 후 감지 시작",
                        boss.config.display_name, wait_s)
                    pre_spawn_logged = True
                continue
            pre_spawn_logged = False

            # 2) detect_boss 폴링
            try:
                present, score, _ = detect_boss()
            except Exception as e:
                logger.warning("[출현감지] detect_boss 실패: %s", e)
                continue

            self._poll_count += 1
            if not self._first_poll_logged:
                logger.info("[출현감지] 폴링 가동 중 (첫 결과: score=%.2f, present=%s)",
                            score, present)
                self._first_poll_logged = True
            elif self._poll_count % HEARTBEAT_LOG_EVERY_N_POLLS == 0:
                threshold = (CONSECUTIVE_FALSE_AFTER_EVER_SEEN if self._flag
                             else CONSECUTIVE_FALSE_BEFORE_EVER_SEEN)
                logger.info("[출현감지] 감지 중... (%d회차, 최근 score=%.2f, consec_false=%d/%d%s)",
                            self._poll_count, score,
                            self._consec_false, threshold,
                            " — 감지 대기" if not self._flag else " — 전투 중")

            self._process(boss, present, score)

    # ─── 상태 처리 ───────────────────────────────────
    def _process(self, boss: BossState, present: bool, score: float):
        if present:
            # True 나오면 카운터 리셋 — 연속 False 규칙 초기화
            self._consec_false = 0
            if not self._flag:
                # 첫 감지 — GUI/로그 용도로 이벤트만 발행 (종료 판단은 별개)
                self._flag = True
                logger.info("[출현감지] %s: 출현 감지 (score=%.2f)",
                            boss.config.display_name, score)
                self.detected.emit(boss)
                self.presence_changed.emit(boss, True)
            return

        # False
        self._consec_false += 1
        threshold = (CONSECUTIVE_FALSE_AFTER_EVER_SEEN if self._flag
                     else CONSECUTIVE_FALSE_BEFORE_EVER_SEEN)
        if self._consec_false >= threshold:
            self._confirm_killed(boss, threshold)

    def _confirm_killed(self, boss: BossState, threshold: int):
        """연속 False threshold 회 관측 → 잡힘 확정 (또는 이미 잡힘 추정)."""
        new_spawn = datetime.now() + boss.config.respawn_interval
        boss.set_next_spawn(new_spawn)
        cause = "출현 후 잡힘" if self._flag else "스폰 부재 (이미 잡혔거나 미스폰)"
        logger.info(
            "[출현감지] %s: %d회 연속 미감지 → %s 확정 | 다음 스폰 = %s",
            boss.config.display_name, threshold, cause,
            new_spawn.strftime("%m/%d %H:%M:%S"))
        self.ended.emit(boss)
        self.presence_changed.emit(boss, False)
        with self._lock:
            self._tracked = None
            self._expected_spawn = None
            self._tracking_started_at = None
            self._flag = False
