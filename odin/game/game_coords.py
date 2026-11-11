"""
게임 좌표 변환 & 마우스 입력 공용 헬퍼.

모든 좌표는 BASE 해상도(1920×1009, 캘리브레이션 기준) 내부 좌표를 기준으로 저장하고,
실행 시 실제 클라이언트 크기로 스케일 + bx/by 오프셋 적용 → 절대 스크린 좌표로 변환됨.

모듈 import만으로 DPI-aware(Per-Monitor v2) 설정 자동 수행.
"""

import ctypes
import ctypes.wintypes
import time

import cv2
import mss
import numpy as np


# --- DPI awareness (게임과 동일하게) ---
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass


# === 기준 클라이언트 해상도 ===
# 1920x1080 모니터 - 타이틀바 ≈ 1920x1009 (캘리브레이션 당시 실측값)
BASE_W = 1920
BASE_H = 1009


# ─── 윈도우 / 클라이언트 정보 ────────────────────────────────

def client_size(hwnd: int) -> tuple[int, int]:
    """게임 클라이언트 영역의 실제 가로·세로 (픽셀)."""
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right - rect.left, rect.bottom - rect.top


def find_game_window(
    title_candidates: tuple[str, ...] = ("ODIN  ", "ODIN"),
) -> tuple[int, int, int, int, int]:
    """게임 윈도우를 찾아 (hwnd, bx, by, cw, ch) 반환.
      - hwnd: 윈도우 핸들
      - bx, by: 클라이언트 영역의 스크린 절대 좌표 (원점)
      - cw, ch: 클라이언트 가로·세로 (실제 픽셀)
    """
    hwnd = 0
    for title in title_candidates:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            break
    if not hwnd:
        raise RuntimeError(
            f"게임 윈도우를 찾을 수 없습니다 (후보: {title_candidates})"
        )
    pt = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    cw, ch = client_size(hwnd)
    return hwnd, pt.x, pt.y, cw, ch


# ─── 게임 UI 배율 · 기준점 ───────────────────────────────────
# 게임 UI 는 창 크기 비율대로 커지지 않을 때가 있다. 4K(3840×2041) 창에선 창이 BASE 의
# 2.0×2.02 배인데 UI 는 1.94 배였고(2026-10-30 실측: 카메라 아이콘 1.935, 오른쪽 아이콘열
# 1.945, 상단 아이콘줄 1.94, 주변감지 바 1.95), 각 UI 는 붙어 있는 화면 가장자리(기준점)
# 쪽으로 모였다 — 카메라 아이콘 y 441 → 4K/2.02 기준 421.
# 실측값이 없는 크기는 예전처럼 창 비율 그대로 (그때는 기준점이 결과에 영향 없음).
UI_SCALE_MEASURED: dict[tuple[int, int], float] = {
    (3840, 2041): 1.94,
    (2560, 1335): 1.24,   # 2번 QHD 모니터 (11/02 실측: 지구본·메뉴·달력·카메라 아이콘 1.23~1.25)
}
# 창 크기가 이 비율만큼 달라도 같은 실측값으로 본다. 게임을 다시 켜면 창 테두리가 바뀌어
# 높이가 2041 → 2031 처럼 몇 px 달라진다 — 예전 허용치 8px 로는 세로만 창 비율(2.01)로
# 돌아가 카메라·AUTO·이동 버튼·경험치 복구 창이 다 어긋났다 (10/31 06:58 이후).
_UI_SCALE_TOLERANCE = 0.02

# 기준점 = (가로, 세로) 각각 0 왼쪽/위 · 0.5 가운데 · 1 오른쪽/아래
ANCHORS: dict[str, tuple[float, float]] = {
    "TL": (0.0, 0.0), "T": (0.5, 0.0), "TR": (1.0, 0.0),
    "L": (0.0, 0.5), "C": (0.5, 0.5), "R": (1.0, 0.5),
    "BL": (0.0, 1.0), "B": (0.5, 1.0), "BR": (1.0, 1.0),
}


class UIPoint(tuple):
    """기준점이 붙은 BASE 좌표. 그냥 (x, y) 튜플처럼 쓰면 된다."""
    anchor: tuple[float, float]

    def __new__(cls, x: float, y: float, anchor: str):
        p = super().__new__(cls, (x, y))
        p.anchor = ANCHORS[anchor]
        return p


def ui(x: float, y: float, anchor: str) -> UIPoint:
    """기준점을 명시한 BASE 좌표. 화면을 3×3 으로 나눈 기본 규칙과 다를 때만 쓴다
    (왼쪽 아이콘열은 화면 중간 높이여도 위쪽 기준, 가운데 팝업은 위·아래 끝이어도 가운데 기준)."""
    return UIPoint(x, y, anchor)


def anchor_of(base_xy) -> tuple[float, float]:
    """명시한 기준점, 없으면 화면 3×3 칸 중 그 좌표가 속한 칸."""
    anchor = getattr(base_xy, "anchor", None)
    if anchor is not None:
        return anchor
    x, y = base_xy

    def third(v: float, size: int) -> float:
        return 0.0 if v < size / 3 else (1.0 if v > size * 2 / 3 else 0.5)
    return third(x, BASE_W), third(y, BASE_H)


def _measured_k(size: int, axis: int) -> float | None:
    for dims, k in UI_SCALE_MEASURED.items():
        if abs(dims[axis] - size) <= dims[axis] * _UI_SCALE_TOLERANCE:
            return k
    return None


def ui_kx(cw: int) -> float:
    """BASE 1px 가 실제 화면 가로 몇 px 인가."""
    k = _measured_k(cw, 0)
    return k if k is not None else cw / BASE_W


def ui_ky(ch: int) -> float:
    """BASE 1px 가 실제 화면 세로 몇 px 인가."""
    k = _measured_k(ch, 1)
    return k if k is not None else ch / BASE_H


# ─── BASE ↔ 실제 좌표 스케일 ────────────────────────────────

def scale_xy(base_xy: tuple[int, int], cw: int, ch: int) -> tuple[int, int]:
    """BASE 해상도 좌표 → 실제 클라이언트 좌표 (기준점에서 UI 배율만큼 떨어진 자리)."""
    bx, by = base_xy
    ax, ay = anchor_of(base_xy)
    return (int(ax * cw + ui_kx(cw) * (bx - ax * BASE_W)),
            int(ay * ch + ui_ky(ch) * (by - ay * BASE_H)))


def scale_w(px: int, cw: int) -> int:
    return int(px * ui_kx(cw))


def scale_h(px: int, ch: int) -> int:
    return int(px * ui_ky(ch))


def center_to_abs_region(
    center_xy: tuple[int, int], base_w: int, base_h: int,
    bx: int, by: int, cw: int, ch: int,
) -> dict:
    """BASE 중심 좌표 + 크롭 크기 → 절대 스크린 좌표 region dict (mss 호환)."""
    cx, cy = scale_xy(center_xy, cw, ch)
    w = scale_w(base_w, cw)
    h = scale_h(base_h, ch)
    return {
        "left":   bx + cx - w // 2,
        "top":    by + cy - h // 2,
        "width":  w,
        "height": h,
    }


# ─── BASE 크기로 캡처 ────────────────────────────────────────
# 템플릿 이미지, 템플릿 안쪽 픽셀 위치(카메라 막대 등), OCR 확대 배율·이진화 값은
# 모두 BASE(1920×1009) 화면에서 잰 것이다. 창이 4K 처럼 커지면 캡처를 UI 배율만큼
# 줄여서(= UI 가 BASE 때 크기가 되게) 판정한다 — 기존 기준값이 그대로 맞는다.

def grab_base(region: dict, cw: int, ch: int, sct=None) -> np.ndarray:
    """실제 스크린 좌표 region 을 캡처해 UI 가 BASE 크기가 되게 줄인 RGB 이미지.
    창이 BASE 크기면 원본 그대로."""
    if sct is None:
        with mss.mss() as s:
            shot = s.grab(region)
    else:
        shot = sct.grab(region)
    img = np.frombuffer(shot.rgb, np.uint8).reshape(shot.height, shot.width, 3)
    sw, sh = 1 / ui_kx(cw), 1 / ui_ky(ch)
    if abs(sw - 1.0) < 0.02 and abs(sh - 1.0) < 0.02:
        return img.copy()
    size = (max(1, round(shot.width * sw)), max(1, round(shot.height * sh)))
    interp = cv2.INTER_AREA if sw < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(img, size, interpolation=interp)


def base_offset_to_abs(region: dict, base_xy: tuple[float, float],
                       cw: int, ch: int) -> tuple[int, int]:
    """grab_base 이미지 안의 위치 → 실제 스크린 절대 좌표."""
    return (region["left"] + round(base_xy[0] * ui_kx(cw)),
            region["top"] + round(base_xy[1] * ui_ky(ch)))


# ─── 게임 창을 맨 앞으로 ────────────────────────────────────
# 백그라운드 프로세스의 SetForegroundWindow 는 Windows 가 조용히 막는다. 결과를 안 보던 때엔
# 게임이 다른 창에 가려진 채 클릭·키가 그 창으로 나갔다 (11/05 16:22 브라우저 — 이동 실패 후
# 아이들 클릭과 C 키 30번이 브라우저로). 그래서 입력 직전에 확인하고, 못 올리면 입력하지 않는다.

class GameNotForeground(RuntimeError):
    """게임 창을 맨 앞으로 올리지 못함 — 입력을 보내지 않았다."""


def _game_hwnd() -> int:
    for title in ("ODIN  ", "ODIN"):
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            return hwnd
    return 0


def _wait_foreground(hwnd: int, sec: float) -> bool:
    end = time.time() + sec
    while True:
        if ctypes.windll.user32.GetForegroundWindow() == hwnd:
            return True
        if time.time() >= end:
            return False
        time.sleep(0.05)


def _raise_window(hwnd: int) -> bool:
    """맨 앞으로 올린다 — 입력 스레드 연결, 안 되면 Alt 한 번 뒤 재시도(Windows 표준 우회)."""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    if _wait_foreground(hwnd, 0.7):
        return True
    user32.keybd_event(0x12, 0, 0, 0)        # VK_MENU
    user32.keybd_event(0x12, 0, 0x0002, 0)
    user32.SetForegroundWindow(hwnd)
    return _wait_foreground(hwnd, 0.7)


def ensure_game_foreground() -> None:
    """게임 창이 맨 앞인지 확인하고 아니면 올린다. 못 올리면 GameNotForeground."""
    hwnd = _game_hwnd()
    if not hwnd or ctypes.windll.user32.GetForegroundWindow() == hwnd:
        return
    if not _raise_window(hwnd):
        raise GameNotForeground("게임 창을 맨 앞으로 올리지 못함 — 입력 안 보냄")
    time.sleep(0.3)   # 화면이 바뀐 뒤 캡처·클릭


# ─── 마우스 입력 ────────────────────────────────────────────

_MOUSEEVENTF_LEFTDOWN = 0x02
_MOUSEEVENTF_LEFTUP = 0x04
_MOUSEEVENTF_WHEEL = 0x0800
_WHEEL_DELTA = 120


def click_at_screen(abs_x: int, abs_y: int) -> None:
    """스크린 절대 좌표에서 좌클릭. 게임 창이 맨 앞이 아니면 올리고, 못 올리면 예외."""
    ensure_game_foreground()
    ctypes.windll.user32.SetCursorPos(abs_x, abs_y)
    time.sleep(0.15)
    ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def click_game(game_x: int, game_y: int, bx: int, by: int) -> None:
    """게임 클라이언트 좌표(이미 스케일된) → bx/by 더해서 스크린 좌표로 클릭."""
    click_at_screen(bx + game_x, by + game_y)


def click_base(
    base_xy: tuple[int, int],
    bx: int, by: int, cw: int, ch: int,
) -> None:
    """BASE 좌표 한 번에 클릭 (scale + offset + click)."""
    gx, gy = scale_xy(base_xy, cw, ch)
    click_game(gx, gy, bx, by)


def scroll_wheel_down(x: int, y: int, notches: int) -> None:
    """해당 스크린 좌표에서 휠 다운 (줌 아웃)."""
    ensure_game_foreground()
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)
    for _ in range(notches):
        ctypes.windll.user32.mouse_event(
            _MOUSEEVENTF_WHEEL, 0, 0, -_WHEEL_DELTA, 0)
        time.sleep(0.02)
