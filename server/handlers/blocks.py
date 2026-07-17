"""Voxel build, paint, and prefab packet handlers.

Protocol handlers only validate framing/state and dispatch to public gameplay
services.  Authoritative block and prefab logic is shared with server bots.
"""

from __future__ import annotations

import logging

from protocol.handler_registry import register_handler
from server.combat_runtime import get_combat_system


logger = logging.getLogger(__name__)


@register_handler(7)  # PaintBlockPacket
async def handle_paint_block(server, player, packet):
    """Route native packet 7 through shared paint authorization/replication."""

    get_combat_system(server).handle_paint_packet(player, packet)


@register_handler(32)  # BlockBuild
async def handle_block_build(server, player, packet):
    """Submit one ordinary block placement to combat authority."""

    if player.alive:
        get_combat_system(server).handle_block_build(player, packet)


@register_handler(35)  # BlockLiberate
async def handle_block_destroy(server, player, packet):
    """Submit one ordinary block-destruction request."""

    if player.alive:
        get_combat_system(server).handle_block_destroy(player, packet)


@register_handler(40)  # BlockLine: retail 1.x ordinary placement path
async def handle_block_line(server, player, packet):
    """Submit a face-connected block line to combat authority."""

    if player.alive:
        get_combat_system(server).handle_block_line(player, packet)


@register_handler(30)  # BuildPrefabAction
async def handle_build_prefab(server, player, packet):
    """Delegate packet 30 to the shared authoritative prefab service."""

    from server.game_rules import get_rules
    if not get_rules(server.config).enabled("RULE_ENABLE_PREFABS"):
        return
    service = getattr(server, "prefab_actions", None)
    if service is None:
        # Compatibility for focused embedders that do not instantiate the
        # complete BattleSpadesServer composition root.
        from server.prefab_actions import PrefabActionService

        service = PrefabActionService(server)
    service.place_packet(player, packet)


@register_handler(31)  # ErasePrefabAction
async def handle_erase_prefab(server, player, packet):
    """Erase the packet-selected prefab footprint through block authority."""

    from server.game_rules import get_rules
    if (
        not player.alive
        or not player.spawned
        or not get_rules(server.config).enabled("RULE_ENABLE_PREFABS")
    ):
        return
    service = getattr(server, "prefab_actions", None)
    if service is not None and bool(getattr(server.config, "ugc_runtime", False)):
        service.erase_packet(player, packet)
        return
    from server import prefabs

    name = str(getattr(packet, "prefab_name", "") or "")
    if service is not None and not service.authorized(player, name):
        return
    model = prefabs.get_registry().get(name) if name else None
    if model is None:
        return
    yaw = int(getattr(packet, "prefab_yaw", 0)) & 3
    pitch = int(getattr(packet, "prefab_pitch", 0)) & 3
    roll = int(getattr(packet, "prefab_roll", 0)) & 3
    raw_position = getattr(packet, "position", (0, 0, 0))
    try:
        position = tuple(int(round(float(value))) for value in raw_position[:3])
    except (IndexError, TypeError, ValueError):
        return
    if len(position) != 3:
        return

    cells = prefabs.expand_prefab(model, position, yaw, pitch, roll)
    targets = [
        coordinate
        for coordinate, _color in cells
        if server.world_manager.get_solid(
            int(coordinate[0]), int(coordinate[1]), int(coordinate[2])
        )
    ]
    if not targets:
        return
    destroyed = server.world_manager.destroy_blocks(targets)
    if destroyed:
        # Erasure has no separate public client action; this method only emits
        # the proven native Damage replication after WorldManager commits.
        get_combat_system(server)._broadcast_block_destroy(player, destroyed)
    logger.info(
        "PREFAB erase %s by %s at %s: removed %d blocks",
        name,
        player.name,
        position,
        len(destroyed or []),
    )
