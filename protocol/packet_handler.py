"""
Packet handler - routes incoming packets to handlers.
Uses aoslib Cython packets for serialization.
"""

import logging
from typing import Callable, Dict, TYPE_CHECKING

from aoslib.bytes import ByteReader
from aoslib.packet import CLIENT_LOADERS

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)

# Handler registry: packet_id -> handler_function
_handlers: Dict[int, Callable] = {}


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
            reader = ByteReader(data[1:])  # Skip packet ID byte
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
    # Update player state
    player.orientation = (packet.o_x, packet.o_y, packet.o_z)
    
    # Update input flags
    player.input.up = packet.up
    player.input.down = packet.down
    player.input.left = packet.left
    player.input.right = packet.right
    player.input.jump = packet.jump
    player.input.crouch = packet.crouch
    player.input.sneak = packet.sneak
    player.input.sprint = packet.sprint
    player.input.primary = packet.primary
    player.input.secondary = packet.secondary
    
    player.tool = packet.tool_id


@register_handler(116)  # PositionData
async def handle_position_data(server, player, packet):
    """Handle position update from client."""
    # TODO: Validate position (anti-cheat)
    pass


@register_handler(6)  # ShootPacket
async def handle_shoot(server, player, packet):
    """Handle shooting."""
    if not player.alive:
        return
    
    # Process hit detection
    # TODO: Server-side hit validation
    pass


@register_handler(32)  # BlockBuild
async def handle_block_build(server, player, packet):
    """Handle block placement."""
    if not player.alive:
        return
    
    x, y, z = packet.x, packet.y, packet.z
    
    # Validate build position
    if not server.world_manager.can_build(x, y, z):
        return
    
    # Place block
    color = player.color if hasattr(player, 'color') else 0xFFFFFF
    server.world_manager.set_block(x, y, z, True, color)
    
    # Broadcast to all players
    from aoslib.packet import BlockBuild
    broadcast_packet = BlockBuild()
    broadcast_packet.loop_count = server.loop_count
    broadcast_packet.player_id = player.id
    broadcast_packet.x = x
    broadcast_packet.y = y
    broadcast_packet.z = z
    broadcast_packet.block_type = 1
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(35)  # BlockLiberate (destroy)
async def handle_block_destroy(server, player, packet):
    """Handle block destruction."""
    if not player.alive:
        return
    
    x, y, z = packet.x, packet.y, packet.z
    
    if not server.world_manager.can_build(x, y, z):
        return
    
    server.world_manager.destroy_block(x, y, z)
    
    # Broadcast
    from aoslib.packet import BlockLiberate
    broadcast_packet = BlockLiberate()
    broadcast_packet.loop_count = server.loop_count
    broadcast_packet.player_id = player.id
    broadcast_packet.x = x
    broadcast_packet.y = y
    broadcast_packet.z = z
    server.broadcast(bytes(broadcast_packet.generate()))


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
    from aoslib.packet import ChatMessage
    broadcast_packet = ChatMessage()
    broadcast_packet.player_id = player.id
    broadcast_packet.chat_type = packet.chat_type
    broadcast_packet.value = message
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(77)  # ChangeTeam
async def handle_change_team(server, player, packet):
    """Handle team change request."""
    new_team = packet.team
    
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
        from aoslib.packet import KillAction
        kill_packet = KillAction()
        kill_packet.player_id = player.id
        kill_packet.killer_id = player.id
        kill_packet.kill_type = 5
        kill_packet.respawn_time = int(server.config.respawn_time)
        server.broadcast(bytes(kill_packet.generate()))
        player.die(kill_type=5)


@register_handler(78)  # ChangeClass
async def handle_change_class(server, player, packet):
    """Handle class change request."""
    player.class_id = packet.class_id


@register_handler(11)  # SetColor
async def handle_set_color(server, player, packet):
    """Handle color change."""
    player.color = packet.value
    
    # Broadcast
    from aoslib.packet import SetColor
    broadcast_packet = SetColor()
    broadcast_packet.player_id = player.id
    broadcast_packet.value = packet.value
    server.broadcast(bytes(broadcast_packet.generate()))


@register_handler(10)  # UseOrientedItem (grenade, etc.)
async def handle_use_oriented_item(server, player, packet):
    """Handle oriented item use (grenades, etc.)."""
    if not player.alive:
        return
    
    # Broadcast to all players
    from aoslib.packet import UseOrientedItem
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
    
    # Broadcast
    from aoslib.packet import WeaponReload
    broadcast_packet = WeaponReload()
    broadcast_packet.player_id = player.id
    broadcast_packet.tool_id = packet.tool_id
    broadcast_packet.is_done = packet.is_done
    server.broadcast(bytes(broadcast_packet.generate()))
