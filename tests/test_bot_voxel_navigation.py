"""Regression tests for semantic bot traversal of the live voxel world."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
import math
import random
import time
from types import SimpleNamespace

import shared.constants as C

from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    MapSnapshot,
    MovementAffordance,
    MovementIntent,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
    VoxelChange,
    WorldDelta,
)
from server.bot_ai.voxel_navigation import VoxelActionPlanner, VoxelTerrain
from server.bot_ai.worker import BotBrain, WorkerVoxelWorld, _BrainState
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.world_manager import WorldManager


def _solid_columns(
    columns: dict[tuple[int, int], set[int]],
) -> Callable[[int, int, int], bool]:
    """Return a deterministic solid query for small navigation fixtures."""

    return lambda x, y, z: int(z) in columns.get((int(x), int(y)), set())


def test_open_waterbed_is_not_an_ordinary_standing_node() -> None:
    terrain = VoxelTerrain(_solid_columns({(10, 10): {239}}))

    assert terrain.standing_node(10, 10, 236.75) is None


def test_mayan_base_voxel_replan_turns_through_stair_exit() -> None:
    canonical = WorldManager(
        ServerConfig(default_map="MayanJungle", maps_path="maps")
    )
    assert canonical.load_map("MayanJungle")
    world = WorkerVoxelWorld()
    world.load(
        MapSnapshot(
            1,
            0,
            bytes(canonical.map_raw_bytes or b""),
            "tdm",
            "MayanJungle",
        )
    )
    abilities = frozenset(
        {
            MovementAffordance.CROUCH,
            MovementAffordance.JUMP,
            MovementAffordance.DROP,
        }
    )
    goal = (447.63, 256.80, 206.75)

    start = (131.84, 233.51, 207.75)
    position = start
    for _ in range(12):
        step = world.action_planner.plan_local(
            position,
            goal,
            abilities=abilities,
            topology_version=0,
        )
        assert step is not None
        assert world._direction_is_traversable(
            position, step.direction, step.affordance
        )
        position = step.waypoint

    assert position[0] > start[0] + 4.0
    assert position[1] > start[1] + 3.0


def test_local_planner_recenters_an_offset_bot_on_a_narrow_route() -> None:
    columns = {(x, 5): {10} for x in range(5, 11)}
    terrain = VoxelTerrain(_solid_columns(columns))
    planner = VoxelActionPlanner(terrain)
    start = (5.5, 5.65, 7.75)

    assert not terrain.direction_is_traversable(
        start, (1.0, 0.0, 0.0), MovementAffordance.WALK
    )
    step = planner.plan_local(
        start,
        (9.5, 5.5, 7.75),
        abilities=frozenset({MovementAffordance.JUMP, MovementAffordance.DROP}),
        topology_version=3,
    )

    assert step is not None
    assert step.direction[0] > 0.9
    assert step.direction[1] < 0.0
    assert terrain.direction_is_traversable(
        start, step.direction, step.affordance
    )


def test_dry_escape_corridor_rejects_water_beyond_the_immediate_step() -> None:
    columns = {
        (10, 10): {23},
        (11, 10): {23},
        (12, 10): {239},
    }
    terrain = VoxelTerrain(_solid_columns(columns))
    start = (10.5, 10.5, 20.75)

    assert terrain.direction_is_traversable(
        start,
        (1.0, 0.0, 0.0),
        MovementAffordance.WALK,
    )
    assert not terrain.dry_corridor_is_traversable(
        start,
        (1.0, 0.0, 0.0),
        distance=2.0,
    )


def test_narrow_bridge_widening_follows_the_body_overhang() -> None:
    columns = {
        (5, 5): {10},
        (5, 4): {10},
        (5, 3): {10},
        (5, 2): {10},
        (4, 5): {10},
    }
    world = WorkerVoxelWorld()
    world.solid = _solid_columns(columns)

    line = world.narrow_bridge_shoulder_line(
        (5.2, 5.5, 7.75),
        (0.0, -1.0, 0.0),
        max_cells=3,
    )

    assert line == ((4, 4, 10), (4, 2, 10))


def test_castlewars_water_exit_reaches_land_beyond_local_search_radius() -> None:
    """A bot stranded in CastleWars' outer water must receive an exit step."""

    world = WorldManager(
        ServerConfig(default_map="CastleWars", maps_path="maps")
    )
    assert world.load_map("CastleWars")
    planner = VoxelActionPlanner(VoxelTerrain(world.get_solid))

    position = (511.5, 0.5, 236.75)
    for _ in range(200):
        if not world.is_water_column(int(position[0]), int(position[1])):
            break
        step = planner.water_exit(position)
        assert step is not None
        next_is_water = world.is_water_column(
            int(step.waypoint[0]), int(step.waypoint[1])
        )
        assert step.affordance is (
            MovementAffordance.WALK
            if next_is_water
            else MovementAffordance.JUMP
        )
        position = step.waypoint
    else:
        raise AssertionError("CastleWars escape flow did not reach dry land")

    assert world.is_water_column(int(position[0]), int(position[1])) is False


def test_double_dragon_island_falls_reach_bounded_dry_exits() -> None:
    world = WorldManager(
        ServerConfig(default_map="DoubleDragon", maps_path="maps")
    )
    assert world.load_map("DoubleDragon")
    planner = VoxelActionPlanner(VoxelTerrain(world.get_solid))

    for start in (
        (132.5, 286.5, 236.75),
        (384.5, 250.5, 236.75),
    ):
        position = start
        for _ in range(80):
            if not world.is_water_column(
                int(position[0]), int(position[1])
            ):
                break
            step = planner.water_exit(position)
            assert step is not None
            position = step.waypoint
        else:
            raise AssertionError(
                f"DoubleDragon escape did not reach land from {start}"
            )

        assert not world.is_water_column(
            int(position[0]), int(position[1])
        )


def test_double_dragon_water_recovery_moves_real_player_physics_to_land() -> None:
    """Exercise worker waypoints through the live motor and native 60 Hz body."""

    async def scenario() -> None:
        config = ServerConfig(
            default_map="DoubleDragon",
            maps_path="maps",
        )
        config.bots.max_bots = 1
        server = BattleSpadesServer(config)
        assert server.world_manager.load_map("DoubleDragon")
        director = BotDirector(server, supervisor=SimpleNamespace())
        bot = await director.add_bot(team=TEAM1, name="NativeSwimmer")
        assert bot is not None
        runtime = director._runtime[bot.id]
        planner = VoxelActionPlanner(
            VoxelTerrain(server.world_manager.get_solid)
        )
        frame_id = 0
        simulated_now = time.monotonic()

        for start in (
            (132.5, 286.5, 236.75),
            (384.5, 250.5, 236.75),
        ):
            bot.set_position(*start)
            bot._world_object.set_velocity(0.0, 0.0, 0.0)
            await bot.simulate_tick(1.0 / 60.0)
            stalled_ticks = 0
            max_stalled_ticks = 0
            landed_after = None

            for tick in range(60 * 45):
                if not server.world_manager.is_water_column(
                    int(bot.x), int(bot.y)
                ):
                    landed_after = tick / 60.0
                    break
                step = planner.water_exit(bot.position)
                assert step is not None
                frame_id += 1
                now = simulated_now + frame_id / 60.0
                runtime.intent = BotIntent(
                    bot_id=bot.id,
                    bot_generation=runtime.generation,
                    frame_id=frame_id,
                    map_epoch=0,
                    mode_epoch=0,
                    topology_version=0,
                    created_at=now,
                    expires_at=now + 1.0,
                    movement=MovementIntent(
                        direction=step.direction,
                        jump=True,
                        affordance=step.affordance,
                    ),
                    debug_role="native_water_acceptance",
                )
                server.loop_count += 1
                before = bot.position
                director._apply_motor(runtime, now, 1.0 / 60.0)
                await bot.simulate_tick(1.0 / 60.0)
                planar_delta = math.hypot(
                    bot.x - before[0],
                    bot.y - before[1],
                )
                stalled_ticks = (
                    stalled_ticks + 1 if planar_delta < 1e-5 else 0
                )
                max_stalled_ticks = max(max_stalled_ticks, stalled_ticks)

            assert landed_after is not None, (
                f"native body did not leave water from {start}: {bot.position}"
            )
            assert landed_after < 40.0
            assert max_stalled_ticks < 60

    asyncio.run(scenario())


def test_double_dragon_production_brain_drives_real_spawned_players() -> None:
    """Run actual TDM policy, motor, mutations, and native bodies together."""

    async def scenario() -> None:
        random_state = random.getstate()
        random.seed(7331)
        try:
            config = ServerConfig(
                default_map="DoubleDragon",
                default_mode="tdm",
                maps_path="maps",
            )
            config.bots.max_bots = 2
            server = BattleSpadesServer(config)
            assert server.world_manager.load_map("DoubleDragon")
            server.mode = TDMMode(server)
            await server.mode.on_mode_start()
            director = BotDirector(server, supervisor=SimpleNamespace())
            engineer = await director.add_bot(
                team=TEAM1,
                name="RuntimeEngineer",
                class_id=int(C.CLASS_ENGINEER),
            )
            soldier = await director.add_bot(
                team=TEAM2,
                name="RuntimeSoldier",
                class_id=int(C.CLASS_SOLDIER),
            )
            assert engineer is not None and soldier is not None
            players = (engineer, soldier)
            starts = {player.id: player.position for player in players}
            assert all(
                server.world_manager.spawn_position_is_safe(position)
                for position in starts.values()
            )

            worker_world = WorkerVoxelWorld()
            worker_world.load(director._make_map_snapshot(current=False))
            brain = BotBrain(
                worker_world,
                seed=7,
                decision_hz=8.0,
                path_requests_per_second=24.0,
            )
            pending_deltas: dict[int, list[VoxelChange]] = {}

            def remember_delta(x, y, z, solid, color, version) -> None:
                pending_deltas.setdefault(int(version), []).append(
                    VoxelChange(x, y, z, solid, color)
                )

            subscription = server.world_manager.subscribe_mutations(
                remember_delta
            )
            previous = dict(starts)
            stall_ticks = {player.id: 0 for player in players}
            max_stall_ticks = {player.id: 0 for player in players}
            idle_stall_ticks = {player.id: 0 for player in players}
            max_idle_stall_ticks = {player.id: 0 for player in players}
            stall_transitions: dict[int, list[tuple[int, str]]] = {
                player.id: [] for player in players
            }
            longest_stall: dict[int, dict[str, object]] = {}
            water_ticks = {player.id: 0 for player in players}
            engineer_weapon_ticks = 0
            engineer_sample_ticks = 0
            engineer_build_actions = 0
            base = time.monotonic() + 1.0
            frame_id = 0

            try:
                for tick in range(60 * 30):
                    now = base + tick / 60.0
                    if tick % 8 == 0:
                        snapshots = director._snapshot_players()
                        entities = director._snapshot_entities()
                        objectives = director._snapshot_objectives()
                        for player in players:
                            runtime = director._runtime[player.id]
                            frame_id += 1
                            intent = brain.decide(
                                PerceptionFrame(
                                    frame_id=frame_id,
                                    map_epoch=0,
                                    mode_epoch=0,
                                    topology_version=(
                                        server.world_manager.topology_version
                                    ),
                                    observer_id=player.id,
                                    observer_generation=runtime.generation,
                                    created_at=now,
                                    mode_id="tdm",
                                    players=snapshots,
                                    profile=runtime.profile,
                                    entities=entities,
                                    objectives=objectives,
                                )
                            )
                            assert intent is not None
                            runtime.intent = intent
                            if (
                                player is engineer
                                and intent.action.kind
                                in {
                                    BotActionKind.BUILD,
                                    BotActionKind.BUILD_LINE,
                                    BotActionKind.PLACE_PREFAB,
                                }
                            ):
                                engineer_build_actions += 1

                    server.loop_count += 1
                    for player in players:
                        director._apply_motor(
                            director._runtime[player.id],
                            now,
                            1.0 / 60.0,
                        )
                    await server.simulation_runtime._simulate_players()
                    server.world_mutations.commit_ready()
                    server.prefab_actions.tick()
                    for version, changes in sorted(pending_deltas.items()):
                        worker_world.apply(
                            WorldDelta(
                                map_epoch=0,
                                topology_version=version,
                                changed_cells=tuple(changes),
                            )
                        )
                    pending_deltas.clear()

                    for player in players:
                        runtime = director._runtime[player.id]
                        intent = runtime.intent
                        requested = (
                            intent is not None
                            and intent.expires_at > now
                            and math.hypot(
                                intent.movement.direction[0],
                                intent.movement.direction[1],
                            )
                            > 0.1
                        )
                        planar_delta = math.hypot(
                            player.x - previous[player.id][0],
                            player.y - previous[player.id][1],
                        )
                        stall_ticks[player.id] = (
                            stall_ticks[player.id] + 1
                            if requested and planar_delta < 1e-5
                            else 0
                        )
                        active_recovery = (
                            intent.debug_role.startswith(
                                (
                                    "hole_",
                                    "stuck_",
                                    "water_gap_",
                                    "single_gap_",
                                )
                            )
                            or intent.action.kind
                            in {
                                BotActionKind.BUILD,
                                BotActionKind.BUILD_LINE,
                                BotActionKind.MELEE,
                                BotActionKind.PLACE_PREFAB,
                            }
                        )
                        idle_stall_ticks[player.id] = (
                            idle_stall_ticks[player.id] + 1
                            if requested
                            and planar_delta < 1e-5
                            and not active_recovery
                            else 0
                        )
                        max_idle_stall_ticks[player.id] = max(
                            max_idle_stall_ticks[player.id],
                            idle_stall_ticks[player.id],
                        )
                        if stall_ticks[player.id] == 0:
                            stall_transitions[player.id].clear()
                        elif (
                            not stall_transitions[player.id]
                            or stall_transitions[player.id][-1][1]
                            != intent.debug_role
                        ):
                            stall_transitions[player.id].append(
                                (tick, intent.debug_role)
                            )
                        if stall_ticks[player.id] > max_stall_ticks[player.id]:
                            max_stall_ticks[player.id] = stall_ticks[player.id]
                            state = brain._states[
                                (player.id, runtime.generation)
                            ]
                            longest_stall[player.id] = {
                                "tick": tick,
                                "position": tuple(
                                    round(float(value), 3)
                                    for value in player.position
                                ),
                                "velocity": tuple(
                                    round(float(value), 4)
                                    for value in (player.vx, player.vy, player.vz)
                                ),
                                "role": intent.debug_role,
                                "affordance": intent.movement.affordance.value,
                                "requested": tuple(
                                    round(float(value), 3)
                                    for value in intent.movement.direction
                                ),
                                "motor": runtime.movement_input,
                                "path_direction": tuple(
                                    round(float(value), 3)
                                    for value in state.last_path_direction
                                ),
                                "path_goal": state.path_goal,
                                "escape_goal": state.route_escape_goal,
                                "escape_failures": state.route_escape_failures,
                                "stuck_attempts": state.stuck_attempts,
                                "transitions": tuple(
                                    stall_transitions[player.id]
                                ),
                            }
                        previous[player.id] = player.position
                        water_ticks[player.id] += int(player.wade)

                    if tick >= 60 * 20:
                        engineer_sample_ticks += 1
                        engineer_weapon_ticks += int(
                            engineer.tool == engineer.weapon
                        )
            finally:
                server.world_manager.unsubscribe_mutations(subscription)

            assert math.dist(starts[engineer.id], engineer.position) > 6.0
            # Native movement accumulates slightly different floating-point
            # trajectories across Windows and Linux.  The contract here is
            # sustained traversal, not an exact platform-specific distance.
            assert math.dist(starts[soldier.id], soldier.position) > 15.0
            assert water_ticks == {engineer.id: 0, soldier.id: 0}
            if (
                max(max_stall_ticks.values()) >= 300
                or max(max_idle_stall_ticks.values()) >= 120
            ):
                for player in players:
                    runtime = director._runtime[player.id]
                    state = brain._states[(player.id, runtime.generation)]
                    position = tuple(
                        float(value)
                        for value in longest_stall[player.id]["position"]
                    )
                    directions = tuple(
                        (
                            round(math.cos(math.radians(angle)), 3),
                            round(math.sin(math.radians(angle)), 3),
                            0.0,
                        )
                        for angle in range(0, 360, 45)
                    )
                    longest_stall[player.id]["terrain"] = {
                        "surface": worker_world._terrain.classify(
                            int(math.floor(position[0])),
                            int(math.floor(position[1])),
                            position[2],
                            vertical_span=8,
                        ),
                        "directions": tuple(
                            (
                                direction,
                                worker_world._terrain.direction_is_traversable(
                                    position,
                                    direction,
                                    MovementAffordance.WALK,
                                ),
                                worker_world._terrain.dry_corridor_is_traversable(
                                    position,
                                    direction,
                                    distance=1.5,
                                ),
                            )
                            for direction in directions
                        ),
                        "blocking": worker_world.blocking_cell(
                            position, state.last_path_direction
                        ),
                        "bridge": worker_world.bridge_cell(
                            position, state.last_path_direction
                        ),
                        "bridge_line": worker_world.water_bridge_line(
                            position, state.last_path_direction
                        ),
                        "emergency_drop": worker_world.emergency_drop(position),
                    }
            stall_report = "\n".join(
                f"bot {player_id}: {details!r}"
                for player_id, details in longest_stall.items()
            )
            # A topology-changing jump/build/breach sequence is visible work,
            # not an idle walking pause. Bound the whole stationary recovery
            # to five seconds, while ordinary non-recovery idling remains
            # capped at two seconds.
            assert max(max_stall_ticks.values()) < 300, stall_report
            assert max(max_idle_stall_ticks.values()) < 120, stall_report
            assert engineer_build_actions >= 1
            assert engineer_weapon_ticks / engineer_sample_ticks > 0.8
            assert engineer.tool == engineer.weapon
        finally:
            random.setstate(random_state)

    asyncio.run(scenario())


def test_remote_terrain_delta_does_not_discard_cached_water_escape() -> None:
    terrain = VoxelTerrain(
        _solid_columns(
            {
                (10, 10): {239},
                (11, 10): {239},
                (12, 10): {238},
            }
        )
    )
    planner = VoxelActionPlanner(terrain)
    assert planner.water_exit((10.5, 10.5, 236.75)) is not None
    cached = dict(planner._water_next)

    planner.invalidate_water_routes(frozenset({(400, 400)}))
    assert planner._water_next == cached

    planner.invalidate_water_routes(frozenset({(11, 10)}))
    assert planner._water_next == {}


class _RecordingNavigator:
    """Capture whether a tile is built or removed without native Recast."""

    def __init__(self) -> None:
        self.built_vertices: list[float] | None = None
        self.removed = False

    def build_tile(self, _tile_x, _tile_y, vertices, *_args) -> bool:
        self.built_vertices = list(vertices)
        return True

    def remove_tile(self, _tile_x: int, _tile_y: int) -> bool:
        self.removed = True
        return True


class _ZeroCrowdNavigator:
    """Prove live navigation never enters the unsafe native path fallback."""

    def __init__(self) -> None:
        self.find_path_calls = 0

    def crowd_steer(self, *_args, **_kwargs):
        return (0.0, 0.0, 0.0)

    def find_path(self, *_args, **_kwargs):
        self.find_path_calls += 1
        raise AssertionError("live worker must use bounded voxel A* fallback")


def test_recast_tile_omits_the_universal_waterbed_surface() -> None:
    world = WorkerVoxelWorld()
    navigator = _RecordingNavigator()
    world._vxl = object()
    world._native_nav = navigator
    world.solid = lambda x, y, z: (int(x), int(y), int(z)) == (0, 0, 239)

    world._rebuild_native_tile(0, 0)

    assert navigator.built_vertices is None
    assert navigator.removed is True


def test_native_steering_preserves_a_two_block_jump_affordance() -> None:
    columns = {
        (5, 5): {10},
        # z grows downward, so support 8 is a two-block climb from support 10.
        (6, 5): {8},
    }
    world = WorkerVoxelWorld()
    world._vxl = object()
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    world._native_path_direction = lambda *_args, **_kwargs: (1.0, 0.0, 0.0)

    direction = world.next_path_direction(
        (5.5, 5.5, 7.75),
        (20.5, 5.5, 7.75),
        agent_id=4,
        abilities=frozenset({MovementAffordance.JUMP}),
    )

    assert direction == (1.0, 0.0, 0.0)
    assert world.last_affordance(4) is MovementAffordance.JUMP


def test_unsafe_body_width_native_vector_falls_back_to_voxel_route() -> None:
    columns = {(x, 5): {10} for x in range(5, 11)}
    world = WorkerVoxelWorld()
    world._vxl = object()
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    world._native_path_direction = (
        lambda *_args, **_kwargs: (1.0, -0.2, 0.0)
    )
    start = (5.5, 5.5, 7.75)
    abilities = frozenset(
        {
            MovementAffordance.CROUCH,
            MovementAffordance.JUMP,
            MovementAffordance.DROP,
        }
    )

    assert not world._direction_is_traversable(
        start, (1.0, -0.2, 0.0), MovementAffordance.WALK
    )

    direction = world.next_path_direction(
        start,
        (9.5, 5.5, 7.75),
        agent_id=4,
        abilities=abilities,
    )

    assert direction == (1.0, 0.0, 0.0)


def test_gap_jump_requires_an_empty_lane_and_body_width_landing() -> None:
    columns = {
        (5, 5): {10},
        (3, 5): {8},
    }
    terrain = VoxelTerrain(_solid_columns(columns))
    start = (5.5, 5.5, 7.75)

    assert terrain.direction_is_traversable(
        start,
        (-1.0, 0.0, 0.0),
        MovementAffordance.JUMP,
    )
    assert not terrain.direction_is_traversable(
        start,
        (-1.0, 0.0, 0.0),
        MovementAffordance.WALK,
    )

    columns[(4, 5)] = {7, 8}
    assert not terrain.direction_is_traversable(
        start,
        (-1.0, 0.0, 0.0),
        MovementAffordance.JUMP,
    )


def test_zero_native_crowd_steering_skips_unbounded_find_path_fallback() -> None:
    world = WorkerVoxelWorld()
    navigator = _ZeroCrowdNavigator()
    start = (5.5, 5.5, 7.75)
    goal = (20.5, 5.5, 7.75)
    world._vxl = object()
    world._native_nav = navigator
    world._built_tiles.update(world._tile_corridor(start, goal))

    direction = world._native_path_direction(
        start,
        goal,
        agent_id=4,
        velocity=(0.0, 0.0, 0.0),
    )

    assert direction == (0.0, 0.0, 0.0)
    assert navigator.find_path_calls == 0


def test_worker_proposes_one_block_line_across_water_gap() -> None:
    columns = {
        (5, 5): {10},
        (10, 5): {10},
    }
    world = WorkerVoxelWorld()
    world._vxl = object()
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)

    line = world.water_bridge_line(
        (5.5, 5.5, 7.75), (1.0, 0.0, 0.0), max_cells=6
    )

    assert line == ((6, 5, 10), (9, 5, 10))


def test_perception_snapshot_publishes_authoritative_wade_state() -> None:
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = asyncio.run(
        director.add_bot(
            team=TEAM1,
            name="WadingBot",
            class_id=int(C.CLASS_SOLDIER),
        )
    )
    assert bot is not None
    bot.wade = True

    snapshot = next(
        player
        for player in director._snapshot_players()
        if player.player_id == bot.id
    )

    assert snapshot.wade is True


def test_live_motor_rejects_open_water_ahead_for_a_dry_bot() -> None:
    world = SimpleNamespace(
        topology_version=7,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda _x, _y: True,
        get_height=lambda _x, _y: 239,
    )
    server = SimpleNamespace(world_manager=world)
    player = SimpleNamespace(
        x=10.5,
        y=10.5,
        z=20.75,
        wade=False,
        connection=SimpleNamespace(server=server),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(runtime, (1.0, 0.0, 0.0)) is False


def test_live_motor_allows_a_wading_bot_to_follow_its_escape_flow() -> None:
    world = SimpleNamespace(
        topology_version=8,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda _x, _y: True,
        get_solid=lambda _x, _y, z: int(z) == 239,
    )
    player = SimpleNamespace(
        x=200.5,
        y=200.5,
        z=236.75,
        wade=True,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(
        runtime,
        (1.0, 0.0, 0.0),
        MovementAffordance.JUMP,
    ) is True


def test_live_motor_brakes_earlier_from_water_as_dry_speed_rises() -> None:
    world = SimpleNamespace(
        topology_version=9,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda x, _y: int(x) >= 11,
        get_height=lambda _x, _y: 23,
    )
    player = SimpleNamespace(
        x=10.0,
        y=10.5,
        z=20.75,
        vx=4.0,
        vy=0.0,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(runtime, (1.0, 0.0, 0.0)) is False


def test_live_motor_uses_native_velocity_units_for_sprint_braking() -> None:
    """A realistic 0.35 native sprint must see water four blocks ahead."""

    world = SimpleNamespace(
        topology_version=10,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda x, _y: int(x) >= 13,
        get_height=lambda _x, _y: 23,
    )
    player = SimpleNamespace(
        x=10.0,
        y=10.5,
        z=20.75,
        vx=0.35,
        vy=0.0,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(
        runtime,
        (1.0, 0.0, 0.0),
    ) is False


def test_live_motor_allows_a_body_clear_one_cell_gap_jump() -> None:
    columns = {
        (5, 5): {10},
        (3, 5): {8},
    }
    solid = _solid_columns(columns)
    world = SimpleNamespace(
        topology_version=8,
        clipbox=lambda x, y, z: solid(
            int(x // 1), int(y // 1), int(z // 1)
        ),
        is_water_column=lambda _x, _y: False,
        get_solid=solid,
    )
    player = SimpleNamespace(
        x=5.5,
        y=5.5,
        z=7.75,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(
        runtime,
        (-1.0, 0.0, 0.0),
        MovementAffordance.JUMP,
    )


def test_live_motor_rejects_a_walk_step_over_an_unsafe_drop() -> None:
    world = SimpleNamespace(
        topology_version=8,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda _x, _y: False,
        get_height=lambda _x, _y: 40,
    )
    player = SimpleNamespace(
        x=10.5,
        y=10.5,
        z=20.75,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(runtime, (1.0, 0.0, 0.0)) is False


def test_live_motor_checks_both_shoulders_near_an_edge() -> None:
    world = SimpleNamespace(
        topology_version=9,
        clipbox=lambda _x, _y, _z: False,
        is_water_column=lambda _x, _y: False,
        get_height=lambda _x, y: 40 if int(y) >= 11 else 23,
    )
    player = SimpleNamespace(
        x=10.5,
        y=10.9,
        z=20.75,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    assert BotDirector._waypoint_is_live(runtime, (1.0, 0.0, 0.0)) is False


def test_live_motor_slides_ordinary_walk_around_a_narrow_obstruction() -> None:
    world = SimpleNamespace(
        topology_version=10,
        clipbox=lambda x, y, _z: (
            float(x) >= 11.0 and abs(float(y) - 10.5) < 0.25
        ),
        is_water_column=lambda _x, _y: False,
        get_height=lambda _x, _y: 23,
    )
    player = SimpleNamespace(
        x=10.5,
        y=10.5,
        z=20.75,
        wade=False,
        connection=SimpleNamespace(
            server=SimpleNamespace(world_manager=world)
        ),
    )
    runtime = SimpleNamespace(
        player=player,
        waypoint_probe_key=None,
        waypoint_probe_result=False,
    )

    direction = BotDirector._live_movement_direction(
        runtime,
        (1.0, 0.0, 0.0),
        MovementAffordance.WALK,
    )

    assert math.hypot(direction[0], direction[1]) > 0.99
    assert direction[0] > 0.0
    assert abs(direction[1]) > 0.5
    assert BotDirector._waypoint_is_live(
        runtime, direction, MovementAffordance.WALK
    )


def test_live_motor_splits_bot_detours_across_both_sides(monkeypatch) -> None:
    def live_probe(_runtime, direction, _affordance):
        return abs(float(direction[1])) > 0.1

    monkeypatch.setattr(
        BotDirector,
        "_waypoint_is_live",
        staticmethod(live_probe),
    )
    first = BotDirector._live_movement_direction(
        SimpleNamespace(player=SimpleNamespace(id=1), generation=1),
        (1.0, 0.0, 0.0),
        MovementAffordance.WALK,
    )
    second = BotDirector._live_movement_direction(
        SimpleNamespace(player=SimpleNamespace(id=2), generation=1),
        (1.0, 0.0, 0.0),
        MovementAffordance.WALK,
    )

    assert first[0] == second[0]
    assert first[1] == -second[1]
    assert abs(first[1]) > 0.1


def test_live_motor_does_not_rotate_a_blocked_jump_landing(monkeypatch) -> None:
    probes = []

    def live_probe(_runtime, direction, _affordance):
        probes.append(tuple(direction))
        return abs(float(direction[1])) > 0.1

    monkeypatch.setattr(
        BotDirector,
        "_waypoint_is_live",
        staticmethod(live_probe),
    )

    assert BotDirector._live_movement_direction(
        SimpleNamespace(),
        (1.0, 0.0, 0.0),
        MovementAffordance.JUMP,
    ) == (0.0, 0.0, 0.0)
    assert probes == [(1.0, 0.0, 0.0)]


def test_live_jump_gate_accepts_a_two_block_landing_support() -> None:
    world = SimpleNamespace(
        clipbox=lambda x, _y, z: float(x) >= 6.0 and int(z) == 8,
        is_water_column=lambda _x, _y: False,
        get_solid=lambda x, _y, z: float(x) >= 6.0 and int(z) == 8,
    )
    player = SimpleNamespace(z=7.75, wade=False)

    assert BotDirector._probe_surface_is_live(
        world,
        player,
        6.1,
        5.5,
        MovementAffordance.JUMP,
    ) is True
    assert BotDirector._probe_surface_is_live(
        world,
        player,
        6.1,
        5.5,
        MovementAffordance.WALK,
    ) is False


def test_jump_request_is_a_bounded_pulse_not_a_leased_held_key() -> None:
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = asyncio.run(
        director.add_bot(
            team=TEAM1,
            name="JumpPulseBot",
            class_id=int(C.CLASS_SOLDIER),
        )
    )
    assert bot is not None
    runtime = director._runtime[bot.id]
    now = time.monotonic()
    runtime.intent = BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=100,
        map_epoch=0,
        mode_epoch=0,
        topology_version=server.world_manager.topology_version,
        created_at=now,
        expires_at=now + 1.0,
        movement=MovementIntent(
            jump=True,
            affordance=MovementAffordance.JUMP,
        ),
    )

    server.loop_count = 100
    director._apply_motor(runtime, now, 1.0 / 60.0)
    assert runtime.movement_input is not None
    assert runtime.movement_input[4] is True

    server.loop_count = 104
    director._apply_motor(runtime, now + 4.0 / 60.0, 1.0 / 60.0)
    assert runtime.movement_input is not None
    assert runtime.movement_input[4] is False

    runtime.intent = replace(
        runtime.intent,
        frame_id=101,
        created_at=now + 8.0 / 60.0,
        expires_at=now + 1.0,
    )
    server.loop_count = 108
    director._apply_motor(runtime, now + 8.0 / 60.0, 1.0 / 60.0)
    assert runtime.movement_input is not None
    assert runtime.movement_input[4] is True


def test_wading_jump_request_remains_held_for_native_swimming() -> None:
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    director = BotDirector(server, supervisor=SimpleNamespace())
    bot = asyncio.run(
        director.add_bot(
            team=TEAM1,
            name="SwimHoldBot",
            class_id=int(C.CLASS_SOLDIER),
        )
    )
    assert bot is not None
    bot.wade = True
    runtime = director._runtime[bot.id]
    now = time.monotonic()
    runtime.intent = BotIntent(
        bot_id=bot.id,
        bot_generation=runtime.generation,
        frame_id=110,
        map_epoch=0,
        mode_epoch=0,
        topology_version=server.world_manager.topology_version,
        created_at=now,
        expires_at=now + 1.0,
        movement=MovementIntent(
            direction=(1.0, 0.0, 0.0),
            jump=True,
            affordance=MovementAffordance.WALK,
        ),
    )

    server.loop_count = 100
    director._apply_motor(runtime, now, 1.0 / 60.0)
    assert runtime.movement_input is not None
    assert runtime.movement_input[4] is True

    server.loop_count = 112
    director._apply_motor(runtime, now + 12.0 / 60.0, 1.0 / 60.0)
    assert runtime.movement_input is not None
    assert runtime.movement_input[4] is True


def test_vertical_bobbing_does_not_count_as_route_progress() -> None:
    state = _BrainState(
        last_position=(5.0, 5.0, 20.0),
        last_progress_at=1.0,
        stuck_attempts=2,
        last_path_direction=(1.0, 0.0, 0.0),
    )

    BotBrain._record_progress(state, (5.0, 5.0, 18.8), 2.0)

    assert state.last_progress_at == 1.0
    assert state.stuck_attempts == 2


def test_sideways_knockback_does_not_hide_a_route_stall() -> None:
    state = _BrainState(
        last_position=(5.0, 5.0, 20.0),
        last_progress_at=1.0,
        stuck_attempts=2,
        last_path_direction=(1.0, 0.0, 0.0),
        path_goal=(20.0, 5.0, 20.0),
    )

    BotBrain._record_progress(state, (5.0, 6.0, 20.0), 2.0)

    assert state.last_progress_at == 1.0
    assert state.stuck_attempts == 2


def test_short_route_oscillation_does_not_count_as_strategic_progress() -> None:
    state = _BrainState(
        last_position=(10.0, 10.0, 20.0),
        last_progress_at=1.0,
        last_path_direction=(1.0, 0.0, 0.0),
        path_goal=(100.0, 10.0, 20.0),
        strategic_progress_at=1.0,
        strategic_goal_distance=90.0,
        regional_progress_anchor=(10.0, 10.0, 20.0),
        regional_progress_at=1.0,
    )

    for index in range(1, 7):
        position = (10.0 + float(index % 2), 10.0, 20.0)
        BotBrain._record_progress(state, position, 1.0 + index)
    state.next_stuck_recovery_at = 7.5
    state.stuck_attempts = 2
    BotBrain._record_progress(state, (12.0, 10.0, 20.0), 8.0)

    assert state.last_progress_at > 1.0
    assert state.strategic_progress_at == 1.0
    assert state.strategic_goal_distance == 90.0
    assert state.regional_progress_anchor == (10.0, 10.0, 20.0)
    assert state.regional_progress_at == 1.0
    assert state.next_stuck_recovery_at == 7.5
    assert state.stuck_attempts == 2


def test_respawn_position_discontinuity_clears_previous_life_route() -> None:
    state = _BrainState(
        life_id=2,
        contacts={1: SimpleNamespace()},
        target_id=1,
        last_position=(108.0, 108.0, 40.0),
        last_progress_at=1.0,
        next_stuck_recovery_at=9.0,
        stuck_attempts=7,
        last_path_direction=(1.0, 0.0, 0.0),
        path=[(310.0, 300.0, 20.0)],
        path_goal=(400.0, 300.0, 20.0),
        path_topology_version=5,
        route_escape_goal=(290.0, 300.0, 20.0),
        route_escape_until=8.0,
        route_escape_failures=4,
        resource_target=(3, (320.0, 300.0, 20.0)),
        resource_target_since=2.0,
    )

    teleported = BotBrain._record_progress(
        state, (100.0, 100.0, 40.0), 10.0, life_id=3
    )

    assert teleported is True
    assert state.life_id == 3
    assert state.contacts == {}
    assert state.target_id is None
    assert state.path == []
    assert state.path_goal is None
    assert state.path_topology_version == -1
    assert state.last_path_direction == (0.0, 0.0, 0.0)
    assert state.last_progress_at == 10.0
    assert state.next_stuck_recovery_at == 0.0
    assert state.stuck_attempts == 0
    assert state.strategic_progress_at == 10.0
    assert state.strategic_goal_distance is None
    assert state.regional_progress_anchor is None
    assert state.regional_progress_at == 10.0
    assert state.route_escape_goal is None
    assert state.route_escape_failures == 0
    assert state.resource_target is None


def test_water_exit_selects_the_nearest_dry_body_clear_surface() -> None:
    columns = {
        (8, 8): {239},
        (9, 8): {239},
        (10, 8): {239},
        (11, 8): {237},
    }
    planner = VoxelActionPlanner(VoxelTerrain(_solid_columns(columns)))

    step = planner.water_exit((8.5, 8.5, 236.75))

    assert step is not None
    assert step.goal == (11.5, 8.5, 234.75)
    assert step.direction == (1.0, 0.0, 0.0)
    assert step.affordance is MovementAffordance.WALK

    shore = planner.water_exit((10.5, 8.5, 236.75))
    assert shore is not None
    assert shore.waypoint == (11.5, 8.5, 234.75)
    assert shore.affordance is MovementAffordance.JUMP


def test_water_exit_corrects_cross_track_drift_toward_cell_center() -> None:
    columns = {
        (8, 8): {239},
        (8, 9): {239},
        (8, 10): {237},
    }
    planner = VoxelActionPlanner(VoxelTerrain(_solid_columns(columns)))

    step = planner.water_exit((8.8, 8.5, 236.75))

    assert step is not None
    assert step.waypoint == (8.5, 9.5, 236.75)
    assert step.direction[0] < -0.25
    assert step.direction[1] > 0.9


def test_emergency_drop_finds_clear_adjacent_lower_surface() -> None:
    columns = {(5, 5): {10}, (6, 5): {20}}
    world = WorkerVoxelWorld()
    world._vxl = object()
    world.solid = _solid_columns(columns)

    drop = world.emergency_drop((5.5, 5.5, 7.75))

    assert drop is not None
    direction, landing = drop
    assert direction == (1.0, 0.0, 0.0)
    assert landing == (6.5, 5.5, 17.75)


def _navigation_player(
    player_id: int,
    team: int,
    position: tuple[float, float, float],
    *,
    class_id: int,
    wade: bool = False,
    is_bot: bool = False,
) -> PlayerSnapshot:
    """Build a complete immutable player view for worker behavior tests."""

    zombie_classes = {
        int(C.CLASS_ZOMBIE),
        int(C.CLASS_FAST_ZOMBIE),
        int(C.CLASS_JUMP_ZOMBIE),
    }
    return PlayerSnapshot(
        player_id=player_id,
        generation=1,
        team=team,
        class_id=class_id,
        alive=True,
        spawned=True,
        position=position,
        eye=(position[0], position[1], position[2] - 1.0),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=int(C.ZOMBIEHAND_TOOL),
        blocks=1000 if int(class_id) in zombie_classes else 100,
        ammo_clip=0,
        ammo_reserve=0,
        is_bot=is_bot,
        weapon_tool=int(C.ZOMBIEHAND_TOOL),
        loadout=(int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)),
        prefabs=(
            (
                "prefab_zombiehand",
                "prefab_zombiebone",
                "prefab_zombiehead",
            )
            if int(class_id) in zombie_classes
            else ()
        ),
        wade=wade,
    )


def test_deep_hole_escape_jumps_then_builds_under_the_airborne_bot() -> None:
    world = SimpleNamespace(
        solid=lambda *_cell: False,
        overhead_block=lambda _position: None,
        hole_escape=lambda _position, _direction: ((1.0, 0.0, 0.0), 4),
        jump_build_cell=lambda _position: (5, 5, 9),
        blocking_cell=lambda _position, _direction: None,
        water_bridge_line=lambda _position, _direction, **_kwargs: None,
        bridge_cell=lambda _position, _direction: None,
    )
    brain = BotBrain(world, seed=3)
    observer = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_ENGINEER),
            is_bot=True,
        ),
        grounded=True,
        blocks=20,
        tool=int(C.BLOCK_TOOL),
        weapon_tool=int(C.SHOTGUN_TOOL),
        loadout=(int(C.SHOTGUN_TOOL), int(C.SPADE_TOOL), int(C.BLOCK_TOOL)),
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        last_path_direction=(1.0, 0.0, 0.0),
    )

    launch = brain._stuck_recovery(frame, observer, state, 2.0)
    airborne = replace(observer, grounded=False, position=(5.5, 5.5, 6.9))
    place = brain._stuck_recovery(
        replace(frame, frame_id=2, players=(airborne,)),
        airborne,
        state,
        2.1,
    )

    assert launch is not None
    assert launch.debug_role == "hole_jump_build_launch"
    assert launch.movement.jump is True
    assert launch.priority is BotIntentPriority.TRAVERSAL
    assert place is not None
    assert place.debug_role == "hole_jump_build_place"
    assert place.action.kind is BotActionKind.BUILD
    assert place.action.position == (5.0, 5.0, 9.0)
    assert state.last_progress_at == 1.0


def test_stuck_bot_breaks_a_low_ceiling_before_climbing() -> None:
    world = SimpleNamespace(
        overhead_block=lambda _position: (5, 5, 5),
        hole_escape=lambda _position, _direction: ((1.0, 0.0, 0.0), 4),
        blocking_cell=lambda _position, _direction: None,
        water_bridge_line=lambda _position, _direction, **_kwargs: None,
        bridge_cell=lambda _position, _direction: None,
    )
    brain = BotBrain(world, seed=5)
    observer = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_MINER),
            is_bot=True,
        ),
        loadout=(int(C.SUPERSPADE_TOOL), int(C.BLOCK_TOOL)),
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        last_path_direction=(1.0, 0.0, 0.0),
    )

    intent = brain._stuck_recovery(frame, observer, state, 2.0)

    assert intent is not None
    assert intent.debug_role == "hole_break_ceiling"
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.position == (5.5, 5.5, 5.5)


def test_failed_ceiling_recovery_is_bounded_and_does_not_fake_progress() -> None:
    resets: list[int] = []
    world = SimpleNamespace(
        overhead_block=lambda _position: (5, 5, 5),
        hole_escape=lambda _position, _direction: None,
        blocking_cell=lambda _position, _direction: None,
        water_bridge_line=lambda _position, _direction, **_kwargs: None,
        bridge_cell=lambda _position, _direction: None,
        reset_agent_navigation=resets.append,
    )
    brain = BotBrain(world, seed=5)
    observer = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_MINER),
            is_bot=True,
        ),
        loadout=(int(C.SUPERSPADE_TOOL), int(C.BLOCK_TOOL)),
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        last_path_direction=(1.0, 0.0, 0.0),
    )

    first = brain._stuck_recovery(frame, observer, state, 2.0)
    second = brain._stuck_recovery(frame, observer, state, 2.8)
    exhausted = brain._stuck_recovery(frame, observer, state, 3.6)

    assert first is not None and first.debug_role == "hole_break_ceiling"
    assert second is not None and second.debug_role == "hole_break_ceiling"
    assert exhausted is None
    assert state.last_progress_at == 1.0
    assert state.stuck_attempts == 0
    assert resets == [observer.player_id]


def test_wading_zombie_recovers_before_engaging_a_visible_survivor() -> None:
    columns = {
        (8, 8): {239},
        (9, 8): {239},
        (10, 8): {239},
        (11, 8): {237},
    }
    world = WorkerVoxelWorld()
    world.map_epoch = 1
    world.topology_version = 1
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    brain = BotBrain(world, seed=4)
    zombie = _navigation_player(
        1,
        TEAM1,
        (8.5, 8.5, 236.75),
        class_id=int(C.CLASS_ZOMBIE),
        wade=True,
        is_bot=True,
    )
    survivor = _navigation_player(
        2,
        1,
        (20.5, 8.5, 234.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=now,
        mode_id="zom",
        players=(zombie, survivor),
        mode_phase="ACTIVE",
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.debug_role == "water_recovery"
    assert intent.movement.direction == (1.0, 0.0, 0.0)
    assert intent.movement.affordance is MovementAffordance.WALK


def test_close_zombie_uses_height_aware_jump_to_elevated_survivor() -> None:
    columns = {
        (6, 5): {10},
        (7, 5): {8},
    }
    world = WorkerVoxelWorld()
    world.map_epoch = 2
    world.topology_version = 3
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    brain = BotBrain(world, seed=5)
    zombie = _navigation_player(
        1,
        TEAM1,
        (6.5, 5.5, 7.75),
        class_id=int(C.CLASS_ZOMBIE),
        is_bot=True,
    )
    survivor = _navigation_player(
        2,
        1,
        (7.5, 5.5, 5.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=2,
        map_epoch=2,
        mode_epoch=1,
        topology_version=3,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=now,
        mode_id="zom",
        players=(zombie, survivor),
        mode_phase="ACTIVE",
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.movement.direction == (1.0, 0.0, 0.0)
    assert intent.movement.affordance is MovementAffordance.JUMP
    assert intent.movement.sprint is False


def test_unreachable_elevated_survivor_escalates_to_zombie_climb_prefab() -> None:
    columns = {
        (6, 5): {10},
        (7, 5): {7},
    }
    world = WorkerVoxelWorld()
    world.map_epoch = 3
    world.topology_version = 4
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    brain = BotBrain(world, seed=6)
    zombie = _navigation_player(
        1,
        TEAM1,
        (6.5, 5.5, 7.75),
        class_id=int(C.CLASS_ZOMBIE),
        is_bot=True,
    )
    survivor = _navigation_player(
        2,
        1,
        (7.5, 5.5, 4.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=3,
        map_epoch=3,
        mode_epoch=1,
        topology_version=4,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=now,
        mode_id="zom",
        players=(zombie, survivor),
        mode_phase="ACTIVE",
    )

    first = brain.decide(frame)
    second = brain.decide(
        replace(frame, frame_id=4, created_at=now + 1.0)
    )

    assert first is not None
    assert second is not None
    assert second.debug_role == "zombie_build_climb"
    assert second.action.kind.value == "place_prefab"
    assert second.action.argument == "prefab_zombiehand"


def test_far_zombie_hunt_uses_local_voxel_route_when_global_path_is_empty() -> None:
    columns = {(x, 5): {10} for x in range(5, 36)}
    world = WorkerVoxelWorld()
    world.map_epoch = 4
    world.topology_version = 5
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    world.has_line_of_sight = lambda _origin, _target: False
    world.next_path_direction = lambda *_args, **_kwargs: (0.0, 0.0, 0.0)
    brain = BotBrain(world, seed=7)
    zombie = _navigation_player(
        1,
        TEAM1,
        (5.5, 5.5, 7.75),
        class_id=int(C.CLASS_ZOMBIE),
        is_bot=True,
    )
    survivor = _navigation_player(
        2,
        1,
        (35.5, 5.5, 7.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=5,
        map_epoch=4,
        mode_epoch=1,
        topology_version=5,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=now,
        mode_id="zom",
        players=(zombie, survivor),
        mode_phase="ACTIVE",
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.debug_role == "zombie_hunt_survivor"
    assert intent.movement.direction[0] > 0.9
    assert intent.movement.affordance is MovementAffordance.WALK


def test_ordinary_objective_uses_local_voxel_route_when_global_path_is_empty() -> None:
    columns = {(x, 5): {10} for x in range(5, 36)}
    world = WorkerVoxelWorld()
    world.map_epoch = 4
    world.topology_version = 5
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    world.has_line_of_sight = lambda _origin, _target: False
    world.next_path_direction = lambda *_args, **_kwargs: (0.0, 0.0, 0.0)
    brain = BotBrain(world, seed=8)
    soldier = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_SOLDIER),
            is_bot=True,
        ),
        weapon_tool=int(C.RIFLE_TOOL),
        loadout=(int(C.RIFLE_TOOL), int(C.SPADE_TOOL), int(C.BLOCK_TOOL)),
        ammo_clip=10,
        ammo_reserve=50,
    )
    enemy = _navigation_player(
        2,
        1,
        (35.5, 5.5, 7.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    objective = ObjectiveSnapshot("team_anchor", 1, enemy.position)
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=6,
        map_epoch=4,
        mode_epoch=1,
        topology_version=5,
        observer_id=soldier.player_id,
        observer_generation=soldier.generation,
        created_at=now,
        mode_id="tdm",
        players=(soldier, enemy),
        objectives=(objective,),
    )

    intent = brain.decide(frame)

    assert intent is not None
    assert intent.debug_role == "team_assault_enemy_side"
    assert intent.movement.direction[0] > 0.9


def test_visible_zombie_stall_escalates_to_topology_action() -> None:
    columns = {
        (5, 5): {10},
        (15, 5): {10},
    }
    world = WorkerVoxelWorld()
    world.map_epoch = 5
    world.topology_version = 6
    world._vxl = object()
    world._native_nav = None
    solid = _solid_columns(columns)
    world.solid = lambda x, y, z: solid(x, y, z)
    world.has_line_of_sight = lambda _origin, _target: True
    world.next_path_direction = lambda *_args, **_kwargs: (0.0, 0.0, 0.0)
    brain = BotBrain(world, seed=9)
    zombie = _navigation_player(
        1,
        TEAM1,
        (5.5, 5.5, 7.75),
        class_id=int(C.CLASS_ZOMBIE),
        is_bot=True,
    )
    survivor = _navigation_player(
        2,
        1,
        (15.5, 5.5, 7.75),
        class_id=int(C.CLASS_SOLDIER),
    )
    now = time.monotonic()
    frame = PerceptionFrame(
        frame_id=7,
        map_epoch=5,
        mode_epoch=1,
        topology_version=6,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=now,
        mode_id="zom",
        players=(zombie, survivor),
        mode_phase="ACTIVE",
    )

    first = brain.decide(frame)
    second = brain.decide(replace(frame, frame_id=8, created_at=now + 1.0))

    assert first is not None
    assert first.debug_role == "zombie_contact_charge"
    assert first.movement.direction == (0.0, 0.0, 0.0)
    assert second is not None
    assert second.debug_role == "zombie_build_climb"
    assert second.action.kind.value == "place_prefab"


def test_exhausted_stuck_route_recycles_physical_recovery() -> None:
    world = SimpleNamespace(
        blocking_cell=lambda _position, _direction: None,
        bridge_cell=lambda _position, _direction: None,
    )
    brain = BotBrain(world, seed=8)
    zombie = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_ZOMBIE),
            is_bot=True,
        ),
        blocks=0,
        prefabs=(),
    )
    frame = PerceptionFrame(
        frame_id=6,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=zombie.player_id,
        observer_generation=zombie.generation,
        created_at=2.0,
        mode_id="zom",
        players=(zombie,),
        mode_phase="ACTIVE",
    )
    state = _BrainState(
        last_progress_at=1.0,
        stuck_attempts=2,
        last_path_direction=(1.0, 0.0, 0.0),
        path=[(9.0, 5.0, 7.75)],
        path_goal=(20.0, 5.0, 7.75),
        path_topology_version=1,
    )

    result = brain._stuck_recovery(frame, zombie, state, 2.0)

    assert result is None
    assert state.stuck_attempts == 0
    assert state.path == []
    assert state.path_goal is None
    assert state.path_topology_version == -1


def test_exhausted_route_uses_a_proven_short_dry_retreat() -> None:
    unsafe_step = SimpleNamespace(
        direction=(1.0, 0.0, 0.0),
        waypoint=(6.5, 5.5, 7.75),
        affordance=MovementAffordance.WALK,
    )
    terrain = SimpleNamespace(
        direction_is_traversable=(
            lambda _position, direction, _affordance: direction[0] < -0.9
        ),
        dry_corridor_is_traversable=(
            lambda _position, direction, **_kwargs: direction[0] < -0.9
        ),
    )
    planner = SimpleNamespace(
        terrain=terrain,
        plan_local=lambda *_args, **_kwargs: unsafe_step,
    )
    brain = BotBrain(
        SimpleNamespace(
            action_planner=planner,
            reset_agent_navigation=lambda _player_id: None,
        ),
        seed=8,
    )
    observer = _navigation_player(
        1,
        TEAM1,
        (20.5, 20.5, 7.75),
        class_id=int(C.CLASS_ENGINEER),
        is_bot=True,
    )
    frame = PerceptionFrame(
        frame_id=7,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        stuck_attempts=3,
        last_path_direction=(1.0, 0.0, 0.0),
        path_goal=(40.0, 20.5, 7.75),
    )

    intent = brain._begin_route_escape(
        frame,
        observer,
        state,
        now=2.0,
        failed_direction=(1.0, 0.0, 0.0),
    )

    assert intent is not None
    assert intent.debug_role == "stuck_route_retreat"
    assert intent.movement.direction == (-1.0, -0.0, 0.0)
    assert intent.movement.sprint is False
    assert state.route_escape_goal == (17.5, 20.5, 7.75)
    assert state.stuck_attempts == 0


def test_stationary_route_escape_expires_before_a_jump_loop() -> None:
    brain = BotBrain(SimpleNamespace(), seed=8)
    observer = _navigation_player(
        1,
        TEAM1,
        (5.5, 5.5, 7.75),
        class_id=int(C.CLASS_ZOMBIE),
        is_bot=True,
    )
    frame = PerceptionFrame(
        frame_id=7,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.1,
        mode_id="zom",
        players=(observer,),
        mode_phase="ACTIVE",
    )
    state = _BrainState(
        last_progress_at=1.0,
        stuck_attempts=0,
        last_path_direction=(1.0, 0.0, 0.0),
        last_affordance=MovementAffordance.JUMP,
        route_escape_goal=(12.5, 5.5, 7.75),
        route_escape_until=4.0,
        route_escape_started_at=1.0,
    )

    intent = brain._active_route_escape(
        frame, observer, state, now=2.1
    )

    assert intent is None
    assert state.route_escape_goal is None
    assert state.route_escape_started_at == 0.0
    assert state.stuck_attempts == 3
    assert state.next_stuck_recovery_at == 0.0


def test_exhausted_route_prefers_voxel_replan_and_resets_crowd() -> None:
    resets: list[int] = []
    step = SimpleNamespace(
        direction=(0.0, 1.0, 0.0),
        waypoint=(5.5, 6.5, 7.75),
        affordance=MovementAffordance.WALK,
    )
    world = SimpleNamespace(
        overhead_block=lambda _position: None,
        hole_escape=lambda _position, _direction: None,
        blocking_cell=lambda _position, _direction: None,
        bridge_cell=lambda _position, _direction: None,
        action_planner=SimpleNamespace(
            plan_local=lambda *_args, **_kwargs: step
        ),
        reset_agent_navigation=resets.append,
    )
    brain = BotBrain(world, seed=8)
    observer = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_ENGINEER),
            is_bot=True,
        ),
        loadout=(int(C.SPADE_TOOL), int(C.BLOCK_TOOL)),
    )
    frame = PerceptionFrame(
        frame_id=7,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        stuck_attempts=2,
        last_path_direction=(1.0, 0.0, 0.0),
        path_goal=(20.0, 5.0, 7.75),
    )

    intent = brain._stuck_recovery(frame, observer, state, 2.0)

    assert intent is not None
    assert intent.debug_role == "stuck_voxel_replan"
    assert intent.movement.direction == step.direction
    assert state.route_escape_goal == (20.0, 5.0, 7.75)
    assert state.path_goal is None
    assert state.stuck_attempts == 0
    assert resets == [observer.player_id]


def test_repeated_route_failure_uses_adjacent_emergency_drop() -> None:
    world = SimpleNamespace(
        overhead_block=lambda _position: None,
        hole_escape=lambda _position, _direction: None,
        blocking_cell=lambda _position, _direction: None,
        bridge_cell=lambda _position, _direction: None,
        emergency_drop=lambda _position: (
            (0.0, 1.0, 0.0),
            (5.5, 6.5, 27.75),
        ),
        reset_agent_navigation=lambda _player_id: None,
    )
    brain = BotBrain(world, seed=8)
    observer = replace(
        _navigation_player(
            1,
            TEAM1,
            (5.5, 5.5, 7.75),
            class_id=int(C.CLASS_MEDIC),
            is_bot=True,
        ),
        loadout=(int(C.SPADE_TOOL),),
    )
    frame = PerceptionFrame(
        frame_id=8,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=2.0,
        mode_id="tdm",
        players=(observer,),
    )
    state = _BrainState(
        last_progress_at=1.0,
        stuck_attempts=2,
        route_escape_failures=2,
        last_path_direction=(1.0, 0.0, 0.0),
    )

    intent = brain._stuck_recovery(frame, observer, state, 2.0)

    assert intent is not None
    assert intent.debug_role == "stuck_emergency_drop"
    assert intent.movement.affordance is MovementAffordance.DROP
    assert intent.priority is BotIntentPriority.SURVIVAL


def test_local_planner_rejects_a_jump_with_blocked_head_arc() -> None:
    columns = {
        (6, 5): {6, 10},
        (7, 5): {8},
    }
    planner = VoxelActionPlanner(
        VoxelTerrain(_solid_columns(columns))
    )

    step = planner.plan_local(
        (6.5, 5.5, 7.75),
        (7.5, 5.5, 5.75),
        abilities=frozenset({MovementAffordance.JUMP}),
        topology_version=5,
    )

    assert step is None or step.affordance is not MovementAffordance.JUMP
