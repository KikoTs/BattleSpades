"""
Player commands - available to all players.
"""

from server.game_constants import (
    CHAT_ALL,
    KILL_CLASS_CHANGE,
    TEAM1,
    TEAM2,
    TEAM_SPECTATOR,
)
import shared.constants as C
from shared.packet import ChatMessage, KillAction

from .command_handler import register_command, CommandContext, send_message, get_all_commands

# Player-authored text relayed by /pm and /me is bounded like ordinary chat
# (the stock chat box's MAX_CHAT_MESSAGE_LENGTH).
_CHAT_TEXT_LIMIT = int(getattr(C, "MAX_CHAT_MESSAGE_LENGTH", 200))


@register_command(
    name="help",
    aliases=["?", "commands"],
    usage="/help [command]",
    description="Show available commands",
)
async def cmd_help(ctx: CommandContext):
    """Show help for commands."""
    if ctx.args:
        # Show help for specific command
        from .command_handler import get_command
        cmd = get_command(ctx.args[0])
        
        if cmd:
            await send_message(ctx.server, ctx.player, f"/{cmd.name}: {cmd.description}")
            if cmd.usage:
                await send_message(ctx.server, ctx.player, f"Usage: {cmd.usage}")
            if cmd.aliases:
                await send_message(ctx.server, ctx.player, f"Aliases: {', '.join(cmd.aliases)}")
        else:
            await send_message(ctx.server, ctx.player, f"Unknown command: {ctx.args[0]}")
    else:
        # Show all commands
        commands = [c for c in get_all_commands() if not getattr(c, "hidden", False)]
        player_cmds = [c for c in commands if not c.admin_only]
        admin_cmds = [c for c in commands if c.admin_only]
        
        await send_message(ctx.server, ctx.player, "Commands:")
        cmd_names = ", ".join(f"/{c.name}" for c in player_cmds)
        await send_message(ctx.server, ctx.player, cmd_names)
        
        if ctx.player.admin and admin_cmds:
            await send_message(ctx.server, ctx.player, "Admin commands:")
            admin_names = ", ".join(f"/{c.name}" for c in admin_cmds)
            await send_message(ctx.server, ctx.player, admin_names)


@register_command(
    name="kill",
    aliases=["suicide"],
    usage="/kill",
    description="Kill yourself",
)
async def cmd_kill(ctx: CommandContext):
    """Suicide command."""
    if not ctx.player.alive:
        await send_message(ctx.server, ctx.player, "You're already dead!")
        return
    
    # Player.die() already broadcasts the KillAction; don't send a second
    # one here (that double-fired the death packet to every client). A
    # suicide right after enemy fire is that enemy's kill, not a free
    # transition death (end_life_for_transition credits it).
    #
    # CLASS_CHANGE_KILL, not TEAM_CHANGE_KILL: the stock
    # process_packet_kill_action clears dominatingLocalPlayer /
    # dominatedByLocalPlayer on TEAM_CHANGE kills (and the feed shows the
    # team-change icon), so /kill would let a dominated player wipe the
    # domination icon and the pending revenge.  The client does not treat
    # CLASS_CHANGE_KILL as a relation reset, and neither does the server
    # (kill_feed.DOMINATION_RESET_KILL_TYPES).  Retail had no player
    # suicide command; a class change is the closest retail action.
    from server.handlers.team import end_life_for_transition

    end_life_for_transition(ctx.server, ctx.player, KILL_CLASS_CHANGE)


@register_command(
    name="team",
    usage="/team <team1|team2|spectator>",
    description="Change your team",
)
async def cmd_team(ctx: CommandContext):
    """Change team through the same rules as the retail ChangeTeam packet."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /team <team1|team2|spectator>")
        return

    team_name = ctx.args[0].lower()

    team_map = {
        "team1": TEAM1,
        str(TEAM1): TEAM1,
        "team2": TEAM2,
        str(TEAM2): TEAM2,
        "spectator": TEAM_SPECTATOR,
        "spec": TEAM_SPECTATOR,
        str(TEAM_SPECTATOR): TEAM_SPECTATOR,
    }

    if team_name not in team_map:
        await send_message(ctx.server, ctx.player, "Invalid team. Use: team1, team2, or spectator")
        return

    new_team = team_map[team_name]
    # One code path for the packet and the command: cooldown, auto-balance,
    # mode team locks, deployable retirement, spectator roster, kill credit
    # and on_player_team_change all live in change_team.
    from server.handlers.team import change_team

    if not change_team(ctx.server, ctx.player, new_team, explain=True):
        return

    label = {TEAM1: "team 1", TEAM2: "team 2"}.get(new_team, "the spectators")
    await send_message(ctx.server, ctx.player, f"You joined {label}")


@register_command(
    name="score",
    aliases=["scores"],
    usage="/score",
    description="Show current scores",
)
async def cmd_score(ctx: CommandContext):
    """Show scores."""
    for team_id, team in ctx.server.teams.items():
        await send_message(
            ctx.server, ctx.player,
            f"{team.name}: {team.score} points ({team.player_count} players)"
        )


@register_command(
    name="players",
    aliases=["who", "list"],
    usage="/players",
    description="List all players",
)
async def cmd_players(ctx: CommandContext):
    """List all players."""
    await send_message(ctx.server, ctx.player, 
                       f"Players ({len(ctx.server.players)}/{ctx.server.config.max_players}):")
    
    for team_id, team in ctx.server.teams.items():
        players = [p.name for p in team.players]
        if players:
            await send_message(ctx.server, ctx.player, f"{team.name}: {', '.join(players)}")


@register_command(
    name="pm",
    aliases=["msg", "whisper", "w"],
    usage="/pm <player> <message>",
    description="Send a private message",
)
async def cmd_pm(ctx: CommandContext):
    """Send private message."""
    if len(ctx.args) < 2:
        await send_message(ctx.server, ctx.player, "Usage: /pm <player> <message>")
        return
    
    if getattr(ctx.player, "muted", False):
        # Mute covers every player-to-player channel, not just public chat.
        await send_message(ctx.server, ctx.player, "You are muted.")
        return

    target_name = ctx.args[0]
    message = " ".join(ctx.args[1:])[:_CHAT_TEXT_LIMIT]
    
    target = ctx.server.get_player_by_name(target_name)
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    await send_message(ctx.server, target, f"[PM from {ctx.player.name}]: {message}")
    await send_message(ctx.server, ctx.player, f"[PM to {target.name}]: {message}")


@register_command(
    name="me",
    usage="/me <action>",
    description="Describe an action",
)
async def cmd_me(ctx: CommandContext):
    """Action message."""
    action = ctx.raw_args.strip()
    if not action or getattr(ctx.player, "muted", False):
        # /me is public chat: a muted player must not reach it this way.
        return

    # The stock HUD.create_line always prefixes a player-sent line with the
    # sender's "Name: " (team colour), so the text carries only the action:
    # clients render "Name: * waves" instead of "Name: * Name waves".
    message = f"* {action}"[:_CHAT_TEXT_LIMIT]
    packet = ChatMessage()
    packet.player_id = ctx.player.id
    packet.chat_type = CHAT_ALL
    packet.value = message
    # Same delivery as ordinary chat: only in-game peers that know the
    # sender (the retail HUD resolves player_id through its roster).
    from server.handlers.social import _relay_chat

    _relay_chat(ctx.server, ctx.player, bytes(packet.generate()), False)


@register_command(
    name="ping",
    usage="/ping",
    description="Show your ping",
)
async def cmd_ping(ctx: CommandContext):
    """Show ping (ENet round-trip time)."""
    rtt = None
    conn = getattr(ctx.player, "connection", None)
    peer = getattr(conn, "peer", None) if conn else None
    if peer is not None:
        # pyenet exposes the smoothed RTT in milliseconds.
        rtt = getattr(peer, "roundTripTime", None)
    if rtt is None:
        await send_message(ctx.server, ctx.player, "Ping unavailable (no active connection).")
    else:
        await send_message(ctx.server, ctx.player, f"Your ping: {int(rtt)} ms")


@register_command(
    name="stats",
    usage="/stats [player]",
    description="Show player stats",
)
async def cmd_stats(ctx: CommandContext):
    """Show player stats."""
    if ctx.args:
        target = ctx.server.get_player_by_name(ctx.args[0])
        if not target:
            await send_message(ctx.server, ctx.player, f"Player not found: {ctx.args[0]}")
            return
    else:
        target = ctx.player
    
    kd = target.kills / max(1, target.deaths)
    await send_message(ctx.server, ctx.player, 
                       f"{target.name} - Kills: {target.kills}, Deaths: {target.deaths}, K/D: {kd:.2f}")
