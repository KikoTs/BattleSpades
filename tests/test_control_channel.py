"""Operator console over --control-stdin and the --status-file snapshot."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from server import control_channel, launcher
from server.bans import BanManager


def _server(tmp_path: Path, **extra):
    players = extra.pop("players", {})
    server = SimpleNamespace(
        running=True,
        config=SimpleNamespace(log_commands=False, max_players=24, server_name="Test", port=32887,
                               default_map="MayanJungle", game_mode="tdm", join_password="",
                               steam=SimpleNamespace(enabled=False)),
        players=players,
        connections={},
        teams={},
        get_player_by_name=lambda name: next((p for p in players.values() if p.name.lower().startswith(name.lower())), None),
        ban_manager=BanManager(str(tmp_path / "bans.json")),
        world_manager=SimpleNamespace(map_name="MayanJungle"),
        mode=SimpleNamespace(name="Team Deathmatch"),
    )
    for key, value in extra.items():
        setattr(server, key, value)
    return server


def test_parse_command_line_accepts_only_prefixed_single_commands() -> None:
    assert control_channel.parse_command_line(b"command say hello") == "say hello"
    assert control_channel.parse_command_line(b"command /kick Bob") == "kick Bob"
    assert control_channel.parse_command_line(b"command    ") is None
    assert control_channel.parse_command_line(b"Command say hi") is None
    assert control_channel.parse_command_line(b"say hi") is None
    assert control_channel.parse_command_line(b"command say \x07hi\x1b") == "say hi"
    too_long = b"command " + b"x" * (control_channel.MAX_COMMAND_CHARS + 1)
    assert control_channel.parse_command_line(too_long) is None


def test_decoder_collects_commands_without_changing_shutdown_contract() -> None:
    decoder = launcher._ControlLineDecoder()
    assert not decoder.feed(b"command say one\r\nnoise\ncommand ")
    assert not decoder.feed(b"players\n")
    assert decoder.take_commands() == ["say one", "players"]
    assert decoder.take_commands() == []
    assert decoder.feed(b"command say two\nshutdown\n")
    assert decoder.take_commands() == ["say two"]


def test_decoder_bounds_pending_commands() -> None:
    decoder = launcher._ControlLineDecoder()
    decoder.feed(b"command players\n" * (control_channel.MAX_PENDING_COMMANDS + 10))
    assert len(decoder.take_commands()) == control_channel.MAX_PENDING_COMMANDS


def test_monitor_dispatches_commands_then_shuts_down_over_a_real_pipe() -> None:
    async def run() -> tuple[list[str], list[str]]:
        read_fd, write_fd = os.pipe()
        reasons: list[str] = []
        commands: list[str] = []
        with os.fdopen(read_fd, "rb", buffering=0) as stream:
            os.write(write_fd, b"command say hi there\ncommand status\nshutdown\n")
            task = launcher._start_control_stdin_monitor(
                asyncio.get_running_loop(), reasons.append, stream=stream, on_command=commands.append,
            )
            await asyncio.wait_for(task, timeout=2.0)
        os.close(write_fd)
        return reasons, commands

    reasons, commands = asyncio.run(run())
    assert commands == ["say hi there", "status"]
    assert reasons == ["parent requested shutdown on stdin"]


def test_operator_decodes_system_chat_replies() -> None:
    from shared.packet import ChatMessage

    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = 0
    packet.value = "Player not found: Bob"
    operator = control_channel.ConsoleOperator()
    operator.send(bytes(packet.generate()))
    operator.send(b"\x02junk")
    assert operator.replies == ["Player not found: Bob"]


def test_console_runs_allowed_admin_commands_as_operator(tmp_path: Path) -> None:
    server = _server(tmp_path)
    replies = asyncio.run(control_channel.run_console_command(server, "kick Nobody"))
    assert replies == ["Player not found: Nobody"]


def test_console_refuses_body_commands_and_unknown_commands(tmp_path: Path) -> None:
    server = _server(tmp_path)
    assert "needs an in-game player" in asyncio.run(control_channel.run_console_command(server, "tp 1 2 3"))[0]
    assert "needs an in-game player" in asyncio.run(control_channel.run_console_command(server, "kill"))[0]
    assert "Unknown console command" in asyncio.run(control_channel.run_console_command(server, "rm -rf"))[0]
    assert asyncio.run(control_channel.run_console_command(server, "god"))[0] == "Usage: /god <player>"


def test_console_waits_for_startup(tmp_path: Path) -> None:
    server = _server(tmp_path, running=False)
    assert "still starting" in asyncio.run(control_channel.run_console_command(server, "say hi"))[0]


def test_builtin_banlist_and_unban(tmp_path: Path) -> None:
    server = _server(tmp_path)
    server.ban_manager.add("10.0.0.5", "Griefer", "testing", 0)
    lines = asyncio.run(control_channel.run_console_command(server, "banlist"))
    assert "10.0.0.5" in lines[0] and "Griefer" in lines[0]
    assert asyncio.run(control_channel.run_console_command(server, "unban griefer")) == ["Unbanned griefer (10.0.0.5)"]
    assert server.ban_manager.bans == {}
    assert asyncio.run(control_channel.run_console_command(server, "unban 1.2.3.4")) == ["No ban for '1.2.3.4'."]


def test_status_snapshot_counts_bots_and_never_leaks_secrets(tmp_path: Path) -> None:
    bot = SimpleNamespace(id=0, name="Rocco", team=2, is_bot=True, kills=3, connection=None)
    human = SimpleNamespace(id=1, name="Kiko", team=3, is_bot=False, kills=1)
    server = _server(tmp_path, players={0: bot, 1: human})
    server.config.join_password = "hunter2"
    snapshot = control_channel.build_status(server, state="running", started_at=0.0)
    assert snapshot["humans"] == 1 and snapshot["bots"] == 1 and snapshot["players"] == 2
    assert snapshot["password"] is True
    assert "hunter2" not in json.dumps(snapshot)
    path = tmp_path / "status.json"
    assert control_channel.write_status(path, snapshot)
    assert json.loads(path.read_text(encoding="utf-8"))["map"] == "MayanJungle"


def test_dispatcher_serialises_commands(tmp_path: Path) -> None:
    server = _server(tmp_path)

    async def run() -> None:
        dispatcher = control_channel.CommandDispatcher(server, asyncio.get_running_loop())
        for _ in range(3):
            dispatcher.submit("commands")
        await asyncio.sleep(0.05)
        assert dispatcher.queue.empty()
        await dispatcher.close()

    asyncio.run(run())


def test_status_file_option_is_parsed_and_fleet_rejects_it() -> None:
    arguments = launcher.build_parser().parse_args(["--control-stdin", "--status-file", "s.json"])
    assert arguments.status_file == Path("s.json")
    assert launcher.run(["--fleet", "fleet.toml", "--status-file", "s.json"]) == 2
