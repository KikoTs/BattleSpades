"""Classify player-visible bot failures from audit_harness outputs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sys

# trace row: [t, alive, x, y, z, role, action, wade, grounded, health, tool,
#             pickup, goal, affordance, move_requested, look_target, blocks, ox, oy, oz]
T, ALIVE, X, Y, Z, ROLE, ACTION, WADE, GROUNDED, HEALTH, TOOL, PICKUP, GOAL, AFF, MOVE, LOOK, BLOCKS, OX, OY, OZ = range(20)

FALL_KILL = 7
TRANSITION_KILLS = {8, 9, 10}
HOLD_WORDS = ("defend", "guard", "watch", "hold", "arrived", "cover_", "strongpoint", "overwatch",
              "shelter", "refuge", "camp", "sentry", "heal", "wait", "rally", "regroup", "escort",
              "surround", "schematic", "fortify", "build", "repair", "mine_blocks", "siege",
              "contest", "capture_hold", "occupy", "medic", "deploy")
COMBAT_WORDS = ("combat", "cover_reload", "evade")
NAV_FAIL_WORDS = ("edge_blocked", "no_route", "planning_wait", "segment_complete", "cycle_blocked",
                  "breach_failed", "fell_past_step", "unusable_edge", "idle")


def base_role(role: str) -> str:
    return role.split(":")[0]


def is_combat(role: str) -> bool:
    return role.startswith(COMBAT_WORDS)


def lives(rows):
    """Split a bot trace into contiguous alive segments."""
    segment = []
    for row in rows:
        if row[ALIVE]:
            segment.append(row)
        elif segment:
            yield segment
            segment = []
    if segment:
        yield segment


def stationary_intervals(segment, radius=2.5, min_seconds=12.0):
    out = []
    i = 0
    n = len(segment)
    while i < n:
        ax, ay = segment[i][X], segment[i][Y]
        j = i + 1
        while j < n and math.hypot(segment[j][X] - ax, segment[j][Y] - ay) <= radius:
            j += 1
        duration = segment[j - 1][T] - segment[i][T]
        if duration >= min_seconds:
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def loop_windows(segment, window=20.0, min_path=30.0, max_net=6.0, max_radius=9.0):
    out = []
    n = len(segment)
    i = 0
    while i < n:
        j = i
        while j < n and segment[j][T] - segment[i][T] < window:
            j += 1
        if j >= n:
            break
        rows = segment[i:j + 1]
        path = sum(math.hypot(b[X] - a[X], b[Y] - a[Y]) for a, b in zip(rows, rows[1:]))
        net = math.hypot(rows[-1][X] - rows[0][X], rows[-1][Y] - rows[0][Y])
        cx = sum(r[X] for r in rows) / len(rows)
        cy = sum(r[Y] for r in rows) / len(rows)
        rad = max(math.hypot(r[X] - cx, r[Y] - cy) for r in rows)
        combat = sum(1 for r in rows if is_combat(r[ROLE])) / len(rows)
        if path >= min_path and net <= max_net and rad <= max_radius and combat < 0.3:
            out.append((i, j, path, net, rad))
            i = j
        else:
            i += max(1, int(len(rows) / 4))
    return out


def summarize_interval(rows):
    roles = Counter(r[ROLE] for r in rows)
    bases = Counter(base_role(r[ROLE]) for r in rows)
    actions = Counter(r[ACTION] for r in rows if r[ACTION] and r[ACTION] != "none")
    move = sum(r[MOVE] for r in rows) / len(rows)
    wade = sum(r[WADE] for r in rows) / len(rows)
    zs = [r[Z] for r in rows]
    return {
        "t0": rows[0][T], "t1": rows[-1][T], "seconds": round(rows[-1][T] - rows[0][T], 1),
        "pos": [rows[0][X], rows[0][Y], rows[0][Z]],
        "z_range": [min(zs), max(zs)],
        "roles": roles.most_common(4), "base_roles": bases.most_common(3),
        "actions": actions.most_common(3),
        "move_requested": round(move, 2), "wade": round(wade, 2),
        "goal": rows[-1][GOAL],
        "pickup": rows[-1][PICKUP],
    }


def classify_stationary(info) -> str:
    top = info["base_roles"][0][0] if info["base_roles"] else ""
    roles_text = " ".join(name for name, _count in info["roles"])
    combat_share = sum(c for name, c in info["base_roles"] if is_combat(name))
    total = sum(c for _n, c in info["base_roles"]) or 1
    if combat_share / total > 0.5:
        return "combat"
    if top in ("NONE", "STALE", "") or top.startswith("idle"):
        return "idle"
    if info["actions"] and info["actions"][0][0] in ("melee", "build", "build_line", "place_prefab", "deploy"):
        return "work"
    if info["move_requested"] >= 0.5:
        return "stuck_moving"
    if any(word in roles_text for word in NAV_FAIL_WORDS):
        return "nav_wait"
    if any(word in top for word in HOLD_WORDS) or "arrived" in roles_text:
        return "hold"
    return "other"


def analyze(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        r = json.load(stream)
    out = {k: r.get(k) for k in ("map", "mode", "seed", "bots", "simulated_seconds", "error",
                                  "team_scores", "final_phase", "behavior")}
    out["file"] = path.name
    sim = float(r.get("simulated_seconds") or 0.0)
    trace = {int(k): v for k, v in r["trace"].items()}
    bot_class = {}
    for bid, rows in trace.items():
        meta = r["bot_meta"].get(str(bid), {})
        bot_class[bid] = [c for _t, c in meta.get("classes", [])]
    out["classes"] = Counter(c for cl in bot_class.values() for c in cl)

    # ---- deaths ---------------------------------------------------------
    deaths = r["deaths"]
    real = [d for d in deaths if d[5] not in TRANSITION_KILLS]
    fall = [d for d in real if d[5] == FALL_KILL]
    self_kill = [d for d in real if d[4] == d[1] and d[5] != FALL_KILL]
    world = [d for d in real if d[4] == -1 and d[5] != FALL_KILL]
    team_kills = []
    final = r["final_bots"]
    team_of = {}
    for bid, rows in trace.items():
        teams = r["bot_meta"].get(str(bid), {}).get("teams", [])
        team_of[bid] = teams
    def team_at(bid, t):
        teams = team_of.get(bid) or []
        current = -1
        for when, team in teams:
            if when <= t + 0.3:
                current = team
        return current
    for d in real:
        if d[4] >= 0 and d[4] != d[1] and team_at(d[4], d[0]) == d[2] and d[2] >= 0:
            team_kills.append(d)
    out["deaths"] = len(real)
    out["death_types"] = Counter(d[5] for d in real)
    out["fall_deaths"] = [[d[0], d[1], d[3], d[6], d[7]] for d in fall]
    out["self_kills"] = [[d[0], d[1], d[3], d[5], d[7]] for d in self_kill]
    out["world_deaths"] = [[d[0], d[1], d[3], d[5], d[6], d[7]] for d in world]
    out["team_kills"] = [[d[0], d[1], d[4], d[5], d[7]] for d in team_kills]
    out["enemy_kills"] = len(real) - len(fall) - len(self_kill) - len(world) - len(team_kills)

    # ---- stationary / loops ------------------------------------------------
    stationary = []
    loops = []
    wade_seconds = Counter()
    alive_seconds = Counter()
    long_wade = []
    role_counts = Counter()
    team_role = defaultdict(Counter)
    for bid, rows in trace.items():
        for segment in lives(rows):
            if len(segment) < 2:
                continue
            for row in segment:
                role_counts[base_role(row[ROLE])] += 1
            alive_seconds[bid] += segment[-1][T] - segment[0][T]
            # wade episodes
            start = None
            for row in segment:
                if row[WADE]:
                    wade_seconds[bid] += 0.25
                    if start is None:
                        start = row
                else:
                    if start is not None and row[T] - start[T] >= 30.0:
                        long_wade.append({"bot": bid, "t0": start[T], "t1": row[T],
                                          "pos": [start[X], start[Y], start[Z]]})
                    start = None
            if start is not None and segment[-1][T] - start[T] >= 30.0:
                long_wade.append({"bot": bid, "t0": start[T], "t1": segment[-1][T],
                                  "pos": [start[X], start[Y], start[Z]], "open": True})
            for i, j in stationary_intervals(segment):
                info = summarize_interval(segment[i:j])
                info["bot"] = bid
                info["class"] = bot_class.get(bid, [-1])[-1]
                info["kind"] = classify_stationary(info)
                stationary.append(info)
            for i, j, path_len, net, rad in loop_windows(segment):
                info = summarize_interval(segment[i:j + 1])
                info.update({"bot": bid, "path": round(path_len, 1), "net": round(net, 1),
                             "radius": round(rad, 1)})
                loops.append(info)
    out["stationary"] = sorted(stationary, key=lambda s: -s["seconds"])
    out["stationary_by_kind"] = {
        kind: {"count": sum(1 for s in stationary if s["kind"] == kind),
               "seconds": round(sum(s["seconds"] for s in stationary if s["kind"] == kind), 1)}
        for kind in sorted({s["kind"] for s in stationary})
    }
    out["loops"] = sorted(loops, key=lambda s: -s["path"])
    out["wade_fraction"] = round(sum(wade_seconds.values()) / max(1.0, sum(alive_seconds.values())), 3)
    out["long_wade"] = sorted(long_wade, key=lambda s: -(s["t1"] - s["t0"]))
    out["role_counts"] = role_counts.most_common(40)
    out["alive_seconds_total"] = round(sum(alive_seconds.values()), 1)
    out["no_intent_seconds"] = {k: v for k, v in r.get("no_intent_seconds", {}).items() if v >= 3.0}

    # ---- early deaths after spawn -----------------------------------------
    spawn_deaths = []
    for bid, rows in trace.items():
        for segment in lives(rows):
            life = segment[-1][T] - segment[0][T]
            if segment[0][T] > 1.0 and life <= 6.0 and segment[-1][T] < sim - 1.0:
                spawn_deaths.append([bid, segment[0][T], round(life, 1)])
    out["short_lives"] = spawn_deaths

    # ---- actions / abilities ------------------------------------------------
    acts = Counter()
    acts_by_class = defaultdict(Counter)
    def class_at(bid, t):
        meta = r["bot_meta"].get(str(bid), {}).get("classes", [])
        current = -1
        for when, cls in meta:
            if when <= t + 0.3:
                current = cls
        return current
    for t, bid, kind, tool, accepted, argument in r["actions"]:
        key = f"{kind}:{tool}:{'ok' if accepted else 'rej'}"
        acts[key] += 1
        acts_by_class[class_at(bid, t)][key] += 1
    out["actions"] = dict(acts)
    out["actions_by_class"] = {str(k): dict(v) for k, v in acts_by_class.items()}
    out["jetpack_seconds"] = r.get("jetpack_seconds", {})
    out["parachute_seconds"] = r.get("parachute_seconds", {})
    out["tool_seconds_total"] = dict(sum((Counter({int(t): s for t, s in c.items()})
                                          for c in r.get("tool_seconds", {}).values()), Counter()))

    # ---- mode events ---------------------------------------------------------
    out["mode_events"] = Counter(e[1] for e in r["mode_events"])
    out["mode_event_times"] = {name: [e[0] for e in r["mode_events"] if e[1] == name][:12]
                               for name in out["mode_events"]}
    out["score_timeline"] = [row for i, row in enumerate(r["scores"]) if i % 60 == 0] + r["scores"][-1:]
    # objective carrier time
    carried = Counter()
    for t, objs in r["objectives"]:
        for kind, team, pos, carrier, state, progress, attacker in objs:
            if carrier >= 0:
                carried[kind] += 0.5
    out["carried_seconds"] = dict(carried)
    obj_kinds = Counter(o[0] for _t, objs in r["objectives"] for o in objs)
    out["objective_kinds"] = dict(obj_kinds)

    # ---- enemy in plain view but ignored --------------------------------------
    fire_times = defaultdict(list)
    for t, bid, kind, tool, accepted, argument in r["actions"]:
        if kind in ("fire", "melee", "oriented"):
            fire_times[bid].append(t)
    role_at = {}
    for bid, rows in trace.items():
        role_at[bid] = rows
    def roles_between(bid, t0, t1):
        return [row for row in role_at.get(bid, ()) if t0 <= row[T] <= t1]
    ignored = []
    for key, rows in r["vis"].items():
        bid = int(key)
        run = []
        def flush(run):
            if len(run) < 8:   # >= 4 s at 2 Hz
                return
            t0, t1 = run[0][0], run[-1][0]
            window = roles_between(bid, t0, t1)
            if not window:
                return
            if any(is_combat(row[ROLE]) for row in window):
                return
            if any(t0 - 0.5 <= ft <= t1 + 0.5 for ft in fire_times.get(bid, ())):
                return
            ignored.append({"bot": bid, "t0": t0, "t1": t1, "enemy": run[0][1],
                            "min_dist": min(x[2] for x in run),
                            "roles": Counter(base_role(row[ROLE]) for row in window).most_common(3),
                            "pickup": window[-1][PICKUP], "class": class_at(bid, t0)})
        previous = None
        for row in rows:
            t, enemy, dist, facing = row
            ok = dist <= 30.0 and facing >= 0.5
            if ok and previous is not None and t - previous <= 0.6:
                run.append(row)
            else:
                flush(run)
                run = [row] if ok else []
            previous = t if ok else None
        flush(run)
    out["ignored_enemy"] = sorted(ignored, key=lambda s: -(s["t1"] - s["t0"]))

    # ---- hit by enemy but no reaction ----------------------------------------
    no_reaction = []
    reacted = 0
    last_checked = defaultdict(lambda: -100.0)
    for t, victim, source, amount, kill_type in r["damages"]:
        if source < 0 or source == victim or amount <= 0 or victim not in trace:
            continue
        if team_at(source, t) == team_at(victim, t):
            continue
        if t - last_checked[victim] < 4.0:
            continue
        last_checked[victim] = t
        window = roles_between(victim, t, t + 3.0)
        if len(window) < 10 or not all(row[ALIVE] for row in window):
            continue
        fired = any(t <= ft <= t + 3.0 for ft in fire_times.get(victim, ()))
        combat = any(is_combat(row[ROLE]) for row in window)
        evade = any(("evade" in row[ROLE] or "retreat" in row[ROLE] or "cover" in row[ROLE]
                     or "flee" in row[ROLE]) for row in window)
        if fired or combat or evade:
            reacted += 1
        else:
            no_reaction.append({"t": t, "bot": victim, "source": source, "amount": amount,
                                "kill_type": kill_type,
                                "roles": Counter(base_role(row[ROLE]) for row in window).most_common(2),
                                "pickup": window[0][PICKUP], "class": class_at(victim, t)})
    out["damage_reacted"] = reacted
    out["damage_no_reaction"] = no_reaction

    # ---- per-team role mix ------------------------------------------------------
    team_mix = defaultdict(Counter)
    for bid, rows in trace.items():
        for row in rows:
            if row[ALIVE]:
                team_mix[team_at(bid, row[T])][base_role(row[ROLE])] += 1
    out["team_role_mix"] = {str(team): mix.most_common(8) for team, mix in team_mix.items()}
    out["kills_by_bot"] = {k: v["kills"] for k, v in final.items()}
    out["behavior_metrics"] = r.get("behavior_metrics", {})
    out["planning"] = r.get("planning", {})
    out["tick_ms_max"] = r.get("tick_ms_max")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    results = []
    for path in args.paths:
        try:
            results.append(analyze(path))
        except Exception as exc:  # noqa: BLE001
            print("FAILED", path, type(exc).__name__, exc, file=sys.stderr)
    if args.out:
        args.out.write_text(json.dumps(results, default=lambda o: dict(o) if isinstance(o, Counter) else str(o)), encoding="utf-8")
    for a in results:
        print(f"{a['map']:16s} {a['mode']:5s} s{a['seed']} sim={a['simulated_seconds']:.0f} "
              f"deaths={a['deaths']} fall={len(a['fall_deaths'])} self={len(a['self_kills'])} "
              f"world={len(a['world_deaths'])} tk={len(a['team_kills'])} "
              f"stat={ {k: v['seconds'] for k, v in a['stationary_by_kind'].items()} } "
              f"loops={len(a['loops'])} wade={a['wade_fraction']} "
              f"ign={len(a['ignored_enemy'])} noreact={len(a['damage_no_reaction'])}/{a['damage_reacted']} "
              f"events={dict(a['mode_events'])} scores={a['team_scores']} err={'Y' if a['error'] else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
