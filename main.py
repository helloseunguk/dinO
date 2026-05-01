"""
python_study
GUI: tkinter
"""
import os
import sys
import json
import time
import shutil
import threading

if sys.platform.startswith("win"):
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

from macro import MacroEngine, HAS_TESSERACT


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(PROJECT_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)


# 각 단계별 이미지 슬롯 정의: (표시명, 저장 파일명, 설명)
IMAGE_SLOTS = [
    ("1. 햄버거 메뉴",        "01_hamburger.png",        "우측 상단 햄버거 버튼"),
    ("2. 던전 아이콘",        "02_dungeon_icon.png",     "던전 메뉴 아이콘"),
    ("3-A. 맹독의 뱀 둥지",   "03_dungeon_snake.png",    "던전: 맹독의 뱀 둥지"),
    ("3-B. 잊혀진 거인의 동굴","03_dungeon_giant.png",   "던전: 잊혀진 거인의 동굴"),
    ("3-C. 난쟁이 왕가의 무덤","03_dungeon_dwarf.png",   "던전: 난쟁이 왕가의 무덤"),
    ("4. 극악 (선택)",        "04_difficulty.png",       "난이도 '극악' 버튼 (비우면 OCR만)"),
    ("5. 비공개 파티 (선택)", "05_private.png",           "'비공개 파티' 버튼 (비우면 OCR만)"),
    ("6. 파티 생성",          "06_create_party.png",      "파티 생성 버튼"),
    ("7. 확인",               "07_confirm.png",           "확인 버튼"),
    ("8. 시작하기",           "08_start.png",             "시작하기 버튼 (반복 기준점)"),
    ("9. 알림 확인",          "09_notice_confirm.png",    "알림 팝업의 확인 버튼"),
    ("10. 티켓 확인",         "10_ticket_confirm.png",    "티켓 소모 팝업의 확인 버튼"),
]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("python_study")
        self.geometry("780x780")
        self.minsize(700, 600)

        self.engine = MacroEngine(PROJECT_DIR, log_fn=self._log)
        self.worker: threading.Thread | None = None

        self._thumb_cache: dict[str, ImageTk.PhotoImage] = {}

        self._build_ui()
        self._load_from_config()
        self._refresh_all_thumbs()

    # ------------------------------------------------------------------- UI --
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_run = ttk.Frame(nb)
        self.tab_img = ttk.Frame(nb)
        self.tab_cfg = ttk.Frame(nb)
        nb.add(self.tab_run, text="실행")
        nb.add(self.tab_img, text="이미지 등록")
        nb.add(self.tab_cfg, text="설정")

        self._build_run_tab()
        self._build_img_tab()
        self._build_cfg_tab()

    def _build_run_tab(self):
        f = self.tab_run

        mon_frame = ttk.LabelFrame(f, text="탐색 대상 디스플레이")
        mon_frame.pack(fill="x", padx=8, pady=8)
        self.var_monitor = tk.IntVar(value=1)
        self._monitor_choices: list[tuple[int, str]] = []
        self.monitor_combo = ttk.Combobox(mon_frame, state="readonly", width=55)
        self.monitor_combo.pack(side="left", padx=8, pady=6)
        self.monitor_combo.bind("<<ComboboxSelected>>", self._on_monitor_selected)
        ttk.Button(mon_frame, text="새로고침",
                   command=self._refresh_monitors).pack(side="left", padx=4)
        self._refresh_monitors()

        top = ttk.LabelFrame(f, text="던전 선택")
        top.pack(fill="x", padx=8, pady=8)

        self.var_dungeon = tk.StringVar(value="snake")
        for val, text in [
            ("snake", "맹독의 뱀 둥지"),
            ("giant", "잊혀진 거인의 동굴"),
            ("dwarf", "난쟁이 왕가의 무덤"),
        ]:
            ttk.Radiobutton(top, text=text, value=val,
                            variable=self.var_dungeon,
                            command=self._save_quick).pack(side="left", padx=10, pady=6)

        rep = ttk.LabelFrame(f, text="반복 설정")
        rep.pack(fill="x", padx=8, pady=4)
        self.var_max_cycles = tk.IntVar(value=10)
        self.var_post_min = tk.DoubleVar(value=5.0)
        self.var_post_max = tk.DoubleVar(value=10.0)
        self.var_dungeon_key = tk.StringVar(value="g")
        ttk.Label(rep, text="던전 반복 횟수 (0 = 무제한)").grid(
            row=0, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(rep, textvariable=self.var_max_cycles, from_=0, to=9999,
                    increment=1, width=8,
                    command=self._save_quick).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(rep, text="진입 후 최소 대기 (초)").grid(
            row=1, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(rep, textvariable=self.var_post_min, from_=0.0, to=120.0,
                    increment=0.5, width=8).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(rep, text="진입 후 최대 대기 (초)").grid(
            row=2, column=0, sticky="w", padx=8, pady=3)
        ttk.Spinbox(rep, textvariable=self.var_post_max, from_=0.0, to=120.0,
                    increment=0.5, width=8).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(rep, text="누를 키").grid(
            row=3, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(rep, textvariable=self.var_dungeon_key, width=10).grid(
            row=3, column=1, sticky="w", padx=8)

        diff = ttk.LabelFrame(f, text="텍스트 항목 (OCR 인식 대상)")
        diff.pack(fill="x", padx=8, pady=4)
        self.var_difficulty = tk.StringVar(value="극악")
        self.var_private = tk.StringVar(value="비공개 파티")
        self.var_create = tk.StringVar(value="파티 생성")
        self.var_confirm = tk.StringVar(value="확인")
        self.var_start = tk.StringVar(value="시작하기")
        for row, (label, var) in enumerate([
            ("4. 난이도 텍스트",   self.var_difficulty),
            ("5. 비공개 파티 텍스트", self.var_private),
            ("6. 파티생성 텍스트", self.var_create),
            ("7. 확인 텍스트 (7/9/10번 공용)", self.var_confirm),
            ("8. 시작하기 텍스트", self.var_start),
        ]):
            ttk.Label(diff, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=2)
            e = ttk.Entry(diff, textvariable=var, width=30)
            e.grid(row=row, column=1, sticky="w", padx=8, pady=2)

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=8, pady=8)
        self.btn_start = ttk.Button(btns, text="▶ 시작", command=self.on_start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(btns, text="■ 정지", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_test = ttk.Button(btns, text="1회만 실행 (반복X)", command=self.on_once)
        self.btn_test.pack(side="left", padx=4)

        ttk.Label(f, text="로그", anchor="w").pack(fill="x", padx=8)
        self.txt_log = tk.Text(f, height=18)
        self.txt_log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.txt_log.configure(state="disabled")

        ttk.Label(
            f,
            text="※ 마우스를 화면 좌상단(0,0)으로 이동하면 PyAutoGUI 안전장치로 즉시 중단됩니다.",
            foreground="#666",
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _build_img_tab(self):
        f = self.tab_img

        info = ttk.Label(
            f,
            text=("각 단계별 참조 이미지를 등록하세요. 이미지는 images/ 폴더에 저장되어 "
                  "프로그램을 닫아도 유지됩니다.\n"
                  "게임 화면에서 버튼 영역만 잘라낸 PNG 가 가장 잘 인식됩니다."),
            foreground="#444",
            justify="left",
        )
        info.pack(fill="x", padx=8, pady=8)

        canvas = tk.Canvas(f, highlightthickness=0)
        scroll = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        scroll.pack(side="right", fill="y", pady=4)

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(_evt):
            canvas.itemconfig(inner_id, width=canvas.winfo_width())
            canvas.configure(scrollregion=canvas.bbox("all"))

        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)

        self._thumb_labels: dict[str, ttk.Label] = {}
        self._status_labels: dict[str, ttk.Label] = {}

        for i, (title, filename, desc) in enumerate(IMAGE_SLOTS):
            row = ttk.LabelFrame(inner, text=title)
            row.pack(fill="x", padx=8, pady=4)

            thumb = ttk.Label(row, text="(이미지 없음)", width=18, anchor="center",
                              relief="sunken")
            thumb.grid(row=0, column=0, rowspan=3, padx=8, pady=8)
            self._thumb_labels[filename] = thumb

            ttk.Label(row, text=desc, foreground="#555").grid(
                row=0, column=1, sticky="w", padx=4, pady=(6, 0))
            status = ttk.Label(row, text="", foreground="#2a7")
            status.grid(row=1, column=1, sticky="w", padx=4)
            self._status_labels[filename] = status

            btns = ttk.Frame(row)
            btns.grid(row=2, column=1, sticky="w", padx=4, pady=(0, 6))
            ttk.Button(btns, text="파일 선택",
                       command=lambda fn=filename: self.on_pick_image(fn)
                       ).pack(side="left", padx=2)
            ttk.Button(btns, text="폴더 열기",
                       command=self.on_open_images_dir).pack(side="left", padx=2)
            ttk.Button(btns, text="삭제",
                       command=lambda fn=filename: self.on_delete_image(fn)
                       ).pack(side="left", padx=2)

            row.grid_columnconfigure(1, weight=1)

    def _build_cfg_tab(self):
        f = self.tab_cfg

        frm = ttk.LabelFrame(f, text="일반 설정")
        frm.pack(fill="x", padx=8, pady=8)

        self.var_threshold = tk.DoubleVar(value=0.8)
        self.var_delay = tk.DoubleVar(value=1.0)
        self.var_poll = tk.DoubleVar(value=10.0)
        self.var_timeout = tk.DoubleVar(value=15.0)
        self.var_ocr_lang = tk.StringVar(value="kor+eng")
        self.var_tess_path = tk.StringVar(value="")
        self.var_jitter = tk.IntVar(value=3)
        self.var_poll_fallback = tk.IntVar(value=2)

        rows = [
            ("이미지 매칭 임계값 (0~1)", self.var_threshold, 0.5, 1.0, 0.05),
            ("단계 간 대기 (초)",         self.var_delay, 0.1, 5.0, 0.1),
            ("시작 버튼 폴링 간격 (초)",  self.var_poll, 1.0, 60.0, 1.0),
            ("폴링 N회 연속 실패 시 좌표 폴백",  self.var_poll_fallback, 1, 20, 1),
            ("이미지/텍스트 탐색 제한 (초)", self.var_timeout, 1.0, 60.0, 1.0),
            ("클릭 지터 (±px, 0=중앙 고정)", self.var_jitter, 0, 20, 1),
        ]
        for i, (label, var, frm_, to_, inc) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=3)
            ttk.Spinbox(frm, textvariable=var, from_=frm_, to=to_,
                        increment=inc, width=10).grid(row=i, column=1, sticky="w", padx=8)

        ttk.Label(frm, text="OCR 언어").grid(row=len(rows), column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(frm, textvariable=self.var_ocr_lang, width=15).grid(
            row=len(rows), column=1, sticky="w", padx=8)

        r = len(rows) + 1
        ttk.Label(frm, text="Tesseract 경로 (비우면 자동탐지)").grid(
            row=r, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(frm, textvariable=self.var_tess_path, width=40).grid(
            row=r, column=1, sticky="w", padx=8)
        ttk.Button(frm, text="찾기...", command=self._pick_tesseract).grid(
            row=r, column=2, padx=4)

        save = ttk.Button(f, text="설정 저장", command=self._save_from_ui)
        save.pack(anchor="w", padx=8, pady=8)

        if not HAS_TESSERACT:
            ttk.Label(
                f,
                text="[경고] pytesseract 가 설치되지 않아 텍스트 인식이 비활성화됩니다.\n"
                     "  pip install pytesseract  및 Tesseract-OCR 설치 필요 (한국어 언어팩 포함)",
                foreground="#c33",
                justify="left",
            ).pack(fill="x", padx=8, pady=8)

    # -------------------------------------------------------- config <-> UI --
    def _load_from_config(self):
        c = self.engine.config
        self.var_dungeon.set(c.get("dungeon", "snake"))
        self.var_difficulty.set(c.get("difficulty_text", "극악"))
        self.var_private.set(c.get("private_text", "비공개 파티"))
        self.var_create.set(c.get("create_text", "파티 생성"))
        self.var_confirm.set(c.get("confirm_text", "확인"))
        self.var_start.set(c.get("start_text", "시작하기"))
        self.var_threshold.set(float(c.get("match_threshold", 0.8)))
        self.var_delay.set(float(c.get("step_delay", 1.0)))
        self.var_poll.set(float(c.get("start_poll_interval", 10)))
        self.var_timeout.set(float(c.get("search_timeout", 15)))
        self.var_ocr_lang.set(c.get("ocr_lang", "kor+eng"))
        self.var_tess_path.set(c.get("tesseract_path", ""))
        self.var_monitor.set(int(c.get("monitor_index", 1)))
        self.var_max_cycles.set(int(c.get("max_cycles", 10)))
        self.var_post_min.set(float(c.get("post_entry_min_delay", 5.0)))
        self.var_post_max.set(float(c.get("post_entry_max_delay", 10.0)))
        self.var_dungeon_key.set(c.get("dungeon_key", "g"))
        self.var_jitter.set(int(c.get("click_jitter_px", 3)))
        self.var_poll_fallback.set(int(c.get("start_polls_before_fallback", 2)))

    def _collect_config(self) -> dict:
        return {
            "dungeon": self.var_dungeon.get(),
            "difficulty_text": self.var_difficulty.get().strip() or "극악",
            "private_text": self.var_private.get().strip() or "비공개 파티",
            "create_text": self.var_create.get().strip() or "파티 생성",
            "confirm_text": self.var_confirm.get().strip() or "확인",
            "start_text": self.var_start.get().strip() or "시작하기",
            "match_threshold": float(self.var_threshold.get()),
            "step_delay": float(self.var_delay.get()),
            "start_poll_interval": float(self.var_poll.get()),
            "search_timeout": float(self.var_timeout.get()),
            "ocr_lang": self.var_ocr_lang.get().strip() or "kor+eng",
            "tesseract_path": self.var_tess_path.get().strip(),
            "monitor_index": int(self.var_monitor.get()),
            "max_cycles": int(self.var_max_cycles.get()),
            "post_entry_min_delay": float(self.var_post_min.get()),
            "post_entry_max_delay": float(self.var_post_max.get()),
            "dungeon_key": self.var_dungeon_key.get().strip().lower() or "g",
            "click_jitter_px": int(self.var_jitter.get()),
            "start_polls_before_fallback": int(self.var_poll_fallback.get()),
        }

    def _save_quick(self):
        cfg = self.engine.config.copy()
        cfg["dungeon"] = self.var_dungeon.get()
        try:
            cfg["max_cycles"] = int(self.var_max_cycles.get())
        except (tk.TclError, ValueError):
            pass
        self.engine.save_config(cfg)

    def _save_from_ui(self):
        cfg = self._collect_config()
        self.engine.save_config(cfg)
        self.engine._apply_tesseract_path()
        messagebox.showinfo("저장", "설정이 저장되었습니다.")

    def _pick_tesseract(self):
        p = filedialog.askopenfilename(
            title="tesseract.exe 선택",
            filetypes=[("tesseract.exe", "tesseract.exe"), ("실행파일", "*.exe")],
        )
        if p:
            self.var_tess_path.set(p)

    # ------------------------------------------------------------- images --
    def on_pick_image(self, filename: str):
        p = filedialog.askopenfilename(
            title=f"{filename} 으로 저장할 이미지 선택",
            filetypes=[("이미지", "*.png *.jpg *.jpeg *.bmp"), ("모든 파일", "*.*")],
        )
        if not p:
            return
        target = os.path.join(IMAGES_DIR, filename)
        try:
            img = Image.open(p).convert("RGB")
            img.save(target, format="PNG")
        except Exception as e:
            messagebox.showerror("오류", f"이미지 저장 실패: {e}")
            return
        self._refresh_thumb(filename)

    def on_delete_image(self, filename: str):
        target = os.path.join(IMAGES_DIR, filename)
        if os.path.exists(target):
            os.remove(target)
        self._refresh_thumb(filename)

    def on_open_images_dir(self):
        if sys.platform.startswith("win"):
            os.startfile(IMAGES_DIR)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", IMAGES_DIR])

    # ------------------------------------------------------------ monitor --
    def _refresh_monitors(self):
        mons = MacroEngine.list_monitors()
        self._monitor_choices = []
        values = []
        for idx, w, h, left, top in mons:
            label = f"디스플레이 {idx}  —  {w}x{h}  @ ({left}, {top})"
            self._monitor_choices.append((idx, label))
            values.append(label)
        self.monitor_combo["values"] = values
        current = int(self.engine.config.get("monitor_index", 1))
        sel_i = 0
        for i, (idx, _) in enumerate(self._monitor_choices):
            if idx == current:
                sel_i = i
                break
        if values:
            self.monitor_combo.current(sel_i)
            self.var_monitor.set(self._monitor_choices[sel_i][0])

    def _on_monitor_selected(self, _evt=None):
        sel = self.monitor_combo.current()
        if sel < 0 or sel >= len(self._monitor_choices):
            return
        idx = self._monitor_choices[sel][0]
        self.var_monitor.set(idx)
        cfg = self.engine.config.copy()
        cfg["monitor_index"] = int(idx)
        self.engine.save_config(cfg)

    def _refresh_all_thumbs(self):
        for _, fn, _ in IMAGE_SLOTS:
            self._refresh_thumb(fn)

    def _refresh_thumb(self, filename: str):
        path = os.path.join(IMAGES_DIR, filename)
        lbl = self._thumb_labels.get(filename)
        status = self._status_labels.get(filename)
        if lbl is None:
            return
        if not os.path.exists(path):
            lbl.configure(image="", text="(이미지 없음)")
            self._thumb_cache.pop(filename, None)
            if status is not None:
                status.configure(text="미등록", foreground="#a33")
            return
        try:
            img = Image.open(path)
            img.thumbnail((120, 80))
            photo = ImageTk.PhotoImage(img)
            self._thumb_cache[filename] = photo
            lbl.configure(image=photo, text="")
            if status is not None:
                status.configure(text=f"등록됨  ({os.path.getsize(path)} bytes)",
                                 foreground="#2a7")
        except Exception as e:
            lbl.configure(image="", text=f"(오류)")
            if status is not None:
                status.configure(text=str(e), foreground="#a33")

    # --------------------------------------------------------------- run --
    def on_start(self):
        if self.worker and self.worker.is_alive():
            return
        self.engine.save_config(self._collect_config())
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()
        self.after(200, self.iconify)

    def on_once(self):
        if self.worker and self.worker.is_alive():
            return
        self.engine.save_config(self._collect_config())
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_once, daemon=True)
        self.worker.start()
        self.after(200, self.iconify)

    def on_stop(self):
        self.engine.stop()
        self._log("[정지 요청]")

    def _run_loop(self):
        try:
            self.engine.run_loop()
        except Exception as e:
            self._log(f"[예외] {e}")
        finally:
            self.after(0, lambda: self._set_running(False))

    def _run_once(self):
        try:
            self.engine.reset()
            self.engine.run_full_cycle()
        except Exception as e:
            self._log(f"[예외] {e}")
        finally:
            self.after(0, lambda: self._set_running(False))

    def _set_running(self, running: bool):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_test.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _log(self, msg: str):
        def _append():
            self.txt_log.configure(state="normal")
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")
            self.txt_log.configure(state="disabled")
        try:
            self.after(0, _append)
        except RuntimeError:
            print(msg)


if __name__ == "__main__":
    app = App()
    app.mainloop()
