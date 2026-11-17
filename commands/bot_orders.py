"""Chat orders for your team's bots: /follow /hold /attack /defend /protect /escort /free.

Any player on a team may order that team's bots. Optional arguments name bots
(prefix or substring); with none, every bot on your team obeys. ``/status``
lists what each of your team's bots is ordered to do and is doing right now.
"""

from server.game_constants import TEAM1, TEAM2

from .command_handler import CommandContext, register_command, send_message

_USAGE_TAIL = " [bot name ...]"


async def _order(ctx: CommandContext, order: str, names, done: str) -> None:
    director = getattr(ctx.server, "bots", None)
    if director is None or not getattr(director, "bots", None):
        await send_message(ctx.server, ctx.player, "There are no bots to command")
        return
    if getattr(ctx.player, "team", None) not in (TEAM1, TEAM2):
        await send_message(ctx.server, ctx.player, "Join a team to command bots")
        return
    count, _ = director.give_order(order, ctx.player, tuple(names))
    if count == 0:
        await send_message(ctx.server, ctx.player, "No matching bots on your team")
        return
    await send_message(ctx.server, ctx.player, f"{count} bot(s) {done}")


@register_command(name="follow", usage="/follow" + _USAGE_TAIL,
                  description="Your team's bots follow you")
async def cmd_follow(ctx: CommandContext):
    await _order(ctx, "follow", ctx.args, "following you")


@register_command(name="hold", aliases=["stay"], usage="/hold" + _USAGE_TAIL,
                  description="Bots hold the spot where they stand")
async def cmd_hold(ctx: CommandContext):
    await _order(ctx, "hold", ctx.args, "holding position")


@register_command(name="attack", aliases=["assault"], usage="/attack" + _USAGE_TAIL,
                  description="Bots push the enemy objective, or hunt the nearest enemy")
async def cmd_attack(ctx: CommandContext):
    await _order(ctx, "attack", ctx.args, "attacking")


@register_command(name="defend", usage="/defend" + _USAGE_TAIL,
                  description="Bots guard your team's base or intel")
async def cmd_defend(ctx: CommandContext):
    await _order(ctx, "defend", ctx.args, "defending")


@register_command(name="protect", aliases=["protectintel"],
                  usage="/protect [intel] [bot name ...]",
                  description="Bots guard your intel, hunt its thief, and escort your carrier")
async def cmd_protect(ctx: CommandContext):
    names = [arg for arg in ctx.args if arg.lower() != "intel"]
    await _order(ctx, "protect", names, "protecting the intel")


@register_command(name="escort", usage="/escort" + _USAGE_TAIL,
                  description="Bots escort your team's intel carrier, else you")
async def cmd_escort(ctx: CommandContext):
    await _order(ctx, "escort", ctx.args, "escorting")


@register_command(name="free", aliases=["resume", "release"], usage="/free" + _USAGE_TAIL,
                  description="Release bots from orders; they play normally again")
async def cmd_free(ctx: CommandContext):
    await _order(ctx, "free", ctx.args, "released")


_STATUS_LINES = 12


@register_command(name="status", aliases=["orders"], usage="/status" + _USAGE_TAIL,
                  description="Show each of your team's bots: its order and what it is doing")
async def cmd_status(ctx: CommandContext):
    director = getattr(ctx.server, "bots", None)
    if director is None or not getattr(director, "bots", None):
        await send_message(ctx.server, ctx.player, "There are no bots to command")
        return
    if getattr(ctx.player, "team", None) not in (TEAM1, TEAM2):
        await send_message(ctx.server, ctx.player, "Join a team to command bots")
        return
    rows = director.order_status(ctx.player, tuple(ctx.args))
    if not rows:
        await send_message(ctx.server, ctx.player, "No matching bots on your team")
        return
    for name, order, doing in rows[:_STATUS_LINES]:
        await send_message(ctx.server, ctx.player, f"{name} [{order}]: {doing}")
    if len(rows) > _STATUS_LINES:
        await send_message(ctx.server, ctx.player, f"... and {len(rows) - _STATUS_LINES} more")
