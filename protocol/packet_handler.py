"""
Packet handler - routes incoming packets to handlers.
Uses reversed shared packets for serialization.
"""

import logging
import time
from typing import TYPE_CHECKING

from shared.bytes import ByteReader
from shared.packet import CLIENT_LOADERS
from protocol.runtime_packets import decode_runtime_packet
from protocol.handler_registry import HANDLERS as _handlers, register_handler

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)

# packet_id -> (last monotonic log time, suppressed count)
_UNROUTABLE_LOG_STATE: dict[int, list] = {}
_UNROUTABLE_LOG_INTERVAL_SECONDS = 60.0
# packet_id -> (last monotonic log time, suppressed count) for handler errors
_HANDLER_ERROR_LOG_STATE: dict[int, list] = {}
_HANDLER_ERROR_LOG_INTERVAL_SECONDS = 10.0


def _log_unroutable(kind: str, packet_id: int, player) -> None:
    """Debug-log an unhandled/unknown client packet id, rate-limited per id."""

    if not logger.isEnabledFor(logging.DEBUG):
        return
    now = time.monotonic()
    state = _UNROUTABLE_LOG_STATE.setdefault(int(packet_id), [float("-inf"), 0])
    if now - state[0] < _UNROUTABLE_LOG_INTERVAL_SECONDS:
        state[1] += 1
        return
    suppressed = state[1]
    state[0], state[1] = now, 0
    logger.debug(
        "%s packet ID %s from %s (%d repeat(s) suppressed)",
        kind,
        packet_id,
        getattr(player, "name", "?"),
        suppressed,
    )

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
            _log_unroutable("Unhandled", packet_id, player)
            return
        
        # Parse packet using aoslib
        packet_class = CLIENT_LOADERS.get(packet_id)
        if packet_class is None:
            # Any client can send arbitrary ids; never let that become
            # warning-level log spam.
            _log_unroutable("Unknown", packet_id, player)
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
            # Malformed client input can raise on every packet; keep the
            # traceback but at most once per packet id per interval.
            state = _HANDLER_ERROR_LOG_STATE.setdefault(
                int(packet_id), [float("-inf"), 0]
            )
            now = time.monotonic()
            if now - state[0] < _HANDLER_ERROR_LOG_INTERVAL_SECONDS:
                state[1] += 1
                return
            suppressed = state[1]
            state[0], state[1] = now, 0
            logger.error(
                "Error handling packet %s from %s: %s (%d similar suppressed)",
                packet_id,
                getattr(player, "name", "?"),
                e,
                suppressed,
                exc_info=True,
            )


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
from server.handlers import deployables as _deployable_handlers  # noqa: E402,F401
from server.handlers import blocks as _block_handlers  # noqa: E402,F401
from server.handlers import world as _world_handlers  # noqa: E402,F401
from server.handlers import ugc as _ugc_handlers  # noqa: E402,F401
