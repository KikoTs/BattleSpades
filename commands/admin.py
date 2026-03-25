"""
Admin commands - requires admin permission.
"""

from server.game_constants import CHAT_SYSTEM
from shared.packet import ChatMessage

from .command_handler import register_command, CommandContext, send_message


@register_command(
    name="kick",
    aliases=["k"],
    admin_only=True,
    usage="/kick <player> [reason]",
    description="Kick a player from the server",
)
async def cmd_kick(ctx: CommandContext):
    """Kick a player."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /kick <player> [reason]")
        return
    
    target_name = ctx.args[0]
    reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "Kicked by admin"
    
    target = ctx.server.get_player_by_name(target_name)
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    msg = f"{target.name} was kicked: {reason}"
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = msg
    ctx.server.broadcast(bytes(packet.generate()))
    
    target.disconnect(reason=2)  # DISCONNECT_KICKED


@register_command(
    name="ban",
    aliases=["b"],
    admin_only=True,
    usage="/ban <player> [duration] [reason]",
    description="Ban a player from the server",
)
async def cmd_ban(ctx: CommandContext):
    """Ban a player."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /ban <player> [duration] [reason]")
        return
    
    target_name = ctx.args[0]
    # TODO: Parse duration and reason
    reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "Banned by admin"
    
    target = ctx.server.get_player_by_name(target_name)
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    # TODO: Add to ban list
    
    msg = f"{target.name} was banned: {reason}"
    packet = ChatMessage()
    packet.player_id = 255
    packet.chat_type = CHAT_SYSTEM
    packet.value = msg
    ctx.server.broadcast(bytes(packet.generate()))
    
    target.disconnect(reason=1)  # DISCONNECT_BANNED


@register_command(
    name="mute",
    admin_only=True,
    usage="/mute <player>",
    description="Mute a player",
)
async def cmd_mute(ctx: CommandContext):
    """Mute a player."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /mute <player>")
        return
    
    target_name = ctx.args[0]
    target = ctx.server.get_player_by_name(target_name)
    
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    target.muted = True
    await send_message(ctx.server, ctx.player, f"Muted {target.name}")
    await send_message(ctx.server, target, "You have been muted by an admin.")


@register_command(
    name="unmute",
    admin_only=True,
    usage="/unmute <player>",
    description="Unmute a player",
)
async def cmd_unmute(ctx: CommandContext):
    """Unmute a player."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /unmute <player>")
        return
    
    target_name = ctx.args[0]
    target = ctx.server.get_player_by_name(target_name)
    
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    target.muted = False
    await send_message(ctx.server, ctx.player, f"Unmuted {target.name}")
    await send_message(ctx.server, target, "You have been unmuted.")


@register_command(
    name="tp",
    aliases=["teleport"],
    admin_only=True,
    usage="/tp <player> [target] or /tp <x> <y> <z>",
    description="Teleport a player",
)
async def cmd_teleport(ctx: CommandContext):
    """Teleport player(s)."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /tp <player> [target] or /tp <x> <y> <z>")
        return
    
    # Try to parse as coordinates
    if len(ctx.args) >= 3:
        try:
            x = float(ctx.args[0])
            y = float(ctx.args[1])
            z = float(ctx.args[2])
            
            ctx.player.set_position(x, y, z)
            await send_message(ctx.server, ctx.player, f"Teleported to ({x}, {y}, {z})")
            return
        except ValueError:
            pass
    
    # Parse as player teleport
    target_name = ctx.args[0]
    target = ctx.server.get_player_by_name(target_name)
    
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return
    
    if len(ctx.args) >= 2:
        # Teleport target to another player
        dest_name = ctx.args[1]
        dest = ctx.server.get_player_by_name(dest_name)
        
        if not dest:
            await send_message(ctx.server, ctx.player, f"Player not found: {dest_name}")
            return
        
        target.set_position(dest.x, dest.y, dest.z)
        await send_message(ctx.server, ctx.player, f"Teleported {target.name} to {dest.name}")
    else:
        # Teleport self to target
        ctx.player.set_position(target.x, target.y, target.z)
        await send_message(ctx.server, ctx.player, f"Teleported to {target.name}")


@register_command(
    name="god",
    admin_only=True,
    usage="/god [player]",
    description="Toggle god mode",
)
async def cmd_god(ctx: CommandContext):
    """Toggle god mode."""
    if ctx.args:
        target = ctx.server.get_player_by_name(ctx.args[0])
        if not target:
            await send_message(ctx.server, ctx.player, f"Player not found: {ctx.args[0]}")
            return
    else:
        target = ctx.player
    
    # TODO: Implement god mode flag
    await send_message(ctx.server, ctx.player, f"God mode toggled for {target.name}")


@register_command(
    name="admin",
    aliases=["login"],
    admin_only=False,
    usage="/admin <password>",
    description="Login as admin",
)
async def cmd_admin_login(ctx: CommandContext):
    """Login as admin."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /admin <password>")
        return
    
    password = ctx.args[0]
    
    if password == ctx.server.config.admin_password:
        ctx.player.admin = True
        await send_message(ctx.server, ctx.player, "You are now an admin.")
    else:
        await send_message(ctx.server, ctx.player, "Invalid password.")
