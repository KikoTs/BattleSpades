"""Chat orders for your team's bots.

``/follow /hold /attack /defend /protect /escort /free`` order them, ``/split``
divides them between two jobs, ``/status`` reads back what each is doing.

Any player on a team may order that team's bots. Optional arguments name bots
(prefix or substring); with none, every bot on your team obeys. The runners
below take an optional ``team`` so the same commands can address another team
(see the ``/team <color> <command>`` form in ``commands/player.py``).
"""

from server.bot_ai.player_orders import DONE_TEXT
from server.game_constants import TEAM1, TEAM2

from .command_handler import CommandContext, register_command, send_message

_USAGE_TAIL = " [bot name ...]"
_ACTION_LIST = "follow, hold, attack, defend, protect, escort, free"

# Every spelling of an action (in /split and /team), to the order it means:
# the long name, its aliases, and the short form (fo ho at de pr es fr).
ACTIONS = {
    "follow": "follow", "fo": "follow",
    "hold": "hold", "stay": "hold", "ho": "hold",
    "attack": "attack", "assault": "attack", "at": "attack",
    "defend": "defend", "de": "defend",
    "protect": "protect", "protectintel": "protect", "pr": "protect",
    "escort": "escort", "es": "escort",
    "free": "free", "resume": "free", "release": "free", "fr": "free",
}
# Spellings of the /team <color> status and split commands.
_STATUS_NAMES = ("status", "orders", "st")
_SPLIT_NAMES = ("split", "sp")
# /team's team argument: b = blue, g = green, a = all.
_ALL_NAMES = ("all", "a")


async def _target(ctx: CommandContext, team: int | None):
    """The director and team a command acts on, or None after telling the player why."""

    director = getattr(ctx.server, "bots", None)
    if director is None or not getattr(director, "bots", None):
        await send_message(ctx.server, ctx.player, "There are no bots to command")
        return None
    if team is None:
        team = getattr(ctx.player, "team", None)
        if team not in (TEAM1, TEAM2):
            await send_message(ctx.server, ctx.player, "Join a team to command bots")
            return None
    return director, int(team)


async def _say(ctx: CommandContext, text: str, label: str | None) -> None:
    """Reply to the player; ``label`` ("Blue") prefixes it when two teams answer."""

    await send_message(ctx.server, ctx.player, f"{label}: {text}" if label else text)


async def run_order(ctx: CommandContext, order: str, names, team: int | None = None,
                    label: str | None = None) -> None:
    target = await _target(ctx, team)
    if target is None:
        return
    director, team = target
    count, message = director.give_order(order, ctx.player, tuple(names), team=team)
    if count == 0:
        text = "None of those bots had an order" if message == "released" else message
        await _say(ctx, text, label)
        return
    done = "released" if order == "free" else DONE_TEXT[order]
    await _say(ctx, f"{count} bot(s) {done}", label)


_STATUS_LINES = 12


async def run_status(ctx: CommandContext, names, team: int | None = None,
                     label: str | None = None) -> None:
    target = await _target(ctx, team)
    if target is None:
        return
    director, team = target
    rows = director.order_status(ctx.player, tuple(names), team=team)
    if not rows:
        where = "your team" if team == getattr(ctx.player, "team", None) else "that team"
        await _say(ctx, f"No matching bots on {where}", label)
        return
    for name, order, half, doing in rows[:_STATUS_LINES]:
        detail = f"{order}, half {half}" if half else order
        await _say(ctx, f"{name} [{detail}]: {doing}", label)
    if len(rows) > _STATUS_LINES:
        await _say(ctx, f"... and {len(rows) - _STATUS_LINES} more", label)


async def run_split(ctx: CommandContext, args, team: int | None = None,
                    label: str | None = None) -> None:
    """``/split <action> [bots]`` or ``/split <action> <action> [bots]``."""

    args = list(args)
    if not args:
        await send_message(ctx.server, ctx.player,
                           f"Usage: /split <action> [<action>] [bot name ...] (actions: {_ACTION_LIST})")
        return
    first = ACTIONS.get(args[0].lower())
    if first is None:
        await send_message(ctx.server, ctx.player,
                           f"Unknown action '{args[0]}'. Actions: {_ACTION_LIST}")
        return
    # "/split protect intel hold": the optional word after protect is not a bot name.
    names = [name for name in args[1:] if not (first == "protect" and name.lower() == "intel")]
    second = None
    if names and names[0].lower() in ACTIONS:
        second, names = ACTIONS[names[0].lower()], names[1:]
    if second == "protect":
        names = [name for name in names if name.lower() != "intel"]
    target = await _target(ctx, team)
    if target is None:
        return
    director, team = target
    count, message = director.give_split(first, second, ctx.player, tuple(names), team=team)
    await _say(ctx, message, label)


@register_command(name="follow", aliases=["fo"], usage="/follow" + _USAGE_TAIL,
                  description="Your team's bots follow you")
async def cmd_follow(ctx: CommandContext):
    await run_order(ctx, "follow", ctx.args)


@register_command(name="hold", aliases=["stay", "ho"], usage="/hold" + _USAGE_TAIL,
                  description="Bots hold the spot where they stand")
async def cmd_hold(ctx: CommandContext):
    await run_order(ctx, "hold", ctx.args)


@register_command(name="attack", aliases=["assault", "at"], usage="/attack" + _USAGE_TAIL,
                  description="Bots push the enemy objective, or hunt the nearest enemy")
async def cmd_attack(ctx: CommandContext):
    await run_order(ctx, "attack", ctx.args)


@register_command(name="defend", aliases=["de"], usage="/defend" + _USAGE_TAIL,
                  description="Bots guard your team's base or intel")
async def cmd_defend(ctx: CommandContext):
    await run_order(ctx, "defend", ctx.args)


@register_command(name="protect", aliases=["protectintel", "pr"],
                  usage="/protect [intel] [bot name ...]",
                  description="Bots guard your intel, hunt its thief, and escort your carrier")
async def cmd_protect(ctx: CommandContext):
    names = [arg for arg in ctx.args if arg.lower() != "intel"]
    await run_order(ctx, "protect", names)


@register_command(name="escort", aliases=["es"], usage="/escort" + _USAGE_TAIL,
                  description="Bots escort your team's intel carrier, else you")
async def cmd_escort(ctx: CommandContext):
    await run_order(ctx, "escort", ctx.args)


@register_command(name="free", aliases=["resume", "release", "fr"], usage="/free" + _USAGE_TAIL,
                  description="Release bots from orders; they play normally again")
async def cmd_free(ctx: CommandContext):
    await run_order(ctx, "free", ctx.args)


@register_command(name="status", aliases=["orders", "st"], usage="/status" + _USAGE_TAIL,
                  description="Show each of your team's bots (both teams for spectators): order, split half, what it is doing")
async def cmd_status(ctx: CommandContext):
    if getattr(ctx.player, "team", None) not in (TEAM1, TEAM2):
        # A spectator is on no team, so show both. It is read-only, so it needs
        # no permission (unlike ordering with /team <color> <command>).
        if await _target(ctx, TEAM1) is None:
            return
        for each in (TEAM1, TEAM2):
            await run_status(ctx, ctx.args, each, team_label(ctx.server, each))
        return
    await run_status(ctx, ctx.args)


@register_command(name="split", aliases=["sp"], usage="/split <action> [<action>]" + _USAGE_TAIL,
                  description="Split bots in two: one action keeps half their orders, two actions split the team")
async def cmd_split(ctx: CommandContext):
    await run_split(ctx, ctx.args)


# The stock team names are string-table ids the client localizes ("TEAM1_COLOR"
# shows as "Blue"), so those are the colour words players know them by.
_STOCK_COLOURS = {"TEAM1_COLOR": "blue", "TEAM2_COLOR": "green"}


def team_tokens(server) -> dict[str, int]:
    """Every spelling of a playable team: team1/team2, its id, its colour name.

    The colour name is the server's own: ``blue``/``green`` for the stock
    ``[teams]`` names, otherwise the configured name itself.
    """

    config = getattr(server, "config", None)
    tokens = {"team1": TEAM1, "team2": TEAM2, str(TEAM1): TEAM1, str(TEAM2): TEAM2,
              "b": TEAM1, "g": TEAM2}
    for team, attr in ((TEAM1, "team1_name"), (TEAM2, "team2_name")):
        name = str(getattr(config, attr, "") or "")
        if name:
            tokens[_STOCK_COLOURS.get(name, name).lower()] = team
            tokens[name.lower()] = team
    return tokens


def team_label(server, team: int) -> str:
    """The server's name for a team as players see it ("Blue", "Green")."""

    config = getattr(server, "config", None)
    name = str(getattr(config, "team1_name" if team == TEAM1 else "team2_name", "") or "")
    return _STOCK_COLOURS.get(name, name or f"Team {team}").capitalize()


# /team <color> <command>: every spelling of every command it forwards.
_TEAM_COMMANDS = ", ".join(("follow", "hold", "attack", "defend", "protect", "escort",
                            "free", "status", "split"))


async def team_command(ctx: CommandContext) -> None:
    """``/team <color|all> <command> ...``: order another team's bots (admins, and
    spectators when ``[bots] order_team_command_spectators`` allows it). ``all``
    runs the command for both teams.

    It forwards to the same runners as the plain commands, so every command and
    every ``/split`` form works, and orders obey the same rules: other players'
    orders on that team keep working and any teammate can ``/free`` them.
    """

    server, player = ctx.server, ctx.player
    token = ctx.args[0].lower()
    everyone = token in _ALL_NAMES
    team = None if everyone else team_tokens(server).get(token)
    if team is None and not everyone:
        await send_message(server, player,
                           f"Unknown team '{ctx.args[0]}'. Use blue, green or all (team1 or team2)")
        return
    is_spectator = getattr(player, "team", None) not in (TEAM1, TEAM2)
    spectators_allowed = bool(getattr(getattr(getattr(server, "config", None), "bots", None),
                                      "order_team_command_spectators", False))
    if not (getattr(player, "admin", False) or (is_spectator and spectators_allowed)):
        if is_spectator:
            await send_message(server, player, "Spectators cannot command bots on this server")
        else:
            await send_message(server, player,
                               "Only admins and spectators can use /team <color> <command>; "
                               "use /<command> to order your own team's bots")
        return
    command, rest = ctx.args[1].lower(), ctx.args[2:]
    if command in ACTIONS:
        action = ACTIONS[command]
        if action == "protect":
            rest = [name for name in rest if name.lower() != "intel"]

        async def runner(target_team, label):
            await run_order(ctx, action, rest, target_team, label)
    elif command in _STATUS_NAMES:
        async def runner(target_team, label):
            await run_status(ctx, rest, target_team, label)
    elif command in _SPLIT_NAMES:
        async def runner(target_team, label):
            await run_split(ctx, rest, target_team, label)
    else:
        await send_message(server, player,
                           f"Unknown command '{ctx.args[1]}'. Use: {_TEAM_COMMANDS}")
        return
    if not everyone:
        await runner(team, None)
        return
    # "all": the same command for each team in turn. Each team answers on its
    # own (a refusal for one never stops the other) under its own label.
    if await _target(ctx, TEAM1) is None:
        return
    for each in (TEAM1, TEAM2):
        await runner(each, team_label(server, each))
