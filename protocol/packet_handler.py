"""
Packet handler - routes incoming packets to handlers.
Uses reversed shared packets for serialization.
"""

import logging
from typing import TYPE_CHECKING

from shared.bytes import ByteReader
from shared.packet import CLIENT_LOADERS
from protocol.runtime_packets import decode_runtime_packet
from protocol.handler_registry import HANDLERS as _handlers, register_handler

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)

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


# Domain modules register against protocol.handler_registry. The protocol layer
# owns only byte decoding and dispatch; gameplay behavior stays server-side.
from server.handlers import equipment as _equipment_handlers  # noqa: E402,F401
from server.handlers import team as _team_handlers  # noqa: E402,F401
from server.handlers import movement as _movement_handlers  # noqa: E402,F401
from server.handlers import combat as _combat_handlers  # noqa: E402,F401
from server.handlers import social as _social_handlers  # noqa: E402,F401
from server.handlers import diagnostics as _diagnostic_handlers  # noqa: E402,F401
from server.handlers import deployables as _deployable_handlers  # noqa: E402,F401
from server.handlers import blocks as _block_handlers  # noqa: E402,F401
from server.handlers import world as _world_handlers  # noqa: E402,F401
from server.handlers import ugc as _ugc_handlers  # noqa: E402,F401
