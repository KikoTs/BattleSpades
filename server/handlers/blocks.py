"""Voxel build, paint, and prefab packet handlers.

Protocol handlers only validate framing/state and dispatch to public gameplay
services.  Authoritative block and prefab logic is shared with server bots.
"""

from __future__ import annotations

import logging

from protocol.handler_registry import register_handler
from server.combat_runtime import get_combat_system
from server.ugc_capacity import ugc_capacity_full


logger = logging.getLogger(__name__)


def _break_disguise(player) -> None:
    """Building ends a Disguise ("Must remain stationary")."""
    breaker = getattr(player, "break_disguise", None)
    if callable(breaker):
        breaker()


@register_handler(7)  # PaintBlockPacket
async def handle_paint_block(server, player, packet):
    """Route native packet 7 through shared paint authorization/replication."""

    accepted = get_combat_system(server).handle_paint_packet(player, packet)
    if accepted:
        _ugc_single_paint_cue(server, player, packet)


def _ugc_single_paint_cue(server, player, packet) -> None:
    """Map Creator PAINT_PRIMARY_SOUND for a single-cell (LMB) paint.

    The RMB surface spray also arrives as packet 7 per cell; ClientData's
    held secondary bit (recorded by the movement handler) excludes it.
    """

    if not bool(getattr(getattr(server, "config", None), "ugc_runtime", False)):
        return
    if bool(getattr(player, "ugc_paint_secondary_held", False)):
        return
    cue = getattr(getattr(server, "mode", None), "on_single_paint", None)
    if not callable(cue):
        return
    try:
        cue(player, int(packet.x), int(packet.y), int(packet.z))
    except (AttributeError, TypeError, ValueError):
        return


@register_handler(32)  # BlockBuild
async def handle_block_build(server, player, packet):
    """Submit one ordinary block placement to combat authority."""

    if ugc_capacity_full(server):
        return
    if player.alive:
        if get_combat_system(server).handle_block_build(player, packet):
            _break_disguise(player)


@register_handler(35)  # BlockLiberate
async def handle_block_destroy(server, player, packet):
    """Reject BlockLiberate(35): the stock retail client never sends it.

    Evidence (2026-09-26): byte-grepping the stock Steam and non-Steam
    installs finds "BlockLiberate" only in ``shared.packet.pyd`` (the class)
    and ``aoslib.scenes.main.gameScene.pyd``, where IDA shows the global used
    solely by the incoming dispatch table (``process_packet_block_liberate``
    -> ``block_manager.liberate_block``). Unlike ShootPacket/BlockLine/
    BlockSuckerPacket, no send instance is ever built; digging travels as
    ShootPacket(6) (``diggingTool.py`` -> ``send_shoot_packet``). Packet 35
    therefore only comes from a modified client -- it used to delete any
    voxel within 19 blocks (block tool) or 13 blocks (spade) through walls,
    with refund. Outside the UGC runtime it is a protocol violation; the
    UGC editor keeps the reach/LOS-validated legacy path.
    """

    if not bool(getattr(getattr(server, "config", None), "ugc_runtime", False)):
        from server import anticheat

        anticheat.protocol_violation(server, player, "block_liberate")
        return
    if player.alive:
        get_combat_system(server).handle_block_destroy(player, packet)


@register_handler(40)  # BlockLine: retail 1.x ordinary placement path
async def handle_block_line(server, player, packet):
    """Submit a face-connected block line to combat authority."""

    if ugc_capacity_full(server):
        # Retail blockToolCommon.py:67-71 refuses every line with
        # BLOCK_PLACE_UGC_CAPACITY while the editor map is full.
        return
    if player.alive:
        if get_combat_system(server).handle_block_line(player, packet):
            _break_disguise(player)


@register_handler(30)  # BuildPrefabAction
async def handle_build_prefab(server, player, packet):
    """Delegate packet 30 to the shared authoritative prefab service."""

    from server.game_rules import get_rules
    if not get_rules(server.config).enabled("RULE_ENABLE_PREFABS"):
        return
    if ugc_capacity_full(server):
        # Retail ugcPrefabTool.py:361-391 gates construct placement too.
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
    # Retail competitive play has no erase action; packet 31 belongs to the
    # UGC Map Creator. Outside it, this used to delete an arbitrary prefab
    # footprint anywhere on the map with no range, cost, or cooldown.
    if not bool(getattr(server.config, "ugc_runtime", False)):
        return
    service = getattr(server, "prefab_actions", None)
    if service is not None:
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
