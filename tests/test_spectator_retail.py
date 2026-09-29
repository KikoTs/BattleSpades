"""Retail spectator admission and spectator -> team joins.

Live 2026-09-26 (stock dev client, console 32918): a team-0 client sends
NewPlayerConnection(15, team 0) and ClientInMenu(110, 0) and then only
ClockSync(0) -- never ClientData(4). Leaving the spectator team re-sends
SetClassLoadout(13) + NewPlayerConnection(15, team 2/3), not ChangeTeam(77).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import shared.constants as C
from protocol.handler_registry import HANDLERS
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.handlers import team as team_handlers
from server.player import Player
from server.team import Team


class _Conn:
    def __init__(self, *, in_game=False):
        self.server = None
        self.player = None
        self.in_game = in_game
        self.in_menu = True
        self.sent = []
        self.menu_notes = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))

    def note_scene_transition_menu(self, in_menu):
        self.menu_notes.append(bool(in_menu))


def _server(reveal_results=(True,)):
    results = list(reveal_results)
    server = SimpleNamespace(
        config=ServerConfig(),
        teams={
            TEAM1: Team(TEAM1, "TEAM1_COLOR", (0, 0, 255)),
            TEAM2: Team(TEAM2, "TEAM2_COLOR", (0, 255, 0)),
        },
        players={},
        connections={},
        broadcasts=[],
        events=[],
        reveals=[],
        pruned=[],
        mode=SimpleNamespace(),
        world_manager=None,
    )

    def reveal(connection):
        server.reveals.append(connection)
        return results.pop(0) if results else True

    server.reveal_world_to = reveal
    server._prune_map_mutations = lambda: server.pruned.append(True)
    server.broadcast = lambda data, **_k: server.broadcasts.append(bytes(data))
    server.queue_mode_event = lambda name, *args: server.events.append((name, args))
    server.round_lifecycle = SimpleNamespace(remove_owned_deployables=lambda p: None)
    return server


def _player(server, pid, team, *, in_game=False, alive=False):
    connection = _Conn(in_game=in_game)
    player = Player(pid, f"P{pid}", team, C.RIFLE_TOOL, connection)
    connection.player = player
    player.alive = alive
    player.spawned = alive
    player.death_time = 0.0
    server.players[pid] = player
    server.connections[pid] = connection
    if team in server.teams:
        server.teams[team].add_player(player)
    return player


def _menu(server, player, in_menu):
    asyncio.run(team_handlers.handle_client_in_menu(
        server, player, SimpleNamespace(in_menu=int(in_menu))
    ))


def test_spectator_gamescene_is_admitted_on_client_in_menu_zero():
    server = _server()
    spectator = _player(server, 1, TEAM_SPECTATOR)

    _menu(server, spectator, 0)

    assert server.reveals == [spectator.connection]
    assert spectator.connection.in_game is True
    assert server.pruned == [True]
    assert spectator.connection.menu_notes == [False]


def test_admission_happens_once_and_only_for_the_settled_scene():
    server = _server()
    spectator = _player(server, 1, TEAM_SPECTATOR)

    _menu(server, spectator, 1)  # still in a menu: not a GameScene yet
    assert server.reveals == []
    _menu(server, spectator, 0)
    _menu(server, spectator, 0)
    assert server.reveals == [spectator.connection]


def test_team_players_still_wait_for_their_first_client_data():
    server = _server()
    player = _player(server, 1, TEAM1)

    _menu(server, player, 0)

    assert server.reveals == []
    assert player.connection.in_game is False


def test_incomplete_reveal_retries_until_admitted():
    server = _server(reveal_results=(False, False, True))
    spectator = _player(server, 1, TEAM_SPECTATOR)

    async def run():
        await team_handlers.handle_client_in_menu(
            server, spectator, SimpleNamespace(in_menu=0)
        )
        assert spectator.connection.in_game is False
        for _ in range(40):
            if spectator.connection.in_game:
                break
            await asyncio.sleep(team_handlers.SPECTATOR_REVEAL_RETRY_SECONDS)

    asyncio.run(run())
    assert len(server.reveals) == 3
    assert spectator.connection.in_game is True


def test_retry_stops_when_the_spectator_left():
    server = _server(reveal_results=(False, True))
    spectator = _player(server, 1, TEAM_SPECTATOR)

    async def run():
        await team_handlers.handle_client_in_menu(
            server, spectator, SimpleNamespace(in_menu=0)
        )
        del server.players[1]
        await asyncio.sleep(team_handlers.SPECTATOR_REVEAL_RETRY_SECONDS * 4)

    asyncio.run(run())
    assert len(server.reveals) == 1
    assert spectator.connection.in_game is False


def test_new_player_connection_from_a_spectator_joins_the_team():
    assert HANDLERS[15] is team_handlers.handle_spectator_rejoin
    server = _server()
    spectator = _player(server, 1, TEAM_SPECTATOR, in_game=True)
    before = time.time()

    asyncio.run(team_handlers.handle_spectator_rejoin(
        server, spectator, SimpleNamespace(team=TEAM2, class_id=0)
    ))

    assert spectator.team == TEAM2
    assert spectator.death_time >= before  # one ordinary respawn is armed
    assert ("on_player_team_change", (spectator, TEAM_SPECTATOR, TEAM2)) in server.events


def test_duplicate_new_player_connection_from_a_team_player_is_ignored():
    server = _server()
    player = _player(server, 1, TEAM1, in_game=True, alive=True)

    asyncio.run(team_handlers.handle_spectator_rejoin(
        server, player, SimpleNamespace(team=TEAM2, class_id=0)
    ))

    assert player.team == TEAM1
    assert server.events == []


def test_spectator_rejoin_ignores_invalid_or_spectator_team():
    server = _server()
    spectator = _player(server, 1, TEAM_SPECTATOR, in_game=True)

    for wire in (TEAM_SPECTATOR, int(C.TEAM_NEUTRAL), 99):
        asyncio.run(team_handlers.handle_spectator_rejoin(
            server, spectator, SimpleNamespace(team=wire, class_id=0)
        ))

    assert spectator.team == TEAM_SPECTATOR
    assert server.events == []


def test_a_spectator_joining_does_not_make_a_bot_leave():
    import asyncio
    from types import SimpleNamespace

    from server.bot_ai.director import BotDirector
    from server.config import ServerConfig
    from server.main import BattleSpadesServer
    from server.player import Player

    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        server.config.max_players = 24
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        director._config.population_mode = "backfill"
        director._config.fill_target = 6
        director._config.max_bots = 6
        director._next_population_at = 0.0
        await director._maintain_population(1.0)
        before = len(director.bots)
        spectator = Player(40, "Watcher", 0, 0, None)
        server.players[spectator.id] = spectator
        director._next_population_at = 0.0
        await director._maintain_population(2.0)
        assert len(director.bots) == before

    asyncio.run(scenario())
