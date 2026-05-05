import logging
import urllib.request
import json
import queue
import threading
import time
from abc import ABC, abstractmethod

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QApplication, QStyle, QSystemTrayIcon

from models import BossState, BossStatus

try:
    import pyttsx3
    _PYTTSX3_AVAILABLE = True
except ImportError:
    _PYTTSX3_AVAILABLE = False

try:
    import asyncio
    import ctypes
    import os
    import tempfile
    import edge_tts
    _EDGE_TTS_AVAILABLE = True
except ImportError:
    _EDGE_TTS_AVAILABLE = False

logger = logging.getLogger(__name__)


class Notifier(ABC):
    """알림 전송 인터페이스"""

    @abstractmethod
    def notify(self, boss: BossState, message: str) -> None:
        ...

    @abstractmethod
    def send(self, message: str) -> None:
        """보스와 무관한 일반 메시지 전송"""
        ...


class LogNotifier(Notifier):
    """로그로 알림을 출력하는 구현체"""

    def notify(self, boss: BossState, message: str) -> None:
        logger.info("[%s] %s", boss.config.name, message)

    def send(self, message: str) -> None:
        logger.info(message)


class DiscordNotifier(Notifier):
    """디스코드 웹훅으로 알림을 전송하는 구현체"""

    REGION_EMOJI: dict[str, str] = {
        "요툰":   "⚒️",
        "니다벨": "🧊",
        "알브":   "🌿",
        "무스펠": "🔥",
        "아스":   "⚡",
        "니플":   "💀",
        "던전":   "🏰",
    }

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _post(self, content: str) -> None:
        payload = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OdinBossTimer/1.0",
            },
        )
        try:
            urllib.request.urlopen(req)
        except Exception as e:
            logger.error("디스코드 전송 실패: %s", e)

    def notify(self, boss: BossState, message: str) -> None:
        emoji = self.REGION_EMOJI.get(boss.config.region, "📌")
        self._post(f"{emoji} **[{boss.config.display_name}]** {message}")

    def send(self, message: str) -> None:
        self._post(message)


class CompositeNotifier(Notifier):
    """여러 Notifier를 동시에 사용"""

    def __init__(self, *notifiers):
        self.notifiers = notifiers

    def notify(self, boss: BossState, message: str) -> None:
        for notifier in self.notifiers:
            notifier.notify(boss, message)

    def send(self, message: str) -> None:
        for notifier in self.notifiers:
            notifier.send(message)


class GuiNotifier(QObject):
    """GUI로 알림을 전달하는 QObject 기반 Notifier (덕 타이핑)."""

    boss_alert = Signal(str, str, str)  # region, display_name, message
    general_alert = Signal(str)

    def notify(self, boss: BossState, message: str) -> None:
        self.boss_alert.emit(boss.config.region, boss.config.display_name, message)

    def send(self, message: str) -> None:
        self.general_alert.emit(message)


class SystemNotifier(QObject):
    """
    Windows 트레이 토스트 + TTS 음성 알림.
    SOON(임박) 이벤트만 알림 — 메시지는 '{display_name} 곧 출현'.

    여러 알림이 겹치면 큐에 쌓아 순차 처리하며, 각 알림 사이에 5초 gap.
    """

    GAP_SECONDS = 3.0
    _show_toast = Signal(str, str)  # title, body — worker → 메인 스레드 라우팅

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._tray: QSystemTrayIcon | None = None
        self._queue: queue.Queue[str] = queue.Queue()
        self._show_toast.connect(self._handle_show_toast)
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="SystemNotifier-worker")
        self._worker.start()

    def _worker_loop(self):
        while True:
            body = self._queue.get()
            try:
                # 1) Windows 트레이 토스트는 비활성 (사용자 요청) — TTS만 재생
                # self._show_toast.emit("⚠️ 보스 임박", body)
                # 2) TTS 동기 재생 (이 스레드에서 블로킹)
                if _EDGE_TTS_AVAILABLE:
                    try:
                        self._speak_edge(body)
                    except Exception as e:
                        logger.warning("edge-tts 실패, pyttsx3 폴백: %s", e)
                        if _PYTTSX3_AVAILABLE:
                            try:
                                self._speak_pyttsx3(body)
                            except Exception as ee:
                                logger.warning("pyttsx3 실패: %s", ee)
                elif _PYTTSX3_AVAILABLE:
                    try:
                        self._speak_pyttsx3(body)
                    except Exception as e:
                        logger.warning("pyttsx3 실패: %s", e)
            except Exception as e:
                logger.warning("알림 처리 실패: %s", e)
            finally:
                self._queue.task_done()
                # 3) 다음 알림까지 5초 gap
                time.sleep(self.GAP_SECONDS)

    @Slot(str, str)
    def _handle_show_toast(self, title: str, body: str):
        tray = self._ensure_tray()
        if tray is not None:
            tray.showMessage(
                title, body,
                QSystemTrayIcon.MessageIcon.Warning, 10000,
            )

    def _ensure_tray(self) -> QSystemTrayIcon | None:
        if self._tray is not None:
            return self._tray
        app = QApplication.instance()
        if app is None or not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        icon = app.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        self._tray = QSystemTrayIcon(icon, self)
        self._tray.setToolTip("Odin Boss Timer")
        self._tray.show()
        return self._tray

    # 네이티브 neural 보이스 (edge-tts)
    EDGE_VOICE = "ko-KR-SunHiNeural"  # 여성. 남성은 ko-KR-InJoonNeural

    def _speak_edge(self, text: str) -> None:
        """edge-tts로 MP3 생성 후 Windows MCI로 재생 (동기, 내부 스레드에서만 호출)."""
        tmp_path: str | None = None
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp.close()
            tmp_path = tmp.name

            async def gen():
                comm = edge_tts.Communicate(text, self.EDGE_VOICE)
                await comm.save(tmp_path)
            asyncio.run(gen())

            # MCI 재생 (mpegvideo는 MP3 포함)
            winmm = ctypes.windll.winmm
            alias = f"ttsmci_{os.getpid()}_{threading.get_ident()}"
            winmm.mciSendStringW(
                f'open "{tmp_path}" type mpegvideo alias {alias}', None, 0, 0)
            winmm.mciSendStringW(f'play {alias} wait', None, 0, 0)
            winmm.mciSendStringW(f'close {alias}', None, 0, 0)
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _speak_pyttsx3(self, text: str) -> None:
        """SAPI Heami 폴백."""
        engine = pyttsx3.init()
        for v in engine.getProperty("voices"):
            vid = (v.id or "").lower()
            name = (v.name or "").lower()
            if "ko" in vid or "korean" in name or "heami" in name:
                engine.setProperty("voice", v.id)
                break
        engine.setProperty("rate", 130)
        engine.say(text)
        engine.runAndWait()
        engine.stop()

    def notify(self, boss: BossState, message: str) -> None:
        if boss.status != BossStatus.SOON:
            return
        body = f"{boss.config.display_name} 곧 출현"
        # 큐에 넣기만 함 — worker가 순차 처리
        self._queue.put(body)
        logger.info("[큐] '%s' 적재 (대기 %d개)", body, self._queue.qsize())

    def send(self, message: str) -> None:
        # 일반 메시지(월드보스 등)는 처리 안 함 — Discord/로그에 맡김
        pass
