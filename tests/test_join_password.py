"""Private servers: PasswordNeeded(112) / PasswordProvided(113) / Password(111).

The server asks after the client's first packet and sends nothing about the
match until the answer is right. Wire contract: docs/PROTOCOL.md,
"Password-protected servers".
"""
from __future__ import annotations

import asyncio
import logging
import textwrap
from types import SimpleNamespace

import pytest

from shared.constants import DISCONNECT
from shared.packet import (
    NewPlayerConnection,
    Password,
    PasswordNeeded,
    PasswordProvided,
    SteamSessionTicket,
)

from server import join_password
from server.a2s_query import A2SHandler
from server.config import ServerConfig, load_config
from server.connection import Connection
from server.main import BattleSpadesServer
from scripts.check_steam_registration import parse_a2s_info

SECRET = "open-sesame"


class Peer:
    def __init__(self, host="203.0.113.7", port=40000):
        self.address = SimpleNamespace(host=host, port=port)
        self.disconnects = []

    def __str__(self):
        return f"{self.address.host}:{self.address.port}"

    def disconnect(self, reason=0):
        self.disconnects.append(int(reason))


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class GatedConnection(Connection):
    """A Connection whose wire and loader handshake are recorded."""

    def __init__(self, peer, server):
        super().__init__(peer, server)
        self.sent = []
        self.handshakes = 0

    def send(self, data, reliable=True, prefix=0x30, *, unsequenced=False):
        self.sent.append(bytes(data))

    async def send_connection_data(self, *, require_map_validation=False):
        if not self._join_password_cleared():
            return False
        self.handshakes += 1
        return True


def _server(password=SECRET, **settings):
    config = SimpleNamespace(
        join_password=password,
        password_max_attempts=3,
        password_timeout_seconds=60.0,
        password_lockout_seconds=60.0,
        log_suppress_packets=set(),
        packet_trace=True,
    )
    for key, value in settings.items():
        setattr(config, key, value)
    return SimpleNamespace(
        config=config, players={}, connections={}, reserved_player_ids=set()
    )


def _ticket() -> bytes:
    packet = SteamSessionTicket()
    packet.ticket_size = 0
    packet.ticket = b""
    return bytes(packet.generate())


def _answer(text, packet_class=PasswordProvided) -> bytes:
    packet = packet_class()
    packet.password = text
    return bytes(packet.generate())


def _ids(connection):
    return [data[0] for data in connection.sent]


@pytest.mark.parametrize("host", ("203.0.113.7", "2001:db8::7"))
def test_tuple_peer_address_lockout_key_ignores_source_port(host):
    connections = [
        SimpleNamespace(peer=SimpleNamespace(address=(host, port)))
        for port in (40000, 40001)
    ]
    assert [join_password.peer_host(connection) for connection in connections] == [host, host]


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(join_password.time, "monotonic", clock)
    return clock


def _run(coroutine):
    return asyncio.run(coroutine)


async def _connect(server, peer=None, clock=None):
    connection = GatedConnection(peer or Peer(), server)
    await connection.handle_pre_join_packet(_ticket())
    return connection


async def _say(connection, text, clock, packet_class=PasswordProvided, wait=1.0):
    clock.now += wait
    await connection.handle_pre_join_packet(_answer(text, packet_class))


def test_open_server_starts_the_handshake_at_once(clock):
    async def scenario():
        connection = await _connect(_server(password=""))
        assert connection.handshakes == 1
        assert connection.sent == []

    _run(scenario())


def test_password_server_sends_only_the_challenge(clock):
    async def scenario():
        connection = await _connect(_server())
        assert connection.sent == [bytes([PasswordNeeded.id])]
        assert connection.handshakes == 0
        assert connection.peer.disconnects == []
        connection._cancel_join_password_timer()

    _run(scenario())


def test_wire_bytes_of_the_three_packets():
    assert bytes(PasswordNeeded().generate()) == b"\x70"
    assert _answer("ab") == b"\x71ab\x00"
    assert _answer("ab", Password) == b"\x6fab\x00"


@pytest.mark.parametrize("packet_class", [PasswordProvided, Password])
def test_right_answer_continues_the_join(clock, packet_class):
    async def scenario():
        connection = await _connect(_server())
        await _say(connection, SECRET, clock, packet_class)
        assert connection.handshakes == 1
        assert _ids(connection) == [PasswordNeeded.id]
        assert connection.peer.disconnects == []
        assert connection._join_password_timer is None
        # A second answer after the join started changes nothing.
        await _say(connection, "anything", clock)
        assert connection.handshakes == 1
        assert _ids(connection) == [PasswordNeeded.id]

    _run(scenario())


def test_wrong_answer_asks_again_and_the_next_right_one_is_accepted(clock):
    async def scenario():
        connection = await _connect(_server())
        await _say(connection, "nope", clock)
        assert _ids(connection) == [PasswordNeeded.id, PasswordNeeded.id]
        assert connection.handshakes == 0
        await _say(connection, SECRET, clock)
        assert connection.handshakes == 1
        assert connection.peer.disconnects == []

    _run(scenario())


def test_third_wrong_answer_kicks_and_locks_the_address_out(clock):
    async def scenario():
        server = _server()
        connection = await _connect(server)
        for _ in range(3):
            await _say(connection, "nope", clock)
        assert connection.peer.disconnects == [int(DISCONNECT.ERROR_KICKED)]
        # Two retries were offered, not three.
        assert _ids(connection) == [PasswordNeeded.id] * 3
        # The right answer after the kick is not read any more.
        await _say(connection, SECRET, clock)
        assert connection.handshakes == 0
        assert connection.peer.disconnects == [int(DISCONNECT.ERROR_KICKED)]

        again = await _connect(server, Peer(port=40001))
        assert again.sent == []
        assert again.peer.disconnects == [int(DISCONNECT.ERROR_KICKED)]

        neighbour = await _connect(server, Peer(host="203.0.113.8"))
        assert _ids(neighbour) == [PasswordNeeded.id]
        neighbour._cancel_join_password_timer()

        clock.now += 61.0
        later = await _connect(server, Peer(port=40002))
        assert _ids(later) == [PasswordNeeded.id]
        later._cancel_join_password_timer()

    _run(scenario())


def test_rushed_answers_count_as_wrong(clock):
    async def scenario():
        connection = await _connect(_server())
        await _say(connection, "nope", clock)
        await _say(connection, SECRET, clock, wait=0.1)
        assert connection.handshakes == 0
        assert connection.join_password.attempts == 2
        await _say(connection, SECRET, clock, wait=0.6)
        assert connection.handshakes == 1

    _run(scenario())


def test_overlong_answer_is_wrong(clock):
    async def scenario():
        server = _server(password="x" * 64)
        connection = await _connect(server)
        await _say(connection, "x" * 65, clock)
        assert connection.handshakes == 0
        await _say(connection, "x" * 64, clock)
        assert connection.handshakes == 1

    _run(scenario())


def test_unanswered_challenge_times_out(clock):
    async def scenario():
        connection = await _connect(_server())
        assert connection._join_password_timer is not None
        connection._cancel_join_password_timer()
        connection._join_password_timed_out()
        assert connection.peer.disconnects == [int(DISCONNECT.ERROR_TIMEOUT)]

    _run(scenario())


def test_timer_after_a_right_answer_does_nothing(clock):
    async def scenario():
        connection = await _connect(_server())
        await _say(connection, SECRET, clock)
        connection._join_password_timed_out()
        assert connection.peer.disconnects == []

    _run(scenario())


def test_repeated_ticket_does_not_repeat_the_challenge(clock):
    async def scenario():
        connection = await _connect(_server())
        await connection.handle_pre_join_packet(_ticket())
        assert _ids(connection) == [PasswordNeeded.id]
        assert connection.handshakes == 0
        connection._cancel_join_password_timer()

    _run(scenario())


def test_nothing_about_the_match_leaves_before_the_answer(clock):
    async def scenario():
        server = BattleSpadesServer(ServerConfig(join_password=SECRET))
        server.world_manager.generate_flat_map()
        connection = Connection(Peer(), server)
        sent = []
        connection.send = lambda data, *args, **kwargs: sent.append(bytes(data))

        await connection.handle_pre_join_packet(_ticket())
        join = NewPlayerConnection()
        join.name = "intruder"
        await connection.handle_pre_join_packet(bytes(join.generate()))
        # The real loader handshake refuses as well.
        assert await connection.send_connection_data() is False
        assert await connection.reload_scene() is False

        assert sent == [bytes([PasswordNeeded.id])]
        assert connection.player is None
        assert connection.reserved_player_id is None
        assert not connection.map_sent and not connection.state_sent
        assert server.players == {}
        connection._cancel_join_password_timer()

    _run(scenario())


def test_password_never_reaches_a_log(clock, caplog):
    async def scenario():
        connection = await _connect(_server())
        with caplog.at_level(logging.DEBUG):
            for text in ("wrong-" + SECRET, SECRET):
                clock.now += 1.0
                await connection.on_receive(b"\x30" + _answer(text))
        assert connection.handshakes == 1

    _run(scenario())
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Join password accepted" in logged
    assert "PasswordProvided" in logged
    assert "len=" not in logged  # the length of the packet gives the length away
    assert SECRET not in logged
    assert SECRET.encode().hex() not in logged.lower().replace(" ", "")


def test_password_packet_after_the_join_is_dropped(clock, caplog):
    async def scenario():
        connection = await _connect(_server(password=""))
        connection.player = SimpleNamespace(id=1)
        connection.in_game = True
        with caplog.at_level(logging.DEBUG):
            await connection.on_receive(b"\x30" + _answer(SECRET))

    _run(scenario())
    assert SECRET not in "\n".join(r.getMessage() for r in caplog.records)


def test_setting_a_password_later_keeps_the_players_inside(clock):
    async def scenario():
        server = _server(password="")
        connection = await _connect(server)
        server.config.join_password = SECRET
        assert await connection.send_connection_data() is True
        assert connection.handshakes == 2
        newcomer = await _connect(server, Peer(port=40009))
        assert _ids(newcomer) == [PasswordNeeded.id]
        newcomer._cancel_join_password_timer()

    _run(scenario())


def test_comparison_is_exact():
    gate = join_password.JoinPasswordGate(SimpleNamespace(join_password="Pässword"))
    assert gate.matches("Pässword")
    assert not gate.matches("pässword")
    assert not gate.matches("Pässword ")
    assert not gate.matches("")
    assert not gate.matches(None)
    assert not join_password.JoinPasswordGate(SimpleNamespace()).enabled


def test_listings_flag_a_password_server():
    for password in ("", SECRET):
        server = BattleSpadesServer(ServerConfig(join_password=password))
        info = parse_a2s_info(A2SHandler(server)._make_info_response())
        assert info["password"] is bool(password)


def test_master_heartbeat_tags_a_password_server(monkeypatch):
    from server.revival_master import RevivalMasterService
    from tests.test_revival_master import make_server

    monkeypatch.setenv("AOS_MASTER_WRITE_TOKEN", "x" * 48)
    for password in ("", SECRET):
        server = make_server()
        server.config.join_password = password
        tags = RevivalMasterService(server).heartbeat_payload()["tags"]
        assert ("password" in tags) is bool(password)
        assert SECRET not in " ".join(tags)


def test_config_reads_and_validates_the_password(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent("""
        [server]
        password = "open-sesame"
        password_max_attempts = 5
        password_timeout_seconds = 30.0
        password_lockout_seconds = 0.0
    """), encoding="utf-8")
    config = load_config(path)
    assert config.join_password == SECRET
    assert config.password_max_attempts == 5
    assert config.password_timeout_seconds == 30.0
    assert config.password_lockout_seconds == 0.0

    assert ServerConfig().join_password == ""
    for bad in ('password = 5', 'password = "%s"' % ("x" * 65)):
        path.write_text("[server]\n" + bad + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config(path)
