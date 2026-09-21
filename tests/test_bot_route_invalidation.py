"""Distant builds cannot consume every bot's next route grant."""

from dataclasses import replace

import pytest

from server.bot_ai.messages import MapSnapshot, MovementAffordance, VoxelChange, WorldDelta
from server.bot_ai.planning_budget import PlanningBudget
from server.bot_ai.simple_navigation import (
    MAP_SIZE, MAX_ROUTE_VALIDATION_STEPS, RouteStep, SimpleVoxelWorld,
)
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from tests.test_simple_bot_tactics import _frame, _player


class _Ground:
    def __init__(self):
        self.changed = {}

    def get_solid(self, x, y, z):
        return self.changed.get((x, y, z), z >= 100)

    def set_solid(self, x, y, z, solid):
        self.changed[(x, y, z)] = solid


def _world():
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(1))
    world.load(MapSnapshot(1, 0, b"", "tdm", "local-invalidation"))
    world._vxl = _Ground()
    return world


POSITION = (10.5, 10.5, 97.75)
ROUTE = tuple(RouteStep((x + .5, 10.5, 97.75), MovementAffordance.WALK)
              for x in range(11, 16))


def test_far_edits_leave_simple_route_and_crouch_clearance_valid():
    world = _world()
    world.apply(WorldDelta(1, 1, (VoxelChange(200, 300, 100, False),)))
    assert not world.route_needs_replan(0, POSITION, ROUTE)
    crouching = tuple(replace(step, affordance=MovementAffordance.CROUCH) for step in ROUTE)
    assert not world.route_needs_replan(0, POSITION, crouching)


@pytest.mark.parametrize("cell", [(11, 10, 100), (12, 11, 98), (10, 9, 20)])
def test_own_floor_adjacent_wall_or_other_height_in_same_column_invalidates(cell):
    world = _world()
    world.apply(WorldDelta(1, 2, (VoxelChange(*cell, False),)))
    assert world.route_needs_replan(0, POSITION, ROUTE)


_SPECIAL = [MovementAffordance.JUMP, MovementAffordance.BREACH, MovementAffordance.BUILD_STEP,
            MovementAffordance.JETPACK, MovementAffordance.SWIM, MovementAffordance.DROP]


@pytest.mark.parametrize("kind", _SPECIAL)
def test_a_distant_edit_does_not_discard_a_route_because_it_contains_a_special_step(kind):
    # Every broken block used to invalidate every route with a jump in it, so
    # a firefight kept whole squads queueing for the planner and standing still.
    world = _world()
    world.apply(WorldDelta(1, 1, (VoxelChange(200, 300, 100, False),)))
    route = (ROUTE[0], replace(ROUTE[1], affordance=kind), *ROUTE[2:])
    assert not world.route_needs_replan(0, POSITION, route)


@pytest.mark.parametrize("kind", _SPECIAL)
def test_an_edit_beside_a_special_step_still_invalidates_it(kind):
    world = _world()
    world.apply(WorldDelta(1, 1, (VoxelChange(13, 11, 96, True),)))
    route = (ROUTE[0], replace(ROUTE[1], affordance=kind), *ROUTE[2:])
    assert world.route_needs_replan(0, POSITION, route)


def test_a_dig_edge_is_invalidated_when_its_own_cells_change():
    from server.bot_ai.simple_navigation import BreachPlan
    world = _world()
    dig = BreachPlan((11, 10, 100), (12, 10, 100), (40, 40, 98), ((40, 40, 97),), 2, False, .5, 3)
    route = (replace(ROUTE[0], affordance=MovementAffordance.BREACH, breach=dig),)
    world.apply(WorldDelta(1, 1, (VoxelChange(200, 300, 100, False),)))
    assert not world.route_needs_replan(0, POSITION, route)
    world.apply(WorldDelta(1, 2, (VoxelChange(40, 40, 98, False),)))
    assert world.route_needs_replan(0, POSITION, route)


def test_history_is_fixed_size_and_unknown_versions_or_map_reset_fail_closed():
    world = _world()
    storage = world._column_versions
    for version in range(1, 5001):
        world.apply(WorldDelta(1, version, (VoxelChange(200, 300, 100, bool(version % 2)),)))
    assert world._column_versions is storage
    assert len(storage) == MAP_SIZE * MAP_SIZE
    assert storage.itemsize * len(storage) == 2 * 1024 * 1024
    assert not world.route_needs_replan(0, POSITION, ROUTE)
    assert world.route_needs_replan(-1, POSITION, ROUTE)
    assert world.route_needs_replan(6000, POSITION, ROUTE)
    world.topology_version += 1  # Untracked/manual timeline is unknown.
    assert world.route_needs_replan(0, POSITION, ROUTE)
    world.apply(WorldDelta(1, 5002, (VoxelChange(200, 300, 100, True),)))
    assert world.route_needs_replan(0, POSITION, ROUTE)
    world.load(MapSnapshot(2, 6000, b"", "tdm", "new-map"))
    assert world.route_needs_replan(5000, POSITION, ROUTE)
    assert not world.route_needs_replan(6000, POSITION, ROUTE)


def test_long_and_sparse_routes_are_checked_locally_within_a_hard_bound():
    world = _world()
    long_route = tuple(RouteStep((x + .5, 10.5, 97.75), MovementAffordance.WALK)
                       for x in range(11, 11 + MAX_ROUTE_VALIDATION_STEPS * 3))
    sparse = (ROUTE[-1],)  # one compacted five-block stride
    assert not world.route_needs_replan(0, POSITION, long_route)
    assert not world.route_needs_replan(0, POSITION, sparse)
    assert world.route_needs_replan(0, POSITION, (replace(ROUTE[0], waypoint=(float("nan"), 10, 97)),))
    # An edit under the middle of the compacted stride is found...
    world.apply(WorldDelta(1, 1, (VoxelChange(13, 10, 100, False),)))
    assert world.route_needs_replan(0, POSITION, sparse)
    # ...while one beyond the validated stretch waits until the body is nearer.
    far = _world()
    far.apply(WorldDelta(1, 1, (VoxelChange(11 + MAX_ROUTE_VALIDATION_STEPS + 40, 10, 100, False),)))
    assert not far.route_needs_replan(0, POSITION, long_route)


@pytest.mark.parametrize("local_edit", [False, True])
def test_real_brain_keeps_moving_without_a_grant_and_retries_an_affected_route(local_edit):
    world = _world()
    observer = _player(1, 1, POSITION, is_bot=True)
    brain = SimpleBotBrain(world)
    state = _BotState(1, 1, observer.life_id)
    goal = _Goal(("build_traffic",), (30.5, 10.5, 97.75), "build_traffic", 1, True)
    brain._set_goal(state, goal, POSITION, 100)
    state.route, state.route_topology_version = ROUTE, 0
    cell = (11, 11, 98) if local_edit else (200, 300, 100)
    world.apply(WorldDelta(1, 1, (VoxelChange(*cell, True),)))
    world.planning_budget.try_acquire((2, 1), 100)
    world.begin_planning((1, 1), 100)
    intent = brain._navigation_intent(_frame(observer), observer, state, goal, 100)
    world.end_planning()
    assert state.route is ROUTE
    assert world.planning_budget.snapshot()["granted"] == 1
    # Either way the bot keeps walking: a busy planner is not a reason to halt.
    assert intent.movement.direction[0] > 0
    assert not intent.debug_role.endswith(":planning_wait")
    # Only the unaffected route is marked current; the edited one asks again.
    assert state.route_topology_version == (0 if local_edit else 1)
