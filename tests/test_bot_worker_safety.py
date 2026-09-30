"""The bounded AI thread survives corrupt map snapshots without thrashing."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from server.bot_ai.compact_vxl import CompactVoxelMap
from server.bot_ai.messages import MapSnapshot
from server.bot_ai import thread_supervisor
from server.bot_ai.thread_supervisor import AIThreadSupervisor


def _wait(predicate, seconds=4.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_compact_vxl_rejects_non_bytes_and_garbage_with_value_errors():
    with pytest.raises(ValueError):
        CompactVoxelMap(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompactVoxelMap("not bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        CompactVoxelMap(b"garbage" * 1000)


def test_thread_worker_rejects_a_corrupt_snapshot_once_and_recovers(monkeypatch):
    # The rejection is published after logger.exception returns. Exercise the
    # real parser and worker thread, but keep unrelated log-handler I/O out of
    # this lifecycle test's scheduling guard and verify the diagnostic itself.
    log_exception = Mock()
    monkeypatch.setattr(thread_supervisor.logger, "exception", log_exception)
    supervisor = AIThreadSupervisor(seed=1)
    supervisor.start(MapSnapshot(1, 0, b"garbage" * 1000, "tdm", "corrupt"))
    owner = supervisor._thread
    try:
        assert _wait(lambda: supervisor.status().snapshot_rejections >= 1)
        status = supervisor.status()
        assert status.snapshot_rejections == 1
        assert status.running is False
        # No thrash: the poisoned serial is not retried at the wake cadence.
        # Observe actual completed batches instead of assuming the worker ran
        # during a wall-clock sleep on a busy test host.
        for _ in range(4):
            previous = supervisor.status().last_heartbeat_batch_id
            supervisor._wake.set()
            assert _wait(
                lambda: supervisor.status().last_heartbeat_batch_id > previous
            )
            after = supervisor.status()
            assert after.snapshot_rejections == 1
            assert after.restarts == status.restarts
            assert after.running is False

        # A fresh snapshot (here an empty one, which the world accepts as
        # "no map") brings the worker back without a restart.
        supervisor.publish_map(MapSnapshot(2, 0, b"", "tdm", "empty"))
        assert _wait(lambda: supervisor.status().running)
        assert supervisor.status().snapshot_rejections == 1
        assert supervisor._thread is owner
        assert supervisor.status().restarts == status.restarts
        log_exception.assert_called_once_with(
            "AI thread rejected map snapshot serial %d (%s); "
            "waiting for a new one",
            1,
            "corrupt",
        )
    finally:
        supervisor.close()
