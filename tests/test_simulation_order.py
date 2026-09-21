"""Causal ordering regressions for the fixed-step gameplay runtime."""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from server.simulation_runtime import SimulationRuntime
from tests.test_reversed_world_update import make_player


def test_scheduler_recovers_input_backlog_after_snapshot_stall(monkeypatch):
    """A 250 ms late-join hitch must not leave movement 10 frames behind."""
    import server.simulation_runtime as runtime_module

    interval = 1.0 / 60.0
    clock = [0.0]
    waiting = deque()
    arrived = 0
    applied = []
    published = []
    batch_sizes = []
    batch_steps = 0
    server = SimpleNamespace(
        running=True,
        tick_interval=interval,
        loop_count=0,
        _broadcast_world_updates=lambda: published.append(server.loop_count),
    )

    async def step():
        nonlocal batch_steps
        batch_steps += 1
        # Exactly one observed row per complete simulation tick; no batching
        # inside player physics and no fabricated sequence labels.
        if waiting:
            applied.append(waiting.popleft())

    async def sleep(delay):
        nonlocal arrived, batch_steps
        if delay == 0:
            return
        batch_sizes.append(batch_steps)
        batch_steps = 0
        # The first event-loop turn blocks during a full map snapshot. The
        # independent client keeps producing one input every 1/60 second.
        clock[0] += 0.250 if clock[0] == 0.0 else 0.001
        expected = int((clock[0] + 1e-9) / interval)
        while arrived < expected:
            arrived += 1
            waiting.append(1000 + arrived)
        if clock[0] >= 1.0:
            server.running = False

    monkeypatch.setattr(
        runtime_module, "time", SimpleNamespace(perf_counter=lambda: clock[0])
    )
    monkeypatch.setattr(runtime_module, "asyncio", SimpleNamespace(sleep=sleep))
    runtime = SimulationRuntime(server)
    runtime.step = step
    asyncio.run(runtime.run())

    # At most the current, not-yet-simulated tick remains. The former elapsed
    # time clamp leaves ten or eleven rows queued indefinitely in this case.
    assert len(waiting) <= 1
    assert applied == list(range(1001, 1001 + len(applied)))
    assert published == list(range(1, server.loop_count + 1))
    assert max(batch_sizes) == runtime.MAX_CATCH_UP_STEPS
    assert server.loop_count >= 59


def test_scheduler_bounds_suspend_recovery_and_stops_between_ticks(monkeypatch):
    """Long suspension debt is bounded and stopping cancels the next tick."""
    import server.simulation_runtime as runtime_module

    clock = [0.0]
    ticks = []
    publications = []
    server = SimpleNamespace(
        running=True,
        tick_interval=1.0 / 60.0,
        loop_count=0,
        _broadcast_world_updates=lambda: publications.append(server.loop_count),
    )
    turns = 0

    async def step():
        ticks.append(server.loop_count)

    async def sleep(delay):
        nonlocal turns
        if delay:
            turns += 1
            if turns == 1:
                clock[0] = 600.0
            elif turns > 30:
                server.running = False

    monkeypatch.setattr(
        runtime_module, "time", SimpleNamespace(perf_counter=lambda: clock[0])
    )
    monkeypatch.setattr(runtime_module, "asyncio", SimpleNamespace(sleep=sleep))
    runtime = SimulationRuntime(server)
    runtime.step = step
    asyncio.run(runtime.run())
    assert 119 <= len(ticks) <= 120
    assert publications == ticks

    async def stop_after_one_tick(delay):
        if delay == 0:
            server.running = False
        else:
            clock[0] += 0.250

    monkeypatch.setattr(
        runtime_module, "asyncio", SimpleNamespace(sleep=stop_after_one_tick)
    )
    ticks.clear()
    publications.clear()
    server.running = True
    asyncio.run(runtime.run())
    assert len(ticks) == 1
    assert publications == ticks


def test_projectile_impacts_are_applied_before_player_physics():
    """Damage(37) reaches retail before its next GameScene physics frame."""
    calls = []

    class Player:
        id = 1

        async def simulate_tick(self, _dt):
            calls.append("players")

    class Plugins:
        async def call_event(self, *_args, **_kwargs):
            return None

    async def respawns():
        return None

    server = SimpleNamespace(
        tick_interval=1.0 / 60.0,
        loop_count=1,
        tick_rate=60,
        players={1: Player()},
        bots=None,
        mode=None,
        _mode_events=[],
        config=SimpleNamespace(
            plugin_event_budget_ms=2.0,
            entity_tick_batch_limit=8192,
        ),
        metrics=SimpleNamespace(
            record_subsystem=lambda *_args: None,
            record_tick=lambda *_args: None,
            skipped_entity_ticks=0,
        ),
        _drain_ingame_packets=lambda: _async_none(),
        world_mutations=SimpleNamespace(commit_ready=lambda: None),
        terrain_repair=SimpleNamespace(tick=lambda: None),
        a2s_handler=SimpleNamespace(update=lambda: None),
        plugin_manager=Plugins(),
        _process_respawns=respawns,
        entity_registry=SimpleNamespace(tick=lambda *_args, **_kwargs: 0),
        _build_entity_ctx=lambda: None,
        rocket_turret_controller=SimpleNamespace(update=lambda *_args: None),
        _update_grenades=lambda _dt: calls.append("projectiles"),
        fire_controller=SimpleNamespace(update=lambda: None),
        vote_manager=SimpleNamespace(active=False),
    )

    asyncio.run(SimulationRuntime(server).step())

    assert calls.index("projectiles") < calls.index("players")


async def _async_none():
    return None


def test_mode_event_drain_remains_bounded_before_clock_checks():
    calls = []
    events = deque([("on_player_kill", (7,)), ("on_player_kill", (8,))])

    async def on_kill(player_id):
        calls.append(("kill", player_id))
        events.append(("on_player_kill", (9,)))

    async def on_tick(tick):
        calls.append(("tick", tick))

    async def plugin_event(name, player_id):
        calls.append((name, player_id))

    server = SimpleNamespace(
        mode=SimpleNamespace(on_player_kill=on_kill, on_tick=on_tick),
        config=SimpleNamespace(mode_event_drain_budget=1),
        loop_count=60,
        _mode_events=events,
        plugin_manager=SimpleNamespace(call_event=plugin_event),
    )
    asyncio.run(SimulationRuntime(server)._tick_mode())
    assert calls == [("kill", 7), ("on_player_kill", 7), ("tick", 60)]
    assert list(events) == [("on_player_kill", (8,)), ("on_player_kill", (9,))]


def test_velocity_impulse_waits_for_the_target_client_loop_label():
    """A Snowball at server loop L must affect authoritative input frame L."""
    player, _connection = make_player()
    observed = []

    async def observe_update(_dt):
        observed.append((player.last_applied_input_loop, player.velocity))

    player.update = observe_update
    player.queue_velocity_impulse(102, (0.3, 0.0, -0.1))
    for loop in (100, 101, 102):
        player.record_input_frame(
            loop, (False,) * 8, (1.0, 0.0, 0.0)
        )
        asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert observed[0][1] == pytest.approx((0.0, 0.0, 0.0))
    assert observed[1][1] == pytest.approx((0.0, 0.0, 0.0))
    assert observed[2][1] == pytest.approx((0.3, 0.0, -0.1))


def test_explosion_impulse_recomputes_after_two_observed_input_frames():
    """Deferred Damage prediction uses target geometry at application time."""

    player, _connection = make_player()
    observed = []

    async def observe_update(_dt):
        observed.append((player.position, player.velocity))

    player.update = observe_update
    player.position = (1.0, 0.0, 0.0)
    player.queue_explosion_impulse(
        2, (0.0, 0.0, 0.0), 16.0, 0.3, 0.3
    )
    player.record_input_frame(100, (False,) * 8, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    # Move between impact detection and Damage's predicted history row.  A
    # frozen vector would still point +X; retail recomputes and points +Y.
    player.position = (0.0, 2.0, 0.0)
    player.record_input_frame(103, (False,) * 8, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert observed[0][1] == pytest.approx((0.0, 0.0, 0.0))
    assert observed[1][1] == pytest.approx((0.0, 0.3, 0.0))
