"""Zombie siege / pursuit scenarios on a real map with the production worker.

Idle, server-owned survivor bodies are placed on a synthetic structure
(``pillar``, ``platform``, ``tower``) or on open ground far from the horde
(``ground``). Zombie bots run through the real director, thread worker,
motor, physics, combat, dig damage and the server's floating-block collapse.

Measured per run:

* ``collapse_s``   first time a survivor loses its footing (fell 3+ blocks)
* ``first_hit_s``  first survivor damage, ``first_kill_s`` first infection
* ``pile_mean`` / ``pile_max``  zombies heaped under an elevated survivor
  (within 2.5 blocks horizontally, 3+ blocks below), sampled every 0.5 s
* ``stuck_incidents``  8-second windows in which a zombie moved < 1.5
  blocks while > 3 blocks from every survivor and not clawing terrain

``--baseline`` disables the horde coordinator (the old nearest-survivor
hunt) for a before/after comparison.

    py -3.12 scripts/bot_zombie_siege_scenario.py --map ArcticBase \
        --structure platform --zombies 8 --seconds 120 --json out.json
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
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C  # noqa: E402
from modes import get_mode_class  # noqa: E402
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase  # noqa: E402
from server.bot_ai import BotDirector  # noqa: E402
from server.class_selection import normalize_class_selection  # noqa: E402
from server.config import load_config  # noqa: E402
from server.game_constants import DEFAULT_WEAPON_TOOL  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.player import Player  # noqa: E402


class _IdleConnection:
    def __init__(self, server) -> None:
        self.server = server
        self.in_game = True
        self.player = None

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        return None

    def send_packet(self, packet, reliable: bool = True) -> None:
        return None

    def disconnect(self, reason: int = 0) -> None:
        return None


def _flat_site(world, rng: random.Random, span: int = 8, avoid=()) -> tuple[int, int, int]:
    """A dry, flat (height range <= 1) square near the map centre."""

    candidates = []
    for y in range(64, 448, 6):
        for x in range(64, 448, 6):
            if any(math.hypot(x - ax, y - ay) < 40 for ax, ay in avoid):
                continue
            heights = []
            ok = True
            for dx in range(-span, span + 1, 2):
                for dy in range(-span, span + 1, 2):
                    if world.is_water_column(x + dx, y + dy):
                        ok = False
                        break
                    heights.append(world.get_height(x + dx, y + dy))
                if not ok:
                    break
            if ok and heights and max(heights) - min(heights) <= 1 and max(heights) < 236:
                candidates.append((math.hypot(x - 256, y - 256), x, y, max(heights)))
    if not candidates:
        raise RuntimeError("no flat dry site on this map")
    candidates.sort()
    _, x, y, floor = rng.choice(candidates[: max(1, len(candidates) // 6)])
    return x, y, floor


def _build(world, structure: str, x: int, y: int, floor: int):
    """Place the structure; return survivor standing tops and built cells."""

    color = 0x777777
    tops = []
    built = []
    real_set = world.set_block

    class _Recorder:
        def set_block(self, cx, cy, cz, solid, colour):
            built.append((cx, cy, cz))
            return real_set(cx, cy, cz, solid, colour)

    world = _Recorder()
    if structure == "pillar":
        for px in (x - 3, x + 3):
            for z in range(floor - 12, floor):
                world.set_block(px, y, z, True, color)
            tops.append((px, y, floor - 12))
    elif structure == "platform":
        top = floor - 14
        for cx in range(x - 3, x + 4):
            for cy in range(y - 3, y + 4):
                world.set_block(cx, cy, top, True, color)
        for lx, ly in ((x - 3, y - 3), (x + 2, y - 3), (x - 3, y + 2), (x + 2, y + 2)):
            for cx in (lx, lx + 1):
                for cy in (ly, ly + 1):
                    for z in range(top + 1, floor):
                        world.set_block(cx, cy, z, True, color)
        tops.extend([(x, y, top), (x + 1, y - 1, top)])
    elif structure == "fortress":
        # A 5x5 solid keep, 16 high: too thick to fall from stray claws.
        top = floor - 16
        for cx in range(x - 2, x + 3):
            for cy in range(y - 2, y + 3):
                for z in range(top, floor):
                    world.set_block(cx, cy, z, True, color)
        tops.extend([(x, y, top), (x + 1, y + 1, top)])
    elif structure == "tower":
        top = floor - 14
        for cx in range(x - 1, x + 2):
            for cy in range(y - 1, y + 2):
                for z in range(top, floor):
                    world.set_block(cx, cy, z, True, color)
        tops.extend([(x, y, top), (x - 1, y + 1, top)])
    return tops, built


def _spawn_survivor(server, position) -> Player:
    connection = _IdleConnection(server)
    player = Player(server.get_next_player_id(), f"Survivor{len(server.players)}",
                    SURVIVOR_TEAM, DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(C.CLASS_SOLDIER)))
    server.players[player.id] = player
    server.teams[SURVIVOR_TEAM].add_player(player)
    server.connections[object()] = connection
    player.spawn(*position)
    server._broadcast_create_player(player, position)
    return player


async def _run(args) -> dict:
    rng = random.Random(args.seed)
    random.seed(args.seed)
    config = load_config(ROOT / "config.toml")
    config.default_mode = "zom"
    config.default_map = args.map
    config.respawn_time = 3.0
    config.bots.population_mode = "admin"
    config.bots.max_bots = int(args.zombies)
    config.bots.seed = int(args.seed)
    config.max_players = max(config.max_players, args.zombies + 8)
    if hasattr(config, "revival"):
        try:
            config.revival.enabled = False
        except AttributeError:
            pass
    if args.baseline:
        BotDirector._objectives_zombie_horde = lambda self, mode: []
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("map did not load")
    server.mode = get_mode_class("zom")(server)
    await server.mode.on_mode_start()
    world = server.world_manager
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)
    deadline = asyncio.get_running_loop().time() + 10.0
    while not director.status().running and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    x, y, floor = _flat_site(world, rng)
    survivors: list[Player] = []
    built_cells: list = []
    elevated = args.structure != "ground"
    if elevated:
        tops, built_cells = _build(world, args.structure, x, y, floor)
        for tx, ty, tz in tops[: args.survivors]:
            survivors.append(_spawn_survivor(server, (tx + 0.5, ty + 0.5, tz - 2.25)))
    else:
        used = [(x, y)]
        for _ in range(args.survivors):
            sx, sy, sf = _flat_site(world, rng, span=2, avoid=used)
            used.append((sx, sy))
            survivors.append(_spawn_survivor(server, (sx + 0.5, sy + 0.5, sf - 2.25)))
    for survivor in survivors:
        server.mode._assign_survivor(survivor)
    server.mode.phase = ZombiePhase.ACTIVE
    server.mode.start_time = time.time()
    server.mode.time_limit = 10_000
    zombies = []
    for index in range(args.zombies):
        bot = await director.add_bot(team=ZOMBIE_TEAM, name=f"Zed{index}", difficulty="hard")
        if bot is None:
            raise RuntimeError("could not add zombie bot")
        if elevated:
            angle = rng.uniform(0, 2 * math.pi)
            radius = rng.uniform(14, 24)
            zx, zy = x + radius * math.cos(angle), y + radius * math.sin(angle)
            anchor = world.dry_ground_anchor(zx, zy, search=12)
            bot.set_position(*anchor)
        zombies.append(bot)
    deadline = asyncio.get_running_loop().time() + 10.0
    while not director.status().running and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    from server.bot_ai.structure_collapse import support_cells

    supports = {s.id: support_cells(world.get_solid, s.position) for s in survivors}
    start_health = {s.id: s.health for s in survivors}
    result = {
        "map": args.map, "structure": args.structure, "zombies": args.zombies,
        "survivors": len(survivors), "baseline": bool(args.baseline), "seed": args.seed,
        "site": (x, y, floor), "collapse_s": None, "first_hit_s": None,
        "first_kill_s": None, "kills": 0, "pile_samples": [], "stuck_incidents": 0,
        "roles": Counter(), "survivor_reach_s": {},
    }
    history = {bot.id: deque() for bot in zombies}
    stuck_flag = {bot.id: False for bot in zombies}
    tick = server.tick_interval
    loop = asyncio.get_running_loop()
    next_at = loop.time()
    steps = int(args.seconds / tick)
    tick_ms = []
    for step in range(steps):
        t = step * tick
        server.loop_count += 1
        started = time.perf_counter()
        await server.simulation_runtime.step()
        tick_ms.append((time.perf_counter() - started) * 1000.0)
        living = [s for s in survivors
                  if s.alive and s.spawned and int(s.team) == SURVIVOR_TEAM]
        for s in survivors:
            support = supports.get(s.id)
            if (elevated and support and s.alive and int(s.team) == SURVIVOR_TEAM
                    and step % 6 == 0
                    and not any(world.get_solid(*cell) for cell in support)):
                # The base under this survivor fell. A real client falls with
                # it; idle bodies are client-authoritative, so drop it here.
                if result["collapse_s"] is None:
                    result["collapse_s"] = round(t, 2)
                cx, cy = int(s.position[0]), int(s.position[1])
                land = next((z for z in range(support[0][2], 240)
                             if world.get_solid(cx, cy, z)), None)
                supports[s.id] = ()
                if land is not None:
                    s.set_position(s.position[0], s.position[1], land - 2.25)
            if result["first_hit_s"] is None and (s.health < start_health[s.id] or not s.alive):
                result["first_hit_s"] = round(t, 2)
            if int(s.team) != SURVIVOR_TEAM and str(s.id) not in result["survivor_reach_s"]:
                result["survivor_reach_s"][str(s.id)] = round(t, 2)
                result["kills"] += 1
                if result["first_kill_s"] is None:
                    result["first_kill_s"] = round(t, 2)
        if step % max(1, int(0.5 / tick)) == 0:
            positions = [z.position for z in zombies if z.alive and z.spawned]
            if elevated and living:
                pile = max(
                    sum(1 for p in positions
                        if math.hypot(p[0] - s.position[0], p[1] - s.position[1]) <= 2.5
                        and p[2] - s.position[2] >= 3.0)
                    for s in living)
                result["pile_samples"].append(pile)
            for bot in zombies:
                runtime = director._runtime.get(bot.id)
                intent = runtime.intent if runtime is not None else None
                role = intent.debug_role.split(":")[0] if intent is not None else "none"
                result["roles"][role] += 1
                if not (bot.alive and bot.spawned):
                    history[bot.id].clear()
                    continue
                buf = history[bot.id]
                buf.append((t, tuple(bot.position)))
                while buf and t - buf[0][0] > 8.0:
                    buf.popleft()
                near = any(math.hypot(bot.position[0] - s.position[0],
                                      bot.position[1] - s.position[1]) <= 3.0
                           for s in living)
                # Clawing terrain on purpose (a cut, a tunnel) is work, not a
                # stall: any authoritative melee result in the last 2 s.
                clawing = (runtime is not None and runtime.feedback_action_kind == "melee"
                           and time.monotonic() - float(runtime.feedback_action_at) < 2.0)
                if buf and t - buf[0][0] >= 7.5 and living and not near and not clawing:
                    moved = math.dist(buf[0][1], bot.position)
                    if moved < 1.5 and not stuck_flag[bot.id]:
                        result["stuck_incidents"] += 1
                        stuck_flag[bot.id] = True
                    elif moved >= 1.5:
                        stuck_flag[bot.id] = False
        if args.trace and step % max(1, int(2.0 / tick)) == 0:
            orders = getattr(getattr(director, "_zombie_siege", None), "last_orders", {})
            for bot in zombies:
                runtime = director._runtime.get(bot.id)
                intent = runtime.intent if runtime is not None else None
                order = orders.get(bot.id)
                print(f"t={t:5.1f} id={bot.id} pos=({bot.position[0]:.1f},{bot.position[1]:.1f},{bot.position[2]:.1f})"
                      f" g={int(bot.grounded)} role={intent.debug_role if intent else None}"
                      f" act={intent.action.kind.value if intent else None}"
                      f" goal={tuple(round(v, 1) for v in intent.debug_goal) if intent and intent.debug_goal else None}"
                      f" order={(order.role, order.aim, tuple(round(v,1) for v in order.goal)) if order else None}"
                      f" fb={runtime.feedback_action_kind if runtime else None}:{runtime.feedback_action_accepted if runtime else None}:{runtime.feedback_reason if runtime else None}",
                      flush=True)
        if not living:
            break
        next_at += tick
        delay = next_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_at = loop.time()
    samples = result.pop("pile_samples")
    result["pile_mean"] = round(sum(samples) / len(samples), 2) if samples else 0.0
    result["pile_max"] = max(samples) if samples else 0
    result["duration_s"] = round(min(args.seconds, steps * tick), 1)
    result["roles"] = dict(result["roles"].most_common(12))
    tick_ms.sort()
    result["tick_p99_ms"] = round(tick_ms[int(0.99 * (len(tick_ms) - 1))], 2) if tick_ms else 0.0
    siege = getattr(director, "_zombie_siege", None)
    if siege is not None:
        result["siege_stats"] = dict(siege.stats)
        result["coordinator"] = {
            "stuck_events": siege.coordinator.metrics.stuck_events,
            "tunnel_orders": siege.coordinator.metrics.tunnel_orders,
        }
    result["structure_cells"] = len(built_cells)
    result["structure_cells_gone"] = sum(1 for c in built_cells if not world.get_solid(*c))
    await director.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map", default="ArcticBase")
    parser.add_argument("--structure", choices=("pillar", "platform", "tower", "fortress", "ground"),
                        default="platform")
    parser.add_argument("--zombies", type=int, default=8)
    parser.add_argument("--survivors", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--baseline", action="store_true")
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
