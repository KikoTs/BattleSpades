"""Bounded in-process supervisor for the simple bot worker.

The legacy process boundary protected the server from unbounded native Recast
queries, but cost one Python interpreter and one copied map per server.  The
replacement planner has hard expansion/radius limits, so a single background
thread can safely own its collision snapshot and controller state without
pickle pipes or a spawned interpreter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import logging
import threading
import time

from .messages import (
    BotIntent,
    MapSnapshot,
    PerceptionFrame,
    VoxelChange,
    WorldDelta,
)
from .simple_navigation import SimpleVoxelWorld
from .simple_worker import SimpleBotBrain
from .supervisor import WorkerStatus


logger = logging.getLogger(__name__)

_INTENT_LIMIT = 128
_TERRAIN_REBASE_THRESHOLD = 65_536


class AIThreadSupervisor:
    """Coalesce immutable inputs for one bounded background AI thread."""

    def __init__(
        self,
        *,
        seed: int = 0,
        decision_hz: float = 8.0,
        path_requests_per_second: float = 24.0,
    ) -> None:
        del seed, path_requests_per_second
        self.decision_hz = max(1.0, float(decision_hz))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest_snapshot: MapSnapshot | None = None
        self._snapshot_serial = 0
        self._frames: dict[tuple[int, int], PerceptionFrame] = {}
        self._terrain_pending: dict[
            tuple[int, int, int], VoxelChange
        ] = {}
        self._terrain_overlay: dict[
            tuple[int, int, int], VoxelChange
        ] = {}
        self._terrain_map_epoch = -1
        self._terrain_version = -1
        self._intents: deque[BotIntent] = deque(maxlen=_INTENT_LIMIT)
        self._running = False
        self._ready = False
        self._restarts = 0
        self._dropped_frames = 0
        self._dropped_intents = 0
        self._last_processed_at = 0.0
        self._last_acknowledged_frame_id = -1
        self._last_batch_id = -1
        self._awaiting_frame_id: int | None = None

    def start(self, snapshot: MapSnapshot) -> None:
        """Start one owner thread and publish its initial map."""

        if self._thread is not None and self._thread.is_alive():
            self.publish_map(snapshot)
            return
        with self._lock:
            self._latest_snapshot = snapshot
            self._snapshot_serial += 1
            self._terrain_map_epoch = int(snapshot.map_epoch)
            self._terrain_version = int(snapshot.topology_version)
            self._terrain_pending.clear()
            self._terrain_overlay = {
                change.coordinate: change
                for change in snapshot.changed_cells
            }
            self._frames.clear()
            self._intents.clear()
            self._running = True
            self._ready = False
            self._last_processed_at = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker_main,
            name="BattleSpadesAI",
            daemon=True,
        )
        self._thread.start()
        self._wake.set()

    def close(self, timeout: float = 3.0) -> None:
        """Stop and join the exact thread owned by this supervisor."""

        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            self._running = False
            self._ready = False
            self._latest_snapshot = None
            self._frames.clear()
            self._intents.clear()
            self._terrain_pending.clear()
            self._terrain_overlay.clear()
        self._thread = None

    def publish_map(self, snapshot: MapSnapshot) -> None:
        """Atomically replace the map generation and all old queued facts."""

        with self._lock:
            self._latest_snapshot = snapshot
            self._snapshot_serial += 1
            self._terrain_map_epoch = int(snapshot.map_epoch)
            self._terrain_version = int(snapshot.topology_version)
            self._terrain_pending.clear()
            self._terrain_overlay = {
                change.coordinate: change
                for change in snapshot.changed_cells
            }
            self._frames.clear()
            self._intents.clear()
            self._awaiting_frame_id = None
            self._ready = False
        self._wake.set()

    def discard_timeline(self) -> None:
        """Drop queued frames and results belonging to the prior game."""

        with self._lock:
            self._frames.clear()
            self._intents.clear()
            self._awaiting_frame_id = None

    def request_restart(self) -> None:
        """Rebuild the private world/brain from canonical state off-thread."""

        with self._lock:
            if self._latest_snapshot is None:
                return
            self._snapshot_serial += 1
            self._frames.clear()
            self._intents.clear()
            self._awaiting_frame_id = None
            self._ready = False
            self._restarts += 1
        self._wake.set()

    def publish_world_change(
        self,
        change: VoxelChange,
        *,
        map_epoch: int,
        topology_version: int,
    ) -> None:
        """Coalesce one authoritative terrain change by concrete cell."""

        with self._lock:
            if int(map_epoch) != self._terrain_map_epoch:
                return
            self._terrain_version = max(
                self._terrain_version,
                int(topology_version),
            )
            self._terrain_pending[change.coordinate] = change
            self._terrain_overlay[change.coordinate] = change
        self._wake.set()

    @property
    def snapshot_required(self) -> bool:
        with self._lock:
            return len(self._terrain_pending) >= _TERRAIN_REBASE_THRESHOLD

    def submit_frame(self, frame: PerceptionFrame) -> bool:
        """Keep only the newest perception for one bot generation."""

        key = int(frame.observer_id), int(frame.observer_generation)
        with self._lock:
            previous = self._frames.get(key)
            if previous is not None and int(previous.frame_id) >= int(frame.frame_id):
                self._dropped_frames += 1
                return False
            self._frames[key] = frame
            if self._awaiting_frame_id is None:
                self._awaiting_frame_id = int(frame.frame_id)
        self._wake.set()
        return True

    def drain_intents(self, limit: int = 12) -> list[BotIntent]:
        """Return at most ``limit`` newest worker results."""

        result: list[BotIntent] = []
        with self._lock:
            for _ in range(min(max(0, int(limit)), len(self._intents))):
                result.append(self._intents.popleft())
        return result

    def status(self) -> WorkerStatus:
        """Return the same operational shape as the process supervisor."""

        with self._lock:
            running = bool(
                self._running
                and self._ready
                and self._thread is not None
                and self._thread.is_alive()
            )
            awaiting = self._awaiting_frame_id
            silence = (
                max(0.0, time.monotonic() - self._last_processed_at)
                if running and awaiting is not None
                else 0.0
            )
            return WorkerStatus(
                running=running,
                process_id=None,
                restarts=int(self._restarts),
                stalled_restarts=0,
                intent_silence_seconds=silence,
                queued_frames=len(self._frames),
                queued_intents=len(self._intents),
                pending_terrain_cells=len(self._terrain_pending),
                dropped_frames=int(self._dropped_frames),
                dropped_intents=int(self._dropped_intents),
                snapshot_required=(
                    len(self._terrain_pending)
                    >= _TERRAIN_REBASE_THRESHOLD
                ),
                awaiting_frame_id=awaiting,
                last_acknowledged_frame_id=(
                    int(self._last_acknowledged_frame_id)
                ),
                last_heartbeat_batch_id=int(self._last_batch_id),
                last_heartbeat_frame_id=(
                    int(self._last_acknowledged_frame_id)
                ),
                awaiting_snapshot_transfer_id=None,
            )

    def _worker_main(self) -> None:
        world = SimpleVoxelWorld()
        brain = SimpleBotBrain(world, decision_hz=self.decision_hz)
        applied_snapshot_serial = -1
        batch_id = 0
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop.is_set():
                break

            with self._lock:
                snapshot_serial = int(self._snapshot_serial)
                snapshot = None
                if (
                    self._latest_snapshot is not None
                    and snapshot_serial != applied_snapshot_serial
                ):
                    snapshot = replace(
                        self._latest_snapshot,
                        topology_version=int(self._terrain_version),
                        changed_cells=tuple(self._terrain_overlay.values()),
                    )
                    self._terrain_pending.clear()
                changes = tuple(self._terrain_pending.values())
                self._terrain_pending.clear()
                map_epoch = int(self._terrain_map_epoch)
                topology_version = int(self._terrain_version)
                frames = tuple(
                    sorted(
                        self._frames.values(),
                        key=lambda frame: int(frame.frame_id),
                    )
                )
                self._frames.clear()

            try:
                if snapshot is not None:
                    world.load(snapshot)
                    # Every full snapshot is a clean ownership boundary. A
                    # periodic same-map recycle has the same map epoch, so a
                    # guarded reset_for_map alone would retain team caches.
                    brain = SimpleBotBrain(
                        world,
                        decision_hz=self.decision_hz,
                    )
                    brain.reset_for_map(snapshot.map_epoch)
                    applied_snapshot_serial = snapshot_serial
                    with self._lock:
                        self._ready = True
                elif changes:
                    world.apply(
                        WorldDelta(
                            map_epoch,
                            topology_version,
                            changes,
                        )
                    )

                intents: list[BotIntent] = []
                processed_frame_id = -1
                for frame in frames:
                    processed_frame_id = max(
                        processed_frame_id,
                        int(frame.frame_id),
                    )
                    if (
                        int(frame.map_epoch) != int(world.map_epoch)
                        or int(frame.topology_version)
                        != int(world.topology_version)
                    ):
                        continue
                    intent = brain.decide(frame)
                    if intent is not None:
                        intents.append(intent)
            except Exception:
                # This is the owner-thread fault boundary, not an ignored
                # decision error. Recreate all private state from the retained
                # canonical snapshot on the next batch.
                logger.exception("Bounded AI thread batch failed; rebuilding")
                world = SimpleVoxelWorld()
                brain = SimpleBotBrain(world, decision_hz=self.decision_hz)
                applied_snapshot_serial = -1
                with self._lock:
                    self._restarts += 1
                    self._ready = False
                continue

            batch_id += 1
            processed_at = time.monotonic()
            with self._lock:
                for intent in intents:
                    if len(self._intents) >= _INTENT_LIMIT:
                        self._intents.popleft()
                        self._dropped_intents += 1
                    self._intents.append(intent)
                self._last_processed_at = processed_at
                self._last_batch_id = batch_id
                self._last_acknowledged_frame_id = max(
                    self._last_acknowledged_frame_id,
                    processed_frame_id,
                )
                if (
                    self._awaiting_frame_id is not None
                    and processed_frame_id >= self._awaiting_frame_id
                ):
                    self._awaiting_frame_id = None

        with self._lock:
            self._running = False
            self._ready = False


__all__ = ["AIThreadSupervisor"]
