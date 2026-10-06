"""Mode-specific objective outcomes and cross-case incident detectors."""

from __future__ import annotations

from collections import Counter, defaultdict
import glob
import gzip
import json
import math
import os
import sys

RUNS = os.environ.get("BOT_AUDIT_RUNS", "tmp/bot-audit")

T, ALIVE, X, Y, Z, ROLE, ACTION, WADE, GROUNDED, HEALTH, TOOL, PICKUP, GOAL, AFF, MOVE, LOOK, BLOCKS, OX, OY, OZ = range(20)
KT = {0: 'weapon', 1: 'headshot', 2: 'melee', 3: 'grenade', 4: 'rocket', 5: 'rocket2', 6: 'drill', 7: 'fall',
      11: 'entity', 14: 'landmine', 15: 'dynamite', 16: 'airstrike', 17: 'bomb', 18: 'turret', 19: 'shrapnel',
      22: 'cgrenade', 23: 'apgrenade', 24: 'molotov', 25: 'blockfire', 26: 'vipmode', 31: 'chem',
      32: 'glauncher', 34: 'sticky', 35: 'mine', 36: 'c4'}


def base(role):
    return role.split(":")[0]


def load(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def end_time(r):
    """First match end; later data belongs to a restarted round with unhooked mode."""
    for t, name, _args in r["mode_events"]:
        if name == "on_mode_end":
            return float(t)
    return float(r.get("simulated_seconds") or 0.0)


def team_of(r, bid, t):
    current = -1
    for when, team in r["bot_meta"].get(str(bid), {}).get("teams", []):
        if when <= t + 0.3:
            current = team
    return current


def class_of(r, bid, t):
    current = -1
    for when, cls in r["bot_meta"].get(str(bid), {}).get("classes", []):
        if when <= t + 0.3:
            current = cls
    return current


def objective_positions(r, kind):
    out = {}
    for _t, objs in r["objectives"][:6]:
        for o in objs:
            if o[0] == kind:
                out.setdefault(o[1], o[2])
    return out


def case(path):
    r = load(path)
    tend = end_time(r)
    mode = r["mode"]
    trace = {int(k): [row for row in v if row[T] <= tend] for k, v in r["trace"].items()}
    events = [e for e in r["mode_events"] if e[0] <= tend]
    deaths = [d for d in r["deaths"] if d[0] <= tend and d[5] not in (8, 9, 10)]
    out = {"file": path.replace("\\", "/").split("/")[-1], "map": r["map"], "mode": mode, "seed": r["seed"],
           "t_end": tend, "error": bool(r["error"]), "scores_at_end": None}
    for t, sc, ph in r["scores"]:
        if t <= tend:
            out["scores_at_end"] = sc
    ev = Counter(e[1] for e in events)
    out["events"] = dict(ev)
    kills = [d for d in deaths if d[4] >= 0 and d[4] != d[1]]
    out["kills"] = len(kills)
    out["kills_per_min"] = round(len(kills) / max(1.0, tend / 60.0), 2)
    out["deaths_by_type"] = dict(Counter(KT.get(d[5], d[5]) for d in deaths))
    out["self_or_world"] = [[d[0], d[1], class_of(r, d[1], d[0]), KT.get(d[5], d[5]), d[7], d[6]]
                            for d in deaths if d[4] == -1 or d[4] == d[1]]

    # ---- under-goal / level mismatch stalls --------------------------------
    under = []
    for bid, rows in trace.items():
        run = []
        def flush(run):
            if len(run) >= 40:   # >= 10 s
                under.append({"bot": bid, "t0": run[0][T], "t1": run[-1][T],
                              "pos": [run[0][X], run[0][Y], run[0][Z]], "goal": run[0][GOAL],
                              "roles": Counter(row[ROLE] for row in run).most_common(3),
                              "pickup": run[-1][PICKUP]})
        for row in rows:
            g = row[GOAL]
            ok = (row[ALIVE] and g is not None and math.hypot(g[0] - row[X], g[1] - row[Y]) <= 7.0
                  and abs(g[2] - row[Z]) >= 5.0)
            if ok:
                run.append(row)
            else:
                flush(run)
                run = []
        flush(run)
    out["under_goal"] = under

    # ---- role time by family per team ----------------------------------------
    mix = defaultdict(Counter)
    for bid, rows in trace.items():
        for row in rows:
            if row[ALIVE]:
                mix[team_of(r, bid, row[T])][base(row[ROLE])] += 1
    out["team_roles"] = {str(k): v.most_common(10) for k, v in mix.items()}
    total = sum(sum(v.values()) for v in mix.values()) or 1
    fam = Counter()
    for v in mix.values():
        for role, n in v.items():
            if role.startswith(("pit_climb", "water_pit_climb", "climb_out", "water_climb_out")):
                fam["climb_skill"] += n
            elif role.startswith("water"):
                fam["water"] += n
            elif role.startswith("combat") or role in ("chase_last_seen", "cover_reload"):
                fam["combat"] += n
            elif role in ("NONE", "STALE") or role.startswith("idle"):
                fam["idle"] += n
    out["family_share"] = {k: round(v / total, 3) for k, v in fam.items()}
    sub = Counter()
    for bid, rows in trace.items():
        for row in rows:
            if row[ALIVE] and ":" in row[ROLE]:
                sub[row[ROLE].split(":")[-1]] += 1
    out["subrole_share"] = {k: round(v / total, 3) for k, v in sub.most_common(12)}

    # ---- mode specific ---------------------------------------------------------
    if mode in ("ctf", "cctf"):
        bases = objective_positions(r, "ctf_base")
        pend = {}
        carries = []
        for t, name, args in events:
            if name == "_pickup_intel":
                pend[args[0]["p"]] = (t, args[0]["team"])
            elif name in ("_drop_intel", "_capture_intel") and isinstance(args[0], dict):
                pid = args[0]["p"]
                if pid in pend:
                    t0, team = pend.pop(pid)
                    rows = [x for x in trace.get(pid, []) if t0 <= x[T] <= t]
                    b = bases.get(team)
                    dmin = min((math.hypot(x[X] - b[0], x[Y] - b[1]) for x in rows), default=-1) if b else -1
                    dzmin = min((abs(x[Z] - b[2]) for x in rows if math.hypot(x[X] - b[0], x[Y] - b[1]) <= 8), default=None) if b else None
                    death = next((d for d in deaths if d[1] == pid and abs(d[0] - t) < 0.6), None)
                    carries.append({"bot": pid, "team": team, "t0": t0, "t1": t,
                                    "result": "capture" if name == "_capture_intel" else "drop",
                                    "min_xy_to_base": round(dmin, 1), "dz_at_base": dzmin,
                                    "death": KT.get(death[5], death[5]) if death else None})
        out["ctf"] = {
            "pickups": ev.get("_pickup_intel", 0), "captures": ev.get("_capture_intel", 0),
            "returns": ev.get("_return_intel", 0),
            "pickups_by_team": dict(Counter(e[2][0]["team"] for e in events if e[1] == "_pickup_intel")),
            "first_pickup": next((e[0] for e in events if e[1] == "_pickup_intel"), None),
            "at_base_no_capture": [c for c in carries if c["result"] == "drop" and 0 <= c["min_xy_to_base"] <= 8],
            "carry_seconds": round(sum(c["t1"] - c["t0"] for c in carries), 1),
            "bases": bases,
        }
    elif mode in ("mh", "tc", "oc", "dem", "dia", "vip"):
        kind = {"mh": "mh_hill", "tc": "tc_territory", "oc": "oc_target", "dem": "dem_base",
                "dia": "dia_dropoff", "vip": "vip"}[mode]
        # presence: per team, share of objective samples with a bot within 10 blocks XY (and 8 Z)
        presence = Counter()
        samples = 0
        closest = defaultdict(lambda: 1e9)
        index = {bid: {round(row[T] * 2) / 2: row for row in rows if abs(row[T] * 2 - round(row[T] * 2)) < 1e-6}
                 for bid, rows in trace.items()}
        for t, objs in r["objectives"]:
            if t > tend:
                break
            targets = [o for o in objs if o[0] == kind]
            if not targets:
                continue
            samples += 1
            seen = set()
            for bid, rows_by_t in index.items():
                row = rows_by_t.get(t)
                if row is None or not row[ALIVE]:
                    continue
                team = team_of(r, bid, t)
                for o in targets:
                    d = math.hypot(row[X] - o[2][0], row[Y] - o[2][1])
                    key = (team, o[1])
                    closest[key] = min(closest[key], d)
                    if d <= 10.0 and abs(row[Z] - o[2][2]) <= 8.0:
                        seen.add((team, o[1]))
            for key in seen:
                presence[key] += 1
        out["objective_presence"] = {f"team{k[0]}@obj_team{k[1]}": round(v / max(1, samples), 3)
                                     for k, v in presence.items()}
        out["objective_closest"] = {f"team{k[0]}@obj_team{k[1]}": round(v, 1) for k, v in closest.items()}
    if mode == "oc":
        det = [e for e in events if e[1] == "_detonate_bomb"]
        out["oc"] = {"pickups": ev.get("_pickup_bomb", 0),
                     "pickups_by_team": dict(Counter(e[2][0]["team"] for e in events if e[1] == "_pickup_bomb" and isinstance(e[2][0], dict))),
                     "detonations_inside": sum(1 for e in det if e[2][0].get("inside")),
                     "detonations_outside": sum(1 for e in det if e[2][0].get("inside") is False),
                     "bomb_deaths": sum(1 for d in deaths if d[5] == 17)}
    if mode == "dia":
        out["dia"] = {"spawned": ev.get("_spawn_diamond", 0), "picked": ev.get("_pickup_diamond", 0),
                      "cashed": ev.get("_cash_in", 0), "dropped": ev.get("_drop_carried_diamond", 0)}
    if mode == "dem":
        out["dem"] = {"destroyed": dict(Counter(e[2][0] for e in events if e[1] == "dem_destroyed")),
                      "repaired": dict(Counter(e[2][0] for e in events if e[1] == "dem_repaired"))}
    if mode == "vip":
        out["vip"] = {"vip_kills": ev.get("_kill_vip", 0), "rounds": ev.get("_finish_round", 0)}
    if mode == "zom":
        out["zom"] = {"infections": ev.get("_infect", 0), "rounds": ev.get("_finish_round", 0)}

    # ---- actions by class --------------------------------------------------------
    acts = defaultdict(Counter)
    for t, bid, kind, tool, accepted, argument in r["actions"]:
        if t > tend:
            continue
        acts[class_of(r, bid, t)][f"{kind}:{tool}:{'ok' if accepted else 'rej'}"] += 1
    out["actions_by_class"] = {str(k): dict(v) for k, v in acts.items()}
    class_alive = Counter()
    for bid, rows in trace.items():
        for row in rows:
            if row[ALIVE]:
                class_alive[class_of(r, bid, row[T])] += 0.25
    out["class_alive_seconds"] = {str(k): round(v) for k, v in class_alive.items()}
    out["jetpack_seconds"] = r.get("jetpack_seconds", {})
    out["parachute_seconds"] = r.get("parachute_seconds", {})
    return out


def main():
    paths = sorted(sys.argv[1:]) or sorted(glob.glob(RUNS + "/m_*.json.gz"))
    results = []
    for path in paths:
        try:
            results.append(case(path))
        except Exception as exc:  # noqa: BLE001
            print("FAILED", path, type(exc).__name__, exc, file=sys.stderr)
    json.dump(results, open(RUNS + "/_detect.json", "w"), default=str)
    print("cases", len(results))


if __name__ == "__main__":
    main()
