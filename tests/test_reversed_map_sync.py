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
from server.runtime_vxl import _is_retail_marker_color, _raw_vxl_size
from server.world_manager import WorldManager, _shift_sync_records


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

    def serialize_dirty_columns_compressed(self, snapshot_columns=None):
        import zlib

        columns = (
            self.dirty_columns if snapshot_columns is None else snapshot_columns
        )
        if not columns:
            return b""
        raw = self.map.serialize_columns(sorted(columns))
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


def parse_sync_records(data: bytes):
    """Split a decompressed MapSync stream into (x, y, full-record) rows."""
    import struct

    records = []
    pos = 0
    while pos < len(data):
        start = pos
        x, y = struct.unpack_from("<II", data, pos)
        pos += 8
        while True:
            span_words = data[pos]
            top_start = data[pos + 1]
            top_end = data[pos + 2]
            top_len = top_end - top_start + 1 if top_end >= top_start else 0
            if span_words == 0:
                pos += 4 + top_len * 4
                break
            pos += span_words * 4
        records.append((x, y, data[start:pos]))
    assert pos == len(data)
    return records


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


def test_shipped_map_normalizes_surface_z_to_retail_waterplane_space():
    path = fixture_map_path()
    original_bytes = path.read_bytes()
    world_map = VXL(1, str(path), 3)

    sample_columns = [
        (100, 200),
        (150, 250),
        (256, 256),
    ]

    _columns, max_ref = _raw_vxl_size(original_bytes)
    z_shift = max(0, 239 - max_ref)
    for x, y in sample_columns:
        assert world_map.get_z(x, y) == raw_column_surface_z(original_bytes, x, y) + z_shift


def test_shipped_map_retail_z_shifts_match_measured_formats():
    expected = {
        "20thCenturyTown": 176,
        "CityOfChicago": 39,
        "ArcticBase": 0,
        "CastleWars": 0,
    }
    for name, shift in expected.items():
        path = MAPS_DIR / f"{name}.vxl"
        if not path.exists():
            continue
        _columns, max_ref = _raw_vxl_size(path.read_bytes())
        assert max(0, 239 - max_ref) == shift


def test_server_world_removes_retail_blue_green_marker_voxels() -> None:
    """Server collision must match the stock client's post-load map.

    ArcticBase stores authored marker points as saturated blue/green voxels.
    The retail loader removes them before gameplay.  A live client reported
    ``(231, 189, 221)`` empty while the unnormalised server treated it as a
    one-block step, producing a deterministic 0.177551-block correction at
    the first walk across that coordinate.
    """
    path = MAPS_DIR / "ArcticBase.vxl"
    if not path.exists():
        return

    wm = WorldManager(SimpleNamespace(
        maps_path=str(path.parent), game_mode="tdm"
    ))
    assert wm.load_map(path.stem)

    assert _is_retail_marker_color(0xFF0000F0) is True
    assert _is_retail_marker_color(0xFF0000FA) is True
    assert _is_retail_marker_color(0xFF05FF05) is True
    assert _is_retail_marker_color(0xFF0AFF0A) is True
    assert _is_retail_marker_color(0xFFFC1111) is False

    assert wm.get_solid(231, 189, 221) is False
    assert wm.get_solid(221, 189, 221) is False
    # Removing the marker must reveal, not hollow, the real terrain below.
    assert wm.get_solid(231, 189, 222) is True
    assert wm.get_solid(221, 189, 222) is True
    # Bright red terrain is ordinary authored geometry and remains solid.
    assert wm.get_solid(288, 339, 207) is True


def test_legacy_chroma_key_cleanup_requires_native_exposure_guard() -> None:
    """Colour alone is insufficient; native cleanup requires exposed air.

    20thCenturyTown contains 2,534 blue-family words. A hash-identical stock
    client removed exactly the 524 with two air cells above and retained the
    embedded remainder. This gates both the colour mask and exposure direction.
    """
    path = MAPS_DIR / "20thCenturyTown.vxl"
    if not path.exists():
        return
    wm = WorldManager(SimpleNamespace(
        maps_path=str(path.parent), game_mode="tdm"
    ))
    assert wm.load_map(path.stem)
    assert len(wm.map.retail_marker_positions) == 524
    assert (362, 153, 229) in wm.map.retail_marker_positions
    assert wm.get_solid(362, 153, 229) is False
    assert wm.get_solid(362, 153, 230) is True


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


def test_runtime_block_colors_are_stored_as_opaque_vxl_colors():
    wm = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    wm.map = VXL(-1, b"", 0, 2)

    assert wm.set_block(10, 20, 30, True, 0x123456) is True
    assert wm.get_color(10, 20, 30) == 0x80123456
    assert wm.map.get_color_tuple(10, 20, 30) == (0x12, 0x34, 0x56, 0xFF)


def test_player_block_delta_serializes_retail_opaque_color_word():
    """Reconnect MapSync must carry the same 0x80 alpha byte produced by the
    retail VXL.make_color(r,g,b,255) path, never alpha zero."""
    import struct
    import zlib

    wm = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    wm.map = VXL(-1, b"", 0, 2)
    assert wm.set_block(10, 20, 30, True, 0x123456)

    raw = zlib.decompress(wm.serialize_dirty_columns_compressed())
    assert struct.unpack_from("<II", raw, 0) == (10, 20)
    # A one-voxel floating run is both its top and bottom surface, so the
    # compact VXL record carries the color twice (header + two color words).
    assert (raw[8], raw[9], raw[10]) == (3, 30, 30)
    assert struct.unpack_from("<I", raw, 12)[0] == 0x80123456
    assert struct.unpack_from("<I", raw, 16)[0] == 0x80123456


def test_out_of_world_block_write_is_not_marked_dirty():
    wm = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    wm.map = VXL(-1, b"", 0, 2)

    assert wm.set_block(10, 20, 240, True, 0x123456) is False
    assert (10, 20) not in wm.dirty_columns


def test_legacy_map_delta_keeps_player_blocks_above_source_z_range():
    path = MAPS_DIR / "20thCenturyTown.vxl"
    if not path.exists():
        return
    wm = WorldManager(SimpleNamespace(maps_path=str(path.parent), game_mode="tdm"))
    assert wm.load_map(path.stem)
    assert wm.map.source_z_shift == 176

    assert wm.set_block(100, 200, 100, True, 0x123456)
    record = bytes(wm.map.serialize_columns([(100, 200)]))
    # u32 x + u32 y, then the first span header's top_start.
    assert record[9] == 100


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


def test_real_full_sync_substitutes_dirty_column_without_duplicate_records():
    """A mismatched-CRC client has no trusted local base. The optimized full
    stream starts from raw pristine VXL records, but dirty coordinates must be
    substituted in place. The strict builder receives exactly one record per
    map coordinate, including when the edit is deletion-only."""
    import zlib

    path = fixture_map_path()
    wm = WorldManager(SimpleNamespace(
        maps_path=str(path.parent), game_mode="tdm"
    ))
    assert wm.load_map(path.stem)
    x, y = 150, 250
    surface = wm.map.get_z(x, y)
    wm.set_block(x, y, surface, False)
    current_record = bytes(wm.map.serialize_columns([(x, y)]))

    chunks = wm.iter_full_sync_chunks(snapshot_columns={(x, y)})
    records = parse_sync_records(zlib.decompress(b"".join(chunks)))
    coordinates = [(rx, ry) for rx, ry, _ in records]
    by_coordinate = {(rx, ry): record for rx, ry, record in records}

    assert len(records) == 512 * 512
    assert len(set(coordinates)) == 512 * 512
    assert by_coordinate[(x, y)] == current_record
