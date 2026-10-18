"""게임 화면 녹화 + 입력 기록 — 새 자동화(월드보스 등)를 만들 근거 자료용.

녹화 한 번 = DATA_DIR/recordings/YYYYMMDD_HHMMSS/ 폴더 하나:
  video_000.mp4, video_001.mp4 …  게임 창만 FPS 장/초. SEGMENT_SEC 마다 새 파일로 끊어
                                   앱이 비정상 종료돼도 앞 조각은 온전하다.
  frames.csv                       프레임별 (녹화 경과 초, 조각 번호, 조각 안 프레임 번호)
  events.jsonl                     마우스·키 누름/뗌 (녹화 경과 초, 커서 BASE 좌표).
                                   게임 창이 맨 앞에 있을 때만 기록한다 — 다른 창에서
                                   치는 키는 남기지 않는다.

게임 조작이 없는 순수 캡처라 game_queue 를 타지 않는다.
"""

import atexit
import csv
import ctypes
import ctypes.wintypes
import json
import logging
import threading
import time
from datetime import datetime

import cv2
import mss
import numpy as np

from odin.core.paths import DATA_DIR
from odin.game.game_coords import BASE_H, BASE_W, find_game_window

logger = logging.getLogger(__name__)

RECORDINGS_DIR = DATA_DIR / "recordings"
FPS = 5
SEGMENT_SEC = 300
MAX_DURATION_SEC = 2 * 3600       # 켜 둔 채 잊어버려도 디스크를 다 채우지 않게
INPUT_POLL_SEC = 0.01

_user32 = ctypes.windll.user32

_VK_NAMES = {
    0x01: "MouseLeft", 0x02: "MouseRight", 0x04: "MouseMiddle",
    0x05: "MouseX1", 0x06: "MouseX2",
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x10: "Shift", 0x11: "Ctrl",
    0x12: "Alt", 0x14: "CapsLock", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}
# Shift/Ctrl/Alt 는 좌우 구분 코드(0xA0~0xA5)도 같이 눌린 걸로 나와 중복되므로 일반 코드만 본다
_WATCHED_VKS = sorted(
    set(_VK_NAMES) | set(range(0x30, 0x3A)) | set(range(0x41, 0x5B))
    | set(range(0x60, 0x6A)) | set(range(0x70, 0x7C)))


def _vk_name(vk: int) -> str:
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x60 <= vk <= 0x69:
        return f"Num{vk - 0x60}"
    if 0x70 <= vk <= 0x7B:
        return f"F{vk - 0x6F}"
    return f"VK_{vk:02X}"


class ScreenRecorder:
    """start() / stop() — 녹화 중에는 두 개의 데몬 스레드(화면, 입력)가 돈다."""

    def __init__(self):
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.folder = None
        self.started_at: float | None = None
        self.frames = 0
        self.events = 0
        atexit.register(self.stop)   # 앱 종료 시 마지막 조각을 닫아 영상이 깨지지 않게

    @property
    def recording(self) -> bool:
        return self.started_at is not None

    def elapsed(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def start(self) -> bool:
        if self.recording:
            return True
        try:
            find_game_window()
        except Exception as e:
            logger.warning("[녹화] 게임 창을 찾지 못해 시작하지 않음: %s", e)
            return False
        self.folder = RECORDINGS_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder.mkdir(parents=True, exist_ok=True)
        self.frames = self.events = 0
        self._stop.clear()
        self.started_at = time.time()
        self._threads = [
            threading.Thread(target=self._video_loop, daemon=True, name="RecorderVideo"),
            threading.Thread(target=self._input_loop, daemon=True, name="RecorderInput"),
        ]
        for t in self._threads:
            t.start()
        logger.info("[녹화] 시작 → %s (%d fps, %d분 단위 파일)",
                    self.folder, FPS, SEGMENT_SEC // 60)
        return True

    def stop(self) -> None:
        if not self.recording:
            return
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        dur = self.elapsed()
        self.started_at = None
        logger.info("[녹화] 종료 — %.0f초, 프레임 %d장, 입력 %d건 → %s",
                    dur, self.frames, self.events, self.folder)

    # ─── 화면 ───────────────────────────────────
    def _video_loop(self):
        writer = None
        seg = -1
        seg_size = None
        seg_frame = 0
        next_t = time.time()
        with mss.mss() as sct, open(self.folder / "frames.csv", "w", newline="",
                                    encoding="utf-8") as fcsv:
            frames_csv = csv.writer(fcsv)
            frames_csv.writerow(["t", "segment", "frame"])
            try:
                while not self._stop.is_set():
                    t = self.elapsed()
                    if t > MAX_DURATION_SEC:
                        logger.warning("[녹화] %d시간 상한 도달 — 자동 종료", MAX_DURATION_SEC // 3600)
                        break
                    try:
                        _h, bx, by, cw, ch = find_game_window()
                        shot = sct.grab({"left": bx, "top": by, "width": cw, "height": ch})
                        frame = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)
                    except Exception as e:
                        logger.debug("[녹화] 캡처 실패: %s", e)
                        frame = None
                    if frame is not None:
                        want_seg = int(t // SEGMENT_SEC)
                        if writer is None or want_seg != seg:
                            if writer is not None:
                                writer.release()
                            seg = want_seg
                            h, w = frame.shape[:2]
                            seg_size = (w - w % 2, h - h % 2)   # 코덱이 짝수 크기를 요구
                            writer = cv2.VideoWriter(
                                str(self.folder / f"video_{seg:03d}.mp4"),
                                cv2.VideoWriter_fourcc(*"mp4v"), FPS, seg_size)
                            seg_frame = 0
                        if (frame.shape[1], frame.shape[0]) != seg_size:
                            frame = cv2.resize(frame, seg_size)
                        writer.write(frame)
                        frames_csv.writerow([f"{t:.2f}", seg, seg_frame])
                        seg_frame += 1
                        self.frames += 1
                    next_t += 1 / FPS
                    self._stop.wait(max(0.0, next_t - time.time()))
            finally:
                if writer is not None:
                    writer.release()

    # ─── 입력 ───────────────────────────────────
    def _input_loop(self):
        held: set[int] = set()
        with open(self.folder / "events.jsonl", "w", encoding="utf-8") as f:
            while not self._stop.is_set():
                try:
                    hwnd, bx, by, cw, ch = find_game_window()
                    in_game = _user32.GetForegroundWindow() == hwnd
                except Exception:
                    in_game = False
                now_down = ({vk for vk in _WATCHED_VKS if _user32.GetAsyncKeyState(vk) & 0x8000}
                            if in_game else set())
                changes = [(vk, "down") for vk in now_down - held] + \
                          [(vk, "up") for vk in held - now_down]
                if changes:
                    pt = ctypes.wintypes.POINT()
                    _user32.GetCursorPos(ctypes.byref(pt))
                    lx, ly = pt.x - bx, pt.y - by
                    base = [round(lx * BASE_W / cw), round(ly * BASE_H / ch)] if cw and ch else None
                    t = round(self.elapsed(), 3)
                    for vk, action in changes:
                        f.write(json.dumps({"t": t, "key": _vk_name(vk), "action": action,
                                            "base": base}, ensure_ascii=False) + "\n")
                        self.events += 1
                    f.flush()
                held = now_down
                time.sleep(INPUT_POLL_SEC)
