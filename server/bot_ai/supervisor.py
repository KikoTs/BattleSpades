"""Bounded non-blocking bridge to the isolated AI worker process."""

from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import dataclass, replace

from .messages import (
    BotIntent,
    MapSnapshot,
    PerceptionFrame,
    VoxelChange,
    WorkerShutdown,
    WorldDelta,
)
from .worker import run_worker


logger = logging.getLogger(__name__)

_SERVER_TO_BRIDGE_LIMIT = 64
_BRIDGE_TO_SERVER_LIMIT = 128
_TERRAIN_SNAPSHOT_THRESHOLD = 65_536
_RESTART_BACKOFF = (1.0, 2.0, 5.0, 30.0)
_WORKER_STARTUP_GRACE_SECONDS = 8.0
_WORKER_STALL_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    """Operational snapshot safe to display through ``/bots status``."""

    running: bool
    process_id: int | None
    restarts: int
    stalled_restarts: int
    intent_silence_seconds: float
    queued_frames: int
    queued_intents: int
    pending_terrain_cells: int
    dropped_frames: int
    dropped_intents: int
    snapshot_required: bool


class AIWorkerSupervisor:
    """Own a Windows-safe spawned worker and its bridge thread.

    Gameplay code only touches bounded in-process queues and small protected
    dictionaries.  Process creation, pickle serialization, pipe writes, result
    reads, health monitoring, and restart backoff all run on ``BotAIBridge``.
    """

    def __init__(
        self,
        *,
        seed: int = 0,
        decision_hz: float = 8.0,
        path_requests_per_second: float = 24.0,
    ) -> None:
        self.seed = int(seed)
        self.decision_hz = max(1.0, float(decision_hz))
        self.path_requests_per_second = max(
            1.0, float(path_requests_per_second)
        )
        # Perception frames are replaceable snapshots, not ordered gameplay
        # events. Coalesce by concrete bot life so a slow worker never spends
        # seconds replaying obsolete decisions for the same player.
        self._frame_lock = threading.Lock()
        self._frames: dict[tuple[int, int], PerceptionFrame] = {}
        self._intents: queue.Queue[BotIntent] = queue.Queue(
            maxsize=_BRIDGE_TO_SERVER_LIMIT
        )
        self._terrain_lock = threading.Lock()
        self._pending_terrain: dict[tuple[int, int, int], VoxelChange] = {}
        self._terrain_overlay: dict[tuple[int, int, int], VoxelChange] = {}
        self._terrain_map_epoch = 0
        self._terrain_version = 0
        self._snapshot_required = False
        self._snapshot_lock = threading.Lock()
        self._latest_snapshot: MapSnapshot | None = None
        self._snapshot_serial = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._running = False
        self._process_id: int | None = None
        self._restarts = 0
        self._stalled_restarts = 0
        self._dropped_frames = 0
        self._dropped_intents = 0
        # The child can remain alive while blocked inside a native path query.
        # Track an unanswered live-bot frame as a heartbeat lease so the
        # bridge can reap that wedged process without touching gameplay.
        self._worker_started_at = 0.0
        self._last_intent_at = 0.0
        self._awaiting_intent_since: float | None = None

    def start(self, snapshot: MapSnapshot) -> None:
        """Start supervision and publish the first map without blocking spawn."""

        self.publish_map(snapshot)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._bridge_main,
            name="BotAIBridge",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = 3.0) -> None:
        """Stop supervision and reap the owned child process."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        self._thread = None

    def publish_map(self, snapshot: MapSnapshot) -> None:
        """Replace the navigation base; serialization happens off-thread."""

        with self._snapshot_lock:
            with self._terrain_lock:
                self._latest_snapshot = snapshot
                self._snapshot_serial += 1
                self._pending_terrain.clear()
                self._terrain_overlay.clear()
                self._terrain_map_epoch = int(snapshot.map_epoch)
                self._terrain_version = int(snapshot.topology_version)
                self._snapshot_required = False

    def publish_world_change(
        self,
        change: VoxelChange,
        *,
        map_epoch: int,
        topology_version: int,
    ) -> None:
        """Coalesce a canonical mutation without ever blocking gameplay.

        At the hard cell threshold a full worker snapshot is requested.  The
        director notices this flag on its next tick and publishes current VXL,
        so an overflow never silently loses navigation state.
        """

        with self._terrain_lock:
            self._terrain_map_epoch = int(map_epoch)
            self._terrain_version = max(
                self._terrain_version, int(topology_version)
            )
            self._terrain_overlay[change.coordinate] = change
            self._pending_terrain[change.coordinate] = change
            if len(self._pending_terrain) >= _TERRAIN_SNAPSHOT_THRESHOLD:
                # The bridge composes raw base + full overlay on its own
                # thread. Gameplay never calls generate_vxl for this recovery.
                self._snapshot_required = True

    @property
    def snapshot_required(self) -> bool:
        """Return whether terrain coalescing crossed its safe hard limit."""

        with self._terrain_lock:
            return self._snapshot_required

    def submit_frame(self, frame: PerceptionFrame) -> bool:
        """Coalesce one strategic frame without delaying the gameplay tick."""

        key = int(frame.observer_id), int(frame.observer_generation)
        dropped = 0
        with self._frame_lock:
            # Reinsert replacements so dictionary order reflects freshness.
            self._frames.pop(key, None)
            if len(self._frames) >= _SERVER_TO_BRIDGE_LIMIT:
                oldest = next(iter(self._frames))
                self._frames.pop(oldest, None)
                dropped = 1
            self._frames[key] = frame
        if dropped:
            with self._status_lock:
                self._dropped_frames += dropped
        return True

    def drain_intents(self, limit: int = 12) -> list[BotIntent]:
        """Return at most ``limit`` worker results without waiting."""

        result: list[BotIntent] = []
        for _ in range(max(0, int(limit))):
            try:
                result.append(self._intents.get_nowait())
            except queue.Empty:
                break
        return result

    def status(self) -> WorkerStatus:
        """Return a lock-bounded operational snapshot."""

        with self._status_lock:
            running = self._running
            process_id = self._process_id
            restarts = self._restarts
            stalled_restarts = self._stalled_restarts
            dropped_frames = self._dropped_frames
            dropped_intents = self._dropped_intents
            last_intent_at = self._last_intent_at
            awaiting_intent_since = self._awaiting_intent_since
            intent_silence_seconds = (
                max(
                    0.0,
                    time.monotonic()
                    - max(last_intent_at, awaiting_intent_since),
                )
                if running and awaiting_intent_since is not None
                else 0.0
            )
        with self._terrain_lock:
            pending = len(self._pending_terrain)
            snapshot_required = self._snapshot_required
        return WorkerStatus(
            running=running,
            process_id=process_id,
            restarts=restarts,
            stalled_restarts=stalled_restarts,
            intent_silence_seconds=intent_silence_seconds,
            queued_frames=self._queued_frame_count(),
            queued_intents=self._intents.qsize(),
            pending_terrain_cells=pending,
            dropped_frames=dropped_frames,
            dropped_intents=dropped_intents,
            snapshot_required=snapshot_required,
        )

    def _bridge_main(self) -> None:
        context = mp.get_context("spawn")
        process = None
        process_input = None
        process_output = None
        sent_snapshot_serial = -1
        failure_count = 0
        next_start_at = 0.0

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if process is None or not process.is_alive():
                    if process is not None:
                        exit_code = process.exitcode
                        self._close_process_queues(process_input, process_output)
                        process.join(timeout=0.1)
                        process = None
                        process_input = None
                        process_output = None
                        failure_count += 1
                        delay = _RESTART_BACKOFF[
                            min(failure_count - 1, len(_RESTART_BACKOFF) - 1)
                        ]
                        next_start_at = now + delay
                        with self._status_lock:
                            self._running = False
                            self._process_id = None
                            self._restarts += 1
                            self._awaiting_intent_since = None
                        logger.warning(
                            "AI worker exited code=%s; restart in %.1fs",
                            exit_code,
                            delay,
                        )
                    if now < next_start_at:
                        self._stop_event.wait(min(0.05, next_start_at - now))
                        continue
                    process_input = context.Queue(maxsize=_SERVER_TO_BRIDGE_LIMIT)
                    process_output = context.Queue(maxsize=_BRIDGE_TO_SERVER_LIMIT)
                    process = context.Process(
                        target=run_worker,
                        args=(
                            process_input,
                            process_output,
                            self.seed,
                            self.decision_hz,
                            self.path_requests_per_second,
                        ),
                        name="BattleSpadesAI",
                        daemon=True,
                    )
                    process.start()
                    sent_snapshot_serial = -1
                    started_at = time.monotonic()
                    with self._status_lock:
                        self._running = True
                        self._process_id = process.pid
                        self._worker_started_at = started_at
                        self._last_intent_at = started_at
                        self._awaiting_intent_since = None
                    logger.info("AI worker started pid=%s", process.pid)

                sent_snapshot_serial = self._send_snapshot_if_needed(
                    process_input, sent_snapshot_serial
                )
                self._send_pending_terrain(process_input)
                self._send_frames(process_input)
                self._receive_intents(process_output)
                if self._worker_is_stalled(time.monotonic()):
                    logger.error(
                        "AI worker pid=%s stopped returning intentions; "
                        "terminating wedged process",
                        process.pid,
                    )
                    process.terminate()
                    process.join(timeout=0.5)
                    with self._status_lock:
                        self._stalled_restarts += 1
                        self._awaiting_intent_since = None
                    continue
                self._stop_event.wait(0.005)
        except (OSError, RuntimeError):
            logger.exception("AI bridge failed")
        finally:
            if process is not None and process.is_alive():
                try:
                    process_input.put_nowait(WorkerShutdown())
                except (AttributeError, OSError, queue.Full):
                    pass
                process.join(timeout=1.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1.0)
            self._close_process_queues(process_input, process_output)
            with self._status_lock:
                self._running = False
                self._process_id = None

    def _send_snapshot_if_needed(self, process_input, sent_serial: int) -> int:
        with self._snapshot_lock:
            with self._terrain_lock:
                serial = self._snapshot_serial
                base_snapshot = self._latest_snapshot
                rebase = self._snapshot_required
                overlay = tuple(self._terrain_overlay.values())
                captured_pending = dict(self._pending_terrain)
                topology_version = self._terrain_version
        if base_snapshot is None or (serial == sent_serial and not rebase):
            return sent_serial
        snapshot = replace(
            base_snapshot,
            topology_version=int(topology_version),
            changed_cells=overlay,
        )
        try:
            process_input.put_nowait(snapshot)
        except (OSError, queue.Full):
            return sent_serial
        # Retain any mutation that raced snapshot composition. Equality is
        # sufficient because a same-value rewrite is idempotent.
        with self._terrain_lock:
            for coordinate, change in captured_pending.items():
                if self._pending_terrain.get(coordinate) == change:
                    self._pending_terrain.pop(coordinate, None)
            self._snapshot_required = (
                len(self._pending_terrain) >= _TERRAIN_SNAPSHOT_THRESHOLD
            )
        return serial

    def _send_pending_terrain(self, process_input) -> None:
        with self._terrain_lock:
            if not self._pending_terrain or self._snapshot_required:
                return
            changes = tuple(self._pending_terrain.values())
            map_epoch = self._terrain_map_epoch
            version = self._terrain_version
            self._pending_terrain.clear()
        delta = WorldDelta(map_epoch, version, changes)
        try:
            process_input.put_nowait(delta)
        except (OSError, queue.Full):
            # Merge back: newer changes for the same coordinate win.
            with self._terrain_lock:
                for change in changes:
                    self._pending_terrain.setdefault(change.coordinate, change)

    def _send_frames(self, process_input) -> None:
        with self._frame_lock:
            batch = list(self._frames.items())[:16]
            for key, _frame in batch:
                self._frames.pop(key, None)
        for index, (_key, frame) in enumerate(batch):
            try:
                process_input.put_nowait(frame)
                self._note_frame_sent(frame, time.monotonic())
            except (OSError, queue.Full):
                # The process pipe is temporarily full. Requeue only frames
                # that have not already been superseded on the game thread.
                with self._frame_lock:
                    for pending_key, pending_frame in batch[index:]:
                        if pending_key not in self._frames:
                            self._frames[pending_key] = pending_frame
                return

    def _note_frame_sent(self, frame: PerceptionFrame, now: float) -> None:
        """Start a heartbeat lease for a live bot's unanswered frame."""

        observer = next(
            (
                player
                for player in frame.players
                if int(player.player_id) == int(frame.observer_id)
                and int(player.generation) == int(frame.observer_generation)
            ),
            None,
        )
        if (
            observer is None
            or not bool(observer.alive)
            or not bool(observer.spawned)
        ):
            return
        with self._status_lock:
            if self._awaiting_intent_since is None:
                self._awaiting_intent_since = float(now)

    def _worker_is_stalled(self, now: float) -> bool:
        """Return true when live frames receive no child result for too long."""

        with self._status_lock:
            waiting_since = self._awaiting_intent_since
            started_at = self._worker_started_at
        if waiting_since is None or started_at <= 0.0:
            return False
        if float(now) - started_at < _WORKER_STARTUP_GRACE_SECONDS:
            return False
        return float(now) - waiting_since >= _WORKER_STALL_TIMEOUT_SECONDS

    def _queued_frame_count(self) -> int:
        """Return the coalesced server-to-bridge frame count."""

        with self._frame_lock:
            return len(self._frames)

    def _receive_intents(self, process_output) -> None:
        for _ in range(32):
            try:
                intent = process_output.get_nowait()
            except (OSError, queue.Empty):
                return
            received_at = time.monotonic()
            with self._status_lock:
                self._last_intent_at = received_at
                self._awaiting_intent_since = None
            try:
                self._intents.put_nowait(intent)
            except queue.Full:
                # Prefer the newest result and keep the server-facing queue
                # bounded. Expiry/generation checks still apply when drained.
                try:
                    self._intents.get_nowait()
                    self._intents.put_nowait(intent)
                except (queue.Empty, queue.Full):
                    pass
                with self._status_lock:
                    self._dropped_intents += 1

    @staticmethod
    def _close_process_queues(process_input, process_output) -> None:
        for process_queue in (process_input, process_output):
            if process_queue is None:
                continue
            try:
                process_queue.cancel_join_thread()
                process_queue.close()
            except (AttributeError, OSError, ValueError):
                continue
