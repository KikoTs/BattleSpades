"""Find the cheapest set of voxels whose removal drops an elevated survivor.

Zombie survivors who pillar up, build a sky platform or a tower are out of
claw reach. Retail terrain has one rule that beats every such base: a solid
component that no longer connects to the indestructible base plane falls
(``WorldManager.find_unsupported_chunks``: 18-neighbour face+edge flood, a
component is grounded once it reaches z > 238). This module models exactly
that rule as a graph problem:

* the survivor's support voxels are the source,
* the base plane (and, conservatively, the edge of a bounded exploration and
  deep terrain under the horde's feet) is the sink,
* every other solid voxel is a vertex with a dig cost (cheap when a zombie
  standing on the ground can reach it, expensive when it hangs in the air).

A minimum vertex cut separates the survivor from the ground: once the horde
has dug exactly those voxels the server's own collapse rule drops the
survivor's part of the structure. Treating extra cells as "ground" only ever
makes a plan more expensive, never invalid, because every real grounding path
leaves the explored region through them.

Pure functions over ``solid(x, y, z) -> bool`` (z grows downward, the base
plane is z >= 239). The planner is a generator that yields after a bounded
amount of work so the gameplay thread can advance it under a per-tick budget,
exactly like the refuge election.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from typing import Callable, Generator, Iterable, Sequence

Cell = tuple[int, int, int]
Vector3 = tuple[float, float, float]
SolidFn = Callable[[int, int, int], bool]

MAP_SIZE = 512
# ``WorldManager.find_unsupported_chunks`` treats z > 238 as grounded.
GROUND_Z = 238
PLAYER_SUPPORT_OFFSET = 2.25
# Same 18-neighbour adjacency as WorldManager.COLLAPSE_NEIGHBORS.
NEIGHBORS_18: tuple[Cell, ...] = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1)
    if 1 <= abs(dx) + abs(dy) + abs(dz) <= 2
)
_FACES: tuple[Cell, ...] = (
    (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
)
_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
# A zombie standing on a floor reaches cells from its feet up to ~3 blocks
# above them with a claw swing (the hand's 3x3x3 cube adds one more).
CLAW_REACH = 3
# Dig cost of a voxel nobody can reach from a floor (it hangs in the air).
UNREACHABLE_COST = 25
_INF = 1 << 30
_WORK_PER_YIELD = 24


@dataclass(frozen=True, slots=True)
class CollapsePlan:
    """The voxels to dig and where to swing at them."""

    support: tuple[Cell, ...]
    cut: tuple[Cell, ...]
    # Aim cells for the zombie hand: one claw cube (3x3x3) around each site
    # covers every cut voxel. One digger per site.
    sites: tuple[Cell, ...]
    cost: int
    # Number of voxels that drop with the survivor (bounded by the search).
    falling: int
    explored: int


@dataclass(frozen=True, slots=True)
class Isolation:
    """Result of the walk-flood isolation test."""

    isolated: bool
    standing_cells: int
    support_z: int


def support_cells(solid: SolidFn, position: Vector3) -> tuple[Cell, ...]:
    """Solid voxels directly under a player's feet (body radius 0.45)."""

    x, y, z = (float(value) for value in position)
    expected = int(round(z + PLAYER_SUPPORT_OFFSET))
    columns = sorted({
        (int(math.floor(x + ox)), int(math.floor(y + oy)))
        for ox in (-0.45, 0.45) for oy in (-0.45, 0.45)
    })
    # Crouching lowers the eye; airborne bodies float above the floor.
    for dz in (0, 1, -1, 2, 3):
        level = expected + dz
        if not 0 <= level <= GROUND_Z + 1:
            continue
        cells = tuple(
            (cx, cy, level) for cx, cy in columns
            if 0 <= cx < MAP_SIZE and 0 <= cy < MAP_SIZE and solid(cx, cy, level)
        )
        if cells:
            return cells
    return ()


def _standable(solid: SolidFn, x: int, y: int, floor_z: int) -> bool:
    return (
        0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE
        and solid(x, y, floor_z)
        and not solid(x, y, floor_z - 1)
        and not solid(x, y, floor_z - 2)
    )


def iter_isolation(
    solid: SolidFn,
    position: Vector3,
    hunters: Sequence[Vector3] = (),
    *,
    limit: int = 400,
    hunter_radius: float = 1.5,
) -> Generator[None, None, Isolation]:
    """Can a walker reach the player's floor from the open map?

    Floods the standing cells a player can walk between (one-block steps up
    or down, crouch headroom) from the player's floor. A flood that outgrows
    ``limit`` cells, or that reaches a hunter's floor, is open ground. A
    small closed flood is a pillar top, a sky platform or a tower: walking
    cannot get there, so the horde must collapse it or climb.
    """

    support = support_cells(solid, position)
    if not support:
        return Isolation(False, 0, int(round(float(position[2]) + PLAYER_SUPPORT_OFFSET)))
    floor_z = support[0][2]
    hunter_cells = {
        (int(math.floor(h[0])), int(math.floor(h[1])),
         int(round(float(h[2]) + PLAYER_SUPPORT_OFFSET)))
        for h in hunters
    }
    start = (support[0][0], support[0][1], floor_z)
    seen = {start}
    queue = deque([start])
    work = 0
    while queue:
        x, y, z = queue.popleft()
        if (x, y, z) in hunter_cells or any(
            abs(hx - x) <= hunter_radius and abs(hy - y) <= hunter_radius
            and abs(hz - z) <= 1
            for hx, hy, hz in hunter_cells
        ):
            return Isolation(False, len(seen), floor_z)
        for dx, dy in _CARDINAL:
            nx, ny = x + dx, y + dy
            for nz in (z, z - 1, z + 1):
                key = (nx, ny, nz)
                if key in seen:
                    continue
                if nz == z - 1 and solid(x, y, z - 3):
                    continue  # no headroom to step up
                if _standable(solid, nx, ny, nz):
                    seen.add(key)
                    queue.append(key)
                    break
        work += 1
        if len(seen) > limit:
            return Isolation(False, len(seen), floor_z)
        if work % _WORK_PER_YIELD == 0:
            yield
    return Isolation(True, len(seen), floor_z)


def isolation(solid: SolidFn, position: Vector3, hunters: Sequence[Vector3] = (),
              **kwargs) -> Isolation:
    return _run(iter_isolation(solid, position, hunters, **kwargs))


def _floor_below(solid: SolidFn, x: int, y: int, z: int, depth: int) -> int | None:
    for level in range(z, min(z + depth, GROUND_Z + 1) + 1):
        if solid(x, y, level):
            return level
    return None


def dig_cost(solid: SolidFn, cell: Cell, ground_floor_z: int | None = None) -> int:
    """Swing cost of ``cell`` for a zombie standing on the horde's floor.

    A cell is reachable when one of its face neighbours is open and a floor
    lies at most ``CLAW_REACH`` blocks below that opening (the zombie stands
    there). Floors more than two blocks above ``ground_floor_z`` belong to
    the structure itself (a platform top, the survivor's pillar) and do not
    count: the horde cannot stand there yet. Low cells cost 1, cells two or
    three blocks up cost 2, anything else ``UNREACHABLE_COST``.
    """

    x, y, z = cell
    if z > GROUND_Z:
        return _INF
    best = UNREACHABLE_COST
    for dx, dy, dz in _FACES:
        ax, ay, az = x + dx, y + dy, z + dz
        if not (0 <= ax < MAP_SIZE and 0 <= ay < MAP_SIZE) or az < 0:
            continue
        if solid(ax, ay, az):
            continue
        floor = _floor_below(solid, ax, ay, az, CLAW_REACH + 1)
        if floor is None:
            continue
        if ground_floor_z is not None and floor < int(ground_floor_z) - 2:
            continue
        height = floor - z
        if height > CLAW_REACH:
            continue
        best = min(best, 1 if height <= 1 else 2)
        if best == 1:
            break
    if best == UNREACHABLE_COST and ground_floor_z is not None:
        # Buried but low (the core of a thick leg or tower base): the claw's
        # 3x3x3 cube opens it up while clearing the outer voxels.
        if -1 <= int(ground_floor_z) - z <= 2:
            best = 2
    return best


def horde_floor(solid: SolidFn, position: Vector3, hunters: Sequence[Vector3] = (),
                *, radius: float = 32.0) -> int:
    """The floor z the horde stands on around ``position``.

    Median floor of hunters within ``radius`` when there are any, otherwise
    the lower quartile of the terrain floors on rings 6 and 10 blocks out
    (first solid voxel below the player's own floor level).
    """

    px, py = float(position[0]), float(position[1])
    floors = sorted(
        int(round(float(h[2]) + PLAYER_SUPPORT_OFFSET)) for h in hunters
        if math.hypot(float(h[0]) - px, float(h[1]) - py) <= radius
    )
    if floors:
        return floors[len(floors) // 2]
    start = int(round(float(position[2]) + PLAYER_SUPPORT_OFFSET))
    samples = []
    for ring in (6, 10):
        for step in range(8):
            angle = step * math.pi / 4.0
            x = int(math.floor(px + ring * math.cos(angle)))
            y = int(math.floor(py + ring * math.sin(angle)))
            if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
                continue
            floor = _floor_below(solid, x, y, start, GROUND_Z + 1 - start)
            samples.append(GROUND_Z + 1 if floor is None else floor)
    if not samples:
        return start
    samples.sort()
    return samples[(3 * len(samples)) // 4]


def iter_plan_collapse(
    solid: SolidFn,
    support: Iterable[Cell],
    *,
    ground_floor_z: int | None = None,
    node_limit: int = 2500,
    max_cost: int = 48,
    cost_fn: Callable[[Cell], int] | None = None,
) -> Generator[None, None, CollapsePlan | None]:
    """Minimum-cost vertex cut between ``support`` and the ground.

    ``ground_floor_z`` is the floor the horde stands on: cells three or more
    blocks below it are deep terrain and count as ground (conservative).
    Returns ``None`` when no cut costs at most ``max_cost`` (a hill, a thick
    fortress) or when the support is already grounded through bedrock.
    """

    sources = tuple(dict.fromkeys(
        (int(c[0]), int(c[1]), int(c[2])) for c in support
    ))
    if not sources:
        return None
    cost_of = cost_fn if cost_fn is not None else (
        lambda cell: dig_cost(solid, cell, ground_floor_z))
    deep_z = (int(ground_floor_z) + 3) if ground_floor_z is not None else GROUND_Z + 1

    def is_ground(cell: Cell) -> bool:
        return cell[2] > GROUND_Z or cell[2] >= deep_z

    # 1. Bounded breadth-first exploration of the solid component.
    index: dict[Cell, int] = {}
    cells: list[Cell] = []
    adjacency: list[list[int]] = []
    to_sink: list[bool] = []
    expanded: list[bool] = []
    terminal: set[Cell] = set()
    for cell in sources:
        if is_ground(cell) or not solid(*cell):
            continue
        index[cell] = len(cells)
        cells.append(cell)
    if not cells:
        return None
    head = 0
    work = 0
    while head < len(cells):
        cell = cells[head]
        neighbours: list[int] = []
        sink = False
        if len(cells) >= node_limit:
            # Unexpanded frontier: treat as ground (conservative).
            sink = True
            expanded.append(False)
        else:
            expanded.append(True)
            x, y, z = cell
            for dx, dy, dz in NEIGHBORS_18:
                nxt = (x + dx, y + dy, z + dz)
                nx, ny, nz = nxt
                if not (0 <= nx < MAP_SIZE and 0 <= ny < MAP_SIZE and 0 <= nz):
                    continue
                known = index.get(nxt)
                if known is not None:
                    neighbours.append(known)
                    continue
                if nxt in terminal:
                    sink = True
                    continue
                if not solid(nx, ny, nz):
                    continue
                if is_ground(nxt):
                    terminal.add(nxt)
                    sink = True
                    continue
                neighbours.append(len(cells))
                index[nxt] = len(cells)
                cells.append(nxt)
            work += 1
        adjacency.append(neighbours)
        to_sink.append(sink)
        head += 1
        if work % _WORK_PER_YIELD == 0:
            yield
    count = len(cells)
    if not any(to_sink):
        # Nothing reaches the ground inside the bound: the server already
        # considers this floating (or the bound is tiny). Nothing to cut.
        return None
    capacity = [0] * count
    for i, cell in enumerate(cells):
        capacity[i] = max(1, int(cost_of(cell)))
        if i % _WORK_PER_YIELD == 0:
            yield
    source_set = {index[c] for c in sources if c in index}
    # The survivor's own floor never counts as a cut: digging one voxel out
    # from under a player leaves the rest of the platform holding him.
    for i in source_set:
        capacity[i] = _INF
    # Expanded nodes already list each other both ways (a later expansion
    # finds the earlier node indexed). Only unexpanded frontier nodes lack
    # their back edges; add those.
    for i in range(count):
        if expanded[i]:
            for j in adjacency[i]:
                if not expanded[j]:
                    adjacency[j].append(i)
        if i % (_WORK_PER_YIELD * 4) == 0:
            yield

    # 2. Max-flow on the vertex-split graph (node 2i = in, 2i+1 = out).
    # Residual capacities: vertex edges in a list, adjacency edges are
    # infinite forward, so only their reverse flow needs tracking.
    vertex_residual = list(capacity)          # in_i -> out_i
    vertex_back = [0] * count                 # out_i -> in_i (flow on vertex)
    edge_flow: dict[tuple[int, int], int] = {}  # out_i -> in_j flow
    sink_flow = [0] * count                   # out_i -> SINK flow
    total = 0
    SOURCE, SINK = -1, -2
    while True:
        # BFS over states: ("in", i) encoded 2i, ("out", i) encoded 2i+1.
        parent: dict[int, int] = {}
        queue: deque[int] = deque()
        for i in source_set:
            parent[2 * i] = SOURCE
            queue.append(2 * i)
        found = -1
        steps = 0
        while queue and found < 0:
            node = queue.popleft()
            i, side = divmod(node, 2)
            if side == 0:
                # in_i -> out_i if vertex capacity remains.
                if vertex_residual[i] > 0 and (2 * i + 1) not in parent:
                    parent[2 * i + 1] = node
                    queue.append(2 * i + 1)
                # in_i -> out_j (reverse of out_j -> in_i flow).
                for j in adjacency[i]:
                    if edge_flow.get((j, i), 0) > 0 and (2 * j + 1) not in parent:
                        parent[2 * j + 1] = node
                        queue.append(2 * j + 1)
            else:
                if to_sink[i]:
                    found = node
                    break
                # out_i -> in_i (undo vertex flow).
                if vertex_back[i] > 0 and (2 * i) not in parent:
                    parent[2 * i] = node
                    queue.append(2 * i)
                for j in adjacency[i]:
                    if (2 * j) not in parent:
                        parent[2 * j] = node
                        queue.append(2 * j)
            steps += 1
            if steps % (_WORK_PER_YIELD * 8) == 0:
                yield
        if found < 0:
            break
        # Bottleneck along the path.
        bottleneck = _INF
        node = found
        while parent[node] != SOURCE:
            prev = parent[node]
            pi, ps = divmod(prev, 2)
            ni, ns = divmod(node, 2)
            if ps == 0 and ns == 1 and pi == ni:
                bottleneck = min(bottleneck, vertex_residual[pi])
            elif ps == 1 and ns == 0 and pi == ni:
                bottleneck = min(bottleneck, vertex_back[pi])
            elif ps == 0 and ns == 1:
                bottleneck = min(bottleneck, edge_flow[(ni, pi)])
            node = prev
        bottleneck = min(bottleneck, max_cost + 1 - total)
        node = found
        sink_flow[found // 2] += bottleneck
        while parent[node] != SOURCE:
            prev = parent[node]
            pi, ps = divmod(prev, 2)
            ni, ns = divmod(node, 2)
            if ps == 0 and ns == 1 and pi == ni:
                vertex_residual[pi] -= bottleneck
                vertex_back[pi] += bottleneck
            elif ps == 1 and ns == 0 and pi == ni:
                vertex_back[pi] -= bottleneck
                vertex_residual[pi] += bottleneck
            elif ps == 0 and ns == 1:
                edge_flow[(ni, pi)] -= bottleneck
            else:
                edge_flow[(pi, ni)] = edge_flow.get((pi, ni), 0) + bottleneck
            node = prev
        total += bottleneck
        if total > max_cost:
            return None
        yield

    # 3. Min cut = vertices whose "in" side is reachable but "out" is not.
    reachable = set(parent)
    cut_idx = [
        i for i in range(count)
        if (2 * i) in reachable and (2 * i + 1) not in reachable
    ]
    if not cut_idx:
        return None
    cut = tuple(sorted(cells[i] for i in cut_idx))
    cost = sum(capacity[i] for i in cut_idx)
    falling = sum(1 for i in range(count) if (2 * i) in reachable) - len(cut_idx)
    return CollapsePlan(
        support=sources,
        cut=cut,
        sites=claw_sites(cut, solid),
        cost=int(cost),
        falling=max(0, falling),
        explored=count,
    )


def plan_collapse(solid: SolidFn, support: Iterable[Cell], **kwargs) -> CollapsePlan | None:
    return _run(iter_plan_collapse(solid, support, **kwargs))


def _chebyshev(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))


def claw_sites(cut: Sequence[Cell], solid: SolidFn | None = None) -> tuple[Cell, ...]:
    """Greedy cover of the cut by zombie-hand cubes (3x3x3 around the hit).

    A claw damages the 3x3x3 cube around the voxel its swing actually hits,
    so sites are exposed cut voxels (an open face) when ``solid`` is given.
    Buried cut voxels that no exposed site covers come last: the first
    swings open them up. Ties prefer the lowest site (nearest the horde).
    """

    remaining = set(cut)

    def exposed(cell: Cell) -> bool:
        if solid is None:
            return True
        x, y, z = cell
        return any(not solid(x + dx, y + dy, z + dz) for dx, dy, dz in _FACES)

    sites: list[Cell] = []
    candidates = sorted(c for c in remaining if exposed(c))
    while remaining:
        pool = [c for c in candidates
                if any(_chebyshev(c, o) <= 1 for o in remaining)] or sorted(remaining)
        best = max(
            pool,
            key=lambda c: (sum(1 for o in remaining if _chebyshev(c, o) <= 1), c[2]),
        )
        sites.append(best)
        remaining = {o for o in remaining if _chebyshev(o, best) > 1}
    return tuple(sites)


def falls_after(solid: SolidFn, removed: Iterable[Cell], support: Iterable[Cell],
                *, limit: int = 20000) -> bool:
    """Reference check: does ``support`` lose its ground once ``removed`` is gone?

    Same rule as the server flood (18 neighbours, grounded at z > 238).
    """

    gone = set(removed)

    def present(x: int, y: int, z: int) -> bool:
        return (x, y, z) not in gone and solid(x, y, z)

    starts = [c for c in support if present(*c)]
    if not starts:
        return True
    seen = set(starts)
    stack = list(starts)
    while stack:
        x, y, z = stack.pop()
        if z > GROUND_Z:
            return False
        if len(seen) > limit:
            return False
        for dx, dy, dz in NEIGHBORS_18:
            nxt = (x + dx, y + dy, z + dz)
            if nxt not in seen and 0 <= nxt[2] and present(*nxt):
                seen.add(nxt)
                stack.append(nxt)
    return True


def _run(generator):
    while True:
        try:
            next(generator)
        except StopIteration as done:
            return done.value


__all__ = [
    "CollapsePlan",
    "Isolation",
    "claw_sites",
    "dig_cost",
    "falls_after",
    "horde_floor",
    "isolation",
    "iter_isolation",
    "iter_plan_collapse",
    "plan_collapse",
    "support_cells",
]
