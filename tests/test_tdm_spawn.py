"""Stock VXL maps must use safe terrain, never colour-marker mutation."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from server.config import ServerConfig  # noqa: E402
from server.game_constants import (  # noqa: E402
    PLAYER_STANDING_POS_ABOVE_GROUND as OFF,
    TEAM1,
    TEAM2,
)
from server.world_manager import MAP_X, MAP_Y, WorldManager  # noqa: E402


MAPS = sorted(Path("maps").glob("*.vxl"))


class RoofMap:
    """Ground at 230 with a raised platform at 226 and air beneath it."""

    def __init__(self, roof_radius):
        self.roof_radius = roof_radius

    def get_solid(self, x, y, z):
        if z >= 230:
            return True
        return z == 226 and max(abs(x - 100), abs(y - 100)) <= self.roof_radius

    def get_color(self, x, y, z):
        return 0


def test_spawn_rejects_small_player_built_platform_with_air_below():
    wm = WorldManager(ServerConfig())
    wm.map = RoofMap(1)
    assert not wm._safe_spawn_column(100, 100)


def test_spawn_rejects_large_roof_even_when_ring_samples_hit_roof():
    wm = WorldManager(ServerConfig())
    wm.map = RoofMap(20)
    assert not wm._safe_spawn_column(100, 100)


def _wm(path: Path) -> WorldManager:
    cfg = ServerConfig()
    wm = WorldManager(cfg)
    assert wm.load_map(path.stem)
    return wm


def test_stock_maps_have_safe_spawn_candidates_for_both_teams():
    for path in MAPS:
        wm = _wm(path)
        assert wm.map_metadata.source is None
        assert wm.dirty_columns == set()
        assert wm._spawn_candidates[TEAM1]
        assert wm._spawn_candidates[TEAM2]
        for team in (TEAM1, TEAM2):
            candidates = wm._get_spawn_candidates(team)
            assert candidates, f"{path.name} team {team} has no dry spawn ground"
            for _ in range(30):
                x, y, z = wm.get_spawn_point(team)
                assert 0.0 <= x < MAP_X and 0.0 <= y < MAP_Y
                surface = wm._get_surface_z(int(x), int(y))
                assert surface <= 238
                assert abs((z + OFF + 0.5) - surface) < 0.001
                assert wm._safe_spawn_column(int(x), int(y))


def test_exposed_retail_chroma_voxel_is_removed_without_dirtying_map():
    arctic = Path("maps/ArcticBase.vxl")
    if not arctic.exists():
        return
    wm = _wm(arctic)
    # Native vxl.pyd removes this exposed blue chroma voxel while loading the
    # same stock file.  It is load-time normalization, not a player mutation,
    # so the late-join mutation journal must remain clean.
    x, y, z = 213, 189, 221
    assert (x, y, z) in wm.map.retail_marker_positions
    assert not wm.get_solid(x, y, z)
    assert wm.dirty_columns == set()
