"""Server defaults and shipped configs match the retail 1.x values.

Ground truth is the client's own tables: ``GAME_RULES_LIST`` (Match Lobby
rule defaults and allowed values) in ``shared/constants_matchmaking.py`` and
the mode constants in ``shared/constants_gamemode.py``. The audit and every
known deliberate deviation are written up in ``docs/RETAIL_VALUES.md``; a
new deviation must be added to ``DOCUMENTED_DEVIATIONS`` below and to that
document, never slipped in silently.
"""

from __future__ import annotations

import glob
import logging
import tomllib
from pathlib import Path

import pytest

import shared.constants_gamemode as CG
import shared.constants_matchmaking as MM
from server import mode_data
from server.config import load_config
from server.game_rules import RULE_DEFINITIONS, GameRules

ROOT = Path(__file__).resolve().parents[1]

# Rule -> the value BattleSpades deliberately uses instead of retail.
# RULE_RESPAWN_TIMES: historic 5 s pace (GameRules.server_defaults docstring).
DOCUMENTED_DEVIATIONS = {
    "RULE_RESPAWN_TIMES": 5,
}
# Code-only default for bare ServerConfig() test servers; every shipped
# config turns the retail 3-second window back on.
CODE_ONLY_DEVIATIONS = {"RULE_SPAWN_PROTECTION_TIME": 0.0}

SHIPPED_CONFIGS = sorted(
    [str(ROOT / "config.toml")] + glob.glob(str(ROOT / "configs" / "*.toml"))
)


def _retail_default(key: str):
    spec = MM.GAME_RULES_LIST[key]
    return spec["values"][spec["default"]]


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return float(a) == float(b)


def _load(path: str):
    logging.disable(logging.WARNING)
    try:
        return load_config(Path(path))
    finally:
        logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# Rule catalog vs the client's GAME_RULES_LIST
# ---------------------------------------------------------------------------


def test_catalog_covers_every_retail_rule():
    assert set(MM.GAME_RULES_LIST) <= set(RULE_DEFINITIONS)
    # The only extra is the hidden parachute switch from RULE_TO_TOOL_MAP.
    assert set(RULE_DEFINITIONS) - set(MM.GAME_RULES_LIST) == {
        "RULE_ENABLE_EQUIPMENT_PARACHUTE_NORMAL"
    }
    assert "RULE_ENABLE_EQUIPMENT_PARACHUTE_NORMAL" in MM.RULE_TO_TOOL_MAP


@pytest.mark.parametrize("key", sorted(MM.GAME_RULES_LIST))
def test_catalog_default_and_choices_match_retail(key):
    definition = RULE_DEFINITIONS[key]
    assert _same(definition.default, _retail_default(key))
    retail_choices = {
        (isinstance(value, bool), float(value))
        for value in MM.GAME_RULES_LIST[key]["values"].values()
    }
    ours = {(isinstance(value, bool), float(value)) for value in definition.choices}
    assert ours == retail_choices


def test_server_defaults_deviate_only_where_documented():
    retail = GameRules.retail_defaults()
    server = GameRules.server_defaults()
    differing = {
        key: server.get(key)
        for key in RULE_DEFINITIONS
        if not _same(server.get(key), retail.get(key))
    }
    allowed = {**DOCUMENTED_DEVIATIONS, **CODE_ONLY_DEVIATIONS}
    assert set(differing) == set(allowed)
    for key, value in differing.items():
        assert _same(value, allowed[key]), key
    # Retail default ON: the Flare Block (tool 22) is offered unless a config
    # turns RULE_ENABLE_FLARE_BLOCKS off.
    assert server.enabled("RULE_ENABLE_FLARE_BLOCKS")
    assert 22 not in server.selection_disabled_tools()


def test_retail_values_used_by_fixed_rules():
    rules = GameRules.server_defaults()
    assert rules.get("RULE_CTF_SCORE_TARGET") == 5
    assert rules.get("RULE_CRATES_SPAWN_TIME") == 25
    assert _retail_default("RULE_RESPAWN_TIMES") == 10


# ---------------------------------------------------------------------------
# Mode data vs constants_gamemode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(CG.DEFAULT_MODE_GAME_LENGTH))
def test_mode_clock_matches_retail_game_length(code):
    assert mode_data.get(code).default_time_limit == float(
        CG.DEFAULT_MODE_GAME_LENGTH[code]
    )


@pytest.mark.parametrize("code,rule", [
    ("tdm", "RULE_TDM_SCORE_TARGET"),
    ("ctf", "RULE_CTF_SCORE_TARGET"),
    ("cctf", "RULE_CTF_SCORE_TARGET"),
    ("dia", "RULE_DIA_SCORE_TARGET"),
    ("oc", "RULE_OCC_SCORE_TARGET"),
    ("vip", "RULE_VIP_NOOF_ROUNDS"),
    ("zom", "RULE_ZOMBIE_NOOF_ROUNDS"),
    ("tc", "RULE_TC_MAX_ACTIVE_BASES"),
])
def test_mode_score_fallback_matches_retail_rule(code, rule):
    assert mode_data.get(code).default_score_limit == _retail_default(rule)


def test_vip_selection_delay_uses_the_final_retail_definition():
    # constants_gamemode defines VIP_SELECTION_DELAY twice (3.0, then 10.0
    # under a "#WTF" comment); the module value the client runs with is 10.
    assert CG.VIP_SELECTION_DELAY == 10.0


# ---------------------------------------------------------------------------
# Shipped configs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: Path(p).name)
def test_shipped_config_rules_are_retail_except_documented(path):
    config = _load(path)
    retail = GameRules.retail_defaults()
    for key in RULE_DEFINITIONS:
        value = config.game_rules.get(key)
        if key in DOCUMENTED_DEVIATIONS:
            assert _same(value, DOCUMENTED_DEVIATIONS[key]) or _same(
                value, retail.get(key)
            ), key
            continue
        assert _same(value, retail.get(key)), (key, value, retail.get(key))
    assert config.respawn_time == float(config.game_rules.get("RULE_RESPAWN_TIMES"))
    assert config.fall_damage is True
    assert config.friendly_fire is False


@pytest.mark.parametrize("path", SHIPPED_CONFIGS, ids=lambda p: Path(p).name)
def test_shipped_config_clock_and_lobby_size_are_retail(path):
    config = _load(path)
    code = config.default_mode
    assert config.configured_time_limit(
        code, mode_data.get(code).default_time_limit
    ) == float(CG.DEFAULT_MODE_GAME_LENGTH[code])
    assert str(config.max_players) in MM.NOOF_PLAYERS_LIST


def test_sample_config_mode_overlays_are_retail():
    with (ROOT / "config.toml").open("rb") as stream:
        modes = tomllib.load(stream)["modes"]
    for code, overlay in modes.items():
        if "time_limit" in overlay:
            assert overlay["time_limit"] == CG.DEFAULT_MODE_GAME_LENGTH[code], code
    assert modes["zom"]["infection_delay"] == CG.ZOM_TIME_BEFORE_FIRST_INFECTION
    assert modes["zom"]["zombie_respawn_time"] == CG.ZOM_RESPAWN_AS_ZOMBIE_TIME
    assert modes["vip"]["selection_delay"] == CG.VIP_SELECTION_DELAY
    assert modes["tdm"]["kill_points"] == CG.TDM_TEAM_SCORE_FOR_KILL
    # Classic CTF playlist: 5 captures, carrier may shoot, no auto-return.
    assert modes["cctf"]["score_limit"] == 5
    assert modes["cctf"]["shoot_with_intel"] is True
    assert modes["cctf"]["intel_auto_return"] is False
