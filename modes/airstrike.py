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

# CreateEntity carries the owner as one byte.  Objective airstrikes have no
# owner, and 0 is a real player slot: use the same 0xFF "no player" byte the
# unmanned machine gun sends (the reference server wrote -1, i.e. 0xFF, for an
# entity without a carrier).
NO_OWNER_PLAYER_ID = 0xFF

_PATTERN = (
    (0.0, 0.0),
    (-5.0, 0.0),
    (5.0, 0.0),
    (0.0, -5.0),
    (0.0, 5.0),
)


def sound_airstrike_siren(server, position, *, warn_ahead: bool = False) -> None:
    """AIRSTRIKE_SIREN_ONESHOT (9), positioned on the target.

    Demolition sounds it when a base falls (``warn_ahead``),
    DEM_TIME_TO_WAIT_FOR_AIRSTRIKE before the shells, so the 11 s siren is
    the warning; the later ``trigger_airstrike`` on that same target then
    skips its own siren. Other callers get the siren from
    ``trigger_airstrike`` itself.
    """
    center = tuple(float(value) for value in position[:3])
    if warn_ahead:
        try:
            warned = getattr(server, "_airstrike_warned", None)
            if not isinstance(warned, set):
                warned = set()
                server._airstrike_warned = warned
            warned.add(center)
        except AttributeError:
            pass
    try:
        from server.audio import SND_AIRSTRIKE_SIREN, play_sound

        play_sound(server, SND_AIRSTRIKE_SIREN, position=center, attenuation=0.25)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass


def _sound_airstrike_flyby(server, position) -> None:
    """AIRSTRIKE_FLYBY (10), or AIRSTRIKE_FLYBY_SPACE (11) on lunar maps.

    The client plays the shell impacts itself (AIRSTRIKE_EXPLODE_SOUND in
    gameScene); the aircraft pass has no client trigger, so the server sends
    it once per strike, positioned on the target.
    """
    center = tuple(float(value) for value in position[:3])
    try:
        from server.audio import airstrike_flyby_sound, play_sound

        play_sound(
            server, airstrike_flyby_sound(server), position=center, attenuation=0.25
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass


def trigger_airstrike(server, position) -> int:
    """Spawn the stock five-shell objective strike and return shell count.

    This is intentionally server-owned: objective airstrikes have no player
    who can receive kill credit.  Lightweight test/plugin facades without the
    projectile service still receive the audio and safely skip the visuals.
    The siren is skipped when ``sound_airstrike_siren(warn_ahead=True)``
    already sounded it for this target.
    """

    center = tuple(float(value) for value in position[:3])
    warned = getattr(server, "_airstrike_warned", None)
    if isinstance(warned, set) and center in warned:
        warned.discard(center)
    else:
        sound_airstrike_siren(server, center)
    _sound_airstrike_flyby(server, center)

    engine = getattr(server, "projectile_engine", None)
    spawn_visual = getattr(server, "spawn_projectile_entity", None)
    if engine is None or not callable(getattr(engine, "spawn_spec", None)):
        return 0

    # VXL z grows downward.  Start above the objective and travel toward the
    # ground with the recovered 100-unit shell speed.
    start_z = max(1.0, center[2] - 48.0)
    owner = SimpleNamespace(id=NO_OWNER_PLAYER_ID, team=TEAM_NEUTRAL)
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


__all__ = [
    "AIRSTRIKE_SPEC",
    "NO_OWNER_PLAYER_ID",
    "sound_airstrike_siren",
    "trigger_airstrike",
]
