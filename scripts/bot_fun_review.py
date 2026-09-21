"""Summarize how varied and active a recorded bot match actually was.

Reads the 10 Hz JSONL written by ``bot_runtime_smoke.py --trace-jsonl`` and
reports per-bot and whole-match gameplay texture: fights, scoring, mechanics
exercised, idle/stationary time, role variety and task switching. It measures
what happened; it does not decide whether a match was fun.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path


def _planar(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def review(path: Path) -> dict[str, object]:
    bots: dict[int, dict[str, object]] = {}
    roles: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    feedback: Counter[str] = Counter()
    tools: Counter[int] = Counter()
    affordances: Counter[str] = Counter()
    last: dict[int, dict] = {}
    seen_feedback: dict[int, object] = {}
    duration = 0.0
    for line in path.open(encoding="utf-8"):
        record = json.loads(line)
        bot_id = int(record["id"])
        duration = max(duration, float(record["t"]))
        bot = bots.setdefault(bot_id, {
            "team": record["team"], "class_id": record["class_id"], "samples": 0,
            "alive": 0, "deaths": 0, "score": 0, "travelled": 0.0,
            "stationary": 0, "longest_stationary": 0.0, "_still": 0,
            "visible": 0, "firing": 0, "airborne": 0, "wading": 0,
            "role_switches": 0, "roles": Counter(), "idle": 0,
        })
        bot["samples"] += 1
        bot["score"] = record["score"]
        previous = last.get(bot_id)
        if not record["alive"]:
            if previous is not None and previous["alive"]:
                bot["deaths"] += 1
            last[bot_id] = record
            bot["_still"] = 0
            continue
        bot["alive"] += 1
        role = str(record.get("role") or "none").split(":")[0]
        roles[role] += 1
        bot["roles"][role] += 1
        if previous is not None and previous["alive"]:
            previous_role = str(previous.get("role") or "none").split(":")[0]
            if previous_role != role:
                bot["role_switches"] += 1
            step = _planar(record["position"], previous["position"])
            if step < 8.0:
                bot["travelled"] += step
            if step < 0.02:
                bot["stationary"] += 1
                bot["_still"] += 1
                bot["longest_stationary"] = max(bot["longest_stationary"], bot["_still"] / 10.0)
            else:
                bot["_still"] = 0
        if record.get("visible"):
            bot["visible"] += 1
        action = record.get("action") or "none"
        actions[action] += 1
        if action == "fire":
            bot["firing"] += 1
        if role.startswith("idle") or role == "none":
            bot["idle"] += 1
        if not record.get("grounded"):
            bot["airborne"] += 1
        if record.get("wade"):
            bot["wading"] += 1
        tools[int(record.get("tool", -1))] += 1
        affordances[str(record.get("affordance"))] += 1
        item = record.get("feedback")
        if item and item[0] and seen_feedback.get(bot_id) != item[2]:
            seen_feedback[bot_id] = item[2]
            feedback[f"{item[0]}:{'ok' if item[1] else 'rejected'}"] += 1
        last[bot_id] = record

    rows = []
    for bot_id, bot in sorted(bots.items()):
        alive = max(1, int(bot["alive"]))
        rows.append({
            "id": bot_id, "team": bot["team"], "class": bot["class_id"],
            "score": bot["score"], "deaths": bot["deaths"],
            "alive_pct": round(100 * alive / max(1, bot["samples"]), 1),
            "blocks_per_s": round(bot["travelled"] / (alive / 10.0), 2),
            "stationary_pct": round(100 * bot["stationary"] / alive, 1),
            "longest_still_s": bot["longest_stationary"],
            "enemy_visible_pct": round(100 * bot["visible"] / alive, 1),
            "firing_pct": round(100 * bot["firing"] / alive, 1),
            "airborne_pct": round(100 * bot["airborne"] / alive, 1),
            "wading_pct": round(100 * bot["wading"] / alive, 1),
            "idle_pct": round(100 * bot["idle"] / alive, 1),
            "role_switches_per_min": round(bot["role_switches"] / (alive / 600.0), 1),
            "top_roles": bot["roles"].most_common(4),
        })
    total = max(1, sum(roles.values()))
    return {
        "duration": duration,
        "bots": rows,
        "roles_pct": {key: round(100 * value / total, 1) for key, value in roles.most_common(24)},
        "actions": dict(actions.most_common()),
        "feedback": dict(feedback.most_common()),
        "tools": dict(tools.most_common()),
        "affordances": dict(affordances.most_common()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = review(args.trace)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"duration {report['duration']:.1f}s")
    for row in report["bots"]:
        print(row)
    for key in ("roles_pct", "actions", "feedback", "tools", "affordances"):
        print(key, report[key])


if __name__ == "__main__":
    main()
