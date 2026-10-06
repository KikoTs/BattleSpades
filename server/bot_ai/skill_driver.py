"""Per-bot ownership of locomotion skills (climb out, pillar, fast bridge).

The worker brain asks this driver at a few fixed points:

* ``step``            continue an active skill (any context);
* ``consider_water``  a swimmer: climb the nearest cliff when that beats
                      swimming to a native shore;
* ``consider_stuck``  a grounded body that has not got anywhere for a while
                      and whose goal is above it, or which stands in a low
                      pocket outside the map's main ground (pit, beach);
* ``consider_gap``    the dry route ends at a chasm the wallet can bridge;
* ``request``         an explicit primitive from strategy code (directive).

Every command is an ordinary input frame (see ``recovery_skills``). The
driver owns failure memory: a failed step re-plans from the live body a few
times, then backs off so the legacy recovery keeps working. Plan searches
are resumable and get a small time slice per decision, so one hard climb
never stalls the worker thread that serves the whole roster.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable

from .messages import BotProfile, PlayerSnapshot, Vector3
from .recovery_skills import (
    SUPPORT_OFFSET,
    WATER_SUPPORT_Z,
    AscentGoal,
    AscentPlan,
    AscentSearch,
    ClimbAbilities,
    LocomotionSkill,
    SkillCommand,
    StepKind,
    find_gap_bridge,
    node_of,
    plan_ascent,
    plan_fast_bridge,
    plan_pillar_up,
    plan_staircase_up,
    quick_climb_estimate,
)


# Directive names a ModeBotDecision may carry to request a primitive.
DIRECTIVE_CLIMB = "climb"
DIRECTIVE_PILLAR_UP = "pillar_up"
DIRECTIVE_STAIRCASE_UP = "dig_staircase_up"
DIRECTIVE_BRIDGE = "bridge"
SKILL_DIRECTIVES = frozenset({
    DIRECTIVE_CLIMB, DIRECTIVE_PILLAR_UP, DIRECTIVE_STAIRCASE_UP, DIRECTIVE_BRIDGE,
})

_SWIM_SECONDS_PER_CELL = 0.34
_WATER_GRACE_SECONDS = 0.6
_REPLAN_LIMIT = 3
_BACKOFF_SECONDS = 8.0
_STUCK_SECONDS = 6.0
_STUCK_DISTANCE = 2.5
_LOW_POCKET_SECONDS = 5.0
_MAX_PLAN_EXPANSIONS = 2500
# How far a stranded body looks for the island's main ground (swim + climb).
_MAIN_GROUND_RADIUS = 28
# Planner time per worker decision; a hard search continues next decision.
_SEARCH_SLICE = 0.012
_SEARCH_GIVE_UP_SECONDS = 5.0
# Whole-skill progress watchdog (a finished step or 0.75 blocks of motion).
_WATER_STALL_SECONDS = 4.0
_DRY_STALL_SECONDS = 7.0


@dataclass(frozen=True, slots=True)
class SkillRequest:
    """An explicit locomotion primitive.

    ``kind``: ``climb`` (reach ``target`` by any mix of stairs, pillars and
    bridging), ``pillar_up`` (``levels`` blocks under the body, or up to
    ``target`` height), ``dig_staircase_up`` (``levels`` toward
    ``direction``/``target``), ``bridge`` (``length`` cells toward
    ``direction``/``target``).
    """

    kind: str
    target: Vector3 | None = None
    direction: Vector3 | None = None
    levels: int = 0
    length: int = 0

    @classmethod
    def from_directive(cls, directive: str, position: Vector3,
                       observer: PlayerSnapshot) -> "SkillRequest":
        here = observer.position
        dx, dy = float(position[0]) - float(here[0]), float(position[1]) - float(here[1])
        rise = max(0, int(round((float(here[2]) - float(position[2])))))
        if directive == DIRECTIVE_PILLAR_UP:
            return cls(directive, position, None, max(1, rise))
        if directive == DIRECTIVE_STAIRCASE_UP:
            return cls(directive, position, (dx, dy, 0.0), max(1, rise))
        if directive == DIRECTIVE_BRIDGE:
            return cls(directive, position, (dx, dy, 0.0), 0,
                       max(1, int(math.ceil(max(abs(dx), abs(dy))))))
        return cls(DIRECTIVE_CLIMB, position)


@dataclass(slots=True)
class _PendingSearch:
    """A resumable plan search, advanced a slice per worker decision."""

    search: AscentSearch
    purpose: str
    goal_spec: tuple
    started_at: float
    accept: Callable[[AscentPlan], bool] | None = None
    # Tried when this search finds nothing (stuck: toward goal, then main).
    fallback: tuple | None = None


@dataclass(slots=True)
class _Slot:
    life_id: int
    skill: LocomotionSkill | None = None
    # How to re-plan: ("main",), ("toward", target, radius), ("request", req)
    # or ("bridge", goal).
    goal: tuple | None = None
    request: SkillRequest | None = None
    pending: _PendingSearch | None = None
    failures: int = 0
    retry_at: float = 0.0
    water_since: float | None = None
    dry_since: float | None = None
    anchor: Vector3 | None = None
    anchor_at: float = 0.0
    low_since: float | None = None
    completed: int = 0
    last_reason: str = ""
    # Cells the executor proved unusable (expiry by cell); the re-plan avoids them.
    avoid: dict = field(default_factory=dict)
    progress_index: int = -1
    progress_position: Vector3 | None = None
    progress_at: float = 0.0


class LocomotionSkillDriver:
    """Own skill execution for every bot handled by one worker brain."""

    def __init__(self, world) -> None:
        self.world = world
        self._slots: dict[tuple[int, int], _Slot] = {}
        self.metrics: dict[str, int] = {}
        # Bounded diagnostics: the last few failures with their context.
        self.events: list[tuple] = []

    @property
    def _voxel_world(self) -> bool:
        """Skills need voxel occupancy; simplified test worlds may lack it."""

        return callable(getattr(self.world, "solid", None))

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        self._slots.clear()

    def forget(self, player_id: int, generation: int) -> None:
        self._slots.pop((int(player_id), int(generation)), None)

    def _slot(self, observer: PlayerSnapshot) -> _Slot:
        key = (int(observer.player_id), int(observer.generation))
        slot = self._slots.get(key)
        if slot is None or slot.life_id != int(observer.life_id):
            slot = _Slot(int(observer.life_id))
            self._slots[key] = slot
            if len(self._slots) > 256:
                self._slots.pop(next(iter(self._slots)))
        return slot

    def _count(self, name: str) -> None:
        self.metrics[name] = self.metrics.get(name, 0) + 1

    def active(self, observer: PlayerSnapshot) -> bool:
        """A skill runs, or a re-plan for one is still being searched."""

        slot = self._slots.get((int(observer.player_id), int(observer.generation)))
        return bool(slot is not None and slot.life_id == int(observer.life_id)
                    and (slot.skill is not None
                         or (slot.pending is not None and slot.goal is not None)))

    def cancel(self, observer: PlayerSnapshot) -> None:
        slot = self._slot(observer)
        slot.skill = None
        slot.goal = None
        slot.request = None
        slot.pending = None

    def describe(self, observer: PlayerSnapshot) -> str:
        slot = self._slots.get((int(observer.player_id), int(observer.generation)))
        if slot is None:
            return ""
        if slot.skill is None:
            searching = f" searching={slot.pending.purpose}" if slot.pending else ""
            return f"idle(last={slot.last_reason}){searching}"
        skill = slot.skill
        step = skill.current
        detail = (f" {step.kind.value} {step.source}->{step.destination} dig={len(step.dig_cells)}"
                  f" build={len(step.build_cells)}" if step is not None else "")
        return f"{skill.purpose}:{skill.index}/{len(skill.plan.steps)}{detail} last={slot.last_reason}"

    # -- searches ------------------------------------------------------------

    def _search_for(self, goal_spec: tuple, start, abilities: ClimbAbilities,
                    avoid: tuple = ()) -> AscentSearch | None:
        kind = goal_spec[0]
        if kind == "main":
            target = AscentGoal.main_ground(self.world, start, radius=_MAIN_GROUND_RADIUS)
            if target is None:
                return None
            return AscentSearch(self.world, start, target, abilities, radius=_MAIN_GROUND_RADIUS,
                                max_expansions=_MAX_PLAN_EXPANSIONS, avoid=avoid)
        if kind == "toward":
            return AscentSearch(self.world, start,
                                AscentGoal.toward(goal_spec[1], radius=goal_spec[2]),
                                abilities, radius=24, max_expansions=_MAX_PLAN_EXPANSIONS,
                                avoid=avoid)
        return None

    def _begin_search(self, slot: _Slot, observer: PlayerSnapshot, profile, now: float,
                      purpose: str, goal_spec: tuple, *, accept=None,
                      fallback: tuple | None = None, avoid: tuple = ()) -> SkillCommand | None:
        start = node_of(observer.position)
        abilities = ClimbAbilities.from_observer(observer)
        search = self._search_for(goal_spec, start, abilities, avoid)
        if search is None:
            if fallback is not None:
                return self._begin_search(slot, observer, profile, now, purpose, fallback,
                                          accept=accept, avoid=avoid)
            self._count(f"{purpose}_no_plan")
            slot.goal = None
            slot.retry_at = now + 3.0
            return None
        slot.goal = goal_spec
        slot.pending = _PendingSearch(search, purpose, goal_spec, now, accept, fallback)
        return self._advance(slot, observer, profile, now)

    def _advance(self, slot: _Slot, observer: PlayerSnapshot, profile,
                 now: float) -> SkillCommand | None:
        pending = slot.pending
        if pending is None:
            return None
        status = pending.search.advance(_SEARCH_SLICE)
        if status == "pending":
            if now - pending.started_at > _SEARCH_GIVE_UP_SECONDS:
                slot.pending = None
                slot.goal = None
                slot.retry_at = now + 3.0
                self._count(f"{pending.purpose}_search_timeout")
            return None
        slot.pending = None
        plan = pending.search.plan
        if status == "found" and plan is not None and (pending.accept is None
                                                       or pending.accept(plan)):
            self._start(slot, observer, profile, plan, pending.purpose, now, pending.goal_spec)
            return slot.skill.update(observer, self.world, now,
                                     ClimbAbilities.from_observer(observer))
        if pending.fallback is not None:
            return self._begin_search(slot, observer, profile, now, pending.purpose,
                                      pending.fallback, accept=pending.accept,
                                      avoid=tuple(slot.avoid))
        self._count(f"{pending.purpose}_no_plan")
        slot.goal = None
        slot.retry_at = now + 3.0
        return None

    # -- execution -----------------------------------------------------------

    def step(self, observer: PlayerSnapshot, profile: BotProfile | None,
             now: float) -> SkillCommand | None:
        """Continue an active skill (or its re-plan). ``done`` comes once."""

        slot = self._slot(observer)
        skill = slot.skill
        if skill is None:
            if slot.pending is not None and slot.goal is not None:
                return self._advance(slot, observer, profile, now)
            return None
        if not observer.alive:
            slot.skill = None
            return None
        abilities = ClimbAbilities.from_observer(observer)
        # Progress watchdog over the whole skill: a step finished, or the body
        # moved. Afloat it is stricter, so a stuck climb hands the swimmer
        # back to the shore flow quickly instead of bobbing in place.
        if (slot.progress_index != skill.index or slot.progress_position is None
                or math.dist(observer.position[:2], slot.progress_position[:2]) >= 0.75
                or abs(observer.position[2] - slot.progress_position[2]) >= 1.5):
            slot.progress_index = skill.index
            slot.progress_position = observer.position
            slot.progress_at = now
        limit = _WATER_STALL_SECONDS if observer.wade else _DRY_STALL_SECONDS
        if now - slot.progress_at > limit:
            self._count(f"{skill.purpose}_stalled")
            self.events.append((round(now, 2), int(observer.player_id), skill.purpose,
                                "stalled", skill.index, len(skill.plan.steps), "", None, None,
                                tuple(round(v, 2) for v in observer.position),
                                bool(observer.grounded), bool(observer.wade)))
            del self.events[:-64]
            slot.skill = None
            slot.goal = None
            slot.request = None
            slot.failures = 0
            slot.progress_position = None
            slot.retry_at = now + _BACKOFF_SECONDS
            return None
        command = skill.update(observer, self.world, now, abilities)
        if command.status == "running":
            return command
        if command.status == "done":
            slot.skill = None
            slot.goal = None
            slot.request = None
            slot.failures = 0
            slot.completed += 1
            slot.anchor = observer.position
            slot.anchor_at = now
            slot.low_since = None
            self._count(f"{skill.purpose}_done")
            return command
        # Failed: re-plan from where the body actually is.
        slot.failures += 1
        slot.last_reason = command.reason
        failed_step = skill.current
        self.events.append((round(now, 2), int(observer.player_id), skill.purpose,
                            command.reason, skill.index, len(skill.plan.steps),
                            failed_step.kind.value if failed_step else "",
                            failed_step.source if failed_step else None,
                            failed_step.destination if failed_step else None,
                            tuple(round(v, 2) for v in observer.position),
                            bool(observer.grounded), bool(observer.wade)))
        del self.events[:-64]
        self._count(f"{skill.purpose}_fail_{command.reason}")
        slot.skill = None
        if skill.blocked_cell is not None:
            slot.avoid[skill.blocked_cell] = now + 20.0
        slot.avoid = {cell: until for cell, until in slot.avoid.items() if until > now}
        goal = slot.goal
        if observer.wade and command.reason == "step_timeout":
            # A swimmer does not keep retrying the same cliff foot: back to
            # the shore flow for a while.
            slot.failures = _REPLAN_LIMIT + 1
        if slot.failures > _REPLAN_LIMIT or goal is None:
            slot.goal = None
            slot.request = None
            slot.retry_at = now + _BACKOFF_SECONDS
            slot.failures = 0
            return None
        if goal[0] in ("request", "bridge"):
            plan = (self._plan_request(observer, goal[1], abilities) if goal[0] == "request"
                    else find_gap_bridge(self.world, observer.position, goal[1], abilities))
            if plan is None:
                slot.goal = None
                slot.retry_at = now + _BACKOFF_SECONDS
                return None
            self._start(slot, observer, profile, plan, skill.purpose, now, goal)
            return slot.skill.update(observer, self.world, now, abilities)
        return self._begin_search(slot, observer, profile, now, skill.purpose, goal,
                                  avoid=tuple(slot.avoid))

    def _start(self, slot: _Slot, observer: PlayerSnapshot, profile: BotProfile | None,
               plan: AscentPlan, purpose: str, now: float, goal: tuple) -> None:
        skill_level = float(getattr(profile, "skill", 0.5)) if profile is not None else 0.5
        reaction = float(getattr(profile, "reaction_time", 0.22)) if profile is not None else 0.22
        identity = int(observer.player_id) * 7919 + int(observer.life_id)
        slot.skill = LocomotionSkill(
            plan, purpose, now, identity=identity, skill=skill_level,
            reaction=max(0.05, min(0.6, reaction)), step_started_at=now,
            # Backwards bridging is the practised technique; casual players
            # bridge facing forward and looking down at the edge.
            backwards=skill_level >= 0.45,
        )
        slot.goal = goal
        slot.progress_index = -1
        slot.progress_position = None
        slot.progress_at = now
        self._count(f"{purpose}_start")

    # -- triggers --------------------------------------------------------------

    def consider_water(self, observer: PlayerSnapshot, profile: BotProfile | None,
                       now: float, *, goal: Vector3 | None, swim_seconds: float,
                       recovering: bool) -> SkillCommand | None:
        """A swimmer: climb out here when it beats the swim to a shore."""

        slot = self._slot(observer)
        if slot.skill is not None:
            return self.step(observer, profile, now)
        if slot.water_since is None:
            slot.water_since = now
        slot.dry_since = None
        if now - slot.water_since > swim_seconds + 8.0:
            # The "near" native shore has not worked out: stop trusting it.
            swim_seconds = math.inf
        if slot.pending is not None:
            return self._advance(slot, observer, profile, now)
        if now < slot.retry_at or now - slot.water_since < _WATER_GRACE_SECONDS:
            return None
        if not self._voxel_world or (not recovering and now - slot.water_since < 6.0):
            # A deliberate crossing keeps swimming toward its goal.
            return None
        abilities = ClimbAbilities.from_observer(observer)
        start = node_of(observer.position)
        if start[2] < WATER_SUPPORT_Z:
            start = (start[0], start[1], WATER_SUPPORT_Z)
        quick = quick_climb_estimate(self.world, start, abilities, radius=_MAIN_GROUND_RADIUS)
        if not math.isfinite(quick) or quick > swim_seconds * 1.3 + 3.0:
            slot.retry_at = now + 2.0
            return None
        here = observer.position

        def accept(plan: AscentPlan, swim: float = swim_seconds) -> bool:
            # Climbs run a little slower than planned (landings, rejected
            # swings); a known swim must clearly lose before a bot climbs.
            if plan.seconds * 1.2 + 2.0 > swim:
                return False
            if goal is not None and plan.destination is not None and math.isfinite(swim):
                near = math.hypot(goal[0] - here[0], goal[1] - here[1])
                far = math.hypot(goal[0] - plan.destination[0] - 0.5,
                                 goal[1] - plan.destination[1] - 0.5)
                if far > near + 12.0:
                    return False  # climbing out on the wrong bank of a crossing
            return True

        return self._begin_search(slot, observer, profile, now, "climb_out", ("main",),
                                  accept=accept, avoid=tuple(slot.avoid))

    def left_water(self, observer: PlayerSnapshot, now: float | None = None) -> None:
        """A dry landing; a brief foothold does not end the wet episode."""

        slot = self._slot(observer)
        if slot.water_since is None:
            return
        if now is None:
            slot.water_since = None
            return
        if slot.dry_since is None:
            slot.dry_since = now
        elif now - slot.dry_since >= 3.0:
            slot.water_since = None
            slot.dry_since = None

    def restart_stuck_clock(self, observer: PlayerSnapshot, now: float) -> None:
        """A new route has arrived: it is not what the stuck clock has timed.

        The worker asks for map-wide guidance when local routing gets nowhere,
        which is also when this clock is about to expire. Starting to dig a
        way out one decision after the walking route arrived took the body
        away from it for twenty seconds.
        """

        slot = self._slot(observer)
        if slot.skill is None and slot.pending is None:
            slot.anchor = observer.position
            slot.anchor_at = now

    def consider_stuck(self, observer: PlayerSnapshot, profile: BotProfile | None,
                       now: float, goal: Vector3 | None) -> SkillCommand | None:
        """A grounded body that is getting nowhere below its goal: climb."""

        slot = self._slot(observer)
        if slot.skill is not None:
            return self.step(observer, profile, now)
        if slot.pending is not None:
            return self._advance(slot, observer, profile, now)
        position = observer.position
        if (slot.anchor is None or math.dist(position[:2], slot.anchor[:2]) >= _STUCK_DISTANCE
                or abs(position[2] - slot.anchor[2]) >= 1.5):
            slot.anchor = position
            slot.anchor_at = now
        if (not observer.grounded or observer.wade or goal is None or now < slot.retry_at
                or not self._voxel_world):
            return None
        start = node_of(position)
        atlas = getattr(self.world, "_atlas", None)
        low_pocket = False
        if atlas is not None and getattr(atlas, "main_region_id", 0):
            index = start[1] * atlas.width + start[0]
            region = int(atlas.regions[index])
            goal_index = int(math.floor(goal[1])) * atlas.width + int(math.floor(goal[0]))
            goal_region = (int(atlas.regions[goal_index])
                           if 0 <= goal_index < len(atlas.regions) else -1)
            main = int(atlas.main_region_id)
            primary = int(atlas.primary_support[index])
            # A beach or sea-level ledge cut off from the main ground (the
            # goal is elsewhere), or standing well below this column's own
            # top: a pit or a dug hole.
            sea_level = start[2] >= WATER_SUPPORT_Z - 4
            stranded = (
                (region != main and region != goal_region and sea_level)
                # Tunnelling along at sea level deep under the island.
                or (sea_level and primary < start[2] - 6)
            )
            # A pit or dug hole: only once the ordinary escape stalls.
            low_pocket = stranded or (primary < start[2] - 2 and region == main
                                      and not atlas.layer_count[index] > 1)
        else:
            stranded = False
        if stranded:
            if slot.low_since is None:
                slot.low_since = now
        else:
            slot.low_since = None
        goal_support = int(round(float(goal[2]) + SUPPORT_OFFSET))
        goal_above = goal_support <= start[2] - 3 and math.dist(position[:2], goal[:2]) <= 40.0
        stalled = now - slot.anchor_at >= _STUCK_SECONDS
        pocket_due = slot.low_since is not None and now - slot.low_since >= _LOW_POCKET_SECONDS
        if not ((stalled and (goal_above or low_pocket)) or pocket_due):
            return None
        self._count("stuck_considered")
        slot.anchor_at = now
        main_spec = ("main",) if low_pocket else None
        first = ("toward", tuple(goal), 2.0) if goal_above else main_spec
        if first is None:
            slot.retry_at = now + 4.0
            return None
        return self._begin_search(
            slot, observer, profile, now, "pit_climb", first,
            accept=lambda plan: plan.rise >= 1,
            fallback=main_spec if first is not main_spec else None,
            avoid=tuple(slot.avoid))

    def consider_gap(self, observer: PlayerSnapshot, profile: BotProfile | None,
                     now: float, goal: Vector3, *, max_cells: int = 16) -> SkillCommand | None:
        """The dry route stops at a chasm: fast-bridge it when affordable."""

        slot = self._slot(observer)
        if slot.skill is not None:
            return self.step(observer, profile, now)
        if slot.pending is not None:
            return self._advance(slot, observer, profile, now)
        if (now < slot.retry_at or not observer.grounded or observer.wade
                or not self._voxel_world):
            return None
        abilities = ClimbAbilities.from_observer(observer)
        plan = find_gap_bridge(self.world, observer.position, goal, abilities,
                               max_cells=max_cells)
        if plan is None:
            slot.retry_at = now + 1.5
            return None
        self._start(slot, observer, profile, plan, "bridge", now, ("bridge", tuple(goal)))
        return self.step(observer, profile, now)

    def request(self, observer: PlayerSnapshot, profile: BotProfile | None,
                request: SkillRequest, now: float) -> SkillCommand | None:
        """Run an explicit primitive (idempotent while the same one runs)."""

        slot = self._slot(observer)
        same = _same_request(slot.request, request)
        if same and slot.skill is not None:
            return self.step(observer, profile, now)
        if same and slot.pending is not None:
            return self._advance(slot, observer, profile, now)
        if now < slot.retry_at and same:
            return None
        if _request_satisfied(observer, request) or not self._voxel_world:
            return None
        slot.request = request
        if request.kind == DIRECTIVE_CLIMB and request.target is not None:
            # A search over stairs, pillars and bridges, spread over decisions.
            return self._begin_search(slot, observer, profile, now, "climb",
                                      ("toward", tuple(request.target), 1.5),
                                      avoid=tuple(slot.avoid))
        abilities = ClimbAbilities.from_observer(observer)
        plan = self._plan_request(observer, request, abilities)
        if plan is None:
            slot.retry_at = now + 2.0
            return None
        purpose = {
            DIRECTIVE_PILLAR_UP: "pillar_up",
            DIRECTIVE_STAIRCASE_UP: "staircase",
            DIRECTIVE_BRIDGE: "bridge",
        }.get(request.kind, "climb")
        self._start(slot, observer, profile, plan, purpose, now, ("request", request))
        return self.step(observer, profile, now)

    def _plan_request(self, observer: PlayerSnapshot, request: SkillRequest,
                      abilities: ClimbAbilities) -> AscentPlan | None:
        start = node_of(observer.position)
        here = observer.position
        if request.kind == DIRECTIVE_PILLAR_UP:
            levels = request.levels
            if request.target is not None:
                target_support = int(round(float(request.target[2]) + SUPPORT_OFFSET))
                levels = max(levels if not request.levels else 0, start[2] - target_support)
            return plan_pillar_up(self.world, start, max(1, levels), abilities)
        if request.kind in (DIRECTIVE_STAIRCASE_UP, DIRECTIVE_BRIDGE):
            direction = request.direction
            if direction is None and request.target is not None:
                direction = (request.target[0] - here[0], request.target[1] - here[1], 0.0)
            if direction is None:
                direction = tuple(observer.orientation)
            if request.kind == DIRECTIVE_STAIRCASE_UP:
                return plan_staircase_up(self.world, start, direction, max(1, request.levels),
                                         abilities)
            return plan_fast_bridge(self.world, start, direction, max(1, request.length),
                                    abilities)
        if request.target is None:
            return None
        return plan_ascent(self.world, start, AscentGoal.toward(request.target, radius=1.5),
                           abilities, radius=24, max_expansions=_MAX_PLAN_EXPANSIONS,
                           time_budget=0.02)


def _same_request(left: SkillRequest | None, right: SkillRequest | None) -> bool:
    """The same order, even as the body (and a moving target) shift a bit."""

    if left is None or right is None or left.kind != right.kind:
        return False
    if left.target is None or right.target is None:
        return left.target is None and right.target is None
    return math.dist(left.target, right.target) <= 2.5


def _request_satisfied(observer: PlayerSnapshot, request: SkillRequest) -> bool:
    """An order whose target height (and spot, for ``climb``) is reached."""

    if request.target is None or request.kind == DIRECTIVE_BRIDGE:
        return False
    start = node_of(observer.position)
    if request.kind == DIRECTIVE_CLIMB:
        return AscentGoal.toward(request.target, radius=1.5).is_goal(start)
    if not observer.grounded:
        return False
    return start[2] <= int(round(float(request.target[2]) + SUPPORT_OFFSET))


def swim_seconds_to_shore(world, observer: PlayerSnapshot, goal: Vector3 | None) -> float:
    """Seconds to swim to a native exit onto main ground (inf: none known).

    The atlas water flow leads every water column to its nearest bank; only
    a one-block bank of the main ground is a real exit for a swimmer. A
    flow ending at a cut-off beach or a cliff counts as no exit at all.
    """

    atlas = getattr(world, "_atlas", None)
    if atlas is None:
        return math.inf
    x, y = int(math.floor(observer.position[0])), int(math.floor(observer.position[1]))
    route = atlas.water_route(x, y)
    if route is None or not route.climbable:
        return math.inf
    goal_index = route.goal_y * atlas.width + route.goal_x
    if int(atlas.regions[goal_index]) != int(getattr(atlas, "main_region_id", 0)):
        return math.inf
    return route.distance * _SWIM_SECONDS_PER_CELL + 1.5


__all__ = [
    "DIRECTIVE_BRIDGE",
    "DIRECTIVE_CLIMB",
    "DIRECTIVE_PILLAR_UP",
    "DIRECTIVE_STAIRCASE_UP",
    "LocomotionSkillDriver",
    "SKILL_DIRECTIVES",
    "SkillRequest",
    "StepKind",
    "swim_seconds_to_shore",
]
