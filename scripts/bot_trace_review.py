"""Review native match traces for travel loops and needless vertical aim.

These are diagnostics, not a claim that bots play well. Combat, terrain work,
deliberate holds, respawns and discontinuous samples are excluded from loop
windows. Walking/jumping/dropping and navigation recovery pauses belong to
the same trip; ground-only aim samples are a separate check. Times use the
trace's simulation clock, not wall-clock worker scheduling time. Retain the
JSONL so every suspected interval can be inspected: a legitimate return from
an unsuccessful detour can also warrant review.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path


_TRAVEL_AFFORDANCES = {"walk", "crouch", "drop", "jump"}
_NAVIGATION_PAUSES = {
    "planning_wait", "edge_blocked", "physical_edge_blocked", "segment_complete",
    "route_cycle",
}
_WINDOW_SECONDS = (20, 30)


def traversal_context(row: dict) -> bool:
    """Travel ownership survives airborne edges and explicit recovery pauses."""
    movement = row.get("movement") or (0, 0, 0)
    moving = math.hypot(*movement[:2]) > .1
    pause = str(row.get("role") or "").rsplit(":", 1)[-1] in _NAVIGATION_PAUSES
    return bool(
        row.get("alive") and row.get("spawned", True) and row.get("intent_fresh", True)
        and not row.get("wade", False)
        and row.get("affordance") in _TRAVEL_AFFORDANCES
        and row.get("action") in {None, "none"}
        and row.get("pending") in {None, "none"}
        and not row.get("visible")
        and (moving or pause)
    )


def ordinary_travel(row: dict) -> bool:
    movement = row.get("movement") or (0, 0, 0)
    return bool(
        traversal_context(row) and row.get("grounded")
        and math.hypot(*movement[:2]) > .1
    )


def _cell(row: dict) -> tuple[int, ...]:
    return tuple(math.floor(value / 2) for value in row["position"])


def _support_age(row: dict) -> tuple[str, float | None]:
    navigation = row.get("navigation") or {}
    basis = ("support_no_progress_time" if "support_no_progress_time" in navigation
             else "goal_progress_age")
    value = navigation.get(basis)
    age = (float(value) if isinstance(value, (int, float))
           and math.isfinite(value) and value >= 0 else None)
    return basis, age


def _support_observation(row: dict) -> tuple[str, float] | None:
    """Observe optional support without treating recovery as a new commitment.

    New traces provide the actor's progress toward a stable support anchor.
    Older traces only provide general strategic progress age, which moving
    goals can reset. Neither proves that the current endpoint is unreachable.
    """
    role = str(row.get("role") or "")
    goal, position = row.get("goal"), row.get("position")
    if (role.split(":", 1)[0] != "tdm_squad_support"
            or role.endswith(":arrived") or role.endswith(":breach_assist_queue")
            or not row.get("alive") or not row.get("spawned", True)
            or row.get("intent_fresh") is not True or row.get("wade", False)
            or row.get("visible") or row.get("action") not in {None, "none"}
            or row.get("pending") not in {None, "none"}
            or row.get("affordance") not in _TRAVEL_AFFORDANCES
            or not goal or not position):
        return None
    # TDM support's policy radius is three; the worker also accepts at most
    # three blocks of vertical difference. A deliberate arrival is not a stall.
    if math.dist(goal[:2], position[:2]) <= 3 and abs(goal[2] - position[2]) <= 3:
        return None
    basis, age = _support_age(row)
    return (basis, age) if age is not None else None


def _review_support(samples: list[dict], stats: dict, findings: list[dict]) -> None:
    episode = []

    def finish():
        # One isolated row is insufficient evidence; short excluded combat,
        # sampling or work interruptions do not erase the worker's age.
        if len(episode) >= 3:
            first, last = episode[0], episode[-1]
            nav = last["navigation"]
            findings.append({
                "id": first["id"], "life": first["life"],
                "start": first["t"], "end": last["t"],
                "observed_support_samples": len(episode),
                "age_basis": _support_age(first)[0],
                "max_support_stall_seconds": max(_support_age(row)[1] for row in episode),
                "max_goal_progress_age_seconds": max((
                    row["navigation"].get("goal_progress_age", 0) for row in episode), default=0),
                "escape_attempts": max(row["navigation"].get("escape_attempts", 0)
                                       for row in episode),
                "corridor_remaining": nav.get("corridor_remaining"),
                "corridor_endpoint_failure": nav.get("corridor_endpoint_failure"),
                "position": last["position"], "goal": last["goal"],
            })
        episode.clear()

    for row in samples:
        _, raw_age = _support_age(row)
        if raw_age is not None and raw_age <= 30:
            finish()
        observation = _support_observation(row)
        if observation is None:
            continue
        basis, age = observation
        stats["optional_support_samples"] += 1
        if basis not in stats["support_age_bases"]:
            stats["support_age_bases"].append(basis)
            stats["support_age_bases"].sort()
        stats["support_stall_max_seconds"] = max(stats["support_stall_max_seconds"], age)
        goal_age = (row.get("navigation") or {}).get("goal_progress_age", 0)
        stats["support_goal_progress_max_age_seconds"] = max(
            stats["support_goal_progress_max_age_seconds"], goal_age)
        if age > 30:
            if episode and basis != _support_age(episode[0])[0]:
                finish()
            episode.append(row)
    finish()


def _navigation_task_context(row: dict) -> bool:
    """Include attempted excavation, but exclude deliberate holds and combat."""
    suffix = str(row.get("role") or "").rsplit(":", 1)[-1]
    movement = row.get("movement") or (0, 0, 0)
    breach = suffix == "route_breach"
    return bool(
        row.get("alive") and row.get("spawned", True)
        and row.get("intent_fresh") is True and not row.get("visible")
        and suffix not in {"arrived", "hold", "breach_assist_queue"}
        and (breach or row.get("action") in {None, "none"}
             and row.get("pending") in {None, "none"})
        and (breach or suffix in _NAVIGATION_PAUSES
             or row.get("travel_waypoint") is not None
             or row.get("navigation") and math.hypot(*movement[:2]) > .1
             and row.get("affordance") in _TRAVEL_AFFORDANCES)
    )


def _review_physical(samples: list[dict], stats: dict) -> None:
    """Describe the longest one-block position run; it is not a failure gate."""
    run = []

    def finish():
        if len(run) < 2:
            run.clear()
            return
        duration = run[-1]["t"] - run[0]["t"]
        previous = stats["longest_stationary_navigation"]
        if previous is not None and previous["seconds"] >= duration:
            run.clear()
            return
        accepted, rejected = defaultdict(int), defaultdict(int)
        terrain_states = {}
        terrain_changes = 0
        for row in run:
            target = row.get("terrain_target")
            if not target or not target.get("cell"):
                continue
            cell = tuple(target["cell"])
            state = (target.get("solid"), target.get("damage"))
            if cell in terrain_states and terrain_states[cell] != state:
                terrain_changes += 1
            terrain_states[cell] = state
        for a, b in zip(run, run[1:]):
            feedback = b.get("feedback")
            if (feedback and feedback != a.get("feedback")
                    and feedback[0] and feedback[2] >= 0):
                (accepted if feedback[1] else rejected)[feedback[0]] += 1
        stats["longest_stationary_navigation"] = {
            "life": run[0]["life"], "start": run[0]["t"], "end": run[-1]["t"],
            "seconds": round(duration, 3), "samples": len(run),
            "wall_seconds": round(run[-1]["wall_t"] - run[0]["wall_t"], 3)
                            if "wall_t" in run[0] and "wall_t" in run[-1] else None,
            "position": run[0]["position"],
            "max_distance_from_start": round(max(
                math.dist(row["position"], run[0]["position"]) for row in run), 3),
            "roles": sorted({row.get("role") for row in run if row.get("role")}),
            "new_accepted_feedback": dict(accepted), "new_rejected_feedback": dict(rejected),
            "topology_recorded": all("topology_version" in row for row in run),
            "topology_changes": sum(a.get("topology_version") != b.get("topology_version")
                                    for a, b in zip(run, run[1:])
                                    if "topology_version" in a and "topology_version" in b),
            "terrain_target_recorded": all("terrain_target" in row for row in run),
            "terrain_target_cells_observed": len(terrain_states),
            "terrain_target_state_changes": terrain_changes,
        }
        run.clear()

    for row in samples:
        if not _navigation_task_context(row):
            finish()
            continue
        if run and (row["t"] - run[-1]["t"] > .3
                    or math.dist(row["position"], run[0]["position"]) > 1):
            finish()
        run.append(row)
    finish()


def _review_water(samples: list[dict], stats: dict, episodes: list[dict],
                  suspects: list[dict]) -> None:
    """Track water recovery until sustained native dry footing, in one pass."""
    run = []
    dry_since = None

    def finish(reason: str):
        nonlocal dry_since
        if not run:
            return
        first, last = run[0], run[-1]
        duration = last["t"] - first["t"]
        observed_seconds = sum(b["t"] - a["t"] for a, b in zip(run, run[1:])
                               if 0 < b["t"] - a["t"] <= .3)
        known = [row for row in run if isinstance(row.get("wade"), bool)]
        wade_fraction = sum(row["wade"] for row in known) / max(1, len(known))
        lower = [min(row["position"][axis] for row in run) for axis in range(3)]
        upper = [max(row["position"][axis] for row in run) for axis in range(3)]
        terrain_states = {}
        terrain_changes = 0
        for row in run:
            target = row.get("terrain_target")
            if not target or not target.get("cell"):
                continue
            cell = tuple(target["cell"])
            state = target.get("solid"), target.get("damage")
            if cell in terrain_states and terrain_states[cell] != state:
                terrain_changes += 1
            terrain_states[cell] = state
        record = {
            "id": first["id"], "life": first["life"],
            "start": first["t"], "end": last["t"], "seconds": round(duration, 3),
            "end_reason": reason, "recovered": reason == "dry_grounded_one_second",
            "dry_footing_basis": ("dry_safe" if all("dry_safe" in row for row in run)
                                  else "grounded_and_not_wading" if all("dry_safe" not in row for row in run)
                                  else "mixed"),
            "samples": len(run), "wade_fraction": round(wade_fraction, 3),
            "observed_seconds": round(observed_seconds, 3),
            "water_state_sample_fraction": round(len(known) / len(run), 3),
            "travel_distance": round(sum(math.dist(a["position"], b["position"])
                                          for a, b in zip(run, run[1:])), 3),
            "displacement": round(math.dist(first["position"], last["position"]), 3),
            "bbox_min": lower, "bbox_max": upper,
            "bbox_diagonal": round(math.dist(lower, upper), 3),
            "recovery_role_fraction": round(sum(str(row.get("role") or "").startswith("water")
                                                   for row in run) / len(run), 3),
            "topology_recorded": all("topology_version" in row for row in run),
            "topology_changes": sum(a["topology_version"] != b["topology_version"]
                                    for a, b in zip(run, run[1:])
                                    if "topology_version" in a and "topology_version" in b),
            "terrain_target_recorded": all("terrain_target" in row for row in run),
            "terrain_target_cells_observed": len(terrain_states),
            "terrain_target_state_changes": terrain_changes,
        }
        episodes.append(record)
        stats["water_recovery_max_seconds"] = max(stats["water_recovery_max_seconds"], duration)
        # Real digging/building may occur during an unsuccessful exit. Report
        # that evidence, but do not let it hide a long confined water episode.
        # This is an inspection candidate, never proof of a navigation defect.
        if (duration >= 30 and observed_seconds >= duration * .9
                and len(known) >= len(run) * .9 and wade_fraction >= .8
                and math.dist(lower, upper) <= 32):
            suspects.append(record)
        run.clear()
        dry_since = None

    for row in samples:
        if not row.get("alive"):
            finish("death")
            continue
        if not run and not row.get("wade"):
            continue
        if run and row["t"] - run[-1]["t"] > .3:
            dry_since = None  # A sampling gap cannot establish continuous footing.
        run.append(row)
        dry = (row.get("dry_safe") is True if "dry_safe" in row else
               row.get("wade") is False and row.get("grounded"))
        if dry:
            if dry_since is None:
                dry_since = row["t"]
            if row["t"] - dry_since >= 1:
                finish("dry_grounded_one_second")
        else:
            dry_since = None
    finish("trace_end")


def review(rows: list[dict]) -> dict:
    lives = defaultdict(list)
    for row in rows:
        lives[(row["id"], row["life"])].append(row)
    report = {"samples": len(rows), "lives": len(lives), "bots": {},
              "time_basis": "trace simulation seconds",
              "window_seconds": list(_WINDOW_SECONDS),
              "intent_freshness_recorded": bool(rows) and all("intent_fresh" in row for row in rows),
              "wall_clock_recorded": bool(rows) and all("wall_t" in row for row in rows),
              "candidate_windows": 0, "eligible_windows": 0,
              "suspected_travel_loops": [], "suspected_support_stagnation": [],
              "water_recovery_episodes": [], "suspected_water_recovery_stagnation": []}
    for (bot_id, life), samples in sorted(lives.items()):
        samples.sort(key=lambda row: row["t"])
        stats = report["bots"].setdefault(str(bot_id), {
            "ordinary_travel_samples": 0, "travel_pitch_over_45_samples": 0,
            "travel_pitch_max_degrees": 0, "travel_pitch_sign_reversals": 0,
            "optional_support_samples": 0, "support_goal_progress_max_age_seconds": 0,
            "support_stall_max_seconds": 0, "support_age_bases": [],
            "longest_stationary_navigation": None,
            "water_recovery_max_seconds": 0,
        })
        _review_support(samples, stats, report["suspected_support_stagnation"])
        _review_physical(samples, stats)
        _review_water(samples, stats, report["water_recovery_episodes"],
                      report["suspected_water_recovery_stagnation"])
        previous = None
        for row in samples:
            if not ordinary_travel(row):
                previous = None
                continue
            x, y, z = row["orientation"]
            pitch = math.degrees(math.atan2(z, math.hypot(x, y)))
            stats["ordinary_travel_samples"] += 1
            stats["travel_pitch_over_45_samples"] += abs(pitch) > 45
            stats["travel_pitch_max_degrees"] = round(max(
                stats["travel_pitch_max_degrees"], abs(pitch)), 2)
            if previous and row["t"] - previous[0] <= .25:
                stats["travel_pitch_sign_reversals"] += (
                    pitch * previous[1] < 0 and abs(pitch - previous[1]) > 10)
            previous = row["t"], pitch
        next_window = samples[0]["t"] + min(_WINDOW_SECONDS)
        for end, row in enumerate(samples):
            if row["t"] < next_window:
                continue
            next_window = row["t"] + 5
            for duration in _WINDOW_SECONDS:
                window = [point for point in samples[:end + 1]
                          if point["t"] >= row["t"] - duration]
                if window[-1]["t"] - window[0]["t"] < duration - .5:
                    continue
                report["candidate_windows"] += 1
                if (any(not traversal_context(point) for point in window)
                        or any(b["t"] - a["t"] > .3 for a, b in zip(window, window[1:]))):
                    continue
                start, finish = window[0], window[-1]
                goal = start.get("goal")
                if (not goal or any(not point.get("goal")
                        or math.dist(goal, point["goal"]) > 4 for point in window)
                        or math.dist(finish["position"], goal) < 12):
                    continue
                report["eligible_windows"] += 1
                length = sum(math.dist(a["position"], b["position"])
                             for a, b in zip(window, window[1:]))
                displacement = math.dist(start["position"], finish["position"])
                progress = (math.dist(start["position"], goal)
                            - math.dist(finish["position"], goal))
                midpoint = len(window) // 2
                early = {_cell(point) for point in window[:midpoint]}
                late = {_cell(point) for point in window[midpoint:]}
                revisit = len(early & late) / max(1, len(late))
                if length < 20 or displacement >= 6 or progress >= 2:
                    continue
                # Repeated small circuits and a long closed return are distinct
                # suspects. Neither is a gameplay-quality acceptance criterion.
                kind = ("repeated_coverage" if revisit >= .7 else
                        "closed_return" if length >= 100 else None)
                if kind is None:
                    continue
                report["suspected_travel_loops"].append({
                    "id": bot_id, "life": life, "start": start["t"], "end": finish["t"],
                    "kind": kind,
                    "travel_distance": round(length, 2), "displacement": round(displacement, 2),
                    "goal_progress": round(progress, 2), "revisited_fraction": round(revisit, 3),
                    "role": finish.get("role"), "position": finish["position"], "goal": goal,
                })
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    result = review([json.loads(line) for line in args.trace.read_text(encoding="utf-8").splitlines() if line])
    output = json.dumps(result, indent=2)
    if args.json:
        args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
