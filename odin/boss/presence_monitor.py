"""
보스 임박(SOON) 이후 출현→종료 추적 모니터.

동작 규칙 (단순화 버전):
1. 감지 시작 — 자동화로 보스 앞에 도착했으면(detect_now) 바로, 아니면 스폰 예정 시각부터.
   보스가 예정보다 일찍 뜨는 경우가 있어 도착하면 미리 본다. 대신 아래 3의 '잡힘'
   확정과 4의 '미감지 중단' 은 **스폰 예정 시각이 지난 뒤에만** 한다.
2. POLL_INTERVAL_SEC (0.5s) 간격으로 두 신호를 함께 폴링
   - 아이콘: 상단 중앙 보스 아이콘 템플릿 매칭 (detect_boss)
   - 패널: 우측 '피해량 - <보스명>' 헤더 OCR (detect_boss_engaged)
   패널은 보스가 죽은 뒤에도 계속 남으므로 패널만으로는 처치를 알 수 없다.
3. 판정 (2026-10-10 사용자 결정 — 이 세 가지만 쓴다)
   - 아이콘 + 패널 → 보스 출현 중
   - 둘 다 없음 → 보스 아직 출현 안 함 (유예 예산 차감)
   - 아이콘 없이 패널만 PANEL_ONLY_KILL_SEC(8초) 연속 → "잡힘" 확정
     → boss.set_next_spawn(패널만 남기 시작한 시각 + 주기), ended emit
   (규칙 밖) 패널 없이 아이콘만 → 출현했지만 아직 교전 전. 유예·연속 횟수만 리셋
4. 출현을 못 본 채 SPAWN_GRACE_SEC 가 지나면 추적만 중단 (타이머는 유지)
   — 연탐(fast_confirm)이면 이 유예도 FAST_SPAWN_GRACE_SEC 로 단축
5. 스폰 전부터 대기해 출현 순간을 본 경우 "예정 대비 실측 오차" 를 로그로 남김
   (보정값 자동 학습은 하지 않음 — 소스별 고정 보정을 각 sync 구현이 담당)

부가 동작:
- 매 폴링마다 사망 화면부터 본다. 죽었으면 위 판정 없이 추적을 끝내고 died emit
  (타이머 유지) — 부활·아이들 복귀는 BossAutomator 가 한다
- 게임 큐가 태스크 실행 중이면 폴링 일시정지 (schedule 팝업 등이 아이콘 가림 방지)
- 처음 출현 중(아이콘+패널)을 보면 detected 시그널 emit (GUI/로그 용도)
- 아이콘 보임/사라짐, 패널 글자 변화를 시각과 함께 로그로 남김 (판정 검증용)
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, Signal

from odin.boss.boss_detector import (MATCH_THRESHOLD, detect_boss, detect_boss_engaged,
                           save_icon_region)
from odin.core.models import BossState
from odin.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# 폴링 주기 — 0.5초
POLL_INTERVAL_SEC = 0.5
# 스폰 예정 시각 이후 "보스가 안 보임" 을 이만큼 참고 기다린다 (True 를 한 번도
# 못 본 상태). 사이트/API 에 등록되는 예정 시각이 실제 출현보다 1분 가량 이른
# 경우가 관측되어, 기존 30초 창으로는 실제 출현 전에 추적이 끊겼다.
# 폴링 카운터가 아니라 "실제로 폴링한 시간" 을 누적한 예산으로 판단한다.
# 카운터 방식은 큐 태스크(주기 동기화 등) 완료 시 리셋되기 때문에, 유예가
# 동기화 주기보다 길면 영원히 임계값에 도달하지 못한다.
SPAWN_GRACE_SEC = 120.0
# 아이콘 없이 패널만 이 시간 동안 연속으로 보이면 "잡힘" 확정.
# 횟수(예전 5회 ≈ 2.5초)가 아니라 시간으로 잰다 — 폴링 한 번이 캡처·OCR 만큼 0.5초보다
# 길어서 횟수로는 실제 창이 들쭉날쭉했다. 2.5초는 아이콘이 잠깐 가려질 때 오판했다.
# 5초도 빠듯했다 — 10/30 이후 아이콘이 사라졌다 돌아오기까지 최장 4초(토르). 그래서 8초.
# 점수로는 못 가른다: 가려졌을 때(0.34~0.37)와 정말 없을 때(0.24~0.37)가 같은 범위다.
PANEL_ONLY_KILL_SEC = 8.0
# 출현 중(아이콘+패널)을 본 뒤에는 아이콘 판정 기준을 이만큼 낮춘다 (히스테리시스).
# 보스에 따라 아이콘 점수가 임계값(0.55) 언저리를 오가 — 토르 0.48~0.63 —
# 아이콘이 그대로인데도 사라짐으로 판정됐다. 아이콘 없는 화면은 로그상 99.9% 가 0.41 이하.
ICON_KEEP_THRESHOLD = 0.45
# 잡힘 확정할 때 아이콘 영역 캡처를 남기는 폴더 (오판 진단용) — 최신 N장만 유지
ICON_DEBUG_DIR = DATA_DIR / "icon_debug"
ICON_DEBUG_KEEP = 100
# 연탐일 때의 미감지 유예 — 한 번도 못 본 채 이만큼 지나면 바로 추적 중단.
# 이 경로는 타이머를 건드리지 않으므로(진실 소스는 주기 동기화) 짧게 잡아도
# 잃는 건 "조금 늦게 뜨는 보스를 버리는 것" 뿐이고, 얻는 건 다음 보스 도착 시간이다.
FAST_SPAWN_GRACE_SEC = 3.0
# heartbeat 로그 간격 (폴링 횟수 기준) — 10초마다 1회 (20 × 0.5s)
HEARTBEAT_LOG_EVERY_N_POLLS = 20
# 사망 화면을 이 횟수만큼 연속으로 읽어야 사망 — OCR 한 번 잘못 읽고 보스를 버리지 않게
DEATH_CONFIRM_CHECKS = 2
# detect_now 시 "근시일 스폰" 판정 임계값 = boss.config.alert_before + 이 버퍼.
# 자동화가 SOON 시점(alert_before 남음)부터 도착까지 소요되는 시간을 흡수.
# 이 안이면 pre-spawn wait 유지, 초과(=advance 됨)면 지금을 감지 기준으로 override.
DETECT_NOW_BUFFER = timedelta(minutes=1)


class PresenceMonitor(QObject):
    # 외부 이벤트 시그널
    detected = Signal(object)                 # BossState — 첫 True 감지 시점
    ended = Signal(object)                    # BossState — 잡힘 판정 시점
    died = Signal(object)                     # BossState — 추적 중 캐릭터 사망 (추적은 끝남)
    presence_changed = Signal(object, bool)   # (boss, flag) — GUI 구독용

    def __init__(self, game_queue=None, parent: QObject | None = None):
        super().__init__(parent)
        self._tracked: BossState | None = None
        self._flag = False             # 출현 중(아이콘+패널)을 봤는지 (GUI 표시용)
        self._paused = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._poll_count = 0
        self._first_poll_logged = False
        self._expected_spawn: datetime | None = None   # 판정(잡힘·미감지 중단) 시작 시각
        self._poll_from: datetime | None = None        # 감지(폴링) 시작 시각
        self._tracking_started_at: datetime | None = None
        self._measure_expected: datetime | None = None  # 실측 오차 로그 기준 시각
        self._grace_polled_sec = 0.0    # 미감지 상태로 실제 폴링한 누적 시간
        self._fast_confirm = False      # 연탐 — 미감지 유예 단축
        self._last_panel = ""           # 직전 폴링에서 읽은 패널 보스 이름 (변화 로그용)
        self._last_icon = False         # 직전 폴링의 아이콘 여부 (변화 로그용)
        self._panel_only = 0            # 아이콘 없이 패널만 보인 연속 횟수
        self._panel_only_since: datetime | None = None  # 그 연속이 시작된 시각 = 처치 시각

        if game_queue is not None:
            game_queue.task_started.connect(self._on_queue_start)
            game_queue.task_finished.connect(self._on_queue_done)
            game_queue.task_failed.connect(self._on_queue_fail)

    # ─── 게임 큐 상태 동기화 ───────────────────────────────────
    def _on_queue_start(self, name: str):
        self._paused = True
        logger.debug("[출현감지] 큐 태스크 '%s' 시작 → 감지 일시정지", name)

    # 큐 태스크 전후로 "패널만" 연속 횟수만 리셋한다 (팝업이 아이콘을 가린 프레임
    # 방어용). _grace_polled_sec 은 리셋하지 않는다 — 리셋하면 동기화가 돌 때마다
    # 유예가 처음부터 다시 시작돼 추적이 영원히 끝나지 않는다.
    def _on_queue_done(self, name: str, duration: float):
        self._paused = False
        self._panel_only = 0

    def _on_queue_fail(self, name: str, err: str):
        self._paused = False
        self._panel_only = 0

    # ─── 외부 API ───────────────────────────────────
    def start_tracking(self, boss: BossState, detect_now: bool = False,
                       fast_confirm: bool = False):
        """추적 시작.

        detect_now=True: 자동화로 이미 보스 앞에 도착한 상태일 때.
            감지는 **지금부터** 돈다 — 보스가 예정보다 일찍 뜨는 경우가 있다.
            - next_spawn_at 이 근시일(≤ alert_before + DETECT_NOW_BUFFER) 미래면
              판정(잡힘·미감지 중단)만 그 시각 이후로 미룬다 (조기 도착 케이스).
              일찍 떠서 일찍 잡혀도 패널만 남기 시작한 시각은 기억해 뒀다가 처치 시각으로 쓴다.
            - 근시일 초과(advance 됨) 또는 과거면 판정도 지금부터
        detect_now=False: next_spawn_at 그대로 사용 (표준 SOON 경로) — 캐릭터가
            보스 앞에 없을 수 있으므로 감지도 그 시각부터

        fast_confirm=True: 연탐 — 다음 보스가 1분 이내로 붙어 있어 바로 넘어가야 함.
            출현을 못 본 경우의 유예를 FAST_SPAWN_GRACE_SEC 로 줄인다.
        """
        now = datetime.now()
        if detect_now and boss.next_spawn_at is not None:
            gap = boss.next_spawn_at - now
            max_early = boss.config.alert_before + DETECT_NOW_BUFFER
            if timedelta(0) < gap <= max_early:
                # 조기 도착 — 감지는 지금부터, 판정은 스폰 예정 시각부터
                effective_spawn = boss.next_spawn_at
                mode_note = (f" — 조기 도착, 감지는 지금부터 · "
                             f"판정은 {gap.total_seconds():.0f}s 뒤 예정 시각부터")
            else:
                # advance됐거나 이미 과거 → 지금을 기준으로 감지 시작
                effective_spawn = now
                mode_note = " — 즉시 감지 모드"
            poll_from = now
        else:
            effective_spawn = boss.next_spawn_at
            poll_from = effective_spawn
            mode_note = ""

        # 실측 오차 로그는 "스폰 전부터 대기해서 출현 순간을 본" 경우만 유효.
        # 이미 스폰이 지난 뒤 감지를 시작하면 첫 True 시각이 실제 출현 시각이 아니다.
        measure_expected = (effective_spawn
                            if effective_spawn is not None and effective_spawn > now
                            else None)

        with self._lock:
            if self._tracked is not None and self._tracked is not boss:
                logger.info("[출현감지] 기존 추적 중단: %s → 신규: %s",
                            self._tracked.config.display_name,
                            boss.config.display_name)
            self._tracked = boss
            self._flag = False
            self._grace_polled_sec = 0.0
            self._poll_count = 0
            self._first_poll_logged = False
            self._expected_spawn = effective_spawn
            self._poll_from = poll_from
            self._tracking_started_at = now
            self._measure_expected = measure_expected
            self._fast_confirm = fast_confirm
            self._last_panel = ""
            self._last_icon = False
            self._panel_only = 0
            self._panel_only_since = None
        logger.info(
            "[출현감지] 추적 시작: %s (감지 기준 = %s%s, 잡힘 = 아이콘 없이 패널만 %.0fs, "
            "미감지 유예 = %.0fs%s)",
            boss.config.display_name,
            effective_spawn.strftime("%m/%d %H:%M:%S") if effective_spawn else "?",
            mode_note,
            PANEL_ONLY_KILL_SEC,
            self._grace_budget(),
            " — 연탐" if fast_confirm else "",
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

    def _grace_budget(self) -> float:
        """한 번도 못 본 상태로 참고 기다리는 폴링 누적 시간."""
        return FAST_SPAWN_GRACE_SEC if self._fast_confirm else SPAWN_GRACE_SEC

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
        dead_streak = 0
        while not self._stop_event.is_set():
            time.sleep(POLL_INTERVAL_SEC)
            with self._lock:
                boss = self._tracked
                paused = self._paused
                poll_from = self._poll_from
            if boss is None or paused:
                pre_spawn_logged = False
                dead_streak = 0
                continue

            # 1) 감지 시작 시각 이전이면 스킵 (pre-spawn wait — 자동화로 도착한 경우엔 없다)
            if poll_from is not None and datetime.now() < poll_from:
                if not pre_spawn_logged:
                    wait_s = (poll_from - datetime.now()).total_seconds()
                    logger.info(
                        "[출현감지] %s: 스폰까지 %.1fs 대기 — 도착 후 감지 시작",
                        boss.config.display_name, wait_s)
                    pre_spawn_logged = True
                continue
            pre_spawn_logged = False

            # 2) 사망 화면이면 판정하지 않고 추적을 끝낸다 — 누워 있는 동안 보이는 아이콘·
            #    패널은 보스 상태가 아니다. 타이머는 건드리지 않는다 (잡혔는지 모름).
            try:
                from odin.automation.respawn import is_dead
                dead = is_dead()
            except Exception as e:
                logger.debug("[출현감지] 사망 확인 실패: %s", e)
                dead = False
            dead_streak = dead_streak + 1 if dead else 0
            if dead_streak >= DEATH_CONFIRM_CHECKS:
                dead_streak = 0
                with self._lock:
                    still_tracked = self._tracked is boss
                    if still_tracked:
                        self._tracked = None
                        self._flag = False
                if still_tracked:
                    logger.info("[출현감지] %s: 캐릭터 사망 — 추적 중단 (타이머 유지)",
                                boss.config.display_name)
                    self.died.emit(boss)
                    self.presence_changed.emit(boss, False)
                continue
            if dead:
                continue   # 아직 한 번 — 확인될 때까지 이번 폴링은 판정에 안 넣는다

            # 3) 아이콘 + 피해량 패널 폴링
            try:
                _, score, _ = detect_boss()
                panel_hit, panel = detect_boss_engaged(boss.config.name)
            except Exception as e:
                logger.warning("[출현감지] 감지 실패: %s", e)
                continue
            icon = score >= (ICON_KEEP_THRESHOLD if self._flag
                             else MATCH_THRESHOLD)

            self._poll_count += 1
            if not self._first_poll_logged:
                logger.info("[출현감지] 폴링 가동 중 (첫 결과: 아이콘=%s(score=%.2f), "
                            "패널='%s')", icon, score, panel)
                self._first_poll_logged = True
                self._last_icon, self._last_panel = icon, panel
            changed = False
            # 신호가 바뀌는 순간을 남겨 둔다 — 패널 잔존 시간·아이콘 동작을 재기 위한 기록
            if icon != self._last_icon:
                logger.info("[출현감지] %s: 아이콘 %s (score=%.2f)",
                            boss.config.display_name, "보임" if icon else "사라짐", score)
                self._last_icon = icon
                changed = True
            if panel != self._last_panel:
                if panel and not panel_hit:
                    # 연탐 직전 보스의 잔상이거나 엉뚱한 보스와 교전 중
                    logger.info("[출현감지] %s: 패널에 다른 보스 '%s' — 이 보스로 치지 않음",
                                boss.config.display_name, panel)
                else:
                    logger.info("[출현감지] %s: 패널 '%s' → '%s'",
                                boss.config.display_name, self._last_panel, panel)
                self._last_panel = panel
                changed = True
            if not changed and self._poll_count % HEARTBEAT_LOG_EVERY_N_POLLS == 0:
                if self._flag:
                    progress = "보스 출현 중"
                elif not self._judging(datetime.now()):
                    progress = "예정 시각 전 — 판정 보류"
                else:
                    progress = (f"유예 {self._grace_polled_sec:.0f}"
                                f"/{self._grace_budget():.0f}s — 감지 대기")
                logger.info("[출현감지] 감지 중... (%d회차, 아이콘 score=%.2f, 패널='%s', %s)",
                            self._poll_count, score, panel, progress)

            self._process(boss, icon, panel_hit, panel, score)

    # ─── 상태 처리 ───────────────────────────────────
    def _judging(self, now: datetime) -> bool:
        """잡힘·미감지 중단 판정을 해도 되는가 — 스폰 예정 시각이 지나야 한다.
        일찍 도착해 미리 감지하는 동안 '아직 안 뜸' 을 '이미 잡힘/없음' 으로 읽으면 안 된다."""
        expected = self._expected_spawn
        return expected is None or now >= expected

    def _process(self, boss: BossState, icon: bool, panel_hit: bool, panel: str,
                 score: float = 0.0):
        now = datetime.now()
        judging = self._judging(now)
        if icon or panel_hit:
            self._grace_polled_sec = 0.0
            if self._measure_expected is not None:
                delta = (now - self._measure_expected).total_seconds()
                self._measure_expected = None
                logger.info("[출현감지] %s: 예정 대비 실측 오차 %+.1fs",
                            boss.config.display_name, delta)
        # 1. 아이콘 + 패널 → 보스 출현 중
        if icon and panel_hit:
            self._panel_only = 0
            if not self._flag:
                self._flag = True
                logger.info("[출현감지] %s: 출현 중 — 아이콘 + 패널 확인",
                            boss.config.display_name)
                self.detected.emit(boss)
                self.presence_changed.emit(boss, True)
            return

        # 3. 아이콘 없이 패널만 → PANEL_ONLY_KILL_SEC 동안 연속이면 잡힘
        #    연속 횟수와 시작 시각(= 처치 시각)은 예정 전에도 센다. 확정만 예정 시각 이후.
        if panel_hit:
            if self._panel_only == 0:
                self._panel_only_since = now
            self._panel_only += 1
            elapsed = (now - self._panel_only_since).total_seconds()
            if elapsed >= PANEL_ONLY_KILL_SEC and judging:
                self._save_icon_debug(boss)
                self._confirm_killed(
                    boss, self._panel_only_since,
                    f"아이콘 없이 패널만 {elapsed:.1f}s 연속 ({self._panel_only}회, "
                    f"마지막 score={score:.2f})")
            return

        self._panel_only = 0
        if icon:
            # (규칙 밖) 아이콘만 — 출현했지만 아직 교전 전
            return

        # 2. 둘 다 없음 → 아직 출현 안 함. 실제 폴링한 시간만 유예 예산에서 차감
        #    예정 시각 전엔 안 보이는 게 정상이라 차감하지 않는다.
        if not judging:
            return
        self._grace_polled_sec += POLL_INTERVAL_SEC
        if self._grace_polled_sec >= self._grace_budget():
            self._confirm_killed(boss)

    def _save_icon_debug(self, boss: BossState):
        """아이콘 소실 확정 순간의 아이콘 영역 캡처 — 오판이면 무엇이 보였는지 확인용."""
        path = ICON_DEBUG_DIR / (datetime.now().strftime("%m%d_%H%M%S")
                                 + f"_{boss.config.name}.png")
        try:
            save_icon_region(path)
            logger.info("[출현감지] 아이콘 영역 캡처 저장: %s", path.name)
            old = sorted(ICON_DEBUG_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for f in old[:max(0, len(old) - ICON_DEBUG_KEEP)]:
                f.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("[출현감지] 아이콘 영역 캡처 실패: %s", e)

    def _confirm_killed(self, boss: BossState, killed_at: datetime | None = None,
                        reason: str = ""):
        """추적 종료.

        killed_at 이 있으면 "잡힘" 으로 보고 killed_at + 주기로 다음 스폰을 찍는다
        (확정 시각이 아니라 아이콘이 사라지고 패널만 남기 시작한 시각 기준).
        없으면 한 번도 못 본 경우 — 이미 잡혔는지 예정 시각이 틀렸는지 구분할 수
        없으므로 타이머를 건드리지 않고 추적만 중단한다 (진실 소스는 주기 동기화).
        """
        if killed_at is not None:
            new_spawn = killed_at + boss.config.respawn_interval
            boss.set_next_spawn(new_spawn)
            logger.info(
                "[출현감지] %s: %s → 잡힘 확정 | 처치 %s | 다음 스폰 = %s",
                boss.config.display_name, reason,
                killed_at.strftime("%H:%M:%S"),
                new_spawn.strftime("%m/%d %H:%M:%S"))
        else:
            logger.info(
                "[출현감지] %s: %.0fs 동안 미감지 → 추적 중단%s "
                "(이미 잡혔거나 예정 시각 어긋남 — 타이머 유지, 동기화에 위임)",
                boss.config.display_name, self._grace_polled_sec,
                " (연탐 — 다음 보스로 즉시 이동)" if self._fast_confirm else "")
        self.ended.emit(boss)
        self.presence_changed.emit(boss, False)
        with self._lock:
            self._tracked = None
            self._expected_spawn = None
            self._poll_from = None
            self._tracking_started_at = None
            self._measure_expected = None
            self._grace_polled_sec = 0.0
            self._flag = False
            self._fast_confirm = False
            self._panel_only = 0
            self._panel_only_since = None
