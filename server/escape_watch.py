"""Reveal players who escaped the playable map or sealed themselves away.

Checked at low frequency (``escape_watch_interval``, 1 Hz by default) from
``BaseMode.on_tick``.  A flagged player gets the retail high-minimap marker
(ChangePlayer ``SET_HIGH_MINIMAP_VISIBILITY``, the same icon CTF carriers,
VIPs and the last Zombie survivor use), broadcast to everyone so the enemy
team can hunt them down.  The marker is cleared once the condition resolves.

Rules (all positions in VXL coordinates, z grows downward, 0..239):

* ``out_of_bounds``  - x/y outside the 512x512 map.  Any player.
* ``below_floor``    - feet below the indestructible z=239 floor.  Any player.
* ``sky``            - head above the map top (z < 0, jetpack/glitch) for
  ``escape_watch_sky_seconds``.  Any player.
* ``embedded``       - the body is inside solid voxels (head and torso cells
  solid) for ``escape_watch_embedded_seconds``.  Any player.
* ``entombed``       - standing in a small air pocket completely sealed by
  solid blocks (flood fill finds no route "outdoors" within a bounded
  budget) for ``escape_watch_entomb_seconds``.  Only for players the mode
  says matter (``BaseMode.escape_watch_objective_player``): objective
  carriers (intel/bomb/diamond), VIPs, Zombie survivors, zone holders.
  Ordinary TDM bunkers, trenches, tunnels open to the surface and normal
  buildings are never marked: a pocket counts as sealed only when the fill
  is exhausted, and any air cell above its column's topmost voxel (open sky)
  or a pocket larger than the budget counts as open.

The watch never clears a marker the mode owns (``mode_marks_player``), and
re-asserts its own marker if the mode clears its marker while the player is
still flagged.  Nothing here changes gameplay state other than the marker;
modes may consult :func:`is_flagged` (Zombie withholds survival score from a
sealed survivor).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

import shared.constants as C
from server.game_constants import TEAM1, TEAM2

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 1.0
DEFAULT_SKY_SECONDS = 5.0
DEFAULT_EMBEDDED_SECONDS = 3.0
DEFAULT_ENTOMB_SECONDS = 5.0
# Hysteresis: a flag clears only after this many consecutive clean checks.
CLEAR_AFTER_CHECKS = 2
# Flood-fill budget for the sealed-pocket test (air cells visited).
ENTOMB_FILL_BUDGET = 256

PLAYABLE_REASONS = ("out_of_bounds", "below_floor", "sky", "embedded", "entombed")

_NEIGHBORS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


@dataclass
class _Track:
    player: object
    condition: str | None = None
    since: float = 0.0
    flagged: str | None = None
    clean_checks: int = 0
    mode_marked: bool = False


@dataclass
class EscapeWatchState:
    mode_key: int
    next_check: float = 0.0
    tracks: dict = field(default_factory=dict)


def _cfg(server, name: str, default):
    return getattr(getattr(server, "config", None), name, default)


def _state(server) -> EscapeWatchState:
    mode_key = id(getattr(server, "mode", None))
    state = getattr(server, "_escape_watch", None)
    if not isinstance(state, EscapeWatchState):
        state = EscapeWatchState(mode_key)
        server._escape_watch = state
    elif state.mode_key != mode_key:
        # A new mode object: release every marker the old one left behind.
        for track in tuple(state.tracks.values()):
            if track.flagged:
                _send_marker(server, track.player, False)
        state = EscapeWatchState(mode_key)
        server._escape_watch = state
    return state


def _connected(server, player) -> bool:
    player_id = getattr(player, "id", None)
    if player_id is None:
        return False
    try:
        return getattr(server, "players", {}).get(int(player_id)) is player
    except (TypeError, ValueError):
        return False


def _send_marker(server, player, visible: bool, connection=None) -> None:
    if connection is None and not _connected(server, player):
        return
    from shared.packet import ChangePlayer

    packet = ChangePlayer()
    packet.player_id = int(player.id)
    packet.type = int(C.SET_HIGH_MINIMAP_VISIBILITY)
    packet.high_minimap_visibility = int(bool(visible))
    data = bytes(packet.generate())
    if connection is None:
        server.broadcast(data, reliable=True)
    else:
        connection.send(data, reliable=True)


def _mode_marks(mode, player) -> bool:
    marks = getattr(mode, "mode_marks_player", None)
    if not callable(marks):
        return False
    try:
        return bool(marks(player))
    except Exception:
        return False


def _objective_player(mode, player) -> bool:
    check = getattr(mode, "escape_watch_objective_player", None)
    if not callable(check):
        return False
    try:
        return bool(check(player))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _dims():
    return int(C.MAP_X), int(C.MAP_Y), int(C.MAP_Z)


def _solid(world, x: int, y: int, z: int) -> bool:
    map_x, map_y, map_z = _dims()
    if not (0 <= x < map_x and 0 <= y < map_y):
        return True
    if z < 0:
        return False
    if z >= map_z:
        return True
    return bool(world.get_solid(x, y, z))


def _column_top(world, x: int, y: int) -> int:
    native = getattr(getattr(world, "map", None), "get_z", None)
    if callable(native):
        try:
            return int(native(x, y))
        except Exception:
            pass
    getter = getattr(world, "_get_surface_z", None)
    if callable(getter):
        return int(getter(x, y))
    _mx, _my, map_z = _dims()
    for z in range(map_z):
        if world.get_solid(x, y, z):
            return z
    return map_z - 1


def body_embedded(world, position) -> bool:
    """Head and torso cells both inside solid voxels."""

    x, y, z = (float(v) for v in position)
    cx, cy = int(math.floor(x)), int(math.floor(y))
    return _solid(world, cx, cy, int(math.floor(z))) and _solid(
        world, cx, cy, int(math.floor(z + 1.0))
    )


def pocket_sealed(world, position, budget: int = ENTOMB_FILL_BUDGET) -> bool:
    """Whether ``position`` sits in a small air pocket sealed on all sides.

    Six-neighbour flood fill through air.  Reaching an air cell above its
    column's topmost voxel (open sky), the map top, or exhausting ``budget``
    means the pocket is open (reachable).  Only a fill that runs out of cells
    first proves the player is sealed in.
    """

    map_x, map_y, map_z = _dims()
    x, y, z = (float(v) for v in position)
    cx, cy = int(math.floor(x)), int(math.floor(y))
    start = None
    for dz in (0.0, 1.0, 2.0):
        cz = int(math.floor(z + dz))
        if not _solid(world, cx, cy, cz):
            start = (cx, cy, cz)
            break
    if start is None:
        return True
    tops: dict[tuple[int, int], int] = {}
    seen = {start}
    queue = [start]
    while queue:
        if len(seen) > budget:
            return False
        px, py, pz = queue.pop()
        if pz <= 0:
            return False
        top = tops.get((px, py))
        if top is None:
            top = tops[(px, py)] = _column_top(world, px, py)
        if pz < top:
            return False  # open sky above this cell: outdoors
        for dx, dy, dz in _NEIGHBORS:
            nx, ny, nz = px + dx, py + dy, pz + dz
            cell = (nx, ny, nz)
            if cell in seen:
                continue
            if not (0 <= nx < map_x and 0 <= ny < map_y) or nz >= map_z - 1:
                continue
            if nz < 0:
                return False
            if world.get_solid(nx, ny, nz):
                continue
            seen.add(cell)
            queue.append(cell)
    return True


def classify(world, player, *, objective: bool) -> str | None:
    """Return the escape condition ``player`` is currently in, or None."""

    try:
        x, y, z = (float(v) for v in player.position)
    except (AttributeError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, z)):
        return "out_of_bounds"
    map_x, map_y, map_z = _dims()
    if not (0.0 <= x < map_x and 0.0 <= y < map_y):
        return "out_of_bounds"
    feet = z + float(C.PLAYER_STANDING_POS_ABOVE_GROUND)
    if feet > float(map_z):
        return "below_floor"
    if z < 0.0:
        return "sky"
    if world is None or getattr(world, "map", None) is None:
        return None
    try:
        if body_embedded(world, (x, y, z)):
            return "embedded"
        if objective and pocket_sealed(world, (x, y, z)):
            return "entombed"
    except Exception:
        logger.debug("escape classification failed", exc_info=True)
    return None


def _hold_seconds(server, condition: str) -> float:
    if condition == "sky":
        return float(_cfg(server, "escape_watch_sky_seconds", DEFAULT_SKY_SECONDS))
    if condition == "embedded":
        return float(_cfg(server, "escape_watch_embedded_seconds", DEFAULT_EMBEDDED_SECONDS))
    if condition == "entombed":
        return float(_cfg(server, "escape_watch_entomb_seconds", DEFAULT_ENTOMB_SECONDS))
    return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_flagged(server, player) -> str | None:
    """The condition ``player`` is currently revealed for, or None."""

    state = getattr(server, "_escape_watch", None)
    if not isinstance(state, EscapeWatchState):
        return None
    track = state.tracks.get(int(getattr(player, "id", -1)))
    if track is None or track.player is not player:
        return None
    return track.flagged


def tick(server, mode=None, now: float | None = None, *, force: bool = False) -> None:
    """Run one throttled pass (cheap no-op between checks)."""

    if not bool(_cfg(server, "escape_watch_enabled", True)):
        return
    mode = mode if mode is not None else getattr(server, "mode", None)
    if mode is None or not bool(getattr(mode, "escape_watch_enabled", True)):
        return
    now = time.monotonic() if now is None else float(now)
    state = _state(server)
    if not force and now < state.next_check:
        return
    interval = max(0.1, float(_cfg(server, "escape_watch_interval", DEFAULT_INTERVAL)))
    state.next_check = now + interval
    world = getattr(server, "world_manager", None)
    players = getattr(server, "players", {}) or {}

    # Drop departed / id-reused tracks silently (never address a reused id).
    for player_id, track in tuple(state.tracks.items()):
        if players.get(player_id) is not track.player:
            state.tracks.pop(player_id, None)

    for player in tuple(players.values()):
        try:
            player_id = int(player.id)
        except (AttributeError, TypeError, ValueError):
            continue
        track = state.tracks.get(player_id)
        live = (
            bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", True))
            and int(getattr(player, "team", -1)) in (TEAM1, TEAM2)
        )
        if not live:
            if track is not None:
                if track.flagged and not _mode_marks(mode, player):
                    _send_marker(server, player, False)
                state.tracks.pop(player_id, None)
            continue
        if track is None:
            track = state.tracks[player_id] = _Track(player)
        condition = classify(
            world, player, objective=_objective_player(mode, player)
        )
        mode_marked = _mode_marks(mode, player)
        if condition is None:
            track.condition = None
            if track.flagged:
                track.clean_checks += 1
                if track.clean_checks >= CLEAR_AFTER_CHECKS:
                    logger.info("escape watch: %s no longer %s",
                                getattr(player, "name", player_id), track.flagged)
                    track.flagged = None
                    if not mode_marked:
                        _send_marker(server, player, False)
            track.mode_marked = mode_marked
            continue
        track.clean_checks = 0
        if condition != track.condition:
            track.condition = condition
            track.since = now
        if track.flagged is None and now - track.since >= _hold_seconds(server, condition):
            track.flagged = condition
            logger.info("escape watch: revealing %s (%s)",
                        getattr(player, "name", player_id), condition)
            if not mode_marked:
                _send_marker(server, player, True)
        elif track.flagged and track.mode_marked and not mode_marked:
            # The mode just dropped its own marker (e.g. intel captured):
            # the escape still stands, so keep the player revealed.
            _send_marker(server, player, True)
        elif track.flagged:
            track.flagged = condition
        track.mode_marked = mode_marked


def reveal_to(server, connection) -> None:
    """Replay current escape markers to a late joiner."""

    state = getattr(server, "_escape_watch", None)
    if not isinstance(state, EscapeWatchState):
        return
    for track in tuple(state.tracks.values()):
        if track.flagged and _connected(server, track.player):
            _send_marker(server, track.player, True, connection=connection)


def forget(server, player) -> None:
    """Drop a player's track (death/leave hooks); clears its own marker."""

    state = getattr(server, "_escape_watch", None)
    if not isinstance(state, EscapeWatchState):
        return
    track = state.tracks.pop(int(getattr(player, "id", -1)), None)
    if track is None or track.player is not player or not track.flagged:
        return
    if not _mode_marks(getattr(server, "mode", None), player):
        _send_marker(server, player, False)


__all__ = [
    "body_embedded",
    "classify",
    "forget",
    "is_flagged",
    "pocket_sealed",
    "reveal_to",
    "tick",
]
