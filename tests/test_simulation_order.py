"""Causal ordering regressions for the fixed-step gameplay runtime."""

import asyncio
from types import SimpleNamespace

import pytest

from server.simulation_runtime import SimulationRuntime
from tests.test_reversed_world_update import make_player


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
