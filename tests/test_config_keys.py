"""Every feature setting read through ``getattr`` is a registered config key.

Features written after the config loader read their tunables with
``getattr(config, name, default)``. A key that is missing from
``server/config.py`` silently keeps the in-code default no matter what the
operator writes in ``config.toml``. These tests pin, per key:

* the TOML section it lives in and the config attribute the loader sets;
* that the dataclass default equals the feature's own fallback constant;
* that ``config.toml`` documents the key with that same default;
* that a non-default TOML value reaches the feature's own accessor;
* that every ``getattr``-style read in the feature sources names a
  registered attribute (so a renamed or new key cannot drift unnoticed).
"""

from __future__ import annotations

import re
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.config import AntiCheatConfig, BotConfig, ServerConfig, load_config

ROOT = Path(__file__).resolve().parents[1]

# (toml section, key, config attribute path, shipped default)
FLAT_KEYS = [
    ("objectives", "escape_watch_enabled", "escape_watch_enabled", True),
    ("objectives", "escape_watch_interval", "escape_watch_interval", 1.0),
    ("objectives", "escape_watch_sky_seconds", "escape_watch_sky_seconds", 5.0),
    ("objectives", "escape_watch_embedded_seconds", "escape_watch_embedded_seconds", 3.0),
    ("objectives", "escape_watch_entomb_seconds", "escape_watch_entomb_seconds", 5.0),
    ("objectives", "objective_entomb_seconds", "objective_entomb_seconds", 5.0),
    ("objectives", "objective_pickup_requires_los", "objective_pickup_requires_los", True),
    ("objectives", "objective_pickup_ends_spawn_protection",
     "objective_pickup_ends_spawn_protection", True),
    ("objectives", "objective_afk_seconds", "objective_afk_seconds", 60.0),
    ("objectives", "ctf_base_pit_depth", "ctf_base_pit_depth", 24.0),
    ("teams", "balance_mid_match", "balance_mid_match", True),
    ("teams", "balance_grace_seconds", "balance_grace_seconds", 5.0),
    ("teams", "balance_player_cooldown", "balance_player_cooldown", 600.0),
    ("teams", "balance_bot_wait_seconds", "balance_bot_wait_seconds", 10.0),
    ("teams", "balance_check_interval", "balance_check_interval", 1.0),
    ("lobby", "map_vote_size_fit", "map_vote_size_fit", True),
    ("lobby", "map_vote_area_per_player", "map_vote_area_per_player", 8000.0),
    ("lobby", "map_vote_min_area", "map_vote_min_area", 24000.0),
    ("lobby", "map_vote_recent_exclude", "map_vote_recent_exclude", 2),
    ("lobby", "map_vote_bot_weight", "map_vote_bot_weight", 1.0),
    ("lobby", "map_size_overrides", "map_size_overrides", {}),
    ("lobby", "map_rotation_shuffle", "map_rotation_shuffle", True),
    ("lobby", "map_vote_retail_max_players", "map_vote_retail_max_players", True),
    ("network", "require_protocol_version", "require_protocol_version", True),
    ("lobby", "votekick_cooldown_seconds", "votekick_cooldown_seconds", 300.0),
    ("lobby", "votekick_cancelled_cooldown_seconds",
     "votekick_cancelled_cooldown_seconds", 45.0),
    ("lobby", "votekick_min_team_players", "votekick_min_team_players", 3),
    ("audio", "mode_start_music", "mode_start_music", True),
    ("network", "prefab_health_state_batch", "prefab_health_state_batch", 128),
    ("network", "lag_compensation_enabled", "lag_compensation_enabled", True),
    ("network", "lag_compensation_max_ms", "lag_compensation_max_ms", 250.0),
    ("network", "lag_compensation_extra_ms", "lag_compensation_extra_ms", 50.0),
    ("network", "lag_compensation_view_delay_ms", "lag_compensation_view_delay_ms", 0.0),
    ("network", "worldupdate_delivery", "worldupdate_delivery", "split"),
    ("network", "worldupdate_reorder_guard", "worldupdate_reorder_guard", True),
    ("bots", "skill_balance", "bots.skill_balance", True),
    ("bots", "skill_balance_max_shift", "bots.skill_balance_max_shift", 0.35),
    ("bots", "skill_balance_rate", "bots.skill_balance_rate", 0.03),
    ("bots", "skill_balance_deadband", "bots.skill_balance_deadband", 0.15),
    ("bots", "skill_balance_min_events", "bots.skill_balance_min_events", 6),
]


def _anticheat_keys():
    from server.anticheat_report import DEFAULTS

    return [
        ("anticheat", name, f"anticheat.{name}", default)
        for name, default in DEFAULTS.items()
    ]


def all_keys():
    return FLAT_KEYS + _anticheat_keys()


def _resolve(obj, path: str):
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _shipped_toml() -> dict:
    with (ROOT / "config.toml").open("rb") as stream:
        return tomllib.load(stream)


# ---------------------------------------------------------------------------
# Defaults agree everywhere
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section,key,path,default", all_keys())
def test_dataclass_default_matches_feature_default(section, key, path, default):
    value = _resolve(ServerConfig(), path)
    assert value == default
    assert type(value) is type(default) or (
        isinstance(default, (int, float)) and not isinstance(default, bool)
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    )


@pytest.mark.parametrize("section,key,path,default", all_keys())
def test_config_toml_documents_key_with_default(section, key, path, default):
    table = _shipped_toml().get(section, {})
    assert key in table, f"config.toml [{section}] is missing {key}"
    assert table[key] == default


@pytest.mark.parametrize("section,key,path,default", all_keys())
def test_admin_guide_documents_key(section, key, path, default):
    guide = (ROOT / "docs" / "ADMIN_GUIDE.md").read_text(encoding="utf-8")
    assert f"`{key}`" in guide, f"docs/ADMIN_GUIDE.md does not mention {key}"


def test_shipped_config_loads_every_key_at_its_default():
    config = load_config(ROOT / "config.toml")
    for section, key, path, default in all_keys():
        assert _resolve(config, path) == default, (section, key)


def test_feature_fallback_constants_match_registration():
    from modes import objective_guard
    from server import escape_watch, team_balance, voting
    from server.bot_ai import skill_balance
    from server import prefab_actions

    config = ServerConfig()
    assert config.escape_watch_interval == escape_watch.DEFAULT_INTERVAL
    assert config.escape_watch_sky_seconds == escape_watch.DEFAULT_SKY_SECONDS
    assert config.escape_watch_embedded_seconds == escape_watch.DEFAULT_EMBEDDED_SECONDS
    assert config.escape_watch_entomb_seconds == escape_watch.DEFAULT_ENTOMB_SECONDS
    assert config.objective_entomb_seconds == objective_guard.DEFAULT_ENTOMB_SECONDS
    assert config.objective_afk_seconds == objective_guard.DEFAULT_AFK_SECONDS
    assert config.balance_check_interval == team_balance.DEFAULT_CHECK_INTERVAL
    assert config.balance_grace_seconds == team_balance.DEFAULT_GRACE_SECONDS
    assert config.balance_player_cooldown == team_balance.DEFAULT_PLAYER_COOLDOWN
    assert config.votekick_cooldown_seconds == voting.VOTE_COOLDOWN
    assert config.votekick_cancelled_cooldown_seconds == voting.CANCELLED_VOTE_COOLDOWN
    assert config.votekick_min_team_players == voting.KICK_MIN_TEAM_PLAYERS
    assert config.balance_bot_wait_seconds == team_balance.DEFAULT_BOT_WAIT_SECONDS
    assert config.map_vote_area_per_player == voting.MAP_VOTE_AREA_PER_PLAYER
    assert config.map_vote_min_area == voting.MAP_VOTE_MIN_AREA
    assert config.map_vote_recent_exclude == voting.MAP_VOTE_RECENT_EXCLUDE
    assert config.bots.skill_balance_max_shift == skill_balance.DEFAULT_MAX_SHIFT
    assert config.bots.skill_balance_rate == skill_balance.DEFAULT_RATE
    assert config.bots.skill_balance_deadband == skill_balance.DEFAULT_DEADBAND
    assert config.bots.skill_balance_min_events == skill_balance.DEFAULT_MIN_EVENTS
    assert config.prefab_health_state_batch == prefab_actions.BLOCK_STATE_DEFAULT_ROWS


def test_every_anticheat_report_default_is_an_anticheat_field():
    from server.anticheat_report import DEFAULTS

    fields = vars(AntiCheatConfig())
    for name, default in DEFAULTS.items():
        assert name in fields, f"AntiCheatConfig lacks {name}"
        assert fields[name] == default


# ---------------------------------------------------------------------------
# A non-default TOML value reaches the feature's own accessor
# ---------------------------------------------------------------------------

_CUSTOM_TOML = textwrap.dedent("""
    [network]
    prefab_health_state_batch = 64
    lag_compensation_enabled = false
    lag_compensation_max_ms = 180
    lag_compensation_extra_ms = 20
    lag_compensation_view_delay_ms = 16

    [lobby]
    map_vote_size_fit = false
    map_vote_area_per_player = 6000
    map_vote_min_area = 30000
    map_vote_recent_exclude = 4
    map_vote_bot_weight = 0.5
    map_size_overrides = { WW1 = "Large", DragonIsland = 27000 }

    [teams]
    auto_balance = true
    balance_mid_match = false
    balance_grace_seconds = 7.5
    balance_player_cooldown = 300
    balance_bot_wait_seconds = 20
    balance_check_interval = 2

    [objectives]
    escape_watch_enabled = false
    escape_watch_interval = 2.5
    escape_watch_sky_seconds = 8
    escape_watch_embedded_seconds = 4
    escape_watch_entomb_seconds = 9
    objective_entomb_seconds = 11
    objective_pickup_requires_los = false
    objective_pickup_ends_spawn_protection = false
    objective_afk_seconds = 90
    ctf_base_pit_depth = 12

    [bots]
    skill_balance = false
    skill_balance_max_shift = 0.2
    skill_balance_rate = 0.05
    skill_balance_deadband = 0.25
    skill_balance_min_events = 9

    [anticheat]
    report_enabled = false
    report_path = "logs/custom-ac.jsonl"
    accuracy_percentile = 95.0
    snap_frames = 3
    headshot_kill_ratio = 0.7
""")


@pytest.fixture()
def custom_server(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(_CUSTOM_TOML, encoding="utf-8")
    config = load_config(path)
    return SimpleNamespace(config=config, players={}, mode=None)


def test_objective_keys_reach_escape_watch_and_objective_guard(custom_server):
    from modes import objective_guard
    from server import escape_watch

    server = custom_server
    assert escape_watch._cfg(server, "escape_watch_enabled", True) is False
    assert escape_watch._cfg(server, "escape_watch_interval", 1.0) == 2.5
    assert escape_watch._cfg(server, "escape_watch_sky_seconds", 5.0) == 8.0
    assert escape_watch._cfg(server, "escape_watch_embedded_seconds", 3.0) == 4.0
    assert escape_watch._cfg(server, "escape_watch_entomb_seconds", 5.0) == 9.0
    assert objective_guard.entomb_seconds(server) == 11.0
    assert objective_guard._cfg(server, "objective_pickup_requires_los", True) is False
    assert objective_guard._cfg(
        server, "objective_pickup_ends_spawn_protection", True
    ) is False
    assert objective_guard._cfg(server, "objective_afk_seconds", 60.0) == 90.0
    assert server.config.ctf_base_pit_depth == 12.0


def test_team_keys_reach_team_balance(custom_server):
    from server import team_balance

    server = custom_server
    assert team_balance.TeamBalancer(server).enabled() is False
    read = team_balance._config_value
    assert read(server, "balance_grace_seconds", 5.0) == 7.5
    assert read(server, "balance_player_cooldown", 600.0) == 300.0
    assert read(server, "balance_bot_wait_seconds", 10.0) == 20.0
    assert read(server, "balance_check_interval", 1.0) == 2.0


def test_lobby_keys_reach_map_vote(custom_server, tmp_path):
    from server.voting import MAP_SIZE_CLASS_AREAS, VoteManager

    server = custom_server
    server.config.maps_path = str(tmp_path)
    manager = VoteManager.__new__(VoteManager)
    manager.server = server
    assert manager._config_number("map_vote_area_per_player", 8000.0) == 6000.0
    assert manager._config_number("map_vote_min_area", 24000.0) == 30000.0
    assert manager._config_number("map_vote_recent_exclude", 2) == 4
    assert server.config.map_vote_size_fit is False
    server.players = {
        1: SimpleNamespace(is_bot=True),
        2: SimpleNamespace(is_bot=False),
    }
    assert manager.lobby_size() == 1.5
    areas = manager._measure_maps(["ww1", "DragonIsland"])
    assert areas == {"ww1": MAP_SIZE_CLASS_AREAS["large"], "dragonisland": 27000}


def test_bot_keys_reach_skill_balancer(custom_server):
    from server.bot_ai.skill_balance import BotSkillBalancer

    balancer = BotSkillBalancer.__new__(BotSkillBalancer)
    balancer.server = custom_server
    assert balancer.enabled() is False
    assert balancer._setting("skill_balance_max_shift", 0.35) == 0.2
    assert balancer._setting("skill_balance_rate", 0.03) == 0.05
    assert balancer._setting("skill_balance_deadband", 0.15) == 0.25
    assert balancer._setting("skill_balance_min_events", 6) == 9


def test_anticheat_keys_reach_report(custom_server):
    from server import anticheat_report

    server = custom_server
    assert anticheat_report._setting(server, "report_enabled") is False
    assert anticheat_report._setting(server, "report_path") == "logs/custom-ac.jsonl"
    assert anticheat_report._setting(server, "accuracy_percentile") == 95.0
    assert anticheat_report._setting(server, "snap_frames") == 3
    assert anticheat_report._setting(server, "headshot_kill_ratio") == 0.7


def test_network_keys_reach_features(custom_server):
    config = custom_server.config
    assert config.prefab_health_state_batch == 64
    assert config.lag_compensation_enabled is False
    assert config.lag_compensation_max_ms == 180.0
    assert config.lag_compensation_extra_ms == 20.0
    assert config.lag_compensation_view_delay_ms == 16.0


# ---------------------------------------------------------------------------
# Clamping and validation
# ---------------------------------------------------------------------------


def test_out_of_range_values_are_clamped(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [network]
        prefab_health_state_batch = 99999
        lag_compensation_max_ms = -5
        [lobby]
        map_vote_bot_weight = 3
        map_vote_recent_exclude = -1
        [teams]
        balance_check_interval = 0
        [objectives]
        escape_watch_interval = 0
        objective_afk_seconds = -3
        [bots]
        skill_balance_max_shift = 5
        skill_balance_deadband = 2
        skill_balance_min_events = 0
        [anticheat]
        accuracy_percentile = 250
        headshot_hit_ratio = 4
        snap_frames = 0
    """), encoding="utf-8")
    config = load_config(path)
    assert config.prefab_health_state_batch == 4096
    assert config.lag_compensation_max_ms == 0.0
    assert config.map_vote_bot_weight == 1.0
    assert config.map_vote_recent_exclude == 0
    assert config.balance_check_interval == 0.1
    assert config.escape_watch_interval == 0.1
    assert config.objective_afk_seconds == 0.0
    assert config.bots.skill_balance_max_shift == 0.9
    assert config.bots.skill_balance_deadband == 0.95
    assert config.bots.skill_balance_min_events == 1
    assert config.anticheat.accuracy_percentile == 100.0
    assert config.anticheat.headshot_hit_ratio == 1.0
    assert config.anticheat.snap_frames == 1


@pytest.mark.parametrize("body", [
    '[lobby]\nmap_size_overrides = { WW1 = "huge" }\n',
    '[lobby]\nmap_size_overrides = { WW1 = true }\n',
    '[lobby]\nmap_size_overrides = ["WW1"]\n',
    'objectives = 3\n',
])
def test_invalid_values_are_rejected(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_empty_anticheat_report_path_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[anticheat]\nreport_path = "  "\n', encoding="utf-8")
    with pytest.raises(ValueError, match="report_path"):
        load_config(path)


# ---------------------------------------------------------------------------
# Source scan: every getattr-style read names a registered attribute
# ---------------------------------------------------------------------------

_SCANS = {
    "server/escape_watch.py": (r'_cfg\(\s*server,\s*"(\w+)"', "config"),
    "modes/objective_guard.py": (r'_cfg\(\s*server,\s*"(\w+)"', "config"),
    "server/team_balance.py": (
        r'(?:_config_value\(\s*self\.server,\s*|getattr\(config,\s*)"(\w+)"',
        "config",
    ),
    "server/voting.py": (
        r'(?:_config_number\(\s*|getattr\(\s*config,\s*|"config", None\),\s*)"(\w+)"',
        "config",
    ),
    "server/bot_ai/skill_balance.py": (
        r'(?:_setting\(|getattr\(self\._bots_config\(\),\s*)"(\w+)"',
        "bots",
    ),
    "server/anticheat_report.py": (r'_setting\(\s*server,\s*"(\w+)"', "anticheat"),
    "server/prefab_actions.py": (r'"config",\s*None\s*\),\s*"(\w+)"', "config"),
    "server/lag_compensation.py": (r'_setting\(\s*server,\s*"(\w+)"', "config"),
    "modes/ctf.py": (r'getattr\(self\.server\.config,\s*"(\w+)"', "config"),
}


# Attributes set at runtime by a launcher, not read from TOML.
_RUNTIME_ATTRIBUTES = frozenset({
    "ugc_runtime",  # server/ugc_launcher.py marks the isolated UGC editor
})


def _source_reads(relative: str, pattern: str) -> set[str]:
    source = (ROOT / relative).read_text(encoding="utf-8")
    return set(re.findall(pattern, source, flags=re.S))


@pytest.mark.parametrize("relative", sorted(_SCANS))
def test_feature_reads_only_registered_keys(relative):
    pattern, target = _SCANS[relative]
    names = _source_reads(relative, pattern)
    assert names, f"scan pattern found no reads in {relative}"
    holder = {
        "config": ServerConfig(),
        "bots": BotConfig(),
        "anticheat": AntiCheatConfig(),
    }[target]
    missing = sorted(
        name for name in names
        if not hasattr(holder, name) and name not in _RUNTIME_ATTRIBUTES
    )
    assert not missing, f"{relative} reads unregistered {target} keys: {missing}"


def test_lag_compensation_reads_registered_keys():
    source_path = ROOT / "server" / "lag_compensation.py"
    if not source_path.exists():
        pytest.skip("server/lag_compensation.py not present yet")
    source = source_path.read_text(encoding="utf-8")
    names = set(re.findall(r'"(lag_compensation_\w+)"', source))
    assert names, "lag_compensation.py reads no lag_compensation_* keys"
    config = ServerConfig()
    missing = sorted(name for name in names if not hasattr(config, name))
    assert not missing, f"unregistered lag compensation keys: {missing}"


# ---------------------------------------------------------------------------
# Omitted-key fallbacks and deprecated keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section,key,attribute",
    [
        ("server", "name", "name"),
        ("server", "port", "port"),
        ("server", "max_players", "max_players"),
        ("server", "tick_rate", "tick_rate"),
        ("game", "default_mode", "default_mode"),
        ("game", "default_map", "default_map"),
    ],
)
def test_omitted_core_keys_fall_back_to_the_shipped_config(
    section, key, attribute
):
    """A key missing from an operator's config behaves like config.toml."""

    assert getattr(ServerConfig(), attribute) == _shipped_toml()[section][key]


def test_empty_config_uses_a_shipped_map_and_a_registered_mode(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = load_config(path)

    assert config.port == 27015
    assert config.max_players == 24
    assert config.default_mode == "tdm"
    assert (ROOT / "maps" / f"{config.default_map}.vxl").is_file()


def test_fleet_port_fallback_matches_server_default():
    from server.fleet_launcher import _configured_port

    assert _configured_port({}, None) == ServerConfig().port
    assert _configured_port({"server": {}}, None) == 27015


@pytest.mark.parametrize(
    "body,fragment",
    [
        ("[weapons]\nrifle_damage = 49\n", "[weapons] rifle_damage ignored"),
        ("[weapons]\nspade_damage = 80\ngrenade_damage = 1\n",
         "[weapons] grenade_damage/spade_damage ignored"),
        ("[world]\nmap_size_x = 1024\n", "[world] map_size_x=1024 ignored"),
    ],
)
def test_dead_keys_log_a_deprecation_warning(tmp_path, caplog, body, fragment):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    with caplog.at_level("WARNING", logger="server.config"):
        config = load_config(path)
    assert fragment in caplog.text
    # Parsed for old configs, but still inert.
    assert config.map_size_z == 240


def test_shipped_config_sets_no_dead_keys(tmp_path, caplog):
    shipped = _shipped_toml()
    assert "weapons" not in shipped
    with caplog.at_level("WARNING", logger="server.config"):
        load_config(ROOT / "config.toml")
    assert "[weapons]" not in caplog.text
    assert "map_size_" not in caplog.text
