"""Gameplay-thread glue between Zombie mode and the horde coordinator.

The director calls :meth:`ZombieSiegeService.objectives` from its objective
snapshot (at most ~10 Hz). The service

* reads the live survivors and infected from the mode,
* keeps a structural analysis per survivor fresh with bounded generator jobs
  advanced under a per-call time budget, exactly like the refuge election,
  so a slow analysis spreads over several ticks instead of stalling one:
  the approach flood (how a zombie on foot gets to him, and whether anything
  does), then, for a survivor nothing walks to or who stands well above the
  horde, the minimum collapse cut from ``structure_collapse``,
* asks :class:`HordeCoordinator` for one order per infected bot and
  publishes them as ``zombie_order`` objectives (``carrier_id`` = the zombie,
  ``attacker`` = its target survivor, ``state`` = role code, ``cells`` = the
  voxels to claw).

Knowledge is legal: retail Zombie mode gives the horde the survivor set as
its target list (survivor markers, entombed-survivor reveal, last-survivor
heart). Survivor bots receive nothing from here.

Any failure degrades to "no orders" (the policy's old nearest-survivor hunt)
and is logged at most once a minute; it never raises into the tick.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import math
import time
from typing import Callable, Iterable

import shared.constants as C

from .horde_strategy import (
    ELEVATED_RISE,
    MOVE_BLOCKS,
    RUN_SPEED,
    HordeCoordinator,
    HordeMember,
    HordeOrder,
    SiegeInfo,
    SurvivorTarget,
    pile_count,
)
from .messages import ObjectiveSnapshot
from .structure_collapse import (
    PLAYER_SUPPORT_OFFSET,
    Approach,
    horde_floor,
    iter_approach,
    iter_plan_collapse,
    support_cells,
)

logger = logging.getLogger(__name__)

ZOMBIE_ORDER_KIND = "zombie_order"
# Gameplay-thread time per objective snapshot spent on structural analysis.
SIEGE_BUDGET_SECONDS = 0.001
ISOLATION_TTL = 2.5
# Survivors with no hunter this close need no structural analysis yet.
ANALYSIS_RANGE = 48.0
PLAN_TTL = 6.0
PLAN_MOVE_TOLERANCE = 2.0
# Only a survivor who has stayed put this long is analysed: one on the run
# is simply hunted, and a flood restarted at every stride never finishes.
SETTLED_SECONDS = 0.75
SETTLED_MOVE = 1.5
# A walkable footing above the horde gets a collapse cut only while no
# hunter within PLAN_HUNTER_RANGE walks up to it in PLAN_WORTH_SECONDS, or
# the hunters sent up are getting nowhere.
PLAN_HUNTER_RANGE = 24.0
PLAN_WORTH_SECONDS = 4.0
# Floors explored round a survivor: about seventeen blocks of open ground,
# and twice the floors for one standing above the horde, whose way up
# (stairs round the back, a ramp) starts well away from his feet.
APPROACH_CELLS = 600
APPROACH_CELLS_ELEVATED = 1200
_BUILD_TOOLS = frozenset({int(C.BLOCK_TOOL), int(getattr(C, "ZOMBIE_PREFAB_TOOL", 28))})
_DIG_TOOLS = frozenset(int(t) for t in getattr(C, "ALL_MELEE_WEAPONS", ()))


@dataclass(slots=True)
class _Analysis:
    position: tuple[float, float, float]
    info: SiegeInfo
    isolation_at: float
    plan_at: float
    # Whether the collapse cut is worth keeping fresh: nothing walks in, or
    # the footing stands well above the horde.
    wants_plan: bool = False


@dataclass(slots=True)
class _Job:
    survivor_id: int
    position: tuple[float, float, float]
    stage: str  # "approach" | "plan"
    generator: object
    floor_z: int = 0


class ZombieSiegeService:
    """Bounded, fail-safe horde orders for the director."""

    def __init__(self, *, budget_seconds: float = SIEGE_BUDGET_SECONDS,
                 clock: Callable[[], float] | None = None) -> None:
        self.coordinator = HordeCoordinator()
        self.budget_seconds = float(budget_seconds)
        # Looked up now, not at import: an accelerated harness replaces the
        # clock before the director creates this service.
        self._clock = clock if clock is not None else time.monotonic
        self._epoch: object = None
        self._analyses: dict[int, _Analysis] = {}
        self._job: _Job | None = None
        self._queue: deque[int] = deque()
        # Per survivor: where he was last seen to move from, and when.
        self._settled: dict[int, tuple[tuple[float, float, float], float]] = {}
        self._last_failure_log = -math.inf
        self.last_orders: dict[int, HordeOrder] = {}
        self.stats = {
            "analyses": 0, "plans": 0, "plans_found": 0,
            "isolated": 0, "max_pile": 0, "orders": 0,
        }

    def reset(self) -> None:
        self.coordinator.reset()
        self._analyses.clear()
        self._job = None
        self._queue.clear()
        self._settled.clear()
        self.last_orders = {}

    # ------------------------------------------------------------------

    def objectives(self, mode, world, *, now: float | None = None,
                   working: Callable[[int], bool] | None = None) -> list[ObjectiveSnapshot]:
        """``working(player_id)``: did that bot's claw land in the last moments?"""

        try:
            return self._objectives(mode, world, self._clock() if now is None else now,
                                    working)
        except Exception:  # noqa: BLE001 - bot planning must never stall the tick
            if self._clock() - self._last_failure_log >= 60.0:
                self._last_failure_log = self._clock()
                logger.exception("zombie siege planning failed; horde falls back to hunting")
            self.reset()
            return []

    def _objectives(self, mode, world, now: float,
                    working: Callable[[int], bool] | None = None) -> list[ObjectiveSnapshot]:
        if not hasattr(mode, "phase") or not hasattr(mode, "_living_survivors"):
            return []
        from modes.zombie import ZOMBIE_TEAM, ZombiePhase

        epoch = (id(mode), getattr(world, "map_name", ""))
        phase = getattr(mode, "phase", None)
        if epoch != self._epoch or phase is not ZombiePhase.ACTIVE:
            if epoch != self._epoch or self._analyses or self.last_orders:
                self.reset()
            self._epoch = epoch
            if phase is not ZombiePhase.ACTIVE:
                return []
        survivors = [
            SurvivorTarget(int(p.id), tuple(float(v) for v in p.position))
            for p in mode._living_survivors()
        ]
        zombies = [
            HordeMember(
                int(p.id),
                tuple(float(v) for v in p.position),
                is_bot=bool(getattr(p, "is_bot", False)),
                can_dig=bool(_DIG_TOOLS & set(_loadout(p))),
                can_build=bool(_BUILD_TOOLS & set(_loadout(p))),
                working=bool(working is not None and working(int(p.id))),
            )
            for p in mode._zombies()
            if bool(getattr(p, "alive", False)) and bool(getattr(p, "spawned", False))
        ]
        if not survivors or not any(z.is_bot for z in zombies):
            self.last_orders = {}
            return []
        solid = getattr(world, "get_solid", None)
        if not callable(solid):
            solid = None
        if solid is not None:
            self._advance_analysis(solid, survivors, zombies, now)
        sieges = {
            sid: analysis.info for sid, analysis in self._analyses.items()
            if any(s.player_id == sid for s in survivors)
        }
        orders = self.coordinator.plan(zombies, survivors, sieges, now, solid=solid)
        self.last_orders = orders
        self.stats["orders"] = len(orders)
        self.stats["max_pile"] = max(self.stats["max_pile"], self.coordinator.metrics.max_pile)
        team = int(ZOMBIE_TEAM)
        return [
            ObjectiveSnapshot(
                ZOMBIE_ORDER_KIND,
                team,
                tuple(float(v) for v in order.goal),
                carrier_id=int(order.zombie_id),
                state=int(order.role_code),
                cells=tuple(order.cells),
                attacker=int(order.target_id),
                progress=float(order.stuck_level),
            )
            for _, order in sorted(orders.items())
        ]

    # ------------------------------------------------------------------

    def _advance_analysis(self, solid, survivors, zombies, now: float) -> None:
        alive = {s.player_id: s for s in survivors}
        for sid in tuple(self._analyses):
            if sid not in alive:
                self._analyses.pop(sid, None)
        for sid in tuple(self._settled):
            if sid not in alive:
                self._settled.pop(sid, None)
        for sid, survivor in alive.items():
            seen = self._settled.get(sid)
            if seen is None or math.dist(seen[0], survivor.position) > SETTLED_MOVE:
                self._settled[sid] = (survivor.position, now)
        if self._job is not None and self._job.survivor_id not in alive:
            self._job = None
        hunters = [z.position for z in zombies]
        if self._job is None:
            self._job = self._next_job(solid, alive, hunters, now)
        deadline = time.perf_counter() + self.budget_seconds
        while self._job is not None:
            job = self._job
            try:
                while True:
                    next(job.generator)
                    if time.perf_counter() >= deadline:
                        return
            except StopIteration as done:
                self._finish(job, done.value, solid, hunters, now)
            self._job = self._next_job(solid, alive, hunters, now)
            if time.perf_counter() >= deadline:
                return

    def _next_job(self, solid, alive, hunters, now: float) -> _Job | None:
        if not self._queue:
            self._queue.extend(sorted(alive))
        for _ in range(len(self._queue)):
            sid = self._queue.popleft()
            survivor = alive.get(sid)
            if survivor is None:
                continue
            if not any(math.dist(h, survivor.position) <= ANALYSIS_RANGE for h in hunters):
                self._analyses.pop(sid, None)
                continue
            if now - self._settled[sid][1] < SETTLED_SECONDS:
                continue
            analysis = self._analyses.get(sid)
            moved = (analysis is None or math.dist(analysis.position, survivor.position)
                     > PLAN_MOVE_TOLERANCE)
            if moved or now - analysis.isolation_at >= ISOLATION_TTL:
                feet = int(round(survivor.position[2] + PLAYER_SUPPORT_OFFSET))
                above = horde_floor(solid, survivor.position, hunters) - feet >= ELEVATED_RISE
                return _Job(sid, survivor.position, "approach", iter_approach(
                    solid, survivor.position,
                    limit=APPROACH_CELLS_ELEVATED if above else APPROACH_CELLS))
            if analysis.wants_plan and now - analysis.plan_at >= PLAN_TTL:
                planned = self._plan_job(solid, sid, survivor.position, analysis.info.floor_z)
                if planned is not None:
                    return planned
        return None

    def _plan_job(self, solid, sid: int, position, floor_z: int) -> _Job | None:
        support = support_cells(solid, position)
        if not support:
            return None
        return _Job(sid, position, "plan",
                    iter_plan_collapse(solid, support, ground_floor_z=floor_z),
                    floor_z=floor_z)

    def _finish(self, job: _Job, value, solid, hunters, now: float) -> None:
        if job.stage == "approach":
            self._finish_approach(job, value, solid, hunters, now)
            return
        self.stats["plans"] += 1
        analysis = self._analyses.get(job.survivor_id)
        if analysis is None:
            return
        if value is not None:
            self.stats["plans_found"] += 1
            logger.info(
                "Zombie siege: survivor %d collapse cut %d voxels (%d claw sites, cost %d)",
                job.survivor_id, len(value.cut), len(value.sites), value.cost,
            )
        info = analysis.info
        self._analyses[job.survivor_id] = _Analysis(
            analysis.position,
            SiegeInfo(info.isolated, job.floor_z, value, now, info.approach, info.position),
            analysis.isolation_at, now, analysis.wants_plan)

    def _finish_approach(self, job: _Job, approach: Approach | None, solid,
                         hunters, now: float) -> None:
        self.stats["analyses"] += 1
        previous = self._analyses.get(job.survivor_id)
        if approach is None:
            # No floor under him (falling, jumping): nothing to analyse yet.
            self._analyses[job.survivor_id] = _Analysis(
                job.position, SiegeInfo(False, 0, None, now, None, job.position), now, now)
            return
        isolated = approach.closed and not any(
            approach.moves_from(h) is not None for h in hunters)
        floor_z = horde_floor(solid, job.position, hunters)
        wants_plan = isolated or self._cut_worth_planning(
            job, approach, floor_z, hunters)
        if isolated:
            self.stats["isolated"] += 1
        plan, plan_at = None, -math.inf
        if (wants_plan and previous is not None and previous.info.plan is not None
                and math.dist(previous.position, job.position) <= PLAN_MOVE_TOLERANCE
                and now - previous.plan_at < PLAN_TTL
                and any(solid(*c) for c in previous.info.plan.cut)):
            plan, plan_at = previous.info.plan, previous.plan_at
        elif (wants_plan and previous is not None and previous.info.plan is None
                and previous.wants_plan
                and math.dist(previous.position, job.position) <= PLAN_MOVE_TOLERANCE
                and now - previous.plan_at < PLAN_TTL):
            plan_at = previous.plan_at  # planned recently: no cheap cut exists
        self._analyses[job.survivor_id] = _Analysis(
            job.position,
            SiegeInfo(isolated, floor_z, plan, now, approach, job.position),
            now, plan_at, wants_plan)
        if wants_plan and plan_at == -math.inf:
            # Plan right away: an elevated survivor is the urgent case.
            self._queue.appendleft(job.survivor_id)

    def _cut_worth_planning(self, job: _Job, approach: Approach, floor_z: int,
                            hunters) -> bool:
        """Is a walkable footing worth the cost of a collapse plan?

        Only one well above the horde, and only while the hunters near it do
        not simply walk up: none of them inside the flood within
        PLAN_WORTH_SECONDS, or the ones sent after him stuck.
        """

        if floor_z - approach.support_z < ELEVATED_RISE:
            return False
        if self.coordinator.struggling(job.survivor_id):
            return True
        near = [h for h in hunters
                if math.hypot(h[0] - job.position[0], h[1] - job.position[1])
                <= PLAN_HUNTER_RANGE]
        if not near:
            return False
        for hunter in near:
            moves = approach.moves_from(hunter)
            if moves is not None and moves * MOVE_BLOCKS / RUN_SPEED <= PLAN_WORTH_SECONDS:
                return False
        return True

    def debug_rows(self) -> list[dict]:
        rows = []
        for order in self.last_orders.values():
            rows.append({
                "zombie": order.zombie_id, "target": order.target_id,
                "role": order.role, "cells": len(order.cells),
                "stuck": order.stuck_level,
            })
        return rows


def _loadout(player) -> Iterable[int]:
    try:
        return tuple(int(tool) for tool in (getattr(player, "loadout", ()) or ()))
    except (TypeError, ValueError):
        return ()


def piles_now(targets: Iterable[tuple[float, float, float]],
              hunters: Iterable[tuple[float, float, float]]) -> int:
    hunters = tuple(hunters)
    return max((pile_count(t, hunters) for t in targets), default=0)


__all__ = ["ZOMBIE_ORDER_KIND", "ZombieSiegeService", "piles_now"]
