"""Adversarial state-convergence tests for late-join terrain sync.

These tests reconstruct the voxel columns a joining retail client receives.
They intentionally verify final solidity and RGB, rather than merely asserting
that the server emitted packets in a plausible order.  That distinction catches
malformed VXL spans which otherwise present as textureless/phantom terrain.
"""

from __future__ import annotations

import random
import struct
from types import SimpleNamespace
import zlib

import pytest

from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.runtime_vxl import ServerVXL
from server.world_manager import WorldManager
from shared.bytes import ByteReader
from shared.packet import BlockBuildColored, Damage


CellState = tuple[bool, int]
ColumnState = list[CellState]

_MAP_HEIGHT = 240
_ONE_EMPTY_COLUMN = b"\x00\xf0\xef\x00"


class _JoiningConnection:
    """Minimal reliable peer used at the MapSync/first-ClientData boundary."""

    def __init__(self) -> None:
        self.in_game = False
        self.player = SimpleNamespace(id=9, team=0)
        self.sent: list[bytes] = []
        self.map_mutation_watermark = None
        self.map_mutation_overflow = False
        self.map_cell_watermark = None
        self.map_cell_overflow = False
        self.map_cell_replay = None
        self.disconnect_reason = None

    def send(self, data: bytes, reliable: bool = True, prefix: int = 0x30) -> None:
        assert reliable is True
        self.sent.append(bytes(data))

    def disconnect(self, reason: int = 0) -> None:
        self.disconnect_reason = int(reason)


def _column_state(vxl: ServerVXL, x: int, y: int) -> ColumnState:
    """Read one complete collision/RGB column from a VXL instance."""

    return [
        (
            bool(vxl.get_solid(x, y, z)),
            int(vxl.get_color(x, y, z)) & 0xFFFFFF,
        )
        for z in range(_MAP_HEIGHT)
    ]


def _decode_sync_columns(payload: bytes) -> dict[tuple[int, int], ColumnState]:
    """Decode compressed MapSync records through the production VXL loader."""

    if not payload:
        return {}
    raw = zlib.decompress(payload)
    columns: dict[tuple[int, int], ColumnState] = {}
    position = 0
    while position < len(raw):
        if position + 8 > len(raw):
            raise AssertionError("truncated MapSync coordinate")
        x, y = struct.unpack_from("<II", raw, position)
        position += 8
        span_start = position
        while True:
            if position + 4 > len(raw):
                raise AssertionError("truncated MapSync span")
            span_words = raw[position]
            top_start = raw[position + 1]
            top_end = raw[position + 2]
            top_length = max(0, top_end - top_start + 1)
            if span_words == 0:
                position += 4 + top_length * 4
                break
            position += span_words * 4

        encoded = raw[span_start:position]
        # A one-column source is centered in the 512x512 runtime map.
        decoded = ServerVXL(-1, encoded, len(encoded), 2)
        assert decoded.ready, f"client rejected MapSync column {(x, y)}"
        columns[(x, y)] = _column_state(decoded, 255, 255)
    assert position == len(raw)
    return columns


def _find_encoded_sync_column(
    raw: bytes, target: tuple[int, int]
) -> bytes:
    """Return one column span while validating every surrounding record."""

    position = 0
    found = None
    while position < len(raw):
        if position + 8 > len(raw):
            raise AssertionError("truncated MapSync coordinate")
        coordinate = struct.unpack_from("<II", raw, position)
        position += 8
        span_start = position
        while True:
            if position + 4 > len(raw):
                raise AssertionError("truncated MapSync span")
            span_words = raw[position]
            top_start = raw[position + 1]
            top_end = raw[position + 2]
            top_length = max(0, top_end - top_start + 1)
            if span_words == 0:
                position += 4 + top_length * 4
                break
            position += span_words * 4
        if coordinate == target:
            assert found is None, f"duplicate MapSync coordinate {target}"
            found = raw[span_start:position]
    assert position == len(raw)
    assert found is not None, f"missing MapSync coordinate {target}"
    return found


def _decode_encoded_column(encoded: bytes) -> ColumnState:
    decoded = ServerVXL(-1, encoded, len(encoded), 2)
    assert decoded.ready
    return _column_state(decoded, 255, 255)


def _blank_column() -> ColumnState:
    vxl = ServerVXL(-1, b"", 0, 2)
    return _column_state(vxl, 0, 0)


def _apply_exact_packets(
    columns: dict[tuple[int, int], ColumnState], packets: list[bytes]
) -> None:
    """Apply the exact-cell catch-up packet subset to reconstructed columns."""

    for data in packets:
        packet_id = data[0]
        if packet_id == BlockBuildColored.id:
            packet = BlockBuildColored(ByteReader(data[1:]))
            column = columns.setdefault((packet.x, packet.y), _blank_column())
            column[packet.z] = (True, int(packet.color) & 0xFFFFFF)
            continue
        if packet_id == Damage.id:
            packet = Damage(ByteReader(data[1:]))
            assert packet.type == 6
            x, y, z = (int(value + 0.5) for value in packet.position)
            column = columns.setdefault((x, y), _blank_column())
            column[z] = (False, 0)
            continue
        raise AssertionError(f"unexpected terrain catch-up packet {packet_id}")


def test_isolated_nonfinal_run_roundtrips_without_overlapping_span_colors() -> None:
    """An island above the floor must not be encoded as overlapping surfaces."""

    source = ServerVXL(-1, b"", 0, 2)
    source.set_point(100, 100, 17, 0x80123456)
    record = bytes(source.serialize_columns(((100, 100),)))
    assert struct.unpack_from("<II", record, 0) == (100, 100)

    decoded = ServerVXL(-1, record[8:], len(record) - 8, 2)
    assert decoded.ready
    assert _column_state(decoded, 255, 255) == _column_state(source, 100, 100)


def test_dirty_source_generation_uses_the_same_valid_span_contract() -> None:
    """Generated/raw fallback VXL output must share the repaired span codec."""

    source = ServerVXL(-1, _ONE_EMPTY_COLUMN, len(_ONE_EMPTY_COLUMN), 2)
    source.set_point(255, 255, 31, 0x8010ABCD)
    serialized = source.generate_vxl(False)

    decoded = ServerVXL(-1, serialized, len(serialized), 2)
    assert decoded.ready
    assert _column_state(decoded, 255, 255) == _column_state(source, 255, 255)


def test_seeded_dirty_column_fuzz_converges_solidity_and_rgb() -> None:
    """Random holes/islands/recolors retain byte-exact client column state."""

    rng = random.Random(0xA05_2026)
    x, y = 123, 234
    for _case in range(64):
        source = ServerVXL(-1, b"", 0, 2)
        for _mutation in range(120):
            z = rng.randrange(0, _MAP_HEIGHT - 1)
            if rng.random() < 0.58:
                # Keep red's high nibble nonzero so this serializer test does
                # not intentionally invoke retail chroma-marker cleanup.
                color = 0x80100000 | rng.randrange(0x100000)
                source.set_point(x, y, z, color)
            else:
                source.remove_point(x, y, z)

        record = bytes(source.serialize_columns(((x, y),)))
        decoded = ServerVXL(-1, record[8:], len(record) - 8, 2)
        assert decoded.ready
        assert _column_state(decoded, 255, 255) == _column_state(source, x, y)


def test_delta_roundtrip_handles_cave_gaps_runs_recolor_and_deletion() -> None:
    """Matching-CRC deltas preserve hard column layouts and empty results."""

    world = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    world.map = ServerVXL(-1, b"", 0, 2)
    x, y = 70, 80
    for z in range(90, 111):
        world.set_block(x, y, z, True, 0x102000 + z)
    world.destroy_blocks([(x, y, z) for z in range(97, 104)])
    world.set_block(x, y, 90, True, 0x10FEDC)  # Recolor the upper surface.
    world.set_block(x, y, 42, True, 0x10CAFE)  # Isolated non-final run.

    payload = world.serialize_dirty_columns_compressed({(x, y)})
    decoded = _decode_sync_columns(payload)
    assert decoded[(x, y)] == _column_state(world.map, x, y)

    # A deletion-only final state must replace the entire old client column,
    # not serialize an empty/no-op delta which would retain stale terrain.
    world.destroy_blocks(
        [(x, y, z) for z in range(_MAP_HEIGHT - 1)]
    )
    payload = world.serialize_dirty_columns_compressed({(x, y)})
    decoded = _decode_sync_columns(payload)
    assert decoded[(x, y)] == _column_state(world.map, x, y)


def test_full_sync_dirty_overlay_roundtrips_legacy_shifted_column() -> None:
    """Mismatched-CRC full sync substitutes canonical world-Z dirty spans."""

    map_path = "maps/20thCenturyTown.vxl"
    world = WorldManager(SimpleNamespace(maps_path="maps", game_mode="tdm"))
    if not world.load_map("20thCenturyTown"):
        pytest.skip(f"official fixture unavailable: {map_path}")
    assert world.map is not None
    assert world.map.source_z_shift > 0

    x, y = 100, 200
    # These edits sit above the legacy source Z range. The overlay is encoded
    # in normalized world coordinates and must not receive a second shift.
    world.set_block(x, y, 100, True, 0x10A1B2)
    world.set_block(x, y, 101, True, 0x10C3D4)
    world.destroy_blocks([(x, y, 101)])

    chunks = world.iter_full_sync_chunks(snapshot_columns={(x, y)})
    assert chunks is not None
    raw = zlib.decompress(b"".join(chunks))
    encoded = _find_encoded_sync_column(raw, (x, y))
    assert _decode_encoded_column(encoded) == _column_state(world.map, x, y)

    # Repeat with a deletion-only overlay. Reusing the pristine raw column
    # here would silently resurrect every removed server-air voxel.
    world.destroy_blocks(
        [(x, y, z) for z in range(_MAP_HEIGHT - 1)]
    )
    chunks = world.iter_full_sync_chunks(snapshot_columns={(x, y)})
    assert chunks is not None
    raw = zlib.decompress(b"".join(chunks))
    encoded = _find_encoded_sync_column(raw, (x, y))
    assert _decode_encoded_column(encoded) == _column_state(world.map, x, y)


def test_join_snapshot_plus_cross_boundary_mutations_converges_exactly() -> None:
    """Build/remove/recolor/collapse while loading ends at canonical VXL state."""

    server = BattleSpadesServer(ServerConfig(max_map_mutation_journal=256))
    world = server.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    world.dirty_columns.clear()

    # State serialized into the immutable MapSync snapshot.
    world.set_block(40, 50, 70, True, 0x10AA11)
    world.set_block(40, 50, 71, True, 0x10AA22)
    world.set_block(41, 50, 70, True, 0x10AA33)
    snapshot_columns = set(world.dirty_columns)
    snapshot = world.serialize_dirty_columns_compressed(snapshot_columns)

    joiner = _JoiningConnection()
    server.connections = {"joining": joiner}
    server.mark_map_snapshot_complete(joiner)

    # These commits cross the map-transfer boundary. Repeated writes exercise
    # final-state coalescing; the batch removal models a collapsed chunk.
    world.set_block(40, 50, 70, True, 0x10BB11)
    world.destroy_blocks([(40, 50, 71)])
    world.set_block(42, 50, 68, True, 0x10CC11)
    world.set_block(42, 50, 68, True, 0x10CC22)
    for z in (72, 73, 74):
        world.set_block(43, 50, z, True, 0x10DD00 + z)
    world.destroy_blocks([(43, 50, 72), (43, 50, 73), (43, 50, 74)])
    world.set_block(44, 50, 69, True, 0x10EE11)

    server.replay_map_mutations(joiner)

    client_columns = _decode_sync_columns(snapshot)
    _apply_exact_packets(client_columns, joiner.sent)
    tracked_columns = set(snapshot_columns) | {
        (42, 50),
        (43, 50),
        (44, 50),
    }
    for x, y in tracked_columns:
        actual = client_columns.get((x, y), _blank_column())
        assert actual == _column_state(world.map, x, y)

    changed_packet_count = len(joiner.sent)
    # Eleven canonical callbacks above collapse to seven final coordinates.
    assert changed_packet_count == 7
    assert joiner.map_cell_watermark == server._map_cell_sequence


def test_topology_journal_overflow_rejects_partial_state() -> None:
    """A capped journal disconnects rather than admitting a phantom map."""

    server = BattleSpadesServer(ServerConfig(max_map_mutation_journal=64))
    world = server.world_manager
    world.map = ServerVXL(-1, b"", 0, 2)
    joiner = _JoiningConnection()
    server.connections = {"joining": joiner}
    server.mark_map_snapshot_complete(joiner)

    for index in range(65):
        world.set_block(index, 60, 80, True, 0x101000 + index)

    assert joiner.map_cell_overflow is True
    with pytest.raises(RuntimeError, match="contiguous terrain snapshot"):
        server.replay_map_mutations(joiner)
    assert joiner.disconnect_reason == 13
