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

import math
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from shared.packet import Entity

from server.game_constants import TEAM_NEUTRAL
from server.entities.behaviors import EntityBehavior

# Entity sits flat on the ground, default +Z up face.
_FACE_UP = 4
_TOUCH_BUCKET_SIZE = 8.0


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
    move: Optional[Callable] = None                # ChangeEntity.SET_POSITION


@dataclass
class MapEntity:
    entity_id: int
    type: int
    x: float
    y: float
    z: float
    yaw: float = 0.0
    state: int = TEAM_NEUTRAL          # team index (TEAM_NEUTRAL for crates)
    color: Optional[Tuple[int, int, int]] = None
    kind: str = "crate"                # ammo | health | block | intel | flag | base
    player_id: int = 0                 # carrier (0 = none)
    face: int = _FACE_UP               # attachment face (C4/dynamite)
    # Runtime (pickup/respawn) bookkeeping — not all entities use these.
    alive: bool = True
    respawn_at: float = 0.0
    home: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Moving entities (projectiles): initial velocity + collision radius the
    # client uses to simulate flight locally. Static entities leave these 0.
    vel: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    radius: float = 0.0
    fuse: float = 0.0
    # Optional server-side behavior (Phase-2). Runtime only — NEVER serialized
    # (to_wire_entity does not read it, so the wire output stays byte-identical).
    behavior: Optional[EntityBehavior] = None
    # Some legacy objective types (notably FLAG=0 and BASE=1) are valid
    # server-side concepts but are absent from the retail GameScene.ENTITIES
    # table.  Sending either through packet 21 makes the native client index
    # that table with an unsupported key and freeze with KeyError.  Keep such
    # markers in the registry for mode logic, but never serialize them as a
    # runtime CreateEntity.
    wire_visible: bool = True
    # Runtime-only terrain attachment for map pickups. Official sidecars may
    # place a crate one to three voxels above (or at) its support voxel, so we
    # retain that authored offset when a broken structure makes it fall.
    terrain_support_z: Optional[int] = None
    terrain_offset_z: float = 0.0
    terrain_check_at: float = 0.0

    def to_wire_entity(self) -> Entity:
        ent = Entity()
        ent.entity_id = int(self.entity_id)
        ent.pos_x, ent.pos_y, ent.pos_z = float(self.x), float(self.y), float(self.z)
        ent.vel_x, ent.vel_y, ent.vel_z = (float(v) for v in self.vel)
        ent.yaw = float(self.yaw)
        ent.radius = float(self.radius)
        ent.fuse = float(self.fuse)
        ent.color = self.color
        ent.type = int(self.type)
        ent.player_id = int(self.player_id)
        ent.state = int(self.state)
        ent.face = int(self.face)
        ent.ugc_mode = 0
        ent.float_properties = []
        ent.int_properties = []
        return ent


class EntityRegistry:
    """Owns placed map entities and a monotonic id allocator."""

    def __init__(self):
        self._entities: dict[int, MapEntity] = {}
        self._next_id: int = 0
        self._tick_cursor: int = 0

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
              radius: float = 0.0, face: int = _FACE_UP,
              fuse: float = 0.0, yaw: float = 0.0,
              wire_visible: bool = True) -> MapEntity:
        ent = MapEntity(
            entity_id=self.allocate_id(), type=int(type),
            x=float(x), y=float(y), z=float(z),
            yaw=float(yaw),
            state=int(state), color=color, kind=kind, player_id=int(player_id),
            face=int(face),
            home=(float(x), float(y), float(z)), behavior=behavior,
            vel=tuple(float(v) for v in vel), radius=float(radius),
            fuse=float(fuse), wire_visible=bool(wire_visible),
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
                if e.alive and e.kind != "projectile" and e.wire_visible]

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

    def tick(self, ctx: EntityContext, max_on_tick: int | None = None) -> int:
        """Advance all entity behaviors for one frame: on_tick, proximity
        touch, then due respawns. Replaces the ad-hoc crate polling that used
        to live in the server loop. Pure computation + broadcast callbacks —
        no I/O, safe to call synchronously in the 60 Hz loop. Returns the
        count of eligible on_tick behaviors deferred by the optional cap."""
        # 1. per-entity on_tick (only alive entities whose behavior overrides
        # it). Optional caps degrade overload as round-robin skipped ticks
        # instead of a single frame hitch.
        tickers = [
            ent for ent in self._entities.values()
            if ent.alive
            and ent.behavior is not None
            and type(ent.behavior).on_tick is not EntityBehavior.on_tick
        ]
        skipped = 0
        if max_on_tick is not None and len(tickers) > int(max_on_tick):
            limit = int(max_on_tick)
            skipped = len(tickers) - limit
            start = self._tick_cursor % len(tickers)
            ordered = tickers[start:] + tickers[:start]
            tickers = ordered[:limit]
            self._tick_cursor = (start + limit) % len(ordered)
        else:
            self._tick_cursor = 0

        for ent in list(tickers):
            b = ent.behavior
            if ent.alive and b is not None:
                b.on_tick(ent, ctx.dt, ctx)

        # 2. proximity touch (alive entities with a touch radius vs alive players)
        touchers = [e for e in self._entities.values()
                    if e.alive and e.behavior is not None and e.behavior.touch_radius > 0.0]
        if touchers and ctx.players:
            buckets = self._bucket_touchers(touchers)
            max_radius = max(e.behavior.touch_radius for e in touchers)
            bucket_range = max(1, int(math.ceil(
                max_radius / _TOUCH_BUCKET_SIZE
            )))
            for player in ctx.players:
                for ent in self._nearby_touchers(
                    buckets, player.x, player.y, bucket_range
                ):
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
        return skipped

    @staticmethod
    def _bucket_touchers(
        touchers: List[MapEntity],
    ) -> dict[tuple[int, int], list[MapEntity]]:
        """Build a transient 2D index for proximity-driven entities.

        Touch entities are currently static pickups/mines, but rebuilding the
        small index each tick also remains correct if a future behavior moves.
        It replaces the previous O(players × all entities) scan.
        """
        buckets: dict[tuple[int, int], list[MapEntity]] = {}
        for entity in touchers:
            key = (
                math.floor(entity.x / _TOUCH_BUCKET_SIZE),
                math.floor(entity.y / _TOUCH_BUCKET_SIZE),
            )
            buckets.setdefault(key, []).append(entity)
        return buckets

    @staticmethod
    def _nearby_touchers(
        buckets: dict[tuple[int, int], list[MapEntity]],
        x: float,
        y: float,
        bucket_range: int,
    ):
        """Yield only touchers in buckets capable of reaching one player."""
        center_x = math.floor(x / _TOUCH_BUCKET_SIZE)
        center_y = math.floor(y / _TOUCH_BUCKET_SIZE)
        for offset_x in range(-bucket_range, bucket_range + 1):
            for offset_y in range(-bucket_range, bucket_range + 1):
                yield from buckets.get(
                    (center_x + offset_x, center_y + offset_y), ()
                )

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
