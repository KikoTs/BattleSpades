"""Characterization tests for the recovered Match Lobby contract."""

from __future__ import annotations

import textwrap

import pytest

import shared.constants as C
from modes import get_mode_class
from modes.lobby_skeletons import (
    DemolitionMode,
    DiamondMineMode,
    MultiHillMode,
    OccupationMode,
    TerritoryControlMode,
)
from server.builders.initial_info import build_initial_info
from server.class_selection import normalize_server_selection
from server.config import ServerConfig, load_config
from server.game_rules import GameRules, RULE_DEFINITIONS
from server.lobby import (
    LOBBY_MATCH_LENGTH_OPTIONS,
    LOBBY_MAX_PLAYER_OPTIONS,
    LOBBY_MODES,
)
from server.main import BattleSpadesServer


def test_retail_lobby_catalog_contains_exact_public_modes_and_selectors():
    assert tuple(LOBBY_MODES) == (
        "tdm", "ctf", "cctf", "zom", "vip", "mh", "tc", "dia", "dem", "oc",
    )
    assert LOBBY_MAX_PLAYER_OPTIONS == tuple(range(2, 25, 2))
    assert LOBBY_MATCH_LENGTH_OPTIONS == (
        5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 90,
    )
    assert LOBBY_MODES["ctf"].maps == (
        "Atlantis", "BlockNess", "CastleWars", "DoubleDragon", "Invasion", "TokyoNeon",
    )
    assert LOBBY_MODES["vip"].mafia is True
    assert LOBBY_MODES["cctf"].classic is True


def test_rule_catalog_retains_visible_and_hidden_recovered_switches():
    assert len(RULE_DEFINITIONS) == 102
    assert RULE_DEFINITIONS["RULE_ENABLE_BLOCKS"].tool_id == int(C.BLOCK_TOOL)
    assert RULE_DEFINITIONS["RULE_ENABLE_CLASS_ENGINEER"].class_id == int(C.CLASS_ENGINEER)
    assert RULE_DEFINITIONS["RULE_VOTES_REQUIRED_FOR_KICK"].menu_visible is False
    assert RULE_DEFINITIONS[
        "RULE_CTF_ENABLE_INTEL_IN_OWN_BASE_TO_SCORE"
    ].menu_visible is False


def test_rule_parser_accepts_retail_labels_and_rejects_typos():
    rules = GameRules.server_defaults()
    rules.apply({
        "RULE_BLOCK_HEALTH": "200%",
        "spawn_protection_time": "OFF",
        "RULE_TDM_SCORE_TARGET": "OFF",
    })
    assert rules.get("RULE_BLOCK_HEALTH") == 2.0
    assert rules.get("RULE_SPAWN_PROTECTION_TIME") == 0.0
    assert rules.get("RULE_TDM_SCORE_TARGET") is False

    with pytest.raises(ValueError, match="Unknown Match Lobby game rule"):
        rules.apply({"RULE_ENABLE_BLOKCS": True})
    with pytest.raises(ValueError, match="must be one of"):
        rules.apply({"RULE_BLOCK_HEALTH": "125%"})


def test_config_rules_drive_initial_info_and_selection(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [game]
        default_mode = "tdm"

        [game_rules]
        RULE_ENABLE_MINI_MAP = false
        RULE_ENABLE_DEATH_CAM = false
        RULE_ENABLE_CLASS_ENGINEER = false
        RULE_ENABLE_WEAPON_AUTOSHOTGUN = false
        RULE_CHARACTER_SPEED = "150%"
        RULE_CHARACTER_BLOCK_WALLETS = "200%"
    """), encoding="utf-8")

    config = load_config(path)
    packet = build_initial_info(BattleSpadesServer(config))
    assert packet.enable_minimap == 0
    assert packet.enable_deathcam == 0
    assert int(C.CLASS_ENGINEER) in packet.disabled_classes
    assert int(C.AUTO_SHOTGUN_TOOL) in packet.disabled_tools

    selection = normalize_server_selection(
        config,
        int(C.CLASS_SPECIALIST),
        (int(C.AUTO_SHOTGUN_TOOL),),
    )
    assert int(C.AUTO_SHOTGUN_TOOL) not in selection.loadout


def test_lobby_config_validates_lengths_and_normalizes_rotation(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [lobby]
        match_length_minutes = 45
        map_rotation = ["MayanJungle.vxl", "Atlantis", "mayANjungle"]
    """), encoding="utf-8")
    config = load_config(path)
    assert config.match_length_minutes == 45
    assert config.map_rotation == ["MayanJungle", "Atlantis"]
    assert config.configured_time_limit("tdm", 900) == 2700.0

    path.write_text("[lobby]\nmatch_length_minutes = 17\n", encoding="utf-8")
    with pytest.raises(ValueError, match="match_length_minutes"):
        load_config(path)


@pytest.mark.parametrize(
    ("code", "mode_type"),
    (
        ("mh", MultiHillMode),
        ("tc", TerritoryControlMode),
        ("dia", DiamondMineMode),
        ("dem", DemolitionMode),
        ("oc", OccupationMode),
    ),
)
def test_missing_retail_modes_are_registered_scene_safe_skeletons(code, mode_type):
    config = ServerConfig(default_mode=code)
    server = BattleSpadesServer(config)
    assert get_mode_class(code) is mode_type
    mode = mode_type(server)
    assert mode.mode_code == code
    assert mode.time_limit == LOBBY_MODES[code].default_seconds


def test_skeleton_rules_are_resolved_from_same_game_rule_service():
    config = ServerConfig(default_mode="dia")
    config.game_rules.apply({
        "RULE_DIAMOND_MAX_ACTIVE_BASES": 4,
        "RULE_DIA_SCORE_TARGET": 55,
        "RULE_MAX_ACTIVE_DIAMONDS": 5,
        "RULE_DIAMOND_LIFETIME": 20,
    })
    mode = DiamondMineMode(BattleSpadesServer(config))
    assert (
        mode.max_active_bases,
        mode.score_limit,
        mode.max_active_diamonds,
        mode.diamond_lifetime,
    ) == (4, 55, 5, 20.0)
