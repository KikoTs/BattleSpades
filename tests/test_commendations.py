"""COM_* commendations are retail leaderboard aggregates
(shared/constants.py SCORE_REASONS_FOR_TOTALS -> LeaderboardMenu columns),
not in-match medals. Every paid member reason / *_TOTAL counter must roll
into its COM_* profile stat; objective events also feed GameStats awards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from server import combat_scores as cs
from server import scoreboard
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from shared.bytes import ByteReader
from shared.packet import GameStats
from tests.test_recovered_objective_modes import _Player, _Server
from tests.test_score_events import _add, _ctf, _dia

R = C.SCORE_REASON


def _stat(player, stat):
    return player.profile_stats.values.get(int(stat), [0, 0])


def test_every_com_aggregate_member_is_a_known_stat():
    missing = [
        aggregate for aggregate in range(int(C.COM_TDM_ASSIST), int(C.COM_MH_CONTROL) + 1)
        if aggregate not in C.SCORE_REASONS_FOR_TOTALS
    ]
    # Retail never defines COM_VIP_ESCORT's members; VIP_TOTAL_SCORE lists
    # VIP_ESCORT_SCORE_REASON directly instead.
    assert missing == [int(C.COM_VIP_ESCORT)]
    for aggregate in range(int(C.COM_TDM_ASSIST), int(C.COM_MH_CONTROL) + 1):
        if aggregate in missing:
            continue
        members = C.SCORE_REASONS_FOR_TOTALS[aggregate]
        assert members and all(0 <= int(m) < int(C.COM_TDM_ASSIST) for m in members)


def test_ctf_events_roll_into_ctf_commendations():
    server = _Server()
    server.mode = SimpleNamespace(ended=False, retiring=False)
    player = _add(server, 1, TEAM1, (0.0, 0.0, 0.0))
    for reason, amount in (
        (R.CTF_ESCORT_SCORE_REASON, CG.CTF_SCORE_ESCORT_SCORE),
        (R.CTF_DISTRACT_SCORE_REASON, CG.CTF_SCORE_DISTRACT),
        (R.CTF_DEFEND_SCORE_REASON, CG.CTF_SCORE_DEFEND),
        (R.CTF_CARRIER_DEFEND_SCORE_REASON, CG.CTF_SCORE_CARRIER_DEFEND),
        (R.CTF_ASSAULT_SCORE_REASON, CG.CTF_SCORE_ASSAULT),
        (R.CTF_ASSAULT_ENEMY_SCORE_REASON, CG.CTF_SCORE_ASSAULT_ENEMY),
        (R.CTF_INTERCEPT_SCORE_REASON, CG.CTF_SCORE_INTERCEPT),
    ):
        assert cs.award_score_event(server, player, int(amount), int(reason))
    assert _stat(player, C.COM_CTF_ASSIST) == [2, 110]
    assert _stat(player, C.COM_CTF_DEFEND) == [2, 150]
    assert _stat(player, C.COM_CTF_ASSAULT) == [3, 150]
    assert _stat(player, C.COM_TDM_ASSIST) == [0, 0]


def test_bots_score_but_keep_no_profile():
    server = _Server()
    bot = _add(server, 1, TEAM2, (0.0, 0.0, 0.0))
    bot.is_bot = True
    assert cs.award_score_event(server, bot, 50, int(R.DIA_DEFEND_SCORE_REASON))
    assert bot.score == 50 and not hasattr(bot, "profile_stats")


def test_record_profile_total_rolls_counters_into_com():
    player = _Player(1, TEAM1, (0.0, 0.0, 0.0))
    cs.record_profile_total(player, C.DIA_STEAL_TOTAL)
    cs.record_profile_total(player, C.DIA_FINDANDCASHIN_TOTAL)
    cs.record_profile_total(player, C.OCC_LASTMAN_TOTAL)
    assert _stat(player, C.DIA_STEAL_TOTAL) == [1, 0]
    assert _stat(player, C.COM_DIA_STEAL) == [2, 0]
    assert _stat(player, C.COM_OCC_SURVIVAL) == [1, 0]


def test_dia_cash_in_records_find_and_steal_totals(monkeypatch):
    now = [100.0]
    server, mode = _dia(monkeypatch, now)
    finder = _add(server, 1, TEAM1, (300.0, 300.0, 60.0))
    mode._spawn_diamond((300.0, 300.0, 60.0), now=now[0], uncovered_by=finder)
    asyncio.run(mode.on_tick(1))
    assert int(finder.id) in mode.carriers
    dropoff = mode.active_dropoffs[0]
    finder.set_position(dropoff.zone.center)
    asyncio.run(mode.on_tick(2))
    assert _stat(finder, C.DIA_FINDANDCASHIN_TOTAL) == [1, 0]
    assert _stat(finder, C.DIA_STEAL_TOTAL) == [0, 0]
    assert _stat(finder, C.COM_DIA_STEAL) == [1, 0]

    # A Green thief grabs a diamond Blue carried and dropped: a steal.
    blue = _add(server, 2, TEAM1, (320.0, 320.0, 60.0))
    thief = _add(server, 3, TEAM2, (400.0, 400.0, 60.0))
    mode._spawn_diamond((320.0, 320.0, 60.0), now=now[0])
    asyncio.run(mode.on_tick(3))
    assert int(blue.id) in mode.carriers
    asyncio.run(mode._drop_carried_diamond(blue))
    blue.set_position((10.0, 10.0, 60.0))
    now[0] += 10.0
    thief.set_position((320.0, 320.0, 60.0))
    asyncio.run(mode.on_tick(4))
    assert int(thief.id) in mode.carriers
    thief.set_position(dropoff.zone.center)
    asyncio.run(mode.on_tick(5))
    assert _stat(thief, C.DIA_STEAL_TOTAL) == [1, 0]
    assert _stat(thief, C.DIA_FINDANDCASHIN_TOTAL) == [0, 0]


def _columns(server):
    columns = {}
    for data in server.sent:
        packet = GameStats(ByteReader(data[1:]))
        columns[packet.team_id] = list(zip(packet.player_ids, packet.types))
    return columns


class _StatsServer:
    def __init__(self):
        self.players = {}
        self.sent = []

    def broadcast(self, data, **_kwargs):
        self.sent.append(bytes(data))


def test_objective_events_feed_round_awards():
    server = _StatsServer()
    defender = SimpleNamespace(id=1, team=TEAM1, score=0, deaths=0,
                               round_combat_stats=cs.RoundCombatStats({C.MOST_DEFENDS: 3}))
    decoy = SimpleNamespace(id=2, team=TEAM1, score=0, deaths=0,
                            round_combat_stats=cs.RoundCombatStats({C.MOST_DISTRACTIONS: 1}))
    bagger = SimpleNamespace(id=3, team=TEAM2, score=0, deaths=0,
                             round_combat_stats=cs.RoundCombatStats({C.MOST_TEABAGS: 2}))
    server.players = {1: defender, 2: decoy, 3: bagger}
    scoreboard.broadcast_game_stats(server)
    columns = _columns(server)
    # Rows are listed in stat-id order (DISTRACTIONS 16 < DEFENDS 17).
    assert sorted(columns[TEAM1]) == [(1, C.MOST_DEFENDS), (2, C.MOST_DISTRACTIONS)]
    assert columns[TEAM2] == [(3, C.MOST_TEABAGS)]


def test_ctf_kill_events_count_defends_for_awards():
    server, mode = _ctf()
    blue = _add(server, 1, TEAM1, (256.0, 20.0, 50.0))
    threat = _add(server, 2, TEAM2, mode.intel_positions[TEAM1])
    threat.alive = False
    asyncio.run(mode.on_player_death(threat, blue, int(C.KILL.WEAPON_KILL)))
    assert cs.round_stats(blue).awards[C.MOST_DEFENDS] == 1
    assert _stat(blue, C.COM_CTF_DEFEND) == [1, int(CG.CTF_SCORE_DEFEND)]
    assert TEAM_NEUTRAL not in (blue.team, threat.team)
