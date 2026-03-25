"""
Server commands - admin commands for server management.
"""

from server.game_constants import CHAT_SYSTEM, TEAM1, TEAM2
from shared.packet import ChatMessage, FogColor

from .command_handler import register_command, CommandContext, send_message


@register_command(
    name="map",
    aliases=["changemap"],
    admin_only=True,
    usage="/map <mapname>",
    description="Change the current map",
)
async def cmd_map(ctx: CommandContext):
    """Change map."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, f"Current map: {ctx.server.world_manager.map_name}")
        await send_message(ctx.server, ctx.player, "Usage: /map <mapname>")
        return
    
    map_name = ctx.args[0]
    
    msg = f"Changing map to {map_name}..."
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = msg
    ctx.server.broadcast(bytes(packet.generate()))
    
    # Load new map
    if ctx.server.world_manager.load_map(map_name):
        # Respawn all players
        for player in ctx.server.players.values():
            spawn = ctx.server.world_manager.get_spawn_point(player.team)
            player.spawn(*spawn)
        
        await send_message(ctx.server, ctx.player, f"Map changed to {map_name}")
    else:
        await send_message(ctx.server, ctx.player, f"Failed to load map: {map_name}")


@register_command(
    name="mode",
    aliases=["gamemode"],
    admin_only=True,
    usage="/mode <ctf|tdm|arena>",
    description="Change the game mode",
)
async def cmd_mode(ctx: CommandContext):
    """Change game mode."""
    if not ctx.args:
        current = ctx.server.mode.name if ctx.server.mode else "None"
        await send_message(ctx.server, ctx.player, f"Current mode: {current}")
        await send_message(ctx.server, ctx.player, "Usage: /mode <ctf|tdm|arena>")
        return
    
    mode_name = ctx.args[0].lower()
    
    from modes import get_mode_class
    mode_class = get_mode_class(mode_name)
    
    if not mode_class:
        await send_message(ctx.server, ctx.player, f"Unknown mode: {mode_name}")
        return
    
    # End current mode
    if ctx.server.mode:
        await ctx.server.mode.on_mode_end()
    
    # Start new mode
    ctx.server.mode = mode_class(ctx.server)
    await ctx.server.mode.on_mode_start()
    
    msg = f"Game mode changed to {ctx.server.mode.name}"
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = msg
    ctx.server.broadcast(bytes(packet.generate()))


@register_command(
    name="restart",
    aliases=["reset"],
    admin_only=True,
    usage="/restart",
    description="Restart the current round/match",
)
async def cmd_restart(ctx: CommandContext):
    """Restart round."""
    # Reset teams
    for team in ctx.server.teams.values():
        team.reset()
    
    # Restart mode
    if ctx.server.mode:
        await ctx.server.mode.on_mode_end()
        await ctx.server.mode.on_mode_start()
    
    # Respawn all players
    for player in ctx.server.players.values():
        spawn = ctx.server.world_manager.get_spawn_point(player.team)
        player.spawn(*spawn)
    
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = "Match restarted!"
    ctx.server.broadcast(bytes(packet.generate()))


@register_command(
    name="say",
    admin_only=True,
    usage="/say <message>",
    description="Send a server announcement",
)
async def cmd_say(ctx: CommandContext):
    """Server announcement."""
    if not ctx.raw_args:
        await send_message(ctx.server, ctx.player, "Usage: /say <message>")
        return
    
    message = f"[SERVER] {ctx.raw_args}"
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = message
    ctx.server.broadcast(bytes(packet.generate()))


@register_command(
    name="fog",
    admin_only=True,
    usage="/fog <r> <g> <b>",
    description="Set fog color",
)
async def cmd_fog(ctx: CommandContext):
    """Set fog color."""
    if len(ctx.args) < 3:
        await send_message(ctx.server, ctx.player, "Usage: /fog <r> <g> <b>")
        return
    
    try:
        r = int(ctx.args[0]) & 0xFF
        g = int(ctx.args[1]) & 0xFF
        b = int(ctx.args[2]) & 0xFF
    except ValueError:
        await send_message(ctx.server, ctx.player, "Invalid RGB values")
        return
    
    color = (r << 16) | (g << 8) | b
    
    packet = FogColor()
    packet.color = color
    ctx.server.broadcast(bytes(packet.generate()))
    
    await send_message(ctx.server, ctx.player, f"Fog color set to ({r}, {g}, {b})")


@register_command(
    name="time",
    admin_only=True,
    usage="/time [seconds]",
    description="Show or set remaining time",
)
async def cmd_time(ctx: CommandContext):
    """Show/set time."""
    if ctx.server.mode:
        if ctx.args:
            try:
                ctx.server.mode.time_limit = int(ctx.args[0])
                await send_message(ctx.server, ctx.player, 
                                   f"Time limit set to {ctx.server.mode.time_limit} seconds")
            except ValueError:
                await send_message(ctx.server, ctx.player, "Invalid time value")
        else:
            remaining = ctx.server.mode.time_limit - ctx.server.mode.elapsed_time
            minutes = int(remaining) // 60
            seconds = int(remaining) % 60
            await send_message(ctx.server, ctx.player, 
                               f"Time remaining: {minutes}:{seconds:02d}")
    else:
        await send_message(ctx.server, ctx.player, "No active game mode")


@register_command(
    name="balance",
    admin_only=True,
    usage="/balance",
    description="Force team balance",
)
async def cmd_balance(ctx: CommandContext):
    """Force team balance."""
    team_counts = {
        TEAM1: len(ctx.server.teams[TEAM1].players),
        TEAM2: len(ctx.server.teams[TEAM2].players),
    }
    
    diff = abs(team_counts[TEAM1] - team_counts[TEAM2])
    
    if diff <= 1:
        await send_message(ctx.server, ctx.player, "Teams are already balanced")
        return
    
    # Determine larger team
    larger_team = TEAM1 if team_counts[TEAM1] > team_counts[TEAM2] else TEAM2
    smaller_team = TEAM2 if larger_team == TEAM1 else TEAM1
    
    to_move = diff // 2
    
    # Move players (last joined first)
    moved = 0
    for player in list(ctx.server.teams[larger_team].players):
        if moved >= to_move:
            break
        
        ctx.server.teams[larger_team].remove_player(player)
        player.team = smaller_team
        ctx.server.teams[smaller_team].add_player(player)
        
        if player.alive:
            spawn = ctx.server.world_manager.get_spawn_point(smaller_team)
            player.spawn(*spawn)
        
        await send_message(ctx.server, player, 
                           f"You were moved to {ctx.server.teams[smaller_team].name}")
        moved += 1
    
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = f"Teams balanced ({moved} players moved)"
    ctx.server.broadcast(bytes(packet.generate()))
