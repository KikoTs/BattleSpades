"""Gameplay outcomes, distinct from a simulation merely completing without errors."""
from __future__ import annotations

from collections import Counter


def review(report: dict) -> dict:
    mode = str(report["mode"]).removeprefix("hc_")
    mode = "zom" if mode == "czom" else mode
    events = Counter(event[1] for event in report.get("mode_events", ()))
    kills = sum(row[4] >= 0 and row[4] != row[1] and row[5] not in {8, 9, 10}
                for row in report.get("deaths", ()))
    actions = Counter(row[2] for row in report.get("actions", ()) if row[4])
    construction = report.get("construction", {}).get("metrics", {})
    snapshots = report.get("fun", ())
    checkpoints = max((p["checkpoint"] for _, state, _ in snapshots for p in state["players"]), default=0)
    stage = max((state["stage"] for _, state, _ in snapshots), default=0)
    wave = max((state.get("wave", 0) for _, state, _ in snapshots), default=0)
    finishers = max((len(scores["finishers"]) for _, _, scores in snapshots), default=0)
    points = max((sum(scores["points"].values()) for _, _, scores in snapshots), default=0)
    outcomes = {
        "kills": kills,
        "captures": events["_capture_intel"], "intel_pickups": events["_pickup_intel"],
        "vip_eliminations": events["_kill_vip"], "infections": events["_infect"],
        "diamond_cash_ins": events["_cash_in"] + events["_cash_in_loose"],
        "territory_captures": events["_announce_capture"],
        "hill_claims": events["_announce_claim"],
        "objective_cells_destroyed": events["dem_destroyed"],
        "objective_cells_repaired": events["dem_repaired"],
        "bombs_delivered": sum(event[1] == "_detonate_bomb" and any(
            isinstance(arg, dict) and arg.get("inside") for arg in event[2])
            for event in report.get("mode_events", ())),
        "structures_started": construction.get("sites_started", 0),
        "structures_completed": construction.get("sites_completed", 0),
        "structures_abandoned": construction.get("sites_abandoned", 0),
        "structure_cells_built": construction.get("line_cells", 0) + construction.get("single_cells", 0),
        "prefabs_placed": actions["place_prefab"],
        "custom_round_finishes": events["finish"], "custom_awards": events["award"],
        "max_checkpoint": checkpoints, "race_finishers": finishers,
        "max_stage": stage, "max_round_points": points,
        "waves_started": events["start_wave"],
        "max_wave": wave,
    }
    scenario = report.get("scenario", {})
    issues = []
    if scenario.get("kind"):
        if not scenario.get("target_started_alive"):
            issues.append("invalid target fixture")
        if scenario.get("target_defeated_s") is None:
            issues.append("target remains undefeated")
    elif report.get("simulated_seconds", 0) >= 240:
        if mode in {"nor", "tdm", "arena"} and kills == 0:
            issues.append("no combat kills")
        required = {"ctf": ("intel_pickups", "intel was never reached"),
                    "cctf": ("intel_pickups", "intel was never reached"),
                    "vip": ("vip_eliminations", "neither VIP was eliminated"),
                    "mh": ("hill_claims", "no hill claimed"),
                    "tc": ("territory_captures", "no territory captured"),
                    "dia": ("diamond_cash_ins", "no diamond cashed in"),
                    "dem": ("objective_cells_destroyed", "neither objective was damaged"),
                    "oc": ("bombs_delivered", "no bomb detonated inside its objective")}
        if mode in required:
            field, reason = required[mode]
            if outcomes[field] == 0:
                issues.append(reason)
        if snapshots and mode in {"crumbling_run", "red_light_raid"} and checkpoints == 0:
            issues.append("no checkpoint progress")
        if snapshots and mode == "night_shift" and wave <= 1:
            issues.append("survival did not progress beyond its opening stage")
    return {"outcomes": outcomes, "gameplay_issues": issues}
