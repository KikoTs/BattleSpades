"""Per-player /music plugin regression tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from commands.command_handler import get_command, handle_command
from plugins.music import MusicPlugin
from modes.base_mode import BaseMode
from server import audio
from server.config import ServerConfig
from server.audio import GAMEPLAY_TRACKS
from shared.bytes import ByteReader
from shared.packet import ChatMessage, PlayMusic


_AUDIO_PACKET_IDS = frozenset((23, 26, 27))


class _Connection:
    def __init__(self, *, in_game=True):
        self.in_game = bool(in_game)
        self.sent = []

    def send(self, data, reliable=True):
        self.sent.append(bytes(data))


class _Player:
    def __init__(self, player_id, name, *, in_game=True):
        self.id = int(player_id)
        self.name = str(name)
        self.admin = False
        self.connection = _Connection(in_game=in_game)

    def send(self, data, reliable=True):
        self.connection.send(data, reliable=reliable)


class _Server:
    def __init__(self):
        self.config = SimpleNamespace(
            mode_start_music=False,
            log_commands=False,
        )
        self.mode = SimpleNamespace(
            started=True,
            ended=False,
            retiring=False,
            start_time=1000.0,
        )
        self.players = {}
        self.audio_music_family = "global-sentinel"


def _audio_packets(player):
    return [
        data for data in player.connection.sent
        if data and data[0] in _AUDIO_PACKET_IDS
    ]


def _chat_values(player):
    values = []
    for data in player.connection.sent:
        if data and data[0] == ChatMessage.id:
            values.append(ChatMessage(ByteReader(data[1:])).value)
    return values


@pytest.fixture()
def loaded_plugin():
    assert get_command("music") is None
    server = _Server()
    player = _Player(1, "Listener")
    other = _Player(2, "Other")
    server.players = {player.id: player, other.id: other}
    plugin = MusicPlugin(server)
    asyncio.run(plugin.on_load())
    try:
        yield plugin, server, player, other
    finally:
        asyncio.run(plugin.on_unload())


def test_play_is_personal_and_works_when_global_mode_music_is_disabled(
    loaded_plugin,
):
    _plugin, server, player, other = loaded_plugin

    asyncio.run(handle_command(server, player, "music play"))

    audio = _audio_packets(player)
    assert [packet[0] for packet in audio] == [23, 27, 26]
    assert PlayMusic(ByteReader(audio[-1][1:])).name in GAMEPLAY_TRACKS
    assert _audio_packets(other) == []
    assert server.audio_music_family == "global-sentinel"
    assert _chat_values(player)[-1].startswith("Music started")


def test_stop_is_personal_and_removes_round_restart_preference(loaded_plugin):
    plugin, server, player, other = loaded_plugin
    asyncio.run(handle_command(server, player, "music play"))
    player.connection.sent.clear()

    asyncio.run(handle_command(server, player, "music stop"))

    assert [packet[0] for packet in _audio_packets(player)] == [27]
    assert _audio_packets(other) == []
    assert server.audio_music_family == "global-sentinel"
    assert _chat_values(player)[-1] == "Music stopped."

    player.connection.sent.clear()
    server.mode.start_time += 1.0
    asyncio.run(plugin.on_tick(1))
    assert _audio_packets(player) == []


def test_opt_in_music_returns_after_new_round_global_stop(loaded_plugin):
    plugin, server, player, _other = loaded_plugin
    asyncio.run(handle_command(server, player, "music PLAY"))
    player.connection.sent.clear()

    server.mode.start_time += 1.0
    asyncio.run(plugin.on_tick(1))

    audio = _audio_packets(player)
    assert [packet[0] for packet in audio] == [23, 27, 26]
    assert PlayMusic(ByteReader(audio[-1][1:])).name in GAMEPLAY_TRACKS


def test_play_before_new_round_tick_does_not_skip_another_listener(loaded_plugin):
    plugin, server, player, other = loaded_plugin
    asyncio.run(handle_command(server, player, "music play"))
    asyncio.run(handle_command(server, other, "music play"))
    player.connection.sent.clear()
    other.connection.sent.clear()

    server.mode.start_time += 1.0
    asyncio.run(handle_command(server, player, "music play"))
    player.connection.sent.clear()
    asyncio.run(plugin.on_tick(1))

    assert _audio_packets(player) == []
    assert [packet[0] for packet in _audio_packets(other)] == [23, 27, 26]


def test_bare_music_toggles_only_its_listener_and_never_revives_after_stop(loaded_plugin):
    plugin, server, player, other = loaded_plugin
    asyncio.run(handle_command(server, player, "music"))
    assert [p[0] for p in _audio_packets(player)] == [23, 27, 26]
    player.connection.sent.clear()
    asyncio.run(handle_command(server, player, "music"))
    assert [p[0] for p in _audio_packets(player)] == [27]
    player.connection.sent.clear()
    server.mode.start_time += 1
    asyncio.run(plugin.on_tick(1))
    assert _audio_packets(player) == _audio_packets(other) == []


@pytest.mark.parametrize("command", ("music pause", "music play extra"))
def test_invalid_music_syntax_only_returns_usage(loaded_plugin, command):
    _plugin, server, player, _other = loaded_plugin

    asyncio.run(handle_command(server, player, command))

    assert _audio_packets(player) == []
    assert _chat_values(player)[-1] == "Usage: /music [play|stop]"


def test_disabled_or_unsettled_plugin_does_not_send_audio(loaded_plugin):
    plugin, server, player, _other = loaded_plugin
    plugin.enabled = False
    asyncio.run(handle_command(server, player, "music play"))
    assert _audio_packets(player) == []
    assert _chat_values(player)[-1] == "Music plugin is disabled."

    plugin.enabled = True
    player.connection.sent.clear()
    player.connection.in_game = False
    asyncio.run(handle_command(server, player, "music play"))
    assert _audio_packets(player) == []
    assert _chat_values(player)[-1] == (
        "Music is available after entering the game."
    )


def test_unload_removes_only_the_registered_music_command():
    assert get_command("music") is None
    server = _Server()
    plugin = MusicPlugin(server)
    asyncio.run(plugin.on_load())
    registered = get_command("music")
    assert registered is not None
    asyncio.run(plugin.on_unload())
    assert get_command("music") is None


@pytest.mark.parametrize("config", (None, SimpleNamespace(), ServerConfig()))
def test_default_music_stays_silent_through_all_round_phases_and_late_join(config):
    server = _Server()
    server.config = config
    server.broadcast = lambda data: sent.append(bytes(data))
    sent = []
    assert not audio.mode_start_music_enabled(server)
    audio.play_gameplay_music(server)
    assert audio.play_last_man_music(server) is False
    audio.play_timeout_music(server)
    assert audio.play_ending_music(server) is False
    assert all(packet[0] != PlayMusic.id for packet in sent)
    for ended in (False, True):
        mode = SimpleNamespace(server=server, started=True, ended=ended,
                               _end_sequence_running=True, retiring=False,
                               _timeout_music_played=True)
        assert BaseMode.join_music_track(mode) is None


def test_round_cues_cannot_restart_or_replace_personal_music(loaded_plugin):
    plugin, server, player, other = loaded_plugin
    server.broadcast = lambda data: [p.send(data) for p in server.players.values()]
    asyncio.run(handle_command(server, player, "music"))
    player.connection.sent.clear()
    audio.play_timeout_music(server)
    audio.play_last_man_music(server)
    audio.play_ending_music(server)
    assert _audio_packets(player) == _audio_packets(other) == []
    asyncio.run(handle_command(server, player, "music stop"))
    player.connection.sent.clear()
    audio.play_last_man_music(server)
    asyncio.run(plugin.on_tick(5))
    assert _audio_packets(player) == []


def test_opt_in_survives_an_in_place_stop_without_mode_or_clock_change(loaded_plugin):
    plugin, server, player, other = loaded_plugin
    server.broadcast = lambda data: [p.send(data) for p in server.players.values()]
    asyncio.run(handle_command(server, player, "music"))
    player.connection.sent.clear()
    audio.play_gameplay_music(server)
    asyncio.run(plugin.on_tick(1))
    assert [p[0] for p in _audio_packets(player)] == [27, 23, 27, 26]
    assert [p[0] for p in _audio_packets(other)] == [27]
    player.connection.sent.clear()
    other.connection.sent.clear()
    asyncio.run(plugin.on_tick(2))
    assert _audio_packets(player) == _audio_packets(other) == []
