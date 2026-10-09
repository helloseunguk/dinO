"""매크로 좌표 수집 공용 모듈 — 저장소 + 좌표 환산 + 단축키 조회.

CLI(mouse_monitor.py)와 GUI(gui.MacroPointDialog)가 같은 구현·같은 파일을
공유한다. 저장 단위는 '점 하나' 이고, 기록 기준은 BASE(1920x1009) 환산 좌표다
(boss_config / automator 상수가 쓰는 좌표계 — 저장값을 그대로 붙여넣을 수 있다).
절대·게임 로컬 좌표와 캡처 당시 클라이언트 크기도 같이 남겨 나중에 검증 가능하게 한다.
"""

import ctypes
import ctypes.wintypes
import json
import logging
from datetime import datetime

from odin.game.game_coords import BASE_H, BASE_W, find_game_window
from odin.core.paths import DATA_DIR

logger = logging.getLogger(__name__)

POINTS_PATH = DATA_DIR / "macro_points.json"

# 단축키 — 게임에 포커스가 있어도 잡히도록 GetAsyncKeyState 폴링으로 감시한다.
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79

_user32 = ctypes.windll.user32


def key_down(vk: int) -> bool:
    """해당 가상키가 지금 눌려 있는지 (포커스 무관)."""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def cursor_pos() -> tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def window_origin() -> tuple[int, int, int, int]:
    """게임 창의 (원점 x, 원점 y, 클라이언트 w, h). 못 찾으면 (0, 0, 0, 0)."""
    try:
        _, gx, gy, cw, ch = find_game_window()
        return gx, gy, cw, ch
    except Exception as e:
        logger.warning("[좌표수집] 게임 창을 찾지 못함: %s", e)
        return 0, 0, 0, 0


def to_base(local_x: int, local_y: int, cw: int, ch: int) -> tuple[int, int]:
    """게임 로컬(실제 픽셀) → BASE 환산. game_coords.scale_xy 의 역변환."""
    if not cw or not ch:
        return local_x, local_y
    return round(local_x * BASE_W / cw), round(local_y * BASE_H / ch)


def outside_client(lx: int, ly: int, cw: int, ch: int) -> bool:
    """커서가 게임 클라이언트 영역 밖인지 — 밖이면 좌표가 음수/초과로 나온다."""
    if not cw or not ch:
        return False
    return not (0 <= lx < cw and 0 <= ly < ch)


def snapshot(gx: int, gy: int, cw: int, ch: int, name: str = "") -> dict:
    """현재 커서 위치를 절대/로컬/BASE 세 기준으로 기록."""
    ax, ay = cursor_pos()
    lx, ly = ax - gx, ay - gy
    bx, by = to_base(lx, ly, cw, ch)
    return {
        "name": name,
        "base": [bx, by],
        "local": [lx, ly],
        "abs": [ax, ay],
        "client": [cw, ch],
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_points() -> list[dict]:
    if not POINTS_PATH.exists():
        return []
    try:
        data = json.loads(POINTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("[좌표수집] 저장 파일 로드 실패 (%s): %s", POINTS_PATH.name, e)
        return []


def save_points(points: list[dict]) -> None:
    POINTS_PATH.write_text(
        json.dumps(points, ensure_ascii=False, indent=2), encoding="utf-8")


def auto_name(points: list[dict]) -> str:
    """이름을 비워둔 채 저장할 때 쓰는 기본 이름 — 기존과 겹치지 않게."""
    used = {p.get("name", "") for p in points}
    i = len(points) + 1
    while f"point{i}" in used:
        i += 1
    return f"point{i}"


def format_line(point: dict) -> str:
    bx, by = point["base"]
    lx, ly = point["local"]
    ax, ay = point["abs"]
    cw, ch = point.get("client", [0, 0])
    mark = "  ⚠ 게임 창 밖" if outside_client(lx, ly, cw, ch) else ""
    return (f"BASE ({bx:4d}, {by:4d}) | 게임 ({lx:5d}, {ly:5d}) "
            f"| 절대 ({ax:5d}, {ay:5d}){mark}")


def code_snippet(points: list[dict]) -> str:
    """코드에 그대로 붙여넣을 수 있는 BASE 좌표 목록."""
    return "\n".join(
        f'    "{p["name"] or "unnamed"}": ({p["base"][0]}, {p["base"][1]}),'
        for p in points
    )
