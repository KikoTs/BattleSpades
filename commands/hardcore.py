"""Public help for the first-party Hardcore playlists."""
from .command_handler import CommandContext, register_command, send_message


@register_command(name="hcrules", description="Explain the Hardcore classes and rules")
async def hardcore_rules(ctx: CommandContext) -> None:
    if getattr(ctx.server.mode, "hardcore", False) and ctx.player is not None:
        await ctx.server.mode.show_rules(ctx.player)
    else:
        await send_message(ctx.server, ctx.player, "Select a Hardcore mode in the host GUI to use the five-class ruleset.")
