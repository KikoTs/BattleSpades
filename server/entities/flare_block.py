"""Server-owned flare/light block entity (PlaceFlareBlock packet 104)."""
from __future__ import annotations

import shared.constants as C

from server.entities.behaviors import DamageableEntityBehavior


_NEIGHBORS = (
    (-1, 0, 0), (1, 0, 0),
    (0, -1, 0), (0, 1, 0),
    (0, 0, -1), (0, 0, 1),
)


def flare_cell(ent):
    return (int(ent.x), int(ent.y), int(ent.z))


def entity_occupies_cell(registry, cell, *, ignore_entity_id=None) -> bool:
    """Return whether a live, static entity already owns this voxel cell."""
    x, y, z = cell
    for ent in registry.all():
        if not ent.alive or ent.kind == "projectile":
            continue
        if ignore_entity_id is not None and ent.entity_id == ignore_entity_id:
            continue
        if (int(ent.x), int(ent.y), int(ent.z)) == (x, y, z):
            return True
    return False


def flare_is_supported(world, registry, cell, *, ignore_entity_id=None) -> bool:
    """Return whether a flare component has a path to real terrain.

    Water placement remains legal: at the retail water plane (z=238), the
    engine's solid waterbed at z=239 satisfies this same contact rule.  This is
    deliberately independent of ``WorldManager.can_build``, whose ordinary
    voxel rules reject water placement.

    Merely touching another flare is insufficient: after the only terrain
    anchor is destroyed, a connected flare group must not support itself in a
    cycle and remain floating forever.
    """
    flares_by_cell = {
        flare_cell(ent): ent
        for ent in registry.all()
        if ent.alive
        and ent.kind == "flare_block"
        and (
            ignore_entity_id is None
            or int(ent.entity_id) != int(ignore_entity_id)
        )
    }
    pending = [tuple(int(value) for value in cell)]
    visited = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        x, y, z = current
        for dx, dy, dz in _NEIGHBORS:
            neighbor = (x + dx, y + dy, z + dz)
            if world.get_solid(*neighbor):
                return True
            if neighbor in flares_by_cell and neighbor not in visited:
                pending.append(neighbor)
    return False


class FlareBlockBehavior(DamageableEntityBehavior):
    """Damage and support lifecycle for one visible flare block.

    Despawning is broadcast through DestroyEntity.  The stock client's
    FlareBlockEntity.delete path owns removal of the associated point light,
    so using the normal entity lifecycle also fixes lighting cleanup.
    """

    hit_radius = 0.75
    hit_center_offset = (0.5, 0.5, 0.5)

    def __init__(self):
        super().__init__(float(C.DEFAULT_BLOCK_HEALTH))

    def on_tick(self, ent, dt, ctx) -> None:
        if ctx.world is None or ctx.server is None:
            return
        registry = ctx.server.entity_registry
        if not flare_is_supported(
            ctx.world, registry, flare_cell(ent), ignore_entity_id=ent.entity_id
        ):
            self.on_destroyed(ent, None, ctx)
