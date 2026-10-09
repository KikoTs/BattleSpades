"""Legacy import must preserve world geometry, colours and network coordinates."""

from __future__ import annotations

import struct
from types import SimpleNamespace
import zlib

import pytest

from aoslib.vxl import VXL, raw_vxl_size
from server.bot_ai.compact_vxl import CompactVoxelMap
from server.runtime_vxl import ServerVXL
from server.world_manager import WorldManager


AREA = 512 * 512
CLASSIC_WATER = bytes((0, 64, 63, 0))
RETAIL_WATER = bytes((0, 240, 239, 0))
GREEN = 0x8000FF00
BLUE = 0x800000FF
EARTH = 0x80604020


def _word(color: int) -> bytes:
    return struct.pack("<I", color)


def _legacy(first: bytes) -> bytes:
    return first + CLASSIC_WATER * (AREA - 1)


def _manager(tmp_path, monkeypatch) -> WorldManager:
    manager = WorldManager(SimpleNamespace(maps_path=str(tmp_path), game_mode="ctf"))
    # Terrain import/network representation do not depend on spawn searches.
    monkeypatch.setattr(manager, "prewarm_spawn_candidates", lambda: None)
    return manager


def test_classic_water_and_shallow_terrain_use_fixed_height_and_keep_chroma():
    raw = _legacy(bytes((0, 20, 21, 0)) + _word(GREEN) + _word(BLUE))
    native = ServerVXL(None, raw, len(raw))
    compact = CompactVoxelMap(raw, source_format="classic64")
    assert native.source_format == "classic64"
    assert native.source_z_shift == compact.source_z_shift == 176
    assert native.retail_marker_positions == ()
    for z in range(240):
        assert native.get_solid(0, 0, z) == compact.get_solid(0, 0, z) == (z >= 196)
        assert native.get_solid(1, 0, z) == compact.get_solid(1, 0, z) == (z == 239)
    assert native.get_color(0, 0, 196) == GREEN
    assert native.get_color(0, 0, 197) == BLUE


def test_classic_cave_bottom_runs_and_implicit_interior_are_preserved():
    # Ceiling at 10..19, air at 20..39, ground at 40..63.
    first = bytes((4, 10, 11, 0)) + _word(GREEN) + _word(EARTH) + _word(BLUE)
    first += bytes((0, 40, 40, 20)) + _word(EARTH)
    raw = _legacy(first)
    native = ServerVXL(None, raw, len(raw))
    compact = CompactVoxelMap(raw, source_format="classic64")
    for z in range(240):
        expected = 186 <= z < 196 or z >= 216
        assert native.get_solid(0, 0, z) == compact.get_solid(0, 0, z) == expected
    assert native.get_color(0, 0, 195) == BLUE
    assert native.get_color(0, 0, 189) == EARTH
    assert native.generate_vxl(False) == raw


def test_classic_map_without_explicit_floor_still_keeps_its_altitude():
    raw = (bytes((0, 20, 20, 0)) + _word(EARTH)) * AREA
    native = VXL(None, raw, len(raw), source_format="auto")
    assert native.ready and native.source_format == "classic64"
    assert native.get_solid(200, 300, 196)
    assert not native.get_solid(200, 300, 195)


def test_colored_z64_is_a_short_retail_map_not_a_classic_water_sentinel():
    raw = (bytes((0, 64, 64, 0)) + _word(EARTH)) * AREA
    native = ServerVXL(None, raw, len(raw))
    compact = CompactVoxelMap(raw, source_format="auto")
    assert native.source_format == "retail"
    assert native.source_z_shift == compact.source_z_shift == 175
    assert native.get_solid(0, 0, 239) and compact.get_solid(0, 0, 239)


@pytest.mark.parametrize("raw", [
    _legacy(bytes((0, 64, 64, 0)) + _word(EARTH)),
    _legacy(bytes((1, 20, 20, 0)) + CLASSIC_WATER),
    _legacy(bytes((3, 10, 10, 0)) + _word(EARTH) * 2 + bytes((0, 40, 40, 10)) + _word(EARTH)),
    _legacy(bytes((0, 40, 20, 0))),
    CLASSIC_WATER * (AREA - 1),
    CLASSIC_WATER * AREA + b"\0",
    CLASSIC_WATER * AREA + CLASSIC_WATER,
], ids=["z-out-of-range", "negative-bottom-length", "overlapping-span", "inverted-top",
        "missing-column", "trailing-byte", "extra-column"])
def test_malformed_classic_maps_fail_closed(raw):
    with pytest.raises(ValueError):
        ServerVXL(None, raw, len(raw), source_format="classic64")
    with pytest.raises(ValueError):
        CompactVoxelMap(raw, source_format="classic64")


def test_explicit_retail_override_retains_existing_short_map_semantics():
    raw = _legacy(bytes((0, 20, 20, 0)) + _word(GREEN))
    native = ServerVXL(None, raw, len(raw), source_format="retail")
    assert native.source_format == "retail"
    assert native.source_z_shift == 175
    assert (0, 0, 195) in native.retail_marker_positions
    assert not native.get_solid(0, 0, 195)


@pytest.mark.parametrize("filename", ["legacy.vxl", "legacy.VXL"])
def test_native_map_load_and_sync_translate_once_and_keep_original_crc(tmp_path, monkeypatch, filename):
    raw = _legacy(bytes((0, 20, 20, 0)) + _word(GREEN))
    (tmp_path / filename).write_bytes(raw)
    manager = _manager(tmp_path, monkeypatch)
    assert manager.load_map(filename)
    assert manager.map_name == "legacy"
    assert manager.map.source_format == "classic64"
    assert manager.map_raw_bytes == raw
    assert manager.map_file_crc == zlib.crc32(raw)
    sync = zlib.decompress(b"".join(manager.capture_map_sync((), full=True).build_chunks()))
    assert sync[:16] == struct.pack("<II", 0, 0) + bytes((0, 196, 196, 0)) + _word(GREEN)
    assert sync[16:28] == struct.pack("<II", 1, 0) + RETAIL_WATER


def test_failed_load_keeps_previous_world_and_metadata(tmp_path, monkeypatch):
    (tmp_path / "good.vxl").write_bytes(CLASSIC_WATER * AREA)
    (tmp_path / "bad.vxl").write_bytes(CLASSIC_WATER * (AREA - 1))
    (tmp_path / "empty.vxl").write_bytes(b"")
    manager = _manager(tmp_path, monkeypatch)
    assert manager.load_map("good")
    previous = manager.map, manager.map_name, manager.map_file_crc, manager.map_metadata
    for name in ("bad", "empty"):
        assert not manager.load_map(name)
        assert (manager.map, manager.map_name, manager.map_file_crc, manager.map_metadata) == previous


def test_sidecar_can_disambiguate_old_retail_and_classic_files(tmp_path, monkeypatch):
    (tmp_path / "short.vxl").write_bytes(CLASSIC_WATER * AREA)
    (tmp_path / "short.txt").write_text('vxl_format = "retail"\n', encoding="utf-8")
    manager = _manager(tmp_path, monkeypatch)
    assert manager.load_map("short")
    assert manager.map.source_format == "retail"
    assert manager.map.source_z_shift == 175
    (tmp_path / "short.txt").write_text('vxl_format = "classic64"\n', encoding="utf-8")
    assert manager.load_map("short")
    assert manager.map.source_format == "classic64"
    assert manager.map.source_z_shift == 176


def test_full_sync_sends_finalized_retail_markers_in_canonical_world_coordinates(tmp_path, monkeypatch):
    # Retail marker at 20 is removed, leaving air above the ground at 40.
    first = bytes((2, 20, 20, 0)) + _word(GREEN)
    first += bytes((0, 40, 40, 21)) + _word(EARTH)
    raw = first + RETAIL_WATER * (AREA - 1)
    assert raw_vxl_size(raw) == (AREA, 240)
    (tmp_path / "retail.vxl").write_bytes(raw)
    manager = _manager(tmp_path, monkeypatch)
    assert manager.load_map("retail")
    assert manager.map.retail_marker_positions == ((0, 0, 20),)
    snapshot = manager.capture_map_sync((), full=True)
    assert snapshot.columns == frozenset({(0, 0)})
    sync = zlib.decompress(b"".join(snapshot.build_chunks()))
    assert sync[:16] == struct.pack("<II", 0, 0) + bytes((0, 40, 40, 0)) + _word(EARTH)
    assert manager.map_raw_bytes == raw
    delta = manager.capture_map_sync((), full=False)
    assert delta.columns == snapshot.columns
    assert zlib.decompress(b"".join(delta.build_chunks())) == sync[:16]

    # A live vivid-green built cell is ordinary terrain, even in a column
    # that originally contained a retail marker. A late join keeps its RGB.
    assert manager.set_block(0, 0, 20, True, GREEN)
    current = zlib.decompress(b"".join(manager.capture_map_sync(manager.dirty_columns, full=True).build_chunks()))
    assert current[:16] == struct.pack("<II", 0, 0) + bytes((2, 20, 20, 0)) + _word(GREEN)
