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
    """Handle client input/orientation data."""
    previous_jump_held = player.jump_held
    previous_pending_jump = getattr(player, "pending_jump", False)
    player.set_orientation_vector(packet.o_x, packet.o_y, packet.o_z)
    player.update_input(
        packet.up,
        packet.down,
        packet.left,
        packet.right,
        packet.jump,
        packet.crouch,
        packet.sneak,
        packet.sprint,
    )
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


@register_handler(10)  # UseOrientedItem (grenade, etc.)
async def handle_use_oriented_item(server, player, packet):
    """Handle oriented item use (grenades, etc.)."""
    if not player.alive:
        return
    
    # Broadcast to all players
    from shared.packet import UseOrientedItem
    broadcast_packet = UseOrientedItem()
    broadcast_packet.loop_count = packet.loop_count
    broadcast_packet.player_id = player.id
    broadcast_packet.tool = packet.tool
    broadcast_packet.value = packet.value
    broadcast_packet.position = packet.position
    broadcast_packet.velocity = packet.velocity
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(76)  # WeaponReload
async def handle_weapon_reload(server, player, packet):
    """Handle weapon reload."""
    if not player.alive:
        return

    get_combat_system(server).handle_weapon_reload(player)
