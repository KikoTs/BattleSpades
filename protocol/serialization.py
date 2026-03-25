"""
Serialization helpers for protocol.
Re-exports from shared.bytes for compatibility.
"""

# Re-export from shared for protocol/runtime compatibility
from shared.bytes import ByteReader, ByteWriter, NoDataLeft

__all__ = [
    "ByteReader",
    "ByteWriter", 
    "NoDataLeft",
]
