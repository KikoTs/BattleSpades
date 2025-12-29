"""
commands - Command System
Player and admin commands with permission handling.
"""

from .command_handler import (
    handle_command,
    register_command,
    Command,
    CommandContext,
)

from .admin import *
from .player import *
from .server_commands import *

__all__ = [
    "handle_command",
    "register_command",
    "Command",
    "CommandContext",
]
