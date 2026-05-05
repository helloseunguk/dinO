"""
게임 조작 태스크 직렬 실행 큐.

동기화(screen_sync)와 자동화(automator)를 같은 큐에 넣어 **순차 처리** →
동시에 마우스·키보드·게임 창을 조작하면서 생기는 충돌 방지.
"""

import logging
import queue
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class GameTaskQueue(QObject):
    """FIFO 큐 + 백그라운드 워커 스레드 1개."""

    task_started = Signal(str)
    task_finished = Signal(str, float)   # name, duration_sec
    task_failed = Signal(str, str)       # name, error_msg

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="GameTaskQueue")
        self._thread.start()

    def submit(self, name: str, func: Callable):
        """task를 큐 뒤에 적재. func는 인자 없이 호출 가능해야 함."""
        self._queue.put((name, func))
        logger.info("[큐] '%s' 적재 (대기 %d개)", name, self._queue.qsize())

    def qsize(self) -> int:
        return self._queue.qsize()

    def clear(self) -> int:
        """대기 중인 태스크 전부 비움 (실행 중인 태스크에는 영향 없음).
        실행 중 태스크는 automator.cancel_current() 로 별도 중단 필요."""
        count = 0
        try:
            while True:
                self._queue.get_nowait()
                self._queue.task_done()
                count += 1
        except queue.Empty:
            pass
        if count > 0:
            logger.info("[큐] %d개 대기 태스크 비움", count)
        return count

    def _loop(self):
        while True:
            name, func = self._queue.get()
            logger.info("[큐] '%s' 실행 시작 (뒤에 %d개 대기)",
                        name, self._queue.qsize())
            self.task_started.emit(name)
            t0 = time.time()
            try:
                func()
                dt = time.time() - t0
                self.task_finished.emit(name, dt)
                logger.info("[큐] '%s' 완료 (%.1fs)", name, dt)
            except Exception as e:
                logger.exception("[큐] '%s' 실패", name)
                self.task_failed.emit(name, str(e))
            finally:
                self._queue.task_done()
