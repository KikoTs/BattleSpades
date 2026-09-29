"""Packet-delivery hardening: overflow, isolation, chat/team/color abuse,
unknown-id filtering, leaver events, queue fairness, handshake gating."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from types import SimpleNamespace

import pytest

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import (
    ChatMessage,
    CreateEntity,
    DestroyEntity,
    Entity,
    ExplodeCorpse,
    GameStats,
    NewPlayerConnection,
    PlayerLeft,
    SetColor,
    SetScore,
    WorldUpdate,
)

from modes.base_mode import BaseMode
from server import scoreboard
from server.config import ServerConfig
from server.corpse_lifecycle import CorpseLifecycle
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.handlers import social, team as team_handlers
from server.main import BattleSpadesServer
from server.player import Player
from server.replication import ReplicationService, wire_entity_id
from server.simulation_runtime import SimulationRuntime
from server.team import Team
from tests.test_reversed_spawn_handshake import DummyServer, make_connection


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, player=None, *, in_game=True, known=()):
        self.server = None
        self.player = player
        self.in_game = in_game
        self.known_player_lives = {int(pid): (0, 0) for pid in known}
        self.known_player_deaths = {}
        self.known_corpse_cleanups = {}
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))

    def on_disconnect(self):
        pass


def _bare_server():
    """A BattleSpadesServer shell exercising the real broadcast filters."""
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server._stopping = False
    server.connections = {}
    server.players = {}
    server.config = SimpleNamespace(log_suppress_packets=set())
    return server


def _ids(conn, packet_id):
    return [data for data in conn.sent if data[0] == packet_id]


# ---------------------------------------------------------------------------
# 1. WorldUpdate rocket-turret id overflow + tick isolation
# ---------------------------------------------------------------------------


def test_worldupdate_turret_row_uses_createentity_signed_id():
    raw_overflow = WorldUpdate()
    raw_overflow.rocket_turrets = [(40000, 0.0, 0.0)]
    with pytest.raises(OverflowError):
        raw_overflow.generate()

    turret = SimpleNamespace(world_update=lambda: (40000, 0.5, -0.25))
    server = SimpleNamespace(
        loop_count=10,
        players={},
        entities={},
        rocket_turrets={40000: turret},
        corpse_lifecycle=None,
    )
    packet = ReplicationService(server).build_world_update_packet()
    raw = bytes(packet.generate())  # must not raise

    entity = Entity()
    entity.entity_id = 40000
    create = CreateEntity()
    create.set_entity(entity)
    create_id_bytes = bytes(create.generate())[1:3]
    destroy = DestroyEntity()
    destroy.entity_id = 40000
    assert bytes(destroy.generate())[1:3] == create_id_bytes
    # Turret row = last 6 bytes: id, yaw, pitch.
    assert raw[-6:-4] == create_id_bytes
    decoded = WorldUpdate(ByteReader(raw[1:]))
    assert decoded.rocket_turrets[0][0] == wire_entity_id(40000) == -25536
    assert wire_entity_id(5) == 5 and wire_entity_id(65535) == -1


def _runtime_server():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    return server


def test_one_failing_subsystem_does_not_abort_the_tick(caplog):
    server = _runtime_server()
    runtime = SimulationRuntime(server)
    calls = []

    def broken_fire():
        raise RuntimeError("synthetic fire failure")

    server.fire_controller.update = broken_fire
    original_record = server.metrics.record_tick
    server.metrics.record_tick = lambda ms: calls.append(ms) or original_record(ms)

    with caplog.at_level(logging.ERROR, logger="server.simulation_runtime"):
        asyncio.run(runtime.step())
        asyncio.run(runtime.step())

    # Later work in the same tick still ran, on both ticks.
    assert len(calls) == 2
    failures = [r for r in caplog.records if "synthetic fire failure" in r.getMessage()]
    assert len(failures) == 1  # rate-limited: second failure only counted
    assert runtime._failure_log["fire"][2] == 2


def test_run_loop_survives_world_update_failure(monkeypatch):
    server = _runtime_server()
    runtime = SimulationRuntime(server)
    published = []

    def broken_publish():
        published.append(server.loop_count)
        if len(published) == 1:
            raise OverflowError("value too large to convert to short")
        if len(published) >= 3:
            server.running = False

    server._broadcast_world_updates = broken_publish
    server.running = True
    asyncio.run(asyncio.wait_for(runtime.run(), timeout=5.0))
    assert len(published) >= 3


def test_failing_mode_event_does_not_drop_the_rest():
    server = _runtime_server()
    runtime = SimulationRuntime(server)
    seen = []

    class _Mode(BaseMode):
        name = "Plain"

        async def on_player_spawn(self, player):
            seen.append(player)
            if player == "bad":
                raise ValueError("boom")

    server.mode = _Mode(server)
    server.queue_mode_event("on_player_spawn", "bad")
    server.queue_mode_event("on_player_spawn", "good")
    asyncio.run(runtime._tick_mode())
    assert seen == ["bad", "good"]


# ---------------------------------------------------------------------------
# 2. Chat
# ---------------------------------------------------------------------------


def _chat_world():
    server = _bare_server()
    blue = SimpleNamespace(id=1, name="Blue", team=TEAM1, muted=False)
    mate = SimpleNamespace(id=2, name="Mate", team=TEAM1, muted=False)
    green = SimpleNamespace(id=3, name="Green", team=TEAM2, muted=False)
    dead_joiner = SimpleNamespace(id=4, name="Dead", team=TEAM2, muted=False)
    everyone = (1, 2, 3)
    conns = {
        "blue": _Conn(blue, known=everyone),
        "mate": _Conn(mate, known=everyone),
        "green": _Conn(green, known=everyone),
        # The dead joiner knows the roster and itself; peers do not know it.
        "dead": _Conn(dead_joiner, known=everyone + (4,)),
        "loading": _Conn(SimpleNamespace(id=5, team=TEAM1), in_game=False, known=everyone),
    }
    server.connections = conns
    return server, conns, (blue, mate, green, dead_joiner)


def _chat(server, player, text, chat_type=0):
    asyncio.run(social.handle_chat(
        server, player, SimpleNamespace(value=text, chat_type=chat_type)
    ))


def test_team_chat_reaches_only_teammates():
    server, conns, (blue, *_rest) = _chat_world()
    _chat(server, blue, "flank left", chat_type=1)
    assert len(_ids(conns["blue"], 49)) == 1
    assert len(_ids(conns["mate"], 49)) == 1
    assert _ids(conns["green"], 49) == []
    assert _ids(conns["dead"], 49) == []
    assert _ids(conns["loading"], 49) == []
    packet = ChatMessage(ByteReader(_ids(conns["mate"], 49)[0][1:]))
    assert (packet.player_id, packet.chat_type) == (1, 1)


@pytest.mark.parametrize("spoofed", [2, 3, 7, 255])
def test_client_cannot_forge_system_or_big_chat(spoofed):
    server, conns, (blue, *_rest) = _chat_world()
    _chat(server, blue, "I am the server", chat_type=spoofed)
    packet = ChatMessage(ByteReader(_ids(conns["green"], 49)[0][1:]))
    assert packet.chat_type == int(C.CHAT_ALL)


def test_chat_is_cut_to_client_limit_and_rate_limited():
    server, conns, (blue, *_rest) = _chat_world()
    _chat(server, blue, "x" * 5000)
    packet = ChatMessage(ByteReader(_ids(conns["green"], 49)[0][1:]))
    assert len(packet.value) == int(C.MAX_CHAT_MESSAGE_LENGTH) == 200
    for index in range(20):
        _chat(server, blue, f"spam {index}")
    # Burst of five total (one used above), extra lines silently dropped.
    assert len(_ids(conns["green"], 49)) == int(social.CHAT_BURST)
    blue._chat_bucket = (0.0, time.monotonic() - 2.0)
    _chat(server, blue, "later")
    assert len(_ids(conns["green"], 49)) == int(social.CHAT_BURST) + 1


def test_dead_joiner_chat_only_reaches_peers_that_know_the_id():
    server, conns, (_blue, _mate, _green, dead) = _chat_world()
    _chat(server, dead, "hello?")
    assert _ids(conns["blue"], 49) == []
    assert _ids(conns["green"], 49) == []
    assert len(_ids(conns["dead"], 49)) == 1


def test_commands_bypass_chat_limits(monkeypatch):
    server, conns, (blue, *_rest) = _chat_world()
    dispatched = []

    async def fake_handle_command(_server, _player, message):
        dispatched.append(message)

    monkeypatch.setattr("commands.handle_command", fake_handle_command)
    # An exhausted chat bucket never blocks the command channel...
    for _ in range(10):
        _chat(server, blue, "spam")
    for _ in range(int(social.COMMAND_BURST)):
        _chat(server, blue, "/help")
    assert dispatched == ["help"] * int(social.COMMAND_BURST)
    # ...which has its own (separate) per-player bucket.
    _chat(server, blue, "/help")
    assert len(dispatched) == int(social.COMMAND_BURST)
    assert not any(b"help" in data for data in _ids(conns["green"], 49))


# ---------------------------------------------------------------------------
# 3. SetColor throttle and ChangeTeam cooldown/balance
# ---------------------------------------------------------------------------


class _PalettePlayer:
    def __init__(self):
        self.id = 7
        self.alive = True
        self.spawned = True
        self.tool = C.BLOCK_TOOL
        self.block_color = 0x707070

    def set_color(self, value):
        self.block_color = int(value)


def test_set_color_relays_are_throttled_with_trailing_latest_value():
    relayed = []
    player = _PalettePlayer()
    server = SimpleNamespace(
        players={7: player},
        broadcast=lambda data, exclude=None, **_k: relayed.append(
            SetColor(ByteReader(bytes(data)[1:])).value
        ),
    )

    async def scenario():
        for value in (0x111111, 0x222222, 0x333333, 0x444444):
            await team_handlers.handle_set_color(
                server, player, SimpleNamespace(value=value)
            )
        assert relayed == [0x111111]
        assert player.block_color == 0x444444
        await asyncio.sleep(team_handlers.SET_COLOR_RELAY_INTERVAL_SECONDS + 0.05)

    asyncio.run(scenario())
    assert relayed == [0x111111, 0x444444]


def test_trailing_color_relay_skips_departed_player():
    relayed = []
    player = _PalettePlayer()
    server = SimpleNamespace(
        players={7: player},
        broadcast=lambda data, **_k: relayed.append(bytes(data)),
    )

    async def scenario():
        await team_handlers.handle_set_color(server, player, SimpleNamespace(value=1))
        await team_handlers.handle_set_color(server, player, SimpleNamespace(value=2))
        server.players.clear()
        await asyncio.sleep(team_handlers.SET_COLOR_RELAY_INTERVAL_SECONDS + 0.05)

    asyncio.run(scenario())
    assert len(relayed) == 1


class _TeamPlayer:
    def __init__(self, player_id, team, connection=None):
        self.id = player_id
        self.name = f"P{player_id}"
        self.team = team
        self.alive = False
        self.connection = connection
        self.death_time = 0.0
        self.sent = []

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))


def _team_server(*, auto_balance=True, threshold=1, mode=None):
    teams = {
        TEAM1: Team(TEAM1, "TEAM1_COLOR", (0, 0, 255)),
        TEAM2: Team(TEAM2, "TEAM2_COLOR", (0, 255, 0)),
    }
    server = SimpleNamespace(
        config=SimpleNamespace(
            auto_balance=auto_balance, balance_threshold=threshold
        ),
        teams=teams,
        players={},
        connections={},
        mode=mode,
        events=[],
    )
    server.queue_mode_event = lambda name, *args: server.events.append(name)
    return server


def _add(server, player):
    server.players[player.id] = player
    server.teams[player.team].add_player(player)


def _change(server, player, wire_team):
    asyncio.run(team_handlers.handle_change_team(
        server, player, SimpleNamespace(team=wire_team)
    ))


def _wire(team):
    from server.connection import internal_team_to_wire

    return internal_team_to_wire(team)


def test_change_team_refuses_unbalancing_switch_with_notice():
    server = _team_server(threshold=1)
    ds = DummyServer()
    ds.config = server.config
    ds.players = server.players
    connection = make_connection(ds)
    mover = _TeamPlayer(0, TEAM1, connection)
    connection.player = mover
    _add(server, mover)
    _add(server, _TeamPlayer(1, TEAM1))
    _add(server, _TeamPlayer(2, TEAM2))
    # Without the mover: TEAM1=1, TEAM2=1 -> moving makes TEAM2 lead by 1.
    _change(server, mover, _wire(TEAM2))
    assert mover.team == TEAM2  # threshold 1: 1-1 = 0 < 1, allowed

    blocker = _TeamPlayer(3, TEAM1, connection)
    connection.player = blocker
    _add(server, blocker)
    # Without blocker: TEAM1=1, TEAM2=2 -> TEAM2 already leads by 1.
    _change(server, blocker, _wire(TEAM2))
    assert blocker.team == TEAM1
    # Retail TEAM_FULL ("Team is full. Auto-balancing...") LocalisedMessage.
    from shared.packet import LocalisedMessage

    notice = LocalisedMessage(ByteReader(blocker.sent[-1][1:]))
    assert blocker.sent[-1][0] == LocalisedMessage.id
    assert notice.string_id == "TEAM_FULL"


def test_change_team_balance_ignored_for_mode_assigned_teams():
    server = _team_server(threshold=1, mode=SimpleNamespace(prepare_join_team=lambda t: t))
    ds = DummyServer()
    ds.config = server.config
    ds.players = server.players
    connection = make_connection(ds)
    mover = _TeamPlayer(0, TEAM1, connection)
    connection.player = mover
    _add(server, mover)
    _add(server, _TeamPlayer(1, TEAM2))
    _add(server, _TeamPlayer(2, TEAM2))
    _change(server, mover, _wire(TEAM2))
    assert mover.team == TEAM2


def test_change_team_has_a_cooldown():
    server = _team_server(auto_balance=False)
    mover = _TeamPlayer(0, TEAM1)
    _add(server, mover)
    _change(server, mover, _wire(TEAM2))
    assert mover.team == TEAM2
    _change(server, mover, _wire(TEAM1))
    assert mover.team == TEAM2
    # Retail TEAM_SWITCH_WAIT; the menu path gets no English seconds count.
    from shared.packet import LocalisedMessage

    assert mover.sent[-1][0] == LocalisedMessage.id
    assert LocalisedMessage(ByteReader(mover.sent[-1][1:])).string_id == "TEAM_SWITCH_WAIT"
    mover._last_team_change_at -= team_handlers.TEAM_CHANGE_COOLDOWN_SECONDS + 1
    _change(server, mover, _wire(TEAM1))
    assert mover.team == TEAM1
    assert server.events == ["on_player_team_change", "on_player_team_change"]


def test_leaving_spectator_is_exempt_from_the_cooldown():
    server = _team_server(auto_balance=False)
    mover = _TeamPlayer(0, TEAM_SPECTATOR)
    server.players[0] = mover
    # Just switched into spectator; rejoining play kills no body.
    mover._last_team_change_at = time.monotonic()
    _change(server, mover, _wire(TEAM2))
    assert mover.team == TEAM2
    assert mover.death_time > 0.0
    # The next body-retiring switch is throttled again.
    _change(server, mover, _wire(TEAM1))
    assert mover.team == TEAM2


# ---------------------------------------------------------------------------
# 4. Packets naming unknown / departed ids
# ---------------------------------------------------------------------------


def test_set_score_skips_peers_that_never_saw_the_player():
    server = _bare_server()
    knows = _Conn(SimpleNamespace(id=1), known=(1, 9))
    unaware = _Conn(SimpleNamespace(id=2), known=(2,))
    loading = _Conn(SimpleNamespace(id=3), in_game=False, known=(9,))
    server.connections = {"a": knows, "b": unaware, "c": loading}
    player = SimpleNamespace(id=9, score=40)
    server.players = {9: player}
    scoreboard.send_player_score(server, player)
    assert len(_ids(knows, SetScore.id)) == 1
    assert _ids(unaware, SetScore.id) == []
    assert _ids(loading, SetScore.id) == []

    scoreboard.reset_round_scores(server)
    assert len(_ids(knows, SetScore.id)) == 2
    assert _ids(unaware, SetScore.id) == []


def test_set_score_for_leaver_is_not_sent_after_id_reuse():
    server = _bare_server()
    conn = _Conn(SimpleNamespace(id=1), known=(1, 9))
    server.connections = {"a": conn}
    leaver = SimpleNamespace(id=9, score=100)
    server.players = {9: SimpleNamespace(id=9, score=0)}  # id reused
    scoreboard.send_player_score(server, leaver)
    assert _ids(conn, SetScore.id) == []


def test_explode_corpse_goes_only_to_peers_that_know_the_id():
    server = _bare_server()
    knows = _Conn(SimpleNamespace(id=1), known=(1, 6))
    unaware = _Conn(SimpleNamespace(id=2), known=(2,))
    server.connections = {"a": knows, "b": unaware}
    CorpseLifecycle(server)._send_packet(6, show_explosion_effect=False)
    assert len(_ids(knows, ExplodeCorpse.id)) == 1
    assert _ids(unaware, ExplodeCorpse.id) == []


def test_game_stats_rows_are_filtered_per_peer():
    server = _bare_server()
    knows = _Conn(SimpleNamespace(id=1), known=(1, 2))
    partial = _Conn(SimpleNamespace(id=3), known=(1, 3))
    server.connections = {"a": knows, "b": partial}
    packet = GameStats()
    packet.team_id = TEAM1
    packet.noOfStats = 2
    packet.player_ids = [1, 2]
    packet.types = [int(C.MOST_KILLS), int(C.MOST_ASSISTS)]
    scoreboard._broadcast_game_stats_rows(server, packet, bytes(packet.generate()))
    full = GameStats(ByteReader(_ids(knows, GameStats.id)[0][1:]))
    filtered = GameStats(ByteReader(_ids(partial, GameStats.id)[0][1:]))
    assert list(full.player_ids) == [1, 2]
    assert list(filtered.player_ids) == [1]


def _disconnect_world():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    return server


def test_player_left_for_dead_joiner_reaches_only_peers_that_knew_it():
    server = _disconnect_world()
    leaver_conn = _Conn()
    leaver = Player(4, "DeadJoiner", TEAM2, C.RIFLE_TOOL, leaver_conn)
    leaver_conn.player = leaver
    server.players[4] = leaver
    server.teams[TEAM2].add_player(leaver)
    unaware = _Conn(SimpleNamespace(id=1), known=(1,))
    aware = _Conn(SimpleNamespace(id=2), known=(2, 4))
    loading = _Conn(SimpleNamespace(id=3), in_game=False, known=(3, 4))
    server.connections = {
        "leaver": leaver_conn, "unaware": unaware, "aware": aware, "loading": loading,
    }
    server._on_disconnect_sync("leaver")
    assert _ids(unaware, PlayerLeft.id) == []
    assert len(_ids(aware, PlayerLeft.id)) == 1
    assert 4 not in aware.known_player_lives
    # Loading peers are repaired by catch_up_roster (stale id still known).
    assert _ids(loading, PlayerLeft.id) == []
    assert 4 in loading.known_player_lives


# ---------------------------------------------------------------------------
# 5. Leaver events
# ---------------------------------------------------------------------------


def test_leaver_scoring_events_are_settled_before_leave_hook():
    server = _disconnect_world()
    conn = _Conn()
    leaver = Player(0, "Leaver", TEAM1, C.RIFLE_TOOL, conn)
    conn.player = leaver
    server.players[0] = leaver
    server.teams[TEAM1].add_player(leaver)
    other = Player(1, "Other", TEAM2, C.RIFLE_TOOL, None)
    third = Player(2, "Third", TEAM1, C.RIFLE_TOOL, None)
    server.players[1] = other
    server.players[2] = third
    server.connections = {"peer": conn}
    order = []

    class _Mode(BaseMode):
        name = "Plain"

        async def on_player_kill(self, killer, victim, kill_type):
            order.append(("kill", killer.id, victim.id))

        async def on_player_death(self, player, killer, kill_type):
            order.append(("death", player.id))

        async def on_player_leave(self, player):
            order.append(("leave", player.id))

    server.mode = _Mode(server)
    server.queue_mode_event("on_player_kill", leaver, other, 0)   # leaver scored
    server.queue_mode_event("on_player_death", other, leaver, 0)
    server.queue_mode_event("on_player_kill", third, other, 0)    # unrelated
    server.queue_mode_event("on_player_spawn", leaver)            # re-acquire
    server.queue_mode_event("on_player_team_change", leaver, TEAM1, TEAM2)
    server.queue_mode_event("on_player_death", leaver, other, 0)  # leaver died

    server._on_disconnect_sync("peer")

    assert order == [
        ("kill", 0, 1),
        ("death", 1),
        ("death", 0),
        ("leave", 0),
    ]
    remaining = [(name, args[0].id) for name, args in server._mode_events]
    assert remaining == [("on_player_kill", 2)]


# ---------------------------------------------------------------------------
# 6. In-game queue fairness
# ---------------------------------------------------------------------------


def test_flooding_peer_cannot_starve_other_connections():
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server._pending_ingame_packets = deque()
    server._pending_ingame_counts = {}
    server._dropped_ingame_packets = 0
    server.metrics = SimpleNamespace(dropped_ingame_packets=0)
    server.config = SimpleNamespace(max_pending_packets=4096)
    flooder, honest = object(), object()
    accepted = sum(server._queue_ingame_packet(flooder, b"\x04") for _ in range(5000))
    assert accepted == server._per_connection_packet_cap() == 256
    assert server._queue_ingame_packet(honest, b"\x04ok") is True
    assert server._dropped_ingame_packets == 5000 - 256

    # A wholesale clear (timeline reset) resets every share.
    server._pending_ingame_packets.clear()
    assert server._queue_ingame_packet(flooder, b"\x04") is True


def test_drain_releases_per_connection_share():
    server = BattleSpadesServer.__new__(BattleSpadesServer)
    server._pending_ingame_packets = deque()
    server._pending_ingame_counts = {}
    server._dropped_ingame_packets = 0
    server.metrics = SimpleNamespace(dropped_ingame_packets=0)
    server.config = SimpleNamespace(max_pending_packets=4096, packet_drain_budget=10)
    received = []
    conn = SimpleNamespace(peer="p", player=None)

    async def on_receive(data):
        received.append(data)

    conn.on_receive = on_receive
    server.connections = {"p": conn}
    server.players = {}
    for _ in range(15):
        server._queue_ingame_packet(conn, b"\x04")
    asyncio.run(server._drain_ingame_packets())
    assert len(received) == 10
    assert server._pending_ingame_counts[id(conn)] == 5


# ---------------------------------------------------------------------------
# 7. NewPlayerConnection requires the pre-join handshake
# ---------------------------------------------------------------------------


def _join_bytes():
    packet = NewPlayerConnection()
    packet.team = 2
    packet.class_id = int(C.CLASS_SOLDIER)
    packet.name = "Joiner"
    return bytes(packet.generate())


def test_new_player_before_handshake_is_ignored(monkeypatch):
    connection = make_connection(DummyServer())
    joins = []

    async def fake_join(packet):
        joins.append(packet.name)

    monkeypatch.setattr(connection, "_on_new_player", fake_join)
    asyncio.run(connection.handle_pre_join_packet(_join_bytes()))
    assert joins == []

    # Map sync + StateData done: stock Steam and ticket-less clients alike.
    connection.map_sent = connection.state_sent = True
    asyncio.run(connection.handle_pre_join_packet(_join_bytes()))
    assert joins == ["Joiner"]


def test_duplicate_in_flight_new_player_is_ignored(monkeypatch):
    connection = make_connection(DummyServer())
    connection.map_sent = connection.state_sent = True
    joins = []

    async def slow_join(packet):
        joins.append(packet.name)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(connection, "_on_new_player", slow_join)

    async def scenario():
        await asyncio.gather(
            connection.handle_pre_join_packet(_join_bytes()),
            connection.handle_pre_join_packet(_join_bytes()),
        )

    asyncio.run(scenario())
    assert joins == ["Joiner"]


# ---------------------------------------------------------------------------
# 8. Debug ids 241-243 / unknown ids
# ---------------------------------------------------------------------------


def test_debug_packet_ids_are_not_registered_and_never_warn(caplog):
    from protocol.handler_registry import HANDLERS
    from protocol.packet_handler import PacketHandler

    assert not {241, 242, 243} & set(HANDLERS)
    handler = PacketHandler(SimpleNamespace(config=SimpleNamespace(log_suppress_packets=set())))
    player = SimpleNamespace(name="Forger")
    with caplog.at_level(logging.DEBUG, logger="protocol.packet_handler"):
        for packet_id in (241, 242, 243, 200, 200, 200):
            asyncio.run(handler.handle(player, bytes([packet_id, 0, 0])))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # Rate-limited: repeats of the same id are counted, not logged.
    assert len([r for r in caplog.records if "packet ID 200" in r.getMessage()]) <= 1


def test_sender_sees_its_own_chat_line_even_though_it_is_not_its_own_peer():
    # Live stock-client check (2026-09-26): known_player_lives lists peers
    # only, so the sender's own echo must not depend on it.
    server, conns, (blue, *_rest) = _chat_world()
    conns["blue"].known_player_lives = {2, 3}
    _chat(server, blue, "hello all")
    _chat(server, blue, "hello team", chat_type=1)
    assert len(_ids(conns["blue"], 49)) == 2
