"""Demolition regressions: personal destroy/repair awards, timeout result
cue, airstrike landing delay, and fair simultaneous destruction."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from modes import demolition as demolition_module
from modes.demolition import AIRSTRIKE_IMPACT_DELAY, DemolitionMode
from server.game_constants import TEAM1, TEAM2
from shared.packet import LocalisedMessage, SetScore
from tests.test_demolition import _decode, _Server


def _active_mode(monkeypatch, now):
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = _Server()
    mode = DemolitionMode(server)

    async def _skip(_winner):
        return None

    mode._run_end_sequence = _skip
    asyncio.run(mode.on_mode_start())
    now[0] += 31.0
    asyncio.run(mode.on_tick(1))
    assert mode.phase == "active"
    return server, mode


def _player(player_id, team, server=None):
    player = SimpleNamespace(id=player_id, team=team, score=0, name=f"P{player_id}")
    if server is not None:
        # Block credit goes only to bodies that still own their roster slot.
        server.players[player_id] = player
    return player


def _ids(server):
    return [m.string_id for m in _decode(server.packets, LocalisedMessage)]


def test_destroying_enemy_objective_blocks_awards_per_interval(monkeypatch) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    monkeypatch.setattr(CG, "DEM_SCORE_DESTROY_INTERVAL", 2)
    blue = _player(1, TEAM1, server)
    world = server.world_manager

    world.mutate((40, 50, 30), False)
    asyncio.run(mode.on_blocks_destroyed(blue, ((40, 50, 30),), True))
    assert blue.score == 0  # 1 of 2 blocks

    world.mutate((41, 50, 30), False)
    asyncio.run(mode.on_blocks_destroyed(blue, ((41, 50, 30),), True))
    assert blue.score == int(CG.DEM_SCORE_DESTROY_SCORE)
    reasons = [p.reason for p in _decode(server.packets, SetScore)]
    assert int(C.SCORE_REASON.DEM_DESTROY_SCORE_REASON) in reasons


def test_own_base_blocks_do_not_count_as_destruction(monkeypatch) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    monkeypatch.setattr(CG, "DEM_SCORE_DESTROY_INTERVAL", 1)
    blue = _player(1, TEAM1)
    server.world_manager.mutate((10, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(blue, ((10, 20, 30),), True))
    assert blue.score == 0


def test_repairing_enemy_damage_awards_but_dig_and_refill_does_not(
    monkeypatch,
) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    monkeypatch.setattr(CG, "DEM_SCORE_REPAIR_INTERVAL", 1)
    blue = _player(1, TEAM1, server)
    green = _player(2, TEAM2, server)
    world = server.world_manager

    # Green knocks a hole in Blue's base; Blue repairs it: rewarded.
    world.mutate((10, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(green, ((10, 20, 30),), True))
    world.mutate((10, 20, 30), True)
    asyncio.run(mode.on_blocks_built(blue, ((10, 20, 30),)))
    assert blue.score == int(CG.DEM_SCORE_REPAIR_SCORE)

    # Blue digs its own objective and refills it: no score farming.
    world.mutate((11, 20, 30), False)
    asyncio.run(mode.on_blocks_destroyed(blue, ((11, 20, 30),), True))
    world.mutate((11, 20, 30), True)
    asyncio.run(mode.on_blocks_built(blue, ((11, 20, 30),)))
    assert blue.score == int(CG.DEM_SCORE_REPAIR_SCORE)


def test_build_phase_placement_is_not_a_repair(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    monkeypatch.setattr(CG, "DEM_SCORE_REPAIR_INTERVAL", 1)
    server = _Server()
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())
    assert mode.phase == "building"
    blue = _player(1, TEAM1)
    mode.objective_cells[TEAM1] = {(10, 20, 30)}
    asyncio.run(mode.on_blocks_built(blue, ((10, 20, 30),)))
    assert blue.score == 0


def test_timeout_sends_retail_result_cue(monkeypatch) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    server.world_manager.mutate((40, 50, 30), False)
    asyncio.run(mode._end_by_time())
    assert mode.winner == TEAM1
    assert _ids(server)[-1] == "TEAM_DEFEAT"

    now2 = [100.0]
    server2, mode2 = _active_mode(monkeypatch, now2)
    asyncio.run(mode2._end_by_time())
    assert mode2.winner is None
    assert _ids(server2)[-1] == "GAME_DRAWN"


def test_airstrike_lands_before_the_match_ends(monkeypatch) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    strikes = []
    monkeypatch.setattr(
        demolition_module,
        "trigger_airstrike",
        lambda _server, position: strikes.append(tuple(position)) or 5,
    )
    server.world_manager.mutate((40, 50, 30), False)
    server.world_manager.mutate((41, 50, 30), False)
    asyncio.run(mode.on_tick(2))
    assert mode.phase == "airstrike"

    now[0] += float(CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE)
    asyncio.run(mode.on_tick(3))
    assert strikes == [tuple(mode.base_zones[TEAM2].center)]
    assert not mode.ended
    assert server.teams[TEAM1].score == 1

    asyncio.run(mode.on_tick(4))
    assert len(strikes) == 1  # launched exactly once
    assert not mode.ended

    now[0] += AIRSTRIKE_IMPACT_DELAY
    asyncio.run(mode.on_tick(5))
    assert mode.ended and mode.winner == TEAM1
    assert _ids(server)[-1] == "TEAM_DEFEAT"


def test_both_bases_destroyed_in_one_tick_is_a_draw(monkeypatch) -> None:
    now = [100.0]
    server, mode = _active_mode(monkeypatch, now)
    strikes = []
    monkeypatch.setattr(
        demolition_module,
        "trigger_airstrike",
        lambda _server, position: strikes.append(tuple(position)) or 5,
    )
    for cell in ((10, 20, 30), (11, 20, 30), (40, 50, 30), (41, 50, 30)):
        server.world_manager.mutate(cell, False)
    asyncio.run(mode.on_tick(2))
    assert mode._destroyed_teams == (TEAM1, TEAM2)

    now[0] += float(CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE)
    asyncio.run(mode.on_tick(3))
    assert len(strikes) == 2
    assert server.teams[TEAM1].score == 0 and server.teams[TEAM2].score == 0
    now[0] += AIRSTRIKE_IMPACT_DELAY
    asyncio.run(mode.on_tick(4))
    assert mode.ended and mode.winner is None
    assert _ids(server)[-1] == "GAME_DRAWN"


def test_deactivate_during_build_phase_releases_zone_locks(monkeypatch) -> None:
    from shared.packet import LockToZone
    from tests.test_demolition import _Connection

    now = [100.0]
    monkeypatch.setattr("modes.demolition.time.time", lambda: now[0])
    server = _Server()
    connection = _Connection()
    player = SimpleNamespace(id=1, team=TEAM1, connection=connection)
    connection.player = player
    server.players[player.id] = player
    mode = DemolitionMode(server)
    asyncio.run(mode.on_mode_start())
    assert mode.phase == "building"

    asyncio.run(mode.deactivate())

    lock = _decode(connection.sent, LockToZone)[-1]
    assert (
        lock.A2018, lock.A2019, lock.A2020,
        lock.A2021, lock.A2022, lock.A2023,
    ) == mode._world_zone().bounds
