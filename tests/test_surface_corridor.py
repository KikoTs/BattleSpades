"""Whole-route completion regressions, beyond displacement/stall oracles."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import math
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import PerceptionFrame, VoxelChange, WorldDelta
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from server.bot_ai.surface_corridor import SurfaceCorridorSearch
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


def test_incremental_search_finds_exit_behind_bot_and_keeps_its_corners() -> None:
    width = 128
    supports = bytearray([20]) * width ** 2
    for y in range(30, 91):
        supports[y * width + 80] = 10
    for x in range(20, 81):
        supports[30 * width + x] = supports[90 * width + x] = 10
    search = SurfaceCorridorSearch(bytes(supports), width, width,
                                   60 * width + 78, 60 * width + 90)
    for _ in range(80):
        before = search.expansions
        search.advance()
        assert search.expansions - before <= 512
        if search.done:
            break
    assert search.done
    assert search.path[-1] == (90.5, 60.5, 17.75)
    assert min(point[0] for point in search.path) < 20
    assert max(abs(point[1] - 60.5) for point in search.path) > 30
    assert not search.frontier and not search.parents and not search.costs


def test_map_search_does_not_cross_rejected_edge_or_unbuildable_void() -> None:
    supports = bytes([20, 20, 20, 20, 255, 20, 20, 20, 20])
    search = SurfaceCorridorSearch(supports, 3, 3, 0, 2, frozenset({(0, 1)}))
    search.advance()
    assert search.path[-1] == (2.5, 0.5, 17.75)
    assert any(point[1] == 2.5 for point in search.path)
    isolated = SurfaceCorridorSearch(bytes([20, 239, 20]), 3, 1, 0, 2)
    isolated.advance()
    assert isolated.done and not isolated.path


def test_unreachable_map_wide_search_releases_its_bounded_working_set() -> None:
    supports = bytearray([20]) * 512 ** 2
    supports[-1] = 10  # Unclimbable destination on an otherwise open map.
    search = SurfaceCorridorSearch(bytes(supports), 512, 512, 0, len(supports) - 1)
    for _ in range(128):
        search.advance()
        assert len(search.costs) <= 32768
        if search.done:
            break
    assert search.done and not search.path
    assert not search.parents and not search.costs and not search.frontier


def test_failure_discovered_mid_search_cannot_return_a_stale_parent_chain(monkeypatch) -> None:
    import server.bot_ai.surface_corridor as corridor_module
    monkeypatch.setattr(corridor_module, "_SLICE_EXPANSIONS", 1)
    search = SurfaceCorridorSearch(bytes([20]) * 4, 2, 2, 0, 1)
    search.advance()  # The direct edge is already in the parent tree.
    state = _BotState(1, 1, 1, corridor_search=search)
    SimpleBotBrain._remember_blocked_edge(state, ((0, 0, 20), (1, 0, 20)), 100.)
    assert state.corridor_search is search
    search.advance()
    assert search.done and not search.path
    retry = SurfaceCorridorSearch(bytes([20]) * 4, 2, 2, 0, 1, search.blocked)
    for _ in range(4):
        retry.advance()
    assert retry.path[-1] == (1.5, 0.5, 17.75)
    assert any(point[1] == 1.5 for point in retry.path)


@pytest.mark.parametrize("terrain,class_id", [
    (terrain, int(C.CLASS_SOLDIER)) for terrain in (
        "detour", "bridge", "drop", "excluded_exits", "under_bridge", "low_roof_step",
        "engineer_gap", "rocketeer_gap", "engineer_water_gap", "island_escape", "narrow_walk", "narrow_corner", "stairs", "corner_drop",
    )
] + [("low_roof_step", class_id) for class_id in range(int(C.CLASS_NOOF))
     if class_id not in {int(C.CLASS_SOLDIER), int(C.CLASS_UGCBUILDER)}]
  + [("escape_drop", class_id) for class_id in range(int(C.CLASS_NOOF))
     if class_id != int(C.CLASS_UGCBUILDER)])
def test_production_bot_completes_route_with_native_physics(terrain: str, class_id: int) -> None:
    """Reach the far side of a U wall or a trench using normal motor/actions."""

    async def scenario() -> None:
        config = ServerConfig()
        config.bots.max_bots = 3 if terrain == "bridge" else 1
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()
        if terrain == "detour":
            walls = {(120, y) for y in range(40, 161)}
            walls.update((x, y) for x in range(60, 121) for y in (40, 160))
            changes = tuple(VoxelChange(x, y, z, True, 0x123456)
                            for x, y in walls for z in range(55, 62))
            start, destination = (118.5, 100.5, 59.75), (130.5, 100.5, 59.75)
        elif terrain == "bridge":
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(101, 108) for y in range(512))
            start, destination = (100.5, 100.5, 59.75), (112.5, 100.5, 59.75)
        elif terrain in {"engineer_water_gap", "island_escape"}:
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(95, 116) for y in range(95, 116))
            if terrain == "engineer_water_gap":
                changes += tuple(VoxelChange(x, y, 238, True, 0x123456)
                                 for x in (*range(98, 101), *range(104, 110))
                                 for y in range(98, 104))
                start, destination = (100.5, 100.5, 235.75), (106.5, 100.5, 235.75)
            else:
                changes += (VoxelChange(100, 100, 238, True, 0x123456),
                            VoxelChange(112, 100, 238, True, 0x123456))
                start, destination = (100.5, 100.5, 235.75), (112.5, 100.5, 235.75)
        elif terrain in {"engineer_gap", "rocketeer_gap"}:
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(101, 104) for y in range(512))
            start, destination = (100.5, 100.5, 59.75), (106.5, 100.5, 59.75)
        elif terrain == "narrow_corner":
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(95, 116) for y in range(95, 116)
                            if not (y == 100 and x >= 100 or x == 100 and y >= 100))
            start, destination = (110.75, 100.363, 59.75), (100.5, 110.5, 59.75)
        elif terrain == "narrow_walk":
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(95, 116) for y in range(96, 105) if y != 100)
            start, destination = (110.75, 100.363, 59.75), (100.5, 100.5, 59.75)
        elif terrain == "corner_drop":
            changes = tuple(VoxelChange(x, y, 62, False, 0)
                            for x in range(95, 116) for y in range(95, 116))
            changes += tuple(VoxelChange(x, 100, 62, True, 0x123456) for x in (100, 101))
            changes += tuple(VoxelChange(x, 99, 64, True, 0x123456) for x in range(95, 102))
            changes += (VoxelChange(100, 99, 60, True, 0x123456),)
            start, destination = (100.869, 100.457, 59.75), (95.5, 99.5, 61.75)
        elif terrain == "stairs":
            changes = tuple(VoxelChange(x, y, z, True, 0x123456)
                            for x in range(101, 113) for y in range(99, 102)
                            for z in range(62 - min(10, x - 100), 62))
            changes += tuple(VoxelChange(x, y, z, True, 0x123456)
                             for x in range(99, 114) for y in (98, 102)
                             for z in range(45, 62))
            start, destination = (100.5, 100.5, 59.75), (111.5, 100.5, 49.75)
        elif terrain in {"drop", "escape_drop"}:
            changes = tuple(VoxelChange(x, y, z, True, 0x123456)
                            for x in range(100, 121) for y in range(95, 106)
                            for z in range(58, 62))
            start, destination = (118.5, 100.5, 55.75), (130.5, 100.5, 59.75)
        elif terrain == "under_bridge":
            # Native water collision survives removal of the VXL waterbed.
            # The untouched z=62 roof keeps these columns classified as dry.
            changes = tuple(VoxelChange(x, y, 239, False, 0)
                            for x in range(95, 126) for y in range(95, 106))
            start, destination = (100.5, 100.5, 236.75), (112.5, 100.5, 236.75)
        elif terrain == "low_roof_step":
            cells = {(x, y, z) for x in range(99, 116) for y in (99, 101)
                     for z in range(55, 62)}
            cells.update((99, 100, z) for z in range(55, 62))
            cells.update((x, 100, 61) for x in range(101, 116))
            cells.add((100, 100, 58))
            # The generated flat map is a floating sheet. Anchor it to the
            # immutable bottom before exercising native structural collapse.
            cells.update((99, 99, z) for z in range(63, 240))
            changes = tuple(VoxelChange(x, y, z, True, 0x123456) for x, y, z in sorted(cells))
            start, destination = (100.5, 100.5, 59.75), (104.5, 100.5, 58.75)
        else:
            changes = ()
            start, destination = (100.5, 100.5, 59.75), (112.5, 100.5, 59.75)
        for change in changes:
            assert server.world_manager.set_block(
                change.x, change.y, change.z, change.solid, change.color)
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        if terrain == "bridge":
            for index in range(2):
                teammate = await director.add_bot(team=TEAM1, name=f"Support{index}")
                assert teammate is not None
                teammate.set_position(50.5, 50.5 + index * 5, 59.75)
        flying = terrain in {"engineer_gap", "rocketeer_gap", "engineer_water_gap"}
        bot = await director.add_bot(team=TEAM1, name="Detour", class_id=int(
            C.CLASS_ENGINEER if flying else class_id))
        assert bot is not None
        assert bot.class_id == (int(C.CLASS_ENGINEER) if flying else class_id)
        if flying:
            bot.jetpack_id = int(C.JETPACK2 if terrain == "rocketeer_gap" else C.JETPACK_ENGINEER)
            bot.jetpack_fuel = 100.0
        runtime = director._runtime[bot.id]
        initial_blocks = bot.blocks
        initial_health = bot.health
        bot.set_position(*start)
        bot.set_orientation_vector(1.0, 0.0, 0.0)
        bot._world_object.set_velocity(0.0, 0.0, 0.0)
        world = SimpleVoxelWorld()
        world.load(director._make_map_snapshot(current=True))
        world.apply(WorldDelta(
            map_epoch=world.map_epoch,
            topology_version=server.world_manager.topology_version,
            changed_cells=changes,
        ))
        built: list[tuple[int, int, int]] = []

        def mutate(x, y, z, solid, color, version) -> None:
            world.apply(WorldDelta(world.map_epoch, version,
                                   (VoxelChange(x, y, z, solid, color),)))
            if solid:
                built.append((x, y, z))

        subscription = server.world_manager.subscribe_mutations(mutate)
        brain = SimpleBotBrain(world)
        state = _BotState(0, 0, 0)
        goal = _Goal(("test",), destination, "test_destination", 1.5, True)
        base = time.monotonic() + 1.0
        if terrain == "excluded_exits":
            state.blocked_edges = {
                ((100, 100, 62), (100 + dx, 100 + dy, 62)): base + 60.0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))
            }
        clock = [base]
        leftmost = bot.x
        minimum_fuel = bot.jetpack_fuel
        flight_trace = []
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            for tick in range(60 * 60):
                now = base + tick / 60.0
                clock[0] = now
                if terrain in {"escape_drop", "corner_drop"} and tick and tick % 8 == 0:
                    # Other players edit the map during the descent. Every
                    # replan must retain the escape's traversal permissions.
                    server.world_manager.set_block(5, 5, 61, bool(tick // 8 % 2), 0x123456)
                if tick % 8 == 0:
                    snapshots = director._snapshot_players()
                    observer = next(p for p in snapshots if p.player_id == bot.id)
                    # This fixture proves using the existing exit without a dig.
                    if terrain != "low_roof_step":
                        observer = replace(observer, loadout=(int(C.MINIGUN_TOOL), int(C.BLOCK_TOOL)))
                    frame = PerceptionFrame(
                        frame_id=tick + 1, map_epoch=0, mode_epoch=0,
                        topology_version=server.world_manager.topology_version,
                        observer_id=bot.id, observer_generation=runtime.generation,
                        created_at=now, mode_id="tdm",
                        players=(observer, *(p for p in snapshots if p.player_id != bot.id)),
                        profile=runtime.profile,
                    )
                    if terrain in {"escape_drop", "corner_drop"} and tick == 0:
                        brain._set_goal(state, goal, observer.position, now)
                        brain._escape_empty_route(frame, observer, state, now)
                        assert any(step.affordance.value == "drop" for step in state.route)
                    runtime.intent = brain._navigation_intent(frame, observer, state, goal, now)
                    if (flying or terrain == "detour") and tick % 60 == 0:
                        flight_trace.append((round(tick / 60, 2), tuple(round(v, 2) for v in bot.position),
                                             runtime.intent.debug_role, state.corridor_index))
                server.loop_count = tick
                if tick % 2 == 0:
                    director._apply_motor(runtime, now, 2.0 / 60.0)
                await bot.simulate_tick(1.0 / 60.0)
                minimum_fuel = min(minimum_fuel, bot.jetpack_fuel)
                server.world_mutations.commit_ready()
                leftmost = min(leftmost, bot.x)
                if math.dist(bot.position, goal.position) <= goal.arrival_radius and (not flying or not bot.airborne):
                    break
        server.world_manager.unsubscribe_mutations(subscription)
        if terrain == "detour":
            assert leftmost < 60.0, (bot.position, runtime.intent.debug_role, flight_trace)
        elif terrain == "bridge":
            assert all((x, 100, 62) in built for x in range(101, 107)), str(built)
            assert initial_blocks - bot.blocks == len(built)
            assert not bot.wade
        else:
            assert bot.alive and bot.health == initial_health, flight_trace
        assert math.dist(bot.position, goal.position) <= goal.arrival_radius, (
            bot.position, runtime.intent.debug_role, state.corridor_index, state.corridor, flight_trace,
        )
        if flying:
            assert minimum_fuel < 85.0, "crossing never engaged native jetpack thrust"
            assert not bot.wade and not bot.airborne
            assert tick < 10 * 60, flight_trace
        if terrain == "corner_drop":
            assert tick < 15 * 60, "cut a drop corner before reaching its approach column"
        if terrain == "excluded_exits":
            assert tick < 25 * 60, "waited for failure memory instead of revalidating an escape"
        if terrain == "under_bridge":
            assert tick < 10 * 60, "water under a dry roof lost live movement"
        if terrain == "low_roof_step":
            assert not world.solid(100, 100, 58), "climbing needs clearance above the source body"
            assert tick < 10 * 60, "repeated a climb beneath a solid source ceiling"

    asyncio.run(scenario())
