"""Trusted local operator channel for a supervising launcher.

A launcher that starts the server with ``--control-stdin`` owns the child's
stdin pipe. Besides the exact ``shutdown`` line, that pipe may carry
``command <text>`` lines. Each one is dispatched to the existing chat-command
handlers as a local console operator with admin rights, because only the
process that started the server can write to its stdin.

Only commands that make sense without an in-game body are accepted (no
``/tp``, ``/kill``, ``/team``...). Replies that the handlers would send to a
player as system chat are written to the ``BattleSpades.console`` log, so the
launcher sees them in the same stream as every other log line and they are
kept in ``server.log`` as an audit trail.

``--status-file PATH`` additionally publishes a small JSON snapshot (state,
population, map, mode, uptime) about once per second. It never contains
passwords or player addresses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("BattleSpades.console")

#: Prefix of a console command line on the control pipe.
COMMAND_PREFIX = b"command "
#: Longest accepted command text (characters, after the prefix).
MAX_COMMAND_CHARS = 1024
#: Commands waiting for dispatch; extra lines are dropped with a warning.
MAX_PENDING_COMMANDS = 32

#: Chat commands a console operator may run. Everything else needs a player
#: body or is a player-only convenience.
CONSOLE_COMMANDS = frozenset({
    "help", "players", "score",
    "kick", "ban", "mute", "unmute", "god",
    "map", "mode", "restart", "endround", "say",
    "fog", "time", "balance", "bots",
    "acreport", "acstats", "lockscore", "infiniteblocks", "netcode",
})
#: Commands implemented here because they only make sense for the operator.
BUILTIN_COMMANDS = frozenset({"status", "banlist", "unban", "commands"})

STATUS_SCHEMA = 1
STATUS_INTERVAL_SECONDS = 1.0
_MAX_STATUS_PLAYERS = 64


class ConsoleOperator:
    """Stand-in ``Player`` for commands typed into the launcher console."""

    id = -1
    player_id = -1
    name = "Console"
    admin = True
    is_bot = False
    team = -1
    muted = False
    god_mode = False
    connection = None

    def __init__(self) -> None:
        self.replies: list[str] = []

    def send(self, data, *_args, **_kwargs) -> None:
        """Decode the system-chat packet a handler sends and log its text."""

        text = _decode_chat(bytes(data))
        if text is None:
            return
        self.replies.append(text)
        logger.info("%s", text)

    def disconnect(self, *_args, **_kwargs) -> None:  # pragma: no cover - guard
        return None


def _decode_chat(data: bytes) -> str | None:
    """Return the text of one ChatMessage packet, or None for anything else."""

    if not data or data[0] != 49:
        return None
    try:
        from shared.bytes import ByteReader
        from shared.packet import ChatMessage

        return str(ChatMessage(ByteReader(data[1:])).value)
    except Exception:  # malformed or unexpected packet: never break a command
        return None


def parse_command_line(line: bytes) -> str | None:
    """Return the command text of one control line, or None if it is not one.

    ``line`` excludes the trailing newline. Text is decoded as UTF-8 with
    replacement, control characters are removed, and over-long text is
    rejected instead of truncated so a cut-off command never runs.
    """

    if not line.startswith(COMMAND_PREFIX):
        return None
    text = line[len(COMMAND_PREFIX):].decode("utf-8", errors="replace")
    text = "".join(ch for ch in text if ch == " " or ch.isprintable()).strip()
    if not text or len(text) > MAX_COMMAND_CHARS:
        return None
    return text.lstrip("/").strip() or None


async def run_console_command(server, text: str) -> list[str]:
    """Run one operator command and return the reply lines it produced."""

    operator = ConsoleOperator()
    parts = text.split(maxsplit=1)
    if not parts:
        return []
    name = parts[0].lower()
    raw_args = parts[1] if len(parts) > 1 else ""
    logger.info("> /%s%s", name, f" {raw_args}" if raw_args else "")

    if name in BUILTIN_COMMANDS:
        for line in _builtin(server, name, raw_args):
            operator.replies.append(line)
            logger.info("%s", line)
        return operator.replies

    from commands.command_handler import get_command

    command = get_command(name)
    if command is None or command.name not in CONSOLE_COMMANDS:
        line = (
            f"Unknown console command: /{name}. Type 'commands' for the list."
            if command is None
            else f"/{command.name} needs an in-game player and is not available here."
        )
        operator.replies.append(line)
        logger.warning("%s", line)
        return operator.replies
    if command.name == "god" and not raw_args.strip():
        line = "Usage: /god <player>"
        operator.replies.append(line)
        logger.info("%s", line)
        return operator.replies
    if not getattr(server, "running", False):
        line = "The server is still starting; try again in a moment."
        operator.replies.append(line)
        logger.info("%s", line)
        return operator.replies

    from commands.command_handler import handle_command

    try:
        await handle_command(server, operator, f"{name} {raw_args}".strip())
    except Exception:
        logger.exception("Console command /%s failed", name)
        operator.replies.append("Command failed; see the log above.")
    return operator.replies


def _builtin(server, name: str, raw_args: str) -> list[str]:
    if name == "commands":
        names = sorted(CONSOLE_COMMANDS | BUILTIN_COMMANDS)
        return ["Console commands: " + ", ".join(names)]
    if name == "status":
        snapshot = build_status(server, state="running" if server.running else "starting")
        return [
            "{name} | {map} ({mode}) | players {humans}+{bots} bots / {max_players} | "
            "port {port} | up {uptime}s".format(
                **{**snapshot, "uptime": int(snapshot["uptime_seconds"])}
            )
        ]
    bans = getattr(getattr(server, "ban_manager", None), "bans", None)
    if bans is None:
        return ["Ban list is unavailable."]
    if name == "banlist":
        if not bans:
            return ["No bans."]
        rows = []
        for address, entry in list(bans.items())[:50]:
            until = entry.get("until") or 0
            when = "permanent" if not until else f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}"
            rows.append(f"{address}  {entry.get('name', '?')}  {when}  {entry.get('reason', '')}")
        return rows
    # unban
    target = raw_args.strip()
    if not target:
        return ["Usage: unban <address|name>"]
    manager = server.ban_manager
    if target in bans:
        manager.remove(target)
        return [f"Unbanned {target}"]
    matches = [ip for ip, entry in bans.items() if str(entry.get("name", "")).lower() == target.lower()]
    if len(matches) == 1:
        manager.remove(matches[0])
        return [f"Unbanned {target} ({matches[0]})"]
    if matches:
        return [f"{len(matches)} bans match {target!r}; unban by address instead."]
    return [f"No ban for {target!r}."]


class CommandDispatcher:
    """Serialise operator commands on the event loop, bounded."""

    def __init__(self, server, loop: asyncio.AbstractEventLoop) -> None:
        self.server = server
        self.loop = loop
        self.queue: asyncio.Queue[str] = asyncio.Queue(MAX_PENDING_COMMANDS)
        self.task: asyncio.Task | None = None

    def submit(self, text: str) -> None:
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            logger.warning("Console command dropped: too many pending commands")
            return
        if self.task is None or self.task.done():
            self.task = self.loop.create_task(self._drain(), name="BattleSpades-console")

    async def _drain(self) -> None:
        while not self.queue.empty():
            text = self.queue.get_nowait()
            await run_console_command(self.server, text)

    async def close(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


def build_status(server, *, state: str, started_at: float | None = None) -> dict[str, Any]:
    """A JSON-safe snapshot of what an operator dashboard shows."""

    config = getattr(server, "config", None)
    from server.steam_master import server_population

    population = server_population(server)
    world = getattr(server, "world_manager", None)
    mode = getattr(server, "mode", None)
    players = []
    for player in list((getattr(server, "players", None) or {}).values())[:_MAX_STATUS_PLAYERS]:
        players.append({
            "id": int(getattr(player, "id", -1)),
            "name": str(getattr(player, "name", "")),
            "team": int(getattr(player, "team", -1)) if isinstance(getattr(player, "team", -1), int) else -1,
            "bot": bool(getattr(player, "is_bot", False)),
            "kills": int(getattr(player, "kills", 0) or 0) if isinstance(getattr(player, "kills", 0), (int, float)) else 0,
        })
    relay = getattr(server, "steam_p2p", None)
    revival = getattr(server, "revival_master", None)
    now = time.time()
    begun = started_at if started_at is not None else getattr(server, "_control_started_at", now)
    return {
        "schema": STATUS_SCHEMA,
        "state": state,
        "pid": os.getpid(),
        "updated_at": now,
        "uptime_seconds": max(0.0, now - begun),
        "name": str(getattr(config, "server_name", getattr(config, "name", ""))),
        "port": int(getattr(config, "port", 0) or 0),
        "max_players": population.max_players,
        "players": population.players,
        "humans": population.humans,
        "bots": population.bots,
        "map": str(getattr(world, "map_name", "") or getattr(config, "default_map", "")),
        "mode": str(getattr(config, "game_mode", "") or ""),
        "mode_name": str(getattr(mode, "name", "") or ""),
        "password": bool(getattr(config, "join_password", "")),
        "steam_p2p": {
            "enabled": bool(getattr(config, "steam_p2p_enabled", False)),
            "hosted": bool(getattr(relay, "hosted", False)),
        },
        "steam_master": {"enabled": bool(getattr(getattr(config, "steam", None), "enabled", False))},
        "revival": {
            "enabled": bool(getattr(revival, "enabled", False)),
            "registering": bool(getattr(revival, "enabled", False) and getattr(revival, "write_token", "")),
        },
        "player_list": players,
    }


def write_status(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically replace ``path``; status I/O must never stop gameplay."""

    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


async def publish_status_loop(server, path: Path, state_ref: dict[str, str]) -> None:
    """Refresh the status file until cancelled."""

    started_at = time.time()
    server._control_started_at = started_at
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            state = state_ref.get("state", "starting")
            if state == "starting" and getattr(server, "running", False):
                state = state_ref["state"] = "running"
            write_status(path, build_status(server, state=state, started_at=started_at))
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a dashboard snapshot stop the server
            logger.debug("Status snapshot failed", exc_info=True)
        await asyncio.sleep(STATUS_INTERVAL_SECONDS)


__all__ = [
    "BUILTIN_COMMANDS",
    "COMMAND_PREFIX",
    "CONSOLE_COMMANDS",
    "CommandDispatcher",
    "ConsoleOperator",
    "build_status",
    "parse_command_line",
    "publish_status_loop",
    "run_console_command",
    "write_status",
]
