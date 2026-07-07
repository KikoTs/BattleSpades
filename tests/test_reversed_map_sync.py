import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

from aoslib.vxl import VXL
from shared.bytes import ByteReader
from shared.packet import MapDataValidation, MapSyncChunk, MapSyncEnd, MapSyncStart
from server.connection import Connection


MAPS_DIR = Path("maps")


class DummyPeer:
    def __init__(self):
        self.address = ("127.0.0.1", 32887)

    def disconnect(self, reason=0):
        return None


class DummyWorldManager:
    def __init__(self, map_obj, map_file_crc=0):
        self.map = map_obj
        self.map_file_crc = map_file_crc
        self.dirty_columns = set()

    def get_chunker(self):
        return self.map.get_chunker()

    def serialize_dirty_columns_compressed(self):
        import zlib

        if not self.dirty_columns:
            return b""
        raw = self.map.serialize_columns(sorted(self.dirty_columns))
        return zlib.compress(raw, 6) if raw else b""


class DummyServer:
    def __init__(self, map_obj, map_file_crc=0):
        self.world_manager = DummyWorldManager(map_obj, map_file_crc)
        self.config = SimpleNamespace(
            log_suppress_packets=set(), map_sync_mode="auto"
        )
        self.players = {}
        self.connections = {}


def fixture_map_path() -> Path:
    return next(path for path in MAPS_DIR.glob("*.vxl"))


def raw_column_surface_z(data: bytes, target_x: int, target_y: int) -> int:
    """Topmost solid z of a column straight from the raw file bytes.

    Walks EVERY span record of every column (a column is a sequence of
    records ending with span_words == 0). The old version consumed only
    the first record per column, drifting out of sync on multi-span
    columns and attributing data to the wrong (x, y).
    """
    pos = 0
    for y in range(512):
        for x in range(512):
            surface = None
            while True:
                span_words = data[pos]
                top_start = data[pos + 1]
                top_end = data[pos + 2]
                if surface is None and top_end >= top_start:
                    surface = top_start
                if span_words == 0:
                    top_len = top_end - top_start + 1 if top_end >= top_start else 0
                    pos += 4 + top_len * 4
                    break
                pos += span_words * 4
            if x == target_x and y == target_y:
                return surface if surface is not None else 239

    raise AssertionError(f"column ({target_x}, {target_y}) not found")


def test_shipped_map_chunker_restores_old_sync_shape():
    path = fixture_map_path()
    original_bytes = path.read_bytes()

    world_map = VXL(1, str(path), 3)
    assert world_map.generate_vxl() == original_bytes

    first_chunker = world_map.get_chunker()
    first_chunks = list(first_chunker.iter())
    second_chunker = world_map.get_chunker()
    second_chunks = list(second_chunker.iter())

    assert first_chunks
    assert all(0 < len(chunk) <= 1024 for chunk in first_chunks)
    assert first_chunks == second_chunks
    assert first_chunker.crc32 == second_chunker.crc32
    assert world_map.estimated_size >= sum(len(chunk) for chunk in first_chunks)


def test_shipped_map_preserves_raw_surface_z_coordinates():
    path = fixture_map_path()
    original_bytes = path.read_bytes()
    world_map = VXL(1, str(path), 3)

    sample_columns = [
        (100, 200),
        (150, 250),
        (256, 256),
    ]

    for x, y in sample_columns:
        assert world_map.get_z(x, y) == raw_column_surface_z(original_bytes, x, y)


def test_generated_and_edited_maps_use_valid_old_style_chunking():
    world_map = VXL(-1, b"", 0, 2)
    world_map.set_point(32, 48, 60, 0x7F008F00)
    world_map.set_point(32, 48, 61, 0x7F008F00)
    world_map.set_point(33, 48, 60, 0x7F005F00)

    initial_chunker = world_map.get_chunker()
    initial_chunks = list(initial_chunker.iter())

    world_map.remove_point(32, 48, 61)
    world_map.set_point(40, 40, 62, 0x7F3366AA)

    edited_chunker = world_map.get_chunker()
    edited_chunks = list(edited_chunker.iter())

    assert initial_chunks
    assert edited_chunks
    assert all(0 < len(chunk) <= 1024 for chunk in initial_chunks)
    assert all(0 < len(chunk) <= 1024 for chunk in edited_chunks)
    assert initial_chunker.crc32 != edited_chunker.crc32
    assert initial_chunks != edited_chunks


def test_get_z_returns_surface_block_not_column_bottom():
    world_map = VXL(-1, b"", 0, 2)
    world_map.set_point(64, 64, 120, 0x7F224466)
    world_map.set_point(64, 64, 121, 0x7F224466)
    world_map.set_point(64, 64, 122, 0x7F224466)

    assert world_map.get_z(64, 64) == 120
    assert world_map.get_z(64, 64, 121) == 121
    assert world_map.get_random_pos(64, 64, 65, 65) == (64, 64, 120)


def _run_send_map_data(server, client_crc):
    connection = Connection(DummyPeer(), server)
    sent_packets = []

    async def fake_wait_for(packet_class, timeout=5.0):
        packet = packet_class()
        packet.crc = client_crc
        return packet

    connection.wait_for = fake_wait_for
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append((prefix, data))
    asyncio.run(connection.send_map_data())
    return sent_packets


def test_send_map_data_uses_restored_vxl_chunker(caplog):
    """Mismatched client CRC -> full world sync stream."""
    import zlib

    path = fixture_map_path()
    file_crc = zlib.crc32(path.read_bytes()) & 0xFFFFFFFF
    world_map = VXL(1, str(path), 3)
    server = DummyServer(world_map, map_file_crc=file_crc)

    caplog.set_level(logging.INFO)
    sent_packets = _run_send_map_data(server, client_crc=0x1234ABCD)

    # The validation reply carries OUR file CRC (never an echo).
    validation = MapDataValidation(ByteReader(sent_packets[0][1][1:]))
    assert sent_packets[0][0] == 0x31
    expected_wire_crc = file_crc - (1 << 32) if file_crc >= (1 << 31) else file_crc
    assert validation.crc == expected_wire_crc

    assert sent_packets[1][0] == 0x32
    assert sent_packets[1][1][0] == MapSyncStart.id

    chunk_packets = [
        MapSyncChunk(ByteReader(data[1:]))
        for prefix, data in sent_packets
        if prefix == 0x31 and data and data[0] == MapSyncChunk.id
    ]
    assert chunk_packets
    assert all(0 < len(packet.data) <= 1024 for packet in chunk_packets)
    percents = [packet.percent_complete for packet in chunk_packets]
    assert percents == sorted(percents)
    assert percents[-1] == 100

    # MEASURED: MapSyncStart is the bare id byte — any extra payload is
    # parsed by the real client as a truncated next packet and crashes it.
    assert len(sent_packets[1][1]) == 1

    assert sent_packets[-1][0] == 0x31
    assert sent_packets[-1][1][0] == MapSyncEnd.id

    expected_chunker = world_map.get_chunker()
    expected_chunks = list(expected_chunker.iter())
    assert [packet.data for packet in chunk_packets] == expected_chunks
    assert world_map.estimated_size > 0
    assert any("Prepared map sync" in record.message for record in caplog.records)


def test_send_map_data_matching_crc_sends_delta_only():
    """Matching client CRC -> the client's local file is the world base;
    only columns changed since map load are streamed (none on fresh map)."""
    import zlib

    path = fixture_map_path()
    file_crc = zlib.crc32(path.read_bytes()) & 0xFFFFFFFF
    world_map = VXL(1, str(path), 3)
    server = DummyServer(world_map, map_file_crc=file_crc)

    client_crc = file_crc - (1 << 32) if file_crc >= (1 << 31) else file_crc
    sent_packets = _run_send_map_data(server, client_crc=client_crc)

    chunk_packets = [
        data for prefix, data in sent_packets if data and data[0] == MapSyncChunk.id
    ]
    assert chunk_packets == []  # fresh map: nothing to sync

    assert sent_packets[1][1][0] == MapSyncStart.id
    assert len(sent_packets[1][1]) == 1  # bare id byte (measured wire format)
    assert sent_packets[-1][1][0] == MapSyncEnd.id


def test_send_map_data_matching_crc_streams_dirty_columns():
    """Blocks changed after load are still delivered to matched-CRC clients."""
    import zlib

    path = fixture_map_path()
    file_crc = zlib.crc32(path.read_bytes()) & 0xFFFFFFFF
    world_map = VXL(1, str(path), 3)
    server = DummyServer(world_map, map_file_crc=file_crc)
    surface = world_map.get_z(150, 250)
    world_map.set_point(150, 250, max(0, surface - 1), 0x7F00AA00)
    server.world_manager.dirty_columns.add((150, 250))

    client_crc = file_crc - (1 << 32) if file_crc >= (1 << 31) else file_crc
    sent_packets = _run_send_map_data(server, client_crc=client_crc)

    chunk_packets = [
        MapSyncChunk(ByteReader(data[1:]))
        for prefix, data in sent_packets
        if data and data[0] == MapSyncChunk.id
    ]
    assert chunk_packets
    payload = b"".join(packet.data for packet in chunk_packets)
    raw = zlib.decompress(payload)
    # Record format: u32 x, u32 y, column spans
    import struct

    x, y = struct.unpack_from("<II", raw, 0)
    assert (x, y) == (150, 250)
    assert raw[8:] == bytes(world_map.serialize_columns([(150, 250)]))[8:]
