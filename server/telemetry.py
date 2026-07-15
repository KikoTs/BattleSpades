"""Composition service for bounded logging and runtime measurements."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .metrics import RuntimeMetrics

if TYPE_CHECKING:
    from .logging_runtime import LoggingRuntime


class TelemetryService:
    """Own operational counters without performing gameplay-thread I/O.

    ``RuntimeMetrics`` stores only bounded in-memory samples. Normal log records
    are handed to the optional ``LoggingRuntime``, whose non-blocking queue and
    listener thread own formatting and file/console writes. The service is
    created before simulation and read from the gameplay thread; saturation is
    reported as a drop count and never blocks a tick.
    """

    def __init__(self, logging_runtime: "LoggingRuntime | None" = None) -> None:
        self.metrics = RuntimeMetrics()
        self.logging_runtime = logging_runtime

    @property
    def dropped_log_records(self) -> int:
        """Return bounded-queue overflow, or zero when logging is not attached."""
        if self.logging_runtime is None:
            return 0
        return int(self.logging_runtime.dropped_records)

    def snapshot(self) -> dict[str, float | int]:
        """Return one read-only metrics summary for diagnostics or gates."""
        result = self.metrics.snapshot()
        result["logging_dropped_records"] = self.dropped_log_records
        return result
