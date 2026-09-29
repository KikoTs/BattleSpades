"""Small, conservative project candidates over the worker's current geometry.

Callers supply an observed lane/public objective, never an enemy registry.
These helpers do no disk I/O, A*, mutation, or global map search. They reject
uncertain terrain; the ordinary action gateway remains the final authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import math
from typing import Mapping, Sequence

import shared.constants as C
from server.dig_profiles import best_navigation_dig_profile, melee_dig_positions

from .messages import PlayerSnapshot, Vector3
from .prefab_policy import Cell, PrefabGeometry
from .simple_navigation import PLAYER_SUPPORT_OFFSET, SimpleVoxelWorld, SurfaceNode

MAX_SITE_CANDIDATES = 12
MAX_PREFAB_ATTEMPTS = 12
MAX_WALK_CELLS = 16
MAX_FRIENDLIES = 32
MAX_PROJECT_BUILD_REACH = 6.0
_CARDINAL = ((1, 0), (-1, 0), (0, 1), (0, -1))
_ADJACENT = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


@dataclass(frozen=True, slots=True)
class ProjectSite:
    """A costed proposal; yaw is radians, suitable for BotAction directly.

    ``approach`` is a body position; ``position`` is the actual action anchor.
    Sniper sites use a body position for both. ``cells`` contains the complete
    planned footprint; ``required_blocks`` counts new cells, while the prefab
    authority additionally requires stock >= ``authored_blocks``. Exits and
    landing are body positions, not block coordinates.
    """

    kind: str
    position: Vector3
    approach: Vector3
    exits: tuple[Vector3, ...]
    prefab_name: str = ""
    yaw: float = 0.0
    cells: tuple[Cell, ...] = ()
    required_blocks: int = 0
    authored_blocks: int = 0
    support_cells: tuple[Cell, ...] = ()
    landing: Vector3 | None = None
    tool_id: int = -1
    estimated_seconds: float = 0.0


def _direction(origin: Vector3, target: Vector3) -> tuple[float, float] | None:
    if not all(math.isfinite(value) for point in (origin, target) for value in point):
        return None
    dx, dy = target[0] - origin[0], target[1] - origin[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-6 else None


def _eligible(player: PlayerSnapshot) -> bool:
    return (player.alive and player.spawned and player.grounded and not player.wade
            and all(math.isfinite(value) for point in (player.position, player.eye) for value in point))


def _surface(world: SimpleVoxelWorld, position: Vector3) -> SurfaceNode | None:
    return world.surface(math.floor(position[0]), math.floor(position[1]), position[2],
                         vertical_span=1, clearance=3)


def _walk_clear(world: SimpleVoxelWorld, start: Vector3, end: Vector3,
                added: frozenset[Cell] = frozenset()) -> bool:
    """Two short Manhattan walks; no jumps, falling, water, or route search."""
    source, target = _surface(world, start), _surface(world, end)
    if source is None or target is None:
        return False
    distance = abs(source.x - target.x) + abs(source.y - target.y)
    if distance > MAX_WALK_CELLS:
        return False
    for order in ((0, 1), (1, 0)):
        cursor = source
        valid = True
        for axis in order:
            while (cursor.x, cursor.y)[axis] != (target.x, target.y)[axis]:
                delta = 1 if (target.x, target.y)[axis] > (cursor.x, cursor.y)[axis] else -1
                x, y = cursor.x + (delta if axis == 0 else 0), cursor.y + (delta if axis == 1 else 0)
                sample = world.surface(x, y, cursor.position[2], vertical_span=0, clearance=3)
                if sample is None or any((x, y, sample.support_z - dz) in added for dz in (1, 2, 3)):
                    valid = False
                    break
                cursor = sample
            if not valid:
                break
        if valid and cursor.support_z == target.support_z:
            return not any((source.x, source.y, source.support_z - dz) in added for dz in (1, 2, 3))
    return False


def _exits(world: SimpleVoxelWorld, position: Vector3,
           added: frozenset[Cell] = frozenset()) -> tuple[Vector3, ...]:
    exits = []
    for dx, dy in _CARDINAL:
        end = (position[0] + 3 * dx, position[1] + 3 * dy, position[2])
        if _walk_clear(world, position, end, added):
            exits.append(end)
    return tuple(exits)


def _body_overlap(cells: frozenset[Cell], positions: Sequence[Vector3]) -> bool:
    # Mirror the prefab authority's conservative 3x3x3 body exclusion.
    for position in islice(positions, MAX_FRIENDLIES + 1):
        px, py, pz = (math.floor(value) for value in position)
        if any((px + dx, py + dy, pz + dz) in cells
               for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (0, 1, 2)):
            return True
    return False


def _ray_clear(world: SimpleVoxelWorld, origin: Vector3, target: Vector3,
               added: frozenset[Cell] = frozenset()) -> bool:
    distance = math.dist(origin, target)
    if distance > 96.0 or not world.has_line_of_sight(origin, target):
        return False
    steps = max(1, math.ceil(distance * 4))
    return all(tuple(math.floor(origin[a] + (target[a] - origin[a]) * n / steps)
                     for a in range(3)) not in added for n in range(1, steps))


def find_sniper_outpost(world: SimpleVoxelWorld, player: PlayerSnapshot,
                       known_lane: Vector3, *,
                       friendly_positions: Sequence[Vector3] = (),
                       reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """Find a nearby dry firing point with a known lane and two usable exits."""
    heading = _direction(player.position, known_lane)
    if (len(friendly_positions) > MAX_FRIENDLIES or heading is None or not _eligible(player)
            or not {int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)}.intersection(player.loadout)
            or not 12 <= math.dist(player.eye, known_lane) <= 96):
        return None
    dx, dy = heading
    sites = []
    offsets = ((0, 0), (0, 3), (0, -3), (3, 0), (-3, 0),
               (3, 3), (3, -3), (-3, 3), (-3, -3), (0, 6), (0, -6), (-6, 0))
    for forward, side in offsets[:MAX_SITE_CANDIDATES]:
        point = (player.position[0] + forward * dx - side * dy,
                 player.position[1] + forward * dy + side * dx, player.position[2])
        node = _surface(world, point)
        if node is None or not _walk_clear(world, player.position, node.position):
            continue
        if any((node.x, node.y, node.support_z - dz) in reserved_cells for dz in (0, 1, 2, 3)):
            continue
        if any(math.dist(node.position, friend) < 2.0 for friend in islice(friendly_positions, MAX_FRIENDLIES)):
            continue
        eye = tuple(node.position[a] + player.eye[a] - player.position[a] for a in range(3))
        exits = _exits(world, node.position)
        if len(exits) < 2 or not _ray_clear(world, eye, known_lane):
            continue
        # Existing side/rear terrain is useful only while the actual firing ray
        # and exits stay open; raw elevation is never the objective.
        cover = sum(world.solid(node.x + ox * 2, node.y + oy * 2, node.support_z - 1)
                    for ox, oy in _CARDINAL)
        score = cover * 3.0 + len(exits) * .25 - math.dist(node.position, player.position) * .1
        sites.append((score, ProjectSite("outpost", node.position, node.position, exits)))
    return max(sites, key=lambda item: item[0])[1] if sites else None


def find_prefab_cover(world: SimpleVoxelWorld, player: PlayerSnapshot,
                      known_lane: Vector3,
                      geometries: Mapping[str, PrefabGeometry] | None = None, *,
                      rear: bool = False,
                      friendly_positions: Sequence[Vector3] = (),
                      reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """Place affordable equipped geometry without blocking body, lane or exits.

    Match the current gateway's top-surface snapping exactly. Covered/cave
    anchors are deliberately rejected until that gateway supports exact anchors.
    """
    heading = _direction(player.position, known_lane)
    if (len(friendly_positions) > MAX_FRIENDLIES or heading is None
            or not _eligible(player) or int(C.PREFAB_TOOL) not in player.loadout):
        return None
    geometry_map = world.prefab_geometry if geometries is None else geometries
    dx, dy = heading
    attempts = 0
    choices = []
    for name in islice(player.prefabs, 3):
        geometry = geometry_map.get(name.lower())
        if geometry is None or not 0 < geometry.block_count <= min(128, player.blocks):
            continue
        # Try the two perpendicular authored orientations, then the other two;
        # actual footprint dimensions decide suitability, not a name substring.
        for quarter in range(4):
            minimum, maximum = geometry.bounds_by_yaw[quarter]
            if maximum[2] - minimum[2] < 1 or max(maximum[a] - minimum[a] for a in (0, 1)) > 8:
                continue
            for sign in (-1, 1):
                attempts += 1
                if attempts > MAX_PREFAB_ATTEMPTS:
                    return min(choices, key=lambda item: item.required_blocks) if choices else None
                forward, side = (-4.0, sign * 1.0) if rear else (2.0, sign * 4.0)
                center_x = player.position[0] + forward * dx - side * dy
                center_y = player.position[1] + forward * dy + side * dx
                x = round(center_x - (minimum[0] + maximum[0]) / 2)
                y = round(center_y - (minimum[1] + maximum[1]) / 2)
                node = world.surface(x, y, player.position[2], vertical_span=1, clearance=3)
                if node is None or world._vxl.surface_z(x, y) != node.support_z:
                    continue
                anchor = (x, y, node.support_z - maximum[2] - 1)
                if math.dist(player.eye, anchor) > MAX_PROJECT_BUILD_REACH:
                    continue
                cells = tuple((ox + x, oy + y, oz + anchor[2])
                              for ox, oy, oz in geometry.cells_by_yaw[quarter])
                occupied = frozenset(cells)
                if any(not (0 <= cx < 512 and 0 <= cy < 512 and 1 <= cz <= 238)
                       for cx, cy, cz in cells) or not occupied.isdisjoint(reserved_cells):
                    continue
                if _body_overlap(occupied, (player.position, *tuple(islice(friendly_positions, MAX_FRIENDLIES)))):
                    continue
                new_cells = tuple(cell for cell in cells if not world.solid(*cell))
                if not new_cells or not _ray_clear(world, player.eye, known_lane, occupied):
                    continue
                # The authored voxels must actually stop a chest-height ray
                # across this side/rear approach. A decorative or hollow model
                # whose bounding box merely looks like cover is not enough.
                chest = (player.position[0], player.position[1], player.position[2] + 1.0)
                across = (2 * center_x - chest[0], 2 * center_y - chest[1], chest[2])
                if not _ray_clear(world, chest, across) or _ray_clear(world, chest, across, occupied):
                    continue
                supports = tuple(sorted({(cx + ox, cy + oy, cz + oz)
                                         for cx, cy, cz in cells for ox, oy, oz in _ADJACENT
                                         if 0 <= cx + ox < 512 and 0 <= cy + oy < 512 and 0 <= cz + oz < 240
                                         and world.solid(cx + ox, cy + oy, cz + oz)}))
                exits = _exits(world, player.position, occupied)
                if not supports or len(exits) < 2:
                    continue
                # Preserve a departure route for nearby teammates, not just
                # the builder's centre. Crowded candidates fail conservatively.
                if any(not _exits(world, friend, occupied)
                       for friend in islice(friendly_positions, MAX_FRIENDLIES)
                       if math.dist(friend, player.position) < 10):
                    continue
                choices.append(ProjectSite("rear_cover" if rear else "cover", anchor,
                                           player.position, exits, name, quarter * math.pi / 2,
                                           cells, len(new_cells), geometry.block_count, supports))
    return min(choices, key=lambda item: item.required_blocks) if choices else None


def find_bridge_project(world: SimpleVoxelWorld, player: PlayerSnapshot,
                        known_goal: Vector3, *,
                        reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """Price one complete short dry-bank crossing, including its usable exit."""
    heading = _direction(player.position, known_goal)
    if heading is None or not _eligible(player) or int(C.BLOCK_TOOL) not in player.loadout:
        return None
    line = world.water_bridge_line(player.position, (*heading, 0.0), max_cells=6,
                                   require_landing_within=min(7, player.blocks + 1))
    if line is None:
        return None
    start, end = line
    if not 1 <= start[2] <= 238:
        return None
    dx = (end[0] > start[0]) - (end[0] < start[0])
    dy = (end[1] > start[1]) - (end[1] < start[1])
    if dx == dy == 0:
        dx, dy = ((1 if heading[0] > 0 else -1, 0) if abs(heading[0]) >= abs(heading[1])
                  else (0, 1 if heading[1] > 0 else -1))
    count = abs(end[0] - start[0]) + abs(end[1] - start[1]) + 1
    cells = tuple((start[0] + dx * i, start[1] + dy * i, start[2]) for i in range(count))
    landing = world.surface(end[0] + dx, end[1] + dy, player.position[2], vertical_span=0, clearance=3)
    if landing is None or count > player.blocks or not frozenset(cells).isdisjoint(reserved_cells):
        return None
    far_exit = (landing.position[0] + 2 * dx, landing.position[1] + 2 * dy, landing.position[2])
    if not _walk_clear(world, landing.position, far_exit):
        return None
    support = (start[0] - dx, start[1] - dy, start[2])
    return ProjectSite("bridge", start, player.position, (player.position, far_exit),
                       cells=cells, required_blocks=count, support_cells=(support,), landing=landing.position)


def find_mine_approach(world: SimpleVoxelWorld, player: PlayerSnapshot,
                       known_lane: Vector3, *, friendly_positions: Sequence[Vector3] = (),
                       reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """Choose a reachable side/rear approach on thick terrain, never a bridge.

    The project must approach, place with ordinary reach, then return before
    arming. Snapshot stock is mandatory. Caller must also avoid known friendly
    project routes, supplying their cells through ``reserved_cells``.
    """
    heading = _direction(player.position, known_lane)
    if (len(friendly_positions) > MAX_FRIENDLIES or heading is None
            or int(C.LANDMINE_TOOL) not in player.loadout or not _eligible(player)):
        return None
    if dict(player.deployable_stock).get(int(C.LANDMINE_TOOL), 0) <= 0:
        return None
    dx, dy = heading
    for forward, side in ((-7, 0), (-6, 3), (-6, -3), (0, 7), (0, -7)):
        point = (player.position[0] + forward * dx - side * dy,
                 player.position[1] + forward * dy + side * dx, player.position[2])
        node = _surface(world, point)
        if node is None or not _walk_clear(world, player.position, node.position):
            continue
        if len(_exits(world, player.position)) < 2 or len(_exits(world, node.position)) < 2:
            continue
        blast_cells = frozenset((node.x + ox, node.y + oy, node.support_z + oz)
                                for ox in range(-1, 2) for oy in range(-1, 2) for oz in range(3))
        if (not blast_cells.isdisjoint(reserved_cells)
                or any(not (0 <= x < 512 and 0 <= y < 512 and 1 <= z <= 238) for x, y, z in blast_cells)
                or not all(world.solid(*cell) for cell in blast_cells)):
            continue
        position = (node.x + .5, node.y + .5, float(node.support_z))
        if any(math.dist(position, friend) < 6 for friend in islice(friendly_positions, MAX_FRIENDLIES)):
            continue
        return ProjectSite("mine_approach", position, node.position, (player.position,),
                           support_cells=((node.x, node.y, node.support_z),))
    return None


def find_breach_project(world: SimpleVoxelWorld, player: PlayerSnapshot,
                        known_lane: Vector3, *,
                        reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """A short, costed body-clear tunnel toward a known dry, usable exit.

    Reuses the production navigator's recovered dig-footprint target selector.
    At most six columns / 64 removed cells / eight seconds of real tool work
    are accepted. No map or predicted world is changed. The normal navigation
    breach executor must perform and confirm every swing before occupation.
    """
    heading = _direction(player.position, known_lane)
    if heading is None or not _eligible(player):
        return None
    source = _surface(world, player.position)
    profile = best_navigation_dig_profile(islice(player.loadout, 16))
    if source is None or profile is None:
        return None
    dx, dy = ((1 if heading[0] > 0 else -1, 0) if abs(heading[0]) >= abs(heading[1])
              else (0, 1 if heading[1] > 0 else -1))
    removed: set[Cell] = set()
    supports: list[Cell] = []
    first_target = None
    approach = source.position
    work = 0.0
    limit = min(6, int(math.hypot(known_lane[0] - player.position[0], known_lane[1] - player.position[1])))
    for distance in range(1, limit + 1):
        x, y, support_z = source.x + dx * distance, source.y + dy * distance, source.support_z
        if not (1 <= x < 511 and 1 <= y < 511 and 4 <= support_z <= 238):
            return None
        floor = (x, y, support_z)
        if not world.solid(*floor):
            return None
        supports.append(floor)
        blockers = tuple((x, y, support_z - offset) for offset in (3, 2, 1)
                         if world.solid(x, y, support_z - offset) and (x, y, support_z - offset) not in removed)
        destination = (x + .5, y + .5, support_z - PLAYER_SUPPORT_OFFSET)
        if not blockers:
            if first_target is None:
                approach = destination
                continue
            landing = _surface(world, destination)
            far_exit = (destination[0] + 2 * dx, destination[1] + 2 * dy, destination[2])
            if landing is None or not _walk_clear(world, destination, far_exit):
                continue
            return ProjectSite("breach", tuple(value + .5 for value in first_target), approach,
                               (source.position, far_exit), cells=tuple(sorted(removed)),
                               support_cells=tuple(supports), landing=destination,
                               tool_id=profile.tool_id, estimated_seconds=work)
        remaining = blockers
        # Three body cells need at most three independently aimed swings.
        for _ in range(3):
            target, swings = world._clearance_target(remaining, support_z, profile)
            if target is None or swings <= 0:
                return None
            footprint = frozenset(melee_dig_positions(target, profile.pattern))
            if any(not (0 <= cx < 512 and 0 <= cy < 512 and 1 <= cz < support_z)
                   for cx, cy, cz in footprint) or not footprint.isdisjoint(reserved_cells):
                return None
            # A side wall can support an occupied perch; never dig any supplied
            # friendly reservation or the walking floor, including area swings.
            removed.update(cell for cell in footprint if world.solid(*cell))
            first_target = target if first_target is None else first_target
            # This aimed swing repeats until its toughest covered blocker
            # breaks (built walls have health 9, map voxels 5).
            work += profile.fire_interval * max(
                profile.swings_for_health(world.block_health(*cell))
                for cell in remaining
                if cell in footprint
            )
            if len(removed) > 64 or work > 8.0:
                return None
            remaining = tuple(cell for cell in remaining if cell not in removed)
            if not remaining:
                break
        if remaining:
            return None
    return None


def find_decorative_site(world: SimpleVoxelWorld, player: PlayerSnapshot, *,
                         friendly_positions: Sequence[Vector3] = (),
                         reserved_cells: frozenset[Cell] = frozenset()) -> ProjectSite | None:
    """One optional supported block in an open corner; never dig any footing."""
    if (len(friendly_positions) > MAX_FRIENDLIES or not _eligible(player)
            or player.blocks < 1 or int(C.BLOCK_TOOL) not in player.loadout):
        return None
    for dx, dy in ((3, 3), (3, -3), (-3, 3), (-3, -3)):
        point = (player.position[0] + dx, player.position[1] + dy, player.position[2])
        node = _surface(world, point)
        if node is None or not _walk_clear(world, player.position, node.position):
            continue
        cell = (node.x, node.y, node.support_z - 1)
        added = frozenset({cell})
        if cell in reserved_cells or not 1 <= cell[2] <= 238:
            continue
        if _body_overlap(added, (player.position, *tuple(islice(friendly_positions, MAX_FRIENDLIES)))):
            continue
        exits = _exits(world, player.position, added)
        if len(exits) < 3 or any(not _exits(world, friend, added)
                                 for friend in islice(friendly_positions, MAX_FRIENDLIES)):
            continue
        return ProjectSite("decoration", cell, player.position, exits,
                           cells=(cell,), required_blocks=1,
                           support_cells=((node.x, node.y, node.support_z),))
    return None


# --- Zombie refuge ramparts -------------------------------------------------
# A survivor squad walls its elected refuge the way humans do in retail
# Zombie rounds: a chest-high ring (two blocks above the plateau top, so a
# standing player still sees and shoots over it while a crouched one is
# covered) built from straight BlockLine runs shared out across the squad.
RAMPART_HALF_WIDTH = 4
RAMPART_HEIGHT = 2
RAMPART_MAX_RUN = 9


def _body_columns(positions: Sequence[Vector3]) -> frozenset[tuple[int, int]]:
    """Columns under a body (the authority's 0.45-wide footprint), any height."""
    columns: set[tuple[int, int]] = set()
    for position in islice(positions, MAX_FRIENDLIES + 1):
        px, py = float(position[0]), float(position[1])
        columns.update((x, y)
                       for x in range(math.floor(px - 0.45), math.floor(px + 0.45) + 1)
                       for y in range(math.floor(py - 0.45), math.floor(py + 0.45) + 1))
    return frozenset(columns)


def _rampart_columns(cx: int, cy: int, half_width: int) -> list[tuple[list[tuple[int, int]], tuple[int, int]]]:
    """Return the ring's four sides in walking order with their inward normals."""
    hw = half_width
    north = ([(x, cy - hw) for x in range(cx - hw, cx + hw + 1)], (0, 1))
    east = ([(cx + hw, y) for y in range(cy - hw + 1, cy + hw + 1)], (-1, 0))
    south = ([(x, cy + hw) for x in range(cx + hw - 1, cx - hw - 1, -1)], (0, -1))
    west = ([(cx - hw, y) for y in range(cy + hw - 1, cy - hw, -1)], (1, 0))
    return [north, east, south, west]


def _rampart_floor(world: SimpleVoxelWorld, x: int, y: int, top: int) -> int | None:
    """Topmost solid z of a ring column near the plateau plane (z grows down)."""
    for z in range(max(1, top - RAMPART_HEIGHT - 1), min(238, top + 4)):
        if world.solid(x, y, z):
            return z
    return None


def rampart_cells(world: SimpleVoxelWorld, centre: Vector3, *,
                  half_width: int = RAMPART_HALF_WIDTH,
                  height: int = RAMPART_HEIGHT) -> dict[tuple[int, int], tuple[int, ...]]:
    """Map each ring column to the wall cells it still needs (lowest first).

    Columns that drop three or more blocks below the plateau are a natural
    cliff and are left open; columns that already rise above the wall height
    need nothing. Cells already solid are omitted.
    """
    node = world.surface(math.floor(centre[0]), math.floor(centre[1]), centre[2],
                         vertical_span=2, clearance=3)
    if node is None:
        return {}
    top = int(node.support_z)
    needed: dict[tuple[int, int], tuple[int, ...]] = {}
    for columns, _inward in _rampart_columns(node.x, node.y, half_width):
        for x, y in columns:
            if not (1 <= x < 511 and 1 <= y < 511):
                continue
            floor = _rampart_floor(world, x, y, top)
            if floor is None or floor >= top + 3 or floor <= top - height:
                continue
            cells = tuple(z for z in range(floor - 1, top - height - 1, -1)
                          if 1 <= z <= 237 and not world.solid(x, y, z))
            if cells:
                needed[(x, y)] = cells
    return needed


def find_rampart_segment(world: SimpleVoxelWorld, player: PlayerSnapshot,
                         refuge: Vector3, *,
                         friendly_positions: Sequence[Vector3] = (),
                         reserved_cells: frozenset[Cell] = frozenset(),
                         half_width: int = RAMPART_HALF_WIDTH,
                         height: int = RAMPART_HEIGHT) -> ProjectSite | None:
    """Pick the next straight, grounded wall run this builder can lay.

    Only cells resting on something solid are offered, so the ring rises one
    layer at a time and every BlockLine cell passes the client's face-contact
    gate. Runs already reserved by a teammate, or passing through a body
    column, are skipped; the nearest run on the lowest unfinished layer wins.
    ``cells`` is the run in line order; ``approach`` is a standing spot two
    blocks inside the ring, within ordinary build reach of every cell.
    """
    if (len(friendly_positions) > MAX_FRIENDLIES or not _eligible(player)
            or int(C.BLOCK_TOOL) not in player.loadout or player.blocks < 2):
        return None
    node = world.surface(math.floor(refuge[0]), math.floor(refuge[1]), refuge[2],
                         vertical_span=2, clearance=3)
    if node is None:
        return None
    needed = rampart_cells(world, refuge, half_width=half_width, height=height)
    if not needed:
        return None
    occupied = _body_columns((player.position, *tuple(islice(friendly_positions, MAX_FRIENDLIES))))
    best: ProjectSite | None = None
    best_key: tuple[float, float] | None = None
    for columns, inward in _rampart_columns(node.x, node.y, half_width):
        layers = sorted({z for column in columns for z in needed.get(column, ())}, reverse=True)
        for z in layers:
            run: list[Cell] = []
            runs: list[list[Cell]] = []
            for x, y in columns:
                cell = (x, y, z)
                buildable = (z in needed.get((x, y), ()) and world.solid(x, y, z + 1)
                             and cell not in reserved_cells and (x, y) not in occupied)
                if buildable and len(run) < min(RAMPART_MAX_RUN, player.blocks):
                    run.append(cell)
                    continue
                if run:
                    runs.append(run)
                run = [cell] if buildable else []
            if run:
                runs.append(run)
            for cells in runs:
                mid = cells[len(cells) // 2]
                approach = None
                for depth in (2, 1, 3):
                    ax, ay = mid[0] + inward[0] * depth, mid[1] + inward[1] * depth
                    stand = world.surface(ax, ay, refuge[2], vertical_span=2, clearance=3)
                    if stand is None:
                        continue
                    eye = (stand.position[0], stand.position[1], stand.position[2])
                    if all(math.dist(eye, (cx + .5, cy + .5, cz + .5)) <= MAX_PROJECT_BUILD_REACH
                           for cx, cy, cz in cells):
                        approach = stand.position
                        break
                if approach is None:
                    continue
                key = (-float(z), math.dist(player.position, approach))
                if best_key is None or key < best_key:
                    supports = tuple((cx, cy, cz + 1) for cx, cy, cz in cells)
                    best_key = key
                    best = ProjectSite("rampart", (cells[0][0] + .5, cells[0][1] + .5, cells[0][2] + .5),
                                       approach, (), cells=tuple(cells), required_blocks=len(cells),
                                       support_cells=supports, tool_id=int(C.BLOCK_TOOL),
                                       estimated_seconds=2.0)
            if best is not None:
                # Lowest unfinished layer of this side found; other sides may
                # still offer a nearer run on the same layer.
                break
    return best
