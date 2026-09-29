"""Shared anti-abuse checks for carried/ground objectives and zones.

* :func:`pickup_line_of_sight` - objective pickups (intel, bomb, diamond)
  need an unobstructed voxel line from the player's body to the objective,
  so nobody grabs one through a wall or floor (pickup range alone is a 3D
  sphere that reaches through a one-block wall).
* :func:`ground_objective_trapped` / :func:`resettle_surface` - a ground
  objective buried under/inside placed blocks, sealed in a pocket, or left
  hovering after its support was dug away. Modes resettle it onto the
  current top surface (or return it home) after
  ``objective_entomb_seconds`` so it can never be made unobtainable.
* :func:`presence_eligible` - who may count toward holding a TC/MH zone
  (AFK bodies and escape-flagged players do not).
* :func:`end_spawn_protection_for_objective` - picking up an objective ends
  the carrier's spawn protection (you cannot run an intel/bomb home while
  invulnerable).

Coordinate spaces: *entity* anchors sit on the supporting voxel surface
(``dry_surface_anchor``); *player* anchors are 2.25 blocks higher
(``dry_ground_anchor``). Helpers take an explicit ``player_space`` flag.
"""

from __future__ import annotations

import math
import time

import shared.constants as C

STANDING = float(C.PLAYER_STANDING_POS_ABOVE_GROUND)
DEFAULT_ENTOMB_SECONDS = 5.0
DEFAULT_AFK_SECONDS = 60.0
# A buried objective in an air pocket smaller than this is unreachable in
# practice (a standing player needs a 1x1x3 shaft and room to approach).
POCKET_BUDGET = 48


def _cfg(server, name: str, default):
    return getattr(getattr(server, "config", None), name, default)


def entomb_seconds(server) -> float:
    return max(0.0, float(_cfg(server, "objective_entomb_seconds", DEFAULT_ENTOMB_SECONDS)))


# ---------------------------------------------------------------------------
# Line of sight
# ---------------------------------------------------------------------------

def pickup_line_of_sight(
    server, player, objective, *, player_space: bool, lift: float = 0.5
) -> bool:
    """True when some body point of ``player`` sees the objective.

    Several body samples (eye, chest, near the feet) keep crouching, stairs
    and standing right on top of the item from being rejected; any clear
    segment suffices. Without a terrain oracle this is permissive.
    """

    if not bool(_cfg(server, "objective_pickup_requires_los", True)):
        return True
    world = getattr(server, "world_manager", None)
    if world is None or getattr(world, "map", None) is None:
        return True
    try:
        ox, oy, oz = (float(v) for v in objective)
        px, py, pz = (float(v) for v in player.position)
    except (AttributeError, TypeError, ValueError):
        return True
    if player_space:
        oz += STANDING
    # The item rests on the surface voxel: aim a little above it.
    target = (ox, oy, oz - float(lift))
    from server.combat_runtime import segment_clear

    for body_z in (pz, pz + 1.0, pz + STANDING - 0.4):
        if segment_clear(world, (px, py, body_z), target, shrink_start=0.05, shrink_end=0.05):
            return True
    return False


# ---------------------------------------------------------------------------
# Buried / floating ground objectives
# ---------------------------------------------------------------------------

def _solid(world, x: int, y: int, z: int) -> bool:
    if not (0 <= x < int(C.MAP_X) and 0 <= y < int(C.MAP_Y)):
        return True
    if z < 0:
        return False
    if z >= int(C.MAP_Z):
        return True
    return bool(world.get_solid(x, y, z))


def _column_top(world, x: int, y: int) -> int:
    native = getattr(getattr(world, "map", None), "get_z", None)
    if callable(native):
        try:
            return int(native(x, y))
        except Exception:
            pass
    return int(world._get_surface_z(x, y))


def ground_objective_trapped(world, position, *, player_space: bool) -> str | None:
    """Why a ground objective is unobtainable right now, or None.

    ``"buried"``   - its own cell is solid (blocks placed on it);
    ``"sealed"``   - it sits in a tiny air pocket closed on every side;
    ``"floating"`` - nothing solid under it any more (support dug away), so
                     players standing below cannot reach it.
    """

    try:
        x, y, z = (float(v) for v in position)
    except (TypeError, ValueError):
        return None
    if (
        world is None
        or getattr(world, "map", None) is None
        or not callable(getattr(world, "get_solid", None))
    ):
        return None
    try:
        return _trapped(world, x, y, z, player_space)
    except Exception:
        return None


def _trapped(world, x: float, y: float, z: float, player_space: bool) -> str | None:
    surface = z + STANDING if player_space else z
    cx, cy = int(math.floor(x)), int(math.floor(y))
    support = int(math.floor(surface + 1e-6))
    cell = support - 1
    if _solid(world, cx, cy, cell):
        return "buried"
    if support < int(C.MAP_Z) - 1 and not any(
        _solid(world, cx, cy, support + dz) for dz in (0, 1)
    ):
        return "floating"
    # Sealed pocket: flood through air from the objective's cell.
    seen = {(cx, cy, cell)}
    queue = [(cx, cy, cell)]
    tops = {}
    while queue:
        if len(seen) > POCKET_BUDGET:
            return None
        px, py, pz = queue.pop()
        top = tops.get((px, py))
        if top is None:
            top = tops[(px, py)] = _column_top(world, px, py)
        if pz < top or pz <= 0:
            return None  # open sky above: reachable from outside
        for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            n = (px + dx, py + dy, pz + dz)
            if n in seen or _solid(world, *n):
                continue
            seen.add(n)
            queue.append(n)
    return "sealed"


def resettle_surface(world, position, *, player_space: bool):
    """A reachable resting place for an objective at ``position``'s column.

    Buried/sealed objectives rise onto the column's current top surface
    (onto the tomb); floating ones drop onto the next solid voxel below.
    Water or anything unusable falls back to the nearest dry surface.
    Returns the new anchor in the same coordinate space.
    """

    x, y, z = (float(v) for v in position)
    cx, cy = int(math.floor(x)), int(math.floor(y))
    surface = z + STANDING if player_space else z
    support = int(math.floor(surface + 1e-6))
    top = None
    if not _solid(world, cx, cy, support):
        native = getattr(getattr(world, "map", None), "get_z", None)
        if callable(native):
            try:
                top = int(native(cx, cy, support))
            except Exception:
                top = None
        if top is None:
            top = next(
                (zz for zz in range(support, int(C.MAP_Z)) if _solid(world, cx, cy, zz)),
                int(C.MAP_Z) - 1,
            )
    else:
        top = _column_top(world, cx, cy)
    if top > int(C.Z_ABOVE_WATERPLANE):
        anchor = world.dry_surface_anchor(x, y)
        new_surface = float(anchor[2])
        x, y = float(anchor[0]), float(anchor[1])
    else:
        new_surface = float(top)
    return (x, y, new_surface - STANDING if player_space else new_surface)


class TrapTimer:
    """Per-objective 'trapped since' clock with a grace period."""

    def __init__(self) -> None:
        self._since: dict = {}

    def due(self, key, trapped: bool, now: float, seconds: float) -> bool:
        if not trapped:
            self._since.pop(key, None)
            return False
        since = self._since.setdefault(key, now)
        if now - since >= seconds:
            self._since.pop(key, None)
            return True
        return False

    def clear(self) -> None:
        self._since.clear()


# ---------------------------------------------------------------------------
# Zone presence
# ---------------------------------------------------------------------------

def _idle_book(server) -> dict:
    book = getattr(server, "_objective_idle", None)
    if not isinstance(book, dict):
        book = {}
        try:
            server._objective_idle = book
        except AttributeError:
            pass
    return book


def idle_seconds(server, player, now: float | None = None) -> float:
    """Seconds since ``player`` last moved or turned (as observed here)."""

    now = time.monotonic() if now is None else float(now)
    try:
        key = int(player.id)
        position = tuple(round(float(v), 2) for v in player.position)
        orientation = tuple(
            round(float(v), 3) for v in getattr(player, "orientation", (0.0, 0.0, 0.0))
        )
    except (AttributeError, TypeError, ValueError):
        return 0.0
    book = _idle_book(server)
    entry = book.get(key)
    if entry is None or entry[0] is not player or entry[1] != position or entry[2] != orientation:
        book[key] = (player, position, orientation, now)
        return 0.0
    return now - entry[3]


def presence_eligible(server, player, now: float | None = None) -> bool:
    """Whether ``player`` may count toward holding an objective zone."""

    afk = float(_cfg(server, "objective_afk_seconds", DEFAULT_AFK_SECONDS))
    if afk > 0.0 and idle_seconds(server, player, now) >= afk:
        return False
    try:
        from server import escape_watch

        if escape_watch.is_flagged(server, player):
            return False
    except Exception:
        pass
    return True


# ---------------------------------------------------------------------------
# Spawn protection
# ---------------------------------------------------------------------------

def end_spawn_protection_for_objective(server, player) -> None:
    """Carrying an objective ends spawn protection (configurable)."""

    if not bool(_cfg(server, "objective_pickup_ends_spawn_protection", True)):
        return
    ender = getattr(player, "end_spawn_protection", None)
    if callable(ender):
        try:
            ender()
        except Exception:
            pass


__all__ = [
    "TrapTimer",
    "end_spawn_protection_for_objective",
    "entomb_seconds",
    "ground_objective_trapped",
    "idle_seconds",
    "pickup_line_of_sight",
    "presence_eligible",
    "resettle_surface",
]
