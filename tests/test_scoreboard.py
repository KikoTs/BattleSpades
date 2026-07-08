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


def test_game_stats_lists_all_players():
    srv = FakeServer()
    srv.players = {
        1: SimpleNamespace(id=1, spawned=True),
        2: SimpleNamespace(id=2, spawned=True),
        3: SimpleNamespace(id=3, spawned=False),
    }
    scoreboard.broadcast_game_stats(srv, winner=2)
    pkt = GameStats(ByteReader(srv.sent[0][1:]))
    assert pkt.noOfStats == 3
    assert set(pkt.player_ids) == {1, 2, 3}
