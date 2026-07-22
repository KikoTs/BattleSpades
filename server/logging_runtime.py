"""Bounded, non-blocking logging for the simulation process."""

from __future__ import annotations

import copy
import logging
import queue
from dataclasses import dataclass
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path


class NonBlockingQueueHandler(QueueHandler):
    """Never wait for a slow log sink and defer normal formatting to it."""

    def __init__(self, target_queue: queue.Queue):
        super().__init__(target_queue)
        self.dropped_records = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler.prepare() formats on the caller thread. Preserve lazy
        # message arguments for the listener instead. Exceptions are rare and
        # need their traceback materialized while the originating frames live.
        if record.exc_info:
            return super().prepare(record)
        prepared = copy.copy(record)
        prepared.exc_text = None
        prepared.stack_info = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_records += 1


class StoppableQueueListener(QueueListener):
    """Guarantee shutdown even when the bounded queue is at capacity."""

    def enqueue_sentinel(self) -> None:
        while True:
            try:
                self.queue.put(self._sentinel, timeout=0.1)
                return
            except queue.Full:
                continue


@dataclass
class LoggingRuntime:
    handler: NonBlockingQueueHandler
    listener: StoppableQueueListener
    sinks: tuple[logging.Handler, ...]

    @property
    def dropped_records(self) -> int:
        return self.handler.dropped_records

    def stop(self) -> None:
        self.listener.stop()
        for sink in self.sinks:
            sink.flush()
            sink.close()


def configure_logging(config, log_dir: Path | str = "logs") -> LoggingRuntime:
    """Install one bounded queue between gameplay code and all I/O sinks."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    sinks: list[logging.Handler] = []
    if bool(getattr(config, "log_console", True)):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        sinks.append(console)
    configured_file = Path(str(getattr(config, "log_file", "server.log")))
    if not configured_file.is_absolute():
        configured_file = log_dir / configured_file
    configured_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        configured_file,
        mode="a",
        maxBytes=max(
            1024 * 1024,
            int(getattr(config, "log_max_bytes", 16 * 1024 * 1024)),
        ),
        backupCount=max(1, int(getattr(config, "log_backup_count", 3))),
        encoding="utf-8",
        errors="replace",
    )
    file_handler.setFormatter(formatter)
    sinks.append(file_handler)

    capacity = max(256, int(getattr(config, "log_queue_capacity", 8192)))
    target_queue: queue.Queue = queue.Queue(maxsize=capacity)
    queue_handler = NonBlockingQueueHandler(target_queue)
    listener = StoppableQueueListener(
        target_queue, *sinks, respect_handler_level=True
    )

    root = logging.getLogger()
    for existing in tuple(root.handlers):
        root.removeHandler(existing)
    root.setLevel(getattr(logging, str(config.log_level).upper(), logging.INFO))
    root.addHandler(queue_handler)
    listener.start()
    return LoggingRuntime(queue_handler, listener, tuple(sinks))
