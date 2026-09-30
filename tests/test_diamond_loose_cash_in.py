"""Diamond Mine: a dropped or thrown diamond that rests in a drop-off scores.

Retail sends DIAMOND_CASHED_IN_LOOSE_YOURTEAM / _OPPOSITION for it: no client
binary references the ids, they carry no player name, and DiamondTool's
primary fire throws the diamond. The trigger itself is inferred.
"""

from __future__ import annotations

import asyncio

import shared.constants as C
import shared.constants_gamemode as CG

from server.game_constants import TEAM1, TEAM2
from shared.packet import LocalisedMessage
from tests.test_diamond_mine_fixes import _ids, _mode
from tests.test_recovered_objective_modes import _decode, _Player

IN_ZONE = (150.5, 100.5, 50.5)
OUTSIDE = (120.5, 100.5, 50.5)


def _carrier(server, mode, now, position, team=TEAM1):
    carrier = _Player(1, team, position)
    mate = _Player(2, team, (0.0, 0.0, 50.0))
    enemy = _Player(3, TEAM2 if team == TEAM1 else TEAM1, (0.0, 20.0, 50.0))
    for player in (carrier, mate, enemy):
        player.connection = player
        server.players[player.id] = player
    asyncio.run(mode.on_mode_start())
    mode._spawn_diamond(position, now=now[0])
    # Pick it up without stepping on the drop-off first.
    dropoff = mode.active_dropoffs[0]
    mode.active_dropoffs = []
    asyncio.run(mode.on_tick(1))
    mode.active_dropoffs = [dropoff]
    assert carrier.id in mode.carriers
    for player in (carrier, mate, enemy):
        player.sent.clear()
    return carrier, mate, enemy


def _messages(player):
    return _ids(player)


def test_diamond_dropped_in_a_drop_off_scores_for_the_carriers_team(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, mate, enemy = _carrier(server, mode, now, IN_ZONE)
    score_before = carrier.score

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))

    assert server.teams[TEAM1].score == 1
    assert server.teams[TEAM2].score == 0
    assert not mode.ground_diamonds and not mode.carriers
    assert server.score_updates[-1] == (
        TEAM1, 1, int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON)
    )
    loose = ("DIAMOND_CASHED_IN_LOOSE_YOURTEAM", [])
    assert _messages(carrier) == [loose]
    assert _messages(mate) == [loose]
    assert _messages(enemy) == [("DIAMOND_CASHED_IN_LOOSE_OPPOSITION", [])]
    # The team point only: nobody carried it in (the death itself costs).
    assert carrier.score <= score_before
    assert mode.active_dropoffs[0].remaining == 2


def test_thrown_diamond_scores_where_it_lands(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, enemy = _carrier(server, mode, now, (147.0, 100.5, 50.5))
    # Standing just outside the volume (x 145..155 would be inside; the
    # carrier is not cashed in because the drop-off list was empty above).
    carrier.set_position((143.0, 100.5, 50.5))

    handled = asyncio.run(mode.handle_drop_pickup(
        carrier, (146.0, 100.5, 50.5), (float(C.DIAMOND_THROW_SPEED), 0.0, 0.0)
    ))

    assert handled is True
    assert server.teams[TEAM1].score == 1
    assert _messages(enemy) == [("DIAMOND_CASHED_IN_LOOSE_OPPOSITION", [])]


def test_diamond_dropped_outside_stays_on_the_ground(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, mate, enemy = _carrier(server, mode, now, OUTSIDE)

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))
    asyncio.run(mode.on_tick(2))

    assert server.teams[TEAM1].score == 0
    assert len(mode.ground_diamonds) == 1
    assert next(iter(mode.ground_diamonds.values())).last_team == TEAM1
    assert _messages(mate) == [] and _messages(enemy) == []


def test_mined_diamond_in_a_drop_off_is_not_cashed_in(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    miner = _Player(1, TEAM1, (0.0, 0.0, 50.0))
    server.players[miner.id] = miner

    mode._spawn_diamond(IN_ZONE, now=now[0], uncovered_by=miner)
    asyncio.run(mode.on_tick(1))

    assert server.teams[TEAM1].score == 0
    assert len(mode.ground_diamonds) == 1


def test_drop_off_opening_around_a_resting_diamond_cashes_it_in(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)
    dropoff = mode.active_dropoffs[0]
    mode.active_dropoffs = []

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))
    assert server.teams[TEAM1].score == 0 and len(mode.ground_diamonds) == 1

    mode.active_dropoffs = [dropoff]
    asyncio.run(mode.on_tick(2))

    assert server.teams[TEAM1].score == 1
    assert not mode.ground_diamonds


def test_team_change_credits_the_team_that_carried_it(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)

    carrier.team = TEAM2
    asyncio.run(mode.on_player_team_change(carrier, TEAM1, TEAM2))

    assert server.teams[TEAM1].score == 1
    assert server.teams[TEAM2].score == 0


def test_full_or_foreign_drop_off_does_not_take_a_loose_diamond(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)
    mode.active_dropoffs[0].team = TEAM2

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))

    assert server.teams[TEAM1].score == 0 and server.teams[TEAM2].score == 0
    assert len(mode.ground_diamonds) == 1

    mode.active_dropoffs[0].team = TEAM1
    mode.active_dropoffs[0].remaining = 0
    asyncio.run(mode.on_tick(2))
    assert server.teams[TEAM1].score == 0


def test_loose_cash_in_can_win_the_round(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)
    server.teams[TEAM1].score = mode.score_limit - 1
    ended = []

    async def end_by_score(team):
        ended.append(team)
        mode.ended = True

    monkeypatch.setattr(mode, "_end_by_score", end_by_score)
    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))

    assert ended == [TEAM1]


def test_loose_cash_in_can_be_switched_off(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now, settings={
        "score_limit": 5, "max_active_bases": 1, "max_active_diamonds": 2,
        "loose_cash_in": False,
    })
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))
    asyncio.run(mode.on_tick(2))

    assert server.teams[TEAM1].score == 0
    assert len(mode.ground_diamonds) == 1


def test_exhausted_drop_off_rotates_after_a_loose_cash_in(monkeypatch):
    now = [100.0]
    server, mode = _mode(monkeypatch, now)
    carrier, _mate, _enemy = _carrier(server, mode, now, IN_ZONE)
    mode.active_dropoffs[0].remaining = 1
    rotated = []
    monkeypatch.setattr(mode, "_rotate_dropoff", rotated.append)

    asyncio.run(mode.on_player_death(carrier, None, int(C.KILL.FALL_KILL)))

    assert len(rotated) == 1
    assert [m.string_id for m in _decode(server.packets, LocalisedMessage)] == []
    assert float(CG.DIA_INDIVIDUAL_SCORE_FOR_CASHED_IN_DIAMOND) > 0
