"""Source coverage and native collision/respawn checks for stock-map crates."""

from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.config import ServerConfig
from server.entities.behaviors import PickupCrateBehavior
from server.entities.registry import EntityContext, EntityRegistry
from server.map_metadata import load_map_metadata
from server.map_resources import MapResourceService
from server.world_manager import WorldManager
from tools.map_metadata.import_wiki_pickups import MANIFEST, ROOT, sidecar_payload

# Independent counts transcribed from the rendered source articles, H/A/B.
COUNTS = {
    "ArcticBase": (5, 4, 3), "Atlantis": (5, 5, 4), "BlockNess": (5, 4, 3),
    "BranCastle": (3, 3, 2), "CastleWars": (6, 6, 6), "Classic": (5, 5, 2),
    "Crossroads": (8, 16, 3), "TheColosseum": (2, 3, 2), "DoubleDragon": (8, 8, 5),
    "Frontier": (5, 7, 4), "GreatWall": (4, 5, 4), "Hiesville": (5, 5, 5),
    "Invasion": (10, 15, 5), "London": (6, 6, 3), "LunarBase": (5, 5, 2),
    "TokyoNeon": (5, 5, 5), "ToTheBridge": (4, 4, 4), "WinterValley": (7, 7, 2),
    "WW1": (3, 3, 2), "CityOfChicago": (10, 14, 6),
}
ENTRIES = {row["name"]: row for row in json.loads(MANIFEST.read_text())["maps"]}


@pytest.fixture(scope="module", params=sorted(COUNTS))
def layout(request):
    name = request.param
    config = ServerConfig(default_mode="vip" if name == "CityOfChicago" else "tdm")
    world = WorldManager(config)
    assert world.load_map(name)
    server = SimpleNamespace(config=config, world_manager=world,
                             entity_registry=EntityRegistry(),
                             broadcast_create_entity=lambda _ent: None)
    service = MapResourceService(server)
    service._place_pickups()
    return name, world, server.entity_registry


def test_every_documented_missing_layout_is_authored_in_all_supported_modes():
    assert set(ENTRIES) == set(COUNTS)
    for name, counts in COUNTS.items():
        path = ROOT / "maps" / f"{name}.vxl"
        modes = load_map_metadata(path, "tdm").retail_modes
        for mode in modes:
            metadata = load_map_metadata(path, mode)
            assert Counter(spec.kind for spec in metadata.entities
                           if spec.kind in {"health", "ammo", "block"}) == dict(
                               zip(("health", "ammo", "block"), counts))
        assert sidecar_payload(path.parent, ENTRIES[name]) == json.loads(
            path.with_suffix(".json").read_text())


def test_wiki_points_have_headroom_and_supported_floors(layout):
    name, world, registry = layout
    points = ENTRIES[name]["points"]
    assert len(registry.all()) == sum(COUNTS[name])
    for point in points:
        x, y, z = point["position"]
        assert math.dist((x, y), point["wiki_xy"]) <= 8
        cx, cy, cz = math.floor(x), math.floor(y), int(z)
        assert 0 <= x < 512 and 0 <= y < 512 and 4 <= z <= 239
        if point["floor"] == "documented water column":
            assert z == 239 and world.get_height(cx, cy) == 239
        else:
            assert world.get_solid(cx, cy, cz), (name, point)
            assert all(not world.get_solid(cx, cy, cz - h) for h in range(1, 5)), (name, point)
    # Prevent coincident or effectively merged drops after terrain review.
    positions = [point["position"] for point in points]
    assert all(math.dist(a, b) > 2.5 for i, a in enumerate(positions)
               for b in positions[i + 1:]), name


def test_native_placement_and_respawn_preserve_xy_and_reviewed_floor(layout):
    name, world, registry = layout
    entities = registry.all()
    expected = Counter((point["type"], *point["position"]) for point in ENTRIES[name]["points"])
    def positions():
        return Counter((ent.kind.removeprefix("map_"), ent.x, ent.y, ent.z)
                       for ent in entities)
    assert positions() == expected  # Includes compact-VXL shift exactly once.
    for tick in range(2):
        registry.tick(EntityContext(dt=1 / 60, now=1000 + tick / 60, players=[], world=world))
    assert positions() == expected  # Original roofs must not move crates.
    for ent in entities:
        ent.alive = False
        ent.respawn_at = 1001.0
    for tick in range(601):
        registry.tick(EntityContext(dt=1 / 60, now=1001 + tick / 60, players=[], world=world))
    assert all(ent.alive and not ent.falling for ent in entities), name
    assert positions() == expected


class _Room:
    def __init__(self):
        self.topology_version = 0
        self.runtime_solid_cells = set()

    def get_solid(self, x, y, z):
        return int(z) == 180 or int(z) >= 215 or (x, y, int(z)) in self.runtime_solid_cells

    def is_runtime_solid(self, x, y, z):
        return (x, y, int(z)) in self.runtime_solid_cells

    def get_height(self, x, y):
        return next(z for z in range(240) if self.get_solid(x, y, z))

    def build(self, z):
        self.runtime_solid_cells.add((128, 252, z))
        self.topology_version += 1


@pytest.mark.parametrize("start_z", (215.0, 215.75))
def test_indoor_drop_ignores_roof_and_builds_above_it_but_rises_to_room_platform(start_z):
    world = _Room()
    registry = EntityRegistry()
    crate = registry.place(int(C.AMMO_CRATE), 128.5, 252.5, start_z,
                           behavior=PickupCrateBehavior(lambda _: None))
    for now in (1.0, 2.0):
        registry.tick(EntityContext(dt=1 / 60, now=now, players=[], world=world))
        assert crate.z == start_z
    world.build(170)  # Above the original roof: unrelated to this indoor drop.
    registry.tick(EntityContext(dt=1 / 60, now=3.0, players=[], world=world))
    assert crate.z == start_z
    world.build(205)  # A new platform inside this room remains accessible.
    registry.tick(EntityContext(dt=1 / 60, now=4.0, players=[], world=world))
    assert crate.z == 205
    assert crate.home == (128.5, 252.5, start_z)
