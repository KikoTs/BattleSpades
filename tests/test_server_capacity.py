import logging
import queue
import asyncio
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

from shared.bytes import ByteReader
from shared.packet import ChatMessage, WorldUpdate
from server.connection import Connection, logger as connection_logger
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.logging_runtime import NonBlockingQueueHandler
from server.metrics import RuntimeMetrics
from server.replication import ReplicationService
from server.simulation_runtime import SimulationRuntime
from server.util import lzf_compress, lzf_decompress
from plugins.base_plugin import BasePlugin, PluginManager


class _Peer:
    address = ("127.0.0.1", 12345)

    def __init__(self):
        self.sent = []

    def send(self, channel, packet):
        self.sent.append((channel, packet))


class _WorldConnection:
    def __init__(self, player):
        self.player = player
        self.in_game = True
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((data, reliable))


def test_plugin_system_message_uses_the_stock_chat_packet():
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    sent = []
    server.broadcast = lambda data, **_kwargs: sent.append(data)

    asyncio.run(server.broadcast_message("KikoTs is on a spree!"))

    packet = ChatMessage(ByteReader(sent[0][1:]))
    assert packet.player_id == 0xFF
    assert packet.chat_type == 2
    assert packet.value == "KikoTs is on a spree!"


def test_packet_details_are_not_parsed_when_trace_is_disabled():
    connection = Connection.__new__(Connection)
    connection.peer = _Peer()
    connection.server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=[], packet_trace=False)
    )

    previous = connection_logger.level
    connection_logger.setLevel(logging.DEBUG)
    try:
        with patch(
            "server.connection.try_parse_packet_for_logging",
            side_effect=AssertionError("packet parsing ran on gameplay thread"),
        ):
            connection.send(b"\x55\x00\x01", reliable=False)
    finally:
        connection_logger.setLevel(previous)

    assert len(connection.peer.sent) == 1


def test_log_queue_drops_instead_of_blocking_when_sink_falls_behind():
    target = queue.Queue(maxsize=1)
    handler = NonBlockingQueueHandler(target)
    first = logging.LogRecord("test", logging.INFO, __file__, 1, "one", (), None)
    second = logging.LogRecord("test", logging.INFO, __file__, 2, "two", (), None)

    handler.emit(first)
    handler.emit(second)

    assert target.qsize() == 1
    assert handler.dropped_records == 1


def test_log_message_formatting_is_deferred_to_listener_thread():
    class Expensive:
        def __str__(self):
            raise AssertionError("formatted on gameplay thread")

    target = queue.Queue(maxsize=2)
    handler = NonBlockingQueueHandler(target)
    record = logging.LogRecord(
        "test", logging.DEBUG, __file__, 1, "value=%s", (Expensive(),), None
    )

    handler.emit(record)

    queued = target.get_nowait()
    assert queued.msg == "value=%s"
    assert isinstance(queued.args[0], Expensive)


def test_literal_packet_framing_round_trips_large_world_update():
    payload = bytes(range(256)) * 12

    encoded = lzf_compress(payload)

    assert lzf_decompress(encoded) == payload
    assert len(encoded) == len(payload) + (len(payload) + 31) // 32


def test_production_defaults_admit_fifty_players_with_headroom():
    config = ServerConfig()

    assert config.max_players >= 50
    assert config.max_connections > config.max_players


def test_packet_drain_budget_preserves_remaining_backlog_for_next_tick():
    processed = []

    class PendingConnection:
        def __init__(self, peer, player):
            self.peer = peer
            self.player = player

        async def on_receive(self, data):
            processed.append(data)

    server = BattleSpadesServer.__new__(BattleSpadesServer)
    peer = object()
    player = SimpleNamespace(id=7)
    connection = PendingConnection(peer, player)
    server.config = SimpleNamespace(packet_drain_budget=2)
    server.connections = {peer: connection}
    server.players = {player.id: player}
    server._pending_ingame_packets = deque(
        [(connection, b"one"), (connection, b"two"), (connection, b"three")]
    )

    asyncio.run(server._drain_ingame_packets())

    assert processed == [b"one", b"two"]
    assert list(server._pending_ingame_packets) == [(connection, b"three")]


def test_packet_drain_rechecks_connection_identity_after_each_await():
    """A disconnect during one packet must invalidate the batch's FIFO tail."""
    processed = []
    peer = object()
    player = SimpleNamespace(id=7)
    server = BattleSpadesServer.__new__(BattleSpadesServer)

    class DisconnectingConnection:
        def __init__(self):
            self.peer = peer
            self.player = player

        async def on_receive(self, data):
            processed.append(data)
            server.connections.pop(peer, None)

    connection = DisconnectingConnection()
    server.config = SimpleNamespace(packet_drain_budget=2)
    server.connections = {peer: connection}
    server.players = {player.id: player}
    server._pending_ingame_packets = deque(
        [(connection, b"disconnect"), (connection, b"stale-tail")]
    )

    asyncio.run(server._drain_ingame_packets())

    assert processed == [b"disconnect"]


def test_packet_drain_rejects_connection_replaced_for_same_peer():
    """Queued data belongs to a connection generation, not just an ENet peer."""
    processed = []
    peer = object()
    old_player = SimpleNamespace(id=7)
    new_player = SimpleNamespace(id=7)

    class PendingConnection:
        def __init__(self, player):
            self.peer = peer
            self.player = player

        async def on_receive(self, data):
            processed.append(data)

    stale = PendingConnection(old_player)
    replacement = PendingConnection(new_player)
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server.config = SimpleNamespace(packet_drain_budget=1)
    server.connections = {peer: replacement}
    server.players = {new_player.id: new_player}
    server._pending_ingame_packets = deque([(stale, b"stale-generation")])

    asyncio.run(server._drain_ingame_packets())

    assert processed == []


def test_mode_event_queue_is_bounded_and_counts_overflow():
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server.config = SimpleNamespace(mode_event_queue_limit=2)
    server.metrics = RuntimeMetrics()
    server._mode_events = deque()

    server.queue_mode_event("one", 1)
    server.queue_mode_event("two", 2)
    server.queue_mode_event("three", 3)

    assert list(server._mode_events) == [("one", (1,)), ("two", (2,))]
    assert server.metrics.dropped_mode_events == 1


def test_mode_event_drain_budget_defers_fifo_tail_to_next_tick():
    calls = []

    class Mode:
        async def on_tick(self, loop_count):
            calls.append(("tick", loop_count))

        async def event(self, value):
            calls.append(("event", value))

    class Plugins:
        async def call_event(self, name, *args):
            calls.append(("plugin", name, *args))

    server = SimpleNamespace(
        mode=Mode(),
        loop_count=77,
        config=SimpleNamespace(mode_event_drain_budget=1),
        plugin_manager=Plugins(),
        _mode_events=deque((
            ("event", (1,)),
            ("event", (2,)),
        )),
    )

    asyncio.run(SimulationRuntime(server)._tick_mode())

    assert calls == [
        ("tick", 77),
        ("event", 1),
        ("plugin", "event", 1),
    ]
    assert list(server._mode_events) == [("event", (2,))]


def test_runtime_metrics_expose_capacity_percentiles_and_world_cost():
    metrics = RuntimeMetrics()
    for elapsed in (1.0, 2.0, 3.0, 4.0, 10.0):
        metrics.record_tick(elapsed)
    metrics.record_world_packet(size=1000, recipients=50)

    snapshot = metrics.snapshot()

    assert snapshot["tick_avg_ms"] == 4.0
    assert snapshot["tick_p50_ms"] == 3.0
    assert snapshot["tick_p99_ms"] == 10.0
    assert snapshot["world_serializations"] == 1
    assert snapshot["world_sends"] == 50
    assert snapshot["world_bytes"] == 50_000


def test_plugin_callbacks_stop_after_gameplay_budget_is_exhausted():
    calls = []
    server = SimpleNamespace(metrics=RuntimeMetrics())
    manager = PluginManager(server)

    class SlowPlugin(BasePlugin):
        name = "slow"

        async def on_tick(self, tick):
            calls.append(("slow", tick))
            deadline = time.perf_counter() + 0.003
            while time.perf_counter() < deadline:
                pass

    class LaterPlugin(BasePlugin):
        name = "later"

        async def on_tick(self, tick):
            calls.append(("later", tick))

    manager.plugins = {
        "slow": SlowPlugin(server),
        "later": LaterPlugin(server),
    }

    asyncio.run(manager.call_event("on_tick", 77, budget_ms=0.1))

    assert calls == [("slow", 77)]
    assert server.metrics.skipped_plugin_callbacks == 1


def _world_server(player_count=50, stamp=1000):
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server.loop_count = 120
    server.tick_rate = 60
    server.config = SimpleNamespace(
        broadcast_world_updates=True,
        worldupdate_broadcast_interval=2,
        worldupdate_include_self=True,
        worldupdate_self_row_interval=2,
        worldupdate_loop_offset=0,
        debug_selfrow=False,
    )
    server.players = {}
    server.connections = {}
    server.metrics = RuntimeMetrics()
    for player_id in range(player_count):
        player = SimpleNamespace(
            id=player_id,
            last_applied_input_loop=stamp,
            wu_ack_loop=0,
            is_block_tool=lambda: False,
        )
        connection = _WorldConnection(player)
        server.players[player_id] = player
        server.connections[player_id] = connection
    return server


def test_fifty_clients_with_same_ack_share_one_world_serialization():
    server = _world_server()
    builds = []

    def build(*, exclude_player_id=None, loop_count_override=None):
        builds.append((exclude_player_id, loop_count_override))
        return b"\x02world"

    server.build_world_update_data = build
    server._broadcast_world_updates()

    assert builds == [(None, 120)]
    assert all(connection.sent == [(b"\x02world", False)]
               for connection in server.connections.values())


def test_world_update_send_rate_is_thirty_hz_on_sixty_hz_simulation():
    server = _world_server(player_count=2)
    server.loop_count = 121
    server.build_world_update_data = lambda **_kwargs: b"\x02world"

    server._broadcast_world_updates()

    assert all(not connection.sent for connection in server.connections.values())


def test_world_serialization_shares_global_snapshot_across_row_pong_stamps():
    server = _world_server(player_count=6)
    for player_id, player in server.players.items():
        player.last_applied_input_loop = 1000 + player_id % 2
    builds = []
    server.build_world_update_data = lambda **kwargs: (
        builds.append(kwargs) or b"\x02world"
    )

    server._broadcast_world_updates()

    assert builds == [{"loop_count_override": 120}]
    assert {player.wu_ack_loop for player in server.players.values()} == {
        1000,
        1001,
    }


def test_same_stamp_serializes_once_and_patches_only_each_owner_tool():
    """One base snapshot must serve owners without replaying local tools."""

    def snapshot(tool):
        return (
            (1.0, 2.0, 3.0),
            (0.0, 1.0, 0.0),
            (0.1, 0.2, 0.3),
            0,
            100,
            100,
            0,
            0,
            0,
            tool,
            0xFF,
            1.0,
            0.0,
            0.0,
        )

    players = {
        player_id: SimpleNamespace(
            id=player_id,
            alive=True,
            spawned=True,
            tool=tool,
            last_applied_input_loop=100,
            wu_ack_loop=0,
            world_update_snapshot=lambda tool=tool: snapshot(tool),
        )
        for player_id, tool in ((0, 5), (1, 7))
    }
    connections = {
        player_id: _WorldConnection(player)
        for player_id, player in players.items()
    }
    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_include_self=True,
            worldupdate_self_row_interval=2,
            worldupdate_loop_offset=0,
            debug_selfrow=False,
        ),
        loop_count=120,
        players=players,
        connections=connections,
        entities={},
        rocket_turrets={},
        metrics=RuntimeMetrics(),
    )
    replication = ReplicationService(server)
    build_calls = []
    base_payloads = []

    def build_world_update_data(**kwargs):
        build_calls.append(kwargs)
        data = replication.build_world_update_data(**kwargs)
        base_payloads.append(data)
        return data

    server.build_world_update_data = build_world_update_data

    replication.broadcast_world_updates()

    assert build_calls == [{"loop_count_override": 120}]
    owner_zero = WorldUpdate(ByteReader(connections[0].sent[0][0][1:]))
    owner_one = WorldUpdate(ByteReader(connections[1].sent[0][0][1:]))
    assert owner_zero.player_updates[0][9] == 0xFF
    assert owner_zero.player_updates[1][9] == 7
    assert owner_one.player_updates[0][9] == 5
    assert owner_one.player_updates[1][9] == 0xFF
    assert [
        index
        for index, (base, owner) in enumerate(
            zip(base_payloads[0], connections[0].sent[0][0])
        )
        if base != owner
    ] == [55]
    assert [
        index
        for index, (base, owner) in enumerate(
            zip(base_payloads[0], connections[1].sent[0][0])
        )
        if base != owner
    ] == [111]
    assert server.metrics.world_serializations == 1
    assert server.metrics.world_sends == 2


def test_owner_override_changes_only_the_local_tool_byte():
    """Recipient specialization must not mutate any gameplay action state."""

    def snapshot(player_id, tool):
        return (
            (float(player_id), 2.0, 3.0),
            (0.0, 1.0, 0.0),
            (0.1, 0.2, 0.3),
            0,
            100,
            100,
            0,
            0x04,
            0,
            tool,
            0xFF,
            90.0,
            0.0,
            0.0,
        )

    players = {
        player_id: SimpleNamespace(
            id=player_id,
            alive=True,
            spawned=True,
            world_update_snapshot=(
                lambda player_id=player_id, tool=tool:
                snapshot(player_id, tool)
            ),
        )
        for player_id, tool in ((0, 5), (1, 7))
    }
    server = SimpleNamespace(
        players=players,
        entities={},
        rocket_turrets={},
        metrics=RuntimeMetrics(),
    )
    replication = ReplicationService(server)
    base = replication.build_world_update_data(loop_count_override=100)
    offsets = replication._player_tool_offsets(base)

    owner = replication._with_local_owner_overrides(
        base,
        offsets,
        player_id=0,
    )
    parsed = WorldUpdate(ByteReader(owner[1:]))

    assert parsed.player_updates[0][7] == 0x04
    assert parsed.player_updates[0][9] == 0xFF
    assert parsed.player_updates[1][7] == 0x04
    assert parsed.player_updates[1][9] == 7
    assert [
        index
        for index, (before, after) in enumerate(zip(base, owner))
        if before != after
    ] == [55]
