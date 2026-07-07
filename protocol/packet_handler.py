"""
Packet handler - routes incoming packets to handlers.
Uses reversed shared packets for serialization.
"""

import logging
import time
from typing import Callable, Dict, TYPE_CHECKING

from shared.bytes import ByteReader
from shared.packet import CLIENT_LOADERS
from protocol.runtime_packets import decode_runtime_packet
from server.game_constants import KILL_TEAM_CHANGE
from server.combat_runtime import get_combat_system

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)

# Handler registry: packet_id -> handler_function
_handlers: Dict[int, Callable] = {}


def _get_input_flags(packet) -> int:
    raw_flags = getattr(packet, "input_flags", None)
    if raw_flags is not None:
        return int(raw_flags) & 0xFF

    flags = 0
    flags |= 0x01 if getattr(packet, "up", False) else 0
    flags |= 0x02 if getattr(packet, "down", False) else 0
    flags |= 0x04 if getattr(packet, "left", False) else 0
    flags |= 0x08 if getattr(packet, "right", False) else 0
    flags |= 0x10 if getattr(packet, "jump", False) else 0
    flags |= 0x20 if getattr(packet, "crouch", False) else 0
    flags |= 0x40 if getattr(packet, "sneak", False) else 0
    flags |= 0x80 if getattr(packet, "sprint", False) else 0
    return flags


def _get_action_flags(packet) -> int:
    raw_flags = getattr(packet, "action_flags", None)
    if raw_flags is not None:
        return int(raw_flags) & 0xFF

    flags = 0
    flags |= 0x01 if getattr(packet, "primary", False) else 0
    flags |= 0x02 if getattr(packet, "secondary", False) else 0
    flags |= 0x04 if getattr(packet, "zoom", False) else 0
    flags |= 0x08 if getattr(packet, "can_pickup", False) else 0
    flags |= 0x10 if getattr(packet, "can_display_weapon", False) else 0
    flags |= 0x20 if getattr(packet, "is_on_fire", False) else 0
    flags |= 0x40 if getattr(packet, "is_weapon_deployed", False) else 0
    flags |= 0x80 if getattr(packet, "hover", False) else 0
    return flags


def register_handler(packet_id: int):
    """Decorator to register a packet handler."""
    def decorator(func: Callable):
        _handlers[packet_id] = func
        return func
    return decorator


class PacketHandler:
    """Manages packet routing and handling."""
    
    def __init__(self, server: 'BattleSpadesServer'):
        self.server = server
    
    async def handle(self, player: 'Player', data: bytes):
        """Handle an incoming packet."""
        if len(data) < 1:
            return
        
        packet_id = data[0]
        # Note: RECV logging is done in connection.py::on_receive() with full hex + parsed fields
        
        # Get handler
        handler = _handlers.get(packet_id)
        if handler is None:
            logger.debug(f"Unhandled packet ID {packet_id} from {player.name}")
            return
        
        # Parse packet using aoslib
        packet_class = CLIENT_LOADERS.get(packet_id)
        if packet_class is None:
            logger.warning(f"Unknown packet ID {packet_id}")
            return
        
        try:
            payload = data[1:]
            packet = decode_runtime_packet(packet_id, payload)
            if packet is None:
                reader = ByteReader(payload)  # Skip packet ID byte
                packet = packet_class(reader)
            # Only log DECODE for non-suppressed packets
            if packet_id not in self.server.config.log_suppress_packets:
                logger.debug(f"DECODE [{player.name}] {packet_class.__name__}")
            await handler(self.server, player, packet)
        except Exception as e:
            logger.error(f"Error handling packet {packet_id}: {e}", exc_info=True)


async def handle_packet(server: 'BattleSpadesServer', player: 'Player', data: bytes):
    """Convenience function to handle a packet."""
    handler = PacketHandler(server)
    await handler.handle(player, data)


# =============================================================================
# Packet Handlers
# =============================================================================

@register_handler(4)  # ClientData
async def handle_client_data(server, player, packet):
    """Handle client input/orientation data.

    Movement inputs are recorded by the client's loop_count and consumed at
    the matching (delayed) simulation tick — see Player.apply_buffered_input.
    They are also applied immediately so non-movement systems (combat aim,
    tool state) see the freshest data.
    """
    previous_jump_held = player.jump_held
    previous_pending_jump = getattr(player, "pending_jump", False)
    flags = (
        packet.up,
        packet.down,
        packet.left,
        packet.right,
        packet.jump,
        packet.crouch,
        packet.sneak,
        packet.sprint,
    )
    player.record_input_frame(
        packet.loop_count,
        flags,
        (packet.o_x, packet.o_y, packet.o_z),
    )
    if packet.jump and player.connection is not None:
        logger.info("RAWJUMP %s sent jump=1 at client_loop=%s", player.name, packet.loop_count)
    # Sampled diagnostic: verify the client's loop_count stamps actually
    # align with our tick counter (the input buffer depends on it).
    if packet.loop_count % 120 == 0:
        logger.info(
            "ClientData stamp check: client_loop=%s server_loop=%s flags=%02X",
            packet.loop_count,
            getattr(server, "loop_count", -1),
            player.pack_input_flags(),
        )
    player.set_orientation_vector(packet.o_x, packet.o_y, packet.o_z)
    player.update_input(*flags)
    player.update_action_input(
        packet.primary,
        packet.secondary,
        packet.zoom,
        packet.can_pickup,
        packet.can_display_weapon,
        packet.is_on_fire,
        packet.is_weapon_deployed,
        packet.hover,
        packet.palette_enabled,
    )
    player.set_tool(packet.tool_id, raw=True)
    if logger.isEnabledFor(logging.DEBUG):
        jump_changed = previous_jump_held != player.jump_held
        pending_changed = previous_pending_jump != getattr(player, "pending_jump", False)
        if packet.jump or jump_changed or pending_changed:
            logger.debug(
                "ClientData jump trace for %s: input_flags=0x%02X action_flags=0x%02X "
                "parsed_jump=%s held=%s pending_before=%s pending_after=%s",
                player.name,
                _get_input_flags(packet),
                _get_action_flags(packet),
                packet.jump,
                player.jump_held,
                previous_pending_jump,
                getattr(player, "pending_jump", False),
            )


@register_handler(0)  # ClockSync
async def handle_clock_sync(server, player, packet):
    """Reply to client clock sync packets to keep the session alive."""
    if player.connection:
        player.connection.send_clock_sync_response(packet.client_time)


@register_handler(116)  # PositionData
async def handle_position_data(server, player, packet):
    """Handle position update from client."""
    reported_position = (packet.x, packet.y, packet.z)
    player.last_reported_position = reported_position
    player.last_position_update = time.time()
    dx = reported_position[0] - player.x
    dy = reported_position[1] - player.y
    dz = reported_position[2] - player.z
    player.last_position_drift_vector = (dx, dy, dz)
    player.last_position_drift = (dx * dx + dy * dy + dz * dz) ** 0.5


@register_handler(6)  # ShootPacket
async def handle_shoot(server, player, packet):
    """Handle shooting."""
    if not player.alive:
        return

    get_combat_system(server).handle_shot(player, packet)


@register_handler(32)  # BlockBuild
async def handle_block_build(server, player, packet):
    """Handle block placement."""
    if not player.alive:
        return

    get_combat_system(server).handle_block_build(player, packet)


@register_handler(35)  # BlockLiberate (destroy)
async def handle_block_destroy(server, player, packet):
    """Handle block destruction."""
    if not player.alive:
        return

    get_combat_system(server).handle_block_destroy(player, packet)


@register_handler(40)  # BlockLine — how the 1.x client PLACES blocks
async def handle_block_line(server, player, packet):
    """Handle block placement (the client sends BlockLine, never BlockBuild)."""
    if not player.alive:
        return

    get_combat_system(server).handle_block_line(player, packet)


@register_handler(10)  # UseOrientedItem — thrown grenades / RPG rockets
async def handle_oriented_item(server, player, packet):
    """A player threw a grenade (or fired an RPG). The client sends its own
    predicted position+velocity+fuse; we rebroadcast it so every OTHER client
    renders and simulates the projectile (arc + explosion FX + sound), and we
    register a server-authoritative grenade that applies blast damage and
    block destruction when the fuse expires."""
    if not player.alive or not player.spawned:
        return
    server.spawn_grenade(player, packet)


@register_handler(30)  # BuildPrefabAction
async def handle_build_prefab(server, player, packet):
    """A player placed a prefab. Faithful port of the original server's
    prefabManager.build_prefab: the client sends only NAME + anchor +
    quarter-turn rotations; the server expands the KV6 model into blocks
    (roll->pitch->yaw rotation, 50/50 team-color blend), validates (class
    allow-list, world contact, player collision, block budget), places the
    blocks, and broadcasts each as BlockBuildColored(33) + PrefabComplete(29)
    back to the builder."""
    if not player.alive or not player.spawned:
        return
    from server import prefabs as P

    name = str(getattr(packet, "prefab_name", "") or "")
    if not name:
        return
    if not P.prefab_allowed(player, name):
        logger.info("PREFAB rejected (not in class list): %s by %s", name, player.name)
        return
    model = P.get_registry().get(name)
    if model is None:
        return

    yaw = int(getattr(packet, "prefab_yaw", 0)) & 3
    pitch = int(getattr(packet, "prefab_pitch", 0)) & 3
    roll = int(getattr(packet, "prefab_roll", 0)) & 3
    position = getattr(packet, "position", None)
    if not position:
        return

    # Color: the packet carries the client's color choice; fall back to the
    # player's team color. Blended 50/50 with each voxel's model color.
    base_color = getattr(packet, "color", None)
    if not base_color or len(base_color) != 3:
        team = server.teams.get(player.team)
        base_color = tuple(getattr(team, "color", (128, 128, 128)))

    cells = P.expand_prefab(model, position, yaw, pitch, roll, base_color=base_color)

    # Budget: whole prefab must fit the player's block count.
    infinite = bool(getattr(server.teams.get(player.team), "infinite_blocks", False))
    if not infinite and len(cells) > int(getattr(player, "blocks", 0)):
        logger.info("PREFAB rejected (blocks %d > budget %d): %s by %s",
                    len(cells), player.blocks, name, player.name)
        return
    # Placement rules from the original: must touch the world, must not
    # entomb a player.
    if not P.touches_world(server.world_manager, cells):
        logger.info("PREFAB rejected (floating): %s by %s", name, player.name)
        return
    if P.collides_with_player(cells, server.players.values()):
        logger.info("PREFAB rejected (player collision): %s by %s", name, player.name)
        return

    from shared.packet import BlockBuild, BlockBuildColored, PrefabComplete
    placed = 0        # NEW cells only — the client charges a block per newly
    new_cells = 0     # added cell, so the server wallet must match that, not
    wm = server.world_manager  # the total (prefab bases overlap terrain).
    for (x, y, z), color in cells:
        if not (0 <= x < 512 and 0 <= y < 512 and 0 <= z < 256):
            continue
        was_solid = wm.get_solid(int(x), int(y), int(z))
        try:
            wm.set_block(int(x), int(y), int(z), solid=True, color=color)
        except Exception:
            continue
        if not was_solid:
            new_cells += 1
        # Spectators get the blended-color block; the BUILDER gets plain
        # BlockBuild(32) instead — the ONLY packet its client deducts a block
        # for (measured live 2026-07-07: 32 with own id -> block_count-1;
        # colored 33 deducts nothing). The server VXL keeps the blended color
        # so map syncs/new joiners see the true prefab.
        out = BlockBuildColored()
        out.loop_count = server.loop_count
        out.player_id = player.id
        out.x, out.y, out.z = int(x), int(y), int(z)
        # cdef int field: pack (r,g,b) as 0xRRGGBB (write_color unpacks it).
        out.color = (int(color[0]) << 16) | (int(color[1]) << 8) | int(color[2])
        server.broadcast(bytes(out.generate()), reliable=True, exclude=player)
        own = BlockBuild()
        own.loop_count = server.loop_count
        own.player_id = player.id
        own.x, own.y, own.z = int(x), int(y), int(z)
        own.block_type = 0
        player.send(bytes(own.generate()), reliable=True)
        placed += 1

    if new_cells and not infinite:
        player.blocks = max(0, int(player.blocks) - new_cells)

    done = PrefabComplete()
    player.send(bytes(done.generate()), reliable=True)
    logger.info("PREFAB %s by %s at %s yaw=%d: placed %d/%d blocks",
                name, player.name, tuple(position), yaw, placed, len(cells))


@register_handler(31)  # ErasePrefabAction
async def handle_erase_prefab(server, player, packet):
    """Carve/undo a placed prefab: expand the same model at the same anchor +
    rotation and destroy those exact cells (broadcast through the verified
    Damage(37) block-destroy path, falling chunks included)."""
    if not player.alive or not player.spawned:
        return
    from server import prefabs as P

    name = str(getattr(packet, "prefab_name", "") or "")
    model = P.get_registry().get(name) if name else None
    if model is None:
        return
    yaw = int(getattr(packet, "prefab_yaw", 0)) & 3
    pitch = int(getattr(packet, "prefab_pitch", 0)) & 3
    roll = int(getattr(packet, "prefab_roll", 0)) & 3
    position = (int(getattr(packet, "x", 0)), int(getattr(packet, "y", 0)),
                int(getattr(packet, "z", 0)))

    cells = P.expand_prefab(model, position, yaw, pitch, roll)
    targets = [c for (c, _color) in cells
               if server.world_manager.get_solid(int(c[0]), int(c[1]), int(c[2]))]
    if not targets:
        return
    destroyed = server.world_manager.destroy_blocks(targets)
    if destroyed:
        get_combat_system(server)._broadcast_block_destroy(player, destroyed)
    logger.info("PREFAB erase %s by %s at %s: removed %d blocks",
                name, player.name, position, len(destroyed or []))


@register_handler(13)  # SetClassLoadout (mid-game)
async def handle_set_class_loadout(server, player, packet):
    """The player picked a new class/loadout from the in-game menu. Original
    semantics: it applies at the NEXT SPAWN (the current life is unchanged).
    The pre-join copy of this packet is handled in the connection handshake;
    this handler covers changes while already playing."""
    player.pending_class_id = int(getattr(packet, "class_id", player.class_id))
    loadout = list(getattr(packet, "loadout", []) or [])
    if loadout:
        player.pending_loadout = loadout
    prefabs = list(getattr(packet, "prefabs", []) or [])
    if prefabs:
        player.prefabs = prefabs
    logger.info("LOADOUT %s -> class=%d loadout=%s (applies at respawn)",
                player.name, player.pending_class_id, loadout)


@register_handler(90)  # PlaceMedPack
async def handle_place_medpack(server, player, packet):
    """A medic placed a medpack. Register a server-side heal entity (touch =
    heal teammates, limited uses). NOT broadcast as a wire entity yet — the
    medpack's CreateEntity type id is unverified against the compiled client
    (an unknown type/state crashes it natively)."""
    if not player.alive or not player.spawned:
        return
    import shared.constants as C
    from server.entities.behaviors import MedpackBehavior
    from server.game_constants import TEAM_NEUTRAL

    x, y, z = float(packet.x), float(packet.y), float(packet.z)
    # Sanity: must be near the placer (client validates placement radius 5).
    dx, dy, dz = x - player.x, y - player.y, z - player.z
    if dx * dx + dy * dy + dz * dz > 100.0:
        return
    server.entity_registry.place(
        int(getattr(C, "HEALTH_DROP_POINT_ENTITY", 19)), x, y, z,
        state=TEAM_NEUTRAL, kind="medpack",
        behavior=MedpackBehavior(team=player.team),
    )
    logger.info("MEDPACK placed by %s at (%.1f,%.1f,%.1f)", player.name, x, y, z)


@register_handler(95)  # DisguisePacket
async def handle_disguise(server, player, packet):
    """Specialist toggled disguise. Tracked server-side; the visual (rendering
    as the enemy team on other clients) needs the wire mechanism verified —
    the packet carries no player_id, so it can't simply be rebroadcast."""
    player.disguised = bool(getattr(packet, "active", 0))
    logger.info("DISGUISE %s -> %s", player.name, player.disguised)


@register_handler(49)  # ChatMessage
async def handle_chat(server, player, packet):
    """Handle chat messages."""
    if player.muted:
        return
    
    message = packet.value
    
    # Check for commands
    if message.startswith('/'):
        from commands import handle_command
        await handle_command(server, player, message[1:])
        return
    
    # Broadcast chat
    from shared.packet import ChatMessage
    broadcast_packet = ChatMessage()
    broadcast_packet.player_id = player.id
    broadcast_packet.chat_type = packet.chat_type
    broadcast_packet.value = message
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(77)  # ChangeTeam
async def handle_change_team(server, player, packet):
    """Handle team change request."""
    from server.connection import wire_team_to_internal

    wire_team = packet.team
    new_team = wire_team_to_internal(wire_team)
    if new_team is None:
        logger.debug(
            "Ignoring ChangeTeam from %s for non-playable/unknown wire team %s",
            player.name,
            wire_team,
        )
        return

    if new_team == player.team:
        return
    
    # Remove from old team
    if player.team in server.teams:
        server.teams[player.team].remove_player(player)
    
    # Add to new team
    player.team = new_team
    if new_team in server.teams:
        server.teams[new_team].add_player(player)
    
    # Kill to respawn
    if player.alive:
        player.die(kill_type=KILL_TEAM_CHANGE)


@register_handler(78)  # ChangeClass
async def handle_change_class(server, player, packet):
    """Handle class change request."""
    player.class_id = packet.class_id


@register_handler(11)  # SetColor
async def handle_set_color(server, player, packet):
    """Handle color change."""
    player.set_color(packet.value)
    
    # Broadcast
    from shared.packet import SetColor
    broadcast_packet = SetColor()
    broadcast_packet.player_id = player.id
    broadcast_packet.value = packet.value
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(110)  # ClientInMenu
async def handle_client_in_menu(server, player, packet):
    """Track whether the client is currently in a menu."""
    if player.connection:
        player.connection.in_menu = bool(packet.in_menu)


@register_handler(241)  # DebugParityToggle
async def handle_debug_parity_toggle(server, player, packet):
    if getattr(server, 'debug_parity', None) is not None:
        server.debug_parity.handle_toggle(player, packet)


@register_handler(242)  # DebugClientSample
async def handle_debug_client_sample(server, player, packet):
    if getattr(server, 'debug_parity', None) is not None:
        server.debug_parity.handle_client_sample(player, packet)


@register_handler(243)  # DebugClientEvent
async def handle_debug_client_event(server, player, packet):
    if getattr(server, 'debug_parity', None) is not None:
        server.debug_parity.handle_client_event(player, packet)


@register_handler(76)  # WeaponReload
async def handle_weapon_reload(server, player, packet):
    """Handle weapon reload."""
    if not player.alive:
        return

    get_combat_system(server).handle_weapon_reload(player)
