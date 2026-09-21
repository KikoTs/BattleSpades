"""Inspect authoritative VIP traces for objective ownership and native aim.

Deliberate VIP/escort holds are useful work. This review does not label them
stuck, infer victory from liveness, or require a winner within a short sample.
Findings retain exact intervals for inspection; summary motion/turn counts are
measurements, not a subjective claim that movement looks natural.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.bot_trace_review import ordinary_travel


def _value(mapping, key):
    return mapping.get(str(key), mapping.get(key))


def _role(row):
    role = str(row.get("role") or "")
    if role.startswith("combat_objective:"):
        role = role.removeprefix("combat_objective:")
    return role.split(":", 1)[0]


def _quiet(row):
    return (row.get("alive") and row.get("spawned", True)
            and row.get("intent_fresh") and not row.get("visible")
            and not row.get("wade") and row.get("action") in {None, "none"}
            and row.get("pending") in {None, "none"}
            and not str(row.get("role") or "").startswith("combat_"))


def _turns(samples):
    """Count measured directional reversals, not merely crossing pitch zero."""
    result = {"yaw_reversals": 0, "pitch_reversals": 0,
              "pitch_max_degrees": 0., "role_changes": 0, "goal_jumps_over_four": 0}
    previous, signs = None, [0, 0]
    for row in samples:
        if not _quiet(row) or not row.get("orientation"):
            previous, signs = None, [0, 0]
            continue
        x, y, z = row["orientation"]
        angles = (math.atan2(y, x), math.atan2(z, math.hypot(x, y)))
        result["pitch_max_degrees"] = max(result["pitch_max_degrees"], abs(math.degrees(angles[1])))
        if previous and row["t"] - previous[0]["t"] <= .3:
            before, old_angles = previous
            result["role_changes"] += _role(row) != _role(before)
            if row.get("goal") and before.get("goal"):
                result["goal_jumps_over_four"] += math.dist(row["goal"], before["goal"]) > 4
            for axis, key in enumerate(("yaw_reversals", "pitch_reversals")):
                delta = math.remainder(angles[axis] - old_angles[axis], 2 * math.pi)
                if abs(delta) >= math.radians(2):
                    sign = 1 if delta > 0 else -1
                    result[key] += signs[axis] != 0 and signs[axis] != sign
                    signs[axis] = sign
        previous = row, angles
    result["pitch_max_degrees"] = round(result["pitch_max_degrees"], 2)
    return result


def _review_mop_up(by_time, findings):
    """A live VIP keeps defenders, but cannot pull every attacker home forever."""
    episodes = defaultdict(list)
    def finish(team):
        run = episodes[team]
        if run and run[-1]["t"] - run[0]["t"] >= 10:
            findings.append({"kind": "no_mop_up_after_enemy_vip_death", "team": str(team),
                             "start": run[0]["t"], "end": run[-1]["t"],
                             "friendly_roles": run[-1]["roles"],
                             "enemy_survivors": run[-1]["survivors"]})
        run.clear()
    for t, roster in sorted(by_time.items()):
        state = next(iter(roster.values()))["mode_state"]
        for team, vip_id in state.get("vips", {}).items():
            vip = roster.get(vip_id)
            friends = [r for r in roster.values() if str(r["team"]) == str(team)
                       and r["id"] != vip_id and r.get("alive") and r.get("spawned", True)]
            enemies = [r for r in roster.values() if str(r["team"]) != str(team)
                       and r.get("alive") and r.get("spawned", True)]
            active = (str(state.get("phase")).upper() == "ACTIVE"
                      and vip and vip.get("alive") and friends and enemies
                      and _value(state.get("vip_alive", {}), team)
                      and all(not bool(alive) for other, alive in state.get("vip_alive", {}).items()
                              if str(other) != str(team)))
            pressure = vip and any(math.dist(enemy["position"], vip["position"]) <= 24 for enemy in enemies)
            all_defending = all(_quiet(r) and _role(r).startswith(("vip_guard", "vip_escort"))
                                for r in friends)
            if not active or pressure or not all_defending:
                finish(team)
                continue
            if episodes[team] and t - episodes[team][-1]["t"] > .3:
                finish(team)
            episodes[team].append({"t": t, "roles": {r["id"]: r["role"] for r in friends},
                                   "survivors": [r["id"] for r in enemies]})
    for team in list(episodes):
        finish(team)


def review(rows: list[dict]) -> dict:
    known = [row for row in rows if (row.get("mode_state") or {}).get("mode") == "vip"
             and row["mode_state"].get("phase") in {
                 "WAITING", "RESETTING", "SELECTING", "ACTIVE", "INTERMISSION"}
             and all(isinstance(row["mode_state"].get(key), dict)
                     for key in ("vips", "vip_alive", "respawn_enabled", "team_scores"))]
    result = {"samples": len(rows), "objective_samples": len(known),
              "objective_state_recorded": len(known) == len(rows) and bool(rows),
              "time_basis": "trace simulation seconds", "bots": {}, "findings": [],
              "vip_selections": [], "vip_deaths": [], "team_score_changes": [],
              "objective_success": {"vip_kills": 0, "round_score_changes": 0}}
    if not known:
        return result
    by_time = defaultdict(dict)
    lives = defaultdict(list)
    for row in known:
        by_time[row["t"]][row["id"]] = row
        lives[(row["id"], row["life"])].append(row)
    last_vips, last_alive, last_scores = {}, {}, {}
    for t, roster in sorted(by_time.items()):
        state = next(iter(roster.values()))["mode_state"]
        for team, vip in state.get("vips", {}).items():
            team = str(team)
            alive = bool(_value(state.get("vip_alive", {}), team))
            if vip is not None and last_vips.get(team) != vip:
                result["vip_selections"].append({"t": t, "team": team, "id": vip})
            if (last_alive.get(team) and not alive
                    and str(state.get("phase")).upper() == "ACTIVE"):
                result["vip_deaths"].append({"t": t, "team": team, "id": last_vips.get(team)})
            last_vips[team], last_alive[team] = vip, alive
        for team, score in state.get("team_scores", {}).items():
            if team in last_scores and score != last_scores[team]:
                result["team_score_changes"].append({"t": t, "team": str(team),
                                                      "before": last_scores[team], "after": score})
            last_scores[team] = score

    for (bot_id, life), samples in sorted(lives.items()):
        samples.sort(key=lambda row: row["t"])
        active = [r for r in samples if str(r["mode_state"].get("phase")).upper() == "ACTIVE"]
        stats = {"samples": len(samples), "active_samples": len(active),
                 "roles": dict(Counter(_role(row) for row in active)),
                 "score_first": samples[0].get("score"), "score_last": samples[-1].get("score"),
                 "guard_distance_median": None, "guard_distance_max": None,
                 "enemy_vip_distance_first": None, "enemy_vip_distance_last": None,
                 "enemy_vip_distance_min": None, **_turns(active)}
        stats["aim_context"] = "quiet samples, including terrain-action cooldowns"
        stats["ordinary_travel_aim"] = _turns([r for r in active if ordinary_travel(r)])
        guards, enemies, episode = [], [], []
        def finish():
            if episode and episode[-1]["t"] - episode[0]["t"] >= 10:
                result["findings"].append({"id": bot_id, "life": life,
                    "kind": episode[0]["kind"], "start": episode[0]["t"],
                    "end": episode[-1]["t"], "role": episode[-1]["role"],
                    "position": episode[-1]["position"], "goal": episode[-1].get("goal"),
                    "own_vip": episode[-1]["own_vip"], "own_vip_position": episode[-1]["vip_position"]})
            episode.clear()
        for row in active:
            state, roster = row["mode_state"], by_time[row["t"]]
            own_id = _value(state.get("vips", {}), row["team"])
            own = roster.get(own_id)
            enemy = next((roster.get(vip) for team, vip in state.get("vips", {}).items()
                          if str(team) != str(row["team"]) and roster.get(vip, {}).get("alive")), None)
            role, kind = _role(row), None
            if enemy and row.get("alive"):
                enemies.append(math.dist(row["position"], enemy["position"]))
            if own and own.get("alive") and _value(state.get("vip_alive", {}), row["team"]):
                if role.startswith(("vip_guard", "vip_escort")):
                    guards.append(math.dist(row["position"], own["position"]))
                    if _quiet(row) and row.get("goal") and math.dist(row["goal"], own["position"]) > 18:
                        kind = "escort_goal_abandons_live_vip"
                if (bot_id == own_id and _quiet(row)
                        and role.startswith(("patrol", "resource", "team_assault", "idle"))):
                    kind = "vip_abandons_mode_for_generic_task"
            if not kind or (episode and (episode[-1]["kind"] != kind
                                        or row["t"] - episode[-1]["t"] > .3)):
                finish()
            if kind:
                episode.append({**row, "kind": kind, "own_vip": own_id,
                                "vip_position": own["position"]})
        finish()
        if guards:
            stats["guard_distance_median"] = round(sorted(guards)[len(guards) // 2], 2)
            stats["guard_distance_max"] = round(max(guards), 2)
        if enemies:
            stats.update(enemy_vip_distance_first=round(enemies[0], 2),
                         enemy_vip_distance_last=round(enemies[-1], 2),
                         enemy_vip_distance_min=round(min(enemies), 2))
        result["bots"][f"{bot_id}:{life}"] = stats
    result["objective_success"] = {"vip_kills": len(result["vip_deaths"]),
                                    "round_score_changes": len(result["team_score_changes"])}
    _review_mop_up(by_time, result["findings"])
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = review([json.loads(line) for line in args.trace.read_text(encoding="utf-8").splitlines() if line])
    output = json.dumps(report, indent=2)
    if args.json:
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
