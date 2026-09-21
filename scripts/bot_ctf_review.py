"""Measure CTF pickups/captures and flag sustained carrier-goal abandonment."""

from collections import Counter, defaultdict
import math


def _rejected_carrier_breaches(samples, bot, life):
    """A correct home goal must not conceal stationary, rejected digging.

    Feedback frames are deduplicated: rereading one rejection is not repeated
    work. Actual target removal/damage, accepted work, body movement and sample
    gaps finish an episode. The short bound captures the real five-second
    CastleWars failure without requiring a carrier to survive ten seconds.
    """
    findings, episode, rejections, terrain = [], [], set(), {}

    def finish():
        if (episode and episode[-1]["t"] - episode[0]["t"] >= 3
                and len(rejections) >= 3 and terrain):
            findings.append({"kind": "carrier_repeated_rejected_breach",
                "id": bot, "life": life, "start": episode[0]["t"],
                "end": episode[-1]["t"], "position": episode[-1]["position"],
                "goal": episode[-1].get("goal"), "unique_rejections": len(rejections),
                "target_cells": [list(cell) for cell in sorted(terrain)]})
        episode.clear()
        rejections.clear()
        terrain.clear()

    for row, carrier in samples:
        target, feedback = row.get("terrain_target"), row.get("feedback")
        cell = tuple(target["cell"]) if target else None
        state = (target.get("solid"), target.get("damage")) if target else None
        breach = (row.get("affordance") == "breach"
                  or str(row.get("role") or "").endswith(":route_breach"))
        accepted = bool(feedback and feedback[0] == "melee" and feedback[1]
                        and episode and feedback[2] != (episode[-1].get("feedback") or [None]*3)[2])
        progress = (accepted or (target and not target.get("solid"))
                    or (cell in terrain and state != terrain[cell]))
        if (not carrier or not row.get("intent_fresh") or not breach or progress
                or (episode and (row["t"] - episode[-1]["t"] > .3
                                 or math.dist(row["position"], episode[0]["position"]) > 1))):
            finish()
        if not carrier or not row.get("intent_fresh") or not breach or progress:
            continue
        episode.append(row)
        if cell is not None:
            terrain[cell] = state
        if feedback and feedback[0] == "melee" and feedback[1] is False:
            rejections.add(feedback[2])
    finish()
    return findings


def review(rows: list[dict]) -> dict:
    known = [r for r in rows if (r.get("mode_state") or {}).get("mode") in {"ctf", "cctf"}
             and any(o["kind"] == "ctf_base" for o in r["mode_state"].get("objectives", ()))]
    report = {"samples": len(rows), "objective_samples": len(known),
              "objective_state_recorded": bool(rows) and len(known) == len(rows),
              "time_basis": "trace simulation seconds", "bots": {}, "findings": [],
              "pickups": [], "captures": [], "carrier_samples": 0}
    frames, lives = {}, defaultdict(list)
    for row in known:
        frames[row["t"]] = row["mode_state"]
        lives[(row["id"], row["life"])].append(row)
    carriers, scores = {}, {}
    for t, state in sorted(frames.items()):
        for obj in state["objectives"]:
            if obj["kind"] != "ctf_intel":
                continue
            team, carrier = str(obj["team"]), obj["carrier_id"]
            if carrier >= 0 and carriers.get(team) != carrier:
                report["pickups"].append({"t": t, "flag_team": team, "carrier": carrier})
            carriers[team] = carrier
        for team, score in state.get("team_scores", {}).items():
            if team in scores and score > scores[team]:
                report["captures"].append({"t": t, "team": str(team), "points": score - scores[team]})
            scores[team] = score
    for (bot, life), samples in sorted(lives.items()):
        samples.sort(key=lambda r: r["t"])
        stats = {"roles": dict(Counter(r.get("role") for r in samples)),
                 "carrier_samples": 0, "enemy_flag_distance_first": None,
                 "enemy_flag_distance_min": None, "enemy_flag_distance_last": None}
        distances, episode, carrier_rows = [], [], []
        def finish():
            if episode and episode[-1]["t"] - episode[0]["t"] >= 10:
                report["findings"].append({"kind": "flag_carrier_abandons_home_base",
                    "id": bot, "life": life, "start": episode[0]["t"],
                    "end": episode[-1]["t"], "role": episode[-1].get("role"),
                    "goal": episode[-1].get("goal")})
            episode.clear()
        for row in samples:
            objectives = row["mode_state"]["objectives"]
            own = next((o for o in objectives if o["kind"] == "ctf_base" and o["team"] == row["team"]), None)
            enemy = next((o for o in objectives if o["kind"] == "ctf_intel" and o["team"] != row["team"]), None)
            if enemy and row.get("alive"):
                distances.append(math.dist(row["position"], enemy["position"]))
            carrier = bool(enemy and enemy["carrier_id"] == bot and row.get("alive"))
            carrier_rows.append((row, carrier))
            stats["carrier_samples"] += carrier
            quiet = (row.get("intent_fresh") and not row.get("visible") and not row.get("wade")
                     and row.get("action") in {None, "none"} and row.get("pending") in {None, "none"})
            abandoned = carrier and own and quiet and (not row.get("goal")
                            or math.dist(row["goal"], own["position"]) > 8)
            if not abandoned or (episode and row["t"] - episode[-1]["t"] > .3):
                finish()
            if abandoned:
                episode.append(row)
        finish()
        report["findings"].extend(_rejected_carrier_breaches(carrier_rows, bot, life))
        if distances:
            stats.update(enemy_flag_distance_first=round(distances[0], 2),
                         enemy_flag_distance_min=round(min(distances), 2),
                         enemy_flag_distance_last=round(distances[-1], 2))
        report["carrier_samples"] += stats["carrier_samples"]
        report["bots"][f"{bot}:{life}"] = stats
    return report
