"""보스 방문 기록 영구 저장 — DATA_DIR/boss_visits.json

자동화로 "보스 앞에 도착한 순간"(시퀀스 완료) 마다 1건 추가.
이동 중 실패(Esc/타임아웃)한 경우는 기록하지 않음.
잡았는지 여부는 기록하지 않음 — "도착했다"는 사실만.

구조:
  [
    {"timestamp": "2026-04-23T17:30:15", "name": "니드호그", "region": "요툰"},
    ...
  ]

오래된 엔트리는 MAX_ENTRIES 초과 시 앞에서부터 잘림.
"""

import json
import logging
from datetime import datetime

from paths import DATA_DIR

logger = logging.getLogger(__name__)

_VISITS_FILE = DATA_DIR / "boss_visits.json"
MAX_ENTRIES = 5000

_cache: list[dict] | None = None


def _load() -> list[dict]:
    if not _VISITS_FILE.exists():
        return []
    try:
        data = json.loads(_VISITS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        logger.warning("[방문기록] %s 형식 오류 — 빈 리스트로 시작", _VISITS_FILE.name)
        return []
    except Exception as e:
        logger.warning("[방문기록] 로드 실패 (%s) — 빈 리스트로 시작", e)
        return []


def _save(visits: list[dict]) -> None:
    try:
        _VISITS_FILE.write_text(
            json.dumps(visits, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[방문기록] 저장 실패: %s", e)


def all_visits() -> list[dict]:
    """저장된 방문 기록 전체 (오래된→최근 순)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return list(_cache)


def append(name: str, region: str, timestamp: datetime | None = None) -> dict:
    """방문 기록 1건 추가 + 파일 저장. 반환: 새로 추가된 엔트리."""
    global _cache
    if _cache is None:
        _cache = _load()
    entry = {
        "timestamp": (timestamp or datetime.now()).isoformat(timespec="seconds"),
        "name": name,
        "region": region,
    }
    _cache.append(entry)
    if len(_cache) > MAX_ENTRIES:
        _cache = _cache[-MAX_ENTRIES:]
    _save(_cache)
    return entry


def format_line(entry: dict) -> str:
    """로그 뷰 표시용 한 줄 문자열."""
    ts = entry.get("timestamp", "")
    # ISO 포맷 → 보기 편한 형식 ('2026-04-23T17:30:15' → '2026-04-23 17:30:15')
    ts = ts.replace("T", " ")
    region = entry.get("region", "")
    name = entry.get("name", "")
    tag = f"[{region}] " if region else ""
    return f"{ts}  {tag}{name}"
