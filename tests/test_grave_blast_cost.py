"""Grave detonations must stay far below one 60 Hz tick.

Soak 2026-09-26 (docs/SOAK_2026-09-26.md finding 3): ``GraveBehavior.on_tick``
spiked the entity subsystem to 5-37 ms. The cost was the collapse check
``WorldManager.find_unsupported_chunks``: a grave's 3x3x3 crater destroys up
to ~20 cells one by one, and every destroyed cell ran a Python flood that
walked grounded terrain down to the base plane (~1.6 ms each). The flood now
runs in C (``aoslib.vxl.VXL.find_unsupported_chunks``); these tests pin its
output to the Python reference and the detonation cost to < 5 ms.
"""

from __future__ import annotations

import gc
import logging
import random
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.config import ServerConfig
from server.runtime_vxl import ServerVXL
from server.world_manager import WorldManager


MAPS = Path(__file__).resolve().parents[1] / "maps"


class _PythonCollapseWorld(WorldManager):
    """Overriding get_solid keeps find_unsupported_chunks on the Python flood."""

    def get_solid(self, x, y, z):
        return super().get_solid(x, y, z)


def _random_world(rng: random.Random):
    """Random voxel blobs resting on the z=239 bed, some touching map edges."""

    world = WorldManager(ServerConfig())
    world.map = ServerVXL(-1, b"", 0, 2)
    ox = rng.choice([0, 100, 500])
    oy = rng.choice([0, 200, 500])
    size = rng.choice([6, 10, 12])
    density = rng.uniform(0.3, 0.75)
    for x in range(ox, ox + size):
        for y in range(oy, oy + size):
            for z in range(239 - size, 239):
                if rng.random() < density:
                    world.map.set_point(x, y, z, 0x7F112233)
    cells = [
        (x, y, z)
        for x in range(ox, ox + size)
        for y in range(oy, oy + size)
        for z in range(239 - size, 239)
        if world.get_solid(x, y, z)
    ]
    removed = rng.sample(cells, min(len(cells), rng.choice([1, 3, 10, 40])))
    world.destroy_blocks(removed)
    return world, removed


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_c_collapse_matches_the_python_flood(seed, monkeypatch):
    rng = random.Random(seed)
    floating = 0
    for _trial in range(120):
        world, removed = _random_world(rng)
        reference = _PythonCollapseWorld(ServerConfig())
        reference.map = world.map
        # Unlimited and small budgets: exhaustion must drop the same chunks.
        for budget in (WorldManager.COLLAPSE_WORK_BUDGET, rng.randrange(1, 4000)):
            monkeypatch.setattr(WorldManager, "COLLAPSE_WORK_BUDGET", budget)
            expected = reference.find_unsupported_chunks(list(removed))
            assert world.find_unsupported_chunks(list(removed)) == expected
            floating += bool(expected)
    assert floating > 20, "fuzz must exercise floating components"


def test_c_collapse_matches_the_python_flood_on_a_real_map():
    path = MAPS / "Invasion.vxl"
    if not path.is_file():
        pytest.skip("retail map not present")
    config = ServerConfig()
    config.maps_path = str(MAPS)
    world = WorldManager(config)
    assert world.load_map("Invasion")
    reference = _PythonCollapseWorld(config)
    reference.map = world.map
    rng = random.Random(9)
    floating = 0
    for _trial in range(40):
        x, y = rng.randrange(20, 490), rng.randrange(20, 490)
        z = world.get_height(x, y)
        if z >= 236:
            continue
        # Undercut a 5x5 cap: floating when its rim is cut too.
        removed = [(x + dx, y + dy, z + 2) for dx in range(-2, 3) for dy in range(-2, 3)]
        for zz in (z, z + 1):
            removed += [(x + dx, y + dy, zz) for dx in (-3, 3) for dy in range(-3, 4)]
            removed += [(x + dx, y + dy, zz) for dx in range(-2, 3) for dy in (-3, 3)]
        world.destroy_blocks(removed)
        expected = reference.find_unsupported_chunks(removed)
        assert world.find_unsupported_chunks(removed) == expected
        floating += bool(expected)
        for chunk in expected:
            world.destroy_blocks(chunk)
    assert floating > 0


class _Peer:
    def __init__(self, player_id):
        self.player = SimpleNamespace(id=player_id, team=0)
        self.in_game = True
        self.known_entity_ids = set()

    def send(self, data, reliable=True):
        pass


def test_grave_detonation_costs_under_five_ms():
    """Real graves through GraveBehavior -> _detonate_deployable on Invasion
    (the soak's worst map), three blasts per spot so the later ones destroy
    the cells the first one cracked (block damage 3 vs map health 5)."""

    if not (MAPS / "Invasion.vxl").is_file():
        pytest.skip("retail map not present")
    from server.entities.behaviors import GraveBehavior, _detonate_deployable
    from server.main import BattleSpadesServer

    config = ServerConfig()
    config.maps_path = str(MAPS)
    server = BattleSpadesServer(config)
    assert server.world_manager.load_map("Invasion")
    server.connections = {pid: _Peer(pid) for pid in range(1, 17)}
    world = server.world_manager
    rng = random.Random(5)
    spots = []
    while len(spots) < 40:
        x, y = rng.randrange(40, 470), rng.randrange(40, 470)
        z = world.get_height(x, y)
        if z < 238:
            spots.append((x + 0.5, y + 0.5, z - 0.5))

    def detonate(spot):
        grave = server.entity_registry.place(
            int(C.GRAVE_ENTITY), *spot, kind="grave",
            behavior=GraveBehavior(thrower_id=-1, explosion_center=spot),
        )
        ctx = server._build_entity_ctx()
        started = time.perf_counter()
        _detonate_deployable(grave.behavior, grave, ctx)
        return (time.perf_counter() - started) * 1000.0

    logging.disable(logging.INFO)
    gc.disable()
    try:
        timings = [detonate(spot) for _ in range(3) for spot in spots]
    finally:
        gc.enable()
        logging.disable(logging.NOTSET)
    # The repeated blasts really destroyed terrain (and ran the collapse).
    assert len(world.dirty_columns) > 100
    timings.sort()
    print(
        f"grave detonation: median {timings[len(timings) // 2]:.2f} ms, "
        f"max {timings[-1]:.2f} ms over {len(timings)}"
    )
    assert timings[-1] < 5.0
