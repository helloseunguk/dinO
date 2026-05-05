"""SOON 이벤트 트리거 시간 (보스 스폰 N분 전) 설정.

GUI 콤보박스에서 1/3/5 분 선택 → alert_settings.json 에 저장.
앱 시작 시 로드 + 모든 BossConfig.alert_before 에 적용.
"""

import json
import logging
from datetime import timedelta

from paths import DATA_DIR

logger = logging.getLogger(__name__)

# 선택 가능한 값 (분 단위)
ALERT_CHOICES: tuple[int, ...] = (1, 3, 5)
DEFAULT_ALERT_MINUTES = 3

_SETTINGS_FILE = DATA_DIR / "alert_settings.json"


def load_alert_minutes() -> int:
    """저장된 분 값 로드. 없거나 잘못되면 DEFAULT 반환."""
    if not _SETTINGS_FILE.exists():
        return DEFAULT_ALERT_MINUTES
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        value = int(data.get("alert_minutes", DEFAULT_ALERT_MINUTES))
        if value in ALERT_CHOICES:
            return value
        logger.warning("[알림] 저장된 값 %s 이 유효 범위 밖 — 기본값 사용", value)
    except Exception as e:
        logger.warning("[알림] 설정 로드 실패 (%s) — 기본값 사용", e)
    return DEFAULT_ALERT_MINUTES


def save_alert_minutes(minutes: int) -> bool:
    """분 값 저장. 유효하지 않으면 False."""
    if minutes not in ALERT_CHOICES:
        logger.warning("[알림] 저장 거부 — 허용 값 아님: %s (허용=%s)",
                       minutes, ALERT_CHOICES)
        return False
    try:
        _SETTINGS_FILE.write_text(
            json.dumps({"alert_minutes": minutes}, indent=2),
            encoding="utf-8",
        )
        logger.info("[알림] 설정 저장 → %d분", minutes)
        return True
    except Exception as e:
        logger.error("[알림] 저장 실패: %s", e)
        return False


def apply_to_bosses(bosses, minutes: int) -> None:
    """모든 BossConfig.alert_before 를 분 단위로 일괄 갱신."""
    td = timedelta(minutes=minutes)
    for boss in bosses:
        boss.config.alert_before = td
    logger.info("[알림] %d개 보스에 alert_before=%d분 적용", len(bosses), minutes)
