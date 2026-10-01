"""Block-line build planning for schematics and arbitrary block structures.

The planner turns a :class:`Placement` into an ordered list of
:class:`BuildStep` s. Each step is one straight BlockLine (packet 40, the
retail block-tool drag) or a single block. It mirrors the server's
``CombatSystem.handle_block_line`` rules:

* every cell in line order must FACE-touch a solid voxel or an earlier cell
  of the same line (``_block_supported`` with ``pending``), so a run is valid
  exactly when its first cell (the drag start) is supported;
* at most ``MAX_LINE_CELLS`` cells (human-plausible, below the server's 64);
* both endpoints within ``PLAN_REACH`` of the builder's eye and visible from
  it (the retail client places onto the face under the crosshair);
* no planned cell inside the builder's own body column.

It also chooses where the builder stands for every step: a body-clear
support (terrain or already-built structure, so bots climb what they built)
reachable by walking with one-block step-ups from the site boundary, from
which the builder can still walk out after the step.

Everything is bounded (site region, cell count) and pure: the same code runs
at plan time over a :class:`VoxelView` overlay and at decision time over the
worker's live :class:`SimpleVoxelWorld`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
from typing import Iterable, Mapping, Sequence

from .model import MAX_BUILD_Z, MIN_BUILD_Z, Cell, Placement, SolidFn

MAX_LINE_CELLS = 8            # retail drag a player makes while standing; server cap is 64
SERVER_LINE_CELLS = 64        # CombatSystem.BLOCK_LINE_MAX_CELLS
PLAN_REACH = 5.5              # eye -> cell centre; the server allows ~11.6
EYE_ABOVE_SUPPORT = 2.25      # PLAYER_SUPPORT_OFFSET: eye/position z = support - 2.25
STAND_MARGIN = 4              # stand search ring around the footprint
MAX_PLAN_CELLS = 400
_FACES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))
_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))

Node = tuple[int, int, int]   # (x, y, support_z)
Vector3 = tuple[float, float, float]


class VoxelView:
    """A solid() overlay: base occupancy plus planned additions."""

    __slots__ = ("_base", "added")

    def __init__(self, base: SolidFn, added: Iterable[Cell] = ()) -> None:
        self._base = base
        self.added: set[Cell] = set(added)

    def solid(self, x: int, y: int, z: int) -> bool:
        return (x, y, z) in self.added or bool(self._base(x, y, z))

    def with_cells(self, cells: Iterable[Cell]) -> "VoxelView":
        view = VoxelView(self._base, self.added)
        view.added.update(cells)
        return view


@dataclass(frozen=True, slots=True)
class BuildStep:
    """One BlockLine (``len(cells) > 1``) or single block.

    ``cells`` are in placement order; ``cells[0]`` is the drag start and is
    the supported end. ``stand`` is the plan-time body position (callers
    re-choose stands at decision time with :func:`find_stand`).
    """

    index: int
    cells: tuple[Cell, ...]
    color: str
    stand: Vector3 | None = None
    foundation: bool = False

    @property
    def start(self) -> Cell:
        return self.cells[0]

    @property
    def end(self) -> Cell:
        return self.cells[-1]

    @property
    def is_line(self) -> bool:
        return len(self.cells) > 1


@dataclass(frozen=True, slots=True)
class BuildPlan:
    placement: Placement
    steps: tuple[BuildStep, ...]
    unbuildable: tuple[Cell, ...] = ()
    region: tuple[int, int, int, int] = (0, 0, 0, 0)
    estimated_seconds: float = 0.0
    walk_cells: int = 0
    cell_step: Mapping[Cell, int] = field(default_factory=dict, compare=False)

    @property
    def feasible(self) -> bool:
        return not self.unbuildable and bool(self.steps)

    @property
    def cost(self) -> int:
        return sum(len(step.cells) for step in self.steps)

    @property
    def lines(self) -> int:
        return sum(1 for step in self.steps if step.is_line)

    @property
    def singles(self) -> int:
        return sum(1 for step in self.steps if not step.is_line)

    @property
    def line_cells(self) -> int:
        return sum(len(step.cells) for step in self.steps if step.is_line)


# --- geometry helpers ---------------------------------------------------------

def supported(solid: SolidFn, cell: Cell, pending: frozenset[Cell] | set[Cell] = frozenset()) -> bool:
    """Server ``_block_supported``: build band plus one solid/pending face."""

    x, y, z = cell
    if not MIN_BUILD_Z <= z <= MAX_BUILD_Z:
        return False
    return any((x + dx, y + dy, z + dz) in pending or solid(x + dx, y + dy, z + dz)
               for dx, dy, dz in _FACES)


def line_valid(solid: SolidFn, cells: Sequence[Cell]) -> bool:
    """Exactly the server's per-line support walk (already-solid cells skipped)."""

    pending: set[Cell] = set()
    for cell in cells:
        if solid(*cell):
            continue
        if not supported(solid, cell, pending):
            return False
        pending.add(cell)
    return True


def cell_centre(cell: Cell) -> Vector3:
    return (cell[0] + .5, cell[1] + .5, cell[2] + .5)


def ray_clear(solid: SolidFn, origin: Vector3, target_cell: Cell,
              ignore: frozenset[Cell] | set[Cell] = frozenset()) -> bool:
    """Voxel DDA from ``origin`` to the centre of ``target_cell``.

    Every voxel crossed before the target must be air (``ignore`` cells are
    treated as air). The target itself may be anything.
    """

    tx, ty, tz = cell_centre(target_cell)
    ox, oy, oz = origin
    x, y, z = math.floor(ox), math.floor(oy), math.floor(oz)
    end = target_cell
    dx, dy, dz = tx - ox, ty - oy, tz - oz
    step_x = 1 if dx > 0 else -1
    step_y = 1 if dy > 0 else -1
    step_z = 1 if dz > 0 else -1

    def first(o: float, d: float, cell: int, step: int) -> float:
        if abs(d) < 1e-12:
            return math.inf
        boundary = cell + (1 if step > 0 else 0)
        return (boundary - o) / d

    t_max_x, t_max_y, t_max_z = first(ox, dx, x, step_x), first(oy, dy, y, step_y), first(oz, dz, z, step_z)
    t_dx = abs(1 / dx) if abs(dx) > 1e-12 else math.inf
    t_dy = abs(1 / dy) if abs(dy) > 1e-12 else math.inf
    t_dz = abs(1 / dz) if abs(dz) > 1e-12 else math.inf
    for _ in range(64):
        if (x, y, z) == end:
            return True
        if t_max_x <= t_max_y and t_max_x <= t_max_z:
            x += step_x
            t_max_x += t_dx
        elif t_max_y <= t_max_z:
            y += step_y
            t_max_y += t_dy
        else:
            z += step_z
            t_max_z += t_dz
        if (x, y, z) == end:
            return True
        if (x, y, z) not in ignore and solid(x, y, z):
            return False
    return False


def body_cells(node: Node) -> tuple[Cell, Cell, Cell]:
    x, y, s = node
    return ((x, y, s - 1), (x, y, s - 2), (x, y, s - 3))


def node_position(node: Node) -> Vector3:
    return (node[0] + .5, node[1] + .5, node[2] - EYE_ABOVE_SUPPORT)


def node_for_position(solid: SolidFn, position: Sequence[float]) -> Node | None:
    """Support node under a body position (eye/position z = support - 2.25)."""

    x, y = math.floor(position[0]), math.floor(position[1])
    expected = round(position[2] + EYE_ABOVE_SUPPORT)
    for s in (expected, expected + 1, expected - 1, expected + 2):
        if is_stand(solid, (x, y, s)):
            return (x, y, s)
    return None


def is_stand(solid: SolidFn, node: Node) -> bool:
    x, y, s = node
    return (solid(x, y, s) and not solid(x, y, s - 1)
            and not solid(x, y, s - 2) and not solid(x, y, s - 3))


@dataclass(frozen=True, slots=True)
class Region:
    """Bounded stand search volume around one site."""

    x0: int
    x1: int
    y0: int
    y1: int
    z_top: int     # smallest support z considered (highest point)
    z_bottom: int  # largest support z considered

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def border(self, x: int, y: int) -> bool:
        return x in (self.x0, self.x1) or y in (self.y0, self.y1)

    def supports(self, solid: SolidFn, x: int, y: int) -> list[int]:
        return [s for s in range(self.z_top, self.z_bottom + 1) if is_stand(solid, (x, y, s))]


def region_for(placement: Placement, margin: int = STAND_MARGIN) -> Region:
    x0, x1, y0, y1 = placement.bounds(margin)
    zs = [z for _x, _y, z in placement.cells] or [placement.ground_z - 1]
    return Region(max(1, x0), min(510, x1), max(1, y0), min(510, y1),
                  max(4, min(zs) - 1), min(MAX_BUILD_Z, placement.ground_z + 3))


def walk_distances(solid: SolidFn, region: Region, sources: Iterable[Node], *,
                   limit: int = 2048) -> dict[Node, int]:
    """BFS over body-clear supports: 4-way steps, up 1 (with jump headroom), down 2."""

    dist: dict[Node, int] = {}
    queue: deque[Node] = deque()
    for node in sources:
        if node not in dist and region.contains(node[0], node[1]) and is_stand(solid, node):
            dist[node] = 0
            queue.append(node)
    while queue and len(dist) < limit:
        node = queue.popleft()
        x, y, s = node
        d = dist[node] + 1
        for dx, dy in _CARDINAL:
            nx, ny = x + dx, y + dy
            if not region.contains(nx, ny):
                continue
            for ns in (s, s - 1, s + 1, s + 2):
                nxt = (nx, ny, ns)
                if nxt in dist or not is_stand(solid, nxt):
                    continue
                if ns < s and solid(x, y, s - 4):
                    continue  # no headroom to jump up from here
                if ns > s and any(solid(nx, ny, z) for z in range(s - 3, ns - 3)):
                    continue  # dropping down must clear the destination column
                dist[nxt] = d
                queue.append(nxt)
                break
    return dist


def walk_path(solid: SolidFn, region: Region, start: Node, goal: Node, *,
              limit: int = 2048) -> list[Node]:
    """Shortest stand-to-stand walk (same moves as :func:`walk_distances`).

    Returns the nodes after ``start`` up to ``goal`` (empty if unreachable).
    Used to lead a builder up a stair it built one tread at a time.
    """

    parents: dict[Node, Node | None] = {start: None}
    queue: deque[Node] = deque([start])
    while queue and len(parents) < limit:
        node = queue.popleft()
        if node == goal:
            break
        x, y, s = node
        for dx, dy in _CARDINAL:
            nx, ny = x + dx, y + dy
            if not region.contains(nx, ny):
                continue
            for ns in (s, s - 1, s + 1, s + 2):
                nxt = (nx, ny, ns)
                if nxt in parents or not is_stand(solid, nxt):
                    continue
                if ns < s and solid(x, y, s - 4):
                    continue
                if ns > s and any(solid(nx, ny, z) for z in range(s - 3, ns - 3)):
                    continue
                parents[nxt] = node
                queue.append(nxt)
                break
    if goal not in parents:
        return []
    path: list[Node] = []
    node: Node | None = goal
    while node is not None and node != start:
        path.append(node)
        node = parents[node]
    return path[::-1]


def boundary_sources(solid: SolidFn, region: Region) -> list[Node]:
    nodes: list[Node] = []
    for x in range(region.x0, region.x1 + 1):
        for y in (region.y0, region.y1):
            nodes.extend((x, y, s) for s in region.supports(solid, x, y))
    for y in range(region.y0 + 1, region.y1):
        for x in (region.x0, region.x1):
            nodes.extend((x, y, s) for s in region.supports(solid, x, y))
    return nodes


def escapes(solid: SolidFn, region: Region, node: Node) -> bool:
    """Can a body at ``node`` walk to the site boundary?"""

    if region.border(node[0], node[1]):
        return True
    dist = walk_distances(solid, region, (node,))
    return any(region.border(x, y) for x, y, _s in dist)


def _mix(*values: int) -> float:
    acc = 0x9E3779B9
    for value in values:
        acc = (acc ^ (int(value) & 0xFFFFFFFF)) * 0x01000193 & 0xFFFFFFFF
    return (acc % 1000) / 1000.0


def find_stand(solid: SolidFn, cells: Sequence[Cell], region: Region, *,
               sources: Iterable[Node] | None = None,
               reach: float = PLAN_REACH,
               avoid_cells: frozenset[Cell] | set[Cell] = frozenset(),
               avoid_columns: frozenset[tuple[int, int]] | set[tuple[int, int]] = frozenset(),
               allowed_columns: frozenset[tuple[int, int]] | None = None,
               prefer: tuple[float, float] | None = None,
               ground_z: int | None = None,
               require_escape: bool = True,
               seed: int = 0,
               distances: Mapping[Node, int] | None = None) -> tuple[Node, int] | None:
    """Choose a reachable stand for building ``cells`` as one step.

    Returns ``(node, walk_steps)``. ``sources`` are the builder's current
    node (or the site boundary when it is outside); ``avoid_cells`` are other
    builders' claimed cells and bodies; ``prefer`` biases toward a column
    (an occupant staying inside, helpers staying out of each other's way).
    """

    cells = tuple(cells)
    if not cells:
        return None
    line = frozenset(cells)
    if distances is None:
        distances = walk_distances(solid, region, sources if sources is not None
                                   else boundary_sources(solid, region))
    endpoints = (cells[0], cells[-1])
    centres = [cell_centre(cell) for cell in endpoints]
    ground = ground_z if ground_z is not None else max(cell[2] for cell in cells) + 1
    candidates: list[tuple[float, Node, int]] = []
    for node, walk in distances.items():
        if (node[0], node[1]) in avoid_columns:
            continue
        if allowed_columns is not None and (node[0], node[1]) not in allowed_columns:
            continue
        body = body_cells(node)
        if any(cell in line or cell in avoid_cells for cell in body):
            continue
        eye = node_position(node)
        far = max(math.dist(eye, centre) for centre in centres)
        near = min(math.dist(eye, centre) for centre in centres)
        if far > reach or near < 0.9:
            continue
        # Standing directly on top of the run (cells under the support) would
        # require looking straight down through the support itself.
        if any(cell[0] == node[0] and cell[1] == node[1] and cell[2] >= node[2] for cell in cells):
            continue
        if not all(ray_clear(solid, eye, cell, line) for cell in endpoints):
            continue
        hug = any(abs(cell[0] - node[0]) + abs(cell[1] - node[1]) == 1
                  and node[2] - 3 <= cell[2] <= node[2] - 1 for cell in cells)
        score = (walk * 1.0 + abs(node[2] - ground) * 0.6 + (1.5 if hug else 0.0)
                 + abs(far - 3.2) * 0.35 + _mix(seed, *node) * 0.4)
        if prefer is not None:
            score += math.hypot(node[0] + .5 - prefer[0], node[1] + .5 - prefer[1]) * 0.8
        candidates.append((score, node, walk))
    candidates.sort()
    if not require_escape:
        return (candidates[0][1], candidates[0][2]) if candidates else None
    after = VoxelView(solid, cells)
    for _score, node, walk in candidates[:12]:
        if escapes(after.solid, region, node):
            return node, walk
    return None


# --- decomposition --------------------------------------------------------------

def _runs(remaining: Mapping[Cell, str], solid: SolidFn, max_line: int,
          foundation: frozenset[Cell] = frozenset()) -> list[tuple[Cell, ...]]:
    """Straight same-colour runs through remaining cells starting at a supported cell.

    Foundation (terrain levelling) cells never share a run with structure
    cells, so the footing is laid before anything rises on it.
    """

    runs: set[tuple[Cell, ...]] = set()
    for axis in (0, 1, 2):
        groups: dict[tuple, list[Cell]] = {}
        for cell, token in remaining.items():
            key = tuple(cell[a] for a in range(3) if a != axis) + (token, cell in foundation)
            groups.setdefault(key, []).append(cell)
        for members in groups.values():
            members.sort(key=lambda c: c[axis])
            segment: list[Cell] = []
            segments: list[list[Cell]] = []
            for cell in members:
                if segment and cell[axis] != segment[-1][axis] + 1:
                    segments.append(segment)
                    segment = []
                segment.append(cell)
            if segment:
                segments.append(segment)
            for seg in segments:
                for k, cell in enumerate(seg):
                    if not supported(solid, cell):
                        continue
                    if axis == 2:
                        # Vertical drags go upward (toward smaller z) only.
                        run = tuple(reversed(seg[max(0, k - max_line + 1):k + 1]))
                        runs.add(run)
                        continue
                    runs.add(tuple(seg[k:k + max_line]))
                    runs.add(tuple(reversed(seg[max(0, k - max_line + 1):k + 1])))
    return list(runs)


def plan_build(solid: SolidFn, placement: Placement, *, max_line: int = MAX_LINE_CELLS,
               reach: float = PLAN_REACH, prefer_occupant: bool = False,
               seed: int = 0, require_stands: bool = True) -> BuildPlan:
    """Decompose ``placement`` into supported BlockLines/singles with stands.

    Greedy, deterministic and bounded: lower layers first, longer and
    horizontal runs preferred, short walks between stands. Cells that cannot
    be supported or reached are reported in ``unbuildable``.
    """

    if not 1 <= max_line <= SERVER_LINE_CELLS:
        raise ValueError("line length outside the server limit")
    region = region_for(placement)
    view = VoxelView(solid)
    remaining: dict[Cell, str] = {cell: token for cell, token in placement.cells.items()
                                  if not solid(*cell)}
    if len(remaining) > MAX_PLAN_CELLS:
        return BuildPlan(placement, (), tuple(sorted(remaining)), (region.x0, region.x1, region.y0, region.y1))
    ground = placement.ground_z
    steps: list[BuildStep] = []
    previous: Node | None = None
    walk_total = 0
    estimated = 0.0
    prefer = placement.occupant[:2] if prefer_occupant and placement.occupant else None
    clear = placement.clear
    while remaining:
        runs = _runs(remaining, view.solid, max_line, placement.foundation)
        if not runs:
            break
        low = min(ground - 1 - z for _x, _y, z in remaining)
        footing = any(cell in placement.foundation for cell in remaining)

        def score(run: tuple[Cell, ...]) -> float:
            horizontal = run[0][2] == run[-1][2]
            height = min(ground - 1 - c[2] for c in run)
            # Footing first (it is buried once a wall covers it), then the
            # lowest unfinished layer; longer and horizontal drags preferred.
            footing_penalty = 8.0 if footing and run[0] not in placement.foundation else 0.0
            return len(run) + (0.4 if horizontal else 0.0) - 2.0 * (height - low) - footing_penalty

        runs.sort(key=lambda run: (-score(run), run))
        chosen = None
        sources = None
        distances = None
        if require_stands:
            if previous is not None and is_stand(view.solid, previous):
                sources = [previous]
            else:
                sources = boundary_sources(view.solid, region)
            distances = walk_distances(view.solid, region, sources)
            if previous is not None and len(distances) <= 1:
                distances = walk_distances(view.solid, region, boundary_sources(view.solid, region))
        for run in runs:
            if any(cell in clear for cell in run):
                continue
            if not require_stands:
                chosen = (run, None, 0)
                break
            found = find_stand(view.solid, run, region, distances=distances, reach=reach,
                               ground_z=ground, prefer=prefer, seed=seed + len(steps))
            if found is not None:
                chosen = (run, found[0], found[1])
                break
        if chosen is None:
            break
        run, node, walk = chosen
        token = remaining[run[0]]
        steps.append(BuildStep(len(steps), run, token,
                               node_position(node) if node is not None else None,
                               foundation=all(cell in placement.foundation for cell in run)))
        for cell in run:
            remaining.pop(cell, None)
        view.added.update(run)
        walk_total += walk
        # Walk ~4 blocks/s, tool/aim settle, the drag itself and a breather.
        estimated += walk / 4.0 + 0.55 + 0.04 * len(run)
        previous = node
    cell_step = {cell: step.index for step in steps for cell in step.cells}
    return BuildPlan(placement, tuple(steps), tuple(sorted(remaining)),
                     (region.x0, region.x1, region.y0, region.y1), round(estimated, 2),
                     walk_total, cell_step)


def validate_plan(solid: SolidFn, plan: BuildPlan, *, max_line: int = MAX_LINE_CELLS,
                  reach: float = PLAN_REACH) -> list[str]:
    """Replay a plan against the server's build/support rules; return problems."""

    problems: list[str] = []
    view = VoxelView(solid)
    covered: dict[Cell, int] = {}
    region = region_for(plan.placement)
    for step in plan.steps:
        if not 1 <= len(step.cells) <= max_line:
            problems.append(f"step {step.index}: length {len(step.cells)}")
        axes = [a for a in range(3) if step.start[a] != step.end[a]]
        if len(axes) > 1:
            problems.append(f"step {step.index}: not a straight line")
        if len(set(step.cells)) != len(step.cells):
            problems.append(f"step {step.index}: repeated cell")
        for cell in step.cells:
            if cell in covered:
                problems.append(f"cell {cell} built twice (steps {covered[cell]}, {step.index})")
            covered[cell] = step.index
            if cell in plan.placement.clear:
                problems.append(f"cell {cell} is a keep-clear cell")
        if not line_valid(view.solid, step.cells):
            problems.append(f"step {step.index}: unsupported cell in line order")
        if step.stand is not None:
            node = node_for_position(view.solid, step.stand)
            if node is None:
                problems.append(f"step {step.index}: stand is not body-clear support")
            else:
                if any(cell in step.cells for cell in body_cells(node)):
                    problems.append(f"step {step.index}: line passes through the builder")
                eye = node_position(node)
                for cell in (step.start, step.end):
                    if math.dist(eye, cell_centre(cell)) > reach + 1e-6:
                        problems.append(f"step {step.index}: {cell} out of reach")
                    if not ray_clear(view.solid, eye, cell, frozenset(step.cells)):
                        problems.append(f"step {step.index}: {cell} not visible")
        view.added.update(step.cells)
        if step.stand is not None:
            node = node_for_position(view.solid, step.stand)
            if node is not None and not escapes(view.solid, region, node):
                problems.append(f"step {step.index}: builder walled in")
    missing = set(plan.placement.cells) - set(covered) - {c for c in plan.placement.cells if solid(*c)}
    if missing:
        problems.append(f"{len(missing)} planned cells never built")
    # Final structure must be face-connected to pre-existing terrain, so the
    # server's floating-chunk collapse cannot drop it when nothing is removed.
    built = set(covered)
    grounded: set[Cell] = set()
    frontier = deque(cell for cell in built
                     if any(solid(cell[0] + dx, cell[1] + dy, cell[2] + dz)
                            for dx, dy, dz in _FACES))
    grounded.update(frontier)
    while frontier:
        x, y, z = frontier.popleft()
        for dx, dy, dz in _FACES:
            nxt = (x + dx, y + dy, z + dz)
            if nxt in built and nxt not in grounded:
                grounded.add(nxt)
                frontier.append(nxt)
    if grounded != built:
        problems.append(f"{len(built - grounded)} cells float")
    return problems


def step_remaining(solid: SolidFn, step: BuildStep) -> tuple[Cell, ...]:
    return tuple(cell for cell in step.cells if not solid(*cell))


def step_ready(solid: SolidFn, step: BuildStep) -> bool:
    """Is the drag start supported now (or already present)?"""

    if not step_remaining(solid, step):
        return False
    return solid(*step.start) or supported(solid, step.start)


def placement_cells_left(solid: SolidFn, plan: BuildPlan) -> int:
    return sum(1 for step in plan.steps for cell in step.cells if not solid(*cell))

