"""Small scripted scenarios probing what a bot notices and how it reacts.

Each scenario spawns ONE production bot (plus idle dummy bodies that stand in
for humans) and injects one stimulus through the same server services a real
player would trigger.  Reaction metrics are read from the harness trace.

    py -3.12 scripts/bot_audit_awareness.py --scenario shot_behind_los --seeds 0 1 2 3 4 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys

import bot_audit_harness as H

ROOT = H.ROOT


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


def spawn_dummy(server, team, position, class_id=0, name="Dummy"):
    import shared.constants as C
    from server.player import Player
    from server.class_selection import normalize_class_selection
    from server.game_constants import DEFAULT_WEAPON_TOOL

    connection = _IdleConnection(server)
    player = Player(server.get_next_player_id(), f"{name}{len(server.players)}", team,
                    DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(class_id)))
    server.players[player.id] = player
    server.teams[team].add_player(player)
    server.connections[object()] = connection
    player.spawn(*position)
    server._broadcast_create_player(player, position)
    return player


def unit(dx, dy):
    h = math.hypot(dx, dy)
    return (dx / h, dy / h) if h > 1e-6 else (1.0, 0.0)


def ground(world, x, y):
    return world.dry_ground_anchor(x, y, search=3)


def los(server, a, b) -> bool:
    return not server._blocked_los(a[0], a[1], a[2], b[0], b[1], b[2])


def find_spot(server, bot, heading, *, dist_range, arc, want_los, level=3.0):
    """A standing spot relative to the bot's heading with the wanted sight relation."""
    world = server.world_manager
    base = math.atan2(heading[1], heading[0])
    eye = (bot.eye_x, bot.eye_y, bot.eye_z)
    for dist in dist_range:
        for step in range(0, 9):
            for sign in (1, -1):
                angle = base + arc + sign * step * 0.12
                x = bot.x + math.cos(angle) * dist
                y = bot.y + math.sin(angle) * dist
                if not (8 < x < 504 and 8 < y < 504):
                    continue
                try:
                    spot = ground(world, x, y)
                except Exception:  # noqa: BLE001
                    continue
                if world.is_water_column(int(spot[0]), int(spot[1])):
                    continue
                if abs(spot[2] - bot.z) > level and want_los is not False:
                    continue
                target_eye = (spot[0], spot[1], spot[2])
                if los(server, eye, target_eye) == bool(want_los):
                    if math.dist(spot[:2], (bot.x, bot.y)) >= min(dist_range) * 0.8:
                        return spot
    return None


def publish_shot(server, shooter):
    from server.bot_ai.messages import StimulusKind
    server.bot_stimuli.publish(
        StimulusKind.SHOT, (float(shooter.eye_x), float(shooter.eye_y), float(shooter.eye_z)),
        source_id=int(shooter.id), team=int(shooter.team), radius=72.0, lifetime=1.25)


def make_scenario(kind: str, *, mode: str):
    async def scenario(server, director, clock, state):
        import shared.constants as C
        from server.game_constants import TEAM1, TEAM2

        bot = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        state.update({"kind": kind, "bot": int(bot.id), "events": [], "dummies": {}})
        ctx = {"placed": False, "done": False, "next": 0.0, "count": 0, "enemy": None}
        world = server.world_manager

        def heading():
            runtime = director._runtime.get(int(bot.id))
            intent = runtime.intent if runtime else None
            if intent is not None and math.hypot(intent.movement.direction[0], intent.movement.direction[1]) > 0.1:
                return unit(intent.movement.direction[0], intent.movement.direction[1])
            return unit(bot.o_x, bot.o_y)

        def log(t, name, **extra):
            state["events"].append({"t": round(t, 2), "name": name, **extra})

        def remember(player, label):
            state["dummies"][label] = {"id": int(player.id), "pos": [round(float(v), 2) for v in player.position],
                                       "team": int(player.team)}

        def on_tick(tick, t):
            if not (bot.alive and bot.spawned):
                return
            h = heading()
            if kind in ("shot_behind_los", "shot_behind_nolos", "gunfire_unseen", "teammate_killed"):
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    want = kind in ("shot_behind_los", "teammate_killed")
                    spot = find_spot(server, bot, h, dist_range=(30, 26, 34, 38) if kind != "teammate_killed" else (45, 40, 50, 36),
                                     arc=math.pi, want_los=want, level=4.0 if want else 30.0)
                    if spot is not None:
                        enemy = spawn_dummy(server, TEAM2, spot, name="Enemy")
                        ctx["enemy"] = enemy
                        remember(enemy, "enemy")
                        if kind == "teammate_killed":
                            side = (-h[1], h[0])
                            ally_spot = ground(world, bot.x + side[0] * 3.0, bot.y + side[1] * 3.0)
                            ally = spawn_dummy(server, TEAM1, ally_spot, name="Ally")
                            ctx["ally"] = ally
                            remember(ally, "ally")
                        ctx["placed"] = True
                        ctx["next"] = t + 0.5
                        log(t, "placed", dist=round(math.dist(spot[:2], (bot.x, bot.y)), 1),
                            bot_pos=[round(bot.x, 1), round(bot.y, 1), round(bot.z, 1)])
                elif ctx["placed"] and not ctx["done"] and t >= ctx["next"]:
                    enemy = ctx["enemy"]
                    publish_shot(server, enemy)
                    if kind in ("shot_behind_los", "shot_behind_nolos"):
                        bot.damage(12, enemy, int(C.WEAPON_KILL))
                        log(t, "shot_hit", hp=int(bot.health))
                    elif kind == "teammate_killed":
                        ally = ctx["ally"]
                        if ally.alive:
                            ally.damage(200, enemy, int(C.HEADSHOT_KILL))
                            log(t, "ally_killed")
                    else:
                        log(t, "shot_heard")
                    ctx["count"] += 1
                    ctx["next"] = t + 0.6
                    limit = 1 if kind == "teammate_killed" else 5
                    if ctx["count"] >= limit:
                        ctx["done"] = True
            elif kind == "grenade_feet":
                if not ctx["placed"] and t >= 4.0:
                    proj = server.projectile_engine.spawn(
                        int(C.GRENADE_TOOL), (bot.x + h[0] * 1.0, bot.y + h[1] * 1.0, bot.z - 0.5),
                        (0.0, 0.0, 0.0), 2.5, 250)
                    ctx["placed"] = True
                    ctx["origin"] = (bot.x + h[0], bot.y + h[1])
                    log(t, "grenade", pos=[round(bot.x + h[0], 1), round(bot.y + h[1], 1)],
                        ok=proj is not None)
                elif ctx["placed"] and not ctx["done"] and t >= 6.5:
                    log(t, "after_fuse", dist=round(math.dist(ctx["origin"], (bot.x, bot.y)), 1),
                        hp=int(bot.health))
                    ctx["done"] = True
            elif kind == "grenade_path":
                if not ctx["placed"] and t >= 4.0:
                    runtime = director._runtime.get(int(bot.id))
                    intent = runtime.intent if runtime else None
                    if intent is None or math.hypot(intent.movement.direction[0], intent.movement.direction[1]) < 0.1:
                        return
                    gx, gy = bot.x + h[0] * 10.0, bot.y + h[1] * 10.0
                    spot = ground(world, gx, gy)
                    proj = server.projectile_engine.spawn(
                        int(C.GRENADE_TOOL), (spot[0], spot[1], spot[2] + 1.5),
                        (0.0, 0.0, 0.0), 1.9, 250)
                    ctx["placed"] = True
                    ctx["origin"] = (spot[0], spot[1])
                    ctx["t"] = t
                    log(t, "grenade", pos=[round(spot[0], 1), round(spot[1], 1)], ok=proj is not None,
                        dist=round(math.dist(spot[:2], (bot.x, bot.y)), 1), hp=int(bot.health))
                elif ctx["placed"] and not ctx["done"] and t >= ctx["t"] + 1.85:
                    log(t, "at_fuse", dist=round(math.dist(ctx["origin"], (bot.x, bot.y)), 1), hp=int(bot.health))
                    ctx["done"] = True
            elif kind == "rocket_incoming":
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    spot = find_spot(server, bot, h, dist_range=(45, 40, 50, 36), arc=0.0, want_los=True, level=4.0)
                    if spot is not None:
                        enemy = spawn_dummy(server, TEAM2, spot, class_id=int(C.CLASS_ROCKETEER), name="Rocket")
                        ctx["enemy"] = enemy
                        remember(enemy, "enemy")
                        dx, dy, dz = bot.x - spot[0], bot.y - spot[1], (bot.z + 1.0) - spot[2]
                        d = math.sqrt(dx * dx + dy * dy + dz * dz)
                        speed = float(getattr(C, "RPG_SPEED", getattr(C, "ROCKET_SPEED", 60.0)))
                        proj = server.projectile_engine.spawn(
                            int(C.RPG_TOOL), (spot[0], spot[1], spot[2]),
                            (dx / d * speed, dy / d * speed, dz / d * speed), 0.0, int(enemy.id))
                        ctx["placed"] = True
                        ctx["target"] = (bot.x, bot.y)
                        log(t, "rocket", dist=round(d, 1), speed=speed, ok=proj is not None,
                            eta=round(d / speed, 2))
                elif ctx["placed"] and not ctx["done"] and t >= state["events"][0]["t"] + 3.0:
                    log(t, "after", moved=round(math.dist(ctx["target"], (bot.x, bot.y)), 1), hp=int(bot.health))
                    ctx["done"] = True
            elif kind in ("mine_ahead", "turret_ahead"):
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    from server.entities.behaviors import ProximityMineBehavior
                    from server.connection import internal_team_to_wire
                    from server.game_constants import KILL_TYPES
                    from server.weapons_retail import RETAIL_EXPLOSIONS_BY_NAME
                    far = find_spot(server, bot, h, dist_range=(60, 50, 70), arc=math.pi, want_los=False, level=40.0)
                    if far is None:
                        far = ground(world, bot.x - h[0] * 60, bot.y - h[1] * 60)
                    owner = spawn_dummy(server, TEAM2, far, class_id=int(C.CLASS_ENGINEER if kind == "turret_ahead" else C.CLASS_SCOUT), name="Owner")
                    ctx["enemy"] = owner
                    remember(owner, "owner")
                    if kind == "mine_ahead":
                        spot = ground(world, bot.x + h[0] * 8.0, bot.y + h[1] * 8.0)
                        pos = (spot[0], spot[1], spot[2] + 2.25 - 0.05)
                        behavior = ProximityMineBehavior(
                            owner.id, owner.team,
                            damage=float(getattr(C, "LANDMINE_EXPLOSION_DAMAGE", 100.0)),
                            block_damage=float(getattr(C, "LANDMINE_EXPLOSION_BLOCK_DAMAGE", 15.0)),
                            crater_radius=1, kill_type=KILL_TYPES.get("LANDMINE_KILL", 14),
                            trigger_radius=float(getattr(C, "LANDMINE_DETECTION_RANGE", 2.5)),
                            arm_delay=0.2,
                            blast_radius=RETAIL_EXPLOSIONS_BY_NAME["landmine"].radius,
                            force_destroy=False,
                            detection_layers=int(getattr(C, "LANDMINE_DETECTION_LAYERS", 3)),
                            health=float(getattr(C, "LANDMINE_HEALTH", 1.0)),
                            vertical_offset=float(getattr(C, "LANDMINE_EXPLOSION_AND_DETECTION_VERTICAL_OFFSET", -0.5)),
                            damage_type=int(getattr(C, "LANDMINE_DAMAGE", 15)),
                        )
                        entity = server.entity_registry.place(
                            int(getattr(C, "LANDMINE_ENTITY", 9)), *pos,
                            state=internal_team_to_wire(owner.team), kind="deployable",
                            player_id=owner.id, behavior=behavior)
                        server.broadcast_create_entity(entity)
                        ctx["target"] = pos
                        log(t, "mine", pos=[round(v, 1) for v in pos], dist=8.0)
                    else:
                        spot = find_spot(server, bot, h, dist_range=(28, 24, 32), arc=0.0, want_los=True, level=3.0)
                        if spot is None:
                            return
                        pos = (spot[0], spot[1], spot[2] + 2.25 - 0.05)
                        owner.rocket_turret_stock = 1
                        yaw = math.atan2(bot.y - pos[1], bot.x - pos[0])
                        turret = server.rocket_turret_controller.place(owner, pos, yaw, now=H.CLOCK.monotonic())
                        ctx["target"] = pos
                        log(t, "turret", pos=[round(v, 1) for v in pos], ok=turret is not None,
                            dist=round(math.dist(pos[:2], (bot.x, bot.y)), 1))
                    ctx["placed"] = True
            elif kind == "outnumbered":
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    spots = []
                    for arc in (-0.25, -0.08, 0.08, 0.25):
                        spot = find_spot(server, bot, h, dist_range=(34, 30, 38), arc=arc, want_los=True, level=4.0)
                        if spot is not None:
                            spots.append(spot)
                    if len(spots) >= 3:
                        ctx["enemies"] = [spawn_dummy(server, TEAM2, spot, name="Foe") for spot in spots]
                        for index, enemy in enumerate(ctx["enemies"]):
                            remember(enemy, f"enemy{index}")
                        ctx["placed"] = True
                        ctx["next"] = t + 1.0
                        ctx["origin"] = (bot.x, bot.y)
                        log(t, "placed", count=len(spots))
                elif ctx["placed"] and t >= ctx["next"] and ctx["count"] < 12:
                    alive = [e for e in ctx["enemies"] if e.alive]
                    for enemy in alive:
                        publish_shot(server, enemy)
                        bot.damage(4, enemy, int(C.WEAPON_KILL))
                    ctx["count"] += 1
                    ctx["next"] = t + 0.7
                    cx = sum(e.x for e in ctx["enemies"]) / len(ctx["enemies"])
                    cy = sum(e.y for e in ctx["enemies"]) / len(ctx["enemies"])
                    log(t, "volley", hp=int(bot.health), enemies_alive=len(alive),
                        dist=round(math.dist((cx, cy), (bot.x, bot.y)), 1))
            elif kind in ("low_health_crate_los", "low_health_crate_hidden", "no_ammo_crate_los"):
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    from server.entities.behaviors import PickupCrateBehavior
                    want = kind != "low_health_crate_hidden"
                    spot = find_spot(server, bot, h, dist_range=(14, 12, 16, 18), arc=math.pi / 2,
                                     want_los=want, level=3.0 if want else 30.0)
                    if spot is None:
                        return
                    pos = (spot[0], spot[1], spot[2] + 2.25 - 0.05)
                    if kind == "no_ammo_crate_los":
                        bot.ammo_clip = 0
                        bot.ammo_reserve = 0
                        stowed = getattr(bot, "_weapon_ammo", None)
                        if isinstance(stowed, dict):
                            for tool in list(stowed):
                                stowed[tool] = (0, 0)
                        crate_type = int(C.AMMO_CRATE)
                        refill = lambda player: player.restock_ammo(int(C.AMMO_CRATE))  # noqa: E731
                    else:
                        bot.health = 25
                        crate_type = int(C.HEALTH_CRATE)
                        refill = lambda player: player.heal(100)  # noqa: E731
                    def counted(player, refill=refill):
                        state["events"].append({"t": round(H.CLOCK.elapsed, 2), "name": "crate_taken",
                                                "by": int(player.id)})
                        return refill(player)
                    entity = server.entity_registry.place(
                        crate_type, *pos, kind="pickup",
                        behavior=PickupCrateBehavior(counted, respawn_delay=600.0))
                    server.broadcast_create_entity(entity)
                    ctx["placed"] = True
                    ctx["target"] = pos
                    log(t, "crate", pos=[round(v, 1) for v in pos],
                        dist=round(math.dist(pos[:2], (bot.x, bot.y)), 1), hp=int(bot.health))

        return on_tick

    return scenario


def reaction(result) -> dict:
    scenario = result["scenario"]
    bot = str(scenario["bot"])
    rows = result["trace"].get(bot, [])
    events = scenario["events"]
    out = {"kind": scenario["kind"], "events": events[:8]}
    if not events:
        out["note"] = "stimulus never placed"
        return out
    t0 = next((e["t"] for e in events if e["name"] not in ("placed",)), events[0]["t"])
    out["t0"] = t0
    threat = None
    for label in ("enemy", "enemy1", "owner"):
        if label in scenario["dummies"]:
            threat = scenario["dummies"][label]
            break
    after = [row for row in rows if row[0] >= t0]
    before = [row for row in rows if t0 - 2.0 <= row[0] < t0]
    out["roles_before"] = sorted({row[5].split(":")[0] for row in before})
    seq = []
    for row in after[:60]:
        role = row[5]
        if not seq or seq[-1][1] != role:
            seq.append((round(row[0] - t0, 2), role))
    out["role_sequence"] = seq[:10]
    if threat is not None:
        tp = threat["pos"]
        t_face = None
        for row in after:
            dx, dy = tp[0] - row[2], tp[1] - row[3]
            h = math.hypot(dx, dy)
            if h < 1e-6:
                continue
            dot = (row[17] * dx + row[18] * dy) / h
            if dot >= 0.8:
                t_face = round(row[0] - t0, 2)
                break
        out["t_face_threat"] = t_face
        out["t_target_lock"] = next((round(row[0] - t0, 2) for row in after if row[15] == threat["id"]), None)
    fire = [a for a in result["actions"] if a[1] == int(bot) and a[0] >= t0 and a[2] in ("fire", "oriented", "melee")]
    out["t_first_attack"] = round(fire[0][0] - t0, 2) if fire else None
    out["attacks"] = len(fire)
    alive_after = [row for row in after if row[1]]
    out["survived_seconds"] = round(alive_after[-1][0] - t0, 1) if alive_after else 0.0
    out["bot_deaths"] = [[d[0], d[4], d[5], d[7]] for d in result["deaths"] if d[1] == int(bot)]
    out["hp_min"] = min((row[9] for row in alive_after), default=None)
    if alive_after:
        out["moved_3s"] = round(math.dist(alive_after[0][2:4], alive_after[min(len(alive_after) - 1, 12)][2:4]), 1)
    return out


async def run(kind, seed, mode, map_name, seconds):
    result = await H.run_case(map_name=map_name, mode_name=mode, seed=seed, seconds=seconds, bots=0,
                              out=None, natural_limits=False, scenario=make_scenario(kind, mode=mode), max_bots=4)
    if result["error"]:
        return {"kind": kind, "seed": seed, "error": result["error"][-800:]}
    out = reaction(result)
    out["seed"] = seed
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", action="append", required=True)
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3])
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--map", default="ArcticBase")
    parser.add_argument("--seconds", type=float, default=22.0)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.json is not None and not args.json.is_absolute():
        args.json = H.ORIGINAL_CWD / args.json
    results = []
    for kind in args.scenario:
        for seed in args.seeds:
            r = asyncio.run(run(kind, seed, args.mode, args.map, args.seconds))
            results.append(r)
            print(json.dumps(r, default=str), flush=True)
    H.uninstall_clock()
    if args.json:
        args.json.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
