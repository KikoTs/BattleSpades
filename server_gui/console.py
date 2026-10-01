"""Operator command palette: only commands the console channel really runs.

Each entry maps to a chat command in ``commands/`` (allow-listed by
``server.control_channel.CONSOLE_COMMANDS``) or to a console built-in.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.control_channel import BUILTIN_COMMANDS, CONSOLE_COMMANDS, MAX_COMMAND_CHARS


@dataclass(frozen=True)
class PaletteEntry:
    label: str
    template: str            # text with {placeholders} the operator fills in
    fields: tuple[str, ...]  # placeholder names, in order
    help: str
    category: str

    @property
    def command(self) -> str:
        return self.template.split()[0]


PALETTE: tuple[PaletteEntry, ...] = (
    PaletteEntry("Status", "status", (), "Map, mode, players and uptime", "Server"),
    PaletteEntry("Players", "players", (), "Who is on which team", "Server"),
    PaletteEntry("Scores", "score", (), "Team scores", "Server"),
    PaletteEntry("Broadcast", "say {message}", ("message",), "Announce a message to everyone", "Server"),
    PaletteEntry("Restart round", "restart", (), "Restart the current round on the same map", "Match"),
    PaletteEntry("End round", "endround", (), "End the round now (leader wins)", "Match"),
    PaletteEntry("Change map", "map {map}", ("map",), "Switch every player to another map", "Match"),
    PaletteEntry("Change mode", "mode {mode}", ("mode",), "Switch game mode (tdm, ctf, zom...)", "Match"),
    PaletteEntry("Time left", "time {seconds}", ("seconds",), "Set the remaining round time", "Match"),
    PaletteEntry("Balance teams", "balance", (), "Move dead players to even the teams", "Match"),
    PaletteEntry("Kick", "kick {player} {reason}", ("player", "reason"), "Disconnect a player", "Players"),
    PaletteEntry("Ban", "ban {player} {duration} {reason}", ("player", "duration", "reason"),
                 "Ban by address; duration like 30m, 2h, 1d or perma", "Players"),
    PaletteEntry("Unban", "unban {player}", ("player",), "Lift a ban by address or name", "Players"),
    PaletteEntry("Ban list", "banlist", (), "Show active bans", "Players"),
    PaletteEntry("Mute", "mute {player}", ("player",), "Silence a player's chat", "Players"),
    PaletteEntry("Unmute", "unmute {player}", ("player",), "Allow a player to chat again", "Players"),
    PaletteEntry("Bot status", "bots status", (), "Bot count and worker health", "Bots"),
    PaletteEntry("Add bots", "bots add {count}", ("count",), "Add bots now", "Bots"),
    PaletteEntry("Remove bots", "bots remove {count}", ("count",), "Remove bots (number or all)", "Bots"),
    PaletteEntry("Fill with bots", "bots fill {count}", ("count",), "Keep the server filled to this many players", "Bots"),
    PaletteEntry("Bot difficulty", "bots difficulty {level}", ("level",), "casual, normal, hard or mixed", "Bots"),
    PaletteEntry("Anti-cheat report", "acreport", (), "Suspicion scores (detection only)", "Admin"),
    PaletteEntry("Help", "commands", (), "List every console command", "Admin"),
)


def fill(entry: PaletteEntry, values: dict[str, str]) -> str:
    """Build the command line; optional trailing fields may be left empty."""

    text = entry.template
    for name in entry.fields:
        text = text.replace("{" + name + "}", str(values.get(name, "")).strip())
    return " ".join(text.split())


def validate_command(text: str) -> str | None:
    """Return an error for text the console channel would refuse, else None."""

    stripped = text.strip().lstrip("/")
    if not stripped:
        return "Type a command."
    if "\n" in text or "\r" in text:
        return "Commands must be a single line."
    if len(stripped) > MAX_COMMAND_CHARS:
        return f"Commands are limited to {MAX_COMMAND_CHARS} characters."
    name = stripped.split()[0].lower()
    if name not in CONSOLE_COMMANDS | BUILTIN_COMMANDS and name not in _ALIASES:
        return f"Unknown console command '{name}'. Type 'commands' for the list."
    return None


# Aliases registered by the command modules for allowed commands.
_ALIASES = frozenset({"k", "b", "changemap", "gamemode", "reset", "endgame", "forceend", "?", "who", "list", "scores", "nc", "infblocks"})
