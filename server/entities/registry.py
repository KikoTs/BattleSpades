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
from typing import List, Optional, Tuple

from shared.packet import Entity

from server.game_constants import TEAM_NEUTRAL

# Entity sits flat on the ground, default +Z up face.
_FACE_UP = 4


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

    def to_wire_entity(self) -> Entity:
        ent = Entity()
        ent.entity_id = int(self.entity_id)
        ent.pos_x, ent.pos_y, ent.pos_z = float(self.x), float(self.y), float(self.z)
        ent.vel_x = ent.vel_y = ent.vel_z = 0.0
        ent.yaw = ent.radius = ent.fuse = 0.0
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
              player_id: int = 0) -> MapEntity:
        ent = MapEntity(
            entity_id=self.allocate_id(), type=int(type),
            x=float(x), y=float(y), z=float(z),
            state=int(state), color=color, kind=kind, player_id=int(player_id),
            home=(float(x), float(y), float(z)),
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
        """Entities to include in the StateData join snapshot (alive ones)."""
        return [e for e in self._entities.values() if e.alive]

    def clear(self) -> None:
        self._entities.clear()
        self._next_id = 0

    def due_respawns(self, now: Optional[float] = None) -> List[MapEntity]:
        """Dead entities whose respawn timer has elapsed (caller re-creates)."""
        if now is None:
            now = time.time()
        return [e for e in self._entities.values()
                if not e.alive and e.respawn_at > 0.0 and now >= e.respawn_at]
