"""Server-authoritative Engineer/Rocketeer rocket turret.

The stock client owns rendering only.  The server creates entity type 8,
streams ``entity_id -> (yaw, pitch)`` in WorldUpdate, chooses targets, and
spawns the same visible rocket entity used by the RPG projectile path.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import shared.constants as C
from server.connection import internal_team_to_wire
from server.entities.behaviors import EntityBehavior
from server.projectiles import ProjectileSpec


ROCKET_TURRET_INITIAL_STOCK = int(getattr(C, "ROCKET_TURRET_INITIAL_STOCK", 2))
ROCKET_TURRET_MAX_STOCK = int(getattr(C, "ROCKET_TURRET_STOCK", 4))
ROCKET_TURRET_AMMO = int(getattr(C, "ROCKET_TURRET_AMMO", 10))
ROCKET_TURRET_HEALTH = float(getattr(C, "ROCKET_TURRET_HEALTH", 100))
ROCKET_TURRET_DETECTION_RANGE = float(getattr(C, "ROCKET_TURRET_DETECTION_RANGE", 30.0))
ROCKET_TURRET_TRACKING_RANGE = float(getattr(C, "ROCKET_TURRET_TRACKING_RANGE", 50.0))
ROCKET_TURRET_AIMING_SPEED = float(getattr(C, "ROCKET_TURRET_AIMING_SPEED", 180.0))
ROCKET_TURRET_TOLERANCE = float(getattr(C, "ROCKET_TURRET_TOLERANCE", 0.1))
ROCKET_TURRET_SHOOT_INTERVAL = float(getattr(C, "ROCKET_TURRET_SHOOT_INTERVAL", 1.5))
ROCKET_TURRET_ROCKET_SPEED = float(getattr(C, "ROCKET_SPEED", 75.0))

ROCKET_TURRET_ROCKET_SPEC = ProjectileSpec(
    "rocket_turret_rocket",
    "contact",
    float(getattr(C, "ROCKET_GRAVITY_MULTIPLIER", 0.05)),
    float(getattr(C, "ROCKET_TURRET_ROCKET_EXPLOSION_DAMAGE", 50)),
    float(getattr(C, "ROCKET_TURRET_ROCKET_EXPLOSION_BLOCK_DAMAGE", 10)),
    int(getattr(C, "ROCKET_TURRET_KILL", 18)),
    int(getattr(C, "ROCKET_TURRET_ROCKET_DAMAGE", 21)),
    entity_type=int(getattr(C, "ROCKET_ENTITY", 21)),
    blast_radius=float(getattr(C, "ROCKET_TURRET_ROCKET_EXPLOSION_RADIUS", 3.0)),
    knockback_min=float(getattr(C, "ROCKET_TURRET_ROCKET_EXPLOSION_KNOCKBACK_MIN", 0.1)),
    knockback_max=float(getattr(C, "ROCKET_TURRET_ROCKET_EXPLOSION_KNOCKBACK_MAX", 0.3)),
)


def _approach_angle(current: float, target: float, amount: float) -> float:
    delta = (target - current + 180.0) % 360.0 - 180.0
    if abs(delta) <= amount:
        return target
    return current + math.copysign(amount, delta)


def _approach(current: float, target: float, amount: float) -> float:
    if abs(target - current) <= amount:
        return target
    return current + math.copysign(amount, target - current)


@dataclass
class RocketTurret:
    entity_id: int
    owner_id: int
    team: int
    x: float
    y: float
    z: float
    yaw: float
    pitch: float = 0.0
    health: float = ROCKET_TURRET_HEALTH
    ammo: int = ROCKET_TURRET_AMMO
    target_id: int | None = None
    next_shot_at: float = 0.0

    def world_update(self) -> tuple[int, float, float]:
        return (self.entity_id, self.yaw, self.pitch)


class RocketTurretBehavior(EntityBehavior):
    """Route ordinary hitscan/blast damage into the turret controller.

    The client renders health effects but never owns turret health. Keeping
    this adapter on the registry entity makes bullets, melee, and explosions
    use the same authoritative entity-hit path as medpacks, C4, and radar.
    """

    takes_damage = True
    hit_radius = 1.25
    hit_center_offset = (0.0, 0.0, -0.55)

    def __init__(self, controller: "RocketTurretController") -> None:
        self.controller = controller

    def on_damage(self, ent, amount, source, ctx) -> None:
        turret = self.controller.server.rocket_turrets.get(int(ent.entity_id))
        if turret is None or not ent.alive:
            return
        turret.health = max(0.0, turret.health - max(0.0, float(amount)))
        if turret.health > 0.0:
            return

        self._destroy(ent, source)

    def on_support_lost(self, ent, ctx) -> None:
        self._destroy(ent, None)

    def _destroy(self, ent, source) -> None:
        """Run the stock turret destruction blast and retire its controller."""

        if not ent.alive:
            return

        # Mark dead before applying the stock 100/15 destruction blast so it
        # cannot recursively damage itself through the registry snapshot.
        ent.alive = False
        server = self.controller.server
        server._apply_blast(
            ent.x,
            ent.y,
            ent.z,
            float(C.ROCKET_TURRET_EXPLOSION_DAMAGE),
            float(C.ROCKET_TURRET_EXPLOSION_BLOCK_DAMAGE),
            int(C.ENTITY_KILL),
            source,
            crater_radius=1,
            force_destroy=True,
            blast_radius=float(C.ROCKET_TURRET_EXPLOSION_RADIUS),
            knockback_min=float(C.ROCKET_TURRET_EXPLOSION_KNOCKBACK_MIN),
            knockback_max=float(C.ROCKET_TURRET_EXPLOSION_KNOCKBACK_MAX),
        )
        self.controller.remove(ent.entity_id)


class RocketTurretController:
    def __init__(self, server):
        self.server = server

    def place(
        self,
        player,
        position,
        yaw: float,
        now: float = 0.0,
        support_cell: tuple[int, int, int] | None = None,
    ):
        if int(getattr(player, "rocket_turret_stock", 0)) <= 0:
            return None
        x, y, z = (float(v) for v in position)
        ent = self.server.entity_registry.place(
            int(C.ROCKET_TURRET_ENTITY), x, y, z,
            state=internal_team_to_wire(player.team),
            kind="rocket_turret", player_id=player.id,
            behavior=RocketTurretBehavior(self),
            support_cell=support_cell,
        )
        turret = RocketTurret(
            entity_id=ent.entity_id,
            owner_id=player.id,
            team=player.team,
            x=x, y=y, z=z,
            yaw=float(yaw),
            next_shot_at=float(now) + ROCKET_TURRET_SHOOT_INTERVAL,
        )
        player.rocket_turret_stock -= 1
        self.server.rocket_turrets[turret.entity_id] = turret
        self.server.broadcast_create_entity(ent)
        self._broadcast_properties(turret)
        return turret

    def update(self, dt: float, now: float) -> None:
        for turret in list(self.server.rocket_turrets.values()):
            if turret.ammo <= 0:
                if turret.target_id is not None:
                    turret.target_id = None
                    self._broadcast_properties(turret)
                continue
            target = self._target_for(turret)
            target_id = None if target is None else int(target.id)
            if target_id != turret.target_id:
                turret.target_id = target_id
                self._broadcast_properties(turret)
            if target is None:
                continue

            origin = self._aim_origin(turret)
            target_point = self._target_point(target)
            dx = target_point[0] - origin[0]
            dy = target_point[1] - origin[1]
            dz = target_point[2] - origin[2]
            horizontal = math.hypot(dx, dy)
            desired_yaw = math.degrees(math.atan2(dx, dy))
            desired_pitch = max(-30.0, min(90.0, -math.degrees(math.atan2(dz, horizontal))))
            amount = ROCKET_TURRET_AIMING_SPEED * max(0.0, float(dt))
            turret.yaw = _approach_angle(turret.yaw, desired_yaw, amount)
            turret.pitch = _approach(turret.pitch, desired_pitch, amount)

            yaw_error = abs((desired_yaw - turret.yaw + 180.0) % 360.0 - 180.0)
            pitch_error = abs(desired_pitch - turret.pitch)
            if now < turret.next_shot_at or max(yaw_error, pitch_error) > ROCKET_TURRET_TOLERANCE:
                continue
            self._fire(turret, target, now)

    def remove_by_owner(self, owner_id: int) -> list[int]:
        """Remove turrets before their compact owner id can be reused.

        Turrets are autonomous projectile producers.  Leaving one alive after
        disconnect would transfer rocket collision exclusion and kill credit
        to an unrelated replacement player with the same wire id.
        """

        owner_id = int(owner_id)
        removed = []
        for entity_id, turret in list(self.server.rocket_turrets.items()):
            if int(turret.owner_id) != owner_id:
                continue
            self.remove(entity_id)
            removed.append(int(entity_id))
        return removed

    def remove(self, entity_id: int) -> bool:
        """Remove one controller/registry pair with create/destroy symmetry."""

        entity_id = int(entity_id)
        self.server.rocket_turrets.pop(entity_id, None)
        entity = self.server.entity_registry.remove(entity_id)
        # DestroyEntity is crash-sensitive in the retail client: never send it
        # for a controller row whose registry entity was already retired.
        if entity is None:
            return False
        self.server.broadcast_destroy_entity(entity_id)
        return True

    def _target_for(self, turret: RocketTurret):
        current = self.server.players.get(turret.target_id) if turret.target_id is not None else None
        if self._valid_target(turret, current, ROCKET_TURRET_TRACKING_RANGE):
            return current
        candidates = [
            player for player in self.server.players.values()
            if self._valid_target(turret, player, ROCKET_TURRET_DETECTION_RANGE)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: (p.x - turret.x) ** 2 + (p.y - turret.y) ** 2 + (p.z - turret.z) ** 2)

    def _valid_target(self, turret, player, radius: float) -> bool:
        if player is None or not player.alive or not player.spawned or player.team == turret.team:
            return False
        origin = self._aim_origin(turret)
        target = self._target_point(player)
        distance_sq = sum(
            (target[index] - origin[index]) ** 2 for index in range(3)
        )
        if distance_sq > radius * radius:
            return False
        blocked = getattr(self.server, "_blocked_los", None)
        return blocked is None or not blocked(
            origin[0], origin[1], origin[2],
            target[0], target[1], target[2],
        )

    def _fire(self, turret: RocketTurret, target, now: float) -> None:
        origin = self._aim_origin(turret)
        target_point = self._target_point(target)
        dx = target_point[0] - origin[0]
        dy = target_point[1] - origin[1]
        dz = target_point[2] - origin[2]
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length <= 1e-6:
            return
        direction = (dx / length, dy / length, dz / length)
        # Spawn one block along the barrel direction. Starting at the packet's
        # supporting voxel made level rockets intersect the floor/turret before
        # their first authoritative contact step.
        pos = tuple(origin[index] + direction[index] for index in range(3))
        vel = tuple(component * ROCKET_TURRET_ROCKET_SPEED for component in direction)
        projectile = self.server.projectile_engine.spawn_spec(
            ROCKET_TURRET_ROCKET_SPEC, pos, vel, turret.owner_id, now=now)
        if projectile is None:
            return
        owner = self.server.players.get(turret.owner_id)
        self.server.spawn_projectile_entity(projectile, owner, pos, vel)
        turret.ammo -= 1
        turret.next_shot_at = now + ROCKET_TURRET_SHOOT_INTERVAL
        self._broadcast_properties(turret)

    @staticmethod
    def _target_point(player) -> tuple[float, float, float]:
        """Aim at the replicated eye/character origin used by stock movement."""

        eye = getattr(player, "eye", None)
        if eye is not None:
            try:
                return tuple(float(value) for value in eye)
            except (TypeError, ValueError):
                pass
        return (float(player.x), float(player.y), float(player.z))

    @staticmethod
    def _aim_origin(turret: RocketTurret) -> tuple[float, float, float]:
        """Return the rendered gun pivot recovered from RocketTurret.set_position.

        The client shifts packet X/Y by -0.5 and, for an upward-facing entity,
        places the gun at ``z - (0.5 - GUN_MODEL_OFFSET_Z)``. Targeting and LOS
        must start there as well or a valid turret appears to aim/fire through
        its own supporting block.
        """

        return (
            float(turret.x) - 0.5,
            float(turret.y) - 0.5,
            float(turret.z) - (
                0.5 - float(C.ROCKET_TURRET_GUN_MODEL_OFFSET_Z)
            ),
        )

    def _broadcast_properties(self, turret) -> None:
        callback = getattr(self.server, "broadcast_turret_properties", None)
        if callback is not None:
            callback(turret)
