"""Rocketeer/Engineer bots climb ledges with their packs; nicknames read human.

Pack behaviour measured on server physics (2026-09-25): the Rocketeer glide
pack (67) never lifts more than a jump, the Rocketeer burst pack (66) gains
~20 blocks per second of thrust and the Engineer pack (68) climbs steadily.
"""

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import MovementAffordance, VoxelChange, WorldDelta
from server.bot_ai.profiles import ProfileFactory
from server.bot_ai.simple_navigation import RouteStep, SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _movement_abilities
from server.config import ServerConfig
from server.main import BattleSpadesServer
from tests.test_simple_bot_tactics import _frame, _player


@pytest.mark.parametrize("pack,fuel,climbs,flies", [
    (C.JETPACK_NORMAL, 100., True, True),
    (C.JETPACK_NORMAL, 40., False, False),
    (C.JETPACK_ENGINEER, 100., True, True),
    (C.JETPACK2, 100., False, True),
])
def test_each_pack_advertises_what_it_can_physically_do(pack, fuel, climbs, flies):
    observer = replace(_player(1, 2, (10.5, 10.5, 17.75)),
                       jetpack_id=int(pack), jetpack_fuel=fuel, grounded=True)
    abilities = _movement_abilities(observer)
    assert (MovementAffordance.JETPACK in abilities) is flies
    assert (MovementAffordance.JETPACK_CLIMB in abilities) is climbs


async def _platform_scene(pack: int, height: int):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    server.bots = director
    bot = await director.add_bot(team=2, class_id=int(C.CLASS_ROCKETEER))
    bot.jetpack_id = int(pack)
    bot.jetpack_fuel = 100.0
    bot.set_position(100.5, 100.5, 59.75)
    for _ in range(20):
        server.loop_count += 1
        await bot.simulate_tick(1 / 60)
    ground = int(round(bot.position[2] + 2.25))
    changes = []
    for x in range(101, 106):
        for y in range(97, 104):
            for z in range(ground - height, ground):
                server.world_manager.set_block(x, y, z, True, 0x808080)
                changes.append(VoxelChange(x, y, z, True, 0x808080))
    # Worker maps start from the map file and receive runtime edits as deltas.
    world = SimpleVoxelWorld()
    world.load(director._make_map_snapshot(current=True))
    world.apply(WorldDelta(world.map_epoch,
                           server.world_manager.topology_version, tuple(changes)))
    return server, director, bot, world


def test_climbing_pack_plans_a_ledge_the_glider_cannot():
    async def scenario():
        _server, director, bot, world = await _platform_scene(C.JETPACK_NORMAL, 5)
        node = (100, 100, int(round(bot.position[2] + 2.25)))
        climber = _movement_abilities(director._snapshot_player(bot))
        edges = {(target[0], target[1], affordance)
                 for target, affordance, _cost, _plan in world._neighbors(
                     node, abilities=climber, dig_profile=None, allow_water=False)}
        assert (101, 100, MovementAffordance.JETPACK) in edges
        glider = frozenset({MovementAffordance.JUMP, MovementAffordance.JETPACK})
        edges = {(target[0], target[1])
                 for target, _affordance, _cost, _plan in world._neighbors(
                     node, abilities=glider, dig_profile=None, allow_water=False)}
        assert (101, 100) not in edges

    asyncio.run(scenario())


@pytest.mark.parametrize("pack", (C.JETPACK_NORMAL, C.JETPACK_ENGINEER))
@pytest.mark.parametrize("height", (3, 8))
def test_flight_controller_lands_on_the_ledge_without_launching(pack, height):
    async def scenario():
        server, director, bot, world = await _platform_scene(pack, height)
        brain = SimpleBotBrain(world)
        source = bot.position
        observer = director._snapshot_player(bot)
        state = _BotState(observer.player_id, observer.generation, observer.life_id)
        state.flight_step = RouteStep((101.5, 100.5, source[2] - height),
                                      MovementAffordance.JETPACK)
        state.flight_source = source
        state.flight_started_at = 100.0
        runtime = director._runtime[bot.id]
        now, peak = 100.0, 0.0
        for _ in range(60 * 6):
            observer = director._snapshot_player(bot)
            intent = brain._flight_intent(_frame(observer, created_at=now),
                                          observer, state, now)
            if intent is None:
                break
            runtime.intent = replace(
                intent, bot_id=bot.id, bot_generation=runtime.generation,
                created_at=time.monotonic(), expires_at=time.monotonic() + 1.0)
            director._apply_motor(runtime, time.monotonic(), 1 / 60)
            server.loop_count += 1
            await bot.simulate_tick(1 / 60)
            peak = max(peak, source[2] - bot.position[2])
            now += 1 / 60
        top = source[2] - height
        assert abs(bot.position[2] - top) < 0.6, bot.position
        assert 101.0 <= bot.position[0] < 106.0, bot.position
        # Thrust is cut on the coast estimate: no rocket launch past the lip.
        assert peak < height + 2.5

    asyncio.run(scenario())


def test_bot_nicknames_look_like_players_and_never_collide():
    factory = ProfileFactory(seed=11)
    names = [factory.create().name for _ in range(200)]
    assert len({name.casefold() for name in names}) == len(names)
    assert all(3 <= len(name) <= 15 and name.isascii() for name in names)
    # The old call-sign roster is gone.
    assert not {"Atlas", "Bishop", "Warden", "Quartz"} & set(names)
    # A mix of styles, not one template.
    assert any("_" in name for name in names)
    assert any(name.islower() for name in names)
    assert any(any(ch.isdigit() for ch in name) for name in names)


def test_released_names_can_be_reused_case_insensitively():
    factory = ProfileFactory(seed=3)
    name = factory.create().name
    factory.release_name(name.upper())
    assert name.casefold() not in factory._used_names
