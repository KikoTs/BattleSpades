"""Terrain regressions for the production lightweight bot navigator."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotActionKind,
    BotIntent,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import (
    SimpleBotBrain,
    _BotState,
    _Goal,
    _dig_profile,
    _movement_abilities,
)
from server.config import ServerConfig
from server.dig_profiles import melee_dig_positions
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


class _FixtureVxl:
    def __init__(self, solids: set[tuple[int, int, int]]) -> None:
        self.solids = set(solids)

    def get_solid(self, x: int, y: int, z: int) -> bool:
        return (int(x), int(y), int(z)) in self.solids


def _world(solids: set[tuple[int, int, int]]) -> SimpleVoxelWorld:
    world = SimpleVoxelWorld()
    world._vxl = _FixtureVxl(solids)
    world._atlas = None
    return world


def test_production_abilities_route_over_a_two_block_obstacle() -> None:
    # AoS z grows downward. Ground support is z=20; the two added voxels at
    # z=18/19 make the obstacle top a two-block climb at support z=18.
    solids = {(x, 10, 20) for x in range(10, 15)}
    solids.update({(12, 10, 18), (12, 10, 19)})
    world = _world(solids)
    abilities = _movement_abilities(SimpleNamespace())

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (14.5, 10.5, 17.75),
        abilities=abilities,
    )

    assert abilities == frozenset({MovementAffordance.JUMP})
    assert plan.reached_segment_goal is True
    assert any(
        step.affordance is MovementAffordance.JUMP
        and step.waypoint == (12.5, 10.5, 15.75)
        for step in plan.steps
    )


def test_water_route_moves_monotonically_to_a_climbable_shore() -> None:
    # Ignore the near two-block bank and traverse the water to a bank that the
    # native body can reliably mount after swimming.
    solids = {(x, 10, 239) for x in range(10, 15)}
    solids.add((9, 10, 237))
    solids.add((15, 10, 238))
    world = _world(solids)

    positions = []
    position = (10.5, 10.5, 236.75)
    for _ in range(6):
        step = world.water_step(position)
        assert step is not None
        positions.append(step.waypoint)
        position = step.waypoint
        if position[0] == 15.5:
            break

    assert [waypoint[0] for waypoint in positions] == [
        11.5,
        12.5,
        13.5,
        14.5,
        15.5,
    ]
    assert positions[-1] == (15.5, 10.5, 235.75)


def test_two_block_route_drives_real_native_player_over_wall() -> None:
    """Join simple A*, live motor validation, and the native 60 Hz body."""

    async def scenario() -> None:
        config = ServerConfig()
        config.bots.max_bots = 1
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()

        # Flat-map support is z=62. Add two full body-width layers so the
        # direct route must climb from support 62 to support 60.
        for y in range(97, 104):
            assert server.world_manager.set_block(
                102, y, 61, True, 0x123456
            )
            assert server.world_manager.set_block(
                102, y, 60, True, 0x123456
            )

        director = BotDirector(server, supervisor=SimpleNamespace())
        bot = await director.add_bot(
            team=TEAM1,
            name="NativeJump",
            class_id=int(C.CLASS_SOLDIER),
        )
        assert bot is not None
        runtime = director._runtime[bot.id]
        bot.set_position(100.5, 100.5, 59.75)
        bot.set_orientation_vector(1.0, 0.0, 0.0)
        bot._world_object.set_velocity(0.0, 0.0, 0.0)
        await bot.simulate_tick(1.0 / 60.0)

        world = SimpleVoxelWorld()
        world._vxl = SimpleNamespace(
            get_solid=server.world_manager.get_solid
        )
        world._atlas = None
        plan = world.plan(
            bot.position,
            (106.5, 100.5, 59.75),
            abilities=_movement_abilities(SimpleNamespace()),
        )
        assert plan.reached_segment_goal is True

        route_index = 0
        frame_id = 0
        base_time = time.monotonic()
        highest_position = float(bot.z)
        for tick in range(360):
            while route_index < len(plan.steps):
                waypoint = plan.steps[route_index].waypoint
                if math.hypot(
                    waypoint[0] - bot.x,
                    waypoint[1] - bot.y,
                ) > 0.9:
                    break
                route_index += 1
            if bot.x > 103.5:
                break
            assert route_index < len(plan.steps)

            if tick % 6 == 0:
                step = plan.steps[route_index]
                dx = float(step.waypoint[0]) - float(bot.x)
                dy = float(step.waypoint[1]) - float(bot.y)
                length = math.hypot(dx, dy)
                direction = (
                    (dx / length, dy / length, 0.0)
                    if length > 1e-6
                    else (0.0, 0.0, 0.0)
                )
                frame_id += 1
                now = base_time + tick / 60.0
                runtime.intent = BotIntent(
                    bot_id=bot.id,
                    bot_generation=runtime.generation,
                    frame_id=frame_id,
                    map_epoch=0,
                    mode_epoch=0,
                    topology_version=server.world_manager.topology_version,
                    created_at=now,
                    expires_at=now + 1.0,
                    movement=MovementIntent(
                        direction=direction,
                        jump=step.affordance is MovementAffordance.JUMP,
                        affordance=step.affordance,
                    ),
                )
                server.loop_count = tick
                director._apply_motor(runtime, now, 6.0 / 60.0)

            await bot.simulate_tick(1.0 / 60.0)
            highest_position = min(highest_position, float(bot.z))

        assert bot.x > 103.5
        assert highest_position < 58.0

    asyncio.run(scenario())


def _sealed_wall_solids(*, thickness: int = 1) -> set[tuple[int, int, int]]:
    solids = {
        (x, y, 20)
        for x in range(10, 18)
        for y in range(8, 13)
    }
    solids.update(
        (x, y, z)
        for x in range(13, 13 + int(thickness))
        for y in range(8, 13)
        for z in (17, 18, 19)
    )
    return solids


def test_sealed_wall_route_plans_exact_spade_clearance_cell() -> None:
    world = _world(_sealed_wall_solids())
    observer = SimpleNamespace(
        loadout=(int(C.SMG_TOOL), int(C.SPADE_TOOL))
    )

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (17.5, 10.5, 17.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )

    breaches = [
        step for step in plan.steps
        if step.affordance is MovementAffordance.BREACH
    ]
    assert plan.reached_segment_goal is True
    assert len(breaches) == 1
    breach = breaches[0].breach
    assert breach is not None
    assert breach.source == (12, 10, 20)
    assert breach.destination == (13, 10, 20)
    assert breach.blocking_cells == ((13, 10, 18), (13, 10, 19))
    # Centering the column at z=18 removes z=17/18/19 but preserves floor z=20.
    assert breach.target_cell == (13, 10, 18)
    assert breach.estimated_swings == 1
    assert (13, 10, 20) not in melee_dig_positions(
        breach.target_cell,
        _dig_profile(observer).pattern,
    )


def test_breach_cost_and_aim_follow_each_owned_tools_real_footprint() -> None:
    expectations = (
        (int(C.PICKAXE_TOOL), 2, False),
        (int(C.KNIFE_TOOL), 10, False),
        (int(C.MACHETE_TOOL), 3, False),
        (int(C.UGC_SUPERSPADE_TOOL), 1, True),
    )
    for tool_id, expected_swings, expected_secondary in expectations:
        world = _world(_sealed_wall_solids())
        observer = SimpleNamespace(loadout=(tool_id,))
        plan = world.plan(
            (12.5, 10.5, 17.75),
            (17.5, 10.5, 17.75),
            abilities=_movement_abilities(observer),
            dig_profile=_dig_profile(observer),
        )
        breach = next(
            step.breach for step in plan.steps if step.breach is not None
        )
        assert breach.target_cell == (13, 10, 18)
        assert breach.tool_id == tool_id
        assert breach.estimated_swings == expected_swings
        assert breach.secondary is expected_secondary


def test_costed_route_uses_short_detour_instead_of_unnecessary_dig() -> None:
    solids = {
        (x, y, 20)
        for x in range(10, 18)
        for y in range(9, 12)
    }
    solids.update((13, 10, z) for z in (17, 18, 19))
    world = _world(solids)
    observer = SimpleNamespace(loadout=(int(C.KNIFE_TOOL),))

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (17.5, 10.5, 17.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )

    assert plan.reached_segment_goal is True
    assert all(
        step.affordance is not MovementAffordance.BREACH
        for step in plan.steps
    )
    assert any(step.waypoint[1] != 10.5 for step in plan.steps)


def test_planned_tunnel_replans_until_a_two_column_wall_is_clear() -> None:
    solids = _sealed_wall_solids(thickness=2)
    world = _world(solids)
    observer = SimpleNamespace(loadout=(int(C.SPADE_TOOL),))
    profile = _dig_profile(observer)
    assert profile is not None
    position = (10.5, 10.5, 17.75)
    digs: list[tuple[int, int, int]] = []

    for _iteration in range(12):
        if math.hypot(position[0] - 17.5, position[1] - 10.5) <= 0.1:
            break
        plan = world.plan(
            position,
            (17.5, 10.5, 17.75),
            abilities=_movement_abilities(observer),
            dig_profile=profile,
        )
        assert plan.steps
        step = plan.steps[0]
        if step.breach is None:
            position = step.waypoint
            continue
        digs.append(step.breach.target_cell)
        for cell in melee_dig_positions(
            step.breach.target_cell,
            profile.pattern,
        ):
            world._vxl.solids.discard(cell)
    else:
        raise AssertionError("planned tunnel did not converge")

    assert position == (17.5, 10.5, 17.75)
    assert digs == [(13, 10, 18), (14, 10, 18)]
    assert all(world.solid(x, 10, 20) for x in range(10, 18))
    assert all(
        not world.solid(x, 10, z)
        for x in (13, 14)
        for z in (17, 18, 19)
    )


def test_production_brain_stops_and_swings_at_planned_wall_voxel() -> None:
    world = _world(_sealed_wall_solids())
    observer = PlayerSnapshot(
        player_id=1,
        generation=1,
        team=TEAM1,
        class_id=int(C.CLASS_SOLDIER),
        alive=True,
        spawned=True,
        position=(12.5, 10.5, 17.75),
        eye=(12.5, 10.5, 16.75),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=int(C.SMG_TOOL),
        blocks=0,
        ammo_clip=30,
        ammo_reserve=90,
        is_bot=True,
        weapon_tool=int(C.SMG_TOOL),
        loadout=(int(C.SMG_TOOL), int(C.SPADE_TOOL)),
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=1,
        observer_generation=1,
        created_at=time.monotonic(),
        mode_id="tdm",
        players=(observer,),
    )
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    goal = _Goal(
        key=("wall-test",),
        position=(17.5, 10.5, 17.75),
        role="wall_test",
        arrival_radius=0.5,
        sprint=True,
    )

    intent = SimpleBotBrain(world)._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )

    assert intent.movement.affordance is MovementAffordance.BREACH
    assert intent.movement.direction == (0.0, 0.0, 0.0)
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.tool_id == int(C.SPADE_TOOL)
    assert intent.action.position == (13.5, 10.5, 18.5)
    assert intent.look is not None
    assert intent.look.target == intent.action.position
