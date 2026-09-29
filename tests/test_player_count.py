"""Every public player count (A2S, LAN, Steam, Revival) agrees on occupancy."""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace

import pytest

from server.a2s_query import A2SHandler
from server.config import ServerConfig
from server.revival_master import RevivalMasterService
import server.revival_master as revival_master_module
from server.steam_master import SteamMasterService, server_population
from scripts.check_steam_registration import parse_a2s_info


def _player(pid, *, bot=False, name=None, score=0, kills=0):
    return SimpleNamespace(
        id=pid, is_bot=bot, name=name or f"P{pid}", score=score, kills=kills,
        team=0,
    )


def _connection(player=None, reserved=None, transition=None):
    return SimpleNamespace(
        player=player,
        reserved_player_id=reserved,
        _scene_transition_ready=transition,
    )


def _server(max_players=12):
    config = ServerConfig(name="Count", default_mode="tdm", max_players=max_players)
    return SimpleNamespace(
        config=config,
        players={},
        connections={},
        teams={},
        world_manager=SimpleNamespace(map_name="ArcticBase"),
        mode=None,
    )


def _join(server, player):
    server.players[player.id] = player
    if not player.is_bot:
        server.connections[object()] = _connection(player, reserved=player.id)


def test_roster_humans_and_bots_follow_source_convention():
    server = _server()
    _join(server, _player(0))
    _join(server, _player(1, bot=True))
    _join(server, _player(2, bot=True))

    population = server_population(server)

    assert (population.players, population.humans, population.bots) == (3, 1, 2)
    assert population.max_players == 12


def test_loading_player_with_reserved_slot_counts_as_human():
    server = _server()
    _join(server, _player(0))
    server.connections[object()] = _connection(reserved=1)
    # A peer that has not been given a slot yet (rejected/handshaking) does not.
    server.connections[object()] = _connection()

    population = server_population(server)

    assert (population.players, population.humans, population.bots) == (2, 2, 0)


def test_humans_stay_counted_while_a_map_change_reloads_their_scene():
    server = _server()
    bot = _player(3, bot=True)
    _join(server, bot)
    # The transition detached the Player and cleared the reservation, but
    # the peer is still connected and about to rejoin the new map.
    server.connections[object()] = _connection(transition=asyncio.Event())

    population = server_population(server)

    assert (population.players, population.humans, population.bots) == (2, 1, 1)


def test_departed_player_is_no_longer_counted():
    server = _server()
    human = _player(0)
    _join(server, human)
    assert server_population(server).players == 1

    # _on_disconnect_sync pops the connection and the roster entry.
    server.connections.clear()
    server.players.pop(0)

    assert server_population(server).players == 0


def test_counts_never_exceed_capacity_and_humans_win_over_bots():
    server = _server(max_players=4)
    for pid in range(3):
        _join(server, _player(pid, bot=True))
    server.connections[object()] = _connection(reserved=3)
    server.connections[object()] = _connection(reserved=4)

    population = server_population(server)

    assert population.players == 4
    assert population.humans == 2
    assert population.bots == 2
    assert population.humans + population.bots == population.players


def test_a2s_info_lan_steam_and_revival_publish_the_same_population(monkeypatch):
    server = _server()
    _join(server, _player(0))
    _join(server, _player(1, bot=True))
    server.connections[object()] = _connection(reserved=2)

    info = parse_a2s_info(A2SHandler(server)._make_info_response())
    assert (info["players"], info["bots"], info["max_players"]) == (3, 1, 12)

    import json
    lan = json.loads(A2SHandler(server)._make_lan_info())
    assert (lan["players_current"], lan["players_max"]) == (3, 12)

    snapshot = SteamMasterService(server).snapshot()
    assert (snapshot.player_count, snapshot.bot_count) == (3, 1)

    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    server.config.revival.public_host = "127.0.0.1"
    payload = RevivalMasterService(server).heartbeat_payload()
    assert payload["players"] == 3
    assert payload["human_players"] == 2
    assert payload["bots"] == 1
    assert payload["max_players"] == 12


def _decode_players(data: bytes):
    assert data[:5] == b"\xff\xff\xff\xffD"
    count, pos, rows = data[5], 6, []
    for _ in range(count):
        pos += 1
        end = data.index(b"\0", pos)
        name = data[pos:end].decode()
        score, duration = struct.unpack("<if", data[end + 1:end + 9])
        pos = end + 9
        rows.append((name, score, duration))
    return rows


def test_a2s_player_rows_carry_scoreboard_score_and_growing_duration(monkeypatch):
    server = _server()
    _join(server, _player(4, name="Late", score=7, kills=2))
    _join(server, _player(1, bot=True, name="Bot", score=-1))
    handler = A2SHandler(server)
    clock = [100.0]
    monkeypatch.setattr("server.a2s_query.time.monotonic", lambda: clock[0])

    first = _decode_players(handler._make_player_response())
    clock[0] = 130.0
    second = _decode_players(handler._make_player_response())

    # Ordered by player id; SCORE column (not kills), signed.
    assert [(name, score) for name, score, _ in second] == [("Bot", -1), ("Late", 7)]
    assert [duration for *_, duration in first] == [0.0, 0.0]
    assert [duration for *_, duration in second] == [30.0, 30.0]

    # A reused id starts a fresh duration.
    server.players[4] = _player(4, name="Newcomer")
    third = _decode_players(handler._make_player_response())
    assert third[1] == ("Newcomer", 0, 0.0)


def test_a2s_rules_report_the_live_map():
    server = _server()
    server.config.default_map = "ConfiguredFirstMap"
    data = A2SHandler(server)._make_rules_response()
    assert b"map\0ArcticBase\0" in data


def test_revival_heartbeat_follows_population_changes_promptly(monkeypatch):
    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    monkeypatch.setattr(revival_master_module, "POPULATION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        revival_master_module, "POPULATION_HEARTBEAT_MIN_GAP_SECONDS", 0.05
    )
    server = _server()
    server.config.revival.heartbeat_interval_seconds = 60.0
    service = RevivalMasterService(server)
    sent = []

    async def fake_post(path, payload):
        sent.append(payload["players"])
        return 200, {"accepted": True}

    service._post = fake_post

    async def scenario():
        await service.publish_heartbeat()
        task = asyncio.create_task(service._heartbeat_loop())
        await asyncio.sleep(0.2)
        idle = len(sent)
        _join(server, _player(0))
        await asyncio.sleep(0.2)
        service._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return idle

    idle = asyncio.run(scenario())

    # Nothing changed: no extra heartbeat before the 60 s interval.
    assert idle == 1
    # The join was published without waiting for the interval.
    assert sent[-1] == 1
    assert len(sent) == 2
