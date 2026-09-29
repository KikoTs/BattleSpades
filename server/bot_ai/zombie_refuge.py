"""Team refuge election for Zombie survivors.

Humans in retail Zombie rounds do not wait at their spawn: they run for a
defensible high spot, build up, and hold it together until it is breached.
This module picks that spot from ordinary map knowledge only (the terrain
height function and the rough positions of the two sides), so the director
can publish one ``zombie_refuge`` objective per survivor team and every bot on
the team converges on the same place.

Pure functions, no server objects: ``surface_z(x, y)`` returns the z of the
topmost solid voxel (smaller z is higher; the waterbed is 239).
"""

from __future__ import annotations

import math
from typing import Callable, Generator, Iterable, Sequence

Vector3 = tuple[float, float, float]
SurfaceFn = Callable[[int, int], int]
RegionFn = Callable[[int, int], int]

MAP_SIZE = 512
# Land surfaces at or above the waterplane; z >= 238 is water or the bed.
WATER_SURFACE_Z = 238
# Player position convention used by the bot objectives (see TacticalMap).
PLAYER_ABOVE_SURFACE = 2.25
_RING = (
    (4, 0), (-4, 0), (0, 4), (0, -4),
    (3, 3), (3, -3), (-3, 3), (-3, -3),
)
# A wider ring sees plateaus up to ~7 blocks across as high ground; the
# near ring alone would call their centre "flat".
_WIDE_RING = (
    (8, 0), (-8, 0), (0, 8), (0, -8),
    (6, 6), (6, -6), (-6, 6), (-6, -6),
)
_TOP = tuple((ox, oy) for ox in (-1, 0, 1) for oy in (-1, 0, 1))


def _centroid(points: Sequence[Vector3]) -> tuple[float, float] | None:
    if not points:
        return None
    return (
        sum(float(p[0]) for p in points) / len(points),
        sum(float(p[1]) for p in points) / len(points),
    )


def refuge_breached(
    refuge: Vector3,
    zombies: Iterable[Vector3],
    *,
    radius: float = 6.0,
    height_tolerance: float = 3.0,
) -> bool:
    """A zombie standing on or beside the refuge top makes it worthless."""

    rx, ry, rz = float(refuge[0]), float(refuge[1]), float(refuge[2])
    for position in zombies:
        if (math.hypot(float(position[0]) - rx, float(position[1]) - ry) <= radius
                and abs(float(position[2]) - rz) <= height_tolerance):
            return True
    return False


def elect_refuge(
    surface_z: SurfaceFn,
    survivors: Sequence[Vector3],
    zombies: Sequence[Vector3] = (),
    *,
    exclude: Iterable[Vector3] = (),
    region_of: RegionFn | None = None,
    search_radius: int = 56,
    step: int = 4,
    max_candidates: int = 1200,
) -> Vector3 | None:
    """Run :func:`iter_elect_refuge` to completion in one call."""

    election = iter_elect_refuge(
        surface_z, survivors, zombies, exclude=exclude, region_of=region_of,
        search_radius=search_radius, step=step, max_candidates=max_candidates,
    )
    while True:
        try:
            next(election)
        except StopIteration as done:
            return done.value


def iter_elect_refuge(
    surface_z: SurfaceFn,
    survivors: Sequence[Vector3],
    zombies: Sequence[Vector3] = (),
    *,
    exclude: Iterable[Vector3] = (),
    region_of: RegionFn | None = None,
    search_radius: int = 56,
    step: int = 4,
    max_candidates: int = 1200,
) -> Generator[None, None, Vector3 | None]:
    """Return the best high, flat, dry spot near the survivors, or None.

    A generator that yields after every candidate it examines and returns the
    elected spot (``StopIteration.value``). On a cold surface cache one
    election costs ~70 ms of column scans (SpookyMansion, measured), so the
    gameplay thread advances it under a per-tick time budget instead of
    running it inside a single perception refresh.

    Scoring favours a local high point (higher than the ring of samples four
    blocks around it), a flat 3x3 top to stand and build on, a short walk from
    the survivors' centroid, and distance from the zombies. Points near an
    excluded refuge (breached or unreachable) are skipped so a re-election
    moves the team somewhere new.
    """

    centre = _centroid(survivors)
    if centre is None:
        return None
    # Walkable-region filter: a refuge the squad can only reach by digging
    # (MayanJungle temple tops) stalls the whole team in breach queues.
    allowed_regions: set[int] | None = None
    if region_of is not None:
        allowed_regions = {
            int(region_of(int(p[0]), int(p[1]))) for p in survivors
        } - {0}
        if not allowed_regions:
            allowed_regions = None
    zombie_centre = _centroid(zombies)
    excluded = [(float(p[0]), float(p[1])) for p in exclude]
    cx, cy = centre
    best: Vector3 | None = None
    best_score = -math.inf
    considered = 0
    # Snap the grid to the map so repeated elections sample the same columns.
    start_x = int(cx - search_radius) // step * step
    start_y = int(cy - search_radius) // step * step
    for gx in range(start_x, int(cx + search_radius) + 1, step):
        for gy in range(start_y, int(cy + search_radius) + 1, step):
            if not (2 <= gx < MAP_SIZE - 2 and 2 <= gy < MAP_SIZE - 2):
                continue
            distance = math.hypot(gx - cx, gy - cy)
            if distance > search_radius:
                continue
            considered += 1
            if considered > max_candidates:
                return best
            yield
            if any(math.hypot(gx - ex, gy - ey) < 12.0 for ex, ey in excluded):
                continue
            if (allowed_regions is not None
                    and int(region_of(gx, gy)) not in allowed_regions):
                continue
            top = int(surface_z(gx, gy))
            if top >= WATER_SURFACE_Z:
                continue
            ring = [int(surface_z(gx + ox, gy + oy)) for ox, oy in _RING]
            if any(value >= WATER_SURFACE_Z for value in ring):
                # Refuges on a shoreline invite a swim-around; skip them.
                continue
            # Flat top: a squad must be able to stand and build here.
            top_cells = [int(surface_z(gx + ox, gy + oy)) for ox, oy in _TOP]
            if max(top_cells) - min(top_cells) > 1:
                continue
            if top > min(ring):
                # Not a local high point: something four blocks away looks down on it.
                continue
            wide = [int(surface_z(gx + ox, gy + oy)) for ox, oy in _WIDE_RING]
            wide_dry = [value for value in wide if value < WATER_SURFACE_Z] or [top]
            advantage = max(
                sum(ring) / len(ring) - top,
                sum(wide_dry) / len(wide_dry) - top,
            )  # positive = higher than the surroundings
            score = 3.0 * min(advantage, 12.0) - 0.06 * distance
            if zombie_centre is not None:
                to_zombies = math.hypot(gx - zombie_centre[0], gy - zombie_centre[1])
                from_survivors = math.hypot(cx - zombie_centre[0], cy - zombie_centre[1])
                score += 0.04 * max(-40.0, min(40.0, to_zombies - from_survivors))
            if score > best_score:
                best_score = score
                best = (gx + 0.5, gy + 0.5, float(top) - PLAYER_ABOVE_SURFACE)
    return best


__all__ = ["elect_refuge", "iter_elect_refuge", "refuge_breached"]
