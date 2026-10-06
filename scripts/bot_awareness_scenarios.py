"""Scripted scenarios for bot reactions whose outcome depends on what the bot does.

``bot_audit_awareness`` injects a stimulus and reports whether the bot reacted.
Its scripted shooters hit the bot whatever it does, so it cannot say whether a
reaction helped. The scenarios here close that loop: a shooter only hits a bot
it has a sight line to, fire only burns a bot that walks into it. Every run
samples the bot six times a second (role, health, how many shooters see it).

    py -3.12 scripts/bot_awareness_scenarios.py --scenario outnumbered_los --seeds 0 1 2 3 4 5 6 7
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import statistics

import bot_audit_awareness as A
import bot_audit_harness as H

SAMPLE_TICKS = 10


def _eye(player):
    return (float(player.eye_x), float(player.eye_y), float(player.eye_z))


def _sees(server, shooter, bot, reach=70.0) -> bool:
    return (shooter.alive and math.dist(_eye(shooter), _eye(bot)) <= reach
            and A.los(server, _eye(shooter), _eye(bot)))


def _role(director, bot) -> str:
    runtime = director._runtime.get(int(bot.id))
    intent = runtime.intent if runtime is not None else None
    return str(getattr(intent, "debug_role", "") or "")


def _heading(director, bot):
    runtime = director._runtime.get(int(bot.id))
    intent = runtime.intent if runtime is not None else None
    if intent is not None and math.hypot(*intent.movement.direction[:2]) > 0.1:
        return A.unit(intent.movement.direction[0], intent.movement.direction[1])
    return A.unit(bot.o_x, bot.o_y)


def make_scenario(kind: str):
    async def scenario(server, director, clock, state):
        import shared.constants as C
        from server.game_constants import TEAM1, TEAM2

        bot = await director.add_bot(team=TEAM1, class_id=int(C.CLASS_SOLDIER))
        state.update({"kind": kind, "bot": int(bot.id), "events": [], "samples": [],
                      "dummies": {}})
        ctx = {"placed": False, "next": 0.0, "volleys": 0, "shooters": [], "t0": None}
        world = server.world_manager

        def log(t, name, **extra):
            state["events"].append({"t": round(t, 2), "name": name, **extra})

        def remember(player, label):
            state["dummies"][label] = {
                "id": int(player.id), "team": int(player.team),
                "pos": [round(float(v), 2) for v in player.position]}

        def place_shooters(t, arcs, distances, label="enemy"):
            heading = _heading(director, bot)
            spots = []
            for arc in arcs:
                spot = A.find_spot(server, bot, heading, dist_range=distances, arc=arc,
                                   want_los=True, level=4.0)
                if spot is not None:
                    spots.append(spot)
            if len(spots) < len(arcs):
                return False
            ctx["shooters"] = [A.spawn_dummy(server, TEAM2, spot, name="Foe") for spot in spots]
            for index, shooter in enumerate(ctx["shooters"]):
                remember(shooter, f"{label}{index}")
            return True

        def volley(t, damage):
            seeing = [s for s in ctx["shooters"] if _sees(server, s, bot)]
            for shooter in ctx["shooters"]:
                if shooter.alive:
                    A.publish_shot(server, shooter)
            for shooter in seeing:
                bot.damage(damage, shooter, int(C.WEAPON_KILL))
            ctx["volleys"] += 1
            log(t, "volley", hp=int(bot.health), seen_by=len(seeing),
                alive=sum(1 for s in ctx["shooters"] if s.alive))

        def on_tick(tick, t):
            if tick % SAMPLE_TICKS == 0 and ctx["t0"] is not None:
                state["samples"].append([
                    round(t, 2), 1 if (bot.alive and bot.spawned) else 0, int(bot.health),
                    _role(director, bot),
                    sum(1 for s in ctx["shooters"] if _sees(server, s, bot)),
                    round(float(bot.x), 1), round(float(bot.y), 1)])
            if not (bot.alive and bot.spawned):
                return
            if kind in ("outnumbered_los", "outnumbered_allies"):
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    if not place_shooters(t, (-0.25, -0.08, 0.08, 0.25), (34, 30, 38)):
                        return
                    if kind == "outnumbered_allies":
                        heading = _heading(director, bot)
                        for index, side in enumerate((-4.0, 4.0)):
                            spot = A.ground(world, bot.x - heading[0] * 26.0 - heading[1] * side,
                                            bot.y - heading[1] * 26.0 + heading[0] * side)
                            remember(A.spawn_dummy(server, TEAM1, spot, name="Ally"), f"ally{index}")
                    ctx["placed"], ctx["next"], ctx["t0"] = True, t + 1.0, t + 1.0
                    log(t, "placed", count=len(ctx["shooters"]))
                elif ctx["placed"] and t >= ctx["next"] and ctx["volleys"] < 20:
                    volley(t, 4)
                    ctx["next"] = t + 0.7
            elif kind == "wounded_duel":
                if not ctx["placed"] and t >= 4.0 and tick % 15 == 0:
                    if not place_shooters(t, (0.0,), (30, 26, 34)):
                        return
                    bot.health = 30
                    ctx["placed"], ctx["next"], ctx["t0"] = True, t + 1.0, t + 1.0
                    log(t, "placed", hp=int(bot.health))
                elif ctx["placed"] and t >= ctx["next"] and ctx["volleys"] < 20:
                    volley(t, 5)
                    ctx["next"] = t + 0.7

        return on_tick

    return scenario


def measure(result) -> dict:
    scenario = result["scenario"]
    samples = scenario.get("samples", [])
    events = scenario.get("events", [])
    out = {"kind": scenario.get("kind"), "events": len(events)}
    if not samples:
        out["note"] = "stimulus never placed"
        return out
    t0 = samples[0][0]
    alive = [row for row in samples if row[1]]
    roles = []
    for row in alive:
        role = row[3].split(":")[0]
        if not roles or roles[-1][1] != role:
            roles.append((round(row[0] - t0, 2), role))
    out["roles"] = roles[:12]
    out["hp_min"] = min((row[2] for row in alive), default=0)
    out["died"] = any(not row[1] for row in samples)
    out["survived_seconds"] = round(alive[-1][0] - t0, 1) if alive else 0.0
    out["exposed_share"] = round(sum(1 for row in alive if row[4] > 0) / max(1, len(alive)), 2)
    hidden = next((row[0] for row in alive if row[4] == 0), None)
    out["t_out_of_sight"] = round(hidden - t0, 2) if hidden is not None else None
    volleys = [e for e in events if e["name"] == "volley"]
    out["hits_taken"] = sum(e["seen_by"] for e in volleys)
    out["shooters_killed"] = (volleys[0]["alive"] - volleys[-1]["alive"]) if volleys else 0
    out["moved"] = round(math.dist(alive[0][5:7], alive[-1][5:7]), 1) if alive else 0.0
    reacted = next((row for row in alive if row[3].startswith(REACTION_ROLES)), None)
    out["t_react"] = round(reacted[0] - t0, 2) if reacted is not None else None
    return out


REACTION_ROLES = ("disengage", "fall_back", "regroup", "under_fire", "cover_", "avoid_",
                  "ally_down", "alert_", "mine_", "turret_", "tdm_regroup")


async def run(kind, seed, mode, map_name, seconds):
    result = await H.run_case(map_name=map_name, mode_name=mode, seed=seed, seconds=seconds,
                              bots=0, out=None, natural_limits=False,
                              scenario=make_scenario(kind), max_bots=6)
    if result["error"]:
        return {"kind": kind, "seed": seed, "error": result["error"][-800:]}
    out = measure(result)
    out["seed"] = seed
    return out


def summarise(kind, rows) -> dict:
    rows = [row for row in rows if "error" not in row and "note" not in row]
    reacted = [row["t_react"] for row in rows if row.get("t_react") is not None]
    hidden = [row["t_out_of_sight"] for row in rows if row.get("t_out_of_sight") is not None]
    return {
        "kind": kind, "runs": len(rows),
        "reacted": len(reacted),
        "t_react_median": round(statistics.median(reacted), 2) if reacted else None,
        "out_of_sight": len(hidden),
        "t_out_of_sight_median": round(statistics.median(hidden), 2) if hidden else None,
        "died": sum(1 for row in rows if row["died"]),
        "hp_min_median": statistics.median(row["hp_min"] for row in rows) if rows else None,
        "hits_taken_mean": round(statistics.fmean(row["hits_taken"] for row in rows), 1) if rows else None,
        "exposed_share_mean": round(statistics.fmean(row["exposed_share"] for row in rows), 2) if rows else None,
        "shooters_killed": sum(row["shooters_killed"] for row in rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", action="append", required=True)
    parser.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4, 5, 6, 7])
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--map", default="ArcticBase")
    parser.add_argument("--seconds", type=float, default=26.0)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.json is not None and not args.json.is_absolute():
        args.json = H.ORIGINAL_CWD / args.json
    results, summaries = [], []
    for kind in args.scenario:
        rows = []
        for seed in args.seeds:
            row = asyncio.run(run(kind, seed, args.mode, args.map, args.seconds))
            rows.append(row)
            print(json.dumps(row, default=str), flush=True)
        results.extend(rows)
        summaries.append(summarise(kind, rows))
    H.uninstall_clock()
    for summary in summaries:
        print("SUMMARY " + json.dumps(summary), flush=True)
    if args.json:
        args.json.write_text(json.dumps({"runs": results, "summary": summaries}, indent=1,
                                        default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
