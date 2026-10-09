"""보스 시간표 주기 갱신 방식 설정 — sync_settings.json

두 가지 소스 중 하나를 고른다:
  "api" — woogi.online /api/boss-timeline 조회. 게임 조작이 없어 가볍고
          절대 시각을 받아 drift 가 없다. 짧은 주기로 폴링 가능.
  "ocr" — 게임 내 시간표를 직접 열어 화면 캡처 + OCR. 게임을 점유하므로
          주기가 길고, 게임 UI 표시 단위만큼 정밀도가 떨어진다.

수동 '동기화' 버튼은 이 설정과 무관하게 항상 OCR 을 쓴다.
"""

import json
import logging

from odin.boss.boss_config import API_SYNC_INTERVAL, SYNC_INTERVAL
from odin.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

SOURCE_API = "api"
SOURCE_OCR = "ocr"
SYNC_SOURCES: tuple[str, ...] = (SOURCE_API, SOURCE_OCR)
DEFAULT_SOURCE = SOURCE_OCR

# 소스별 폴링 주기 (초) — 튜닝 값은 boss_config 에 모아둠
SYNC_INTERVALS: dict[str, int] = {
    SOURCE_API: API_SYNC_INTERVAL,
    SOURCE_OCR: SYNC_INTERVAL,
}

SOURCE_LABELS: dict[str, str] = {
    SOURCE_API: "API (woogi.online)",
    SOURCE_OCR: "화면 캡처 (OCR)",
}

_SETTINGS_FILE = DATA_DIR / "sync_settings.json"


def interval_for(source: str) -> int:
    """소스의 폴링 주기(초). 모르는 소스면 기본 소스의 주기."""
    return SYNC_INTERVALS.get(source, SYNC_INTERVALS[DEFAULT_SOURCE])


def label_for(source: str) -> str:
    """GUI/로그 표시용 이름."""
    return SOURCE_LABELS.get(source, source)


def describe(source: str) -> str:
    """'API (woogi.online) — 1분 주기' 형태의 설명 문자열."""
    sec = interval_for(source)
    period = f"{sec // 60}분" if sec >= 60 else f"{sec}초"
    return f"{label_for(source)} — {period} 주기"


def load_sync_source() -> str:
    """저장된 소스 로드. 없거나 잘못되면 DEFAULT_SOURCE."""
    if not _SETTINGS_FILE.exists():
        return DEFAULT_SOURCE
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        value = data.get("sync_source", DEFAULT_SOURCE)
        if value in SYNC_SOURCES:
            return value
        logger.warning("[동기화] 저장된 소스 %r 이 유효하지 않음 — 기본값 사용", value)
    except Exception as e:
        logger.warning("[동기화] 설정 로드 실패 (%s) — 기본값 사용", e)
    return DEFAULT_SOURCE


def save_sync_source(source: str) -> bool:
    """소스 저장. 허용 값이 아니면 False."""
    if source not in SYNC_SOURCES:
        logger.warning("[동기화] 저장 거부 — 허용 값 아님: %r (허용=%s)",
                       source, SYNC_SOURCES)
        return False
    try:
        _SETTINGS_FILE.write_text(
            json.dumps({"sync_source": source}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("[동기화] 설정 저장 → %s", describe(source))
        return True
    except Exception as e:
        logger.error("[동기화] 저장 실패: %s", e)
        return False
