"""Server-side map entities (ammo/health/block crates, intel, flags, ...).

The wire format already exists (shared.packet.Entity / CreateEntity(21) /
DestroyEntity(19)); this is the missing server model: a registry of placed
entities + a monotonic uint16 id allocator. Static entities (crates) are sent
once in StateData at join and via CreateEntity at spawn/respawn — they are NOT
streamed through the 60Hz WorldUpdate (that feed is reserved for moving
entities, to avoid loop bloat).

CRITICAL wire-safety (a bad field crashes the compiled client natively, the same
class as the pickup=0xFF bug): `state` must be a valid team index — TEAM_NEUTRAL
for crates, or internal_team_to_wire(team) for team objects; never a raw 0xFF.
`type` must be a known ENTITY id. `entity_id` must be a unique uint16 (the client
ignores a CreateEntity whose id already exists).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from shared.packet import Entity

from server.game_constants import TEAM_NEUTRAL
from server.entities.behaviors import EntityBehavior

# Entity sits flat on the ground, default +Z up face.
_FACE_UP = 4


@dataclass
class EntityContext:
    """Per-tick context handed to entity behaviors. Built once per frame in the
    server loop so behaviors never touch sockets/players directly."""
    dt: float
    now: float
    players: list                                  # alive + spawned players
    world: object = None                           # world_manager
    server: object = None
    create: Optional[Callable] = None              # broadcast_create_entity
    destroy: Optional[Callable] = None             # broadcast_destroy_entity


@dataclass
class MapEntity:
    entity_id: int
    type: int
    x: float
    y: float
    z: float
    state: int = TEAM_NEUTRAL          # team index (TEAM_NEUTRAL for crates)
    color: Optional[Tuple[int, int, int]] = None
    kind: str = "crate"                # ammo | health | block | intel | flag | base
    player_id: int = 0                 # carrier (0 = none)
    # Runtime (pickup/respawn) bookkeeping — not all entities use these.
    alive: bool = True
    respawn_at: float = 0.0
    home: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Moving entities (projectiles): initial velocity + collision radius the
    # client uses to simulate flight locally. Static entities leave these 0.
    vel: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius: float = 0.0
    # Optional server-side behavior (Phase-2). Runtime only — NEVER serialized
    # (to_wire_entity does not read it, so the wire output stays byte-identical).
    behavior: Optional[EntityBehavior] = None

    def to_wire_entity(self) -> Entity:
        ent = Entity()
        ent.entity_id = int(self.entity_id)
        ent.pos_x, ent.pos_y, ent.pos_z = float(self.x), float(self.y), float(self.z)
        ent.vel_x, ent.vel_y, ent.vel_z = (float(v) for v in self.vel)
        ent.yaw = 0.0
        ent.radius = float(self.radius)
        ent.fuse = 0.0
        ent.color = self.color
        ent.type = int(self.type)
        ent.player_id = int(self.player_id)
        ent.state = int(self.state)
        ent.face = _FACE_UP
        ent.ugc_mode = 0
        ent.float_properties = []
        ent.int_properties = []
        return ent


class EntityRegistry:
    """Owns placed map entities and a monotonic id allocator."""

    def __init__(self):
        self._entities: dict[int, MapEntity] = {}
        self._next_id: int = 0

    def allocate_id(self) -> int:
        """Monotonic uint16 id (wraps at 65536, skipping live ids)."""
        for _ in range(65536):
            eid = self._next_id
            self._next_id = (self._next_id + 1) & 0xFFFF
            if eid not in self._entities:
                return eid
        raise RuntimeError("entity id space exhausted")

    def place(self, type: int, x: float, y: float, z: float, *,
              state: int = TEAM_NEUTRAL, color=None, kind: str = "crate",
              player_id: int = 0, behavior: Optional[EntityBehavior] = None,
              vel: Tuple[float, float, float] = (0.0, 0.0, 0.0),
              radius: float = 0.0) -> MapEntity:
        ent = MapEntity(
            entity_id=self.allocate_id(), type=int(type),
            x=float(x), y=float(y), z=float(z),
            state=int(state), color=color, kind=kind, player_id=int(player_id),
            home=(float(x), float(y), float(z)), behavior=behavior,
            vel=tuple(float(v) for v in vel), radius=float(radius),
        )
        self._entities[ent.entity_id] = ent
        return ent

    def remove(self, entity_id: int) -> Optional[MapEntity]:
        return self._entities.pop(int(entity_id), None)

    def get(self, entity_id: int) -> Optional[MapEntity]:
        return self._entities.get(int(entity_id))

    def all(self) -> List[MapEntity]:
        return list(self._entities.values())

    def static_entities(self) -> List[MapEntity]:
        """Entities to include in a joining client's snapshot (alive, static).
        Short-lived moving projectiles are excluded — a mid-flight rocket
        streamed as a static join entity would render frozen/stale."""
        return [e for e in self._entities.values()
                if e.alive and e.kind != "projectile"]

    def clear(self) -> None:
        self._entities.clear()
        self._next_id = 0

    def due_respawns(self, now: Optional[float] = None) -> List[MapEntity]:
        """Dead entities whose respawn timer has elapsed (caller re-creates)."""
        if now is None:
            now = time.time()
        return [e for e in self._entities.values()
                if not e.alive and e.respawn_at > 0.0 and now >= e.respawn_at]

    # ------------------------------------------------------------------
    # Per-tick behavior driver (Phase-2)
    # ------------------------------------------------------------------

    def tick(self, ctx: EntityContext) -> None:
        """Advance all entity behaviors for one frame: on_tick, proximity
        touch, then due respawns. Replaces the ad-hoc crate polling that used
        to live in the server loop. Pure computation + broadcast callbacks —
        no I/O, safe to call synchronously in the 60 Hz loop."""
        # 1. per-entity on_tick (only alive entities whose behavior overrides it)
        for ent in list(self._entities.values()):
            b = ent.behavior
            if ent.alive and b is not None and type(b).on_tick is not EntityBehavior.on_tick:
                b.on_tick(ent, ctx.dt, ctx)

        # 2. proximity touch (alive entities with a touch radius vs alive players)
        touchers = [e for e in self._entities.values()
                    if e.alive and e.behavior is not None and e.behavior.touch_radius > 0.0]
        if touchers and ctx.players:
            for player in ctx.players:
                for ent in touchers:
                    if not ent.alive:          # a prior touch this tick despawned it
                        continue
                    r = ent.behavior.touch_radius
                    dx = player.x - ent.x
                    dy = player.y - ent.y
                    dz = player.z - ent.z
                    if (dx * dx + dy * dy + dz * dz) <= r * r:
                        ent.behavior.on_touch(ent, player, ctx)

        # 3. respawns (re-create entities whose timer elapsed)
        for ent in self.due_respawns(ctx.now):
            ent.alive = True
            ent.respawn_at = 0.0
            if ctx.create is not None:
                ctx.create(ent)

    def damage_entity(self, entity_id: int, amount: float, source, ctx: EntityContext) -> None:
        """Route damage to an entity's behavior (deployables, diggable graves,
        ...). No-op unless the entity is alive and its behavior takes damage."""
        ent = self._entities.get(int(entity_id))
        if ent is None or not ent.alive:
            return
        b = ent.behavior
        if b is None or not b.takes_damage:
            return
        b.on_damage(ent, amount, source, ctx)
