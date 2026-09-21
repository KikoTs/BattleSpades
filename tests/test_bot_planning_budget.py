"""Active planner admission, fairness and bounded work regressions."""

from __future__ import annotations

from collections import Counter
import math
import queue
import time

import pytest

from server.bot_ai.planning_budget import (
    MAX_EXPANSIONS_PER_JOB, MAX_PENDING_OBSERVERS, MAX_PROFILE_SAMPLES,
    PlanningBudget, PlanningJob,
)
from server.bot_ai.simple_navigation import SimpleVoxelWorld, _BudgetedCorridorSearch
from server.bot_ai.surface_corridor import SurfaceCorridorSearch
from server.bot_ai.messages import MapSnapshot, MovementAffordance
from server.dig_profiles import navigation_dig_profile


@pytest.mark.parametrize("fleet", [12, 24, 48])
def test_fixed_order_fleets_share_rate_without_low_id_starvation(fleet):
    budget = PlanningBudget(24, decision_hz=8)
    served = Counter()
    last_service = {}
    longest_gap = 0.0
    for tick in range(160):
        now = tick / 8.0
        grants = 0
        for bot in range(fleet):
            job = budget.try_acquire((bot, 1), now)
            if job is not None:
                grants += 1
                served[bot] += 1
                if bot in last_service:
                    longest_gap = max(longest_gap, now - last_service[bot])
                last_service[bot] = now
                # Even a failed route's same-frame fallback cannot monopolize
                # the worker by repeatedly spending this observer's allowance.
                assert budget.try_acquire((bot, 1), now) is None
        assert grants <= 3
        assert budget.granted <= 3 + math.floor(now * 24 + 1e-8)
    assert len(served) == fleet
    assert max(served.values()) - min(served.values()) <= 1
    assert longest_gap <= math.ceil(fleet / 3) / 8.0


def test_idle_burst_missing_observer_and_generation_reset_are_bounded():
    budget = PlanningBudget(1, decision_hz=8)
    assert budget.try_acquire((1, 1), 0.0)
    assert budget.try_acquire((2, 1), 0.0) is None
    assert budget.try_acquire((3, 1), 0.0) is None
    # Observer2 vanished; observer3 renews its place until the absent head expires.
    for tick in range(1, 9):
        assert budget.try_acquire((3, 1), tick / 8.0) is None
    assert budget.try_acquire((3, 1), 1.125)
    assert budget.snapshot()["expired"] == 1
    assert budget.try_acquire((4, 1), 100.0)
    assert budget.try_acquire((5, 1), 100.0) is None
    budget.reset()
    assert budget.snapshot()["pending"] == 0
    assert budget.try_acquire((5, 2), 0.0)


def test_slow_batches_keep_live_fifo_order_instead_of_restarting_at_low_ids():
    budget = PlanningBudget(24, decision_hz=8)
    observers = [(bot, 1) for bot in range(48)]
    served = Counter()
    for batch in range(32):
        now = batch * 2.0  # Longer than expiry; represents an overloaded owner.
        budget.refresh_waiters(observers, now)
        for observer in observers:
            if budget.try_acquire(observer, now) is not None:
                served[observer] += 1
    assert len(served) == 48
    assert set(served.values()) == {2}


def test_bot_switching_to_combat_releases_its_unused_pending_turn():
    world = _world()
    assert _route(world).steps
    assert _route(world, (2, 1)).deferred
    world.begin_planning((2, 1), 0.125)
    world.end_planning(cancel_unused=True)
    assert world.planning_budget.snapshot()["pending"] == 0
    assert _route(world, (3, 1), 1.0).steps


def test_request_memory_and_profile_samples_have_hard_limits():
    budget = PlanningBudget(1)
    for bot in range(MAX_PENDING_OBSERVERS * 4):
        budget.try_acquire((bot, 1), 0.0)
    assert budget.snapshot()["pending"] == MAX_PENDING_OBSERVERS
    assert budget.snapshot()["queue_full"] > 0
    for duration in range(MAX_PROFILE_SAMPLES * 2):
        budget.finish(PlanningJob(searches=1, expansions=7), duration / 1000.0)
    report = budget.snapshot()
    assert report["samples"] == MAX_PROFILE_SAMPLES
    assert report["work_p95_ms"] == 499.0
    assert report["work_max_ms"] == 511.0
    assert report["expansions"] == MAX_PROFILE_SAMPLES * 2 * 7


@pytest.mark.parametrize("rate", [0, -1, math.nan, math.inf])
def test_invalid_rates_are_rejected(rate):
    with pytest.raises(ValueError):
        PlanningBudget(rate)


class _Ground:
    def __init__(self, wall=False):
        self.wall = wall

    def get_solid(self, x, y, z):
        return z >= 100 or (self.wall and x == 11 and 90 <= z < 100)


def _world(rate=1, wall=False):
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(rate))
    world._vxl = _Ground(wall)
    return world


def _route(world, observer=(1, 1), now=0.0, breach=False):
    world.begin_planning(observer, now)
    try:
        abilities = {MovementAffordance.WALK}
        if breach:
            abilities.add(MovementAffordance.BREACH)
        return world.plan((10.5, 10.5, 97.75), (30.5, 10.5, 97.75),
                          abilities=frozenset(abilities),
                          dig_profile=navigation_dig_profile(3) if breach else None)
    finally:
        world.end_planning()


def test_real_route_deferral_does_no_geometry_work_and_retries_successfully():
    world = _world()
    first = _route(world)
    assert first.steps and not first.deferred
    work = world.planning_budget.snapshot()["expansions"]
    denied = _route(world, (2, 1))
    assert denied.deferred and denied.steps == () and denied.expansions == 0
    assert world.planning_budget.snapshot()["expansions"] == work
    assert _route(world, (2, 1), now=1.0).steps
    world.load(MapSnapshot(2, 0, b"", "tdm", "budget-reset"))
    assert world.planning_budget.snapshot()["pending"] == 0


def test_breach_fallback_uses_one_permit_with_two_bounded_searches():
    world = _world(wall=True)
    result = _route(world, breach=True)
    report = world.planning_budget.snapshot()
    assert not result.deferred
    assert result.steps and any(step.breach for step in result.steps)
    assert report["granted"] == 1
    assert report["searches"] == 2
    assert 256 < report["expansions"] <= MAX_EXPANSIONS_PER_JOB


def test_corridor_slices_share_route_rate_and_keep_frontier_when_deferred():
    world = _world()
    # A long one-cell-wide search needs several ordinary512-node slices.
    search = SurfaceCorridorSearch(bytes([100]) * 1600, 1600, 1, 0, 1599)
    guarded = _BudgetedCorridorSearch(world, search)
    world.begin_planning((1, 1), 0.0)
    guarded.advance()
    assert search.expansions == 512 and not guarded.done
    before = (list(search.frontier), dict(search.costs), search.expansions)
    guarded.advance()
    assert guarded.deferred and not guarded.done
    assert (search.frontier, search.costs, search.expansions) == before
    assert _route(world, (2, 1), 0.0).deferred
    # The waiting ordinary route is served before another slice frombot1.
    assert _route(world, (2, 1), 1.0).steps
    world.begin_planning((1, 1), 2.0)
    guarded.advance()
    assert not guarded.deferred and search.expansions == 1024
    assert world.planning_budget.snapshot()["granted"] == 3


class _RoutingBrain:
    results = []

    def __init__(self, world, **_kwargs):
        self.world = world

    def reset_for_map(self, _epoch):
        self.world._vxl = _Ground()

    def reset_bot(self, *_args):
        raise AssertionError("budget denial must not reset an observer")

    def decide(self, frame):
        plan = self.world.plan((10.5, 10.5, 97.75), (30.5, 10.5, 97.75),
                               abilities=frozenset({MovementAffordance.WALK}))
        self.results.append((frame.observer_id, plan.deferred,
                             self.world.planning_budget.requests_per_second))
        return None


def _frame(bot):
    from server.bot_ai.messages import PerceptionFrame
    return PerceptionFrame(frame_id=bot, map_epoch=1, mode_epoch=1,
                           topology_version=0, observer_id=bot, observer_generation=1,
                           created_at=10.0, mode_id="tdm", players=())


def test_thread_backend_enforces_configured_rate_through_real_dispatch(monkeypatch):
    from server.bot_ai import thread_supervisor
    monkeypatch.setattr(thread_supervisor, "SimpleBotBrain", _RoutingBrain)
    monkeypatch.setattr(_RoutingBrain, "results", [])
    supervisor = thread_supervisor.AIThreadSupervisor(path_requests_per_second=1)
    supervisor.start(MapSnapshot(1, 0, b"", "tdm", "budget-thread"))
    try:
        supervisor.submit_frame(_frame(1))
        supervisor.submit_frame(_frame(2))
        deadline = time.monotonic() + 2.0
        while supervisor.planning_metrics().get("requested", 0) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        assert [row[1] for row in _RoutingBrain.results].count(True) == 1
        assert all(row[2] == 1.0 for row in _RoutingBrain.results)
        assert supervisor.planning_metrics()["granted"] == 1
        assert supervisor.planning_metrics()["samples"] == 1
    finally:
        supervisor.close()


def test_process_entry_enforces_same_rate_and_observer_context(monkeypatch):
    from server.bot_ai import simple_worker
    from server.bot_ai.messages import WorkerShutdown
    monkeypatch.setattr(simple_worker, "SimpleBotBrain", _RoutingBrain)
    monkeypatch.setattr(_RoutingBrain, "results", [])

    class Input:
        batches = [
            [MapSnapshot(1, 0, b"", "tdm", "budget-process"), _frame(1), _frame(2)],
            [WorkerShutdown()],
        ]
        current = []

        def get(self, **_kwargs):
            self.current = self.batches.pop(0)
            return self.current.pop(0)

        def get_nowait(self):
            if not self.current:
                raise queue.Empty
            return self.current.pop(0)

    simple_worker.run_worker(Input(), queue.Queue(), path_requests_per_second=1)
    assert _RoutingBrain.results == [(1, False, 1.0), (2, True, 1.0)]
