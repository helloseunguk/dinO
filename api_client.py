"""woogi.online 보스 타이머 API 클라이언트.

토큰 관리:
  - DATA_DIR/api_config.json (gitignore) 에 base_url / token / nickname / password 저장
  - 요청 시 Authorization: Bearer <token>
  - 401 응답이면 /api/auth/login {nickname, password} 로 재로그인 후 토큰 갱신·1회 재시도

표준 라이브러리(urllib)만 사용 — 별도 패키지 불필요.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from paths import DATA_DIR

logger = logging.getLogger(__name__)

_CONFIG_PATH = DATA_DIR / "api_config.json"
_DEFAULT_BASE_URL = "https://woogi.online"
_LOGIN_PATH = "/api/auth/login"
# 서버가 거의 모든 API 에서 "어느 길드인지" 를 요구한다 (없으면 400).
# 호출부마다 붙이면 빠뜨리기 쉬워서 _request 에서 일괄로 붙인다.
# api_config.json 의 "guild" 로 바꿀 수 있다.
_DEFAULT_GUILD = "dejavu"
# 인증 경로는 길드와 무관 — 붙이지 않는다.
_NO_GUILD_PREFIXES = ("/api/auth/",)
_TIMEOUT = 15
# 이미지 업로드는 수 MB 전송 + 서버 OCR(수 초)까지 기다려야 해서 별도 타임아웃
_UPLOAD_TIMEOUT = 90
# 일시적 서버 오류/네트워크 오류는 1회 재시도. 큐가 직렬이라 오래 붙잡지 않도록
# 재시도 횟수와 백오프는 짧게 유지한다.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})
_MAX_RETRIES = 1
_RETRY_BACKOFF_SEC = 2.0
# 기본 파이썬 UA(Python-urllib/x)는 서버 WAF에서 403으로 차단될 수 있어 브라우저 UA 사용
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class ApiError(Exception):
    """API 호출 관련 오류."""


def _http_detail(e: urllib.error.HTTPError) -> str:
    """HTTPError 를 서버가 준 본문까지 포함한 한 줄 메시지로.

    urllib 기본 메시지는 'HTTP Error 500: Internal Server Error' 뿐이라
    서버가 무슨 말을 했는지 알 수 없다. 본문을 읽어 붙인다.
    """
    try:
        raw = e.read().decode("utf-8", "replace").strip()
    except Exception:
        raw = ""
    if not raw:
        return f"HTTP {e.code} {e.reason}"
    try:
        data = json.loads(raw)
        msg = data.get("message") or data.get("error") or raw
    except Exception:
        msg = raw
    return f"HTTP {e.code} {e.reason} — {str(msg)[:200]}"


def _multipart_body(fields: dict[str, str],
                    files: list[tuple[str, str, bytes, str]]
                    ) -> tuple[bytes, str]:
    """multipart/form-data 본문 생성 — requests 없이 표준 라이브러리만으로.

    반환: (본문 바이트, Content-Type 헤더 값)
    """
    boundary = f"----odin{uuid.uuid4().hex}"
    sep = f"--{boundary}\r\n".encode()
    out = bytearray()
    for name, value in fields.items():
        out += sep
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    for name, filename, blob, mime in files:
        out += sep
        out += (f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n').encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += blob
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class ApiClient:
    """woogi.online API 호출 + 토큰 자동 갱신."""

    def __init__(self, config_path=None):
        self._path = config_path or _CONFIG_PATH
        self._cfg = self._load()
        self.base_url = self._cfg.get("base_url", _DEFAULT_BASE_URL).rstrip("/")
        self.guild = self._cfg.get("guild") or _DEFAULT_GUILD
        self._token = self._cfg.get("token")

    # ── 설정 파일 ──────────────────────────────────────────
    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("[API] 설정 로드 실패 (%s): %s", self._path.name, e)
        else:
            logger.warning("[API] 설정 파일 없음: %s", self._path)
        return {}

    def _save_token(self, token: str) -> None:
        self._token = token
        self._cfg["token"] = token
        try:
            self._path.write_text(
                json.dumps(self._cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[API] 토큰 저장 실패: %s", e)

    def _with_guild(self, path: str) -> str:
        """경로에 ?guild= 를 붙인다 (인증 경로·이미 붙은 경우는 그대로)."""
        if path.startswith(_NO_GUILD_PREFIXES) or "guild=" in path:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}guild={urllib.parse.quote(str(self.guild))}"

    # ── HTTP ──────────────────────────────────────────────
    def _request(self, method: str, path: str, body=None, with_auth=True,
                 raw_data: bytes | None = None, content_type: str | None = None,
                 extra_headers: dict | None = None, timeout: int | None = None):
        """JSON 요청이 기본. raw_data 를 주면 그 바이트를 그대로 보낸다(멀티파트용)."""
        url = self.base_url + self._with_guild(path)
        if raw_data is not None:
            data = raw_data
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            if data is not None:
                content_type = content_type or "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if data is not None and content_type:
            req.add_header("Content-Type", content_type)
        if with_auth and self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        for key, value in (extra_headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None

    # ── 인증 ──────────────────────────────────────────────
    def login(self) -> str:
        """nickname/password 로 토큰 발급 후 저장. 성공 시 토큰 반환."""
        nickname = self._cfg.get("nickname")
        password = self._cfg.get("password")
        if not nickname or not password:
            raise ApiError("로그인 자격증명(nickname/password)이 api_config.json 에 없음")
        result = self._request(
            "POST", _LOGIN_PATH,
            body={"nickname": nickname, "password": password},
            with_auth=False,
        )
        # 응답의 토큰 필드명이 서버마다 다를 수 있어 방어적으로 탐색
        token = None
        if isinstance(result, dict):
            for key in ("token", "accessToken", "access_token", "jwt"):
                if result.get(key):
                    token = result[key]
                    break
        if not token:
            keys = list(result.keys()) if isinstance(result, dict) else type(result).__name__
            raise ApiError(f"로그인 응답에서 토큰을 찾지 못함 (응답 키: {keys})")
        self._save_token(token)
        logger.info("[API] 로그인 성공 — 토큰 갱신됨")
        return token

    def get(self, key: str, default=None):
        """api_config.json 에 들어 있는 임의 설정값 조회 (예: ocr_test_key)."""
        return self._cfg.get(key, default)

    def post_multipart(self, path: str, fields: dict[str, str],
                       files: list[tuple[str, str, bytes, str]],
                       with_auth: bool = True,
                       extra_headers: dict | None = None):
        """multipart/form-data POST.

        files: (필드명, 파일명, 바이트, mime) 리스트 — 같은 필드명을 여러 번 넣으면
        서버에서 배열(images[])로 받는다.
        재시도 규칙은 get_json 과 동일하되, 큰 본문을 재전송하므로 5xx 만 재시도한다.
        """
        body, content_type = _multipart_body(fields, files)
        relogged = False
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._request(
                    "POST", path, with_auth=with_auth, raw_data=body,
                    content_type=content_type, extra_headers=extra_headers,
                    timeout=_UPLOAD_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code == 401 and with_auth and not relogged:
                    logger.info("[API] 401 Unauthorized — 재로그인 시도")
                    relogged = True
                    self.login()
                    continue
                detail = _http_detail(e)
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    logger.warning("[API] %s %s — %.1fs 후 재시도",
                                   path, detail, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                if attempt < _MAX_RETRIES:
                    logger.warning("[API] %s 업로드 실패 (%s) — %.1fs 후 재시도",
                                   path, reason, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: 연결 실패 ({reason})") from e

    def post_json(self, path: str, body: dict | None = None):
        """POST 요청 (JSON). 재시도·재로그인 규칙은 get_json 과 동일."""
        relogged = False
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._request("POST", path, body=body if body is not None else {})
            except urllib.error.HTTPError as e:
                if e.code == 401 and not relogged:
                    logger.info("[API] 401 Unauthorized — 재로그인 시도")
                    relogged = True
                    self.login()
                    continue
                detail = _http_detail(e)
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    logger.warning("[API] %s %s — %.1fs 후 재시도",
                                   path, detail, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                if attempt < _MAX_RETRIES:
                    logger.warning("[API] %s 연결 실패 (%s) — %.1fs 후 재시도",
                                   path, reason, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: 연결 실패 ({reason})") from e

    def get_json(self, path: str):
        """GET 요청.

        - 401 → 재로그인 후 1회 재시도
        - 5xx / 네트워크 오류 → _MAX_RETRIES 회 백오프 재시도
        - 최종 실패는 ApiError 로 감싼다 (서버 응답 본문 포함)
        """
        relogged = False
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._request("GET", path)
            except urllib.error.HTTPError as e:
                if e.code == 401 and not relogged:
                    logger.info("[API] 401 Unauthorized — 재로그인 시도")
                    relogged = True
                    self.login()
                    continue
                detail = _http_detail(e)
                if e.code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                    logger.warning("[API] %s %s — %.1fs 후 재시도",
                                   path, detail, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                if attempt < _MAX_RETRIES:
                    logger.warning("[API] %s 연결 실패 (%s) — %.1fs 후 재시도",
                                   path, reason, _RETRY_BACKOFF_SEC)
                    time.sleep(_RETRY_BACKOFF_SEC)
                    continue
                raise ApiError(f"{path}: 연결 실패 ({reason})") from e
