"""
마우스 좌표 실시간 모니터링
게임 윈도우 기준 로컬 좌표도 함께 표시

사용법: python mouse_monitor.py
종료: Ctrl+C
"""

import ctypes
import ctypes.wintypes
import time

# DPI awareness
ctypes.windll.shcore.SetProcessDpiAwareness(2)

# 게임 윈도우 찾기
hwnd = ctypes.windll.user32.FindWindowW(None, "ODIN  ")
if not hwnd:
    hwnd = ctypes.windll.user32.FindWindowW(None, "ODIN")

if hwnd:
    pt = ctypes.wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt))
    game_x, game_y = pt.x, pt.y
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    print(f"게임 윈도우: ({game_x}, {game_y}), 크기: {rect.right}x{rect.bottom}")
else:
    game_x, game_y = 0, 0
    print("게임 윈도우를 찾지 못했습니다. 절대 좌표만 표시합니다.")

print("=" * 60)
print("마우스를 움직이세요. Ctrl+C로 종료.")
print("=" * 60)

try:
    while True:
        cursor = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
        local_x = cursor.x - game_x
        local_y = cursor.y - game_y
        print(f"\r절대: ({cursor.x:5d}, {cursor.y:5d}) | 게임 내: ({local_x:5d}, {local_y:5d})", end="", flush=True)
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\n종료")
