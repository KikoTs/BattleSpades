"""Read-only map-load timing attribution for the Beta 0.2 macOS release gate."""
from __future__ import annotations

import asyncio
import ctypes
import gc
import json
from pathlib import Path
import sys
import time
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_map_load_stall import _load_world
from server.config import ServerConfig
import server.world_manager as wm

phase = "idle"
events: list[tuple[float, str]] = []


def mark(value: str) -> None:
    """Record a phase boundary without doing console I/O in the sample."""
    global phase
    phase = value
    events.append((time.perf_counter(), value))


def traced(label: str, operation: Callable[..., object]) -> Callable[..., object]:
    """Attribute a heartbeat gap to the loader stage active at that time."""
    def call(*args: object, **kwargs: object) -> object:
        mark(label)
        try:
            return operation(*args, **kwargs)
        finally:
            mark(label + ":done")
    return call


wm.VXL = traced("VXL", wm.VXL)
wm.load_map_metadata = traced("metadata", wm.load_map_metadata)
wm.WorldManager.prewarm_spawn_candidates = traced(
    "spawns", wm.WorldManager.prewarm_spawn_candidates)


def retained_load(name: str) -> wm.WorldManager:
    """Mirror the production transition returning its live map candidate."""
    config = ServerConfig()
    config.maps_path = str(ROOT / "maps")
    world = wm.WorldManager(config)
    assert world.load_map(name)
    mark("candidate ready")
    return world


def gc_event(state: str, info: dict[str, int]) -> None:
    """Expose collection pauses without changing the collection policy."""
    events.append((time.perf_counter(), f"GC {state} {info['generation']}"))


async def measure(label: str, operation: Callable[[], object], warm: bool) -> None:
    """Measure one operation with the existing one-millisecond heartbeat."""
    if warm:
        await asyncio.to_thread(lambda: None)
    events.clear()
    mark("enqueue")
    task = asyncio.create_task(asyncio.to_thread(operation))
    started = last = time.perf_counter()
    gaps: list[tuple[float, float, str]] = []
    while not task.done():
        before = phase
        await asyncio.sleep(0.001)
        now = time.perf_counter()
        gaps.append((now - last, last - started, before + " -> " + phase))
        last = now
    result = await task  # Keep a returned candidate alive through the measurement.
    print(json.dumps({"label": label, "warm_executor": warm,
                      "elapsed": time.perf_counter() - started,
                      "worst": max(g[0] for g in gaps), "samples": len(gaps),
                      "largest": sorted(gaps, reverse=True)[:5],
                      "events": [(stamp - started, name) for stamp, name in events]}),
          flush=True)
    del result


def gil_held_sleep() -> None:
    """Negative control: PyDLL deliberately retains the GIL during this wait."""
    if sys.platform == "win32":
        ctypes.PyDLL("kernel32.dll").Sleep(150)
    else:
        ctypes.PyDLL(None).usleep(150_000)


def main() -> None:
    """Compare the original fixture, production lifetime and scheduling controls."""
    for name in ("MayanJungle", "Classic"):
        _load_world(name)
    gc.callbacks.append(gc_event)
    try:
        for repeat in range(5):
            asyncio.run(measure(f"baseline:{repeat}", lambda: time.sleep(0.4), True))
            for name in ("MayanJungle", "Classic"):
                asyncio.run(measure(f"original:{name}:{repeat}",
                                    lambda name=name: _load_world(name), False))
                gc.collect()
                asyncio.run(measure(f"retained:{name}:{repeat}",
                                    lambda name=name: retained_load(name), True))
        asyncio.run(measure("GIL negative control", gil_held_sleep, True))
    finally:
        gc.callbacks.remove(gc_event)


if __name__ == "__main__":
    main()
