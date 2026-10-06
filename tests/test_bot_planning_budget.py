"""Active planner admission, fairness and bounded work regressions."""

from __future__ import annotations

from collections import Counter
import math
import queue
import time

import pytest

from server.bot_ai.planning_budget import (
    MAX_DECISION_JOBS, MAX_EXPANSIONS_PER_JOB, MAX_PENDING_OBSERVERS, MAX_PROFILE_SAMPLES,
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


def test_the_request_rate_follows_the_roster_and_never_drops_below_the_configured_floor():
    budget = PlanningBudget(24, decision_hz=8)
    budget.scale_for(2)
    assert budget.requests_per_second == 24  # a small roster keeps the configured rate
    budget.scale_for(10)
    assert budget.requests_per_second == 60 and budget.burst == math.ceil(60 / 8)
    budget.scale_for(0)
    assert budget.requests_per_second == 24


def test_would_grant_predicts_admission_without_spending_it():
    budget = PlanningBudget(8, decision_hz=8)
    assert budget.would_grant((1, 1), 0.0) and budget.would_grant((1, 1), 0.0)
    assert budget.try_acquire((1, 1), 0.0) is not None
    assert not budget.would_grant((2, 1), 0.0)  # this instant's allowance is spent
    assert budget.snapshot()["granted"] == 1


def _work(budget, observer, now, expansions=MAX_EXPANSIONS_PER_JOB):
    """Run one job of the given size, as the planner does; False when denied."""
    job = budget.try_acquire(observer, now)
    if job is None:
        return False
    job.expansions = expansions
    budget.finish(job, 0.0)
    return True


def test_a_waiter_that_is_not_asking_does_not_block_the_bots_behind_it():
    budget = PlanningBudget(24, decision_hz=8)
    for bot in (1, 2, 3):
        assert budget.try_acquire((bot, 1), 0.0)
    assert budget.try_acquire((4, 1), 0.0) is None
    assert budget.try_acquire((5, 1), 0.0) is None
    # Observer 4 is alive but has not reached its next decision. A head-of-line
    # queue denied everyone behind it until it asked again; with three credits
    # in hand there is one for it and one for observer 5.
    assert budget.try_acquire((5, 1), 0.125)
    assert budget.try_acquire((4, 1), 0.125)


def test_the_longest_waiter_owns_the_next_credit_when_credit_is_short():
    budget = PlanningBudget(8, decision_hz=8)
    assert budget.try_acquire((1, 1), 0.0)
    assert budget.try_acquire((2, 1), 0.0) is None
    assert budget.try_acquire((3, 1), 0.0) is None
    # The one credit that accrued is observer 2's, whoever asks first.
    assert budget.try_acquire((3, 1), 0.125) is None
    assert budget.try_acquire((2, 1), 0.125)
    assert budget.try_acquire((3, 1), 0.25)
    assert budget.snapshot()["wait_max_s"] == 0.25


@pytest.mark.parametrize("fleet", [12, 24, 48])
def test_no_bot_waits_long_when_every_bot_wants_a_full_route_every_decision(fleet):
    budget = PlanningBudget(24, decision_hz=8)
    budget.scale_for(fleet)
    served = Counter()
    asked_at = {}
    longest = 0.0
    for tick in range(240):
        now = tick / 8.0
        # Frame order changes from batch to batch in play.
        for bot in ((index + tick * 5) % fleet for index in range(fleet)):
            asked_at.setdefault(bot, now)
            if _work(budget, (bot, 1), now):
                longest = max(longest, now - asked_at.pop(bot))
                served[bot] += 1
    assert len(served) == fleet
    assert longest <= 1.0 and budget.snapshot()["wait_max_s"] <= 1.0
    assert max(served.values()) - min(served.values()) <= 1
    assert budget.expansions <= (budget.burst + 30 * budget.requests_per_second) * 512


def test_short_routes_are_charged_for_the_work_they_did():
    budget = PlanningBudget(8, decision_hz=8)
    assert _work(budget, (1, 1), 0.0, expansions=64)
    # An eighth of the job was used and the rest came back.
    assert not _work(budget, (2, 1), 0.0)
    assert _work(budget, (2, 1), 0.02)
    full = PlanningBudget(8, decision_hz=8)
    assert _work(full, (1, 1), 0.0)
    assert not _work(full, (2, 1), 0.1)
    assert _work(full, (2, 1), 0.125)


def test_work_admitted_never_exceeds_the_rate_however_cheap_the_jobs():
    budget = PlanningBudget(24, decision_hz=8)
    spent = 0
    for tick in range(800):
        now = tick / 100.0
        for bot in range(16):
            size = (17, 64, 200, 512)[(bot + tick) % 4]
            if _work(budget, (bot, 1), now, expansions=size):
                spent += size
        assert spent <= (budget.burst + now * 24) * MAX_EXPANSIONS_PER_JOB + 1e-6
    assert spent > 0.5 * 8 * 24 * MAX_EXPANSIONS_PER_JOB  # and the credit is used, not hoarded


def test_one_decision_chains_its_fallbacks_only_from_credit_nobody_is_waiting_for():
    budget = PlanningBudget(32, decision_hz=8)
    # A route and both its fallbacks in one decision. That is the allowance:
    # a fourth request is refused with credit in hand and joins no queue.
    assert all(_work(budget, (1, 1), 0.0) for _ in range(3))
    assert not _work(budget, (1, 1), 0.0)
    assert budget.snapshot()["pending"] == 0
    assert _work(budget, (2, 1), 0.0)
    assert not _work(budget, (3, 1), 0.0)
    # Two credits accrue. Observer 1 takes one; a second helping would be the
    # credit observer 3 has been waiting for.
    now = 2 / 32
    assert _work(budget, (1, 1), now)
    assert not _work(budget, (1, 1), now)
    assert _work(budget, (3, 1), now)


def test_cheap_searches_share_one_decision_allowance():
    budget = PlanningBudget(64, decision_hz=8)
    done = 0
    while _work(budget, (1, 1), 0.0, expansions=128):
        done += 1
    # Escape candidates are small: nine fit where three full routes would.
    assert done == 9
    assert budget.expansions <= MAX_DECISION_JOBS * MAX_EXPANSIONS_PER_JOB
    assert _work(budget, (1, 1), 0.125, expansions=128)


def test_an_unfinished_job_is_the_only_one_its_observer_holds():
    budget = PlanningBudget(64, decision_hz=8)
    job = budget.try_acquire((1, 1), 0.0)
    assert job is not None and budget.try_acquire((1, 1), 0.0) is None
    budget.finish(job, 0.0)
    budget.finish(job, 0.0)  # A repeated completion hands nothing back twice.
    assert budget.try_acquire((1, 1), 0.0) is not None
    assert budget.snapshot()["granted"] == 2


def test_spare_credit_leaves_half_a_batch_for_bots_without_a_route():
    budget = PlanningBudget(64, decision_hz=8)
    assert budget.burst == 8 and budget.spare((9, 1), 0.0)
    for bot in (1, 2, 3):
        assert budget.try_acquire((bot, 1), 0.0)
    assert budget.spare((9, 1), 0.0)
    assert budget.try_acquire((4, 1), 0.0)
    before = budget.snapshot()
    # Half the batch is gone: background work stops, ordinary requests do not.
    assert not budget.spare((9, 1), 0.0)
    assert budget.would_grant((9, 1), 0.0)
    assert budget.snapshot() == before
    # One job per decision: it is spare until somebody is waiting for it.
    small = PlanningBudget(8, decision_hz=8)
    assert small.spare((1, 1), 0.0)
    assert small.try_acquire((2, 1), 0.0) and small.try_acquire((3, 1), 0.0) is None
    assert not small.spare((1, 1), 0.125) and small.would_grant((3, 1), 0.125)


def test_a_waiting_observer_has_first_call_on_spare_credit():
    budget = PlanningBudget(64, decision_hz=8)
    for bot in range(8):
        assert budget.try_acquire((bot, 1), 0.0)
    assert budget.try_acquire((8, 1), 0.0) is None and budget.try_acquire((9, 1), 0.0) is None
    now = 6 / 64  # Six credits back and two spoken for: half a batch is not left.
    assert not budget.spare((20, 1), now)
    assert budget.spare((20, 1), 7 / 64)
