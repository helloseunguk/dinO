"""서버 OCR 동기화 — 게임 시간표 화면을 탭별로 캡처해 woogi.online 에 올린다.

플로우:
  1. '노른의 시간표' 아이콘 클릭 → 시간표 열림
  2. 보스 탭을 순서대로: 탭 클릭 → 캡처 → 다음 탭
  3. 캡처한 이미지를 한 번에 multipart 로 /api/bosses/ocr 전송
     → {entries, unmatched}, 각 entry 의 imageIndex 로 어느 탭에서 나왔는지 알 수 있다

로컬 OCR(screen_sync)과 달리 보스 카드 인식과 이름/시간 파싱은 서버가 한다.
좌표는 모두 BASE(1920x1009) 기준이며 좌표 수집 도구(macro_points)로 찍은 값이다.

단독 실행 (테스트 엔드포인트, 인식 결과 + debug 출력):
    python -m odin.sync.ocr_sync --test
    .venv\\Scripts\\python -m odin.sync.ocr_sync --test      # 의존성은 venv 에만 있음
"""

import ctypes
import io
import json
import logging
import sys
import time
from datetime import datetime, timedelta

import mss
from PIL import Image

from odin.sync.api_client import ApiClient
from odin.game.game_coords import ensure_game_foreground
from odin.game.game_coords import click_base, find_game_window, scale_xy, ui
from odin.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

# ─── 좌표 (BASE 1920x1009) ─────────────────────────────────
SCHEDULE_ICON = ui(30, 348, "TL")   # 노른의 시간표 아이콘 (왼쪽 아이콘열 — 위쪽 기준)
SCHEDULE_CLOSE = ui(1596, 247, "C")  # 시간표 닫기 X (screen_sync 와 동일 위치)
CAPTURE_START = ui(300, 230, "C")   # 캡처 영역 좌상단
CAPTURE_END = ui(1615, 782, "C")    # 캡처 영역 우하단

# 보스 탭 — 선언 순서대로 순회한다 (클릭 → 캡처 → 다음)
TAB_COORDS: dict[str, tuple[int, int]] = {
    "요툰": (720, 311),
    "니다": (844, 312),
    "알브": (939, 313),
    "무스펠": (1057, 310),
    "아스": (1171, 315),
    "니플": (1275, 313),
    "바나": (1399, 314),
    "던전": (1487, 314),
}
# 시간표 창은 화면 가운데 기준
TAB_COORDS = {name: ui(*xy, "C") for name, xy in TAB_COORDS.items()}

# 게임 창을 앞으로 가져온 뒤 입력이 먹을 때까지 대기.
# 이 단계를 건너뛰면 터미널에 포커스가 있을 때 첫 클릭이 창 활성화로 소비돼
# 아이콘이 눌리지 않는다 (screen_sync.open_schedule 과 같은 이유).
FOCUS_WAIT_SEC = 0.5
# 아이콘 클릭 후 시간표가 다 그려질 때까지 대기
OPEN_WAIT_SEC = 1.5
# 탭 클릭 후 목록이 갈릴 때까지 대기 (screen_sync 의 tab_delay 와 같은 값)
TAB_WAIT_SEC = 0.3
# 닫기 클릭 후 화면이 정리될 때까지 대기
CLOSE_WAIT_SEC = 0.3

# ─── API ───────────────────────────────────────────────────
OCR_PATH = "/api/bosses/ocr"
OCR_TEST_PATH = "/api/bosses/ocr/test"
_TEST_KEY_HEADER = "X-Ocr-Test-Key"
# ?guild= 는 ApiClient 가 모든 요청에 자동으로 붙인다 (api_client._with_guild)

# mode — 서버가 해석하는 시간표 종류. 인식·등록 요청에 함께 보낸다.
MODE_NORMAL = 0        # 본토
MODE_INVASION = 1      # 침공전
MODE_ALT_INVASION = 2  # 부캐침공
MODE_LABELS: dict[int, str] = {
    MODE_NORMAL: "본토",
    MODE_INVASION: "침공전",
    MODE_ALT_INVASION: "부캐침공",
}

# 마지막으로 고른 mode 를 기억한다 — 침공전만 계속 돌리는 날이 있어서,
# 창을 열 때마다 본토로 되돌아가면 잘못된 모드로 등록하기 쉽다.
_SETTINGS_PATH = DATA_DIR / "ocr_settings.json"


def load_mode() -> int:
    """저장된 mode. 없거나 이상하면 본토(0)."""
    if not _SETTINGS_PATH.exists():
        return MODE_NORMAL
    try:
        value = int(json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
                    .get("mode", MODE_NORMAL))
        if value in MODE_LABELS:
            return value
        logger.warning("[서버OCR] 저장된 mode %s 가 범위 밖 — 본토로", value)
    except Exception as e:
        logger.warning("[서버OCR] mode 설정 로드 실패: %s", e)
    return MODE_NORMAL


def save_mode(mode: int) -> None:
    """선택한 mode 저장. 다음 실행에도 유지된다."""
    if mode not in MODE_LABELS:
        logger.warning("[서버OCR] 저장 거부 — 알 수 없는 mode %s", mode)
        return
    try:
        _SETTINGS_PATH.write_text(
            json.dumps({"mode": mode}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        logger.info("[서버OCR] mode = %d (%s) 저장", mode, MODE_LABELS[mode])
    except Exception as e:
        logger.warning("[서버OCR] mode 설정 저장 실패: %s", e)

# 서버가 받아주는 파일당 최대 크기 (8MiB). 넘으면 accepted=false 로 되돌아온다.
MAX_FILE_BYTES = 8 * 1024 * 1024

# 캡처 원본을 남겨 두는 곳 — 인식이 어긋날 때 무엇을 보냈는지 확인용
DEBUG_DIR = DATA_DIR / "ocr_debug"
# 최근 몇 회 동기화분만 남긴다 (회당 탭 8장 × 약 0.7MB — 그대로 두면 한 달에 수백 MB)
DEBUG_KEEP_ROUNDS = 3


def _prune_debug(keep_files: int) -> None:
    """시간표 캡처를 최신 keep_files 장만 남기고 오래된 것부터 지운다."""
    files = sorted(DEBUG_DIR.glob("schedule_*.png"), key=lambda p: p.stat().st_mtime)
    for f in files[:max(0, len(files) - keep_files)]:
        try:
            f.unlink()
        except OSError as e:
            logger.warning("[서버OCR] 오래된 캡처 삭제 실패 (%s): %s", f.name, e)


def focus_game(hwnd: int) -> bool:
    """게임 창을 앞으로. 활성화에 실패하면 클릭이 엉뚱한 창으로 가므로 경고한다."""
    user32 = ctypes.windll.user32
    ensure_game_foreground()
    time.sleep(FOCUS_WAIT_SEC)
    active = user32.GetForegroundWindow()
    if active != hwnd:
        logger.warning("[서버OCR] 게임 창 활성화 실패 (foreground=%s, 게임=%s) — "
                       "클릭이 들어가지 않을 수 있습니다", active, hwnd)
        return False
    return True


def _grab_png(sct, bx: int, by: int, cw: int, ch: int) -> bytes:
    """캡처 영역(BASE 좌표)을 실제 좌표로 환산해 잡고 PNG 바이트로."""
    x1, y1 = scale_xy(CAPTURE_START, cw, ch)
    x2, y2 = scale_xy(CAPTURE_END, cw, ch)
    shot = sct.grab({"left": bx + x1, "top": by + y1,
                     "width": x2 - x1, "height": y2 - y1})
    img = Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def capture_tabs(tabs: list[str] | None = None, save_debug: bool = True,
                 close_after: bool = True) -> list[tuple[str, bytes]]:
    """시간표를 열고 탭을 순서대로 돌며 캡처. [(탭 이름, PNG 바이트), ...] 반환.

    게임 창을 조작하므로 game_queue 워커에서 호출할 것 (자동화와 경쟁 방지).
    close_after=True 면 마지막에 시간표를 닫는다 — 열린 채로 두면 이후 자동화가 막힌다.
    """
    names = tabs if tabs is not None else list(TAB_COORDS)
    hwnd, bx, by, cw, ch = find_game_window()
    logger.info("[서버OCR] 시간표 열기 — 클라이언트 %dx%d, 탭 %d개", cw, ch, len(names))
    focus_game(hwnd)
    click_base(SCHEDULE_ICON, bx, by, cw, ch)
    time.sleep(OPEN_WAIT_SEC)

    stamp = datetime.now().strftime("%H%M%S")
    shots: list[tuple[str, bytes]] = []
    with mss.mss() as sct:
        for name in names:
            coord = TAB_COORDS.get(name)
            if coord is None:
                logger.warning("[서버OCR] 알 수 없는 탭 '%s' — 건너뜀", name)
                continue
            click_base(coord, bx, by, cw, ch)
            time.sleep(TAB_WAIT_SEC)
            blob = _grab_png(sct, bx, by, cw, ch)
            if len(blob) > MAX_FILE_BYTES:
                logger.warning("[서버OCR] %s 탭 캡처가 서버 한도(%d)를 넘어 제외: %d 바이트",
                               name, MAX_FILE_BYTES, len(blob))
                continue
            shots.append((name, blob))
            logger.info("[서버OCR] %s 탭 캡처 (%.2f MB)", name, len(blob) / 1024 / 1024)

            if save_debug:
                try:
                    DEBUG_DIR.mkdir(exist_ok=True)
                    (DEBUG_DIR / f"schedule_{stamp}_{name}.png").write_bytes(blob)
                except Exception as e:
                    logger.warning("[서버OCR] 캡처 저장 실패 (%s): %s", name, e)

    if save_debug:
        _prune_debug(DEBUG_KEEP_ROUNDS * len(names))

    if close_after:
        click_base(SCHEDULE_CLOSE, bx, by, cw, ch)
        time.sleep(CLOSE_WAIT_SEC)

    total = sum(len(b) for _, b in shots)
    logger.info("[서버OCR] 캡처 완료 — %d장, 합계 %.2f MB",
                len(shots), total / 1024 / 1024)
    return shots


def send_images(images: list[bytes], mode: int = MODE_NORMAL,
                test: bool = False, client: ApiClient | None = None) -> dict:
    """캡처 이미지를 한 번의 요청으로 OCR 엔드포인트에 전송하고 응답(dict) 반환."""
    if not images:
        raise RuntimeError("보낼 캡처 이미지가 없습니다")
    client = client or ApiClient()
    files = [("images", f"schedule{i}.png", blob, "image/png")
             for i, blob in enumerate(images)]

    if test:
        key = client.get("ocr_test_key")
        if not key:
            raise RuntimeError(
                "api_config.json 에 ocr_test_key 가 없습니다 (테스트 엔드포인트용)")
        return client.post_multipart(
            OCR_TEST_PATH, {"mode": str(mode)}, files,
            with_auth=False, extra_headers={_TEST_KEY_HEADER: key}) or {}
    return client.post_multipart(OCR_PATH, {"mode": str(mode)}, files) or {}


def summarize(result: dict, tab_names: list[str] | None = None) -> dict:
    """응답에서 결과/경고를 로그로 남기고 {entries, unmatched} 반환."""
    for f in result.get("files", []) or []:
        if f.get("accepted") is False:
            logger.warning("[서버OCR] 파일 거부: %s (%s bytes) — %s",
                           f.get("name"), f.get("bytes"), f.get("reason"))
    if result.get("ok") is False:
        logger.warning("[서버OCR] 서버가 인식까지 가지 못했습니다 (ok=false)")
    if result.get("tesseractReady") is False:
        logger.warning("[서버OCR] 서버에 한국어 OCR 데이터가 없습니다 (tesseractReady=false)")

    # 테스트 엔드포인트는 실제 결과(result) 를 한 겹 더 감싸서 준다
    payload = result.get("result") if isinstance(result.get("result"), dict) \
        else result
    entries = payload.get("entries") or []
    unmatched = payload.get("unmatched") or []
    matched = sum(1 for e in entries if e.get("bossId") is not None)
    logger.info("[서버OCR] 인식 %d개 · 자동 매칭 %d개 · 미매칭 %d개%s",
                len(entries), matched, len(unmatched),
                f" ({', '.join(map(str, unmatched))})" if unmatched else "")

    for dbg in result.get("debug", []) or []:
        idx = dbg.get("imageIndex")
        tab = (tab_names[idx] if tab_names and isinstance(idx, int)
               and idx < len(tab_names) else f"#{idx}")
        logger.info("[서버OCR] %s 탭: 카드 %s개, 기준시각 %s, 시계후보 %s",
                    tab, dbg.get("cards"), dbg.get("ref"),
                    dbg.get("clockCandidates"))
        if not dbg.get("cards"):
            logger.warning("[서버OCR] %s 탭 카드 0개 — 캡처 범위가 시간표를 "
                           "못 담았을 수 있습니다", tab)
    return {"entries": entries, "unmatched": unmatched}


# ─── 적용 (서버에 반영) ────────────────────────────────────
# /api/bosses/ocr 는 인식만 하고 아무것도 저장하지 않는다.
# 실제 저장은 전용 엔드포인트가 맡는다 — 잡은 시각 계산도 서버가 한다.
#   POST /api/bosses/ocr/apply?guild=<길드>
#        {mode, entries: [{bossId, days, hours, minutes, seconds, ref, live}]}
#   → {applied, live, skipped, failed, results: [{bossId, bossName, status,
#                                                 reason, spawnAt, killedAt}]}
# 관리자 토큰이 필요하므로 Bearer 인증으로 보낸다 (api_client 가 401 시 재로그인).
APPLY_PATH = "/api/bosses/ocr/apply"


def build_apply_payload(entries: list[dict]) -> list[dict]:
    """인식 결과 → apply 엔드포인트가 받는 entries.

    bossId 가 없는(=서버 보스와 매칭 안 된) 항목은 보낼 수 없어 제외한다.
    출현 중인 보스는 시간 없이 live 만 보낸다.
    mode 는 요청 최상위에만 넣는다 — 줄마다 넣으면 서버가 400 으로 거부한다
    ("entries.N.property mode should not exist").
    """
    payload = []
    for entry in entries:
        boss_id = entry.get("bossId")
        if boss_id is None:
            continue
        row = {"bossId": boss_id}
        if entry.get("live"):
            row["live"] = True
            payload.append(row)
            continue
        row.update({
            "days": entry.get("days") or 0,
            "hours": entry.get("hours") or 0,
            "minutes": entry.get("minutes") or 0,
            "seconds": entry.get("seconds") or 0,
            "ref": entry.get("ref"),
            "live": False,
        })
        payload.append(row)
    return payload


class RecognitionError(RuntimeError):
    """인식 결과에 문제가 있어 반영하면 안 되는 상태."""


def unmatched_names(entries: list[dict], unmatched: list | None = None) -> list[str]:
    """서버 보스와 매칭되지 않은 줄의 이름 목록."""
    names = [str(e.get("name") or e.get("bossName") or "?")
             for e in entries if e.get("bossId") is None]
    for name in unmatched or []:
        if str(name) not in names:
            names.append(str(name))
    return names


def ensure_recognition_ok(entries: list[dict], unmatched: list | None = None) -> None:
    """반영 전 안전장치.

    매칭 안 된 줄은 그냥 건너뛴다 — 모드마다 존재하는 보스가 달라서
    (침공전에는 던전 보스가 없다) 정상적으로도 매칭이 안 되는 줄이 생긴다.
    다만 매칭된 줄이 하나도 없으면 인식 자체가 실패한 것이므로 중단한다.
    """
    if not entries:
        raise RecognitionError("인식된 보스가 없습니다")
    ignored = unmatched_names(entries, unmatched)
    if len(ignored) == len(entries):
        shown = ", ".join(ignored[:5]) + (" 외" if len(ignored) > 5 else "")
        raise RecognitionError(
            f"서버 보스와 매칭된 줄이 하나도 없습니다 ({shown})")
    if ignored:
        shown = ", ".join(ignored[:5]) + (" 외" if len(ignored) > 5 else "")
        logger.info("[서버OCR] 매칭 안 된 %d건은 건너뜀 (%s)", len(ignored), shown)


def apply_entries(entries: list[dict], mode: int = MODE_NORMAL,
                  client: ApiClient | None = None) -> dict:
    """인식 결과를 서버에 반영하고 응답(dict)을 그대로 반환."""
    payload = build_apply_payload(entries)
    if not payload:
        raise RuntimeError("반영할 항목이 없습니다 (서버 보스와 매칭된 결과 없음)")
    client = client or ApiClient()
    result = client.post_json(APPLY_PATH,
                              {"mode": mode, "entries": payload}) or {}

    logger.info("[서버OCR] 반영 %s개 · 출현중 %s개 · 건너뜀 %s개 · 실패 %s개",
                result.get("applied"), result.get("live"),
                result.get("skipped"), result.get("failed"))
    for row in result.get("results") or []:
        status = row.get("status")
        if status in ("applied", "live"):
            logger.info("[서버OCR] %s: %s (출현 %s)", row.get("bossName"),
                        status, row.get("spawnAt"))
        else:
            logger.warning("[서버OCR] %s: %s — %s", row.get("bossName"),
                           status, row.get("reason"))
    return result


def entry_remaining(entry: dict) -> timedelta:
    """entry 의 남은 시간 (days/hours/minutes/seconds)."""
    return timedelta(days=entry.get("days") or 0,
                     hours=entry.get("hours") or 0,
                     minutes=entry.get("minutes") or 0,
                     seconds=entry.get("seconds") or 0)


def run(mode: int = MODE_NORMAL, test: bool = False,
        tabs: list[str] | None = None, close_after: bool = True,
        client: ApiClient | None = None) -> dict:
    """전체 플로우 — 시간표 열기 → 탭별 캡처 → 전송. 서버 응답 dict 반환."""
    shots = capture_tabs(tabs=tabs, close_after=close_after)
    result = send_images([blob for _, blob in shots], mode=mode, test=test,
                         client=client)
    summarize(result, [name for name, _ in shots])
    return result


def run_and_apply(mode: int = MODE_NORMAL, test: bool = False,
                  tabs: list[str] | None = None, close_after: bool = True,
                  client: ApiClient | None = None) -> tuple[dict, dict]:
    """캡처 → 인식 → 반영까지 한 번에. (인식 응답, 반영 응답) 반환.

    매칭 안 된 줄이 하나라도 있으면 RecognitionError 로 중단하고 반영하지 않는다.
    """
    client = client or ApiClient()
    shots = capture_tabs(tabs=tabs, close_after=close_after)
    ocr_result = send_images([blob for _, blob in shots], mode=mode, test=test,
                             client=client)
    summary = summarize(ocr_result, [name for name, _ in shots])
    ensure_recognition_ok(summary["entries"], summary["unmatched"])
    return ocr_result, apply_entries(summary["entries"], mode=mode, client=client)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    test = "--test" in sys.argv
    mode = load_mode()      # --mode 를 주지 않으면 마지막에 고른 값
    tabs = None
    for arg in sys.argv[1:]:
        if arg.startswith("--mode="):
            mode = int(arg.split("=", 1)[1])
        elif arg.startswith("--tabs="):
            tabs = arg.split("=", 1)[1].split(",")
    logger.info("[서버OCR] mode = %d (%s)", mode,
                MODE_LABELS.get(mode, "?"))
    # --apply 면 캡처→인식→반영을 한 번에 (인식 실패가 있으면 반영 전에 중단)
    applied = None
    if "--apply" in sys.argv:
        try:
            result, applied = run_and_apply(mode=mode, test=test, tabs=tabs)
        except RecognitionError as e:
            print(f"\n반영 중단 — {e}")
            return
    else:
        result = run(mode=mode, test=test, tabs=tabs)

    payload = result.get("result") if isinstance(result.get("result"), dict) \
        else result
    entries = payload.get("entries", []) or []
    for entry in entries:
        rem = "출현 중" if entry.get("live") else str(entry_remaining(entry))
        print(f"  [사진 {entry.get('imageIndex')}] "
              f"{entry.get('bossName') or entry.get('name', ''):<24} "
              f"{rem}  bossId={entry.get('bossId')}")

    if applied is not None:
        print(f"\n반영 {applied.get('applied')}개 · 출현중 {applied.get('live')}개 "
              f"· 건너뜀 {applied.get('skipped')}개 · 실패 {applied.get('failed')}개")
        for row in applied.get("results") or []:
            mark = {"applied": "✔", "live": "●"}.get(row.get("status"), "–")
            detail = row.get("spawnAt") or row.get("reason") or ""
            print(f"  {mark} {row.get('bossName')}: {row.get('status')} {detail}")
    if test:
        for dbg in result.get("debug", []) or []:
            print(f"\n[debug #{dbg.get('imageIndex')}] 원문 이름: {dbg.get('rawNames')}")
            print(f"[debug #{dbg.get('imageIndex')}] 원문 시간: {dbg.get('rawTimes')}")


if __name__ == "__main__":
    main()
