"""Stock VXL maps must use safe terrain, never colour-marker mutation."""

import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import (  # noqa: E402
    PLAYER_STANDING_POS_ABOVE_GROUND as OFF,
    TEAM1,
    TEAM2,
)
from server.round_lifecycle import resolve_player_spawn  # noqa: E402
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


def test_team_base_anchor_is_resolved_once_per_loaded_map():
    """Bot perception must not rescan an authored zone every frame."""

    wm = WorldManager(ServerConfig())
    zone = SimpleNamespace(x=100, y=120)
    wm.map_metadata = SimpleNamespace(
        base_zones={TEAM1: []},
        spawn_zones={TEAM1: [zone]},
    )
    calls = 0

    def candidates(_team):
        nonlocal calls
        calls += 1
        return [(100, 120)]

    wm._zone_spawn_candidates = candidates
    wm.dry_ground_anchor = lambda x, y: (float(x), float(y), 40.0)

    first = wm.team_base_anchor(TEAM1)
    second = wm.team_base_anchor(TEAM1)

    assert first == second == (100.0, 120.0, 40.0)
    assert calls == 1


def _wm(path: Path) -> WorldManager:
    cfg = ServerConfig()
    wm = WorldManager(cfg)
    assert wm.load_map(path.stem)
    return wm


def test_stock_maps_have_safe_spawn_candidates_for_both_teams():
    for path in MAPS:
        wm = _wm(path)
        # Stock sidecars may own environment, crates, and explicit team spawn
        # volumes. Both authored and fallback candidates must resolve to safe
        # dry terrain.
        assert all(
            entity.entity_type in {
                int(C.AMMO_CRATE), int(C.HEALTH_CRATE), int(C.BLOCK_CRATE),
                int(C.JETPACK_CRATE), int(C.FLARE_BLOCK),
            }
            for entity in wm.map_metadata.entities
        )
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
                authored = wm._zone_at(
                    wm.map_metadata.spawn_zones.get(team, []), int(x), int(y)
                )
                assert wm._safe_spawn_column(
                    int(x), int(y), authored_zone=authored,
                    reject_roofs=authored is None,
                )


def test_double_dragon_water_life_candidates_relocate_nearby_to_dry_ground():
    wm = _wm(Path("maps/DoubleDragon.vxl"))

    for team, water_spawn in (
        (TEAM1, (132.5, 286.5, 236.75)),
        (TEAM2, (384.5, 250.5, 236.75)),
    ):
        assert wm.is_water_column(int(water_spawn[0]), int(water_spawn[1]))
        resolved = wm.sanitize_spawn_point(water_spawn, team)

        assert wm.spawn_position_is_safe(resolved)
        assert not wm.is_water_column(int(resolved[0]), int(resolved[1]))
        assert math.dist(resolved[:2], water_spawn[:2]) <= 64.0


def test_double_dragon_fallback_base_keeps_safe_spawn_diversity():
    wm = _wm(Path("maps/DoubleDragon.vxl"))
    random_state = random.getstate()
    random.seed(7331)
    try:
        spawns = {wm.get_spawn_point(TEAM1) for _ in range(32)}
    finally:
        random.setstate(random_state)

    assert len(spawns) >= 4
    assert all(wm.spawn_position_is_safe(spawn) for spawn in spawns)
    assert max(
        math.dist(spawn[:2], wm.team_base_anchor(TEAM1)[:2])
        for spawn in spawns
    ) <= 40.0


def test_mode_spawn_candidate_passes_through_final_world_sanitizer():
    calls = []

    class SpawnWorld:
        def sanitize_spawn_point(self, candidate, team):
            calls.append((candidate, team))
            return (10.5, 20.5, 30.5)

    candidate = (100.0, 200.0, 236.75)
    player = SimpleNamespace(team=TEAM2)
    server = SimpleNamespace(
        mode=SimpleNamespace(get_spawn_point=lambda _player: candidate),
        world_manager=SpawnWorld(),
    )

    assert resolve_player_spawn(server, player) == (10.5, 20.5, 30.5)
    assert calls == [(candidate, TEAM2)]


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
