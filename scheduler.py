import logging
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from automation_config import (
    is_automation_target, is_priority_enabled, order_key, priority_key)
from boss_config import CHECK_INTERVAL
import boss_store
import sync_settings
from models import BossState, BossStatus
from screen_sync import SPAWNED, ScreenSync

logger = logging.getLogger(__name__)

WORLD_BOSS_HOURS = [12, 20]

# 스폰 시각이 정확히 겹치는 보스들을 우선순위 순으로 1초씩 벌려 실행 순서를 확정.
# (사이트/API 가 분 단위로만 주기 때문에 동시 스폰이 자주 발생 — boss_times.json 참고)
SPAWN_STAGGER_STEP = timedelta(seconds=1)


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
        api_sync=None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.bosses = bosses
        self._boss_map = {b.config.name: b for b in bosses}
        self.notifier = notifier
        self.screen_sync = screen_sync   # 수동 동기화 버튼 (OCR)
        self.api_sync = api_sync         # 자동 주기 동기화 (API)
        self.automator = automator
        self.game_queue = game_queue
        self.presence_monitor = presence_monitor

        # 보스 종료 시(True→False) 즉시 boss_times.json 에 저장
        if self.presence_monitor is not None:
            self.presence_monitor.ended.connect(self._on_boss_ended)
        self.check_interval = CHECK_INTERVAL if check_interval is None else check_interval
        # 주기 동기화 소스 — 옵션에서 선택 (sync_settings.json 에 저장).
        # sync_interval 을 명시로 넘기면 소스별 기본 주기를 덮어쓴다 (테스트용).
        self.sync_source = sync_settings.load_sync_source()
        self._sync_interval_override = sync_interval
        self.sync_interval = (sync_interval if sync_interval is not None
                              else sync_settings.interval_for(self.sync_source))
        logger.info("[동기화] 주기 갱신 방식: %s",
                    sync_settings.describe(self.sync_source))
        self._alerted: set[str] = set()
        self._world_boss_alerted: set[str] = set()
        self._last_sync: float = 0
        self._running = False
        self._sync_pending = False   # 큐에 sync 태스크가 적재됐는지
        self._stagger_logged: set[str] = set()  # 겹침보정 로그 중복 방지

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

    def _stagger_overlapping_spawns(self):
        """스폰 시각이 완전히 겹치는 보스들을 우선순위 순으로 +1초씩 분리.

        priority_key 가 작을수록(=상위) 원래 시각 유지, 하위로 갈수록 +1초씩 밀림.
        결과적으로 겹치는 구간에서 상위 보스가 항상 먼저 SOON/출발한다.
        한 번 벌려두면 시각이 서로 달라지므로 재실행해도 추가로 밀리지 않음(멱등).
        동기화가 분 단위 원본 시각으로 덮어쓰면 다음 tick 에서 다시 보정된다.
        """
        groups: dict[datetime, list[BossState]] = {}
        for boss in self.bosses:
            if boss.status == BossStatus.SPAWNED:
                continue  # 출현 중 — 시각 건드리지 않음
            if boss.next_spawn_at is None:
                continue
            groups.setdefault(boss.next_spawn_at, []).append(boss)

        for spawn, group in groups.items():
            if len(group) < 2:
                continue
            # 시각이 똑같은 무리라 '빠른 순' 기준이 없다 —
            # 우선순위 OFF 면 config 선언 순서를 그대로 둔다.
            if is_priority_enabled():
                group.sort(key=lambda b: priority_key(b.config))
            for rank, boss in enumerate(group[1:], start=1):
                boss.set_next_spawn(spawn + SPAWN_STAGGER_STEP * rank)
            order = " > ".join(b.config.display_name for b in group)
            sig = f"{spawn.isoformat()}|{order}"
            if sig not in self._stagger_logged:
                if len(self._stagger_logged) > 200:
                    self._stagger_logged.clear()
                self._stagger_logged.add(sig)
                logger.info("[겹침보정] %s 동시 스폰 %d마리 → 우선순위 순 +1초씩 분리: %s",
                            spawn.strftime("%m/%d %H:%M:%S"), len(group), order)

    def _check_all(self):
        # 이번 tick에서 새로 SOON 된 자동화 대상 보스 — 우선순위 정렬 후 일괄 트리거
        new_automation_targets: list[BossState] = []

        # 스폰 시각 갱신 후 겹치는 보스 분리 — SOON 판정 전에 순서를 확정해야 함
        for boss in self.bosses:
            boss.advance_to_next_spawn()
        self._stagger_overlapping_spawns()

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
            new_automation_targets.sort(key=order_key)
            order_log = " > ".join(b.config.display_name for b in new_automation_targets)
            logger.info("[자동화] %s 정렬: %s",
                        "우선순위" if is_priority_enabled() else "스폰 빠른 순",
                        order_log)
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

    @Slot(str)
    def set_sync_source(self, source: str):
        """주기 동기화 소스 변경 (옵션 창에서 호출). 주기도 소스 기본값으로 갱신."""
        if source not in sync_settings.SYNC_SOURCES:
            logger.warning("[동기화] 알 수 없는 소스 %r — 변경 안 함", source)
            return
        if source == self.sync_source:
            return
        self.sync_source = source
        if self._sync_interval_override is None:
            self.sync_interval = sync_settings.interval_for(source)
        logger.info("[동기화] 주기 갱신 방식 변경 → %s",
                    sync_settings.describe(source))

    def _try_sync(self):
        """주기 동기화 — 선택된 소스(API / OCR)로 실행."""
        if self.game_queue is None:
            return
        if self._sync_pending:
            return
        now = time.time()
        if now - self._last_sync < self.sync_interval:
            return
        if self.sync_source == sync_settings.SOURCE_API:
            self.trigger_api_sync()
        else:
            self.trigger_sync_now()

    @Slot()
    def trigger_api_sync(self):
        """API(/api/boss-timeline) 동기화."""
        if self.api_sync is None:
            logger.warning("[동기화] api_sync 미설정 — 주기 동기화 스킵")
            return
        self._enqueue_sync("api")

    @Slot()
    def trigger_sync_now(self):
        """OCR(화면 캡처) 동기화 — 수동 '동기화' 버튼 + OCR 모드의 주기 동기화."""
        if self.screen_sync is None:
            self.sync_failed.emit("screen_sync가 설정되지 않음")
            return
        self._enqueue_sync("ocr")

    def _enqueue_sync(self, source: str):
        """sync 태스크를 공용 큐에 적재 (자동화와 경쟁 없이 순차 처리).

        source: "api" = 주기 동기화(HTTP), "ocr" = 수동 버튼(화면 캡처).
        """
        if self.game_queue is None:
            self.sync_failed.emit("game_queue가 설정되지 않음")
            return
        if self._sync_pending:
            logger.info("[동기화] 이미 큐에 적재됨 — 스킵")
            return
        self._sync_pending = True
        self.sync_started.emit()
        self.game_queue.submit(f"sync:{source}", lambda: self._do_sync(source))

    def _do_sync(self, source: str = "ocr"):
        """큐 워커 스레드에서 실행되는 실제 sync 로직."""
        try:
            if source == "api":
                logger.info("[동기화] API 시간표 조회...")
                sync_data = self.api_sync.fetch_all()
            else:
                logger.info("[동기화] 게임 화면 캡처 시작...")
                sync_data = self.screen_sync.sync_all()
            synced = self._apply_sync_result(sync_data)
            self.sync_finished.emit(synced)
            self.tick.emit()
        except Exception as e:
            from api_client import ApiError
            if isinstance(e, ApiError):
                # 서버 5xx/네트워크 단절 등 — 다음 주기에 다시 시도하면 되므로
                # 스택 트레이스 없이 한 줄로 남긴다.
                logger.warning("[동기화] 실패 (source=%s): %s — 다음 주기에 재시도",
                               source, e)
            else:
                logger.exception("[동기화] 실패 (source=%s)", source)
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
            # value = 절대 스폰 시각 (datetime).
            # 소스별 보정은 각 sync 구현이 이미 반영해서 넘긴다 (api_sync.API_TIME_OFFSET).
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
