"""Server-owned mounted machine-gun entity (PlaceMG packet 87)."""
from __future__ import annotations

import time

import shared.constants as C
from shared.packet import ChangeEntity

from server.entities.behaviors import EntityBehavior


UNMOUNTED_PLAYER_ID = 0xFF
MOUNT_RADIUS = 3.0
MOUNT_BREAK_RADIUS = 4.0
MOUNT_INPUT_GRACE = 0.25


class MachineGunBehavior(EntityBehavior):
    """Health, ownership, and mount state for one placed machine gun."""

    takes_damage = True
    hit_radius = 1.5
    hit_center_offset = (0.0, 0.0, -0.75)

    def __init__(self, owner_id: int, team: int):
        self.owner_id = int(owner_id)
        self.team = int(team)
        self.health = float(C.MG_HEALTH)
        self.ammo = int(C.MG_AMMO)
        self.carrier_id = None
        self._mounted_at = 0.0

    def mount(self, ent, player, server) -> bool:
        if self.carrier_id is not None:
            return False
        self.carrier_id = int(player.id)
        self._mounted_at = time.time()
        ent.player_id = int(player.id)
        player.mounted_entity_id = int(ent.entity_id)
        _broadcast_player(server, ent.entity_id, player.id)
        return True

    def unmount(self, ent, server) -> bool:
        if self.carrier_id is None:
            return False
        player = getattr(server, "players", {}).get(self.carrier_id)
        if player is not None and getattr(player, "mounted_entity_id", None) == ent.entity_id:
            player.mounted_entity_id = None
        self.carrier_id = None
        ent.player_id = UNMOUNTED_PLAYER_ID
        _broadcast_player(server, ent.entity_id, UNMOUNTED_PLAYER_ID)
        return True

    def on_tick(self, ent, dt, ctx) -> None:
        if self.carrier_id is None or ctx.server is None:
            return
        player = ctx.server.players.get(self.carrier_id)
        if player is None or not player.alive or not player.spawned:
            self.unmount(ent, ctx.server)
            return
        dx, dy, dz = player.x - ent.x, player.y - ent.y, player.z - ent.z
        if dx * dx + dy * dy + dz * dz > MOUNT_BREAK_RADIUS ** 2:
            self.unmount(ent, ctx.server)
            return
        if ctx.now - self._mounted_at < MOUNT_INPUT_GRACE:
            return
        inp = getattr(player, "input", None)
        if inp is not None and any(bool(getattr(inp, name, False)) for name in (
            "up", "down", "left", "right", "jump", "crouch", "sneak", "sprint"
        )):
            self.unmount(ent, ctx.server)

    def on_damage(self, ent, amount, source, ctx) -> None:
        self.health -= max(0.0, float(amount))
        if self.health > 0.0 or not ent.alive:
            return
        if self.carrier_id is not None and ctx.server is not None:
            self.unmount(ent, ctx.server)
        # Blast routing also damages nearby entities. Mark this gun dead first
        # so its own explosion cannot re-enter on_damage recursively.
        ent.alive = False
        if ctx.server is not None:
            ctx.server._apply_blast(
                ent.x, ent.y, ent.z,
                float(C.MG_EXPLOSION_DAMAGE),
                float(C.MG_EXPLOSION_BLOCK_DAMAGE),
                int(C.ENTITY_KILL), source,
                crater_radius=1, force_destroy=True,
                blast_radius=float(C.MG_EXPLOSION_RADIUS),
                knockback_min=float(C.MG_EXPLOSION_KNOCKBACK_MIN),
                knockback_max=float(C.MG_EXPLOSION_KNOCKBACK_MAX),
            )
        if ctx.destroy is not None:
            ctx.destroy(ent.entity_id)
        registry = getattr(ctx.server, "entity_registry", None) if ctx.server else None
        if registry is not None:
            registry.remove(ent.entity_id)


def _broadcast_player(server, entity_id: int, player_id: int) -> None:
    packet = ChangeEntity()
    packet.entity_id = int(entity_id)
    packet.action = int(C.SET_PLAYER)
    packet.player_id = int(player_id)
    server.broadcast(bytes(packet.generate()), reliable=True)
