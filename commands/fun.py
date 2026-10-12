"""Public help and interaction commands for community game modes."""
from .command_handler import register_command, send_message


@register_command(name="funmodes", description="List unofficial game modes")
async def fun_modes(ctx):
    from modes.unofficial.catalog import SPECS
    for start in range(0, len(SPECS), 4):
        await send_message(ctx.server, ctx.player, " | ".join(f"{s.code}: {s.title}" for s in SPECS[start:start + 4]))


@register_command(name="funrules", description="Show the active unofficial mode's rules")
async def fun_rules(ctx):
    mode = ctx.server.mode
    if getattr(mode, "unofficial", False) and ctx.player is not None:
        await mode.show_rules(ctx.player)
        return
    await send_message(ctx.server, ctx.player, getattr(mode, "description", "No mode active."))
    if getattr(mode, "unofficial", False):
        await send_message(ctx.server, ctx.player, f"{mode.name}; phase: {mode.phase}; round {mode.round_number}/{mode.rounds}. /funscore for standings.")


@register_command(name="funscore", description="Show unofficial round and match standings")
async def fun_score(ctx):
    mode = ctx.server.mode
    if not getattr(mode, "unofficial", False):
        return
    for title, values in (("Round", mode.points), ("Match", mode.match_points)):
        leaders = sorted(values.items(), key=lambda pair: -pair[1])[:8]
        await send_message(ctx.server, ctx.player, title + ": " + (", ".join(f"{mode.label(p)} {n}" for p, n in leaders) or "no points yet"))


@register_command(name="retry", description="Return to your race checkpoint (+2 seconds)")
async def retry(ctx):
    callback = getattr(ctx.server.mode, "retry", None)
    if callable(callback) and ctx.player is not None:
        await callback(ctx.player)


@register_command(name="stash", description="Hide your team's False Wall cache during preparation")
async def stash(ctx):
    callback = getattr(ctx.server.mode, "stash", None)
    if callable(callback) and ctx.player is not None:
        await callback(ctx.player)
