"""Authoritative Molotov block-fire and player burning state.

The retail constants describe two distinct effects: the Molotov's immediate
impact blast and persistent ``BLOCKFIRE`` entities.  This controller owns the
latter so fire damage, spread, expiry, and the WorldUpdate ``is_on_fire`` bit
remain deterministic on the server.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
import time
from typing import Optional

import shared.constants as C


@dataclass
class _BlockFire:
    entity_id: int
    block: tuple[int, int, int]
    owner_id: int
    expires_at: float
    next_character_damage: float
    next_block_damage: float
    next_spread: float
    cluster_id: int
    spread_pending: bool = True


@dataclass
class _FireCluster:
    """A single Molotov's bounded, shared spread budget.

    ``BLOCKFIRE_SPREAD_COUNT`` belongs to the impact as a whole.  Giving the
    full count to every child creates a supercritical tree (five initial
    fires, each producing children that each receive five more attempts).
    Besides being unlike the retail effect, that eventually floods clean
    clients with particle entities.  Members therefore consume this one
    shared budget and each member receives at most one attempt.
    """

    cluster_id: int
    spreads_left: int
    members: int = 0


@dataclass
class _BurningPlayer:
    owner_id: int
    expires_at: float
    next_damage: float
    fractional_damage: float = 0.0


class FireController:
    """Own visible block-fire entities and damage-over-time state."""

    # Retail clients render each BLOCKFIRE as a persistent particle emitter.
    # Keep a hard safety ceiling even when many Gangsters throw at once; old
    # fire naturally expires after four seconds and new impact fire replaces
    # the oldest emitter so player feedback is never silently lost.
    MAX_ACTIVE_BLOCK_FIRES = 96

    def __init__(self, server, rng: Optional[random.Random] = None):
        self.server = server
        self.rng = rng if rng is not None else random.Random()
        self.block_fires: dict[int, _BlockFire] = {}
        self.burning_players: dict[int, _BurningPlayer] = {}
        self._burning_blocks: dict[tuple[int, int, int], int] = {}
        self._clusters: dict[int, _FireCluster] = {}
        self._next_cluster_id = 1

    def clear(self) -> None:
        """Discard runtime state when the world/round is replaced."""
        for player in self.server.players.values():
            player.on_fire = False
        self.block_fires.clear()
        self.burning_players.clear()
        self._burning_blocks.clear()
        self._clusters.clear()

    def forget_player(self, player_id: int) -> None:
        """Remove fire whose target or damage owner is disconnecting.

        Both mappings use compact player ids.  Allowing either state to
        survive an id reuse can ignite or credit damage to the replacement.
        Visible block-fire entities are destroyed through their normal wire
        lifecycle; affected character fire bits are cleared immediately.
        """

        player_id = int(player_id)
        for state in list(self.block_fires.values()):
            if state.owner_id == player_id:
                self._remove_block_fire(state)

        affected_targets = [
            target_id
            for target_id, state in self.burning_players.items()
            if target_id == player_id or state.owner_id == player_id
        ]
        for target_id in affected_targets:
            self.extinguish_player(target_id)

    def ignite_impact(self, x: float, y: float, z: float, owner,
                      now: Optional[float] = None) -> list[int]:
        """Light exposed blocks around a Molotov impact.

        The client constants specify an initial radius of two and five spread
        slots.  Nearest exposed cells are selected first so identical impacts
        produce the same authoritative layout on every client.
        """
        if owner is None:
            return []
        if now is None:
            now = time.time()
        radius = int(getattr(C, "BLOCKFIRE_INITIAL_SPREAD_RADIUS", 2))
        limit = max(1, int(getattr(C, "BLOCKFIRE_SPREAD_COUNT", 5)))
        bx, by, bz = math.floor(x), math.floor(y), math.floor(z)
        candidates: list[tuple[float, tuple[int, int, int]]] = []
        world = self.server.world_manager
        for cx in range(bx - radius, bx + radius + 1):
            for cy in range(by - radius, by + radius + 1):
                for cz in range(bz - 1, bz + 2):
                    if not (0 <= cx < 512 and 0 <= cy < 512 and 0 <= cz <= 238):
                        continue
                    if not world.get_solid(cx, cy, cz):
                        continue
                    # Molotovs can strike walls. The old top-face-only test
                    # rejected every voxel in a vertical wall and created no
                    # visible fire at all.
                    if not self._exposed_faces((cx, cy, cz)):
                        continue
                    distance = (cx + 0.5 - x) ** 2 + (cy + 0.5 - y) ** 2 + (cz - z) ** 2
                    if distance <= float(radius + 1) ** 2:
                        candidates.append((distance, (cx, cy, cz)))
        candidates.sort(key=lambda item: (item[0], item[1]))
        cluster_id = self._new_cluster()
        entity_ids = [
            entity_id
            for _distance, block in candidates[:limit]
            if (
                entity_id := self.ignite_block(
                    block,
                    owner,
                    now=now,
                    _cluster_id=cluster_id,
                    _replace_oldest=True,
                )
            ) is not None
        ]
        if not entity_ids:
            self._clusters.pop(cluster_id, None)
        return entity_ids

    def ignite_block(self, block: tuple[int, int, int], owner,
                     now: Optional[float] = None, *,
                     _cluster_id: Optional[int] = None,
                     _replace_oldest: bool = True) -> Optional[int]:
        """Create one replicated BLOCKFIRE entity unless already burning."""
        block = tuple(int(value) for value in block)
        if block in self._burning_blocks:
            return None
        if now is None:
            now = time.time()
        if not self.server.world_manager.get_solid(*block):
            return None

        if len(self.block_fires) >= self.MAX_ACTIVE_BLOCK_FIRES:
            if not _replace_oldest:
                return None
            oldest = min(
                self.block_fires.values(),
                key=lambda state: (state.expires_at, state.entity_id),
            )
            self._remove_block_fire(oldest)

        if _cluster_id is None:
            _cluster_id = self._new_cluster()
        cluster = self._clusters.get(_cluster_id)
        if cluster is None:
            # A spread may have lost its last member to the global cap between
            # candidate selection and creation. Recreate only for direct/new
            # impacts; stale child spreads must not resurrect old clusters.
            if not _replace_oldest:
                return None
            self._clusters[_cluster_id] = cluster = _FireCluster(
                cluster_id=_cluster_id,
                spreads_left=int(getattr(C, "BLOCKFIRE_SPREAD_COUNT", 5)),
            )

        from server.connection import internal_team_to_wire

        lifespan = float(getattr(C, "BLOCKFIRE_MAX_LIFESPAN", 4.0))
        anchor, _surface_face = self._surface_anchor(block)
        ent = self.server.entity_registry.place(
            int(getattr(C, "BLOCKFIRE", 28)),
            *anchor,
            state=internal_team_to_wire(owner.team),
            kind="blockfire",
            player_id=owner.id,
            fuse=lifespan,
            # Base Entity.set_face rotates faces 0, 1, 2, 3 and 5. Retail
            # BlockFireEntity is particle-only and deliberately has no
            # ``model`` attribute, so any of those values tears the GameScene
            # down in Entity.rotate. FACE_TOP (4) is the sole no-rotation
            # value. The side/top placement still comes from ``anchor``.
            face=int(C.FACE_TOP),
        )
        self.server.broadcast_create_entity(ent)
        self.block_fires[ent.entity_id] = _BlockFire(
            entity_id=ent.entity_id,
            block=block,
            owner_id=owner.id,
            expires_at=now + lifespan,
            next_character_damage=now,
            next_block_damage=now + float(
                getattr(C, "BLOCKFIRE_BLOCK_DAMAGE_TIMER", 0.4)
            ),
            next_spread=now + float(getattr(C, "BLOCKFIRE_SPREAD_TIMER", 0.5)),
            cluster_id=_cluster_id,
        )
        cluster.members += 1
        self._burning_blocks[block] = ent.entity_id
        return ent.entity_id

    def _new_cluster(self) -> int:
        """Allocate one wrap-safe Molotov spread group."""

        cluster_id = self._next_cluster_id
        self._next_cluster_id += 1
        self._clusters[cluster_id] = _FireCluster(
            cluster_id=cluster_id,
            spreads_left=max(0, int(getattr(C, "BLOCKFIRE_SPREAD_COUNT", 5))),
        )
        return cluster_id

    def _exposed_faces(self, block: tuple[int, int, int]):
        """Return wire face/anchor pairs for every air-facing voxel side."""
        x, y, z = block
        world = self.server.world_manager
        faces = (
            (int(C.FACE_TOP), (0, 0, -1), (x + 0.5, y + 0.5, z - 0.01)),
            (int(C.FACE_RIGHT), (1, 0, 0), (x + 1.01, y + 0.5, z + 0.5)),
            (int(C.FACE_LEFT), (-1, 0, 0), (x - 0.01, y + 0.5, z + 0.5)),
            (int(C.FACE_FRONT), (0, 1, 0), (x + 0.5, y + 1.01, z + 0.5)),
            (int(C.FACE_BACK), (0, -1, 0), (x + 0.5, y - 0.01, z + 0.5)),
            (int(C.FACE_BOTTOM), (0, 0, 1), (x + 0.5, y + 0.5, z + 1.01)),
        )
        return [
            (face, anchor)
            for face, (dx, dy, dz), anchor in faces
            if not world.get_solid(x + dx, y + dy, z + dz)
        ]

    def _surface_anchor(self, block: tuple[int, int, int]):
        """Choose a stable visible face, preferring the conventional top."""
        faces = self._exposed_faces(block)
        if faces:
            return faces[0][1], faces[0][0]
        x, y, z = block
        return (x + 0.5, y + 0.5, z - 0.01), int(C.FACE_TOP)

    def ignite_player(self, player, owner_id: int,
                      now: Optional[float] = None) -> None:
        """Start or refresh the retail ten-second character burn."""
        if not player.alive or not player.spawned:
            return
        if now is None:
            now = time.time()
        duration = float(getattr(C, "BLOCKFIRE_CHARACTER_DURATION", 10.0))
        interval = float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE_TIMER", 0.3))
        state = self.burning_players.get(player.id)
        if state is None:
            self.burning_players[player.id] = _BurningPlayer(
                owner_id=int(owner_id),
                expires_at=now + duration,
                next_damage=now + interval,
            )
        else:
            state.owner_id = int(owner_id)
            state.expires_at = max(state.expires_at, now + duration)
        player.on_fire = True

    def extinguish_player(self, player_id: int) -> None:
        self.burning_players.pop(int(player_id), None)
        player = self.server.players.get(int(player_id))
        if player is not None:
            player.on_fire = False

    def update(self, now: Optional[float] = None) -> None:
        """Advance block and character fire using absolute timers."""
        if now is None:
            now = time.time()
        self._update_block_fires(now)
        self._update_burning_players(now)

    def _update_block_fires(self, now: float) -> None:
        char_range = float(getattr(C, "BLOCKFIRE_CHARACTER_SPREAD_RANGE", 3.0))
        char_interval = float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE_TIMER", 0.3))
        block_interval = float(getattr(C, "BLOCKFIRE_BLOCK_DAMAGE_TIMER", 0.4))
        block_damage = float(getattr(C, "BLOCKFIRE_BLOCK_DAMAGE", 0.7))
        for state in list(self.block_fires.values()):
            owner = self.server.players.get(state.owner_id)
            if now >= state.expires_at or not self.server.world_manager.get_solid(*state.block):
                self._remove_block_fire(state)
                continue

            if now >= state.next_character_damage:
                state.next_character_damage = now + char_interval
                ex, ey, ez = state.block
                for player in self.server.players.values():
                    if not player.alive or not player.spawned:
                        continue
                    dx, dy, dz = player.x - ex, player.y - ey, player.z - ez
                    if dx * dx + dy * dy + dz * dz <= char_range * char_range:
                        self.ignite_player(player, state.owner_id, now=now)

            if now >= state.next_block_damage:
                state.next_block_damage = now + block_interval
                if owner is not None:
                    from server.combat_runtime import get_combat_system
                    get_combat_system(self.server)._apply_block_damage(
                        owner,
                        state.block,
                        block_damage,
                        damage_type=int(getattr(C, "BLOCKFIRE_DAMAGE", 25)),
                        causer_id=state.entity_id,
                    )

            cluster = self._clusters.get(state.cluster_id)
            if (
                state.spread_pending
                and cluster is not None
                and cluster.spreads_left > 0
                and now >= state.next_spread
            ):
                # The count is an impact-wide attempt budget. Consume it even
                # if the random roll finds no target, exactly once per member.
                state.spread_pending = False
                cluster.spreads_left -= 1
                if owner is not None:
                    self._spread_one(state, owner, now)

    def _spread_one(self, state: _BlockFire, owner, now: float) -> None:
        radius = int(math.ceil(float(getattr(C, "BLOCKFIRE_SPREAD_RADIUS", 2.0))))
        chance = float(getattr(C, "BLOCKFIRE_MAX_RANDOM_CHANCE", 0.3))
        x, y, z = state.block
        candidates = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if dx == 0 and dy == 0:
                    continue
                if dx * dx + dy * dy > radius * radius:
                    continue
                block = (x + dx, y + dy, z)
                if block in self._burning_blocks:
                    continue
                if (
                    self.server.world_manager.get_solid(*block)
                    and self._exposed_faces(block)
                ):
                    candidates.append(block)
        if candidates and self.rng.random() <= chance:
            self.ignite_block(
                self.rng.choice(candidates),
                owner,
                now=now,
                _cluster_id=state.cluster_id,
                _replace_oldest=False,
            )

    def _update_burning_players(self, now: float) -> None:
        interval = float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE_TIMER", 0.3))
        damage = float(getattr(C, "BLOCKFIRE_CHARACTER_DAMAGE", 2.5))
        kill_type = int(getattr(C.KILL, "BLOCKFIRE_KILL", 25))
        for player_id, state in list(self.burning_players.items()):
            player = self.server.players.get(player_id)
            if (
                player is None
                or not player.alive
                or not player.spawned
                or bool(getattr(player, "wade", False))
                or now >= state.expires_at
            ):
                self.extinguish_player(player_id)
                continue
            while now >= state.next_damage and player.alive:
                state.next_damage += interval
                state.fractional_damage += damage
                whole_damage = int(state.fractional_damage)
                state.fractional_damage -= whole_damage
                if whole_damage:
                    owner = self.server.players.get(state.owner_id)
                    player.damage(whole_damage, source=owner, kill_type=kill_type)

    def _remove_block_fire(self, state: _BlockFire) -> None:
        self.block_fires.pop(state.entity_id, None)
        self._burning_blocks.pop(state.block, None)
        cluster = self._clusters.get(state.cluster_id)
        if cluster is not None:
            cluster.members -= 1
            if cluster.members <= 0:
                self._clusters.pop(state.cluster_id, None)
        if self.server.entity_registry.remove(state.entity_id) is not None:
            self.server.broadcast_destroy_entity(state.entity_id)
