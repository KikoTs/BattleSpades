"""Full-roster, multi-seed mode and adversarial-target gameplay audits.

Each case owns a separate process because the native world and synthetic clock
are process-global. Reports identify the sources and retain the full trace.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class Case:
    mode: str
    map_name: str
    seed: int
    scenario: str | None = None
    humans: int = 0
    custom: bool = False

    @property
    def key(self) -> str:
        return f"{self.mode}_{self.map_name}_{self.scenario or 'match'}_s{self.seed}"


def cases(*, scope: str, seeds: tuple[int, ...], maps_per_mode: int,
          targets: bool = False, retired: bool = False) -> list[Case]:
    from modes.unofficial.catalog import CATALOG, RETIRED
    from server.mode_data import MODES

    core = ("tdm", "ctf", "cctf", "vip", "zom", "czom", "mh", "tc", "dia", "dem", "oc", "arena")
    modes = list(core) if scope in {"all", "core"} else []
    if scope in {"all", "variants"}:
        modes += [code for code in MODES if code.startswith("hc_")]
    if scope in {"all", "custom"}:
        modes += [code for code in CATALOG if retired or code not in RETIRED]
    metadata = json.loads((ROOT / "maps/retail_map_info.json").read_text(encoding="utf-8"))["maps"]
    result: list[Case] = []
    for mode in modes:
        if mode in CATALOG:
            choices = [CATALOG[mode].map_name]
        else:
            base = mode.removeprefix("hc_")
            base = {"cctf": "ctf", "czom": "zom", "arena": "tdm", "nor": "tdm"}.get(base, base)
            preferred = (["Crossroads", "WW1"] if mode.removeprefix("hc_") == "cctf" else
                         ["SpookyMansion", "ArcticBase", "CityOfChicago"] if base == "zom" else
                         ["CityOfChicago", "ArcticBase", "SpookyMansion", "Crossroads", "London"])
            choices = [name for name in preferred if base in metadata.get(name, {}).get("valid_modes", ())]
            choices += [name for name, data in metadata.items()
                        if name not in choices and base in data["valid_modes"]]
        if not choices:
            raise ValueError(f"no legal map for {mode}")
        for name in choices[:maps_per_mode]:
            result.extend(Case(mode, name, seed, humans=2 if mode in CATALOG else 0,
                               custom=mode in CATALOG) for seed in seeds)
    if targets:
        from scripts.bot_gameplay_scenarios import TARGET_SCENARIOS
        result.extend(Case(mode, "ArcticBase", seed, scenario)
                      for mode in ("vip", "zom", "czom")
                      for scenario in TARGET_SCENARIOS for seed in seeds)
    return result


def generate_maps(directory: Path, selected: list[Case]) -> None:
    from modes.unofficial.maps import atomic_write, build_arena

    directory.mkdir(parents=True, exist_ok=True)
    for name in sorted({case.map_name for case in selected if case.custom}):
        path = directory / f"{name}.vxl"
        sidecar = path.with_suffix(".txt")
        if path.exists() and sidecar.exists():
            continue
        world, metadata = build_arena(name)
        atomic_write(path, world.generate_vxl(False))
        atomic_write(sidecar, json.dumps(metadata).encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("all", "core", "variants", "custom"), default="all")
    parser.add_argument("--seed", action="append", type=int, dest="seeds")
    parser.add_argument("--maps-per-mode", type=int, default=1)
    parser.add_argument("--skip-primary-map", action="store_true", help="Only additional maps for each mode")
    parser.add_argument("--targets", action="store_true")
    parser.add_argument("--targets-only", action="store_true")
    parser.add_argument("--mode", action="append", dest="modes", help="Restrict to these exact mode codes")
    parser.add_argument("--resume", action="store_true", help="Reuse complete reports from identical gameplay sources")
    parser.add_argument("--include-retired", action="store_true")
    parser.add_argument("--bots", type=int, default=16)
    parser.add_argument("--seconds", type=float, default=360.0)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if not 4 <= args.bots <= 24 or args.seconds <= 0 or args.jobs < 1 or args.maps_per_mode < 1:
        parser.error("use 4..24 bots, positive seconds/jobs/maps-per-mode")
    selected = cases(scope=args.scope, seeds=tuple(args.seeds or (0, 7, 19)),
                     maps_per_mode=args.maps_per_mode, targets=args.targets or args.targets_only, retired=args.include_retired)
    if args.targets_only:
        selected = [case for case in selected if case.scenario]
    if args.skip_primary_map:
        primary = {}
        for case in selected:
            if not case.scenario:
                primary.setdefault(case.mode, case.map_name)
        selected = [case for case in selected if case.scenario or case.map_name != primary.get(case.mode)]
    if args.modes:
        selected = [case for case in selected if case.mode in args.modes]
    if not selected:
        parser.error("no cases selected")
    if args.list:
        print(json.dumps([asdict(case) for case in selected], indent=2))
        return 0
    output = args.out_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    map_dir = output / "arenas"
    generate_maps(map_dir, selected)
    environment = dict(os.environ, BOT_AUDIT_ROOT=str(args.source_root.resolve()))
    started = time.perf_counter()

    def run(case: Case) -> dict:
        path = output / f"{case.key}.json.gz"
        cmd = [sys.executable, "-X", "faulthandler", str(args.source_root / "scripts/bot_audit_harness.py"),
               "--map", case.map_name, "--mode", case.mode, "--seed", str(case.seed),
               "--seconds", str(args.seconds), "--bots", str(args.bots), "--out", str(path),
               "--humans", str(case.humans)]
        if case.custom:
            cmd += ["--maps-path", str(map_dir)]
        if case.mode == "night_shift":
            cmd += ["--max-bots", "30"]
        if case.scenario:
            cmd += ["--scenario", case.scenario]
        try:
            reuse = False
            if args.resume and path.exists():
                from scripts.bot_audit_harness import source_digest
                with gzip.open(path, "rt", encoding="utf-8") as stream:
                    previous = json.load(stream)
                reuse = (not previous.get("error") and previous.get("source_unchanged_during_run")
                         and previous.get("source_digest") == source_digest(args.source_root.resolve())
                         and previous.get("simulated_seconds", 0) >= args.seconds
                         and previous.get("requested_bots") == args.bots
                         and (case.mode != "night_shift" or previous.get("initial_bot_count") == args.bots)
                         and previous.get("audit_version") == 3)
            if not reuse:
                process = subprocess.run(cmd, env=environment, capture_output=True, text=True, timeout=3600)
                path.with_suffix(".log").write_text(process.stdout + process.stderr, encoding="utf-8")
                if process.returncode or not path.exists():
                    return {"case": asdict(case), "error": f"exit {process.returncode}", "report": str(path)}
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                report = json.load(stream)
            from scripts.bot_gameplay_review import review
            return {"case": asdict(case), "error": report["error"],
                    "source_unchanged": report.get("source_unchanged_during_run", False),
                    "source_digest": report.get("source_digest"),
                    "seconds": report["simulated_seconds"], "scores": report["team_scores"],
                    "phase": report["final_phase"], "scenario": report.get("scenario", {}),
                    "behavior": report.get("behavior_metrics", {}), "report": str(path),
                    **review(report)}
        except (subprocess.TimeoutExpired, OSError, ValueError) as error:
            return {"case": asdict(case), "error": str(error), "report": str(path)}

    completed: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        pending = {pool.submit(run, case): case for case in selected}
        for future in as_completed(pending):
            row = future.result()
            completed.append(row)
            print(f"[{len(completed)}/{len(selected)} {time.perf_counter()-started:.0f}s] "
                  f"{pending[future].key} error={row['error'] or '-'} scores={row.get('scores')}", flush=True)
            (output / "index.json").write_text(json.dumps({"complete": False, "results": completed}, indent=2), encoding="utf-8")
    failures = sum(bool(row["error"]) or not row.get("source_unchanged") for row in completed)
    (output / "index.json").write_text(json.dumps({"complete": True, "cases": len(completed),
        "execution_failures": failures, "results": completed}, indent=2), encoding="utf-8")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
