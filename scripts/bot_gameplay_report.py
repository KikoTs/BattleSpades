"""Produce a compact, reviewable summary while retaining the original traces."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.bot_gameplay_review import review


def summarize(path: Path, digest: str) -> dict:
    report = json.loads(gzip.decompress(path.read_bytes()))
    result = review(report)
    uses = Counter(row[5] for rows in report["trace"].values() for row in rows
                   if row[1] and row[5] in {"schematic_climb", "schematic_overwatch"})
    return {
        "mode": report["mode"], "map": report["map"], "seed": report["seed"],
        "seconds": report["simulated_seconds"], "requested_bots": report["requested_bots"],
        "initial_bots": report.get("initial_bot_count", len(report["trace"])),
        "peak_bots": report.get("peak_bot_count", len(report["trace"])),
        "error": report["error"], "source_digest": report["source_digest"],
        "current_sources": report["source_digest"] == digest,
        "source_unchanged": report["source_unchanged_during_run"],
        "scores": report["team_scores"], "scenario": report.get("scenario", {}),
        "construction_rejections": report.get("construction_rejections", {}),
        "outside_bomb_explosions": sum(e[1] == "_detonate_bomb" and any(
            isinstance(arg, dict) and arg.get("inside") is False for arg in e[2])
            for e in report["mode_events"]),
        "defender_bomb_pickups": sum(e[1] == "_pickup_bomb" and any(
            isinstance(arg, dict) and arg.get("team") == 3 for arg in e[2])
            for e in report["mode_events"]),
        "overwatch_seconds": round(uses["schematic_overwatch"] / 4, 2),
        "climbing_built_structure_seconds": round(uses["schematic_climb"] / 4, 2),
        "trace": str(path.resolve()), **result,
    }


def markdown(report: dict) -> str:
    rows = report["cases"]
    lines = ["# Bot gameplay audit", "",
        f"{len(rows)} cases; {report['simulation_hours']:.2f} simulated hours; "
        f"{report['current_source_cases']} cases on the current gameplay sources.", "",
        f"Execution errors: {report['execution_errors']}. "
        f"Cases changed during execution: {report['source_changed_cases']}.", "",
        "Starting squads contain 16 bots; mode-owned survival adds enemy waves. "
        "Target challenges use one connected idle last player against 16 bots. "
        "These measurements describe observed play, not a subjective fun score.", "",
        "## Matches", "", "| Mode | Cases | Kills | Objective events | Built structures completed |",
        "|---|---:|---:|---:|---:|"]
    modes = defaultdict(list)
    for row in rows:
        if not row["scenario"].get("kind"):
            modes[row["mode"]].append(row)
    fields = ("captures", "vip_eliminations", "infections", "diamond_cash_ins",
              "territory_captures", "hill_claims", "objective_cells_destroyed",
              "bombs_delivered", "custom_round_finishes", "custom_awards")
    for mode, matches in sorted(modes.items()):
        events = sum(row["outcomes"][name] for row in matches for name in fields)
        lines.append(f"| {mode} | {len(matches)} | {sum(r['outcomes']['kills'] for r in matches)} "
                     f"| {events} | {sum(r['outcomes']['structures_completed'] for r in matches)} |")
    lines += ["", "Objective events differ by mode; kills and capture events are not interchangeable. "
              "Infections include mode setup. Full reports retain per-event timing and results.", "",
              "## Last-player challenges", "", "| Mode | Setup | Seed | First damage (s) | Defeated (s) | Structure cells removed |",
              "|---|---|---:|---:|---:|---:|"]
    for row in sorted(rows, key=lambda r: (r["mode"], r["scenario"].get("kind", ""), r["seed"])):
        scene = row["scenario"]
        if scene.get("kind"):
            lines.append(f"| {row['mode']} | {scene['kind']} | {row['seed']} | "
                         f"{scene.get('first_damage_s')} | {scene.get('target_defeated_s')} | "
                         f"{scene.get('structure_cells_removed')} |")
    observations = [r for r in rows if r["gameplay_issues"]]
    lines += ["", "## Cases requiring interpretation", ""]
    if not observations:
        lines.append("No automatic gameplay review flags in this cohort.")
    for row in observations:
        lines.append(f"- {row['mode']} / {row['map']} / seed {row['seed']} / {row['seconds']:g}s: "
                     + "; ".join(row["gameplay_issues"]))
        longer = [r for r in rows if (r["mode"], r["map"], r["seed"]) ==
                  (row["mode"], row["map"], row["seed"]) and r["seconds"] > row["seconds"]]
        if longer:
            extended = max(longer, key=lambda r: r["seconds"])
            exercised = {key: extended["outcomes"][key] for key in
                         ("objective_cells_destroyed", "objective_cells_repaired", "vip_eliminations", "bombs_delivered")
                         if extended["outcomes"][key]}
            if exercised:
                lines.append(f"  Separate {extended['seconds']:g}s run with the same seed: {exercised}.")
        if row["mode"].removeprefix("hc_") == "oc":
            lines.append(f"  Defenders scored {row['scores'].get('3', 0)}; "
                         f"{row['outside_bomb_explosions']} bombs exploded outside the target; "
                         f"{row['defender_bomb_pickups']} defender pickups. "
                         "A defender-dominated match remains visible here; an attacking win is not guaranteed.")
    lines += ["", "Review flags are prompts for inspecting the trace. A contested objective or "
              "a successful defense can legitimately prevent an attacking score. "
              "The JSON summary links every original trace and identifies its exact sources.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", action="append", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    from scripts.bot_audit_harness import source_digest
    digest = source_digest()
    rows = [summarize(path, digest) for directory in args.runs
            for path in sorted(directory.glob("*.json.gz"))]
    data = {
        "source_digest": digest, "cases": rows,
        "simulation_hours": round(sum(row["seconds"] for row in rows) / 3600, 3),
        "execution_errors": sum(bool(row["error"]) for row in rows),
        "source_changed_cases": sum(not row["source_unchanged"] for row in rows),
        "current_source_cases": sum(row["current_sources"] for row in rows),
        "gameplay_flags": sum(bool(row["gameplay_issues"]) for row in rows),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(data), encoding="utf-8")
    print({key: value for key, value in data.items() if key != "cases"})
    return int(bool(data["execution_errors"] or data["source_changed_cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
