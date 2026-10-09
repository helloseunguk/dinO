from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class BossStatus(Enum):
    WAITING = "대기중"
    SOON = "임박"
    SPAWNED = "출현"
    UNKNOWN = "알 수 없음"


@dataclass
class BossConfig:
    """보스 설정 정보"""
    name: str
    respawn_interval: timedelta  # 리젠 주기
    region: str = ""  # 지역명
    alert_before: timedelta = field(default_factory=lambda: timedelta(minutes=5))  # 몇 분 전에 알림

    @property
    def display_name(self) -> str:
        return f"[{self.region}] {self.name}" if self.region else self.name


@dataclass
class BossState:
    """보스의 현재 상태"""
    config: BossConfig
    _next_spawn_at: datetime | None = None  # 다음 리젠 시각
    _is_spawned: bool = False  # OCR에서 "출현 중"으로 확인됨

    @property
    def next_spawn_at(self) -> datetime | None:
        return self._next_spawn_at

    @property
    def remaining_time(self) -> timedelta | None:
        if self._next_spawn_at is None:
            return None
        remaining = self._next_spawn_at - datetime.now()
        return remaining if remaining.total_seconds() > 0 else timedelta(0)

    @property
    def status(self) -> BossStatus:
        if self._is_spawned:
            return BossStatus.SPAWNED
        remaining = self.remaining_time
        if remaining is None:
            return BossStatus.UNKNOWN
        if remaining.total_seconds() <= 0:
            return BossStatus.SPAWNED
        if remaining <= self.config.alert_before:
            return BossStatus.SOON
        return BossStatus.WAITING

    def set_next_spawn(self, spawn_at: datetime):
        """최초 리젠 시각을 직접 지정"""
        self._next_spawn_at = spawn_at
        self._is_spawned = False

    def mark_spawned(self):
        """출현 중 상태로 설정 (OCR에서 '출현 중' 감지)"""
        self._is_spawned = True

    def sync_remaining(self, remaining: timedelta):
        """OCR에서 읽은 남은시간으로 리젠 시각 보정"""
        self._next_spawn_at = datetime.now() + remaining
        self._is_spawned = False

    def advance_to_next_spawn(self):
        """리젠 시각이 지났으면 다음 주기로 자동 갱신 (출현 중이면 건너뜀)"""
        if self._is_spawned:
            return
        if self._next_spawn_at is None:
            return
        now = datetime.now()
        while self._next_spawn_at <= now:
            self._next_spawn_at += self.config.respawn_interval
