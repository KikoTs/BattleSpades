"""Rollover carries team/class-select peers instead of kicking them.

Regression for logs/soak/main-20260926: a client that had loaded the map but
not picked a team (no ClientData -> ``in_game`` False) was disconnected with
ERROR_MATCH_ENDED by the next map/mode rollover.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.connection import Connection
from server.match import MatchTransitionService, _carries_over_scene
from tests.test_match_transitions import _Connection, _NewMode, _Server


class _PregameConnection(_Connection):
    """Loader handshake done (StateData sent) but still on team select."""

    def __init__(self) -> None:
        super().__init__()
        self.in_game = False
        self.map_sent = True
        self.state_sent = True
        self.sent: list[bytes] = []

    def send(self, data: bytes, reliable: bool = True, prefix: int = 0x30) -> None:
        self.sent.append(bytes(data))


def _service(monkeypatch, server):
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    return service


def test_classification_separates_mid_mapsync_from_team_select() -> None:
    assert _carries_over_scene(SimpleNamespace(in_game=True))
    assert _carries_over_scene(
        SimpleNamespace(in_game=False, map_sent=True, state_sent=True)
    )
    # Mid InitialInfo/MapSync: VXL still streaming or StateData not yet sent.
    assert not _carries_over_scene(
        SimpleNamespace(in_game=False, map_sent=False, state_sent=False)
    )
    assert not _carries_over_scene(
        SimpleNamespace(in_game=False, map_sent=True, state_sent=False)
    )


def test_team_select_peer_is_reloaded_not_kicked(monkeypatch) -> None:
    server = _Server()
    pregame = _PregameConnection()
    server.connections[object()] = pregame
    service = _service(monkeypatch, server)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert result.reconnect_required is False
    assert pregame.disconnect_reasons == []
    assert pregame.transition_arms == 1
    assert pregame.reload_calls == 1
    # broadcast() is in_game-gated; the pre-game peer gets MapEnded directly,
    # preceded by the silent OpenAL-error flush (PlaySound 23).
    assert [packet[0] for packet in pregame.sent] == [23, 52]
    assert all(c.reload_calls == 1 for c in server.connections.values())


def test_mode_change_also_carries_team_select_peer(monkeypatch) -> None:
    server = _Server()
    pregame = _PregameConnection()
    server.connections[object()] = pregame
    service = _service(monkeypatch, server)

    result = asyncio.run(service.change_mode("tdm"))

    assert result.ok is True
    assert pregame.disconnect_reasons == []
    assert pregame.reload_calls == 1


def test_team_select_peer_without_loader_ack_is_retired(monkeypatch) -> None:
    server = _Server()
    pregame = _PregameConnection()
    pregame.transition_ready = False
    server.connections[object()] = pregame
    service = _service(monkeypatch, server)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert result.reconnect_required is True
    assert pregame.reload_calls == 0
    assert pregame.disconnect_reasons == [18]


def test_mid_mapsync_peer_still_retired_and_gets_no_mapended(monkeypatch) -> None:
    server = _Server()
    loading = _PregameConnection()
    loading.state_sent = False
    server.connections[object()] = loading
    service = _service(monkeypatch, server)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.reconnect_required is True
    assert loading.sent == []
    assert loading.reload_calls == 0
    assert loading.disconnect_reasons == [18]


def _bare_connection() -> Connection:
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=[]),
        reserved_player_ids=set(),
    )
    peer = SimpleNamespace(address="127.0.0.1:1")
    return Connection(peer, server)


def test_new_player_connection_ignored_once_scene_transition_armed() -> None:
    connection = _bare_connection()
    connection.map_sent = True
    connection.state_sent = True
    calls: list[object] = []

    async def fake_join(packet) -> None:
        calls.append(packet)

    connection._on_new_player = fake_join

    async def scenario() -> None:
        connection.arm_scene_transition()
        # NewPlayerConnection(15): the client picked a team as MapEnded left.
        await connection.handle_pre_join_packet(bytes([15]) + bytes(64))

    asyncio.run(scenario())
    assert calls == []


def test_arming_transition_invalidates_in_flight_join_epoch() -> None:
    connection = _bare_connection()
    before = connection._map_sync_generation

    async def scenario() -> None:
        connection.arm_scene_transition()

    asyncio.run(scenario())
    assert connection._map_sync_generation != before
