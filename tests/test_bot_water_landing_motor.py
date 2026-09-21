"""A completed native shore landing consumes the stale water movement lease."""

import asyncio
from dataclasses import replace
import math
import time
from types import SimpleNamespace

import pytest

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotIntent, BotIntentPriority, LookIntent, MovementAffordance, MovementIntent,
)
from server.config import ServerConfig
from server.main import BattleSpadesServer


async def _landing_fixture():
    server = BattleSpadesServer(ServerConfig())
    assert server.world_manager.load_map("London")
    # The candidate-7 match built this single-cell foothold beside the cliff.
    server.world_manager.set_block(296, 228, 238, True, 0x123456)
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = await director.add_bot(team=3, class_id=12)
    assert bot is not None
    runtime = director._runtime[bot.id]
    bot.set_position(296.50982666015625, 227.97491455078125, 234.8914031982422)
    bot._world_object.set_velocity(-.0207777, .101209, .260736)
    bot.set_orientation_vector(0., 1., 0.)
    runtime.motor.yaw = math.pi / 2
    # Initialize the native airborne flag after moving the spawned body.
    await bot.simulate_tick(1. / 60.)
    now = time.monotonic()
    runtime.wade_observed_until = now + .5
    runtime.intent = BotIntent(
        bot.id, runtime.generation, 1, 1, 0, server.world_manager.topology_version,
        now, now + .4,
        movement=MovementIntent(direction=(-.01871, .99982, 0.), jump=True,
                                sprint=True, affordance=MovementAffordance.JUMP),
        look=LookIntent((296.5, 228.5, 235.75)),
        priority=BotIntentPriority.SURVIVAL, debug_role="water_exit",
    )
    return server, director, bot, runtime, now


def test_recorded_london_shore_jump_cannot_bounce_off_its_completed_landing():
    async def scenario():
        server, director, bot, runtime, now = await _landing_fixture()
        landed_z = None
        for tick in range(24):
            server.loop_count = tick
            if tick % 6 == 0:
                director._apply_motor(runtime, now + tick / 60., .1)
            await bot.simulate_tick(1. / 60.)
            director.observe_player_physics(bot, now + tick / 60.)
            if bot.grounded and not bot.wade and landed_z is None:
                landed_z = bot.z
            if landed_z is not None:
                assert not bot.input.jump
                assert bot.z >= landed_z - .2
        assert landed_z is not None
        assert bot.grounded and not bot.wade
        assert server.world_manager.spawn_position_is_safe(bot.position)

    asyncio.run(scenario())


@pytest.mark.parametrize("affordance", (MovementAffordance.SWIM, MovementAffordance.JUMP))
def test_prelanding_worker_snapshot_cannot_rearm_water_motion_but_fresh_jump_can(affordance):
    async def scenario():
        server, director, bot, runtime, now = await _landing_fixture()
        for tick in range(10):
            server.loop_count = tick
            director._apply_motor(runtime, now + tick / 60., 1. / 60.)
            await bot.simulate_tick(1. / 60.)
            director.observe_player_physics(bot, now + tick / 60.)
            if bot.grounded and not bot.wade:
                break
        assert bot.grounded and not bot.wade
        # A different frame can still have been computed before landing.
        runtime.intent = replace(runtime.intent, frame_id=2, created_at=now + .001,
            movement=MovementIntent(direction=(0., 1., 0.), jump=True,
                                    sprint=True, affordance=affordance))
        server.loop_count = 20
        director._apply_motor(runtime, now + .2, .1)
        assert not bot.input.jump and not bot.input.sprint
        up, down, left, right, *_ = runtime.movement_input
        ox, oy = bot.orientation[:2]
        dx = (up - down) * ox - (right - left) * oy
        dy = (up - down) * oy + (right - left) * ox
        assert dx * bot.vx + dy * bot.vy <= 0.
        assert runtime.jump_until_loop == -1
        # Expiry can fall between slower motor updates. Do not leave the
        # last braking key held through those intervening native ticks.
        assert any(runtime.movement_input[:4])
        runtime.intent = replace(runtime.intent, expires_at=now + .25)
        director.observe_player_physics(bot, now + .26)
        assert runtime.movement_input == (False,) * 8
        # A new decision based on the dry state retains ordinary jump control.
        runtime.intent = replace(runtime.intent, frame_id=3, created_at=now + .3,
            expires_at=now + .7,
            movement=MovementIntent(jump=True, affordance=MovementAffordance.JUMP))
        server.loop_count = 30
        director._apply_motor(runtime, now + .3, .1)
        assert bot.input.jump
        await bot.simulate_tick(1. / 60.)
        assert bot.vz < 0.

    asyncio.run(scenario())
