"""
Odin Boss Timer — PySide6 GUI
- 탭별 보스 테이블, 실시간 남은시간 카운트다운
- 상단 툴바: 시작/중지, 수동 동기화, Discord 토글
- 하단 로그 콘솔 (logging 핸들러 연동)
"""

import json
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
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
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from boss_config import TAB_BOSS_MAP
import automation_config
import macro_points
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


class StatusBanner(QFrame):
    """현재 자동화 상태 + 경험치 100% 예상 시간."""

    # 큐 태스크 이름 → 표시 문구 (IDLE 중에 도는 태스크)
    _TASK_LABELS = {
        "idle_action": "🚶 사냥터 이동 중",
        "camera_check": "📷 카메라 보정 중",
        "ocr_test": "🧪 OCR 테스트 중",
    }

    def __init__(self, scheduler, exp_tracker=None, parent=None):
        super().__init__(parent)
        self.scheduler = scheduler
        self.exp_tracker = exp_tracker
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("""
            StatusBanner {
                background-color: #1e1e1e;
                border: 1px solid #444;
                border-radius: 6px;
            }
            QLabel { background: transparent; }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(14)

        self.label_state = QLabel("-")
        self.label_state.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #f5f5f5;")
        self.label_exp = QLabel("-")
        self.label_exp.setStyleSheet("font-size: 13px; color: #9ccc65;")

        layout.addWidget(self.label_state)
        layout.addStretch(1)
        layout.addWidget(self.label_exp)

    def refresh(self, running: bool):
        self.label_state.setText(self._state_text(running))
        self.label_exp.setText(self._exp_text())

    def _state_text(self, running: bool) -> str:
        automator = getattr(self.scheduler, "automator", None)
        game_queue = getattr(self.scheduler, "game_queue", None)
        presence = getattr(self.scheduler, "presence_monitor", None)
        if not running:
            text = "⏸ 스케줄러 중지"
        elif automator is None:
            text = "-"
        elif automator.state == automator.STATE_RUNNING:
            boss = automator.current_boss
            text = f"🏃 보스 이동 중 — {boss.config.display_name if boss else '?'}"
        elif automator.state == automator.STATE_TRACKING:
            boss = automator.current_boss
            name = boss.config.display_name if boss else "?"
            fighting = presence is not None and presence.flag
            text = f"⚔ 교전 중 — {name}" if fighting else f"👁 출현 대기 — {name}"
        else:
            task = game_queue.current_task if game_queue is not None else None
            if task is None:
                text = "💤 IDLE — 사냥 중"
            elif task.startswith("sync:"):
                text = "🔄 동기화 중"
            else:
                text = self._TASK_LABELS.get(task, f"⚙ {task}")
        if running and automator is not None and automator.pending_count:
            text += f"  ·  대기열 {automator.pending_count}개"
        return text

    def _exp_text(self) -> str:
        if self.exp_tracker is None:
            return ""
        from exp_tracker import (MIN_SPAN_SEC, TARGET_PERCENT,
                                 format_duration)
        snap = self.exp_tracker.snapshot()
        current, est = snap["current"], snap["estimate"]
        if current is None:
            return "EXP -  (IDLE 사냥 중에만 측정)"
        parts = [f"EXP {current:.4f}%"]
        if est is not None:
            parts.append(f"{est.rate_per_hour:+.3f}%/h (최근 {est.span_min:.0f}분)")
            if est.hours_left is None:
                parts.append("오르지 않음")
            else:
                parts.append(f"{TARGET_PERCENT:.0f}%까지 약 {format_duration(est.hours_left)}"
                             f" ({est.eta.strftime('%m/%d %H:%M')})")
            parts.append(est.computed_at.strftime("%H:%M 계산"))
        else:
            parts.append(f"기록 쌓는 중 ({snap['segment_min']:.0f}/"
                         f"{MIN_SPAN_SEC / 60:.0f}분)")
        return "  ·  ".join(parts)


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
    "바나":   "#20c997",
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


class _LogSignal(QObject):
    """logging 핸들러 <-> GUI 간 스레드 안전 시그널 브릿지.

    QWidget 이면 안 된다 — 레이아웃에 들어가지 않는 자식 위젯이라 창 좌상단
    (0,0) 에 기본 크기로 얹혀 툴바 첫 버튼의 클릭을 가로챈다. 시그널만 필요하므로
    QObject 로 둔다.
    """
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

        # 보스 시간표 주기 갱신 방식 (API / 화면 캡처)
        import sync_settings
        self.cb_sync_source = QComboBox()
        for src in sync_settings.SYNC_SOURCES:
            self.cb_sync_source.addItem(sync_settings.describe(src), src)
        idx = self.cb_sync_source.findData(
            getattr(main_window.scheduler, "sync_source",
                    sync_settings.load_sync_source()))
        if idx >= 0:
            self.cb_sync_source.setCurrentIndex(idx)
        self.cb_sync_source.currentIndexChanged.connect(self._on_sync_source_changed)
        layout.addRow("시간표 갱신:", self.cb_sync_source)

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

    def _on_sync_source_changed(self, idx: int):
        source = self.cb_sync_source.itemData(idx)
        if source is None:
            return
        import sync_settings
        if not sync_settings.save_sync_source(source):
            return
        self.main_window.scheduler.set_sync_source(source)

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


class OcrTestDialog(QDialog):
    """서버 OCR 테스트 — 시간표를 캡처해 업로드하고 응답을 그대로 보여준다.

    게임 창을 조작하므로 실행은 game_queue 에 태운다 (자동화·동기화와 경쟁 방지).
    캡처/업로드는 수 초 걸리므로 워커 스레드에서 돌리고, 결과는 시그널로 받는다.
    """

    # 워커 스레드 → GUI 스레드로 결과 전달 (blob, 응답 dict, 오류 메시지)
    finished_run = Signal(object, object, str)
    # 반영 결과 전달 (apply 응답 dict, 오류 메시지)
    finished_apply = Signal(object, str)

    # 보스별 반영 결과 표시색
    _STATUS_COLOR = {
        "applied": "#2ecc71",
        "live": "#3498db",
        "skipped": "#999999",
        "failed": "#e74c3c",
    }

    MODES = [("본토", 0), ("침공전", 1), ("부캐침공", 2)]
    MODE_HINT = ("어느 시간표로 등록할지 — 인식·등록 요청에 함께 보냅니다\n"
                 "0 본토 · 1 침공전 · 2 부캐침공\n"
                 "고른 값은 저장돼 다음에 열 때도 유지됩니다")

    def __init__(self, game_queue=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("서버 OCR 테스트")
        self.resize(900, 680)
        self.game_queue = game_queue
        # 원본 캡처를 들고 있다가 라벨 크기에 맞춰 다시 스케일한다
        # (레이아웃 전에 스케일하면 라벨이 아직 작아서 미리보기가 쪼그라든다)
        self._preview_pix: QPixmap | None = None
        self._shots: list[tuple[str, bytes]] = []   # [(탭 이름, PNG 바이트)]
        self._entries: list[dict] = []              # 마지막 인식 결과
        self._last_mode = 0                         # 그 인식에 쓴 mode — 반영 때 같이 보낸다

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        mode_label = QLabel("등록 모드", self)
        mode_label.setToolTip(self.MODE_HINT)
        top.addWidget(mode_label)
        self.combo_mode = QComboBox(self)
        self.combo_mode.setToolTip(self.MODE_HINT)
        for label, value in self.MODES:
            self.combo_mode.addItem(f"{label} ({value})", value)
        # 지난번에 고른 모드로 복원 — 매번 본토로 돌아가면 잘못 등록하기 쉽다
        import ocr_sync
        idx = self.combo_mode.findData(ocr_sync.load_mode())
        if idx >= 0:
            self.combo_mode.setCurrentIndex(idx)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        top.addWidget(self.combo_mode)

        self.chk_test = QCheckBox("테스트 엔드포인트 (/api/bosses/ocr/test)", self)
        self.chk_test.setChecked(True)
        self.chk_test.setToolTip(
            "끄면 실제 /api/bosses/ocr 로 보냅니다 (Bearer 토큰 사용)")
        top.addWidget(self.chk_test)

        self.chk_close = QCheckBox("캡처 후 시간표 닫기", self)
        self.chk_close.setChecked(True)
        top.addWidget(self.chk_close)

        top.addStretch(1)
        # 캡처 → 인식 → 반영을 한 번에. 인식 실패가 있으면 반영 직전에 멈춘다.
        self.btn_run = QPushButton("⚡ 캡처 → 인식 → 반영", self)
        self.btn_run.setFixedHeight(32)
        self.btn_run.setToolTip(
            "시간표를 열어 탭별로 캡처하고, 인식 결과를 그대로 서버에 반영합니다\n"
            "매칭 안 된 줄이 하나라도 있으면 반영하지 않고 멈춥니다")
        self.btn_run.clicked.connect(self._start)
        top.addWidget(self.btn_run)
        layout.addLayout(top)

        self.status = QLabel("대기 중 — 게임을 실행한 상태에서 실행하세요.", self)
        self.status.setStyleSheet("font-weight: bold; padding: 4px;")
        layout.addWidget(self.status)

        body = QHBoxLayout()

        preview_box = QVBoxLayout()
        self.combo_preview = QComboBox(self)   # 탭별 캡처 전환
        self.combo_preview.currentIndexChanged.connect(self._on_preview_changed)
        preview_box.addWidget(self.combo_preview)
        self.preview = QLabel("캡처 미리보기", self)
        self.preview.setMinimumWidth(380)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet(
            "border: 1px solid #444; background-color: #1e1e1e; color: #777;")
        preview_box.addWidget(self.preview, stretch=1)
        body.addLayout(preview_box, stretch=1)

        self.table = QTableWidget(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["탭", "인식된 이름", "남은 시간", "bossId", "결과"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        body.addWidget(self.table, stretch=1)
        layout.addLayout(body, stretch=2)

        layout.addWidget(QLabel("응답 원문", self))
        self.raw = QPlainTextEdit(self)
        self.raw.setReadOnly(True)
        self.raw.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, monospace; font-size: 11px; }")
        layout.addWidget(self.raw, stretch=1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        btn_close = QPushButton("닫기", self)
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        layout.addLayout(btns)

        self.finished_run.connect(self._on_finished)
        self.finished_apply.connect(self._on_applied)

    def _on_mode_changed(self, index: int):
        mode = self.combo_mode.itemData(index)
        if mode is None:
            return
        import ocr_sync
        ocr_sync.save_mode(mode)

    # ─── 실행 ─────────────────────────────────────────────
    def _start(self):
        mode = self.combo_mode.currentData()
        test = self.chk_test.isChecked()
        close_after = self.chk_close.isChecked()

        self.btn_run.setEnabled(False)
        self._last_mode = mode
        self.status.setText("시간표 여는 중… (게임 창이 앞으로 나옵니다)")

        def task():
            """캡처 → 인식 → (문제 없으면) 반영까지 한 흐름으로."""
            shots, result = [], None
            try:
                import ocr_sync
                shots = ocr_sync.capture_tabs(close_after=close_after)
                result = ocr_sync.send_images(
                    [blob for _, blob in shots], mode=mode, test=test)
                summary = ocr_sync.summarize(result, [name for name, _ in shots])
            except Exception as e:
                logger.exception("[서버OCR] 인식 실패")
                self.finished_run.emit(shots, result, str(e))
                return

            # 인식 결과를 먼저 화면에 올리고, 이어서 반영 단계로
            self.finished_run.emit(shots, result, "")
            try:
                ocr_sync.ensure_recognition_ok(summary["entries"],
                                               summary["unmatched"])
                applied = ocr_sync.apply_entries(summary["entries"], mode=mode)
                self.finished_apply.emit(applied, "")
            except ocr_sync.RecognitionError as e:
                logger.warning("[서버OCR] 반영 중단 — %s", e)
                self.finished_apply.emit(None, f"{e} — 반영하지 않았습니다")
            except Exception as e:
                logger.exception("[서버OCR] 반영 실패")
                self.finished_apply.emit(None, str(e))

        # 게임 조작이므로 공용 큐에 태워 자동화와 순서를 맞춘다. 큐가 없으면 단독 스레드.
        if self.game_queue is not None:
            self.game_queue.submit("ocr_test", task)
        else:
            threading.Thread(target=task, daemon=True,
                             name="OcrTestRun").start()

    def _on_finished(self, shots, result, error):
        # 인식이 끝났을 뿐 반영이 남았다 — 버튼은 _on_applied 에서 다시 켠다
        if error or not isinstance(result, dict):
            self.btn_run.setEnabled(True)
        self._shots = list(shots or [])
        self.combo_preview.blockSignals(True)
        self.combo_preview.clear()
        for name, blob in self._shots:
            self.combo_preview.addItem(f"{name} 탭 ({len(blob) / 1024:.0f} KB)")
        self.combo_preview.blockSignals(False)
        if self._shots:
            self._show_shot(0)
        if error:
            self.status.setText(f"❌ 실패 — {error}")
            self.raw.setPlainText(error)
            return
        if not isinstance(result, dict):
            self.status.setText("❌ 응답이 비었습니다")
            return

        self.raw.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))

        # 테스트 엔드포인트는 실제 결과를 result 안에 한 겹 더 감싼다
        payload = result.get("result") if isinstance(result.get("result"), dict) \
            else result
        entries = payload.get("entries") or []
        unmatched = payload.get("unmatched") or []
        self._entries = entries
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            boss_id = entry.get("bossId")
            name_item = QTableWidgetItem(
                str(entry.get("bossName") or entry.get("name", "")))
            if boss_id is None:
                name_item.setForeground(QColor("#999999"))
                name_item.setToolTip(
                    "이 모드의 서버 보스 목록에 없음 — 등록에서 제외됩니다")
            self.table.setItem(row, 0, QTableWidgetItem(
                self._tab_of(entry.get("imageIndex"))))
            self.table.setItem(row, 1, name_item)
            self.table.setItem(row, 2, QTableWidgetItem(self._remaining_text(entry)))
            self.table.setItem(row, 3, QTableWidgetItem(
                "-" if boss_id is None else str(boss_id)))
            self.table.setItem(row, 4, QTableWidgetItem(""))

        self.status.setText(
            self._summary(result, entries, unmatched) + " · 반영 중…")

    def _remaining_text(self, entry: dict) -> str:
        """entry 의 남은 시간을 사람이 읽는 형태로."""
        if entry.get("live"):
            return "출현 중"
        d, h = entry.get("days") or 0, entry.get("hours") or 0
        m, s = entry.get("minutes") or 0, entry.get("seconds") or 0
        if not any((d, h, m, s)):
            return str(entry.get("time", ""))   # 테스트용 응답 호환
        if d:
            return f"{d}일 {h}시간"
        if h:
            return f"{h}시간 {m}분"
        return f"{m}분 {s}초"

    # ─── 반영 ─────────────────────────────────────────────
    def _on_applied(self, result, error: str):
        self.btn_run.setEnabled(True)
        if error:
            self.status.setText(f"❌ 반영 실패 — {error}")
            self.raw.setPlainText(error)
            return

        self.raw.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        by_id = {r.get("bossId"): r for r in result.get("results") or []}
        ignored = 0
        for row, entry in enumerate(self._entries):
            if entry.get("bossId") is None:
                # 이 모드에 없는 보스 — 보내지 않았다는 걸 표에 남긴다
                cell = QTableWidgetItem("제외")
                cell.setForeground(QColor("#999999"))
                cell.setToolTip("이 모드의 서버 보스 목록에 없어 등록하지 않음")
                self.table.setItem(row, 4, cell)
                ignored += 1
                continue
            info = by_id.get(entry.get("bossId"))
            if info is None:
                continue
            status = info.get("status", "")
            col = 4
            cell = QTableWidgetItem(status)
            color = self._STATUS_COLOR.get(status)
            if color:
                cell.setForeground(QColor(color))
            # 서버가 왜 건너뛰었는지(수동 설정 보호 등)는 툴팁으로
            tip = " / ".join(x for x in (info.get("reason"),
                                         info.get("spawnAt"),
                                         info.get("killedAt")) if x)
            if tip:
                cell.setToolTip(tip)
            self.table.setItem(row, col, cell)

        failed = result.get("failed") or 0
        text = (f"{'⚠' if failed else '✅'} 반영 {result.get('applied', 0)}개 "
                f"· 출현중 {result.get('live', 0)}개 "
                f"· 건너뜀 {result.get('skipped', 0)}개 · 실패 {failed}개")
        if ignored:
            text += f" · 이 모드에 없는 보스 {ignored}개 제외"
        self.status.setText(text)

    def _tab_of(self, image_index) -> str:
        """entry.imageIndex → 그 이미지를 찍은 탭 이름."""
        if isinstance(image_index, int) and 0 <= image_index < len(self._shots):
            return self._shots[image_index][0]
        return "-" if image_index is None else f"#{image_index}"

    def _on_preview_changed(self, index: int):
        if 0 <= index < len(self._shots):
            self._show_shot(index)

    def _show_shot(self, index: int):
        pix = QPixmap()
        if pix.loadFromData(bytes(self._shots[index][1]), "PNG"):
            self._preview_pix = pix
            self._update_preview()

    def _update_preview(self):
        """보관 중인 캡처를 현재 라벨 크기에 맞춰 표시."""
        if self._preview_pix is None:
            return
        size = self.preview.size()
        self.preview.setPixmap(self._preview_pix.scaled(
            max(size.width(), 1), max(size.height(), 1),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_preview()

    def _summary(self, result: dict, entries: list, unmatched: list) -> str:
        matched = sum(1 for e in entries if e.get("bossId") is not None)
        parts = [f"인식 {len(entries)}개", f"매칭 {matched}개",
                 f"미매칭 {len(unmatched)}개"]
        if result.get("elapsedMs") is not None:
            parts.append(f"{result['elapsedMs']}ms")
        cards = [f"{self._tab_of(d.get('imageIndex'))} {d.get('cards')}"
                 for d in result.get("debug") or []]
        if cards:
            parts.append("카드 " + "/".join(cards))
            refs = {d.get("ref") for d in result.get("debug") or [] if d.get("ref")}
            parts.append(f"기준시각 {', '.join(sorted(refs)) if refs else '못 찾음'}")
        for f in result.get("files") or []:
            if f.get("accepted") is False:
                parts.append(f"⚠ 파일 거부: {f.get('reason')}")
        if result.get("tesseractReady") is False:
            parts.append("⚠ 서버 OCR 데이터 없음")
        mark = "✅" if entries else "⚠"
        return f"{mark} " + " · ".join(parts)


class MacroPointDialog(QDialog):
    """매크로 좌표 수집 — 커서 위치를 실시간 표시하고 F8 로 찍어 저장.

    게임에 포커스가 있어도 좌표를 찍을 수 있어야 하므로, 단축키는 Qt 단축키가
    아니라 GetAsyncKeyState 폴링(macro_points.key_down)으로 감시한다.
    저장 파일·좌표 환산은 터미널판(mouse_monitor.py)과 공유한다.
    """

    COLS = ["이름", "BASE", "게임 로컬", "절대", "저장 시각"]
    POLL_MS = 50

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("매크로 좌표 수집")
        self.resize(720, 420)
        self.points = macro_points.load_points()
        self._gx, self._gy, self._cw, self._ch = macro_points.window_origin()
        self._prev_keys = {vk: False for vk in
                           (macro_points.VK_F8, macro_points.VK_F9)}

        layout = QVBoxLayout(self)

        origin = (f"게임 창 {self._cw}x{self._ch} (원점 {self._gx}, {self._gy})"
                  if self._cw else "게임 창을 찾지 못했습니다 — 절대 좌표만 유효합니다")
        layout.addWidget(QLabel(origin, self))

        self.live = QLabel("-", self)
        self.live.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 15px; "
            "font-weight: bold; padding: 6px; background-color: #1e1e1e; "
            "color: #ddd; border: 1px solid #444;")
        layout.addWidget(self.live)

        layout.addWidget(QLabel(
            "F8 저장 · F9 마지막 취소 · 이름은 표에서 바로 고칠 수 있습니다", self))

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(self.COLS)):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table, stretch=1)

        btns = QHBoxLayout()
        btn_save = QPushButton("＋ 지금 좌표 저장 (F8)", self)
        btn_save.clicked.connect(self._save_current)
        btns.addWidget(btn_save)
        btn_del = QPushButton("선택 삭제", self)
        btn_del.clicked.connect(self._delete_selected)
        btns.addWidget(btn_del)
        btn_copy = QPushButton("코드로 복사", self)
        btn_copy.clicked.connect(self._copy_code)
        btns.addWidget(btn_copy)
        btns.addStretch(1)
        btn_close = QPushButton("닫기", self)
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_close)
        layout.addLayout(btns)

        self._refresh_table()

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ─── 실시간 표시 + 단축키 ───────────────────────────────
    def _tick(self):
        ax, ay = macro_points.cursor_pos()
        lx, ly = ax - self._gx, ay - self._gy
        bx, by = macro_points.to_base(lx, ly, self._cw, self._ch)
        outside = macro_points.outside_client(lx, ly, self._cw, self._ch)
        self.live.setText(
            f"BASE ({bx}, {by})    게임 ({lx}, {ly})    절대 ({ax}, {ay})"
            + ("    ⚠ 게임 창 밖" if outside else ""))

        for vk, action in ((macro_points.VK_F8, self._save_current),
                           (macro_points.VK_F9, self._undo_last)):
            down = macro_points.key_down(vk)
            if down and not self._prev_keys[vk]:
                action()
            self._prev_keys[vk] = down

    # ─── 좌표 편집 ─────────────────────────────────────────
    def _save_current(self):
        point = macro_points.snapshot(self._gx, self._gy, self._cw, self._ch)
        point["name"] = macro_points.auto_name(self.points)
        self.points.append(point)
        macro_points.save_points(self.points)
        logger.info("[좌표수집] 저장: %s %s", point["name"],
                    macro_points.format_line(point))
        self._refresh_table()
        # 방금 저장한 행의 이름 칸을 바로 편집 상태로 — 이름 붙이는 흐름이 끊기지 않게
        row = len(self.points) - 1
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def _undo_last(self):
        if not self.points:
            return
        removed = self.points.pop()
        macro_points.save_points(self.points)
        logger.info("[좌표수집] 마지막 저장 취소: %s", removed.get("name"))
        self._refresh_table()

    def _delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self.points):
                self.points.pop(row)
        macro_points.save_points(self.points)
        logger.info("[좌표수집] %d개 삭제", len(rows))
        self._refresh_table()

    def _copy_code(self):
        text = macro_points.code_snippet(self.points)
        QApplication.clipboard().setText(text)
        logger.info("[좌표수집] 코드 %d줄 클립보드 복사", len(self.points))

    def _on_item_changed(self, item):
        """이름 칸 편집 → 저장 파일에 반영."""
        if item.column() != 0:
            return
        row = item.row()
        if 0 <= row < len(self.points):
            self.points[row]["name"] = item.text().strip()
            macro_points.save_points(self.points)

    def _refresh_table(self):
        self.table.blockSignals(True)   # 채우는 동안 itemChanged 오발동 방지
        self.table.setRowCount(len(self.points))
        for row, p in enumerate(self.points):
            bx, by = p["base"]
            lx, ly = p["local"]
            ax, ay = p["abs"]
            cw, ch = p.get("client", [0, 0])
            name_item = QTableWidgetItem(p.get("name", ""))
            if macro_points.outside_client(lx, ly, cw, ch):
                name_item.setForeground(QColor("#e74c3c"))
                name_item.setToolTip("저장 당시 커서가 게임 창 밖이었습니다")
            self.table.setItem(row, 0, name_item)
            for col, text in enumerate(
                    (f"({bx}, {by})", f"({lx}, {ly})", f"({ax}, {ay})",
                     p.get("saved_at", "")), start=1):
                cell = QTableWidgetItem(text)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, cell)
        self.table.blockSignals(False)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)


class MainWindow(QMainWindow):

    def __init__(self, bosses: list[BossState], scheduler: BossScheduler,
                 gui_notifier, discord_notifier=None, exp_tracker=None):
        super().__init__()
        self.setWindowTitle("Odin Boss Timer")
        self.resize(1180, 760)
        self.all_bosses = bosses
        self.scheduler = scheduler
        self.exp_tracker = exp_tracker
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

    # 툴바 공통 스타일 — 두 줄이 같은 모양을 쓴다.
    # 버튼 폭을 고정하지 않고 글자에 맞춰 늘어나게 둔다 (고정폭이면 창이 좁을 때
    # 툴바가 '...' 오버플로 메뉴로 접혀 버튼이 안 보인다).
    _TOOLBAR_QSS = """
        QToolBar { spacing: 6px; padding: 4px; border: 0; }
        QToolBar::separator { background: #444; width: 1px; margin: 4px 6px; }
        QPushButton {
            border: 2px solid #666;
            border-radius: 4px;
            background-color: #333;
            color: #ddd;
            font-size: 13px;
            font-weight: bold;
            padding: 0 12px;
        }
        QPushButton:hover { background-color: #555; border-color: #888; }
        QPushButton:pressed { background-color: #222; }
        QPushButton:checked {
            background-color: #2d7a2d;
            border-color: #4caf50;
            color: white;
        }
        /* 실행 버튼만 예외 — 시작(초록) / 중지(빨강).
           ID 선택자라 위의 :checked 초록 규칙보다 우선한다. */
        QPushButton#runButton {
            background-color: #2d7a2d; border-color: #4caf50; color: white;
        }
        QPushButton#runButton:hover {
            background-color: #379137; border-color: #66bb6a;
        }
        QPushButton#runButton:checked {
            background-color: #a03030; border-color: #e74c3c; color: white;
        }
        QPushButton#runButton:checked:hover {
            background-color: #b83a3a; border-color: #ff6b6b;
        }
        QPushButton::menu-indicator { subcontrol-position: right center;
                                      subcontrol-origin: padding; right: 6px; }
    """

    # 버튼 최소 폭 — 이보다 좁으면 누르기 불편하다
    _BTN_MIN_W = 104
    # 글자 폭에 더할 여백 (좌우 padding 12 + 테두리 2 + 이모지 보정)
    _BTN_PADDING = 44

    def _toolbar_button(self, text: str, tooltip: str = "",
                        checkable: bool = False,
                        labels: tuple[str, ...] = ()) -> "DebugButton":
        """툴바 버튼 생성.

        labels 에 이 버튼이 가질 수 있는 모든 라벨을 넘기면 그중 가장 긴 것에
        맞춰 폭을 고정한다. 상태에 따라 글자가 바뀌는 버튼(시작↔중지, ON↔OFF)은
        폭이 흔들리면 옆 버튼까지 밀려서 방금 누른 자리가 다른 버튼이 된다.
        """
        btn = DebugButton(text)
        btn.setFixedHeight(34)
        btn.setCheckable(checkable)
        fm = btn.fontMetrics()
        widest = max(fm.horizontalAdvance(t) for t in (labels or (text,)))
        btn.setFixedWidth(max(widest + self._BTN_PADDING, self._BTN_MIN_W))
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _build_toolbar(self):
        # ─── 첫 줄: 실행 · 도구 ───
        tb = QToolBar("Main", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setStyleSheet(self._TOOLBAR_QSS)
        self.addToolBar(tb)

        # DebugButton (QPushButton 상속) + addWidget
        # mousePressEvent 로그로 버튼 어디 눌렀는지 가시화
        self.btn_run = self._toolbar_button(
            "▶ 시작", "스케줄러 시작/중지 (Ctrl+Esc 로도 중지)",
            checkable=True, labels=("▶ 시작", "■ 중지"))
        self.btn_run.setObjectName("runButton")   # 시작=초록 / 중지=빨강
        self.btn_run.clicked.connect(lambda: logger.info("[클릭] 실행 버튼 clicked"))
        self.btn_run.toggled.connect(self._on_run_toggled)
        tb.addWidget(self.btn_run)

        self.btn_sync = self._toolbar_button("🔄 동기화", "지금 화면 캡처(OCR)로 시간표 동기화")
        self.btn_sync.clicked.connect(self._on_sync_clicked)
        tb.addWidget(self.btn_sync)

        # 오른쪽 그룹(옵션·도구)을 끝으로 밀어내는 빈 공간
        spacer = QWidget(tb)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.btn_options = self._toolbar_button("⚙ 옵션", "사냥터 · 알림 시간 · 스킵 · 자동화 대상")
        self.btn_options.clicked.connect(self._open_options_dialog)
        tb.addWidget(self.btn_options)

        # 가끔 쓰는 개발/보정 도구는 메뉴로 묶어 툴바를 비운다
        self.btn_tools = self._toolbar_button("🛠 도구", "좌표 수집 · 서버 OCR 테스트")
        tools_menu = QMenu(self.btn_tools)
        act_macro = tools_menu.addAction("🖱 좌표 수집…")
        act_macro.triggered.connect(self._open_macro_point_dialog)
        act_ocr = tools_menu.addAction("🧪 서버 OCR 테스트…")
        act_ocr.triggered.connect(self._open_ocr_test_dialog)
        self.btn_tools.setMenu(tools_menu)
        tb.addWidget(self.btn_tools)

        # ─── 둘째 줄: 켜고 끄는 것들 ───
        self.addToolBarBreak()
        tb2 = QToolBar("Toggles", self)
        tb2.setMovable(False)
        tb2.setStyleSheet(self._TOOLBAR_QSS)
        self.addToolBar(tb2)

        self.btn_discord = self._toolbar_button(
            "🔔 Discord", "보스 알림을 Discord 로도 보낼지", checkable=True,
            labels=self._toggle_labels("Discord"))
        self.btn_discord.setChecked(self._discord_enabled)
        self.btn_discord.toggled.connect(self._on_toggle_discord)
        tb2.addWidget(self.btn_discord)

        self.btn_automation = self._toolbar_button(
            "🤖 자동화", "보스 앞으로 이동하는 자동화 (OFF 면 알림만)", checkable=True,
            labels=self._toggle_labels("자동화"))
        self.btn_automation.setChecked(True)
        self.btn_automation.toggled.connect(self._on_toggle_automation)
        tb2.addWidget(self.btn_automation)

        self.btn_idle = self._toolbar_button(
            "💤 아이들", "다음 보스까지 여유 있을 때 사냥터 순회", checkable=True,
            labels=self._toggle_labels("아이들"))
        self.btn_idle.setChecked(True)
        self.btn_idle.toggled.connect(self._on_toggle_idle)
        tb2.addWidget(self.btn_idle)

        tb2.addSeparator()

        # 우선순위 적용 토글 — OFF 면 우선순위 무시하고 스폰 빠른 순으로 간다
        self.btn_priority_mode = self._toolbar_button(
            "🎯 우선순위",
            "ON — 지역/보스 우선순위 순서로 이동\nOFF — 스폰이 빠른 순서로 이동",
            checkable=True, labels=self._toggle_labels("우선순위"))
        self.btn_priority_mode.setChecked(automation_config.is_priority_enabled())
        self.btn_priority_mode.toggled.connect(self._on_toggle_priority_mode)
        tb2.addWidget(self.btn_priority_mode)

        # 초기 라벨을 현재 상태에 맞춰 한 번 갱신 (ON/OFF 접미사)
        for btn, base in ((self.btn_discord, "Discord"),
                          (self.btn_automation, "자동화"),
                          (self.btn_idle, "아이들"),
                          (self.btn_priority_mode, "우선순위")):
            self._set_toggle_text(btn, base, btn.isChecked())

    # 토글 버튼 라벨 — 아이콘 + 이름 + 상태. 상태는 색(체크 시 초록)으로도 보이지만
    # 글자로도 남겨 둔다 (색만으로는 ON/OFF 가 헷갈린다는 피드백).
    _TOGGLE_ICONS = {
        "Discord": ("🔔", "🔕"),
        "자동화": ("🤖", "🚫"),
        "아이들": ("💤", "🚫"),
        "우선순위": ("🎯", "⏱"),
    }

    def _toggle_labels(self, base: str) -> tuple[str, str]:
        """이 토글이 가질 수 있는 ON/OFF 라벨 — 버튼 폭 고정용."""
        icon_on, icon_off = self._TOGGLE_ICONS.get(base, ("", ""))
        return (f"{icon_on} {base} ON", f"{icon_off} {base} OFF")

    def _set_toggle_text(self, btn, base: str, on: bool):
        on_label, off_label = self._toggle_labels(base)
        btn.setText(on_label if on else off_label)

    def _on_toggle_priority_mode(self, checked: bool):
        automation_config.set_priority_enabled(checked)
        self._set_toggle_text(self.btn_priority_mode, "우선순위", checked)

    def _open_macro_point_dialog(self):
        MacroPointDialog(self).exec()

    def _open_ocr_test_dialog(self):
        game_queue = getattr(self.scheduler, "game_queue", None)
        OcrTestDialog(game_queue, self).exec()

    def _build_central(self):
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        presence_monitor = getattr(self.scheduler, "presence_monitor", None)
        self.next_banner = NextBossBanner(self.all_bosses, central,
                                          presence_monitor=presence_monitor)
        layout.addWidget(self.next_banner)

        self.status_banner = StatusBanner(self.scheduler, self.exp_tracker, central)
        layout.addWidget(self.status_banner)

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
        if hasattr(self, "status_banner"):
            self.status_banner.refresh(self.btn_run.isChecked())

    def _on_run_toggled(self, running: bool):
        logger.info("[클릭] 실행 토글: %s", "ON" if running else "OFF")
        if running:
            # 초기화된 상태에서 새로 시작 — 이전 세션 잔여 상태 제거
            self._full_reset()
            self.scheduler.start()
            self.btn_run.setText("■ 중지")
            self.status.showMessage("스케줄러 실행 중")
        else:
            # 진행 중인 모든 작업 종료 (실행 중 자동화 취소 + 대기 큐 비움 + 추적 중단)
            self._full_reset()
            self.scheduler.stop()
            self.btn_run.setText("▶ 시작")
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
        self._set_toggle_text(self.btn_automation, "자동화", checked)
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
        self._set_toggle_text(self.btn_idle, "아이들", checked)
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
        # 라벨은 notifier 유무와 무관하게 먼저 맞춘다 (없을 때 글자가 안 바뀌던 문제)
        self._set_toggle_text(self.btn_discord, "Discord", checked)
        if self.discord_notifier is None:
            return
        if checked:
            if self.discord_notifier not in self.scheduler.notifier.notifiers:
                self.scheduler.notifier.notifiers = (
                    *self.scheduler.notifier.notifiers, self.discord_notifier,
                )
        else:
            self._set_toggle_text(self.btn_discord, "Discord", False)
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
