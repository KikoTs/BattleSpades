"""
Serialization helpers for protocol.
Re-exports from shared.bytes for compatibility.
"""

import struct

from shared.bytes import (
    ByteReader as _NativeByteReader,
    ByteWriter as _NativeByteWriter,
    NoDataLeft,
)


class ByteReader:
    """Compatibility façade adding unsigned reads and remaining-byte count."""

    def __init__(self, data: bytes) -> None:
        self._reader = _NativeByteReader(data)
        self._length = len(data)

    def __getattr__(self, name):
        return getattr(self._reader, name)

    @property
    def remaining(self) -> int:
        return self._length - int(self._reader.tell())

    def read_uint16(self) -> int:
        return struct.unpack("<H", self._reader.read(2))[0]

    def read_uint32(self) -> int:
        return struct.unpack("<I", self._reader.read(4))[0]


class ByteWriter:
    """Compatibility façade adding unsigned/vector helpers and ``get_data``."""

    def __init__(self) -> None:
        self._writer = _NativeByteWriter()

    def __getattr__(self, name):
        return getattr(self._writer, name)

    def get_data(self) -> bytes:
        return bytes(self._writer)

    def write_uint16(self, value: int) -> None:
        self._writer.write(struct.pack("<H", int(value)))

    def write_uint32(self, value: int) -> None:
        self._writer.write(struct.pack("<I", int(value)))

    def write_vector3(self, x: float, y: float, z: float) -> None:
        self._writer.write(struct.pack("<fff", float(x), float(y), float(z)))


def tofixed(value: float) -> int:
    """Convert a number to the legacy generic signed 8.8 fixed format.

    Packet fields with recovered 1/64 scaling remain encoded by their packet
    classes in ``shared.packet``.  This compatibility helper belongs only to
    the old protocol-serialization façade and is retained for plugins/tests
    that imported it directly.
    """
    return int(round(float(value) * 256.0))


def fromfixed(value: int) -> float:
    """Decode the legacy generic signed 8.8 fixed format."""
    return float(value) / 256.0

__all__ = [
    "ByteReader",
    "ByteWriter", 
    "NoDataLeft",
    "tofixed",
    "fromfixed",
]
