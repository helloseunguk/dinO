"""
python_study - 실행 로직
"""
import os
import sys
import time
import json
import random
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, Callable

if sys.platform.startswith("win"):
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # Windows 백그라운드 프로세스 자동 throttling(EcoQoS) opt-out.
    # 매크로 창이 최소화돼도 CPU 클럭이 떨어지지 않도록 함.
    # 64비트 HANDLE 이 잘리지 않게 argtypes 를 반드시 명시해야 호출 성공.
    try:
        class _PPTState(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]
        _k32 = ctypes.windll.kernel32
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        _k32.SetProcessInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        _k32.SetProcessInformation.restype = ctypes.c_int
        _state = _PPTState(1, 0x1, 0x0)  # EXECUTION_SPEED, opt-out
        _k32.SetProcessInformation(
            _k32.GetCurrentProcess(),
            4,  # ProcessPowerThrottling
            ctypes.byref(_state),
            ctypes.sizeof(_state),
        )
    except Exception:
        pass

import cv2
import numpy as np
import pyautogui
import mss
from PIL import Image

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    import pydirectinput
    HAS_PYDIRECTINPUT = True
except ImportError:
    HAS_PYDIRECTINPUT = False


pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


@dataclass
class StepResult:
    ok: bool
    pos: Optional[Tuple[int, int]] = None
    msg: str = ""


class MacroEngine:
    def __init__(self, project_dir: str, log_fn: Optional[Callable[[str], None]] = None):
        self.project_dir = project_dir
        self.images_dir = os.path.join(project_dir, "images")
        self.config_path = os.path.join(project_dir, "config.json")
        self.positions_path = os.path.join(project_dir, "last_positions.json")
        self.log = log_fn or (lambda m: print(m))
        self.stop_event = threading.Event()
        self.config = self._load_config()
        self._apply_tesseract_path()
        self._last_positions: dict = self._load_positions()

    def _load_positions(self) -> dict:
        if not os.path.exists(self.positions_path):
            return {}
        try:
            with open(self.positions_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            out = {}
            for k, v in data.items():
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    out[str(k)] = (int(v[0]), int(v[1]))
            return out
        except Exception:
            return {}

    def _save_positions(self):
        try:
            with open(self.positions_path, "w", encoding="utf-8") as f:
                json.dump({k: list(v) for k, v in self._last_positions.items()},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"[좌표 저장 실패] {e}")

    def _remember_click(self, key: str, cx: int, cy: int):
        if not key:
            return
        self._last_positions[key] = (int(cx), int(cy))
        self._save_positions()

    def _load_config(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self, cfg: dict):
        self.config = cfg
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def _apply_tesseract_path(self):
        if not HAS_TESSERACT:
            return
        path = self.config.get("tesseract_path", "").strip()
        if path and os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return
        default_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
        for p in default_paths:
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
                return

    def stop(self):
        self.stop_event.set()

    def reset(self):
        self.stop_event.clear()

    def _sleep(self, seconds: float) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            time.sleep(0.05)
        return True

    def grab_screen_bgr(self) -> Tuple[np.ndarray, int, int]:
        """선택된 디스플레이의 화면을 캡처. 반환: (bgr, mon_left, mon_top).

        Parsec 등 원격 스트리밍 클라이언트가 background 일 때 다른 창에 가려져
        스크린샷이 다른 창 픽셀을 잡거나 stale frame 을 잡는 문제를 방지하기 위해,
        캡처 직전에 게임 창(저장된 시작 버튼 위 윈도우) 을 foreground 로 끌어옴.
        설정에서 'force_focus_on_capture': false 로 끄면 비활성화."""
        if self.config.get("force_focus_on_capture", True):
            if self._focus_game_window():
                # 포커스 직후엔 클라이언트가 프레임 따라잡을 시간을 줌
                time.sleep(0.05)

        idx = int(self.config.get("monitor_index", 1))
        with mss.mss() as sct:
            mons = sct.monitors
            if idx < 1 or idx >= len(mons):
                idx = 1
            mon = mons[idx]
            raw = np.array(sct.grab(mon))
        img = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
        return img, int(mon["left"]), int(mon["top"])

    @staticmethod
    def list_monitors() -> list:
        """사용 가능한 디스플레이 목록: [(index, width, height, left, top), ...]"""
        out = []
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                out.append((i, int(m["width"]), int(m["height"]),
                            int(m["left"]), int(m["top"])))
        return out

    def find_image(self, template_path: str, threshold: Optional[float] = None,
                   region: Optional[Tuple[int, int, int, int]] = None,
                   debug_label: Optional[str] = None,
                   ) -> Optional[Tuple[int, int, int, int, float]]:
        """멀티 스케일 템플릿 매칭. 반환: (절대x, 절대y, w, h, score) 또는 None.
        debug_label 지정 시 실패해도 최고 점수/임계값/스케일을 로그로 남김."""
        if not os.path.exists(template_path):
            if debug_label:
                self.log(f"  [매칭실패] {debug_label}: 템플릿 파일 없음")
            return None
        thr = threshold if threshold is not None else float(self.config.get("match_threshold", 0.8))
        screen, mon_x, mon_y = self.grab_screen_bgr()
        ox, oy = mon_x, mon_y
        if region:
            x, y, w, h = region
            screen = screen[y:y + h, x:x + w]
            ox, oy = mon_x + x, mon_y + y

        tmpl = cv2.imread(template_path, cv2.IMREAD_COLOR)
        if tmpl is None:
            if debug_label:
                self.log(f"  [매칭실패] {debug_label}: 템플릿 로드 실패")
            return None

        best = None
        best_overall_score = -1.0
        best_overall_scale = 1.0
        for scale in (1.0, 0.9, 1.1, 0.8, 1.2, 0.7, 1.3):
            if scale != 1.0:
                t = cv2.resize(tmpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                t = tmpl
            if t.shape[0] > screen.shape[0] or t.shape[1] > screen.shape[1]:
                continue
            res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_overall_score:
                best_overall_score = float(max_val)
                best_overall_scale = scale
            if max_val >= thr and (best is None or max_val > best[4]):
                h, w = t.shape[:2]
                best = (max_loc[0] + ox, max_loc[1] + oy, w, h, float(max_val))
        if best is None and debug_label:
            self.log(f"  [매칭실패] {debug_label}: 최고점수={best_overall_score:.3f} "
                     f"(임계값={thr:.2f}, 부족분={(thr - best_overall_score):+.3f}, "
                     f"scale={best_overall_scale:.1f})")
        return best

    def wait_image(self, template_path: str, timeout: Optional[float] = None,
                   region: Optional[Tuple[int, int, int, int]] = None
                   ) -> Optional[Tuple[int, int, int, int, float]]:
        timeout = timeout if timeout is not None else float(self.config.get("search_timeout", 15))
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                return None
            box = self.find_image(template_path, region=region)
            if box:
                return box
            time.sleep(0.3)
        return None

    def _get_region(self, filename: str) -> Optional[Tuple[int, int, int, int]]:
        regions = self.config.get("regions", {}) or {}
        r = regions.get(filename)
        if r and len(r) == 4:
            return tuple(int(v) for v in r)
        return None

    def find_text(self, text: str, region: Optional[Tuple[int, int, int, int]] = None
                  ) -> Optional[Tuple[int, int, int, int]]:
        """OCR 로 텍스트 찾기. 반환: (절대x, 절대y, w, h)"""
        if not HAS_TESSERACT:
            self.log("[경고] pytesseract 미설치 - 텍스트 인식 불가")
            return None
        screen, mon_x, mon_y = self.grab_screen_bgr()
        ox, oy = mon_x, mon_y
        if region:
            x, y, w, h = region
            screen = screen[y:y + h, x:x + w]
            ox, oy = mon_x + x, mon_y + y

        gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        scaled = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidates = [binary, cv2.bitwise_not(binary)]

        lang = self.config.get("ocr_lang", "kor+eng")
        target = text.replace(" ", "")
        for img in candidates:
            try:
                data = pytesseract.image_to_data(
                    img, lang=lang, config="--psm 6",
                    output_type=pytesseract.Output.DICT,
                )
            except Exception as e:
                self.log(f"[OCR 오류] {e}")
                return None

            n = len(data["text"])
            joined = []
            for i in range(n):
                t = (data["text"][i] or "").strip()
                if not t:
                    continue
                joined.append((i, t))

            for i, t in joined:
                if target in t.replace(" ", ""):
                    x = int(data["left"][i] / 2) + ox
                    y = int(data["top"][i] / 2) + oy
                    w = int(data["width"][i] / 2)
                    h = int(data["height"][i] / 2)
                    return (x, y, w, h)

            for start in range(len(joined)):
                combined = ""
                indices = []
                for k in range(start, min(start + 5, len(joined))):
                    idx, t = joined[k]
                    combined += t
                    indices.append(idx)
                    if target in combined.replace(" ", ""):
                        xs = [int(data["left"][i] / 2) for i in indices]
                        ys = [int(data["top"][i] / 2) for i in indices]
                        xe = [int((data["left"][i] + data["width"][i]) / 2) for i in indices]
                        ye = [int((data["top"][i] + data["height"][i]) / 2) for i in indices]
                        x = min(xs) + ox
                        y = min(ys) + oy
                        w = max(xe) - min(xs)
                        h = max(ye) - min(ys)
                        return (x, y, w, h)
        return None

    def wait_text(self, text: str, timeout: Optional[float] = None,
                  region: Optional[Tuple[int, int, int, int]] = None
                  ) -> Optional[Tuple[int, int, int, int]]:
        timeout = timeout if timeout is not None else float(self.config.get("search_timeout", 15))
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                return None
            box = self.find_text(text, region=region)
            if box:
                return box
            time.sleep(0.4)
        return None

    def _click_xy(self, cx: int, cy: int):
        jitter = int(self.config.get("click_jitter_px", 3))
        x, y = int(cx), int(cy)
        if jitter > 0:
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)

        # 0) 클릭 좌표 위에 있는 윈도우(예: Parsec)를 foreground 로.
        #    원격 스트리밍 클라이언트는 자기 창이 활성화돼 있을 때만
        #    마우스 입력을 원격 호스트로 forward 하므로 매 클릭마다 필수.
        focused = self._focus_game_window(at_xy=(x, y))

        # 1) 커서를 정확히 위치시킨 뒤 살짝 안정화
        pyautogui.moveTo(x, y, duration=0.1)
        time.sleep(0.05)

        # 2) mouseDown → 80ms 홀드 → mouseUp.
        #    pyautogui.click() 은 0ms 홀드라 일부 게임이 무시함.
        #    pydirectinput 가 있으면 우선 사용 (DirectInput 게임 호환).
        backend = "pyautogui"
        try:
            if HAS_PYDIRECTINPUT:
                pydirectinput.mouseDown(button="primary", _pause=False)
                time.sleep(0.08)
                pydirectinput.mouseUp(button="primary", _pause=False)
                backend = "pydirectinput"
            else:
                pyautogui.mouseDown(button="primary", _pause=False)
                time.sleep(0.08)
                pyautogui.mouseUp(button="primary", _pause=False)
        except Exception as e:
            self.log(f"  {backend} 클릭 실패 ({e}) → pyautogui.click 폴백")
            try:
                pyautogui.click()
            except Exception as e2:
                self.log(f"  pyautogui.click 도 실패: {e2}")

        focus_msg = "포커스OK" if focused else "포커스실패"
        self.log(f"  클릭 위치: ({x}, {y})  "
                 f"[지터 ±{jitter}px, {backend}, 80ms 홀드, {focus_msg}]")

        # 3) 게임이 클릭을 처리할 시간 확보 후 커서 파킹
        time.sleep(0.15)
        self._park_cursor()

    def _park_cursor(self):
        """클릭 후 커서를 모니터 우하단 끝으로 옮겨 hover 잔상으로 인한
        템플릿 매칭 실패를 방지. (0,0) 은 pyautogui FAILSAFE 라 피함."""
        try:
            idx = int(self.config.get("monitor_index", 1))
            with mss.mss() as sct:
                mons = sct.monitors
                if idx < 1 or idx >= len(mons):
                    idx = 1
                mon = mons[idx]
            px = int(mon["left"]) + int(mon["width"]) - 2
            py = int(mon["top"]) + int(mon["height"]) - 2
            pyautogui.moveTo(px, py, duration=0.0)
        except Exception:
            pass

    def _save_debug_screenshot(self, label: str,
                               click_xy: Optional[Tuple[int, int]] = None,
                               keep_last: int = 30):
        """현재 화면을 debug_screenshots/ 에 저장. click_xy 가 주어지면
        그 위치에 빨간 십자/원 표시. keep_last 개 초과분은 자동 삭제."""
        try:
            debug_dir = os.path.join(self.project_dir, "debug_screenshots")
            os.makedirs(debug_dir, exist_ok=True)
            screen, mon_x, mon_y = self.grab_screen_bgr()
            if click_xy is not None:
                cx = int(click_xy[0]) - mon_x
                cy = int(click_xy[1]) - mon_y
                if 0 <= cx < screen.shape[1] and 0 <= cy < screen.shape[0]:
                    cv2.drawMarker(screen, (cx, cy), (0, 0, 255),
                                   cv2.MARKER_CROSS, markerSize=80, thickness=4)
                    cv2.circle(screen, (cx, cy), 50, (0, 0, 255), 3)
                    cv2.putText(screen, "CLICK", (cx + 60, cy - 20),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in label)
            path = os.path.join(debug_dir, f"{ts}_{safe}.png")
            cv2.imwrite(path, screen)
            self.log(f"  [디버그] 스크린샷 저장: debug_screenshots/{os.path.basename(path)}")

            # 오래된 파일 자동 정리 (최근 keep_last 개만 유지)
            files = sorted(
                (os.path.join(debug_dir, f) for f in os.listdir(debug_dir)
                 if f.lower().endswith(".png")),
                key=os.path.getmtime,
            )
            for old in files[:-keep_last]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"  [디버그] 스크린샷 저장 실패: {e}")

    def click_center(self, box: Tuple[int, ...], save_key: Optional[str] = None):
        x, y, w, h = box[0], box[1], box[2], box[3]
        cx, cy = x + w // 2, y + h // 2
        if save_key:
            self._remember_click(save_key, cx, cy)
        self._click_xy(cx, cy)

    def step_click_image(self, label: str, template_filename: str,
                         *, optional: bool = False,
                         timeout: Optional[float] = None) -> StepResult:
        if self.stop_event.is_set():
            return StepResult(False, msg="중단")
        path = os.path.join(self.images_dir, template_filename)
        region = self._get_region(template_filename)
        region_msg = f" [영역={region}]" if region else " [전체화면]"
        opt_msg = "  (선택)" if optional else ""
        self.log(f"[{label}] 이미지 탐색: {template_filename}{region_msg}{opt_msg}")
        if not os.path.exists(path):
            if optional:
                self.log("  이미지 파일 없음 → 선택 단계라 건너뜀")
                return StepResult(True, msg="skipped")
            return StepResult(False, msg=f"이미지 파일 없음: {template_filename}")
        box = self.wait_image(path, timeout=timeout, region=region)
        if not box:
            if optional:
                self.log("  미발견 → 선택 단계라 건너뜀")
                return StepResult(True, msg="skipped")
            return StepResult(False, msg=f"{label} 이미지 인식 실패")
        self.log(f"  발견 (score={box[4]:.3f})")
        self.click_center(box, save_key=template_filename)
        self._sleep(float(self.config.get("step_delay", 1.0)))
        return StepResult(True, (box[0], box[1]))

    def step_click_image_or_text(self, label: str, template_filename: str,
                                 text: str, *, optional: bool = False,
                                 timeout: Optional[float] = None) -> StepResult:
        """이미지 우선 시도, 실패하면 텍스트 OCR 로 폴백"""
        if self.stop_event.is_set():
            return StepResult(False, msg="중단")
        region = self._get_region(template_filename)
        region_msg = f" [영역={region}]" if region else " [전체화면]"
        opt_msg = "  (선택)" if optional else ""
        self.log(f"[{label}] 이미지+텍스트 탐색: '{text}'{region_msg}{opt_msg}")
        path = os.path.join(self.images_dir, template_filename)
        if os.path.exists(path):
            box = self.find_image(path, region=region)
            if box:
                self.log(f"  이미지 발견 (score={box[4]:.3f})")
                self.click_center(box, save_key=template_filename)
                self._sleep(float(self.config.get("step_delay", 1.0)))
                return StepResult(True, (box[0], box[1]))
            self.log("  이미지 미발견 → 텍스트로 재시도")
        else:
            self.log("  (이미지 파일 없음 → 텍스트만 사용)")

        tbox = self.wait_text(text, timeout=timeout, region=region)
        if not tbox:
            if optional:
                self.log("  미발견 → 선택 단계라 건너뜀")
                return StepResult(True, msg="skipped")
            return StepResult(False, msg=f"{label} 텍스트 '{text}' 인식 실패")
        self.log(f"  텍스트 발견")
        self.click_center(tbox, save_key=template_filename)
        self._sleep(float(self.config.get("step_delay", 1.0)))
        return StepResult(True, (tbox[0], tbox[1]))

    def step_click_text(self, label: str, text: str,
                        region_key: Optional[str] = None) -> StepResult:
        if self.stop_event.is_set():
            return StepResult(False, msg="중단")
        region = self._get_region(region_key) if region_key else None
        self.log(f"[{label}] 텍스트 탐색: '{text}'")
        tbox = self.wait_text(text, region=region)
        if not tbox:
            return StepResult(False, msg=f"{label} 텍스트 '{text}' 인식 실패")
        self.click_center(tbox)
        self._sleep(float(self.config.get("step_delay", 1.0)))
        return StepResult(True, (tbox[0], tbox[1]))

    def _focus_game_window(self, at_xy: Optional[Tuple[int, int]] = None) -> bool:
        """지정 좌표(또는 저장된 시작 버튼 좌표) 위의 윈도우를 강제로 포커스로 가져옴.
        Parsec/원격 스트리밍 환경에서 클릭/키 입력이 게임으로 forward 되려면
        Parsec 창이 foreground 여야 하므로 클릭/키 입력 직전에 호출."""
        if not sys.platform.startswith("win"):
            return False
        if at_xy is None:
            at_xy = self._last_positions.get("08_start.png")
        if not at_xy:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            sx, sy = at_xy
            point = wintypes.POINT(int(sx), int(sy))
            hwnd = user32.WindowFromPoint(point)
            if not hwnd:
                return False
            while True:
                parent = user32.GetParent(hwnd)
                if not parent:
                    break
                hwnd = parent
            fg_hwnd = user32.GetForegroundWindow()
            if fg_hwnd == hwnd:
                return True

            fg_thread = user32.GetWindowThreadProcessId(fg_hwnd, None)
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            current_thread = kernel32.GetCurrentThreadId()

            # 우회 트릭 1: foreground lock timeout 을 0 으로 (이번 호출 동안만)
            # 우회 트릭 2: Alt 키를 한 번 톡 → 매크로 프로세스가 입력 이벤트
            #              발생시킨 것으로 인식돼 SetForegroundWindow 차단 해제.
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            try:
                user32.keybd_event(VK_MENU, 0, 0, 0)
                user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

            user32.AttachThreadInput(current_thread, fg_thread, True)
            user32.AttachThreadInput(current_thread, target_thread, True)
            try:
                # 최소화된 경우에만 RESTORE.
                # 최대화 상태에서 SW_RESTORE 를 부르면 창 크기가 줄어들어
                # 화면 레이아웃이 바뀌고 미리 잡아둔 좌표가 어긋남.
                if user32.IsIconic(hwnd):
                    SW_RESTORE = 9
                    user32.ShowWindow(hwnd, SW_RESTORE)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                # 우회 트릭 3: 미문서 SwitchToThisWindow 도 시도
                # SetForegroundWindow 가 silent fail 했을 때 대비.
                try:
                    user32.SwitchToThisWindow(hwnd, True)
                except Exception:
                    pass
            finally:
                user32.AttachThreadInput(current_thread, target_thread, False)
                user32.AttachThreadInput(current_thread, fg_thread, False)

            time.sleep(0.05)
            return user32.GetForegroundWindow() == hwnd
        except Exception as e:
            self.log(f"  포커스 설정 예외: {e}")
            return False

    def _press_key(self, key: str):
        """키 입력. pydirectinput 우선, keyDown+keyUp 형태로 100ms 홀드."""
        if HAS_PYDIRECTINPUT:
            try:
                pydirectinput.keyDown(key)
                time.sleep(0.1)
                pydirectinput.keyUp(key)
                return
            except Exception as e:
                self.log(f"  pydirectinput 실패 ({e}) → pyautogui 로 시도")
        pyautogui.keyDown(key)
        time.sleep(0.1)
        pyautogui.keyUp(key)

    def _after_dungeon_entry(self):
        """던전 진입 직후: 5~10초 랜덤 대기 → 시작 버튼 미발견 2회 확인 → 지정 키 입력."""
        if self.stop_event.is_set():
            return
        min_d = float(self.config.get("post_entry_min_delay", 5.0))
        max_d = float(self.config.get("post_entry_max_delay", 10.0))
        if max_d < min_d:
            max_d = min_d
        delay = random.uniform(min_d, max_d)
        self.log(f"[던전 진입 후 대기] {delay:.1f}초 (범위 {min_d:.0f}~{max_d:.0f}초)")
        if not self._sleep(delay):
            return

        start_path = os.path.join(self.images_dir, "08_start.png")

        def _start_visible():
            if not os.path.exists(start_path):
                return False
            return self.find_image(start_path) is not None

        if _start_visible():
            self.log("  시작 버튼이 보임 → 던전 진입 미완료로 판단, 키 입력 생략")
            return
        if not self._sleep(1.5):
            return
        if _start_visible():
            self.log("  2차 확인에서 시작 버튼이 보임 → 키 입력 생략")
            return

        gkey = self.config.get("dungeon_key", "g").strip().lower() or "g"
        backend = "pydirectinput" if HAS_PYDIRECTINPUT else "pyautogui"
        focused = self._focus_game_window()
        self.log(f"  게임창 포커스: {'성공' if focused else '실패(직접 입력 시도)'}")
        if not self._sleep(0.2):
            return
        self.log(f"  시작 버튼 미발견 (2회 확인) → 던전 진입 확정, "
                 f"'{gkey}' 키 입력 [{backend}]")
        try:
            self._press_key(gkey)
        except Exception as e:
            self.log(f"  키 입력 실패: {e}")

    def _run_steps(self, steps) -> bool:
        """각 단계 1회 시도. 실패 시 즉시 저장된 좌표로 폴백."""
        for entry in steps:
            if len(entry) == 3:
                label, fn, key = entry
            else:
                label, fn = entry
                key = None
            if self.stop_event.is_set():
                return False
            r = fn()
            if r.ok:
                continue
            saved = self._last_positions.get(key) if key else None
            if saved:
                px, py = saved
                self.log(f"  [폴백] 인식 실패 → 저장된 좌표 ({px}, {py}) 즉시 사용")
                self._click_xy(px, py)
                self._sleep(float(self.config.get("step_delay", 1.0)))
                continue
            self.log(f"  → 실패 (저장 좌표 없음): {r.msg}")
            return False
        return True

    # -------- 전체 시나리오 ---------------------------------------------------
    def run_full_cycle(self) -> bool:
        """1~10 단계 한 번 실행. 성공 시 True."""
        cfg = self.config
        dungeon_map = {
            "snake": ("03_dungeon_snake.png", "맹독의 뱀 둥지"),
            "giant": ("03_dungeon_giant.png", "잊혀진 거인의 동굴"),
            "dwarf": ("03_dungeon_dwarf.png", "난쟁이 왕가의 무덤"),
        }
        selected = cfg.get("dungeon", "snake")
        dungeon_img, dungeon_text = dungeon_map.get(selected, dungeon_map["snake"])
        confirm_text = cfg.get("confirm_text", "확인")
        start_text = cfg.get("start_text", "시작하기")

        steps = [
            ("1. 햄버거 메뉴",
             lambda: self.step_click_image("1. 햄버거 메뉴", "01_hamburger.png"),
             "01_hamburger.png"),
            ("2. 던전 아이콘",
             lambda: self.step_click_image("2. 던전 아이콘", "02_dungeon_icon.png"),
             "02_dungeon_icon.png"),
            (f"3. 던전 선택({dungeon_text})",
             lambda: self.step_click_image_or_text(f"3. 던전 선택", dungeon_img, dungeon_text),
             dungeon_img),
            ("4. 극악",
             lambda: self.step_click_image_or_text("4. 극악", "04_difficulty.png",
                                                   cfg.get("difficulty_text", "극악")),
             "04_difficulty.png"),
            ("5. 비공개 파티",
             lambda: self.step_click_image_or_text("5. 비공개 파티", "05_private.png",
                                                   cfg.get("private_text", "비공개 파티")),
             "05_private.png"),
            ("6. 파티 생성",
             lambda: self.step_click_image_or_text("6. 파티 생성", "06_create_party.png",
                                                   cfg.get("create_text", "파티 생성")),
             "06_create_party.png"),
            ("7. 확인",
             lambda: self.step_click_image_or_text("7. 확인", "07_confirm.png", confirm_text),
             "07_confirm.png"),
            ("8. 시작하기",
             lambda: self.step_click_image_or_text("8. 시작하기", "08_start.png", start_text),
             "08_start.png"),
            ("9. 알림 확인",
             lambda: self.step_click_image_or_text("9. 알림 확인", "09_notice_confirm.png",
                                                   confirm_text, optional=True, timeout=4.0),
             "09_notice_confirm.png"),
            ("10. 티켓 확인",
             lambda: self.step_click_image_or_text("10. 티켓 확인", "10_ticket_confirm.png",
                                                   confirm_text),
             "10_ticket_confirm.png"),
        ]
        return self._run_steps(steps)

    def _click_start_with_verify(self, box, max_attempts: int = 3) -> bool:
        """시작 버튼 클릭 후 알림/티켓 확인 팝업 등장 여부로 적용 검증.
        팝업 미등장 시 시작 버튼 재클릭 (최대 max_attempts회)."""
        notice_path = os.path.join(self.images_dir, "09_notice_confirm.png")
        ticket_path = os.path.join(self.images_dir, "10_ticket_confirm.png")
        start_path = os.path.join(self.images_dir, "08_start.png")

        self.log("[8. 시작하기] 발견 → 클릭")
        self.click_center(box, save_key="08_start.png")
        last_target = box

        strict_thr = max(0.88, float(self.config.get("match_threshold", 0.8)) + 0.05)
        for attempt in range(max_attempts):
            if self.stop_event.is_set():
                return False
            if not self._sleep(2.0):
                return False
            if os.path.exists(notice_path):
                check = self.find_image(notice_path, threshold=strict_thr,
                                        debug_label=f"알림팝업검증(시도{attempt+1}/{max_attempts})")
                if check:
                    self.log(f"  ✓ 알림 확인 팝업 발견 (score={check[4]:.3f}) "
                             f"→ 시작 버튼 적용 확인")
                    return True
            if os.path.exists(ticket_path):
                check = self.find_image(ticket_path, threshold=strict_thr,
                                        debug_label=f"티켓팝업검증(시도{attempt+1}/{max_attempts})")
                if check:
                    self.log(f"  ✓ 티켓 확인 팝업 발견 (score={check[4]:.3f}) "
                             f"→ 시작 버튼 적용 확인")
                    return True
            if attempt < max_attempts - 1:
                self.log(f"  팝업 미확인 → 시작 버튼 재클릭 "
                         f"({attempt+2}/{max_attempts})")
                new_box = self.find_image(
                    start_path,
                    debug_label=f"재클릭전 시작버튼 재탐색(시도{attempt+2}/{max_attempts})",
                ) if os.path.exists(start_path) else None
                target = new_box if new_box else box
                self.click_center(target, save_key="08_start.png")
                last_target = target
        self.log(f"  {max_attempts}회 시도해도 팝업 미확인 → 시작 버튼 클릭 실패로 판단")
        # 클릭 위치를 표시한 스크린샷 저장 → 화면에 실제로 무엇이 떠 있는지 확인용
        try:
            cx = int(last_target[0]) + int(last_target[2]) // 2
            cy = int(last_target[1]) + int(last_target[3]) // 2
            self._save_debug_screenshot("click_verify_fail", click_xy=(cx, cy))
        except Exception:
            pass
        return False

    def _poll_for_start_button(self, poll_interval: float):
        """시작 버튼 폴링. N회 연속 미발견 시 저장 좌표 폴백 + 팝업 검증.
        반환:
          ("box", box) - 정상 발견 (호출자가 클릭/검증 수행)
          ("clicked", None) - 폴백 클릭 + 팝업 확인 완료 (호출자가 클릭 생략)
          None - 중단"""
        key = "08_start.png"
        path = os.path.join(self.images_dir, key)
        start_text = self.config.get("start_text", "시작하기")
        fallback_after = max(1, int(self.config.get("start_polls_before_fallback", 2)))
        saved = self._last_positions.get(key)
        notice_path = os.path.join(self.images_dir, "09_notice_confirm.png")
        ticket_path = os.path.join(self.images_dir, "10_ticket_confirm.png")

        fallback_used = False
        consecutive_miss = 0
        i = 0
        next_log_time = time.time()
        while not self.stop_event.is_set():
            i += 1
            if os.path.exists(path):
                # 매 폴링마다 매칭 실패 점수까지 로그 (시작 버튼 못 찾는 원인 추적)
                box = self.find_image(path, debug_label=f"시작버튼 폴링#{i}")
                if box:
                    self.log(f"  [폴링 #{i}] 시작 버튼 이미지 발견 (score={box[4]:.3f})")
                    return ("box", box)
            if i % 3 == 0:
                tbox = self.find_text(start_text)
                if tbox:
                    self.log(f"  [폴링 #{i}] 시작 버튼 텍스트 발견")
                    return ("box", (tbox[0], tbox[1], tbox[2], tbox[3], 0.0))
            consecutive_miss += 1
            if (not fallback_used) and saved and consecutive_miss >= fallback_after:
                sx, sy = saved
                self.log(f"  [폴링 #{i}] {consecutive_miss}회 연속 미발견 "
                         f"→ 저장 좌표 ({sx}, {sy}) 클릭 시도")
                self._click_xy(sx, sy)
                fallback_used = True
                if not self._sleep(2.5):
                    return None
                strict_thr = max(0.88, float(self.config.get("match_threshold", 0.8)) + 0.05)
                popup_ok = False
                for p in (notice_path, ticket_path):
                    if os.path.exists(p):
                        chk = self.find_image(
                            p, threshold=strict_thr,
                            debug_label=f"폴백후 팝업검증({os.path.basename(p)})",
                        )
                        if chk:
                            self.log(f"  ✓ 팝업 발견 (score={chk[4]:.3f}, "
                                     f"엄격 임계값={strict_thr:.2f}) → 폴백 클릭 성공")
                            popup_ok = True
                            break
                if popup_ok:
                    return ("clicked", None)
                self.log(f"  팝업 미확인 (엄격 임계값 {strict_thr:.2f} 미달) "
                         f"→ 폴백 클릭 무효. 폴링 계속.")
                self._save_debug_screenshot("fallback_verify_fail",
                                            click_xy=(sx, sy))
                consecutive_miss = 0
            now = time.time()
            if now >= next_log_time:
                self.log(f"  [폴링 #{i}] 시작 버튼 미발견 → {poll_interval:.0f}초 후 재확인")
                next_log_time = now + 60.0
            if not self._sleep(poll_interval):
                return None
        return None

    def run_restart_cycle(self) -> bool:
        """반복 루프용: 8~10 단계만 실행 (시작 버튼 재출현 후)"""
        cfg = self.config
        confirm_text = cfg.get("confirm_text", "확인")
        start_text = cfg.get("start_text", "시작하기")
        steps = [
            ("8. 시작하기",
             lambda: self.step_click_image_or_text("8. 시작하기", "08_start.png", start_text),
             "08_start.png"),
            ("9. 알림 확인",
             lambda: self.step_click_image_or_text("9. 알림 확인", "09_notice_confirm.png",
                                                   confirm_text, optional=True, timeout=4.0),
             "09_notice_confirm.png"),
            ("10. 티켓 확인",
             lambda: self.step_click_image_or_text("10. 티켓 확인", "10_ticket_confirm.png",
                                                   confirm_text),
             "10_ticket_confirm.png"),
        ]
        return self._run_steps(steps)

    def _set_keep_awake(self, enable: bool):
        """매크로 실행 중 디스플레이/시스템이 절전으로 가지 않도록 유지."""
        if not sys.platform.startswith("win"):
            return
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            flags = ES_CONTINUOUS
            if enable:
                flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
        except Exception:
            pass

    def run_loop(self):
        """시나리오 1회 후 '시작 버튼 재인식 → 8~10 반복'. 반복 횟수에 도달하면 종료"""
        self.reset()
        self._set_keep_awake(True)
        try:
            self._run_loop_inner()
        finally:
            self._set_keep_awake(False)

    def _run_loop_inner(self):
        self.log("=" * 50)
        self.log("python_study 시작")
        self.log("=" * 50)

        max_cycles = int(self.config.get("max_cycles", 0))
        cycle = 0

        if not self.run_full_cycle():
            self.log("첫 사이클 실패 → 종료")
            return
        cycle += 1
        self.log(f"[진행] 던전 진입 {cycle}/"
                 f"{max_cycles if max_cycles > 0 else '∞'} 회 완료")
        self._after_dungeon_entry()

        if max_cycles > 0 and cycle >= max_cycles:
            self.log(f"목표 {max_cycles}회 달성 → 종료")
            return

        poll_interval = float(self.config.get("start_poll_interval", 10.0))
        cfg = self.config
        confirm_text = cfg.get("confirm_text", "확인")

        while not self.stop_event.is_set():
            self.log(f"[대기] 던전 종료 후 시작 버튼 폴링 ({poll_interval:.0f}초 간격)")
            result = self._poll_for_start_button(poll_interval)
            if result is None:
                break
            mode, payload = result
            if mode == "box":
                if not self._click_start_with_verify(payload):
                    self.log("→ 시작 버튼 클릭이 실제로 적용되지 않음. 다시 폴링.")
                    continue
            else:
                self.log("[8. 시작하기] 저장 좌표 폴백 클릭 + 팝업 확인 완료")
            self._sleep(float(cfg.get("step_delay", 1.0)))

            steps = [
                ("9. 알림 확인",
                 lambda: self.step_click_image_or_text(
                     "9. 알림 확인", "09_notice_confirm.png",
                     confirm_text, optional=False, timeout=8.0),
                 "09_notice_confirm.png"),
                ("10. 티켓 확인",
                 lambda: self.step_click_image_or_text(
                     "10. 티켓 확인", "10_ticket_confirm.png",
                     confirm_text, timeout=8.0),
                 "10_ticket_confirm.png"),
            ]
            if not self._run_steps(steps):
                self.log("반복 사이클 실패 → 종료")
                break
            cycle += 1
            self.log(f"[진행] 던전 진입 {cycle}/"
                     f"{max_cycles if max_cycles > 0 else '∞'} 회 완료")
            self._after_dungeon_entry()
            if max_cycles > 0 and cycle >= max_cycles:
                self.log(f"목표 {max_cycles}회 달성 → 종료")
                break
        self.log("python_study 종료")
