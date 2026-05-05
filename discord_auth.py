"""Discord OAuth2 기반 실행 인증.

흐름:
  1. 캐시된 토큰 + 최근 24시간 이내 검증 이력 있으면 바로 통과
  2. 토큰 만료됐으면 refresh_token 으로 갱신
  3. 캐시 없음/refresh 실패 → 브라우저 OAuth2 플로우 새로 진행
  4. access_token 으로 /users/@me/guilds 조회 → 지정 GUILD_ID 포함 여부 확인

파일:
  - discord_secret.txt (bundled) : Client Secret
  - auth.json (DATA_DIR)         : access_token, refresh_token, 검증 시각 캐시
"""

import base64
import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from paths import BUNDLED_DIR, DATA_DIR

logger = logging.getLogger(__name__)

# ─── 설정 (Client Secret 만 외부 파일) ───
CLIENT_ID = "1495429106274144387"
GUILD_ID = "1490302925791301675"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPES = "identify guilds"
AUTH_CACHE_HOURS = 24  # 이 기간 내엔 네트워크 확인 생략

_SECRET_FILE = BUNDLED_DIR / "discord_secret.txt"
_AUTH_CACHE_FILE = DATA_DIR / "auth.json"
_BROWSER_TIMEOUT_SEC = 120

_TOKEN_URL = "https://discord.com/api/oauth2/token"
_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
_GUILDS_URL = "https://discord.com/api/users/@me/guilds"


def _load_secret() -> str:
    if not _SECRET_FILE.exists():
        raise FileNotFoundError(
            f"Discord Client Secret 파일 없음: {_SECRET_FILE.name}")
    secret = _SECRET_FILE.read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError(f"{_SECRET_FILE.name} 가 비어있음")
    return secret


def _basic_auth_header() -> str:
    creds = f"{CLIENT_ID}:{_load_secret()}".encode()
    return "Basic " + base64.b64encode(creds).decode()


# ─── 로컬 HTTP 콜백 서버: code 캡처 ───────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    captured_code: str | None = None
    captured_state: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        _CallbackHandler.captured_code = qs.get("code", [None])[0]
        _CallbackHandler.captured_state = qs.get("state", [None])[0]
        body = (
            "<html><body style='font-family:sans-serif;padding:40px;text-align:center;'>"
            "<h1>Odin Boss Timer</h1>"
            "<p>인증이 완료되었습니다. 이 창을 닫으셔도 됩니다.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass  # HTTP 서버 로그 억제


def _capture_auth_code(expected_state: str) -> str:
    _CallbackHandler.captured_code = None
    _CallbackHandler.captured_state = None
    try:
        server = HTTPServer(("127.0.0.1", REDIRECT_PORT), _CallbackHandler)
    except OSError as e:
        logger.error("[Discord] 로컬 콜백 서버 시작 실패 (포트 %d 사용 중?): %s",
                     REDIRECT_PORT, e)
        raise
    logger.info("[Discord] 콜백 서버 listen 중 (port=%d, 최대 %ds 대기)",
                REDIRECT_PORT, _BROWSER_TIMEOUT_SEC)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.time() + _BROWSER_TIMEOUT_SEC
    try:
        while time.time() < deadline:
            if _CallbackHandler.captured_code is not None:
                break
            time.sleep(0.3)
    finally:
        server.shutdown()
        server.server_close()
    code = _CallbackHandler.captured_code
    state = _CallbackHandler.captured_state
    logger.info("[Discord] 콜백 수신: code=%s, state=%s (expected=%s)",
                "있음" if code else "없음",
                state[:8] + "..." if state else "없음",
                expected_state[:8] + "...")
    if code is None:
        raise TimeoutError(f"OAuth2 인증 {_BROWSER_TIMEOUT_SEC}s 초과")
    if state != expected_state:
        raise RuntimeError("OAuth2 state 불일치 — CSRF 의심")
    return code


# ─── HTTP 호출 ───────────────────────

def _post_token(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Authorization", _basic_auth_header())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _to_cache_record(payload: dict) -> dict:
    expires_at = datetime.now() + timedelta(seconds=int(payload.get("expires_in", 604800)))
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "expires_at": expires_at.isoformat(),
        "last_verified_at": datetime.now().isoformat(),
    }


def _browser_auth_flow() -> dict:
    """브라우저로 Discord OAuth2 authorization_code 플로우 진행."""
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": SCOPES,
        "state": state,
        "redirect_uri": REDIRECT_URI,
        "prompt": "none",
    }
    auth_url = f"{_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    logger.info("[Discord] 브라우저에 인증 URL 열기: %s", auth_url)
    webbrowser.open(auth_url)
    code = _capture_auth_code(state)

    payload = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })
    return _to_cache_record(payload)


def _refresh(refresh_token: str) -> dict:
    payload = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    record = _to_cache_record(payload)
    # Discord 가 refresh_token 새로 안 내릴 수도 있음 — 기존 값 유지
    if record["refresh_token"] is None:
        record["refresh_token"] = refresh_token
    return record


def _check_guild_membership(access_token: str) -> bool:
    req = urllib.request.Request(
        _GUILDS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        guilds = json.loads(resp.read())
    # 디버그: 받아온 길드 목록 전체 로깅
    logger.info("[Discord] 조회된 길드 %d개 (대상 GUILD_ID=%s):",
                len(guilds), GUILD_ID)
    for g in guilds:
        gid = str(g.get("id"))
        name = g.get("name", "?")
        match = "★" if gid == GUILD_ID else " "
        logger.info("  %s  id=%s  name=%s", match, gid, name)
    return any(str(g.get("id")) == GUILD_ID for g in guilds)


# ─── 캐시 ───────────────────────

def _load_cache() -> dict | None:
    if not _AUTH_CACHE_FILE.exists():
        return None
    try:
        return json.loads(_AUTH_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[Discord] 캐시 읽기 실패: %s", e)
        return None


def _save_cache(record: dict) -> None:
    try:
        _AUTH_CACHE_FILE.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[Discord] 캐시 저장 실패: %s", e)


# ─── 외부 API ───────────────────────

def authenticate() -> tuple[bool, str]:
    """Discord 인증 + 길드 멤버십 확인. 반환: (성공여부, 메시지)."""
    now = datetime.now()
    cache = _load_cache()

    # 1) 캐시된 확인 이력이 있고 24시간 이내 → 네트워크 생략
    if cache:
        try:
            last = datetime.fromisoformat(cache.get("last_verified_at", ""))
            if now - last < timedelta(hours=AUTH_CACHE_HOURS):
                logger.info("[Discord] 최근 %dh 이내 확인됨 — 캐시 사용", AUTH_CACHE_HOURS)
                return True, "cache-hit"
        except Exception:
            pass

    # 2) access_token 확보 — 만료 안 됐으면 그대로, 아니면 refresh
    access_token: str | None = None
    if cache:
        try:
            expires_at = datetime.fromisoformat(cache.get("expires_at", ""))
            if now < expires_at and cache.get("access_token"):
                access_token = cache["access_token"]
            elif cache.get("refresh_token"):
                logger.info("[Discord] 토큰 만료 → refresh 시도")
                cache = _refresh(cache["refresh_token"])
                access_token = cache["access_token"]
        except Exception as e:
            logger.warning("[Discord] 캐시 활용 실패: %s", e)
            cache = None

    # 3) 토큰 없음/획득 실패 → 브라우저 플로우 새로
    if access_token is None:
        try:
            cache = _browser_auth_flow()
            access_token = cache["access_token"]
        except Exception as e:
            logger.exception("[Discord] 브라우저 플로우 실패")
            return False, f"Discord 로그인 실패: {e}"

    # 4) 길드 멤버십 확인
    try:
        is_member = _check_guild_membership(access_token)
    except Exception as e:
        return False, f"Discord API 조회 실패: {e}"

    if not is_member:
        # 로그 파일에도 남겨서 배포 exe 환경에서도 디버그 가능
        try:
            debug_file = DATA_DIR / "discord_auth_debug.txt"
            req = urllib.request.Request(
                _GUILDS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
            debug_file.write_text(
                f"대상 GUILD_ID: {GUILD_ID}\n"
                f"시각: {now.isoformat()}\n"
                f"Discord API 응답:\n{raw}\n",
                encoding="utf-8",
            )
            logger.warning("[Discord] 실패 상세: %s", debug_file)
        except Exception:
            pass
        return False, "지정된 Discord 서버에 참여되어 있지 않습니다."

    # 5) 성공 — 확인 시각 갱신 + 캐시 저장
    cache["last_verified_at"] = now.isoformat()
    _save_cache(cache)
    logger.info("[Discord] 길드 멤버 확인 완료 (guild_id=%s)", GUILD_ID)
    return True, "ok"
