"""Regression tests for semantic bot traversal of the live voxel world."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
import time
from types import SimpleNamespace

import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotIntent,
    MovementAffordance,
    MovementIntent,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.voxel_navigation import VoxelActionPlanner, VoxelTerrain
from server.bot_ai.worker import BotBrain, WorkerVoxelWorld, _BrainState
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


def _solid_columns(
    columns: dict[tuple[int, int], set[int]],
) -> Callable[[int, int, int], bool]:
    """Return a deterministic solid query for small navigation fixtures."""

    return lambda x, y, z: int(z) in columns.get((int(x), int(y)), set())


def test_open_waterbed_is_not_an_ordinary_standing_node() -> None:
    terrain = VoxelTerrain(_solid_columns({(10, 10): {239}}))

    assert terrain.standing_node(10, 10, 236.75) is None


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


def test_recast_tile_omits_the_universal_waterbed_surface() -> None:
    world = WorkerVoxelWorld()
    navigator = _RecordingNavigator()
    world._vxl = object()
    world._native_nav = navigator
    world.solid = lambda x, y, z: (int(x), int(y), int(z)) == (0, 0, 239)

    world._rebuild_native_tile(0, 0)

    assert navigator.built_vertices is None
    assert navigator.removed is True


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
    assert step.affordance is MovementAffordance.JUMP


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
        blocks=100,
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
    assert intent.movement.affordance is MovementAffordance.JUMP


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


def test_exhausted_stuck_route_resets_for_a_fresh_replan() -> None:
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
