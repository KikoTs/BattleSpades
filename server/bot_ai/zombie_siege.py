"""Gameplay-thread glue between Zombie mode and the horde coordinator.

The director calls :meth:`ZombieSiegeService.objectives` from its objective
snapshot (at most ~10 Hz). The service

* reads the live survivors and infected from the mode,
* keeps a structural analysis per survivor (walk-flood isolation, then the
  minimum collapse cut from ``structure_collapse``) fresh with bounded
  generator jobs advanced under a per-call time budget, exactly like the
  refuge election, so a slow analysis spreads over several ticks instead of
  stalling one,
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
    HordeCoordinator,
    HordeMember,
    HordeOrder,
    SiegeInfo,
    SurvivorTarget,
    pile_count,
)
from .messages import ObjectiveSnapshot
from .structure_collapse import (
    horde_floor,
    iter_isolation,
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
_BUILD_TOOLS = frozenset({int(C.BLOCK_TOOL), int(getattr(C, "ZOMBIE_PREFAB_TOOL", 28))})
_DIG_TOOLS = frozenset(int(t) for t in getattr(C, "ALL_MELEE_WEAPONS", ()))


@dataclass(slots=True)
class _Analysis:
    position: tuple[float, float, float]
    info: SiegeInfo
    isolation_at: float
    plan_at: float


@dataclass(slots=True)
class _Job:
    survivor_id: int
    position: tuple[float, float, float]
    stage: str  # "isolation" | "plan"
    generator: object
    floor_z: int = 0


class ZombieSiegeService:
    """Bounded, fail-safe horde orders for the director."""

    def __init__(self, *, budget_seconds: float = SIEGE_BUDGET_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.coordinator = HordeCoordinator()
        self.budget_seconds = float(budget_seconds)
        self._clock = clock
        self._epoch: object = None
        self._analyses: dict[int, _Analysis] = {}
        self._job: _Job | None = None
        self._queue: deque[int] = deque()
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
        self.last_orders = {}

    # ------------------------------------------------------------------

    def objectives(self, mode, world, *, now: float | None = None) -> list[ObjectiveSnapshot]:
        try:
            return self._objectives(mode, world, self._clock() if now is None else now)
        except Exception:  # noqa: BLE001 - bot planning must never stall the tick
            if self._clock() - self._last_failure_log >= 60.0:
                self._last_failure_log = self._clock()
                logger.exception("zombie siege planning failed; horde falls back to hunting")
            self.reset()
            return []

    def _objectives(self, mode, world, now: float) -> list[ObjectiveSnapshot]:
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
            analysis = self._analyses.get(sid)
            moved = (analysis is None or math.dist(analysis.position, survivor.position)
                     > PLAN_MOVE_TOLERANCE)
            if moved or now - analysis.isolation_at >= ISOLATION_TTL:
                return _Job(sid, survivor.position, "isolation",
                            iter_isolation(solid, survivor.position, hunters))
            if (analysis.info.isolated and now - analysis.plan_at >= PLAN_TTL):
                return self._plan_job(solid, sid, survivor.position, analysis.info.floor_z)
        return None

    def _plan_job(self, solid, sid: int, position, floor_z: int) -> _Job | None:
        support = support_cells(solid, position)
        if not support:
            return None
        return _Job(sid, position, "plan",
                    iter_plan_collapse(solid, support, ground_floor_z=floor_z),
                    floor_z=floor_z)

    def _finish(self, job: _Job, value, solid, hunters, now: float) -> None:
        if job.stage == "isolation":
            self.stats["analyses"] += 1
            isolated = bool(value is not None and value.isolated)
            previous = self._analyses.get(job.survivor_id)
            if not isolated:
                self._analyses[job.survivor_id] = _Analysis(
                    job.position, SiegeInfo(False, int(getattr(value, "support_z", 0))),
                    now, now)
                return
            self.stats["isolated"] += 1
            floor_z = horde_floor(solid, job.position, hunters)
            keep_plan = (
                previous is not None and previous.info.isolated
                and previous.info.plan is not None
                and math.dist(previous.position, job.position) <= PLAN_MOVE_TOLERANCE
                and now - previous.plan_at < PLAN_TTL
                and any(solid(*c) for c in previous.info.plan.cut)
            )
            if keep_plan:
                self._analyses[job.survivor_id] = _Analysis(
                    previous.position,
                    SiegeInfo(True, floor_z, previous.info.plan, previous.info.analysed_at),
                    now, previous.plan_at)
                return
            self._analyses[job.survivor_id] = _Analysis(
                job.position, SiegeInfo(True, floor_z, None, now), now, -math.inf)
            # Plan right away: an elevated survivor is the urgent case.
            planned = self._plan_job(solid, job.survivor_id, job.position, floor_z)
            if planned is not None:
                self._queue.appendleft(job.survivor_id)
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
        self._analyses[job.survivor_id] = _Analysis(
            analysis.position,
            SiegeInfo(True, job.floor_z, value, now),
            analysis.isolation_at, now)

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
