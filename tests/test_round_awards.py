"""End-of-round GameStats(67) awards for every mode, ties, leavers, bots."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import GameStats

from server import combat_scores, scoreboard
from server.combat_scores import RoundCombatStats
from server.game_constants import TEAM1, TEAM2

_MODES = ("tdm", "ctf", "cctf", "tc", "mh", "oc", "dem", "dia", "vip", "arena", "zom")


def _server(mode_code="tdm"):
    server = SimpleNamespace(
        players={},
        sent=[],
        mode=SimpleNamespace(ended=False, mode_code=mode_code),
        config=SimpleNamespace(default_mode=mode_code),
    )
    server.broadcast = lambda data, **_kwargs: server.sent.append(data)
    return server


def _player(server, player_id, team, *, bot=False, score=0, deaths=0):
    player = SimpleNamespace(
        id=player_id, team=team, score=score, deaths=deaths, kills=0,
        replication_generation=1, kill_streak=0, is_bot=bot,
        connection=SimpleNamespace(server=server),
    )
    server.players[player_id] = player
    return player


def _kill(server, killer, victim, kill_type=C.WEAPON_KILL):
    killer.kill_streak += 1
    victim.replication_generation += 1
    combat_scores.record_death(server, victim, killer, int(kill_type))


def _columns(server):
    scoreboard.broadcast_game_stats(server)
    result = {}
    for data in server.sent:
        packet = GameStats(ByteReader(data[1:]))
        result[packet.team_id] = list(zip(packet.player_ids, packet.types))
    return result


@pytest.mark.parametrize("mode_code", _MODES)
def test_every_mode_reports_kill_awards_for_both_teams(mode_code):
    server = _server(mode_code)
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2, bot=True)
    for _ in range(2):
        _kill(server, blue, green)
    _kill(server, green, blue, C.HEADSHOT_KILL)
    columns = _columns(server)
    zombie = mode_code == "zom"
    # Zombie's infected team is TEAM1: its kills are brains eaten.
    blue_kill_award = C.MOST_BRAINS_EATEN if zombie else C.MOST_KILLS
    assert (1, blue_kill_award) in columns[TEAM1]
    assert (1, C.BIGGEST_KILL_STREAK) in columns[TEAM1]
    assert {(2, C.MOST_KILLS), (2, C.MOST_HEADSHOTS)} <= set(columns[TEAM2])


def test_single_kill_streak_is_not_an_award():
    server = _server()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    _kill(server, blue, green)
    assert _columns(server)[TEAM1] == [(1, C.MOST_KILLS)]


def test_ties_go_to_score_then_fewer_deaths_then_lower_id():
    server = _server()
    first = _player(server, 5, TEAM1, score=10, deaths=4)
    second = _player(server, 3, TEAM1, score=30, deaths=9)
    third = _player(server, 1, TEAM1, score=30, deaths=2)
    for player in (first, second, third):
        player.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 4})
    assert _columns(server)[TEAM1] == [(1, C.MOST_KILLS)]
    third.deaths = 9
    server.sent.clear()
    server._game_stats_sent = False
    assert _columns(server)[TEAM1] == [(1, C.MOST_KILLS)]  # full tie: lower id
    third.score = 0
    server.sent.clear()
    server._game_stats_sent = False
    assert _columns(server)[TEAM1] == [(3, C.MOST_KILLS)]


def test_departed_and_spectating_players_are_not_awarded():
    server = _server()
    leaver = _player(server, 1, TEAM1)
    stayer = _player(server, 2, TEAM1)
    spectator = _player(server, 3, -1)
    leaver.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 9})
    stayer.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 2})
    spectator.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 20})
    del server.players[1]
    columns = _columns(server)
    assert columns[TEAM1] == [(2, C.MOST_KILLS)]
    assert all(player_id != 3 for rows in columns.values() for player_id, _ in rows)


def test_bots_earn_awards_like_players():
    server = _server()
    bot = _player(server, 1, TEAM2, bot=True)
    human = _player(server, 2, TEAM1)
    _kill(server, bot, human, C.MELEE_KILL)
    assert _columns(server)[TEAM2] == [(1, C.MOST_KILLS), (1, C.MOST_MELEE_KILLS)]


def test_suicides_team_kills_and_transitions_are_not_kill_awards():
    server = _server()
    blue = _player(server, 1, TEAM1)
    mate = _player(server, 2, TEAM1)
    combat_scores.record_death(server, blue, blue, int(C.WEAPON_KILL))
    mate.replication_generation += 1
    combat_scores.record_death(server, mate, blue, int(C.WEAPON_KILL))
    mate.replication_generation += 1
    combat_scores.record_death(server, mate, None, int(C.TEAM_CHANGE_KILL))
    assert _columns(server)[TEAM1] == [(1, C.MOST_SUICIDES)]


def test_awards_after_the_round_ended_do_not_count():
    server = _server()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    server.mode.ended = True
    _kill(server, blue, green)
    assert _columns(server) == {TEAM1: [], TEAM2: []}


def test_zombie_survivor_kills_stay_most_kills():
    from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM

    server = _server("zom")
    survivor = _player(server, 1, SURVIVOR_TEAM)
    zombie = _player(server, 2, ZOMBIE_TEAM)
    _kill(server, survivor, zombie)
    _kill(server, zombie, survivor)
    columns = _columns(server)
    assert columns[SURVIVOR_TEAM] == [(1, C.MOST_KILLS)]
    assert columns[ZOMBIE_TEAM] == [(2, C.MOST_BRAINS_EATEN)]


# ------------------------------------------- all 30 retail stat types (2026-09-28)


def test_award_pool_is_every_retail_game_stat_type():
    assert scoreboard._AWARD_ORDER == tuple(range(30))
    assert set(scoreboard._AWARD_ORDER) == {int(k) for k in C.GAME_STAT_TYPES}


def test_three_rows_are_sampled_from_every_earned_type():
    server = _server()
    blue = _player(server, 1, TEAM1)
    earned = {C.MOST_KILLS: 5, C.MOST_DISTANCE_RAN: 300, C.MOST_TIME_IN_AIR: 12,
              C.MOST_BLOCKS_PLACED: 40, C.HIGHEST_BLOCK: 60, C.MOST_DOMINATIONS: 1}
    blue.round_combat_stats = RoundCombatStats(dict(earned))
    seen = set()
    for seed in range(40):
        import random

        server._game_stats_rng = random.Random(seed)
        server.sent.clear()
        server._game_stats_sent = False
        rows = _columns(server)[TEAM1]
        assert len(rows) == 3
        assert [award for _pid, award in rows] == sorted(award for _pid, award in rows)
        seen.update(award for _pid, award in rows)
    # Over many rounds every earned type appears, not a fixed three.
    assert seen == set(earned)


def test_fewest_shots_needs_a_real_round_and_picks_the_minimum():
    server = _server()
    sharp = _player(server, 1, TEAM1)
    spray = _player(server, 2, TEAM1)
    idle = _player(server, 3, TEAM1)
    sharp.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 3})
    sharp.round_combat_stats.measures["shots"] = 4.0
    spray.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 3})
    spray.round_combat_stats.measures["shots"] = 90.0
    idle.round_combat_stats = RoundCombatStats({C.MOST_KILLS: 1})
    idle.round_combat_stats.measures["shots"] = 1.0
    assert scoreboard._award_amount(idle, C.FEWEST_SHOTS_FIRED) is None
    rows = scoreboard._team_awards(server, TEAM1)
    assert (1, C.FEWEST_SHOTS_FIRED) in rows


def test_new_round_counters_are_truthful():
    server = _server()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    blue.x, blue.y, blue.z = 10.0, 10.0, 50.0
    green.x, green.y, green.z = 70.0, 10.0, 50.0
    blue.health = 15
    green.tool = int(C.SNIPER_TOOL)
    blue.kill_streak += 1
    green.replication_generation += 1
    combat_scores.record_death(server, green, blue, int(C.WEAPON_KILL), domination=True)
    awards = blue.round_combat_stats.awards
    assert awards[C.MOST_KILLS_AT_LOW_HEALTH] == 1
    assert awards[C.LONGEST_RANGED_KILL] == 60
    assert awards[C.MOST_SNIPERS_KILLED] == 1
    assert awards[C.MOST_DOMINATIONS] == 1
    assert green.round_combat_stats.awards[C.MOST_DOMINATED] == 1
    combat_scores.record_blocks_placed(server, blue, [(1, 1, 200), (1, 1, 100)])
    combat_scores.record_blocks_destroyed(server, blue, 7, [[(0, 0, 0)] * 5])
    assert awards[C.MOST_BLOCKS_PLACED] == 2 and awards[C.HIGHEST_BLOCK] == 139
    assert awards[C.MOST_BLOCKS_DESTROYED] == 7
    assert awards[C.BIGGEST_COLLAPSING_OBJECT] == 5
    combat_scores.record_crate(server, blue, C.MOST_AMMO_CRATES_COLLECTED)
    assert awards[C.MOST_AMMO_CRATES_COLLECTED] == 1
    green.health = 40
    combat_scores.record_damage_taken(server, green, blue, 30, int(C.HEADSHOT_KILL))
    combat_scores.record_damage_taken(server, green, None, 20, int(C.AIRSTRIKE_KILL), now=100.0)
    combat_scores.record_damage_taken(server, green, None, 5, int(C.AIRSTRIKE_KILL), now=100.5)
    green_awards = green.round_combat_stats.awards
    assert green_awards[C.MOST_DAMAGE_TAKEN] == 55
    assert green_awards[C.MOST_HEADSHOTS_RECEIVED] == 1
    assert green_awards[C.MOST_AIRSTRIKES_SURVIVED] == 1  # five shells, one strike
    blue.alive, blue.airborne, blue.on_fire = True, True, True
    combat_scores.round_tick(blue, server, 0.5)
    combat_scores.round_tick(blue, server, 0.5)
    blue.airborne = False
    blue.x += 1.5
    combat_scores.round_tick(blue, server, 0.5)
    assert awards[C.MOST_TIME_IN_AIR] == 1
    assert awards[C.MOST_TIME_ON_FIRE] == 1
    assert blue.round_combat_stats.measures[C.MOST_DISTANCE_RAN] == 1.5


def test_kill_steal_is_counted_when_a_teammate_did_the_work():
    server = _server()
    stealer = _player(server, 1, TEAM1)
    worker = _player(server, 3, TEAM1)
    victim = _player(server, 2, TEAM2)
    combat_scores.record_damage(server, victim, worker, 80, now=10.0)
    combat_scores.record_damage(server, victim, stealer, 5, now=10.5)
    stealer.kill_streak += 1
    victim.replication_generation += 1
    combat_scores.record_death(server, victim, stealer, int(C.WEAPON_KILL), now=11.0)
    assert stealer.round_combat_stats.awards[C.MOST_KILL_STEALS] == 1
