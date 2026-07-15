import asyncio
from types import SimpleNamespace

from shared.bytes import ByteReader
from shared.packet import PaintBlockPacket


def test_paint_block_wire_coordinates_match_retail_raw_i16_probe():
    packet = PaintBlockPacket()
    packet.loop_count = 0x11223344
    packet.x, packet.y, packet.z = 123, 234, 45
    packet.color = (0x12, 0x34, 0x56)

    assert bytes(packet.generate()).hex() == "07443322117b00ea002d00563412"
