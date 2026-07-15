"""Deployable, mounted-entity, and equipment-action packet handlers.

Every placement validates the committed class, active loadout, and held tool
before mutating authoritative entity state. Coordinates remain literal retail
voxel shorts as decoded by protocol.runtime_packets.
"""

from __future__ import annotations

import logging
import time

from protocol.handler_registry import register_handler
from server.class_selection import deployable_authorized
from server.combat_runtime import get_combat_system

logger = logging.getLogger(__name__)


@register_handler(90)  # PlaceMedPack
async def handle_place_medpack(server, player, packet):
    """Place the visible Medic pack (25 HP per touch, three uses)."""
    return server.deployable_actions.place_medpack(
        player,
        (packet.x, packet.y, packet.z),
        face=int(getattr(packet, "face", 4)),
    )


def _deploy_pos(player, packet, max_distance: float = 15.0):
    """Common Place* validation: parse block coords, reject NaN/far placements
    (the client already range-checks; this guards against a bad/hostile
    packet). Returns (x, y, z) or None."""
    try:
        x, y, z = float(packet.x), float(packet.y), float(packet.z)
    except Exception:
        return None
    if any(v != v or abs(v) > 1e6 for v in (x, y, z)):
        return None
    dx, dy, dz = x - player.x, y - player.y, z - player.z
    if dx * dx + dy * dy + dz * dz > float(max_distance) ** 2:
        return None
    return (x, y, z)


@register_handler(104)  # PlaceFlareBlock
async def handle_place_flare_block(server, player, packet):
    """Place the stock coloured light block as entity type 13.

    Packet 104 is decoded by ``protocol.runtime_packets`` because its live
    coordinates are raw voxel shorts, not the fixed-point coordinates used by
    the generated loader.  The visible block is an entity, not a VXL mutation.
    """
    if not player.alive or not player.spawned:
        return

    import shared.constants as C
    tool = int(C.FLAREBLOCK_TOOL)
    if int(getattr(player, "tool", -1)) != tool:
        return
    if tool not in [int(value) for value in (getattr(player, "loadout", None) or [])]:
        return

    pos = _deploy_pos(player, packet, max_distance=float(C.MAX_BLOCK_DISTANCE))
    if pos is None:
        return
    # Live packet fields are integral voxels. Keep this check explicit so an
    # alternate/malformed packet object cannot create a fractional light.
    if any(value != int(value) for value in pos):
        return
    cell = tuple(int(value) for value in pos)
    x, y, z = cell
    if not (0 <= x < int(C.MAP_X) and 0 <= y < int(C.MAP_Y)
            and 0 <= z <= int(C.Z_ABOVE_WATERPLANE)):
        return

    registry = server.entity_registry
    world = server.world_manager
    from server.entities.flare_block import (
        FlareBlockBehavior, entity_occupies_cell, flare_is_supported,
    )
    if world.get_solid(x, y, z) or entity_occupies_cell(registry, cell):
        return
    if not flare_is_supported(world, registry, cell):
        return

    # Match the normal block tool's player-collision safety without applying
    # can_build(): flare blocks are explicitly allowed at the water plane.
    from server.prefabs import collides_with_player
    if collides_with_player([(cell, (0, 0, 0))], server.players.values()):
        return

    cost = int(C.FLAREBLOCK_COST)
    infinite = bool(getattr(server.teams.get(player.team), "infinite_blocks", False))
    if not infinite and int(getattr(player, "blocks", 0)) < cost:
        return

    packed_color = int(getattr(player, "block_color", 0)) & 0xFFFFFF
    color = (
        (packed_color >> 16) & 0xFF,
        (packed_color >> 8) & 0xFF,
        packed_color & 0xFF,
    )
    from server.connection import internal_team_to_wire
    ent = registry.place(
        int(C.FLARE_BLOCK), x, y, z,
        state=internal_team_to_wire(player.team),
        color=color,
        kind="flare_block",
        player_id=player.id,
        behavior=FlareBlockBehavior(),
    )
    if not infinite:
        player.blocks = max(0, int(player.blocks) - cost)
    server.broadcast_create_entity(ent)
    logger.info(
        "FLARE TOOL block id=%d placed by %s at %s color=%s cost=%d "
        "(tool=%d packet=104)",
        ent.entity_id, player.name, cell, color, 0 if infinite else cost,
        int(C.FLAREBLOCK_TOOL),
    )


@register_handler(1)  # PlaceDynamite
async def handle_place_dynamite(server, player, packet):
    """Miner dynamite: a timed charge that craters + damages on a 7s fuse."""
    return server.deployable_actions.place_dynamite(
        player, (packet.x, packet.y, packet.z)
    )


@register_handler(89)  # PlaceLandmine
async def handle_place_landmine(server, player, packet):
    """Scout landmine: arms, then detonates when an enemy walks near it."""
    return server.deployable_actions.place_landmine(
        player, (packet.x, packet.y, packet.z)
    )


@register_handler(92)  # PlaceC4
async def handle_place_c4(server, player, packet):
    """Miner remote charge: attach to any valid block face, then persist until
    this owner sends DetonateC4."""
    return server.deployable_actions.place_c4(
        player,
        (packet.x, packet.y, packet.z),
        face=int(getattr(packet, "face", -1)),
    )


@register_handler(93)  # DetonateC4
async def handle_detonate_c4(server, player, packet):
    """Detonate all live charges owned by this Miner."""
    return server.deployable_actions.detonate_c4(player)


@register_handler(91)  # PlaceRadarStation
async def handle_place_radar_station(server, player, packet):
    """Place the Scout radar station and expose enemies to that team until its
    stock lifetime expires."""
    return server.deployable_actions.place_radar(
        player, (packet.x, packet.y, packet.z)
    )


@register_handler(87)  # PlaceMG
async def handle_place_mg(server, player, packet):
    """Create the stock durable mounted machine-gun entity."""
    return server.deployable_actions.place_machine_gun(
        player,
        (packet.x, packet.y, packet.z),
        yaw=float(getattr(packet, "yaw", 0.0)),
    )


@register_handler(86)  # UseCommand
async def handle_use_command(server, player, packet):
    """Mount/dismount the nearest stock machine-gun entity."""
    if not player.alive or not player.spawned:
        return
    from server.entities.machine_gun import MachineGunBehavior, MOUNT_RADIUS

    mounted_id = getattr(player, "mounted_entity_id", None)
    mounted = server.entity_registry.get(mounted_id) if mounted_id is not None else None
    if mounted is not None and isinstance(mounted.behavior, MachineGunBehavior):
        mounted.behavior.unmount(mounted, server)
        return

    candidates = []
    for ent in server.entity_registry.all():
        if not ent.alive or not isinstance(ent.behavior, MachineGunBehavior):
            continue
        if ent.behavior.carrier_id is not None:
            continue
        dx, dy, dz = player.x - ent.x, player.y - ent.y, player.z - ent.z
        distance_sq = dx * dx + dy * dy + dz * dz
        if distance_sq <= MOUNT_RADIUS ** 2:
            candidates.append((distance_sq, ent.entity_id, ent))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        ent = candidates[0][2]
        ent.behavior.mount(ent, player, server)


@register_handler(88)  # PlaceRocketTurret
async def handle_place_rocket_turret(server, player, packet):
    """Place a server-owned Engineer/Rocketeer rocket turret."""
    return server.deployable_actions.place_rocket_turret(
        player,
        (packet.x, packet.y, packet.z),
        yaw=float(getattr(packet, "yaw", 0.0)),
    )


@register_handler(95)  # DisguisePacket
async def handle_disguise(server, player, packet):
    """Apply one stock Engineer disguise activation.

    The client starts with two uses and decrements locally before packet 95.
    WorldUpdate state bit 0x02 is the verified observer visual; keeping the
    matching server wallet prevents duplicate packets from granting unlimited
    disguises or consuming two charges for an already-active player.
    """
    return server.deployable_actions.set_disguise(
        player, active=bool(getattr(packet, "active", 0))
    )


@register_handler(94)  # BlockSuckerPacket
async def handle_block_sucker(server, player, packet):
    """Relay Blocksucker state and apply its authoritative voxel pull."""
    import shared.constants as C
    if not deployable_authorized(player, C.BLOCK_SUCKER_TOOL):
        return

    state = int(getattr(packet, "state", C.BLOCK_SUCKER_STATE_INACTIVE))
    if state not in (
        int(C.BLOCK_SUCKER_STATE_INACTIVE),
        int(C.BLOCK_SUCKER_STATE_WARMING_UP),
        int(C.BLOCK_SUCKER_STATE_FULL_POWER),
    ):
        return
    shot = bool(getattr(packet, "shot", 0))

    # Remote clients explicitly consume this packet to animate the warm-up,
    # loop sound, and debris. Never trust the packet's claimed shooter id.
    from shared.packet import BlockSuckerPacket
    out = BlockSuckerPacket()
    out.loop_count = int(getattr(server, "loop_count", 0))
    out.shooter_id = player.id
    out.state = state
    out.shot = int(shot)
    server.broadcast(bytes(out.generate()))

    if not shot or state != int(C.BLOCK_SUCKER_STATE_FULL_POWER):
        return
    now = time.monotonic()
    if now < float(getattr(player, "_block_sucker_next_shot", 0.0)):
        return
    player._block_sucker_next_shot = now + float(C.BLOCK_SUCKER_SHOOT_INTERVAL)

    direction = player.orientation
    hit = server.world_manager.raycast(
        player.eye[0], player.eye[1], player.eye[2],
        direction[0], direction[1], direction[2],
        float(C.BLOCK_SUCKER_RANGE),
    )
    if hit is None:
        return
    was_solid = server.world_manager.get_solid(*hit)
    get_combat_system(server)._apply_block_damage(
        player, hit, float(C.BLOCK_SUCKER_BLOCK_DAMAGE)
    )
    if was_solid and not server.world_manager.get_solid(*hit):
        player.add_blocks(1)
