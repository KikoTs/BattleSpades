"""Team brain for the Zombie horde: who hunts whom, and how.

Retail Zombie mode hands the infected a strategic target set: every survivor
is a horde target (the mode reveals entombed survivors and the last
survivor's marker on the map). Each infected bot used to run at the nearest
survivor on its own, so a survivor on a pillar or a sky platform collected
the whole horde in one pile under his feet while everyone else was safe.

``HordeCoordinator`` runs on the gameplay thread (director) once per
perception snapshot and assigns every infected bot one order:

``hunt``      run at the target (spread across survivors with a load penalty
              and hysteresis, so the horde splits instead of dog-piling one).
              The default: the horde rushes.
``tunnel``    claw straight through what stands between the bot and its
              target (a wall, a door, a barricade, a sealed room) whenever
              that is quicker than the way round, or there is no way round.
``dig_root``  claw one site of the minimum collapse cut under an elevated
              survivor (``structure_collapse``): when the cut is dug the
              server's own floating-block rule drops the base. Chosen when
              the cut is quicker than reaching him, or nothing reaches him.
``surround``  the rest of a besieging group waits just outside the footprint
              of what is about to fall, never stacked under it.
``climb``     nothing walks up and no cheap cut exists (a natural spire, a
              thick fortress): dig a staircase / take the climb skill.
``flank``     the progress watchdog fired on a walker: try the target
              from another side (a second alarm lets it claw or climb).

Every choice is a comparison of estimated seconds (walking, from the
survivor's approach flood; clawing, from the claw's cadence), corrected by
what the bots are actually achieving: time without progress counts against
the way being tried.

Pure Python over plain tuples; the director supplies positions, siege
analyses and the world's ``solid`` probe. No server objects, no randomness
(stable ordering by player id), bounded memory (forgets departed bots).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Iterable, Mapping, Sequence

from .structure_collapse import (
    PLAYER_SUPPORT_OFFSET,
    Approach,
    CollapsePlan,
)

Vector3 = tuple[float, float, float]
Cell = tuple[int, int, int]
SolidFn = Callable[[int, int, int], bool]

ROLE_CODES: dict[str, int] = {
    "hunt": 0,
    "flank": 1,
    "dig_root": 2,
    "climb": 3,
    "surround": 4,
    "tunnel": 5,
}
ROLE_NAMES: dict[int, str] = {code: name for name, code in ROLE_CODES.items()}

# Watchdog: no goal progress of at least PROGRESS_STEP blocks for STALL_SECONDS
# raises the bot's stuck level; sustained progress lowers it again. Measured
# over eight maps, three, five and seven seconds reach the survivors equally
# fast; three seconds raises twice the alarms for it, so the wait is five.
PROGRESS_STEP = 1.5
STALL_SECONDS = 5.0
RECOVER_SECONDS = 8.0
MAX_STUCK_LEVEL = 3
# Landed swings count as progress for this long without getting any closer:
# enough for a thick wall, not for digging into a hillside for ever.
WORK_CREDIT_SECONDS = 8.0
# Piling: hunters within this horizontal radius of an elevated target and at
# least PILE_DEPTH blocks below it are standing in a heap under the survivor.
PILE_RADIUS = 2.5
PILE_DEPTH = 3.0
MAX_DIGGERS = 6
# With no cut to dig, a third of the group climbs (at least two, at most
# four): one finished staircase carries everybody, but one bot digging it
# alone leaves the rest of the horde watching.
MIN_CLIMBERS = 2
MAX_CLIMBERS = 4
# The waiting ring stands this far out when the footing's size is unknown,
# and never closer than RING_MIN: just clear of the heap, one stride from
# where a falling survivor lands.
SURROUND_RADIUS = 7.0
RING_MIN = 4.0
RING_CLEARANCE = 2.0
TUNNEL_RANGE = 20.0
FLANK_RADIUS = 9.0
# A survivor at least this many blocks above the horde's floor is "elevated":
# bringing his footing down is worth comparing with walking up to him.
ELEVATED_RISE = 3
# A target that has moved this far since its footing was analysed (it fell,
# it jumped off) is hunted on sight; the old analysis describes an empty spot.
STALE_ANALYSIS_MOVE = 2.5

# Planning speeds, measured with scripts/bot_zombie_horde_scenario.py on the
# retail class speeds (Zombie sprint 13.2 blocks/s, walk 4.0; about half of a
# hunter's travel over broken ground is sprinted).
RUN_SPEED = 8.0
# A flood move is one cardinal cell; real bodies cut the diagonals.
MOVE_BLOCKS = 0.8
# One claw cube of a collapse cut: about three swings (ZOMBIEHAND 0.4 s, 2-10
# damage a cell against 5 for map voxels, 9 for built ones) and the shuffle.
SITE_SECONDS = 1.6
# Clawing a body-sized hole through one wall on the way, and through each
# further column of a thicker one.
WALL_SECONDS = 1.2
COLUMN_SECONDS = 0.4
# Seconds a way must save before the horde changes to it, and how hard time
# without progress counts against the way being tried.
CHOICE_MARGIN = 1.0
STALL_WEIGHT = 2.0
# A hunter's breach decision stands this long unless the ground changes.
BREACH_REFRESH = 0.5
# A hunter on a known way in is sent this many moves along it at a time (far
# enough to clear the corner the bot's own local planner cannot see round),
# but only when that way is a real detour or a climb: longer than the
# straight run by VIA_DETOUR blocks, or ending VIA_RISE blocks up.
VIA_AHEAD = 10
VIA_DETOUR = 4.0
VIA_RISE = 2.0
VIA_ENTRY_REACH = 24.0


@dataclass(frozen=True, slots=True)
class HordeMember:
    player_id: int
    position: Vector3
    is_bot: bool = True
    can_dig: bool = True
    can_build: bool = False
    # A claw swing of this bot landed within the last moments (on terrain or
    # on a survivor): standing still to dig is work, not a stall.
    working: bool = False


@dataclass(frozen=True, slots=True)
class SurvivorTarget:
    player_id: int
    position: Vector3


@dataclass(frozen=True, slots=True)
class SiegeInfo:
    """Director's latest structural analysis of one survivor's footing."""

    isolated: bool
    floor_z: int
    plan: CollapsePlan | None = None
    analysed_at: float = 0.0
    # Walk-in distances around the survivor (None: not analysed yet) and
    # where he stood when they were taken.
    approach: Approach | None = None
    position: Vector3 | None = None


@dataclass(frozen=True, slots=True)
class HordeOrder:
    zombie_id: int
    target_id: int
    role: str
    goal: Vector3
    cells: tuple[Cell, ...] = ()
    aim: Cell | None = None
    stuck_level: int = 0

    @property
    def role_code(self) -> int:
        return ROLE_CODES[self.role]


@dataclass(slots=True)
class _Progress:
    goal_key: tuple
    best_distance: float
    progress_at: float
    level: int = 0
    level_since: float = 0.0
    anchor: Vector3 = (0.0, 0.0, 0.0)
    # When the body last got closer; digging renews ``progress_at`` only for
    # WORK_CREDIT_SECONDS after this.
    closed_at: float = 0.0


@dataclass(slots=True)
class HordeMetrics:
    stuck_events: int = 0
    tunnel_orders: int = 0
    dig_orders: int = 0
    surround_orders: int = 0
    climb_orders: int = 0
    max_pile: int = 0
    piles: dict[int, int] = field(default_factory=dict)
    # Targets currently besieged because the cut beats the walk although a
    # walk exists, and hunters sent through a wall before any stall.
    collapse_choices: int = 0
    breach_choices: int = 0


def _xy_distance(a: Vector3, b: Vector3) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _floor_of(position: Vector3) -> int:
    return int(round(float(position[2]) + PLAYER_SUPPORT_OFFSET))


def _ring_point(center: Vector3, slot: int, slots: int, radius: float,
                floor_z: int, phase: float = 0.0) -> Vector3:
    angle = phase + 2.0 * math.pi * (slot % max(1, slots)) / max(1, slots)
    return (
        float(center[0]) + radius * math.cos(angle),
        float(center[1]) + radius * math.sin(angle),
        float(floor_z) - PLAYER_SUPPORT_OFFSET,
    )


def standable_near(solid: SolidFn | None, point: Vector3, floor_z: int,
                   *, span: int = 3) -> Vector3 | None:
    """``point`` snapped onto an open floor within ``span`` blocks, or None.

    A flank or ring spot inside a wall or over a drop is worse than none:
    the bot stalls on it and the watchdog ends up tunnelling.
    """

    if solid is None:
        return point
    x, y = int(math.floor(point[0])), int(math.floor(point[1]))
    if not (0 <= x < 512 and 0 <= y < 512):
        return None
    for offset in sorted(range(-span, span + 1), key=abs):
        floor = int(floor_z) + offset
        if not 2 <= floor < 239:
            continue
        if solid(x, y, floor) and not solid(x, y, floor - 1) and not solid(x, y, floor - 2):
            return (x + 0.5, y + 0.5, float(floor) - PLAYER_SUPPORT_OFFSET)
    return None


def pile_count(target: Vector3, hunters: Iterable[Vector3]) -> int:
    """Hunters heaped under an elevated target (the anti-pattern we remove)."""

    return sum(
        1 for h in hunters
        if _xy_distance(h, target) <= PILE_RADIUS
        and float(h[2]) - float(target[2]) >= PILE_DEPTH
    )


def dig_stand_spot(solid: SolidFn | None, site: Cell, floor_z: int,
                   away_from: Vector3 | None = None) -> Vector3:
    """Where a zombie stands to claw ``site``: an open floor beside it.

    Prefers the side facing away from ``away_from`` (the survivor), so the
    digger works from outside the structure's footprint rather than under
    the part that is about to fall.
    """

    x, y, z = site
    if solid is not None:
        best = None
        for radius in (1, 2):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    cx, cy = x + dx, y + dy
                    for floor in range(max(z - 1, 0), min(z + 5, 240)):
                        if not solid(cx, cy, floor):
                            continue
                        if (floor - z <= 3 and not solid(cx, cy, floor - 1)
                                and not solid(cx, cy, floor - 2)):
                            outward = 0.0
                            if away_from is not None:
                                outward = -round(math.hypot(
                                    cx + 0.5 - float(away_from[0]),
                                    cy + 0.5 - float(away_from[1])), 1)
                            score = (radius, abs(floor - floor_z), outward,
                                     abs(dx) + abs(dy))
                            if best is None or score < best[0]:
                                best = (score, (cx + 0.5, cy + 0.5,
                                                float(floor) - PLAYER_SUPPORT_OFFSET))
                        break
            if best is not None:
                return best[1]
    return (x + 0.5, y + 0.5, float(floor_z) - PLAYER_SUPPORT_OFFSET)


def tunnel_cells(solid: SolidFn | None, start: Vector3, target: Vector3,
                 *, length: float = 4.0) -> tuple[Cell, ...]:
    """Solid voxels on the body-height line from ``start`` toward ``target``."""

    if solid is None:
        return ()
    sx, sy, sz = (float(v) for v in start)
    tx, ty = float(target[0]), float(target[1])
    dx, dy = tx - sx, ty - sy
    span = math.hypot(dx, dy)
    if span < 1e-6:
        return ()
    ux, uy = dx / span, dy / span
    floor = int(round(sz + PLAYER_SUPPORT_OFFSET))
    # Rising target: aim the tunnel one block up per two forward (a ramp).
    rise = 1 if float(target[2]) < sz - 1.5 else 0
    cells: list[Cell] = []
    step = 0.5
    t = 0.75
    while t <= length:
        cx = int(math.floor(sx + ux * t))
        cy = int(math.floor(sy + uy * t))
        lift = int(t // 2.0) * rise
        for level in (floor - 1 - lift, floor - 2 - lift):
            cell = (cx, cy, level)
            if cell not in cells and 0 <= level < 239 and solid(*cell):
                cells.append(cell)
        t += step
    return tuple(cells[:8])


@dataclass(frozen=True, slots=True)
class WallLine:
    """What a straight run at the target would have to claw through."""

    walls: int       # separate obstacles on the line
    columns: int     # solid columns in total (their thickness)
    first: float     # blocks from the start to the first of them


def wall_line(solid: SolidFn | None, start: Vector3, target: Vector3,
              *, limit: float = TUNNEL_RANGE + 4.0) -> WallLine | None:
    """Walk the straight line toward ``target`` and count what blocks a body.

    Follows the ground like a walker (one block up, a few down), so a
    terrace is not a wall; a column whose head-height cell is solid is. Only
    for a target on roughly the hunter's level: None when it is more than
    two blocks higher or lower (digging does not climb through air), out of
    ``limit``, or when the line leaves the ground altogether.
    """

    if solid is None:
        return None
    sx, sy = float(start[0]), float(start[1])
    dx, dy = float(target[0]) - sx, float(target[1]) - sy
    span = math.hypot(dx, dy)
    floor = _floor_of(start)
    if span < 1.0 or span > limit or abs(_floor_of(target) - floor) > 2:
        return None
    ux, uy = dx / span, dy / span
    walls = columns = 0
    first = -1.0
    in_wall = False
    seen = (int(math.floor(sx)), int(math.floor(sy)))
    t = 0.5
    while t < span - 0.5:
        column = (int(math.floor(sx + ux * t)), int(math.floor(sy + uy * t)))
        t += 0.5
        if column == seen:
            continue
        seen = column
        x, y = column
        if not (0 <= x < 512 and 0 <= y < 512):
            return None
        if solid(x, y, floor - 2) or (solid(x, y, floor - 1) and solid(x, y, floor - 3)):
            # Head height is blocked (or a step up under a low ceiling).
            columns += 1
            if not in_wall:
                walls += 1
                in_wall = True
                if first < 0.0:
                    first = t - 0.5
            continue
        in_wall = False
        if solid(x, y, floor - 1):
            floor -= 1            # a step up
        elif not solid(x, y, floor):
            for drop in range(1, 5):
                if solid(x, y, floor + drop):
                    floor += drop
                    break
            else:
                return None       # a pit or a ledge: not a walking line
    return WallLine(walls, columns, first)


class HordeCoordinator:
    """Stateful but bounded assignment of infected bots to survivors."""

    def __init__(self) -> None:
        self._targets: dict[int, int] = {}
        self._progress: dict[int, _Progress] = {}
        # Per survivor: besieged (True) or pursued; per zombie: its last role.
        self._besieged: dict[int, bool] = {}
        self._roles: dict[int, tuple[int, str]] = {}
        # Per zombie: (decided at, target, its column, the breach decision).
        self._breaches: dict[int, tuple] = {}
        # Per zombie: (searched at, the approach used, its column, the via
        # point, the flood's nearest floor when it stands outside the flood).
        self._vias: dict[int, tuple] = {}
        # Per survivor: the highest stuck level among the hunters sent at him.
        self._stuck: dict[int, int] = {}
        self.metrics = HordeMetrics()

    def reset(self) -> None:
        self._targets.clear()
        self._progress.clear()
        self._besieged.clear()
        self._roles.clear()
        self._breaches.clear()
        self._vias.clear()
        self._stuck.clear()
        self.metrics = HordeMetrics()

    def struggling(self, target_id: int) -> bool:
        """Is some hunter assigned to this survivor stuck (last snapshot)?"""

        return self._stuck.get(int(target_id), 0) > 0

    # -- target assignment -------------------------------------------------

    def assign_targets(
        self,
        zombies: Sequence[HordeMember],
        survivors: Sequence[SurvivorTarget],
    ) -> dict[int, int]:
        """Spread hunters over survivors: distance plus a load penalty.

        Closest hunters claim first; a hunter keeps its previous target
        unless another is clearly cheaper (hysteresis), so assignments do not
        flap every snapshot. Human infected count toward a target's load
        (they chase the nearest survivor) but receive no orders.
        """

        if not survivors:
            self._targets.clear()
            return {}
        alive = {s.player_id: s for s in survivors}
        load: dict[int, int] = {s.player_id: 0 for s in survivors}
        for member in zombies:
            if not member.is_bot:
                nearest = min(survivors, key=lambda s: (
                    math.dist(member.position, s.position), s.player_id))
                load[nearest.player_id] += 1
        bots = [m for m in zombies if m.is_bot]
        cap = max(1, math.ceil(len(zombies) / len(survivors))) + 1

        def nearest_distance(member: HordeMember) -> float:
            return min(math.dist(member.position, s.position) for s in survivors)

        result: dict[int, int] = {}
        for member in sorted(bots, key=lambda m: (nearest_distance(m), m.player_id)):
            def cost(survivor: SurvivorTarget) -> float:
                distance = math.dist(member.position, survivor.position)
                return distance * (1.0 + 0.35 * load[survivor.player_id]) + (
                    1000.0 if load[survivor.player_id] >= cap else 0.0)

            best = min(survivors, key=lambda s: (cost(s), s.player_id))
            previous = self._targets.get(member.player_id)
            if (previous in alive and previous != best.player_id
                    and load[previous] < cap
                    and cost(alive[previous]) <= cost(best) * 1.3 + 8.0):
                best = alive[previous]
            result[member.player_id] = best.player_id
            load[best.player_id] += 1
        self._targets = dict(result)
        return result

    # -- watchdog ----------------------------------------------------------

    def _observe_progress(self, zombie_id: int, position: Vector3, goal: Vector3,
                          goal_key: tuple, now: float, working: bool = False) -> int:
        distance = math.dist(position, goal)
        record = self._progress.get(zombie_id)
        if record is None or record.goal_key != goal_key:
            level = record.level if record is not None else 0
            record = _Progress(goal_key, distance, now, level, now, tuple(position), now)
            self._progress[zombie_id] = record
            return record.level
        if distance + PROGRESS_STEP <= record.best_distance:
            record.best_distance = distance
            record.progress_at = now
            record.closed_at = now
            record.anchor = tuple(position)
            if record.level > 0 and now - record.level_since >= RECOVER_SECONDS:
                record.level -= 1
                record.level_since = now
        elif working and now - record.closed_at <= WORK_CREDIT_SECONDS:
            record.progress_at = now
        elif distance > 3.0 and now - record.progress_at >= STALL_SECONDS:
            if math.dist(position, record.anchor) > 4.0:
                # Still covering ground (a fleeing target): a chase, not a
                # stall. Restart the window from here.
                record.progress_at = now
                record.anchor = tuple(position)
                record.best_distance = distance
                return record.level
            if record.level < MAX_STUCK_LEVEL:
                record.level += 1
                self.metrics.stuck_events += 1
            record.level_since = now
            record.progress_at = now
            record.closed_at = now
            record.anchor = tuple(position)
            record.best_distance = distance
        if distance <= 3.0:
            record.progress_at = now
            record.closed_at = now
            record.best_distance = min(record.best_distance, distance)
        return record.level

    def _stalled_seconds(self, member: HordeMember, target: SurvivorTarget,
                         now: float) -> float:
        """How long this hunter has gone without closing on this target."""

        record = self._progress.get(member.player_id)
        if record is None or not record.goal_key or record.goal_key[0] != target.player_id:
            return 0.0
        if math.dist(member.position, record.anchor) > 4.0:
            return 0.0  # covering ground: a detour or a chase, not a stall
        idle = now - record.progress_at
        return idle if idle >= 1.0 else 0.0

    # -- estimates ---------------------------------------------------------

    def _walk_seconds(self, member: HordeMember, target: SurvivorTarget,
                      siege: SiegeInfo | None, now: float) -> float:
        return self._walk_estimate(member, target, siege, now)[0]

    def _walk_estimate(self, member: HordeMember, target: SurvivorTarget,
                       siege: SiegeInfo | None, now: float) -> tuple[float, bool]:
        """Seconds for this hunter to reach the target on foot (inf: never).

        Exact inside the survivor's approach flood; outside it only a lower
        bound from the flood's frontier (the second value is then True: the
        real walk is at least this long); a straight-line guess with no
        analysis. Time already spent getting nowhere is added on top, so a
        way that is not working stops looking cheap.
        """

        flat = _xy_distance(member.position, target.position)
        climb = max(0.0, float(member.position[2]) - float(target.position[2]))
        approach = siege.approach if siege is not None else None
        bound = False
        if approach is None:
            if siege is not None and siege.isolated:
                return math.inf, False
            blocks = flat + 2.0 * climb
        else:
            moves = approach.moves_from(member.position)
            if moves is not None:
                blocks = max(moves * MOVE_BLOCKS, flat)
            elif approach.closed:
                return math.inf, False
            else:
                bound = True
                blocks = max(approach.frontier * MOVE_BLOCKS + max(0.0, flat - approach.radius),
                             flat + 2.0 * climb)
        return (blocks / RUN_SPEED
                + STALL_WEIGHT * self._stalled_seconds(member, target, now)), bound

    @staticmethod
    def _collapse_seconds(plan: CollapsePlan, group: Sequence[HordeMember],
                          solid: SolidFn | None) -> float:
        """Seconds for this group to dig what is left of the cut."""

        cut = [c for c in plan.cut if solid is None or solid(*c)]
        if not cut:
            return 0.0
        sites = [s for s in plan.sites if any(
            max(abs(c[0] - s[0]), abs(c[1] - s[1]), abs(c[2] - s[2])) <= 1 for c in cut)] or [cut[0]]
        diggers = [m for m in group if m.can_dig]
        if not diggers:
            return math.inf
        crew = min(len(diggers), len(sites))
        # The crew is complete when its farthest member has walked in.
        walks = sorted(
            min(math.dist(m.position, (s[0] + .5, s[1] + .5, s[2] + .5)) for s in sites)
            for m in diggers)
        hardness = max(1.0, plan.cost / max(1, len(plan.cut)))
        return (walks[crew - 1] / RUN_SPEED
                + math.ceil(len(sites) / crew) * SITE_SECONDS * hardness)

    # -- orders ------------------------------------------------------------

    def plan(
        self,
        zombies: Sequence[HordeMember],
        survivors: Sequence[SurvivorTarget],
        sieges: Mapping[int, SiegeInfo],
        now: float,
        *,
        solid: SolidFn | None = None,
    ) -> dict[int, HordeOrder]:
        """One order per infected bot."""

        known = {m.player_id for m in zombies if m.is_bot}
        for zombie_id in tuple(self._progress):
            if zombie_id not in known:
                self._progress.pop(zombie_id, None)
                self._roles.pop(zombie_id, None)
                self._breaches.pop(zombie_id, None)
                self._vias.pop(zombie_id, None)
        targets = self.assign_targets(zombies, survivors)
        by_id = {s.player_id: s for s in survivors}
        for survivor_id in tuple(self._besieged):
            if survivor_id not in by_id:
                self._besieged.pop(survivor_id, None)
        members = {m.player_id: m for m in zombies}
        groups: dict[int, list[HordeMember]] = {}
        for zombie_id, target_id in targets.items():
            groups.setdefault(target_id, []).append(members[zombie_id])
        orders: dict[int, HordeOrder] = {}
        self.metrics.piles = {}
        self._stuck = {}
        for target_id, group in sorted(groups.items()):
            target = by_id[target_id]
            group.sort(key=lambda m: (math.dist(m.position, target.position), m.player_id))
            siege = sieges.get(target_id)
            if (siege is not None and siege.position is not None
                    and math.dist(siege.position, target.position) > STALE_ANALYSIS_MOVE):
                siege = None  # he fell or jumped: that footing is history
            if self._besiege(target, group, siege, solid, now):
                group_orders = self._siege_orders(target, group, siege, solid)
            else:
                group_orders = self._pursuit_orders(target, group, siege, solid, now)
            pile = pile_count(target.position, (m.position for m in group))
            self.metrics.piles[target_id] = pile
            self.metrics.max_pile = max(self.metrics.max_pile, pile)
            for order in group_orders:
                member = members[order.zombie_id]
                # The watchdog measures closing on the prey for every way of
                # hunting it, so a switch from running to clawing does not
                # restart its clock; standing orders are measured by spot.
                if order.role in ("hunt", "tunnel"):
                    key = (order.target_id, "hunt")
                    goal = tuple(float(v) for v in target.position)
                else:
                    key = (order.target_id, order.role, order.aim,
                           tuple(int(v) for v in order.goal[:2]))
                    goal = order.goal
                level = self._observe_progress(
                    member.player_id, member.position, goal, key, now, member.working)
                self._stuck[target_id] = max(self._stuck.get(target_id, 0), level)
                order = self._escalate(order, member, target, level, solid)
                if order.role == "tunnel":
                    self.metrics.tunnel_orders += 1
                self._roles[order.zombie_id] = (order.target_id, order.role)
                orders[order.zombie_id] = order
        return orders

    def _besiege(self, target: SurvivorTarget, group: Sequence[HordeMember],
                 siege: SiegeInfo | None, solid: SolidFn | None, now: float) -> bool:
        """Bring the footing down (or climb it) instead of walking to him?

        Always when nothing walks in. Otherwise only for a survivor well
        above the horde whose collapse cut is known and quicker to dig than
        the walk up is to make, with a margin and a memory so the horde does
        not change its mind every snapshot.
        """

        target_id = target.player_id
        if siege is None:
            self._besieged.pop(target_id, None)
            return False
        approach = siege.approach
        if approach is not None:
            isolated = approach.closed and not any(
                approach.moves_from(m.position) is not None for m in group)
            if isolated and siege.floor_z - approach.support_z < ELEVATED_RISE:
                # Walled in on the horde's own level (a sealed room, a pit
                # bunker): that is a wall to claw through, not a tower.
                self._besieged.pop(target_id, None)
                return False
        else:
            isolated = siege.isolated
        if isolated:
            self._besieged[target_id] = True
            return True
        support_z = approach.support_z if approach is not None else _floor_of(target.position)
        if siege.plan is None or siege.floor_z - support_z < ELEVATED_RISE:
            self._besieged.pop(target_id, None)
            return False
        walk = min((self._walk_seconds(m, target, siege, now) for m in group),
                   default=math.inf)
        dig = self._collapse_seconds(siege.plan, group, solid)
        if self._besieged.get(target_id):
            chosen = dig <= walk + CHOICE_MARGIN
        else:
            chosen = dig + CHOICE_MARGIN < walk
        if chosen:
            if not self._besieged.get(target_id):
                self.metrics.collapse_choices += 1
            self._besieged[target_id] = True
        else:
            self._besieged.pop(target_id, None)
        return chosen

    def _pursuit_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                        siege: SiegeInfo | None, solid: SolidFn | None,
                        now: float) -> list[HordeOrder]:
        """Everyone runs at the prey; whoever is quicker through a wall claws."""

        goal = tuple(float(v) for v in target.position)
        orders = []
        for member in group:
            breach = self._breach(member, target, siege, solid, now)
            if breach is not None:
                orders.append(HordeOrder(member.player_id, target.player_id, "tunnel",
                                         breach[0], cells=breach[1],
                                         aim=breach[1][0] if breach[1] else None))
            else:
                orders.append(HordeOrder(member.player_id, target.player_id, "hunt",
                                         self._via(member, target, siege, now) or goal))
        return orders

    def _via(self, member: HordeMember, target: SurvivorTarget,
             siege: SiegeInfo | None, now: float) -> Vector3 | None:
        """The next stretch of the known way in, when it is not the straight run.

        The approach flood knows the stairs round the back, the ramp, the
        door; a bot's own planner sees a dozen blocks and otherwise stands
        under a roof it could walk up to. None for a straight run, an
        unexplored hunter far from every explored floor, or no analysis.
        """

        approach = siege.approach if siege is not None else None
        if approach is None:
            return None
        here = (int(math.floor(member.position[0])), int(math.floor(member.position[1])))
        cached = self._vias.get(member.player_id)
        fresh = (cached is not None and cached[1] is approach
                 and now - cached[0] < BREACH_REFRESH)
        if fresh and cached[2] == here:
            return cached[3]
        via = None
        entry = None
        cell = approach.cell_at(member.position)
        if cell is None and not approach.closed:
            # Outside the flood: where its nearest floor is changes slowly,
            # so the search is repeated on the refresh, not at every stride.
            entry = cached[4] if fresh else approach.entry_near(
                member.position, VIA_ENTRY_REACH)
            cell = entry
        if cell is not None:
            moves = approach.steps[cell]
            flat = _xy_distance(member.position, target.position)
            rise = float(member.position[2]) - float(target.position[2])
            if moves > VIA_AHEAD and (moves * MOVE_BLOCKS > flat + VIA_DETOUR
                                      or abs(rise) >= VIA_RISE):
                x, y, z = approach.waypoint(cell, VIA_AHEAD)
                via = (x + 0.5, y + 0.5, float(z) - PLAYER_SUPPORT_OFFSET)
        self._vias[member.player_id] = (
            cached[0] if fresh else now, approach, here, via, entry)
        return via

    def _breach(self, member: HordeMember, target: SurvivorTarget,
                siege: SiegeInfo | None, solid: SolidFn | None,
                now: float) -> tuple[Vector3, tuple[Cell, ...]] | None:
        """Where to go and what to claw when straight through beats round.

        None when the hunter cannot dig, is out of range, has nothing in the
        way, or walks there sooner. The line is re-walked when the hunter or
        its prey changes column, a clawed cell goes, or after BREACH_REFRESH.
        """

        if solid is None or not member.can_dig:
            return None
        flat = _xy_distance(member.position, target.position)
        if flat > TUNNEL_RANGE:
            self._breaches.pop(member.player_id, None)
            return None
        here = (int(math.floor(member.position[0])), int(math.floor(member.position[1])),
                int(math.floor(target.position[0])), int(math.floor(target.position[1])))
        cached = self._breaches.get(member.player_id)
        if (cached is not None and cached[1] == target.player_id and cached[2] == here
                and now - cached[0] < BREACH_REFRESH
                and (cached[3] is None or all(solid(*cell) for cell in cached[3][1]))):
            return cached[3]
        decision = self._breach_now(member, target, siege, solid, now, flat)
        self._breaches[member.player_id] = (now, target.player_id, here, decision)
        return decision

    def _breach_now(self, member: HordeMember, target: SurvivorTarget,
                    siege: SiegeInfo | None, solid: SolidFn, now: float,
                    flat: float) -> tuple[Vector3, tuple[Cell, ...]] | None:
        line = wall_line(solid, member.position, target.position)
        if line is None or line.walls == 0:
            return None
        dig = (flat / RUN_SPEED + line.walls * WALL_SECONDS
               + (line.columns - line.walls) * COLUMN_SECONDS)
        walk, at_least = self._walk_estimate(member, target, siege, now)
        clawing = self._roles.get(member.player_id) == (target.player_id, "tunnel")
        # A lower bound needs no safety margin: the walk is no shorter.
        margin = 0.0 if at_least else CHOICE_MARGIN
        if clawing and member.working:
            pass  # a claw that is landing is not given up for a walk that
            # only looks short again because the claw counts as progress
        elif not (dig <= walk + CHOICE_MARGIN if clawing else dig + margin <= walk):
            return None
        if not clawing:
            self.metrics.breach_choices += 1
        cells = tunnel_cells(solid, member.position, target.position)
        position = tuple(float(v) for v in target.position)
        if line.first <= 1.75:
            return position, cells
        # The wall is still some strides off: run to its face first, on the
        # line, or the route round it carries the bot along the wall instead.
        back = line.first - 1.0
        face = (float(member.position[0])
                + (float(target.position[0]) - float(member.position[0])) / flat * back,
                float(member.position[1])
                + (float(target.position[1]) - float(member.position[1])) / flat * back,
                float(member.position[2]))
        spot = standable_near(solid, face, _floor_of(member.position))
        return (spot or position), cells

    def _siege_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                      siege: SiegeInfo, solid: SolidFn | None) -> list[HordeOrder]:
        orders: list[HordeOrder] = []
        remaining = list(group)
        plan = siege.plan
        if plan is not None:
            cut = [c for c in plan.cut if solid is None or solid(*c)]
            sites = [s for s in plan.sites if any(
                max(abs(c[0] - s[0]), abs(c[1] - s[1]), abs(c[2] - s[2])) <= 1 for c in cut)]
            if not sites and cut:
                sites = [cut[0]]
            per_site = 2 if len(remaining) >= 2 * len(sites) + 2 else 1
            slots = [site for site in sites[:MAX_DIGGERS] for _ in range(per_site)]
            # Nearest free digger per site, so the horde splits by side.
            for site in slots:
                if not remaining:
                    break
                member = min(remaining, key=lambda m: (
                    math.dist(m.position, (site[0] + .5, site[1] + .5, site[2] + .5)),
                    m.player_id))
                remaining.remove(member)
                site_cells = tuple(sorted(
                    c for c in cut
                    if max(abs(c[0] - site[0]), abs(c[1] - site[1]),
                           abs(c[2] - site[2])) <= 1)) or (site,)
                orders.append(HordeOrder(
                    member.player_id, target.player_id, "dig_root",
                    dig_stand_spot(solid, site, siege.floor_z, target.position),
                    cells=site_cells, aim=site,
                ))
                self.metrics.dig_orders += 1
        else:
            crew = min(MAX_CLIMBERS, max(MIN_CLIMBERS, math.ceil(len(remaining) / 3)))
            climbers = [m for m in remaining if m.can_dig or m.can_build][:crew]
            for member in climbers:
                remaining.remove(member)
                orders.append(HordeOrder(
                    member.player_id, target.player_id, "climb",
                    tuple(float(v) for v in target.position),
                ))
                self.metrics.climb_orders += 1
        # Everyone else stands just clear of the footing that is coming down
        # (its measured footprint when the flood closed), one stride from
        # where the survivor lands.
        approach = siege.approach
        radius = SURROUND_RADIUS
        if approach is not None and approach.closed:
            radius = max(RING_MIN, approach.radius + RING_CLEARANCE)
        for slot, member in enumerate(remaining):
            raw = _ring_point(target.position, slot, max(4, len(remaining)),
                              radius + 1.5 * (slot % 2), siege.floor_z,
                              phase=0.6 * target.player_id)
            goal = standable_near(solid, raw, siege.floor_z) or raw
            orders.append(HordeOrder(member.player_id, target.player_id,
                                     "surround", goal))
            self.metrics.surround_orders += 1
        return orders

    def _escalate(self, order: HordeOrder, member: HordeMember,
                  target: SurvivorTarget, level: int,
                  solid: SolidFn | None) -> HordeOrder:
        """A stalled walker tries another side, then digs, then climbs."""

        if level <= 0:
            return order
        if order.role in ("surround", "dig_root"):
            if level >= 2 and order.role == "dig_root":
                cells = tunnel_cells(solid, member.position, order.goal, length=3.0)
                if cells:
                    return HordeOrder(order.zombie_id, order.target_id, "tunnel",
                                      order.goal, cells=cells + order.cells,
                                      aim=order.aim, stuck_level=level)
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        if order.role == "tunnel" and (level < 2 or member.working):
            # Already clawing by choice; give it up only when the swings
            # have stopped landing and the stall has lasted.
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        # Clawing only pays off near the prey; far away the route planner's
        # own breach edges and detours are the better tool.
        near = _xy_distance(member.position, target.position) <= TUNNEL_RANGE
        position = tuple(float(v) for v in target.position)
        # The first alarm sends a walker round another side: where clawing
        # was the quicker way the estimate has already chosen it (_breach),
        # and digging at every rise of rough ground holds a horde up (on
        # MayanJungle it kept half of it fifteen blocks short of the prey).
        cells = (tunnel_cells(solid, member.position, target.position)
                 if near and level >= 2 and order.role != "tunnel" else ())
        if cells and member.can_dig:
            return HordeOrder(order.zombie_id, order.target_id, "tunnel", position,
                              cells=cells, aim=cells[0], stuck_level=level)
        above = float(member.position[2]) - float(target.position[2]) >= 2.0
        if level >= 2 and near and above and (member.can_dig or member.can_build):
            return HordeOrder(order.zombie_id, order.target_id, "climb", position,
                              stuck_level=level)
        floor_z = _floor_of(target.position)
        goal = standable_near(solid, _ring_point(
            target.position, member.player_id + level, 4, FLANK_RADIUS, floor_z,
            phase=math.pi / 4.0), floor_z)
        if goal is None:
            return HordeOrder(order.zombie_id, order.target_id, order.role,
                              order.goal, order.cells, order.aim, level)
        return HordeOrder(order.zombie_id, order.target_id, "flank", goal,
                          stuck_level=level)


__all__ = [
    "HordeCoordinator",
    "HordeMember",
    "HordeMetrics",
    "HordeOrder",
    "ROLE_CODES",
    "ROLE_NAMES",
    "SiegeInfo",
    "SurvivorTarget",
    "WallLine",
    "dig_stand_spot",
    "pile_count",
    "standable_near",
    "tunnel_cells",
    "wall_line",
]
