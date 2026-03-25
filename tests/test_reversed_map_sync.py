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
    def __init__(self, map_obj):
        self.map = map_obj

    def get_chunker(self):
        return self.map.get_chunker()


class DummyServer:
    def __init__(self, map_obj):
        self.world_manager = DummyWorldManager(map_obj)
        self.config = SimpleNamespace(log_suppress_packets=set())
        self.players = {}
        self.connections = {}


def fixture_map_path() -> Path:
    return next(path for path in MAPS_DIR.glob("*.vxl"))


def raw_column_surface_z(data: bytes, target_x: int, target_y: int) -> int:
    pos = 0
    for y in range(512):
        for x in range(512):
            header = data[pos : pos + 4]
            span_words = header[0]
            top_start = header[1]
            top_end = header[2]
            pos += 4

            if x == target_x and y == target_y:
                if top_end >= top_start:
                    return top_start
                return 239

            if span_words == 0:
                if top_end >= top_start:
                    pos += (top_end - top_start + 1) * 4
                continue

            pos += (span_words - 1) * 4

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


def test_send_map_data_uses_restored_vxl_chunker(caplog):
    world_map = VXL(1, str(fixture_map_path()), 3)
    server = DummyServer(world_map)
    connection = Connection(DummyPeer(), server)

    client_crc = 0x1234ABCD
    sent_packets = []

    async def fake_wait_for(packet_class, timeout=5.0):
        packet = packet_class()
        packet.crc = client_crc
        return packet

    connection.wait_for = fake_wait_for
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append((prefix, data))

    caplog.set_level(logging.INFO)
    asyncio.run(connection.send_map_data())

    validation = MapDataValidation(ByteReader(sent_packets[0][1][1:]))
    assert sent_packets[0][0] == 0x31
    assert validation.crc == client_crc

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

    assert sent_packets[-1][0] == 0x31
    assert sent_packets[-1][1][0] == MapSyncEnd.id

    expected_chunker = world_map.get_chunker()
    expected_chunks = list(expected_chunker.iter())
    assert [packet.data for packet in chunk_packets] == expected_chunks
    assert world_map.estimated_size > 0
    assert any("Prepared map sync" in record.message for record in caplog.records)
