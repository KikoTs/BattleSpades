"""Exercise reconstructed targets on the real native maps, not mock terrain."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

import shared.constants as C
from server import achievements
from server.map_metadata import AchievementRegion, load_map_metadata
from server.world_manager import WorldManager
from tests.achievement_helpers import kill, make_player, make_server, unlocked, TEAM1, TEAM2
from tests.test_achievement_rules import _region_server

ROOT = Path(__file__).resolve().parents[1]


def native_server(name, mode="tdm"):
    world = WorldManager(SimpleNamespace(maps_path=str(ROOT / "maps"), game_mode=mode))
    assert world.load_map(name)
    server = make_server(mode)
    server.world_manager = world
    achievements.match_started(server)
    return server


def destroy(server, player, cells):
    for cell in cells:
        server.world_manager.map.remove_point_nochecks(*cell)
    achievements.blocks_destroyed(server, player, list(cells), [], [])


@pytest.mark.parametrize("name,count,api", [
    ("AncientEgypt", 2, "map_egypt_destroy"),
    ("GreatWall", 234, "map_greatwall_destroy"),
    ("London", 75, "map_london_destroy"),
    ("LunarBase", 36, "map_moon_destroy"),
])
def test_native_landmarks_require_every_target_and_ignore_nearby_terrain(name, count, api):
    server = native_server(name)
    player = make_player(server, 1)
    entries = server.achievements._regions
    cells = sorted(set().union(*(entry.remaining for entry in entries)))
    assert len(cells) == count
    assert all(server.world_manager.get_solid(*cell) for cell in cells)
    # Rocket credit travels through the actual explosion scope.
    engine = server.achievements
    scope = engine.blast_begin(player, int(C.ROCKET_KILL), None) if name == "LunarBase" else None
    destroy(server, player, cells[:-1])
    assert api not in unlocked(server, player)
    destroy(server, player, cells[-1:])
    if scope is not None:
        engine.blast_end(scope)
    assert api in unlocked(server, player)
    assert engine.faults == {}


@pytest.mark.parametrize("eligible", [TEAM1, TEAM2])
def test_native_castle_enemy_pillars_are_independent_and_team_gated(eligible):
    server = native_server("CastleWars", "dem")
    attacker = make_player(server, 1, eligible)
    defender = make_player(server, 2, TEAM2 if eligible == TEAM1 else TEAM1)
    enemy = [entry for entry in server.achievements._regions if entry.region.team == eligible]
    own = [entry for entry in server.achievements._regions if entry.region.team != eligible]
    assert len(enemy) == len(own) == 12
    # A defender removing the enemy building cannot earn the attack objective.
    for entry in enemy:
        cells = sorted(entry.remaining)
        destroy(server, defender, cells[:1])
        destroy(server, attacker, cells[1:])
    assert "map_concrete_destroy" in unlocked(server, attacker)
    assert "map_concrete_destroy" not in unlocked(server, defender)
    assert all(entry.remaining for entry in own)


def test_undertaker_counts_only_original_covered_tunnel_air_once_per_kill():
    server = native_server("TheColosseum")
    killer = make_player(server, 1, TEAM1)
    victim = make_player(server, 2, TEAM2)
    (entry,) = server.achievements._regions
    assert len(entry.allowed_cells) == 17359
    assert all(achievements._contains(entry.region.bounds, tuple(v + .5 for v in cell))
               for cell in entry.allowed_cells)
    assert server.world_manager.map.source_z_shift == 111
    inside = next(cell for cell in entry.allowed_cells if cell[2] == 225)
    for position in ((245, 250, 210), (50, 50, 225), (250, 250, 225)):
        assert position not in entry.allowed_cells
        killer.x, killer.y, killer.z = position
        kill(server, killer, victim)
    # Overlapping regions must never count one enemy twice.
    server.achievements._regions.append(entry)
    killer.x, killer.y, killer.z = (value + .5 for value in inside)
    for _ in range(9):
        kill(server, killer, victim)
    assert "map_colosseum_kill" not in unlocked(server, killer)
    kill(server, killer, victim)
    assert "map_colosseum_kill" in unlocked(server, killer)


def test_mask_limits_targets_and_source_shift_is_applied_exactly_once():
    region = AchievementRegion("map_egypt_destroy", 2, (1, 2, 1, 1, 1, 1), cells=((1, 1, 1),))
    server = _region_server([region], [(1, 1, 11), (2, 1, 11)])
    server.world_manager.map = SimpleNamespace(source_z_shift=10)
    achievements.match_started(server)
    assert server.achievements._regions[0].remaining == {(1, 1, 11)}
    achievements.match_started(server)
    assert server.achievements._regions[0].remaining == {(1, 1, 11)}
    player = make_player(server, 1)
    server.world_manager.destroy(server, player, [(2, 1, 11)])
    assert not unlocked(server, player)
    server.world_manager.destroy(server, player, [(1, 1, 11)])
    assert "map_egypt_destroy" in unlocked(server, player)


def test_already_razed_opponent_castle_does_not_disable_the_other_team():
    region = AchievementRegion("map_concrete_destroy", 2, (1, 1, 1, 1, 1, 1), team=TEAM1)
    other = replace(region, bounds=(2, 2, 2, 2, 2, 2), team=TEAM2)
    server = _region_server([region, other], [(2, 2, 2)], "dem")
    assert [entry.region.team for entry in server.achievements._regions] == [TEAM2]
    player = make_player(server, 1, TEAM2)
    server.world_manager.destroy(server, player, [(2, 2, 2)])
    assert "map_concrete_destroy" in unlocked(server, player)


@pytest.mark.parametrize("mask", [[], [[99, 1, 1]], [[1.5, 1, 1]], [[True, 1, 1]], "bad"])
def test_invalid_or_empty_mask_never_falls_back_to_the_entire_box(tmp_path, mask):
    (tmp_path / "Test.vxl").write_bytes(b"")
    payload = {"ac_ids": ["map_egypt_destroy"], "ac_types": [2],
               "ac_centres": [[1, 1, 1]], "ac_w_h_d": [[2, 2, 2]], "ac_cells": [mask]}
    (tmp_path / "Test.json").write_text(json.dumps(payload))
    regions = load_map_metadata(tmp_path / "Test.vxl", "tdm").achievement_regions
    server = _region_server(regions, [(1, 1, 1)])
    assert server.achievements._regions == []
