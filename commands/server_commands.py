"""
Server commands - admin commands for server management.
"""

import time

from server.game_constants import TEAM1, TEAM2
from shared.packet import FogColor

from .command_handler import register_command, CommandContext, send_message


@register_command(
    name="netcode",
    aliases=["nc"],
    admin_only=True,
    usage="/netcode [selfrow on|off] [offset <n>] [interval <n>]",
    description="Live-tune WorldUpdate self-row reconciliation (no restart)",
)
async def cmd_netcode(ctx: CommandContext):
    """Adjust the self-row reconciliation knobs at RUNTIME.

    Restarting the server to change these is not an option while calibrating:
    a restart resets server loop_count while the client's keeps climbing, so
    every self-row lands on a loop_count the client's movement_history has
    never seen -> hard SNAP on every packet (see docs/NETCODE_RECONCILIATION.md).
    The sim loop re-reads these off config each tick, so mutating them here
    takes effect immediately and the measurement stays valid.
    """
    cfg = ctx.server.config
    args = list(ctx.args)
    if not args:
        await send_message(
            ctx.server, ctx.player,
            "selfrow=%s offset=%s interval=%s" % (
                cfg.worldupdate_include_self,
                cfg.worldupdate_loop_offset,
                cfg.worldupdate_self_row_interval,
            ))
        return
    try:
        while args:
            key = args.pop(0).lower()
            if key in ("selfrow", "self"):
                cfg.worldupdate_include_self = args.pop(0).lower() in ("on", "true", "1")
            elif key == "offset":
                cfg.worldupdate_loop_offset = int(args.pop(0))
            elif key == "interval":
                cfg.worldupdate_self_row_interval = max(1, int(args.pop(0)))
            else:
                await send_message(ctx.server, ctx.player, "unknown key: %s" % key)
                return
    except (IndexError, ValueError):
        await send_message(ctx.server, ctx.player, "usage: /netcode selfrow on|off offset <n> interval <n>")
        return
    await send_message(
        ctx.server, ctx.player,
        "netcode -> selfrow=%s offset=%s interval=%s" % (
            cfg.worldupdate_include_self,
            cfg.worldupdate_loop_offset,
            cfg.worldupdate_self_row_interval,
        ))


@register_command(
    name="map",
    aliases=["changemap"],
    admin_only=True,
    usage="/map <mapname>",
    description="Change the current map",
)
async def cmd_map(ctx: CommandContext):
    """Change map through a crash-safe full client-session rollover."""
    if not ctx.args:
        await send_message(ctx.server, ctx.player, f"Current map: {ctx.server.world_manager.map_name}")
        await send_message(ctx.server, ctx.player, "Usage: /map <mapname>")
        return
    
    transition = ctx.server.match_transition
    request = getattr(transition, "request_map_change", None)
    if callable(request):
        result = request(ctx.raw_args.strip(), requester=ctx.player)
    else:
        # Compatibility for plugins/tests implementing the pre-service API.
        result = await transition.change_map(ctx.raw_args.strip())
    if not result.ok:
        await send_message(ctx.server, ctx.player, result.message)
    elif not result.reconnect_required:
        await send_message(ctx.server, ctx.player, result.message)


@register_command(
    name="mode",
    aliases=["gamemode"],
    admin_only=True,
    usage="/mode <tdm|ctf|cctf|zom|vip|mh|tc|dia|dem|oc|arena>",
    description="Change the game mode",
)
async def cmd_mode(ctx: CommandContext):
    """Change mode through a crash-safe full client-session rollover."""
    if not ctx.args:
        current = ctx.server.mode.name if ctx.server.mode else "None"
        await send_message(ctx.server, ctx.player, f"Current mode: {current}")
        await send_message(
            ctx.server,
            ctx.player,
            "Usage: /mode <tdm|ctf|cctf|zom|vip|mh|tc|dia|dem|oc|arena>",
        )
        return
    
    transition = ctx.server.match_transition
    request = getattr(transition, "request_mode_change", None)
    if callable(request):
        result = request(ctx.args[0], requester=ctx.player)
    else:
        result = await transition.change_mode(ctx.args[0])
    if not result.ok:
        await send_message(ctx.server, ctx.player, result.message)
    elif not result.reconnect_required:
        await send_message(ctx.server, ctx.player, result.message)


@register_command(
    name="restart",
    aliases=["reset"],
    admin_only=True,
    usage="/restart",
    description="Restart the current round/match",
)
async def cmd_restart(ctx: CommandContext):
    """Restart the round immediately (skips the end-of-round stats screen)."""
    result = await ctx.server.match_transition.restart_round()
    if not result.ok:
        await send_message(ctx.server, ctx.player, result.message)
        return

    from server.announcements import broadcast_overlay

    broadcast_overlay(ctx.server, "Match restarted!")


@register_command(
    name="endround",
    aliases=["endgame", "forceend"],
    admin_only=True,
    usage="/endround [team]",
    description="Force the current round to end (victory audio → stats screen → restart)",
)
async def cmd_endround(ctx: CommandContext):
    """Force-trigger the end-of-round sequence. Optional team arg (1/2) sets
    the winner; otherwise the current score leader wins."""
    mode = ctx.server.mode
    if mode is None or mode.ended:
        await send_message(ctx.server, ctx.player, "No active round to end.")
        return
    winner = None
    if ctx.args:
        try:
            from server.game_constants import TEAM1, TEAM2
            winner = {1: TEAM1, 2: TEAM2}.get(int(ctx.args[0]))
        except (ValueError, IndexError):
            winner = None
    if winner is not None:
        await mode._end_by_score(winner)
    else:
        await mode._end_by_time()


@register_command(
    name="say",
    admin_only=True,
    usage="/say <message>",
    description="Send a server announcement",
)
async def cmd_say(ctx: CommandContext):
    """Server announcement."""
    message_text = ctx.raw_args.strip()
    if not message_text:
        await send_message(ctx.server, ctx.player, "Usage: /say <message>")
        return
    if len(message_text) > 256:
        await send_message(ctx.server, ctx.player, "Announcement is too long (max 256 characters)")
        return
    
    from server.announcements import broadcast_overlay

    broadcast_overlay(ctx.server, f"[SERVER] {message_text}")


@register_command(
    name="fog",
    admin_only=True,
    usage="/fog <r> <g> <b>",
    description="Set fog color",
)
async def cmd_fog(ctx: CommandContext):
    """Set fog color."""
    if len(ctx.args) != 3:
        await send_message(ctx.server, ctx.player, "Usage: /fog <r> <g> <b>")
        return
    
    try:
        r = int(ctx.args[0])
        g = int(ctx.args[1])
        b = int(ctx.args[2])
    except ValueError:
        await send_message(ctx.server, ctx.player, "Invalid RGB values")
        return
    if any(component < 0 or component > 255 for component in (r, g, b)):
        await send_message(ctx.server, ctx.player, "RGB values must be in range 0-255")
        return
    
    color = (r << 16) | (g << 8) | b
    
    packet = FogColor()
    packet.color = color
    ctx.server.broadcast(bytes(packet.generate()))
    # Future StateData on this map must agree with the live fog after a
    # reconnect. MatchTransitionService clears the map-epoch override.
    ctx.server.fog_color_override = (r, g, b)
    
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
                seconds = int(ctx.args[0])
                if seconds < 0:
                    raise ValueError
                ctx.server.mode.time_limit = seconds
                ctx.server.mode.start_time = time.time()
                ctx.server.mode.elapsed_time = 0.0
                await send_message(ctx.server, ctx.player, 
                                   f"Time remaining set to {seconds} seconds")
            except ValueError:
                await send_message(ctx.server, ctx.player, "Invalid time value")
        else:
            remaining = max(
                0.0,
                ctx.server.mode.time_limit - ctx.server.mode.elapsed_time,
            )
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
    
    # Move players (last joined first). A raw ``player.team =`` mutation is
    # invisible to other clients; KillAction + the ordinary respawn boundary
    # re-announces class/loadout/team through CreatePlayer.
    moved = 0
    for player in reversed(list(ctx.server.teams[larger_team].players)):
        if moved >= to_move:
            break

        old_team = player.team
        ctx.server.teams[larger_team].remove_player(player)
        player.team = smaller_team
        ctx.server.teams[smaller_team].add_player(player)

        die = getattr(player, "die", None)
        if player.alive and callable(die):
            from server.game_constants import KILL_TEAM_CHANGE
            die(kill_type=KILL_TEAM_CHANGE)
        queue_event = getattr(ctx.server, "queue_mode_event", None)
        if callable(queue_event):
            queue_event("on_player_team_change", player, old_team, smaller_team)
        ctx.server.respawn_player(player)
        
        await send_message(ctx.server, player, 
                           f"You were moved to {ctx.server.teams[smaller_team].name}")
        moved += 1
    
    from server.announcements import broadcast_overlay

    broadcast_overlay(ctx.server, f"Teams balanced ({moved} players moved)")


async def _ensure_bot_director(ctx: CommandContext):
    """Start an empty director for an explicit admin bot command."""

    if ctx.server.bots is not None:
        return ctx.server.bots
    from server.bot_ai import BotDirector

    director = BotDirector(ctx.server)
    ctx.server.bots = director
    await director.start(initial_count=0)
    ctx.server.config.bots.enabled = True
    return director


@register_command(
    name="bots",
    admin_only=True,
    usage=(
        "/bots status|fill <count>|add <count> [team]|"
        "remove <count|name|all>|difficulty <casual|normal|hard|mixed>|"
        "debug <on|off|name>"
    ),
    description="Manage isolated server-owned bots",
)
async def cmd_bots(ctx: CommandContext):
    """Inspect and manage the bounded bot population."""

    subcommand = ctx.args[0].lower() if ctx.args else "status"
    if subcommand == "status":
        director = ctx.server.bots
        if director is None:
            await send_message(ctx.server, ctx.player, "Bots disabled (0 active)")
            return
        worker = director.status()
        await send_message(
            ctx.server,
            ctx.player,
            (
                f"Bots={len(director.bots)} worker={'up' if worker.running else 'down'} "
                f"pid={worker.process_id or '-'} restarts={worker.restarts} "
                f"frames={worker.queued_frames} intents={worker.queued_intents} "
                f"terrain={worker.pending_terrain_cells}"
            ),
        )
        return

    director = await _ensure_bot_director(ctx)
    config = ctx.server.config.bots
    if subcommand == "fill":
        if len(ctx.args) != 2:
            await send_message(ctx.server, ctx.player, "Usage: /bots fill <count>")
            return
        try:
            target = max(0, min(ctx.server.config.max_players, int(ctx.args[1])))
        except ValueError:
            await send_message(ctx.server, ctx.player, "Bot fill count must be an integer")
            return
        config.population_mode = "backfill"
        config.fill_target = target
        config.max_bots = max(config.max_bots, min(target, ctx.server.config.max_players))
        director.request_population_refresh()
        await send_message(ctx.server, ctx.player, f"Bot backfill target set to {target}")
        return

    if subcommand == "add":
        if len(ctx.args) < 2:
            await send_message(ctx.server, ctx.player, "Usage: /bots add <count> [team]")
            return
        try:
            count = max(0, min(12, int(ctx.args[1])))
        except ValueError:
            await send_message(ctx.server, ctx.player, "Bot add count must be an integer")
            return
        team = None
        if len(ctx.args) >= 3:
            token = ctx.args[2].lower()
            team = {
                "1": TEAM1,
                "team1": TEAM1,
                "blue": TEAM1,
                "2": TEAM2,
                "team2": TEAM2,
                "green": TEAM2,
            }.get(token)
            if team is None:
                await send_message(ctx.server, ctx.player, "Team must be 1/blue or 2/green")
                return
        config.population_mode = "admin"
        config.max_bots = max(config.max_bots, len(director.bots) + count)
        added = 0
        for _ in range(count):
            if await director.add_bot(team=team) is None:
                break
            added += 1
        await send_message(ctx.server, ctx.player, f"Added {added} bot(s)")
        return

    if subcommand == "remove":
        if len(ctx.args) != 2:
            await send_message(
                ctx.server, ctx.player, "Usage: /bots remove <count|name|all>"
            )
            return
        config.population_mode = "admin"
        token = ctx.args[1]
        if token.lower() == "all":
            candidates = list(director.bots)
        else:
            try:
                count = max(0, int(token))
            except ValueError:
                candidates = [
                    bot for bot in director.bots
                    if bot.name.lower().startswith(token.lower())
                ][:1]
            else:
                candidates = list(director.bots)[:count]
        removed = 0
        for bot in candidates:
            if await director.remove_bot(bot):
                removed += 1
        await send_message(
            ctx.server,
            ctx.player,
            f"Removed {removed} bot(s); objective-protected bots were kept",
        )
        return

    if subcommand == "difficulty":
        if len(ctx.args) != 2 or ctx.args[1].lower() not in (
            "casual", "normal", "hard", "mixed"
        ):
            await send_message(
                ctx.server,
                ctx.player,
                "Usage: /bots difficulty <casual|normal|hard|mixed>",
            )
            return
        config.difficulty = ctx.args[1].lower()
        await send_message(
            ctx.server,
            ctx.player,
            f"Bot difficulty set to {config.difficulty} for new profiles",
        )
        return

    if subcommand == "debug":
        if len(ctx.args) != 2:
            await send_message(
                ctx.server, ctx.player, "Usage: /bots debug <on|off|name>"
            )
            return
        token = ctx.args[1]
        if token.lower() in ("on", "off"):
            config.debug_visualization = token.lower() == "on"
            await send_message(
                ctx.server,
                ctx.player,
                f"Bot debug visualization {'enabled' if config.debug_visualization else 'disabled'}",
            )
            return
        rows = director.debug_snapshot(token)
        if not rows:
            await send_message(
                ctx.server,
                ctx.player,
                "Bot debug is disabled or no matching bot exists",
            )
            return
        row = rows[0]
        await send_message(
            ctx.server,
            ctx.player,
            (
                f"{row['name']} action={row['action']} edge={row['affordance']} "
                f"goal={row['goal']} path={row['path']}"
            )[:240],
        )
        return

    await send_message(ctx.server, ctx.player, "Unknown /bots subcommand")
