import ctypes
import logging
import sys
from datetime import datetime


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate_and_exit():
    """현재 프로세스를 관리자 권한으로 재실행하고 원본은 종료."""
    params = " ".join(f'"{a}"' for a in sys.argv)
    # SW_SHOWNORMAL = 1
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    # ShellExecuteW 반환값이 32 초과면 성공
    if rc <= 32:
        # UAC 거절/실패 — 에러 메시지 띄우고 종료
        ctypes.windll.user32.MessageBoxW(
            None,
            "관리자 권한이 필요합니다. 실행이 취소되었습니다.",
            "Odin Boss Timer", 0x10,
        )
    sys.exit(0)


# QApplication import 전에 승격 체크 (무거운 모듈 로딩 전 종료하도록)
if not _is_admin():
    _elevate_and_exit()


from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

import boss_store
import ocr_engine
from automation_config import load_runtime_targets, load_runtime_presets
from boss_config import BOSS_CONFIGS, CHECK_INTERVAL, SYNC_INTERVAL
from paths import BUNDLED_DIR

# 자동화 우선순위 / 스킬 프리셋 커스텀 저장 있으면 로드
load_runtime_targets()
load_runtime_presets()
from models import BossState
from notifier import CompositeNotifier, DiscordNotifier, GuiNotifier, LogNotifier, SystemNotifier
from screen_sync import ScreenSync
from scheduler import BossScheduler
from automator import BossAutomator
from game_queue import GameTaskQueue
from presence_monitor import PresenceMonitor
from gui import MainWindow


from paths import DATA_DIR as _DATA_DIR_FOR_LOG

_LOG_FILE = _DATA_DIR_FOR_LOG / "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),  # 콘솔 (python.exe 에서 실행 시)
        logging.FileHandler(str(_LOG_FILE), mode="a", encoding="utf-8"),
    ],
)

# 스크린샷 기준 계산된 최초 리젠 시각 (2026-04-05 19:34~19:37 캡처 기반)
INITIAL_SPAWNS: dict[str, datetime] = {
    "파르바":     datetime(2026, 4, 6, 2, 17),
    "흐니르":     datetime(2026, 4, 6, 5, 28),
    "바우티":     datetime(2026, 4, 5, 23, 24),
    "야른":       datetime(2026, 4, 5, 22, 51),
    "셀로비아":   datetime(2026, 4, 6, 0, 36),
    "페티":       datetime(2026, 4, 6, 2, 17),
    "니드호그":   datetime(2026, 4, 5, 22, 50),
    "티르":       datetime(2026, 4, 7, 17, 34),

    "라이노르":   datetime(2026, 4, 6, 1, 44),
    "헤르모드":   datetime(2026, 4, 5, 22, 1),
    "브륀힐드":   datetime(2026, 4, 6, 0, 53),
    "수드리":     datetime(2026, 4, 6, 0, 22),
    "비요른":     datetime(2026, 4, 5, 22, 1),
    "스칼라니르": datetime(2026, 4, 5, 23, 25),
    "라타토스크": datetime(2026, 4, 6, 3, 20),
    "토르":       datetime(2026, 4, 7, 9, 37),

    "스바르트":   datetime(2026, 4, 6, 12, 20),
    "모네가름":   datetime(2026, 4, 5, 20, 30),
    "굴베이그":   datetime(2026, 4, 6, 12, 21),
    "두라스로르": datetime(2026, 4, 6, 15, 31),
    "드라우그":   datetime(2026, 4, 6, 17, 2),
    "오딘":       datetime(2026, 4, 7, 9, 37),

    "메기르":     datetime(2026, 4, 6, 6, 16),
    "헤르가름":   datetime(2026, 4, 6, 4, 37),
    "엘드룬":     datetime(2026, 4, 5, 23, 36),
    "수르트":     datetime(2026, 4, 7, 15, 37),
    "신마라":     datetime(2026, 4, 6, 4, 31),
    "탕그리스니르": datetime(2026, 4, 6, 7, 56),
    "우로보로스": datetime(2026, 4, 5, 22, 5),

    "발리":       datetime(2026, 4, 7, 9, 37),
    "샤무크":     datetime(2026, 4, 7, 10, 37),
    "화신 그로아":     datetime(2026, 4, 6, 7, 5),
    "노트":       datetime(2026, 4, 7, 16, 37),
    "스칼드메르": datetime(2026, 4, 7, 11, 37),
    "미미르":     datetime(2026, 4, 8, 0, 37),

    "히로킨":     datetime(2026, 4, 6, 22, 48),
    "헤이드":     datetime(2026, 4, 7, 21, 48),
    "호드":       datetime(2026, 4, 7, 21, 48),

    "최하층굴베": datetime(2026, 4, 6, 12, 3),
    "스네르":     datetime(2026, 4, 8, 14, 43),
    "최하층강글": datetime(2026, 4, 6, 3, 12),
    "4층":        datetime(2026, 4, 5, 22, 55),
    "7층":        datetime(2026, 4, 6, 17, 17),
    "10층":       datetime(2026, 4, 5, 22, 22),

    "그로아(미드)": datetime(2026, 4, 5, 20, 9),
    "매트리악":     datetime(2026, 4, 5, 20, 14),
    "탕그뇨스트":   datetime(2026, 4, 5, 20, 11),
    "칼바람 하피":  datetime(2026, 4, 5, 20, 13),
    "레라드":       datetime(2026, 4, 5, 20, 9),
}


def create_bosses() -> list[BossState]:
    bosses = []
    for cfg in BOSS_CONFIGS:
        boss = BossState(config=cfg)
        if cfg.name in INITIAL_SPAWNS:
            boss.set_next_spawn(INITIAL_SPAWNS[cfg.name])
            boss.advance_to_next_spawn()
        bosses.append(boss)

    loaded = boss_store.load(bosses)
    if loaded > 0:
        logging.getLogger(__name__).info("저장된 보스 시각 %d개 로드 완료", loaded)

    return bosses


DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1490302982905135215/1MNi0mZHpqXLf7dt_JbOuFIW5HwTNTXlh1Vy8rUmAkKF9Ldd30nkkRxkDm-6SLagkYjt"


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(BUNDLED_DIR / "images" / "boss.ico")))

    # Discord 서버 멤버십 인증 — 임시 비활성화 (재활성화 시 아래 블록 복원)
    # from PySide6.QtWidgets import QMessageBox
    # from discord_auth import authenticate
    # logger = logging.getLogger("main")
    # try:
    #     ok, msg = authenticate()
    # except Exception as e:
    #     logger.exception("[인증] authenticate() 예외")
    #     QMessageBox.critical(
    #         None, "Odin Boss Timer — 인증 오류",
    #         f"예외 발생: {e}\n\napp.log 확인 후 재실행 해주세요.",
    #     )
    #     sys.exit(1)
    # logger.info("[인증] 결과: ok=%s, msg=%s", ok, msg)
    # if not ok:
    #     QMessageBox.critical(
    #         None, "Odin Boss Timer — 실행 권한 없음",
    #         f"{msg}\n\n지정된 Discord 서버에 참여 후 다시 실행해주세요.",
    #     )
    #     sys.exit(1)

    # Windows OCR 엔진 백그라운드 웜업 — 첫 sync의 초기 레이턴시 제거
    ocr_engine.warmup_async()

    gui_notifier = GuiNotifier()
    system_notifier = SystemNotifier()
    discord_notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
    notifier = CompositeNotifier(
        LogNotifier(),
        gui_notifier,
        system_notifier,
        discord_notifier,
    )

    bosses = create_bosses()
    # 저장된 알림 트리거 시간 적용 (1/3/5분)
    from alert_settings import load_alert_minutes, apply_to_bosses
    apply_to_bosses(bosses, load_alert_minutes())

    screen_sync = ScreenSync()
    game_queue = GameTaskQueue()  # sync + automation 순차 처리
    presence_monitor = PresenceMonitor(game_queue=game_queue)
    automator = BossAutomator(screen_sync, game_queue,
                              presence_monitor=presence_monitor,
                              bosses=bosses)
    scheduler = BossScheduler(
        bosses, notifier,
        screen_sync=screen_sync,
        check_interval=CHECK_INTERVAL,
        sync_interval=SYNC_INTERVAL,
        automator=automator,
        game_queue=game_queue,
        presence_monitor=presence_monitor,
    )

    window = MainWindow(bosses, scheduler, gui_notifier, discord_notifier)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
