"""Threat-aware team spawn selection (anti spawn-camping).

Plain spawning picked a random safe column in the team's spawn area, so a
camper standing over the base killed every new life. For each spawn this
module samples safe columns from the team's spawn candidates and scores them:

* distance to the nearest living enemy (far is good, very close is terrible);
* whether a nearby enemy has voxel line of sight to the spot;
* teammates who died near the spot in the last few seconds (camped area);
* other lives that spawned on the spot moments ago (no stacking);
* a small bonus for a teammate within support range (spawn with your squad);
* random jitter, then a weighted pick among the best few, so spawns stay
  unpredictable.

When every base spot is hot (enemies on top of or watching the base) it also
scores columns across the team's whole side of the map and uses one if it is
clearly safer. Everything is bounded (a few dozen samples, a handful of
enemies, cached region candidates) because it runs on the gameplay thread.

Availability: a life can always be created. When the base and the team's
side are dug away (e.g. down to the water plane) ``emergency_spawn`` picks
dry land nearest the team (team side first, then anywhere) from a cached
coarse grid of dry columns, and only as the last resort stands the player in
the water at the base, lifted clear of any solid voxel. ``rescue_spawn`` is
the bounded replacement for invalid mode proposals used by
``round_lifecycle.resolve_player_spawn``.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1, TEAM2

PLAYABLE_TEAMS = (TEAM1, TEAM2)

SAMPLE_COUNT = 20
ENEMY_SIGHT_RANGE = 72.0
MAX_SIGHT_CHECKS = 6
DANGER_RADIUS = 14.0
COMFORT_DISTANCE = 64.0
DEATH_MEMORY_SECONDS = 20.0
DEATH_RADIUS = 10.0
SPAWN_MEMORY_SECONDS = 6.0
STACK_RADIUS = 3.0
SUPPORT_RADIUS = 24.0
TOP_CHOICES = 3
# A best base score below this means the base is being camped.
HOT_BASE_SCORE = -3.0


@dataclass
class SpawnIntel:
    """Recent spawns and deaths for one map/mode instance."""

    key: tuple
    deaths: deque = field(default_factory=lambda: deque(maxlen=128))
    spawns: deque = field(default_factory=lambda: deque(maxlen=64))
    region_candidates: dict = field(default_factory=dict)


def _intel(server) -> SpawnIntel:
    world = getattr(server, "world_manager", None)
    key = (id(getattr(server, "mode", None)), id(getattr(world, "map", None)))
    intel = getattr(server, "_spawn_intel", None)
    if not isinstance(intel, SpawnIntel) or intel.key != key:
        intel = SpawnIntel(key)
        server._spawn_intel = intel
    return intel


def record_death(server, player) -> None:
    """Remember where a life ended (camped spots score worse for a while)."""

    try:
        team = int(player.team)
        x, y, z = (float(v) for v in player.position)
    except (AttributeError, TypeError, ValueError):
        return
    if team in PLAYABLE_TEAMS:
        try:
            _intel(server).deaths.append((time.monotonic(), team, x, y))
        except AttributeError:
            # Minimal embedders/test doubles without per-server state.
            pass


def _live_players(server):
    for player in tuple(getattr(server, "players", {}).values()):
        if getattr(player, "alive", False) and getattr(player, "spawned", False):
            yield player


def _column_position(world, x: int, y: int) -> tuple[float, float, float]:
    surface_z = world._get_surface_z(x, y)
    return (
        float(x) + 0.5,
        float(y) + 0.5,
        float(surface_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
    )


def _safe(world, x: int, y: int, authored_zones) -> bool:
    authored_zone = world._zone_at(authored_zones, x, y) if authored_zones else None
    return world._safe_spawn_column(
        x, y, authored_zone=authored_zone, reject_roofs=authored_zone is None,
    )


def _score(world, position, team, enemies, allies, intel, now, rng) -> float:
    from server.combat_runtime import segment_clear

    px, py, pz = position
    distances = sorted(
        (math.hypot(e[0] - px, e[1] - py), e) for e in enemies
    )
    nearest = distances[0][0] if distances else COMFORT_DISTANCE
    score = 3.0 * min(nearest, COMFORT_DISTANCE) / COMFORT_DISTANCE
    if nearest < DANGER_RADIUS:
        score -= 6.0
    sight_checks = 0
    for distance, enemy_eye in distances:
        if distance > ENEMY_SIGHT_RANGE or sight_checks >= MAX_SIGHT_CHECKS:
            break
        sight_checks += 1
        # Head of a standing body at the spot.
        head = (px, py, pz - 0.5)
        if segment_clear(world, enemy_eye, head):
            score -= 4.0
    for stamp, dead_team, dx, dy in intel.deaths:
        if (dead_team == team and now - stamp <= DEATH_MEMORY_SECONDS
                and math.hypot(dx - px, dy - py) <= DEATH_RADIUS):
            score -= 1.5
    for stamp, spawn_team, sx, sy in intel.spawns:
        if now - stamp <= SPAWN_MEMORY_SECONDS and math.hypot(sx - px, sy - py) <= STACK_RADIUS:
            score -= 2.0
    if any(math.hypot(a[0] - px, a[1] - py) <= SUPPORT_RADIUS for a in allies):
        score += 0.5
    return score + rng.uniform(0.0, 0.6)


def _candidate_position(world, candidate, authored_zones):
    """Live standing position for one spawn candidate, or None if unsafe.

    Candidates are ``(x, y)`` top-surface columns or, inside authored retail
    boxes, ``(x, y, floor_z)`` storeys / sea spawns (see
    ``WorldManager._zone_candidates``).
    """
    resolver = getattr(world, "spawn_candidate_position", None)
    if callable(resolver):
        return resolver(candidate, authored_zones)
    x, y = candidate[0], candidate[1]
    if not _safe(world, x, y, authored_zones):
        return None
    return _column_position(world, x, y)


def _best(world, columns, authored_zones, team, enemies, allies, intel, now, rng):
    sample = rng.sample(columns, min(len(columns), SAMPLE_COUNT))
    scored = []
    for candidate in sample:
        position = _candidate_position(world, candidate, authored_zones)
        if position is None:
            continue
        scored.append((_score(world, position, team, enemies, allies, intel, now, rng), position))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def _pick(scored, rng):
    top = scored[:TOP_CHOICES]
    if not top:
        return None
    floor = min(score for score, _ in top)
    weights = [score - floor + 0.5 for score, _ in top]
    return rng.choices(top, weights=weights, k=1)[0]


def choose_team_spawn(server, player, *, rng: random.Random | None = None):
    """A threat-aware spawn for ``player`` or None to use the plain resolver.

    Once a map is loaded this never gives up on a playable team: when the
    whole base (and the team's side) has been dug away it returns
    :func:`emergency_spawn` instead of None.  None made the caller fall into
    ``WorldManager.get_spawn_point``'s exhaustive region spiral, which took
    seconds per life on a dug-out base and stalled the gameplay thread for
    every respawn (the live "nobody can spawn" hard lock).
    """

    rng = rng or random
    world = getattr(server, "world_manager", None)
    team = int(getattr(player, "team", -1))
    if world is None or getattr(world, "map", None) is None or team not in PLAYABLE_TEAMS:
        return None
    try:
        columns = list(world._get_spawn_candidates(team))
    except Exception:
        return None
    if not columns:
        return emergency_spawn(server, team)
    authored_zones = world.map_metadata.spawn_zones.get(team, [])
    intel = _intel(server)
    now = time.monotonic()
    enemies, allies = [], []
    for other in _live_players(server):
        if other is player:
            continue
        other_team = int(getattr(other, "team", -1))
        if other_team not in PLAYABLE_TEAMS:
            continue
        eye = tuple(float(v) for v in getattr(other, "eye", other.position))
        (allies if other_team == team else enemies).append(eye)

    scored = _best(world, columns, authored_zones, team, enemies, allies, intel, now, rng)
    if not scored and len(columns) > SAMPLE_COUNT:
        # A heavily dug base can still hold a few intact columns; one more
        # bounded sample before leaving the base entirely.
        scored = _best(world, columns, authored_zones, team, enemies, allies, intel, now, rng)
    choice = _pick(scored, rng)
    if choice is None or (enemies and choice[0] < HOT_BASE_SCORE):
        # The base is camped (or dug away): look across the team's side.
        region = intel.region_candidates.get(team)
        if region is None:
            try:
                region = list(world._fallback_spawn_candidates(team))
            except Exception:
                region = []
            intel.region_candidates[team] = region
        if region:
            wider = _pick(_best(world, region, (), team, enemies, allies, intel, now, rng), rng)
            if wider is not None and (choice is None or wider[0] > choice[0] + 1.0):
                choice = wider
    if choice is None:
        position = emergency_spawn(server, team)
    else:
        position = choice[1]
    intel.spawns.append((now, team, position[0], position[1]))
    return position


# ---------------------------------------------------------------------------
# Spawn availability: a life must always be creatable.
# ---------------------------------------------------------------------------

EMERGENCY_GRID_STEP = 8
EMERGENCY_MAX_CHECKS = 96
EMERGENCY_CACHE_SECONDS = 15.0
EMERGENCY_SPREAD = 3


def _map_dims(world):
    from server.world_manager import MAP_X, MAP_Y, MAP_Z

    return int(MAP_X), int(MAP_Y), int(MAP_Z)


def _column_top(world, x: int, y: int) -> int:
    """Topmost solid z of one column; the native O(1) VXL index when present."""

    native = getattr(getattr(world, "map", None), "get_z", None)
    if callable(native):
        try:
            return int(native(x, y))
        except Exception:
            pass
    return int(world._get_surface_z(x, y))


def _dry_grid(server, world) -> list[tuple[int, int]]:
    """Coarse grid of currently dry columns across the whole map (cached).

    One native top-z read per grid cell (4096 on a 512x512 map), refreshed
    after terrain changes at most every ``EMERGENCY_CACHE_SECONDS``. Entries
    are hints only: every use revalidates the live column.
    """

    intel = _intel(server)
    version = getattr(world, "topology_version", None)
    cached = getattr(intel, "dry_grid", None)
    now = time.monotonic()
    if cached is not None:
        stamp, cached_version, columns = cached
        if cached_version == version or now - stamp < EMERGENCY_CACHE_SECONDS:
            return columns
    map_x, map_y, map_z = _map_dims(world)
    waterline = map_z - 2
    step = EMERGENCY_GRID_STEP
    columns = [
        (x, y)
        for x in range(step // 2, map_x - 1, step)
        for y in range(step // 2, map_y - 1, step)
        if _column_top(world, x, y) <= waterline
    ]
    intel.dry_grid = (now, version, columns)
    return columns


def _team_region(world, team: int):
    try:
        return tuple(int(v) for v in world._spawn_region(int(team)))
    except Exception:
        return None


def _team_anchor_xy(world, team: int):
    try:
        anchor = world.team_base_anchor(int(team))
        return float(anchor[0]), float(anchor[1])
    except Exception:
        region = _team_region(world, team)
        if region is None:
            return 256.0, 256.0
        x0, y0, x1, y1 = region
        return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def _dry_position(world, x: int, y: int, *, strict: bool):
    """Standing position on column (x, y) when it is a valid dry spawn now."""

    try:
        if strict:
            if not world._safe_spawn_column(x, y):
                return None
            return _column_position(world, x, y)
        position = _column_position(world, x, y)
        checker = getattr(world, "spawn_position_is_safe", None)
        if callable(checker) and not checker(position):
            return None
        return position
    except Exception:
        return None


def water_fallback(world, x: float, y: float) -> tuple[float, float, float]:
    """Last resort: stand in the water (or on whatever is there) at (x, y).

    The body is lifted until the full standing capsule is clear, so the
    result is never inside solid blocks even when (x, y) is still land.
    """

    map_x, map_y, map_z = _map_dims(world)
    cx = min(max(int(math.floor(x)), 1), map_x - 2)
    cy = min(max(int(math.floor(y)), 1), map_y - 2)
    px, py = float(cx) + 0.5, float(cy) + 0.5
    try:
        top = int(world._get_surface_z(cx, cy))
    except Exception:
        top = map_z - 1
    z = float(top) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5
    clear = getattr(world, "_player_body_is_clear", None)
    if callable(clear):
        for _ in range(map_z):
            try:
                if clear(px, py, z):
                    break
            except Exception:
                break
            z -= 1.0
    return px, py, z


def emergency_spawn(server, team: int, near=None) -> tuple[float, float, float]:
    """A spawn that always exists: team-side dry land, any dry land, water.

    Preference order (each step bounded to ``EMERGENCY_MAX_CHECKS`` live
    validations over a coarse cached grid):

    1. strict safe ground (level, not a roof) on the team's side, nearest to
       ``near`` (the mode's proposal) or the team base;
    2. strict safe ground anywhere, nearest first;
    3. any dry standable surface (roofs/slopes allowed) anywhere;
    4. the water at the team base (accepted by design as the final resort).
    """

    world = getattr(server, "world_manager", None)
    if near is not None:
        try:
            origin = float(near[0]), float(near[1])
            if not all(math.isfinite(v) for v in origin):
                raise ValueError
        except (TypeError, ValueError, IndexError):
            origin = _team_anchor_xy(world, team)
    else:
        origin = _team_anchor_xy(world, team)
    if world is None or getattr(world, "map", None) is None:
        return (256.0, 256.0, 230.0)
    try:
        grid = _dry_grid(server, world)
    except Exception:
        grid = []

    def nearest(columns):
        return sorted(
            columns,
            key=lambda c: (c[0] + 0.5 - origin[0]) ** 2 + (c[1] + 0.5 - origin[1]) ** 2,
        )

    region = _team_region(world, team) if int(team) in PLAYABLE_TEAMS else None
    ordered = nearest(grid)
    passes = []
    if region is not None:
        x0, y0, x1, y1 = region
        passes.append((
            [c for c in ordered if x0 <= c[0] <= x1 and y0 <= c[1] <= y1], True,
        ))
    passes.append((ordered, True))
    passes.append((ordered, False))
    for columns, strict in passes:
        found = []
        for x, y in columns[:EMERGENCY_MAX_CHECKS]:
            position = _dry_position(world, x, y, strict=strict)
            if position is not None:
                found.append(position)
                if len(found) >= EMERGENCY_SPREAD:
                    break
        if found:
            # Spread consecutive lives over the nearest few spots.
            return random.choice(found)
    return water_fallback(world, *_team_anchor_xy(world, team))


def rescue_spawn(server, player, candidate) -> tuple[float, float, float]:
    """Replace an unusable mode spawn with a bounded, always-valid one.

    First a small local search around the proposal (authored mode spawns stay
    close to where the mode wanted them), then :func:`emergency_spawn`.
    Unlike ``WorldManager.sanitize_spawn_point`` this never walks a whole
    team region cell by cell, so a dug-out map cannot stall respawns.
    """

    world = getattr(server, "world_manager", None)
    team = int(getattr(player, "team", -1))
    # The generic body check has no team, so it rejects every water
    # position; a retail sea spawn (zombies in SpookyMansion's water ring)
    # is only valid for the team whose authored area it is.
    checker = getattr(world, "spawn_position_is_safe", None)
    if candidate is not None and callable(checker) and team in PLAYABLE_TEAMS:
        try:
            if checker(candidate, team=team):
                return tuple(float(candidate[index]) for index in range(3))
        except Exception:
            pass
    try:
        x, y = float(candidate[0]), float(candidate[1])
        finite = math.isfinite(x) and math.isfinite(y)
    except (TypeError, ValueError, IndexError):
        finite = False
    if finite and world is not None:
        local = getattr(world, "_nearest_safe_spawn_point", None)
        if callable(local):
            try:
                found = local(x, y, search=6)
            except Exception:
                found = None
            if found is not None:
                return tuple(float(v) for v in found)
    return emergency_spawn(server, team, near=candidate if finite else None)
