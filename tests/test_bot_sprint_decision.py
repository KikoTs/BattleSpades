"""When a travelling bot holds sprint: the game's rule first, then what it must place exactly."""

from dataclasses import replace

import pytest

import shared.constants as C
from server.bot_ai.messages import MovementAffordance
from server.bot_ai.simple_navigation import RouteStep
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from tests.test_bot_path_following import _Ground, _HEAD, _START
from tests.test_simple_bot_tactics import _frame, _player

UPHILL = [int(C.CLASS_SOLDIER), int(C.CLASS_ZOMBIE), int(C.CLASS_FAST_ZOMBIE)]
FLAT_ONLY = [int(C.CLASS_CLASSIC_SOLDIER), int(C.CLASS_JUMP_ZOMBIE)]


def _route(heights, affordance=MovementAffordance.WALK, start=11):
    """One stride east per entry, each ``height`` blocks above the start floor."""

    return tuple(RouteStep((start + index + .5, 10.5, _HEAD - height), affordance)
                 for index, height in enumerate(heights))


def _world(heights, start=11):
    return _Ground(raised={(start + index, 10): height
                           for index, height in enumerate(heights) if height})


def _allowed(class_id, heights, *, velocity=.35):
    observer = replace(_player(1, 1, _START, class_id=class_id, is_bot=True),
                       velocity=(velocity, 0., 0.))
    state = _BotState(1, 1, 0, route=_route(heights))
    return SimpleBotBrain._route_allows_sprint(state, observer)


def _far_case(class_id, heights):
    observer = replace(_player(1, 2, _START, class_id=class_id, is_bot=True), eye=_START)
    brain = SimpleBotBrain(_world(heights))
    state = _BotState(1, 1, observer.life_id)
    goal = _Goal(("trip",), (200.5, 10.5, _HEAD), "trip", 3.0, True)
    brain._set_goal(state, goal, observer.position, 99.0)
    state.route, state.route_topology_version = _route(heights), 1
    state.waypoint_progress_at = 100.0
    state.next_extension_at = 1e9  # hand-made route; no planner
    intent = brain._navigation_intent(_frame(observer), observer, state, goal, 100.0)
    return intent, state


def test_only_the_classic_soldier_and_the_jump_zombie_may_not_sprint_uphill():
    refused = {class_id for class_id, allowed in C.CLASS_CAN_SPRINT_UPHILL.items()
               if not allowed}
    assert refused == set(FLAT_ONLY)


@pytest.mark.parametrize("class_id", UPHILL)
def test_a_class_that_sprints_uphill_runs_a_staircase_cell_by_cell(class_id):
    # The native mover climbs a block a stride at nine tenths of the flat
    # sprint; walking it cost a Zombie two thirds of its speed.
    assert _allowed(class_id, [1, 2, 3, 4, 5, 6])
    assert _allowed(class_id, [-1, -2, -3, -4, -5, -6])
    assert _allowed(class_id, [0, 1, 1, 2, 2, 3])


@pytest.mark.parametrize("class_id", FLAT_ONLY)
def test_a_class_that_may_not_walks_into_a_rise_and_runs_everything_else(class_id):
    # Sprinting, it would stand against the first step with its keys held.
    assert not _allowed(class_id, [1, 2, 3, 4, 5, 6])
    assert not _allowed(class_id, [0, 0, 1, 1, 1, 1])
    # Downhill and the flat before a slope are run like anybody's.
    assert _allowed(class_id, [-1, -2, -3, -4, -5, -6])
    assert _allowed(class_id, [0, 0, 0, 0, 0, 1])


@pytest.mark.parametrize("class_id", UPHILL + FLAT_ONLY)
def test_two_blocks_in_one_stride_is_no_step_for_anybody(class_id):
    assert not _allowed(class_id, [0, 2, 2, 2, 2, 2])
    assert not _allowed(class_id, [0, -2, -2, -2, -2, -2])


def test_what_is_placed_exactly_still_ends_a_sprint():
    soldier = int(C.CLASS_SOLDIER)
    observer = replace(_player(1, 1, _START, class_id=soldier, is_bot=True),
                       velocity=(.35, 0., 0.))

    def allowed(route):
        return SimpleBotBrain._route_allows_sprint(_BotState(1, 1, 0, route=route), observer)

    assert not allowed(_route([0, 0]) + _route([0], MovementAffordance.JUMP, start=13))
    assert not allowed(_route([0, 0, 0]))                            # too short to stop in
    assert not allowed(_route([0, 0]) + tuple(                       # a corner
        RouteStep((12.5, 11.5 + index, _HEAD), MovementAffordance.WALK) for index in range(6)))


@pytest.mark.parametrize("class_id", UPHILL)
def test_a_run_steered_at_a_far_point_sprints_up_a_slope(class_id):
    intent, state = _far_case(class_id, [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6])
    assert state.lookahead_active and intent.movement.sprint


@pytest.mark.parametrize("class_id", FLAT_ONLY)
def test_a_class_that_may_not_lets_go_of_sprint_just_before_the_step(class_id):
    # The step is one stride ahead: sprint held here pins the body to it.
    intent, state = _far_case(class_id, [0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6])
    assert state.lookahead_active and not intent.movement.sprint
    # ... and six strides ahead it is still a flat run.
    intent, state = _far_case(class_id, [0, 0, 0, 0, 0, 0, 1, 1, 2, 2, 3, 3])
    assert state.lookahead_active and intent.movement.sprint
    # Down a slope there is nothing to climb.
    intent, state = _far_case(class_id, [0, -1, -1, -2, -2, -3, -3, -4, -4, -5, -5, -6])
    assert state.lookahead_active and intent.movement.sprint
