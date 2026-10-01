"""
Admin commands - requires admin permission.
"""

import hmac
import logging
import math
import time

import shared.constants as C

from .command_handler import register_command, CommandContext, send_message

logger = logging.getLogger(__name__)


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
        ip = address_host(target.connection.peer, ctx.server)
    if ip:
        ctx.server.ban_manager.add(ip, target.name, reason, duration)

    when = "permanently" if duration <= 0 else f"for {ctx.args[1]}"
    from server.announcements import broadcast_overlay

    broadcast_overlay(
        ctx.server, f"{target.name} was banned {when}: {reason}"
    )

    # Retail DISCONNECT enum: ERROR_BANNED (1) is permanent, a timed ban is
    # ERROR_TEMP_BANNED (19).
    target.disconnect(reason=1 if duration <= 0 else 19)


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
    """Log in as admin with the ``[admin] password``.

    Disabled while that password is the shipped default, empty or short
    (see :func:`server.config.admin_password_problem`). The comparison is
    constant-time, and ``[anticheat] admin_login_attempts`` consecutive
    failures from one address kick the player and ban the address for
    :data:`ADMIN_LOGIN_BAN_SECONDS`. Slash commands are also rate-limited
    per player (``server.handlers.social``), so guessing is slow.
    """
    server, player = ctx.server, ctx.player
    if getattr(player, "admin", False):
        await send_message(server, player, "You are already an admin.")
        return
    configured = getattr(server.config, "admin_password", "")
    from server.config import admin_password_problem

    problem = admin_password_problem(configured)
    if problem is not None:
        logger.warning(
            "Refused /admin from %s: login disabled (%s)",
            getattr(player, "name", "?"), problem,
        )
        await send_message(
            server,
            player,
            "Admin login is disabled on this server: the operator has not "
            "set a secure admin password.",
        )
        return
    attempt = ctx.raw_args.strip()
    if not attempt:
        await send_message(server, player, "Usage: /admin <password>")
        return

    key = _login_key(player)
    if hmac.compare_digest(
        attempt.encode("utf-8"), str(configured).encode("utf-8")
    ):
        _login_failures(server).pop(key, None)
        player.admin = True
        logger.info(
            "Admin login: %s (%s)", getattr(player, "name", "?"), key
        )
        await send_message(server, player, "You are now an admin.")
        return

    await _reject_login(server, player, key, "Invalid password.")


async def _reject_login(server, player, key: str, message: str) -> None:
    """Count one failed admin credential; kick and ban at the limit.

    Shared by /admin and /claimhost so neither secret can be guessed faster
    than the other. Loopback is never banned: the hosting client, the AoSPlay
    relay tunnel and the Steam relay all reach a player-hosted room from
    127.0.0.1, so banning it would lock the room's own creator out.
    """
    from server import anticheat

    failures = _record_login_failure(server, key)
    limit = max(1, int(anticheat.setting(server, "admin_login_attempts", 3)))
    anticheat.report(
        server, player, "admin_login_failed", attempts=failures, limit=limit
    )
    if failures < limit:
        await send_message(server, player, message)
        return
    _login_failures(server).pop(key, None)
    ban_manager = getattr(server, "ban_manager", None)
    bannable = key.startswith("ip:") and not _is_loopback(key[3:])
    logger.warning(
        "Kicking %s (%s) after %d failed admin credential attempts%s",
        getattr(player, "name", "?"), key, failures,
        f"; address banned for {ADMIN_LOGIN_BAN_SECONDS} s" if bannable else "",
    )
    if bannable and ban_manager is not None:
        ban_manager.add(
            key[3:],
            getattr(player, "name", ""),
            "Too many failed admin login attempts",
            ADMIN_LOGIN_BAN_SECONDS,
        )
    player.disconnect(int(C.DISCONNECT.ERROR_KICKED))


def _is_loopback(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return str(host).lower() == "localhost"


@register_command(
    name="claimhost",
    admin_only=False,
    hidden=True,
    usage="/claimhost <token>",
    description="Room creator's one-time admin claim (sent by the client)",
)
async def cmd_claim_host(ctx: CommandContext):
    """Grant admin to the player who created this room.

    The BattleSpades client that launches a player-hosted server writes a
    fresh random ``[admin] creator_token`` into that room's private session
    config and sends ``/claimhost <token>`` right after it joins. The token
    never leaves the creator's machine otherwise, is compared in constant
    time and works exactly once: a sniffed or replayed claim is refused and
    counts as a failed admin login. Dedicated servers leave it empty.
    """
    server, player = ctx.server, ctx.player
    configured = str(getattr(server.config, "admin_creator_token", "") or "")
    if not configured:
        await send_message(server, player, "This server has no room host to claim.")
        return
    if getattr(player, "admin", False):
        await send_message(server, player, "You are already an admin.")
        return
    attempt = ctx.raw_args.strip()
    if not attempt:
        await send_message(server, player, "Usage: /claimhost <token>")
        return
    key = _login_key(player)
    matches = hmac.compare_digest(
        attempt.encode("utf-8"), configured.encode("utf-8")
    )
    if not matches or getattr(server, "_creator_token_used", False):
        logger.warning(
            "Refused room-host claim from %s (%s): %s",
            getattr(player, "name", "?"), key,
            "token already used" if matches else "wrong token",
        )
        await _reject_login(
            server, player, key,
            "The room host was already claimed." if matches else "Invalid host token.",
        )
        return
    server._creator_token_used = True
    _login_failures(server).pop(key, None)
    player.admin = True
    player.room_host = True
    logger.info("Room host claimed by %s (%s)", getattr(player, "name", "?"), key)
    await send_message(server, player, "You created this room, so you are its admin.")
    await _send_room_password(server, player)


@register_command(
    name="roompassword",
    aliases=["roompass"],
    admin_only=True,
    usage="/roompassword",
    description="Show this player-hosted room's admin password",
)
async def cmd_room_password(ctx: CommandContext):
    """Repeat a client-hosted room's generated admin password to an admin.

    Only rooms started by the BattleSpades client (they carry a creator
    token) echo it; a dedicated operator's password is never sent anywhere.
    """
    if not getattr(ctx.server.config, "admin_creator_token", ""):
        await send_message(
            ctx.server, ctx.player,
            "Only player-hosted rooms show their admin password in game.",
        )
        return
    await _send_room_password(ctx.server, ctx.player)


async def _send_room_password(server, player) -> None:
    from server.config import admin_password_problem

    password = str(getattr(server.config, "admin_password", "") or "")
    if admin_password_problem(password) is not None:
        return
    await send_message(
        server, player,
        f"Room admin password: {password} (share it only with people "
        "who should get admin: they type /admin <password>).",
    )


def player_admin_identities(server, player, identity=None) -> set[str]:
    """Verified identity keys for ``player`` (``steam:<id>``, ``aosplay:<id>``).

    Only authenticated sources count: the Steam P2P relay's peer SteamID and
    an AoSPlay join ticket the master verified. The typed name never does.
    """
    from server.bans import address_host

    keys: set[str] = set()
    connection = getattr(player, "connection", None)
    peer = getattr(connection, "peer", None)
    if peer is not None and getattr(server, "steam_p2p", None) is not None:
        host = address_host(peer, server)
        if host.startswith("steam:") and host[6:].isdigit():
            keys.add(f"steam:{int(host[6:])}")
    if identity is not None:
        for value in (getattr(identity, "public_id", ""), getattr(identity, "legacy_id", "")):
            if value:
                keys.add(f"aosplay:{str(value).lower()}")
        steam_id = str(getattr(identity, "steam_id", "") or "")
        if steam_id.isdigit():
            keys.add(f"steam:{int(steam_id)}")
    return keys


def grant_auto_admin(server, player, identity=None) -> bool:
    """Make ``player`` an admin when a verified identity is in ``[admin] auto_admin``."""
    configured = set(getattr(server.config, "admin_auto_ids", ()) or ())
    if not configured:
        return False
    matched = sorted(configured & player_admin_identities(server, player, identity))
    if not matched:
        return False
    player.admin = True
    logger.info(
        "Auto-admin: %s (%s) from [admin] auto_admin",
        getattr(player, "name", "?"), matched[0],
    )
    return True


# A kicked brute-forcer's address is banned this long (bans.json, expiring).
ADMIN_LOGIN_BAN_SECONDS = 600
# Failed attempts older than this are forgotten.
_LOGIN_FAILURE_WINDOW_SECONDS = 600.0


def _login_key(player) -> str:
    """Count failures per client address so reconnecting does not reset them."""
    from server.bans import address_host

    connection = getattr(player, "connection", None)
    peer = getattr(connection, "peer", None)
    if peer is not None:
        host = address_host(peer, getattr(connection, 'server', None))
        if host and host != "unknown":
            return f"ip:{host}"
    return f"player:{getattr(player, 'id', id(player))}"


def _login_failures(server) -> dict:
    failures = getattr(server, "_admin_login_failures", None)
    if not isinstance(failures, dict):
        failures = {}
        server._admin_login_failures = failures
    return failures


def _record_login_failure(server, key: str) -> int:
    """Record one failure for ``key``; return the count inside the window."""
    now = time.monotonic()
    failures = _login_failures(server)
    for stale in [
        name for name, (_, last) in failures.items()
        if now - last > _LOGIN_FAILURE_WINDOW_SECONDS
    ]:
        del failures[stale]
    count, _ = failures.get(key, (0, now))
    failures[key] = (count + 1, now)
    return count + 1
