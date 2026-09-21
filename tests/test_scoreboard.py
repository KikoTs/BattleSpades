"""Scoreboard + HUD-timer packet tests.

Verifies the wire pieces the client uses to show scores/time:
- SetScore(85) type PLAYER / TEAM
- DisplayCountdown(84) round timer
- GameStats(67) end-of-round widget
"""
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import SetScore, DisplayCountdown, GameStats  # noqa: E402
from server import scoreboard  # noqa: E402


def _pkt_id(data):
    return data[0]


class FakeServer:
    def __init__(self):
        self.sent = []
        self.players = {}

    def broadcast(self, data):
        self.sent.append(data)


def test_send_player_score_emits_setscore_player():
    srv = FakeServer()
    player = SimpleNamespace(id=7, score=300)
    scoreboard.send_player_score(srv, player)
    assert len(srv.sent) == 1
    pkt = SetScore(ByteReader(srv.sent[0][1:]))
    assert pkt.type == scoreboard.SCORE_PLAYER
    assert pkt.specifier == 7
    assert pkt.value == 300


def test_send_team_score_emits_setscore_team():
    srv = FakeServer()
    team = SimpleNamespace(id=2, score=42)
    scoreboard.send_team_score(srv, team)
    pkt = SetScore(ByteReader(srv.sent[0][1:]))
    assert pkt.type == scoreboard.SCORE_TEAM
    assert pkt.value == 42


def test_round_timer_sends_seconds_remaining():
    srv = FakeServer()
    scoreboard.send_round_timer(srv, 125.5)
    pkt = DisplayCountdown(ByteReader(srv.sent[0][1:]))
    assert abs(pkt.timer - 125.5) < 0.01


def test_round_timer_clamps_negative_to_zero():
    srv = FakeServer()
    scoreboard.send_round_timer(srv, -3.0)
    pkt = DisplayCountdown(ByteReader(srv.sent[0][1:]))
    assert pkt.timer == 0.0


def test_game_stats_sends_real_awards_per_team_once():
    import shared.constants as C
    from server.combat_scores import RoundCombatStats
    srv = FakeServer()
    srv.players = {
        1: SimpleNamespace(id=1, team=2, round_combat_stats=RoundCombatStats({C.MOST_KILLS: 3})),
        2: SimpleNamespace(id=2, team=2, round_combat_stats=RoundCombatStats({C.MOST_KILLS: 3, C.MOST_ASSISTS: 2})),
        3: SimpleNamespace(id=3, team=3, round_combat_stats=RoundCombatStats({C.MOST_MELEE_KILLS: 2})),
    }
    scoreboard.broadcast_game_stats(srv, winner=2)
    scoreboard.broadcast_game_stats(srv, winner=2)
    assert len(srv.sent) == 2
    pkt = GameStats(ByteReader(srv.sent[0][1:]))
    assert pkt.team_id == 2 and pkt.noOfStats == 2
    assert list(zip(pkt.player_ids, pkt.types)) == [(1, C.MOST_KILLS), (2, C.MOST_ASSISTS)]
    pkt = GameStats(ByteReader(srv.sent[1][1:]))
    assert pkt.team_id == 3
    assert pkt.player_ids == [3] and pkt.types == [C.MOST_MELEE_KILLS]


def test_late_join_replays_absolute_signed_scores_without_profile_rewards():
    srv = FakeServer()
    srv.players = {1: SimpleNamespace(id=1, score=-100)}
    srv.teams = {2: SimpleNamespace(id=2, score=19)}
    sent = []
    conn = SimpleNamespace(send=lambda data, **kw: sent.append(data))
    scoreboard.reveal_to(srv, conn)
    packets = [SetScore(ByteReader(data[1:])) for data in sent]
    assert [(p.type, p.specifier, p.value, p.reason) for p in packets] == [(1, 1, -100, 0), (0, 2, 19, 0)]
    assert not hasattr(srv.players[1], "profile_stats")


def test_show_game_stats_emits_packet_53():
    srv = FakeServer()
    scoreboard.show_game_stats(srv)
    assert _pkt_id(srv.sent[0]) == 53


def test_award_rows_are_bounded_and_empty_teams_are_encoded():
    from server.combat_scores import RoundCombatStats
    srv = FakeServer()
    srv.players = {1: SimpleNamespace(id=1, team=2, round_combat_stats=RoundCombatStats(
        {award: 1 for award in scoreboard._AWARD_ORDER}))}
    scoreboard.broadcast_game_stats(srv)
    blue, green = [GameStats(ByteReader(data[1:])) for data in srv.sent]
    assert blue.team_id == 2 and blue.noOfStats == 3
    assert green.team_id == 3 and green.noOfStats == 0


def test_score_replay_skips_unknown_dead_roster_ids():
    srv = FakeServer()
    srv.players = {1: SimpleNamespace(id=1, score=100), 2: SimpleNamespace(id=2, score=200)}
    sent = []
    conn = SimpleNamespace(known_player_lives={1: (1, 1)}, send=lambda data, **kw: sent.append(data))
    scoreboard.reveal_to(srv, conn)
    assert [SetScore(ByteReader(data[1:])).specifier for data in sent] == [1]


def test_send_map_ended_emits_packet_52():
    srv = FakeServer()
    scoreboard.send_map_ended(srv)
    assert _pkt_id(srv.sent[0]) == 52
