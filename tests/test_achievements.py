"""Achievement catalog, store, identity, persistence and announcement.

The rules themselves are in test_achievement_rules.py (engine level),
test_achievement_modes.py (game modes) and test_achievement_wiring.py (the
real combat path).
"""

from __future__ import annotations

import json
import re
import sqlite3
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from shared.achievement_table import ACHIEVEMENTS

from server import achievements
from server.achievements import BY_NAME, TIERS, AchievementEngine, AchievementStore
from server.config import AchievementConfig, ServerConfig, load_config
from tests.achievement_helpers import (
    HEADSHOT, TEAM1, TEAM2, announcements, kill, make_player, make_server,
    progress, unlocked,
)

ROOT = Path(__file__).resolve().parents[1]

# Tiers that share one Steam statistic.
SHARED_TIERS = {
    "sniper_kill_count": [(25, "sniper_kill"), (50, "sniper_kill_hard")],
    "distance_run": [(21000, "misc_half_marathon"), (42000, "misc_marathon")],
    "intel_defence_count": [(5, "intel_defence_easy"), (10, "intel_defence_hard")],
}

# The six map achievements whose volumes were not recovered.
NOT_IMPLEMENTED = {
    "map_greatwall_destroy", "map_moon_destroy", "map_london_destroy",
    "map_concrete_destroy", "map_egypt_destroy", "map_colosseum_kill",
}


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def test_catalog_has_the_77_retail_achievements_and_32_statistics():
    assert len(ACHIEVEMENTS) == 77
    assert len({row.api_name for row in ACHIEVEMENTS}) == 77
    assert len({row.token for row in ACHIEVEMENTS}) == 77
    assert len({row.display_name for row in ACHIEVEMENTS}) == 77
    with_stat = [row for row in ACHIEVEMENTS if row.stat]
    assert len(with_stat) == 35
    assert len({row.stat for row in with_stat}) == 32 == len(TIERS)
    for row in ACHIEVEMENTS:
        assert row.token.startswith("NEW_ACHIEVEMENT_")
        assert row.display_name and row.description
        # A statistic and its threshold come together or not at all.
        assert (row.stat is None) == (row.threshold is None)
        if row.threshold is not None:
            assert isinstance(row.threshold, int) and row.threshold > 0


def test_tiers_sharing_a_statistic_are_consistent():
    shared = {stat: list(tiers) for stat, tiers in TIERS.items() if len(tiers) > 1}
    assert shared == SHARED_TIERS
    for tiers in TIERS.values():
        thresholds = [threshold for threshold, _name in tiers]
        # Ascending and distinct: the easier tier always unlocks first.
        assert thresholds == sorted(set(thresholds))
        for threshold, name in tiers:
            assert BY_NAME[name].threshold == threshold


def test_every_name_the_code_awards_exists_in_the_catalog():
    """A typo in a hook would otherwise only surface as a swallowed fault."""
    sources = [ROOT / "server" / "achievements.py", *sorted((ROOT / "modes").glob("*.py"))]
    stats, names, literals = set(), set(), set()
    for path in sources:
        text = path.read_text(encoding="utf-8")
        # The first string literal of each call is the statistic / api name.
        stats.update(re.findall(r"\.add(?:_fraction)?\([^\"\n)]*\"(\w+)\"", text))
        names.update(re.findall(r"\.unlock\([^\"\n)]*\"(\w+)\"", text))
        literals.update(re.findall(r"\"(\w+)\"", text))
    # A statistic may also be picked into a variable before the call.
    stats |= literals & set(TIERS)
    # Map achievements are named by the maps; the engine only lists the
    # extra conditions of some. Every other api name in the sources is one
    # the code awards (at a call site, through a helper or from a table).
    conditions = set().union(
        achievements._MELEE_KILL_REGIONS, achievements._ZOMBIE_VICTIM_KILL_REGIONS,
        achievements._ZOMBIE_DESTROY_REGIONS, achievements._ROCKET_DESTROY_REGIONS,
        achievements._DEMOLITION_DESTROY_REGIONS,
    )
    names |= (literals & set(BY_NAME)) - conditions
    assert stats and stats <= set(TIERS), stats - set(TIERS)
    assert names and names <= set(BY_NAME), names - set(BY_NAME)
    assert conditions <= set(BY_NAME), conditions - set(BY_NAME)
    # Every statistic is fed by some hook, and everything but the six
    # documented map achievements can be unlocked.
    assert stats == set(TIERS)
    recovered = set()  # map achievements are named by the maps' own ac_ids
    for path in (ROOT / "maps").glob("*.json"):
        recovered.update(json.loads(path.read_text(encoding="utf-8")).get("ac_ids", ()))
    assert recovered == {
        "map_isleofdoom_destroy", "map_maya_kill",
        "map_zombieisland_destroy", "map_zombieisland_zombie_kill",
    }
    awarded = names | recovered | {name for stat in stats for _t, name in TIERS[stat]}
    assert set(BY_NAME) - awarded == NOT_IMPLEMENTED


def test_announcement_string_is_the_retail_one():
    assert achievements.ANNOUNCEMENT_STRING_ID == "ACHIEVEMENT_GAINED"


def test_the_documentation_lists_all_77_with_the_same_status():
    text = (ROOT / "docs" / "ACHIEVEMENTS.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) >= 8 and cells[1].startswith("`") and cells[5] in ("implemented", "not yet"):
            rows[cells[1].strip("`")] = cells
    assert set(rows) == set(BY_NAME)
    assert {name for name, cells in rows.items() if cells[5] == "not yet"} == NOT_IMPLEMENTED
    for name, cells in rows.items():
        row = BY_NAME[name]
        assert cells[2] == row.display_name and cells[3] == row.description
        expected = f"`{row.stat}` >= {row.threshold}" if row.stat else "one-shot"
        assert cells[4] == expected
        assert cells[6], name  # every row states its rule or its reason
    assert f"{77 - len(NOT_IMPLEMENTED)} of the 77 are implemented" in text
    # The six are also listed with what each one needs.
    needs = text.split("## Not implemented", 1)[1].split("## Map volumes", 1)[0]
    assert all(f"`{name}`" in needs for name in NOT_IMPLEMENTED)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_store_counters_are_increments_and_unlocks_are_idempotent(tmp_path):
    path = tmp_path / "state" / "achievements.sqlite3"
    store = AchievementStore(path)
    store.add_counters([("steam:1", "spade_kill_count", 3), ("steam:1", "distance_run", 10)])
    store.add_counters([("steam:1", "spade_kill_count", 4), ("steam:2", "spade_kill_count", 1)])
    assert store.counters("steam:1") == {"spade_kill_count": 7, "distance_run": 10}
    assert store.counters("steam:2") == {"spade_kill_count": 1}
    assert store.unlock("steam:1", "spade_kill", "Kiko", 100.0) is True
    assert store.unlock("steam:1", "spade_kill", "Kiko", 200.0) is False
    assert store.unlocks("steam:1") == {"spade_kill": 100.0}
    store.close()

    # A second process (or a restart) sees the same rows and adds to them.
    again = AchievementStore(path)
    again.add_counters([("steam:1", "spade_kill_count", 1)])
    assert again.counters("steam:1")["spade_kill_count"] == 8
    assert again.unlocks("steam:1") == {"spade_kill": 100.0}
    again.close()


def test_store_hands_each_unlock_to_the_master_once():
    store = AchievementStore(":memory:")
    store.unlock("aosplay:7", "spade_kill", "A", 1.0)
    store.unlock("aosplay:7", "pickaxe_kill", "A", 2.0)
    store.unlock("aosplay:8", "spade_kill", "B", 3.0)
    assert store.take_unreported("aosplay:7", 10.0) == ["spade_kill", "pickaxe_kill"]
    assert store.take_unreported("aosplay:7", 11.0) == []
    assert store.take_unreported("aosplay:8", 12.0) == ["spade_kill"]


# ---------------------------------------------------------------------------
# Engine: counters, tiers, unlocks
# ---------------------------------------------------------------------------

def test_add_unlocks_at_the_threshold_not_before():
    server = make_server()
    player = make_player(server, 1)
    engine = server.achievements
    assert engine.add(player, "spade_kill_count", 9) == []
    assert unlocked(server, player) == set()
    assert announcements(server) == []
    assert engine.add(player, "spade_kill_count") == ["spade_kill"]
    assert unlocked(server, player) == {"spade_kill"}
    assert announcements(server) == [("ACHIEVEMENT_GAINED", ["P1", "Dig Deep"])]


@pytest.mark.parametrize("stat", sorted(SHARED_TIERS))
def test_tiers_sharing_a_statistic_unlock_one_after_the_other(stat):
    (low, easy), (high, hard) = SHARED_TIERS[stat]
    server = make_server()
    player = make_player(server, 1)
    engine = server.achievements
    assert engine.add(player, stat, low - 1) == []
    assert engine.add(player, stat) == [easy]
    assert engine.add(player, stat, high - low - 1) == []
    assert unlocked(server, player) == {easy}
    assert engine.add(player, stat) == [hard]
    assert unlocked(server, player) == {easy, hard}
    assert [row[1][1] for row in announcements(server)] == [
        BY_NAME[easy].display_name, BY_NAME[hard].display_name,
    ]


def test_one_jump_past_both_tiers_unlocks_both():
    server = make_server()
    player = make_player(server, 1)
    assert server.achievements.add(player, "sniper_kill_count", 60) == [
        "sniper_kill", "sniper_kill_hard",
    ]


def test_unlock_is_saved_before_it_is_announced_and_never_twice():
    server = make_server()
    player = make_player(server, 1)
    engine = server.achievements
    seen = []
    original = server.broadcast

    def broadcast(data, **kwargs):
        # The row is already in the store when the packet goes out.
        seen.append(engine.store.unlocks(engine.identity_for(player)))
        original(data, **kwargs)

    server.broadcast = broadcast
    assert engine.unlock(player, "jetpack_killed_using") is True
    assert seen == [{"jetpack_killed_using": pytest.approx(seen[0]["jetpack_killed_using"])}]
    assert engine.unlock(player, "jetpack_killed_using") is False
    assert engine.add(player, "spade_kill_count", 10) == ["spade_kill"]
    assert engine.add(player, "spade_kill_count", 10) == []
    assert len(announcements(server)) == 2


def test_unknown_names_are_rejected():
    server = make_server()
    player = make_player(server, 1)
    with pytest.raises(KeyError):
        server.achievements.add(player, "no_such_stat")
    with pytest.raises(KeyError):
        server.achievements.unlock(player, "no_such_achievement")
    # Through a gameplay hook the same mistake is swallowed and counted.
    assert achievements.add(server, player, "no_such_stat") == ()
    assert server.achievements.faults == {"add": 1}


def test_progress_and_unlocks_survive_an_engine_restart(tmp_path):
    path = tmp_path / "achievements.sqlite3"
    server = make_server(store=AchievementStore(path))
    player = make_player(server, 1, name="Kiko")
    engine = server.achievements
    engine.add(player, "spade_kill_count", 7)
    engine.add(player, "pickaxe_kill_count", 10)
    engine.close()
    assert engine.active is False

    reopened = make_server(store=AchievementStore(path))
    again = make_player(reopened, 4, name="Kiko")
    assert progress(reopened, again, "spade_kill_count") == 7
    assert unlocked(reopened, again) == {"pickaxe_kill"}
    # Already unlocked before the restart: no second unlock or broadcast.
    assert reopened.achievements.add(again, "pickaxe_kill_count", 5) == []
    assert reopened.achievements.add(again, "spade_kill_count", 3) == ["spade_kill"]
    assert [row[1] for row in announcements(reopened)] == [["Kiko", "Dig Deep"]]
    reopened.achievements.close()


def test_counters_are_flushed_in_batches_and_when_a_player_leaves(tmp_path):
    path = tmp_path / "achievements.sqlite3"
    server = make_server(store=AchievementStore(path))
    player = make_player(server, 1, name="Kiko")
    engine = server.achievements
    engine.add(player, "distance_run", 5)
    reader = AchievementStore(path)
    assert reader.counters("name:kiko") == {}
    # The periodic flush.
    for _ in range(int(achievements.FLUSH_INTERVAL_SECONDS) + 1):
        engine.tick(1.0)
    assert reader.counters("name:kiko") == {"distance_run": 5}
    engine.add(player, "distance_run", 2)
    achievements.player_left(server, player)
    assert reader.counters("name:kiko") == {"distance_run": 7}
    reader.close()
    engine.close()


def test_two_servers_sharing_one_store_add_up(tmp_path):
    path = tmp_path / "achievements.sqlite3"
    first = make_server(store=AchievementStore(path))
    second = make_server(store=AchievementStore(path))
    one = make_player(first, 1, name="Kiko")
    two = make_player(second, 1, name="Kiko")
    first.achievements.add(one, "spade_kill_count", 6)
    first.achievements.flush()
    # Loaded after the first server saved: the second continues from six.
    assert second.achievements.add(two, "spade_kill_count", 4) == ["spade_kill"]
    assert len(announcements(second)) == 1
    # The first server still holds its own view (six, locked). Reaching ten
    # there finds the row already recorded and announces nothing.
    assert first.achievements.add(one, "spade_kill_count", 4) == []
    assert announcements(first) == []
    assert "spade_kill" in unlocked(first, one)
    # A later session on the first server reads the shared total.
    achievements.player_left(first, one)
    back = make_player(first, 2, name="Kiko")
    assert progress(first, back, "spade_kill_count") == 14
    assert first.achievements.unlock(back, "spade_kill") is False
    assert announcements(first) == []
    first.achievements.close()
    second.achievements.close()


def test_a_store_failure_never_reaches_gameplay_and_is_retried():
    clock = SimpleNamespace(now=50.0)
    server = make_server(clock=lambda: clock.now)
    player = make_player(server, 1)
    engine = server.achievements
    real = engine.store

    class Broken:
        path = "broken"

        def counters(self, identity):
            return real.counters(identity)

        def unlocks(self, identity):
            return real.unlocks(identity)

        def add_counters(self, rows):
            raise sqlite3.OperationalError("database is locked")

        def unlock(self, *args):
            raise sqlite3.OperationalError("database is locked")

    engine.store = Broken()
    assert engine.add(player, "spade_kill_count", 10) == []
    assert announcements(server) == []
    # Not retried on every call while the file stays locked.
    engine.store = real
    assert engine.unlock(player, "spade_kill") is False
    clock.now += achievements.SAVE_RETRY_SECONDS + 0.1
    # The counter was kept in memory and is written with the unlock.
    assert engine.add(player, "spade_kill_count") == ["spade_kill"]
    assert real.counters(engine.identity_for(player)) == {"spade_kill_count": 11}


def test_a_faulty_hook_is_swallowed_counted_and_logged_once(caplog):
    server = make_server()
    player = make_player(server, 1)

    def boom(*_args, **_kwargs):
        raise RuntimeError("bug")

    server.achievements.died = boom
    victim = make_player(server, 2, TEAM2)
    with caplog.at_level("ERROR"):
        achievements.died(server, victim, player, 0, False)
        achievements.died(server, victim, player, 0, False)
    assert server.achievements.faults == {"died": 2}
    assert sum("Achievement hook died failed" in record.message for record in caplog.records) == 1


def test_hooks_are_inert_without_a_started_engine():
    bare = SimpleNamespace(players={}, config=ServerConfig())
    player = SimpleNamespace(id=1, name="P", team=TEAM1, is_bot=False)
    assert achievements.engine_of(bare) is None
    assert achievements.add(bare, player, "spade_kill_count", 10) == ()
    assert achievements.unlock(bare, player, "spade_kill") is False
    achievements.died(bare, player, player, 0, False)
    achievements.tick(bare, 1.0)
    with achievements.block_cause(bare, player, int(C.DRILL_KILL)):
        pass
    # Constructed but not started, as in BattleSpadesServer.__init__.
    bare.achievements = AchievementEngine(bare)
    assert achievements.engine_of(bare) is None
    assert achievements.add(bare, player, "spade_kill_count", 10) == ()
    assert not hasattr(player, "achievement_tracker")
    assert achievements.add(None, player, "spade_kill_count") == ()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_identity_prefers_the_verified_account_then_steam_then_the_name():
    server = make_server()
    engine = server.achievements
    peer = object()
    server.steam_p2p = SimpleNamespace(
        identity_for=lambda candidate: "steam:76561198000000001" if candidate is peer else None
    )
    account = make_player(server, 1, name="Kiko", account_legacy_id="900000000000001")
    account.connection.peer = peer
    relay = make_player(server, 2, name="Kiko2")
    relay.connection.peer = peer
    direct = make_player(server, 3, name="  DeuCe ")
    bot = make_player(server, 4, name="[BOT] Rex", bot=True)
    nameless = make_player(server, 5, name="")
    assert engine.identity_for(account) == "aosplay:900000000000001"
    assert engine.identity_for(relay) == "steam:76561198000000001"
    assert engine.identity_for(direct) == "name:deuce"
    assert engine.identity_for(bot) is None
    assert engine.identity_for(nameless) is None
    assert engine.identity_for(None) is None


def test_progress_follows_the_identity_not_the_connection():
    server = make_server()
    first = make_player(server, 1, name="Kiko", account_legacy_id="42")
    server.achievements.add(first, "spade_kill_count", 9)
    # The same account on another slot, with another displayed name.
    second = make_player(server, 9, name="Renamed", account_legacy_id="42")
    assert server.achievements.add(second, "spade_kill_count") == ["spade_kill"]
    stranger = make_player(server, 3, name="Kiko")
    assert progress(server, stranger, "spade_kill_count") == 0


def test_bots_never_earn_and_never_announce():
    server = make_server()
    bot = make_player(server, 1, bot=True)
    human = make_player(server, 2, TEAM2)
    engine = server.achievements
    assert engine.add(bot, "spade_kill_count", 100) == []
    assert engine.unlock(bot, "jetpack_killed_using") is False
    for _ in range(15):
        kill(server, bot, human, HEADSHOT)
    assert announcements(server) == []
    assert engine._progress == {} and engine._dirty == {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_config_defaults_and_shipped_toml_agree():
    defaults = AchievementConfig()
    assert (defaults.enabled, defaults.count_bot_kills, defaults.path) == (
        True, True, "state/achievements.sqlite3",
    )
    with (ROOT / "config.toml").open("rb") as stream:
        table = tomllib.load(stream)["achievements"]
    assert table == {
        "enabled": True, "count_bot_kills": True, "path": "state/achievements.sqlite3",
    }
    shipped = load_config(ROOT / "config.toml").achievements
    assert shipped == defaults


def test_config_toml_values_are_parsed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [achievements]
        enabled = false
        count_bot_kills = false
        path = " custom/ach.sqlite3 "
    """), encoding="utf-8")
    parsed = load_config(path).achievements
    assert (parsed.enabled, parsed.count_bot_kills, parsed.path) == (
        False, False, "custom/ach.sqlite3",
    )
    path.write_text('[achievements]\npath = ""\n', encoding="utf-8")
    with pytest.raises(ValueError, match="achievements.path"):
        load_config(path)
    path.write_text('achievements = 3\n', encoding="utf-8")
    with pytest.raises(ValueError, match="TOML table"):
        load_config(path)


def test_runtime_paths_anchor_the_store_to_the_application_root(tmp_path):
    from server.runtime_paths import RuntimePaths, apply_runtime_paths

    paths = RuntimePaths.from_root(tmp_path)
    config = apply_runtime_paths(ServerConfig(), paths)
    assert Path(config.achievements.path) == tmp_path / "state" / "achievements.sqlite3"
    memory = ServerConfig()
    memory.achievements.path = ":memory:"
    assert apply_runtime_paths(memory, paths).achievements.path == ":memory:"


def test_container_moves_the_store_to_the_data_volume(tmp_path):
    from scripts.container_entrypoint import build_runtime_config
    from tests.test_container_entrypoint import _template

    environment = {"BATTLESPADES_ADMIN_PASSWORD": "strong-local-password"}
    document = build_runtime_config(_template(), environment, data_directory=tmp_path)
    assert document["achievements"]["path"] == str(
        tmp_path / "state" / "achievements.sqlite3"
    )
    # An absolute path is the operator's choice; an in-memory store has none.
    template = _template()
    template.setdefault("achievements", {})["path"] = str(tmp_path / "elsewhere.sqlite3")
    document = build_runtime_config(template, environment, data_directory=tmp_path / "data")
    assert document["achievements"]["path"] == str(tmp_path / "elsewhere.sqlite3")
    template["achievements"]["path"] = ":memory:"
    document = build_runtime_config(template, environment, data_directory=tmp_path / "data")
    assert document["achievements"]["path"] == ":memory:"


def test_start_and_close_are_idempotent():
    server = make_server()
    engine = server.achievements
    store = engine.store
    assert engine.start() is True and engine.store is store
    engine.close()
    engine.close()
    assert engine.active is False


def test_disabled_engine_never_starts_or_opens_a_file(tmp_path):
    config = ServerConfig()
    config.achievements.enabled = False
    config.achievements.path = str(tmp_path / "never.sqlite3")
    server = SimpleNamespace(players={}, config=config, sent=[])
    engine = server.achievements = AchievementEngine(server)
    assert engine.start() is False
    player = make_player(server, 1)
    assert achievements.add(server, player, "spade_kill_count", 10) == ()
    assert not (tmp_path / "never.sqlite3").exists()


def test_an_unusable_store_path_falls_back_to_memory(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory", encoding="utf-8")
    config = ServerConfig()
    config.achievements.path = str(blocker / "state" / "achievements.sqlite3")
    server = SimpleNamespace(players={}, config=config, sent=[],
                             mode=SimpleNamespace(ended=False))
    server.broadcast = lambda data, **_kwargs: server.sent.append(bytes(data))
    engine = server.achievements = AchievementEngine(server)
    with caplog.at_level("WARNING"):
        assert engine.start() is True
    assert engine.store.path == ":memory:"
    assert any("will not survive a restart" in record.message for record in caplog.records)
    player = make_player(server, 1)
    assert engine.add(player, "spade_kill_count", 10) == ["spade_kill"]


def test_the_default_path_is_used_and_created_on_start(tmp_path):
    config = ServerConfig()
    config.achievements.path = str(tmp_path / "state" / "achievements.sqlite3")
    server = SimpleNamespace(players={}, config=config, sent=[])
    engine = AchievementEngine(server)
    assert not (tmp_path / "state").exists()
    assert engine.start() is True
    assert (tmp_path / "state" / "achievements.sqlite3").is_file()
    engine.close()


# ---------------------------------------------------------------------------
# No official / ranked / write-token gate
# ---------------------------------------------------------------------------

def test_a_local_create_match_server_with_bots_still_unlocks(monkeypatch, tmp_path):
    """The player's own server: unofficial, no master credential, bots only."""
    monkeypatch.delenv("AOS_MASTER_WRITE_TOKEN", raising=False)
    server = make_server(store=AchievementStore(tmp_path / "achievements.sqlite3"))
    config = server.config
    assert config.revival.official is False
    config.revival.require_identity = False
    host = make_player(server, 0, TEAM1, name="Host", tool=int(C.SPADE_TOOL))
    # A legacy join: no ticket, no verified identity, not ranked.
    host.identity_type, host.ranked_eligible = "legacy", False
    host.account_legacy_id = host.account_public_id = None
    bots = [make_player(server, index, TEAM2, name=f"[BOT] {index}", bot=True)
            for index in range(1, 6)]
    for index in range(10):
        kill(server, host, bots[index % len(bots)], int(C.MELEE_KILL))
    assert unlocked(server, host) == {"spade_kill", "misc_five_in_a_row", "misc_ten_in_a_row"}
    assert [row[1] for row in announcements(server)] == [
        ["Host", "Five Alive"], ["Host", "Streaker"], ["Host", "Dig Deep"],
    ]
    # It is on disk under the name identity, for the next local session.
    server.achievements.close()
    saved = AchievementStore(tmp_path / "achievements.sqlite3")
    assert set(saved.unlocks("name:host")) == {
        "spade_kill", "misc_five_in_a_row", "misc_ten_in_a_row",
    }
    assert saved.counters("name:host") == {"spade_kill_count": 10}
    saved.close()


def test_the_engine_reads_no_official_ranked_or_token_setting():
    source = (ROOT / "server" / "achievements.py").read_text(encoding="utf-8")
    code = "\n".join(source.split('"""')[2::2])  # outside docstrings
    for forbidden in ("official", "ranked", "ranked_eligible", "WRITE_TOKEN",
                      "write_token", "require_identity", "revival"):
        assert not re.search(rf"\b{forbidden}\b", code), forbidden


@pytest.mark.parametrize("official", [False, True])
def test_official_flag_changes_nothing(official):
    server = make_server()
    server.config.revival.official = official
    player = make_player(server, 1, tool=int(C.SPADE_TOOL))
    victim = make_player(server, 2, TEAM2)
    for _ in range(10):
        victim.kill_streak = 0
        kill(server, player, victim, int(C.MELEE_KILL))
    assert "spade_kill" in unlocked(server, player)


# ---------------------------------------------------------------------------
# Bot-kill switch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count_bot_kills,expected", [(True, 10), (False, 0)])
def test_bot_kill_switch(count_bot_kills, expected):
    server = make_server(count_bot_kills=count_bot_kills)
    player = make_player(server, 1, tool=int(C.SPADE_TOOL))
    bot = make_player(server, 2, TEAM2, bot=True)
    for _ in range(10):
        kill(server, player, bot, int(C.MELEE_KILL))
    assert progress(server, player, "spade_kill_count") == expected
    assert ("spade_kill" in unlocked(server, player)) is count_bot_kills
    # With the switch off nothing a bot kill feeds unlocks, streaks included.
    if not count_bot_kills:
        assert unlocked(server, player) == set()
        human = make_player(server, 3, TEAM2)
        player.kill_streak = 0
        for _ in range(10):
            kill(server, player, human, int(C.MELEE_KILL))
        assert "spade_kill" in unlocked(server, player)


# ---------------------------------------------------------------------------
# Master round result
# ---------------------------------------------------------------------------

def _master(monkeypatch, tmp_path, *, token="secret"):
    from server.revival_master import RevivalMasterService

    if token:
        monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", token)
    else:
        monkeypatch.delenv("AOS_MASTER_WRITE_TOKEN", raising=False)
    server = make_server()
    server.config.revival.enabled = True
    server.config.revival.results_path = str(tmp_path / "results.sqlite3")
    server.config.port = 32887
    server.world_manager = SimpleNamespace(map_file_crc=0x1234, map_name="Classic")
    service = RevivalMasterService(server)
    return server, service


def test_round_result_carries_new_unlocks_once(monkeypatch, tmp_path):
    server, service = _master(monkeypatch, tmp_path)
    player = make_player(server, 1, name="Kiko", account_legacy_id="900000000000001",
                         kills=3, deaths=1, captures=0, score=30)
    other = make_player(server, 2, TEAM2, name="Deuce", account_legacy_id="900000000000002",
                        kills=1, deaths=3, captures=0, score=10)
    server.achievements.unlock(player, "jetpack_killed_using")
    server.achievements.add(player, "spade_kill_count", 10)
    service._capture_round_results(TEAM1)
    (event,) = service._pending_results.values()
    by_id = {row["steamid"]: row for row in event["players"]}
    assert by_id["900000000000001"]["achievements"] == ["jetpack_killed_using", "spade_kill"]
    # Nothing unlocked: the optional field is simply absent.
    assert "achievements" not in by_id["900000000000002"]
    assert set(by_id["900000000000002"]) == {"steamid", "name", "total", "stats"}

    # The next round reports only what is new since.
    player.kills, player.score = 5, 50
    server.achievements.unlock(other, "rocket_fall")
    other.kills, other.score = 2, 20
    service._capture_round_results(TEAM1)
    second = list(service._pending_results.values())[1]
    by_id = {row["steamid"]: row for row in second["players"]}
    assert "achievements" not in by_id["900000000000001"]
    assert by_id["900000000000002"]["achievements"] == ["rocket_fall"]


def test_no_round_result_and_no_handover_without_the_master(monkeypatch, tmp_path):
    server, service = _master(monkeypatch, tmp_path, token="")
    player = make_player(server, 1, name="Kiko", account_legacy_id="900000000000001",
                         kills=3, deaths=1, captures=0, score=30)
    server.achievements.unlock(player, "jetpack_killed_using")
    service._capture_round_results(TEAM1)
    service.schedule_round_results(TEAM1)
    assert service._pending_results == {}
    # Still unreported: a later round with the master active hands it over.
    assert server.achievements.take_unreported("aosplay:900000000000001") == [
        "jetpack_killed_using",
    ]


def test_round_result_is_unchanged_when_achievements_are_off(monkeypatch, tmp_path):
    server, service = _master(monkeypatch, tmp_path)
    server.achievements.close()
    make_player(server, 1, name="Kiko", account_legacy_id="900000000000001",
                kills=3, deaths=1, captures=0, score=30)
    service._capture_round_results(TEAM1)
    (event,) = service._pending_results.values()
    assert all("achievements" not in row for row in event["players"])
