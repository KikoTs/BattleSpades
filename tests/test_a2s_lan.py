"""LAN queries use real UDP without competing with game or Steam sockets."""

from __future__ import annotations

import asyncio
import socket
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.check_steam_registration import parse_a2s_info
from server import a2s_query
from server.config import ServerConfig, load_config
from server.main import BattleSpadesServer


def _owner() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind(("0.0.0.0", 0))
    return sock


def _handler(config: ServerConfig) -> a2s_query.A2SHandler:
    return a2s_query.A2SHandler(SimpleNamespace(
        config=config, players={}, world_manager=None, steam_master=None,
    ))


def test_lan_default_old_configs_and_explicit_opt_out(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport=32887\n", encoding="utf-8")
    assert load_config(path).lan_discovery is True
    path.write_text("[server]\nlan_discovery=false\n", encoding="utf-8")
    assert load_config(path).lan_discovery is False
    path.write_text('[server]\nlan_discovery="false"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="lan_discovery must be a boolean"):
        load_config(path)


def test_udp_discovery_falls_back_and_reports_actual_game_port(monkeypatch) -> None:
    async def scenario() -> None:
        with closing(_owner()) as busy, closing(_owner()) as available:
            busy_port = busy.getsockname()[1]
            port = available.getsockname()[1]
            available.close()
            monkeypatch.setattr(a2s_query, "STEAM_LAN_PORTS", (busy_port, port))
            handler = _handler(ServerConfig(port=32887))
            try:
                await handler.start()
                assert handler.lan_query_port == port
                transport = handler._transport
                await handler.start()
                assert handler._transport is transport
                loop = asyncio.get_running_loop()
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.setblocking(False)
                    await loop.sock_connect(client, ("127.0.0.1", port))
                    # Steam's short LAN broadcast gets a direct INFO reply.
                    await loop.sock_sendall(client, b"\xff\xff\xff\xffT")
                    packet = await asyncio.wait_for(loop.sock_recv(client, 4096), 1)
                    info = parse_a2s_info(packet)
                    assert info["game_port"] == 32887
                    assert info["game_id"] == 224540
                    assert info["folder"] == "aceofspades"
                    # Full queries retain the existing challenge exchange.
                    query = b"\xff\xff\xff\xffTSource Engine Query\0"
                    await loop.sock_sendall(client, query)
                    challenge = await asyncio.wait_for(loop.sock_recv(client, 4096), 1)
                    assert challenge[:5] == b"\xff\xff\xff\xffA"
                    await loop.sock_sendall(client, query + challenge[5:9])
                    reply = await asyncio.wait_for(loop.sock_recv(client, 4096), 1)
                    assert reply == packet
                    # HELLOLAN must never advertise a non-game join endpoint.
                    await loop.sock_sendall(client, b"HELLOLAN")
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(loop.sock_recv(client, 4096), 0.05)
            finally:
                handler.stop()
                handler.stop()
                await asyncio.sleep(0)
            assert handler.lan_query_port is None
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
                rebound.bind(("0.0.0.0", port))

    asyncio.run(scenario())


def test_game_port_intercept_needs_no_second_socket(monkeypatch) -> None:
    async def scenario() -> None:
        with closing(_owner()) as game:
            port = game.getsockname()[1]
            monkeypatch.setattr(a2s_query, "STEAM_LAN_PORTS", (port,))
            handler = _handler(ServerConfig(port=port))
            await handler.start()
            assert handler.lan_query_port == port
            assert handler._transport is None
            replies = []
            handler.server.host = SimpleNamespace(socket=SimpleNamespace(
                send=lambda addr, data: replies.append((addr, data)),
            ))
            handler.intercept(("127.0.0.1", 1234), b"\xff\xff\xff\xffT")
            assert parse_a2s_info(replies[0][1])["game_port"] == port
            handler.stop()
            assert game.getsockname()[1] == port  # ENet's socket remains its own.

    asyncio.run(scenario())


def test_disabled_reserved_and_busy_ports_keep_hosting_available(monkeypatch, caplog) -> None:
    async def scenario() -> None:
        with closing(_owner()) as busy, closing(_owner()) as free:
            busy_port, reserved = busy.getsockname()[1], free.getsockname()[1]
            free.close()
            monkeypatch.setattr(a2s_query, "STEAM_LAN_PORTS", (busy_port, reserved))
            config = ServerConfig(port=32887, lan_discovery=False)
            handler = _handler(config)
            await handler.start()
            assert handler._transport is None and handler.lan_query_port is None
            config.lan_discovery = True
            config.steam.enabled = True
            config.steam.query_port = reserved
            await handler.start()
            assert handler._transport is None and handler.lan_query_port is None
            assert "Steam LAN discovery unavailable" in caplog.text
            assert handler.handle_packet(b"HELLO", ("127.0.0.1", 1234)) == b"HI"

    asyncio.run(scenario())


def test_server_shutdown_closes_lan_listener(monkeypatch) -> None:
    async def scenario() -> None:
        with closing(_owner()) as available:
            port = available.getsockname()[1]
            available.close()
            monkeypatch.setattr(a2s_query, "STEAM_LAN_PORTS", (port,))
            server = BattleSpadesServer(ServerConfig(port=32887))
            await server.a2s_handler.start()
            assert server.a2s_handler.lan_query_port == port
            try:
                await server.stop()
                await asyncio.sleep(0)
                assert server.a2s_handler._transport is None
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rebound:
                    rebound.bind(("0.0.0.0", port))
            finally:
                server.a2s_handler.stop()

    asyncio.run(scenario())
