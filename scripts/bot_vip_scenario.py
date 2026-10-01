"""VIP-mode roster edge cases on a real map with the production bot stack.

Runs the real VIP mode, bot director, worker, motor and physics in-process
and samples, every half second, the VIP phase, who is VIP, and how far every
bot moved. Scenarios:

* ``bots1v1``      one bot per team, no humans
* ``vip_leaves``   two bots per team plus one idle "human" per team; the
                   human on team 1 is made VIP and disconnects mid-round
* ``last_human``   one idle "human" and one bot per team; both humans
                   disconnect mid-round, leaving bots only

Reported: time to the first VIP selection, whether each sub-round finished,
rounds played, and ``idle_windows``: 8-second windows in which a living bot
moved under 1.5 blocks while the round was ACTIVE and its role was not a
deliberate VIP hold (the VIP's own rally/shelter), and
``longest_quiet_active_s``: the longest ACTIVE stretch in which no bot lost
health (a stalemate measure independent of role labels).

    py -3.12 scripts/bot_vip_scenario.py --scenario bots1v1 --seconds 120
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C  # noqa: E402
from modes import get_mode_class  # noqa: E402
from modes.vip import VIPPhase  # noqa: E402
from server.bot_ai import BotDirector  # noqa: E402
from server.class_selection import normalize_class_selection  # noqa: E402
from server.config import load_config  # noqa: E402
from server.game_constants import DEFAULT_WEAPON_TOOL, TEAM1, TEAM2  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.player import Player  # noqa: E402

_HOLD_ROLES = {"vip_rally", "vip_sheltered", "vip_retreat"}


class _IdleConnection:
    """A connected but motionless human stand-in."""

    def __init__(self, server) -> None:
        self.server = server
        self.in_game = True
        self.player = None

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        return None

    def send_packet(self, packet, reliable: bool = True) -> None:
        return None

    def disconnect(self, reason: int = 0) -> None:
        self.in_game = False


async def _add_human(server, team: int, name: str) -> Player:
    connection = _IdleConnection(server)
    player = Player(server.get_next_player_id(), name, team,
                    DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(C.CLASS_GANGSTER_1)))
    server.players[player.id] = player
    server.teams[team].add_player(player)
    server.connections[object()] = connection
    server.respawn_player(player)
    await server.mode.on_player_join(player)
    return player


async def _remove_human(server, player: Player) -> None:
    """The ordinary disconnect order: mode hook while the id is still known."""
    await server.mode.on_player_leave(player)
    server.players.pop(player.id, None)
    if player.team in server.teams:
        server.teams[player.team].remove_player(player)
    player.connection.in_game = False
    server.round_lifecycle.forget_player(player)


async def _run(args) -> dict:
    random.seed(args.seed)
    config = load_config(ROOT / "config.toml")
    config.default_mode = "vip"
    config.default_map = args.map
    config.respawn_time = 3.0
    config.bots.population_mode = "admin"
    config.bots.max_bots = 8
    config.bots.seed = int(args.seed)
    config.mode_settings = dict(getattr(config, "mode_settings", {}) or {})
    config.mode_settings["vip"] = {"round_intermission": 2.0}
    if hasattr(config, "revival"):
        try:
            config.revival.enabled = False
        except AttributeError:
            pass
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("map did not load")
    mode = get_mode_class("vip")(server)
    server.mode = mode
    await mode.on_mode_start()
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)

    humans: list[Player] = []
    bots: list[Player] = []
    per_team = {"bots1v1": 1, "vip_leaves": 2, "last_human": 1}[args.scenario]
    if args.scenario in {"vip_leaves", "last_human"}:
        for team in (TEAM1, TEAM2):
            humans.append(await _add_human(server, team, f"Human{team}"))
    for team in (TEAM1, TEAM2):
        for index in range(per_team):
            bot = await director.add_bot(team=team, name=f"Bot{team}_{index}",
                                         difficulty="hard")
            if bot is None:
                raise RuntimeError("could not add bot")
            bots.append(bot)

    tick = server.tick_interval
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    steps = int(args.seconds / tick)
    leave_at = args.leave_at
    left = False
    result = {
        "scenario": args.scenario, "map": args.map, "seed": args.seed,
        "first_selection_s": None, "vip_ids": [], "rounds_played": 0,
        "round_ends_s": [], "idle_windows": 0, "phases": Counter(),
        "roles": Counter(), "left_at_s": None, "ended": False,
        "longest_quiet_active_s": 0.0,
    }
    # Longest ACTIVE stretch in which no bot lost health or died: a
    # stalemate measure that does not trust any role label.
    last_health = {bot.id: (bot.health, bot.alive) for bot in bots}
    quiet_since = None
    history = {bot.id: deque() for bot in bots}
    flagged = {bot.id: False for bot in bots}
    last_rounds = 0
    for step in range(steps):
        t = step * tick
        server.loop_count += 1
        await server.simulation_runtime.step()
        if not left and humans and t >= leave_at and mode.phase is VIPPhase.ACTIVE:
            if args.scenario == "vip_leaves":
                # Make sure the leaving human is the team-1 VIP.
                leaving = [mode.vips.get(TEAM1)]
                if leaving[0] not in humans:
                    leaving = [humans[0]]
            else:
                leaving = list(humans)
            for player in leaving:
                await _remove_human(server, player)
            left = True
            result["left_at_s"] = round(t, 2)
        if mode.phase is VIPPhase.ACTIVE and result["first_selection_s"] is None:
            result["first_selection_s"] = round(t, 2)
        vip_ids = sorted(int(v.id) for v in mode.vips.values() if v is not None)
        if vip_ids and vip_ids not in result["vip_ids"]:
            result["vip_ids"].append(vip_ids)
        if mode.rounds_played != last_rounds:
            last_rounds = mode.rounds_played
            result["round_ends_s"].append(round(t, 2))
        if step % max(1, int(0.5 / tick)) == 0:
            result["phases"][mode.phase.name] += 1
            hurt = False
            for bot in bots:
                health = (bot.health, bot.alive)
                previous = last_health[bot.id]
                if health[0] < previous[0] or (previous[1] and not health[1]):
                    hurt = True
                last_health[bot.id] = health
            if mode.phase is not VIPPhase.ACTIVE or hurt:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = t
            else:
                result["longest_quiet_active_s"] = max(
                    result["longest_quiet_active_s"], round(t - quiet_since, 1))
            for bot in bots:
                runtime = director._runtime.get(bot.id)
                intent = runtime.intent if runtime is not None else None
                role = intent.debug_role.split(":")[0] if intent is not None else "none"
                result["roles"][role] += 1
                buf = history[bot.id]
                if not (bot.alive and bot.spawned) or mode.phase is not VIPPhase.ACTIVE:
                    buf.clear()
                    continue
                buf.append((t, tuple(bot.position)))
                while buf and t - buf[0][0] > 8.0:
                    buf.popleft()
                if buf and t - buf[0][0] >= 7.5 and role not in _HOLD_ROLES:
                    moved = math.dist(buf[0][1], bot.position)
                    if moved < 1.5 and not flagged[bot.id]:
                        result["idle_windows"] += 1
                        flagged[bot.id] = True
                        if args.trace:
                            print(f"idle t={t:.1f} id={bot.id} role={role}", flush=True)
                    elif moved >= 1.5:
                        flagged[bot.id] = False
            if args.trace and step % max(1, int(2.0 / tick)) == 0:
                from server.combat_runtime import segment_clear

                living = [b for b in bots if b.alive and b.spawned]
                if len(living) == 2 and living[0].team != living[1].team:
                    print(f"t={t:5.1f} los={bool(segment_clear(server.world_manager, living[0].eye, living[1].eye))}"
                          f" dist={math.dist(living[0].position, living[1].position):.1f}", flush=True)
                for bot in bots:
                    runtime = director._runtime.get(bot.id)
                    intent = runtime.intent if runtime is not None else None
                    print(f"t={t:5.1f} phase={mode.phase.name} vips={vip_ids}"
                          f" id={bot.id} team={bot.team} alive={int(bot.alive)}"
                          f" hp={bot.health} pos=({bot.position[0]:.0f},{bot.position[1]:.0f},{bot.position[2]:.0f})"
                          f" role={intent.debug_role if intent else None}", flush=True)
        if mode.ended:
            result["ended"] = True
            break
        next_at += tick
        delay = next_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_at = loop.time()
    result["rounds_played"] = mode.rounds_played
    result["final_phase"] = mode.phase.name
    result["team_scores"] = {int(t): int(server.teams[t].score) for t in (TEAM1, TEAM2)}
    result["phases"] = dict(result["phases"])
    result["roles"] = dict(result["roles"].most_common(12))
    await director.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=("bots1v1", "vip_leaves", "last_human"),
                        default="bots1v1")
    parser.add_argument("--map", default="CityOfChicago")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--leave-at", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    text = json.dumps(result, indent=1, default=str)
    print(text)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
