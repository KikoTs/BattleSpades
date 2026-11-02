"""The Classic+ variant preserves infection, round reset and horde identity."""

import asyncio

import pytest
import shared.constants as C
from modes import canonical_mode_code, get_mode_class
from modes.classic_zombie import ClassicZombieMode
from modes.zombie import SURVIVOR_TEAM
from server import mode_data
from server.bot_ai.policies import canonical_mode_id
from server.builders.initial_info import build_initial_info
from server.builders.state_data import build_state_data
from server.class_selection import normalize_class_selection
from server.map_metadata import canonical_mode_code as map_mode
from tests.test_zombie import _Server, _player


def _game():
    server = _Server()
    server.config.default_mode = "czom"
    server.mode = ClassicZombieMode(server)
    return server, server.mode


def test_variant_keeps_zombie_scene_radar_and_asymmetric_class_menus():
    server, mode = _game()
    assert get_mode_class("classic-zombie") is ClassicZombieMode
    assert canonical_mode_code("classic_zombie") == "czom"
    assert map_mode("czom") == canonical_mode_id("czom") == "zom"
    state, info = build_state_data(server), build_initial_info(server)
    assert info.mode_key == mode_data.get("zom").mode_id
    assert info.classic == 0 and info.enable_minimap == 1
    assert state.team1_classes == [C.CLASS_ZOMBIE]
    assert state.team2_classes == [C.CLASS_CLASSIC_SOLDIER]
    assert state.team1_can_see_team2


@pytest.mark.parametrize("weapon", [C.RIFLE_TOOL, C.CLASSIC_SMG_TOOL, C.CLASSIC_SHOTGUN_TOOL])
def test_survivor_weapons_and_infection_round_trip(weapon):
    server, mode = _game()
    player = _player(server, 1)
    choice = normalize_class_selection(C.CLASS_CLASSIC_SOLDIER, [weapon])
    selected = mode.prepare_join_selection(player.team, choice)
    assert selected.class_id == C.CLASS_CLASSIC_SOLDIER
    assert weapon in selected.loadout
    assert C.CLASSIC_SPADE_TOOL in selected.loadout
    player.apply_class_selection(selected)
    asyncio.run(mode._infect(player, patient_zero=False))
    assert player.class_id == C.CLASS_ZOMBIE
    assert C.ZOMBIEHAND_TOOL in player.loadout
    mode._assign_survivor(player)
    assert player.class_id == C.CLASS_CLASSIC_SOLDIER
    assert weapon in player.loadout


def test_default_human_and_bot_selections_cannot_restore_the_retail_roster():
    server, mode = _game()
    choice = normalize_class_selection(C.CLASS_SOLDIER)
    assert mode.prepare_join_selection(SURVIVOR_TEAM, choice).class_id == C.CLASS_CLASSIC_SOLDIER
    assert mode.prepare_bot_selection(SURVIVOR_TEAM, choice, player_id=1).class_id == C.CLASS_CLASSIC_SOLDIER
    player = _player(server, 1, class_id=C.CLASS_ZOMBIE)
    mode._assign_survivor(player)
    assert player.class_id == C.CLASS_CLASSIC_SOLDIER


@pytest.mark.parametrize("mode", ("czom", "classic_zombie", "classic-zombie"))
@pytest.mark.parametrize("attacker_class,health", [(C.CLASS_ZOMBIE, 80), (C.CLASS_CLASSIC_SOLDIER, 90)])
def test_classic_zombie_damage_rule_only_scales_infected_attacks(mode, attacker_class, health):
    from server.config import ServerConfig
    from server.game_constants import TEAM1, TEAM2
    from server.main import BattleSpadesServer
    from tests.test_unofficial_modes import add_player

    config = ServerConfig(default_mode=mode)
    config.game_rules.apply({"RULE_ZOMBIE_CLASS_DAMAGE": "200%", "RULE_SPAWN_PROTECTION_TIME": "OFF"})
    server = BattleSpadesServer(config)
    attacker, victim = add_player(server, 1, TEAM1), add_player(server, 2, TEAM2)
    attacker.class_id = int(attacker_class)
    attacker.alive = victim.alive = True
    victim.health = 100
    victim.damage(10, source=attacker, kill_type=int(C.MELEE_KILL))
    assert victim.health == health
