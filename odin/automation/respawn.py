"""사망 화면 감지 · 마을에서 부활 · 경험치 복구 — 월보와 필드 보스가 같이 쓴다.

근거 자료: recordings/20261018_195849 (월보 이둔에서 사망).
사망하면 화면 아래 '부활 대기 시간 …N초' 가 뜨고, 다 지나면 같은 자리에 '마을에서 부활'
버튼이 나온다. 필드 보스에서 죽었을 때도 같은 화면이라고 보고 같은 좌표를 쓴다.
부활하면 항상 바로 경험치 복구까지 한다 (사용자 요청 10/30). 무료가 다 떨어지면 골드로도
복구한다 (11/07). 사망을 확인하면 그 화면을 death_debug/ 에 남긴다 (11/07).
"""

import ctypes
import logging
import re
import time
from datetime import datetime

import cv2
import mss
import numpy as np

from odin.core.paths import DATA_DIR
from odin.automation.automator import (
    _cancel_event, _check_cancel, _hud_shown, _match_region, _press_key, _sleep_cancellable,
    _wait_for_loading_complete)
from odin.game.game_coords import (
    center_to_abs_region, click_at_screen, click_base, ensure_game_foreground, find_game_window,
    grab_base, ui)
from odin.game.ocr_engine import ocr_text

logger = logging.getLogger(__name__)

RESPAWN_CENTER = (1076, 897)       # '마을에서 부활' 버튼. 부활 대기가 끝나야 나온다
# 글자를 잘라 읽지 않게 넉넉히. 280×50 에선 버튼이 '서 부활' 로만 읽혀(녹화 223s) "마을" 을
# 찾던 판정이 사망을 놓쳤다 — 11/05 04:12 부활을 건너뛰고 7시간 동안 보스 이동이 실패.
RESPAWN_W, RESPAWN_H = 400, 80
# 버튼이 뜨기 전 같은 자리의 '부활 대기 시간 …2초' — OCR 은 '시간…1초' 로 읽는다
_RESPAWN_WAIT_RE = re.compile(r"시간.*\d\s*초")
RESPAWN_MAX_WAIT_SEC = 30.0

# ── 경험치 복구 ──────────────────────────────────────────
EXP_ICON = "exp_restore.png"       # 위쪽 띠의 묘비 아이콘 — 사망한 적이 있을 때만 있다
EXP_ICON_CENTER = (400, 28)
EXP_ICON_W, EXP_ICON_H = 400, 56
EXP_ICON_MIN = 0.6                 # 있을 때 0.65~1.0, 없을 때 0.46 이하
EXP_TITLE_CENTER = ui(925, 298, "C")    # '경험치 복구'
EXP_FIRST_ROW = (940, 370)
EXP_FREE_BTN = ui(1050, 702, "C")       # '무료 복구 3/3' — 행을 골라야 나온다
EXP_FREE_RE = re.compile(r"무료.*?(\d)\s*/\s*\d")
# 창 오른쪽 위 X. 예전 (1260, 298) 은 X 왼쪽 빈 제목줄이라 눌러도 안 닫혔다
# (녹화 20261018_195849 의 300.2s 프레임에서 X 중심 실측).
EXP_CLOSE = ui(1302, 299, "C")
EXP_OPEN_WAIT_SEC = 6.0
_VK_ESC = 0x1B
# 무료가 남아 있으면 왼쪽이 골드, 오른쪽이 무료 버튼이다 — 무료부터 쓴다.
# 무료가 다 떨어지면 골드 버튼 하나만 가운데에 남는다: '복구 (G) 593,300'. 사용자 요청(11/07)으로
# 이것도 누른다. 다이아가 아니라 골드다 (녹화 20261018_195849 302.6s 실측, 확인 창 없이 바로 복구).
EXP_GOLD_ONLY_BTN = ui(958, 700, "C")
EXP_GOLD_RE = re.compile(r"\d{1,3}(?:[,.]\d{3})+")
# 창 아래 빨간 '손실된 경험치 목록 20개 초과 시 자동삭제 (4/20)' — 복구할 때마다 줄어든다
EXP_COUNT_CENTER = ui(960, 643, "C")
EXP_COUNT_RE = re.compile(r"(\d+)\s*/\s*20")
EXP_MAX_RESTORES = 20              # 목록 상한 — 이보다 많이 누를 일이 없다

# ── 사망 화면 저장 ───────────────────────────────────────
# 왜 죽는지 보려고 사망을 확인한 순간의 화면을 남긴다 (사용자 요청 11/07).
DEATH_DEBUG_DIR = DATA_DIR / "death_debug"
DEATH_DEBUG_KEEP = 60


def _ocr(center, w, h, win, scale=2) -> str:
    _hwnd, bx, by, cw, ch = win
    region = center_to_abs_region(center, w, h, bx, by, cw, ch)
    rgb = grab_base(region, cw, ch)
    big = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return ocr_text(big)


def _respawn_text(win) -> str:
    return _ocr(RESPAWN_CENTER, RESPAWN_W, RESPAWN_H, win)


def is_dead(win=None) -> bool:
    """사망 화면인가 — '마을에서 부활' 버튼이나 '부활 대기 시간 …초' 글자."""
    text = _respawn_text(win or find_game_window())
    return _is_dead_text(text)


def _is_dead_text(text: str) -> bool:
    """'마을에서 부활' 버튼이나 '부활 대기 시간 …초'. OCR 이 앞글자를 자주 틀린다
    ('서 부활', '눈활대기 시간 …2초') — 어느 한 조각만 읽혀도 사망이다."""
    return "마을" in text or "부활" in text or bool(_RESPAWN_WAIT_RE.search(text))


def save_death_capture(win, where: str) -> None:
    """사망을 확인한 순간의 게임 화면을 death_debug/ 에 절반 크기로. 게임이 맨 앞이
    아니면 다른 창이 찍히므로 파일명에 '_covered' 를 붙인다."""
    try:
        hwnd, bx, by, cw, ch = win
        DEATH_DEBUG_DIR.mkdir(exist_ok=True)
        with mss.mss() as sct:
            shot = sct.grab({"left": bx, "top": by, "width": cw, "height": ch})
        img = np.frombuffer(shot.rgb, np.uint8).reshape(shot.height, shot.width, 3)
        small = cv2.resize(img[..., ::-1], None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        covered = "" if ctypes.windll.user32.GetForegroundWindow() == hwnd else "_covered"
        safe = re.sub(r"[^0-9a-zA-Z가-힣]", "_", where)[:24]
        path = DEATH_DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{safe}{covered}.jpg"
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            path.write_bytes(buf.tobytes())   # 한글 경로 — cv2.imwrite 는 못 쓴다
            logger.info("[사망] 화면 저장: %s", path.name)
        for old in sorted(DEATH_DEBUG_DIR.glob("*.jpg"))[:-DEATH_DEBUG_KEEP]:
            old.unlink()
    except Exception as e:
        logger.warning("[사망] 화면 저장 실패: %s", e)


def respawn_in_town(win, where: str = ""):
    """사망 화면 — '부활 대기 시간 …N초' 가 끝나면 그 자리에 '마을에서 부활' 버튼이 뜬다.
    부활한 뒤엔 경험치 복구까지 한다. where 는 사망 화면 저장 파일명에 붙는다.

    녹화에선 버튼이 뜨자마자 눌러서 버튼이 찍힌 프레임이 거의 없다. 그래서 대기
    글자로도 사망을 잡고, 대기 글자가 사라졌는데 버튼을 못 읽었으면 같은 자리를 누른다
    (녹화에서 사람이 한 것도 그 자리 연타였다)."""
    save_death_capture(win, where or "사망")
    _hwnd, bx, by, cw, ch = win
    start = time.time()
    gone_since: float | None = None
    while time.time() - start < RESPAWN_MAX_WAIT_SEC:
        _check_cancel()
        text = _respawn_text(win)
        if "마을" in text or text.strip().endswith("부활"):
            break
        if _RESPAWN_WAIT_RE.search(text):
            gone_since = None
        else:
            gone_since = gone_since or time.time()
            if time.time() - gone_since >= 1.0:
                break
        _sleep_cancellable(0.3)
    click_base(RESPAWN_CENTER, bx, by, cw, ch)
    _wait_for_loading_complete(bx, by, cw, ch)
    _sleep_cancellable(1.5)   # 로딩 글자가 사라지고도 1초쯤 화면이 어둡다
    restore_exp(win)


def _exp_count(win) -> int | None:
    """복구 창의 남은 항목 수 '(4/20)'. 못 읽으면 None."""
    m = EXP_COUNT_RE.search(_ocr(EXP_COUNT_CENTER, 420, 32, win))
    return int(m.group(1)) if m else None


def restore_exp(win):
    """경험치 복구 — 목록이 빌 때까지. 무료(하루 3번)부터 쓰고, 다 떨어지면 골드로."""
    _hwnd, bx, by, cw, ch = win
    present, score, abs_xy = _match_region(
        EXP_ICON, EXP_ICON_CENTER, EXP_ICON_W, EXP_ICON_H, bx, by, cw, ch, EXP_ICON_MIN)
    if not present:
        logger.info("[경험치 복구] 아이콘 없음 (score=%.2f) — 생략", score)
        return
    click_at_screen(*abs_xy)
    if not _exp_window_open(win, EXP_OPEN_WAIT_SEC):
        logger.warning("[경험치 복구] 창이 안 열림 (%.0fs) — 생략", EXP_OPEN_WAIT_SEC)
        return
    free = gold = 0
    for _ in range(EXP_MAX_RESTORES):
        _check_cancel()
        before = _exp_count(win)
        click_base(EXP_FIRST_ROW, bx, by, cw, ch)
        _sleep_cancellable(0.8)
        text = _ocr(EXP_FREE_BTN, 250, 40, win)
        m = EXP_FREE_RE.search(text)
        if m and m.group(1) != "0":
            click_base(EXP_FREE_BTN, bx, by, cw, ch)
            free += 1
            logger.info("[경험치 복구] 무료 복구 (%s)", text)
        else:
            # 무료가 없으면 가운데 골드 버튼 하나 — '복구' 와 금액이 둘 다 읽힐 때만 누른다
            gold_text = _ocr(EXP_GOLD_ONLY_BTN, 300, 44, win)
            cost = EXP_GOLD_RE.search(gold_text)
            # 무료가 남은 두 버튼 화면이면 이 영역에 '무료 복구' 가 같이 읽힌다(녹화 299.2s) —
            # 그때 가운데를 누르면 두 버튼 사이다. 버튼이 하나만 남은 화면에서만 누른다.
            if "복구" not in gold_text or not cost or "무료" in gold_text:
                logger.warning("[경험치 복구] 골드 버튼을 못 읽음 (%r) — 중단", gold_text)
                break
            click_base(EXP_GOLD_ONLY_BTN, bx, by, cw, ch)
            gold += 1
            logger.info("[경험치 복구] 골드 복구 (%s 골드)", cost.group(0))
        _sleep_cancellable(1.2)
        if "경험치" not in _ocr(EXP_TITLE_CENTER, 200, 36, win):
            # 복구할 항목이 다 떨어지면 창이 스스로 닫힌다
            logger.info("[경험치 복구] 무료 %d회 · 골드 %d회 — 항목이 다 떨어져 창이 닫힘",
                        free, gold)
            return
        after = _exp_count(win)
        if before is not None and after is not None and after >= before:
            # 골드 부족 등으로 안 줄었다 — 같은 버튼을 계속 누르지 않는다
            logger.warning("[경험치 복구] 눌렀는데 항목이 안 줄어듦 (%d → %d) — 중단", before, after)
            break
    logger.info("[경험치 복구] 무료 %d회 · 골드 %d회", free, gold)
    _close_exp_window(win)


def _exp_window_open(win, wait_sec: float) -> bool:
    """'경험치 복구' 제목이 wait_sec 안에 읽히는가. 부활 직후엔 창이 늦게 뜬다 —
    10/31 엔 1.5초에 한 번만 보고 '안 열림' 으로 넘어가 열린 창을 그대로 뒀다."""
    start = time.time()
    while True:
        if "경험치" in _ocr(EXP_TITLE_CENTER, 200, 36, win):
            return True
        if time.time() - start >= wait_sec:
            return False
        _sleep_cancellable(0.5)


def _close_exp_window(win):
    """경험치 복구 창 닫기 — X 를 누르고, 그래도 열려 있으면 Esc."""
    _hwnd, bx, by, cw, ch = win
    for attempt in range(2):
        if "경험치" not in _ocr(EXP_TITLE_CENTER, 200, 36, win):
            return
        if attempt == 0:
            click_base(EXP_CLOSE, bx, by, cw, ch)
        else:
            _press_key(_VK_ESC)
        _sleep_cancellable(0.8)
    if "경험치" in _ocr(EXP_TITLE_CENTER, 200, 36, win):
        logger.warning("[경험치 복구] 창이 안 닫힘")


def run_respawn(where: str = "필드보스"):
    """필드 보스에서 사망 — 공용 큐 워커 스레드에서 동기 실행. where 는 보스 이름."""
    _cancel_event.clear()
    win = find_game_window()
    ensure_game_foreground()
    _sleep_cancellable(0.3)
    _hwnd, bx, by, cw, ch = win
    # 방금 사망을 확인하고 불린 함수다 — 글자를 못 읽어도 HUD 가 없으면 아직 누워 있다
    if not is_dead(win) and not _hud_shown(bx, by, cw, ch):
        logger.info("[부활] 글자는 못 읽었지만 HUD 없음 — 사망으로 보고 부활")
        respawn_in_town(win, f"{where}_글자없음")
        logger.info("[부활] 마을에서 부활 완료")
        return
    if not is_dead(win):
        # 그새 부활했을 수 있다 — 묘비 아이콘이 있을 때만 복구하니 그냥 불러도 된다
        save_death_capture(win, f"{where}_사망화면아님")
        logger.info("[부활] 사망 화면이 아님 — 부활 생략, 경험치 복구만 확인")
        restore_exp(win)
        return
    respawn_in_town(win, where)
    logger.info("[부활] 마을에서 부활 완료")
