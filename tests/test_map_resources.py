"""Mode-neutral map pickup and static-light regression tests."""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

import shared.constants as C

from server.entities.registry import EntityRegistry
from server.game_constants import MAX_HEALTH, TEAM_NEUTRAL
from server.map_metadata import MapEntitySpec, MapMetadata
from server.map_resources import MapResourceService


class _Map:
    source_z_shift = 2
    retail_marker_families = (
        (40, 50, 60, 0),
        (41, 51, 61, 1),
    )


class _World:
    map_name = "Fixture"

    def __init__(self, metadata: MapMetadata | None = None):
        self.map = _Map()
        self.map_metadata = metadata or MapMetadata()

    def team_base_anchor(self, team: int):
        return (64.0, 100.0, 50.0) if team == 0 else (448.0, 400.0, 50.0)

    def dry_surface_anchor(self, x: float, y: float):
        return (float(x), float(y), 55.0)


class _Server:
    def __init__(self, metadata: MapMetadata | None = None, *, wire=True):
        self.config = SimpleNamespace(entities_wire_ready=wire)
        self.world_manager = _World(metadata)
        self.entity_registry = EntityRegistry()
        self.created = []
        self.destroyed = []

    def broadcast_create_entity(self, entity):
        self.created.append(entity)

    def broadcast_destroy_entity(self, entity_id: int):
        self.destroyed.append(entity_id)


class _Player:
    def __init__(self):
        self.calls = []

    def restock_ammo(self, restock_type=0):
        self.calls.append(("ammo", restock_type))

    def heal(self, amount):
        self.calls.append(("health", amount))

    def restock_blocks(self):
        self.calls.append(("blocks", None))

    def restock_jetpack(self):
        self.calls.append(("jetpack", None))


def test_fallback_resources_exist_for_every_mode_boundary():
    server = _Server()

    MapResourceService(server).rebuild()

    assert Counter(entity.kind for entity in server.entity_registry.all()) == {
        "map_ammo": 3,
        "map_health": 3,
        "map_block": 3,
    }
    assert len(server.created) == 9


def test_each_pickup_refills_only_its_own_resource():
    behaviors = MapResourceService._behaviors()
    player = _Player()

    for entity_type in (
        int(C.AMMO_CRATE), int(C.HEALTH_CRATE), int(C.BLOCK_CRATE),
        int(C.JETPACK_CRATE),
    ):
        behaviors[entity_type][1].refill(player)

    assert player.calls == [
        ("ammo", int(C.AMMO_CRATE)),
        ("health", MAX_HEALTH),
        ("blocks", None),
        ("jetpack", None),
    ]


def test_authored_pickups_and_chroma_lights_replicate_with_map_values():
    metadata = MapMetadata(
        static_light_colors={0: (224, 172, 29)},
        entities=[
            MapEntitySpec(int(C.AMMO_CRATE), "ammo", 10, 20, 30, "ammo"),
            MapEntitySpec(
                int(C.FLARE_BLOCK), "static_flare", 11, 21, 31,
                "flare_block", (1, 2, 3),
            ),
        ],
    )
    server = _Server(metadata)

    MapResourceService(server).rebuild()

    entities = server.entity_registry.all()
    assert Counter(entity.kind for entity in entities) == {
        "map_ammo": 1,
        "map_flare": 2,
    }
    authored_flare = next(
        entity for entity in entities
        if entity.kind == "map_flare" and entity.color == (1, 2, 3)
    )
    assert (authored_flare.x, authored_flare.y, authored_flare.z) == (11, 21, 33)
    marker_flare = next(
        entity for entity in entities
        if entity.kind == "map_flare" and entity.color == (224, 172, 29)
    )
    assert (marker_flare.x, marker_flare.y, marker_flare.z) == (40, 50, 60)
    assert all(entity.state == TEAM_NEUTRAL for entity in entities)


def test_rebuild_replaces_only_map_owned_entities():
    server = _Server()
    deployable = server.entity_registry.place(
        int(C.LANDMINE_ENTITY), 1, 2, 3, kind="landmine",
    )
    service = MapResourceService(server)
    service.rebuild()
    old_resource_ids = {
        entity.entity_id for entity in server.entity_registry.all()
        if entity.kind.startswith("map_")
    }

    service.rebuild()

    assert server.entity_registry.get(deployable.entity_id) is deployable
    assert old_resource_ids.issubset(set(server.destroyed))
    assert len([
        entity for entity in server.entity_registry.all()
        if entity.kind.startswith("map_")
    ]) == 9
