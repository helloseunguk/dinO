"""
게임 좌표 변환 & 마우스 입력 공용 헬퍼.

모든 좌표는 BASE 해상도(1920×1009, 캘리브레이션 기준) 내부 좌표를 기준으로 저장하고,
실행 시 실제 클라이언트 크기로 스케일 + bx/by 오프셋 적용 → 절대 스크린 좌표로 변환됨.

모듈 import만으로 DPI-aware(Per-Monitor v2) 설정 자동 수행.
"""

import ctypes
import ctypes.wintypes
import time


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


# ─── BASE ↔ 실제 좌표 스케일 ────────────────────────────────

def scale_xy(base_xy: tuple[int, int], cw: int, ch: int) -> tuple[int, int]:
    """BASE 해상도 좌표 → 실제 클라이언트 크기 비율로 스케일."""
    bx, by = base_xy
    return int(bx * cw / BASE_W), int(by * ch / BASE_H)


def scale_w(px: int, cw: int) -> int:
    return int(px * cw / BASE_W)


def scale_h(px: int, ch: int) -> int:
    return int(px * ch / BASE_H)


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


# ─── 마우스 입력 ────────────────────────────────────────────

_MOUSEEVENTF_LEFTDOWN = 0x02
_MOUSEEVENTF_LEFTUP = 0x04
_MOUSEEVENTF_WHEEL = 0x0800
_WHEEL_DELTA = 120


def click_at_screen(abs_x: int, abs_y: int) -> None:
    """스크린 절대 좌표에서 좌클릭."""
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
    ctypes.windll.user32.SetCursorPos(x, y)
    time.sleep(0.05)
    for _ in range(notches):
        ctypes.windll.user32.mouse_event(
            _MOUSEEVENTF_WHEEL, 0, 0, -_WHEEL_DELTA, 0)
        time.sleep(0.02)
