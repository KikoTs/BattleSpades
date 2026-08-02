"""Regression coverage for actionable ENet startup failures."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

import server.main as server_main


def test_occupied_udp_port_reports_bind_collision() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as owner:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            owner.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        owner.bind(("", 0))
        port = owner.getsockname()[1]

        with pytest.raises(server_main.ServerBindError) as error:
            server_main._assert_udp_port_available(port)

    message = str(error.value)
    assert f"UDP port {port}" in message
    assert "already in use or reserved" in message
    assert "--port" in message


def test_native_null_host_does_not_escape_as_false_memory_error(monkeypatch) -> None:
    monkeypatch.setattr(server_main, "_assert_udp_port_available", lambda _port: None)

    class NullHostEnet:
        Address = staticmethod(lambda host, port: (host, port))

        @staticmethod
        def Host(_address, **_options):
            raise MemoryError("Unable to create host structure!")

    with pytest.raises(RuntimeError) as error:
        server_main._create_enet_host(
            NullHostEnet,
            port=27015,
            max_connections=32,
        )

    assert not isinstance(error.value, MemoryError)
    assert "late bind collisions and resource failures" in str(error.value)
    assert "max_connections=32" in str(error.value)


def test_enet_host_factory_preserves_reference_transport_settings(monkeypatch) -> None:
    monkeypatch.setattr(server_main, "_assert_udp_port_available", lambda _port: None)
    calls = []
    expected_host = SimpleNamespace()

    class RecordingEnet:
        Address = staticmethod(lambda host, port: (host, port))

        @staticmethod
        def Host(address, **options):
            calls.append((address, options))
            return expected_host

    host = server_main._create_enet_host(
        RecordingEnet,
        port=27015,
        max_connections=50,
    )

    assert host is expected_host
    assert calls == [
        (
            (b"", 27015),
            {
                "peerCount": 50,
                "channelLimit": 1,
                "incomingBandwidth": 0,
                "outgoingBandwidth": 0,
            },
        )
    ]
