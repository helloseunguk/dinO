"""운영 공지 감시 — woogi.online 공지가 갱신되면 알림 발송.

동작:
  자체 데몬 스레드에서 POLL_INTERVAL_SEC 간격으로 /api/notices 를 조회하고,
  운영 공지 섹션의 지문(fingerprint)이 바뀌면 notice_posted 시그널을 emit 한다.
  게임 조작이 없는 순수 HTTP 호출이라 game_queue 를 타지 않는다
  (큐를 타면 공지 폴링이 보스 자동화를 밀어낸다).

중복 방지:
  마지막으로 알린 지문을 notice_state.json 에 저장 → 앱을 재시작해도
  같은 공지를 다시 알리지 않는다. 최초 실행(저장된 지문 없음) 때는
  현재 공지를 "이미 본 것"으로 기록만 하고 알리지 않는다.

서버 계약:
  /api/notices 응답의 dict 값 중 NOTICE_KEYS 에 해당하는 항목.
  항목 형태는 기존 섹션들과 동일:
      {"key": "...", "content": <임의>, "updatedAt": "ISO8601"}
  content 가 무엇이든(문자열/dict/list) 지문 계산에 그대로 쓰이므로,
  사이트에서 어떤 구조로 넣든 갱신 감지는 동작한다.
"""

import hashlib
import json
import logging
import threading
import time

from PySide6.QtCore import QObject, Signal

from paths import DATA_DIR

logger = logging.getLogger(__name__)

_NOTICES_PATH = "/api/notices"
_STATE_FILE = DATA_DIR / "notice_state.json"

# 폴링 간격 (초) — 공지는 실시간성이 크지 않고 서버 부담도 줄인다
POLL_INTERVAL_SEC = 120.0
# 첫 조회를 앱 기동 직후로 미루는 지연 (초) — 시작 시 다른 초기화와 겹치지 않게
INITIAL_DELAY_SEC = 15.0

# 사이트가 어떤 key 로 넣을지 확정 전이라 후보를 순서대로 탐색한다.
# 먼저 발견되는 하나를 운영 공지로 취급.
NOTICE_KEYS: tuple[str, ...] = (
    "ops", "opsNotice", "adminNotice", "운영공지", "운영 공지",
)

# 알림 본문에 넣을 미리보기 최대 길이
PREVIEW_MAX_CHARS = 200


def _fingerprint(entry: dict) -> str:
    """공지 항목의 지문. updatedAt 이 있으면 그것만으로도 충분하지만,
    없거나 안 바뀌는 서버 구현도 있을 수 있어 content 해시를 함께 쓴다."""
    updated = str(entry.get("updatedAt", ""))
    content = json.dumps(entry.get("content", ""), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"{updated}|{digest}"


def _preview(content) -> str:
    """content 에서 사람이 읽을 미리보기 한 토막 뽑기."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, dict):
        # title/text/body 처럼 사람이 읽는 필드가 있으면 우선 사용
        for key in ("title", "text", "body", "content", "message"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                text = value
                break
        else:
            text = json.dumps(content, ensure_ascii=False)
    elif isinstance(content, list):
        parts = [_preview(item) for item in content[:3]]
        text = " / ".join(p for p in parts if p)
    else:
        text = str(content)
    text = " ".join(text.split())  # 개행·연속 공백 정리
    if len(text) > PREVIEW_MAX_CHARS:
        text = text[:PREVIEW_MAX_CHARS].rstrip() + "…"
    return text


class NoticeWatcher(QObject):
    """운영 공지 갱신 감시. 갱신 시 notice_posted(preview) emit."""

    notice_posted = Signal(str)   # 미리보기 텍스트

    def __init__(self, api_client, parent: QObject | None = None):
        super().__init__(parent)
        self.client = api_client
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_fingerprint: str | None = self._load_state()

    # ─── 상태 저장 ───────────────────────────────────
    def _load_state(self) -> str | None:
        if not _STATE_FILE.exists():
            return None
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            value = data.get("fingerprint")
            return value if isinstance(value, str) else None
        except Exception as e:
            logger.warning("[운영공지] 상태 로드 실패 (%s) — 처음부터 시작", e)
            return None

    def _save_state(self, fingerprint: str) -> None:
        try:
            _STATE_FILE.write_text(
                json.dumps({"fingerprint": fingerprint}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[운영공지] 상태 저장 실패: %s", e)

    # ─── 외부 API ───────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="NoticeWatcher")
        self._thread.start()
        logger.info("[운영공지] 감시 시작 (%.0f초 간격)", POLL_INTERVAL_SEC)

    def stop(self) -> None:
        self._stop_event.set()

    def check_once(self) -> str | None:
        """1회 조회. 새 공지가 있으면 미리보기 반환(+시그널 emit), 없으면 None."""
        entry = self._fetch_entry()
        if entry is None:
            return None
        fingerprint = _fingerprint(entry)
        if fingerprint == self._last_fingerprint:
            return None

        first_run = self._last_fingerprint is None
        self._last_fingerprint = fingerprint
        self._save_state(fingerprint)
        if first_run:
            # 최초 실행 — 기존 공지를 새 글로 오인해 알리지 않는다
            logger.info("[운영공지] 최초 조회 — 현재 공지를 기준선으로 기록 (알림 없음)")
            return None

        preview = _preview(entry.get("content", ""))
        logger.info("[운영공지] 갱신 감지 → 알림 발송: %s", preview)
        self.notice_posted.emit(preview)
        return preview

    # ─── 내부 ───────────────────────────────────
    def _fetch_entry(self) -> dict | None:
        """/api/notices 에서 운영 공지 항목 찾기. 없으면 None."""
        try:
            payload = self.client.get_json(_NOTICES_PATH)
        except Exception as e:
            logger.warning("[운영공지] 조회 실패: %s", e)
            return None
        if not isinstance(payload, dict):
            logger.warning("[운영공지] 예상과 다른 응답 형식: %s", type(payload).__name__)
            return None
        for key in NOTICE_KEYS:
            entry = payload.get(key)
            if isinstance(entry, dict):
                return entry
        logger.debug("[운영공지] 응답에 운영 공지 키 없음 (있는 키: %s)",
                     list(payload.keys()))
        return None

    def _loop(self) -> None:
        if self._stop_event.wait(INITIAL_DELAY_SEC):
            return
        while not self._stop_event.is_set():
            if self.enabled:
                try:
                    self.check_once()
                except Exception:
                    logger.exception("[운영공지] 감시 루프 오류")
            self._stop_event.wait(POLL_INTERVAL_SEC)
