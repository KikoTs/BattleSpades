"""Specialist Chemical Bomb goo: dissolving, damaging surface patches.

Retail evidence (stock Steam client, headless IDA 2026-09-26; see
docs/WEAPONS_RETAIL.md, "Molotov fire and Chemical Bomb goo"):

* ``CHEMICALBOMB_TOOL_DESCRIPTION``: "Dissolves blocks over time / Create
  damaging Goo".
* ``GameScene.ENTITIES[31]`` is ``BlockGooEntity`` (blockgooEntity.py), a
  clone of ``BlockFireEntity``: a fused, lit surface patch whose colour ramps
  white -> (20,255,50) -> green (A2432..A2434) over ``fuse`` divided by
  A1764 (= ``BLOCKFIRE_MAX_LIFESPAN`` 4.0). It has no model, no particles
  and no sound of its own; the server creates and destroys it.
* Damage(37) type 43 (A417) is ``BlockManager.handle_chemical_bomb_damage``:
  a single-block handler; ``on_single_block_damaged`` spawns the chemical
  debris particles for it. That is the "dissolve" terrain channel.
* WorldUpdate post-action state bit 0x08 feeds ``Character.set_touching_goo``
  (``process_packet_world_update``): it starts the chemical burn loop sound
  (A2920) on observers and ends it with A2921, or A2922 in water.
* The shared BlockManager precomputes a radius list for
  ``CHEMICALBOMB_EXPLOSION_RADIUS`` (A1666 = 3) exactly as it does for the
  Molotov's ``BLOCKFIRE_INITIAL_SPREAD_RADIUS``; the client never reads it,
  so it is the server's goo footprint.

What the retail server used for the goo's own timers is not in any client
binary. Because the client's goo is literally the block-fire entity with a
green palette (it even reads the block-fire lifespan for its colour ramp),
the goo reuses the ``BLOCKFIRE_*`` lifespan and damage cadence, without the
fire's spreading or ten-second after-burn: goo hurts only while touched.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Optional

import shared.constants as C

# SetHP.damage_type 3: stock burn branch (burn sound + BURN_INDICATOR flash).
GOO_HP_DAMAGE_TYPE = 3
# Damage(37) type -> BlockManager.handle_chemical_bomb_damage (A417 = 43).
CHEMICAL_BOMB_BLOCK_DAMAGE_TYPE = 43
# WorldUpdate post-action state bit consumed by Character.set_touching_goo.
TOUCHING_GOO_STATE_BIT = 0x08

MAX_DAMAGEABLE_Z = 238
# Player hull used for the contact test (native half-width 0.45).
_HULL_HALF_WIDTH = 0.45
_HEAD_ABOVE_EYE = 0.5
_CONTACT_MARGIN = 0.1


def goo_radius() -> int:
    return int(round(float(getattr(C, "CHEMICALBOMB_EXPLOSION_RADIUS", 3.0))))


def goo_lifespan() -> float:
    return float(getattr(C, "BLOCKFIRE_MAX_LIFESPAN", 4.0))


def goo_block_damage() -> float:
    return float(getattr(C, "BLOCKFIRE_BLOCK_DAMAGE", 0.7))


def goo_block_interval() -> float:
    return float(getattr(C, "BLOCKFIRE_BLOCK_DAMAGE_TIMER", 0.4))


def goo_character_damage() -> float:
    return float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE", 2.5))


def goo_character_interval() -> float:
    return float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE_TIMER", 0.3))


@dataclass
class _Goo:
    entity_id: int
    block: tuple[int, int, int]
    owner_id: int
    expires_at: float
    next_block_damage: float


@dataclass
class _GooContact:
    owner_id: int
    next_damage: float
    fractional_damage: float = 0.0


class ChemicalGooController:
    """Own Chemical Bomb goo entities, terrain dissolve and contact damage."""

    # BlockGooEntity adds one static point light per patch on every client.
    MAX_GOO_PER_BOMB = 48
    MAX_ACTIVE_GOO = 192

    def __init__(self, server):
        self.server = server
        self.goo: dict[int, _Goo] = {}
        self._goo_blocks: dict[tuple[int, int, int], int] = {}
        self.contacts: dict[int, _GooContact] = {}

    # -- lifecycle ---------------------------------------------------------
    def clear(self) -> None:
        for player in self.server.players.values():
            player.touching_goo = False
        self.goo.clear()
        self._goo_blocks.clear()
        self.contacts.clear()

    def forget_player(self, player_id: int) -> None:
        player_id = int(player_id)
        for state in list(self.goo.values()):
            if state.owner_id == player_id:
                self._remove(state)
        for target_id, contact in list(self.contacts.items()):
            if target_id == player_id or contact.owner_id == player_id:
                self._stop_contact(target_id)

    # -- creation ------------------------------------------------------------
    def splash(self, x: float, y: float, z: float, owner,
               now: Optional[float] = None) -> list[int]:
        """Coat every exposed solid voxel within the goo radius of an impact.

        Nearest cells first so a capped splash keeps its centre.
        """
        if owner is None:
            return []
        if now is None:
            now = time.time()
        from server.projectiles import radius_damage_offsets

        world = self.server.world_manager
        cx, cy, cz = (int(math.floor(float(v))) for v in (x, y, z))
        candidates = []
        for dx, dy, dz in radius_damage_offsets(goo_radius()):
            block = (cx + dx, cy + dy, cz + dz)
            bx, by, bz = block
            if not (0 <= bx < 512 and 0 <= by < 512 and 0 <= bz <= MAX_DAMAGEABLE_Z):
                continue
            if block in self._goo_blocks or not world.get_solid(*block):
                continue
            if not self._exposed(block):
                continue
            distance = (bx + 0.5 - x) ** 2 + (by + 0.5 - y) ** 2 + (bz + 0.5 - z) ** 2
            candidates.append((distance, block))
        candidates.sort()
        created = []
        for _distance, block in candidates[: self.MAX_GOO_PER_BOMB]:
            entity_id = self.coat_block(block, owner, now=now)
            if entity_id is not None:
                created.append(entity_id)
        return created

    def coat_block(self, block, owner, now: Optional[float] = None, *,
                   lifespan: Optional[float] = None) -> Optional[int]:
        block = tuple(int(v) for v in block)
        if block in self._goo_blocks:
            return None
        if not self.server.world_manager.get_solid(*block):
            return None
        if now is None:
            now = time.time()
        if len(self.goo) >= self.MAX_ACTIVE_GOO:
            oldest = min(self.goo.values(), key=lambda s: (s.expires_at, s.entity_id))
            self._remove(oldest)
        life = goo_lifespan() if lifespan is None else max(0.05, float(lifespan))
        from server.connection import internal_team_to_wire

        ent = self.server.entity_registry.place(
            int(getattr(C, "BLOCK_GOO_ENTITY", 31)),
            *self._anchor(block),
            state=internal_team_to_wire(owner.team),
            kind="blockgoo",
            player_id=owner.id,
            fuse=life,
            # BlockGooEntity, like BlockFireEntity, has no model: any face
            # that Entity.set_face rotates would crash the GameScene.
            face=int(C.FACE_TOP),
        )
        self.server.broadcast_create_entity(ent)
        self.goo[ent.entity_id] = _Goo(
            entity_id=ent.entity_id,
            block=block,
            owner_id=int(owner.id),
            expires_at=now + life,
            next_block_damage=now + goo_block_interval(),
        )
        self._goo_blocks[block] = ent.entity_id
        return ent.entity_id

    def _exposed(self, block) -> bool:
        x, y, z = block
        world = self.server.world_manager
        return any(
            not world.get_solid(x + dx, y + dy, z + dz)
            for dx, dy, dz in (
                (0, 0, -1), (1, 0, 0), (-1, 0, 0),
                (0, 1, 0), (0, -1, 0), (0, 0, 1),
            )
        )

    def _anchor(self, block):
        """Top face when open, otherwise the first open side (as fire)."""
        x, y, z = block
        world = self.server.world_manager
        for (dx, dy, dz), anchor in (
            ((0, 0, -1), (x + 0.5, y + 0.5, z - 0.01)),
            ((1, 0, 0), (x + 1.01, y + 0.5, z + 0.5)),
            ((-1, 0, 0), (x - 0.01, y + 0.5, z + 0.5)),
            ((0, 1, 0), (x + 0.5, y + 1.01, z + 0.5)),
            ((0, -1, 0), (x + 0.5, y - 0.01, z + 0.5)),
            ((0, 0, 1), (x + 0.5, y + 0.5, z + 1.01)),
        ):
            if not world.get_solid(x + dx, y + dy, z + dz):
                return anchor
        return (x + 0.5, y + 0.5, z - 0.01)

    # -- per tick --------------------------------------------------------------
    def update(self, now: Optional[float] = None) -> None:
        if not self.goo and not self.contacts:
            return
        if now is None:
            now = time.time()
        self._update_goo(now)
        self._update_contacts(now)

    def _update_goo(self, now: float) -> None:
        world = self.server.world_manager
        interval = goo_block_interval()
        amount = goo_block_damage()
        for state in list(self.goo.values()):
            if now >= state.expires_at:
                self._remove(state)
                continue
            if not world.get_solid(*state.block):
                # The goo ate through its voxel: it drops onto the one below
                # (BLOCKFIRE_MAX_FALLING_DISTANCE) and keeps dissolving.
                owner = self.server.players.get(state.owner_id)
                self._remove(state)
                below = (state.block[0], state.block[1], state.block[2] + 1)
                if owner is not None and below[2] <= MAX_DAMAGEABLE_Z:
                    self.coat_block(
                        below, owner, now=now,
                        lifespan=state.expires_at - now,
                    )
                continue
            entity = self.server.entity_registry.get(state.entity_id)
            if entity is not None:
                entity.fuse = max(0.0, state.expires_at - now)
            if now >= state.next_block_damage:
                state.next_block_damage = now + interval
                owner = self.server.players.get(state.owner_id)
                if owner is not None:
                    from server.combat_runtime import get_combat_system

                    get_combat_system(self.server)._apply_block_damage(
                        owner,
                        state.block,
                        amount,
                        damage_type=CHEMICAL_BOMB_BLOCK_DAMAGE_TYPE,
                        causer_id=state.entity_id,
                    )

    def touching_owner(self, player) -> Optional[int]:
        """Owner id of a goo patch ``player``'s hull touches, else None."""
        if not self._goo_blocks:
            return None
        px, py, pz = float(player.x), float(player.y), float(player.z)
        contact = getattr(player, "_current_contact_offset", None)
        try:
            feet = pz + (float(contact()) if callable(contact) else 2.25)
        except Exception:
            feet = pz + 2.25
        reach = _HULL_HALF_WIDTH + _CONTACT_MARGIN
        x0, x1 = px - reach, px + reach
        y0, y1 = py - reach, py + reach
        z0, z1 = pz - _HEAD_ABOVE_EYE, feet + _CONTACT_MARGIN
        for bx in range(int(math.floor(x0)), int(math.floor(x1)) + 1):
            for by in range(int(math.floor(y0)), int(math.floor(y1)) + 1):
                for bz in range(int(math.floor(z0)), int(math.floor(z1)) + 1):
                    entity_id = self._goo_blocks.get((bx, by, bz))
                    if entity_id is None:
                        continue
                    state = self.goo.get(entity_id)
                    if state is not None:
                        return state.owner_id
        return None

    def _friendly_protected(self, player, owner_id: int) -> bool:
        if int(getattr(player, "id", -1)) == int(owner_id):
            return False
        if bool(getattr(getattr(self.server, "config", None), "friendly_fire", False)):
            return False
        owner = self.server.players.get(int(owner_id))
        return owner is not None and getattr(owner, "team", None) == getattr(player, "team", None)

    def _update_contacts(self, now: float) -> None:
        interval = goo_character_interval()
        damage = goo_character_damage()
        kill_type = int(getattr(C.KILL, "CHEMICALBOMB_KILL", 31))
        for player in list(self.server.players.values()):
            owner_id = None
            if player.alive and player.spawned:
                owner_id = self.touching_owner(player)
                if owner_id is not None and self._friendly_protected(player, owner_id):
                    owner_id = None
            contact = self.contacts.get(player.id)
            if owner_id is None:
                if contact is not None:
                    self._stop_contact(player.id)
                continue
            if contact is None:
                # Contact damages at once, then on the block-fire cadence.
                contact = self.contacts[player.id] = _GooContact(
                    owner_id=owner_id, next_damage=now,
                )
            contact.owner_id = owner_id
            player.touching_goo = True
            while now >= contact.next_damage and player.alive:
                contact.next_damage += interval
                contact.fractional_damage += damage
                whole = int(contact.fractional_damage)
                contact.fractional_damage -= whole
                if whole:
                    player.damage(
                        whole,
                        source=self.server.players.get(contact.owner_id),
                        kill_type=kill_type,
                        hp_damage_type=GOO_HP_DAMAGE_TYPE,
                    )
            if not player.alive:
                self._stop_contact(player.id)

    def _stop_contact(self, player_id: int) -> None:
        self.contacts.pop(int(player_id), None)
        player = self.server.players.get(int(player_id))
        if player is not None:
            player.touching_goo = False

    def _remove(self, state: _Goo) -> None:
        self.goo.pop(state.entity_id, None)
        if self._goo_blocks.get(state.block) == state.entity_id:
            self._goo_blocks.pop(state.block, None)
        if self.server.entity_registry.remove(state.entity_id) is not None:
            self.server.broadcast_destroy_entity(state.entity_id)
