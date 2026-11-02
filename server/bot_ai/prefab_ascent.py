"""Plan an actual walk up owned prefabs before spending a single block.

The ordinary block tool is absent from Zombie loadouts. These small authored
hand/bone constructs supply their legal ascent instead. Geometry is received
in the map snapshot; no files or native objects are loaded on a decision.

The zombie hand (135 voxels, 9 wide, 7 deep, 9 tall) is a staircase: a body
on the ground beside the back of its wrist jumps onto the wrist (2 up), jumps
again (4 up), steps onto the palm's edge (5) and the palm (6), onto the
thumb's base (7) and jumps onto the thumb tip (9 up). That native ladder is
measured once per orientation (``ladder_of``) on a flat floor; the planner
then places the hand so that one of its landings ends where the climb can
go on: flush against the structure's face (the claw digs stairs from there),
beside a floor the body can walk or jump onto (a roof's edge, a teammate's
stack), or inside the destination band itself (a stand in the air beside a
cut). Sites are ranked by estimated seconds (the walk to the ladder's foot,
the ladder, the stairs still to dig) and the best few are verified with the
ordinary native route search over the proposed geometry. Nothing is placed
whose landing would be a dead end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math
import time
from typing import Generator

import shared.constants as C

from .messages import BotAction, BotActionKind, MovementAffordance, PerceptionFrame, PlayerSnapshot, Vector3
from .prefab_policy import PrefabGeometry
from .project_sites import _body_overlap
from .recovery_skills import (
    AscentPlan, ClimbStep, StepKind, node_of, standable,
)
from .simple_navigation import SimpleVoxelWorld

Cell = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Landing:
    """One standable voxel of a prefab, anchor-relative, and how to reach it."""

    cell: Cell                  # the prefab voxel stood on (local coordinates of this yaw)
    rise: int                   # levels above the ground the prefab rests on
    cost: float                 # seconds of native moves from ``entry``
    moves: int
    entry: tuple[int, int]      # ground column (local) the climb starts from
    open: tuple[tuple[int, int], ...]   # horizontal normals toward free neighbouring columns


@dataclass(frozen=True, slots=True)
class Ladder:
    """The native way up one prefab standing on flat ground (one yaw)."""

    landings: tuple[Landing, ...]   # rise >= 2, highest first, cheapest first per rise
    top: int                        # the highest rise

    @property
    def entry(self) -> tuple[int, int]:
        return self.landings[0].entry if self.landings else (0, 0)


@dataclass(frozen=True, slots=True)
class PrefabAccess:
    name: str
    anchor: Cell
    yaw: int
    cells: frozenset[Cell]
    route: AscentPlan
    look: Vector3 | None
    authored_cost: int
    # The ladder's foot (a ground node) and the landing the route ends on.
    entry: Cell = (0, 0, 0)
    landing: Cell = (0, 0, 0)
    estimate: float = 0.
    # Every standable voxel of the stack (world nodes), for teammates.
    ladder: tuple[Cell, ...] = ()


@dataclass(slots=True)
class _Slot:
    life: int
    target: Vector3
    job: Generator[None, None, PrefabAccess | None] | None = None
    proposal: PrefabAccess | None = None
    # A verified site whose footprint is still out of placing reach: the
    # body walks to the ladder's foot and places on the way.
    approach: PrefabAccess | None = None
    approach_since: float = 0.
    request: int = 0
    sent_at: float = 0.
    retry_at: float = 0.
    avoided: dict[tuple[str, Cell, int], float] = field(default_factory=dict)
    failed_cells: frozenset[Cell] = frozenset()
    # Which side of the destination the sites are laid out from: 0 is the
    # bearing from the body to the destination, then +90, -90 and 180
    # degrees. Two failures on one side (a rejected placement, a route that
    # could not be walked, no connected site) turn to the next.
    side: int = 0
    failures_seen: int = 0
    own_failures: int = 0
    last_reason: str = ""
    planning_since: float = 0.


# Bearing offsets per ``_Slot.side``: ahead, left, right, behind.
_SIDE_OFFSETS = (0., math.pi / 2, -math.pi / 2, math.pi)
_FAILURES_PER_SIDE = 2
# Planner time per worker decision (a candidate's native route search is
# resumable and continues on the next decision).
_PLAN_SLICE = 0.004
# The melee band around the destination floor a climb may end in.
_REACH_ABOVE = 3
# Placing reach kept well inside the server's (MAX_BLOCK_DISTANCE + 1).
_PLACE_REACH = 5.0
# How far the ladder's foot may be from the body (a short walk, not a trek).
_ENTRY_WALK = 9.0
# Sites scanned around the body, and verified per plan.
_SCAN_RADIUS = 12
_VERIFY_LIMIT = 6
_ROUTE_EXPANSIONS = 320
# Cost model (seconds): a dug stair with the claw, a jump, a walked block.
_CLAW_STAIR = 1.3
_JUMP = 0.9
_WALK = 0.27
_STEP_UP = 0.45
_STEP_DOWN = 0.32

_LADDERS: dict[tuple[str, int], Ladder] = {}


@dataclass(frozen=True, slots=True)
class PrefabStep:
    role: str
    action: BotAction = BotAction()
    look: Vector3 | None = None
    route: AscentPlan | None = None
    # A body position to walk to (the ladder's foot) while the site waits.
    approach: Vector3 | None = None


class _AddedWorld:
    def __init__(self, world: object, cells: frozenset[Cell]) -> None:
        self.world, self.cells = world, cells

    def solid(self, x: int, y: int, z: int) -> bool:
        return (x, y, z) in self.cells or self.world.solid(x, y, z)

    get_solid = solid


class _FlatWorld:
    """A prefab resting on an endless floor: the ladder is measured here."""

    __slots__ = ("cells", "ground")

    def __init__(self, cells: frozenset[Cell], ground: int) -> None:
        self.cells, self.ground = cells, ground

    def solid(self, x: int, y: int, z: int) -> bool:
        return z >= self.ground or (x, y, z) in self.cells

    get_solid = solid


_CARDINALS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _ray_clear(world: object, origin: Vector3, target: Vector3) -> bool:
    """A straight voxel ray from ``origin`` to ``target`` meets nothing solid."""

    dx, dy, dz = (float(target[i]) - float(origin[i]) for i in range(3))
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance <= 1e-6 or distance > 96.:
        return distance <= 1e-6
    steps = max(1, int(math.ceil(distance * 2.)))
    for index in range(1, steps):
        f = index / steps
        if world.solid(int(math.floor(origin[0] + dx * f)), int(math.floor(origin[1] + dy * f)),
                       int(math.floor(origin[2] + dz * f))):
            return False
    return True


def ladder_of(model: PrefabGeometry, yaw: int) -> Ladder:
    """Measure the native landings of ``model`` at ``yaw`` on flat ground.

    Walking (one level up or down) and the native jump (exactly two up with
    head room over the source) are the only moves: no digging, no blocks. A
    landing's ``cell`` is the voxel stood on, relative to the anchor the
    server places the model by; its ``entry`` is the ground column beside
    the footprint where its cheapest climb starts.
    """

    key = (model.name, int(yaw) & 3)
    cached = _LADDERS.get(key)
    if cached is not None:
        return cached
    minimum, maximum = model.bounds_by_yaw[key[1]]
    ox, oy = 64 - minimum[0], 64 - minimum[1]
    ground = 128
    az = ground - 1 - maximum[2]
    cells = frozenset((ox + x, oy + y, az + z) for x, y, z in model.cells_by_yaw[key[1]])
    world = _FlatWorld(cells, ground)
    columns = {(x, y) for x, y, _z in cells}
    lo_x, hi_x = ox + minimum[0] - 1, ox + maximum[0] + 1
    lo_y, hi_y = oy + minimum[1] - 1, oy + maximum[1] + 1
    starts = [(x, y, ground) for x in range(lo_x, hi_x + 1) for y in range(lo_y, hi_y + 1)
              if standable(world, (x, y, ground))]
    cost = {node: 0. for node in starts}
    origin = {node: node for node in starts}
    moves = {node: 0 for node in starts}
    heap = [(0., node) for node in starts]
    heapq.heapify(heap)
    while heap:
        spent, node = heapq.heappop(heap)
        if spent > cost.get(node, math.inf):
            continue
        x, y, s = node
        for dx, dy in _CARDINALS:
            nx, ny = x + dx, y + dy
            if not (lo_x <= nx <= hi_x and lo_y <= ny <= hi_y):
                continue
            for dz, price in ((0, _WALK), (-1, _STEP_UP), (1, _STEP_DOWN), (-2, _JUMP)):
                step = (nx, ny, s + dz)
                if step[2] > ground or not standable(world, step):
                    continue
                if dz == -2 and (world.solid(x, y, s - 4) or world.solid(x, y, s - 5)):
                    continue
                total = spent + price
                if total < cost.get(step, math.inf):
                    cost[step] = total
                    origin[step] = origin[node]
                    moves[step] = moves[node] + 1
                    heapq.heappush(heap, (total, step))
    landings = []
    for node, spent in cost.items():
        x, y, s = node
        if (x, y) not in columns or s >= ground - 1:
            continue
        local = (x - ox, y - oy, s - az)
        free = tuple((dx, dy) for dx, dy in _CARDINALS
                     if not any((x + dx, y + dy, z) in cells for z in range(s - 4, s + 2)))
        entry = origin[node]
        landings.append(Landing(local, ground - s, round(spent, 3), moves[node],
                                (entry[0] - ox, entry[1] - oy), free))
    landings.sort(key=lambda item: (-item.rise, item.cost, item.cell))
    ladder = Ladder(tuple(landings), landings[0].rise if landings else 0)
    if len(_LADDERS) > 64:
        _LADDERS.clear()
    _LADDERS[key] = ladder
    return ladder


_CLIMB_ABILITIES = frozenset({MovementAffordance.JUMP})
_DESCENT_ABILITIES = frozenset({MovementAffordance.JUMP, MovementAffordance.DROP})


def _access_route(world: _AddedWorld, start: Cell, tops: frozenset[Cell],
                  avoided: frozenset[Cell] = frozenset(), limit: int = _ROUTE_EXPANSIONS,
                  abilities: frozenset = _CLIMB_ABILITIES):
    """Resume the ordinary, conservative jump graph over proposed geometry.

    Only already-owned native walking/jumping is permitted. In particular this
    search cannot manufacture floors, dig through a prefab, or add flight.
    """
    if not tops:
        return None
    navigation = SimpleVoxelWorld()
    navigation._vxl = world
    frontier = [(0., start)]
    costs = {start: 0.}
    parents = {}
    expanded = 0
    while frontier and expanded < limit:
        _, node = heapq.heappop(frontier)
        if node in tops:
            steps = []
            destination = node
            while destination != start:
                source, affordance = parents[destination]
                kind = StepKind.JUMP if affordance is MovementAffordance.JUMP else StepKind.WALK
                steps.append(ClimbStep(kind, source, destination))
                destination = source
            steps.reverse()
            return AscentPlan(tuple(steps), costs[node], 0, 0, expanded, costs[node] * .25)
        expanded += 1
        for neighbor, affordance, cost, _ in navigation._neighbors(
                node, abilities=abilities, dig_profile=None, allow_water=False):
            if (abs(neighbor[0] - start[0]) > 13 or abs(neighbor[1] - start[1]) > 13
                    or abs(neighbor[0] - node[0]) + abs(neighbor[1] - node[1]) != 1
                    or neighbor in avoided or not standable(world, neighbor)):
                continue
            proposed = costs[node] + cost
            if proposed >= costs.get(neighbor, math.inf):
                continue
            costs[neighbor] = proposed
            parents[neighbor] = node, affordance
            heuristic = min(math.hypot(neighbor[0]-a, neighbor[1]-b)
                            + max(0, neighbor[2]-c) * .6 for a, b, c in tops)
            heapq.heappush(frontier, (proposed + heuristic, neighbor))
        if expanded & 15 == 0:
            yield
    return None


@dataclass(slots=True)
class _Site:
    estimate: float
    name: str
    yaw: int
    anchor: Cell
    landing: Landing
    kind: str
    ground: int


class PrefabAscent:
    def __init__(self, world: object) -> None:
        self.world = world
        self.slots: dict[tuple[int, int], _Slot] = {}
        self.leases: dict[tuple[int, int], tuple[float, frozenset[Cell]]] = {}
        self.epoch: tuple[int, int] | None = None
        self.metrics: dict[str, int] = {}
        self.landings: dict[tuple[int, Cell], float] = {}
        # Standing stacks per team: their voxels are kept from the claws.
        self.stacks: dict[tuple[int, Cell, int], tuple[float, frozenset[Cell]]] = {}
        # Where each stack's ladder starts (a ground node), for climbing down.
        self.feet: dict[tuple[int, Cell, int], Cell] = {}

    def reset(self) -> None:
        self.slots.clear()
        self.leases.clear()
        self.epoch = None
        self.landings.clear()
        self.stacks.clear()

    def protected_cells(self, team: int, now: float) -> frozenset[Cell]:
        """Voxels of this team's standing stacks: no claw ramp may take them."""

        cells: set[Cell] = set()
        for key, (until, stack) in list(self.stacks.items()):
            if until <= now:
                self.stacks.pop(key, None)
            elif key[0] == team:
                cells.update(stack)
        return frozenset(cells)

    def forget(self, player_id: int, generation: int) -> None:
        self.slots.pop((player_id, generation), None)
        self.leases.pop((player_id, generation), None)

    def _count(self, reason: str) -> None:
        self.metrics[reason] = self.metrics.get(reason, 0) + 1

    @staticmethod
    def _protected(frame: PerceptionFrame, cell: Cell) -> bool:
        x, y, _ = cell
        return any(x0 <= x <= x1 and y0 <= y <= y1 for x0, x1, y0, y1 in frame.protected_build_bounds) or any(
            (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 for cx, cy, radius in frame.protected_build_circles)

    def _valid(self, frame: PerceptionFrame, actor: PlayerSnapshot,
               cells: frozenset[Cell], *, require_new: bool) -> bool:
        if any(not (0 <= x < 512 and 0 <= y < 512 and 1 <= z <= 238)
               or self._protected(frame, (x, y, z)) for x, y, z in cells):
            return False
        friends = tuple(p.position for p in frame.players if p.alive and p.spawned
                        and p.team == actor.team and math.dist(p.position, actor.position) < 20)
        if _body_overlap(cells, friends):
            return False
        if require_new and not any(not self.world.solid(*cell) for cell in cells):
            return False
        # Stock attachment is face-connected. An unsupported cloud of prefab
        # voxels is never a construction route.
        return any(self.world.solid(x + dx, y + dy, z + dz)
                   for x, y, z in cells for dx, dy, dz in
                   ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)))

    @staticmethod
    def _finals(world: object, target: Vector3) -> frozenset[Cell]:
        """Standing nodes that count as having reached the destination.

        The destination is a floor to stand on (the roof beside a pillar, a
        cut site's stand, or the prey itself); its melee band runs from one
        level below that floor to three above it, within 1.5 blocks.
        """

        tx, ty = float(target[0]), float(target[1])
        support = int(round(float(target[2]) + 2.25))
        return frozenset(
            (x, y, z)
            for x in range(int(math.floor(tx)) - 1, int(math.floor(tx)) + 2)
            for y in range(int(math.floor(ty)) - 1, int(math.floor(ty)) + 2)
            if math.hypot(x + .5 - tx, y + .5 - ty) <= 1.5
            for z in range(support - _REACH_ABOVE, support + 2)
            if standable(world, (x, y, z)))

    @staticmethod
    def _in_band(node: Cell, target: Vector3) -> bool:
        support = int(round(float(target[2]) + 2.25))
        return (math.hypot(node[0] + .5 - float(target[0]), node[1] + .5 - float(target[1])) <= 1.5
                and support - _REACH_ABOVE <= node[2] <= support + 1)

    def _look_at(self, actor: PlayerSnapshot, cells: frozenset[Cell]) -> Vector3 | None:
        """A real footprint voxel the crosshair reaches (a human's placement)."""

        eye = actor.eye
        visible = sorted(cells, key=lambda c: (c[0] + .5 - eye[0]) ** 2 + (c[1] + .5 - eye[1]) ** 2
                         + (c[2] + .5 - eye[2]) ** 2)[:12]
        for a, b, c in visible:
            centre = (a + .5, b + .5, c + .5)
            if math.dist(eye, centre) > _PLACE_REACH:
                return None
            if self.world.has_line_of_sight(eye, centre):
                return centre
        return None

    # -- continuation ---------------------------------------------------------

    @staticmethod
    def _face_beside(world: object, node: Cell) -> bool:
        """A structure's face beside ``node`` at the level of the next stair."""

        x, y, z = node
        return any(world.solid(x + dx, y + dy, z - 1) and world.solid(x + dx, y + dy, z)
                   for dx, dy in _CARDINALS)

    def _continuation(self, world: object, node: Cell, target: Vector3,
                      blocks_left: int, cheapest: int) -> tuple[str, float]:
        """How the climb goes on from ``node``: (kind, estimated seconds).

        ``band``: arrived. ``step``: a floor of the world beside it to walk
        or jump onto (the roof's edge, a terrace, a teammate's stack).
        ``face``: the structure's face beside it, a claw stair up per level.
        ``chain``: another owned prefab from here. ``""``: a dead end.
        """

        if self._in_band(node, target):
            return "band", 0.
        x, y, z = node
        support = int(round(float(target[2]) + 2.25))
        across = max(0., math.hypot(x + .5 - float(target[0]), y + .5 - float(target[1])) - 1.5)
        levels = max(0, z - support - 1)
        best: tuple[str, float] | None = None
        for dx, dy in _CARDINALS:
            nx, ny = x + dx, y + dy
            for dz in (0, -1, 1, -2):
                floor = (nx, ny, z + dz)
                if not standable(world, floor):
                    continue
                # A floor level with the band (or above it) is walked to the
                # destination; a lower terrace only helps when the structure's
                # face stands beside it too (the stairs go on from there).
                left = max(0, floor[2] - support - 1)
                if left and not any(world.solid(nx + ex, ny + ey, floor[2] - 1)
                                    and world.solid(nx + ex, ny + ey, floor[2])
                                    for ex, ey in _CARDINALS):
                    break
                price = (_JUMP if dz == -2 else _STEP_UP if dz == -1 else _WALK)
                price += across * _WALK + left * _CLAW_STAIR
                if best is None or price < best[1]:
                    best = ("step", price)
                break
            if world.solid(nx, ny, z - 1) and world.solid(nx, ny, z) and levels > 0:
                price = levels * _CLAW_STAIR + max(0., across - levels) * _WALK
                if best is None or price < best[1]:
                    best = ("face", price)
        if best is not None:
            return best
        if blocks_left >= cheapest > 0 and levels >= 4:
            return "chain", 2. + levels * _STEP_UP + across * _WALK
        return "", math.inf

    # -- candidates -----------------------------------------------------------

    def _sites(self, actor: PlayerSnapshot, target: Vector3, slot: _Slot,
               names: list[tuple[str, PrefabGeometry]]) -> Generator[None, None, list[_Site]]:
        """Rank where an owned prefab could stand so that a landing goes on.

        The columns around the body (toward the destination) are read once
        for their solids between the body's feet and twelve levels up; every
        ladder landing that could sit beside a face or a floor of those
        columns, or inside the destination band, proposes an anchor.
        """

        world = self.world
        start = node_of(actor.position)
        sx, sy, s = start
        tx, ty = float(target[0]), float(target[1])
        support = int(round(float(target[2]) + 2.25))
        dx, dy = tx - actor.position[0], ty - actor.position[1]
        span = math.hypot(dx, dy)
        bearing = math.atan2(dy, dx) if span > .5 else math.atan2(*actor.orientation[1::-1])
        preferred = bearing + _SIDE_OFFSETS[slot.side % len(_SIDE_OFFSETS)]
        ux, uy = math.cos(bearing), math.sin(bearing)
        px, py = math.cos(preferred), math.sin(preferred)
        cheapest = min((model.block_count for _n, model in names), default=0)
        sites: dict[tuple[str, int, Cell], _Site] = {}

        def consider(name: str, model: PrefabGeometry, yaw: int, landing: Landing,
                     anchor_xy: tuple[int, int], kind: str, extra: float) -> None:
            ex, ey = anchor_xy[0] + landing.entry[0], anchor_xy[1] + landing.entry[1]
            if not (0 <= ex < 512 and 0 <= ey < 512):
                return
            walk = math.hypot(ex + .5 - actor.position[0], ey + .5 - actor.position[1])
            if walk > _ENTRY_WALK:
                return
            ground = next((g for g in (s, s - 1, s + 1) if standable(world, (ex, ey, g))), None)
            if ground is None:
                return
            maximum = model.bounds_by_yaw[yaw][1]
            anchor = (anchor_xy[0], anchor_xy[1], ground - 1 - maximum[2])
            node = (anchor[0] + landing.cell[0], anchor[1] + landing.cell[1], anchor[2] + landing.cell[2])
            if node[2] - 3 < 1 or anchor[2] + maximum[2] > 238:
                return
            estimate = (max(0., walk - .5) * _WALK + abs(ground - s) * _STEP_UP + landing.cost + extra)
            # The side the stacking was turned to is preferred, not forced.
            away = (node[0] + .5 - actor.position[0]) * px + (node[1] + .5 - actor.position[1]) * py
            if slot.side and away < 0:
                estimate += 2.
            key = (name, yaw, anchor)
            known = sites.get(key)
            if known is None or estimate < known.estimate:
                sites[key] = _Site(estimate, name, yaw, anchor, landing, kind, ground)

        # Columns to read: toward the destination, within reach of a short walk.
        columns = []
        for cx in range(sx - _SCAN_RADIUS, sx + _SCAN_RADIUS + 1):
            for cy in range(sy - _SCAN_RADIUS, sy + _SCAN_RADIUS + 1):
                if not (0 <= cx < 512 and 0 <= cy < 512):
                    continue
                ahead = (cx + .5 - actor.position[0]) * ux + (cy + .5 - actor.position[1]) * uy
                if span > 3. and ahead < -4.:
                    continue
                if math.hypot(cx + .5 - tx, cy + .5 - ty) > 18.:
                    continue
                columns.append((cx, cy))
        yield
        # Solids of each column from twelve levels up to the feet; a column
        # with nothing there (open street) offers neither face nor floor.
        profile: dict[tuple[int, int], tuple[bool, ...]] = {}
        for index, (cx, cy) in enumerate(columns):
            run = tuple(world.solid(cx, cy, z) for z in range(s - 13, s + 1))
            if any(run[:-1]):
                profile[(cx, cy)] = run
            if index & 63 == 63:
                yield
        for name, model in names:
            for yaw in range(4):
                ladder = ladder_of(model, yaw)
                edge = [item for item in ladder.landings if item.open and item.rise >= 4][:14]
                if not edge:
                    continue
                # Landings inside the destination band: the stack is the stand.
                # A landing under the destination that is still short of it is
                # the last resort (a lone pillar-up toward a point in the sky):
                # it is only kept when the verification finds a way on from
                # it, which with nothing to stand near means another owned
                # prefab stacked on this one (``_continuation`` "chain").
                for landing in edge:
                    for cx in range(int(math.floor(tx)) - 1, int(math.floor(tx)) + 2):
                        for cy in range(int(math.floor(ty)) - 1, int(math.floor(ty)) + 2):
                            anchor_xy = (cx - landing.cell[0], cy - landing.cell[1])
                            node_z = s - 1 - model.bounds_by_yaw[yaw][1][2] + landing.cell[2]
                            if math.hypot(cx + .5 - tx, cy + .5 - ty) > 1.5:
                                continue
                            if support - _REACH_ABOVE - 1 <= node_z <= support + 2:
                                consider(name, model, yaw, landing, anchor_xy, "band", 0.)
                            elif node_z > support and landing.rise >= ladder.top - 1:
                                consider(name, model, yaw, landing, anchor_xy, "chain",
                                         4. + (node_z - support - 1) * 1.)
                yield
                # Landings beside a face (claw stairs) or a floor (a step).
                for (cx, cy), run in profile.items():
                    for landing in edge:
                        node_z = s - 1 - model.bounds_by_yaw[yaw][1][2] + landing.cell[2]
                        levels = max(0, node_z - support - 1)
                        for nx, ny in landing.open:
                            lx, ly = cx - nx, cy - ny
                            # The landing's own column must be open around it.
                            if any(world.solid(lx, ly, node_z + k) for k in (-2, -1, 0)):
                                continue
                            anchor_xy = (lx - landing.cell[0], ly - landing.cell[1])
                            across = max(0., math.hypot(lx + .5 - tx, ly + .5 - ty) - 1.5)
                            floor = node_z - 1 - (s - 13)
                            if (levels > 0 and 0 <= floor < len(run) and run[floor]
                                    and 0 <= floor + 1 < len(run) and run[floor + 1]):
                                consider(name, model, yaw, landing, anchor_xy, "face",
                                         levels * _CLAW_STAIR + max(0., across - levels) * _WALK)
                            for dz, price in ((0, _WALK), (-1, _STEP_UP), (1, _STEP_DOWN), (-2, _JUMP)):
                                zz = node_z + dz
                                if standable(world, (cx, cy, zz)):
                                    consider(name, model, yaw, landing, anchor_xy, "step",
                                             price + across * _WALK
                                             + max(0, zz - support - 1) * _CLAW_STAIR)
                                    break
                yield
        ranked = sorted(sites.values(), key=lambda item: (item.estimate, item.name, item.yaw, item.anchor))
        return ranked

    def _candidates(self, frame: PerceptionFrame, actor: PlayerSnapshot,
                    target: Vector3, slot: _Slot,
                    threat: Vector3 | None = None) -> Generator[None, None, PrefabAccess | None]:
        geometry = getattr(self.world, "prefab_geometry", {})
        start = node_of(actor.position)
        reserved = frozenset(cell for key, (until, cells) in self.leases.items()
                             if key != (actor.player_id, actor.generation) and until > frame.created_at for cell in cells)
        # A teammate's stack whose landing is above this body: climb it
        # first (and from it, the destination itself when a walk or jump
        # leads there). Only the floors that are really standing count, and
        # the highest of them are the goal: one route up, not one per rung.
        existing = frozenset(cell for (team, cell), until in self.landings.items()
            if team == actor.team and until > frame.created_at and cell[2] <= start[2]-3
            and math.dist(cell[:2], start[:2]) <= 12 and standable(self.world,cell))
        if existing:
            highest = min(cell[2] for cell in existing)
            tops = frozenset(cell for cell in existing if cell[2] <= highest + 1)
            finals = self._finals(self.world, target)
            route = yield from _access_route(_AddedWorld(self.world, frozenset()), start,
                                             tops | finals, slot.failed_cells, limit=400)
            if route is not None and route.steps:
                return PrefabAccess("", (0,0,0), 0, frozenset(), route,
                    tuple(float(v) for v in route.destination), 0,
                    landing=route.destination)
        names = [(name, geometry[name.lower()]) for name in sorted(
                    (name for name in actor.prefabs if name.lower() in geometry),
                    key=lambda name: ("hand" not in name.lower(), name))
                 if 0 < geometry[name.lower()].block_count <= min(160, actor.blocks)][:3]
        if not names:
            return None
        cheapest = min(model.block_count for _n, model in names)
        ranked = yield from self._sites(actor, target, slot, names)
        self.metrics["sites_ranked"] = self.metrics.get("sites_ranked", 0) + len(ranked)
        if threat is not None and ranked:
            # A ladder in the survivor's sight is a queue for his rifle: among
            # the best sites, one whose foot, rungs and landing he cannot see
            # goes first (each exposed point costs about a second of fire).
            # The stack itself is cover: the ray is cast through the proposed
            # geometry, so a hand whose palm shades its own wrist from the
            # shooter ranks ahead of the same hand turned the other way.
            eye = (float(threat[0]), float(threat[1]), float(threat[2]) - .8)
            head = ranked[:12]
            for site in head:
                model = geometry[site.name.lower()]
                ax, ay, az = site.anchor
                shaded = _AddedWorld(self.world, frozenset(
                    (ax + a, ay + b, az + c) for a, b, c in model.cells_by_yaw[site.yaw]))
                top = site.landing.cell
                points = (
                    (ax + site.landing.entry[0] + .5, ay + site.landing.entry[1] + .5, site.ground - 1.2),
                    (ax + top[0] + .5, ay + top[1] + .5, az + top[2] - 1.2),
                    (ax + (top[0] + site.landing.entry[0]) / 2 + .5, ay + (top[1] + site.landing.entry[1]) / 2 + .5,
                     (az + top[2] + site.ground) / 2 - 1.2),
                )
                site.estimate += 1.2 * sum(1 for point in points if _ray_clear(shaded, eye, point))
                yield
            head.sort(key=lambda item: (item.estimate, item.name, item.yaw, item.anchor))
            ranked[:12] = head
        verified = 0
        for site in ranked:
            if verified >= _VERIFY_LIMIT:
                break
            if slot.avoided.get((site.name, site.anchor, site.yaw), 0) > frame.created_at:
                continue
            model = geometry[site.name.lower()]
            ax, ay, az = site.anchor
            cells = frozenset((ax + a, ay + b, az + c) for a, b, c in model.cells_by_yaw[site.yaw])
            if not reserved.isdisjoint(cells) or not self._valid(frame, actor, cells, require_new=True):
                continue
            # A hand buried in the structure is wasted material (and a human
            # cannot push a ghost into a wall); a voxel or two overlapping
            # the ground is ordinary placement.
            if sum(1 for cell in cells if self.world.solid(*cell)) * 10 > len(cells):
                continue
            verified += 1
            added = _AddedWorld(self.world, cells)
            landing = (ax + site.landing.cell[0], ay + site.landing.cell[1], az + site.landing.cell[2])
            if not standable(added, landing):
                continue
            finals = self._finals(added, target)
            route = yield from _access_route(added, start, frozenset({landing}) | finals, slot.failed_cells)
            if route is None or not route.steps:
                self._count("site_unroutable")
                continue
            if not any(step.destination in cells for step in route.steps):
                # The destination is reachable without the stack: walk (and
                # jump) there, keep the blocks.
                self._count("routes_without_prefab")
                return PrefabAccess("", (0, 0, 0), 0, frozenset(), route,
                                    tuple(float(v) for v in route.destination), 0,
                                    landing=route.destination)
            end = route.destination
            if end not in finals:
                kind, _price = self._continuation(added, end, target, actor.blocks - model.block_count, cheapest)
                if kind == "step" and not self._face_beside(added, end):
                    # A floor beside the landing is only a way on when it
                    # leads to the destination: a post's top or a ledge
                    # that ends in the air is as much a dead end as nothing.
                    onward = yield from _access_route(added, end, finals, slot.failed_cells)
                    if onward is None:
                        kind = ""
                if not kind:
                    # A landing in the open with no wall to claw into and no
                    # blocks for another stack strands the climber on its
                    # own prefab.
                    self._count("landing_dead_end")
                    slot.avoided[(site.name, site.anchor, site.yaw)] = frame.created_at + 30
                    continue
            self._count("plans_found")
            entry = (ax + site.landing.entry[0], ay + site.landing.entry[1], site.ground)
            rungs = tuple((ax + item.cell[0], ay + item.cell[1], az + item.cell[2])
                          for item in ladder_of(model, site.yaw).landings)
            return PrefabAccess(site.name, site.anchor, site.yaw, cells, route,
                                self._look_at(actor, cells), model.block_count,
                                entry=entry, landing=end, estimate=site.estimate, ladder=rungs)
        if ranked and verified == 0:
            self._count("sites_all_blocked")
        return None

    def describe(self, actor: PlayerSnapshot) -> str:
        slot = self.slots.get((actor.player_id, actor.generation))
        if slot is None or slot.life != actor.life_id:
            return ""
        phase = ("approach" if slot.approach is not None else "proposal" if slot.proposal is not None
                 else "planning" if slot.job is not None else "")
        return f"side={slot.side} failures={slot.own_failures} last={slot.last_reason} {phase}"

    def _fail(self, slot: _Slot, reason: str) -> None:
        slot.own_failures += 1
        slot.last_reason = reason
        self._count(reason)

    def _register(self, actor: PlayerSnapshot, proposal: PrefabAccess, now: float) -> None:
        rungs = proposal.ladder or tuple(step.destination for step in proposal.route.steps
                                          if step.destination[2] <= proposal.route.steps[0].source[2] - 2)
        for cell in rungs:
            self.landings[(actor.team, cell)] = now + 60
        while len(self.landings) > 160:
            self.landings.pop(min(self.landings, key=self.landings.get))
        if proposal.cells:
            key = (actor.team, proposal.anchor, proposal.yaw)
            self.stacks[key] = now + 120, proposal.cells
            self.feet[key] = proposal.entry
            while len(self.stacks) > 24:
                dropped = min(self.stacks, key=lambda key: self.stacks[key][0])
                self.stacks.pop(dropped)
                self.feet.pop(dropped, None)

    def _descend(self, actor: PlayerSnapshot, node: Cell, avoided: frozenset[Cell],
                 now: float) -> AscentPlan | None:
        """Climb back down the stack ``node`` stands on and forget its rungs.

        A landing that leads nowhere (the claw found no stair from it, the
        structure changed) is left for good: its rungs are no longer offered
        to teammates and the body returns to the ladder's foot, where a
        fresh site is chosen. Dropping two levels at a time is allowed on
        the way down.
        """

        stack = next((key for key, (until, cells) in self.stacks.items()
                      if until > now and key[0] == actor.team and node in cells), None)
        if stack is None:
            return None
        _until, cells = self.stacks[stack]
        for cell in cells:
            self.landings.pop((actor.team, cell), None)
        foot = self.feet.get(stack)
        ground = set()
        columns = {(x, y) for x, y, _z in cells}
        base = max(z for _x, _y, z in cells) + 1
        for x, y in columns:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if (x + dx, y + dy) in columns:
                        continue
                    for z in (base, base - 1, base + 1):
                        if standable(self.world, (x + dx, y + dy, z)):
                            ground.add((x + dx, y + dy, z))
        if foot is not None and standable(self.world, foot):
            ground.add(foot)
        job = _access_route(_AddedWorld(self.world, frozenset()), node, frozenset(ground), avoided,
                            abilities=_DESCENT_ABILITIES)
        deadline = time.perf_counter() + 0.012
        try:
            while True:
                next(job)
                if time.perf_counter() > deadline:
                    break
        except StopIteration as result:
            return result.value
        return None

    def _reroute(self, actor: PlayerSnapshot, proposal: PrefabAccess, target: Vector3,
                 avoided: frozenset[Cell]) -> AscentPlan | None:
        """The committed route, from where the body stands now.

        The site was chosen (and placed) while the body walked; the plan's
        first steps may start a few blocks away. One bounded search over the
        real world (the stack is standing) finds the walk to the ladder's
        foot and the rungs from here.
        """

        start = node_of(actor.position)
        first = proposal.route.steps[0].source if proposal.route.steps else None
        if first is not None and start[:2] == first[:2] and abs(start[2] - first[2]) <= 1:
            return proposal.route
        self._count("routes_restarted")
        finals = self._finals(self.world, target)
        job = _access_route(_AddedWorld(self.world, frozenset()), start,
                            frozenset({proposal.landing}) | finals, avoided)
        deadline = time.perf_counter() + 0.012
        try:
            while True:
                next(job)
                if time.perf_counter() > deadline:
                    break
        except StopIteration as result:
            return result.value
        return None

    def _build_step(self, slot: _Slot, frame: PerceptionFrame, actor: PlayerSnapshot,
                    proposal: PrefabAccess, now: float) -> PrefabStep:
        slot.proposal, slot.sent_at = proposal, now
        slot.approach = None
        slot.request = int(frame.frame_id) * 256 + int(actor.player_id) + 1
        self.leases[(actor.player_id, actor.generation)] = now + 2, proposal.cells
        return PrefabStep("prefab_ascent_build", action=BotAction(
            BotActionKind.PLACE_PREFAB, tool_id=int(C.ZOMBIE_PREFAB_TOOL),
            position=tuple(float(v) for v in proposal.anchor), argument=proposal.name,
            yaw=proposal.yaw * math.pi / 2, request_id=slot.request, exact_placement=True),
            look=proposal.look)

    def step(self, frame: PerceptionFrame, actor: PlayerSnapshot, target: Vector3,
             *, avoided: frozenset[Cell] = frozenset(), failures: int = 0,
             threat: Vector3 | None = None, failure_reason: str = "") -> PrefabStep | None:
        """One decision of prefab stacking toward ``target``.

        ``target`` is the destination floor to stand on (its melee band is
        the goal; see ``_finals``), 3 or more levels above the body and
        within 14 columns. ``failures`` is the skill driver's recent failure
        count for this body (routes that could not be walked); with this
        controller's own (rejected placements, no connected site) every two
        turn the site search to another side of the destination. ``threat``
        is the survivor's position: among equally quick sites, a ladder he
        cannot see is preferred. ``failure_reason`` is the driver's last
        failure: a claw that found no stair from a stack's landing sends the
        body back down (``prefab_ascent_descend``) rather than leaving it
        standing there.
        """

        if any(o.kind=="zombie_order" and o.carrier_id==actor.player_id and o.planning_pending
               for o in frame.objectives):
            # Do not give a tower extra grounding connections while another
            # crew is proving how to disconnect it. Walking and excavation
            # remain available; prefab ascent starts if no legal cut exists.
            return None
        if (int(C.ZOMBIE_PREFAB_TOOL) not in actor.loadout or actor.wade or not actor.grounded
                or not getattr(self.world, "prefab_geometry", None)):
            return None
        now = frame.created_at
        if failure_reason in ("climb_no_plan", "climb_search_timeout", "climb_fail_dig_unreachable",
                              "dig_unreachable") and self.stacks:
            node = node_of(actor.position)
            if (actor.team, node) in self.landings and not self._in_band(node, target):
                route = self._descend(actor, node, avoided, now)
                if route is not None and route.steps:
                    self._count("descents")
                    slot = self.slots.get((actor.player_id, actor.generation))
                    if slot is not None:
                        slot.job = None
                        slot.approach = None
                    return PrefabStep("prefab_ascent_descend", route=route)
        if (actor.position[2] - target[2] < 3
                or math.dist(actor.position[:2], target[:2]) > 14):
            return None
        epoch = frame.map_epoch, frame.mode_epoch
        if self.epoch != epoch:
            self.reset()
            self.epoch = epoch
        key = actor.player_id, actor.generation
        slot = self.slots.get(key)
        if slot is None or slot.life != actor.life_id or math.dist(slot.target, target) > 4.5:
            if len(self.slots) >= 64:
                self.slots.pop(next(iter(self.slots)))
            slot = self.slots[key] = _Slot(actor.life_id, target, failures_seen=int(failures))
            self.leases.pop(key, None)
        if now < slot.retry_at:
            return None
        if avoided != slot.failed_cells:
            slot.failed_cells = avoided
            slot.job = None
        total = int(failures) + slot.own_failures
        if total - slot.failures_seen >= _FAILURES_PER_SIDE:
            # This side keeps failing (a stack that cannot be walked, a
            # placement the server refused, nothing connected here): lay the
            # next stack out from another side of the destination.
            slot.failures_seen = total
            slot.side += 1
            slot.job = None
            slot.proposal = None
            slot.approach = None
            self.leases.pop(key, None)
            self._count("side_rotated")
        proposal = slot.proposal
        if proposal is not None:
            if actor.last_action_request_id == slot.request:
                ready = (actor.last_action_accepted and actor.last_task_accepted
                         and all(self.world.solid(*cell) for cell in proposal.cells))
                slot.proposal = None
                self.leases.pop(key, None)
                if ready:
                    route = self._reroute(actor, proposal, target, avoided)
                    self._register(actor, proposal, now)
                    if route is None or not route.steps:
                        # The stack stands; the next plan climbs it as a
                        # teammate's (``landings``).
                        slot.retry_at = now + .2
                        self._fail(slot, "route_lost_after_placement")
                        return None
                    self._count("routes_committed")
                    return PrefabStep("prefab_ascent_ready", route=route)
                slot.avoided[(proposal.name, proposal.anchor, proposal.yaw)] = now + 20
                slot.retry_at = now + .3
                self._fail(slot, "placement_rejected_or_changed")
                return None
            if now - slot.sent_at <= 1.0:
                return PrefabStep("prefab_ascent_confirm", look=proposal.look)
            slot.proposal = None
            self.leases.pop(key, None)
            slot.avoided[(proposal.name, proposal.anchor, proposal.yaw)] = now + 20
            slot.retry_at = now + .3
            self._fail(slot, "placement_timeout")
            return None
        approach = slot.approach
        if approach is not None:
            foot = (approach.entry[0] + .5, approach.entry[1] + .5, approach.entry[2] - 2.25)
            look = self._look_at(actor, approach.cells)
            if look is not None:
                if (self._valid(frame, actor, approach.cells, require_new=True)
                        and approach.authored_cost <= actor.blocks):
                    self._count("approach_placed")
                    return self._build_step(slot, frame, actor, PrefabAccess(
                        approach.name, approach.anchor, approach.yaw, approach.cells, approach.route,
                        look, approach.authored_cost, approach.entry, approach.landing,
                        approach.estimate, approach.ladder), now)
                slot.approach = None
                slot.avoided[(approach.name, approach.anchor, approach.yaw)] = now + 10
                self._fail(slot, "approach_site_lost")
                return None
            if now - slot.approach_since > 4.0 or math.dist(actor.position[:2], foot[:2]) < .7:
                slot.approach = None
                slot.avoided[(approach.name, approach.anchor, approach.yaw)] = now + 10
                self._fail(slot, "approach_timeout")
                return None
            self.leases[key] = now + 2, approach.cells
            return PrefabStep("prefab_ascent_approach", look=target, approach=foot)
        if slot.job is None:
            slot.avoided = {key: until for key, until in slot.avoided.items() if until > now}
            slot.job = self._candidates(frame, actor, target, slot, threat)
            slot.planning_since = now
        deadline = time.perf_counter() + _PLAN_SLICE
        while time.perf_counter() < deadline:
            try:
                next(slot.job)
            except StopIteration as result:
                slot.job = None
                proposal = result.value
                if proposal is None:
                    # Nothing connected from this side with what this body
                    # owns. The claw ramp gets its turn meanwhile.
                    slot.retry_at = now + 1.0
                    self._fail(slot, "no_connected_ascent")
                    return None
                if not proposal.name:
                    if all(standable(self.world,step.destination) for step in proposal.route.steps):
                        # The body kept walking while the search ran: the
                        # route is re-threaded from where it stands now.
                        route = self._reroute(actor, proposal, target, avoided)
                        if route is None or not route.steps:
                            slot.retry_at = now + .3
                            self._fail(slot, "shared_route_lost")
                            return None
                        self._count("routes_reused")
                        return PrefabStep("prefab_ascent_shared", route=route)
                    return None
                if not self._valid(frame, actor, proposal.cells, require_new=True) or proposal.authored_cost > actor.blocks:
                    slot.retry_at = now + .3
                    return None
                if proposal.look is None:
                    # Verified, but the footprint is beyond placing reach:
                    # walk to the ladder's foot and place on the way.
                    slot.approach, slot.approach_since = proposal, now
                    self.leases[key] = now + 2, proposal.cells
                    self._count("approaches")
                    foot = (proposal.entry[0] + .5, proposal.entry[1] + .5, proposal.entry[2] - 2.25)
                    return PrefabStep("prefab_ascent_approach", look=target, approach=foot)
                return self._build_step(slot, frame, actor, proposal, now)
        if now - slot.planning_since > 1.0:
            self._count("planning_over_1s")
        return PrefabStep("prefab_ascent_planning", look=target)
