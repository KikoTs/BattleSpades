"""Lock the (engine-faithful) marker-based spawn so a future change can't
silently break TDM/CTF spawns. ArcticBase carries blue(TEAM1)/green(TEAM2)
spawn markers; both must be found, team1 must be north of team2, and every
spawn must be on dry, in-bounds ground."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from server.config import ServerConfig  # noqa: E402
from server.game_constants import TEAM1, TEAM2  # noqa: E402
from server.world_manager import WorldManager, MAP_X, MAP_Y, MAP_Z  # noqa: E402

ARCTIC = Path("maps/ArcticBase.vxl")


def _wm():
    cfg = ServerConfig()
    wm = WorldManager(cfg)
    wm.load_map("ArcticBase")
    return wm


def test_both_teams_have_markers():
    if not ARCTIC.exists():
        return  # map not present in this checkout
    wm = _wm()
    assert len(wm.spawn_markers[TEAM1]) > 0
    assert len(wm.spawn_markers[TEAM2]) > 0


def test_team1_north_of_team2():
    if not ARCTIC.exists():
        return
    wm = _wm()
    m1 = wm.spawn_markers[TEAM1]
    m2 = wm.spawn_markers[TEAM2]
    cy1 = sum(y for _, y, _ in m1) / len(m1)
    cy2 = sum(y for _, y, _ in m2) / len(m2)
    # Measured on ArcticBase: blue ~197, green ~330 (N/S split).
    assert cy1 < cy2


def test_spawns_are_dry_and_in_bounds():
    if not ARCTIC.exists():
        return
    wm = _wm()
    from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND as OFF
    for team in (TEAM1, TEAM2):
        for _ in range(50):
            x, y, z = wm.get_spawn_point(team)
            assert 0.0 <= x < MAP_X and 0.0 <= y < MAP_Y
            feet = z + OFF
            # feet must be ABOVE the waterplane (dry), not at/under the seabed.
            assert feet < 239.0, f"team {team} spawned wet at feet z={feet}"
            # surface under the spawn column must be land (<=238).
            assert wm._get_surface_z(int(x), int(y)) <= MAP_Z - 2
