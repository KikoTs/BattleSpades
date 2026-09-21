"""Measure how player-like bot locomotion looks in a recorded match.

Reads the 10 Hz JSONL from ``bot_runtime_smoke.py --trace-jsonl`` and reports,
for quiet travel only (alive, no visible enemy, no terrain action): ground
speed, share of time sprinting, share standing still, head-turn rate and
left/right head reversals. A player crossing a map sprints nearly all the time,
almost never stops, and turns their head only at corners.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def review(path: Path, *, until: float = math.inf) -> dict[str, object]:
    records: dict[int, list[dict]] = defaultdict(list)
    for line in path.open(encoding="utf-8"):
        record = json.loads(line)
        if record["t"] <= until:
            records[int(record["id"])].append(record)
    rows = []
    for bot_id, items in sorted(records.items()):
        samples = distance = still = sprint = reversals = 0
        yaw_total = 0.0
        last_sign = 0
        for before, after in zip(items, items[1:]):
            quiet = (before["alive"] and after["alive"] and not after.get("visible")
                     and (after.get("action") or "none") == "none"
                     and after.get("affordance") in {"walk", "jump", "drop"}
                     and not after.get("wade"))
            if not quiet:
                last_sign = 0
                continue
            samples += 1
            step = math.hypot(after["position"][0] - before["position"][0],
                              after["position"][1] - before["position"][1])
            distance += step if step < 8.0 else 0.0
            still += step < 0.05
            sprint += bool(after.get("sprint"))
            turn = _wrap(math.atan2(after["orientation"][1], after["orientation"][0])
                         - math.atan2(before["orientation"][1], before["orientation"][0]))
            yaw_total += abs(turn)
            sign = (turn > 0.02) - (turn < -0.02)
            if sign and last_sign and sign != last_sign:
                reversals += 1
            if sign:
                last_sign = sign
        seconds = max(0.1, samples / 10.0)
        rows.append({"id": bot_id, "quiet_seconds": round(seconds, 1),
                     "blocks_per_s": round(distance / seconds, 2),
                     "sprint_pct": round(100.0 * sprint / max(1, samples), 1),
                     "still_pct": round(100.0 * still / max(1, samples), 1),
                     "yaw_deg_per_s": round(math.degrees(yaw_total) / seconds, 1),
                     "head_reversals_per_min": round(reversals / seconds * 60.0, 1)})
    keys = ("blocks_per_s", "sprint_pct", "still_pct", "yaw_deg_per_s", "head_reversals_per_min")
    weight = sum(row["quiet_seconds"] for row in rows) or 1.0
    mean = {key: round(sum(row[key] * row["quiet_seconds"] for row in rows) / weight, 1)
            for key in keys}
    return {"bots": rows, "mean": mean}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--until", type=float, default=math.inf,
                        help="only review the first N simulated seconds")
    args = parser.parse_args()
    report = review(args.trace, until=args.until)
    for row in report["bots"]:
        print(row)
    print("MEAN", report["mean"])


if __name__ == "__main__":
    main()
