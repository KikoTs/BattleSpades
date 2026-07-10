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
ROCKET_TURRET_ROCKET_SPEED = 75.0

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


class RocketTurretController:
    def __init__(self, server):
        self.server = server

    def place(self, player, position, yaw: float, now: float = 0.0):
        if int(getattr(player, "rocket_turret_stock", 0)) <= 0:
            return None
        x, y, z = (float(v) for v in position)
        ent = self.server.entity_registry.place(
            int(C.ROCKET_TURRET_ENTITY), x, y, z,
            state=internal_team_to_wire(player.team),
            kind="rocket_turret", player_id=player.id,
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

            dx, dy, dz = target.x - turret.x, target.y - turret.y, target.z - turret.z
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
        distance_sq = ((player.x - turret.x) ** 2 + (player.y - turret.y) ** 2 +
                       (player.z - turret.z) ** 2)
        if distance_sq > radius * radius:
            return False
        blocked = getattr(self.server, "_blocked_los", None)
        return blocked is None or not blocked(
            turret.x, turret.y, turret.z, player.x, player.y, player.z
        )

    def _fire(self, turret: RocketTurret, target, now: float) -> None:
        dx, dy, dz = target.x - turret.x, target.y - turret.y, target.z - turret.z
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        if length <= 1e-6:
            return
        direction = (dx / length, dy / length, dz / length)
        pos = (turret.x + direction[0], turret.y + direction[1], turret.z + direction[2])
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

    def _broadcast_properties(self, turret) -> None:
        callback = getattr(self.server, "broadcast_turret_properties", None)
        if callback is not None:
            callback(turret)
