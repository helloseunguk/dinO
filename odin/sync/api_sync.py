"""woogi.online API 기반 보스 시간표 동기화.

자동(주기) 동기화 전용 — 게임 화면 조작 없이 /api/boss-timeline 을 조회한다.
수동 '동기화' 버튼은 기존 OCR(screen_sync) 방식을 그대로 사용한다.

반환 계약은 ScreenSync.sync_all() 과 동일:
    dict[보스이름, datetime | SPAWNED]
따라서 scheduler._apply_sync_result() 를 그대로 재사용한다.
"""

import logging
import re
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

# 보스가 아닌 일정(kind='content') — 월드보스·발할라 대전 등.
# 행에 "HH:MM" 만 있고 날짜는 바로 위 날짜 구분 행("오늘 2026-10-18")에 있다.
# 서버는 **지난 회차를 내려주지 않으므로** 받은 시각을 여기서 들고 있는다 —
# 18:00 이 지나면 18:00 행이 사라지는데, 참가는 그 뒤에도 해야 한다.
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
CONTENT_KEEP = timedelta(hours=2)   # 지난 회차를 이만큼은 기억


def parse_contents(rows: list[dict]) -> list[tuple[str, datetime]]:
    """타임라인 행 → [(컨텐츠 이름, 시작 시각)]. 날짜를 모르는 행은 버린다."""
    out: list[tuple[str, datetime]] = []
    day: str | None = None
    for r in rows:
        kind = r.get("kind")
        if kind == "date":
            m = _DATE_RE.search(r.get("name") or "")
            day = m.group(0) if m else None
        elif kind == "content" and r.get("name"):
            try:
                # 서버가 전체 시각을 실어 주면 그쪽을 믿는다
                if r.get("datetime"):
                    at = datetime.fromisoformat(r["datetime"])
                elif day and r.get("time"):
                    at = datetime.fromisoformat(f"{day}T{r['time']}")
                else:
                    continue
            except ValueError:
                logger.warning("[API] 컨텐츠 시각 파싱 실패: %r", r)
                continue
            out.append((r["name"], at))
    return out


class ApiSync:
    """/api/boss-timeline 조회 → 보스별 다음 스폰 시각 dict 생성."""

    def __init__(self, client: ApiClient | None = None):
        self.client = client or ApiClient()
        self._contents: set[tuple[str, datetime]] = set()

    def content_times(self, name: str) -> list[datetime]:
        """마지막 동기화까지 알게 된 이 컨텐츠의 시작 시각들 (지난 회차 포함, 오름차순)."""
        return sorted(at for n, at in set(self._contents) if n == name)

    def fetch_all(self) -> dict[str, object]:
        payload = self.client.get_json(_TIMELINE_PATH)
        rows = (payload or {}).get("bosses", [])

        cutoff = datetime.now() - CONTENT_KEEP
        # 통째로 바꿔 끼운다 — GUI 스레드가 content_times 로 읽는 중일 수 있다
        self._contents = {c for c in self._contents | set(parse_contents(rows))
                          if c[1] >= cutoff}

        entries = [e for e in rows if e.get("kind") == "boss"]

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
