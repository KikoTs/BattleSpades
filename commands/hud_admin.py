"""Admin commands for the runtime team rules (TeamLockScore 81 / TeamInfiniteBlocks 82)."""

from __future__ import annotations

from server.game_constants import TEAM1, TEAM2

from .command_handler import CommandContext, register_command, send_message

__all__ = ["cmd_infiniteblocks", "cmd_lockscore"]

_ON = {"on", "1", "true", "yes", "enable", "enabled"}
_OFF = {"off", "0", "false", "no", "disable", "disabled"}


def _parse_teams(server, token: str) -> list[int] | None:
    token = token.strip().lower()
    if token in ("all", "both", "*"):
        return [TEAM1, TEAM2]
    aliases = {"1": TEAM1, "team1": TEAM1, "2": TEAM2, "team2": TEAM2}
    if token in aliases:
        return [aliases[token]]
    for team_id in (TEAM1, TEAM2):
        team = server.teams.get(team_id)
        if team is not None and str(getattr(team, "name", "")).lower() == token:
            return [team_id]
    return None


def _parse_state(token: str) -> bool | None:
    token = token.strip().lower()
    if token in _ON:
        return True
    if token in _OFF:
        return False
    return None


async def _team_rule_command(ctx: CommandContext, *, usage: str, label: str, setter, getter) -> None:
    if not ctx.args:
        await send_message(ctx.server, ctx.player, f"Usage: {usage}")
        return
    teams = _parse_teams(ctx.server, ctx.args[0])
    if teams is None:
        await send_message(ctx.server, ctx.player, f"Unknown team {ctx.args[0]!r}. {usage}")
        return
    if len(ctx.args) < 2:
        states = ", ".join(
            f"{ctx.server.teams[t].name}: {'on' if getter(ctx.server, t) else 'off'}"
            for t in teams
        )
        await send_message(ctx.server, ctx.player, f"{label} - {states}")
        return
    enabled = _parse_state(ctx.args[1])
    if enabled is None:
        await send_message(ctx.server, ctx.player, f"Usage: {usage}")
        return
    changed = [t for t in teams if setter(ctx.server, t, enabled)]
    if not changed:
        await send_message(ctx.server, ctx.player, f"{label} unchanged")
        return
    names = ", ".join(str(ctx.server.teams[t].name) for t in changed)
    await send_message(
        ctx.server, ctx.player, f"{label} {'on' if enabled else 'off'} for {names}"
    )


@register_command(
    name="lockscore",
    admin_only=True,
    usage="/lockscore <1|2|all> [on|off]",
    description="Freeze a team's score (TeamLockScore)",
)
async def cmd_lockscore(ctx: CommandContext):
    from server.hud_packets import set_team_lock_score, team_score_locked

    await _team_rule_command(
        ctx,
        usage="/lockscore <1|2|all> [on|off]",
        label="Score lock",
        setter=set_team_lock_score,
        getter=team_score_locked,
    )


@register_command(
    name="infiniteblocks",
    aliases=["infblocks"],
    admin_only=True,
    usage="/infiniteblocks <1|2|all> [on|off]",
    description="Give a team an unlimited block wallet (TeamInfiniteBlocks)",
)
async def cmd_infiniteblocks(ctx: CommandContext):
    from server.hud_packets import set_team_infinite_blocks, team_infinite_blocks

    await _team_rule_command(
        ctx,
        usage="/infiniteblocks <1|2|all> [on|off]",
        label="Infinite blocks",
        setter=set_team_infinite_blocks,
        getter=team_infinite_blocks,
    )
