"""
protocol - Network Protocol
Packet handling using the reversed shared Cython packets.
"""

from shared.packet import (
    Loader,
    CLIENT_LOADERS,
)

from .packet_handler import (
    handle_packet,
    register_handler,
    PacketHandler,
)

from .serialization import (
    ByteReader,
    ByteWriter,
)

__all__ = [
    "Loader",
    "CLIENT_LOADERS",
    "handle_packet",
    "register_handler",
    "PacketHandler",
    "ByteReader",
    "ByteWriter",
]
