"""Real thread handoff regressions for fleet-wide intent starvation."""

from __future__ import annotations

from dataclasses import replace
import asyncio
import time
import threading

import pytest

from server.bot_ai import thread_supervisor as worker
from server.bot_ai.messages import BotIntent, MapSnapshot, MovementIntent, PerceptionFrame, VoxelChange
from server.bot_ai.simple_worker import _process_worker_batch
from server.game_constants import TEAM1
from .test_bot_architecture import _facing_fixture, _player_snapshot


class _World:
    map_epoch = -1
    topology_version = -1

    def __init__(self, *, planning_budget=None):
        self.planning_budget = planning_budget

    def begin_planning(self, observer, now):
        pass

    def end_planning(self, **_kwargs):
        pass

    def load(self, snapshot):
        self.map_epoch = snapshot.map_epoch
        self.topology_version = snapshot.topology_version

    def apply(self, delta):
        self.topology_version = delta.topology_version


class _Brain:
    poison_id: int | None = None

    def __init__(self, world, **kwargs):
        self.world = world

    def reset_for_map(self, epoch):
        pass

    def reset_bot(self, bot_id, generation):
        pass

    def decide(self, frame):
        if frame.observer_id == self.poison_id:
            raise ValueError("injected single-bot decision failure")
        return BotIntent(
            bot_id=frame.observer_id, bot_generation=frame.observer_generation,
            frame_id=frame.frame_id, map_epoch=frame.map_epoch, mode_epoch=frame.mode_epoch,
            topology_version=frame.topology_version, created_at=frame.created_at,
            expires_at=frame.created_at + 0.4, movement=MovementIntent(direction=(1.0, 0.0, 0.0)),
        )


def _frame(frame_id: int, *, bot_id: int = 1, topology: int = 0) -> PerceptionFrame:
    observer = _player_snapshot(bot_id, TEAM1, (10.0, 10.0, 20.0), is_bot=True)
    return PerceptionFrame(frame_id=frame_id, map_epoch=1, mode_epoch=1,
                           topology_version=topology, observer_id=bot_id,
                           observer_generation=1, created_at=time.monotonic(),
                           mode_id="tdm", players=(observer,))


def _wait_intent(supervisor, expected_id: int) -> BotIntent | None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        for intent in supervisor.drain_intents():
            if intent.bot_id == expected_id:
                return intent
        time.sleep(0.005)
    return None


@pytest.fixture
def supervisor(monkeypatch):
    monkeypatch.setattr(worker, "SimpleVoxelWorld", _World)
    monkeypatch.setattr(worker, "SimpleBotBrain", _Brain)
    monkeypatch.setattr(_Brain, "poison_id", None)
    supervisor = worker.AIThreadSupervisor()
    supervisor.start(MapSnapshot(1, 0, b"", "tdm", "liveness"))
    try:
        yield supervisor
    finally:
        supervisor.close()


def test_terrain_churn_does_not_starve_real_worker_intents(supervisor) -> None:
    for version in range(1, 21):
        supervisor.publish_world_change(VoxelChange(10, 10, 22, bool(version % 2), 0),
                                        map_epoch=1, topology_version=version)
        # Gameplay captured the roster just before the next terrain commit.
        supervisor.submit_frame(_frame(version, topology=version - 1))
        intent = _wait_intent(supervisor, 1)
        assert intent is not None, f"worker starved at terrain version {version}"
        assert intent.topology_version == version


def test_one_failed_bot_does_not_restart_or_freeze_the_other_bots(supervisor, monkeypatch) -> None:
    monkeypatch.setattr(_Brain, "poison_id", 1)
    supervisor.submit_frame(_frame(1))
    supervisor.submit_frame(_frame(2, bot_id=2))
    assert _wait_intent(supervisor, 2) is not None
    assert supervisor.status().restarts == 0
    monkeypatch.setattr(_Brain, "poison_id", None)
    supervisor.submit_frame(_frame(3))
    assert _wait_intent(supervisor, 1) is not None


def test_process_backend_also_handles_terrain_churn_and_a_failed_observer(monkeypatch) -> None:
    world = _World()
    world.load(MapSnapshot(1, 8, b"", "tdm", "liveness"))
    brain = _Brain(world)
    monkeypatch.setattr(_Brain, "poison_id", 1)
    shutdown, intents = _process_worker_batch(world, brain, (
        _frame(1, topology=7), _frame(2, bot_id=2, topology=7),
        _frame(3, bot_id=3, topology=9),
    ))
    assert not shutdown
    assert [intent.bot_id for intent in intents] == [2]
    assert intents[0].topology_version == 8


def test_unexpected_thread_exit_recovers_with_live_terrain(monkeypatch) -> None:
    monkeypatch.setattr(worker, "SimpleVoxelWorld", _World)
    monkeypatch.setattr(worker, "SimpleBotBrain", _Brain)
    supervisor = worker.AIThreadSupervisor()
    run = supervisor._worker_main
    supervisor._worker_main = lambda: None
    supervisor.start(MapSnapshot(1, 0, b"", "tdm", "liveness"))
    supervisor._thread.join(2.0)
    supervisor.publish_world_change(VoxelChange(10, 10, 22, True, 0),
                                    map_epoch=1, topology_version=1)
    supervisor._worker_main = run
    try:
        assert supervisor.recover_if_stopped()
        supervisor.submit_frame(_frame(1, topology=1))
        intent = _wait_intent(supervisor, 1)
        assert intent is not None and intent.topology_version == 1
        assert supervisor.status().restarts == 1
        assert supervisor._latest_snapshot.changed_cells
    finally:
        supervisor.close()
    assert not supervisor.recover_if_stopped()


def test_slow_shutdown_never_starts_a_second_owner_thread() -> None:
    supervisor = worker.AIThreadSupervisor()
    entered = threading.Event()
    release = threading.Event()

    def blocked_owner():
        entered.set()
        release.wait(2.0)

    supervisor._worker_main = blocked_owner
    snapshot = MapSnapshot(1, 0, b"", "tdm", "shutdown")
    supervisor.start(snapshot)
    assert entered.wait(2.0)
    owner = supervisor._thread
    try:
        supervisor.close(timeout=0.0)
        assert supervisor._thread is owner
        with pytest.raises(RuntimeError, match="still shutting down"):
            supervisor.start(snapshot)
    finally:
        release.set()
        owner.join(2.0)
        supervisor.close()
    assert supervisor._thread is None


def test_director_accepts_fresh_movement_across_unrelated_terrain_changes() -> None:
    _, director, bot, runtime = _facing_fixture()
    now = time.monotonic()
    intent = BotIntent(
        bot_id=bot.id, bot_generation=runtime.generation, frame_id=1,
        map_epoch=director._map_epoch, mode_epoch=director._mode_epoch,
        topology_version=director._topology_version, created_at=now, expires_at=now + 0.4,
        movement=MovementIntent(direction=(1.0, 0.0, 0.0)),
    )
    director._topology_version += 1
    director.supervisor.drain_intents = lambda **kwargs: [intent]
    director._drain_intents(now)
    assert runtime.intent is intent
    for invalid in (
        replace(intent, frame_id=2, topology_version=director._topology_version + 1),
        replace(intent, frame_id=2, map_epoch=director._map_epoch + 1),
        replace(intent, frame_id=2, mode_epoch=director._mode_epoch + 1),
        replace(intent, frame_id=2, bot_generation=runtime.generation + 1),
        replace(intent, frame_id=2, expires_at=now),
    ):
        director.supervisor.drain_intents = lambda **kwargs: [invalid]
        director._drain_intents(now)
        assert runtime.intent is intent


def test_perception_refresh_cannot_permanently_starve_one_motor_phase(monkeypatch) -> None:
    server, director, bot, _ = _facing_fixture()
    director._started = True
    director._next_population_at = float("inf")
    director._refresh_epochs = lambda: None
    director._drain_intents = lambda now: None
    # An exact 10 Hz roster refresh used to skip bot id % 6 on every cycle.
    director._publish_due_perception = lambda now: server.loop_count % 6 == bot.id % 6
    updates: list[tuple[int, float]] = []
    director._apply_motor = lambda runtime, now, dt: updates.append((server.loop_count, dt))
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + server.loop_count / 60.0)

    async def run():
        for tick in range(1, 121):
            server.loop_count = tick
            await director.update(1.0 / 60.0)

    asyncio.run(run())
    assert len(updates) >= 15, updates
    assert max(b[0] - a[0] for a, b in zip(updates, updates[1:])) <= 8
    assert all(0.1 <= dt <= 0.14 for _, dt in updates[1:])
