"""One private, bounded welcome/MOTD after a client enters the GameScene."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from server.build_info import (
    CLIENT_REPOSITORY,
    PROJECT_WEBSITE,
    SERVER_REPOSITORY,
    BuildInfo,
    runtime_build_info,
)

if TYPE_CHECKING:
    from server.config import ServerConfig
    from server.connection import Connection


DEFAULT_JOIN_GREETING = "Welcome, {player}! {release} | Build (UTC): {build_date}"
DEFAULT_MOTD = (
    f"Dig in. Build big. Server + client: an open AoS Revival project | {PROJECT_WEBSITE}",
    f"Server: {SERVER_REPOSITORY}",
    f"Client: {CLIENT_REPOSITORY}",
)
# Use the more conservative original chat-field limit, in UTF-8 bytes.
MAX_MESSAGE_BYTES = 90
MAX_JOIN_LINES = 8
_TOKEN = re.compile(r"\{(?:player|release|build_date)\}")


def _safe_line(value: str) -> str:
    text = "".join(character for character in value
                   if character.isprintable()).strip()
    return text.encode("utf-8", errors="replace")[:MAX_MESSAGE_BYTES].decode(
        "utf-8", errors="ignore"
    )


def join_message_lines(config: ServerConfig, player_name: str,
                       build: BuildInfo | None = None) -> tuple[str, ...]:
    """Expand supported tokens while preserving operator-supplied text."""

    build = runtime_build_info() if build is None else build
    greeting = str(getattr(config, "join_greeting", DEFAULT_JOIN_GREETING))
    motd = getattr(config, "motd", DEFAULT_MOTD)
    if isinstance(motd, str):
        motd = motd.splitlines()
    substitutions = {
        "{player}": _safe_line(str(player_name)),
        "{release}": build.release,
        "{build_date}": build.date_label,
    }
    lines: list[str] = []
    for template in (greeting, *tuple(motd)):
        for line in str(template).splitlines():
            line = _TOKEN.sub(lambda match: substitutions[match.group(0)], line)
            line = _safe_line(line)
            if line:
                lines.append(line)
                if len(lines) == MAX_JOIN_LINES:
                    return tuple(lines)
    return tuple(lines)


def send_join_greeting(connection: Connection) -> None:
    """Queue this peer's MOTD once, after successful world reveal only."""

    if (
        not connection.in_game
        or connection.player is None
        or getattr(connection, "_join_greeting_sent", False)
    ):
        return
    from shared.packet import ChatMessage
    from server.game_constants import CHAT_SYSTEM

    lines = join_message_lines(connection.server.config, connection.player.name)
    # No repeat on respawn, repeated ClientData or same-peer scene reload.
    connection._join_greeting_sent = True
    for line in lines:
        packet = ChatMessage()
        packet.player_id = 255
        packet.chat_type = CHAT_SYSTEM
        packet.value = line
        connection.send(bytes(packet.generate()), reliable=True)
