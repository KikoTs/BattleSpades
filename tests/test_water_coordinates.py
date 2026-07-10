import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import WATER_LEVEL  # noqa: E402
from server.world_manager import WorldManager  # noqa: E402


def test_server_uses_retail_240_high_water_coordinate():
    config = ServerConfig()
    assert config.map_size_z == int(C.MAP_Z) == 240
    assert config.water_level == WATER_LEVEL == int(C.Z_ABOVE_WATERPLANE) == 238


def test_generated_flat_map_is_a_dry_debug_plateau():
    wm = WorldManager(ServerConfig())
    wm.generate_flat_map()
    assert wm.get_height(256, 256) == 62
    assert wm.get_height(256, 256) < int(C.Z_ABOVE_WATERPLANE)
    assert not wm.is_water_column(256, 256)
