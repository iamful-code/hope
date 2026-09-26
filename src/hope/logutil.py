"""Логи через очередь: log.info() в цикле событий только кладёт запись в очередь, в консоль пишет отдельный поток.

Зачем: на Windows вывод в консоль блокируется, пока в окне выделен текст (режим QuickEdit, в заголовке
«Выбрать»). Раньше вместе с выводом вставал весь движок: поток с биржи не читался, биржа рвала WebSocket.
Теперь при выделении ждёт только поток вывода, записи копятся в очереди и выводятся после Esc.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
from typing import TextIO

FORMAT = "%(asctime)s %(levelname).1s %(name)s: %(message)s"
DATEFMT = "%H:%M:%S"

_listener: logging.handlers.QueueListener | None = None


def attach(logger: logging.Logger, stream: TextIO) -> logging.handlers.QueueListener:
    """Вешает на logger обработчик-очередь, а вывод в stream — в фоновый поток. Возвращает запущенный слушатель."""
    q: queue.SimpleQueue = queue.SimpleQueue()
    out = logging.StreamHandler(stream)
    out.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    listener = logging.handlers.QueueListener(q, out)
    listener.start()
    logger.addHandler(logging.handlers.QueueHandler(q))
    return listener


def setup_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Как logging.basicConfig: если у корневого логгера уже есть обработчики (повторный вызов, pytest), ничего не делает."""
    global _listener
    root = logging.getLogger()
    if root.handlers:
        return
    _listener = attach(root, stream if stream is not None else sys.stderr)
    root.setLevel(level)
    atexit.register(stop_logging)


def stop_logging() -> None:
    """Дописывает очередь в консоль и останавливает поток вывода. Нужна перед os._exit (atexit там не срабатывает)."""
    global _listener
    listener, _listener = _listener, None
    if listener is not None:
        listener.stop()
