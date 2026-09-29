"""Implicit VXL interior voxels take a per-column fill colour instead of a table entry.

The loader used to store a colour for every solid voxel below the surface
(MayanJungle: 286 MB). Only the explicit surface runs of the file are stored
now; a solid voxel without an entry reports its column's fill colour (the
deepest surface colour, as before), and dirty-column serialization leaves the
interior implicit exactly as the map file does.
"""

from __future__ import annotations

import gc
import os
import struct
from pathlib import Path

import pytest

from aoslib.vxl import VXL

MAPS = Path(__file__).resolve().parents[1] / "maps"


def _raw_columns(data: bytes):
    """Yield (x, y, column_bytes) for every column of a raw 512x512 VXL."""
    position = 0
    limit = len(data)
    for y in range(512):
        for x in range(512):
            start = position
            while True:
                span_words = data[position]
                top_start = data[position + 1]
                top_end = data[position + 2]
                top_len = top_end - top_start + 1 if top_end >= top_start else 0
                if span_words == 0:
                    position += 4 + top_len * 4
                    break
                position += span_words * 4
            yield x, y, data[start:position]
    assert position == limit


@pytest.fixture(scope="module")
def arctic():
    path = MAPS / "ArcticBase.vxl"
    if not path.is_file():
        pytest.skip("stock ArcticBase.vxl not present")
    data = path.read_bytes()
    return data, VXL(None, data, 0)


def test_interior_voxels_have_no_table_entry_but_report_the_column_fill(arctic):
    data, world = arctic
    solid = 0
    checked = 0
    for x, y, column in _raw_columns(data):
        if (x * 7 + y * 13) % 97:
            continue
        top_start, top_end = column[1], column[2]
        if column[0] != 0 or top_end < top_start:
            continue  # multi-span or empty column; keep the sample simple
        deepest = struct.unpack_from("<I", column, 4 + (top_end - top_start) * 4)[0]
        for z in range(top_end + 1, 240):
            assert world.get_solid(x, y, z)
            assert world.get_color(x, y, z) == deepest
            assert world.get_color(x, y, z) != 0
            solid += 1
        assert world.column_fill_color(x, y) == deepest
        checked += 1
    assert checked > 100 and solid > 10_000
    # Explicit entries are a small fraction of the solid voxels.
    assert world.color_entries() < 4_000_000


def _file_colours(column: bytes) -> dict[int, int]:
    """z -> authored colour for every explicit voxel of one raw column."""
    colours = {}
    position = 0
    while True:
        span_words, top_start, top_end = column[position], column[position + 1], column[position + 2]
        top_len = top_end - top_start + 1 if top_end >= top_start else 0
        for i in range(top_len):
            colours[top_start + i] = struct.unpack_from("<I", column, position + 4 + i * 4)[0]
        if span_words == 0:
            return colours
        bottom_len = span_words - top_len - 1
        next_air = column[position + span_words * 4 + 3]
        bottom_start = next_air - bottom_len
        for i in range(bottom_len):
            colours[bottom_start + i] = struct.unpack_from(
                "<I", column, position + 4 + (top_len + i) * 4)[0]
        position += span_words * 4


def test_pristine_columns_serialize_like_the_file(arctic):
    """Single-span columns are byte-identical; every authored colour survives."""
    data, world = arctic
    mismatches = []
    sampled = 0
    exact = 0
    for x, y, column in _raw_columns(data):
        if (x * 3 + y * 5) % 151:
            continue
        if column == bytes((0, 240, 239, 0)):
            # Open water: the server keeps the client-style z=239 bed voxel
            # in memory, so it re-serializes as one explicit bed (unchanged
            # behaviour, verified live on water maps).
            continue
        sampled += 1
        encoded = world.serialize_columns([(x, y)])
        assert encoded[:8] == struct.pack("<II", x, y)
        if encoded[8:] == column:
            exact += 1
            continue
        if column[0] == 0:
            mismatches.append((x, y, column.hex(), encoded[8:].hex()))
            continue
        # Multi-span columns (converted maps encode mid-column colours with
        # an empty final span) re-serialize into one span carrying the
        # same geometry and every authored colour.
        # Pad to a 2x2 source so the loader's short-map normalization does
        # not shift a lone column whose interior is implicit.
        padded = encoded[8:] + bytes((0, 240, 239, 0)) * 3
        decoded = VXL(-1, padded, len(padded), 2)
        assert decoded.ready
        for z in range(240):
            assert decoded.get_solid(255, 255, z) == world.get_solid(x, y, z), (x, y, z)
        for z, colour in _file_colours(column).items():
            assert decoded.get_color(255, 255, z) == colour, (x, y, z)
    assert sampled > 1000
    assert exact > sampled * 0.9
    assert mismatches == []


def test_dug_surface_exposes_fill_colour_and_stays_a_valid_last_span(arctic):
    data, world = arctic
    # Pick a plain single-span land column.
    target = next((x, y, c) for x, y, c in _raw_columns(data)
                  if c[0] == 0 and c[2] >= c[1] and c[2] < 230 and (x, y) > (100, 100))
    x, y, column = target
    top_start, top_end = column[1], column[2]
    fill = world.column_fill_color(x, y)
    for z in range(top_start, top_end + 1):
        world.remove_point(x, y, z)
    exposed = top_end + 1
    assert world.get_solid(x, y, exposed)
    assert world.get_color(x, y, exposed) == fill
    encoded = world.serialize_columns([(x, y)])[8:]
    # One last span whose explicit run is exactly the exposed voxel, then implicit fill.
    assert encoded[0] == 0
    assert encoded[1] == exposed and encoded[2] == exposed
    assert struct.unpack_from("<I", encoded, 4)[0] == fill
    assert len(encoded) == 8


def test_mayan_jungle_load_stays_under_the_memory_budget():
    psutil = pytest.importorskip("psutil")
    path = MAPS / "MayanJungle.vxl"
    if not path.is_file():
        pytest.skip("stock MayanJungle.vxl not present")
    process = psutil.Process(os.getpid())
    gc.collect()
    before = process.memory_info().rss
    world = VXL(None, path.read_bytes(), 0)
    gc.collect()
    delta = process.memory_info().rss - before
    assert world.ready
    assert world.color_entries() < 6_000_000
    assert delta < 120 * 1024 * 1024, f"MayanJungle took {delta / 1e6:.0f} MB"
    del world
