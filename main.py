"""
python_study
GUI: tkinter + sv-ttk (Sun Valley) 다크 테마
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
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

try:
    import sv_ttk
    HAS_SV_TTK = True
except ImportError:
    HAS_SV_TTK = False

from macro import MacroEngine, HAS_TESSERACT, DIFFICULTY_MAP


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(PROJECT_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)


# 난이도 라디오 옵션 (key, 표시명) — DIFFICULTY_MAP 의 표시명 그대로 사용
DIFFICULTY_OPTIONS = [(k, v[1]) for k, v in DIFFICULTY_MAP.items()]


def _migrate_old_difficulty_image():
    """구 버전 04_difficulty.png 가 있고 04_difficulty_extreme.png 가 없으면
    극악으로 등록돼 있던 걸로 간주해 자동 복사 (사용자가 다시 등록 안 하도록)."""
    old = os.path.join(IMAGES_DIR, "04_difficulty.png")
    new_extreme = os.path.join(IMAGES_DIR, "04_difficulty_extreme.png")
    if os.path.exists(old) and not os.path.exists(new_extreme):
        try:
            shutil.copy2(old, new_extreme)
        except Exception:
            pass


_migrate_old_difficulty_image()


# 각 단계별 이미지 슬롯 정의: (표시명, 저장 파일명, 설명)
IMAGE_SLOTS = [
    ("1. 햄버거 메뉴",        "01_hamburger.png",        "우측 상단 햄버거 버튼"),
    ("2. 던전 아이콘",        "02_dungeon_icon.png",     "던전 메뉴 아이콘"),
    ("3-A. 맹독의 뱀 둥지",   "03_dungeon_snake.png",    "던전: 맹독의 뱀 둥지"),
    ("3-B. 잊혀진 거인의 동굴", "03_dungeon_giant.png",   "던전: 잊혀진 거인의 동굴"),
    ("3-C. 난쟁이 왕가의 무덤", "03_dungeon_dwarf.png",   "던전: 난쟁이 왕가의 무덤"),
    ("4-A. 보통 난이도",      "04_difficulty_normal.png",    "난이도 '보통' 버튼"),
    ("4-B. 어려움 난이도",    "04_difficulty_hard.png",      "난이도 '어려움' 버튼"),
    ("4-C. 매우 어려움 난이도", "04_difficulty_very_hard.png", "난이도 '매우 어려움' 버튼"),
    ("4-D. 극악 난이도",      "04_difficulty_extreme.png",   "난이도 '극악' 버튼"),
    ("5. 비공개 파티 (선택)", "05_private.png",           "'비공개 파티' 버튼 (비우면 OCR만)"),
    ("6. 파티 생성",          "06_create_party.png",      "파티 생성 버튼"),
    ("7. 확인",               "07_confirm.png",           "확인 버튼"),
    ("8. 시작하기",           "08_start.png",             "시작하기 버튼 (반복 기준점)"),
    ("9. 알림 확인",          "09_notice_confirm.png",    "알림 팝업의 확인 버튼"),
    ("10. 티켓 확인",         "10_ticket_confirm.png",    "티켓 소모 팝업의 확인 버튼"),
]


# 색상 팔레트 (다크 테마 보조용)
ACCENT = "#4cc2ff"        # 파란 강조 (실행 중)
SUCCESS = "#4ade80"       # 초록 (성공)
DANGER = "#f87171"        # 빨강 (위험/중단)
WARN = "#fbbf24"          # 노랑 (경고)
MUTED = "#9ca3af"         # 회색 텍스트
LOG_BG = "#1a1a1a"        # 로그창 배경
LOG_FG = "#e5e5e5"        # 로그 기본 글자
LOG_DIM = "#6b7280"       # 로그 부가설명


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("python_study")
        self.geometry("960x880")
        self.minsize(820, 700)

        if HAS_SV_TTK:
            sv_ttk.set_theme("dark")
        # 커스텀 스타일들
        self._setup_styles()

        # 백그라운드/상태
        self.engine = MacroEngine(PROJECT_DIR, log_fn=self._log)
        self.worker: threading.Thread | None = None
        self._cycle_count = 0
        self._running_state = "idle"  # idle / running / stopping

        self._thumb_cache: dict[str, ImageTk.PhotoImage] = {}

        self._build_ui()
        self._load_from_config()
        self._refresh_all_thumbs()
        self._update_header_status()

    # ------------------------------------------------------------------ style --
    def _setup_styles(self):
        st = ttk.Style(self)

        # 폰트 통일
        base_family = "Segoe UI" if sys.platform.startswith("win") else "TkDefaultFont"
        self._fonts = {
            "title": tkfont.Font(family=base_family, size=18, weight="bold"),
            "subtitle": tkfont.Font(family=base_family, size=11),
            "body": tkfont.Font(family=base_family, size=10),
            "small": tkfont.Font(family=base_family, size=9),
            "btn": tkfont.Font(family=base_family, size=11, weight="bold"),
            "log": tkfont.Font(family="Consolas", size=10) if sys.platform.startswith("win")
                    else tkfont.Font(family="Monaco", size=10),
            "section": tkfont.Font(family=base_family, size=11, weight="bold"),
        }

        # 강조 버튼들
        st.configure("Big.Accent.TButton", font=self._fonts["btn"], padding=(20, 10))
        st.configure("Big.TButton", font=self._fonts["btn"], padding=(20, 10))
        st.configure("Section.TLabel", font=self._fonts["section"])
        st.configure("Title.TLabel", font=self._fonts["title"])
        st.configure("Subtitle.TLabel", font=self._fonts["subtitle"], foreground=MUTED)
        st.configure("Status.TLabel", font=self._fonts["body"])
        st.configure("Mini.TLabel", font=self._fonts["small"], foreground=MUTED)
        st.configure("Card.TLabelframe", padding=(14, 10))
        st.configure("Card.TLabelframe.Label", font=self._fonts["section"])

    # ------------------------------------------------------------------- UI --
    def _build_ui(self):
        # 헤더
        header = ttk.Frame(self, padding=(20, 16, 20, 8))
        header.pack(fill="x")
        ttk.Label(header, text="python_study", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  ·  자동화 도구", style="Subtitle.TLabel").pack(side="left")

        # 헤더 우측 상태 인디케이터
        right = ttk.Frame(header)
        right.pack(side="right")
        self.status_dot = tk.Canvas(right, width=14, height=14, highlightthickness=0,
                                    bg=self._get_bg())
        self.status_dot.pack(side="left", padx=(0, 6))
        self._status_dot_id = self.status_dot.create_oval(2, 2, 12, 12,
                                                          fill=MUTED, outline="")
        self.status_text = ttk.Label(right, text="준비됨", style="Status.TLabel")
        self.status_text.pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=20)

        # 노트북
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=20, pady=(12, 0))

        self.tab_run = ttk.Frame(nb, padding=12)
        self.tab_img = ttk.Frame(nb, padding=12)
        self.tab_cfg = ttk.Frame(nb, padding=12)
        nb.add(self.tab_run, text="  실행  ")
        nb.add(self.tab_img, text="  이미지 등록  ")
        nb.add(self.tab_cfg, text="  설정  ")

        self._build_run_tab()
        self._build_img_tab()
        self._build_cfg_tab()

        # 하단 상태바
        bar = ttk.Frame(self, padding=(20, 8, 20, 10))
        bar.pack(fill="x", side="bottom")
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=20, side="bottom")
        self.statusbar_left = ttk.Label(
            bar, text="● 대기 중", style="Mini.TLabel")
        self.statusbar_left.pack(side="left")
        self.statusbar_right = ttk.Label(
            bar,
            text="※ 마우스 좌상단 (0,0) 이동 시 PyAutoGUI 안전장치로 즉시 중단",
            style="Mini.TLabel")
        self.statusbar_right.pack(side="right")

    def _get_bg(self) -> str:
        """현재 테마의 배경색 (status_dot Canvas 배경 매칭용)."""
        try:
            return ttk.Style().lookup("TFrame", "background") or "#202020"
        except Exception:
            return "#202020"

    # ------------------------------------------------------------ run tab --
    def _build_run_tab(self):
        f = self.tab_run

        # 좌우 2단 레이아웃
        top_row = ttk.Frame(f)
        top_row.pack(fill="x", pady=(0, 10))

        # 디스플레이
        mon_frame = ttk.LabelFrame(top_row, text="탐색 대상 디스플레이",
                                    style="Card.TLabelframe")
        mon_frame.pack(fill="x")
        self.var_monitor = tk.IntVar(value=1)
        self._monitor_choices: list[tuple[int, str]] = []
        self.monitor_combo = ttk.Combobox(mon_frame, state="readonly", width=55)
        self.monitor_combo.pack(side="left", padx=(0, 8), pady=4, fill="x", expand=True)
        self.monitor_combo.bind("<<ComboboxSelected>>", self._on_monitor_selected)
        ttk.Button(mon_frame, text="새로고침",
                   command=self._refresh_monitors).pack(side="left")
        self._refresh_monitors()

        # 던전 + 난이도 (좌우 분할)
        sel_row = ttk.Frame(f)
        sel_row.pack(fill="x", pady=(0, 10))
        sel_row.grid_columnconfigure(0, weight=1)
        sel_row.grid_columnconfigure(1, weight=1)

        top = ttk.LabelFrame(sel_row, text="던전 선택", style="Card.TLabelframe")
        top.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.var_dungeon = tk.StringVar(value="snake")
        for val, text in [
            ("snake", "맹독의 뱀 둥지"),
            ("giant", "잊혀진 거인의 동굴"),
            ("dwarf", "난쟁이 왕가의 무덤"),
        ]:
            ttk.Radiobutton(top, text=text, value=val,
                            variable=self.var_dungeon,
                            command=self._save_quick).pack(anchor="w", padx=4, pady=4)

        diff_box = ttk.LabelFrame(sel_row, text="난이도 선택",
                                   style="Card.TLabelframe")
        diff_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.var_difficulty = tk.StringVar(value="extreme")
        for val, text in DIFFICULTY_OPTIONS:
            ttk.Radiobutton(diff_box, text=text, value=val,
                            variable=self.var_difficulty,
                            command=self._save_quick).pack(anchor="w", padx=4, pady=4)

        # 반복 설정 (전폭)
        rep = ttk.LabelFrame(f, text="반복 설정", style="Card.TLabelframe")
        rep.pack(fill="x", pady=(0, 10))
        self.var_max_cycles = tk.IntVar(value=10)
        self.var_post_min = tk.DoubleVar(value=5.0)
        self.var_post_max = tk.DoubleVar(value=10.0)
        self.var_dungeon_key = tk.StringVar(value="g")
        self.var_auto_press_key = tk.BooleanVar(value=True)
        rows_rep = [
            ("반복 횟수 (0=무제한)", self.var_max_cycles, 0, 9999, 1),
            ("진입 후 최소 대기 (초)", self.var_post_min, 0.0, 120.0, 0.5),
            ("진입 후 최대 대기 (초)", self.var_post_max, 0.0, 120.0, 0.5),
        ]
        for r, (label, var, frm_, to_, inc) in enumerate(rows_rep):
            ttk.Label(rep, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=3)
            ttk.Spinbox(rep, textvariable=var, from_=frm_, to=to_, increment=inc,
                        width=8, command=self._save_quick).grid(
                row=r, column=1, sticky="w", padx=4)
        # 키 입력 토글 + 키 이름
        ttk.Checkbutton(rep, text="던전 진입 후 키 자동 입력",
                        variable=self.var_auto_press_key,
                        command=self._save_quick).grid(
            row=3, column=0, sticky="w", padx=4, pady=3)
        key_row = ttk.Frame(rep)
        key_row.grid(row=3, column=1, sticky="w", padx=4)
        ttk.Label(key_row, text="누를 키").pack(side="left", padx=(0, 4))
        ttk.Entry(key_row, textvariable=self.var_dungeon_key, width=10).pack(side="left")
        rep.grid_columnconfigure(0, weight=1)

        # 5~8번 텍스트 항목 (난이도는 위 라디오로 이동)
        diff = ttk.LabelFrame(f, text="텍스트 항목 (OCR 인식 대상)",
                              style="Card.TLabelframe")
        diff.pack(fill="x", pady=(0, 10))
        self.var_private = tk.StringVar(value="비공개 파티")
        self.var_create = tk.StringVar(value="파티 생성")
        self.var_confirm = tk.StringVar(value="확인")
        self.var_start = tk.StringVar(value="시작하기")
        for row, (label, var) in enumerate([
            ("5. 비공개 파티 텍스트", self.var_private),
            ("6. 파티생성 텍스트", self.var_create),
            ("7. 확인 텍스트 (7/9/10번 공용)", self.var_confirm),
            ("8. 시작하기 텍스트", self.var_start),
        ]):
            ttk.Label(diff, text=label).grid(row=row, column=0, sticky="w",
                                              padx=4, pady=3)
            ttk.Entry(diff, textvariable=var, width=28).grid(
                row=row, column=1, sticky="w", padx=8, pady=3)

        # 액션 버튼들 (큰 강조)
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(4, 10))
        self.btn_start = ttk.Button(btns, text="▶  시작",
                                    style="Big.Accent.TButton",
                                    command=self.on_start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text="■  정지",
                                   style="Big.TButton",
                                   command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_test = ttk.Button(btns, text="1회만 실행",
                                   style="Big.TButton",
                                   command=self.on_once)
        self.btn_test.pack(side="left", padx=6)
        ttk.Button(btns, text="로그 지우기",
                   command=self._clear_log).pack(side="right")

        # 로그 (다크 박스)
        log_card = ttk.LabelFrame(f, text="실행 로그", style="Card.TLabelframe")
        log_card.pack(fill="both", expand=True)
        log_inner = ttk.Frame(log_card)
        log_inner.pack(fill="both", expand=True)

        self.txt_log = tk.Text(
            log_inner,
            height=18,
            font=self._fonts["log"],
            bg=LOG_BG, fg=LOG_FG,
            insertbackground=LOG_FG,
            selectbackground="#3b82f6",
            relief="flat", borderwidth=0,
            wrap="word",
            padx=10, pady=8,
        )
        scroll = ttk.Scrollbar(log_inner, orient="vertical",
                               command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=scroll.set, state="disabled")
        self.txt_log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 로그 컬러 태그
        self.txt_log.tag_configure("ok", foreground=SUCCESS)
        self.txt_log.tag_configure("warn", foreground=WARN)
        self.txt_log.tag_configure("err", foreground=DANGER)
        self.txt_log.tag_configure("info", foreground=ACCENT)
        self.txt_log.tag_configure("dim", foreground=LOG_DIM)
        self.txt_log.tag_configure("step", foreground="#c084fc")  # 보라

    # ------------------------------------------------------------ image tab --
    def _build_img_tab(self):
        f = self.tab_img

        ttk.Label(
            f,
            text="각 단계별 참조 이미지를 등록하세요. images/ 폴더에 저장돼 프로그램 종료 후에도 유지됩니다.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            f,
            text="게임 화면에서 버튼 영역만 정확히 잘라낸 PNG 가 가장 잘 인식됩니다.",
            style="Mini.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        # 스크롤 가능한 카드 그리드
        canvas = tk.Canvas(f, highlightthickness=0,
                           bg=self._get_bg())
        scroll = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(_evt):
            canvas.itemconfig(inner_id, width=canvas.winfo_width())
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)

        # 마우스 휠 스크롤
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._thumb_labels: dict[str, ttk.Label] = {}
        self._status_labels: dict[str, ttk.Label] = {}

        for i, (title, filename, desc) in enumerate(IMAGE_SLOTS):
            card = ttk.LabelFrame(inner, text=title, style="Card.TLabelframe")
            card.pack(fill="x", pady=4)

            thumb = ttk.Label(card, text="(미등록)", width=18, anchor="center",
                              relief="solid")
            thumb.grid(row=0, column=0, rowspan=3, padx=(2, 14), pady=4)
            self._thumb_labels[filename] = thumb

            ttk.Label(card, text=desc, style="Mini.TLabel").grid(
                row=0, column=1, sticky="w", pady=(2, 0))
            status = ttk.Label(card, text="", style="Mini.TLabel")
            status.grid(row=1, column=1, sticky="w")
            self._status_labels[filename] = status

            btnrow = ttk.Frame(card)
            btnrow.grid(row=2, column=1, sticky="w", pady=(4, 2))
            ttk.Button(btnrow, text="파일 선택",
                       command=lambda fn=filename: self.on_pick_image(fn)
                       ).pack(side="left", padx=(0, 4))
            ttk.Button(btnrow, text="폴더 열기",
                       command=self.on_open_images_dir).pack(side="left", padx=4)
            ttk.Button(btnrow, text="삭제",
                       command=lambda fn=filename: self.on_delete_image(fn)
                       ).pack(side="left", padx=4)

            card.grid_columnconfigure(1, weight=1)

    # ----------------------------------------------------------- config tab --
    def _build_cfg_tab(self):
        f = self.tab_cfg

        frm = ttk.LabelFrame(f, text="일반 설정", style="Card.TLabelframe")
        frm.pack(fill="x")

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
            ("단계 간 대기 (초)", self.var_delay, 0.1, 5.0, 0.1),
            ("시작 버튼 폴링 간격 (초)", self.var_poll, 1.0, 60.0, 1.0),
            ("폴링 N회 연속 실패 시 좌표 폴백", self.var_poll_fallback, 1, 20, 1),
            ("이미지/텍스트 탐색 제한 (초)", self.var_timeout, 1.0, 60.0, 1.0),
            ("클릭 지터 (±px, 0=중앙 고정)", self.var_jitter, 0, 20, 1),
        ]
        for i, (label, var, frm_, to_, inc) in enumerate(rows):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w",
                                             padx=4, pady=4)
            ttk.Spinbox(frm, textvariable=var, from_=frm_, to=to_, increment=inc,
                        width=10).grid(row=i, column=1, sticky="w", padx=8)

        ttk.Label(frm, text="OCR 언어").grid(row=len(rows), column=0, sticky="w",
                                              padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_ocr_lang, width=18).grid(
            row=len(rows), column=1, sticky="w", padx=8)

        r = len(rows) + 1
        ttk.Label(frm, text="Tesseract 경로 (비우면 자동탐지)").grid(
            row=r, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(frm, textvariable=self.var_tess_path, width=42).grid(
            row=r, column=1, sticky="w", padx=8)
        ttk.Button(frm, text="찾기...", command=self._pick_tesseract).grid(
            row=r, column=2, padx=4)

        save_row = ttk.Frame(f)
        save_row.pack(fill="x", pady=12)
        ttk.Button(save_row, text="설정 저장",
                   style="Big.Accent.TButton",
                   command=self._save_from_ui).pack(side="left")

        if not HAS_TESSERACT:
            warn = ttk.LabelFrame(f, text="⚠ 경고", style="Card.TLabelframe")
            warn.pack(fill="x", pady=(0, 8))
            ttk.Label(
                warn,
                text="pytesseract 가 설치되지 않아 텍스트 인식이 비활성화됩니다.\n"
                     "  pip install pytesseract  +  Tesseract-OCR 설치 필요 (한국어 언어팩 포함)",
                foreground=DANGER,
                justify="left",
            ).pack(anchor="w", padx=4, pady=4)

        if not HAS_SV_TTK:
            tip = ttk.LabelFrame(f, text="UI 테마", style="Card.TLabelframe")
            tip.pack(fill="x", pady=(0, 8))
            ttk.Label(
                tip,
                text="더 깔끔한 다크 테마를 원하면:  pip install sv-ttk",
                style="Mini.TLabel",
            ).pack(anchor="w", padx=4, pady=4)

    # -------------------------------------------------------- config <-> UI --
    def _load_from_config(self):
        c = self.engine.config
        self.var_dungeon.set(c.get("dungeon", "snake"))
        # 새 'difficulty' 키 우선. 없으면 옛 'difficulty_text' 로부터 추정.
        diff_key = c.get("difficulty")
        if not diff_key:
            old_text = (c.get("difficulty_text") or "").strip()
            text_to_key = {v[1]: k for k, v in DIFFICULTY_MAP.items()}
            diff_key = text_to_key.get(old_text, "extreme")
        self.var_difficulty.set(diff_key)
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
        self.var_auto_press_key.set(bool(c.get("auto_press_dungeon_key", True)))
        self.var_jitter.set(int(c.get("click_jitter_px", 3)))
        self.var_poll_fallback.set(int(c.get("start_polls_before_fallback", 2)))

    def _collect_config(self) -> dict:
        diff_key = self.var_difficulty.get() or "extreme"
        diff_text = DIFFICULTY_MAP.get(diff_key, DIFFICULTY_MAP["extreme"])[1]
        return {
            "dungeon": self.var_dungeon.get(),
            "difficulty": diff_key,
            "difficulty_text": diff_text,  # OCR fallback 용 (라디오 선택값에서 자동)
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
            "auto_press_dungeon_key": bool(self.var_auto_press_key.get()),
            "click_jitter_px": int(self.var_jitter.get()),
            "start_polls_before_fallback": int(self.var_poll_fallback.get()),
        }

    def _save_quick(self):
        cfg = self.engine.config.copy()
        cfg["dungeon"] = self.var_dungeon.get()
        diff_key = self.var_difficulty.get() or "extreme"
        cfg["difficulty"] = diff_key
        cfg["difficulty_text"] = DIFFICULTY_MAP.get(
            diff_key, DIFFICULTY_MAP["extreme"])[1]
        cfg["auto_press_dungeon_key"] = bool(self.var_auto_press_key.get())
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
            label = f"디스플레이 {idx}    {w}×{h}    @ ({left}, {top})"
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
            lbl.configure(image="", text="(미등록)")
            self._thumb_cache.pop(filename, None)
            if status is not None:
                status.configure(text="● 미등록", foreground=DANGER)
            return
        try:
            img = Image.open(path)
            img.thumbnail((140, 90))
            photo = ImageTk.PhotoImage(img)
            self._thumb_cache[filename] = photo
            lbl.configure(image=photo, text="")
            if status is not None:
                size_kb = os.path.getsize(path) / 1024
                status.configure(text=f"● 등록됨  ({size_kb:.1f} KB)",
                                 foreground=SUCCESS)
        except Exception as e:
            lbl.configure(image="", text="(오류)")
            if status is not None:
                status.configure(text=str(e), foreground=DANGER)

    # --------------------------------------------------------------- run --
    def on_start(self):
        if self.worker and self.worker.is_alive():
            return
        self.engine.save_config(self._collect_config())
        self._cycle_count = 0
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()
        self.after(200, self.iconify)

    def on_once(self):
        if self.worker and self.worker.is_alive():
            return
        self.engine.save_config(self._collect_config())
        self._cycle_count = 0
        self._set_running(True)
        self.worker = threading.Thread(target=self._run_once, daemon=True)
        self.worker.start()
        self.after(200, self.iconify)

    def on_stop(self):
        self.engine.stop()
        self._running_state = "stopping"
        self._update_header_status()
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
        self._running_state = "running" if running else "idle"
        self._update_header_status()

    def _update_header_status(self):
        if self._running_state == "running":
            color = ACCENT
            label = f"실행 중   ({self._cycle_count} 사이클)"
            bar = f"●  실행 중  ·  {self._cycle_count} 사이클 완료"
        elif self._running_state == "stopping":
            color = WARN
            label = "중단 중..."
            bar = "●  중단 요청됨"
        else:
            color = MUTED
            label = "준비됨"
            bar = "●  대기 중"
        self.status_dot.itemconfigure(self._status_dot_id, fill=color)
        self.status_text.configure(text=label, foreground=color)
        self.statusbar_left.configure(text=bar, foreground=color)

    def _clear_log(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    # ------------------------------------------------------------- logging --
    def _classify_log_tag(self, msg: str) -> str:
        """로그 한 줄을 색상 태그로 분류."""
        m = msg.strip()
        if "✓" in m or "성공" in m or "발견" in m or "완료" in m:
            return "ok"
        if m.startswith("[예외]") or "실패" in m or "오류" in m or "ERROR" in m:
            return "err"
        if "경고" in m or "warn" in m.lower() or "[정지" in m:
            return "warn"
        if m.startswith("[진행]") or m.startswith("[대기]"):
            return "info"
        if m.startswith("[") and "]" in m and not m.startswith("[매칭실패]"):
            return "step"
        if m.startswith("[매칭실패]") or m.startswith("  ["):
            return "dim"
        return ""

    def _log(self, msg: str):
        # 사이클 카운트 파싱 → 헤더 상태 갱신
        if "[진행] 던전 진입" in msg:
            try:
                # "[진행] 던전 진입 12/200 회 완료"
                part = msg.split("진입")[1].split("/")[0].strip()
                self._cycle_count = int(part)
                self.after(0, self._update_header_status)
            except (ValueError, IndexError):
                pass

        tag = self._classify_log_tag(msg)

        def _append():
            self.txt_log.configure(state="normal")
            if tag:
                self.txt_log.insert("end", msg + "\n", tag)
            else:
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
