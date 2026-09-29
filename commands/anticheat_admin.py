"""Admin anti-cheat review commands (detection only; nothing here punishes).

/acreport [player]  top suspicion scores, or one player's reasons
/acstats <player>   raw anti-cheat counters for one player
"""

from .command_handler import register_command, CommandContext, send_message

_TOP = 5
_LINE = 110  # keep system chat lines readable in the retail chat box


def _chunks(text: str, width: int = _LINE):
    text = str(text)
    while len(text) > width:
        cut = text.rfind(" ", 0, width)
        cut = width if cut <= 0 else cut
        yield text[:cut]
        text = text[cut:].lstrip()
    if text:
        yield text


async def _say(ctx: CommandContext, text: str) -> None:
    for line in _chunks(text):
        await send_message(ctx.server, ctx.player, line)


def _find(ctx: CommandContext, name: str):
    finder = getattr(ctx.server, "get_player_by_name", None)
    target = finder(name) if callable(finder) else None
    if target is None and name.lstrip("#").isdigit():
        target = getattr(ctx.server, "players", {}).get(int(name.lstrip("#")))
    return target


@register_command(
    name="acreport",
    admin_only=True,
    usage="/acreport [player]",
    description="Anti-cheat suspicion scores (detection only)",
)
async def cmd_acreport(ctx: CommandContext):
    from server import anticheat_report

    if ctx.args:
        target = _find(ctx, ctx.raw_args.strip())
        if target is None:
            await _say(ctx, f"Player not found: {ctx.raw_args.strip()}")
            return
        report = anticheat_report.analyze_player(ctx.server, target)
        if report is None:
            await _say(ctx, f"{target.name} is a bot (never analysed).")
            return
        state = "FLAGGED" if report["flagged"] else "not flagged"
        await _say(ctx, f"AC {target.name} (#{target.id}) score {report['score']:.2f} {state}")
        if not report["reasons"]:
            await _say(ctx, "  no check over threshold (or samples too small)")
        for reason in report["reasons"]:
            await _say(ctx, f"  [{reason['score']:.1f}] {reason['check']}: {reason['detail']}")
        return

    reports = anticheat_report.analyze_all(ctx.server)
    suspects = [r for r in reports if r["score"] > 0]
    if not suspects:
        await _say(ctx, f"AC: no suspicious players ({len(reports)} humans analysed).")
        return
    await _say(ctx, f"AC top {min(_TOP, len(suspects))} of {len(reports)} humans:")
    for report in suspects[:_TOP]:
        checks = ", ".join(r["check"] for r in report["reasons"])
        mark = "!" if report["flagged"] else " "
        await _say(
            ctx,
            f"{mark}#{report['player_id']} {report['name']} "
            f"score {report['score']:.2f}: {checks}",
        )


@register_command(
    name="acstats",
    admin_only=True,
    usage="/acstats <player>",
    description="Raw anti-cheat counters for a player",
)
async def cmd_acstats(ctx: CommandContext):
    from server import anticheat_report

    if not ctx.args:
        await _say(ctx, "Usage: /acstats <player>")
        return
    target = _find(ctx, ctx.raw_args.strip())
    if target is None:
        await _say(ctx, f"Player not found: {ctx.raw_args.strip()}")
        return
    raw = anticheat_report.raw_stats(target)
    bot = " [bot]" if getattr(target, "is_bot", False) else ""
    await _say(ctx, f"AC stats {target.name} (#{target.id}){bot}")
    await _say(
        ctx,
        f"K/D {raw['kills']}/{raw['deaths']} hitscan kills {raw['hitscan_kills']} "
        f"HS kills {raw['headshot_kills']}",
    )
    await _say(
        ctx,
        f"shots {raw['shots']} hits {raw['hits']} headshots {raw['headshots']} "
        f"pellet hits {raw['pellet_hits']} seed shots {raw['pellet_seed_shots']}",
    )
    for tool, weapon in sorted(raw["weapons"].items()):
        shots = int(weapon.get("shots", 0))
        hits = int(weapon.get("hits", 0))
        accuracy = f"{hits / shots:.0%}" if shots else "-"
        await _say(
            ctx,
            f"  tool {tool}: {shots} shots {hits} hits ({accuracy}) "
            f"{int(weapon.get('headshots', 0))} hs",
        )
    aim = raw["aim"]
    await _say(
        ctx,
        f"aim: engaged {aim['engaged']} on-head {aim['on_head']} snaps {aim['snaps']} "
        f"acq {aim['acquisition_samples']} median {aim['acquisition_median_ms']} ms",
    )
    for key in ("origin_error", "origin_error_fallback", "aim_angle", "aim_angle_fallback"):
        if raw[key]:
            await _say(ctx, f"{key}: {_fmt(raw[key])}")
    if raw["violations"]:
        await _say(ctx, f"violations: {_fmt(raw['violations'])}")
    if raw["rejected"]:
        await _say(ctx, f"rejected: {_fmt(raw['rejected'])}")
    queue = raw["input_queue_delay"]
    if queue:
        await _say(
            ctx,
            f"input queue delay: n={queue.get('samples')} p50={queue.get('p50')} "
            f"max={queue.get('max')} ticks",
        )


def _fmt(mapping: dict) -> str:
    return " ".join(f"{key}={value}" for key, value in sorted(mapping.items(), key=str))
