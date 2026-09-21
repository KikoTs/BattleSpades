"""Behavior contracts spanning perception, movement ownership and game resets."""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import shared.constants as C

from modes.tdm import TDMMode
from modes.arena import ArenaMode
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotAction, BotActionKind, LookIntent, MovementAffordance, MovementIntent, ObjectiveSnapshot, VoxelChange, WorldDelta
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal, _movement_abilities, _dig_profile, _TraversalStyle, _route_edges
from server.bot_ai.simple_navigation import SimpleVoxelWorld, RouteStep
from server.bot_ai.surface_corridor import SurfaceCorridorSearch
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.player import Player
from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _player, _profile
from tests.test_simple_bot_navigation import _world
from scripts.bot_map_matrix import water_stall_contact


@pytest.mark.parametrize("height,wade,expected", [
    (237.7, True, True), (235.5, True, True), (233.0, True, True),
    (200.0, True, False), (237.7, False, False),
])
def test_water_stall_measurement_includes_bobbing_but_excludes_high_knockback(height, wade, expected):
    assert water_stall_contact(SimpleNamespace(z=height, wade=wade)) is expected


def test_compacted_escape_revalidates_its_first_edge_without_erasing_other_floors():
    edges = _route_edges((165.316, 254.416, 223.744),
                         (RouteStep((159.5, 254.5, 223.75), MovementAffordance.WALK),))
    assert ((165, 254, 226), (164, 254, 226)) in edges
    assert ((165, 254, 228), (164, 254, 228)) not in edges
    assert ((165, 254, 226), (165, 253, 226)) not in edges


@pytest.mark.parametrize("kind,affordance", [
    (BotActionKind.MELEE, MovementAffordance.BREACH),
    (BotActionKind.BUILD, MovementAffordance.BUILD_STEP),
    (BotActionKind.BUILD_LINE, MovementAffordance.BUILD_BRIDGE),
    (BotActionKind.PLACE_PREFAB, MovementAffordance.PLACE_PREFAB),
])
@pytest.mark.parametrize("class_id", [int(C.CLASS_SOLDIER), int(C.CLASS_ZOMBIE)])
def test_visible_enemy_cannot_cancel_the_action_needed_to_reach_it(monkeypatch, kind, affordance, class_id):
    observer = _player(1, 2, (10., 10., 20.), is_bot=True, class_id=class_id)
    enemy = _player(2, 3, (130., 10., 20.))
    frame = _frame(observer, enemy)
    brain = SimpleBotBrain(_TacticalWorld())
    look = LookIntent((11., 10., 21.), visible=False)
    traversal = brain._intent(frame, movement=MovementIntent(crouch=True, affordance=affordance),
        look=look, tool_id=int(C.SPADE_TOOL),
        action=BotAction(kind, tool_id=int(C.SPADE_TOOL), position=(11., 10., 21.)),
        debug_role="planned_edge")
    monkeypatch.setattr(brain, "_navigation_intent", lambda *_args, **_kwargs: traversal)
    intent = brain._combat_intent(frame, observer, enemy, _BotState(1, 1, 0), _profile(), 100.)
    assert intent.action == traversal.action
    assert intent.look == look and intent.tool_id == traversal.tool_id
    assert intent.movement == traversal.movement


def test_moving_target_keeps_bridge_detour_until_the_segment_finishes():
    state = _BotState(1, 1, 0)
    goal = _Goal(("enemy", 2, 1), (80., 10., 20.), "pursuit", 2., True)
    SimpleBotBrain._set_goal(state, goal, (10., 10., 20.), 100.)
    state.corridor = ((10., 30., 20.), (80., 30., 20.))
    SimpleBotBrain._set_goal(state, replace(goal, position=(85., 10., 20.)), (10., 10., 20.), 101.)
    assert len(state.corridor) == 2
    assert state.goal.position == (85., 10., 20.)


def test_occupied_cell_replan_reconsiders_the_escape_without_waiting_for_its_lease(monkeypatch):
    observer = _player(1, 2, (10.5, 10.5, 17.75), is_bot=True)
    frame = _frame(observer, created_at=100.)
    world = _TacticalWorld(route_step=RouteStep(observer.position, MovementAffordance.WALK))
    brain = SimpleBotBrain(world)
    state = _BotState(1, 1, 0)
    goal = _Goal(("test",), (30.5, 10.5, 17.75), "test_escape", 1., True)
    brain._set_goal(state, goal, observer.position, 99.)
    state.escape_goal = (20.5, 10.5, 17.75)
    state.escape_until = 106.
    alternative = RouteStep((10.5, 16.5, 17.75), MovementAffordance.WALK)
    reconsidered = []

    def escape(_frame, _observer, target_state, now):
        reconsidered.append(now)
        target_state.route = (alternative,)
        target_state.escape_goal = alternative.waypoint

    monkeypatch.setattr(brain, "_escape_empty_route", escape)
    intent = brain._navigation_intent(frame, observer, state, goal, 100.)
    assert reconsidered == [100.]
    assert state.escape_goal == alternative.waypoint
    assert intent.movement.direction[1] > 0.9


def test_dry_corridor_tracks_the_road_below_a_disconnected_roof():
    def floor(x: int, y: int, z: int) -> int | None:
        return 20 if z == 20 and y == 1 else None
    search = SurfaceCorridorSearch(bytes([5] * 21), 7, 3, 7, 13,
        surface_at=floor, start_height=20, target_height=20)
    search.advance()
    assert search.done and search.path[-1] == (6.5, 1.5, 17.75)
    assert all(point[2] == 17.75 for point in search.path)


@pytest.mark.parametrize("hazard", ["none", "gap", "ceiling", "crouch", "cautious"])
def test_tactical_hop_requires_a_clear_dry_runway_and_respects_temperament(hazard):
    solids = {(x, y, 20) for x in range(9, 17) for y in range(9, 13)}
    if hazard == "gap":
        solids.remove((13, 10, 20))
    if hazard == "ceiling":
        solids.add((12, 10, 15))
    observer = _player(1, 2, (10.5, 10.5, 17.75), is_bot=True)
    brain, state = SimpleBotBrain(_world(solids)), _BotState(1, 1, 0)
    observer = replace(observer, health=60, last_damage_at=99.9)
    frame = _frame(observer)
    if hazard == "cautious":
        frame = replace(frame, profile=replace(frame.profile, aggression=0.2))
    movement = MovementIntent(direction=(1., 0., 0.), crouch=hazard == "crouch")
    assert brain._safe_tactical_hop(frame, observer, state, movement, 100.) == (hazard == "none")
    if hazard == "none":
        assert not brain._safe_tactical_hop(frame, observer, state, movement, 100.2)
        assert not brain._safe_tactical_hop(frame, observer, state, movement, 106.)
        newly_hit = replace(observer, last_damage_at=105.9)
        assert brain._safe_tactical_hop(frame, newly_hit, state, movement, 106.)


@pytest.mark.parametrize("hazard", ["none", "empty", "ally", "objective", "gap"])
def test_prefab_cover_keeps_resources_teammates_and_objectives_clear(hazard):
    solids = {(x, y, 20) for x in range(9, 18) for y in range(5, 16)}
    if hazard == "gap":
        solids.remove((13, 10, 20))
    observer = replace(_player(1, 2, (10.5, 10.5, 17.75), is_bot=True,
                              loadout=(int(C.SMG_TOOL), int(C.PREFAB_TOOL))),
                       prefabs=("prefab_small_wall",), blocks=0 if hazard == "empty" else 100)
    players = (_player(2, 2, (13.5, 10.5, 17.75)),) if hazard == "ally" else ()
    objectives = ((ObjectiveSnapshot("flag", 1, (13.5, 10.5, 17.75)),)
                  if hazard == "objective" else ())
    frame = _frame(observer, *players, objectives=objectives)
    brain, state = SimpleBotBrain(_world(solids)), _BotState(1, 1, 0)
    intent = brain._prefab_cover_intent(frame, observer, state, (20., 10.5, 17.75), 100.)
    assert (intent is not None) == (hazard == "none")
    if intent is not None:
        assert intent.action.kind is BotActionKind.PLACE_PREFAB
        assert brain._prefab_cover_intent(frame, observer, state, (20., 10.5, 17.75), 101.) is None


@pytest.mark.parametrize("pack,fuel,grounded,allowed", [
    (C.JETPACK_ENGINEER, 100., True, True), (C.JETPACK2, 100., True, True),
    (C.JETPACK_ENGINEER, 30., True, False), (C.NO_JETPACK, 100., True, False),
    (C.JETPACK_ENGINEER, 100., False, False),
])
def test_flight_plans_require_takeoff_fuel_and_a_real_grounded_pack(pack, fuel, grounded, allowed):
    observer = replace(_player(1, 2, (10.5, 10.5, 17.75)),
                       jetpack_id=int(pack), jetpack_fuel=fuel, grounded=grounded)
    assert (MovementAffordance.JETPACK in _movement_abilities(observer)) == allowed


def test_flight_does_not_plan_through_a_roof_and_has_a_bounded_recovery():
    world = _world({(10, 10, 20), (14, 10, 20), (12, 10, 13)})
    assert not world._flight_gap_is_clear(10, 10, 1, 0, 4, 20, 20)
    observer = replace(_player(1, 2, (10.5, 10.5, 17.75)),
                       jetpack_id=int(C.JETPACK_ENGINEER), jetpack_fuel=100.)
    state = _BotState(1, 1, 0, flight_source=observer.position,
                      flight_step=RouteStep((14.5, 10.5, 17.75), MovementAffordance.JETPACK),
                      flight_started_at=100.)
    brain = SimpleBotBrain(world)
    assert brain._flight_intent(_frame(observer), observer, state, 104.) is None
    assert state.flight_step is None and state.blocked_edges and state.next_flight_at > 104.


def test_planned_cover_reaches_native_prefab_service_and_spends_real_blocks():
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        bot = await director.add_bot(team=2, class_id=int(C.CLASS_ENGINEER))
        bot.set_position(100.5, 100.5, 59.75)
        bot.grounded = True
        world = SimpleVoxelWorld()
        world.load(director._make_map_snapshot(current=True))
        observer = director._snapshot_player(bot)
        brain = SimpleBotBrain(world)
        intent = brain._prefab_cover_intent(_frame(observer), observer,
                    _BotState(1, 1, 0), (120., 100.5, 59.75), 100.)
        assert intent is not None, observer.prefabs
        blocks = bot.blocks
        version = server.world_manager.topology_version
        assert director.gateway.execute(bot, intent.action)
        for _ in range(20):
            server.loop_count += 1
            await bot.simulate_tick(1. / 60.)
            server.world_mutations.commit_ready()
            server.prefab_actions.tick()
        assert bot.blocks < blocks and server.world_manager.topology_version > version
        assert server.world_manager.spawn_position_is_safe(bot.position)
    asyncio.run(scenario())


@pytest.mark.parametrize("solid_above_bed", (False, True))
def test_london_waterbed_has_bounded_escape_or_normal_death_recovery(solid_above_bed):
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        assert server.world_manager.load_map("London")
        changes = []
        for x, y, z in ((252, 307, 238), (252, 308, 238), (253, 306, 238), (254, 306, 238)):
            server.world_manager.set_block(x, y, z, True, 0x123456)
            changes.append(VoxelChange(x, y, z, True, 0x123456))
        if not solid_above_bed:
            server.world_manager.set_block(252, 307, 238, False, 0)
            changes.append(VoxelChange(252, 307, 238, False, 0))
        assert server.world_manager.get_solid(252, 307, 239)
        assert server.world_manager.get_solid(252, 307, 238) is solid_above_bed
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        bot = await director.add_bot(team=2, class_id=int(C.CLASS_SOLDIER))
        start = (252.338, 307.747, 238.647)
        bot.set_position(*start)
        bot.wade = True
        bot.grounded = False
        world = SimpleVoxelWorld()
        world.load(director._make_map_snapshot(current=True))
        world.apply(WorldDelta(world.map_epoch, server.world_manager.topology_version, tuple(changes)))
        subscription = server.world_manager.subscribe_mutations(lambda x, y, z, solid, color, version:
            world.apply(WorldDelta(world.map_epoch, version, (VoxelChange(x, y, z, solid, color),))))
        brain = SimpleBotBrain(world)
        runtime = director._runtime[bot.id]
        clock = [time.monotonic() + 1.]
        origin = clock[0]
        trace = []
        escaped_since = None
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            for tick in range(10 * 60):
                clock[0] = origin + tick / 60.
                if tick % 8 == 0:
                    frame = replace(_frame(director._snapshot_player(bot), created_at=clock[0]),
                                     frame_id=tick + 1, map_epoch=world.map_epoch)
                    intent = brain.decide(frame)
                    if intent is not None:
                        runtime.intent = intent
                server.loop_count = tick
                if tick % 2 == 0:
                    director._apply_motor(runtime, clock[0], 2. / 60.)
                await bot.simulate_tick(1. / 60.)
                server.world_mutations.commit_ready()
                if tick % 60 == 0:
                    trace.append((tick, tuple(round(v, 2) for v in bot.position), runtime.intent.debug_role))
                if not bot.alive:
                    break
                escaped = (bot.grounded and not bot.wade and bot.z < 236.0
                           and server.world_manager.spawn_position_is_safe(bot.position))
                if escaped:
                    if escaped_since is None:
                        escaped_since = tick
                else:
                    escaped_since = None
        server.world_manager.unsubscribe_mutations(subscription)
        if solid_above_bed:
            assert not bot.alive and bot.deaths == 1, trace
            assert director.terrain_recoveries == 1
            assert tick <= 7 * 60, trace
        else:
            # Retail collision permits a mostly vertical escape when the
            # editable bed cell is clear. Require a clear, supported dry body
            # throughout the final second, not arbitrary horizontal travel;
            # this fixture gives the recovered bot no further strategic goal.
            # A jump apex or an embedded standing flag is not recovery.
            assert bot.alive and bot.grounded and not bot.wade and bot.z < 236.0, trace
            assert server.world_manager.spawn_position_is_safe(bot.position), trace
            assert bot.deaths == 0 and director.terrain_recoveries == 0
            assert escaped_since is not None and escaped_since <= 7 * 60, trace
            assert tick - escaped_since >= 60, trace
    asyncio.run(scenario())


@pytest.mark.parametrize("line", [False, True])
def test_bot_construction_rechecks_a_swimmers_feet_after_the_movement_boundary(line):
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        builder = await director.add_bot(team=2, class_id=int(C.CLASS_SOLDIER))
        swimmer = await director.add_bot(team=2, class_id=int(C.CLASS_SOLDIER))
        builder.set_position(100.5, 100.5, 59.75)
        swimmer.set_position(110.5, 100.5, 59.75)
        cells = ((103, 100, 61), (104, 100, 61)) if line else ((103, 100, 61),)
        reservation, reason = server.construction.reserve_construction(builder.id, 2, cells)
        assert reservation is not None, reason
        blocks = builder.blocks
        builder.blocks -= len(cells)
        # The second body enters after validation. Its fractional native foot
        # is in z=61 although floor(z), floor(z)+1 only cover z=59 and z=60.
        swimmer.set_position(103.5, 100.5, 59.75)
        if line:
            server.combat._commit_block_line(builder, 0, (*cells[0], *cells[-1]), cells, 0x123456)
        else:
            server.combat._commit_block_build(builder, 0, cells[0], 0x123456)
        assert builder.blocks == blocks
        assert all(not server.world_manager.get_solid(*cell) for cell in cells)
        server.construction.release(reservation)
    asyncio.run(scenario())


def test_arena_counts_late_joins_and_intermission_does_not_suspend_the_tick():
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        server.mode = ArenaMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        bots = [await director.add_bot(team=2 if i < 2 else 3) for i in range(4)]
        await server.mode._begin_round()
        assert {p.id for p in server.mode.alive_players} == {p.id for p in bots}
        bots[0].alive = False
        await server.mode.on_player_death(bots[0], bots[2], 0)
        assert not server.mode.round_ended
        bots[1].alive = False
        with patch.object(time, "time", return_value=100.):
            await asyncio.wait_for(server.mode.on_player_death(bots[1], bots[2], 0), 1.0)
        assert server.mode.round_ended and server.mode.next_round_at == 105.
        with patch.object(time, "time", return_value=104.):
            await server.mode.on_tick(100)
            assert server.mode.current_round == 1
        with patch.object(time, "time", return_value=105.):
            await server.mode.on_tick(101)
        assert server.mode.current_round == 2 and server.mode.next_round_at is None
        assert all(p.alive for p in bots)
    asyncio.run(scenario())


def test_delayed_game_disconnect_cannot_refill_roster_or_commit_old_actions(monkeypatch):
    async def scenario():
        config = ServerConfig()
        config.bots.max_bots = 3
        config.bots.population_mode = "fixed"
        supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                     request_restart=lambda: None, close=lambda: None)
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=supervisor)
        server.bots = director
        await director.start(initial_count=3)
        original_remove = director.remove_bot
        retired = asyncio.Event()
        resume = asyncio.Event()
        calls = []

        async def delayed_remove(bot, **kwargs):
            result = await original_remove(bot, **kwargs)
            if not retired.is_set():
                retired.set()
                await resume.wait()
            return result

        monkeypatch.setattr(director, "remove_bot", delayed_remove)
        monkeypatch.setattr(director, "_commit_pending_action", lambda *args: calls.append(args))
        transition = asyncio.create_task(director.prepare_for_game_transition())
        try:
            await asyncio.wait_for(retired.wait(), 2.0)
            old_roster = tuple(director.bots)
            assert len(old_roster) == 2
            runtime = director._runtime[old_roster[0].id]
            action = BotAction(BotActionKind.FIRE)
            runtime.pending_action = action
            director._pending_gateway_actions[runtime.player.id] = (runtime.generation, action, 1.0)
            director._next_population_at = 0.0
            await director.update(1.0 / 60.0)
            await director._maintain_population(time.monotonic())
            director._execute_pending(runtime, action, time.monotonic())
            assert director.drain_actions(10) == 0
            assert not calls
            assert tuple(director.bots) == old_roster
        finally:
            resume.set()
            await transition
            await director.reset_after_round_restart()
            assert len(director.bots) == 3
            assert not director._pending_gateway_actions
            await director.close()
    asyncio.run(scenario())


def test_each_game_reconnects_bots_and_zeroes_stats_without_replacing_humans():
    async def scenario():
        config = ServerConfig()
        config.bots.max_bots = 3
        supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                     request_restart=lambda: None, close=lambda: None)
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        human = Player(100, "Human", 2, 6)
        server.players[human.id] = human
        server.teams[2].add_player(human)
        director = BotDirector(server, supervisor=supervisor)
        server.bots = director
        await director.start(initial_count=3)
        try:
            for _ in range(4):
                previous = tuple(director.bots)
                profiles = {bot.id: director._runtime[bot.id].profile for bot in previous}
                generations = {bot.id: bot.bot_generation for bot in previous}
                for bot in previous:
                    bot.score, bot.kills, bot.deaths = 700000, 450, 120
                await server.mode._restart_round()
                assert server.players[100] is human
                assert len(director.bots) == 3
                for bot in director.bots:
                    assert all(bot is not old for old in previous)
                    assert (bot.score, bot.kills, bot.deaths) == (0, 0, 0)
                    assert bot.bot_generation > generations.get(bot.id, 0)
                    assert director._runtime[bot.id].profile != profiles.get(bot.id)
        finally:
            await director.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("fixture_name,map_name,start,destination,excavates", (
    ("bot_london_excavated_pocket.json", "London", (255.452, 188.356, 233.745),
     (124.59, 249.52, 223.75), True),
    ("bot_london_arena_pocket.json", "London", (125.543, 220.453, 235.748),
     (381.18, 229.221, 223.75), False),
    ("bot_london_drop_shelf.json", "London", (227.497, 276.454, 232.749),
     (137.59, 250.306, 223.75), False),
    ("bot_london_shelf_exit.json", "London", (225.496, 223.501, 231.749),
     (126.263, 242.68, 223.75), False),
    ("bot_mayan_corner_drop.json", "MayanJungle", (143.869, 209.457, 205.744),
     (434.18, 245.221, 206.75), False),
))
def test_bot_escapes_excavated_map_pockets_with_native_physics(
    monkeypatch, fixture_name, map_name, start, destination, excavates,
):
    """Replay the terrain edits around the 76- and 69-second freezes."""
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        assert server.world_manager.load_map(map_name)
        changes = json.loads((Path(__file__).parent / "fixtures" / fixture_name).read_text())
        for x, y, z, solid in changes:
            server.world_manager.set_block(x, y, z, solid, 0x123456)
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        bot = await director.add_bot(team=3, class_id=int(C.CLASS_SOLDIER))
        bot.loadout = [int(C.RIFLE_TOOL), int(C.SPADE_TOOL), int(C.BLOCK_TOOL)]
        bot.set_position(*start)
        world = SimpleVoxelWorld()
        world.load(director._make_map_snapshot(current=True))
        world.apply(WorldDelta(world.map_epoch, server.world_manager.topology_version,
                               tuple(VoxelChange(x, y, z, solid, 0x123456)
                                     for x, y, z, solid in changes)))
        subscription = server.world_manager.subscribe_mutations(lambda x, y, z, solid, color, version:
            world.apply(WorldDelta(world.map_epoch, version, (VoxelChange(x, y, z, solid, color),))))
        brain = SimpleBotBrain(world)
        temperament = brain._traversal_personality
        monkeypatch.setattr(brain, "_traversal_personality", lambda *args:
                            replace(temperament(*args), style=(
                                _TraversalStyle.SWIM if excavates else _TraversalStyle.DRY)))
        monkeypatch.setattr(brain, "_team_lane_segment_goal", lambda _frame, _observer, goal: goal)
        goal = _Goal(("pocket",), destination, "pocket_exit", 1., True)
        observer = director._snapshot_player(bot)
        arguments = dict(abilities=_movement_abilities(observer), dig_profile=_dig_profile(observer))
        if excavates:
            assert not world.plan(start, goal.position, allow_water=True, **arguments).steps
            dry = world.plan(start, goal.position, allow_water=False, **arguments)
            assert any(step.affordance is MovementAffordance.BREACH for step in dry.steps)
        monkeypatch.setattr(brain, "_select_goal", lambda *_args, **_kwargs: goal)
        runtime = director._runtime[bot.id]
        clock = [time.monotonic() + 1.]
        base = clock[0]
        initial_topology = server.world_manager.topology_version
        trace = []
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            for tick in range(60 * 60):
                clock[0] = base + tick / 60.
                if tick and tick % 8 == 0:
                    server.world_manager.set_block(5, 5, 61, bool(tick // 8 % 2), 0x123456)
                if tick % 8 == 0:
                    observer = director._snapshot_player(bot)
                    frame = replace(_frame(observer, created_at=clock[0]),
                                    frame_id=tick+1, topology_version=server.world_manager.topology_version)
                    runtime.intent = brain.decide(frame)
                server.loop_count = tick
                if tick % 2 == 0:
                    director._apply_motor(runtime, clock[0], 2. / 60.)
                await bot.simulate_tick(1. / 60.)
                server.world_mutations.commit_ready()
                if tick % 60 == 0:
                    trace.append((tick, tuple(round(v, 2) for v in bot.position), runtime.intent.debug_role))
                if math.dist(bot.position, start) >= 8.:
                    break
        server.world_manager.unsubscribe_mutations(subscription)
        assert bot.alive and math.dist(bot.position, start) >= 8., "\n".join(map(str, trace))
        if fixture_name in {"bot_london_drop_shelf.json", "bot_london_shelf_exit.json", "bot_mayan_corner_drop.json"}:
            assert bot.health == 100 and tick < 15 * 60, trace
        if excavates:
            assert server.world_manager.topology_version > initial_topology, trace
    asyncio.run(scenario())
