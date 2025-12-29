"""
Player commands - available to all players.
"""

from .command_handler import register_command, CommandContext, send_message, get_all_commands


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
        commands = get_all_commands()
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
    
    ctx.player.die(killer=ctx.player, kill_type=5)  # KILL_TEAM_CHANGE (suicide)
    
    # Broadcast kill
    from protocol.packets import KillAction
    packet = KillAction(
        player_id=ctx.player.id,
        killer_id=ctx.player.id,
        kill_type=5,
        respawn_time=int(ctx.server.config.respawn_time),
    )
    ctx.server.broadcast(packet.write())


@register_command(
    name="team",
    usage="/team <blue|green|spectator>",
    description="Change your team",
)
async def cmd_team(ctx: CommandContext):
    """Change team."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /team <blue|green|spectator>")
        return
    
    team_name = ctx.args[0].lower()
    
    team_map = {
        "blue": 0,
        "0": 0,
        "green": 1, 
        "1": 1,
        "spectator": -1,
        "spec": -1,
        "-1": -1,
    }
    
    if team_name not in team_map:
        await send_message(ctx.server, ctx.player, "Invalid team. Use: blue, green, or spectator")
        return
    
    new_team = team_map[team_name]
    old_team = ctx.player.team
    
    if new_team == old_team:
        await send_message(ctx.server, ctx.player, "You're already on that team!")
        return
    
    # Change team
    if old_team in ctx.server.teams:
        ctx.server.teams[old_team].remove_player(ctx.player)
    
    ctx.player.team = new_team
    
    if new_team in ctx.server.teams:
        ctx.server.teams[new_team].add_player(ctx.player)
    
    # Kill player to respawn on new team
    if ctx.player.alive:
        ctx.player.die(kill_type=5)
    
    team_name = ctx.server.teams[new_team].name if new_team in ctx.server.teams else "Spectator"
    await send_message(ctx.server, ctx.player, f"You joined {team_name}")


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
    
    target_name = ctx.args[0]
    message = " ".join(ctx.args[1:])
    
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
    if not ctx.raw_args:
        return
    
    from protocol.packets import ChatMessage
    from aoslib.constants import CHAT_ALL
    
    message = f"* {ctx.player.name} {ctx.raw_args}"
    packet = ChatMessage(player_id=ctx.player.id, chat_type=CHAT_ALL, message=message)
    ctx.server.broadcast(packet.write())


@register_command(
    name="ping",
    usage="/ping",
    description="Show your ping",
)
async def cmd_ping(ctx: CommandContext):
    """Show ping."""
    # TODO: Get actual ping from connection
    await send_message(ctx.server, ctx.player, "Pong!")


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
