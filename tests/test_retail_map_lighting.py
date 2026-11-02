"""Original map light data stays separate from decorative/emissive terrain."""
import pytest

import shared.constants as C
from server.config import ServerConfig
from server.main import BattleSpadesServer


@pytest.mark.parametrize("name,expected", [("SpookyMansion", 78), ("MayanJungle", 24)])
def test_retail_marker_lights_use_original_palette_without_colour_cluster_inference(name, expected):
    server = BattleSpadesServer(ServerConfig())
    world = server.world_manager
    assert world.load_map(name)
    environment = (world.map_metadata.light_color, world.map_metadata.ambient_light_color,
                   world.map_metadata.ambient_light_intensity)
    markers = {(x, y, z): world.map_metadata.static_light_colors[family]
               for x, y, z, family in world.map.retail_marker_families}
    fixtures = [s for s in world.map_metadata.entities if s.kind == "map_fixture_light"]
    fixture_cells = {(int(s.x), int(s.y), int(s.z)): s.color for s in fixtures}
    if name == "SpookyMansion":
        assert len(fixture_cells) == 77
        assert set(fixture_cells.values()) == {(125, 125, 100)}
        assert all(world.get_color(x, y, z) & 0xFFFFFF == 0xFAFAC8 for x, y, z in fixture_cells)
        assert all(world.get_color(x, y, z + 1) & 0xFFFFFF in (0x422906, 0x8F6C03)
                   for x, y, z in fixture_cells)
    else:
        assert len(fixture_cells) == 20
        assert set(fixture_cells.values()) == {(112, 86, 14)}
    assert len(markers) + len(fixture_cells) == expected
    server.map_resources.rebuild()
    lights = [e for e in server.entity_registry.all() if e.kind == "map_flare"]
    assert len(lights) == expected
    assert {(int(e.x), int(e.y), int(e.z)): e.color for e in lights} == markers | fixture_cells
    assert all(e.type == int(C.FLARE_BLOCK) and e.wire_visible for e in lights)
    assert all(world.get_color(*cell) & 0xFFFFFF == (color[0] << 16) | (color[1] << 8) | color[2]
               for cell, color in fixture_cells.items())
    assert environment == (world.map_metadata.light_color, world.map_metadata.ambient_light_color,
                           world.map_metadata.ambient_light_intensity)
    assert not any(spec.kind == "static_fixture_light" for spec in world.map_metadata.entities)


def test_breaking_a_reviewed_light_top_removes_its_light_without_rebuilding_the_prop():
    from types import SimpleNamespace
    server = BattleSpadesServer(ServerConfig())
    world = server.world_manager
    assert world.load_map("SpookyMansion")
    server.map_resources.rebuild()
    lamp = next(e for e in server.entity_registry.all() if e.kind == "map_flare" and e.behavior is not None)
    cell = (int(lamp.x), int(lamp.y), int(lamp.z))
    world.map.remove_point_nochecks(*cell)
    removed = []
    lamp.behavior.on_tick(lamp, .1, SimpleNamespace(world=world, server=server, destroy=removed.append))
    assert removed == [lamp.entity_id] and not lamp.alive
    assert cell not in world.static_light_cells
    server.map_resources.rebuild()
    assert not world.get_solid(*cell)
    assert not any((int(e.x), int(e.y), int(e.z)) == cell for e in server.entity_registry.all())


def test_map_light_annotation_is_negotiated_and_never_sent_to_the_original_client():
    from types import SimpleNamespace
    from server.entities.registry import EntityRegistry, send_create_entity_to
    from server.entity_extensions import MAP_LIGHT_TAG
    from shared.bytes import ByteReader
    from shared.packet import CreateEntity
    registry = EntityRegistry()
    lamp = registry.place(13, 120, 70, 232, color=(125, 125, 100), kind="map_flare")
    lamp.map_light_source_color = (250, 250, 200)
    for capable in (False, True):
        sent = []
        peer = SimpleNamespace(flight_profile_capable=capable, known_entity_ids=set(),
                               send=lambda data, **kw: sent.append(bytes(data)))
        assert send_create_entity_to(peer, lamp)
        packet = CreateEntity(ByteReader(sent[0][1:]))
        assert packet.entity.int_properties == ([MAP_LIGHT_TAG, 0xFAFAC8] if capable else [])
        assert packet.entity.color == (125, 125, 100)
        assert not send_create_entity_to(peer, lamp)  # no doubled light on reveal
    # A player flare has no map annotation even for a negotiated client.
    player_lamp = registry.place(13, 130, 70, 232, color=(125, 125, 100), kind="flare_block")
    sent = []
    peer = SimpleNamespace(flight_profile_capable=True, known_entity_ids=set(),
                           send=lambda data, **kw: sent.append(bytes(data)))
    assert send_create_entity_to(peer, player_lamp)
    assert CreateEntity(ByteReader(sent[0][1:])).entity.int_properties == []
