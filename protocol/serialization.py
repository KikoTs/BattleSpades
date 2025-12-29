"""
Serialization helpers for protocol.
Re-exports from aoslib.bytes for compatibility.
"""

# Re-export from aoslib for backwards compatibility
from aoslib.bytes import ByteReader, ByteWriter, NoDataLeft

__all__ = [
    "ByteReader",
    "ByteWriter", 
    "NoDataLeft",
]
