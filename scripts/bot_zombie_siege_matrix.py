"""Run the Zombie siege matrix and compare it with a baseline folder.

The matrix is the one ``run_matrix.sh`` used for the 2026-10 pillar work:
London roofpillar (8 and 20 high), wallpillar, pillar, platform; ArcticBase
pillar, platform, fortress, tower; one idle survivor, 8 zombies, 150 s, seeds
3/7/11. Every run is ``scripts/bot_zombie_horde_scenario.py`` (accelerated
clock, production brain inline) and lands as ``<map>_<scenario>_<extra>_s<seed>.json``
in ``--out`` (the baseline's names), so folders can be compared run by run.

    py -3.12 scripts/bot_zombie_siege_matrix.py --out DIR [--preset full|london]
        [--seeds 3,7,11] [--zombies 8] [--armed] [--jobs 3]
    py -3.12 scripts/bot_zombie_siege_matrix.py --out DIR --compare BASELINE_DIR

Runs are subprocesses by default (one per run, ``--jobs`` in parallel) only
for speed. ``--inprocess`` imports ``run_scenario`` / ``scenario_args`` and
runs the scenarios one after another in this process; the harness restores
the clocks, the horde hook and the global RNG per run, and this was checked
on 2026-10-31 (London roofpillar seed 7, 60 s): two in-process runs and a
subprocess run produced identical infection, reach, pile, activity and
distance results, so both paths give the same JSON.

``--compare`` prints per-scenario medians before/after for all_infected_s,
collapse_s, zombies_reached, pile_mean, useful_share, the worst bot's idle
share and first_at_height_s, then an overall summary. Keys missing from
older JSONs (useful_share, first_at_height_s) are derived when possible and
shown as ``-`` otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
for entry in (str(SCRIPTS), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

HARNESS = SCRIPTS / "bot_zombie_horde_scenario.py"

# (map, scenario, extra options) in run_matrix.sh order.
ROWS_FULL = (
    ("London", "roofpillar", {}),
    ("London", "roofpillar", {"height": 20}),
    ("London", "wallpillar", {}),
    ("London", "pillar", {}),
    ("London", "platform", {}),
    ("ArcticBase", "pillar", {}),
    ("ArcticBase", "platform", {}),
    ("ArcticBase", "fortress", {}),
    ("ArcticBase", "tower", {}),
)
ROWS_LONDON = ROWS_FULL[:3]
PRESETS = {"full": ROWS_FULL, "london": ROWS_LONDON}

METRICS = (
    ("all_infected_s", "infected_s"),
    ("collapse_s", "collapse_s"),
    ("zombies_reached", "reached"),
    ("pile_mean", "pile"),
    ("useful_share", "useful"),
    ("idle_share_worst_bot", "idle_worst"),
    ("first_at_height_s", "at_height_s"),
)
USEFUL_KINDS = ("closing", "digging", "fighting", "building", "climbing")
_SEED_SUFFIX = re.compile(r"^(?P<group>.+)_s(?P<seed>\d+)$")


def run_name(map_name: str, scenario: str, extra: dict, seed: int) -> str:
    tail = "".join(f"{key}{value}" for key, value in sorted(extra.items()))
    return f"{map_name}_{scenario}_{tail}_s{seed}"


def plan(preset: str, seeds, only: str | None):
    jobs = []
    for seed in seeds:
        for map_name, scenario, extra in PRESETS[preset]:
            name = run_name(map_name, scenario, extra, seed)
            if only and only not in name:
                continue
            jobs.append((name, map_name, scenario, dict(extra), int(seed)))
    return jobs


def _cli_args(job, args) -> list[str]:
    name, map_name, scenario, extra, seed = job
    command = [sys.executable, "-X", "utf8", str(HARNESS), "--map", map_name,
               "--scenario", scenario, "--zombies", str(args.zombies),
               "--survivors", str(args.survivors), "--seconds", str(args.seconds),
               "--seed", str(seed), "--json", str(Path(args.out) / f"{name}.json")]
    if "height" in extra:
        command += ["--height", str(extra["height"])]
    if args.armed:
        command.append("--armed")
        if float(getattr(args, "hit_chance", 1.0)) < 1.0:
            command += ["--hit-chance", str(args.hit_chance)]
    if getattr(args, "spawn_distance", None):
        command += ["--spawn-distance", str(args.spawn_distance)]
    if args.baseline:
        command.append("--baseline")
    return command


def run_subprocess(job, args) -> str:
    name = job[0]
    out = Path(args.out)
    err_path = out / f"{name}.err"
    started = time.perf_counter()
    with err_path.open("w", encoding="utf-8") as err:
        completed = subprocess.run(_cli_args(job, args), cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                   stderr=err, timeout=max(60.0, args.seconds * 12))
    status = "ok" if completed.returncode == 0 and (out / f"{name}.json").is_file() else "FAILED"
    return f"{status:6s} {name} ({time.perf_counter() - started:.0f} s wall)"


def run_inprocess(job, args) -> str:
    from bot_zombie_horde_scenario import run_scenario, scenario_args

    name, map_name, scenario, extra, seed = job
    started = time.perf_counter()
    options = scenario_args(map=map_name, scenario=scenario, zombies=args.zombies,
                            survivors=args.survivors, seconds=float(args.seconds), seed=seed,
                            armed=bool(args.armed), baseline=bool(args.baseline),
                            height=extra.get("height"))
    result = asyncio.run(run_scenario(options))
    target = Path(args.out) / f"{name}.json"
    target.write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    return f"ok     {name} ({time.perf_counter() - started:.0f} s wall)"


def run_matrix(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [job for job in plan(args.preset, args.seeds, args.only)
            if args.force or not (out / f"{job[0]}.json").is_file()]
    if not jobs:
        print("nothing to run (every JSON exists; --force to redo)")
        return
    print(f"{len(jobs)} run(s) -> {out}" + (" [armed]" if args.armed else ""), flush=True)
    if args.inprocess:
        for job in jobs:
            print(run_inprocess(job, args), flush=True)
        return
    with ThreadPoolExecutor(max_workers=max(1, int(args.jobs))) as pool:
        for line in pool.map(lambda job: run_subprocess(job, args), jobs):
            print(line, flush=True)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def load_runs(folder: Path) -> dict[str, dict]:
    runs = {}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            runs[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return runs


def metric(run: dict, key: str):
    """A metric value, derived for older JSONs; None when unknown."""

    value = run.get(key)
    if value is None and key == "useful_share":
        shares = run.get("activity_share")
        if isinstance(shares, dict):
            value = round(sum(float(shares.get(k, 0.0)) for k in USEFUL_KINDS), 3)
    if value is None and key == "idle_share_worst_bot":
        shares = run.get("activity_share")
        if isinstance(shares, dict):
            value = None  # the per-bot split is not in the JSON; stay honest
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt(value, width: int = 8) -> str:
    if value is None:
        return f"{'-':>{width}s}"
    if abs(value - round(value)) < 1e-9 and abs(value) >= 10:
        return f"{value:>{width}.0f}"
    return f"{value:>{width}.1f}" if abs(value) >= 10 else f"{value:>{width}.2f}"


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _summary_row(label: str, runs: list[dict]) -> str:
    cells = [f"{label:38s}", f"{len(runs):>4d}"]
    infected = [metric(r, "all_infected_s") for r in runs]
    cells.append(f"{sum(1 for v in infected if v is not None):>3d}/{len(runs):<3d}")
    for key, _short in METRICS:
        cells.append(_fmt(_median(metric(r, key) for r in runs)))
    return " ".join(cells)


def compare(baseline_dir: Path, after_dir: Path) -> None:
    before = load_runs(baseline_dir)
    after = load_runs(after_dir)
    groups: dict[str, dict[str, list]] = {}
    for label, runs in (("before", before), ("after", after)):
        for stem, run in runs.items():
            match = _SEED_SUFFIX.match(stem)
            group = match.group("group") if match else stem
            groups.setdefault(group, {"before": [], "after": []})[label].append(run)
    header = (f"{'scenario (medians)':38s} {'runs':>4s} {'inf':>7s} "
              + " ".join(f"{short:>8s}" for _key, short in METRICS))
    print(f"before: {baseline_dir}\nafter:  {after_dir}\n")
    print(header)
    print("-" * len(header))
    for group in sorted(groups):
        for label in ("before", "after"):
            runs = groups[group][label]
            if runs:
                print(_summary_row(f"{group} [{label}]", runs))
        print()
    print(header)
    print("-" * len(header))
    shared = sorted(g for g, sides in groups.items() if sides["before"] and sides["after"])
    if len(shared) != len(groups):
        # Different scenario sets: the honest overall is over the shared ones.
        print(_summary_row("ALL shared scenarios [before]",
                           [r for g in shared for r in groups[g]["before"]]))
        print(_summary_row("ALL shared scenarios [after]",
                           [r for g in shared for r in groups[g]["after"]]))
        print(f"  ({len(shared)} of {len(groups)} scenario groups are on both sides; "
              "the rows below take every run of each folder)")
    print(_summary_row("ALL [before]", list(before.values())))
    print(_summary_row("ALL [after]", list(after.values())))
    common = sorted(set(before) & set(after))
    if common:
        print(f"\npaired runs ({len(common)}):")
        print(f"{'run':40s} {'infected before':>16s} {'after':>8s} {'useful before':>14s} {'after':>8s}")
        for stem in common:
            print(f"{stem:40s} {_fmt(metric(before[stem], 'all_infected_s'), 16)} "
                  f"{_fmt(metric(after[stem], 'all_infected_s'))} "
                  f"{_fmt(metric(before[stem], 'useful_share'), 14)} "
                  f"{_fmt(metric(after[stem], 'useful_share'))}")


def table(folder: Path) -> None:
    runs = load_runs(folder)
    header = (f"{'run':40s} {'infected':>8s} {'collapse':>8s} {'reach':>5s} {'pile':>5s} "
              f"{'pileS':>6s} {'useful':>6s} {'idleW':>6s} {'atH':>6s} {'sides':>5s} "
              f"{'still':>5s} {'deaths':>6s} {'shots':>5s} {'hits':>4s} {'rep':>3s} {'dur':>5s}")
    print(header)
    print("-" * len(header))
    for stem in sorted(runs):
        r = runs[stem]
        shots = r.get("survivor_shots")
        print(f"{stem:40s} {_fmt(metric(r, 'all_infected_s'))} {_fmt(metric(r, 'collapse_s'))} "
              f"{_fmt(metric(r, 'zombies_reached'), 5)} {_fmt(metric(r, 'pile_mean'), 5)} "
              f"{_fmt(metric(r, 'pile_seconds'), 6)} {_fmt(metric(r, 'useful_share'), 6)} "
              f"{_fmt(metric(r, 'idle_share_worst_bot'), 6)} {_fmt(metric(r, 'first_at_height_s'), 6)} "
              f"{_fmt(metric(r, 'sides_used'), 5)} {_fmt(metric(r, 'longest_still_max_s'), 5)} "
              f"{_fmt(metric(r, 'zombie_deaths'), 6)} "
              f"{_fmt(None if shots is None else float(shots), 5)} "
              f"{_fmt(metric(r, 'survivor_hits'), 4)} {_fmt(metric(r, 'repairs'), 3)} "
              f"{_fmt(metric(r, 'duration_s'), 5)}")
    print()
    print(_summary_row("ALL", list(runs.values())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="folder for the run JSONs")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="full")
    parser.add_argument("--seeds", default="3,7,11")
    parser.add_argument("--zombies", type=int, default=8)
    parser.add_argument("--survivors", type=int, default=1)
    parser.add_argument("--seconds", type=float, default=150.0)
    parser.add_argument("--armed", action="store_true", help="the survivor shoots back and repairs")
    parser.add_argument("--hit-chance", type=float, default=1.0,
                        help="armed survivor accuracy (1.0 perfect, 0.6 a good human)")
    parser.add_argument("--spawn-distance", default=None,
                        help="LO,HI blocks from the structure where the horde starts (harness default 16,26)")
    parser.add_argument("--baseline", action="store_true", help="disable the horde coordinator")
    parser.add_argument("--jobs", type=int, default=3, help="parallel subprocesses")
    parser.add_argument("--inprocess", action="store_true",
                        help="import the harness and run every scenario in this process")
    parser.add_argument("--only", help="run only names containing this text")
    parser.add_argument("--force", action="store_true", help="redo runs whose JSON exists")
    parser.add_argument("--compare", type=Path, metavar="BASELINE_DIR",
                        help="print before/after medians of BASELINE_DIR vs --out (no runs)")
    parser.add_argument("--table", action="store_true", help="print the --out table only (no runs)")
    args = parser.parse_args()
    args.seeds = [int(s) for s in str(args.seeds).split(",") if s.strip()]
    if args.compare is not None:
        compare(args.compare, args.out)
        return
    if not args.table:
        run_matrix(args)
    table(args.out)


if __name__ == "__main__":
    main()
