"""Retail airstrike projectile used by objective game modes."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C

from server.game_constants import TEAM_NEUTRAL
from server.projectiles import ProjectileSpec


AIRSTRIKE_SPEC = ProjectileSpec(
    "airstrike",
    "contact",
    float(C.AIRSTRIKE_GRAVITY_MULTIPLIER),
    float(C.AIRSTRIKE_EXPLOSION_DAMAGE),
    float(C.AIRSTRIKE_EXPLOSION_BLOCK_DAMAGE),
    int(C.KILL.AIRSTRIKE_KILL),
    int(C.AIRSTRIKE_DAMAGE),
    entity_type=int(C.AIRSTRIKE_ENTITY),
    blast_radius=float(C.AIRSTRIKE_EXPLOSION_RADIUS),
    knockback_min=float(C.AIRSTRIKE_EXPLOSION_KNOCKBACK_MIN),
    knockback_max=float(C.AIRSTRIKE_EXPLOSION_KNOCKBACK_MAX),
)

_PATTERN = (
    (0.0, 0.0),
    (-5.0, 0.0),
    (5.0, 0.0),
    (0.0, -5.0),
    (0.0, 5.0),
)


def trigger_airstrike(server, position) -> int:
    """Spawn the stock five-shell objective strike and return shell count.

    This is intentionally server-owned: objective airstrikes have no player
    who can receive kill credit.  Lightweight test/plugin facades without the
    projectile service still receive the siren and safely skip the visuals.
    """

    center = tuple(float(value) for value in position[:3])
    try:
        from server.audio import SND_AIRSTRIKE_SIREN, play_sound

        play_sound(server, SND_AIRSTRIKE_SIREN, position=center, attenuation=0.25)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass

    engine = getattr(server, "projectile_engine", None)
    spawn_visual = getattr(server, "spawn_projectile_entity", None)
    if engine is None or not callable(getattr(engine, "spawn_spec", None)):
        return 0

    # VXL z grows downward.  Start above the objective and travel toward the
    # ground with the recovered 100-unit shell speed.
    start_z = max(1.0, center[2] - 48.0)
    owner = SimpleNamespace(id=0, team=TEAM_NEUTRAL)
    spawned = 0
    for dx, dy in _PATTERN:
        pos = (center[0] + dx, center[1] + dy, start_z)
        vel = (0.0, 0.0, float(C.AIRSTRIKE_SHELL_SPEED))
        projectile = engine.spawn_spec(
            AIRSTRIKE_SPEC,
            pos,
            vel,
            thrower_id=-1,
        )
        if callable(spawn_visual):
            spawn_visual(projectile, owner, pos, vel)
        spawned += 1
    return spawned


__all__ = ["AIRSTRIKE_SPEC", "trigger_airstrike"]
