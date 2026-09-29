"""Round-2 mode fixes: restarts, admin transitions and late-joiner reveal.

* an in-place restart never gives a spectator a body;
* a retiring mode (admin map/mode change, same-map restart) never finishes
  the match, opens a map vote or starts its end sequence while its roster
  is detached -- verified for Zombie, VIP and Arena;
* an end-screen joiner gets ForceShowScores(1) and the ending track.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import ForceShowScores, PlayMusic

from modes.arena import ArenaMode
from modes.base_mode import BaseMode
from modes.tdm import TDMMode
from modes.vip import VIPMode, VIPPhase
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombieMode, ZombiePhase
from server import audio
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.match import MatchTransitionService
from tests import test_arena as arena_fx
from tests import test_vip as vip_fx
from tests import test_zombie as zombie_fx
from tests.test_match_transitions import _Connection as _TransitionConnection
from tests.test_match_transitions import _NewMode
from tests.test_mode_lifecycle_contracts import _Conn, _scoring_server


class _Votes:
    def __init__(self) -> None:
        self.opened: list[float] = []
        self.cancels = 0

    def ensure_round_end_map_vote(self, now) -> None:
        self.opened.append(now)

    def ensure_map_vote(self, now) -> None:
        self.opened.append(now)

    def cancel(self) -> None:
        self.cancels += 1

    def consume_next_map(self):
        return None


def _prepare_rollover(server, ordered_players, mode_code: str) -> _Votes:
    """Give a mode-test fake server what MatchTransitionService needs."""
    votes = _Votes()
    server.vote_manager = votes
    for key, value in {
        "default_map": "London",
        "default_mode": mode_code,
        "maps_path": "maps",
        "transition_grace_seconds": 0.0,
    }.items():
        setattr(server.config, key, value)
    server.connections = {}
    for player in ordered_players:
        connection = _TransitionConnection()
        connection.player = player
        server.connections[object()] = connection
    server.fog_color_override = None
    if not hasattr(server, "world_manager"):
        server.world_manager = SimpleNamespace(map_name="London")
    server.host = SimpleNamespace(flush=lambda: None)
    server.terrain_repair = SimpleNamespace(reset=lambda: None)
    server.reset_round_runtime = lambda: None
    server._pending_ingame_packets = deque()
    server._mode_events = deque()
    server._map_mutation_journal = deque()
    server._map_mutation_sequence = 0
    return votes


def _change_mode(monkeypatch, server) -> None:
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="London", config=None)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(service, "_load_world_candidate", lambda *_a: candidate)
    result = asyncio.run(service.change_mode("tdm"))
    assert result.ok is True
    assert isinstance(server.mode, _NewMode)


# -- 2. restart never respawns spectators --------------------------------


class _PlainMode(BaseMode):
    name = "Plain"


def test_in_place_restart_skips_spectators():
    server = _scoring_server()
    respawned = []
    server.respawn_player = lambda player: respawned.append(player.id)
    playing = SimpleNamespace(id=1, team=TEAM1, connection=object(), score=0)
    other = SimpleNamespace(id=2, team=TEAM2, connection=object(), score=0)
    spectator = SimpleNamespace(id=3, team=TEAM_SPECTATOR, connection=object(), score=0)
    server.players.update({1: playing, 2: other, 3: spectator})
    mode = _PlainMode(server)
    asyncio.run(mode._restart_round())
    assert sorted(respawned) == [1, 2]


# -- 5. retiring modes never end the match during a transition -----------


def test_zombie_rollover_does_not_end_match_or_open_vote(monkeypatch):
    server = zombie_fx._Server()
    server.config.mode_settings["zom"].update({"score_limit": 1, "first_infected": 2})
    for player_id in range(1, 5):
        zombie_fx._player(server, player_id)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    assert mode.phase is ZombiePhase.ACTIVE
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    zombies = [p for p in server.players.values() if p.team == ZOMBIE_TEAM]
    assert len(survivors) == 2 and len(zombies) == 2
    # Detach survivors first: without the retiring flag the second departure
    # left only zombies and ZOMBIE_WIN finished the (final) round.
    votes = _prepare_rollover(server, survivors + zombies, "zom")
    _change_mode(monkeypatch, server)

    assert votes.opened == []
    assert server.teams[ZOMBIE_TEAM].score == 0
    assert mode.rounds_played == 0
    assert mode._end_task is None


def test_vip_rollover_does_not_finish_a_sudden_death_round(monkeypatch):
    server = vip_fx._Server()
    server.config.mode_settings["vip"]["score_limit"] = 1
    mode = VIPMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    blue_a = vip_fx._player(server, 1, TEAM1)
    blue_b = vip_fx._player(server, 3, TEAM1)
    green = vip_fx._player(server, 2, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    blue_vip = mode.vips[TEAM1]
    guard = blue_b if blue_vip is blue_a else blue_a
    blue_vip.alive = blue_vip.spawned = False
    asyncio.run(mode.on_player_death(blue_vip, mode.vips[TEAM2], 0))
    assert mode.respawn_enabled[TEAM1] is False

    votes = _prepare_rollover(server, [guard, blue_vip, green], "vip")
    _change_mode(monkeypatch, server)

    assert votes.opened == []
    assert server.teams[TEAM2].score == 0
    assert mode._round_task is None
    assert mode._end_task is None


def test_arena_rollover_does_not_award_the_round(monkeypatch):
    server = arena_fx._Server({"arena": {"rounds_to_win": 1}})
    blue = arena_fx._player(server, 1, TEAM1)
    green = arena_fx._player(server, 2, TEAM2)
    mode = ArenaMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode._begin_round())
    assert mode.round_started

    votes = _prepare_rollover(server, [blue, green], "arena")
    _change_mode(monkeypatch, server)

    assert votes.opened == []
    assert mode.round_wins == {TEAM1: 0, TEAM2: 0}
    assert mode._end_task is None


def test_same_map_restart_drains_a_deciding_leave_without_a_vote():
    server = zombie_fx._Server()
    server.config.mode_settings["zom"].update({"score_limit": 1, "first_infected": 2})
    for player_id in range(1, 5):
        zombie_fx._player(server, player_id)
    server.respawn_player = lambda player: setattr(player, "alive", True)
    mode = ZombieMode(server)
    server.mode = mode
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    votes = _Votes()
    server.vote_manager = votes
    survivors = [p for p in server.players.values() if p.team == SURVIVOR_TEAM]
    # One survivor already left; its queued leave (the last human) drains
    # inside restart_round.
    server.players.pop(survivors[0].id)
    last = survivors[1]
    server.players.pop(last.id)
    server._mode_events = deque([("on_player_leave", (last,))])
    service = MatchTransitionService(server)

    result = asyncio.run(service.restart_round())

    assert result.ok is True
    assert votes.opened == []
    assert mode._end_task is None
    assert mode.ended is False and mode.retiring is False


def test_retiring_mode_on_mode_end_is_inert():
    server = _scoring_server()
    votes = _Votes()
    server.vote_manager = votes
    mode = _PlainMode(server)
    mode.begin_retirement(end_round=False)
    asyncio.run(mode._end_by_score(TEAM1))
    asyncio.run(mode.on_mode_end(TEAM1))
    assert votes.opened == []
    assert mode._end_task is None
    # A fresh round clears the flag.
    asyncio.run(mode.on_mode_start())
    assert mode.retiring is False and mode.ended is False


# -- 10. end-screen joiner ------------------------------------------------


def _music(rows):
    return [
        PlayMusic(ByteReader(data[1:])).name
        for data in rows if data and data[0] == PlayMusic.id
    ]


def _forced(rows):
    return [
        ForceShowScores(ByteReader(data[1:])).forced
        for data in rows if data and data[0] == ForceShowScores.id
    ]


def test_end_screen_joiner_gets_forced_scores_and_ending_track():
    server = _scoring_server()
    mode = TDMMode(server)
    asyncio.run(mode.on_mode_start())
    mode.ended = True
    mode._end_sequence_running = True
    connection = _Conn()
    mode.reveal_to(connection)
    assert _forced(connection.sent) == [1]
    # The track is started by the world reveal BEFORE the catch-up burst
    # (server._send_join_audio), so the mode reveal itself sends no music.
    assert _music(connection.sent) == []
    assert mode.join_music_track() in audio.GAME_ENDING_TRACKS


def test_final_minute_joiner_gets_the_timeout_track_and_normal_joiner_nothing():
    server = _scoring_server()
    mode = _PlainMode(server)
    asyncio.run(mode.on_mode_start())
    quiet = _Conn()
    mode.reveal_to(quiet)
    assert _forced(quiet.sent) == [] and _music(quiet.sent) == []
    assert mode.join_music_track() is None
    mode._timeout_music_played = True
    late = _Conn()
    mode.reveal_to(late)
    assert _forced(late.sent) == []
    assert mode.join_music_track() in audio.GAME_ENDING_TRACKS


def test_retired_mode_reveals_no_end_screen():
    server = _scoring_server()
    mode = _PlainMode(server)
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.deactivate())
    connection = _Conn()
    mode.reveal_to(connection)
    assert _forced(connection.sent) == []


def test_every_mode_reveal_extends_the_base_reveal():
    """Each subclass reveal_to must call super() so the end-screen replay
    reaches joiners in every ruleset."""
    import inspect

    from modes.ctf import CTFMode
    from modes.demolition import DemolitionMode
    from modes.diamond_mine import DiamondMineMode
    from modes.multi_hill import MultiHillMode
    from modes.occupation import OccupationMode
    from modes.territory_control import TerritoryControlMode

    for mode_class in (
        CTFMode, DemolitionMode, DiamondMineMode, MultiHillMode,
        OccupationMode, TDMMode, TerritoryControlMode, VIPMode, ZombieMode,
    ):
        source = inspect.getsource(mode_class.reveal_to)
        assert "super().reveal_to(connection)" in source, mode_class.__name__
    assert ArenaMode.reveal_to is BaseMode.reveal_to
    assert int(C.TEAM_SPECTATOR) == TEAM_SPECTATOR
