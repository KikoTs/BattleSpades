"""Schematic data model: authored voxel layouts, rotation and terrain fitting.

A schematic is a small structure authored as ASCII layers (bottom first).
Each layer lists rows from FRONT (toward the threat) to BACK; columns run
left to right as seen from behind the structure looking at the threat.

Legend (per character):

``#``  primary block (``palette['#']``)
``+``  accent block (``palette['+']``)
``=``  secondary block (``palette['=']``)
``_``  keep clear: must be air when the site is chosen and is never built
       (doorways, firing slits, interior standing room, stair headroom)
``.``  or space: don't care

Local axes: ``u`` = right, ``f`` = forward (toward the threat), ``h`` =
layer index above the ground surface (0 rests on the terrain top). The
anchor ``(col, row)`` is the grid column the structure is centred on; for
shelters it is the cell the occupant stands in.

World axes follow the VXL: ``z`` grows DOWN, the terrain top solid voxel of
the anchor column is ``ground_z`` and layer ``h`` occupies ``ground_z - 1 - h``.

Everything here is pure, pickle-safe and free of I/O, so it can run in the
isolated worker at decision time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Mapping

Cell = tuple[int, int, int]
SolidFn = Callable[[int, int, int], bool]

# Named palette entries; ``team`` keeps the builder's current (team) colour.
PALETTE: dict[str, int | None] = {
    "team": None,
    "sand": 0xC8B27A,
    "concrete": 0x8C8C8C,
    "wood": 0x8A5A2E,
    "dark": 0x4A4A4A,
    "olive": 0x6B7A3A,
    "brick": 0x9A4A3A,
    "gold": 0xD4AF37,
    "stone": 0x77736B,
}

BUILD_CHARS = frozenset("#+=")
CLEAR_CHAR = "_"
# Forward unit vector per quarter turn; right = (-forward_y, forward_x).
FORWARD = ((1, 0), (0, 1), (-1, 0), (0, -1))
MIN_BUILD_Z = 1
MAX_BUILD_Z = 238
MAP_SIZE = 512


def right_of(rotation: int) -> tuple[int, int]:
    fx, fy = FORWARD[rotation % 4]
    return (-fy, fx)


def rotation_for_facing(dx: float, dy: float) -> int:
    """Quarter turn whose forward axis best matches a world direction."""

    if not (math.isfinite(dx) and math.isfinite(dy)) or abs(dx) + abs(dy) < 1e-9:
        return 0
    return max(range(4), key=lambda r: FORWARD[r][0] * dx + FORWARD[r][1] * dy)


@dataclass(frozen=True, slots=True)
class Schematic:
    """One authored structure; local cells are precomputed at construction.

    ``ground_mode``: ``"fill"`` levels uneven terrain by adding foundation
    cells under low columns (at most ``max_fill`` per column); ``"deck"``
    places cells at absolute layers relative to the anchor (bridges), with
    no per-column terrain requirement except the anchor bank itself.
    """

    name: str
    layers: tuple[tuple[str, ...], ...]
    anchor: tuple[int, int]
    palette: Mapping[str, str]
    tags: tuple[str, ...] = ()
    purpose: str = "cover"
    description: str = ""
    ground_mode: str = "fill"
    max_fill: int = 2
    base_level: int = 0
    # Where an occupant (VIP, sniper) stands, local (u, f, h-of-support).
    occupant: tuple[int, int, int] | None = None
    max_builders: int = 3
    cells: tuple[tuple[int, int, int, str], ...] = field(default=(), compare=False)
    clear: tuple[tuple[int, int, int], ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        cells: list[tuple[int, int, int, str]] = []
        clear: list[tuple[int, int, int]] = []
        col0, row0 = self.anchor
        for layer_index, rows in enumerate(self.layers):
            h = layer_index + self.base_level
            for row_index, row in enumerate(rows):
                f = row0 - row_index
                for col_index, char in enumerate(row):
                    u = col_index - col0
                    if char in BUILD_CHARS:
                        token = self.palette.get(char, "team")
                        if token not in PALETTE:
                            raise ValueError(f"{self.name}: unknown palette {token!r}")
                        cells.append((u, f, h, token))
                    elif char == CLEAR_CHAR:
                        clear.append((u, f, h))
                    elif char not in ". ":
                        raise ValueError(f"{self.name}: unknown legend {char!r}")
        if not cells:
            raise ValueError(f"{self.name}: empty schematic")
        if self.ground_mode not in {"fill", "deck"}:
            raise ValueError(f"{self.name}: unknown ground mode {self.ground_mode!r}")
        object.__setattr__(self, "cells", tuple(cells))
        object.__setattr__(self, "clear", tuple(clear))

    @property
    def block_count(self) -> int:
        return len(self.cells)

    @property
    def height(self) -> int:
        return max(h for _u, _f, h, _c in self.cells) + 1

    @property
    def footprint(self) -> frozenset[tuple[int, int]]:
        return frozenset((u, f) for u, f, _h, _c in self.cells) | frozenset(
            (u, f) for u, f, _h in self.clear)

    @property
    def radius(self) -> int:
        return max(max(abs(u), abs(f)) for u, f in self.footprint)


def local_to_world(anchor: tuple[int, int], rotation: int, u: int, f: int) -> tuple[int, int]:
    fx, fy = FORWARD[rotation % 4]
    rx, ry = right_of(rotation)
    return anchor[0] + u * rx + f * fx, anchor[1] + u * ry + f * fy


@dataclass(frozen=True, slots=True)
class Placement:
    """A schematic fitted to real terrain: exact world cells to add.

    ``cells`` maps every cell to build (already-solid cells are omitted) to
    its palette token. ``foundation`` lists the subset that levels terrain.
    ``clear`` must stay air. ``occupant`` is the body position (if any).
    """

    schematic: Schematic
    anchor: tuple[int, int, int]
    rotation: int
    cells: Mapping[Cell, str]
    foundation: frozenset[Cell]
    clear: frozenset[Cell]
    columns: frozenset[tuple[int, int]]
    occupant: tuple[float, float, float] | None = None

    @property
    def name(self) -> str:
        return self.schematic.name

    @property
    def forward(self) -> tuple[int, int]:
        return FORWARD[self.rotation % 4]

    @property
    def ground_z(self) -> int:
        return self.anchor[2]

    @property
    def cost(self) -> int:
        return len(self.cells)

    def bounds(self, margin: int = 0) -> tuple[int, int, int, int]:
        xs = [x for x, _y in self.columns]
        ys = [y for _x, y in self.columns]
        return min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin


def _column_top(solid: SolidFn, x: int, y: int, ground_z: int, span: int) -> int | None:
    """Topmost solid voxel within ``span`` layers of ``ground_z`` (z grows down)."""

    for z in range(ground_z - span, ground_z + span + 1):
        if solid(x, y, z):
            return z
    return None


def fit(schematic: Schematic, solid: SolidFn, anchor_xy: tuple[int, int],
        ground_z: int, rotation: int, *, allow_obstruction: int = 0) -> tuple[Placement | None, str]:
    """Fit ``schematic`` to the terrain around ``anchor_xy``.

    Returns ``(placement, "")`` or ``(None, reason)``. Rejects out-of-map
    cells, keep-clear cells that are solid, columns that rise more than one
    layer or drop more than ``max_fill`` below the anchor, and foreign solids
    occupying more than ``allow_obstruction`` planned cells above layer 0.
    """

    rotation %= 4
    cells: dict[Cell, str] = {}
    foundation: set[Cell] = set()
    clear: set[Cell] = set()
    columns: set[tuple[int, int]] = set()
    obstructed = 0
    tops: dict[tuple[int, int], int | None] = {}
    by_column: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for u, f, h, token in schematic.cells:
        by_column.setdefault((u, f), []).append((h, token))
    clear_by_column: dict[tuple[int, int], list[int]] = {}
    for u, f, h in schematic.clear:
        clear_by_column.setdefault((u, f), []).append(h)
    for (u, f) in set(by_column) | set(clear_by_column):
        x, y = local_to_world(anchor_xy, rotation, u, f)
        if not (1 <= x < MAP_SIZE - 1 and 1 <= y < MAP_SIZE - 1):
            return None, "out_of_map"
        columns.add((x, y))
        if schematic.ground_mode == "fill":
            top = _column_top(solid, x, y, ground_z, 3)
            tops[(u, f)] = top
            if top is None or top < ground_z - 1 or top > ground_z + schematic.max_fill:
                return None, "uneven_ground"
    for (u, f), entries in by_column.items():
        x, y = local_to_world(anchor_xy, rotation, u, f)
        top = tops.get((u, f))
        if schematic.ground_mode == "fill" and top is not None and top > ground_z:
            lowest = min(h for h, _t in entries)
            if lowest == 0:
                token = min(entries)[1]
                for z in range(ground_z, top):
                    cell = (x, y, z)
                    cells[cell] = token
                    foundation.add(cell)
        for h, token in entries:
            z = ground_z - 1 - h
            if not MIN_BUILD_Z <= z <= MAX_BUILD_Z:
                return None, "out_of_build_band"
            cell = (x, y, z)
            if solid(x, y, z):
                if h >= 1 or schematic.ground_mode == "deck":
                    obstructed += 1
                continue
            cells[cell] = token
    for (u, f), heights in clear_by_column.items():
        x, y = local_to_world(anchor_xy, rotation, u, f)
        for h in heights:
            cell = (x, y, ground_z - 1 - h)
            if solid(*cell):
                return None, "clearance_blocked"
            clear.add(cell)
    if obstructed > allow_obstruction:
        return None, "obstructed"
    if not cells:
        return None, "already_built"
    if not clear.isdisjoint(cells):
        return None, "clearance_conflict"
    occupant = None
    if schematic.occupant is not None:
        u, f, h = schematic.occupant
        x, y = local_to_world(anchor_xy, rotation, u, f)
        occupant = (x + .5, y + .5, ground_z - h - 2.25)
    return Placement(schematic, (anchor_xy[0], anchor_xy[1], ground_z), rotation,
                     cells, frozenset(foundation), frozenset(clear), frozenset(columns),
                     occupant), ""


def structure_placement(name: str, cells: Mapping[Cell, str] | tuple[Cell, ...], *,
                        anchor: Cell | None = None, purpose: str = "traversal",
                        tags: tuple[str, ...] = ("custom",)) -> Placement:
    """Wrap arbitrary world cells (e.g. a zombie stair) as a placement.

    No terrain fitting is applied; the planner's support checks still decide
    whether and in which order the cells can be built.
    """

    mapping = dict(cells) if isinstance(cells, Mapping) else {cell: "team" for cell in cells}
    if not mapping:
        raise ValueError("empty structure")
    first = min(mapping, key=lambda c: (-c[2], c))
    anchor = anchor or (first[0], first[1], first[2] + 1)
    local = tuple((x - anchor[0], y - anchor[1], anchor[2] - 1 - z, token)
                  for (x, y, z), token in mapping.items())
    schematic = _CustomSchematic(name, local, purpose, tags)
    return Placement(schematic, anchor, 0, mapping, frozenset(), frozenset(),
                     frozenset((x, y) for x, y, _z in mapping))


def _CustomSchematic(name: str, local, purpose: str, tags) -> Schematic:
    # A one-layer stand-in carrying metadata; cells are replaced below.
    schematic = Schematic(name, (("#",),), (0, 0), {"#": "team"}, tags=tuple(tags),
                          purpose=purpose, ground_mode="deck")
    object.__setattr__(schematic, "cells", tuple(local))
    return schematic


def palette_rgb(token: str) -> int | None:
    return PALETTE.get(token)
