"""Production brain continuations under the shared route admission budget."""

from dataclasses import replace
import math

import pytest

from server.bot_ai.messages import MovementAffordance, VoxelChange, WorldDelta
from server.bot_ai.planning_budget import PlanningBudget
from server.bot_ai.simple_navigation import (
    RoutePlan, RouteStep, SimpleVoxelWorld, _BudgetedCorridorSearch,
)
from server.bot_ai.surface_corridor import SurfaceCorridorSearch
from server.bot_ai.simple_worker import (
    SimpleBotBrain, _BotState, _Goal, _TraversalPersonality, _TraversalStyle,
    behavior_metrics_snapshot, decide_current_frame,
)
from tests.test_simple_bot_tactics import _frame, _player


class _Ground:
    def get_solid(self, x, y, z):
        return z >= 100


class _Island:
    def __init__(self):
        self.added = set()

    def get_solid(self, x, y, z):
        return (x, y, z) in self.added or (x == 10 and y == 10 and z >= 238)

    def set_solid(self, x, y, z, solid):
        assert solid
        self.added.add((x, y, z))


def _setup(monkeypatch, *, island=False, style=_TraversalStyle.DRY, world=None):
    world = world or SimpleVoxelWorld(planning_budget=PlanningBudget(1))
    world._vxl = _Island() if island else _Ground()
    world.map_epoch = world.topology_version = 1
    observer = _player(1, 1, (10.5, 10.5, 235.75 if island else 97.75), is_bot=True)
    brain = SimpleBotBrain(world)
    monkeypatch.setattr(brain, "_traversal_personality", lambda *_: _TraversalPersonality(style, 1, .5))
    monkeypatch.setattr(brain, "_team_lane_segment_goal", lambda _frame, _observer, target: target)
    state = _BotState(1, 1, observer.life_id)
    goal = _Goal(("budget_goal",), (30.5, 10.5, observer.position[2]), "budget_goal", 1, True)
    return world, brain, observer, state, goal


def _navigate(world, brain, observer, state, goal, now=100.0):
    frame = replace(_frame(observer, created_at=now), topology_version=world.topology_version)
    world.begin_planning((observer.player_id, observer.generation), now)
    try:
        return brain._navigation_intent(frame, observer, state, goal, now)
    finally:
        world.end_planning(cancel_unused=True)


def test_dry_detour_and_wet_fallback_resume_without_counting_untried_failure(monkeypatch):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    first = _navigate(world, brain, observer, state, goal)
    assert first.debug_role.endswith(":planning_wait")
    assert state.dry_detour_goal is None and state.dry_route_failures == 0
    assert len(state.planning_results) == 1
    second = _navigate(world, brain, observer, state, goal, 101)
    assert second.debug_role.endswith(":planning_wait")
    assert state.dry_detour_goal is None and state.dry_route_failures == 0
    assert len(state.planning_results) == 2
    third = _navigate(world, brain, observer, state, goal, 102)
    assert not third.debug_role.endswith(":planning_wait")
    assert any(step.affordance is MovementAffordance.SWIM for step in state.route)
    assert state.dry_route_failures == 1
    assert not state.planning_results
    assert world.planning_budget.snapshot()["granted"] == 3
    assert not state.blocked_edges


class _WetDeadEndWorld(SimpleVoxelWorld):
    """A deterministic wet-layer dead end with ordinary real dry geometry."""
    def _plan(self, start, goal, **arguments):
        if arguments.get("allow_water"):
            return RoutePlan((), False, 0)
        return super()._plan(start, goal, **arguments)


def test_swimmer_reaches_dry_fallback_instead_of_repeating_failed_wet_query(monkeypatch):
    world = _WetDeadEndWorld(planning_budget=PlanningBudget(1))
    world, brain, observer, state, goal = _setup(monkeypatch, world=world, style=_TraversalStyle.SWIM)
    assert _navigate(world, brain, observer, state, goal).debug_role.endswith(":planning_wait")
    recovered = _navigate(world, brain, observer, state, goal, 101)
    assert recovered.movement.direction[0] > 0
    assert world.planning_budget.snapshot()["granted"] == 2


def test_real_brain_dispatch_keeps_live_state_when_another_observer_used_grant(monkeypatch):
    world, brain, observer, _, goal = _setup(monkeypatch)
    monkeypatch.setattr(brain, "_select_goal", lambda *_args, **_kwargs: goal)
    world.planning_budget.try_acquire((2, 1), 100)
    intent = decide_current_frame(world, brain, _frame(observer))
    assert intent is not None and intent.debug_role.endswith(":planning_wait")
    state = brain._states[(observer.player_id, observer.generation)]
    assert not state.blocked_edges
    result = decide_current_frame(world, brain, _frame(observer, created_at=101))
    assert result is not None and result.movement.direction[0] > 0
    assert brain._states[(observer.player_id, observer.generation)] is state
    assert world._planning_observer is None


@pytest.mark.parametrize("kind", (MovementAffordance.WALK, MovementAffordance.BREACH))
def test_topology_denial_preserves_route_breach_and_failed_edge_history(monkeypatch, kind):
    world, brain, observer, state, goal = _setup(monkeypatch)
    brain._set_goal(state, goal, observer.position, 99)
    state.route = (RouteStep((12.5, 10.5, 97.75), kind),)
    state.route_topology_version = 0
    state.breach_key = ("unchanged_breach",)
    state.breach_started_at = 99
    state.next_breach_at = 101
    state.waypoint_progress_at = 99
    state.navigation_progress_at = state.navigation_window_at = 99
    state.navigation_progress_position = state.navigation_window_position = observer.position
    route = state.route
    world.planning_budget.try_acquire((2, 1), 100)
    denied = _navigate(world, brain, observer, state, goal)
    assert state.route is route and state.route_topology_version == 0
    assert not state.blocked_edges
    if kind is MovementAffordance.WALK:
        # A busy planner is no reason to halt on plain ground: the still-usable
        # walk continues and the revalidation is asked for again next decision.
        assert not denied.debug_role.endswith(":planning_wait")
        assert denied.movement.direction[0] > 0
    else:
        # An exact edge is never executed against changed terrain; it waits
        # with its dig clocks and progress history untouched.
        assert denied.debug_role.endswith(":planning_wait")
        assert state.breach_key == ("unchanged_breach",)
        assert state.breach_started_at == 99 and state.next_breach_at == 101
        assert state.waypoint_progress_at == 99
    recovered = _navigate(world, brain, observer, state, goal, 101)
    assert recovered.movement.direction[0] > 0
    assert state.route_topology_version == 1
    assert not state.blocked_edges


def test_topology_change_discards_completed_query_before_resuming(monkeypatch):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    assert _navigate(world, brain, observer, state, goal).debug_role.endswith(":planning_wait")
    # Authoritative bridge support appears while waiting; the previously failed
    # dry query must run again against current terrain instead of going wet.
    world.apply(WorldDelta(map_epoch=1, topology_version=2, changed_cells=tuple(
        VoxelChange(x, 10, 238, True) for x in range(11, 32))))
    result = _navigate(world, brain, observer, state, goal, 101)
    assert not result.debug_role.endswith(":planning_wait")
    assert result.movement.direction[0] > 0
    assert not any(step.affordance is MovementAffordance.SWIM for step in state.route)
    assert world.planning_budget.snapshot()["granted"] == 2


def test_escape_reaches_later_water_candidates_and_is_resumed_before_routing(monkeypatch):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    brain._set_goal(state, goal, observer.position, 100)
    world.begin_planning((1, 1), 100)
    brain._escape_empty_route(_frame(observer), observer, state, 100)
    world.end_planning()
    assert state.escape_search is not None and state.escape_search[2] == 1
    assert state.escape_attempts == 0
    for tick in range(1, 10):
        result = _navigate(world, brain, observer, state, goal, 100 + tick)
        if state.escape_goal is not None:
            break
        assert result.debug_role.endswith(":planning_wait")
    assert tick == 8  # Eight tested dry headings, then the first water heading.
    assert state.escape_search is None and state.escape_attempts == 1
    assert state.escape_allow_water
    assert world.planning_budget.snapshot()["granted"] == 9


@pytest.mark.parametrize("terrain_changes", [False, True])
def test_escape_exhausts_headings_despite_moving_goal_and_distant_edits(monkeypatch, terrain_changes):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    brain._set_goal(state, goal, observer.position, 100)
    world.begin_planning((1, 1), 100)
    brain._escape_empty_route(_frame(observer), observer, state, 100)
    world.end_planning()
    original_queries = state.escape_search[1]
    for tick in range(1, 9):
        moving = replace(goal, position=(goal.position[0] + (4. if tick % 2 else -.001),
                                         goal.position[1], goal.position[2]))
        brain._set_goal(state, moving, observer.position, 100 + tick)
        if terrain_changes:
            world.apply(WorldDelta(map_epoch=1, topology_version=tick + 1,
                                   changed_cells=(VoxelChange(400 + tick, 400, 238, True),)))
        result = _navigate(world, brain, observer, state, moving, 100 + tick)
        if tick < 8:
            assert result.debug_role.endswith(":planning_wait")
            assert state.escape_search[1] is original_queries
            assert state.escape_search[2] == tick + 1
    assert state.escape_search is None and state.escape_attempts == 1
    assert state.escape_allow_water and state.escape_goal is not None
    assert state.route_topology_version == world.topology_version
    assert world.planning_budget.snapshot()["granted"] == 9


@pytest.mark.parametrize("changed", ["actor", "target", "role"])
def test_escape_restarts_for_material_actor_target_or_ownership_changes(monkeypatch, changed):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    brain._set_goal(state, goal, observer.position, 100)
    world.begin_planning((1, 1), 100)
    brain._escape_empty_route(_frame(observer), observer, state, 100)
    world.end_planning()
    original_queries = state.escape_search[1]
    if changed == "actor":
        observer = replace(observer, position=(12.5, 10.5, 235.75))
    elif changed == "target":
        goal = replace(goal, position=(50.5, 10.5, 235.75))
    else:
        goal = replace(goal, key=("critical_objective",), role="ctf_capture")
    brain._set_goal(state, goal, observer.position, 101)
    world.begin_planning((1, 1), 101)
    brain._escape_empty_route(_frame(observer), observer, state, 101)
    world.end_planning()
    assert state.escape_search is None or state.escape_search[1] is not original_queries


@pytest.mark.parametrize("renewed", [False, True])
def test_resumed_escape_queries_use_current_terrain_and_new_edge_exclusions(monkeypatch, renewed):
    world, brain, observer, state, goal = _setup(monkeypatch, island=True)
    brain._set_goal(state, goal, observer.position, 100)
    new_edge = ((10, 10, 238), (10, 9, 238))
    if renewed:
        state.blocked_edges[new_edge] = 100.5
        state.blocked_edge_since[new_edge] = 90.
    world.begin_planning((1, 1), 100)
    brain._escape_empty_route(_frame(observer), observer, state, 100)
    world.end_planning()
    next_candidate = state.escape_search[1][1][0]
    if renewed:
        context, queries, index = state.escape_search
        # A late recovery query intentionally relaxes an old rejected edge.
        # Rejection of that same edge again must renew its protection.
        candidate, water, profile, _ = queries[index]
        state.escape_search = (context, ((candidate, water, profile, frozenset()),), 0)
    state.blocked_edges[new_edge] = 110.
    state.blocked_edge_since[new_edge] = 101.
    world.apply(WorldDelta(map_epoch=1, topology_version=2,
                           changed_cells=(VoxelChange(10, 9, 238, True),)))
    calls, actual_plan = [], SimpleVoxelWorld._plan
    def record(current, start, target, **arguments):
        calls.append((target, arguments["blocked_edges"], current.topology_version))
        return actual_plan(current, start, target, **arguments)
    monkeypatch.setattr(SimpleVoxelWorld, "_plan", record)
    _navigate(world, brain, observer, state, goal, 101)
    assert calls and calls[0] == (next_candidate, frozenset({new_edge}), 2)


def test_behavior_metrics_are_bounded_copies_not_worker_owned_dictionaries(monkeypatch):
    world, brain, *_ = _setup(monkeypatch)
    for index in range(300):
        brain.cooperative.teams.event("tasks_started", index, "test", float(index))
    snapshot = behavior_metrics_snapshot(brain)
    assert snapshot["counters"] == {"tasks_started": 300}
    assert len(snapshot["events"]) == 256
    snapshot["events"][0]["reason"] = "caller mutation"
    snapshot["counters"]["tasks_started"] = 0
    assert brain.cooperative.teams.metrics["tasks_started"] == 300
    assert brain.cooperative.teams.events[0]["reason"] == "test"


def test_completed_corridor_survives_deferred_join_then_executes(monkeypatch):
    world, brain, observer, state, goal = _setup(monkeypatch)
    brain._set_goal(state, goal, observer.position, 100)
    search = SurfaceCorridorSearch(bytes([100]) * 1024, 32, 32,
                                   10 * 32 + 12, 10 * 32 + 30)
    state.corridor_search = _BudgetedCorridorSearch(world, search)
    world.begin_planning((1, 1), 100)
    assert brain._corridor_segment_goal(state, observer, goal, 100) is None
    world.end_planning()
    assert search.done and search.path
    assert state.corridor_search is not None and state.corridor == ()
    world.begin_planning((1, 1), 101)
    target = brain._corridor_segment_goal(state, observer, goal, 101)
    world.end_planning()
    # One detailed plan is sent a straight section along the corridor, not to its next corner.
    assert target in search.path[1:] and math.dist(target, search.path[0]) <= 8.0
    assert state.corridor_search is None and state.corridor == search.path
    result = _navigate(world, brain, observer, state, goal, 102)
    assert result.movement.direction[0] > 0
    assert not state.blocked_edges
    assert world.planning_budget.snapshot()["granted"] == 3


@pytest.mark.parametrize("stale_route", (False, True))
def test_long_coarse_search_reserves_next_grant_for_missing_local_route(monkeypatch, stale_route):
    world, brain, observer, state, goal = _setup(monkeypatch)
    goal = replace(goal, position=(400.5, 400.5, 97.75))
    brain._set_goal(state, goal, observer.position, 100)
    if stale_route:
        state.route = (RouteStep((12.5, 10.5, 97.75), MovementAffordance.WALK),)
        state.route_topology_version = 0  # Retained route needs terrain revalidation.
    # An inaccessible map-wide destination needs many512-node slices, while
    # the actor's immediate ground route remains clear and executable.
    supports = bytearray([100]) * (512 * 512)
    for y in range(512):
        supports[y * 512 + 100] = 239
    search = SurfaceCorridorSearch(bytes(supports), 512, 512,
                                   10 * 512 + 10, 400 * 512 + 400)
    state.corridor_search = _BudgetedCorridorSearch(world, search)
    first = _navigate(world, brain, observer, state, goal, 100)
    # Without a route there is nothing to walk: wait. A retained route is kept
    # in use while its revalidation queues behind the coarse search.
    assert first.debug_role.endswith(":planning_wait") is not stale_route
    assert search.expansions == 512 and not search.done
    # Another observer owns this turn. Retain the local reservation; denied
    # admission must not let coarse search steal the following grant again.
    world.planning_budget.try_acquire((2, 1), 101)
    second = _navigate(world, brain, observer, state, goal, 101)
    assert second.debug_role.endswith(":planning_wait") is not stale_route
    if stale_route:
        assert first.movement.direction[0] > 0 and second.movement.direction[0] > 0
        assert state.route_topology_version == 0
    recovered = _navigate(world, brain, observer, state, goal, 102)
    assert math.hypot(*recovered.movement.direction[:2]) > .9
    assert state.route_topology_version == world.topology_version
    assert search.expansions == 512 and not search.done
    assert not state.blocked_edges
    assert world.planning_budget.snapshot()["granted"] == 3
    # The local route keeps moving while retained guidance resumes its work.
    moving = replace(observer, position=(11.5, 10.5, 97.75))
    following = _navigate(world, brain, moving, state, goal, 103)
    assert math.hypot(*following.movement.direction[:2]) > .9
    assert search.expansions == 1024


def test_thread_behavior_snapshot_cannot_be_mutated_by_a_reader():
    from server.bot_ai.thread_supervisor import AIThreadSupervisor
    supervisor = AIThreadSupervisor()
    supervisor._behavior_metrics = {"counters": {"tasks_started": 1},
                                    "events": ({"reason": "original"},)}
    report = supervisor.behavior_metrics()
    report["counters"]["tasks_started"] = 99
    report["events"][0]["reason"] = "reader"
    assert supervisor.behavior_metrics()["counters"]["tasks_started"] == 1
    assert supervisor.behavior_metrics()["events"][0]["reason"] == "original"


def test_a_fallback_chain_runs_in_one_decision_when_credit_is_free(monkeypatch):
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(24))
    world, brain, observer, state, goal = _setup(monkeypatch, island=True, world=world)
    # Dry route, lateral detour and the swim are three small searches. With
    # credit nobody is waiting for they no longer take three decisions.
    result = _navigate(world, brain, observer, state, goal)
    assert not result.debug_role.endswith(":planning_wait")
    assert any(step.affordance is MovementAffordance.SWIM for step in state.route)
    assert world.planning_budget.snapshot()["granted"] == 3


class _NoWayOut(SimpleVoxelWorld):
    """Every search comes back empty; counts the searches actually run."""

    searches = 0

    def _plan(self, start, goal, **arguments):
        type(self).searches += 1
        return RoutePlan((), False, 0)


def test_a_bot_with_no_way_on_does_not_search_the_same_dead_end_every_decision(monkeypatch):
    monkeypatch.setattr(_NoWayOut, "searches", 0)
    world, brain, observer, state, goal = _setup(
        monkeypatch, world=_NoWayOut(planning_budget=PlanningBudget(64)))
    for tick in range(7):
        result = _navigate(world, brain, observer, state, goal, 100 + tick / 8)
        assert not result.debug_role.endswith(":planning_wait")
    # Dry, detour and wet once; after that only each new lateral detour is a
    # new question. Without the memory this is seventeen searches.
    assert _NoWayOut.searches == 5
    assert not state.blocked_edges
    state.escape_retry_at = math.inf  # Keep the separate escape search out of the count.
    # The answer is trusted for a few seconds, not for ever...
    _navigate(world, brain, observer, state, goal, 104)
    assert _NoWayOut.searches == 7
    # ...and never across a change of terrain or a step to somewhere else.
    world.apply(WorldDelta(map_epoch=1, topology_version=2, changed_cells=()))
    _navigate(world, brain, observer, state, goal, 104.125)
    assert _NoWayOut.searches == 9
    moved = replace(observer, position=(11.5, 10.5, 97.75))
    _navigate(world, brain, moved, state, goal, 104.25)
    assert _NoWayOut.searches == 11


def _coarse_dead_end():
    """A 64x64 plain cut by water the coarse search cannot cross."""
    supports = bytearray([100]) * (64 * 64)
    for y in range(64):
        supports[y * 64 + 20] = 239
    return SurfaceCorridorSearch(bytes(supports), 64, 64, 10 * 64 + 10, 10 * 64 + 40)


def test_a_map_wide_search_that_found_nothing_is_not_repeated_from_the_same_ground(monkeypatch):
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(64))
    world, brain, observer, state, goal = _setup(monkeypatch, world=world)
    goal = replace(goal, position=(40.5, 10.5, 97.75))
    brain._set_goal(state, goal, observer.position, 100)
    state.route = (RouteStep((12.5, 10.5, 97.75), MovementAffordance.WALK),)
    state.route_topology_version = world.topology_version
    started = []

    def begin(_world, start, _goal, *, blocked_edges):
        started.append(start)
        return _coarse_dead_end()

    monkeypatch.setattr(SimpleVoxelWorld, "begin_corridor", begin)

    def guide(body, target, now):
        state.dead_end, state.dead_end_retry_at = True, 0.0
        world.begin_planning((1, 1), now)
        try:
            return brain._corridor_segment_goal(state, body, target, now)
        finally:
            world.end_planning()

    now = 100.0
    while not started or state.corridor_search is not None:
        assert guide(observer, goal, now) is None
        now += 0.125
    assert len(started) == 1 and state.corridor_futile
    # Asking again from the same ground would flood the same bank again.
    for later in (now + 5, now + 20, now + 60):
        guide(replace(observer, position=(14.5, 12.5, 97.75)), goal, later)
    assert len(started) == 1
    # Somewhere else, another target, or much later is a new question.
    guide(replace(observer, position=(10.5, 40.5, 97.75)), goal, now + 61)
    assert len(started) == 2
    state.corridor_search = None
    guide(observer, replace(goal, position=(40.5, 60.5, 97.75)), now + 62)
    assert len(started) == 3
    state.corridor_search = None
    state.corridor_futile.clear()
    guide(observer, goal, now + 63)
    assert len(started) == 4
    while state.corridor_search is not None:
        now += 0.125
        guide(observer, goal, now + 63)
    guide(observer, goal, now + 100)
    assert len(started) == 4
    guide(observer, goal, now + 200)
    assert len(started) == 5


def _long_search(world):
    supports = bytearray([100]) * (512 * 512)
    for y in range(512):
        supports[y * 512 + 100] = 239
    search = SurfaceCorridorSearch(bytes(supports), 512, 512,
                                   10 * 512 + 10, 400 * 512 + 400)
    return search, _BudgetedCorridorSearch(world, search)


@pytest.mark.parametrize("spare, slices", [(False, (512, 512, 512, 512, 1024)),
                                            (True, (512, 1024, 1536, 2048, 2560))])
def test_map_wide_guidance_runs_on_spare_credit_and_is_never_starved(monkeypatch, spare, slices):
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(64))
    world, brain, observer, state, goal = _setup(monkeypatch, world=world)
    if not spare:
        # Teammates are waiting on every credit the batch has.
        monkeypatch.setattr(world.planning_budget, "spare", lambda *_args: False)
    goal = replace(goal, position=(400.5, 400.5, 97.75))
    brain._set_goal(state, goal, observer.position, 100)
    # The bot has ground to walk: its sixty-four slices of guidance can wait.
    state.route = (RouteStep((12.5, 10.5, 97.75), MovementAffordance.WALK),)
    state.route_topology_version = world.topology_version
    search, state.corridor_search = _long_search(world)
    seen = []
    for tick in range(5):
        now = 100 + tick / 8
        world.begin_planning((1, 1), now)
        brain._corridor_segment_goal(state, observer, goal, now)
        world.end_planning()
        seen.append(search.expansions)
    # With nothing spare the search asks for a slice every half second, as
    # an ordinary request that waits its turn like any other. With credit to
    # spare it takes one every decision.
    assert tuple(seen) == slices
    if not spare:
        assert world.planning_budget.snapshot()["requested"] == 2


@pytest.mark.parametrize("waiting, extended", [(True, False), (False, True)])
def test_looking_further_ahead_never_takes_a_waiting_teammates_credit(
        monkeypatch, waiting, extended):
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(8))
    world, brain, observer, state, goal = _setup(monkeypatch, world=world)
    goal = replace(goal, position=(60.5, 10.5, 97.75))
    brain._set_goal(state, goal, observer.position, 100)
    state.route = (RouteStep((14.5, 10.5, 97.75), MovementAffordance.WALK),)
    state.route_topology_version = world.topology_version
    budget = world.planning_budget
    if waiting:
        # One credit a decision: observer 3 had the last, observer 2 wants the next.
        assert budget.try_acquire((3, 1), 99.875) and budget.try_acquire((2, 1), 99.875) is None
    asked = budget.snapshot()["requested"]
    frame = replace(_frame(observer, created_at=100.0), topology_version=world.topology_version)
    world.begin_planning((1, 1), 100.0)
    brain._extend_route(frame, observer, state, goal, 100.0)
    world.end_planning()
    assert (len(state.route) > 1) is extended
    assert budget.snapshot()["requested"] - asked == (1 if extended else 0)
    assert not waiting or budget.would_grant((2, 1), 100.0)


class _WalledGround:
    def get_solid(self, x, y, z):
        return z >= 100 or (y == 13 and 90 <= z < 100)


def test_a_waiting_bot_walks_on_toward_its_goal_when_its_old_heading_meets_a_wall(monkeypatch):
    world, brain, observer, state, goal = _setup(monkeypatch)
    world._vxl = _WalledGround()
    state.navigation_window_at = 100.0
    state.travel_heading = (0.0, 1.0, 0.0)
    movement = brain._coast(observer, state, goal, 100.0)
    assert movement.direction[0] > 0.9 and abs(movement.direction[1]) < 0.1
    # The old heading is still preferred where it is open...
    state.travel_heading = (0.0, -1.0, 0.0)
    assert brain._coast(observer, state, goal, 100.0).direction[1] < -0.9
    # ...and nothing is invented when neither way is plain walking.
    blocked = replace(goal, position=(10.5, 30.5, 97.75))
    state.travel_heading = (0.0, 1.0, 0.0)
    assert brain._coast(observer, state, blocked, 100.0).direction == (0.0, 0.0, 0.0)


def test_an_adopted_corridor_starts_on_fresh_clocks_instead_of_taking_the_blame(monkeypatch):
    """Map-wide guidance is asked for because the body got nowhere.

    Its progress clocks are therefore expired, or about to be, when the
    answer arrives. They used to fire on the corridor's first step: that
    edge was excluded for a minute and the corridor dropped with it.
    """
    world, brain, observer, state, goal = _setup(monkeypatch)
    brain._set_goal(state, goal, observer.position, 90)
    brain.skills._slot(observer).anchor_at = 90
    search = SurfaceCorridorSearch(bytes([100]) * 1024, 32, 32,
                                   10 * 32 + 12, 10 * 32 + 30)
    state.corridor_search = _BudgetedCorridorSearch(world, search)
    for now in (100, 101):
        world.begin_planning((1, 1), now)
        brain._corridor_segment_goal(state, observer, goal, now)
        world.end_planning()
    assert state.corridor == search.path
    assert state.navigation_progress_at == state.navigation_window_at == 101
    assert brain.skills._slot(observer).anchor_at == 101
    result = _navigate(world, brain, observer, state, goal, 102)
    assert result.movement.direction[0] > 0
    assert state.corridor == search.path and not state.blocked_edges
