"""Transport parity with the retail client (audit3 network L7/L10, 2026-09-28).

* The ClockSync reply is unsequenced (retail sends the request unsequenced).
* ENet connect data must be protocol 168 (retail ``game_version()`` and the
  native client); anything else is refused with the retail version reasons.
* A timed ban is refused with ERROR_TEMP_BANNED (19), a permanent one with
  ERROR_BANNED (1).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from server.connection import Connection  # noqa: E402
from server.main import BattleSpadesServer, protocol_version_refusal  # noqa: E402
from server.config import ServerConfig  # noqa: E402


class _Peer:
    address = "127.0.0.1:5000"

    def __init__(self):
        self.disconnects = []

    def disconnect(self, reason=0):
        self.disconnects.append(int(reason))


def _fake_enet(monkeypatch, allocated):
    def packet(data, flags):
        allocated.append((data, flags))
        return data

    monkeypatch.setitem(sys.modules, "enet", SimpleNamespace(
        Packet=packet, PACKET_FLAG_RELIABLE=1, PACKET_FLAG_UNSEQUENCED=2,
    ))


def test_clock_sync_reply_is_unsequenced(monkeypatch):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=[], packet_trace=False,
                               clock_sync_loop_bias=1),
        loop_count=500,
    )
    sent = []
    transport = SimpleNamespace(send=lambda channel, data: sent.append(channel))
    connection = Connection(transport, server)
    connection.send_clock_sync_response(1234)
    assert allocated[-1][1] == 2 and sent == [0]
    # Ordinary sends keep their flags.
    connection.send(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0]), reliable=True)
    assert allocated[-1][1] == 1
    connection.send(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0]), reliable=False)
    assert allocated[-1][1] == 0


def test_protocol_version_refusal_uses_the_retail_reasons():
    assert protocol_version_refusal(168) is None
    assert protocol_version_refusal(167) == 10  # ERROR_CLIENT_OUT_OF_DATE
    assert protocol_version_refusal(0) == 10
    assert protocol_version_refusal(169) == 3  # ERROR_SERVER_OUT_OF_DATE
    assert protocol_version_refusal(None) == 10


def _server(ban=None, **config):
    return SimpleNamespace(
        config=SimpleNamespace(**config),
        ban_manager=SimpleNamespace(is_banned=lambda _host: ban),
        vote_manager=None,
        connections={},
    )


def test_mismatched_connect_data_is_refused_before_any_state():
    peer = _Peer()
    server = _server()
    BattleSpadesServer._on_connect_sync(server, peer, 150)
    assert peer.disconnects == [10] and server.connections == {}
    peer = _Peer()
    BattleSpadesServer._on_connect_sync(server, peer, 200)
    assert peer.disconnects == [3]
    # The switch turns the check off for odd tooling.
    peer = _Peer()
    lenient = _server(ban={"reason": "x", "until": 0},
                      require_protocol_version=False)
    BattleSpadesServer._on_connect_sync(lenient, peer, 150)
    assert peer.disconnects == [1]


def test_bans_refuse_with_temp_or_permanent_reason():
    peer = _Peer()
    BattleSpadesServer._on_connect_sync(
        _server(ban={"reason": "x", "until": 9e18}), peer, 168)
    assert peer.disconnects == [19]
    peer = _Peer()
    BattleSpadesServer._on_connect_sync(
        _server(ban={"reason": "x", "until": 0}), peer, 168)
    assert peer.disconnects == [1]


def test_dead_network_and_water_keys_are_accepted_but_ignored(tmp_path, caplog):
    import logging

    from server.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(
        "[network]\ntimeout_ms = 3000\nbandwidth_limit = 5\n"
        "[world]\nwater_level = 200\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="server.config")
    config = load_config(path)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "timeout_ms is ignored" in messages
    assert "bandwidth_limit is ignored" in messages
    assert "water_level/water_damage are ignored" in messages
    assert config.require_protocol_version is True
    assert ServerConfig().require_protocol_version is True


def test_voice_data_payload_is_never_utf8_decoded():
    """VoiceData(103) is out of scope, but must never be corrupted: the
    runtime decoder keeps it as raw bytes and no handler relays it."""
    from protocol.packet_handler import _handlers
    from protocol.runtime_packets import decode_runtime_packet

    frame = bytes(range(256))[:200]
    payload = bytes([7]) + len(frame).to_bytes(2, "little") + frame
    packet = decode_runtime_packet(103, payload)
    assert packet.player_id == 7 and packet.data_size == 200
    assert packet.data == frame  # byte-for-byte, invalid UTF-8 included
    assert 103 not in _handlers
