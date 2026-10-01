"""Understand the server's log lines (format from server/logging_runtime.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LINE = re.compile(
    r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(?P<level>[A-Z]+)\] "
    r"(?P<logger>[^:]+): (?P<message>.*)$"
)
_STEAM_READY = re.compile(r"Steam relay hosting ready: lobby=(?P<lobby>\S+) virtual_port=(?P<port>\d+)")
_STEAM_STOPPED = re.compile(r"Steam relay hosting stopped|Retrying Steam hosting")
_STARTED = re.compile(r"Server started: ")
_PORT_IN_USE = re.compile(r"address already in use|could not (?:bind|create).*host|only one usage of each socket address", re.I)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class LogLine:
    text: str
    level: str          # one of LEVELS, or "OUTPUT" for unformatted lines
    logger: str = ""
    message: str = ""
    time: str = ""


def parse(text: str, previous_level: str = "INFO") -> LogLine:
    """Classify one line. Traceback continuation lines inherit the level."""

    match = _LINE.match(text)
    if match:
        level = match.group("level")
        if level not in LEVELS:
            level = "INFO"
        return LogLine(text, level, match.group("logger"), match.group("message"), match.group("time"))
    if text.startswith(("Traceback", "  File ", "    ")) or (text and text[0] in " \t"):
        return LogLine(text, previous_level if previous_level in ("ERROR", "CRITICAL", "WARNING") else "OUTPUT", message=text)
    return LogLine(text, "OUTPUT", message=text)


def level_rank(level: str) -> int:
    try:
        return LEVELS.index(level)
    except ValueError:
        return 1  # plain output counts as INFO


@dataclass
class SessionEvents:
    """Facts the dashboard learns from the log stream."""

    started: bool = False
    steam_lobby: str = ""
    steam_virtual_port: int = 0
    port_in_use: bool = False

    def feed(self, line: LogLine) -> None:
        message = line.message or line.text
        if _STARTED.search(message):
            self.started = True
        ready = _STEAM_READY.search(message)
        if ready:
            self.steam_lobby = ready.group("lobby")
            self.steam_virtual_port = int(ready.group("port"))
        elif _STEAM_STOPPED.search(message):
            self.steam_lobby = ""
        if _PORT_IN_USE.search(message):
            self.port_in_use = True


def matches(line: LogLine, *, min_level: str = "DEBUG", search: str = "") -> bool:
    if level_rank(line.level) < level_rank(min_level):
        return False
    return not search or search.casefold() in line.text.casefold()
