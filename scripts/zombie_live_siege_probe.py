"""Launch a real server with the Zombie siege probe plugin and print its summary.

Copies config.toml into a temporary folder with Zombie mode on London, 12
admin-population bots, port 32895, only ``plugins/zombie_siege_probe.py``
enabled, a short infection delay, master/Steam registration off; starts
``run_server.py --config <tmp> --offline --control-stdin --status-file ...``
in this checkout; relays the plugin's ``probe`` log lines; waits for the
summary JSON; stops the server with a clean ``shutdown`` on stdin; prints the
summary.

    py -3.12 scripts/zombie_live_siege_probe.py [--seconds 150] [--armed] [--height 8]
        [--bots 12] [--port 32895] [--map London] [--infection-delay 8] [--out PATH]

The probe plugin stays inert unless named in ``[plugins] allowlist``; this
launcher is the only thing that names it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import toml

ROOT = Path(__file__).resolve().parents[1]
PROBE_MARKERS = ("probe ", "ZombieSiegeProbe", "Server started", "Loaded plugin",
                 "Bot runtime started", "Traceback", "Error", "ERROR")


def build_config(source: Path, target: Path, args) -> None:
    document = toml.load(source)
    document.setdefault("server", {})["port"] = int(args.port)
    game = document.setdefault("game", {})
    game["default_mode"] = "zom"
    game["default_map"] = args.map
    bots = document.setdefault("bots", {})
    bots["enabled"] = True
    bots["population_mode"] = "admin"
    bots["max_bots"] = int(args.bots)
    bots["fill_target"] = int(args.bots)
    plugins = document.setdefault("plugins", {})
    plugins["enabled"] = True
    plugins["path"] = "plugins"
    plugins["allowlist"] = ["zombie_siege_probe"]
    plugins["denylist"] = []
    document.setdefault("revival", {})["enabled"] = False
    document.setdefault("steam", {})["enabled"] = False
    zom = document.setdefault("modes", {}).setdefault("zom", {})
    zom["infection_delay"] = float(args.infection_delay)
    zom["minimum_players"] = 2
    zom["time_limit"] = max(600, int(args.seconds) + 300)
    logging_table = document.setdefault("logging", {})
    logging_table["console"] = True
    logging_table["level"] = "INFO"
    target.write_text(toml.dumps(document), encoding="utf-8")


def _relay(stream, log_path: Path, quiet: bool) -> None:
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            log.write(line + "\n")
            log.flush()
            if not quiet and any(marker in line for marker in PROBE_MARKERS):
                print(line, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=150.0, help="observation window")
    parser.add_argument("--armed", action="store_true", help="the survivor shoots back and repairs")
    parser.add_argument("--height", type=int, default=8)
    parser.add_argument("--bots", type=int, default=12)
    parser.add_argument("--port", type=int, default=32895)
    parser.add_argument("--map", default="London")
    parser.add_argument("--infection-delay", type=float, default=8.0,
                        help="[modes.zom] infection_delay for the temp config (retail 60)")
    parser.add_argument("--no-infect", action="store_true",
                        help="leave the bot survivors alone instead of infecting them at the start")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=ROOT / "logs" / "zombie_siege_probe.json")
    parser.add_argument("--startup-timeout", type=float, default=300.0,
                        help="seconds allowed for map load, bots and the outbreak")
    parser.add_argument("--quiet", action="store_true", help="do not relay probe log lines")
    parser.add_argument("--keep", action="store_true", help="keep the temp folder")
    args = parser.parse_args()

    if not (ROOT / "config.toml").is_file():
        print("config.toml not found in", ROOT, file=sys.stderr)
        return 2
    temp = Path(tempfile.mkdtemp(prefix="zombie_siege_probe_"))
    config_path = temp / "config.toml"
    status_path = temp / "status.json"
    server_log = temp / "server.log"
    build_config(ROOT / "config.toml", config_path, args)
    out = args.out.resolve()
    if out.exists():
        out.unlink()

    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "ZOMBIE_SIEGE_PROBE_SECONDS": str(args.seconds),
        "ZOMBIE_SIEGE_PROBE_HEIGHT": str(args.height),
        "ZOMBIE_SIEGE_PROBE_ARMED": "1" if args.armed else "0",
        "ZOMBIE_SIEGE_PROBE_INFECT": "0" if args.no_infect else "1",
        "ZOMBIE_SIEGE_PROBE_MAP": args.map,
        "ZOMBIE_SIEGE_PROBE_SEED": str(args.seed),
        "ZOMBIE_SIEGE_PROBE_OUT": str(out),
    })
    command = [sys.executable, "-X", "utf8", str(ROOT / "run_server.py"), "--config", str(config_path),
               "--offline", "--control-stdin", "--status-file", str(status_path)]
    print("launching:", " ".join(command), flush=True)
    print("temp folder:", temp, flush=True)
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    relay = threading.Thread(target=_relay, args=(process.stdout, server_log, args.quiet), daemon=True)
    relay.start()

    deadline = started + args.startup_timeout + args.seconds + 60.0
    summary = None
    exit_code = 1
    try:
        while time.monotonic() < deadline:
            if out.is_file():
                time.sleep(0.5)
                try:
                    summary = json.loads(out.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                break
            if process.poll() is not None:
                print(f"server exited early with code {process.returncode}; see {server_log}",
                      file=sys.stderr)
                break
            time.sleep(1.0)
        else:
            print("timed out waiting for the probe summary; see", server_log, file=sys.stderr)
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(b"shutdown\n")
                process.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=25.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10.0)
        relay.join(timeout=5.0)

    if summary is None:
        print("no summary; server log:", server_log)
        return exit_code
    exit_code = 0
    print("\n=== zombie siege probe summary ===")
    print(json.dumps(summary, indent=1))
    shares = summary.get("activity_share", {})
    print("\nmap=%s site=%s pillar=%s armed=%s zombies=%s duration=%ss" % (
        summary.get("map"), summary.get("site"), summary.get("height"), summary.get("armed"),
        summary.get("zombies"), summary.get("duration_s")))
    print("survivor infected at: %s s   first hit: %s s   collapse: %s s" % (
        summary.get("survivor_infected_s"), summary.get("first_hit_s"), summary.get("collapse_s")))
    print("zombies reached (<=3 blocks): %s/%s   first at height: %s s   sides used: %s/8" % (
        summary.get("zombies_reached"), summary.get("zombies"), summary.get("first_at_height_s"),
        summary.get("sides_used")))
    print("pile mean/max/seconds: %s / %s / %s" % (
        summary.get("pile_mean"), summary.get("pile_max"), summary.get("pile_seconds")))
    print("useful share: %s   worst bot idle: %s   ring idle: %s   longest still: %s s" % (
        summary.get("useful_share"), summary.get("idle_share_worst_bot"),
        summary.get("ring_idle_share"), summary.get("longest_still_max_s")))
    print("activity:", ", ".join(f"{k}={v}" for k, v in shares.items()))
    print("zombie deaths/respawns: %s / %s" % (summary.get("zombie_deaths"), summary.get("zombie_respawns")))
    if "survivor_shots" in summary:
        print("survivor shots/hits/kills: %s / %s / %s   repairs: %s   blocked: %s" % (
            summary.get("survivor_shots"), summary.get("survivor_hits"), summary.get("survivor_kills"),
            summary.get("repairs"), summary.get("shots_blocked")))
    print("summary file:", out)
    print("server log:", server_log)
    if not args.keep:
        # Keep the server log beside the summary for the report; drop the rest.
        try:
            shutil.copy(server_log, out.with_suffix(".server.log"))
            shutil.rmtree(temp, ignore_errors=True)
            print("server log copied to:", out.with_suffix(".server.log"))
        except OSError:
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
