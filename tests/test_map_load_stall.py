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
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
import server.world_manager as world_manager


MAPS = Path(__file__).resolve().parents[1] / "maps"


def test_main_thread_spawn_scan_never_sleeps_or_reads_clock(monkeypatch):
    def unexpected_call(*args):
        pytest.fail("main-thread spawn selection must remain synchronous")

    monkeypatch.setattr(world_manager, "_scan_clock", unexpected_call)
    monkeypatch.setattr(world_manager, "_scan_yield", unexpected_call)
    assert list(world_manager._spawn_scan_columns(1, 6, 2, 7, 2)) == [
        (1, 2), (1, 4), (1, 6), (3, 2), (3, 4), (3, 6),
        (5, 2), (5, 4), (5, 6),
    ]


def test_background_spawn_scan_yields_within_budget_and_preserves_order(monkeypatch):
    now = 0.0
    pauses = []

    def read_clock():
        return now

    def pause(delay):
        pauses.append((now, delay))

    def scan():
        nonlocal now
        columns = []
        for column in world_manager._spawn_scan_columns(1, 6, 2, 7, 2):
            columns.append(column)
            now += 0.001  # Each terrain probe consumes one millisecond.
        return columns

    monkeypatch.setattr(world_manager, "_scan_clock", read_clock)
    monkeypatch.setattr(world_manager, "_scan_yield", pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        columns = executor.submit(scan).result()
    assert columns == [(x, y) for x in range(1, 6, 2) for y in range(2, 7, 2)]
    assert len(pauses) >= 3
    previous = 0.0
    for at, delay in pauses:
        assert 0.002 <= at - previous < 0.0031
        assert delay == 0.001
        previous = at
    assert now - previous < 0.0031


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


async def _sample_main_gaps(name: str) -> list[tuple[float, float]]:
    """Use a fixed sample set so shared-runner scheduling cannot pick a pass."""
    return [await _worst_main_gap_during(name) for _ in range(5)]


def test_responsiveness_probe_detects_gil_held_work(monkeypatch):
    import ctypes

    def blocking_load(name):
        # PyDLL, unlike CDLL, retains the GIL throughout the native call.
        if sys.platform == "win32":
            ctypes.PyDLL("kernel32.dll").Sleep(100)
        else:
            ctypes.PyDLL(None).usleep(100_000)
        return 0.1

    monkeypatch.setattr(sys.modules[__name__], "_load_world", blocking_load)
    samples = asyncio.run(_sample_main_gaps("negative control"))
    assert statistics.median(gap for gap, _ in samples) >= 0.030


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
        samples = asyncio.run(_sample_main_gaps(name))
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
    gaps_ms = [round(gap * 1000, 1) for gap, _ in samples]
    print(f"{name}: five worst main-thread gaps {gaps_ms} ms")
    # Keep the 30 ms budget. A fixed five-sample median rejects repeatable
    # GIL stalls while tolerating occasional descheduling by shared CI hosts.
    # The native GIL-held negative control above guards the measurement.
    assert statistics.median(gap for gap, _ in samples) < 0.030


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
