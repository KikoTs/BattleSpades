"""Team brain for the Zombie horde: who hunts whom, and how.

Retail Zombie mode hands the infected a strategic target set: every survivor
is a horde target (the mode reveals entombed survivors and the last
survivor's marker on the map). Each infected bot used to run at the nearest
survivor on its own, so a survivor on a pillar or a sky platform collected
the whole horde in one pile under his feet while everyone else was safe.

``HordeCoordinator`` runs on the gameplay thread (director) once per
perception snapshot and assigns every infected bot one order:

``hunt``      chase the target (spread across survivors with a load penalty
              and hysteresis, so the horde splits instead of dog-piling one).
``flank``     approach a contested target from another side.
``dig_root``  claw one site of the minimum collapse cut under an elevated
              survivor (``structure_collapse``): when the cut is dug the
              server's own floating-block rule drops the base.
``climb``     no cheap cut exists (a natural spire, a thick fortress): dig a
              staircase up / take the movement layer's climb skill.
``surround``  everyone else waits on a wide ring around an elevated target's
              base, never stacked under it, ready for the fall.
``tunnel``    the progress watchdog fired: claw straight through whatever is
              between the bot and its target.

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
# raises the bot's stuck level (flank -> tunnel -> climb); sustained progress
# lowers it again.
PROGRESS_STEP = 1.5
STALL_SECONDS = 7.0
RECOVER_SECONDS = 10.0
MAX_STUCK_LEVEL = 3
# Piling: hunters within this horizontal radius of an elevated target and at
# least PILE_DEPTH blocks below it are standing in a heap under the survivor.
PILE_RADIUS = 2.5
PILE_DEPTH = 3.0
MAX_DIGGERS = 6
MAX_CLIMBERS = 2
SURROUND_RADIUS = 7.0
TUNNEL_RANGE = 20.0
FLANK_MAX_RANGE = 48.0


@dataclass(frozen=True, slots=True)
class HordeMember:
    player_id: int
    position: Vector3
    is_bot: bool = True
    can_dig: bool = True
    can_build: bool = False


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


@dataclass(slots=True)
class HordeMetrics:
    stuck_events: int = 0
    tunnel_orders: int = 0
    dig_orders: int = 0
    surround_orders: int = 0
    climb_orders: int = 0
    max_pile: int = 0
    piles: dict[int, int] = field(default_factory=dict)


def _xy_distance(a: Vector3, b: Vector3) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


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


class HordeCoordinator:
    """Stateful but bounded assignment of infected bots to survivors."""

    def __init__(self) -> None:
        self._targets: dict[int, int] = {}
        self._progress: dict[int, _Progress] = {}
        self.metrics = HordeMetrics()

    def reset(self) -> None:
        self._targets.clear()
        self._progress.clear()
        self.metrics = HordeMetrics()

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
                          goal_key: tuple, now: float) -> int:
        distance = math.dist(position, goal)
        record = self._progress.get(zombie_id)
        if record is None or record.goal_key != goal_key:
            level = record.level if record is not None else 0
            record = _Progress(goal_key, distance, now, level, now, tuple(position))
            self._progress[zombie_id] = record
            return record.level
        if distance + PROGRESS_STEP <= record.best_distance:
            record.best_distance = distance
            record.progress_at = now
            record.anchor = tuple(position)
            if record.level > 0 and now - record.level_since >= RECOVER_SECONDS:
                record.level -= 1
                record.level_since = now
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
            record.anchor = tuple(position)
            record.best_distance = distance
        if distance <= 3.0:
            record.progress_at = now
            record.best_distance = min(record.best_distance, distance)
        return record.level

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
        targets = self.assign_targets(zombies, survivors)
        by_id = {s.player_id: s for s in survivors}
        members = {m.player_id: m for m in zombies}
        groups: dict[int, list[HordeMember]] = {}
        for zombie_id, target_id in targets.items():
            groups.setdefault(target_id, []).append(members[zombie_id])
        orders: dict[int, HordeOrder] = {}
        self.metrics.piles = {}
        for target_id, group in sorted(groups.items()):
            target = by_id[target_id]
            group.sort(key=lambda m: (math.dist(m.position, target.position), m.player_id))
            siege = sieges.get(target_id)
            if siege is not None and siege.isolated:
                group_orders = self._siege_orders(target, group, siege, solid)
            else:
                group_orders = self._pursuit_orders(target, group, solid)
            pile = pile_count(target.position, (m.position for m in group))
            self.metrics.piles[target_id] = pile
            self.metrics.max_pile = max(self.metrics.max_pile, pile)
            for order in group_orders:
                member = members[order.zombie_id]
                key = (order.target_id, order.role, order.aim,
                       tuple(int(v) for v in order.goal[:2]) if order.role != "hunt" else ())
                level = self._observe_progress(
                    member.player_id, member.position, order.goal, key, now)
                order = self._escalate(order, member, target, level, solid)
                if order.role == "tunnel":
                    self.metrics.tunnel_orders += 1
                orders[order.zombie_id] = order
        return orders

    def _pursuit_orders(self, target: SurvivorTarget, group: Sequence[HordeMember],
                        solid: SolidFn | None = None) -> list[HordeOrder]:
        orders = []
        floor_z = int(round(float(target.position[2]) + PLAYER_SUPPORT_OFFSET))
        for rank, member in enumerate(group):
            distance = math.dist(member.position, target.position)
            # Flanking pays off at mid range; from across the map the terrain
            # decides the route and a side point only lengthens it.
            mid_range = 12.0 < distance <= FLANK_MAX_RANGE
            goal = None
            if rank >= 2 and mid_range:
                # The two nearest go straight in; the rest come from other
                # sides so one choke point never holds the whole horde.
                goal = standable_near(solid, _ring_point(
                    target.position, rank - 2, max(3, len(group) - 2),
                    6.0, floor_z, phase=0.6 * target.player_id), floor_z)
            if goal is not None:
                orders.append(HordeOrder(member.player_id, target.player_id,
                                         "flank", goal))
            else:
                orders.append(HordeOrder(member.player_id, target.player_id,
                                         "hunt", tuple(float(v) for v in target.position)))
        return orders

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
            climbers = [m for m in remaining if m.can_dig or m.can_build][:MAX_CLIMBERS]
            for member in climbers:
                remaining.remove(member)
                orders.append(HordeOrder(
                    member.player_id, target.player_id, "climb",
                    tuple(float(v) for v in target.position),
                ))
                self.metrics.climb_orders += 1
        for slot, member in enumerate(remaining):
            radius = SURROUND_RADIUS + 2.0 * (slot % 2)
            raw = _ring_point(target.position, slot, max(4, len(remaining)),
                              radius, siege.floor_z, phase=0.6 * target.player_id)
            goal = standable_near(solid, raw, siege.floor_z) or raw
            orders.append(HordeOrder(member.player_id, target.player_id,
                                     "surround", goal))
            self.metrics.surround_orders += 1
        return orders

    def _escalate(self, order: HordeOrder, member: HordeMember,
                  target: SurvivorTarget, level: int,
                  solid: SolidFn | None) -> HordeOrder:
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
        floor_z = int(round(float(target.position[2]) + PLAYER_SUPPORT_OFFSET))
        if level == 1 and order.role in ("hunt", "flank"):
            goal = standable_near(solid, _ring_point(
                target.position, member.player_id, 4, 9.0, floor_z,
                phase=math.pi / 4.0), floor_z)
            if goal is None:
                return HordeOrder(order.zombie_id, order.target_id, order.role,
                                  order.goal, order.cells, order.aim, level)
            return HordeOrder(order.zombie_id, order.target_id, "flank", goal,
                              stuck_level=level)
        # Clawing a straight tunnel only pays off near the prey; far away the
        # route planner's own breach edges and detours are the better tool.
        near = _xy_distance(member.position, target.position) <= TUNNEL_RANGE
        cells = tunnel_cells(solid, member.position, target.position) if near else ()
        if cells and member.can_dig:
            return HordeOrder(order.zombie_id, order.target_id, "tunnel",
                              tuple(float(v) for v in target.position),
                              cells=cells, aim=cells[0], stuck_level=level)
        if level >= MAX_STUCK_LEVEL and near and (member.can_dig or member.can_build):
            return HordeOrder(order.zombie_id, order.target_id, "climb",
                              tuple(float(v) for v in target.position),
                              stuck_level=level)
        return HordeOrder(order.zombie_id, order.target_id, order.role, order.goal,
                          order.cells, order.aim, level)


__all__ = [
    "HordeCoordinator",
    "HordeMember",
    "HordeMetrics",
    "HordeOrder",
    "ROLE_CODES",
    "ROLE_NAMES",
    "SiegeInfo",
    "SurvivorTarget",
    "dig_stand_spot",
    "pile_count",
    "standable_near",
    "tunnel_cells",
]
