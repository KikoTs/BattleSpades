"""Accepted gameplay events and durable result boundaries for retail profiles."""
from types import SimpleNamespace

import pytest
import shared.constants as C

from server import profile_stats as stats
from tests.test_revival_master import result_service


def player(**values):
    defaults = dict(tool=C.SNIPER_TOOL, class_id=C.CLASS_SCOUT, team=2,
                    is_bot=False, alive=True, score=0, kill_streak=1,
                    position=(100.0, 100.0, 50.0), orientation=(1.0, 0.0, 0.0))
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_one_accuracy_hit_per_trigger_even_with_multiple_targets_and_pellets():
    shooter, target = player(), player(team=3)
    stats.shot(shooter)
    for _ in range(12):
        stats.hit(shooter, target)
        stats.hit(shooter, player(team=3))
    stats.shot(shooter)  # Missed second shot remains in the denominator.
    assert stats.snapshot(shooter) == {1018: [2, 0], 2018: [1, 0]}


def test_friendly_damage_and_hits_without_accepted_shot_do_not_award_accuracy():
    shooter = player()
    stats.hit(shooter, player(team=3))
    stats.shot(shooter)
    stats.hit(shooter, player(team=2))
    assert 2018 not in stats.snapshot(shooter)


def test_class_weapon_kills_headshots_and_life_streaks():
    killer = player(kill_streak=5)
    victim = player(team=3)
    stats.death(victim, killer, C.HEADSHOT_KILL)
    assert stats.snapshot(victim)[C.DEATH_SCORE_REASON] == [1, 0]
    counters = stats.snapshot(killer)
    assert counters[C.KILL_SCORE_REASON] == [1, 0]
    assert counters[C.SCOUT_SNIPER_KILLS] == [1, 0]
    assert counters[C.KILL_SCORE_HEADSHOT_REASON] == [1, 0]
    assert counters[C.COMBAT_5INAROW_TOTAL] == [1, 0]


def test_delayed_explosive_kill_does_not_become_equipped_pistol_kill():
    killer = player(class_id=C.CLASS_SOLDIER, tool=C.PISTOL_TOOL)
    stats.death(player(team=3), killer, C.ROCKET_KILL)
    counters = stats.snapshot(killer)
    assert counters[C.SOLDIER_RPG_KILLS] == [1, 0]
    assert C.SOLDIER_PISTOL_KILLS not in counters


@pytest.mark.parametrize("kill_type", [C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL,
                                     C.FORCED_TEAM_CHANGE_KILL])
def test_administrative_deaths_do_not_advance_profiles(kill_type):
    victim = player()
    stats.death(victim, None, kill_type)
    assert stats.snapshot(victim) == {}


def test_score_refresh_is_not_a_second_capture_or_kill():
    owner = player(score=100)
    stats.score_changed(owner, C.CTF_CAPTURE_SCORE_REASON)
    stats.score_changed(owner, C.CTF_CAPTURE_SCORE_REASON)
    assert stats.snapshot(owner)[C.CTF_CAPTURE_SCORE_REASON] == [1, 100]
    owner.score += 100
    stats.score_changed(owner, C.KILL_SCORE_REASON)
    assert stats.snapshot(owner)[C.KILL_SCORE_REASON] == [0, 100]


def test_map_time_accumulates_fractional_minutes_and_skips_spectators():
    server = SimpleNamespace(config=SimpleNamespace(default_map="MayanJungle"))
    owner = player()
    for _ in range(60 * 60):
        stats.tick(owner, server, 1 / 60)
    assert stats.snapshot(owner)[C.MAYAN_JUNGLE_TIME_SCORE] == [1, 0]
    owner.team = 0
    for _ in range(60):
        stats.tick(owner, server, 1)
    assert stats.snapshot(owner)[C.MAYAN_JUNGLE_TIME_SCORE] == [1, 0]


@pytest.mark.parametrize("mode", ["tutorial", "tut", "ugc"])
def test_offline_editor_and_tutorial_do_not_emit_gameplay_progress(mode):
    server = SimpleNamespace(config=SimpleNamespace(default_mode=mode))
    owner = player(connection=SimpleNamespace(server=server))
    stats.shot(owner)
    stats.add(owner, C.MAP_SINGLEBLOCKS_ADDED_TOTAL)
    assert stats.snapshot(owner) == {}


def test_departure_rejoin_and_retry_reserve_rich_stats_once(tmp_path, monkeypatch):
    service, first = result_service(tmp_path, monkeypatch)
    first.kills = first.deaths = first.score = 0
    stats.add(first, C.MAP_SINGLEBLOCKS_ADDED_TOTAL, 17)
    service.accumulate_departing_player(first)
    second = player(name=first.name, account_legacy_id=first.account_legacy_id)
    stats.add(second, C.MAP_SINGLEBLOCKS_ADDED_TOTAL, 4)
    service.server.players = {0: second}
    # Inspection is pure: repeated snapshotting cannot modify departed totals.
    for _ in range(2):
        rows, _ = service._result_players(2)
        assert rows[0]["stats"][str(C.MAP_SINGLEBLOCKS_ADDED_TOTAL)] == [21, 0]
    service._capture_round_results(2)
    event = next(iter(service._pending_results.values()))
    stats.add(second, C.MAP_SINGLEBLOCKS_ADDED_TOTAL, 3)
    assert event["players"][0]["stats"][str(C.MAP_SINGLEBLOCKS_ADDED_TOTAL)] == [21, 0]
    service._capture_round_results(2)
    service._capture_round_results(2)
    assert len(service._pending_results) == 2
    assert list(service._pending_results.values())[1]["players"][0]["stats"]["156"] == [3, 0]


def test_non_ctf_objectives_do_not_fabricate_flag_captures(tmp_path, monkeypatch):
    service, owner = result_service(tmp_path, monkeypatch)
    owner.captures = 3
    service.server.config.default_mode = "diamond_mine"
    rows, _ = service._result_players(None)
    assert "49" not in rows[0]["stats"]


def test_actual_rejected_shot_does_not_advance_weapon_stats():
    from tests.test_reversed_combat import DummyServer, make_player, make_shoot_packet
    from server.combat_runtime import get_combat_system
    server = DummyServer()
    owner, _ = make_player(server, 1, "Shooter", 2, C.SNIPER_TOOL, (100.5, 100.5, 60.0))
    combat = get_combat_system(server)
    combat.handle_shot(owner, make_shoot_packet(owner))
    combat.handle_shot(owner, make_shoot_packet(owner))  # Cadence rejects repeat.
    assert stats.snapshot(owner)[1000 + C.SNIPER_TOOL] == [1, 0]
