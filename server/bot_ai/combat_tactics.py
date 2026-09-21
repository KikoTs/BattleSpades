"""Situational fighting choices for the single-owner worker loop.

Everything here consumes the worker's own perception and voxel copy: no hidden
enemy state, no authoritative objects. The native motor, gateway and combat
service still validate every movement and shot.

The helpers answer the questions a player asks during a firefight: which part
of the enemy can I actually hit, why are my shots not leaving the barrel,
where can I duck to reload, is that grenade about to land on me, and have I
been standing in this spot too long.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .messages import BotProfile, EntitySnapshot, PerceptionFrame, PlayerSnapshot, Vector3

# AoS z grows downward. Player position is the head; the torso sits about one
# block lower and a crouch lowers the head by roughly the same amount.
TORSO_OFFSET = 1.0
CROUCH_DROP = 0.9
_COVER_RADII = (2.0, 3.5, 5.0, 7.0)
_COVER_DIRECTIONS = 12
_MAX_COVER_RAYS = 14


def mix(*values: int) -> float:
    """Repeatable 0..1 variation from small integers (no global RNG)."""

    state = 0x9E3779B9
    for value in values:
        state ^= (int(value) + 0x7F4A7C15 + (state << 6) + (state >> 2)) & 0xFFFFFFFF
        state = (state * 2654435761) & 0xFFFFFFFF
    state ^= state >> 15
    return (state & 0xFFFF) / 65535.0


@dataclass(slots=True)
class Engagement:
    """Bounded fighting memory for one bot life."""

    target: tuple[int, int, int] | None = None
    wanting_fire_since: float | None = None
    blocked_until: float = 0.0
    head_aim_until: float = 0.0
    spot: Vector3 | None = None
    spot_since: float = 0.0
    spot_hold: float = 0.0
    spot_pressure_at: float = 0.0
    relocate_until: float = 0.0
    relocate_heading: Vector3 = (0.0, 0.0, 0.0)
    relocations: int = 0
    cover: Vector3 | None = None
    cover_until: float = 0.0
    cover_duck: bool = False
    next_cover_search_at: float = 0.0
    evade_until: float = 0.0
    evade_heading: Vector3 = (0.0, 0.0, 0.0)
    evade_entity: int = 0

    def retarget(self, target: PlayerSnapshot) -> None:
        identity = (target.player_id, target.generation, target.life_id)
        if identity != self.target:
            self.target = identity
            self.wanting_fire_since = None
            self.blocked_until = 0.0
            self.head_aim_until = 0.0


def aim_offset(world, observer: PlayerSnapshot, target: PlayerSnapshot,
               profile: BotProfile, engagement: Engagement, now: float) -> float:
    """Aim at a body part that is really exposed.

    Terraces, window sills and trench lips routinely hide a torso while the
    head stays visible. Aiming at the hidden torso made the live lane probe
    veto every shot, freezing both fighters in a silent standoff.
    """

    if profile.skill >= 0.82 or now < engagement.head_aim_until:
        return 0.0
    torso = (target.eye[0], target.eye[1], target.eye[2] + TORSO_OFFSET)
    return TORSO_OFFSET if world.has_line_of_sight(observer.eye, torso) else 0.0


def fire_blocked(engagement: Engagement, observer: PlayerSnapshot,
                 wants_fire: bool, now: float, *, cadence: float) -> bool:
    """Notice that wanted shots are not being accepted and demand a new angle.

    The worker's sampled sight ray and the authoritative raycast can disagree
    on a grazing edge. A player whose gun stays silent moves; so must a bot.
    """

    if not wants_fire or observer.reloading:
        engagement.wanting_fire_since = None
        return now < engagement.blocked_until
    fired = (observer.last_action_kind == "fire" and observer.last_action_accepted
             and 0.0 <= now - observer.last_action_at <= max(1.0, cadence))
    if fired or engagement.wanting_fire_since is None:
        engagement.wanting_fire_since = now
    elif now - engagement.wanting_fire_since >= max(1.5, cadence + 1.0):
        engagement.blocked_until = now + 2.5
        engagement.head_aim_until = now + 2.5
        engagement.wanting_fire_since = now
    return now < engagement.blocked_until


def walkable_heading(world, observer: PlayerSnapshot, heading: Vector3,
                     *, reach: float = 2.0) -> bool:
    """Reject strides into walls, pits or water before the motor finds out."""

    z = observer.position[2]
    for distance in (1.0, reach):
        x = observer.position[0] + heading[0] * distance
        y = observer.position[1] + heading[1] * distance
        surface = world.surface(int(math.floor(x)), int(math.floor(y)), z,
                                vertical_span=2, allow_water=False)
        if surface is None or abs(surface.position[2] - z) > 1.1:
            return False
        z = surface.position[2]
    return True


def relocation_heading(world, observer: PlayerSnapshot, target: PlayerSnapshot,
                       profile: BotProfile, engagement: Engagement) -> Vector3 | None:
    """Pick a lateral (slightly range-correcting) stride that is really open."""

    dx = target.position[0] - observer.position[0]
    dy = target.position[1] - observer.position[1]
    distance = math.hypot(dx, dy)
    if distance < 1e-6:
        return None
    ux, uy = dx / distance, dy / distance
    side = 1.0 if mix(observer.player_id, observer.life_id, engagement.relocations) < 0.5 else -1.0
    radial = 0.35 * (profile.aggression - profile.caution)
    for sign in (side, -side):
        for blend in (radial, 0.0, -0.4):
            hx, hy = -uy * sign + ux * blend, ux * sign + uy * blend
            length = math.hypot(hx, hy)
            heading = (hx / length, hy / length, 0.0)
            if walkable_heading(world, observer, heading, reach=3.0):
                return heading
    return None


def stationary_relocation(world, observer: PlayerSnapshot, target: PlayerSnapshot,
                          profile: BotProfile, engagement: Engagement,
                          now: float) -> Vector3 | None:
    """Make planted shooters (snipers, machine guns) displace like players do.

    A marksman fires from one spot for a personality-dependent spell, then
    shifts; being hit while planted forces the move immediately.
    """

    if now < engagement.relocate_until:
        return engagement.relocate_heading
    if (engagement.spot is None
            or math.dist(observer.position[:2], engagement.spot[:2]) > 1.5):
        engagement.spot = observer.position
        engagement.spot_since = now
        engagement.spot_pressure_at = observer.last_damage_at
        variation = mix(observer.player_id, observer.life_id, engagement.relocations, 7)
        engagement.spot_hold = 4.0 + 7.0 * profile.caution + 4.0 * variation
        return None
    pressured = (observer.last_damage_at > engagement.spot_pressure_at
                 and 0.0 <= now - observer.last_damage_at <= 1.0
                 and now - engagement.spot_since >= 0.5)
    if not pressured and now - engagement.spot_since < engagement.spot_hold:
        return None
    heading = relocation_heading(world, observer, target, profile, engagement)
    engagement.relocations += 1
    engagement.spot = None
    if heading is None:
        return None
    variation = mix(observer.player_id, observer.life_id, engagement.relocations, 11)
    engagement.relocate_heading = heading
    engagement.relocate_until = now + (0.9 + 0.9 * variation if pressured
                                       else 1.2 + 1.2 * variation)
    return heading


def find_cover(world, observer: PlayerSnapshot, threat_eye: Vector3,
               engagement: Engagement, now: float) -> tuple[Vector3 | None, bool]:
    """Return ``(position, duck)`` hiding the bot from one visible threat.

    ``duck`` means crouching in place already breaks the sight line (a low
    wall or terrace lip). Otherwise a nearby reachable cell behind terrain is
    returned. Searches are rate limited and ray-bounded.
    """

    if now < engagement.cover_until and (engagement.cover is not None or engagement.cover_duck):
        return engagement.cover, engagement.cover_duck
    if now < engagement.next_cover_search_at:
        return None, False
    engagement.next_cover_search_at = now + 1.0
    engagement.cover = None
    engagement.cover_duck = False
    ducked = (observer.eye[0], observer.eye[1], observer.eye[2] + CROUCH_DROP)
    if not world.has_line_of_sight(ducked, threat_eye):
        engagement.cover_duck = True
        engagement.cover_until = now + 3.0
        return None, True
    away = math.atan2(observer.position[1] - threat_eye[1],
                      observer.position[0] - threat_eye[0])
    rays = 0
    for radius in _COVER_RADII:
        # Prefer cells behind the bot relative to the threat, then the sides.
        for index in range(_COVER_DIRECTIONS):
            step = (index + 1) // 2 * (1 if index % 2 else -1)
            if abs(step) > _COVER_DIRECTIONS // 3:
                continue  # never run toward the shooter for cover
            angle = away + step * (2.0 * math.pi / _COVER_DIRECTIONS)
            heading = (math.cos(angle), math.sin(angle), 0.0)
            x = observer.position[0] + heading[0] * radius
            y = observer.position[1] + heading[1] * radius
            surface = world.surface(int(math.floor(x)), int(math.floor(y)),
                                    observer.position[2], vertical_span=2,
                                    allow_water=False)
            if surface is None or abs(surface.position[2] - observer.position[2]) > 1.6:
                continue
            if not walkable_heading(world, observer, heading, reach=min(radius, 3.0)):
                continue
            rays += 1
            if not world.has_line_of_sight(surface.position, threat_eye):
                engagement.cover = surface.position
                engagement.cover_until = now + 4.0
                return surface.position, False
            if rays >= _MAX_COVER_RAYS:
                return None, False
    return None, False


def _hazard_point(entity: EntitySnapshot, now: float) -> Vector3:
    """Where a slow thrown explosive will roughly be when it matters."""

    remaining = entity.detonate_at - now if entity.detonate_at > 0.0 else 0.4
    lead = max(0.0, min(0.4, remaining))
    return (entity.position[0] + entity.velocity[0] * lead,
            entity.position[1] + entity.velocity[1] * lead,
            entity.position[2])


def hazard_escape(world, frame: PerceptionFrame, observer: PlayerSnapshot,
                  profile: BotProfile, engagement: Engagement,
                  now: float) -> Vector3 | None:
    """Run from a grenade or lit charge that a player would notice.

    Only moving projectiles and the bot's own team's charges qualify; hidden
    enemy mines stay hidden. Casual profiles notice less often.
    """

    if now < engagement.evade_until:
        return engagement.evade_heading
    threat: tuple[float, EntitySnapshot, Vector3] | None = None
    # Projectiles are published after the static registry, so a slice would
    # hide every grenade on an entity-rich map. The frame is already bounded.
    for entity in frame.entities:
        if not entity.alive or not entity.hazardous or entity.blast_radius <= 0.0:
            continue
        projectile = entity.kind == "projectile"
        if not projectile and entity.team != observer.team:
            continue
        if not projectile and not 0.0 < entity.detonate_at - now <= 4.0:
            continue  # an unlit friendly mine is not an emergency
        if projectile and math.hypot(*entity.velocity) > 45.0:
            continue  # nobody sidesteps a rocket already in flight
        point = _hazard_point(entity, now)
        gap = math.dist(point, observer.position)
        if gap > entity.blast_radius + 3.0:
            continue
        if mix(observer.player_id, observer.life_id, entity.entity_id) > 0.3 + 0.75 * profile.skill:
            continue
        if threat is None or gap < threat[0]:
            threat = (gap, entity, point)
    if threat is None:
        return None
    _, entity, point = threat
    dx, dy = observer.position[0] - point[0], observer.position[1] - point[1]
    length = math.hypot(dx, dy)
    if length < 1e-3:
        angle = mix(observer.player_id, entity.entity_id, 3) * 2.0 * math.pi
        dx, dy, length = math.cos(angle), math.sin(angle), 1.0
    base = math.atan2(dy / length, dx / length)
    for turn in (0.0, 0.7, -0.7, 1.4, -1.4):
        heading = (math.cos(base + turn), math.sin(base + turn), 0.0)
        if walkable_heading(world, observer, heading, reach=3.0):
            engagement.evade_heading = heading
            engagement.evade_until = now + 0.6
            engagement.evade_entity = entity.entity_id
            return heading
    return None
