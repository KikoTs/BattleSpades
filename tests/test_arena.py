"""Arena round lifecycle regressions (respawn path, leave, scores, restart)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from modes.arena import ArenaMode
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.team import Team
from shared.bytes import ByteReader
from shared.packet import SetScore


class _Server:
    tick_rate = 60

    def __init__(self, settings=None):
        self.config = SimpleNamespace(mode_settings=settings or {})
        self.teams = {
            TEAM1: Team(TEAM1, "Blue", (0, 0, 255)),
            TEAM2: Team(TEAM2, "Green", (0, 255, 0)),
        }
        self.players = {}
        self.packets = []
        self.respawned = []

    def broadcast(self, data, **_kwargs):
        self.packets.append(bytes(data))

    def respawn_player(self, player):
        self.respawned.append(player.id)
        player.alive = True
        player.spawned = True


def _player(server, player_id, team, alive=True):
    player = SimpleNamespace(
        id=player_id, team=team, alive=alive, spawned=alive, name=f"P{player_id}",
        score=0, connection=None,
    )
    server.players[player_id] = player
    return player


def _team_scores(packets):
    rows = []
    for data in packets:
        if data and data[0] == SetScore.id:
            packet = SetScore(ByteReader(data[1:]))
            rows.append((packet.specifier, packet.value))
    return rows


def _live_round(server):
    mode = ArenaMode(server)
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode._begin_round())
    return mode


def test_mode_start_does_not_spawn_and_restart_resets_round_counter():
    server = _Server()
    _player(server, 1, TEAM1)
    mode = ArenaMode(server)
    asyncio.run(mode.on_mode_start())
    # The in-place restart (BaseMode._restart_round) and the rollover join
    # path own the bodies; on_mode_start must not send a second CreatePlayer.
    assert server.respawned == []
    assert mode.current_round == 1
    mode.current_round = 4
    mode.round_wins = {TEAM1: 3, TEAM2: 2}
    server.teams[TEAM1].score = 3
    asyncio.run(mode.on_mode_start())
    assert mode.current_round == 1
    assert mode.round_wins == {TEAM1: 0, TEAM2: 0}
    assert server.teams[TEAM1].score == 0


def test_next_round_respawns_players_through_the_server_path(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.arena.time.time", lambda: now[0])
    server = _Server()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    _player(server, 3, TEAM_SPECTATOR, alive=False)
    mode = _live_round(server)
    blue.alive = False
    asyncio.run(mode.on_player_death(blue, green, 0))
    assert mode.round_ended and mode.next_round_at == 105.0
    now[0] = 105.0
    asyncio.run(mode.on_tick(1))
    assert mode.current_round == 2
    assert sorted(server.respawned) == [1, 2]  # never the spectator


def test_no_timed_respawn_once_the_round_is_live():
    server = _Server()
    blue = _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    mode = ArenaMode(server)
    asyncio.run(mode.on_mode_start())
    assert mode.can_player_respawn(blue)  # countdown: late spawns allowed
    asyncio.run(mode._begin_round())
    assert not mode.can_player_respawn(blue)
    asyncio.run(mode._end_round(TEAM1))
    assert not mode.can_player_respawn(blue)  # intermission: next round respawns


def test_leaving_last_fighter_ends_round_and_mirrors_team_score():
    server = _Server()
    blue = _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    mode = _live_round(server)
    server.players.pop(blue.id)
    asyncio.run(mode.on_player_leave(blue))
    assert mode.round_ended
    assert mode.round_wins == {TEAM1: 0, TEAM2: 1}
    assert server.teams[TEAM2].score == 1
    assert (TEAM2, 1) in _team_scores(server.packets)


def test_reused_player_id_does_not_keep_a_departed_fighter_alive():
    server = _Server()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    mode = _live_round(server)
    # Departure event arrives after the id was reused by a newcomer.
    server.players.pop(blue.id)
    _player(server, 1, TEAM1, alive=False)
    asyncio.run(mode.on_player_leave(blue))
    assert mode.round_wins[TEAM2] == 1
    assert green.alive


def test_match_win_ends_once_through_score_end():
    server = _Server({"arena": {"rounds_to_win": 2}})
    _player(server, 1, TEAM1)
    _player(server, 2, TEAM2)
    mode = _live_round(server)
    assert mode.rounds_to_win == 2 and mode.score_limit == 2
    ends = []

    async def end(winner=None):
        ends.append(winner)
        mode.ended = True

    mode.on_mode_end = end
    asyncio.run(mode._end_round(TEAM1))
    mode.round_started, mode.round_ended = True, False
    asyncio.run(mode._end_round(TEAM1))
    asyncio.run(mode._end_round(TEAM1))  # after the end: ignored
    assert ends == [TEAM1]
    assert mode.next_round_at is None


def test_arena_overlay_rules():
    server = _Server({"arena": {"round_time_limit": 60, "time_limit": 600}})
    mode = ArenaMode(server)
    assert mode.round_time_limit == 60.0
    assert mode.time_limit == 600.0
    assert ArenaMode(_Server()).time_limit == 0.0
