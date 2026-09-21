"""Private post-load MOTD, display branding and stable build metadata."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.build_info import (
    BUILD_INFO_FILENAME,
    CLIENT_REPOSITORY,
    DISPLAY_RELEASE,
    PROJECT_WEBSITE,
    SERVER_REPOSITORY,
    BuildInfo,
    load_build_info,
    write_build_info,
)
from server.config import ServerConfig, load_config
from server.game_constants import CHAT_SYSTEM
from server.join_greeting import (
    MAX_JOIN_LINES,
    MAX_MESSAGE_BYTES,
    join_message_lines,
    send_join_greeting,
)
from shared.bytes import ByteReader
from shared.packet import ChatMessage, NewPlayerConnection
from tests.test_reversed_spawn_handshake import DummyServer, make_connection


def test_default_greeting_contains_requested_release_and_verified_project_links():
    config = ServerConfig()
    lines = join_message_lines(config, "Builder", BuildInfo(DISPLAY_RELEASE, "2026-09-21"))
    assert len(lines) == 4
    assert lines[0] == "Welcome, Builder! BattleSpades Beta 0.1 | Build (UTC): 2026-09-21"
    assert "open AoS Revival project" in lines[1]
    assert "Server + client" in lines[1]
    assert PROJECT_WEBSITE in lines[1]
    assert lines[2] == f"Server: {SERVER_REPOSITORY}"
    assert lines[3] == f"Client: {CLIENT_REPOSITORY}"
    assert all(len(line.encode("utf-8")) <= MAX_MESSAGE_BYTES for line in lines)
    assert config.steam.game_version == "1.0.0.0"
    assert config.steam.protocol_version == 168


def test_operator_motd_customization_and_empty_disable_survive_config_loading(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[server]\njoin_greeting = "Hi {player}!"\nmotd = ["Local rules", "Have fun"]\n')
    config = load_config(path)
    assert join_message_lines(config, "Guest") == ("Hi Guest!", "Local rules", "Have fun")
    path.write_text('[server]\njoin_greeting = ""\nmotd = []\n')
    assert join_message_lines(load_config(path), "Guest") == ()


def test_multiline_motd_is_bounded_and_utf8_control_safe(tmp_path):
    path = tmp_path / "server.toml"
    path.write_text('[server]\njoin_greeting = ""\nmotd = """One\nTwo\n"""\n')
    assert join_message_lines(load_config(path), "Guest") == ("One", "Two")
    config = ServerConfig(join_greeting="{player} {unknown}", motd=["Ж" * 100 + "\x00"] * 20)
    lines = join_message_lines(config, "{release}\x00\n")
    assert lines[0] == "{release} {unknown}"  # inserted names are not templates
    assert len(lines) == MAX_JOIN_LINES
    assert all("\x00" not in line and "\n" not in line for line in lines)
    assert all(len(line.encode("utf-8")) <= MAX_MESSAGE_BYTES for line in lines)
    assert all("\ufffd" not in line for line in lines)


@pytest.mark.parametrize("setting", ('motd = 42', 'motd = [1, 2]', 'join_greeting = false'))
def test_invalid_motd_configuration_is_rejected(tmp_path, setting):
    path = tmp_path / "server.toml"
    path.write_text("[server]\n" + setting)
    with pytest.raises(ValueError, match="server\\."):
        load_config(path)


def test_join_dispatch_waits_for_world_reveal_and_never_repeats(monkeypatch):
    server = DummyServer()
    server.config = ServerConfig()
    connection = make_connection(server)
    sent = []
    connection.send = lambda data, **kwargs: sent.append((data, kwargs))
    reveals = iter((False, True, True))
    server.reveal_world_to = lambda peer: next(reveals)
    handled = []

    async def handle(_handler, player, data):
        handled.append(data)

    monkeypatch.setattr("protocol.packet_handler.PacketHandler.handle", handle)
    monkeypatch.setattr("server.join_greeting.runtime_build_info",
                        lambda: BuildInfo(DISPLAY_RELEASE, "2026-09-21"))
    join = NewPlayerConnection()
    join.team = 2
    join.class_id = 0
    join.forced_team = 0
    join.local_language = 0
    join.name = "Builder"

    async def scenario():
        await connection._on_new_player(join)
        assert not any(data[0] == ChatMessage.id for data, _ in sent)
        send_join_greeting(connection)
        assert not connection._join_greeting_sent
        await connection.on_receive(b"\x30\x04")
        assert not connection.in_game
        assert not connection._join_greeting_sent
        await connection.on_receive(b"\x30\x04")
        assert connection.in_game
        assert connection._join_greeting_sent
        await connection.on_receive(b"\x30\x04")
        # Same-peer map reload must not turn the welcome into recurring spam.
        player = connection.player
        connection.player = None
        connection.reset_for_scene_reload()
        connection.player = player
        await connection.on_receive(b"\x30\x04")

    asyncio.run(scenario())
    chats = [(ChatMessage(ByteReader(data[1:])), flags)
             for data, flags in sent if data[0] == ChatMessage.id]
    assert len(chats) == 4
    assert all(packet.player_id == 255 and packet.chat_type == CHAT_SYSTEM
               and flags["reliable"] for packet, flags in chats)
    assert chats[0][0].value.startswith("Welcome, Builder! BattleSpades Beta 0.1")
    assert not any(data[0] == ChatMessage.id for data in server.broadcast_packets)
    assert len(handled) == 3


def test_build_stamp_is_reproducible_and_does_not_use_login_day(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1789948800")  # 2026-09-21 UTC
    stamp = write_build_info(tmp_path / "_internal" / BUILD_INFO_FILENAME)
    original = stamp.read_bytes()
    write_build_info(stamp)
    assert stamp.read_bytes() == original
    metadata = json.loads(original)
    assert metadata["build_date"] == "2026-09-21"
    monkeypatch.setattr("server.build_info.time.time", lambda: 0.0)
    assert load_build_info(tmp_path) == BuildInfo(DISPLAY_RELEASE, "2026-09-21")


def test_source_and_old_binary_fallbacks_are_stable_and_honestly_labeled(tmp_path):
    source = tmp_path / "server" / "build_info.py"
    source.parent.mkdir()
    source.write_text("# source")
    os.utime(source, (1789948800, 1789948800))
    assert load_build_info(tmp_path).date_label == "source 2026-09-21"
    binary = tmp_path / "BattleSpades.exe"
    binary.write_bytes(b"old build")
    os.utime(binary, (1789948800, 1789948800))
    assert load_build_info(tmp_path, binary=binary).date_label == "binary 2026-09-21"


def test_invalid_or_conflicting_stamp_does_not_advertise_fabricated_build_day(tmp_path):
    stamp = tmp_path / BUILD_INFO_FILENAME
    stamp.write_text(json.dumps({"schema": 1, "release": DISPLAY_RELEASE,
                                 "build_epoch": 1789948800, "build_date": "2040-01-01"}))
    assert load_build_info(tmp_path).date_label == "date unknown"


def test_freezer_includes_stable_stamp_without_changing_internal_version():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "BattleSpades.spec").read_text(encoding="utf-8")
    assert "from server.build_info import write_build_info" in spec
    assert 'datas.append((str(build_info_path), "."))' in spec
    assert (root / "VERSION").read_text().strip() == "0.1.0-beta.1"
