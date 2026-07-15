import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
from server.map_metadata import MapMetadata, MapZone, load_map_metadata  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.world_manager import WorldManager  # noqa: E402


def test_loads_ugc_spawn_base_and_drop_points(tmp_path: Path):
    vxl = tmp_path / "arena.vxl"
    vxl.write_bytes(b"")
    sidecar = tmp_path / "arena.txt"
    sidecar.write_text(json.dumps({"ugc_entities": [
        {"position": [100, 110, 230], "mode": "tdm", "item": "ugc_spawnblue_small"},
        {"position": [400, 410, 228], "mode": "tdm", "item": "ugc_spawngreen_med"},
        {"position": [90, 100, 232], "mode": "tdm", "item": "ugc_baseblue_large"},
        {"position": [256, 256, 237], "mode": "nor", "item": "ugc_health_drop"},
        {"position": [300, 300, 237], "mode": "ctf", "item": "ugc_ammo_drop"},
    ]}), encoding="utf-8")

    metadata = load_map_metadata(vxl, "tdm")
    assert metadata.source == sidecar
    assert len(metadata.spawn_zones[TEAM1]) == 1
    assert len(metadata.spawn_zones[TEAM2]) == 1
    assert metadata.spawn_zones[TEAM1][0].xy_bounds() == (95, 105, 105, 115)
    assert len(metadata.base_zones[TEAM1]) == 1
    assert [(e.entity_type, e.kind) for e in metadata.entities] == [
        (int(C.HEALTH_CRATE), "health")
    ]


def test_loads_skybox_from_original_and_ugc_metadata_keys(tmp_path: Path):
    """Map sidecars, not the connection handler, own the skybox resource."""
    vxl = tmp_path / "arena.vxl"
    vxl.write_bytes(b"")

    vxl.with_suffix(".json").write_text(
        json.dumps({"skybox_texture": "ArcticBase.txt"}),
        encoding="utf-8",
    )
    assert load_map_metadata(vxl, "tdm").skybox_name == "ArcticBase.txt"

    vxl.with_suffix(".json").write_text(
        json.dumps({"skybox_name": "User_Grassland.txt"}),
        encoding="utf-8",
    )
    assert load_map_metadata(vxl, "tdm").skybox_name == "User_Grassland.txt"


def test_loads_skybox_from_legacy_python_assignment_without_executing_it(
    tmp_path: Path,
):
    vxl = tmp_path / "legacy.vxl"
    vxl.write_bytes(b"")
    sidecar = vxl.with_suffix(".txt")
    sidecar.write_text(
        "name = 'Legacy Map'\nskybox_texture = 'WW1.txt'\n",
        encoding="utf-8",
    )

    metadata = load_map_metadata(vxl, "tdm")

    assert metadata.source == sidecar
    assert metadata.skybox_name == "WW1.txt"


def test_loads_stock_environment_lights_and_resource_points(tmp_path: Path):
    vxl = tmp_path / "legacy.vxl"
    vxl.write_bytes(b"")
    sidecar = vxl.with_suffix(".txt")
    sidecar.write_text(
        "\n".join((
            "skybox_texture = 'MayanJungle.txt'",
            "fog_color = [69, 76, 39]",
            "static_light_color0 = [224, 172, 29]",
            "ammo_crate_drop_points = [[10, 20, 30]]",
            "health_crate_drop_points = [(11, 21, 31)]",
            "block_crate_drop_points = [[12, 22, 32]]",
            "team_one_spawn_area = [((100, 110, 220), (20, 30, 40))]",
            "team_two_spawn_area = [((400, 410, 221), (10, 20, 30))]",
            "team_one_base_point = (90, 100, 222)",
            "team_one_base_w_h_d = (12, 14, 16)",
            # This must never execute while parsing the trusted scalar subset.
            "raise RuntimeError('metadata parser executed map code')",
        )),
        encoding="utf-8",
    )

    metadata = load_map_metadata(vxl, "zom")

    assert metadata.fog_color == (69, 76, 39)
    assert metadata.static_light_colors == {0: (224, 172, 29)}
    assert metadata.spawn_zones[TEAM1][0].xy_bounds() == (90, 110, 95, 125)
    assert metadata.spawn_zones[TEAM2][0].xy_bounds() == (395, 405, 400, 420)
    assert metadata.base_zones[TEAM1][0].xy_bounds() == (84, 96, 93, 107)
    assert [
        (entity.entity_type, entity.kind, entity.x, entity.y, entity.z)
        for entity in metadata.entities
    ] == [
        (int(C.AMMO_CRATE), "ammo", 10.0, 20.0, 30.0),
        (int(C.HEALTH_CRATE), "health", 11.0, 21.0, 31.0),
        (int(C.BLOCK_CRATE), "block", 12.0, 22.0, 32.0),
    ]


def test_skybox_catalog_supplies_fog_when_sidecar_omits_it(tmp_path: Path):
    vxl = tmp_path / "arena.vxl"
    vxl.write_bytes(b"")
    vxl.with_suffix(".json").write_text(
        json.dumps({"skybox_texture": "ArcticBase.txt"}),
        encoding="utf-8",
    )

    metadata = load_map_metadata(vxl, "ctf")

    assert metadata.fog_color == (114, 174, 175)


def test_ugc_static_flare_requires_explicit_valid_color(tmp_path: Path):
    vxl = tmp_path / "arena.vxl"
    vxl.write_bytes(b"")
    vxl.with_suffix(".json").write_text(json.dumps({"ugc_entities": [
        {"position": [1, 2, 3], "item": "flare_block", "color": [4, 5, 6]},
        {"position": [7, 8, 9], "item": "flare_block", "color": [999, 0, 0]},
    ]}), encoding="utf-8")

    metadata = load_map_metadata(vxl, "tdm")

    assert len(metadata.entities) == 1
    flare = metadata.entities[0]
    assert flare.entity_type == int(C.FLARE_BLOCK)
    assert flare.kind == "static_flare"
    assert flare.color == (4, 5, 6)


def test_rejects_skybox_paths_that_escape_client_mesh_assets(tmp_path: Path):
    vxl = tmp_path / "arena.vxl"
    vxl.write_bytes(b"")
    vxl.with_suffix(".json").write_text(
        json.dumps({"skybox_texture": "../arbitrary.txt"}),
        encoding="utf-8",
    )

    assert load_map_metadata(vxl, "tdm").skybox_name is None


def test_shipped_maps_declare_their_stock_skyboxes():
    expected = {
        "ArcticBase": "ArcticBase.txt",
        "CastleWars": "Classic.txt",
        "CityOfChicago": "Chicago.txt",
        "20thCenturyTown": "WW1.txt",
    }

    for map_name, skybox_name in expected.items():
        metadata = load_map_metadata(Path("maps") / f"{map_name}.vxl", "tdm")
        assert metadata.skybox_name == skybox_name


def test_official_alias_catalog_selects_client_assets_but_not_map_sync():
    metadata = load_map_metadata(Path("maps") / "AncientEgypt.vxl", "tdm")

    assert metadata.official_map is True
    assert metadata.skybox_name == "Egypt.txt"
    assert [sound.name for sound in metadata.ambient_sounds] == ["amb_desert"]
    assert metadata.ambient_sounds[0].points == ()


def test_legacy_metadata_loads_global_and_local_ambience_and_lighting(tmp_path: Path):
    vxl = tmp_path / "river.vxl"
    vxl.write_bytes(b"")
    vxl.with_suffix(".txt").write_text(
        "\n".join((
            "skybox_texture = 'User_Urban.txt'",
            "ambient_sounds = [['amb_ww_lighter', [], 1.0, 0.0], "
            "['em_river', [(10, 20, 236-3), (30, 40, 232)], 1.0, 1.0]]",
            "light_color = (180, 192, 220)",
            "light_direction = (0.0, 0.8, 0.2)",
            "back_light_color = (64, 64, 64)",
            "back_light_direction = (0.3, -0.6, -0.1)",
            "ambient_light_color = (52, 56, 64)",
            "ambient_light_intensity = 0.2",
        )),
        encoding="utf-8",
    )

    metadata = load_map_metadata(vxl, "tdm")

    assert [sound.name for sound in metadata.ambient_sounds] == [
        "amb_ww_lighter", "em_river",
    ]
    assert metadata.ambient_sounds[0].points == ()
    assert metadata.ambient_sounds[1].points == ((10, 20, 233), (30, 40, 232))
    assert metadata.light_color == (180, 192, 220)
    assert metadata.light_direction == (0.0, 0.8, 0.2)
    assert metadata.ambient_light_intensity == 0.2


def test_unknown_ambient_resource_never_reaches_native_client(tmp_path: Path):
    vxl = tmp_path / "unsafe.vxl"
    vxl.write_bytes(b"")
    vxl.with_suffix(".json").write_text(json.dumps({
        "ambient_sounds": [["../sounds/arbitrary", [], 1.0, 0.0]],
    }), encoding="utf-8")

    metadata = load_map_metadata(vxl, "tdm")

    assert [sound.name for sound in metadata.ambient_sounds] == ["amb_rural"]


def test_missing_or_invalid_sidecar_is_empty(tmp_path: Path):
    vxl = tmp_path / "plain.vxl"
    vxl.write_bytes(b"")
    assert load_map_metadata(vxl, "tdm").source is None
    vxl.with_suffix(".txt").write_text("not json", encoding="utf-8")
    metadata = load_map_metadata(vxl, "tdm")
    assert metadata.source is None
    assert metadata.entities == []


class _Terrain:
    def __init__(self, surface_at):
        self.surface_at = surface_at

    def get_solid(self, x, y, z):
        return z >= self.surface_at(x, y)


def test_authored_zone_controls_spawn_xy_and_z():
    wm = WorldManager(ServerConfig())
    wm.map = _Terrain(lambda _x, _y: 230)
    zone = MapZone("spawn", TEAM1, 100, 110, 230, (-5, 5, -5, 5, -8, 2), "test")
    wm.map_metadata = MapMetadata()
    wm.map_metadata.spawn_zones[TEAM1].append(zone)

    candidates = wm._get_spawn_candidates(TEAM1)
    assert candidates
    assert all(95 <= x <= 105 and 105 <= y <= 115 for x, y in candidates)
    for _ in range(10):
        x, y, z = wm.get_spawn_point(TEAM1)
        assert 95.5 <= x <= 105.5 and 105.5 <= y <= 115.5
        assert z == 230 - 2.25 - 0.5


def test_safe_terrain_rejects_roofs_and_water():
    def surface(x, y):
        if abs(x - 100) <= 5 and abs(y - 100) <= 5:
            return 100  # raised building roof
        if x == 200 and y == 200:
            return 239  # ocean bed
        return 230

    wm = WorldManager(ServerConfig())
    wm.map = _Terrain(surface)
    assert not wm._safe_spawn_column(100, 100)
    assert not wm._safe_spawn_column(200, 200)
    assert wm._safe_spawn_column(150, 150)


def test_voxel_only_map_base_anchor_is_nearest_safe_team_region_center():
    wm = WorldManager(ServerConfig())
    wm.map = _Terrain(lambda _x, _y: 230)
    wm._spawn_candidates[TEAM1] = [
        (64, 256),
        (100, 128),
        (120, 128),  # lexicographic median, but nowhere near team base centre
        (128, 256),
        (180, 384),
    ]

    x, y, _z = wm.team_base_anchor(TEAM1)

    assert (x, y) == (128.5, 256.5)


def test_voxel_only_map_spawns_stay_clustered_around_team_base(monkeypatch):
    wm = WorldManager(ServerConfig())
    wm.map = _Terrain(lambda _x, _y: 230)
    wm._spawn_candidates[TEAM1] = [
        (64, 128),   # old whole-region shuffle could pick this first
        (128, 256),
        (140, 256),
        (160, 256),  # outside the intended fallback base cluster
    ]
    monkeypatch.setattr("server.world_manager.random.shuffle", lambda values: None)

    x, y, _z = wm.get_spawn_point(TEAM1)

    assert (x, y) == (128.5, 256.5)
