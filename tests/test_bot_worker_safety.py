"""The bounded AI thread survives corrupt map snapshots without thrashing."""

from __future__ import annotations

import time

import pytest

from server.bot_ai.compact_vxl import CompactVoxelMap
from server.bot_ai.messages import MapSnapshot
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


def test_thread_worker_rejects_a_corrupt_snapshot_once_and_recovers():
    supervisor = AIThreadSupervisor(seed=1)
    supervisor.start(MapSnapshot(1, 0, b"garbage" * 1000, "tdm", "corrupt"))
    try:
        assert _wait(lambda: supervisor.status().snapshot_rejections >= 1)
        status = supervisor.status()
        assert status.snapshot_rejections == 1
        assert status.running is False
        # No thrash: the poisoned serial is not retried at the wake cadence.
        time.sleep(1.0)
        after = supervisor.status()
        assert after.snapshot_rejections == 1
        assert after.restarts == status.restarts

        # A fresh snapshot (here an empty one, which the world accepts as
        # "no map") brings the worker back without a restart.
        supervisor.publish_map(MapSnapshot(2, 0, b"", "tdm", "empty"))
        assert _wait(lambda: supervisor.status().running)
        assert supervisor.status().snapshot_rejections == 1
    finally:
        supervisor.close()
