"""Terrain regressions for the production lightweight bot navigator."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import math
import time
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.simple_navigation import RoutePlan, RouteStep, SimpleVoxelWorld
from server.bot_ai.simple_worker import (
    SimpleBotBrain,
    _BotState,
    _Goal,
    _TraversalStyle,
    _dig_profile,
    _movement_abilities,
    _route_step_reached,
    _same_traversal_step,
)
from server.config import ServerConfig
from server.dig_profiles import melee_dig_positions, navigation_dig_profile
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


class _FixtureVxl:
    def __init__(self, solids: set[tuple[int, int, int]]) -> None:
        self.solids = set(solids)

    def get_solid(self, x: int, y: int, z: int) -> bool:
        return (int(x), int(y), int(z)) in self.solids


class _Columns:
    def __init__(self, value):
        self.value = value

    def __getitem__(self, _index):
        return self.value


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


def test_blocked_two_block_jump_can_lower_ledge_with_spade() -> None:
    """A failed native jump must expose a distinct excavation fallback."""

    solids = {(x, 10, 20) for x in range(10, 13)}
    solids.update({(11, 10, 18), (11, 10, 19)})
    world = _world(solids)
    profile = navigation_dig_profile(int(C.SPADE_TOOL))
    assert profile is not None

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (12.5, 10.5, 17.75),
        abilities=frozenset(
            {MovementAffordance.JUMP, MovementAffordance.BREACH}
        ),
        dig_profile=profile,
        blocked_edges=frozenset(
            {((10, 10, 20), (11, 10, 18))}
        ),
    )

    assert plan.steps
    assert plan.steps[0].affordance is MovementAffordance.BREACH
    assert plan.steps[0].breach is not None
    assert plan.steps[0].breach.destination == (11, 10, 20)
    assert plan.steps[0].breach.target_cell == (11, 10, 18)


def test_planner_recovers_native_body_center_overlapping_a_wall_face() -> None:
    """A fractional centre in a solid cell must still expose its dig edge."""

    solids = {(x, 10, 20) for x in range(2, 6)}
    # A full-height wall makes (4, 10) invalid as a standing column around the
    # player's z, matching the MayanJungle x=364 tunnel boundary.
    solids.update((4, 10, z) for z in range(0, 21))
    world = _world(solids)
    profile = navigation_dig_profile(int(C.SPADE_TOOL))

    plan = world.plan(
        (4.68, 10.5, 17.75),
        (2.5, 10.5, 17.75),
        abilities=frozenset(
            {MovementAffordance.JUMP, MovementAffordance.BREACH}
        ),
        dig_profile=profile,
    )

    assert profile is not None
    assert plan.steps
    assert plan.steps[0].affordance is MovementAffordance.BREACH
    assert plan.steps[0].breach is not None
    assert plan.steps[0].breach.source == (5, 10, 20)
    assert plan.steps[0].breach.destination == (4, 10, 20)


def test_planner_recovers_diagonal_support_above_a_lower_floor() -> None:
    solids = {
        (10, 10, 23),
        (11, 11, 20),
        (12, 11, 20),
        (13, 11, 20),
    }
    world = _world(solids)

    plan = world.plan(
        (10.9, 10.9, 17.75),
        (13.5, 11.5, 17.75),
        abilities=frozenset({MovementAffordance.JUMP}),
    )

    assert plan.steps
    assert plan.reached_segment_goal is True
    assert plan.steps[-1].waypoint == (13.5, 11.5, 17.75)
    assert all(step.waypoint[2] == 17.75 for step in plan.steps)


def test_compacted_route_biases_away_from_parallel_body_height_wall() -> None:
    solids = {(5, y, 20) for y in range(5, 11)}
    solids.update((4, y, z) for y in range(5, 11) for z in range(0, 21))
    world = _world(solids)

    plan = world.plan(
        (5.46, 10.5, 17.75),
        (5.5, 5.5, 17.75),
        abilities=frozenset({MovementAffordance.JUMP}),
    )

    assert plan.steps
    assert plan.steps[0].waypoint[0] > 5.6
    assert plan.steps[0].waypoint[1] < 10.0


def test_route_compaction_preserves_each_vertical_stair_transition() -> None:
    steps = tuple(
        RouteStep(
            (float(x) + 0.5, 10.5, float(28 - x) + 0.75),
            MovementAffordance.WALK,
        )
        for x in range(11, 14)
    )

    compacted = SimpleVoxelWorld._compact_straight_steps(
        steps,
        (10.5, 10.5, 17.75),
    )

    assert compacted == steps


def test_one_block_drop_under_source_height_overhang_requires_crouch() -> None:
    solids = {(10, 10, 20), (11, 10, 21), (11, 10, 17)}
    world = _world(solids)

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (11.5, 10.5, 18.75),
        abilities=frozenset(
            {MovementAffordance.JUMP, MovementAffordance.CROUCH}
        ),
    )

    assert plan.reached_segment_goal is True
    assert plan.steps[0].affordance is MovementAffordance.CROUCH


def test_level_two_cell_high_passage_requires_crouch() -> None:
    solids = {
        (10, 10, 20),
        (11, 10, 20),
        (11, 10, 17),
    }
    world = _world(solids)

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (11.5, 10.5, 17.75),
        abilities=frozenset(
            {MovementAffordance.JUMP, MovementAffordance.CROUCH}
        ),
    )

    assert plan.reached_segment_goal is True
    assert plan.steps[0].affordance is MovementAffordance.CROUCH


def test_production_bot_breaches_level_two_cell_high_passage() -> None:
    solids = {
        (10, 10, 20),
        (11, 10, 20),
        (11, 10, 17),
    }
    world = _world(solids)
    observer = SimpleNamespace(loadout=(int(C.SPADE_TOOL),))

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (11.5, 10.5, 17.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )

    assert plan.reached_segment_goal is True
    assert plan.steps[0].affordance is MovementAffordance.BREACH
    assert plan.steps[0].breach is not None
    # The recovered vertical-spade handler aims one cell below the overhang;
    # its authoritative dig column includes the blocking z=17 voxel.
    assert plan.steps[0].breach.target_cell == (11, 10, 18)
    assert plan.steps[0].breach.blocking_cells == ((11, 10, 17),)


def test_topology_replan_retains_only_the_same_edge_progress_clock() -> None:
    jump = RouteStep(
        (11.5, 10.5, 15.75),
        MovementAffordance.JUMP,
    )

    assert _same_traversal_step(
        jump,
        RouteStep((11.5, 10.5, 15.75), MovementAffordance.JUMP),
    )
    assert not _same_traversal_step(
        jump,
        RouteStep((11.5, 10.5, 15.75), MovementAffordance.WALK),
    )
    assert not _same_traversal_step(
        jump,
        RouteStep((12.5, 10.5, 15.75), MovementAffordance.JUMP),
    )


def test_jump_waypoint_requires_vertical_landing_and_dry_shore() -> None:
    jump = RouteStep(
        (11.5, 10.5, 15.75),
        MovementAffordance.JUMP,
    )

    assert not _route_step_reached(
        jump,
        (11.45, 10.5, 17.75),
        wading=False,
    )
    assert _route_step_reached(
        jump,
        (11.45, 10.5, 15.8),
        wading=False,
    )
    assert not _route_step_reached(
        jump,
        (11.45, 10.5, 15.8),
        wading=True,
    )


def test_topology_replan_skips_reached_prefix_before_progress_comparison() -> None:
    observer = PlayerSnapshot(
        player_id=1,
        generation=1,
        team=TEAM1,
        class_id=int(C.CLASS_SOLDIER),
        alive=True,
        spawned=True,
        position=(10.5, 10.5, 17.75),
        eye=(10.5, 10.5, 16.75),
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
    actionable = RouteStep(
        (11.5, 10.5, 17.75),
        MovementAffordance.WALK,
    )
    world = SimpleNamespace(
        plan=lambda *_args, **_kwargs: RoutePlan(
            (
                RouteStep(
                    observer.position,
                    MovementAffordance.WALK,
                ),
                actionable,
            ),
            True,
            2,
        )
    )
    brain = SimpleBotBrain(world)
    goal = _Goal(
        ("topology-prefix",),
        (20.5, 10.5, 17.75),
        "topology_prefix",
        0.5,
        True,
    )
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    state.goal = goal
    state.route = (actionable,)
    state.route_index = 0
    state.route_topology_version = 1
    state.waypoint_best_distance = 1.0
    state.waypoint_progress_at = 90.0
    state.goal_best_distance = 10.0
    state.goal_progress_at = 90.0
    frame = PerceptionFrame(
        frame_id=2,
        map_epoch=1,
        mode_epoch=1,
        topology_version=2,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=91.0,
        mode_id="tdm",
        players=(observer,),
    )

    intent = brain._navigation_intent(
        frame,
        observer,
        state,
        goal,
        frame.created_at,
    )

    assert intent.debug_role == "topology_prefix"
    assert state.route_index == 1
    assert state.waypoint_progress_at == 90.0


def test_goal_switch_retains_bounded_directed_blocked_edges() -> None:
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    edge = ((10, 10, 20), (12, 10, 20))
    state.blocked_edges[edge] = 16.0
    state.goal = _Goal(
        ("contact", 1),
        (20.5, 10.5, 17.75),
        "combat_pursuit",
        1.0,
        True,
    )

    SimpleBotBrain._set_goal(
        state,
        _Goal(
            ("contact", 2),
            (22.5, 10.5, 17.75),
            "chase_last_seen",
            1.0,
            True,
        ),
        (10.5, 10.5, 17.75),
        now=11.0,
    )

    assert state.blocked_edges == {edge: 16.0}


def test_water_route_moves_monotonically_to_a_climbable_shore() -> None:
    # Ignore the near two-block bank and traverse the water to a bank that the
    # native body can reliably mount after swimming.
    solids = {(x, 10, 239) for x in range(10, 15)}
    solids.add((9, 10, 237))
    solids.add((15, 10, 238))
    world = _world(solids)
    routes = {
        10: SimpleNamespace(
            next_x=11,
            next_y=10,
            goal_x=15,
            goal_y=10,
            goal_support_z=238,
            distance=5,
            climbable=True,
        ),
        11: SimpleNamespace(
            next_x=12,
            next_y=10,
            goal_x=15,
            goal_y=10,
            goal_support_z=238,
            distance=4,
            climbable=True,
        ),
        12: SimpleNamespace(
            next_x=13,
            next_y=10,
            goal_x=15,
            goal_y=10,
            goal_support_z=238,
            distance=3,
            climbable=True,
        ),
        13: SimpleNamespace(
            next_x=14,
            next_y=10,
            goal_x=15,
            goal_y=10,
            goal_support_z=238,
            distance=2,
            climbable=True,
        ),
        14: SimpleNamespace(
            next_x=15,
            next_y=10,
            goal_x=15,
            goal_y=10,
            goal_support_z=238,
            distance=1,
            climbable=True,
        ),
    }
    world._atlas = SimpleNamespace(
        water_route=lambda x, _y: routes.get(int(x)),
    )
    world._dirty_columns.update((x, 10) for x in range(9, 16))

    positions = []
    position = (10.5, 10.5, 236.75)
    for _ in range(6):
        step = world.water_step(position)
        assert step is not None
        positions.append(step.waypoint)
        position = step.waypoint
        if position[0] == 15.5:
            break

    assert all(
        right[0] > left[0]
        for left, right in zip(positions, positions[1:])
    )
    assert positions[0] == (14.5, 10.5, 236.75)
    assert positions[-1] == (15.5, 10.5, 235.75)


def test_water_route_avoids_a_recently_failed_shore_edge() -> None:
    world = _world({
        (10, 10, 239),
        (10, 11, 239),
        (9, 10, 238),
        (10, 12, 238),
    })

    step = world.water_step(
        (10.5, 10.5, 236.75),
        blocked_edges=frozenset({
            ((10, 10, 239), (9, 10, 238)),
        }),
    )

    assert step is not None
    assert step.waypoint == (10.5, 11.5, 236.75)
    assert step.affordance is MovementAffordance.WALK


def test_goal_directed_water_recovery_keeps_crossing_opposite_bank() -> None:
    world = _world({
        (9, 10, 238),
        (10, 10, 239),
        (11, 10, 239),
        (12, 10, 239),
        (13, 10, 239),
        (14, 10, 239),
        (15, 10, 238),
    })
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            next_x=9,
            next_y=10,
            goal_x=9,
            goal_y=10,
            goal_support_z=238,
            distance=1,
            climbable=True,
        ),
    )
    world._dirty_columns.update((x, 10) for x in range(9, 16))

    step = world.water_step(
        (10.5, 10.5, 236.75),
        preferred_goal=(20.5, 10.5, 235.75),
    )

    assert step is not None
    assert step.affordance is MovementAffordance.SWIM
    assert step.waypoint == (14.5, 10.5, 236.75)


def test_blocked_goal_facing_bank_falls_back_to_another_live_shore() -> None:
    solids = {
        (10, 10, 239),
        (11, 10, 239),
        (12, 10, 238),
    }
    solids.update((9, 10, z) for z in range(235, 240))
    world = _world(solids)
    blocked = ((10, 10, 239), (9, 10, 235))

    step = world.water_step(
        (10.5, 10.5, 236.75),
        preferred_goal=(0.5, 10.5, 232.75),
        blocked_edges=frozenset({blocked}),
    )

    assert step is not None
    assert step.waypoint == (11.5, 10.5, 236.75)
    assert step.affordance is MovementAffordance.WALK


def test_blocked_edge_later_in_atlas_lookahead_uses_live_water_search() -> None:
    world = _world({
        (9, 10, 238),
        (10, 10, 239),
        (11, 10, 239),
        (12, 10, 239),
        (13, 10, 238),
    })
    routes = {
        (11, 10): SimpleNamespace(
            next_x=10,
            next_y=10,
            goal_x=9,
            goal_y=10,
            goal_support_z=238,
            distance=2,
            climbable=True,
        ),
        (10, 10): SimpleNamespace(
            next_x=9,
            next_y=10,
            goal_x=9,
            goal_y=10,
            goal_support_z=238,
            distance=1,
            climbable=True,
        ),
    }
    world._atlas = SimpleNamespace(
        water_route=lambda x, y: routes.get((int(x), int(y))),
    )
    world._dirty_columns.update((x, 10) for x in range(9, 14))

    step = world.water_step(
        (11.5, 10.5, 236.75),
        blocked_edges=frozenset({
            ((10, 10, 239), (9, 10, 238)),
        }),
    )

    assert step is not None
    assert step.waypoint == (12.5, 10.5, 236.75)
    assert step.affordance is MovementAffordance.WALK


def test_water_surface_wins_over_an_overhead_platform_support() -> None:
    world = _world({(10, 10, 233)})

    surface = world.surface(
        10,
        10,
        236.75,
        vertical_span=8,
        allow_water=True,
    )

    assert surface is not None
    assert surface.support_z == 239


def test_failed_two_cell_gap_jump_records_the_concrete_landing_edge() -> None:
    world = _world({(10, 10, 20), (12, 10, 20)})
    brain = SimpleBotBrain(world)
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    state.route = (
        RouteStep((12.5, 10.5, 17.75), MovementAffordance.JUMP),
    )

    brain._invalidate_current_edge(
        state,
        (10.5, 10.5, 17.75),
        now=10.0,
    )

    edge = ((10, 10, 20), (12, 10, 20))
    assert edge in state.blocked_edges
    assert state.blocked_edges[edge] == pytest.approx(22.0)


def test_failed_water_jump_records_the_concrete_shore_edge() -> None:
    world = _world({(10, 10, 239), (9, 10, 238)})
    brain = SimpleBotBrain(world)
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    state.route = (
        RouteStep((9.5, 10.5, 235.75), MovementAffordance.JUMP),
    )

    brain._invalidate_current_edge(
        state,
        (10.5, 10.5, 236.75),
        now=10.0,
    )

    edge = ((10, 10, 239), (9, 10, 238))
    assert edge in state.blocked_edges
    assert state.blocked_edges[edge] == pytest.approx(30.0)


def test_failed_swim_records_the_adjacent_water_edge() -> None:
    world = _world({
        (10, 10, 239),
        (11, 10, 239),
        (12, 10, 239),
    })
    brain = SimpleBotBrain(world)
    state = _BotState(map_epoch=1, mode_epoch=1, life_id=0)
    state.route = (
        RouteStep((12.5, 10.5, 236.75), MovementAffordance.SWIM),
    )

    brain._invalidate_current_edge(
        state,
        (10.5, 10.5, 236.75),
        now=10.0,
    )

    edge = ((10, 10, 239), (11, 10, 239))
    assert edge in state.blocked_edges
    assert state.blocked_edges[edge] == pytest.approx(30.0)


def test_strategic_planner_can_enter_water_and_reach_the_opposite_shore() -> None:
    solids = {(9, 10, 238), (15, 10, 238)}
    solids.update((x, 10, 239) for x in range(10, 15))
    world = _world(solids)

    dry_only = world.plan(
        (9.5, 10.5, 235.75),
        (15.5, 10.5, 235.75),
        abilities=frozenset({MovementAffordance.JUMP}),
    )
    amphibious = world.plan(
        (9.5, 10.5, 235.75),
        (15.5, 10.5, 235.75),
        abilities=frozenset({MovementAffordance.JUMP}),
        allow_water=True,
    )

    assert dry_only.reached_segment_goal is False
    assert amphibious.reached_segment_goal is True
    assert any(
        step.affordance is MovementAffordance.SWIM
        for step in amphibious.steps
    )
    assert amphibious.steps[-1].affordance is MovementAffordance.JUMP
    assert amphibious.steps[-1].waypoint == (15.5, 10.5, 235.75)


def test_zombie_hand_can_carve_a_waterline_exit_through_a_high_bank() -> None:
    solids = {(10, 10, 239)}
    solids.update((11, 10, z) for z in range(230, 240))
    world = _world(solids)
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=11,
            goal_y=10,
            goal_support_z=230,
        )
    )
    world._dirty_columns.update({(10, 10), (11, 10)})
    profile = _dig_profile(
        SimpleNamespace(loadout=(int(C.ZOMBIEHAND_TOOL),))
    )

    step = world.water_bank_breach((10.5, 10.5, 236.75), profile)

    assert step is not None
    assert step.affordance is MovementAffordance.BREACH
    assert step.breach is not None
    assert step.breach.blocking_cells == (
        (11, 10, 237),
        (11, 10, 238),
    )
    assert step.breach.target_cell == (11, 10, 237)
    assert (11, 10, 239) not in melee_dig_positions(
        step.breach.target_cell,
        profile.pattern,
    )


def test_adjacent_high_bank_skips_redundant_water_component_search(
    monkeypatch,
) -> None:
    """Atlas-proven assisted exits must not rescan an entire sea per tick."""

    solids = {(10, 10, 239)}
    solids.update((11, 10, z) for z in range(230, 240))
    world = _world(solids)

    world._atlas = SimpleNamespace(
        width=512,
        primary_support=_Columns(239),
        flags=_Columns(0),
        layer_count=_Columns(1),
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=11,
            goal_y=10,
            goal_support_z=230,
        )
    )

    def unexpected_search(_self, _start, **_kwargs):
        raise AssertionError("high-bank recovery repeated the water BFS")

    monkeypatch.setattr(
        SimpleVoxelWorld,
        "_bounded_water_search",
        unexpected_search,
    )

    assert world.water_step((10.5, 10.5, 236.75)) is None


def test_cached_high_bank_is_live_validated_as_climbable_after_excavation() -> None:
    world = _world({(10, 10, 239), (11, 10, 238)})
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=11,
            goal_y=10,
            goal_support_z=230,
        ),
    )

    step = world.water_step((10.5, 10.5, 236.75))

    assert step is not None
    assert step.waypoint == (11.5, 10.5, 235.75)
    assert step.affordance is MovementAffordance.JUMP


def test_destroyed_cached_bank_searches_live_water_for_another_shore() -> None:
    solids = {(x, 10, 239) for x in range(10, 14)}
    solids.add((14, 10, 238))
    world = _world(solids)
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=9,
            goal_y=10,
            goal_support_z=230,
        ),
    )

    step = world.water_step((10.5, 10.5, 236.75))

    assert step is not None
    assert step.waypoint == (11.5, 10.5, 236.75)
    assert step.affordance is MovementAffordance.WALK


def test_live_high_bank_hands_off_to_assisted_water_recovery() -> None:
    solids = {(x, 10, 239) for x in range(10, 14)}
    solids.update((14, 10, z) for z in range(235, 240))
    world = _world(solids)
    world._atlas = SimpleNamespace(water_route=lambda _x, _y: None)

    assert world.water_step((13.5, 10.5, 236.75)) is None
    assisted = world.assisted_water_step((13.5, 10.5, 236.75))

    assert assisted is not None
    assert assisted.waypoint == (14.5, 10.5, 232.75)
    assert assisted.affordance is MovementAffordance.BUILD_STEP


def test_tall_live_bank_beyond_twelve_cells_keeps_atlas_crossing_straight() -> None:
    solids = {(x, 10, 239) for x in range(10, 13)}
    solids.update((13, 10, z) for z in range(226, 240))
    world = _world(solids)
    routes = {
        10: SimpleNamespace(
            distance=3,
            next_x=11,
            next_y=10,
            goal_x=13,
            goal_y=10,
            goal_support_z=226,
            climbable=False,
        ),
        11: SimpleNamespace(
            distance=2,
            next_x=12,
            next_y=10,
            goal_x=13,
            goal_y=10,
            goal_support_z=226,
            climbable=False,
        ),
        12: SimpleNamespace(
            distance=1,
            next_x=13,
            next_y=10,
            goal_x=13,
            goal_y=10,
            goal_support_z=226,
            climbable=False,
        ),
    }
    world._atlas = SimpleNamespace(
        water_route=lambda x, _y: routes.get(int(x)),
    )

    step = world.water_step((10.5, 10.5, 236.75))

    assert step is not None
    assert step.waypoint == (12.5, 10.5, 236.75)
    assert step.affordance is MovementAffordance.WALK


def test_dirty_assisted_bank_accepts_live_height_after_excavation() -> None:
    solids = {(10, 10, 239)}
    solids.update((11, 10, z) for z in range(235, 240))
    world = _world(solids)
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=11,
            goal_y=10,
            goal_support_z=234,
        ),
    )
    world._dirty_columns.add((11, 10))

    step = world.assisted_water_step((10.5, 10.5, 236.75))

    assert step is not None
    assert step.waypoint == (11.5, 10.5, 232.75)
    assert step.affordance is MovementAffordance.BUILD_STEP


def test_dirty_water_columns_live_validate_cached_route_without_full_bfs(
    monkeypatch,
) -> None:
    world = _world({(10, 10, 239), (11, 10, 239), (12, 10, 238)})
    world._atlas = SimpleNamespace(
        water_route=lambda _x, _y: SimpleNamespace(
            distance=2,
            climbable=True,
            next_x=11,
            next_y=10,
            goal_x=12,
            goal_y=10,
            goal_support_z=238,
        )
    )
    world._dirty_columns.update({(10, 10), (11, 10), (12, 10)})

    def unexpected_search(_self, _start):
        raise AssertionError("dirty atlas route repeated the water BFS")

    monkeypatch.setattr(
        SimpleVoxelWorld,
        "_bounded_water_search",
        unexpected_search,
    )

    step = world.water_step((10.5, 10.5, 236.75))

    assert step is not None
    assert step.waypoint == (11.5, 10.5, 236.75)


def test_production_zombie_brain_digs_an_unclimbable_water_bank() -> None:
    solids = {(10, 10, 239)}
    solids.update((11, 10, z) for z in range(230, 240))
    world = _world(solids)
    world._atlas = SimpleNamespace(
        width=512,
        primary_support=_Columns(239),
        flags=_Columns(0),
        layer_count=_Columns(1),
        water_route=lambda _x, _y: SimpleNamespace(
            distance=1,
            climbable=False,
            goal_x=11,
            goal_y=10,
            goal_support_z=230,
        )
    )
    world._dirty_columns.add((11, 10))
    observer = PlayerSnapshot(
        player_id=1,
        generation=1,
        team=TEAM1,
        class_id=int(C.CLASS_ZOMBIE),
        alive=True,
        spawned=True,
        position=(10.5, 10.5, 236.75),
        eye=(10.5, 10.5, 235.75),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=int(C.ZOMBIEHAND_TOOL),
        blocks=0,
        ammo_clip=0,
        ammo_reserve=0,
        is_bot=True,
        grounded=False,
        wade=True,
        weapon_tool=int(C.ZOMBIEHAND_TOOL),
        loadout=(int(C.ZOMBIEHAND_TOOL),),
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=1,
        observer_generation=1,
        created_at=time.monotonic(),
        mode_id="zombie",
        players=(observer,),
    )

    intent = SimpleBotBrain(world).decide(frame)

    assert intent is not None
    assert intent.movement.affordance is MovementAffordance.BREACH
    assert intent.movement.jump is False
    assert intent.action.kind is BotActionKind.MELEE
    assert intent.action.tool_id == int(C.ZOMBIEHAND_TOOL)
    assert intent.action.position == (11.5, 10.5, 237.5)


def test_swimmer_can_build_a_supported_step_on_the_waterbed() -> None:
    world = _world({(10, 10, 239)})

    assert world.jump_build_cell((10.5, 10.5, 235.75)) == (10, 10, 238)


def test_swimmer_waits_until_capsule_clears_water_step_before_building() -> None:
    world = _world({(10, 10, 239)})

    assert world.jump_build_cell((10.5, 10.5, 236.75)) is None


def test_bridge_builder_extends_a_dry_shore_over_water() -> None:
    solids = {(10, 10, 238)}
    solids.update((x, 10, 239) for x in range(11, 18))
    world = _world(solids)

    line = world.water_bridge_line(
        (10.5, 10.5, 235.75),
        (1.0, 0.0, 0.0),
        max_cells=6,
    )

    assert line == ((11, 10, 238), (16, 10, 238))


def test_bridge_widener_follows_the_native_body_overhang() -> None:
    solids = {(5, y, 10) for y in range(2, 6)}
    solids.add((4, 5, 10))
    world = _world(solids)

    line = world.narrow_bridge_shoulder_line(
        (5.2, 5.5, 7.75),
        (0.0, -1.0, 0.0),
        max_cells=3,
    )

    assert line == ((4, 4, 10), (4, 2, 10))


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

    approach = world.plan(
        (10.5, 10.5, 17.75),
        (17.5, 10.5, 17.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )
    assert approach.reached_segment_goal is False
    assert approach.steps[-1].waypoint == (12.5, 10.5, 17.75)
    assert all(step.breach is None for step in approach.steps)

    plan = world.plan(
        approach.steps[-1].waypoint,
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


def test_route_skips_malformed_edge_cost_without_losing_valid_neighbors(
    monkeypatch,
) -> None:
    world = _world({(x, 10, 20) for x in range(10, 13)})
    original_neighbors = SimpleVoxelWorld._neighbors

    def malformed_then_valid(self, node, **kwargs):
        yield ((11, 10, 20), MovementAffordance.WALK, (1.0, 2.0), None)
        yield from original_neighbors(self, node, **kwargs)

    monkeypatch.setattr(SimpleVoxelWorld, "_neighbors", malformed_then_valid)

    plan = world.plan(
        (10.5, 10.5, 17.75),
        (12.5, 10.5, 17.75),
        abilities=frozenset({MovementAffordance.WALK}),
    )

    assert plan.reached_segment_goal is True
    assert plan.steps[-1].waypoint == (12.5, 10.5, 17.75)


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


def test_route_carves_upward_instead_of_arriving_under_high_ground() -> None:
    """The target walkable height participates in both A* and arrival."""

    solids = {
        (x, y, 20)
        for x in range(10, 13)
        for y in range(8, 13)
    }
    solids.update(
        (x, y, z)
        for x in range(13, 21)
        for y in range(8, 13)
        for z in range(17, 21)
    )
    world = _world(solids)
    observer = SimpleNamespace(loadout=(int(C.SPADE_TOOL),))

    approach = world.plan(
        (10.5, 10.5, 17.75),
        (18.5, 10.5, 14.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )
    assert approach.reached_segment_goal is False
    assert approach.steps[-1].waypoint == (12.5, 10.5, 17.75)

    plan = world.plan(
        approach.steps[-1].waypoint,
        (18.5, 10.5, 14.75),
        abilities=_movement_abilities(observer),
        dig_profile=_dig_profile(observer),
    )

    assert plan.reached_segment_goal is True
    breaches = [step.breach for step in plan.steps if step.breach is not None]
    assert breaches
    assert breaches[0].destination == (13, 10, 19)
    assert breaches[0].target_cell == (13, 10, 17)
    assert plan.steps[-1].waypoint == (18.5, 10.5, 14.75)


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


def test_follower_backs_away_from_teammates_active_breach() -> None:
    world = _world(_sealed_wall_solids())
    observer = PlayerSnapshot(
        player_id=3,
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
    created_at = time.monotonic()
    target = (13.5, 10.5, 18.5)
    digger = replace(
        observer,
        player_id=1,
        position=(12.6, 10.5, 17.75),
        last_action_kind=BotActionKind.MELEE.value,
        last_action_accepted=True,
        last_action_position=target,
        last_action_at=created_at - 0.1,
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=created_at,
        mode_id="tdm",
        players=(observer, digger),
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
        created_at,
    )

    assert intent.debug_role == "wall_test:breach_assist_queue"
    assert intent.movement.direction[0] < -0.9
    assert intent.debug_path[-1] == target


def test_overlapping_friendly_bots_separate_without_reversing_route() -> None:
    observer = PlayerSnapshot(
        player_id=3,
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
    teammate = replace(observer, player_id=1)
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=time.monotonic(),
        mode_id="tdm",
        players=(observer, teammate),
    )

    movement = SimpleBotBrain._crowd_adjusted_movement(
        frame,
        MovementIntent(direction=(1.0, 0.0, 0.0)),
        action=BotAction(),
    )

    assert movement.direction[0] >= 0.35
    assert movement.direction[1] < -0.9
    assert abs(
        movement.direction[0] ** 2 + movement.direction[1] ** 2 - 1.0
    ) < 1e-6


def test_follower_queues_while_shared_breach_digger_is_active() -> None:
    world = _world(_sealed_wall_solids())
    observer = PlayerSnapshot(
        player_id=3,
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
    created_at = time.monotonic()
    teammate = replace(
        observer,
        player_id=1,
        last_action_kind=BotActionKind.MELEE.value,
        last_action_accepted=True,
        last_action_position=(13.5, 10.5, 18.5),
        last_action_at=created_at,
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=created_at,
        mode_id="tdm",
        players=(observer, teammate),
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

    assert intent.debug_role == "wall_test:breach_assist_queue"
    assert intent.movement.affordance is MovementAffordance.WALK
    assert intent.movement.direction[0] < -0.9
    assert intent.action.kind is BotActionKind.NONE
    assert state.breach_key is None

    detour_frame = replace(
        frame,
        frame_id=2,
        created_at=frame.created_at + 1.5,
        players=(
            observer,
            replace(teammate, last_action_at=frame.created_at + 1.5),
        ),
    )
    detour = SimpleBotBrain(world)._navigation_intent(
        detour_frame,
        observer,
        state,
        goal,
        detour_frame.created_at,
    )

    assert detour.debug_role == "wall_test:breach_assist_queue"
    assert detour.movement.affordance is MovementAffordance.WALK
    assert not state.blocked_edges

    owner_frame = replace(
        frame,
        observer_id=teammate.player_id,
        players=(teammate, observer),
    )
    owner_intent = SimpleBotBrain(world)._navigation_intent(
        owner_frame,
        teammate,
        _BotState(map_epoch=1, mode_epoch=1, life_id=0),
        goal,
        owner_frame.created_at,
    )

    assert owner_intent.debug_role == "wall_test:route_breach"
    assert owner_intent.action.kind is BotActionKind.MELEE


def test_passive_nearby_teammate_does_not_steal_breach_reservation() -> None:
    observer = PlayerSnapshot(
        player_id=3,
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
        tool=int(C.SPADE_TOOL),
        blocks=0,
        ammo_clip=0,
        ammo_reserve=0,
        is_bot=True,
        weapon_tool=int(C.SMG_TOOL),
        loadout=(int(C.SMG_TOOL), int(C.SPADE_TOOL)),
    )
    passive = replace(observer, player_id=1)
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=time.monotonic(),
        mode_id="tdm",
        players=(passive, observer),
    )

    queue = SimpleBotBrain._breach_queue(
        frame,
        observer,
        (13.5, 10.5, 18.5),
    )

    assert tuple(player.player_id for player in queue) == (observer.player_id,)


def test_recent_rejected_digger_still_shares_breach_reservation() -> None:
    observer = PlayerSnapshot(
        player_id=3,
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
        tool=int(C.SPADE_TOOL),
        blocks=0,
        ammo_clip=0,
        ammo_reserve=0,
        is_bot=True,
        weapon_tool=int(C.SMG_TOOL),
        loadout=(int(C.SMG_TOOL), int(C.SPADE_TOOL)),
    )
    created_at = time.monotonic()
    active = replace(
        observer,
        player_id=1,
        last_action_kind=BotActionKind.MELEE.value,
        last_action_accepted=False,
        last_action_position=(11.5, 10.5, 18.5),
        last_action_at=created_at - 0.1,
    )
    frame = PerceptionFrame(
        frame_id=1,
        map_epoch=1,
        mode_epoch=1,
        topology_version=1,
        observer_id=observer.player_id,
        observer_generation=observer.generation,
        created_at=created_at,
        mode_id="tdm",
        players=(active, observer),
    )

    queue = SimpleBotBrain._breach_queue(
        frame,
        observer,
        (13.5, 10.5, 18.5),
    )

    assert tuple(player.player_id for player in queue) == (1, 3)


def test_distant_team_routes_receive_stable_distinct_segment_lanes() -> None:
    observer = PlayerSnapshot(
        player_id=1,
        generation=1,
        team=TEAM1,
        class_id=int(C.CLASS_SOLDIER),
        alive=True,
        spawned=True,
        position=(50.0, 50.0, 17.75),
        eye=(50.0, 50.0, 16.75),
        orientation=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0),
        health=100,
        tool=int(C.SMG_TOOL),
        blocks=0,
        ammo_clip=30,
        ammo_reserve=90,
        is_bot=True,
    )
    teammates = tuple(
        replace(observer, player_id=player_id)
        for player_id in range(1, 24, 2)
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
        players=teammates,
    )
    goals = tuple(
        SimpleBotBrain._team_lane_segment_goal(
            replace(frame, observer_id=teammate.player_id),
            teammate,
            (350.0, 50.0, 17.75),
        )
        for teammate in teammates
    )
    styles = tuple(
        SimpleBotBrain._traversal_personality(
            replace(frame, observer_id=teammate.player_id),
            teammate,
        ).style
        for teammate in teammates
    )

    assert len(set(goals)) == len(teammates)
    assert min(goal[1] for goal in goals) == 30.0
    assert max(goal[1] for goal in goals) == 70.0
    assert styles.count(_TraversalStyle.DRY) == 4
    assert styles.count(_TraversalStyle.SWIM) == 4
    assert styles.count(_TraversalStyle.BRIDGE) == 4
