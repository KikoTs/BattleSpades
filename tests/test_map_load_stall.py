"""A background map load must not freeze the live server.

Soak 2026-09-26 (docs/SOAK_2026-09-26.md finding 2): every /map and /mode
transition stalled the event loop and ENet for 60-370 ms although the map is
prepared on a worker thread (``MatchTransitionService._load_world_candidate``
via ``asyncio.to_thread``). The Cython VXL parse held the GIL for ~0.4 s and
the Python marker/size walks and spawn surface scans added more. The parse,
floor fill, size scan and marker scan now run with the GIL released, and the
surface scan is a C column probe.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

import pytest

from aoslib.vxl import find_marker_voxels, raw_vxl_size
from server.config import ServerConfig
from server.runtime_vxl import (
    _is_retail_marker_color,
    _iter_explicit_voxels,
    _raw_vxl_size,
)
from server.world_manager import WorldManager


MAPS = Path(__file__).resolve().parents[1] / "maps"


def _load_world(name: str) -> float:
    config = ServerConfig()
    config.maps_path = str(MAPS)
    started = time.perf_counter()
    assert WorldManager(config).load_map(name)
    return time.perf_counter() - started


async def _worst_main_gap_during(name: str) -> tuple[float, float]:
    """Heartbeat the event loop (as the server's tick loop does) while the
    map loads on a worker thread; return (worst gap s, load time s)."""

    task = asyncio.ensure_future(asyncio.to_thread(_load_world, name))
    last = time.perf_counter()
    worst = 0.0
    while not task.done():
        await asyncio.sleep(0.001)
        now = time.perf_counter()
        worst = max(worst, now - last)
        last = now
    return worst, await task


@pytest.mark.parametrize("name", ["MayanJungle", "Classic"])
def test_background_map_load_keeps_main_thread_responsive(name):
    if not (MAPS / f"{name}.vxl").is_file():
        pytest.skip("retail map not present")
    winmm = None
    if sys.platform == "win32":
        # The server raises the timer resolution the same way (main.py);
        # without it every asyncio sleep rounds up to a 15.6 ms tick.
        import ctypes

        winmm = ctypes.windll.winmm
        winmm.timeBeginPeriod(1)
    try:
        _load_world(name)  # warm imports/metadata caches off the clock
        worst, load_s = asyncio.run(_worst_main_gap_during(name))
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
    print(f"{name}: worst main-thread gap {worst * 1000:.1f} ms, load {load_s:.2f} s")
    # Target < 30 ms (measured ~8 ms; the GIL-held parse used to give ~0.4 s).
    assert worst < 0.030


def _variants(data: bytes, rng: random.Random):
    yield data
    yield data[: len(data) // 2]
    yield data[:-3]
    for _ in range(2):
        corrupt = bytearray(data)
        for _ in range(5):
            corrupt[rng.randrange(len(corrupt))] = rng.randrange(256)
        yield bytes(corrupt)


@pytest.mark.parametrize("name", ["20thCenturyTown", "ArcticBase", "TheColosseum", "Classic"])
def test_c_marker_and_size_scans_match_the_python_walkers(name):
    path = MAPS / f"{name}.vxl"
    if not path.is_file():
        pytest.skip("retail map not present")
    rng = random.Random(name)
    for data in _variants(path.read_bytes(), rng):
        expected = [
            (x, y, z, color)
            for x, y, z, color in _iter_explicit_voxels(data)
            if _is_retail_marker_color(color)
        ]
        assert find_marker_voxels(data) == expected
        assert raw_vxl_size(data) == tuple(_raw_vxl_size(data))


def test_surface_scan_matches_get_solid_probe():
    path = MAPS / "MayanJungle.vxl"
    if not path.is_file():
        pytest.skip("retail map not present")
    config = ServerConfig()
    config.maps_path = str(MAPS)
    world = WorldManager(config)
    assert world.load_map("MayanJungle")
    vxl = world.map
    rng = random.Random(4)
    columns = [(rng.randrange(512), rng.randrange(512)) for _ in range(300)]
    # Edits: an overhang above a column and a column dug to the bed.
    x, y = columns[0]
    vxl.set_point(x, y, 3, 0x7F445566)
    x, y = columns[1]
    for z in range(239):
        vxl.remove_point(x, y, z)
    columns += [(-1, 5), (512, 5), (5, 600)]
    for x, y in columns:
        expected = 239
        for z in range(240):
            if 0 <= x < 512 and 0 <= y < 512 and vxl.get_solid(x, y, z):
                expected = z
                break
        assert vxl.surface_z(x, y) == expected
        world._surface_cache.pop((x, y), None)
        assert world._get_surface_z(x, y) == expected
