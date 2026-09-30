"""Stock weapon data and explosion geometry with server damage policies.

The data was recovered from the stock ``aos.pkg`` of the Steam build
(PYZ byte-identical to the embedded archive) -- see docs/WEAPONS_RETAIL.md:

* ``RETAIL_EXPLOSIONS`` are the literal arguments every
  ``shared.explosionDamageManager.ExplosionDamageManager.handle_*_damage``
  wrapper passes to ``handle_explosion_damage`` (captured by running the stock
  32-bit module against the stock constants).
* :func:`explosion_player_damage` / :func:`explosion_los_fraction` use the
  stock ``handle_explosion_damage`` geometry (distance falloff, crouch body offset, three
  line-of-sight rays weighted by ``LINE_OF_SIGHT_EXPLOSION_MODIFIERS``,
  ``SELF_EXPLOSION_DAMAGE_REDUCTION``). The neutral-team reduction remains a
  server policy: the stock client instead compares the victim's team ID to
  the attacking PLAYER ID, an apparent stock bug exposed by varying that ID.
* The per-class multipliers are the stock ``CLASS_DAMAGE_MULTIPLIER`` /
  ``CLASS_HEADSHOT_DAMAGE_MULTIPLIER`` tables. The stock HUD divides normalized
  health by the class damage multiplier; class descriptions also support
  applying these multipliers to the damage a class TAKES.

The module is pure: no server objects are imported, so it is unit-testable in
isolation and shared by combat_runtime and main._apply_blast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Tuple

import shared.constants as C

Vec3 = Tuple[float, float, float]

# ---------------------------------------------------------------------------
# Stock constants
# ---------------------------------------------------------------------------

SELF_EXPLOSION_DAMAGE_REDUCTION = float(
    getattr(C, "SELF_EXPLOSION_DAMAGE_REDUCTION", 0.5))
TEAM_EXPLOSION_DAMAGE_REDUCTION = float(
    getattr(C, "TEAM_EXPLOSION_DAMAGE_REDUCTION", 0.5))
# Existing server policy, not the stock client's apparent ID/team mix-up.
# See the 2026-09-30 correction in docs/WEAPONS_RETAIL.md.
TEAM_NEUTRAL = int(getattr(C, "TEAM_NEUTRAL", 1))

# LINE_OF_SIGHT_HEAD/TORSO/LEGS weights (stock A252 = {0: .5, 1: .3, 2: .2}).
LINE_OF_SIGHT_WEIGHTS = (0.5, 0.3, 0.2)
# Player.get_line_of_sight_positions (stock player.pyd, called live): the
# eye/head position, then +0.9 and +1.8 down the body (z grows downward);
# crouched +0.45 and +0.9.
LOS_OFFSETS_STANDING = (0.0, 0.9, 1.8)
LOS_OFFSETS_CROUCHING = (0.0, 0.45, 0.9)
# handle_explosion_damage measures the falloff to the body centre.
BODY_OFFSET_STANDING = 0.75
BODY_OFFSET_CROUCHING = 1.25
# Each LOS ray is world.hitscan_accurate(e + 0.1*v, v, length=1): the stock
# ray end is start + dir*length WITHOUT normalisation, so it spans 10%..110%
# of the way from the explosion to the sight point.
LOS_RAY_START_FRACTION = 0.1

# JETPACK_PROPERTIES field 7 (JETPACK_DAMAGE_MULTIPLIER index constant = 7).
JETPACK_DAMAGE_MULTIPLIER_FIELD = int(getattr(C, "JETPACK_DAMAGE_MULTIPLIER", 7))


# ---------------------------------------------------------------------------
# Stock per-explosive arguments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RetailExplosion:
    name: str
    kill_type: int
    radius: float
    knockback_min: float
    knockback_max: float
    damage: float
    classic: bool = False


def _kill(name: str, default: int) -> int:
    return int(getattr(getattr(C, "KILL", None), name, default))


# (handler, kill type, radius, knockback min, knockback max, damage, classic)
RETAIL_EXPLOSIONS: Tuple[RetailExplosion, ...] = (
    RetailExplosion("grenade", _kill("GRENADE_KILL", 3), 4.0, 0.5, 1.0, 230.0),
    RetailExplosion("classic_grenade", _kill("CLASSIC_GRENADE_KILL", 22),
                    9.0, 0.1, 0.1, 130.0, classic=True),
    RetailExplosion("antipersonnel_grenade",
                    _kill("ANTIPERSONNEL_GRENADE_KILL", 23),
                    6.0, 0.25, 0.5, 500.0),
    RetailExplosion("rocket", _kill("ROCKET_KILL", 4), 6.0, 0.0, 0.25, 140.0),
    RetailExplosion("rocket2", _kill("ROCKET2_KILL", 5), 6.0, 0.0, 0.25, 40.0),
    RetailExplosion("ugc_rocket2", _kill("UGC_ROCKET2_KILL", 27),
                    4.0, 0.0, 0.25, 50.0),
    RetailExplosion("drill", _kill("DRILL_KILL", 6), 3.0, 0.01, 0.1, 50.0),
    RetailExplosion("drill_destroyed", _kill("DRILL_KILL", 6),
                    3.5, 0.1, 0.2, 95.0),
    RetailExplosion("ugc_drill", _kill("UGC_DRILL_KILL", 28),
                    3.0, 0.01, 0.1, 50.0),
    RetailExplosion("rocket_turret", _kill("ROCKET_TURRET_KILL", 18),
                    3.0, 0.2, 1.0, 100.0),
    RetailExplosion("rocket_turret_rocket", _kill("ROCKET_TURRET_KILL", 18),
                    3.0, 0.1, 0.3, 50.0),
    RetailExplosion("corpse", _kill("CORPSE_KILL", 12), 3.0, 0.05, 0.1, 0.0),
    RetailExplosion("landmine", _kill("LANDMINE_KILL", 14),
                    6.0, 0.75, 0.75, 100.0),
    RetailExplosion("dynamite", _kill("DYNAMITE_KILL", 15),
                    8.0, 0.1, 0.15, 300.0),
    RetailExplosion("molotov", _kill("MOLOTOV_KILL", 24), 4.0, 0.0, 0.1, 50.0),
    RetailExplosion("airstrike", _kill("AIRSTRIKE_KILL", 16),
                    6.0, 1.0, 2.0, 400.0),
    RetailExplosion("bomb", _kill("BOMB_KILL", 17), 7.0, 2.0, 3.0, 500.0),
    RetailExplosion("snowball", _kill("SNOWBALL_KILL", 21),
                    5.0, 0.3, 0.3, 10.0),
    RetailExplosion("gl_grenade", _kill("GRENADE_LAUNCHER_KILL", 32),
                    4.0, 0.0, 0.25, 100.0),
    RetailExplosion("radar_station", _kill("RADAR_STATION_KILL", 33),
                    3.0, 0.0, 0.1, 7.0),
    # Stock wrapper order really is min .75 / max .1 (the edge pushes harder).
    RetailExplosion("sticky_grenade", _kill("STICKY_GRENADE_KILL", 34),
                    5.0, 0.75, 0.1, 200.0),
    RetailExplosion("c4", _kill("C4_KILL", 36), 8.0, 0.1, 0.15, 300.0),
    RetailExplosion("mine_launcher", _kill("MINE_KILL", 35),
                    6.0, 0.75, 0.75, 100.0),
)

RETAIL_EXPLOSIONS_BY_NAME = {spec.name: spec for spec in RETAIL_EXPLOSIONS}


def retail_explosion_for(kill_type, damage) -> Optional[RetailExplosion]:
    """The stock handler matching a blast's kill type and warhead damage.

    Several stock handlers share a kill type (drill / drill-destroyed, turret
    / turret rocket), so the caller's damage picks the variant. Returns
    ``None`` for blasts with no stock handler (e.g. grave, chemical bomb) or
    a damage figure that matches no variant.
    """
    try:
        kill_type = int(kill_type)
        damage = float(damage)
    except (TypeError, ValueError):
        return None
    for spec in RETAIL_EXPLOSIONS:
        if spec.kill_type == kill_type and math.isclose(
            spec.damage, damage, rel_tol=0.0, abs_tol=1e-6
        ):
            return spec
    return None


# ---------------------------------------------------------------------------
# Explosion damage (stock ExplosionDamageManager.handle_explosion_damage)
# ---------------------------------------------------------------------------

def explosion_falloff(explosion: Vec3, position: Vec3, radius: float,
                      body_offset: float) -> float:
    """(R^2 - d^2) / R^2 with d measured to ``position`` + body offset."""
    radius = float(radius)
    if radius <= 0.0:
        return 0.0
    dx = float(position[0]) - float(explosion[0])
    dy = float(position[1]) - float(explosion[1])
    dz = float(position[2]) + float(body_offset) - float(explosion[2])
    distance_sq = dx * dx + dy * dy + dz * dz
    radius_sq = radius * radius
    if distance_sq >= radius_sq:
        return 0.0
    return (radius_sq - distance_sq) / radius_sq


def los_sight_points(position: Vec3, crouched: bool) -> Tuple[Vec3, ...]:
    offsets = LOS_OFFSETS_CROUCHING if crouched else LOS_OFFSETS_STANDING
    x, y, z = (float(v) for v in position)
    return tuple((x, y, z + offset) for offset in offsets)


def los_ray_segment(explosion: Vec3, point: Vec3) -> Optional[Tuple[Vec3, Vec3]]:
    """Stock LOS ray segment: 10%..110% of the explosion->point vector."""
    vx = float(point[0]) - float(explosion[0])
    vy = float(point[1]) - float(explosion[1])
    vz = float(point[2]) - float(explosion[2])
    if vx * vx + vy * vy + vz * vz <= 1e-18:
        return None
    start = (
        float(explosion[0]) + vx * LOS_RAY_START_FRACTION,
        float(explosion[1]) + vy * LOS_RAY_START_FRACTION,
        float(explosion[2]) + vz * LOS_RAY_START_FRACTION,
    )
    end = (start[0] + vx, start[1] + vy, start[2] + vz)
    return start, end


def raycast_segment_blocker(raycast: Callable) -> Callable:
    """Adapt ``raycast(x, y, z, dx, dy, dz, length)`` (normalised direction,
    returns a hit or ``None``) to a ``blocked(start, end)`` predicate."""

    def blocked(start: Vec3, end: Vec3) -> bool:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        dz = end[2] - start[2]
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length <= 1e-9:
            return False
        return raycast(start[0], start[1], start[2],
                       dx / length, dy / length, dz / length,
                       length) is not None

    return blocked


def los_ray_blocked(blocked: Callable, explosion: Vec3, point: Vec3) -> bool:
    """Is the stock sight ray from ``explosion`` to ``point`` occluded?"""
    segment = los_ray_segment(explosion, point)
    if segment is None:
        return False
    return bool(blocked(segment[0], segment[1]))


def explosion_los_fraction(blocked: Optional[Callable], explosion: Vec3,
                           position: Vec3, crouched: bool) -> float:
    """Sum of LINE_OF_SIGHT weights of the sight points the blast reaches.

    ``blocked(start, end)`` tests one segment; ``None`` means unoccluded.
    """
    if blocked is None:
        return 1.0
    fraction = 0.0
    for weight, point in zip(LINE_OF_SIGHT_WEIGHTS,
                             los_sight_points(position, crouched)):
        if not los_ray_blocked(blocked, explosion, point):
            fraction += weight
    return fraction


def explosion_player_damage(
    explosion: Vec3,
    position: Vec3,
    radius: float,
    damage: float,
    *,
    crouched: bool = False,
    los_fraction: float = 1.0,
    is_self: bool = False,
    target_team: Optional[int] = None,
    classic: bool = False,
) -> float:
    """Stock blast geometry plus server reductions, before class multipliers."""
    falloff = explosion_falloff(
        explosion, position, radius,
        BODY_OFFSET_CROUCHING if crouched else BODY_OFFSET_STANDING,
    )
    if falloff <= 0.0 or los_fraction <= 0.0:
        return 0.0
    amount = float(damage) * falloff * float(los_fraction)
    if not classic:
        if is_self:
            amount *= SELF_EXPLOSION_DAMAGE_REDUCTION
        elif target_team is not None and int(target_team) == TEAM_NEUTRAL:
            amount *= TEAM_EXPLOSION_DAMAGE_REDUCTION
    return amount


def explosion_entity_damage(explosion: Vec3, position: Vec3, radius: float,
                            damage: float) -> float:
    """Stock damage to a non-player damageable (no body offset, one ray)."""
    return float(damage) * explosion_falloff(explosion, position, radius, 0.0)


# ---------------------------------------------------------------------------
# Victim-side multipliers
# ---------------------------------------------------------------------------

def _class_row(class_id, table_name: str, default: float) -> float:
    table = getattr(C, table_name, None) or {}
    try:
        return float(table.get(int(class_id), default))
    except (TypeError, ValueError):
        return float(default)


def class_damage_multiplier(target) -> float:
    """Stock CLASS_DAMAGE_MULTIPLIER of the class TAKING the damage."""
    return _class_row(getattr(target, "class_id", C.CLASS_SOLDIER),
                      "CLASS_DAMAGE_MULTIPLIER", 1.0)


def class_headshot_multiplier(target) -> float:
    """Stock CLASS_HEADSHOT_DAMAGE_MULTIPLIER of the class hit in the head."""
    return _class_row(getattr(target, "class_id", C.CLASS_SOLDIER),
                      "CLASS_HEADSHOT_DAMAGE_MULTIPLIER", 1.0)


def jetpack_damage_multiplier(target) -> float:
    """JETPACK_PROPERTIES[pack][JETPACK_DAMAGE_MULTIPLIER] while flying.

    2.0 for the Rocketeer packs (66/67), 1.0 for Engineer/UGC. Only the
    original server read this field (no client reference); it applies while
    the pack is actively thrusting.
    """
    if not bool(getattr(target, "jetpack_active", False)):
        return 1.0
    properties = (getattr(C, "JETPACK_PROPERTIES", None) or {}).get(
        int(getattr(target, "jetpack_id", 0) or 0))
    if not properties:
        return 1.0
    try:
        return float(properties.get(JETPACK_DAMAGE_MULTIPLIER_FIELD, 1.0))
    except (TypeError, ValueError):
        return 1.0


def victim_damage_multiplier(target, *, headshot: bool = False) -> float:
    multiplier = class_damage_multiplier(target) * jetpack_damage_multiplier(target)
    if headshot:
        multiplier *= class_headshot_multiplier(target)
    return multiplier


def scale_knockback(knockback_min: float, knockback_max: float,
                    los_fraction: float) -> Tuple[float, float]:
    """The stock impulse magnitude is scaled by the same LOS fraction."""
    fraction = max(0.0, float(los_fraction))
    return float(knockback_min) * fraction, float(knockback_max) * fraction


__all__: Sequence[str] = (
    "RETAIL_EXPLOSIONS",
    "RETAIL_EXPLOSIONS_BY_NAME",
    "RetailExplosion",
    "class_damage_multiplier",
    "class_headshot_multiplier",
    "explosion_entity_damage",
    "explosion_falloff",
    "explosion_los_fraction",
    "explosion_player_damage",
    "jetpack_damage_multiplier",
    "los_ray_blocked",
    "los_ray_segment",
    "los_sight_points",
    "raycast_segment_blocker",
    "retail_explosion_for",
    "scale_knockback",
    "victim_damage_multiplier",
)
