"""
Odin Boss Timer — PySide6 GUI
- 탭별 보스 테이블, 실시간 남은시간 카운트다운
- 상단 툴바: 시작/중지, 수동 동기화, Discord 토글
- 하단 로그 콘솔 (logging 핸들러 연동)
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from boss_config import TAB_BOSS_MAP
from models import BossState, BossStatus
import boss_store
from scheduler import BossScheduler


class NextBossBanner(QFrame):
    """가장 빨리 출현할 보스 배너 (남은시간 ASC로 정렬 후 Top 1)."""

    def __init__(self, bosses: list[BossState], parent=None, presence_monitor=None):
        super().__init__(parent)
        self.bosses = bosses
        self.presence_monitor = presence_monitor
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            NextBossBanner {
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 6px;
            }
            QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(14)

        self.label_title = QLabel("⏭  다음 출현")
        self.label_title.setStyleSheet("font-size: 12px; color: #888;")

        self.label_name = QLabel("-")
        self.label_name.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #f5f5f5;")

        self.label_remaining = QLabel("-")
        self.label_remaining.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #ff8c00;")

        self.label_spawn = QLabel("-")
        self.label_spawn.setStyleSheet("font-size: 12px; color: #888;")

        layout.addWidget(self.label_title)
        layout.addWidget(self.label_name)
        layout.addStretch(1)
        layout.addWidget(self.label_remaining)
        layout.addWidget(self.label_spawn)

    def refresh(self):
        # 1순위: 현재 출현 중인 보스 (PresenceMonitor가 확인)
        if self.presence_monitor is not None and self.presence_monitor.flag:
            tracked = self.presence_monitor.tracked
            if tracked is not None:
                self.label_title.setText("🔴  현재 출현 중")
                self.label_title.setStyleSheet(
                    "font-size: 12px; color: #ff4545; font-weight: bold;")
                self.label_name.setText(tracked.config.display_name)
                self.label_remaining.setText("진행 중")
                self.label_remaining.setStyleSheet(
                    "font-size: 18px; font-weight: bold; color: #ff4545;")
                self.label_spawn.setText("")
                return

        # 기본: 다음 출현 보스 표시
        self.label_title.setText("⏭  다음 출현")
        self.label_title.setStyleSheet("font-size: 12px; color: #888;")
        self.label_remaining.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #ff8c00;")

        candidates = []
        for boss in self.bosses:
            boss.advance_to_next_spawn()
            r = boss.remaining_time
            if r is None or r.total_seconds() <= 0:
                continue
            candidates.append((r, boss))
        if not candidates:
            self.label_name.setText("-")
            self.label_remaining.setText("-")
            self.label_spawn.setText("-")
            return

        candidates.sort(key=lambda t: t[0])
        remaining, boss = candidates[0]
        self.label_name.setText(boss.config.display_name)
        self.label_remaining.setText(_format_remaining(remaining))
        self.label_spawn.setText(
            boss.next_spawn_at.strftime("예정 %m/%d %H:%M:%S")
            if boss.next_spawn_at else ""
        )


class DebugButton(QPushButton):
    """mousePressEvent 위치를 로그로 남기는 디버그용 QPushButton."""

    def mousePressEvent(self, event):
        pos = event.pos()
        logger.info("[MOUSE] '%s' press (%d, %d) / size %dx%d",
                    self.text(), pos.x(), pos.y(),
                    self.width(), self.height())
        super().mousePressEvent(event)


STATUS_COLOR = {
    BossStatus.WAITING: QColor("#7a7a7a"),
    BossStatus.SOON:    QColor("#ff8c00"),  # 주황 (임박)
    BossStatus.SPAWNED: QColor("#d9534f"),
    BossStatus.UNKNOWN: QColor("#555555"),
}

REGION_COLOR_GUI = {
    "요툰":   "#5bc0de",
    "니다벨": "#c58af9",
    "알브":   "#5cb85c",
    "무스펠": "#ff6b6b",
    "아스":   "#f0ad4e",
    "니플":   "#6fa8dc",
    "던전":   "#cccccc",
}


def _format_remaining(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total <= 0:
        return "출현!"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}시간 {m}분 {s}초"
    if m > 0:
        return f"{m}분 {s}초"
    return f"{s}초"


class QtLogHandler(logging.Handler):
    """logging 메시지를 Qt 시그널로 전달하는 핸들러."""

    def __init__(self, signal):
        super().__init__()
        self._signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self._signal.emit(record.levelno, msg)
        except Exception:
            pass


class _LogSignal(QWidget):
    """logging 핸들러 <-> GUI 간 스레드 안전 시그널 브릿지."""
    log = Signal(int, str)


class PriorityDialog(QDialog):
    """지역별 자동화 대상 보스 관리/우선순위 편집 다이얼로그.

    - 각 지역의 모든 보스를 리스트로 표시
    - 체크박스로 자동화 대상 on/off
    - 드래그 / ▲▼ 로 순서 변경 (체크된 항목 순서 = 자동화 우선순위)
    - OK 누르면 체크된 항목이 표시 순서대로 AUTOMATION_TARGETS 에 저장
    """

    # 프리셋 정보는 item.data(UserRole) 에 저장. 표시명은 _format_item_text 로 렌더.
    _PRESET_ROLE = Qt.UserRole
    _NAME_ROLE = Qt.UserRole + 1
    _PRESET_CHOICES = ("A", "C")

    def __init__(self, parent=None):
        super().__init__(parent)
        from automation_config import (
            REGION_PRIORITY, AUTOMATION_TARGETS, AUTOMATION_PRESETS,
        )
        from boss_config import BOSS_CONFIGS
        self.setWindowTitle("자동화 대상 / 우선순위 편집")
        self.resize(640, 600)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "☑ 체크된 보스만 자동화 대상. 드래그/▲▼ 로 순서 변경 (위쪽 = 높은 우선순위).\n"
            "우측 프리셋(A/C): 도착 후 눌릴 키 — A=Delete, C=PageDown. OK 누르면 저장됨."
        ))

        self.tabs = QTabWidget()
        self.list_widgets: dict[str, QListWidget] = {}

        # 지역별로 모든 보스 구성 — BOSS_CONFIGS 에서 region 일치하는 것들
        for region in REGION_PRIORITY:
            region_bosses = [c.name for c in BOSS_CONFIGS if c.region == region]
            current_targets = AUTOMATION_TARGETS.get(region, [])
            # 체크된 보스를 우선순위 순으로 먼저, 나머지는 뒤에
            ordered = list(current_targets)
            for name in region_bosses:
                if name not in ordered:
                    ordered.append(name)

            tab = QWidget()
            tl = QHBoxLayout(tab)
            lw = QListWidget()
            lw.setDragDropMode(QListWidget.InternalMove)
            for name in ordered:
                preset = AUTOMATION_PRESETS.get(name, "A")
                item = QListWidgetItem()
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.Checked if name in current_targets else Qt.Unchecked)
                item.setData(self._NAME_ROLE, name)
                item.setData(self._PRESET_ROLE, preset)
                item.setText(self._format_item_text(name, preset))
                lw.addItem(item)
            lw.currentItemChanged.connect(self._on_selection_changed)
            self.list_widgets[region] = lw
            tl.addWidget(lw, stretch=1)

            bcol = QVBoxLayout()
            up = QPushButton("▲ 위로")
            dn = QPushButton("▼ 아래로")
            chk_all = QPushButton("전체 선택")
            uncheck_all = QPushButton("전체 해제")
            up.clicked.connect(lambda _, w=lw: self._move(w, -1))
            dn.clicked.connect(lambda _, w=lw: self._move(w, +1))
            chk_all.clicked.connect(lambda _, w=lw: self._set_all_check(w, True))
            uncheck_all.clicked.connect(lambda _, w=lw: self._set_all_check(w, False))
            bcol.addWidget(up)
            bcol.addWidget(dn)
            bcol.addSpacing(12)
            bcol.addWidget(chk_all)
            bcol.addWidget(uncheck_all)
            bcol.addStretch(1)
            tl.addLayout(bcol)

            n_checked = len(current_targets)
            n_total = len(region_bosses)
            self.tabs.addTab(tab, f"{region} ({n_checked}/{n_total})")

        self.tabs.currentChanged.connect(lambda _: self._on_selection_changed())
        layout.addWidget(self.tabs, stretch=1)

        # 프리셋 선택 라디오 — 현재 선택된 항목의 프리셋을 바꿈
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("선택된 보스 프리셋:"))
        self._preset_group = QButtonGroup(self)
        self._radio_a = QRadioButton("A (Delete)")
        self._radio_c = QRadioButton("C (PageDown)")
        self._preset_group.addButton(self._radio_a)
        self._preset_group.addButton(self._radio_c)
        self._radio_a.toggled.connect(
            lambda checked: checked and self._apply_preset_to_current("A"))
        self._radio_c.toggled.connect(
            lambda checked: checked and self._apply_preset_to_current("C"))
        preset_row.addWidget(self._radio_a)
        preset_row.addWidget(self._radio_c)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._on_selection_changed()  # 초기 라디오 동기화

    def _move(self, list_w: QListWidget, delta: int):
        row = list_w.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if new_row < 0 or new_row >= list_w.count():
            return
        item = list_w.takeItem(row)
        list_w.insertItem(new_row, item)
        list_w.setCurrentRow(new_row)

    def _set_all_check(self, list_w: QListWidget, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(list_w.count()):
            list_w.item(i).setCheckState(state)

    def result_targets(self) -> dict[str, list[str]]:
        """체크된 항목만 표시 순서대로 반환."""
        return {
            region: [lw.item(i).data(self._NAME_ROLE) for i in range(lw.count())
                     if lw.item(i).checkState() == Qt.Checked]
            for region, lw in self.list_widgets.items()
        }

    def result_presets(self) -> dict[str, str]:
        """모든 보스의 프리셋 매핑 (체크 여부 무관)."""
        result: dict[str, str] = {}
        for lw in self.list_widgets.values():
            for i in range(lw.count()):
                item = lw.item(i)
                name = item.data(self._NAME_ROLE)
                preset = item.data(self._PRESET_ROLE) or "A"
                result[name] = preset
        return result

    def _format_item_text(self, name: str, preset: str) -> str:
        """리스트 아이템 표시 문자열 — 기본 프리셋 A 는 suffix 생략."""
        return f"{name}  ({preset})" if preset != "A" else name

    def _current_list(self) -> QListWidget | None:
        tab_idx = self.tabs.currentIndex()
        if tab_idx < 0:
            return None
        from automation_config import REGION_PRIORITY
        region = REGION_PRIORITY[tab_idx]
        return self.list_widgets.get(region)

    def _on_selection_changed(self, *_):
        """현재 선택된 아이템의 프리셋으로 라디오 동기화."""
        lw = self._current_list()
        item = lw.currentItem() if lw is not None else None
        preset = item.data(self._PRESET_ROLE) if item is not None else None
        # 시그널 블록 — 라디오 변경이 역방향으로 아이템을 건드리지 않도록
        self._radio_a.blockSignals(True)
        self._radio_c.blockSignals(True)
        enabled = item is not None
        self._radio_a.setEnabled(enabled)
        self._radio_c.setEnabled(enabled)
        self._radio_a.setChecked(preset == "A")
        self._radio_c.setChecked(preset == "C")
        self._radio_a.blockSignals(False)
        self._radio_c.blockSignals(False)

    def _apply_preset_to_current(self, preset: str):
        """현재 선택된 아이템의 프리셋 변경 + 표시 갱신."""
        lw = self._current_list()
        if lw is None:
            return
        item = lw.currentItem()
        if item is None:
            return
        item.setData(self._PRESET_ROLE, preset)
        name = item.data(self._NAME_ROLE)
        item.setText(self._format_item_text(name, preset))


class OptionsDialog(QDialog):
    """앱 전반 옵션 창. 사냥터/알림 시간 + 향후 추가 옵션 누적용."""

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setWindowTitle("옵션")
        self.resize(440, 320)

        layout = QFormLayout(self)

        # 사냥터 (아이들 아이템)
        from automator import IDLE_ITEMS, get_idle_item
        self.cb_idle = QComboBox()
        for name in IDLE_ITEMS:
            self.cb_idle.addItem(name)
        current = get_idle_item()
        if current in IDLE_ITEMS:
            self.cb_idle.setCurrentText(current)
        self.cb_idle.currentTextChanged.connect(main_window._on_idle_item_changed)
        layout.addRow("사냥터:", self.cb_idle)

        # 알림 시간 (보스 출현 알림 분 전)
        from alert_settings import ALERT_CHOICES, load_alert_minutes
        self.cb_alert = QComboBox()
        for m in ALERT_CHOICES:
            self.cb_alert.addItem(f"{m}분 전", m)
        idx = self.cb_alert.findData(load_alert_minutes())
        if idx >= 0:
            self.cb_alert.setCurrentIndex(idx)
        self.cb_alert.currentIndexChanged.connect(self._on_alert_changed)
        layout.addRow("알림 시간:", self.cb_alert)

        # 40초 미만 스킵 토글 — automator.skip_short_remaining 직결
        self.chk_skip_short = QCheckBox("남은 시간 40초 미만 보스는 자동화 스킵")
        automator = getattr(main_window.scheduler, "automator", None)
        self.chk_skip_short.setChecked(
            getattr(automator, "skip_short_remaining", True) if automator else True)
        self.chk_skip_short.toggled.connect(main_window._on_toggle_skip_short)
        layout.addRow("스킵 옵션:", self.chk_skip_short)

        # 우선순위 편집 — 별도 다이얼로그 호출
        self.btn_priority = QPushButton("우선순위 편집…")
        self.btn_priority.clicked.connect(main_window._open_priority_dialog)
        layout.addRow("자동화 대상:", self.btn_priority)

        btns = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        btns.rejected.connect(self.accept)
        layout.addRow(btns)

    def _on_alert_changed(self, idx: int):
        minutes = self.cb_alert.itemData(idx)
        if minutes is None:
            return
        from alert_settings import save_alert_minutes, apply_to_bosses
        apply_to_bosses(self.main_window.all_bosses, minutes)
        save_alert_minutes(minutes)
        logger.info("[설정] 알림 트리거 %d분 전으로 변경", minutes)


class BossKillDialog(QDialog):
    """'지금 처치' / 수동 시각 입력 다이얼로그."""

    def __init__(self, boss: BossState, parent=None):
        super().__init__(parent)
        self.boss = boss
        self.setWindowTitle(f"보스 시각 입력 — {boss.config.display_name}")
        layout = QFormLayout(self)
        self.dt = QDateTimeEdit(self)
        self.dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.dt.setCalendarPopup(True)
        now = datetime.now()
        next_spawn = now + boss.config.respawn_interval
        self.dt.setDateTime(next_spawn)
        layout.addRow(QLabel("다음 리젠 시각:"), self.dt)

        btns = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_spawn(self) -> datetime:
        return self.dt.dateTime().toPython()


class BossTable(QTableWidget):
    """탭 하나에 해당하는 보스 테이블."""

    COLS = ["보스", "상태", "남은시간", "리젠예정"]

    def __init__(self, bosses: list[BossState], parent=None):
        super().__init__(parent)
        self.bosses = bosses
        self.setColumnCount(len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.setRowCount(len(bosses))
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)

        # "남은시간" 열 고정 너비: "24시간 59분 59초" 기준
        from PySide6.QtGui import QFontMetrics
        metrics = QFontMetrics(self.font())
        fixed_w = metrics.horizontalAdvance("24시간 59분 59초") + 30
        self.setColumnWidth(2, fixed_w)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        for row, boss in enumerate(bosses):
            name_item = QTableWidgetItem(boss.config.display_name)
            color = REGION_COLOR_GUI.get(boss.config.region)
            if color:
                name_item.setForeground(QColor(color))
            self.setItem(row, 0, name_item)
            for col in range(1, len(self.COLS)):
                self.setItem(row, col, QTableWidgetItem("-"))

        self.refresh()

    def refresh(self):
        for row, boss in enumerate(self.bosses):
            boss.advance_to_next_spawn()
            status = boss.status
            remaining = boss.remaining_time

            status_item = self.item(row, 1)
            status_item.setText(status.value)
            status_item.setForeground(STATUS_COLOR.get(status, QColor("#000")))

            rem_text = _format_remaining(remaining) if remaining is not None else "-"
            self.item(row, 2).setText(rem_text)

            spawn_text = (
                boss.next_spawn_at.strftime("%m/%d %H:%M:%S")
                if boss.next_spawn_at else "-"
            )
            self.item(row, 3).setText(spawn_text)

    def _on_context_menu(self, pos):
        row = self.rowAt(pos.y())
        if row < 0:
            return
        boss = self.bosses[row]
        menu = QMenu(self)
        act_now = QAction("지금 처치 (다음 리젠 = 지금 + 주기)", self)
        act_edit = QAction("시각 직접 입력...", self)
        act_clear = QAction("출현 중 표시 해제", self)
        act_automate = QAction("🧪 자동화 시퀀스 테스트 실행", self)
        menu.addAction(act_now)
        menu.addAction(act_edit)
        menu.addSeparator()
        menu.addAction(act_clear)
        menu.addSeparator()
        menu.addAction(act_automate)

        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_now:
            boss.set_next_spawn(datetime.now() + boss.config.respawn_interval)
        elif chosen is act_edit:
            dlg = BossKillDialog(boss, self)
            if dlg.exec() == QDialog.Accepted:
                boss.set_next_spawn(dlg.selected_spawn())
                boss.advance_to_next_spawn()
        elif chosen is act_clear:
            if boss.next_spawn_at is not None:
                boss.sync_remaining(boss.next_spawn_at - datetime.now())
        elif chosen is act_automate:
            main_win = self.window()
            scheduler = getattr(main_win, "scheduler", None)
            automator = getattr(scheduler, "automator", None) if scheduler else None
            if automator is None:
                logger.warning("[테스트] automator가 없음")
            else:
                logger.info("[테스트] 자동화 시퀀스 수동 실행: %s",
                            boss.config.display_name)
                # run_for 내부에서 완료 후 start_tracking(detect_now=True) 자동 호출
                automator.run_for(boss)
        self.refresh()
        # 저장
        try:
            all_bosses = getattr(self.window(), "all_bosses", None)
            if all_bosses:
                boss_store.save(all_bosses)
        except Exception:
            pass


class MainWindow(QMainWindow):

    def __init__(self, bosses: list[BossState], scheduler: BossScheduler,
                 gui_notifier, discord_notifier=None):
        super().__init__()
        self.setWindowTitle("Odin Boss Timer")
        self.resize(960, 720)
        self.all_bosses = bosses
        self.scheduler = scheduler
        self.gui_notifier = gui_notifier
        self.discord_notifier = discord_notifier
        self._discord_enabled = discord_notifier is not None

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._wire_signals()
        self._install_log_handler()

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(1000)
        self._ui_timer.timeout.connect(self._refresh_tables)
        self._ui_timer.start()

        # Esc 키 누르면 스케줄러도 중지 — automator 의 전역 Esc 콜백에 등록
        # (콜백은 watcher 스레드에서 호출되므로 QTimer.singleShot 으로 메인 스레드로 회부)
        try:
            from automator import register_esc_callback
            register_esc_callback(
                lambda: QTimer.singleShot(0, self._on_esc_stop_scheduler)
            )
        except Exception as e:
            logger.warning("[GUI] Esc 콜백 등록 실패: %s", e)

    def _on_esc_stop_scheduler(self):
        """Esc → 스케줄러 중지 (버튼 토글해서 _on_run_toggled 가 처리)."""
        if self.btn_run.isChecked():
            logger.info("[Esc] 스케줄러 중지")
            self.btn_run.setChecked(False)

    def _build_toolbar(self):
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setStyleSheet("""
            QToolBar { spacing: 8px; padding: 4px; }
            QPushButton {
                border: 2px solid #666;
                border-radius: 4px;
                background-color: #333;
                color: #ddd;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #555;
                border-color: #888;
            }
            QPushButton:pressed { background-color: #222; }
            QPushButton:checked {
                background-color: #2d7a2d;
                border-color: #4caf50;
                color: white;
            }
        """)
        self.addToolBar(tb)

        # DebugButton (QPushButton 상속) + addWidget
        # mousePressEvent 로그로 버튼 어디 눌렀는지 가시화
        self.btn_run = DebugButton("▶ 스케줄러 시작")
        self.btn_run.setCheckable(True)
        self.btn_run.setFixedSize(180, 38)
        self.btn_run.clicked.connect(lambda: logger.info("[클릭] 실행 버튼 clicked"))
        self.btn_run.toggled.connect(self._on_run_toggled)
        tb.addWidget(self.btn_run)

        self.btn_sync = DebugButton("🔄 수동 동기화")
        self.btn_sync.setFixedSize(160, 38)
        self.btn_sync.clicked.connect(self._on_sync_clicked)
        tb.addWidget(self.btn_sync)

        self.btn_discord = DebugButton("🔔 Discord: ON")
        self.btn_discord.setCheckable(True)
        self.btn_discord.setChecked(self._discord_enabled)
        self.btn_discord.setFixedSize(180, 38)
        self.btn_discord.toggled.connect(self._on_toggle_discord)
        tb.addWidget(self.btn_discord)

        self.btn_automation = DebugButton("🤖 자동화: ON")
        self.btn_automation.setCheckable(True)
        self.btn_automation.setChecked(True)
        self.btn_automation.setFixedSize(180, 38)
        self.btn_automation.toggled.connect(self._on_toggle_automation)
        tb.addWidget(self.btn_automation)

        self.btn_idle = DebugButton("💤 아이들: ON")
        self.btn_idle.setCheckable(True)
        self.btn_idle.setChecked(True)
        self.btn_idle.setFixedSize(180, 38)
        self.btn_idle.toggled.connect(self._on_toggle_idle)
        tb.addWidget(self.btn_idle)

        # ─── 두 번째 툴바 줄 (옵션·설정용) ───
        self.addToolBarBreak()
        tb2 = QToolBar("Options", self)
        tb2.setMovable(False)
        tb2.setStyleSheet(tb.styleSheet())
        self.addToolBar(tb2)

        # 옵션 창 — 사냥터, 알림 시간, 40초 스킵, 우선순위 편집 등 모든 설정 통합
        self.btn_options = DebugButton("⚙ 옵션…")
        self.btn_options.setFixedSize(130, 38)
        self.btn_options.clicked.connect(self._open_options_dialog)
        tb2.addWidget(self.btn_options)

    def _build_central(self):
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        presence_monitor = getattr(self.scheduler, "presence_monitor", None)
        self.next_banner = NextBossBanner(self.all_bosses, central,
                                          presence_monitor=presence_monitor)
        layout.addWidget(self.next_banner)

        self.tabs = QTabWidget(central)
        self.tables: dict[str, BossTable] = {}

        boss_by_name = {b.config.name: b for b in self.all_bosses}
        for tab_name, configs in TAB_BOSS_MAP.items():
            tab_bosses = [boss_by_name[c.name] for c in configs if c.name in boss_by_name]
            table = BossTable(tab_bosses, self)
            self.tabs.addTab(table, tab_name)
            self.tables[tab_name] = table

        layout.addWidget(self.tabs, stretch=3)

        log_label = QLabel("로그", central)
        layout.addWidget(log_label)
        self.log_tabs = QTabWidget(central)
        _log_style = ("QPlainTextEdit { font-family: Consolas, monospace; "
                      "font-size: 11px; }")

        self.log_view = QPlainTextEdit(self.log_tabs)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setStyleSheet(_log_style)
        self.log_tabs.addTab(self.log_view, "로그")

        self.detail_log_view = QPlainTextEdit(self.log_tabs)
        self.detail_log_view.setReadOnly(True)
        self.detail_log_view.setMaximumBlockCount(5000)
        self.detail_log_view.setStyleSheet(_log_style)
        self.log_tabs.addTab(self.detail_log_view, "상세 로그")

        # 방문 기록 탭 — 자동화가 보스 앞 도착에 성공한 기록, boss_visits.json 에 영구 저장
        self.visit_log_view = QPlainTextEdit(self.log_tabs)
        self.visit_log_view.setReadOnly(True)
        self.visit_log_view.setMaximumBlockCount(10000)
        self.visit_log_view.setStyleSheet(_log_style)
        self.log_tabs.addTab(self.visit_log_view, "방문 기록")

        layout.addWidget(self.log_tabs, stretch=2)

        self.setCentralWidget(central)

    def _build_statusbar(self):
        self.status = QStatusBar(self)
        self.setStatusBar(self.status)
        self.status.showMessage("대기 중")

    def _wire_signals(self):
        self.scheduler.tick.connect(self._refresh_tables)
        self.scheduler.sync_started.connect(lambda: self.status.showMessage("[동기화] 진행 중..."))
        self.scheduler.sync_finished.connect(
            lambda n: self.status.showMessage(f"[동기화] 완료 ({n}개)", 5000)
        )
        self.scheduler.sync_failed.connect(
            lambda msg: self.status.showMessage(f"[동기화] 실패: {msg}", 8000)
        )

        if self.gui_notifier is not None:
            self.gui_notifier.boss_alert.connect(self._on_boss_alert)
            self.gui_notifier.general_alert.connect(self._on_general_alert)

        # 방문 기록: 저장된 엔트리 로드 후 탭 채우고, 자동화 시작마다 append
        self._populate_visit_log()
        automator = getattr(self.scheduler, "automator", None)
        if automator is not None:
            automator.boss_visited.connect(self._on_boss_visited)

    def _populate_visit_log(self):
        import boss_visits
        for entry in boss_visits.all_visits():
            self.visit_log_view.appendPlainText(boss_visits.format_line(entry))
        self.visit_log_view.moveCursor(QTextCursor.End)

    def _on_boss_visited(self, entry: dict):
        import boss_visits
        self.visit_log_view.appendPlainText(boss_visits.format_line(entry))
        self.visit_log_view.moveCursor(QTextCursor.End)

    def _install_log_handler(self):
        # 로그 탭 (INFO+) — 기존 그대로
        self._log_bridge = _LogSignal(self)
        self._log_bridge.log.connect(self._append_log)
        h_info = QtLogHandler(self._log_bridge.log)
        h_info.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        h_info.setLevel(logging.INFO)
        logging.getLogger().addHandler(h_info)

        # 상세 로그 탭 (DEBUG+) — 토글 없이 항상 수집. 모듈명 포함 포맷으로 구분 용이.
        self._detail_bridge = _LogSignal(self)
        self._detail_bridge.log.connect(self._append_detail_log)
        h_debug = QtLogHandler(self._detail_bridge.log)
        h_debug.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s][%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        h_debug.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(h_debug)

        # 지정된 모듈 로거들을 DEBUG 고정 → 상세 로그 탭에 항상 기록됨
        for name in self._DEBUG_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)

        # basicConfig 로 설치된 Stream/File 핸들러는 INFO+ 로 제한 (app.log 비대화 방지).
        # 새로 추가한 Qt 핸들러는 예외 — 각자 setLevel 로 이미 설정됨.
        root_logger = logging.getLogger()
        for h in root_logger.handlers:
            if h is h_info or h is h_debug:
                continue
            h.setLevel(logging.INFO)

    _LOG_COLOR = {
        logging.ERROR: "#d9534f",
        logging.WARNING: "#e2a93a",
        logging.INFO: "#dddddd",
        logging.DEBUG: "#888888",
    }

    def _append_log(self, levelno: int, msg: str):
        color = self._LOG_COLOR.get(levelno, "#dddddd")
        self.log_view.appendHtml(
            f'<span style="color:{color}">{msg}</span>'
        )
        self.log_view.moveCursor(QTextCursor.End)

    def _append_detail_log(self, levelno: int, msg: str):
        color = self._LOG_COLOR.get(levelno, "#dddddd")
        self.detail_log_view.appendHtml(
            f'<span style="color:{color}">{msg}</span>'
        )
        self.detail_log_view.moveCursor(QTextCursor.End)

    def _refresh_tables(self):
        for table in self.tables.values():
            table.refresh()
        if hasattr(self, "next_banner"):
            self.next_banner.refresh()

    def _on_run_toggled(self, running: bool):
        logger.info("[클릭] 실행 토글: %s", "ON" if running else "OFF")
        if running:
            # 초기화된 상태에서 새로 시작 — 이전 세션 잔여 상태 제거
            self._full_reset()
            self.scheduler.start()
            self.btn_run.setText("■ 스케줄러 중지")
            self.status.showMessage("스케줄러 실행 중")
        else:
            # 진행 중인 모든 작업 종료 (실행 중 자동화 취소 + 대기 큐 비움 + 추적 중단)
            self._full_reset()
            self.scheduler.stop()
            self.btn_run.setText("▶ 스케줄러 시작")
            self.status.showMessage("스케줄러 중지")

    def _full_reset(self):
        """진행중 태스크 취소 + 대기 큐 비움 + automator 상태 초기화."""
        from automator import cancel_current
        automator = getattr(self.scheduler, "automator", None)
        game_queue = getattr(self.scheduler, "game_queue", None)
        cancel_current()       # 현재 실행 중인 자동화에 중단 신호
        if game_queue is not None:
            try:
                game_queue.clear()
            except Exception as e:
                logger.warning("[GUI] game_queue.clear 실패: %s", e)
        if automator is not None:
            try:
                automator.reset()
            except Exception as e:
                logger.warning("[GUI] automator.reset 실패: %s", e)

    def _on_sync_clicked(self):
        logger.info("[클릭] 🔄 수동 동기화 버튼")
        self.scheduler.trigger_sync_now()

    # DEBUG 토글로 활성화할 모듈들 (라이브러리 로그 제외)
    _DEBUG_LOGGERS = [
        "boss_detector",
        "presence_monitor",
        "automator",
        "scheduler",
        "screen_sync",
        "notifier",
        "game_queue",
        "gui",
    ]

    def _open_priority_dialog(self):
        from automation_config import (
            save_runtime_targets, update_region_targets,
            save_runtime_presets, update_presets,
        )
        dlg = PriorityDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        targets = dlg.result_targets()
        for region, names in targets.items():
            update_region_targets(region, names)
        save_runtime_targets()

        presets = dlg.result_presets()
        update_presets(presets)
        save_runtime_presets()
        n_nondefault = sum(1 for v in presets.values() if v != "A")
        logger.info(
            "[GUI] 자동화 우선순위 저장 완료 (%d개 지역, 비기본 프리셋 %d개)",
            len(targets), n_nondefault)

    def _on_idle_item_changed(self, name: str):
        from automator import set_idle_item
        logger.info("[GUI] 아이들 아이템 선택: %s", name)
        try:
            set_idle_item(name)
        except Exception as e:
            logger.warning("[GUI] 아이들 아이템 변경 실패: %s", e)
            return
        # 즉시 재실행하지 않음 — 선택만 저장. 다음 IDLE 기간 진입 시 새 아이템으로 적용됨.

    def _open_options_dialog(self):
        dlg = OptionsDialog(self, parent=self)
        dlg.exec()

    def _on_toggle_automation(self, checked: bool):
        self.scheduler.automation_enabled = checked
        label = "ON" if checked else "OFF"
        icon = "🤖" if checked else "🚫"
        self.btn_automation.setText(f"{icon} 자동화: {label}")
        logger.info("[설정] 보스 이동 자동화 %s", label)
        # OFF 토글: 보류 큐 비워서 현재 시퀀스 종료 후 다음 보스로 이어지지 않게.
        # (진행 중인 시퀀스 자체는 게임 상태 보호 위해 자연 종료시까지 유지 — Ctrl+Esc 로 강제 중단)
        if not checked:
            automator = getattr(self.scheduler, "automator", None)
            if automator is not None and automator._pending:
                cleared = len(automator._pending)
                automator._pending.clear()
                logger.info("[설정] 자동화 OFF — 보류 큐 %d개 비움", cleared)

    def _on_toggle_idle(self, checked: bool):
        self.scheduler.idle_enabled = checked
        label = "ON" if checked else "OFF"
        icon = "💤" if checked else "🚫"
        self.btn_idle.setText(f"{icon} 아이들: {label}")
        logger.info("[설정] 아이들 액션 %s", label)

    def _on_toggle_skip_short(self, checked: bool):
        automator = getattr(self.scheduler, "automator", None)
        if automator is None:
            logger.warning("[설정] automator 없음 — 40초 스킵 토글 실패")
            return
        automator.skip_short_remaining = checked
        logger.info("[설정] 40초 미만 스킵 %s", "ON" if checked else "OFF")

    def _on_toggle_discord(self, checked: bool):
        logger.info("[클릭] Discord 토글: %s", "ON" if checked else "OFF")
        self._discord_enabled = checked
        if self.discord_notifier is None:
            return
        if checked:
            self.btn_discord.setText("🔔 Discord: ON")
            if self.discord_notifier not in self.scheduler.notifier.notifiers:
                self.scheduler.notifier.notifiers = (
                    *self.scheduler.notifier.notifiers, self.discord_notifier,
                )
        else:
            self.btn_discord.setText("🔕 Discord: OFF")
            self.scheduler.notifier.notifiers = tuple(
                n for n in self.scheduler.notifier.notifiers if n is not self.discord_notifier
            )

    def _on_boss_alert(self, region: str, display_name: str, message: str):
        self.status.showMessage(f"[{display_name}] {message}", 10000)

    def _on_general_alert(self, message: str):
        self.status.showMessage(message, 10000)

    def closeEvent(self, event):
        try:
            self.scheduler.stop()
            boss_store.save(self.all_bosses)
        finally:
            super().closeEvent(event)
