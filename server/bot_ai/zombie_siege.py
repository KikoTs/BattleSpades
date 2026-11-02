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
import os
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
    ELEVATED_STAND_COST,
    PLAYER_SUPPORT_OFFSET,
    Approach,
    dig_cost,
    horde_floor,
    iter_approach,
    iter_plan_collapse,
    iter_plan_foundation,
    support_cells,
)

logger = logging.getLogger(__name__)

ZOMBIE_ORDER_KIND = "zombie_order"
# Gameplay-thread time per objective snapshot: the orders and what they leave
# of it for structural analysis (never less than a quarter).
SIEGE_BUDGET_SECONDS = 0.001
# How often a survivor's analysis is looked at again. The flood itself is
# repeated only when the ground near him changed since (or the world cannot
# say); otherwise only who stands inside it is brought up to date.
ISOLATION_TTL = 2.5
# Terrain edits remembered between looks; more than this and every analysis
# is simply redone.
EDIT_LOG = 512
# Horizontal proximity: height is precisely why a siege needs analysis.
ANALYSIS_RANGE = 48.0
PLAN_TTL = 6.0
PLAN_MOVE_TOLERANCE = 2.0
# Only a survivor who has stayed put this long is analysed: one on the run
# is simply hunted, and a flood restarted at every stride never finishes.
# A survivor well above the horde is the exception: he is analysed on the
# move (a player pacing a roof top never settles, and the horde must not
# wait under him for it), and his analysis stays good for as long as he
# keeps to the footing it explored.
SETTLED_SECONDS = 0.75
SETTLED_MOVE = 1.5
ELEVATED_ANALYSIS_RANGE = 24.0
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
# Tall multi-column structures exceed the ordinary 2,500-voxel component
# budget before the search reaches their foundations. Keep the same time
# slice, but allow the resumable job to retain a larger bounded graph.
TALL_STRUCTURE_NODES = 8192
# An exact cut this dear (voxels out of reach, a wide base) sends the
# foundation planner after the structure's contact with the ground as well;
# the coordinator digs whichever is quicker. A foundation that was taken
# out without dropping him is not tried again for this long.
EXPENSIVE_CUT = 20
FOUNDATION_TTL = 6.0
FOUNDATION_RETRY = 20.0
FOUNDATION_BUDGET_BOOST = 2.0
# A/B switch for the harness: ZOMBIE_FOUNDATION=0 disables the foundation
# planner (the exact cut and the climb remain).
_FOUNDATION_ENABLED = os.environ.get("ZOMBIE_FOUNDATION", "1") != "0"
_BUILD_TOOLS = frozenset({int(C.BLOCK_TOOL), int(getattr(C, "ZOMBIE_PREFAB_TOOL", 28))})
_DIG_TOOLS = frozenset(int(t) for t in getattr(C, "ALL_MELEE_WEAPONS", ()))
# How long a survivor stays "dangerous" after hitting or killing a zombie:
# the horde rallies out of his sight and rushes him in waves meanwhile.
THREAT_SECONDS = 25.0
# A/B switch for the harness: ZOMBIE_ADVANCE=0 makes no survivor dangerous.
_ADVANCE_ENABLED = os.environ.get("ZOMBIE_ADVANCE", "1") != "0"


@dataclass(slots=True)
class _Analysis:
    position: tuple[float, float, float]
    info: SiegeInfo
    isolation_at: float
    plan_at: float
    # Whether the collapse cut is worth keeping fresh: nothing walks in, or
    # the footing stands well above the horde.
    wants_plan: bool = False
    # The terrain edit count when the flood began (None: edits unknown).
    edits: int | None = None
    # When the foundation cut was last sought for this footing, and whether
    # the search found the structure too big to read (a city block): not
    # tried again for this footing.
    foundation_at: float = -math.inf
    foundation_hopeless: bool = False


@dataclass(slots=True)
class _Job:
    survivor_id: int
    position: tuple[float, float, float]
    stage: str  # "approach" | "plan"
    generator: object
    floor_z: int = 0
    edits: int | None = None
    diagnostics: dict | None = None


class ZombieSiegeService:
    """Bounded, fail-safe horde orders for the director."""

    def __init__(self, *, budget_seconds: float = SIEGE_BUDGET_SECONDS,
                 seed: int | None = None,
                 clock: Callable[[], float] | None = None,
                 budget_clock: Callable[[], float] | None = None) -> None:
        self.coordinator = HordeCoordinator(seed=seed)
        self._seed = seed
        self.budget_seconds = float(budget_seconds)
        # Looked up now, not at import: an accelerated harness replaces the
        # clock before the director creates this service.
        self._clock = clock if clock is not None else time.monotonic
        # What the budget is measured with (None: time.perf_counter, read at
        # each call). A harness passes a counter that models the cost of one
        # analysis step, so a run does not depend on the machine's load.
        self._budget_clock = budget_clock
        # Smoothed cost of the orders, taken off the analysis budget.
        self._order_seconds = 0.0
        # Terrain edits since the service began watching this world.
        self._edits: deque[tuple[int, int, int, bool]] = deque(maxlen=EDIT_LOG)
        self._edit_count = 0
        self._unseen_edits = 0
        self._watched: tuple[object, int] | None = None
        self._epoch: object = None
        self._analyses: dict[int, _Analysis] = {}
        self._job: _Job | None = None
        self._queue: deque[int] = deque()
        # Per survivor: where he was last seen to move from, and when.
        self._settled: dict[int, tuple[tuple[float, float, float], float]] = {}
        # Per survivor: until when (monotonic) he counts as dangerous, from
        # the kills the director reports and the zombies' own damage records.
        self._threats: dict[int, float] = {}
        # Per survivor: since when hunters have stood right under him.
        self._under_since: dict[int, float] = {}
        self._last_failure_log = -math.inf
        self._height = None
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
        self._under_since.clear()
        self._threats.clear()
        self.last_orders = {}
        self._unwatch()

    def _watch(self, world) -> bool:
        """Follow the world's terrain edits; False when it cannot report them."""

        self._unseen_edits = 0
        if self._watched is not None and self._watched[0] is world:
            return True
        self._unwatch()
        subscribe = getattr(world, "subscribe_mutations", None)
        if not callable(subscribe):
            return False

        def edited(x, y, z, solid, color, version) -> None:
            if self._watched is None:
                return
            self._edit_count += 1
            self._edits.append((self._edit_count, int(x), int(y), bool(solid)))
            self._unseen_edits += 1
            if self._unseen_edits > 8 * EDIT_LOG:
                # Nobody has asked for orders in a long while (another mode
                # is running): stop listening until somebody does.
                self._unwatch()

        self._watched = (world, subscribe(edited))
        return True

    def _unwatch(self) -> None:
        watched, self._watched = self._watched, None
        self._edits.clear()
        # Edits go unseen from here on: no analysis taken so far is current.
        self._edit_count += 1
        if watched is not None:
            unsubscribe = getattr(watched[0], "unsubscribe_mutations", None)
            if callable(unsubscribe):
                unsubscribe(watched[1])

    def _edited_near(self, analysis: _Analysis, *, additions_only: bool = False) -> bool:
        """Has the ground the flood covered changed since it was taken?"""

        if analysis.edits is None or self._watched is None:
            return True
        if self._edit_count == analysis.edits:
            return False
        if not self._edits or self._edits[0][0] > analysis.edits + 1:
            return True  # some of the edits since have left the log
        approach = analysis.info.approach
        reach = (approach.radius if approach is not None else 0.0) + 3.0
        px, py = analysis.position[0], analysis.position[1]
        plan = analysis.info.plan
        points = ((px,py), *(cell[:2] for cell in plan.cut)) if plan is not None else ((px,py),)
        min_x, max_x = min(p[0] for p in points)-3, max(p[0] for p in points)+3
        min_y, max_y = min(p[1] for p in points)-3, max(p[1] for p in points)+3
        for count, x, y, added in reversed(self._edits):
            if count <= analysis.edits:
                break
            if additions_only and not added:
                continue
            if (abs(x + 0.5 - px) <= reach and abs(y + 0.5 - py) <= reach
                    or min_x <= x+.5 <= max_x and min_y <= y+.5 <= max_y):
                return True
        return False

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

        budget_clock = self._budget_clock or time.perf_counter
        started = budget_clock()
        round_number = int(getattr(mode, "rounds_played", 0))
        epoch = (id(mode), getattr(world, "map_name", ""), round_number)
        phase = getattr(mode, "phase", None)
        if epoch != self._epoch or phase is not ZombiePhase.ACTIVE:
            if epoch != self._epoch or self._analyses or self.last_orders:
                self.reset()
            self._epoch = epoch
            self.coordinator.seed = (None if self._seed is None else
                                     self._seed ^ round_number * 104729)
            if phase is not ZombiePhase.ACTIVE:
                return []
        self._note_hits(mode)
        survivors = [
            SurvivorTarget(int(p.id), tuple(float(v) for v in p.position),
                           threat_until=self._threat_until(int(p.id), now))
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
                grounded=bool(getattr(p, "grounded", True)),
                build_levels=(float(max(0,int(getattr(p,"blocks",0))))
                    if int(C.BLOCK_TOOL) in _loadout(p) else
                    8.*(max(0,int(getattr(p,"blocks",0)))//135)
                    if int(C.ZOMBIE_PREFAB_TOOL) in _loadout(p) and
                    "prefab_zombiehand" in getattr(p,"prefabs",()) else 0.),
                build_seconds_per_level=2.5 if int(C.BLOCK_TOOL) in _loadout(p) else 1.25,
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
        height = getattr(world, "get_height", None)
        self._height = height if callable(height) and _FOUNDATION_ENABLED else None
        if solid is not None:
            watching = self._watch(world)
            # Proving a whole structure's foundation reads more voxels than
            # a cut: it gets a bigger slice of the tick while it runs.
            budget = self.budget_seconds * (
                FOUNDATION_BUDGET_BOOST if self._job is not None
                and self._job.stage == "foundation" else 1.0)
            self._advance_analysis(
                solid, survivors, zombies, now, budget_clock,
                started + max(0.25 * budget, budget - self._order_seconds),
                self._edit_count if watching else None)
        sieges = {
            sid: analysis.info for sid, analysis in self._analyses.items()
            if any(s.player_id == sid for s in survivors)
        }
        ordering = budget_clock()
        water = getattr(world, "is_water_column", None)
        orders = self.coordinator.plan(zombies, survivors, sieges, now, solid=solid,
                                       height=self._height,
                                       water=water if callable(water) else None)
        # (One stalled call must not starve the analysis for the next dozen.)
        self._order_seconds += 0.2 * (
            min(budget_clock() - ordering, self.budget_seconds) - self._order_seconds)
        self.last_orders = orders
        self.stats["orders"] = len(orders)
        self.stats["max_pile"] = max(self.stats["max_pile"], self.coordinator.metrics.max_pile)
        team = int(ZOMBIE_TEAM)
        # Under a shooter every walk in is a rush (sprint, weave, hop).
        dangerous = {s.player_id for s in survivors if s.dangerous(now)}
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
                # No cut known yet and one is being sought: climbers hold
                # their prefabs so the structure gets no new grounding while
                # the proof runs. With a cut in hand, work goes on.
                planning_pending=self._planning_pending(order.target_id, solid),
                rush=bool(order.target_id in dangerous
                          or self.coordinator.wave_running(order.target_id, now)),
            )
            for _, order in sorted(orders.items())
        ]

    def _planning_pending(self, sid: int, solid) -> bool:
        """Should climbers hold their prefabs while a cut is still being sought?

        No cut known yet and one wanted; or the only cut known is dear or
        out of reach and the foundation planner has not had its say: a stack
        leaning on the structure meanwhile would ground it another way and
        pollute the proof. With a reachable cut in hand, work goes on.
        """

        analysis = self._analyses.get(sid)
        if analysis is None:
            return True
        if not analysis.wants_plan:
            return False
        job = self._job
        running = job is not None and job.survivor_id == sid
        if analysis.info.plan is None:
            if analysis.plan_at == -math.inf or (running and job.stage == "plan"):
                return True
        if (analysis.info.plan is None and self._height is not None
                and analysis.info.foundation is None
                and (analysis.foundation_at == -math.inf
                     or (running and job.stage == "foundation"))):
            # Nothing known at all yet: hold the prefabs a moment longer
            # while the foundation is sought. With an exact cut in hand
            # (even one that needs a climb) the climb goes on.
            return True
        return False

    # ------------------------------------------------------------------

    def _advance_analysis(self, solid, survivors, zombies, now: float,
                          budget_clock: Callable[[], float], deadline: float,
                          edits: int | None) -> None:
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
        while True:
            if self._job is None:
                self._job = self._next_job(
                    solid, alive, hunters, now, budget_clock, deadline, edits)
                if self._job is None or budget_clock() >= deadline:
                    return
            job = self._job
            try:
                while True:
                    next(job.generator)
                    if budget_clock() >= deadline:
                        return
            except StopIteration as done:
                self._job = None
                self._finish(job, done.value, solid, hunters, now)
            if budget_clock() >= deadline:
                return

    def _next_job(self, solid, alive, hunters, now: float,
                  budget_clock: Callable[[], float], deadline: float,
                  edits: int | None) -> _Job | None:
        if not self._queue:
            self._queue.extend(sorted(alive))
        for _ in range(len(self._queue)):
            sid = self._queue.popleft()
            survivor = alive.get(sid)
            if survivor is None:
                continue
            if not any(math.hypot(h[0] - survivor.position[0],
                                  h[1] - survivor.position[1]) <= ANALYSIS_RANGE
                       for h in hunters):
                self._analyses.pop(sid, None)
                continue
            analysis = self._analyses.get(sid)
            if (now - self._settled[sid][1] < SETTLED_SECONDS
                    and not self._elevated(survivor.position, hunters)):
                continue
            moved = (analysis is None or math.dist(analysis.position, survivor.position)
                     > PLAN_MOVE_TOLERANCE)
            if (not moved and analysis is not None and analysis.info.approach is not None):
                # A flood taken as he fell has a support three blocks under
                # his feet; once he has landed (on a stub, on a stack) the
                # footing it describes is not his. Read his support afresh.
                fresh = support_cells(solid, survivor.position)
                if fresh and fresh[0][2] != analysis.info.approach.support_z:
                    moved = True
                    self.stats["refloods_on_landing"] = self.stats.get("refloods_on_landing", 0) + 1
            if (moved and analysis is not None and analysis.info.approach is not None
                    and analysis.info.approach.cell_at(survivor.position) is not None):
                # He walked along the footing the flood explored (a roof, a
                # deck): the ways in are the same, only his spot has changed.
                # The cut was planned for where he stood; it is planned again
                # on its own schedule, not at every stride.
                info = analysis.info
                analysis = self._analyses[sid] = _Analysis(
                    survivor.position,
                    SiegeInfo(info.isolated, info.floor_z, None, info.analysed_at,
                              info.approach, survivor.position, foundation=info.foundation),
                    analysis.isolation_at, analysis.plan_at, analysis.wants_plan,
                    analysis.edits, analysis.foundation_at, analysis.foundation_hopeless)
                self.stats["followed"] = self.stats.get("followed", 0) + 1
                moved = False
            if not moved and now - analysis.isolation_at >= ISOLATION_TTL:
                if analysis.info.approach is None or self._edited_near(analysis):
                    moved = True
                else:
                    # The same ground: only the hunters on it have changed.
                    self._assess(sid, analysis.position, analysis.info.approach,
                                 solid, hunters, now, analysis.edits)
                    analysis = self._analyses[sid]
                    if budget_clock() >= deadline:
                        return None
            if moved:
                return _Job(sid, survivor.position, "approach",
                            self._flood(solid, survivor.position, hunters), edits=edits)
            if analysis.wants_plan and now - analysis.plan_at >= PLAN_TTL:
                planned = self._plan_job(solid, sid, survivor.position, analysis.info.floor_z)
                if planned is not None:
                    return planned
            if (analysis.wants_plan and self._height is not None
                    and analysis.plan_at != -math.inf
                    and not analysis.foundation_hopeless
                    and now - analysis.foundation_at >= (
                        FOUNDATION_TTL if analysis.info.foundation is not None else FOUNDATION_RETRY)
                    and self._foundation_wanted(analysis, solid)):
                founded = self._foundation_job(solid, sid, survivor.position,
                                               analysis.info.floor_z)
                if founded is not None:
                    return founded
        return None

    @staticmethod
    def _foundation_wanted(analysis: _Analysis, solid) -> bool:
        """Is the exact cut missing, dear, or wholly out of reach from the ground?

        A cut with a stand on the ground is dug as it is. One whose every
        voxel hangs in the air or sits on a floor of the structure (a
        walkway voxel fifty blocks from the tree; the foot of a pillar on a
        roof) sends the planner after the ground contact as well; for a
        building that search ends quickly on its column bound and the roof
        stays the way.
        """

        plan = analysis.info.plan
        if plan is None:
            return True
        if plan.cost >= EXPENSIVE_CUT:
            return True
        floor_z = analysis.info.floor_z
        standing = [cell for cell in plan.cut if solid(*cell)]
        return bool(standing) and all(dig_cost(solid, cell, floor_z) >= ELEVATED_STAND_COST
                                      for cell in standing)

    def _foundation_job(self, solid, sid: int, position, floor_z: int) -> _Job | None:
        support = support_cells(solid, position)
        if not support:
            return None
        diagnostics = {}
        return _Job(sid, position, "foundation",
                    iter_plan_foundation(solid, self._height, support,
                                         ground_floor_z=floor_z, diagnostics=diagnostics),
                    floor_z=floor_z, diagnostics=diagnostics)

    @staticmethod
    def _elevated(position, hunters) -> bool:
        """Is some hunter near him standing well below his feet?"""

        px, py, pz = (float(v) for v in position)
        return any(math.hypot(h[0] - px, h[1] - py) <= ELEVATED_ANALYSIS_RANGE
                   and float(h[2]) - pz >= ELEVATED_RISE for h in hunters)

    @staticmethod
    def _flood(solid, position, hunters):
        yield  # (the floor below is a step of its own)
        feet = int(round(position[2] + PLAYER_SUPPORT_OFFSET))
        above = horde_floor(solid, position, hunters) - feet >= ELEVATED_RISE
        yield
        return (yield from iter_approach(
            solid, position, limit=APPROACH_CELLS_ELEVATED if above else APPROACH_CELLS))

    def _plan_job(self, solid, sid: int, position, floor_z: int) -> _Job | None:
        support = support_cells(solid, position)
        if not support:
            return None
        tall = floor_z - (position[2] + PLAYER_SUPPORT_OFFSET) > ANALYSIS_RANGE
        diagnostics = {}
        return _Job(sid, position, "plan",
                    iter_plan_collapse(solid, support, ground_floor_z=floor_z,
                                       node_limit=TALL_STRUCTURE_NODES if tall else 2500,
                                       diagnostics=diagnostics),
                    floor_z=floor_z, diagnostics=diagnostics)

    def _finish(self, job: _Job, value, solid, hunters, now: float) -> None:
        if job.stage == "approach":
            self._finish_approach(job, value, solid, hunters, now)
            return
        if job.stage == "foundation":
            self._finish_foundation(job, value, now)
            return
        self.stats["plans"] += 1
        if job.diagnostics is not None:
            reason = str(job.diagnostics.get("reason", "unknown"))
            key = "plan_" + reason
            self.stats[key] = self.stats.get(key, 0) + 1
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
        if value is None and info.plan is not None and any(solid(*c) for c in info.plan.cut):
            # The fresh proof found nothing cheap (the search ran out of
            # budget on a huge component); the cut in hand is still there
            # to dig, so keep it rather than idle the crews.
            value = info.plan
            self.stats["plan_kept_after_miss"] = self.stats.get("plan_kept_after_miss", 0) + 1
        self._analyses[job.survivor_id] = _Analysis(
            analysis.position,
            SiegeInfo(info.isolated, job.floor_z, value, now, info.approach, info.position,
                      foundation=info.foundation),
            analysis.isolation_at, now, analysis.wants_plan, analysis.edits,
            analysis.foundation_at, analysis.foundation_hopeless)

    def _finish_foundation(self, job: _Job, value, now: float) -> None:
        self.stats["foundations"] = self.stats.get("foundations", 0) + 1
        if job.diagnostics is not None:
            key = "foundation_" + str(job.diagnostics.get("reason", "unknown"))
            self.stats[key] = self.stats.get(key, 0) + 1
        analysis = self._analyses.get(job.survivor_id)
        if analysis is None:
            return
        if value is not None:
            self.stats["foundations_found"] = self.stats.get("foundations_found", 0) + 1
            logger.info(
                "Zombie siege: survivor %d foundation cut %d voxels (%d claw sites, %d columns)",
                job.survivor_id, len(value.cut), len(value.sites), value.explored,
            )
        info = analysis.info
        reason = str(job.diagnostics.get("reason", "")) if job.diagnostics else ""
        hopeless = value is None and reason in (
            "footprint_bounded", "verify_bounded", "cut_too_large", "support_not_tall")
        self._analyses[job.survivor_id] = _Analysis(
            analysis.position,
            SiegeInfo(info.isolated, info.floor_z, info.plan, info.analysed_at,
                      info.approach, info.position, foundation=value),
            analysis.isolation_at, analysis.plan_at, analysis.wants_plan, analysis.edits, now,
            hopeless or analysis.foundation_hopeless)

    def _finish_approach(self, job: _Job, approach: Approach | None, solid,
                         hunters, now: float) -> None:
        self.stats["analyses"] += 1
        if approach is None:
            # No floor under him (falling, jumping): nothing to analyse yet.
            self._analyses[job.survivor_id] = _Analysis(
                job.position, SiegeInfo(False, 0, None, now, None, job.position), now, now)
            return
        self._assess(job.survivor_id, job.position, approach, solid, hunters, now, job.edits)

    def _assess(self, sid: int, position, approach: Approach, solid, hunters,
                now: float, edits: int | None) -> None:
        """Read a flood against the hunters as they stand now."""

        previous = self._analyses.get(sid)
        isolated = approach.closed and not any(
            approach.moves_from(h) is not None for h in hunters)
        floor_z = horde_floor(solid, position, hunters)
        wants_plan = isolated or self._cut_worth_planning(
            sid, position, approach, floor_z, hunters, now, solid)
        if isolated and (previous is None or not previous.info.isolated
                         or previous.info.approach is not approach):
            self.stats["isolated"] += 1
        plan, plan_at = None, -math.inf
        if (wants_plan and previous is not None and previous.info.plan is not None
                and math.dist(previous.position, position) <= PLAN_MOVE_TOLERANCE
                and any(solid(*c) for c in previous.info.plan.cut)):
            # Removing vertices cannot create a new grounding connection:
            # the remaining cut stays good while crews work. Additions near
            # it (a survivor's repair, a climber's prefab stack leaning on
            # the pillar) may have grounded him another way, so a fresh
            # proof is queued at once; the crews keep digging the old cut
            # until it lands. Throwing the cut away meanwhile left every
            # digger idle on the roof after each stack a teammate placed.
            plan = previous.info.plan
            fresh = self._watched is None or not self._edited_near(previous, additions_only=True)
            plan_at = now if fresh else -math.inf
            if not fresh:
                self.stats["replans_on_additions"] = self.stats.get("replans_on_additions", 0) + 1
        elif (wants_plan and previous is not None and previous.info.plan is None
                and previous.wants_plan
                and math.dist(previous.position, position) <= PLAN_MOVE_TOLERANCE
                and now - previous.plan_at < PLAN_TTL):
            plan_at = previous.plan_at  # planned recently: no cheap cut exists
        foundation, foundation_at = None, -math.inf
        if (wants_plan and previous is not None and previous.info.foundation is not None
                and math.dist(previous.position, position) <= PLAN_MOVE_TOLERANCE
                and any(solid(*c) for c in previous.info.foundation.cut)):
            foundation, foundation_at = previous.info.foundation, previous.foundation_at
        elif (wants_plan and previous is not None
                and math.dist(previous.position, position) <= PLAN_MOVE_TOLERANCE):
            # A foundation dug out without dropping him, or none found: not
            # sought again before its own TTL.
            foundation_at = previous.foundation_at
        hopeless = bool(previous is not None and previous.foundation_hopeless
                        and math.dist(previous.position, position) <= PLAN_MOVE_TOLERANCE)
        self._analyses[sid] = _Analysis(
            position,
            SiegeInfo(isolated, floor_z, plan, now, approach, position, foundation=foundation),
            now, plan_at, wants_plan, edits, foundation_at, hopeless)
        if wants_plan and plan_at == -math.inf:
            # Plan right away: an elevated survivor is the urgent case.
            self._queue.appendleft(sid)

    def _cut_worth_planning(self, sid: int, position, approach: Approach, floor_z: int,
                            hunters, now: float, solid=None) -> bool:
        """Is a walkable footing worth the cost of a collapse plan?

        Only one well above the horde, and only while the hunters near it do
        not simply walk up: none of them inside the flood within
        PLAN_WORTH_SECONDS, or the ones sent after him stuck, or hunters
        standing at his feet for that long though a walk is supposed to
        lead up (the stub he landed on when his pillar fell; a ledge the
        flood jumps to but the bodies never do). His support is read afresh:
        the flood's was taken where he stood before he fell.
        """

        if self._parked_under_for(sid, position, hunters, now) >= PLAN_WORTH_SECONDS:
            return True
        support_z = approach.support_z
        if solid is not None:
            fresh = support_cells(solid, position)
            if fresh:
                support_z = fresh[0][2]
        if floor_z - support_z < ELEVATED_RISE:
            return False
        if self.coordinator.struggling(sid):
            return True
        near = [h for h in hunters
                if math.hypot(h[0] - position[0], h[1] - position[1])
                <= PLAN_HUNTER_RANGE]
        if not near:
            return False
        for hunter in near:
            moves = approach.moves_from(hunter)
            if moves is not None and moves * MOVE_BLOCKS / RUN_SPEED <= PLAN_WORTH_SECONDS:
                return False
        return True

    def _parked_under_for(self, sid: int, position, hunters, now: float) -> float:
        """Seconds hunters have spent at his feet, three or more blocks below."""

        under = any(
            math.hypot(h[0] - position[0], h[1] - position[1]) <= 3.0
            and float(h[2]) - float(position[2]) >= ELEVATED_RISE
            for h in hunters)
        if not under:
            self._under_since.pop(sid, None)
            return 0.0
        since = self._under_since.setdefault(sid, now)
        return now - since

    # --- who is dangerous -----------------------------------------------------

    def note_kill(self, killer_id: int, killer_team: int, victim_team: int,
                  at: float | None = None) -> None:
        """The director saw a kill: a survivor who kills a zombie is dangerous."""

        from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM
        if int(killer_team) != int(SURVIVOR_TEAM) or int(victim_team) != int(ZOMBIE_TEAM):
            return
        when = time.monotonic() if at is None else float(at)
        self._threats[int(killer_id)] = max(self._threats.get(int(killer_id), 0.0),
                                            when + THREAT_SECONDS)

    def _note_hits(self, mode) -> None:
        """A zombie hit by a survivor lately (its own damage record) marks him."""

        wall = time.monotonic()
        for z in mode._zombies():
            source = int(getattr(z, "_last_damage_source_id", -1))
            if source < 0 or source == int(getattr(z, "id", -1)):
                continue
            hit_at = float(getattr(z, "_last_combat_damage_at", 0.0))
            if hit_at <= 0.0 or wall - hit_at > THREAT_SECONDS:
                continue
            self._threats[source] = max(self._threats.get(source, 0.0), hit_at + THREAT_SECONDS)

    def _threat_until(self, sid: int, now: float) -> float:
        """``now``-based deadline: the records are monotonic, ``now`` may not be."""

        until = self._threats.get(int(sid))
        if until is None or not _ADVANCE_ENABLED:
            return 0.0
        remaining = until - time.monotonic()
        if remaining <= 0.0:
            self._threats.pop(int(sid), None)
            return 0.0
        return float(now) + remaining

    def debug_rows(self) -> list[dict]:
        rows = []
        for order in self.last_orders.values():
            analysis = self._analyses.get(order.target_id)
            rows.append({
                "zombie": order.zombie_id, "target": order.target_id,
                "role": order.role, "cells": len(order.cells),
                "stuck": order.stuck_level,
                "target_height": analysis.position[2] if analysis else None,
                "ground_floor": analysis.info.floor_z if analysis else None,
                "cut_cells": len(analysis.info.plan.cut) if analysis and analysis.info.plan else 0,
                "foundation_cells": (len(analysis.info.foundation.cut)
                                     if analysis and analysis.info.foundation else 0),
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
