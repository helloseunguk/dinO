"""
보스 리젠 시각 영구 저장/로드 모듈
boss_times.json 파일로 관리
"""

import json
import logging
from datetime import datetime, timedelta

from models import BossState
from paths import DATA_DIR

logger = logging.getLogger(__name__)

STORE_PATH = DATA_DIR / "boss_times.json"


def save(bosses: list[BossState]) -> None:
    """현재 보스 리젠 시각을 JSON 파일에 저장"""
    data = {}
    for boss in bosses:
        if boss.next_spawn_at is not None:
            data[boss.config.name] = boss.next_spawn_at.isoformat()

    STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[저장] %d개 보스 리젠 시각 저장 → %s", len(data), STORE_PATH.name)


def load(bosses: list[BossState]) -> int:
    """JSON 파일에서 보스 리젠 시각 로드 → BossState에 반영"""
    if not STORE_PATH.exists():
        logger.info("[로드] 저장 파일 없음, 건너뜀")
        return 0

    data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    boss_map = {b.config.name: b for b in bosses}
    loaded = 0

    for name, iso_str in data.items():
        boss = boss_map.get(name)
        if boss is None:
            continue
        try:
            spawn_at = datetime.fromisoformat(iso_str)
            boss.set_next_spawn(spawn_at)
            boss.advance_to_next_spawn()
            loaded += 1
        except (ValueError, TypeError):
            logger.warning("[로드] %s: 잘못된 시각 형식 (%s)", name, iso_str)

    logger.info("[로드] %d개 보스 리젠 시각 로드 ← %s", loaded, STORE_PATH.name)
    return loaded
