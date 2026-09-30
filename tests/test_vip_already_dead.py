"""VIP_ALREADY_DEAD: "your team's V.I.P. is already dead".

No client binary references the id, so the retail server sent it. The
trigger is not recorded; the server sends it to a player who arrives on a
team whose VIP has died and therefore stays dead until the next sub-round.
"""

import asyncio

from modes.vip import VIPPhase
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from shared.bytes import ByteReader
from shared.packet import LocalisedMessage
from tests.test_vip import _Connection, _new_mode, _player


def _lines(connection):
    return [
        LocalisedMessage(ByteReader(data[1:])).string_id
        for data in connection.sent
        if data and data[0] == LocalisedMessage.id
    ]


def _round_with_dead_blue_vip():
    server, mode = _new_mode()
    _player(server, 1, TEAM1)
    _player(server, 3, TEAM1)
    _player(server, 2, TEAM2)
    _player(server, 4, TEAM2)
    asyncio.run(mode.on_tick(1))
    assert mode.phase is VIPPhase.ACTIVE
    vip = mode.vips[TEAM1]
    vip.die()
    asyncio.run(mode.on_player_death(vip, None, 0))
    assert mode.vip_alive[TEAM1] is False
    assert mode.respawn_enabled[TEAM1] is False
    return server, mode


def _joiner(server, team, *, alive=False):
    player = _player(server, 9, team, alive=alive)
    player.connection.player = player
    player.connection.in_game = False  # reveal_to runs before it turns true
    player.connection.sent.clear()
    return player


def test_joiner_on_the_vipless_team_is_told_why_it_stays_dead():
    server, mode = _round_with_dead_blue_vip()
    joiner = _joiner(server, TEAM1)
    assert mode.can_player_respawn(joiner) is False

    mode.reveal_to(joiner.connection)

    assert _lines(joiner.connection) == ["VIP_ALREADY_DEAD"]
    # Nobody else hears it.
    assert all(
        "VIP_ALREADY_DEAD" not in _lines(other.connection)
        for other in server.players.values() if other is not joiner
    )


def test_joiner_on_the_team_with_a_live_vip_hears_nothing():
    server, mode = _round_with_dead_blue_vip()
    joiner = _joiner(server, TEAM2)

    mode.reveal_to(joiner.connection)

    assert _lines(joiner.connection) == []


def test_no_line_before_the_vips_are_chosen_or_after_the_match():
    server, mode = _new_mode()
    joiner = _joiner(server, TEAM1)
    assert mode.phase is not VIPPhase.ACTIVE
    mode.reveal_to(joiner.connection)
    assert _lines(joiner.connection) == []

    server, mode = _round_with_dead_blue_vip()
    joiner = _joiner(server, TEAM1)
    mode.ended = True
    mode.reveal_to(joiner.connection)
    assert _lines(joiner.connection) == []


def test_no_line_when_sudden_death_is_off_and_the_team_still_respawns():
    server, mode = _round_with_dead_blue_vip()
    mode.respawn_enabled[TEAM1] = True
    joiner = _joiner(server, TEAM1)

    mode.reveal_to(joiner.connection)

    assert _lines(joiner.connection) == []


def test_connection_without_a_player_is_ignored():
    _server, mode = _round_with_dead_blue_vip()
    bare = _Connection()

    mode.reveal_to(bare)

    assert _lines(bare) == []


def test_spectator_stepping_onto_the_vipless_team_is_told():
    server, mode = _round_with_dead_blue_vip()
    player = _joiner(server, TEAM1)
    player.connection.in_game = True

    asyncio.run(mode.on_player_team_change(player, TEAM_SPECTATOR, TEAM1))

    assert _lines(player.connection) == ["VIP_ALREADY_DEAD"]


def test_survivor_of_the_vipless_team_is_not_told():
    server, mode = _round_with_dead_blue_vip()
    survivor = next(
        player for player in server.players.values()
        if player.team == TEAM1 and player.alive
    )
    survivor.connection.player = survivor
    survivor.connection.sent.clear()

    mode.reveal_to(survivor.connection)

    assert _lines(survivor.connection) == []
