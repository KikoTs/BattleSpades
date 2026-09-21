"""Bounded prefab complexity metadata for bot tactical decisions.

The counts mirror the KV6 voxel-count header. Keeping this tiny table in the
AI package avoids loading models or touching the filesystem in either the
gameplay hot path or the isolated worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Iterable

Cell = tuple[int, int, int]
Bounds = tuple[Cell, Cell]
MAX_PROJECT_PREFABS = 20
MAX_PROJECT_PREFAB_CELLS = 256


@dataclass(frozen=True, slots=True)
class PrefabGeometry:
    """Pickle-safe authored geometry prepared once outside worker decisions."""

    name: str
    cells_by_yaw: tuple[tuple[Cell, ...], ...]
    bounds_by_yaw: tuple[Bounds, ...]
    block_count: int


def load_bot_prefab_geometry(names: Iterable[str]) -> tuple[PrefabGeometry, ...]:
    """Load at most 20 small real KV6s at map/setup time, never per decision.

    Uses the same configured registry and invscale=1 expansion as authoritative
    placement. Missing and oversized geometry fails closed. The returned data
    contains no native handles and can travel in a MapSnapshot.
    """
    from server.prefabs import get_registry, rotate_point

    registry = get_registry()
    result: list[PrefabGeometry] = []
    seen: set[str] = set()
    for value in islice(names, MAX_PROJECT_PREFABS):
        name = str(value).lower()
        if name in seen:
            continue
        seen.add(name)
        model = registry.get(name)
        if model is None:
            continue
        points = model.get_points()
        if not 0 < len(points) <= MAX_PROJECT_PREFAB_CELLS:
            continue
        rotations = tuple(tuple(rotate_point(x, y, z, yaw, 0, 0)
                                for x, y, z, _r, _g, _b in points)
                          for yaw in range(4))
        bounds = tuple((tuple(min(cell[axis] for cell in cells) for axis in range(3)),
                        tuple(max(cell[axis] for cell in cells) for axis in range(3)))
                       for cells in rotations)
        result.append(PrefabGeometry(name, rotations, bounds, len(points)))
    return tuple(result)


BOT_PREFAB_BLOCK_COUNTS: dict[str, int] = {
    "prefab_caltrop": 11,
    "prefab_fort_wall": 39,
    "prefab_ladder": 20,
    "prefab_platform": 36,
    "prefab_safety_corridor": 452,
    "prefab_safety_tube": 120,
    "prefab_small_platform": 6,
    "prefab_small_wall": 6,
    "prefab_square_bunker": 50,
    "prefab_superbarrier": 96,
    "prefab_superbridge": 222,
    "prefab_superdome": 675,
    "prefab_superminibunker": 68,
    "prefab_superpole": 126,
    "prefab_supersmallwall": 32,
    "prefab_supertower": 138,
    "prefab_ultrabarrier": 268,
    "prefab_zombiebone": 142,
    "prefab_zombiehand": 135,
    "prefab_zombiehead": 446,
}

_PURPOSE_BLOCK_LIMITS = {
    "climb": 160,
    "cover": 128,
    "traversal": 256,
    "variety": 160,
}


def bot_prefab_block_count(name: str) -> int | None:
    """Return the authored voxel count for a bot-selectable prefab."""

    return BOT_PREFAB_BLOCK_COUNTS.get(str(name).lower())


def bot_prefab_is_suitable(name: str, purpose: str) -> bool:
    """Return whether a prefab is bounded enough for one tactical purpose."""

    count = bot_prefab_block_count(name)
    limit = _PURPOSE_BLOCK_LIMITS.get(
        str(purpose), _PURPOSE_BLOCK_LIMITS["cover"]
    )
    return count is not None and count <= limit


def is_zombie_prefab(name: str) -> bool:
    """Return whether a name belongs to the mode-authored zombie set."""

    return str(name).lower().startswith("prefab_zombie")
