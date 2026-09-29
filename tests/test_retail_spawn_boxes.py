"""Retail spawn boxes: indoor storeys, zombie sea spawns and CTF intel axis.

Real retail layouts recovered from the shipped ``.txtc`` map descriptions
(maps/*.json): retail dropped a player anywhere into a spawn box volume, so
boxes inside buildings (MayanJungle's temple, the SpookyMansion mansion
floors) must produce spawns on the storeys inside them, and SpookyMansion's
zombies rose out of the water ring around the island.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
import shared.constants_gamemode as CG
from server.config import ServerConfig
from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1, TEAM2
from server.map_metadata import MapMetadata, MapZone
from server.world_manager import WorldManager

MAPS = Path(__file__).resolve().parents[1] / "maps"
WATERBED = int(C.MAP_Z) - 1


def _sidecar(name: str) -> dict:
    return json.loads((MAPS / f"{name}.json").read_text(encoding="utf-8"))


_WORLDS: dict[tuple[str, str], WorldManager] = {}


def _world(name: str, mode: str) -> WorldManager:
    key = (name, mode)
    if key not in _WORLDS:
        wm = WorldManager(ServerConfig(default_mode=mode))
        assert wm.load_map(name)
        _WORLDS[key] = wm
    return _WORLDS[key]


def _in_box(x: float, y: float, row, margin: float = 1.0) -> bool:
    (cx, cy, _cz), (w, h, _d) = row
    return (cx - w / 2 - margin <= x <= cx + w / 2 + margin
            and cy - h / 2 - margin <= y <= cy + h / 2 + margin)


def _floor_of(position) -> int:
    return int(round(position[2] + PLAYER_STANDING_POS_ABOVE_GROUND + 0.5))


# ------------------------------------------------------------ indoor boxes


def test_mayan_temple_box_spawns_on_the_temple_floor_under_its_roof():
    wm = _world("MayanJungle", "tdm")
    temple = _sidecar("MayanJungle")["team_two_spawn_area"][0]
    (cx, cy, cz), (_w, _h, d) = temple
    box_top, box_bottom = cz - d / 2, cz + d / 2
    inside = [
        c for c in wm._get_spawn_candidates(TEAM2)
        if len(c) == 3 and _in_box(c[0], c[1], temple, margin=0)
    ]
    assert len(inside) >= 50
    for x, y, floor_z in inside:
        # Under the temple roof (the column top is above the box) ...
        assert wm._get_surface_z(x, y) < box_top
        # ... on a floor a drop into the box lands on.
        assert box_top < floor_z <= box_bottom + 3
        position = wm.spawn_candidate_position((x, y, floor_z), wm.map_metadata.spawn_zones[TEAM2])
        assert position is not None
        assert wm.spawn_position_is_safe(position)


@pytest.mark.parametrize("mode, key", [
    ("zom", "survivor_spawn_area"),
    ("oc", "oc_team_two_spawn_area"),
])
def test_every_spooky_mansion_interior_box_yields_spawns(mode, key):
    wm = _world("SpookyMansion", mode)
    boxes = _sidecar("SpookyMansion")[key]
    candidates = wm._get_spawn_candidates(TEAM2)
    for row in boxes:
        inside = [c for c in candidates if _in_box(c[0], c[1], row, margin=0)]
        assert inside, row
    # The mansion storeys are mostly under the roof: floor slots, not tops.
    assert sum(len(c) == 3 for c in candidates) > sum(len(c) == 2 for c in candidates)


def test_spawns_spread_into_the_indoor_boxes_and_stay_safe():
    wm = _world("SpookyMansion", "zom")
    boxes = _sidecar("SpookyMansion")["survivor_spawn_area"]
    random.seed(7)
    hit = set()
    for _ in range(300):
        position = wm.get_spawn_point(TEAM2)
        assert wm.spawn_position_is_safe(position)
        assert position[2] + PLAYER_STANDING_POS_ABOVE_GROUND + 0.5 <= WATERBED - 1
        for index, row in enumerate(boxes):
            if _in_box(position[0], position[1], row, margin=0):
                hit.add(index)
    assert len(hit) >= 4, hit


def test_smart_spawn_scoring_uses_indoor_floor_slots():
    from server.spawn_selection import choose_team_spawn

    wm = _world("MayanJungle", "tdm")
    temple = _sidecar("MayanJungle")["team_two_spawn_area"][0]
    server = SimpleNamespace(world_manager=wm, players={}, mode=None)
    player = SimpleNamespace(team=TEAM2)
    rng = random.Random(11)
    indoor = 0
    for _ in range(200):
        position = choose_team_spawn(server, player, rng=rng)
        assert position is not None
        assert wm.spawn_position_is_safe(position)
        if (_in_box(position[0], position[1], temple, margin=0)
                and wm._get_surface_z(int(position[0]), int(position[1])) < _floor_of(position) - 3):
            indoor += 1
    assert indoor > 0


def test_floor_slots_skip_sealed_voids_under_the_trench_boards():
    wm = _world("Trenches", "ctf")
    for x, y, floor_z in (c for c in wm._get_spawn_candidates(TEAM1) if len(c) == 3):
        if floor_z != wm._get_surface_z(x, y):
            assert not wm._sealed_air_pocket(x, y, floor_z - 1)


class _Building:
    """Flat ground at z=230 with a roofed hut: roof z=200, floor z=220."""

    def get_solid(self, x, y, z):
        if z >= 230:
            return True
        inside = 95 <= x <= 105 and 95 <= y <= 105
        if not inside:
            return False
        if z == 200:  # roof
            return True
        if 220 <= z < 230:  # raised hut floor/plinth
            return True
        return x in (95, 105) or y in (95, 105)  # walls


def test_synthetic_box_inside_a_building_yields_its_floor():
    wm = WorldManager(ServerConfig())
    wm.map = _Building()
    wm.map_metadata = MapMetadata()
    # A centred retail box (z 212..222 extended to the floor by the loader).
    zone = MapZone("spawn", TEAM1, 100, 100, 217, (-4, 4, -4, 4, -5, 22), "team_one_spawn_area")
    wm.map_metadata.spawn_zones[TEAM1].append(zone)
    candidates = wm._get_spawn_candidates(TEAM1)
    assert candidates
    assert all(len(c) == 3 and c[2] == 220 for c in candidates)
    x, y, z = wm.get_spawn_point(TEAM1)
    assert 96 <= x < 105 and 96 <= y < 105
    assert z == 220 - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5
    # The same hut with an unauthored map never spawns inside or on it.
    wm.map_metadata = MapMetadata()
    wm._spawn_candidates = {TEAM1: [], TEAM2: []}
    assert all(len(c) == 2 for c in wm._get_spawn_candidates(TEAM1))


def test_unauthored_maps_keep_top_surface_columns_only():
    wm = _world("ArcticBase", "tdm")
    for team in (TEAM1, TEAM2):
        assert not wm.map_metadata.spawn_zones[team]
        assert all(len(c) == 2 for c in wm._get_spawn_candidates(team))


# ------------------------------------------------------------ zombie sea


def test_spooky_mansion_zombies_spawn_in_the_retail_water_ring():
    wm = _world("SpookyMansion", "zom")
    areas = _sidecar("SpookyMansion")["zombie_spawn_area"]
    candidates = wm._get_spawn_candidates(TEAM1)
    assert candidates
    # Retail zombies only rose out of the zombie areas (no dry shore complement).
    assert all(any(_in_box(c[0], c[1], row) for row in areas) for c in candidates)
    assert sum(1 for c in candidates if len(c) == 3 and c[2] == WATERBED) > 1000
    for _ in range(40):
        position = wm.get_spawn_point(TEAM1)
        assert any(_in_box(position[0], position[1], row) for row in areas)
        assert wm.spawn_position_is_safe(position, team=TEAM1)
    water = next(
        wm.spawn_candidate_position(c, wm.map_metadata.spawn_zones[TEAM1])
        for c in candidates if len(c) == 3 and c[2] == WATERBED
    )
    assert wm.is_water_column(int(water[0]), int(water[1]))
    assert water[2] == WATERBED - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5
    # Water stays rejected for everyone else and for team-less checks.
    assert wm.spawn_position_is_safe(water, team=TEAM1)
    assert not wm.spawn_position_is_safe(water)
    assert not wm.spawn_position_is_safe(water, team=TEAM2)


def test_zombie_sea_spawn_survives_the_life_resolver():
    from server.round_lifecycle import resolve_player_spawn

    wm = _world("SpookyMansion", "zom")
    areas = _sidecar("SpookyMansion")["zombie_spawn_area"]
    server = SimpleNamespace(world_manager=wm, players={})
    server.mode = SimpleNamespace(get_spawn_point=lambda player: wm.get_spawn_point(player.team))
    zombie = SimpleNamespace(team=TEAM1, name="z")
    water_lives = 0
    for _ in range(20):
        position = resolve_player_spawn(server, zombie)
        assert any(_in_box(position[0], position[1], row) for row in areas)
        if wm.is_water_column(int(position[0]), int(position[1])):
            water_lives += 1
    assert water_lives > 10
    # A survivor proposal in the same water is still rescued onto land.
    survivor = SimpleNamespace(team=TEAM2, name="s")
    wet = next(
        wm.spawn_candidate_position(c, wm.map_metadata.spawn_zones[TEAM1])
        for c in wm._get_spawn_candidates(TEAM1) if len(c) == 3
    )
    server.mode = SimpleNamespace(get_spawn_point=lambda player: wet)
    rescued = resolve_player_spawn(server, survivor)
    assert not wm.is_water_column(int(rescued[0]), int(rescued[1]))


def test_water_spawns_stay_rejected_outside_zombie_areas():
    wm = _world("SpookyMansion", "tdm")
    for team in (TEAM1, TEAM2):
        for c in wm._get_spawn_candidates(team):
            floor_z = c[2] if len(c) == 3 else wm._get_surface_z(c[0], c[1])
            assert floor_z <= int(C.Z_ABOVE_WATERPLANE)


# ------------------------------------------------------------ CTF intel


def _ctf(map_name: str, mode_code: str = "ctf"):
    from modes import get_mode_class
    from server.main import BattleSpadesServer

    server = BattleSpadesServer(ServerConfig(default_mode=mode_code))
    assert server.world_manager.load_map(map_name)
    server.mode = get_mode_class(mode_code)(server)
    asyncio.run(server.mode.on_mode_start())
    return server.mode


def test_trenches_intel_sits_on_the_retail_ctf_base_point():
    """Retail CTF derives the intel from the authored base: it sits on the
    base point (docs/RETAIL_VALUES.md, CTF intel placement)."""
    mode = _ctf("Trenches")
    bases = _sidecar("Trenches")["ctf_base_points"]
    for team, (bx, by, _bz) in ((TEAM1, bases[0]), (TEAM2, bases[1])):
        ix, iy, _iz = mode.intel_positions[team]
        base = mode.base_positions[team]
        assert (ix, iy) == (base[0], base[1]), (team, (ix, iy), base)
        # Inside the 10x10 retail capture box.
        assert abs(ix - bx) <= 5 and abs(iy - by) <= 5, (team, ix, iy)


def test_trenches_classic_intel_keeps_the_retail_minimum_radius_toward_the_enemy():
    mode = _ctf("Trenches", "cctf")
    bases = _sidecar("Trenches")["ctf_base_points"]
    for team, enemy, (bx, by, _bz) in ((TEAM1, TEAM2, bases[0]), (TEAM2, TEAM1, bases[1])):
        ix, iy, _iz = mode.intel_positions[team]
        assert abs(ix - bx) <= 5 and abs(iy - by) <= 5, (team, ix, iy)
        base = mode.base_positions[team]
        toward = mode.base_positions[enemy][0] - base[0]
        assert (ix - base[0]) * toward > 0  # midfield side
        distance = math.hypot(ix - base[0], iy - base[1])
        assert distance >= float(CG.CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE) - 0.01
        # Still inside the retail Classic capture radius around the base.
        assert distance <= float(CG.CLASSIC_CTF_BASE_CAPTURE_DISTANCE)


@pytest.mark.parametrize("map_name", ["WW1", "ToTheBridge", "Crossroads"])
def test_north_south_maps_offset_the_intel_along_the_base_axis(map_name):
    mode = _ctf(map_name)
    for team, enemy in ((TEAM1, TEAM2), (TEAM2, TEAM1)):
        base = mode.base_positions[team]
        other = mode.base_positions[enemy]
        axis = (other[0] - base[0], other[1] - base[1])
        assert abs(axis[1]) > abs(axis[0])  # the teams are split north/south
        ix, iy, _ = mode.intel_positions[team]
        offset = (ix - base[0], iy - base[1])
        # Toward the enemy along y, not sideways along x.
        assert offset[1] * axis[1] > 0, (map_name, team, base, (ix, iy))
        assert abs(offset[1]) > abs(offset[0])


def test_intel_home_math_follows_the_axis_and_clamps_into_authored_boxes():
    from modes.ctf import _intel_home

    server = SimpleNamespace()  # no world manager: raw coordinates
    assert _intel_home(server, (100.5, 400.5, 0), (100.5, 100.5, 0), 12.0)[:2] == (100.5, 388.5)
    assert _intel_home(server, (50.5, 50.5, 0), (50.5, 50.5, 0), 12.0, fallback_sign=-1.0)[:2] == (38.5, 50.5)
    box = MapZone("base", TEAM1, 127, 255, 222, (-5, 5, -5, 5, -5, 5), "ctf_base_points[0]")
    x, y, _ = _intel_home(server, (127.5, 255.5, 0), (384.5, 255.5, 0), 12.0, base_zone=box)
    assert (x, y) == (131.5, 255.5)
    tiny = MapZone("base", TEAM1, 127, 255, 222, (-1, 1, -1, 1, -1, 1), "tiny")
    x, _y, _ = _intel_home(server, (127.5, 255.5, 0), (384.5, 255.5, 0), 12.0, base_zone=tiny)
    assert x == 130.5  # never closer than the retail 3-block minimum
