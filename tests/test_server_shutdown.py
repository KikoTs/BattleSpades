"""Regression coverage for ENet host/peer shutdown ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.config import ServerConfig
from server.connection import Connection
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


class _AsyncCloser:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    async def close(self) -> None:
        self.events.append(self.name)


class _SyncCloser:
    def __init__(self, events: list[str], name: str) -> None:
        self.events = events
        self.name = name

    def close(self) -> None:
        self.events.append(self.name)


class _ShutdownMode:
    def __init__(self, server: BattleSpadesServer, events: list[str]) -> None:
        self.server = server
        self.events = events
        self.end_calls = 0

    async def cancel_end_sequence(self) -> None:
        assert self.server._stopping is True
        self.events.append("mode-cancel")

    async def deactivate(self) -> None:
        assert self.server._stopping is True
        self.events.append("mode-deactivate")

    async def on_mode_end(self, _winner=None) -> None:
        self.end_calls += 1
        raise AssertionError("server shutdown must not start the victory sequence")


class _Peer:
    def __init__(self, server: BattleSpadesServer, events: list[str]) -> None:
        self.server = server
        self.events = events
        self.host = None
        self.address = "test-peer"
        self.disconnect_calls = 0

    def disconnect(self, _reason: int = 0) -> None:
        assert self.server.host is self.host
        assert self.server.connections == {}
        self.disconnect_calls += 1
        self.events.append("peer-disconnect")


class _Host:
    def __init__(
        self,
        server: BattleSpadesServer,
        peer: _Peer,
        events: list[str],
    ) -> None:
        self.server = server
        self.peer = peer
        self.events = events
        self.flush_calls = 0

    def flush(self) -> None:
        assert self.server.connections == {}
        assert self.peer.disconnect_calls == 1
        self.flush_calls += 1
        self.events.append("host-flush")


def test_stop_retires_mode_connections_and_peers_before_host() -> None:
    async def scenario():
        events: list[str] = []
        server = BattleSpadesServer(ServerConfig())
        server.running = True
        server.mode = _ShutdownMode(server, events)
        server.bots = None
        server.steam_master = _AsyncCloser(events, "steam-close")
        server.revival_master = _AsyncCloser(events, "revival-close")
        server.debug_parity = _SyncCloser(events, "debug-close")
        server.prefab_actions = _SyncCloser(events, "prefab-close")

        peer = _Peer(server, events)
        host = _Host(server, peer, events)
        peer.host = host
        server.host = host
        connection = Connection(peer, server)
        player = SimpleNamespace(connection=connection)
        connection.player = player
        waiter = asyncio.get_running_loop().create_future()
        connection._waiters[99] = waiter
        server.connections[peer] = connection
        server.players[0] = player
        server.teams[TEAM1].players.append(player)
        server._pending_ingame_packets.append((connection, b"stale"))
        server._mode_events.append(("on_tick", ()))
        server.reserved_player_ids.add(1)

        task_entered = asyncio.Event()

        async def pending_handshake() -> None:
            task_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("handshake-cancel")

        handshake = asyncio.create_task(pending_handshake())
        server._connection_tasks.add(handshake)
        await task_entered.wait()

        await server.stop()
        await server.stop()
        return server, connection, player, waiter, handshake, host, peer, events

    (
        server,
        connection,
        player,
        waiter,
        handshake,
        host,
        peer,
        events,
    ) = asyncio.run(scenario())

    assert server.running is False
    assert server._stopping is True
    assert server._stopped is True
    assert server.host is None
    assert server.connections == {}
    assert server.players == {}
    assert not server._pending_ingame_packets
    assert not server._mode_events
    assert server.reserved_player_ids == set()
    assert server.teams[TEAM1].players == []
    assert player.connection is None
    assert connection.player is None
    assert connection.in_game is False
    assert waiter.cancelled()
    assert handshake.cancelled()
    assert peer.disconnect_calls == 1
    assert host.flush_calls == 1
    assert server.mode.end_calls == 0
    assert events.index("mode-cancel") < events.index("mode-deactivate")
    assert events.index("mode-deactivate") < events.index("peer-disconnect")
    assert events.index("peer-disconnect") < events.index("host-flush")
    assert events.count("steam-close") == 1
    assert events.count("revival-close") == 1


def test_stop_releases_host_after_partial_start_failure() -> None:
    async def scenario():
        events: list[str] = []
        server = BattleSpadesServer(ServerConfig())
        server.running = False
        server.mode = None
        server.bots = None
        server.steam_master = _AsyncCloser(events, "steam-close")
        server.revival_master = _AsyncCloser(events, "revival-close")
        server.debug_parity = _SyncCloser(events, "debug-close")
        server.prefab_actions = _SyncCloser(events, "prefab-close")
        peer = _Peer(server, events)
        host = _Host(server, peer, events)
        peer.host = host
        server.host = host
        connection = Connection(peer, server)
        server.connections[peer] = connection

        await server.stop()
        return server, host, peer, events

    server, host, peer, events = asyncio.run(scenario())

    assert server.host is None
    assert server._stopped is True
    assert peer.disconnect_calls == 1
    assert host.flush_calls == 1
    assert events[-2:] == ["peer-disconnect", "host-flush"]


def test_connection_native_calls_are_inert_once_shutdown_starts() -> None:
    class _PoisonPeer:
        @property
        def address(self):
            raise AssertionError("shutdown send touched native peer address")

        def send(self, _channel, _packet) -> None:
            raise AssertionError("shutdown send reached native peer")

        def disconnect(self, _reason=0) -> None:
            raise AssertionError("shutdown disconnect reached native peer")

    connection = Connection.__new__(Connection)
    connection.server = SimpleNamespace(_stopping=True)
    connection.peer = _PoisonPeer()

    connection.send(b"\x01")
    connection.disconnect()
    asyncio.run(connection.on_receive(b"\x30\x01"))


def test_broadcast_is_inert_once_shutdown_starts() -> None:
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server._stopping = True

    # No config, connection registry, or mutation journal is installed.  The
    # lifecycle guard therefore has to return before touching any of them.
    server.broadcast(b"\x01")
