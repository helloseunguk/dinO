"""월드보스(대륙 침략자) 순회.

근거 자료: recordings/20261018_195849 (19:58:49 ~ 20:03:54, 20:00 회차).

다섯 마리가 정각에 동시에 뜨고 최대 1시간 산다. 저장해 둔 순간이동 지점
('은총의 순간 이동 목록' 의 '[월보] …' 행)으로 하나씩 옮겨 다니며 처치에 참여한다.

  보스마다:
    1) 왼쪽 위치핀 아이콘 → 순간이동 목록 → '[월보] 이름' 행의 이동 버튼
    2) 로딩이 끝나면 AUTO 켜기(순간이동하면 꺼진다) + 보스 프리셋
    3) '대륙 침략자 처치 보상' 창이 뜨면 처치 — 창 바깥을 눌러 닫고 다음 보스
       사망하면 '마을에서 부활'(+경험치 복구) → 1) 부터 다시. 보스당 MAX_DEATHS 번 죽으면 다음 보스
       (이둔은 포기하지 않는다 — 출현 1시간이 지날 때까지)
       체력바('…분 …초 남음')가 한동안 안 보이면 이미 죽었거나 없는 것 — 다음 보스
  다 돌면 아이들 프리셋

목록의 OCR 은 이름을 조금씩 틀리게 읽는다(가릉 → '가름', 아우둠라 → '아우-들라',
해임달 → '해임단'). 그래서 글자가 같은 행이 아니라 **가장 비슷한 행**을 고른다.
"""

import ctypes
import difflib
import logging
import re
import time
from datetime import datetime

import cv2
import mss
import numpy as np

from odin.automation.automator import (
    _VK_G, _cancel_event, _check_cancel, _press_key,
    _sleep_cancellable, _wait_for_loading_complete, AutomationCancelled)
from odin.automation.respawn import is_dead, respawn_in_town
from odin.automation.valhalla import AUTO_RING_MIN, _auto_ratio, _grab, _press_preset
from odin.core.paths import DATA_DIR
from odin.game.game_coords import ensure_game_foreground
from odin.game.game_coords import (
    center_to_abs_region, click_base, find_game_window, grab_base, ui, ui_kx)
from odin.game.ocr_engine import ocr_lines, ocr_text

logger = logging.getLogger(__name__)

# 도는 순서 그대로. 발라는 가지 않는다 (10-18 결정).
BOSSES = ("가릉", "아우둠라", "해임달", "이둔")
MAX_DEATHS = 4
# 몇 번 죽어도 포기하지 않는 보스 (사용자 요청 11/07). 보스가 사는 동안만 — 출현 뒤
# WORLD_BOSS_LIFETIME 이 지나면 그만둔다. 잡혔거나 없으면 _fight 가 killed/absent 로 끝낸다.
NEVER_GIVE_UP = ("이둔",)
WORLD_BOSS_LIFETIME_SEC = 60 * 60

# ── 순간이동 목록 ────────────────────────────────────────
TELEPORT_MENU = (30, 165)          # 왼쪽 아이콘 열의 위치핀
LIST_CENTER = ui(170, 360, "TL")         # '[월보] …' 행들 — 녹화에선 y 246·302·358·414·470
LIST_W = 260
LIST_H = 320
TELEPORT_BTN_X = 326               # 행 오른쪽 이동(불꽃) 버튼
ROW_MATCH_MIN = 0.4                # 가릉↔'가름' 0.5, 아우둠라↔'아우-들라' 0.67

# ── 전투 중 판정 (OCR) ───────────────────────────────────
REWARD_CENTER = ui(960, 182, "C")       # '대륙 침략자 처치 보상' 제목
# 높이는 녹화(1920)의 50 보다 넉넉히 — 4K 에선 창이 가운데/위 어느 쪽 기준인지 아직 못 봤다
REWARD_W, REWARD_H = 420, 90
REWARD_CLOSE = (400, 250)          # 창 바깥 — 녹화에서 누른 자리 근처
BOSS_BAR_CENTER = (930, 22)        # '타락한 이둔 58분 16초 남음'
BOSS_BAR_W, BOSS_BAR_H = 560, 40
_BOSS_BAR_RE = re.compile(r"분\s*\d+\s*초|남음")

POLL_SEC = 0.5
# 체력바가 이만큼 안 보이면 다음 보스로. 출현 전(첫 보스)에는 출현 시각부터 잰다.
BOSS_ABSENT_SEC = 90.0
BOSS_MAX_SEC = 6 * 60.0            # 한 번 도착해서 머무는 상한 — 이둔이 사망 포함 2분 남짓

# AUTO 링은 효과에 가려 한 장으로는 0 으로 보일 때가 있다(켜진 상태에서 0.00~0.14).
# 여러 장 중 최댓값으로 본다 — 켜진 걸 꺼짐으로 읽고 G 를 누르면 도로 꺼진다.
AUTO_SAMPLES = 6
AUTO_SAMPLE_GAP = 0.4

# 4K 로 바꾼 뒤(10/30~) 체력바·보상 창을 한 번도 못 읽어 모든 보스가 'absent'(출현 90초 뒤
# 다음 보스로)가 됐다 — 다음 보스엔 이미 늦는다. 원인을 볼 수 있게 싸우는 동안 화면을 남긴다.
DEBUG_DIR = DATA_DIR / "worldboss_debug"
DEBUG_EVERY_SEC = 4.0
DEBUG_KEEP_RUNS = 3



def _ocr(center, w, h, win, scale=2) -> str:
    """BASE 기준 scale 배 크기로 OCR. 4K 에선 BASE 로 줄였다 다시 키우지 않고 원본을 그대로
    쓴다(≈1.03배) — 작은 글씨가 줄였다 키우며 뭉개지지 않게. 1920 에선 예전과 같다."""
    _hwnd, bx, by, cw, ch = win
    region = center_to_abs_region(center, w, h, bx, by, cw, ch)
    with mss.mss() as sct:
        shot = sct.grab(region)
    rgb = np.frombuffer(shot.rgb, np.uint8).reshape(shot.height, shot.width, 3)
    f = scale / ui_kx(cw)
    if abs(f - 1.0) > 0.1:
        rgb = cv2.resize(rgb, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    return ocr_text(rgb)


def _debug_prepare(name: str):
    """이번 보스용 캡처 폴더 — 최근 DEBUG_KEEP_RUNS 회분(보스 단위)만 유지."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        runs = sorted(p for p in DEBUG_DIR.iterdir() if p.is_dir())
        for old in runs[:max(0, len(runs) - DEBUG_KEEP_RUNS * len(BOSSES) + 1)]:
            for f in old.iterdir():
                f.unlink(missing_ok=True)
            old.rmdir()
        run = DEBUG_DIR / f"{datetime.now():%m%d_%H%M%S}_{name}"
        run.mkdir()
        return run
    except Exception as e:
        logger.warning("[월보] 디버그 폴더 준비 실패: %s", e)
        return None


def _debug_save(run_dir, elapsed: float, win, bar: str, reward: str):
    """화면 전체를 BASE 크기 JPG 로. 파일명에 경과 시간·체력바/보상 OCR 결과."""
    try:
        _hwnd, bx, by, cw, ch = win
        rgb = grab_base({"left": bx, "top": by, "width": cw, "height": ch}, cw, ch)
        safe = lambda t: re.sub(r"[^0-9a-zA-Z가-힣]", "_", t or "")[:16]
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                               [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            (run_dir / f"{elapsed:05.1f}s_bar-{safe(bar)}_rw-{safe(reward)}.jpg"
             ).write_bytes(buf.tobytes())
    except Exception as e:
        logger.debug("[월보] 디버그 저장 실패: %s", e)


def _row_key(text: str) -> str:
    """'[월보] 가름' → '가름'. 닫는 괄호를 '뵈' 로 읽는 경우가 있다('[월뵈 아우-들라')."""
    return re.split(r"[\]뵈]", text)[-1].strip()


def _find_row(name: str, win) -> int | None:
    """목록에서 name 과 가장 비슷한 '[월보]' 행의 BASE y."""
    img = _grab(LIST_CENTER, LIST_W, LIST_H, win)
    big = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    lines = ocr_lines(cv2.cvtColor(big, cv2.COLOR_BGR2RGB))
    best, best_y = 0.0, None   # 캡처(_grab)는 이미 BASE 크기 — 2배 확대만 되돌린다
    for line in lines:
        if "월" not in line.text:
            continue
        ratio = difflib.SequenceMatcher(None, name, _row_key(line.text)).ratio()
        if ratio > best:
            best = ratio
            best_y = LIST_CENTER[1] - LIST_H / 2 + line.y_center / 2
    logger.debug("[월보] 목록 OCR: %s", [ln.text for ln in lines])
    if best_y is None or best < ROW_MATCH_MIN:
        return None
    return round(best_y)


def _teleport(name: str, win):
    _hwnd, bx, by, cw, ch = win
    for attempt in range(3):
        _check_cancel()
        click_base(TELEPORT_MENU, bx, by, cw, ch)
        _sleep_cancellable(1.0)
        y = _find_row(name, win)
        if y is not None:
            click_base(ui(TELEPORT_BTN_X, y, "TL"), bx, by, cw, ch)
            logger.info("[월보] '%s' 로 순간이동 (행 y=%d)", name, y)
            return
        logger.warning("[월보] 목록에서 '%s' 를 못 찾음 (%d회차)", name, attempt + 1)
        _sleep_cancellable(1.0)
    raise RuntimeError(f"순간이동 목록에 '[월보] {name}' 없음")


def _auto_on(win) -> bool:
    best = 0.0
    for _ in range(AUTO_SAMPLES):
        best = max(best, _auto_ratio(win))
        if best >= AUTO_RING_MIN:
            return True
        _sleep_cancellable(AUTO_SAMPLE_GAP)
    return False


def _ensure_auto(win):
    """순간이동하면 AUTO 가 꺼진다 (녹화 7번 모두). 꺼져 있을 때만 G."""
    for _ in range(3):
        if _auto_on(win):
            logger.info("[월보] AUTO 켜짐")
            return
        _press_key(_VK_G)
        logger.info("[월보] AUTO 꺼짐 → G")
        _sleep_cancellable(0.5)
    logger.warning("[월보] AUTO 를 켰는지 확인 못 함 — 그대로 진행")


def _wait_loading(win):
    """순간이동·부활 버튼을 누른 직후부터 — 로딩 화면이 떴다가 걷힐 때까지."""
    _hwnd, bx, by, cw, ch = win
    _wait_for_loading_complete(bx, by, cw, ch)


def _arrive(name: str, win):
    _wait_loading(win)
    _sleep_cancellable(1.5)
    _ensure_auto(win)
    from odin.automation.automation_config import get_preset
    _press_preset(get_preset(name), f"월보 {name}")


def _fight(name: str, spawn_at: datetime, win) -> str:
    """도착한 뒤 결판이 날 때까지. 반환: 'killed' | 'died' | 'absent' | 'timeout'."""
    _hwnd, bx, by, cw, ch = win
    start = time.time()
    # 출현 전에 도착했으면(첫 보스) 출현 시각부터 '안 보임' 을 잰다
    seen_at = max(start, spawn_at.timestamp())
    debug_dir = _debug_prepare(name)
    last_debug = 0.0
    bar_seen = 0
    while time.time() - start < BOSS_MAX_SEC:
        _check_cancel()
        reward = _ocr(REWARD_CENTER, REWARD_W, REWARD_H, win)
        if debug_dir is not None and time.time() - last_debug >= DEBUG_EVERY_SEC:
            last_debug = time.time()
            bar = _ocr(BOSS_BAR_CENTER, BOSS_BAR_W, BOSS_BAR_H, win)
            _debug_save(debug_dir, last_debug - start, win, bar, reward)
            logger.debug("[월보] '%s' 체력바=%r 보상=%r", name, bar, reward)
        if "보상" in reward:
            logger.info("[월보] '%s' 처치 — 보상 창 (%s)", name, reward)
            for _ in range(3):
                click_base(REWARD_CLOSE, bx, by, cw, ch)
                _sleep_cancellable(1.0)
                if "보상" not in _ocr(REWARD_CENTER, REWARD_W, REWARD_H, win):
                    break
            return "killed"
        if is_dead(win):
            respawn_in_town(win, f"월보_{name}")
            logger.info("[월보] '%s' 에서 사망 → 마을에서 부활", name)
            return "died"
        if _BOSS_BAR_RE.search(_ocr(BOSS_BAR_CENTER, BOSS_BAR_W, BOSS_BAR_H, win)):
            seen_at = time.time()
            bar_seen += 1
        elif time.time() - seen_at > BOSS_ABSENT_SEC:
            logger.info("[월보] '%s' 체력바 %.0fs 안 보임 (그동안 읽은 횟수 %d)",
                        name, BOSS_ABSENT_SEC, bar_seen)
            return "absent"
        _sleep_cancellable(POLL_SEC)
    return "timeout"


def run_world_boss(spawn_at: datetime):
    """월드보스 한 회차 — 공용 큐 워커 스레드에서 동기 실행."""
    _cancel_event.clear()
    logger.info("[월보] 시작 — %s 회차 (Ctrl+Esc 로 중단 가능)", spawn_at.strftime("%H:%M"))
    try:
        win = find_game_window()
        ensure_game_foreground()
        _sleep_cancellable(0.5)

        results = {}
        for name in BOSSES:
            deaths = 0
            while True:
                _teleport(name, win)
                _arrive(name, win)
                result = _fight(name, spawn_at, win)
                # 죽은 채로 다른 결과가 먼저 잡힐 수 있다 — 누워 있는 동안 보스가 잡혀
                # 보상 창이 먼저 읽히거나, 사망 글자를 끝내 못 읽고 'absent' 가 되거나.
                # 그대로 다음 보스로 가면 사망 화면에서 순간이동 목록이 안 열린다.
                if result != "died" and is_dead(win):
                    respawn_in_town(win, f"월보_{name}_{result}")
                    logger.info("[월보] '%s' %s 뒤 사망 화면 → 마을에서 부활", name, result)
                    if result != "killed":
                        result = "died"   # 보스는 아직 살아 있다 — 다시 간다
                if result == "died":
                    deaths += 1
                    if name in NEVER_GIVE_UP:
                        if time.time() - spawn_at.timestamp() < WORLD_BOSS_LIFETIME_SEC:
                            logger.info("[월보] '%s' 에서 %d번째 사망 — 포기 안 함, 다시 간다",
                                        name, deaths)
                            continue
                        logger.info("[월보] '%s' 출현 %d분 지남 — 그만둠 (사망 %d회)",
                                    name, WORLD_BOSS_LIFETIME_SEC // 60, deaths)
                    elif deaths < MAX_DEATHS:
                        continue
                    else:
                        logger.info("[월보] '%s' 에서 %d번 사망 — 다음 보스로", name, deaths)
                break
            results[name] = result if result != "died" else f"사망 {deaths}회"
            logger.info("[월보] '%s' 결과: %s", name, results[name])

        from odin.automation.automation_config import get_idle_preset
        _press_preset(get_idle_preset(), "월보 끝")
        logger.info("[월보] 완료 — %s", results)
    except AutomationCancelled:
        logger.info("[월보] ⏹ Ctrl+Esc 로 중단됨")
        raise
