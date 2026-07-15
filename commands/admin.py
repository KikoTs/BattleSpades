"""
Admin commands - requires admin permission.
"""

import math

import shared.constants as C

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
    
    from server.announcements import broadcast_overlay

    broadcast_overlay(ctx.server, f"{target.name} was kicked: {reason}")
    
    target.disconnect(reason=2)  # DISCONNECT_KICKED


@register_command(
    name="ban",
    aliases=["b"],
    admin_only=True,
    usage="/ban <player> [duration] [reason]",
    description="Ban a player from the server",
)
async def cmd_ban(ctx: CommandContext):
    """Ban a player. /ban <player> [duration] [reason].

    Duration accepts 30m / 2h / 1d / 90 (seconds) / perma. If the second arg
    isn't a duration it's treated as the start of the reason (permanent ban).
    """
    if not ctx.args:
        await send_message(ctx.server, ctx.player, "Usage: /ban <player> [duration] [reason]")
        return

    from server.bans import parse_duration, address_host

    target_name = ctx.args[0]

    # Second token may be a duration or the first word of the reason.
    rest = ctx.args[1:]
    duration = 0
    if rest:
        parsed = parse_duration(rest[0])
        if parsed >= 0:
            duration = parsed
            rest = rest[1:]
    reason = " ".join(rest) if rest else "Banned by admin"

    target = ctx.server.get_player_by_name(target_name)
    if not target:
        await send_message(ctx.server, ctx.player, f"Player not found: {target_name}")
        return

    # Persist the ban keyed by IP so it survives reconnects and restarts.
    ip = None
    if target.connection and getattr(target.connection, "peer", None) is not None:
        ip = address_host(target.connection.peer)
    if ip:
        ctx.server.ban_manager.add(ip, target.name, reason, duration)

    when = "permanently" if duration <= 0 else f"for {ctx.args[1]}"
    from server.announcements import broadcast_overlay

    broadcast_overlay(
        ctx.server, f"{target.name} was banned {when}: {reason}"
    )

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
            if (
                not all(math.isfinite(value) for value in (x, y, z))
                or not 0.0 <= x < float(C.MAP_X)
                or not 0.0 <= y < float(C.MAP_Y)
                or not 0.0 <= z < float(C.MAP_Z)
            ):
                await send_message(
                    ctx.server,
                    ctx.player,
                    "Coordinates must be finite and inside the map",
                )
                return
            
            ctx.player.set_position(x, y, z)
            set_velocity = getattr(ctx.player, "set_velocity", None)
            if callable(set_velocity):
                set_velocity(0.0, 0.0, 0.0)
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

    target.god_mode = not target.god_mode
    state = "ON" if target.god_mode else "OFF"
    await send_message(ctx.server, ctx.player, f"God mode {state} for {target.name}")
    if target is not ctx.player:
        await send_message(ctx.server, target, f"An admin set your god mode {state}.")


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
