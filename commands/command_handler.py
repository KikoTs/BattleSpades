"""
Command handler - parses and dispatches commands.
"""

import logging
from typing import Callable, Dict, List, Optional, Awaitable, TYPE_CHECKING
from dataclasses import dataclass

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)


@dataclass
class CommandContext:
    """Context passed to command handlers."""
    server: 'BattleSpadesServer'
    player: 'Player'
    args: List[str]
    raw_args: str


class Command:
    """Command definition."""
    
    def __init__(
        self,
        name: str,
        handler: Callable[[CommandContext], Awaitable[None]],
        aliases: List[str] = None,
        admin_only: bool = False,
        usage: str = "",
        description: str = "",
    ):
        self.name = name
        self.handler = handler
        self.aliases = aliases or []
        self.admin_only = admin_only
        self.usage = usage
        self.description = description


# Registered commands
_commands: Dict[str, Command] = {}


def register_command(
    name: str,
    aliases: List[str] = None,
    admin_only: bool = False,
    usage: str = "",
    description: str = "",
):
    """Decorator to register a command."""
    def decorator(func: Callable[[CommandContext], Awaitable[None]]):
        cmd = Command(
            name=name,
            handler=func,
            aliases=aliases or [],
            admin_only=admin_only,
            usage=usage,
            description=description,
        )
        
        # Register by name
        _commands[name.lower()] = cmd
        
        # Register aliases
        for alias in cmd.aliases:
            _commands[alias.lower()] = cmd
        
        return func
    
    return decorator


async def handle_command(server: 'BattleSpadesServer', player: 'Player', message: str):
    """
    Parse and execute a command.
    Message should not include the leading '/'.
    """
    parts = message.split(maxsplit=1)
    if not parts:
        return
    
    cmd_name = parts[0].lower()
    raw_args = parts[1] if len(parts) > 1 else ""
    args = raw_args.split() if raw_args else []
    
    command = _commands.get(cmd_name)
    
    if not command:
        await send_message(server, player, f"Unknown command: /{cmd_name}")
        return
    
    # Check permissions
    if command.admin_only and not player.admin:
        await send_message(server, player, "You don't have permission to use this command.")
        return
    
    # Create context
    context = CommandContext(
        server=server,
        player=player,
        args=args,
        raw_args=raw_args,
    )
    
    try:
        await command.handler(context)
        
        if server.config.log_commands:
            logger.info(f"Command: {player.name} used /{cmd_name} {raw_args}")
    except Exception as e:
        logger.error(f"Command error: {cmd_name} - {e}", exc_info=True)
        await send_message(server, player, f"Command error: {e}")


async def send_message(server: 'BattleSpadesServer', player: 'Player', message: str):
    """Send a system message to a specific player."""
    from protocol.packets import ChatMessage
    from aoslib.constants import CHAT_SYSTEM
    
    packet = ChatMessage(player_id=255, chat_type=CHAT_SYSTEM, message=message)
    player.send(packet.write())


def get_command(name: str) -> Optional[Command]:
    """Get a command by name."""
    return _commands.get(name.lower())


def get_all_commands() -> List[Command]:
    """Get all unique registered commands."""
    seen = set()
    commands = []
    for cmd in _commands.values():
        if cmd.name not in seen:
            seen.add(cmd.name)
            commands.append(cmd)
    return commands
