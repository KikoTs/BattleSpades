"""The Rocketeer (Glide 67 / Jump 66 packs) is selectable again where it was
before 68c36ca: TDM, Zombie survivors and bot picks (2026-10-01 request)."""
from types import SimpleNamespace

import pytest

import shared.constants as C
from server.bot_ai import director
from server.class_data import BATTLESPADES_TEAM_CLASSES
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.game_rules import GameRules
from server.handlers.equipment import is_class_selectable

ROCKETEER = int(C.CLASS_ROCKETEER)
GLIDER = int(C.JETPACK2)
JUMP_PACK = int(C.JETPACK_NORMAL)


def _server(mode, **rules):
    config = ServerConfig()
    config.default_mode = mode
    config.game_rules = GameRules.server_defaults()
    if rules:
        config.game_rules.apply(rules)
    return SimpleNamespace(config=config, mode=None)


def test_wire_ids_are_the_recovered_glide_and_jump_packs():
    assert (ROCKETEER, GLIDER, JUMP_PACK) == (2, 67, 66)


@pytest.mark.parametrize("mode", ["tdm", "zom"])
def test_rocketeer_is_selectable_in_tdm_and_zombie(mode):
    assert is_class_selectable(_server(mode), ROCKETEER)


def test_operator_rule_still_switches_the_rocketeer_off():
    assert not is_class_selectable(
        _server("tdm", RULE_ENABLE_CLASS_ROCKETEER=False), ROCKETEER
    )
    assert is_class_selectable(
        _server("tdm", RULE_ENABLE_CLASS_ROCKETEER=False), int(C.CLASS_ENGINEER)
    )


def test_ctf_keeps_its_pre_removal_six_class_list():
    assert not is_class_selectable(_server("ctf"), ROCKETEER)


def test_default_rocketeer_loadout_carries_the_glider():
    selection = normalize_class_selection(ROCKETEER)
    assert selection.class_id == ROCKETEER
    assert GLIDER in selection.loadout
    assert JUMP_PACK not in selection.loadout
    assert int(C.SMG_TOOL) in selection.loadout


def test_jump_pack_is_the_alternative_equipment_choice():
    selection = normalize_class_selection(ROCKETEER, [JUMP_PACK])
    assert JUMP_PACK in selection.loadout
    assert GLIDER not in selection.loadout


def test_bots_draw_from_the_roster_with_the_rocketeer():
    assert ROCKETEER in director._DEFAULT_CLASSES
    assert tuple(director._DEFAULT_CLASSES) == BATTLESPADES_TEAM_CLASSES
