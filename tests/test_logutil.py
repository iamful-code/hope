"""Логи через очередь: медленная (заблокированная) консоль не должна тормозить вызывающий код."""

from __future__ import annotations

import io
import logging
import threading
import time

from hope import logutil


class _BlockedStream(io.StringIO):
    """Консоль Windows с выделенным текстом: write ждёт, пока выделение не снимут."""

    def __init__(self) -> None:
        super().__init__()
        self.released = threading.Event()

    def write(self, s: str) -> int:
        self.released.wait(10)
        return super().write(s)


def test_blocked_console_does_not_block_logging() -> None:
    stream = _BlockedStream()
    log = logging.getLogger("hope.test_logutil")
    log.propagate = False
    log.setLevel(logging.INFO)
    listener = logutil.attach(log, stream)
    try:
        t0 = time.perf_counter()
        for i in range(200):
            log.info("сообщение %d", i)
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("ошибка")
        assert time.perf_counter() - t0 < 0.5  # раньше каждый вызов ждал консоль
        assert stream.getvalue() == ""
        stream.released.set()
    finally:
        listener.stop()
        for h in list(log.handlers):
            log.removeHandler(h)
    out = stream.getvalue()
    assert "сообщение 0" in out and "сообщение 199" in out
    assert "ValueError: boom" in out  # трассировка не теряется
    assert out.splitlines()[0].split(" ", 2)[1:2] == ["I"]  # общий формат: время, уровень, логгер


def test_setup_logging_is_noop_when_root_configured() -> None:
    # под pytest у корневого логгера уже есть обработчики — как basicConfig, ничего не трогаем
    root = logging.getLogger()
    before = list(root.handlers)
    assert before
    logutil.setup_logging(logging.DEBUG)
    assert root.handlers == before
    logutil.stop_logging()  # без запущенного слушателя — без ошибок
