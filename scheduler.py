import logging
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from automation_config import is_automation_target, priority_key
from boss_config import CHECK_INTERVAL, SYNC_INTERVAL
import boss_store
from models import BossState, BossStatus
from screen_sync import SPAWNED, ScreenSync

logger = logging.getLogger(__name__)

WORLD_BOSS_HOURS = [12, 20]


class BossScheduler(QObject):
    """Qt 시그널 기반 보스 스케줄러 — GUI 스레드에서 QTimer로 동작."""

    tick = Signal()
    sync_started = Signal()
    sync_finished = Signal(int)
    sync_failed = Signal(str)

    def __init__(
        self,
        bosses: list[BossState],
        notifier,
        screen_sync: ScreenSync | None = None,
        check_interval: int | None = None,
        sync_interval: int | None = None,
        automator=None,
        game_queue=None,
        presence_monitor=None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.bosses = bosses
        self._boss_map = {b.config.name: b for b in bosses}
        self.notifier = notifier
        self.screen_sync = screen_sync
        self.automator = automator
        self.game_queue = game_queue
        self.presence_monitor = presence_monitor

        # 보스 종료 시(True→False) 즉시 boss_times.json 에 저장
        if self.presence_monitor is not None:
            self.presence_monitor.ended.connect(self._on_boss_ended)
        self.check_interval = CHECK_INTERVAL if check_interval is None else check_interval
        self.sync_interval = SYNC_INTERVAL if sync_interval is None else sync_interval
        self._alerted: set[str] = set()
        self._world_boss_alerted: set[str] = set()
        self._last_sync: float = 0
        self._running = False
        self._sync_pending = False   # 큐에 sync 태스크가 적재됐는지

        # 런타임 토글 — GUI 에서 on/off. 기본값 전부 ON.
        self.automation_enabled = True    # 보스로 이동 자동화 (run_for)
        self.idle_enabled = True          # 유휴 시 아이들 액션 (check_idle_action)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _format_remaining(self, td: timedelta) -> str:
        total_seconds = int(td.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}시간 {minutes}분 {seconds}초"
        if minutes > 0:
            return f"{minutes}분 {seconds}초"
        return f"{seconds}초"

    def _check_all(self):
        # 이번 tick에서 새로 SOON 된 자동화 대상 보스 — 우선순위 정렬 후 일괄 트리거
        new_automation_targets: list[BossState] = []

        for boss in self.bosses:
            boss.advance_to_next_spawn()
            remaining = boss.remaining_time
            status = boss.status

            if remaining is None:
                continue

            remaining_str = self._format_remaining(remaining)
            spawn_str = boss.next_spawn_at.strftime("%m/%d %H:%M:%S") if boss.next_spawn_at else "?"

            if status == BossStatus.SOON and boss.config.name not in self._alerted:
                # 알림은 모든 SOON 보스에 대해 발동 (자동화 대상 여부 무관)
                self.notifier.notify(boss, f"{remaining_str} 후 출현! (예정: {spawn_str})")
                self._alerted.add(boss.config.name)
                # 자동화/출현감지는 대상 리스트에 있는 보스만
                if is_automation_target(boss.config):
                    new_automation_targets.append(boss)
                else:
                    logger.debug("[자동화] 대상 아님 스킵: %s",
                                 boss.config.display_name)

            if status == BossStatus.SPAWNED and boss.config.name in self._alerted:
                self.notifier.notify(boss, "보스가 출현했습니다!")
                self._alerted.discard(boss.config.name)

        # 우선순위 정렬 후 automator 큐에 순서대로 적재
        # (presence_monitor는 automator가 시퀀스 완료 후 start_tracking 호출)
        if new_automation_targets and self.automation_enabled:
            new_automation_targets.sort(key=lambda b: priority_key(b.config))
            order_log = " > ".join(b.config.display_name for b in new_automation_targets)
            logger.info("[자동화] 우선순위 정렬: %s", order_log)
            for boss in new_automation_targets:
                if self.automator is not None:
                    try:
                        self.automator.run_for(boss)
                    except Exception as e:
                        logger.warning("[자동화] run_for 실패: %s", e)
        elif new_automation_targets:
            logger.info("[자동화] 비활성화 상태 — %d마리 스킵", len(new_automation_targets))

    def _check_world_boss(self):
        now = datetime.now()
        for hour in WORLD_BOSS_HOURS:
            key = f"{now.date()}_{hour}"
            minutes_until = (hour * 60) - (now.hour * 60 + now.minute)
            if 0 < minutes_until <= 3 and key not in self._world_boss_alerted:
                self.notifier.send(f"⚔️ **월드보스** {minutes_until}분 후 출현! ({hour}:00)")
                self._world_boss_alerted.add(key)

    def _try_sync(self):
        if self.screen_sync is None or self.game_queue is None:
            return
        if self._sync_pending:
            return
        now = time.time()
        if now - self._last_sync < self.sync_interval:
            return
        self.trigger_sync_now()

    @Slot()
    def trigger_sync_now(self):
        """OCR 동기화 태스크를 공용 큐에 적재 (자동화와 경쟁 없이 순차 처리)."""
        if self.screen_sync is None:
            self.sync_failed.emit("screen_sync가 설정되지 않음")
            return
        if self.game_queue is None:
            self.sync_failed.emit("game_queue가 설정되지 않음")
            return
        if self._sync_pending:
            logger.info("[동기화] 이미 큐에 적재됨 — 스킵")
            return

        self._sync_pending = True
        self.sync_started.emit()
        self.game_queue.submit("sync", self._do_sync)

    def _do_sync(self):
        """큐 워커 스레드에서 실행되는 실제 sync 로직."""
        try:
            logger.info("[동기화] 게임 화면 캡처 시작...")
            sync_data = self.screen_sync.sync_all()
            synced = self._apply_sync_result(sync_data)
            self.sync_finished.emit(synced)
            self.tick.emit()
        except Exception as e:
            logger.exception("[동기화] 실패")
            self.sync_failed.emit(str(e))
        finally:
            self._last_sync = time.time()
            self._sync_pending = False

    def _apply_sync_result(self, sync_data: dict) -> int:
        """sync_data: {boss_name: datetime | SPAWNED}
        datetime은 캡처 시점 기준 절대 스폰 시각이라 적용 지연에 영향 없음.
        """
        synced = 0
        for boss_name, value in sync_data.items():
            boss = self._boss_map.get(boss_name)
            if boss is None:
                continue
            if value == SPAWNED:
                boss.mark_spawned()
                logger.info("[동기화] %s: 출현 중", boss_name)
                synced += 1
                continue
            # value = 절대 스폰 시각 (datetime)
            old_spawn = boss.next_spawn_at
            boss.set_next_spawn(value)
            if old_spawn is not None:
                diff = abs((value - old_spawn).total_seconds())
                if diff > 60:
                    logger.info("[보정] %s: %s → %s (차이: %d초)",
                                boss_name,
                                old_spawn.strftime("%m/%d %H:%M:%S"),
                                value.strftime("%m/%d %H:%M:%S"),
                                int(diff))
            synced += 1
        logger.info("[동기화] %d개 보스 동기화 완료", synced)
        boss_store.save(self.bosses)
        return synced

    @Slot()
    def _tick(self):
        self._try_sync()
        self._check_all()
        self._check_world_boss()
        self._check_idle_action()
        self.tick.emit()

    def _check_idle_action(self):
        """자동화 유휴 시 다음 스폰까지 남은 시간 계산 후 automator에 전달."""
        if self.automator is None or not self.idle_enabled:
            return
        # 자동화 마스터 OFF면 아이들도 OFF (자동화 OFF = 알림만 동작)
        if not self.automation_enabled:
            return
        now = datetime.now()
        min_sec = None
        for boss in self.bosses:
            if not is_automation_target(boss.config):
                continue
            if boss.next_spawn_at is None:
                continue
            sec = (boss.next_spawn_at - now).total_seconds()
            if sec > 0 and (min_sec is None or sec < min_sec):
                min_sec = sec
        try:
            self.automator.check_idle_action(min_sec)
        except Exception as e:
            logger.warning("[아이들] check_idle_action 실패: %s", e)

    @Slot()
    def start(self):
        if self._running:
            return
        logger.info("스케줄러 시작 (체크 %d초 / 동기화 %d초)",
                    self.check_interval, self.sync_interval)
        self._running = True
        self._timer.start(self.check_interval * 1000)
        self._tick()

    @Slot()
    def stop(self):
        if not self._running:
            return
        self._running = False
        self._timer.stop()
        logger.info("스케줄러 종료")

    @property
    def is_running(self) -> bool:
        return self._running

    @Slot(object)
    def _on_boss_ended(self, boss):
        """PresenceMonitor.ended 훅 — 스폰 시간 리셋은 이미 됐고, 파일에 즉시 반영."""
        # 새 스폰 시점 도래 전에 같은 보스 SOON 재발동을 위해 _alerted에서 제거
        self._alerted.discard(boss.config.name)
        try:
            boss_store.save(self.bosses)
            logger.info("[스케줄러] '%s' 종료 감지 → boss_times.json 저장",
                        boss.config.display_name)
        except Exception as e:
            logger.warning("[스케줄러] boss_times.json 저장 실패: %s", e)
