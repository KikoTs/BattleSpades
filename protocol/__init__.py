"""
protocol - Network Protocol
Packet handling using aoslib Cython packets.
"""

# Import packet classes from aoslib (compiled Cython)
from aoslib.packet import (
    Loader,
    CLIENT_LOADERS,
    tofixed,
    fromfixed,
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
    "tofixed",
    "fromfixed",
    "handle_packet",
    "register_handler",
    "PacketHandler",
    "ByteReader",
    "ByteWriter",
]
