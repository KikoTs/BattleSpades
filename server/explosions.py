"""Pure explosion falloff and knockback helpers.

The stock ``shared.explosionDamageManager`` applies an impulse directly to a
character's current velocity.  Distance falloff is measured from the
character's body centre (0.75 blocks below the standing eye, 1.25 crouched),
while the impulse direction is measured from the explosion to the network
position itself.  Keeping the arithmetic here makes projectile and deployable
explosions share one implementation and keeps it independently testable.
"""
from __future__ import annotations

import math
from typing import Optional, Tuple


STANDING_BODY_OFFSET = 0.75
CROUCHING_BODY_OFFSET = 1.25


def explosion_falloff(
    explosion: Tuple[float, float, float],
    target: Tuple[float, float, float],
    radius: float,
    *,
    crouched: bool = False,
) -> float:
    """Return the stock squared-distance falloff in the inclusive 0..1 range."""
    radius = float(radius)
    if radius <= 0.0:
        return 0.0
    body_offset = CROUCHING_BODY_OFFSET if crouched else STANDING_BODY_OFFSET
    dx = float(target[0]) - float(explosion[0])
    dy = float(target[1]) - float(explosion[1])
    dz = float(target[2]) + body_offset - float(explosion[2])
    distance_sq = dx * dx + dy * dy + dz * dz
    radius_sq = radius * radius
    if distance_sq >= radius_sq:
        return 0.0
    return (radius_sq - distance_sq) / radius_sq


def explosion_impulse(
    explosion: Tuple[float, float, float],
    target: Tuple[float, float, float],
    radius: float,
    knockback_min: float,
    knockback_max: float,
    *,
    crouched: bool = False,
) -> Optional[Tuple[float, float, float]]:
    """Calculate the stock additive velocity impulse, or ``None`` out of range."""
    falloff = explosion_falloff(
        explosion, target, radius, crouched=crouched
    )
    if falloff <= 0.0:
        return None

    dx = float(target[0]) - float(explosion[0])
    dy = float(target[1]) - float(explosion[1])
    dz = float(target[2]) - float(explosion[2])
    direction_length = math.sqrt(dx * dx + dy * dy + dz * dz)
    if direction_length <= 1e-9:
        return None

    magnitude = float(knockback_min) + falloff * (
        float(knockback_max) - float(knockback_min)
    )
    if magnitude == 0.0:
        return None
    scale = magnitude / direction_length
    return (dx * scale, dy * scale, dz * scale)
