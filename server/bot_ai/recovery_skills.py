"""Human-style terrain locomotion skills: climb out, pillar up, fast-bridge.

A player who falls into the sea under a cliff, lands in a pit, or meets a
chasm does not wait for a walkable route to appear. They dig a staircase
into the wall, jump-and-place a pillar under themselves, or walk backwards
over the edge laying a line of blocks. This module gives bots the same
skills through the ordinary input path: tool selection, aim, held primary
(MELEE), BlockBuild/BlockLine placement and native jumping. The gameplay
server still validates every swing and block exactly as for a client.

Two layers, both pure (no gameplay objects):

* **Planning** (``plan_ascent``, ``plan_pillar_up``, ``plan_staircase_up``,
  ``plan_fast_bridge``, ``find_gap_bridge``) turns the current voxel world
  into a short list of :class:`ClimbStep`. A bounded weighted A* over standing
  nodes ``(x, y, support_z)`` costs walking, swimming, digging (recovered
  per-tool swings x cadence, footprint aware) and block placement (wallet
  aware). A bot with an instant-dig tool prefers digging; one without
  pillars. Nothing here mutates the world.
* **Execution** (:class:`LocomotionSkill`) follows a plan one step at a
  time. Every frame it re-reads the live world, so a teammate's edit, a
  rejected swing or a collapsed floor simply leads to the next correct
  input or to a clean ``failed`` status (the caller re-plans). It never
  teleports, never sets velocity and never places a block where the
  authoritative body is.

Public entry points for strategy code (zombie/VIP policies, cooperative
projects) are documented in ``docs/BOT_RECOVERY_SKILLS_2026-10-01.md``.

Body geometry used throughout (verified against native physics): standing
on support ``s`` puts the body origin at ``z = s - 2.25`` and occupies cells
``s-3 .. s-1``; a jump rises 2.24 blocks and the feet clear cell ``s-1`` for
about 0.6 s; walking climbs a one-block step without jumping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import heapq
import math
import time
from typing import Callable, Iterable, Protocol

import shared.constants as C
from server.dig_profiles import (
    BUILT_BLOCK_HEALTH,
    DIG_CUBE,
    DIG_SINGLE,
    MAP_BLOCK_HEALTH,
    DigProfile,
    best_navigation_dig_profile,
    melee_dig_positions,
)

from .messages import (
    BotAction,
    BotActionKind,
    MovementAffordance,
    PlayerSnapshot,
    Vector3,
)


Cell = tuple[int, int, int]
Node = tuple[int, int, int]

MAP_SIZE = 512
WATER_SUPPORT_Z = int(C.Z_ABOVE_WATERPLANE) + 1  # immutable waterbed (239)
MAX_EDIT_Z = int(C.Z_ABOVE_WATERPLANE)            # highest editable layer (238)
SUPPORT_OFFSET = 2.25
BODY_CELLS = 3
# Melee world reach (server MELEE_WORLD_RANGE) minus a margin for aim drift.
MELEE_REACH = 3.6
# Retail build reach is 10 from the eye; stay well inside it.
BUILD_REACH = 6.5
# Native jump rises 2.24 blocks (measured); keep a margin for a low ceiling.
JUMP_RISE = 2.24

_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))

# Planner cost model (seconds of a player's time).
_WALK_COST = 0.27
_SWIM_COST = 0.36
_STEP_UP_COST = 0.45
_STEP_DOWN_COST = 0.32
_PILLAR_COST = 0.85
_BUILD_COST = 0.25
_AIM_COST = 0.12
# A block is a resource. With an instant-dig tool a staircase keeps the
# wallet for fortifying; without one, blocks are the fast way up.
_BLOCK_PENALTY_WITH_DIGGER = 1.2
_BLOCK_PENALTY = 0.15
_HEURISTIC_WEIGHT = 2.2


class WorldReader(Protocol):
    """The subset of :class:`SimpleVoxelWorld` the skills need."""

    def solid(self, x: int, y: int, z: int) -> bool: ...


class StepKind(str, Enum):
    """One physical move of a skill plan."""

    WALK = "walk"      # same level (or one down); floor may need a block
    SWIM = "swim"      # along the water plane
    STAIR = "stair"    # one level up into an adjacent column
    PILLAR = "pillar"  # one level up in place: jump, place a block below
    BRIDGE = "bridge"  # same level over a gap: place the floor, step onto it


@dataclass(frozen=True, slots=True)
class ClimbStep:
    """Move from ``source`` to ``destination`` (both standing nodes).

    ``dig_cells`` must be air and ``build_cells`` solid before the body
    moves. Nodes are ``(x, y, support_z)`` with AoS z increasing downward.
    """

    kind: StepKind
    source: Node
    destination: Node
    dig_cells: tuple[Cell, ...] = ()
    build_cells: tuple[Cell, ...] = ()


@dataclass(frozen=True, slots=True)
class AscentPlan:
    """A bounded sequence of steps and its estimated cost."""

    steps: tuple[ClimbStep, ...]
    cost: float
    blocks: int
    swings: int
    expansions: int = 0
    # Estimated wall-clock seconds (``cost`` also prices wallet use).
    seconds: float = 0.0

    @property
    def destination(self) -> Node | None:
        return self.steps[-1].destination if self.steps else None

    @property
    def rise(self) -> int:
        if not self.steps:
            return 0
        return int(self.steps[0].source[2]) - int(self.steps[-1].destination[2])


@dataclass(frozen=True, slots=True)
class ClimbAbilities:
    """What a body can do to terrain right now."""

    dig: DigProfile | None
    blocks: int
    can_build: bool

    @property
    def instant_dig(self) -> bool:
        """One swing breaks an authored voxel (spade, pickaxe, Super Spade)."""

        return self.dig is not None and self.dig.swings_per_block <= 1

    @property
    def block_penalty(self) -> float:
        return _BLOCK_PENALTY_WITH_DIGGER if self.instant_dig else _BLOCK_PENALTY

    @classmethod
    def from_observer(cls, observer: PlayerSnapshot) -> "ClimbAbilities":
        loadout = tuple(int(tool) for tool in getattr(observer, "loadout", ()))
        dig = (best_navigation_dig_profile(loadout)
               if bool(getattr(observer, "can_shoot", True)) else None)
        if dig is not None and dig.block_damage <= 0.0:
            dig = None
        can_build = int(C.BLOCK_TOOL) in loadout
        return cls(dig, max(0, int(getattr(observer, "blocks", 0))) if can_build else 0,
                   can_build)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def node_of(position: Vector3) -> Node:
    """Standing node of a body origin (swimmers stand on the waterbed)."""

    # floor(z + 2.3): standing gives s + 0.05, crouching (+0.9) s + 0.95.
    support = int(math.floor(float(position[2]) + SUPPORT_OFFSET + 0.05))
    return (int(math.floor(position[0])), int(math.floor(position[1])),
            min(WATER_SUPPORT_Z, support))


def node_position(node: Node) -> Vector3:
    return (node[0] + 0.5, node[1] + 0.5, node[2] - SUPPORT_OFFSET)


def cell_center(cell: Cell) -> Vector3:
    return (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)


def body_cells(node: Node) -> tuple[Cell, ...]:
    x, y, s = node
    return tuple((x, y, s - offset) for offset in range(1, BODY_CELLS + 1))


def in_map(x: int, y: int) -> bool:
    return 0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE


def standable(world: WorldReader, node: Node) -> bool:
    """Floor solid and a full standing body of air above it."""

    x, y, s = node
    if not in_map(x, y) or not 4 <= s <= WATER_SUPPORT_Z:
        return False
    return world.solid(x, y, s) and not any(world.solid(*cell) for cell in body_cells(node))


def diggable(cell: Cell) -> bool:
    return in_map(cell[0], cell[1]) and 1 <= cell[2] <= MAX_EDIT_Z


def buildable(cell: Cell) -> bool:
    return in_map(cell[0], cell[1]) and 1 <= cell[2] <= MAX_EDIT_Z


def face_supported(world: WorldReader, cell: Cell, pending: Iterable[Cell] = (),
                   *, ignore: Iterable[Cell] = ()) -> bool:
    """Client-parity placement rule: the cell touches a solid face.

    ``ignore`` lists neighbours that will be air by then (cells the plan
    digs before placing), so they cannot count as support.
    """

    pending = set(pending)
    ignore = set(ignore)
    x, y, z = cell
    for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
        neighbor = (x + dx, y + dy, z + dz)
        if neighbor in ignore:
            continue
        if neighbor in pending or world.solid(*neighbor):
            return True
    return False


def _cell_health(world: WorldReader, cell: Cell) -> float:
    reader = getattr(world, "block_health", None)
    if callable(reader):
        try:
            return float(reader(*cell))
        except (TypeError, ValueError):
            return MAP_BLOCK_HEALTH
    return MAP_BLOCK_HEALTH


def dig_aims(
    world: WorldReader,
    required: Iterable[Cell],
    protected: Iterable[Cell],
    profile: DigProfile,
) -> list[Cell] | None:
    """Greedy footprint cover: aim cells whose swings clear ``required``.

    A footprint may never touch ``protected`` (floors the body stands or will
    stand on). Returns ``None`` when some required solid cell cannot be
    cleared that way, ``[]`` when everything is already air.
    """

    protected = frozenset(protected)
    remaining = {cell for cell in required if world.solid(*cell)}
    if not remaining:
        return []
    if any(not diggable(cell) for cell in remaining):
        return None
    aims: list[Cell] = []
    while remaining:
        best: tuple[int, int, Cell] | None = None
        candidates = set(remaining)
        if profile.pattern != DIG_SINGLE:
            for cell in remaining:
                for dz in (-1, 1):
                    candidates.add((cell[0], cell[1], cell[2] + dz))
        for aim in candidates:
            if not world.solid(*aim) or not diggable(aim):
                continue
            footprint = set(melee_dig_positions(aim, profile.pattern))
            if footprint & protected:
                continue
            covered = len(footprint & remaining)
            if covered <= 0:
                continue
            # Prefer covering more, then the least collateral (a wide swing
            # also eats the terrain later steps stand on), then the higher
            # cell (head room first, so an overhang never blocks a ray).
            collateral = sum(1 for cell in footprint
                             if cell not in remaining and world.solid(*cell))
            key = (covered, -collateral, -aim[2], aim)
            if best is None or key[:3] > best[:3]:
                best = key
        if best is None:
            return None
        aim = best[3]
        aims.append(aim)
        remaining -= set(melee_dig_positions(aim, profile.pattern))
        if len(aims) > 16:
            return None
    return aims


def estimate_swings(
    world: WorldReader,
    required: Iterable[Cell],
    protected: Iterable[Cell],
    profile: DigProfile | None,
) -> int | None:
    """Swings needed to clear ``required`` (``None`` = impossible)."""

    required = tuple(required)
    solid = [cell for cell in required if world.solid(*cell)]
    if not solid:
        return 0
    if profile is None or profile.block_damage <= 0.0:
        return None
    if any(not diggable(cell) for cell in solid):
        return None
    per_cell = max(profile.swings_for_health(_cell_health(world, cell)) for cell in solid)
    if len(solid) == 1 or profile.pattern != DIG_SINGLE:
        # Fast path: one swing at the middle solid cell often covers it all.
        middle = sorted(solid, key=lambda cell: cell[2])[len(solid) // 2]
        footprint = set(melee_dig_positions(middle, profile.pattern))
        if footprint.issuperset(solid) and not footprint.intersection(protected):
            return per_cell
    if profile.pattern == DIG_SINGLE:
        return len(solid) * per_cell
    aims = dig_aims(world, solid, protected, profile)
    if aims is None:
        return None
    return len(aims) * per_cell


def first_solid_on_ray(world: WorldReader, origin: Vector3, target: Vector3,
                       *, limit: float = 6.0) -> Cell | None:
    """First solid voxel a ray from ``origin`` toward ``target`` enters."""

    dx, dy, dz = (float(target[i]) - float(origin[i]) for i in range(3))
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance <= 1e-6:
        return None
    steps = max(1, int(math.ceil(min(distance + 0.6, limit) * 10.0)))
    last: Cell | None = None
    for index in range(1, steps + 1):
        t = index * 0.1 / distance
        cell = (int(math.floor(origin[0] + dx * t)), int(math.floor(origin[1] + dy * t)),
                int(math.floor(origin[2] + dz * t)))
        if cell == last:
            continue
        last = cell
        if world.solid(*cell):
            return cell
    return None


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AscentGoal:
    """Where a climb ends.

    ``is_goal(node)`` accepts a finishing node; ``estimate(node)`` returns
    ``(horizontal_cells, levels_to_climb)`` still separating it from the
    goal. The planner prices levels with the body's own abilities, so the
    search heads straight up a cliff instead of flooding the water plane.
    """

    is_goal: Callable[[Node], bool]
    estimate: Callable[[Node], tuple[float, int]]
    label: str = ""

    @classmethod
    def toward(cls, target: Vector3, *, radius: float = 1.5) -> "AscentGoal":
        """Stand within ``radius`` of ``target`` at its height or higher."""

        tx, ty = float(target[0]), float(target[1])
        target_support = int(round(float(target[2]) + SUPPORT_OFFSET))

        def is_goal(node: Node) -> bool:
            return (math.hypot(node[0] + 0.5 - tx, node[1] + 0.5 - ty) <= radius
                    and node[2] <= target_support + 1)

        def estimate(node: Node) -> tuple[float, int]:
            horizontal = max(0.0, math.hypot(node[0] + 0.5 - tx, node[1] + 0.5 - ty) - radius)
            return horizontal, max(0, node[2] - target_support - 1)

        return cls(is_goal, estimate, "toward")

    @classmethod
    def main_ground(cls, world, start: Node, *, radius: int = 18,
                    candidates: int = 12) -> "AscentGoal | None":
        """Reach the top of any main-ground column near ``start``.

        Uses the cached navigation atlas (connected primary ground). Returns
        ``None`` when the world has no atlas or no main ground nearby.
        """

        atlas = getattr(world, "_atlas", None)
        if atlas is None or not getattr(atlas, "main_region_id", 0):
            return None
        main = int(atlas.main_region_id)
        width = int(atlas.width)
        found: list[tuple[float, int, int, int]] = []
        x0, y0, s0 = start
        for y in range(max(0, y0 - radius), min(MAP_SIZE, y0 + radius + 1)):
            for x in range(max(0, x0 - radius), min(MAP_SIZE, x0 + radius + 1)):
                index = y * width + x
                if int(atlas.regions[index]) != main:
                    continue
                primary = int(atlas.primary_support[index])
                if primary >= WATER_SUPPORT_Z:
                    continue
                distance = abs(x - x0) + abs(y - y0)
                found.append((distance * _WALK_COST + max(0, s0 - primary) * _STEP_UP_COST,
                              x, y, primary))
        if not found:
            return None
        found.sort()
        nearest = tuple(found[:max(1, int(candidates))])
        goal_columns = {(x, y): primary for _cost, x, y, primary in found}

        def is_goal(node: Node) -> bool:
            primary = goal_columns.get((node[0], node[1]))
            return primary is not None and abs(node[2] - primary) <= 1

        cache: dict[Node, tuple[float, int]] = {}

        def estimate(node: Node) -> tuple[float, int]:
            value = cache.get(node)
            if value is None:
                nx, ny, ns = node
                value = min(
                    ((abs(nx - x) + abs(ny - y)), max(0, ns - primary))
                    for _cost, x, y, primary in nearest
                )
                cache[node] = value
            return value

        return cls(is_goal, estimate, "main_ground")


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class _CachedWorld:
    """Memoize solid() during one bounded search."""

    __slots__ = ("_world", "_cache", "block_health")

    def __init__(self, world: WorldReader) -> None:
        self._world = world
        self._cache: dict[Cell, bool] = {}
        self.block_health = getattr(world, "block_health", None)

    def solid(self, x: int, y: int, z: int) -> bool:
        key = (x, y, z)
        value = self._cache.get(key)
        if value is None:
            value = bool(self._world.solid(x, y, z))
            self._cache[key] = value
        return value


def _move(
    world: WorldReader,
    abilities: ClimbAbilities,
    node: Node,
    kind: StepKind,
    dx: int,
    dy: int,
    wallet: int,
    avoid: frozenset[Cell] = frozenset(),
) -> tuple[ClimbStep, float, int, int] | None:
    """Validate one move; return (step, cost, blocks_spent, cells_dug)."""

    x, y, s = node
    profile = abilities.dig
    if kind is StepKind.PILLAR:
        destination = (x, y, s - 1)
        floor = (x, y, s - 1)
        if (not abilities.can_build or wallet < 1 or not buildable(floor)
                or s - 4 < 1 or floor in avoid or destination in avoid
                or s >= WATER_SUPPORT_Z):
            # Afloat, the bob rarely lifts the feet clear of the cell above
            # the waterbed: a swimmer lays a block beside itself instead
            # (a stair onto it), which the waterbed always supports.
            return None
        # Head room for a full jump (the body vacates s-1 for ~0.6 s only
        # when nothing caps the 2.24-block rise).
        digs = ((x, y, s - 4), (x, y, s - 5)) if s - 5 >= 1 else ((x, y, s - 4),)
        protected = ((x, y, s), floor)
        swings = estimate_swings(world, digs, protected, profile)
        if swings is None:
            return None
        dug = sum(1 for cell in digs if world.solid(*cell))
        cost = (_PILLAR_COST + _BUILD_COST + abilities.block_penalty
                + swings * ((profile.fire_interval if profile else 0.0) + _AIM_COST))
        return ClimbStep(kind, node, destination, tuple(c for c in digs if world.solid(*c)),
                         (floor,)), cost, 1, dug

    nx, ny = x + dx, y + dy
    if not in_map(nx, ny) or not in_map(x, y):
        return None
    if kind is StepKind.STAIR:
        destination = (nx, ny, s - 1)
        digs = ((nx, ny, s - 4), (nx, ny, s - 3), (nx, ny, s - 2), (x, y, s - 4))
        floor = (nx, ny, s - 1)
        step_cost = _STEP_UP_COST
    else:
        destination = (nx, ny, s)
        digs = ((nx, ny, s - 3), (nx, ny, s - 2), (nx, ny, s - 1))
        floor = (nx, ny, s)
        swimming = s >= WATER_SUPPORT_Z
        step_cost = _SWIM_COST if swimming else _WALK_COST
        if swimming:
            kind = StepKind.SWIM
    if destination[2] - BODY_CELLS < 1 or destination in avoid:
        return None
    builds: tuple[Cell, ...] = ()
    spent = 0
    if not world.solid(*floor):
        # One level down is an ordinary walk off a short ledge: cheaper than
        # a block whenever the lower floor is there.
        if kind is StepKind.WALK and s + 1 < WATER_SUPPORT_Z:
            lower = (nx, ny, s + 1)
            if standable(world, lower):
                return (ClimbStep(StepKind.WALK, node, lower), _STEP_DOWN_COST, 0, 0)
        if (not abilities.can_build or wallet < 1 or not buildable(floor) or floor in avoid
                or not face_supported(world, floor, ignore=(*body_cells(node), *digs))):
            return None
        if kind is StepKind.WALK and floor[2] >= MAX_EDIT_Z:
            # No causeways along the sea surface: swimming is quicker.
            return None
        builds = (floor,)
        spent = 1
        if kind is StepKind.WALK:
            kind = StepKind.BRIDGE
    protected = ((x, y, s), floor)
    swings = estimate_swings(world, digs, protected, profile)
    if swings is None:
        return None
    solid_digs = tuple(cell for cell in digs if world.solid(*cell))
    cost = step_cost + spent * (_BUILD_COST + abilities.block_penalty)
    if swings:
        cost += swings * ((profile.fire_interval if profile else 0.0) + _AIM_COST)
        if profile is not None and profile.pattern == DIG_CUBE and kind is StepKind.STAIR:
            # A 3x3x3 swing also removes the next step's floor; budget the
            # replacement block the executor will place.
            cost += _BUILD_COST + abilities.block_penalty * 0.5
    if kind is StepKind.SWIM and solid_digs:
        return None
    return (ClimbStep(kind, node, destination, solid_digs, builds), cost, spent,
            len(solid_digs))


def typical_level_cost(abilities: ClimbAbilities) -> float:
    """Seconds one level of climbing usually costs this body.

    Used as the search heuristic (deliberately not admissible: natural steps
    are cheaper, which keeps the bounded search focused on going up).
    """

    options = []
    if abilities.can_build and abilities.blocks > 0:
        options.append(_STEP_UP_COST + _BUILD_COST + abilities.block_penalty)
    profile = abilities.dig
    if profile is not None and profile.block_damage > 0.0:
        cells = {DIG_SINGLE: 4, DIG_CUBE: 1}.get(profile.pattern, 2)
        swing = profile.fire_interval + _AIM_COST
        options.append(_STEP_UP_COST + cells * swing * max(1, profile.swings_per_block)
                       + (0.5 if profile.pattern == DIG_CUBE else 0.0))
    return min(options) if options else 4.0


class AscentSearch:
    """Resumable bounded weighted A* over standing nodes.

    Moves: walk/swim (same level, digging the body corridor or bridging a
    missing floor), one-level-down walk, stair (one up into a neighbor,
    digging body and head room, building a missing floor) and pillar (jump
    and place a block below). ``advance`` spends at most ``time_budget``
    seconds and returns ``"found"``, ``"failed"`` or ``"pending"``, so a
    worker serving a whole roster can spread one hard search over several
    decisions. The world is read lazily; the executor re-validates every
    step against the live world anyway.
    """

    __slots__ = ("world", "start", "goal", "abilities", "radius", "max_expansions",
                 "allow_swim", "kinds", "avoid", "level_cost", "open_heap", "best",
                 "parents", "wallets", "counter", "expansions", "plan", "status", "cube")

    def __init__(
        self,
        world: WorldReader,
        start: Node,
        goal: AscentGoal,
        abilities: ClimbAbilities,
        *,
        radius: int = 18,
        max_expansions: int = 2500,
        allow_swim: bool = True,
        allowed_kinds: frozenset[StepKind] | None = None,
        avoid: Iterable[Cell] = (),
    ) -> None:
        self.world = _CachedWorld(world)
        self.start = start
        self.goal = goal
        self.abilities = abilities
        self.radius = int(radius)
        self.max_expansions = int(max_expansions)
        self.allow_swim = bool(allow_swim)
        self.kinds = allowed_kinds or frozenset(StepKind)
        self.avoid = frozenset(avoid)
        self.level_cost = typical_level_cost(abilities)
        self.cube = abilities.dig is not None and abilities.dig.pattern == DIG_CUBE
        self.open_heap: list[tuple[float, float, int, Node]] = []
        self.best: dict[Node, float] = {start: 0.0}
        self.parents: dict[Node, tuple[Node, ClimbStep, int, int]] = {}
        self.wallets: dict[Node, int] = {start: int(abilities.blocks)}
        self.counter = 0
        self.expansions = 0
        self.plan: AscentPlan | None = None
        self.status = "pending"
        heapq.heappush(self.open_heap, (self._heuristic(start), 0.0, 0, start))

    def _heuristic(self, node: Node) -> float:
        horizontal, levels = self.goal.estimate(node)
        return (horizontal * _WALK_COST + levels * self.level_cost) * _HEURISTIC_WEIGHT

    def advance(self, time_budget: float = 0.015) -> str:
        if self.status != "pending":
            return self.status
        deadline = time.perf_counter() + max(0.0005, float(time_budget))
        x0, y0, _s0 = self.start
        abilities = self.abilities
        open_heap, best, wallets, parents = self.open_heap, self.best, self.wallets, self.parents
        steps_this_call = 0
        while open_heap and self.expansions < self.max_expansions:
            steps_this_call += 1
            if steps_this_call & 31 == 0 and time.perf_counter() > deadline:
                return "pending"
            _priority, cost, _tie, node = heapq.heappop(open_heap)
            if cost > best.get(node, math.inf) + 1e-9:
                continue
            self.expansions += 1
            if self.goal.is_goal(node) and node != self.start:
                self.plan = _reconstruct(self.start, node, parents, cost, self.expansions,
                                         abilities)
                self.status = "found"
                return self.status
            wallet = wallets.get(node, int(abilities.blocks))
            parent = parents.get(node)
            for kind, dx, dy in _MOVES:
                if kind in (StepKind.PILLAR, StepKind.STAIR) and kind not in self.kinds:
                    continue
                nx, ny = node[0] + dx, node[1] + dy
                if (kind is StepKind.STAIR and parent is not None
                        and parent[1].kind is StepKind.STAIR and parent[1].build_cells
                        and (nx, ny) == parent[0][:2]):
                    # No back-and-forth towers of built steps: with open air
                    # on every side, the momentum of each hop throws the body
                    # off. Pillar straight up instead.
                    continue
                if (kind is StepKind.STAIR and self.cube and parent is not None
                        and parent[1].kind is StepKind.STAIR
                        and max(abs(nx - parent[0][0]), abs(ny - parent[0][1])) < 2):
                    # The previous 3x3x3 swing removed everything that could
                    # hold this step's floor: a cube staircase runs straight.
                    continue
                if abs(nx - x0) > self.radius or abs(ny - y0) > self.radius:
                    continue
                result = _move(self.world, abilities, node, kind, dx, dy, wallet, self.avoid)
                if result is None:
                    continue
                step, move_cost, spent, dug = result
                if step.kind not in self.kinds:
                    continue
                if step.kind is StepKind.SWIM and not self.allow_swim:
                    continue
                destination = step.destination
                total = cost + move_cost
                if total + 1e-9 >= best.get(destination, math.inf):
                    continue
                best[destination] = total
                wallets[destination] = wallet - spent + dug
                parents[destination] = (node, step, spent, dug)
                self.counter += 1
                heapq.heappush(open_heap, (total + self._heuristic(destination), total,
                                           self.counter, destination))
        self.status = "failed"
        return self.status


_MOVES: tuple[tuple[StepKind, int, int], ...] = (
    (StepKind.PILLAR, 0, 0),
    *((kind, dx, dy) for dx, dy in _CARDINALS for kind in (StepKind.STAIR, StepKind.WALK)),
)


def plan_ascent(
    world: WorldReader,
    start: Node,
    goal: AscentGoal,
    abilities: ClimbAbilities,
    *,
    radius: int = 18,
    max_expansions: int = 2500,
    allow_swim: bool = True,
    allowed_kinds: frozenset[StepKind] | None = None,
    avoid: Iterable[Cell] = (),
    time_budget: float = 0.06,
) -> AscentPlan | None:
    """One-shot :class:`AscentSearch`: the plan, or ``None`` when nothing is
    found within ``max_expansions``, ``radius`` columns or ``time_budget``."""

    search = AscentSearch(world, start, goal, abilities, radius=radius,
                          max_expansions=max_expansions, allow_swim=allow_swim,
                          allowed_kinds=allowed_kinds, avoid=avoid)
    return search.plan if search.advance(time_budget) == "found" else None


def _reconstruct(start: Node, node: Node, parents, cost: float, expansions: int,
                 abilities: ClimbAbilities) -> AscentPlan:
    steps: list[ClimbStep] = []
    blocks = swings = 0
    cursor = node
    while cursor != start:
        parent, step, spent, _dug = parents[cursor]
        steps.append(step)
        blocks += spent
        cursor = parent
    steps.reverse()
    profile = abilities.dig
    for step in steps:
        if step.dig_cells and profile is not None:
            swings += len(step.dig_cells) if profile.pattern == DIG_SINGLE else 1
    seconds = max(0.0, cost - blocks * abilities.block_penalty)
    return AscentPlan(tuple(steps), round(cost, 3), blocks, swings, expansions,
                      round(seconds, 3))


# ---------------------------------------------------------------------------
# Scripted primitives (no search): the zombie/VIP strategy layer calls these
# ---------------------------------------------------------------------------


def plan_pillar_up(world: WorldReader, start: Node, levels: int,
                   abilities: ClimbAbilities) -> AscentPlan | None:
    """Jump-and-place ``levels`` blocks under the body, digging head room."""

    steps: list[ClimbStep] = []
    cost = 0.0
    wallet = int(abilities.blocks)
    node = start
    for _ in range(max(0, int(levels))):
        result = _move(world, abilities, node, StepKind.PILLAR, 0, 0, wallet)
        if result is None:
            break
        step, move_cost, spent, dug = result
        steps.append(step)
        cost += move_cost
        wallet += dug - spent
        node = step.destination
    if not steps:
        return None
    return AscentPlan(tuple(steps), round(cost, 3), len(steps), 0)


def _axis(direction: Vector3) -> tuple[int, int]:
    dx, dy = float(direction[0]), float(direction[1])
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return 1, 0
    if abs(dx) >= abs(dy):
        return (1 if dx > 0 else -1), 0
    return 0, (1 if dy > 0 else -1)


def plan_staircase_up(world: WorldReader, start: Node, direction: Vector3, levels: int,
                      abilities: ClimbAbilities) -> AscentPlan | None:
    """Dig (and where needed build) a straight staircase ``levels`` high.

    Each step clears the next column's body cells and the head room above
    the current one, then walks up one block (native step-up).
    """

    dx, dy = _axis(direction)
    steps: list[ClimbStep] = []
    cost = 0.0
    wallet = int(abilities.blocks)
    node = start
    for _ in range(max(0, int(levels))):
        result = _move(world, abilities, node, StepKind.STAIR, dx, dy, wallet)
        if result is None:
            break
        step, move_cost, spent, dug = result
        steps.append(step)
        cost += move_cost
        wallet += dug - spent
        node = step.destination
    if not steps:
        return None
    return AscentPlan(tuple(steps), round(cost, 3),
                      sum(len(step.build_cells) for step in steps), 0)


def plan_fast_bridge(world: WorldReader, start: Node, direction: Vector3, length: int,
                     abilities: ClimbAbilities) -> AscentPlan | None:
    """Lay a level floor ``length`` cells along ``direction`` and walk it.

    Existing floor is simply walked. Stops early at a wall or when the
    wallet runs out; the executor lays the floor in short BlockLines while
    the body backs over the edge.
    """

    dx, dy = _axis(direction)
    steps: list[ClimbStep] = []
    wallet = int(abilities.blocks)
    node = start
    cost = 0.0
    for _ in range(max(0, int(length))):
        x, y, s = node
        nx, ny = x + dx, y + dy
        destination = (nx, ny, s)
        if not in_map(nx, ny) or any(world.solid(*cell) for cell in body_cells(destination)):
            break
        if world.solid(nx, ny, s):
            steps.append(ClimbStep(StepKind.WALK, node, destination))
            cost += _WALK_COST
        else:
            if not abilities.can_build or wallet < 1 or not buildable((nx, ny, s)):
                break
            steps.append(ClimbStep(StepKind.BRIDGE, node, destination, (), ((nx, ny, s),)))
            wallet -= 1
            cost += _WALK_COST + _BUILD_COST
        node = destination
    if not steps:
        return None
    return AscentPlan(tuple(steps), round(cost, 3),
                      sum(len(step.build_cells) for step in steps), 0)


def find_gap_bridge(world: WorldReader, position: Vector3, goal: Vector3,
                    abilities: ClimbAbilities, *, max_cells: int = 16,
                    min_gap: int = 2) -> AscentPlan | None:
    """A crossable gap straight toward ``goal``: floor line plus landing.

    Looks along the dominant axis toward the goal from the body's column.
    The gap must start within two cells, be at least ``min_gap`` and at most
    ``max_cells`` columns wide (and affordable), keep a standing body clear
    over every new cell, and end on a standable landing at the same level
    (or one step up/down). Returns ``None`` otherwise.
    """

    if not abilities.can_build or abilities.blocks < min_gap:
        return None
    start = node_of(position)
    if start[2] >= WATER_SUPPORT_Z or not world.solid(start[0], start[1], start[2]):
        return None
    dx, dy = _axis((goal[0] - position[0], goal[1] - position[1], 0.0))
    x, y, s = start
    # Walk up to two cells of existing floor toward the edge.
    lead = 0
    while lead < 2 and standable(world, (x + dx, y + dy, s)):
        x, y = x + dx, y + dy
        lead += 1
    gap: list[Cell] = []
    limit = min(int(max_cells), int(abilities.blocks))
    cx, cy = x, y
    for _ in range(limit + 1):
        cx, cy = cx + dx, cy + dy
        if not in_map(cx, cy):
            return None
        for landing_s in (s, s - 1, s + 1):
            if standable(world, (cx, cy, landing_s)):
                if len(gap) < min_gap:
                    return None
                steps = []
                node = start
                for _lead in range(lead):
                    destination = (node[0] + dx, node[1] + dy, s)
                    steps.append(ClimbStep(StepKind.WALK, node, destination))
                    node = destination
                for cell in gap:
                    destination = cell
                    steps.append(ClimbStep(StepKind.BRIDGE, node, destination, (), (cell,)))
                    node = destination
                landing = (cx, cy, landing_s)
                kind = StepKind.STAIR if landing_s < s else StepKind.WALK
                steps.append(ClimbStep(kind, node, landing))
                return AscentPlan(tuple(steps), round(len(steps) * (_WALK_COST + _BUILD_COST), 3),
                                  len(gap), 0)
        # Not a landing: must be a real gap (no floor within a short drop)
        # with a clear body corridor above the bridge cell.
        if any(world.solid(cx, cy, s - offset) for offset in range(0, BODY_CELLS + 1)):
            return None
        if any(world.solid(cx, cy, s + depth) for depth in (1, 2)):
            # A shallow dip is walkable terrain, not a gap.
            return None
        gap.append((cx, cy, s))
    return None


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkillCommand:
    """One frame of input for the worker to wrap into a ``BotIntent``."""

    status: str  # "running", "done" or "failed"
    role: str = ""
    direction: Vector3 = (0.0, 0.0, 0.0)
    jump: bool = False
    crouch: bool = False
    sneak: bool = False
    sprint: bool = False
    affordance: MovementAffordance = MovementAffordance.WALK
    look: Vector3 | None = None
    tool_id: int = -1  # -1: the caller keeps the held weapon
    action: BotAction = BotAction()
    secondary: bool = False
    reason: str = ""


def _mix(*values: int) -> float:
    """Deterministic 0..1 jitter (no global RNG; identical across processes)."""

    value = 0x9E3779B9
    for item in values:
        value = ((value ^ (int(item) & 0xFFFFFFFF)) * 0x01000193) & 0xFFFFFFFF
        value ^= value >> 15
    return (value & 0xFFFF) / 65535.0


@dataclass(slots=True)
class LocomotionSkill:
    """Execute an :class:`AscentPlan` through ordinary player inputs.

    ``purpose`` labels diagnostics (``water_climb``, ``pit_climb``,
    ``climb``, ``pillar_up``, ``staircase``, ``bridge``). ``update`` is
    called once per worker decision and is side-effect free apart from this
    object's own timers.
    """

    plan: AscentPlan
    purpose: str
    started_at: float
    identity: int = 0
    skill: float = 0.5
    reaction: float = 0.2
    index: int = 0
    step_started_at: float = 0.0
    phase: str = ""
    next_swing_at: float = 0.0
    next_build_at: float = 0.0
    pause_until: float = 0.0
    attempts: dict[Cell, int] = field(default_factory=dict)
    build_attempts: int = 0
    jumped_at: float = 0.0
    swings: int = 0
    builds: int = 0
    reason: str = ""
    backwards: bool = True
    feedback_at: float = 0.0
    align_since: float = 0.0
    # A cell this skill found it cannot use (for the re-plan to avoid).
    blocked_cell: Cell | None = None

    @property
    def done(self) -> bool:
        return self.index >= len(self.plan.steps)

    @property
    def current(self) -> ClimbStep | None:
        return self.plan.steps[self.index] if self.index < len(self.plan.steps) else None

    def _jitter(self, salt: int) -> float:
        return _mix(self.identity, self.index, salt, self.swings, self.builds)

    def _fail(self, reason: str) -> SkillCommand:
        self.reason = reason
        return SkillCommand("failed", role=f"{self.purpose}:failed", reason=reason)

    def update(self, observer: PlayerSnapshot, world: WorldReader, now: float,
               abilities: ClimbAbilities) -> SkillCommand:
        """Return the next input. ``failed`` means re-plan from here."""

        position = observer.position
        body = node_of(position)
        wading = bool(observer.wade)
        settled = bool(observer.grounded) or wading
        self._catch_up(observer, world)
        for _ in range(3):
            step = self.current
            if step is None:
                return SkillCommand("done", role=f"{self.purpose}:done")
            if self._arrived(step, observer, world):
                self.index += 1
                self.step_started_at = now
                self.phase = ""
                self.attempts.clear()
                self.build_attempts = 0
                self.align_since = 0.0
                # A short human beat between distinct actions (not inside a
                # run of plain walking/swimming), longer for careful bots.
                following = self.current
                if following is not None and (following.dig_cells or following.build_cells
                                              or following.kind is not step.kind):
                    self.pause_until = now + self.reaction * (0.15 + 0.35 * self._jitter(1))
                continue
            break
        step = self.current
        if step is None:
            return SkillCommand("done", role=f"{self.purpose}:done")
        if self.step_started_at <= 0.0:
            self.step_started_at = now
        source, destination = step.source, step.destination
        # Distance from the planned line: the nearest of this step's source
        # and the next few nodes (a smooth swim cuts corners between cells).
        horizontal_from_source = min(
            math.hypot(position[0] - (node[0] + 0.5), position[1] - (node[1] + 0.5))
            for node in (source, *(later.destination for later in
                                   self.plan.steps[self.index:self.index + 6]))
        )
        at_destination_column = (body[0], body[1]) == destination[:2]
        # Only a body standing on dry ground has a meaningful support level;
        # a swimmer jumping onto a ledge reports wade while airborne.
        standing = bool(observer.grounded) and not wading
        if (horizontal_from_source > 1.9 and not at_destination_column) or (
                standing and abs(body[2] - source[2]) > 1 and abs(body[2] - destination[2]) > 1):
            return self._fail("off_plan")
        if (wading and source[2] < MAX_EDIT_Z - 1 and destination[2] < MAX_EDIT_Z - 1
                and float(position[2]) > source[2] - SUPPORT_OFFSET + 1.5):
            # Fell off the climb back into the sea: start over from here.
            return self._fail("fell")
        if now - self.step_started_at > self._step_timeout(step, abilities):
            # Remember the spot that would not work so the re-plan differs.
            self.blocked_cell = step.destination
            return self._fail("step_timeout")
        # Near the water plane the director steers any idle-looking body
        # as a swimmer unless the intent is a declared water action.
        near_water = float(position[2]) >= MAX_EDIT_Z - 4.25 - 0.05
        prefix = "water_" if wading or near_water else ""
        role = f"{prefix}{self.purpose}:{step.kind.value}"

        # 1. Clear every cell the move needs (head room first).
        remaining = [cell for cell in step.dig_cells if world.solid(*cell)]
        if step.kind is not StepKind.PILLAR:
            # Collateral from a previous swing never blocks; a missing floor
            # is handled below. Also re-check the body corridor live.
            remaining += [cell for cell in self._corridor(step)
                          if world.solid(*cell) and cell not in remaining]
        if remaining:
            return self._dig(observer, world, now, abilities, step, remaining, role)

        # 2. Lay the floor the move needs (stair/walk/bridge; pillars below).
        if step.kind is not StepKind.PILLAR:
            floor = (destination[0], destination[1], destination[2])
            if not world.solid(*floor):
                return self._lay_floor(observer, world, now, abilities, step, floor, role)

        if now < self.pause_until:
            return SkillCommand("running", role=role + "_beat",
                                look=self._gaze(step, observer), affordance=self._affordance(step, wading))

        # 3. Move.
        if step.kind is StepKind.PILLAR:
            return self._pillar(observer, world, now, abilities, step, role, settled)
        return self._walk(observer, now, step, role, wading)

    # -- phases ------------------------------------------------------------

    def _catch_up(self, observer: PlayerSnapshot, world: WorldReader) -> None:
        """Skip plain moves the body has already overtaken (cut corners)."""

        body = node_of(observer.position)
        settled = bool(observer.grounded) or bool(observer.wade)
        if not settled:
            return
        last = min(len(self.plan.steps), self.index + 8)
        for index in range(self.index, last):
            step = self.plan.steps[index]
            if step.kind not in (StepKind.SWIM, StepKind.WALK) or step.dig_cells or step.build_cells:
                return
            destination = step.destination
            if (body[0], body[1]) == destination[:2] and abs(body[2] - destination[2]) <= 1                     and world.solid(*destination) and index > self.index:
                self.index = index
                self.step_started_at = 0.0
                return

    def _note_build_feedback(self, observer: PlayerSnapshot) -> None:
        """Count each authoritative build rejection once."""

        if (observer.last_action_kind in (BotActionKind.BUILD.value, BotActionKind.BUILD_LINE.value)
                and float(observer.last_action_at) > self.feedback_at):
            self.feedback_at = float(observer.last_action_at)
            if not observer.last_action_accepted:
                self.build_attempts += 1

    def _arrived(self, step: ClimbStep, observer: PlayerSnapshot, world: WorldReader) -> bool:
        destination = step.destination
        body = node_of(observer.position)
        if (body[0], body[1]) != destination[:2]:
            return False
        if not world.solid(*destination):
            return False
        support_error = float(observer.position[2]) + SUPPORT_OFFSET - destination[2]
        settled = bool(observer.grounded) or bool(observer.wade)
        if destination[2] >= WATER_SUPPORT_Z:
            return bool(observer.wade) or abs(support_error) <= 0.8
        # Crouching lowers the body origin by 0.9.
        return settled and -0.45 <= support_error <= 1.0

    @staticmethod
    def _corridor(step: ClimbStep) -> tuple[Cell, ...]:
        source, destination = step.source, step.destination
        cells = list(body_cells(destination))
        if step.kind is StepKind.STAIR:
            cells.append((source[0], source[1], source[2] - 4))
        return tuple(cell for cell in cells if diggable(cell))

    def _step_timeout(self, step: ClimbStep, abilities: ClimbAbilities) -> float:
        profile = abilities.dig
        swing = (profile.fire_interval * max(1, profile.swings_per_block)) if profile else 0.0
        digs = len(step.dig_cells) + (1 if step.kind is StepKind.STAIR else 0)
        return 3.5 + digs * swing * 2.5 + len(step.build_cells) * 1.5

    def _protected(self, step: ClimbStep, profile: DigProfile | None = None) -> set[Cell]:
        protected = {step.source, step.destination}
        if profile is not None and profile.pattern == DIG_CUBE:
            # A 3x3x3 swing cannot spare the next floor; it is re-laid from
            # the blocks the swing refunds.
            return protected
        following = self.plan.steps[self.index + 1:self.index + 2]
        for later in following:
            if later.kind is not StepKind.PILLAR:
                protected.add(later.destination)
        return protected

    def _dig(self, observer: PlayerSnapshot, world: WorldReader, now: float,
             abilities: ClimbAbilities, step: ClimbStep, remaining: list[Cell],
             role: str) -> SkillCommand:
        profile = abilities.dig
        if profile is None:
            return self._fail("no_dig_tool")
        source = step.source
        offset = math.hypot(observer.position[0] - (source[0] + 0.5),
                            observer.position[1] - (source[1] + 0.5))
        if offset > 1.25:
            # Step back to where the planned swings are in reach and safe.
            return self._steer(observer, node_position(source), role + "_align",
                               bool(observer.wade), sneak=True)
        # Swing from where the body stands (a player does not re-centre to
        # dig); never through the floor it is standing on.
        protected = self._protected(step, profile) | {node_of(observer.position)}
        eye = tuple(float(value) for value in observer.eye)
        aim = self._visible_aim(world, eye, remaining, protected, profile)
        if aim is None:
            # Remaining cells are hidden behind something we may not dig
            # (or out of reach): let the caller re-plan.
            return self._fail("dig_unreachable")
        count = self.attempts.get(aim, 0)
        limit = profile.swings_for_health(max(BUILT_BLOCK_HEALTH, _cell_health(world, aim))) + 3
        if count > limit:
            return self._fail("dig_rejected")
        action = BotAction()
        if now >= self.next_swing_at:
            action = BotAction(BotActionKind.MELEE, tool_id=int(profile.tool_id),
                               position=cell_center(aim))
            self.attempts[aim] = count + 1
            self.swings += 1
            # Stock cadence plus a human hand: a skilled digger is steadier.
            jitter = (0.03 + 0.12 * (1.2 - self.skill)) * self._jitter(7)
            self.next_swing_at = now + max(0.05, profile.fire_interval) + jitter
        return SkillCommand(
            "running", role=role + "_dig",
            affordance=MovementAffordance.BREACH,
            look=cell_center(aim), tool_id=int(profile.tool_id), action=action,
            secondary=bool(profile.secondary),
        )

    @staticmethod
    def _visible_aim(world: WorldReader, eye: Vector3, remaining: list[Cell],
                     protected: set[Cell], profile: DigProfile) -> Cell | None:
        candidates: list[tuple[int, float, Cell]] = []
        required = set(remaining)
        pool = set(remaining)
        if profile.pattern != DIG_SINGLE:
            for cell in remaining:
                pool.add((cell[0], cell[1], cell[2] - 1))
                pool.add((cell[0], cell[1], cell[2] + 1))
        for aim in pool:
            if not world.solid(*aim) or not diggable(aim):
                continue
            footprint = set(melee_dig_positions(aim, profile.pattern))
            if footprint & protected:
                continue
            covered = len(footprint & required)
            if covered <= 0:
                continue
            center = cell_center(aim)
            distance = math.dist(eye, center)
            if distance > MELEE_REACH:
                continue
            if first_solid_on_ray(world, eye, center) != aim:
                continue
            collateral = sum(1 for cell in footprint
                             if cell not in required and world.solid(*cell))
            candidates.append((covered, -collateral, -distance, aim))
        if candidates:
            return max(candidates)[3]
        # Something not in the plan is in the way (an overhang lip, a
        # neighbour's block): clear that first if it is safe to.
        for cell in sorted(remaining, key=lambda c: math.dist(eye, cell_center(c))):
            blocker = first_solid_on_ray(world, eye, cell_center(cell))
            if blocker is None or blocker in protected or not diggable(blocker):
                continue
            if math.dist(eye, cell_center(blocker)) > MELEE_REACH:
                continue
            if set(melee_dig_positions(blocker, profile.pattern)) & protected:
                continue
            return blocker
        return None

    def _lay_floor(self, observer: PlayerSnapshot, world: WorldReader, now: float,
                   abilities: ClimbAbilities, step: ClimbStep, floor: Cell,
                   role: str) -> SkillCommand:
        if not abilities.can_build or abilities.blocks < 1:
            return self._fail("no_blocks")
        if not face_supported(world, floor):
            # Collateral (a 3x3x3 swing, a teammate) took the support away:
            # lay a block under it first, as a player would.
            support = self._support_for(world, step, floor)
            if support is None or abilities.blocks < 2:
                return self._fail("floor_unsupported")
            floor = support
        self._note_build_feedback(observer)
        if self.build_attempts > 4:
            return self._fail("build_rejected")
        if step.kind is StepKind.BRIDGE:
            return self._bridge(observer, world, now, abilities, step, floor, role)
        if _body_overlaps_column(observer.position, floor) and observer.wade:
            # Afloat, just paddle back a little from the new block's column.
            source = node_position(step.source)
            away = (step.source[0] - floor[0], step.source[1] - floor[1])
            target = (source[0] + away[0] * 0.25, source[1] + away[1] * 0.25, source[2])
            command = self._steer(observer, target, role + "_align", True, tolerance=0.1)
            return SkillCommand("running", role=role + "_align", direction=command.direction,
                                affordance=MovementAffordance.SWIM, look=cell_center(floor),
                                tool_id=int(C.BLOCK_TOOL))
        if _body_overlaps_column(observer.position, floor):
            # The next step's floor is at foot height beside the body and the
            # server refuses a block that intersects it. Hop and place it at
            # the top of the jump, the way a player lays a stair step.
            z = float(observer.position[2])
            margin = 0.02 if observer.wade else 0.1
            feet_clear = z < floor[2] - 2.0 - margin
            settled = bool(observer.grounded) or bool(observer.wade)
            action = BotAction()
            jump = False
            if feet_clear and now >= self.next_build_at:
                action = BotAction(BotActionKind.BUILD, tool_id=int(C.BLOCK_TOOL),
                                   position=tuple(float(value) for value in floor))
                self.next_build_at = now + 0.18
                self.builds += 1
            elif settled and not feet_clear and now - self.jumped_at > 0.32:
                jump = True
                self.jumped_at = now
            return SkillCommand("running", role=role + "_hop_floor",
                                jump=jump or bool(observer.wade),
                                affordance=MovementAffordance.BUILD_STEP,
                                look=cell_center(floor), tool_id=int(C.BLOCK_TOOL), action=action)
        action = BotAction()
        if now >= self.next_build_at:
            action = BotAction(BotActionKind.BUILD, tool_id=int(C.BLOCK_TOOL),
                               position=tuple(float(value) for value in floor))
            self.next_build_at = now + 0.3 + 0.2 * self._jitter(11)
            self.builds += 1
        return SkillCommand("running", role=role + "_floor",
                            affordance=MovementAffordance.BUILD_STEP,
                            look=cell_center(floor), tool_id=int(C.BLOCK_TOOL), action=action)

    @staticmethod
    def _support_for(world: WorldReader, step: ClimbStep, floor: Cell) -> Cell | None:
        """A supported air cell touching ``floor`` outside both bodies."""

        keep_clear = set(body_cells(step.source)) | set(body_cells(step.destination))
        x, y, z = floor
        for neighbor in ((x, y, z + 1), (x + 1, y, z), (x - 1, y, z), (x, y + 1, z),
                         (x, y - 1, z)):
            if neighbor in keep_clear or not buildable(neighbor) or world.solid(*neighbor):
                continue
            if face_supported(world, neighbor):
                return neighbor
        return None

    def _bridge(self, observer: PlayerSnapshot, world: WorldReader, now: float,
                abilities: ClimbAbilities, step: ClimbStep, floor: Cell,
                role: str) -> SkillCommand:
        """Back over the edge laying the next few floor cells in one line."""

        line = [floor]
        for later in self.plan.steps[self.index + 1:]:
            if later.kind is not StepKind.BRIDGE or len(line) >= self._line_length(abilities):
                break
            cell = later.build_cells[0]
            if world.solid(*cell):
                break
            line.append(cell)
        travel = (step.destination[0] - step.source[0], step.destination[1] - step.source[1])
        source = step.source
        look = self._bridge_look(observer, line[0], travel)
        body = node_of(observer.position)
        vx, vy = float(observer.velocity[0]), float(observer.velocity[1])
        speed = math.hypot(vx, vy)
        if (body[0], body[1]) != source[:2]:
            # Carried past the lip (or sideways off a one-wide bridge):
            # straight back to the middle of the last solid cell, carefully.
            command = self._steer(observer, node_position(source), role + "_recover", False,
                                  tolerance=0.1, sneak=True)
            return SkillCommand("running", role=role + "_recover", direction=command.direction,
                                sneak=True, affordance=MovementAffordance.WALK, look=look,
                                tool_id=int(C.BLOCK_TOOL))
        if speed > 0.06:
            # Arrived at a run: counter the momentum before working the edge.
            return SkillCommand("running", role=role + "_brake", direction=(-vx / speed,
                                                                            -vy / speed, 0.0),
                                affordance=MovementAffordance.WALK, look=look,
                                tool_id=int(C.BLOCK_TOOL))
        action = BotAction()
        if now >= self.next_build_at and settled_on(observer, source, world):
            action = BotAction(BotActionKind.BUILD_LINE, tool_id=int(C.BLOCK_TOOL),
                               position=tuple(float(v) for v in line[0]),
                               end_position=tuple(float(v) for v in line[-1]))
            self.next_build_at = now + 0.25 + 0.15 * self._jitter(13)
            self.builds += 1
        # Ease toward the edge of the current floor, never past it (the
        # motor's live ledge gate also refuses to walk off a drop).
        edge = (source[0] + 0.5 + travel[0] * 0.25, source[1] + 0.5 + travel[1] * 0.25,
                position_z(observer))
        command = self._steer(observer, edge, role + "_lay", False, tolerance=0.12, sneak=True)
        # The server's body check counts a crouched body one cell lower, down
        # to the floor layer. Crouching while the body reaches into the line's
        # first column cancels the accepted line at its commit, unreported,
        # and the bot re-lays it for as long as it stands there.
        crouch = (action.kind is not BotActionKind.NONE
                  and not _body_overlaps_column(observer.position, line[0], margin=0.15))
        return SkillCommand("running", role=role + "_lay", direction=command.direction,
                            sneak=True, crouch=crouch,
                            affordance=MovementAffordance.WALK,
                            look=look, tool_id=int(C.BLOCK_TOOL), action=action)

    def _line_length(self, abilities: ClimbAbilities) -> int:
        # Skilled builders drag longer lines; never more than the wallet.
        length = 2 + int(round(2.0 * self.skill))
        return max(1, min(length, int(abilities.blocks)))

    def _bridge_look(self, observer: PlayerSnapshot, cell: Cell,
                     travel: tuple[int, int]) -> Vector3:
        if self.backwards:
            # Face back the way we came, eyes steeply down at the edge face:
            # the backwards-bridging posture a player uses at a drop.
            eye = observer.eye
            return (eye[0] - travel[0] * 0.55, eye[1] - travel[1] * 0.55, eye[2] + 2.4)
        return cell_center(cell)

    def _pillar(self, observer: PlayerSnapshot, world: WorldReader, now: float,
                abilities: ClimbAbilities, step: ClimbStep, role: str,
                settled: bool) -> SkillCommand:
        x, y, s = step.source
        floor = (x, y, s - 1)
        look = (x + 0.5 + 0.08 * (self._jitter(17) - 0.5), y + 0.5 + 0.08 * (self._jitter(19) - 0.5),
                float(s) - 0.6)
        center = node_position(step.source)
        centering = _toward(observer.position, center, deadband=0.18)
        if world.solid(*floor):
            # Placed: drift onto it and land.
            return SkillCommand("running", role=role + "_land", direction=centering,
                                affordance=MovementAffordance.BUILD_STEP, look=look,
                                tool_id=int(C.BLOCK_TOOL))
        if not abilities.can_build or abilities.blocks < 1:
            return self._fail("no_blocks")
        self._note_build_feedback(observer)
        if self.build_attempts > 6:
            return self._fail("build_rejected")
        z = float(observer.position[2])
        rising = float(observer.velocity[2]) < 0.02
        # Server rule: the cell may not intersect the body (feet cell is
        # floor(z + 2)). A swimmer bobs slowly, so it needs less margin.
        margin = 0.02 if observer.wade else 0.1
        feet_clear = z < (s - 1) - 2.0 - margin
        action = BotAction()
        # Place while still rising (the commit lands a motor tick later), or
        # when comfortably clear on the way down.
        if feet_clear and now >= self.next_build_at and (rising or z < s - 3.6):
            # Two levels in one jump when the body cleared both cells, the
            # plan continues upward and the hand is quick enough.
            following = self.plan.steps[self.index + 1] if self.index + 1 < len(self.plan.steps) else None
            second = (x, y, s - 2)
            if (following is not None and following.kind is StepKind.PILLAR
                    and not following.dig_cells and abilities.blocks >= 2
                    and self.skill >= 0.55 and rising
                    and z < (s - 2) - 2.0 - 0.15 and not world.solid(*second)
                    and not world.solid(x, y, s - 5)):
                action = BotAction(BotActionKind.BUILD_LINE, tool_id=int(C.BLOCK_TOOL),
                                   position=tuple(float(v) for v in floor),
                                   end_position=tuple(float(v) for v in second))
            else:
                action = BotAction(BotActionKind.BUILD, tool_id=int(C.BLOCK_TOOL),
                                   position=tuple(float(v) for v in floor))
            self.next_build_at = now + 0.18
            self.builds += 1
        jump = False
        if settled and not feet_clear and now - self.jumped_at > 0.32:
            jump = True
            self.jumped_at = now
        return SkillCommand("running", role=role + ("_place" if action.kind is not BotActionKind.NONE
                                                      else "_jump"),
                            direction=centering, jump=jump or bool(observer.wade),
                            affordance=MovementAffordance.BUILD_STEP, look=look,
                            tool_id=int(C.BLOCK_TOOL), action=action)

    def _walk(self, observer: PlayerSnapshot, now: float, step: ClimbStep, role: str,
              wading: bool) -> SkillCommand:
        # Steer at the end of the current run of plain moves of this kind
        # (a swim along a cliff, a level walk): one smooth line, no stops.
        last = step
        if step.kind in (StepKind.SWIM, StepKind.WALK):
            for later in self.plan.steps[self.index + 1:self.index + 6]:
                if (later.kind is not step.kind or later.dig_cells or later.build_cells
                        or later.destination[2] != step.destination[2]):
                    break
                last = later
        target = node_position(last.destination)
        # Ease into a spot where work starts (digging, a floor to lay) so
        # momentum does not carry the body past it.
        following = self.plan.steps[self.index + 1] if self.index + 1 < len(self.plan.steps) else None
        work_next = following is not None and (following.dig_cells or following.build_cells
                                               or following.kind is StepKind.PILLAR)
        arriving = math.hypot(target[0] - observer.position[0],
                              target[1] - observer.position[1]) < 0.9
        # Ease onto a laid step (open air around it) and into work spots.
        careful = arriving and (
            (work_next and step.kind is not StepKind.STAIR)
            or (step.kind is StepKind.STAIR and bool(step.build_cells)))
        command = self._steer(observer, target, role, wading, tolerance=0.2,
                              sneak=bool(last is step and careful))
        stuck = now - self.step_started_at > 1.4
        climbing_out = wading and step.destination[2] < step.source[2]
        jump = climbing_out or (
            step.kind is StepKind.STAIR and stuck and bool(observer.grounded))
        # Swimming to the climbing spot is urgent; a staircase is walked.
        sprint = step.kind is StepKind.SWIM or (step.kind is StepKind.WALK and last is not step)
        return SkillCommand("running", role=role, direction=command.direction, jump=jump,
                            sprint=sprint and not command.sneak, sneak=command.sneak,
                            affordance=self._affordance(step, wading),
                            crouch=False, look=self._gaze(step, observer))

    @staticmethod
    def _affordance(step: ClimbStep, wading: bool) -> MovementAffordance:
        if step.kind is StepKind.SWIM or step.destination[2] >= WATER_SUPPORT_Z:
            return MovementAffordance.SWIM
        if wading and step.kind is not StepKind.PILLAR:
            # Leaving the water onto a ledge: the motor validates the raised
            # landing only for a shore jump.
            return MovementAffordance.JUMP
        if step.kind is StepKind.BRIDGE:
            # Walking onto freshly laid floor is ordinary walking; the live
            # ledge gate still refuses the unbuilt edge.
            return MovementAffordance.WALK
        return MovementAffordance.WALK if step.kind is not StepKind.PILLAR else MovementAffordance.BUILD_STEP

    def _gaze(self, step: ClimbStep, observer: PlayerSnapshot) -> Vector3:
        if step.kind is StepKind.PILLAR:
            return (step.source[0] + 0.5, step.source[1] + 0.5, float(step.source[2]) - 0.6)
        if step.kind is StepKind.BRIDGE:
            travel = (step.destination[0] - step.source[0], step.destination[1] - step.source[1])
            if self.backwards:
                return self._bridge_look(observer, step.destination, travel)
            return (step.destination[0] + 0.5 + travel[0], step.destination[1] + 0.5 + travel[1],
                    float(step.destination[2]) + 0.2)
        # Look at where the feet go next, a touch above the floor.
        later = self.plan.steps[min(len(self.plan.steps) - 1, self.index + 2)]
        target = later.destination
        return (target[0] + 0.5, target[1] + 0.5, float(target[2]) - 1.6)

    @staticmethod
    def _steer(observer: PlayerSnapshot, target: Vector3, role: str, wading: bool,
               *, tolerance: float = 0.25, sneak: bool = False) -> SkillCommand:
        direction = _toward(observer.position, target, deadband=tolerance)
        return SkillCommand("running", role=role, direction=direction, sneak=sneak and not wading,
                            affordance=MovementAffordance.SWIM if wading else MovementAffordance.WALK,
                            look=(target[0], target[1], float(observer.eye[2])))


def _body_overlaps_column(position: Vector3, cell: Cell, *, margin: float = 0.03) -> bool:
    """Does the 0.9-wide body reach into ``cell``'s column (server rule)?"""

    half = 0.45 + margin
    return (math.floor(position[0] - half) <= cell[0] <= math.floor(position[0] + half)
            and math.floor(position[1] - half) <= cell[1] <= math.floor(position[1] + half))


def position_z(observer: PlayerSnapshot) -> float:
    return float(observer.position[2])


def settled_on(observer: PlayerSnapshot, node: Node, world: WorldReader) -> bool:
    body = node_of(observer.position)
    return ((body[0], body[1]) == node[:2] and bool(observer.grounded)
            and -0.5 <= float(observer.position[2]) + SUPPORT_OFFSET - node[2] <= 1.0)


def _toward(position: Vector3, target: Vector3, *, deadband: float) -> Vector3:
    dx = float(target[0]) - float(position[0])
    dy = float(target[1]) - float(position[1])
    length = math.hypot(dx, dy)
    if length <= deadband:
        return (0.0, 0.0, 0.0)
    return (dx / length, dy / length, 0.0)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def in_water(world: WorldReader, observer: PlayerSnapshot) -> bool:
    """Native wade bit, or a body floating on the open water plane."""

    if bool(observer.wade):
        return True
    x, y = int(math.floor(observer.position[0])), int(math.floor(observer.position[1]))
    return (float(observer.position[2]) >= WATER_SUPPORT_Z - SUPPORT_OFFSET - 0.6
            and not world.solid(x, y, WATER_SUPPORT_Z - 1))


def quick_climb_estimate(world, start: Node, abilities: ClimbAbilities,
                         *, radius: int = 12) -> float:
    """Cheap lower-bound-ish seconds to climb onto nearby main ground.

    Used to decide whether running the full planner is worth it. Infinite
    when the atlas knows no main ground nearby or the body can neither dig
    nor build.
    """

    atlas = getattr(world, "_atlas", None)
    if atlas is None or not getattr(atlas, "main_region_id", 0):
        return math.inf
    if abilities.dig is None and not (abilities.can_build and abilities.blocks > 0):
        return math.inf
    per_level = _PILLAR_COST + _BUILD_COST
    if abilities.dig is not None:
        dig_level = _STEP_UP_COST + 2 * (abilities.dig.fire_interval + _AIM_COST) * max(
            1, abilities.dig.swings_per_block)
        per_level = dig_level if not (abilities.can_build and abilities.blocks > 4) else min(
            per_level + abilities.block_penalty, dig_level)
    main = int(atlas.main_region_id)
    best = math.inf
    x0, y0, s0 = start
    for y in range(max(0, y0 - radius), min(MAP_SIZE, y0 + radius + 1)):
        for x in range(max(0, x0 - radius), min(MAP_SIZE, x0 + radius + 1)):
            index = y * atlas.width + x
            if int(atlas.regions[index]) != main:
                continue
            primary = int(atlas.primary_support[index])
            rise = max(0, s0 - primary)
            cost = (abs(x - x0) + abs(y - y0)) * _SWIM_COST + rise * per_level
            if cost < best:
                best = cost
    return best


__all__ = [
    "AscentGoal",
    "AscentSearch",
    "AscentPlan",
    "ClimbAbilities",
    "ClimbStep",
    "LocomotionSkill",
    "SkillCommand",
    "StepKind",
    "dig_aims",
    "estimate_swings",
    "face_supported",
    "find_gap_bridge",
    "in_water",
    "node_of",
    "plan_ascent",
    "plan_fast_bridge",
    "plan_pillar_up",
    "plan_staircase_up",
    "quick_climb_estimate",
    "standable",
]
