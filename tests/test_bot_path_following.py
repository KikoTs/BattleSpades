"""Routes are walked like a player walks them: far steering point, steady head."""

import math
from types import SimpleNamespace

from server.bot_ai.messages import MovementAffordance
from server.bot_ai.path_following import (
    edge_axis,
    gaze_waypoint,
    held_index,
    lookahead,
    passed_index,
    remaining_distance,
    run_end,
    runs_off,
    straight_walkable,
    takeoff_alignment,
)
from server.bot_ai.simple_navigation import RouteStep

_FLOOR = 40
_HEAD = _FLOOR - 2.25


class _Ground:
    """Flat floor; ``walls`` have no standable surface, ``pits`` sit 4 lower."""

    def __init__(self, walls=(), pits=(), raised=()):
        self.walls, self.pits, self.raised = set(walls), set(pits), dict(raised)
        self.lookups = 0

    def surface(self, x, y, height, **_kwargs):
        self.lookups += 1
        if (x, y) in self.walls:
            return None
        support = _FLOOR + 4 if (x, y) in self.pits else _FLOOR - self.raised.get((x, y), 0)
        if abs((support - 2.25) - height) > 1.0 + 1e-6:
            return None  # outside the +-1 span the follower asks for
        return SimpleNamespace(x=x, y=y, support_z=support, position=(x + .5, y + .5, support - 2.25))


def _walk(points, affordance=MovementAffordance.WALK):
    return tuple(RouteStep((x + .5, y + .5, _HEAD), affordance) for x, y in points)


_START = (10.5, 10.5, _HEAD)
_STRAIGHT = _walk((x, 10) for x in range(11, 31))


def test_open_ground_is_steered_at_a_far_point_with_one_line_test():
    world = _Ground()
    index = lookahead(world, _STRAIGHT, 0, _START)
    target = _STRAIGHT[index].waypoint
    assert 8. <= math.dist(target[:2], _START[:2]) <= 12.
    assert world.lookups <= 80  # the first (farthest) candidate already passes


def test_a_shortcut_never_crosses_a_wall_a_pit_or_clips_a_corner_with_the_shoulder():
    dogleg = _walk([(11, 10), (12, 10), (13, 10), (13, 11), (13, 12), (13, 13), (13, 14)])
    assert lookahead(_Ground(), dogleg, 0, _START) == len(dogleg) - 1  # open field: cut the corner
    corner_wall = _Ground(walls={(11, 12), (12, 12), (11, 13), (12, 13), (11, 11), (12, 11)})
    index = lookahead(corner_wall, dogleg, 0, _START)
    assert dogleg[index].waypoint[1] <= 11.5  # still before the bend
    assert not straight_walkable(_Ground(pits={(15, 10)}), _START, (20.5, 10.5, _HEAD))
    # The centre line is clear but a full-width body would scrape the pillar.
    pillar = _Ground(walls={(15, 11)})
    grazing = (20.5, 10.9, _HEAD)
    assert not straight_walkable(pillar, (10.5, 10.9, _HEAD), grazing)
    assert straight_walkable(_Ground(), (10.5, 10.9, _HEAD), grazing)


def test_single_block_steps_are_walked_but_a_two_block_rise_breaks_the_run():
    stairs = _Ground(raised={(x, 10): min(x - 12, 3) for x in range(13, 31)})
    assert straight_walkable(stairs, _START, (15.5, 10.5, _HEAD - 3))
    cliff = _Ground(raised={(x, 10): 2 for x in range(14, 31)})
    assert not straight_walkable(cliff, _START, (16.5, 10.5, _HEAD - 2))
    route = (_walk([(11, 10), (12, 10), (13, 10)])
             + (RouteStep((14.5, 10.5, _HEAD - 2), MovementAffordance.WALK),))
    assert run_end(route, 0) == 2


def test_exact_steps_end_a_run_and_are_never_skipped():
    route = (_walk([(11, 10), (12, 10), (13, 10)])
             + _walk([(14, 10)], MovementAffordance.JUMP) + _walk([(15, 10), (16, 10)]))
    assert run_end(route, 0) == 2
    assert lookahead(_Ground(), route, 0, _START) == 2
    assert lookahead(_Ground(), route, 3, (13.5, 10.5, _HEAD)) == 3  # the jump itself: exact
    assert run_end(route, 3) == 3


def test_the_steering_point_is_held_until_close_then_moved_on():
    world = _Ground()
    first = lookahead(world, _STRAIGHT, 0, _START)
    kept = _STRAIGHT[first].waypoint
    for x in (11.5, 12.5, 13.5):
        position = (x, 10.5, _HEAD)
        index = passed_index(_STRAIGHT, 0, first, position)
        assert _STRAIGHT[lookahead(world, _STRAIGHT, index, position, keep=kept)].waypoint == kept
    near = (kept[0] - 3.0, 10.5, _HEAD)
    index = passed_index(_STRAIGHT, 0, first, near)
    moved_on = _STRAIGHT[lookahead(world, _STRAIGHT, index, near, keep=kept)].waypoint
    assert moved_on[0] > kept[0]


def test_a_blocked_held_point_is_dropped_for_a_reachable_one():
    kept = _STRAIGHT[lookahead(_Ground(), _STRAIGHT, 0, _START)].waypoint
    blocked = _Ground(walls={(16, 10)})
    chosen = _STRAIGHT[lookahead(blocked, _STRAIGHT, 0, _START, keep=kept)].waypoint
    assert chosen[0] < 16.


def test_cut_corners_still_advance_the_route_index():
    dogleg = _walk([(11, 10), (12, 10), (13, 10), (13, 11), (13, 12), (13, 13), (13, 14)])
    target = len(dogleg) - 1
    # Walking the diagonal never touches the corner cells' centres.
    assert passed_index(dogleg, 0, target, (10.6, 10.6, _HEAD)) == 0
    assert passed_index(dogleg, 0, target, (12.4, 12.9, _HEAD)) >= 3
    assert passed_index(dogleg, 0, target, (13.4, 14.2, _HEAD)) == target


def test_eyes_rest_on_where_the_route_is_heading_not_on_a_sidestep():
    # One block sideways, then a long run east: the view stays east.
    route = _walk([(10, 11)] + [(x, 11) for x in range(11, 25)])
    rest = gaze_waypoint(route, 0, _START)
    bearing = math.degrees(math.atan2(rest[1] - _START[1], rest[0] - _START[0]))
    assert abs(bearing) < 12.
    sidestep = math.degrees(math.atan2(route[0].waypoint[1] - _START[1],
                                       route[0].waypoint[0] - _START[0]))
    assert abs(sidestep) > 80.
    assert gaze_waypoint(_walk([(11, 10)]), 0, _START) is None  # nothing ahead to look at


def test_minimal_worlds_and_short_routes_fall_back_to_exact_stepping():
    assert lookahead(SimpleNamespace(), _STRAIGHT, 0, _START) == 0
    assert lookahead(_Ground(), _walk([(11, 10)]), 0, _START) == 0
    assert remaining_distance(_STRAIGHT, 0, _START) == 20.
    assert remaining_distance(_STRAIGHT, 18, (28.5, 10.5, _HEAD)) == 2.


def _brain_case(route, *, world=None, sprint_goal=True):
    from dataclasses import replace
    from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
    from tests.test_simple_bot_tactics import _frame, _player

    world = world or _Ground()
    observer = replace(_player(1, 2, _START, is_bot=True), eye=_START)
    brain = SimpleBotBrain(world)
    state = _BotState(1, 1, observer.life_id)
    goal = _Goal(("trip",), (200.5, 10.5, _HEAD), "trip", 3.0, sprint_goal)
    brain._set_goal(state, goal, observer.position, 99.0)
    state.route, state.route_topology_version = route, 1
    state.waypoint_progress_at = state.next_extension_at = 100.0
    state.next_extension_at = 1e9  # routes here are hand-made; no planner
    return brain, state, goal, observer, _frame(observer)


def test_a_bot_with_room_sprints_at_a_far_point_instead_of_strolling_cell_to_cell():
    brain, state, goal, observer, frame = _brain_case(_STRAIGHT)
    intent = brain._navigation_intent(frame, observer, state, goal, 100.0)
    assert intent.movement.sprint
    assert math.dist(intent.movement.travel_waypoint[:2], _START[:2]) >= 8.
    assert intent.movement.direction[0] > .99


def test_it_brakes_only_for_a_step_that_needs_an_exact_takeoff():
    far_jump = (_walk((x, 10) for x in range(11, 22))
                + _walk([(22, 10)], MovementAffordance.JUMP))
    brain, state, goal, observer, frame = _brain_case(far_jump)
    assert brain._navigation_intent(frame, observer, state, goal, 100.0).movement.sprint
    near_jump = _walk([(11, 10), (12, 10), (13, 10)]) + _walk([(14, 10)], MovementAffordance.JUMP)
    brain, state, goal, observer, frame = _brain_case(near_jump)
    assert not brain._navigation_intent(frame, observer, state, goal, 100.0).movement.sprint


def test_a_failed_straight_walk_falls_back_to_cell_by_cell_for_that_stretch():
    from dataclasses import replace
    brain, state, goal, observer, frame = _brain_case(_STRAIGHT)
    assert brain._navigation_intent(frame, observer, state, goal, 100.0)  # lookahead active
    assert state.lookahead_active
    stalled = brain._navigation_intent(replace(frame, created_at=102.0), observer, state, goal, 102.0)
    assert stalled.debug_role.endswith(":edge_blocked")
    assert state.lookahead_off_until > 102.0


def test_waiting_for_a_plan_keeps_walking_only_over_plain_ground():
    from server.bot_ai.messages import MovementIntent
    brain, state, goal, observer, _ = _brain_case(_STRAIGHT)
    state.travel_heading = (1.0, 0.0, 0.0)
    state.navigation_window_at = 99.0
    coasting = brain._coast(observer, state, goal, 100.0)
    assert coasting.direction == (1.0, 0.0, 0.0) and not coasting.sprint
    assert coasting.travel_waypoint[0] > observer.position[0] + 3.
    # A body that has gone nowhere for a while thinks standing still, as before.
    state.navigation_window_at = 100.0
    assert brain._coast(observer, state, goal, 110.0) == MovementIntent()
    brain, state, goal, observer, _ = _brain_case(_STRAIGHT, world=_Ground(pits={(13, 10)}))
    state.travel_heading = (1.0, 0.0, 0.0)
    state.navigation_window_at = 99.0
    assert brain._coast(observer, state, goal, 100.0) == MovementIntent()


class _Voxels:
    """Columns of solid ground with a real ``solid`` query, for ledges."""

    def __init__(self, floors, *, default=_FLOOR, walls=(), water=()):
        self.floors, self.default = dict(floors), default
        self.walls, self.water = set(walls), set(water)

    def _floor(self, x, y):
        return self.floors.get((x, y), self.default)

    def solid(self, x, y, z):
        return (x, y) in self.walls or z >= self._floor(x, y)

    def surface(self, x, y, height, *, vertical_span=1, allow_water=False, **_kwargs):
        floor = self._floor(x, y)
        if (x, y) in self.walls or ((x, y) in self.water and not allow_water):
            return None
        if abs(floor - round(height + 2.25)) > vertical_span:
            return None
        return SimpleNamespace(x=x, y=y, support_z=floor, position=(x + .5, y + .5, floor - 2.25))


def _terrace(depth, **kwargs):
    return _Voxels({(x, y): _FLOOR + depth for x in range(15, 40) for y in range(0, 30)}, **kwargs)


def test_a_harmless_ledge_is_run_off_but_a_long_fall_a_wall_or_water_is_not():
    below = lambda depth: (20.5, 10.5, _HEAD + depth)
    assert straight_walkable(_terrace(3), _START, below(3))
    assert straight_walkable(_terrace(4), _START, below(4))
    assert not straight_walkable(_terrace(6), _START, below(6))  # that one hurts
    assert not straight_walkable(_terrace(3, walls={(15, 10)}), _START, below(3))
    assert not straight_walkable(_terrace(3, water={(15, 10)}), _START, below(3))
    # Down is a run; back up the same ledge is a climb the planner must author.
    assert not straight_walkable(_terrace(3), below(3), _START)


def test_safe_drops_stay_inside_a_run_and_a_lone_one_is_walked_off():
    ledge = RouteStep((15.5, 10.5, _HEAD + 3), MovementAffordance.DROP)
    route = _walk([(12, 10), (13, 10), (14, 10)]) + (ledge,) + tuple(
        RouteStep((x + .5, 10.5, _HEAD + 3), MovementAffordance.WALK) for x in (16, 17, 18))
    assert run_end(route, 0) == len(route) - 1
    assert lookahead(_terrace(3), route, 0, _START) == len(route) - 1
    deep = tuple(replace_z(step, 6) if index >= 3 else step for index, step in enumerate(route))
    assert run_end(deep, 0) == 2
    at_lip = (14.5, 10.5, _HEAD)
    assert runs_off(_terrace(3), ledge, at_lip)
    assert not runs_off(_terrace(3, walls={(15, 10)}), ledge, at_lip)
    assert not runs_off(_terrace(3), route[0], at_lip)  # a plain walk is not a ledge


def replace_z(step, depth):
    return RouteStep((step.waypoint[0], step.waypoint[1], _HEAD + depth), step.affordance)


def test_the_held_point_is_kept_in_mid_air_only_while_it_is_still_on_this_run():
    kept = _STRAIGHT[9].waypoint
    assert held_index(_STRAIGHT, 2, kept) == 9
    assert held_index(_STRAIGHT, 2, None) == 2
    assert held_index(_STRAIGHT, 12, kept) == 12  # already behind
    jump = _walk([(11, 10), (12, 10)]) + _walk([(13, 10)], MovementAffordance.JUMP) + _walk([(14, 10)])
    assert held_index(jump, 0, jump[3].waypoint) == 0  # never across an exact step


def test_an_exact_edge_is_taken_along_its_own_axis_after_one_sidestep():
    step = RouteStep((15.3, 10.5, _HEAD + 2), MovementAffordance.DROP,
                     entry_edge=((14, 10, _FLOOR), (15, 10, _FLOOR + 2)))
    assert edge_axis(step) == (1.0, 0.0, 0.0)  # not the wall-nudged diagonal
    assert takeoff_alignment(step, (14.5, 10.55, _HEAD)) is None  # lined up
    shuffle = takeoff_alignment(step, (14.5, 11.2, _HEAD))
    assert shuffle is not None and abs(shuffle[1] - 10.5) < 1e-6 and shuffle[0] <= 14.6
    assert takeoff_alignment(step, (15.2, 11.2, _HEAD)) is None  # already over the lip
    assert edge_axis(RouteStep((15.5, 10.5, _HEAD), MovementAffordance.WALK)) is None


def test_a_run_over_a_ledge_tells_the_motor_and_survives_the_moment_in_the_air():
    from dataclasses import replace
    ledge_route = _walk([(12, 10), (13, 10), (14, 10)]) + tuple(
        RouteStep((x + .5, 10.5, _HEAD + 3), MovementAffordance.DROP if x == 15
                  else MovementAffordance.WALK) for x in range(15, 24))
    brain, state, goal, observer, frame = _brain_case(ledge_route, world=_terrace(3))
    first = brain._navigation_intent(frame, observer, state, goal, 100.0)
    assert first.movement.walk_drop == 4 and first.movement.affordance is MovementAffordance.WALK
    assert first.movement.travel_waypoint[0] > 18.
    brain2, state2, goal2, observer2, frame2 = _brain_case(_STRAIGHT)
    level = brain2._navigation_intent(frame2, observer2, state2, goal2, 100.0)
    assert level.movement.walk_drop == 1  # level ground keeps the strict gate
    # Stepping off leaves the body airborne for a moment: same steering point.
    airborne = replace(observer, grounded=False, position=(15.2, 10.5, _HEAD + 1.0),
                       eye=(15.2, 10.5, _HEAD + 1.0))
    second = brain._navigation_intent(replace(frame, created_at=100.2), airborne, state, goal, 100.2)
    assert second.movement.travel_waypoint == first.movement.travel_waypoint


def test_eyes_stay_on_a_lost_enemy_then_on_the_route_and_hold_when_it_runs_out():
    brain, state, goal, observer, frame = _brain_case(_STRAIGHT)
    from server.bot_ai import simple_worker
    ahead = gaze_waypoint(_STRAIGHT, 0, _START)
    assert brain._travel_gaze(state, observer, 100.0) == ahead
    state.contact_position = (10.5, 30.5, _HEAD)
    state.contact_until = 100.0 + simple_worker._CONTACT_SECONDS - 0.5  # lost half a second ago
    assert brain._travel_gaze(state, observer, 100.0) == state.contact_position
    assert brain._travel_gaze(state, observer, 103.0) == ahead  # moved on
    state.route, state.route_index = _walk([(11, 10)]), 0
    assert brain._travel_gaze(state, observer, 103.5) == ahead  # nothing new to look at yet
    assert brain._travel_gaze(state, observer, 106.0) is None


def test_corridor_guidance_is_followed_a_stretch_at_a_time_not_corner_to_corner():
    from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
    state = _BotState(1, 1, 1)
    state.corridor = tuple((10.5 + x, 10.5, _HEAD - x % 2) for x in range(30))  # terrace: a corner per cell
    target = SimpleBotBrain._corridor_point_ahead(state, (10.5, 10.5, _HEAD))
    assert 6. <= target[0] - 10.5 <= 9.
    SimpleBotBrain._corridor_point_ahead(state, (16.4, 10.6, _HEAD))
    assert state.corridor_index == 7  # everything it has come alongside is done
    state.corridor_reach = 0.0  # a failed stretch is retried corner by corner
    assert SimpleBotBrain._corridor_point_ahead(state, (16.4, 10.6, _HEAD)) == state.corridor[7]
    state.corridor_index = 29
    assert SimpleBotBrain._corridor_point_ahead(state, state.corridor[29]) is None and state.corridor == ()


def test_a_drop_whose_landing_is_now_overhead_is_replanned_not_climbed_back_to():
    from dataclasses import replace
    route = (RouteStep((11.5, 10.5, _HEAD), MovementAffordance.DROP),) + _walk([(12, 10), (13, 10)])
    brain, state, goal, observer, frame = _brain_case(route)
    fallen = replace(observer, position=(10.5, 10.5, _HEAD + 3.0), eye=(10.5, 10.5, _HEAD + 3.0))
    intent = brain._navigation_intent(frame, fallen, state, goal, 100.0)
    assert intent.debug_role.endswith(":fell_past_step") and state.route == ()


def test_casual_errands_are_not_swapped_every_second_but_real_business_never_waits():
    from server.bot_ai.simple_worker import _Goal
    brain, state, goal, observer, frame = _brain_case(_STRAIGHT)
    look = _Goal(("noise", 1), (200.5, 10.5, _HEAD), "investigate_sound", 3.0, True)
    other = _Goal(("contact", 2, 1), (10.5, 200.5, _HEAD), "chase_last_seen", 1.75, True)
    brain._set_goal(state, look, observer.position, 99.5)
    state.goal_since = 99.5
    state.route, state.route_topology_version = _STRAIGHT, 1
    brain._navigation_intent(frame, observer, state, other, 100.0)
    assert state.goal.key == look.key and state.route == _STRAIGHT  # still on the first errand
    from server.bot_ai.simple_navigation import RoutePlan
    brain.world.plan = lambda *_args, **_kwargs: RoutePlan(_STRAIGHT, True, 1)
    brain._navigation_intent(frame, observer, state, goal, 100.1)  # "trip" is not a casual errand
    assert state.goal.key == goal.key
