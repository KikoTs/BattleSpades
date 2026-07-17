"""Crash-safe administrative match transition tests."""

from __future__ import annotations

import asyncio
from collections import deque
import time
from types import SimpleNamespace

from server.match import MatchTransitionService


class _Connection:
    def __init__(self) -> None:
        self.in_game = True
        self.player = None
        self.reload_calls = 0
        self.reload_result = True
        self.disconnect_reasons: list[int] = []

    async def reload_scene(self) -> bool:
        self.reload_calls += 1
        return self.reload_result

    def disconnect(self, reason: int = 0) -> None:
        self.disconnect_reasons.append(reason)


class _Mode:
    name = "Old Mode"

    def __init__(self) -> None:
        self.restart_calls = 0
        self.cancel_calls = 0
        self.deactivate_calls = 0

    async def _restart_round(self) -> None:
        self.restart_calls += 1

    async def cancel_end_sequence(self) -> None:
        self.cancel_calls += 1

    async def deactivate(self) -> None:
        self.deactivate_calls += 1


class _NewMode:
    name = "New Mode"
    starts = 0

    def __init__(self, server) -> None:
        self.server = server

    async def on_mode_start(self) -> None:
        # No new-mode gameplay packet may reach the old GameScene.
        assert all(not conn.in_game for conn in self.server.connections.values())
        type(self).starts += 1


class _Server:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            default_map="London",
            default_mode="ctf",
            maps_path="maps",
            transition_grace_seconds=0.0,
        )
        self.mode = _Mode()
        self.world_manager = SimpleNamespace(map_name="London")
        self.fog_color_override = (1, 2, 3)
        self.connections = {object(): _Connection(), object(): _Connection()}
        self.players = {}
        self.teams = {
            0: SimpleNamespace(reset=lambda: None),
            1: SimpleNamespace(reset=lambda: None),
        }
        self._pending_ingame_packets = deque([(object(), b"old")])
        self._mode_events = deque([("old_event", ())])
        self._map_mutation_journal = deque([(1, b"old")])
        self._map_mutation_sequence = 7
        self.runtime_resets = 0
        self.terrain_repair = SimpleNamespace(reset=lambda: None)
        self.host = SimpleNamespace(flush=lambda: None)
        self.broadcast_packets: list[bytes] = []

    def reset_round_runtime(self) -> None:
        self.runtime_resets += 1

    def broadcast(self, data: bytes, **_kwargs) -> None:
        self.broadcast_packets.append(bytes(data))


def test_restart_is_serialized_and_cancels_delayed_end_sequence() -> None:
    async def scenario() -> tuple[_Server, list[bool]]:
        server = _Server()
        service = MatchTransitionService(server)
        states: list[bool] = []

        original = server.mode._restart_round

        async def observed_restart() -> None:
            states.append(service.in_progress)
            await asyncio.sleep(0.02)
            await original()

        server.mode._restart_round = observed_restart
        first, second = await asyncio.gather(
            service.restart_round(), service.restart_round()
        )
        assert sum(result.ok for result in (first, second)) == 1
        return server, states

    server, states = asyncio.run(scenario())
    assert states == [True]
    assert server.mode.restart_calls == 1
    assert server.mode.cancel_calls == 1
    assert all(conn.disconnect_reasons == [] for conn in server.connections.values())


def test_mode_change_gates_old_scene_and_reloads_the_retained_peers(
    monkeypatch,
) -> None:
    _NewMode.starts = 0
    server = _Server()
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="London", config=None)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(
        service,
        "_load_world_candidate",
        lambda map_name, mode_name: (
            candidate
            if (map_name, mode_name) == ("London", "tdm")
            else None
        ),
    )

    result = asyncio.run(service.change_mode("tdm"))

    assert result.ok is True
    assert result.reconnect_required is False
    assert server.config.default_mode == "tdm"
    assert server.world_manager is candidate
    assert isinstance(server.mode, _NewMode)
    assert _NewMode.starts == 1
    assert server.runtime_resets == 1
    assert list(server._pending_ingame_packets) == []
    assert list(server._mode_events) == []
    assert list(server._map_mutation_journal) == []
    assert server._map_mutation_sequence == 0
    assert all(conn.in_game is False for conn in server.connections.values())
    assert 52 in [packet[0] for packet in server.broadcast_packets if packet]
    assert all(conn.reload_calls == 1 for conn in server.connections.values())
    assert all(conn.disconnect_reasons == [] for conn in server.connections.values())


def test_map_change_prepares_world_before_gating_clients(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin")
    prepared_while_live: list[bool] = []

    def load_candidate(map_name: str, mode_name: str):
        prepared_while_live.append(
            all(conn.in_game for conn in server.connections.values())
        )
        assert (map_name, mode_name) == ("HallwayPin", "ctf")
        return candidate

    monkeypatch.setattr(service, "_load_world_candidate", load_candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert prepared_while_live == [True]
    assert server.world_manager is candidate
    assert server.config.default_map == "HallwayPin"
    assert server.fog_color_override is None
    assert 52 in [packet[0] for packet in server.broadcast_packets if packet]
    assert all(conn.reload_calls == 1 for conn in server.connections.values())
    assert all(conn.disconnect_reasons == [] for conn in server.connections.values())


def test_scene_transition_holds_reload_until_after_mapended_grace(
    monkeypatch,
) -> None:
    server = _Server()
    server.config.transition_grace_seconds = 0.75
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    sleeps: list[float] = []

    async def observed_sleep(seconds: float) -> None:
        assert 52 in [
            packet[0] for packet in server.broadcast_packets if packet
        ]
        assert all(connection.reload_calls == 0 for connection in server.connections.values())
        sleeps.append(seconds)

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert sleeps == [0.75]
    assert all(connection.reload_calls == 1 for connection in server.connections.values())
    assert all(connection.disconnect_reasons == [] for connection in server.connections.values())


def test_voted_map_preflights_then_holds_scores_before_mapended(
    monkeypatch,
) -> None:
    server = _Server()
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    observations: list[tuple[float, list[int]]] = []

    async def observed_sleep(seconds: float) -> None:
        packet_ids = [packet[0] for packet in server.broadcast_packets if packet]
        observations.append((seconds, packet_ids))
        assert 53 in packet_ids
        assert 52 not in packet_ids
        assert all(connection.reload_calls == 0 for connection in server.connections.values())

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    result = asyncio.run(
        service.change_map_after_end_screen(
            "HallwayPin",
            end_screen_seconds=9.5,
        )
    )

    packet_ids = [packet[0] for packet in server.broadcast_packets if packet]
    assert result.ok is True
    assert len(observations) == 1
    assert observations[0][0] == 9.5
    assert packet_ids.index(53) < packet_ids.index(52)
    assert all(connection.reload_calls == 1 for connection in server.connections.values())


def test_invalid_voted_map_never_opens_terminal_scores(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)

    def fail_load(_map_name: str, _mode_name: str):
        raise ValueError("Map not found: Missing")

    monkeypatch.setattr(service, "_load_world_candidate", fail_load)

    result = asyncio.run(
        service.change_map_after_end_screen(
            "Missing",
            end_screen_seconds=12.0,
        )
    )

    assert result.ok is False
    assert [packet[0] for packet in server.broadcast_packets if packet] == []
    assert all(connection.in_game for connection in server.connections.values())


def test_custom_map_without_stock_screenshot_skips_packet_53(monkeypatch) -> None:
    server = _Server()
    server.config.default_map = "Training"
    server.world_manager.map_name = "Training"
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    sleeps: list[float] = []

    async def observed_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    result = asyncio.run(
        service.change_map_after_end_screen(
            "HallwayPin",
            end_screen_seconds=7.0,
        )
    )

    packet_ids = [packet[0] for packet in server.broadcast_packets if packet]
    assert result.ok is True
    assert sleeps == [7.0]
    assert 53 not in packet_ids
    assert 52 in packet_ids


def test_player_joining_during_end_screen_is_included_in_rollover(
    monkeypatch,
) -> None:
    server = _Server()
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    late_connection = _Connection()

    async def join_during_dwell(_seconds: float) -> None:
        server.connections[object()] = late_connection

    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)
    monkeypatch.setattr(asyncio, "sleep", join_during_dwell)

    result = asyncio.run(
        service.change_map_after_end_screen(
            "HallwayPin",
            end_screen_seconds=12.0,
        )
    )

    assert result.ok is True
    assert late_connection.reload_calls == 1
    assert late_connection.in_game is False
    assert late_connection.disconnect_reasons == []


def test_mid_mapsync_peer_is_retired_instead_of_receiving_second_handshake(
    monkeypatch,
) -> None:
    server = _Server()
    loading_connection = _Connection()
    loading_connection.in_game = False
    server.connections[object()] = loading_connection
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert result.reconnect_required is True
    assert loading_connection.reload_calls == 0
    assert loading_connection.disconnect_reasons == [18]
    settled = [
        connection
        for connection in server.connections.values()
        if connection is not loading_connection
    ]
    assert all(connection.reload_calls == 1 for connection in settled)


def test_scene_reload_failure_retires_only_the_incompatible_peer(monkeypatch) -> None:
    server = _Server()
    connections = list(server.connections.values())
    connections[0].reload_result = False
    service = MatchTransitionService(server)
    candidate = SimpleNamespace(map_name="HallwayPin", config=None)
    monkeypatch.setattr(service, "_load_world_candidate", lambda *_args: candidate)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    result = asyncio.run(service.change_map("HallwayPin"))

    assert result.ok is True
    assert result.reconnect_required is True
    assert connections[0].disconnect_reasons == [18]
    assert connections[1].disconnect_reasons == []
    assert connections[0].reload_calls == connections[1].reload_calls == 1


def test_failed_map_preflight_keeps_current_match_and_clients_untouched(
    monkeypatch,
) -> None:
    server = _Server()
    service = MatchTransitionService(server)

    def fail_load(_map_name: str, _mode_name: str):
        raise ValueError("Map not found: Missing")

    monkeypatch.setattr(service, "_load_world_candidate", fail_load)

    result = asyncio.run(service.change_map("Missing"))

    assert result.ok is False
    assert server.config.default_map == "London"
    assert server.world_manager.map_name == "London"
    assert server.fog_color_override == (1, 2, 3)
    assert all(conn.in_game is True for conn in server.connections.values())
    assert all(conn.disconnect_reasons == [] for conn in server.connections.values())


def test_requested_map_preload_does_not_block_the_event_loop(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)

    def slow_load(_map_name: str, _mode_name: str):
        time.sleep(0.08)
        return SimpleNamespace(map_name="HallwayPin")

    monkeypatch.setattr(service, "_load_world_candidate", slow_load)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    async def scenario() -> tuple[object, int]:
        accepted = service.request_map_change("HallwayPin")
        heartbeat = 0
        while service._request_task is not None and not service._request_task.done():
            heartbeat += 1
            await asyncio.sleep(0.005)
        if service._request_task is not None:
            await service._request_task
        return accepted, heartbeat

    accepted, heartbeat = asyncio.run(scenario())
    assert accepted.ok is True
    assert heartbeat >= 5
    assert server.world_manager.map_name == "HallwayPin"


def test_requested_mode_preload_does_not_hold_the_requesting_task(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)

    def slow_load(_map_name: str, _mode_name: str):
        time.sleep(0.08)
        return SimpleNamespace(map_name="London", config=None)

    monkeypatch.setattr(service, "_load_world_candidate", slow_load)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    async def scenario() -> tuple[object, int]:
        accepted = service.request_mode_change("tdm")
        heartbeat = 0
        while service._request_task is not None and not service._request_task.done():
            heartbeat += 1
            await asyncio.sleep(0.005)
        if service._request_task is not None:
            await service._request_task
        return accepted, heartbeat

    accepted, heartbeat = asyncio.run(scenario())
    assert accepted.ok is True
    assert heartbeat >= 5
    assert server.config.default_mode == "tdm"
    assert server.world_manager.map_name == "London"


def test_same_map_and_mode_are_noops_without_retiring_clients(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    map_result = service.request_map_change("london.vxl")
    mode_result = asyncio.run(service.change_mode("CTF"))

    assert map_result.ok and map_result.reconnect_required is False
    assert mode_result.ok and mode_result.reconnect_required is False
    assert service._request_task is None
    assert all(conn.in_game is True for conn in server.connections.values())
    assert all(conn.disconnect_reasons == [] for conn in server.connections.values())


def test_mode_and_restart_are_rejected_while_map_preload_runs(monkeypatch) -> None:
    server = _Server()
    service = MatchTransitionService(server)

    def slow_load(_map_name: str, _mode_name: str):
        time.sleep(0.08)
        return SimpleNamespace(map_name="HallwayPin")

    monkeypatch.setattr(service, "_load_world_candidate", slow_load)
    monkeypatch.setattr(service, "_resolve_mode_class", lambda _name: _NewMode)

    async def scenario():
        accepted = service.request_map_change("HallwayPin")
        await asyncio.sleep(0)
        mode_result = await service.change_mode("tdm")
        restart_result = await service.restart_round()
        task = service._request_task
        if task is not None:
            await task
        return accepted, mode_result, restart_result

    accepted, mode_result, restart_result = asyncio.run(scenario())

    assert accepted.ok is True
    assert mode_result.ok is False
    assert restart_result.ok is False
    assert server.world_manager.map_name == "HallwayPin"
