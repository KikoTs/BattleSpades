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


def test_coloured_vxl_terrain_is_not_deleted_as_spawn_metadata():
    arctic = Path("maps/ArcticBase.vxl")
    if not arctic.exists():
        return
    wm = _wm(arctic)
    # Pure-blue surface voxels exist in the stock map.  They are terrain, not
    # UGC metadata, and must remain byte/geometry-identical after loading.
    x, y, z = 213, 189, 221
    assert wm.get_solid(x, y, z)
    r, g, b, _a = wm.map.get_color_tuple(x, y, z)
    assert (r & 0xF0, g & 0xF0, b & 0xF0) == (0, 0, 0xF0)
    assert wm.dirty_columns == set()
