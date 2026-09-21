"""Meaningful route progress and travel gaze regressions for visible circling."""

from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.bot_ai.messages import MapSnapshot, MovementAffordance, ObjectiveSnapshot
from server.bot_ai.policies import ModeBotDecision
from server.bot_ai.simple_navigation import RouteStep, SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState, _Goal
from tests.test_simple_bot_tactics import _TacticalWorld, _frame, _player


SQUARE = ((10., 10., 20.), (16., 10., 20.), (16., 16., 20.), (10., 16., 20.))


def _follow_positions(points, *, swap_goals=False, mixed_traversal=False):
    """Feed valid movement edges through the real route executor, not a timer mock."""
    brain = SimpleBotBrain(_TacticalWorld())
    state = _BotState(1, 1, 0)
    roles = []
    for tick, position in enumerate(points[:-1]):
        observer = _player(1, 1, position, is_bot=True)
        goal = _Goal(("loop", tick if swap_goals else 0),
                     (100., 10., 20.), "route_test", 1., True)
        brain._set_goal(state, goal, position, 100. + tick)
        affordance = ((MovementAffordance.WALK, MovementAffordance.JUMP,
                       MovementAffordance.DROP)[tick % 3]
                      if mixed_traversal else MovementAffordance.WALK)
        state.route = (RouteStep(points[tick + 1], affordance),)
        state.route_index, state.route_topology_version = 0, 1
        state.waypoint_best_distance = math.inf
        result = brain._navigation_intent(_frame(observer, created_at=100. + tick),
                                          observer, state, goal, 100. + tick)
        roles.append(result.debug_role)
    return roles, state


@pytest.mark.parametrize("swap_goals", [False, True])
def test_closed_loop_cannot_renew_progress_by_crossing_four_blocks_or_changing_tasks(swap_goals):
    roles, state = _follow_positions(SQUARE * 10 + (SQUARE[0],), swap_goals=swap_goals)
    assert "route_test:route_cycle" in roles[:16]
    assert roles.count("route_test:route_cycle") >= 2
    assert state.blocked_edges
    assert len(state.navigation_visited) == 4


def test_long_useful_detour_can_move_away_from_goal_without_being_a_loop():
    positions = tuple((10., 10. + tick * 5., 20.) for tick in range(35))
    roles, _ = _follow_positions(positions)
    assert set(roles) == {"route_test"}


def test_repeated_jump_and_drop_edges_cannot_reset_terrace_loop_deadline():
    roles, _ = _follow_positions(SQUARE * 10 + (SQUARE[0],), mixed_traversal=True)
    assert "route_test:route_cycle" in roles[:16]


def test_local_cycle_retains_pending_mapwide_detour_and_teaches_failed_edge():
    observer = _player(1, 1, SQUARE[0], is_bot=True)
    brain, state = SimpleBotBrain(_TacticalWorld()), _BotState(1, 1, 0)
    goal = _Goal(("shelf",), (100., 10., 30.), "shelf", 1., True)
    brain._set_goal(state, goal, observer.position, 100.)
    state.route = (RouteStep(SQUARE[1], MovementAffordance.WALK),)
    state.route_topology_version = 1
    state.navigation_visited[(5, 5, 10)] = 100.
    state.navigation_coverage_at = 100.
    state.navigation_coverage_last_at = 108.
    excluded = []
    search = SimpleNamespace(done=False, exclude_edge=lambda *edge: excluded.append(edge))
    state.corridor_search = search
    result = brain._navigation_intent(_frame(observer, created_at=109.),
                                      observer, state, goal, 109.)
    assert result.debug_role == "shelf:route_cycle"
    assert state.corridor_search is search
    assert excluded and state.blocked_edges


def test_internal_navigation_diagnostics_are_fixed_and_detached():
    state = _BotState(1, 1, 0)
    state.corridor_search = SimpleNamespace(expansions=1536, done=False)
    state.escape_goal = (10., 20., 30.)
    state.goal_progress_at = 90.
    state.planning_results[("cached",)] = object()
    snapshot = SimpleBotBrain._navigation_diagnostics(state, 100.)
    assert len(snapshot) == 20
    assert dict(snapshot)["corridor_expansions"] == 1536
    assert dict(snapshot)["goal_progress_age"] == 10.
    assert dict(snapshot)["escape_goal"] == (10., 20., 30.)
    state.corridor_search.expansions = 2048
    state.planning_results.clear()
    assert dict(snapshot)["corridor_expansions"] == 1536
    assert dict(snapshot)["planning_results"] == 1


def _failed_support():
    observer = _player(3, 1, (297.5, 260.5, 225.75), is_bot=True)
    decision = ModeBotDecision((280.44, 260.42, 236.65), "tdm_squad_support",
                               objective_priority=.58)
    brain, state = SimpleBotBrain(_TacticalWorld()), _BotState(1, 1, 0)
    brain._set_goal(state, brain._goal_from_mode_decision(decision), observer.position, 80.)
    state.escape_attempts = 1
    state.corridor_failed_goal = decision.position
    state.support_progress_anchor = decision.position
    state.support_best_distance = math.dist(observer.position, decision.position)
    state.support_no_progress_time = 20.
    state.support_last_decision_at = 100.
    state.support_failed_goal = decision.position
    state.support_failure_at = 99.
    state.support_escape_attempted = True
    frame = _frame(observer, objectives=(
        ObjectiveSnapshot("team_anchor", team=2, position=(100., 100., 225.75)),))
    return brain, state, observer, frame, decision


def test_failed_optional_support_uses_bounded_assault_fallback_then_retries():
    brain, state, observer, frame, decision = _failed_support()
    fallback = brain._select_goal(frame, observer, state, 100., decision=decision)
    assert fallback.role == "team_assault_enemy_side"
    assert fallback.position == (100., 100., 225.75)
    assert state.support_retry_at == 130.
    brain._set_goal(state, fallback, observer.position, 100.)
    assert brain._select_goal(frame, observer, state, 120., decision=decision).role == fallback.role
    assert brain._select_goal(frame, observer, state, 130., decision=decision).role == decision.role


def test_recorded_london_lower_layer_support_has_no_endpoint_and_selects_useful_fallback():
    brain, state, observer, frame, decision = _failed_support()
    world = SimpleVoxelWorld()
    world.load(MapSnapshot(1, 1, (Path(__file__).parents[1] / "maps/London.vxl").read_bytes(),
                           "tdm", "London"))
    brain.world = world
    state.corridor_failed_goal = None
    assert brain._corridor_segment_goal(state, observer, state.goal, 100.) is None
    assert state.corridor_failed_goal == decision.position
    plan = world.plan(observer.position, decision.position,
                      abilities=frozenset({MovementAffordance.JUMP, MovementAffordance.DROP}),
                      allow_water=True)
    assert not plan.steps and plan.expansions == 256
    fallback = brain._select_goal(frame, observer, state, 100., decision=decision)
    assert fallback.role == "team_assault_enemy_side"
    assert fallback.position == frame.objectives[0].position


@pytest.mark.parametrize("evidence", ("recent_progress", "no_escape", "no_endpoint_failure"))
def test_reachable_or_untried_support_does_not_get_suppressed(evidence):
    brain, state, observer, frame, decision = _failed_support()
    if evidence == "recent_progress":
        state.support_no_progress_time = 1.
    elif evidence == "no_escape":
        state.support_escape_attempted = False
    else:
        state.support_failed_goal = None
    assert brain._select_goal(frame, observer, state, 100., decision=decision).role == decision.role
    assert state.support_retry_at == 0.


def test_changed_support_position_releases_cooldown_without_waiting():
    brain, state, observer, frame, decision = _failed_support()
    fallback = brain._select_goal(frame, observer, state, 100., decision=decision)
    brain._set_goal(state, fallback, observer.position, 100.)
    moved = replace(decision, position=(270., 260.42, 236.65))
    assert brain._select_goal(frame, observer, state, 101., decision=moved).position == moved.position
    assert state.support_retry_at == 0.


def test_successful_support_arrival_clears_old_failed_approach_evidence():
    brain, state, observer, frame, decision = _failed_support()
    state.support_retry_at = 130.
    state.support_rejected_goal = decision.position
    arrived = replace(observer, position=decision.position, wade=False)
    assert brain._select_goal(frame, arrived, state, 100., decision=decision).role == decision.role
    assert state.corridor_failed_goal is None
    assert state.support_retry_at == 0.


def test_support_cooldown_never_suppresses_critical_objectives_or_recent_contact():
    brain, state, observer, frame, decision = _failed_support()
    state.support_retry_at = 130.
    critical = replace(decision, role="ctf_capture", objective_priority=.98)
    assert brain._select_goal(frame, observer, state, 100., decision=critical).role == "ctf_capture"
    state.contact_position, state.contact_until = (295., 255., 225.75), 110.
    assert brain._select_goal(frame, observer, state, 100., decision=decision).role == "chase_last_seen"


def test_stationary_support_commitment_survives_target_jitter_and_transient_role_resets():
    brain, state, observer, frame, decision = _failed_support()
    state.support_no_progress_time = 0.
    selected = None
    for tick in range(65):
        now = 100. + tick / 4
        moving = replace(decision, position=(decision.position[0] + 4.5 * math.sin(tick / 9),
                                             decision.position[1] + 4.5 * math.sin(tick / 7),
                                             decision.position[2]))
        # Captured London behavior: the objective moves while actor position
        # is fixed, renewing the generic goal timer and corridor ownership.
        brain._set_goal(state, brain._goal_from_mode_decision(moving), observer.position, now)
        state.goal_progress_at = now
        if tick % 16 == 0:
            state.support_failed_goal = moving.position
            state.support_failure_at = now
        selected = brain._select_goal(frame, observer, state, now, decision=moving)
        if selected.role == "team_assault_enemy_side":
            break
    assert selected.role == "team_assault_enemy_side"
    assert 115. <= now <= 116.
    assert state.goal_progress_at == now
    assert state.support_retry_at == now + 30.


def test_actor_progress_and_large_support_relocation_start_fresh_commitments():
    brain, state, observer, frame, decision = _failed_support()
    toward = tuple(a + (b - a) * .25 for a, b in zip(observer.position, decision.position))
    progressing = replace(observer, position=toward)
    assert math.dist(observer.position, decision.position) - math.dist(toward, decision.position) > 4.
    result = brain._select_goal(frame, progressing, state, 100.25, decision=decision)
    assert result.role == decision.role
    assert state.support_no_progress_time == 0.
    assert not state.support_escape_attempted and state.support_failed_goal is None
    state.support_no_progress_time = 20.
    relocated = replace(decision, position=(250., 260.42, 236.65))
    assert brain._select_goal(frame, observer, state, 100.5, decision=relocated).role == decision.role
    assert state.support_no_progress_time < 1.


def test_support_commitment_pauses_combat_gaps_and_committed_excavation():
    brain, state, observer, frame, decision = _failed_support()
    state.support_no_progress_time = 5.
    brain._select_goal(frame, observer, state, 140., decision=decision)
    assert state.support_no_progress_time == 5.
    state.route = (RouteStep((296.5, 260.5, 225.75), MovementAffordance.BREACH),)
    brain._select_goal(frame, observer, state, 140.25, decision=decision)
    assert state.support_no_progress_time == 5.


def test_actual_egypt_passed_terrace_landing_continues_forward_without_lowering_tolerance():
    # Recorded bot2 worker snapshot at60.3s. Native auto-step/momentum passed
    # the old lower waypoint and occupies the next higher ordinary landing.
    observer = _player(2, 1, (212.802734375, 231.24636840820312, 226.2506866455078),
                       is_bot=True)
    route = tuple(RouteStep(point, MovementAffordance.WALK) for point in (
        (211.5, 231.68, 227.75), (212.5, 231.5, 226.75),
        (213.5, 231.68, 226.75), (214.5, 231.5, 225.75)))
    goal = _Goal(("egypt",), (351.303, 261.174, 207.75), "terrace", 1., True)
    brain, state = SimpleBotBrain(_TacticalWorld()), _BotState(2, 1, 0)
    brain._set_goal(state, goal, observer.position, 100.)
    state.route, state.route_topology_version = route, 1
    intent = brain._navigation_intent(_frame(observer), observer, state, goal, 100.)
    assert state.route_index >= 2
    assert intent.movement.direction[0] > 0
    assert not intent.movement.sprint
    assert intent.movement.travel_source == observer.position
    assert intent.movement.travel_waypoint == route[state.route_index].waypoint


@pytest.mark.parametrize("special", [MovementAffordance.JUMP, MovementAffordance.DROP,
                                     MovementAffordance.BREACH, MovementAffordance.SWIM])
def test_route_catchup_does_not_skip_unexecuted_special_traversal(special):
    state = _BotState(1, 1, 0, route=(
        RouteStep((10.5, 10.5, 20.), MovementAffordance.WALK),
        RouteStep((11.5, 10.5, 18.), special),
        RouteStep((12.5, 10.5, 18.), MovementAffordance.WALK)))
    SimpleBotBrain._catch_up_walk_route(state, (12.5, 10.5, 18.))
    assert state.route_index == 0


def test_route_catchup_requires_actual_destination_column_and_height():
    state = _BotState(1, 1, 0, route=(
        RouteStep((10.5, 10.5, 20.), MovementAffordance.WALK),
        RouteStep((11.5, 10.5, 19.), MovementAffordance.WALK)))
    for position in ((10.95, 10.5, 19.), (11.5, 10.5, 20.)):
        SimpleBotBrain._catch_up_walk_route(state, position)
        assert state.route_index == 0


def test_sprint_brakes_before_terrace_and_turn_but_preserves_clear_straights():
    observer = replace(_player(1, 1, (10.5, 10.5, 20.), is_bot=True),
                       velocity=(.35, 0., 0.))
    def allowed(points):
        state = _BotState(1, 1, 0, route=tuple(
            RouteStep(point, MovementAffordance.WALK) for point in points))
        return SimpleBotBrain._route_allows_sprint(state, observer)
    assert allowed(((30.5, 10.5, 20.),))
    assert allowed(((12.5, 10.5, 20.), (16.5, 10.5, 20.)))
    assert not allowed(((12.5, 10.5, 20.), (12.5, 20.5, 20.)))
    assert not allowed(((12.5, 10.5, 20.), (13.5, 10.5, 19.)))
    assert not allowed(((13.5, 10.5, 20.),))


def test_coverage_memory_is_bounded_and_combat_gap_does_not_create_failure():
    state = _BotState(1, 1, 0)
    goal = _Goal(("cover",), (10., 10., 20.), "route", 1., True)
    for index in range(200):
        assert not SimpleBotBrain._navigation_revisits(state, (index * 3., 10., 20.),
                                                      goal, 100. + index)
    assert len(state.navigation_visited) == 128
    assert not SimpleBotBrain._navigation_revisits(state, (597., 10., 20.), goal, 500.)
    assert not _BotState(1, 1, 1).navigation_visited


def test_old_cells_still_count_when_returning_toward_a_different_goal():
    state = _BotState(1, 1, 0)
    outward = _Goal(("out",), (90., 10., 20.), "route", 1., True)
    for index in range(10):
        assert not SimpleBotBrain._navigation_revisits(state, (index * 5., 10., 20.),
                                                      outward, 100. + index)
    homeward = replace(outward, key=("home",), position=(-10., 10., 20.))
    for index in range(10):
        assert not SimpleBotBrain._navigation_revisits(state, ((9 - index) * 5., 10., 20.),
                                                      homeward, 110. + index)


def test_escape_attempts_survive_perpendicular_motion_through_old_loop():
    state = _BotState(1, 1, 0, escape_attempts=3, escape_goal=(10., 30., 20.))
    goal = _Goal(("main",), (100., 10., 20.), "route", 1., True)
    SimpleBotBrain._navigation_revisits(state, SQUARE[0], goal, 100.)
    SimpleBotBrain._navigation_revisits(state, SQUARE[1], goal, 101.)
    assert state.escape_attempts == 3


def test_travel_aim_stays_ahead_at_eye_height_when_the_body_passes_a_near_waypoint():
    observer = _player(1, 1, (10., 10., 20.), is_bot=True)
    step = RouteStep((11., 10., 20.25), MovementAffordance.WALK)
    brain = SimpleBotBrain(_TacticalWorld(route_step=step))
    state = _BotState(1, 1, observer.life_id)
    goal = _Goal(("travel",), (30., 10., 20.), "travel", 1., True)
    intent = brain._navigation_intent(_frame(observer), observer, state, goal, 100.)
    target = intent.look.target
    assert target[2] == observer.eye[2]
    assert intent.movement.travel_source == observer.position
    assert intent.movement.travel_waypoint == step.waypoint
    # At6units/s the actor can pass the old footstep before the next worker
    # update, but the gaze target remains forward and cannot cause a pitch flip.
    for advance in (0., .6, 1.2):
        assert target[0] - (observer.eye[0] + advance) >= 4.8 - 1e-9
        assert target[2] - observer.eye[2] == 0


def test_repeated_budget_deferral_does_not_become_a_route_cycle(monkeypatch):
    from tests.test_bot_planning_integration import _setup, _navigate
    world, brain, observer, state, goal = _setup(monkeypatch)
    world.planning_budget.try_acquire((2, 1), 100.)
    # Each turn is denied by a current competing observer; no geometry outcome
    # or physical movement has occurred, so time spent waiting is not failure.
    for index in range(120):
        now = 100. + index / 8
        world.planning_budget._credits = 0
        world.planning_budget._last_time = now
        intent = _navigate(world, brain, observer, state, goal, now)
        assert intent.debug_role.endswith(":planning_wait")
    assert not state.blocked_edges
    assert not state.escape_attempts
