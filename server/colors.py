"""One RGB convention for every server colour path.

Server state stores colours as packed ``0xRRGGBB`` integers (Player
``block_color``, VXL cells without their alpha byte) or ``(r, g, b)`` tuples
(team colours, prefab/paint packets). The wire codec in ``shared.packet``
accepts both and performs the retail byte reversal itself, so callers never
swap channels by hand; they only normalise through these helpers so an alpha
byte, an out-of-range channel or a tuple/int mix can never leak into a packet.
"""

from __future__ import annotations


def pack_rgb(color) -> int:
    """Return ``color`` (int, tuple or list) as ``0xRRGGBB``.

    Integers are masked to their low 24 bits, which drops a VXL alpha/shade
    byte (``0x80RRGGBB`` -> ``0xRRGGBB``). Tuples keep their first three
    channels, each masked to one byte.
    """

    if isinstance(color, (tuple, list)):
        if len(color) < 3:
            raise ValueError("RGB colour needs three channels")
        red, green, blue = (int(value) & 0xFF for value in color[:3])
        return (red << 16) | (green << 8) | blue
    return int(color) & 0xFFFFFF


def unpack_rgb(color) -> tuple[int, int, int]:
    """Return ``color`` (int, tuple or list) as an ``(r, g, b)`` tuple."""

    packed = pack_rgb(color)
    return ((packed >> 16) & 0xFF, (packed >> 8) & 0xFF, packed & 0xFF)
