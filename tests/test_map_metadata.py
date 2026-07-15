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
