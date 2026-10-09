"""woogi.online API 기반 보스 시간표 동기화.

자동(주기) 동기화 전용 — 게임 화면 조작 없이 /api/boss-timeline 을 조회한다.
수동 '동기화' 버튼은 기존 OCR(screen_sync) 방식을 그대로 사용한다.

반환 계약은 ScreenSync.sync_all() 과 동일:
    dict[보스이름, datetime | SPAWNED]
따라서 scheduler._apply_sync_result() 를 그대로 재사용한다.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from odin.sync.api_client import ApiClient
from odin.boss.boss_config import BOSS_CONFIGS
from odin.sync.screen_sync import SPAWNED  # 동일 sentinel 재사용 (계약 일치 보장)

logger = logging.getLogger(__name__)

_TIMELINE_PATH = "/api/boss-timeline"

# API 등록 시각은 실제 출현보다 이를 수 있어(분 단위로만 등록됨) 일괄 보정.
# 파싱 시점에 더하므로 이후 정렬·"가장 이른 미래 스폰" 선택도 보정된 값 기준.
# OCR(screen_sync) 경로에는 적용되지 않는다.
API_TIME_OFFSET = timedelta(minutes=1)

# 매칭: config 보스 이름 == API 원본 이름(name). 별칭(displayName)·지역은 무시.
# 이름이 어긋나면 매핑을 추가하지 말고 config 이름을 API 원본 이름에 맞춘다.


class ApiSync:
    """/api/boss-timeline 조회 → 보스별 다음 스폰 시각 dict 생성."""

    def __init__(self, client: ApiClient | None = None):
        self.client = client or ApiClient()

    def fetch_all(self) -> dict[str, object]:
        payload = self.client.get_json(_TIMELINE_PATH)
        entries = [e for e in (payload or {}).get("bosses", [])
                   if e.get("kind") == "boss"]

        # API 이름별로 집계: 출현중 여부 + 스폰 시각 목록(보스당 여러 날치)
        agg: dict[str, dict] = defaultdict(lambda: {"live": False, "times": []})
        for e in entries:
            name = e.get("name")
            if not name:
                continue
            bucket = agg[name]
            if e.get("isLiveBoss"):
                bucket["live"] = True
            dt_str = e.get("bossDatetime")
            if dt_str:
                try:
                    # 타임존 없는 로컬 시각 — 앱의 datetime.now()(naive)와 동일 기준
                    bucket["times"].append(
                        datetime.fromisoformat(dt_str) + API_TIME_OFFSET)
                except ValueError:
                    logger.warning("[API] 시각 파싱 실패: %r", dt_str)

        now = datetime.now()
        result: dict[str, object] = {}
        matched = 0
        for cfg in BOSS_CONFIGS:
            bucket = agg.get(cfg.name)
            if bucket is None:
                continue  # 타임라인에 없음 → 기존 값 유지
            if bucket["live"]:
                result[cfg.name] = SPAWNED
                matched += 1
                continue
            future = sorted(t for t in bucket["times"] if t >= now)
            times = future if future else sorted(bucket["times"])
            if not times:
                continue
            result[cfg.name] = times[0]  # 가장 이른 미래 스폰
            matched += 1

        logger.info("[API 동기화] %d/%d 보스 매칭 (타임라인 %d행, 보정 +%.0f초)",
                    matched, len(BOSS_CONFIGS), len(entries),
                    API_TIME_OFFSET.total_seconds())
        return result
