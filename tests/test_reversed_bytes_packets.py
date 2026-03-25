import math

from shared.bytes import ByteReader, ByteWriter
from shared.packet import BlockOccupy, ClockSync


def test_byte_writer_and_reader_round_trip():
    writer = ByteWriter()
    writer.write_byte(42)
    writer.write_short(1234)
    writer.write_int(0x12345678)
    writer.write_float(3.14159)
    writer.write_string("hello")

    data = bytes(writer)
    reader = ByteReader(data)

    assert reader.read_byte() == 42
    assert reader.read_short() == 1234
    assert reader.read_int() == 0x12345678
    assert math.isclose(reader.read_float(), 3.14159, abs_tol=1e-6)
    assert reader.read_string() == "hello"
    assert reader.data_left() is False


def test_reader_seek_and_rewind():
    reader = ByteReader(b"\x01\x02\x03\x04")

    assert reader.tell() == 0
    assert reader.read_byte() == 1
    assert reader.tell() == 1

    reader.seek(2)
    assert reader.tell() == 2
    assert reader.read_byte() == 3

    reader.rewind()
    assert reader.tell() == 0
    assert reader.read_byte() == 1


def test_clock_sync_packet_round_trip():
    packet = ClockSync()
    packet.client_time = 5678
    packet.server_loop_count = 123

    raw = bytes(packet.generate())
    parsed = ClockSync(ByteReader(raw[1:]))

    assert raw[0] == ClockSync.id
    assert parsed.client_time == 5678
    assert parsed.server_loop_count == 123


def test_block_occupy_packet_round_trip():
    packet = BlockOccupy()
    packet.loop_count = 9
    packet.player_id = 4
    packet.x = 111
    packet.y = 222
    packet.z = 63

    raw = bytes(packet.generate())
    parsed = BlockOccupy(ByteReader(raw[1:]))

    assert raw[0] == BlockOccupy.id
    assert parsed.loop_count == 9
    assert parsed.player_id == 4
    assert (parsed.x, parsed.y, parsed.z) == (111, 222, 63)
