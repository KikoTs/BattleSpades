"""Run the audit harness over maps x modes x seeds in parallel subprocesses."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[1]
CLASSIC_MAPS = {"Crossroads", "Hiesville", "ToTheBridge", "Trenches", "WinterValley", "WW1", "Classic"}
SECONDS = {"tdm": 240, "ctf": 420, "cctf": 420, "vip": 360, "zom": 420, "mh": 360,
           "tc": 360, "dia": 420, "dem": 420, "oc": 420}


def cases(seeds, only_maps=None, only_modes=None):
    info = json.loads((ROOT / "maps" / "retail_map_info.json").read_text())["maps"]
    maps = sorted(p.stem for p in (ROOT / "maps").glob("*.vxl"))
    out = []
    for map_name in maps:
        if only_maps and map_name not in only_maps:
            continue
        entry = info.get(map_name)
        modes = [m for m in (entry["valid_modes"] if entry else ["tdm"]) if m not in ("tut", "ugc")]
        if map_name == "Training":
            modes = ["tdm"]
        if map_name == "20thCenturyTown":
            modes = ["tdm", "zom", "ctf"]
        if map_name in CLASSIC_MAPS and "ctf" in modes:
            modes = ["cctf" if m == "ctf" else m for m in modes]
            if map_name in ("Classic", "WW1"):
                modes.append("ctf")
        for mode in modes:
            if only_modes and mode not in only_modes:
                continue
            for seed in seeds:
                out.append((map_name, mode, int(seed)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", action="append", type=int, dest="seeds")
    parser.add_argument("--map", action="append", dest="maps")
    parser.add_argument("--mode", action="append", dest="modes")
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--bots", type=int, default=12)
    parser.add_argument("--seconds", type=float, default=0.0)
    parser.add_argument("--tag", default="m")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--behavior", default=None)
    parser.add_argument("--out-dir", default=str(ROOT / "tmp" / "bot-audit"))
    args = parser.parse_args()
    todo = cases(args.seeds or [0], set(args.maps or ()), set(args.modes or ()))
    if args.list:
        print(len(todo))
        for item in todo:
            print(item)
        return 0
    runs = Path(args.out_dir)
    runs.mkdir(exist_ok=True)
    log = runs / f"index_{args.tag}.log"
    lock = threading.Lock()
    started = time.time()

    def run(case):
        map_name, mode, seed = case
        out = runs / f"{args.tag}_{map_name}_{mode}_s{seed}.json.gz"
        if out.exists():
            return
        seconds = args.seconds or SECONDS.get(mode, 300)
        cmd = [sys.executable, "-X", "faulthandler", str(HERE / "bot_audit_harness.py"), "--map", map_name, "--mode", mode,
               "--seed", str(seed), "--seconds", str(seconds), "--bots", str(args.bots),
               "--out", str(out)]
        if args.behavior:
            cmd += ["--behavior", args.behavior]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            line = (proc.stdout.strip().splitlines() or ["(no output)"])[0]
            if proc.returncode != 0:
                line = f"FAIL rc={proc.returncode} {map_name} {mode} s{seed} :: " + (proc.stdout[-1500:] + proc.stderr[:4000]).replace(chr(10), " | ")
        except subprocess.TimeoutExpired:
            line = f"TIMEOUT {map_name} {mode} s{seed}"
        with lock:
            with log.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
            print(f"[{time.time() - started:6.0f}s] {line}", flush=True)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        list(pool.map(run, todo))
    print("DONE", len(todo), "cases", round(time.time() - started), "s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
