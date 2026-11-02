"""Zombie fun gate: a scripted human survivor against the horde, on the real tick.

Nobody play-tests for us, so this is the judge. A server-owned survivor is
driven exactly like a retail client would drive it (input frames for every
move, BlockLine for every block, ShootPacket for every shot; see
``scripts/zombie_fun_survivor.py``) and plays one tactic against ``--bots``
zombie bots that spawn at the map's retail zombie spawns and run through the
production director, thread worker, motor, physics, claws and collapse, on the
real clock (one asyncio sleep per 60 Hz tick, like ``bot_zombie_siege_scenario``).

Tactics (``--tactic``): ``pillar`` run to open ground and pillar up 8;
``roof`` get on a building roof (exterior route if the heightmap has one,
else start there) and pillar up 6; ``tree`` walk out the treebridge walkway,
retreat 10 when a zombie steps on it; ``runner`` sprint a 4-waypoint street
loop and shoot on the move; ``bunker`` walk into a street-level room, seal
the doorway's lower cell, shoot through the hole, re-seal; ``neck`` stand on
the wall-neck platform and thicken the neck when it is clawed.

    py -3.12 scripts/zombie_fun_gate.py --map London --bots 16 --seconds 240 \
        --tactic pillar --seed 7 --json out.json
    py -3.12 scripts/zombie_fun_gate.py --all --seconds 240 --parallel 3
    py -3.12 scripts/zombie_fun_gate.py --real-server --tactic neck --seed 3

``--all`` runs every tactic for seeds 3, 7, 11 (subprocesses, ``--parallel``
at a time), prints a table and PASS/FAIL per threshold (``--max-infection-s``,
``--max-infection-runner-s``, ``--max-stuck``, ``--max-pile-mean``,
``--max-idle-share``). ``--real-server`` launches ``run_server.py`` with the
helper module installed as a plugin and reports the same JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from zombie_fun_survivor import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    ELEVATED_TACTICS,
    FunMetrics,
    SAMPLE_SECONDS,
    Survivor,
    TACTIC_CLASSES,
    TACTICS,
    WEAPON_BY_NAME,
    judge,
    spawn_survivor,
    summary_lines,
)

DEFAULT_SEEDS = (3, 7, 11)


class _Gate:
    def __init__(self, server, survivor_spawn) -> None:
        self.server = server
        self.world = server.world_manager
        self.survivor_spawn = survivor_spawn


async def run_in_process(args) -> dict:
    from modes import get_mode_class
    from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase
    from server.bot_ai import BotDirector
    from server.config import load_config
    from server.main import BattleSpadesServer

    rng = random.Random(args.seed)
    random.seed(args.seed)
    config = load_config(ROOT / "config.toml")
    config.default_mode = "zom"
    config.default_map = args.map
    config.bots.population_mode = "admin"
    config.bots.max_bots = int(args.bots)
    config.bots.seed = int(args.seed)
    config.max_players = max(config.max_players, args.bots + 8)
    if hasattr(config, "revival"):
        try:
            config.revival.enabled = False
        except AttributeError:
            pass
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("map did not load")
    world = server.world_manager
    server.mode = get_mode_class("zom")(server)
    await server.mode.on_mode_start()
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 10.0
    while not director.status().running and loop.time() < deadline:
        await asyncio.sleep(0.02)

    # The round is on: the survivor spawns where the map spawns survivors,
    # the tactic may walk from there or (tree/neck/bunker/roof-without-stairs)
    # start on its structure. That first placement is the only one.
    server.mode.phase = ZombiePhase.ACTIVE
    server.mode.start_time = time.time()
    server.mode.time_limit = max(float(server.mode.time_limit), float(args.seconds) + 120.0)
    survivor_spawn = tuple(float(v) for v in world.get_spawn_point(SURVIVOR_TEAM))
    gate = _Gate(server, survivor_spawn)
    tactic = TACTIC_CLASSES[args.tactic](gate, rng, {"height": args.height})
    start = tactic.start_position()
    player = spawn_survivor(server, start, name="GateHuman")
    survivor = Survivor(server, player, random.Random(int(args.seed) * 7919 + 1),
                        hit_chance=float(args.hit_chance),
                        weapon=WEAPON_BY_NAME[args.weapon], resupply=bool(args.resupply))
    tactic.after_spawn(survivor)

    zombies = []
    for index in range(int(args.bots)):
        bot = await director.add_bot(team=ZOMBIE_TEAM, name=f"Zed{index}", difficulty="hard")
        if bot is None:
            raise RuntimeError("could not add zombie bot")
        zombies.append(bot)   # spawned by the mode at the retail zombie spawn
    zombie_starts = [tuple(round(float(v), 1) for v in z.position) for z in zombies]
    deadline = loop.time() + 10.0
    while not director.status().running and loop.time() < deadline:
        await asyncio.sleep(0.02)

    metrics = FunMetrics(server, director, zombies, [player],
                         horde_floor=tactic.horde_floor, elevated=tactic.name in ELEVATED_TACTICS)
    tick = server.tick_interval
    steps = int(args.seconds / tick)
    sample_every = max(1, int(round(SAMPLE_SECONDS / tick)))
    next_at = loop.time()
    wall_started = time.perf_counter()
    ended_at = None
    step = 0
    for step in range(steps):
        t = step * tick
        server.loop_count += 1
        survivor.settle_builds()
        survivor.observe(t, zombies)
        if survivor.alive:
            tactic.update(t, zombies)
            survivor.record_frame(t)
        started = time.perf_counter()
        await server.simulation_runtime.step()
        metrics.tick_ms.append((time.perf_counter() - started) * 1000.0)
        metrics.tick(t)
        if step % sample_every == 0:
            metrics.sample(t, [player] if survivor.alive else [])
            if args.trace and step % (sample_every * 4) == 0:
                _trace(t, survivor, tactic, zombies, director)
        if not survivor.alive:
            if ended_at is None:
                ended_at = t
            elif t - ended_at > 2.0:
                break   # the infection has had its ticks
        next_at += tick
        delay = next_at - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            next_at = loop.time()
    survivor.observe((step + 1) * tick, zombies)
    result = {
        "map": args.map, "tactic": args.tactic, "seed": int(args.seed), "zombies": len(zombies),
        "seconds": float(args.seconds), "duration_s": round((step + 1) * tick, 1),
        "wall_s": round(time.perf_counter() - wall_started, 1),
        "hit_chance": float(args.hit_chance), "weapon": args.weapon, "resupply": bool(args.resupply),
        "real_server": False,
        "survivor_spawn": tuple(round(v, 1) for v in survivor_spawn),
        "survivor_start": tuple(round(float(v), 1) for v in start),
        "zombie_starts": zombie_starts[:4],
        "zombie_start_distance_mean": round(sum(
            math.hypot(z[0] - start[0], z[1] - start[1]) for z in zombie_starts) / max(1, len(zombie_starts)), 1),
        "site": tactic.site,
    }
    result.update(metrics.report(len(zombies)))
    result.update(survivor.report())
    result.update(tactic.report())
    siege = getattr(director, "_zombie_siege", None)
    if siege is not None:
        result["siege_stats"] = dict(siege.stats)
    result["verdict"] = judge(result, thresholds_from(args))
    await director.close()
    return result


def _trace(t, survivor, tactic, zombies, director) -> None:
    s = survivor.player
    runtimes = getattr(director, "_runtime", {}) or {}
    near = sorted(zombies, key=lambda z: math.dist(z.position, s.position))[:3]
    desc = " ".join(
        f"[{z.id}:{math.dist(z.position, s.position):.0f}m "
        f"{(getattr(getattr(runtimes.get(z.id), 'intent', None), 'debug_role', '') or '-').split(':')[0]}]"
        for z in near)
    print(f"t={t:6.1f} {tactic.state:<18} hp={int(s.health):3d} pos=({s.x:.1f},{s.y:.1f},{s.z:.1f}) "
          f"g={int(bool(s.grounded))} tool={s.tool} blocks={s.blocks} clip={s.ammo_clip} "
          f"shots={survivor.shots} hits={survivor.hits} builds={survivor.builds_accepted}/{survivor.builds_sent} "
          f"frames a/s/st={getattr(s, 'input_frames_applied', 0)}/{getattr(s, 'input_frames_synthesized', 0)}/"
          f"{getattr(s, 'input_starved_ticks', 0)} hist={len(getattr(s, 'input_history', {}) or {})} "
          f"near={desc}", flush=True)


def thresholds_from(args) -> dict:
    return {
        "max_infection_s": float(args.max_infection_s),
        "max_infection_runner_s": float(args.max_infection_runner_s),
        "max_stuck_zombies": int(args.max_stuck),
        "max_pile_mean": float(args.max_pile_mean),
        "max_idle_share": float(args.max_idle_share),
    }


# --------------------------------------------------------------------------- #
# --real-server: the same survivor inside run_server.py through the plugin
# --------------------------------------------------------------------------- #

def run_real_server(args) -> dict | None:
    import toml

    temp = Path(tempfile.mkdtemp(prefix="zombie_fun_gate_"))
    plugin_dir = temp / "plugins"
    plugin_dir.mkdir()
    shutil.copy(ROOT / "scripts" / "zombie_fun_survivor.py", plugin_dir / "zombie_fun_survivor.py")
    document = toml.load(ROOT / "config.toml")
    document.setdefault("server", {})["port"] = int(args.port)
    game = document.setdefault("game", {})
    game["default_mode"] = "zom"
    game["default_map"] = args.map
    bots = document.setdefault("bots", {})
    bots["enabled"] = True
    bots["population_mode"] = "admin"
    bots["max_bots"] = int(args.bots)
    bots["fill_target"] = int(args.bots)
    bots["seed"] = int(args.seed)
    plugins = document.setdefault("plugins", {})
    plugins["enabled"] = True
    plugins["path"] = str(plugin_dir)
    plugins["allowlist"] = ["zombie_fun_survivor"]
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
    config_path = temp / "config.toml"
    config_path.write_text(toml.dumps(document), encoding="utf-8")
    out = (args.json or (ROOT / "logs" / f"zombie_fun_gate_live_{args.tactic}_{args.seed}.json")).resolve()
    if out.exists():
        out.unlink()
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "ZOMBIE_FUN_GATE_ROOT": str(ROOT),
        "ZOMBIE_FUN_GATE_TACTIC": args.tactic,
        "ZOMBIE_FUN_GATE_SEED": str(args.seed),
        "ZOMBIE_FUN_GATE_SECONDS": str(args.seconds),
        "ZOMBIE_FUN_GATE_HIT_CHANCE": str(args.hit_chance),
        "ZOMBIE_FUN_GATE_WEAPON": args.weapon,
        "ZOMBIE_FUN_GATE_RESUPPLY": "1" if args.resupply else "0",
        "ZOMBIE_FUN_GATE_OUT": str(out),
    })
    status_path = temp / "status.json"
    server_log = temp / "server.log"
    command = [sys.executable, "-X", "utf8", str(ROOT / "run_server.py"), "--config", str(config_path),
               "--offline", "--control-stdin", "--status-file", str(status_path)]
    print("launching:", " ".join(command), flush=True)
    process = subprocess.Popen(command, cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    markers = ("gate ", "ZombieFunGate", "Server started", "Loaded plugin", "Traceback", "Error", "ERROR")

    def relay(stream):
        with server_log.open("w", encoding="utf-8", errors="replace") as log:
            for raw in stream:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                if any(marker in line for marker in markers):
                    print(line, flush=True)

    thread = threading.Thread(target=relay, args=(process.stdout,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 300.0 + float(args.seconds) + 60.0
    summary = None
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
                print(f"server exited early with code {process.returncode}; see {server_log}", file=sys.stderr)
                break
            time.sleep(1.0)
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
        thread.join(timeout=5.0)
    if summary is not None:
        summary["verdict"] = judge(summary, thresholds_from(args))
        try:
            shutil.copy(server_log, out.with_suffix(".server.log"))
        except OSError:
            pass
    if not args.keep:
        shutil.rmtree(temp, ignore_errors=True)
    return summary


# --------------------------------------------------------------------------- #
# --all
# --------------------------------------------------------------------------- #

def run_all(args) -> int:
    tactics = [t.strip() for t in args.tactics.split(",") if t.strip()] if args.tactics else list(TACTICS)
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else list(DEFAULT_SEEDS)
    out_dir = (args.out_dir or (ROOT / "tmp" / "zombie_fun_gate")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for tactic in tactics:
        for seed in seeds:
            path = out_dir / f"{tactic}_{seed}.json"
            cmd = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--map", args.map,
                   "--bots", str(args.bots), "--seconds", str(args.seconds), "--tactic", tactic,
                   "--seed", str(seed), "--json", str(path), "--hit-chance", str(args.hit_chance),
                   "--weapon", args.weapon, "--height", str(args.height), "--quiet"]
            if args.resupply:
                cmd.append("--resupply")
            if args.real_server:
                cmd += ["--real-server", "--port", str(int(args.port) + len(jobs))]
            jobs.append((tactic, seed, path, cmd))
    results = {}
    running: list = []
    pending = list(jobs)
    env = dict(os.environ, PYTHONUTF8="1")
    started = time.monotonic()
    while pending or running:
        while pending and len(running) < max(1, int(args.parallel)):
            tactic, seed, path, cmd = pending.pop(0)
            log = open(path.with_suffix(".log"), "w", encoding="utf-8")
            proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
            running.append((tactic, seed, path, proc, log))
            print(f"started {tactic} seed {seed}", flush=True)
        time.sleep(1.0)
        for item in list(running):
            tactic, seed, path, proc, log = item
            if proc.poll() is None:
                continue
            running.remove(item)
            log.close()
            if path.is_file():
                try:
                    results[(tactic, seed)] = json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    results[(tactic, seed)] = {"error": "bad json"}
            else:
                results[(tactic, seed)] = {"error": f"exit {proc.returncode}; see {path.with_suffix('.log')}"}
            print(f"finished {tactic} seed {seed} ({time.monotonic() - started:.0f}s elapsed)", flush=True)
    thresholds = thresholds_from(args)
    table = render_table(jobs, results, thresholds)
    print(table)
    (out_dir / "table.txt").write_text(table, encoding="utf-8")
    (out_dir / "all.json").write_text(json.dumps(
        {f"{t}_{s}": r for (t, s), r in results.items()}, indent=1, default=str), encoding="utf-8")
    print("results in", out_dir)
    failed = sum(1 for r in results.values() if "error" in r or not r.get("verdict", {}).get("all"))
    return 1 if failed else 0


def render_table(jobs, results, thresholds) -> str:
    def fmt(value, width, digits=1):
        if value is None:
            text = "-"
        elif isinstance(value, float):
            text = f"{value:.{digits}f}"
        else:
            text = str(value)
        return text.rjust(width)

    header = (f"{'tactic':<7}{'seed':>5}{'infect':>8}{'contact':>8}{'dmg':>7}{'zdead':>6}{'pile':>6}"
              f"{'pmax':>5}{'idle':>6}{'useful':>7}{'stuck':>6}{'nvr10':>6}{'stack':>6}{'waste':>6}"
              f"{'rep':>5}{'blk':>5}{'shots':>6}{'hits':>5}{'p99':>6}  verdict")
    lines = [header, "-" * len(header)]
    for tactic, seed, _path, _cmd in jobs:
        r = results.get((tactic, seed), {})
        if "error" in r:
            lines.append(f"{tactic:<7}{seed:>5}  ERROR {r['error']}")
            continue
        v = judge(r, thresholds)
        flags = "".join(k[0].upper() if ok else k[0] for k, ok in v.items() if k != "all")
        lines.append(
            f"{tactic:<7}{seed:>5}{fmt(r.get('infected_s'), 8)}{fmt(r.get('first_contact_s'), 8)}"
            f"{fmt(r.get('first_damage_s'), 7)}{fmt(r.get('zombie_deaths'), 6)}{fmt(r.get('pile_mean'), 6, 2)}"
            f"{fmt(r.get('pile_max'), 5)}{fmt(r.get('idle_share'), 6, 2)}{fmt(r.get('useful_share'), 7, 2)}"
            f"{fmt(r.get('stuck_zombies'), 6)}{fmt(r.get('zombies_never_within_10'), 6)}"
            f"{fmt(r.get('stacking_events'), 6)}{fmt(r.get('stacking_wasted'), 6)}{fmt(r.get('repairs'), 5)}"
            f"{fmt(r.get('blocks_spent'), 5)}{fmt(r.get('survivor_shots'), 6)}{fmt(r.get('survivor_hits'), 5)}"
            f"{fmt(r.get('tick_p99_ms'), 6)}  {'PASS' if v['all'] else 'FAIL'} [{flags}]"
            + (f" tele={r.get('teleports_after_spawn')}" if r.get("teleports_after_spawn") else ""))
    lines.append("")
    lines.append("flags: I=infection in time, S=no stuck zombie, P=pile_mean below limit, D=idle share below limit "
                 "(upper case = pass)")
    lines.append(f"thresholds: infection <= {thresholds['max_infection_s']:.0f} s "
                 f"(runner {thresholds['max_infection_runner_s']:.0f} s), stuck <= {thresholds['max_stuck_zombies']}, "
                 f"pile_mean < {thresholds['max_pile_mean']}, standing+wandering < {thresholds['max_idle_share']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--map", default="London")
    parser.add_argument("--bots", type=int, default=16)
    parser.add_argument("--seconds", type=float, default=240.0)
    parser.add_argument("--tactic", choices=TACTICS, default="pillar")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--hit-chance", type=float, default=0.6, help="survivor hit chance 0.5-0.7 (bounded aim)")
    parser.add_argument("--weapon", choices=sorted(WEAPON_BY_NAME), default="rifle")
    parser.add_argument("--resupply", action="store_true", help="ammo crate when dry instead of 10+30 rounds only")
    parser.add_argument("--height", type=int, default=8, help="pillar height (roof uses 6)")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="no JSON dump on stdout (summary only)")
    parser.add_argument("--real-server", action="store_true", help="run inside a real run_server.py via the plugin")
    parser.add_argument("--port", type=int, default=32895)
    parser.add_argument("--infection-delay", type=float, default=8.0, help="--real-server: [modes.zom] infection_delay")
    parser.add_argument("--keep", action="store_true", help="--real-server: keep the temp folder")
    parser.add_argument("--all", action="store_true", help="every tactic for seeds 3,7,11; prints a table")
    parser.add_argument("--tactics", help="--all: comma list of tactics (default all)")
    parser.add_argument("--seeds", help="--all: comma list of seeds (default 3,7,11)")
    parser.add_argument("--parallel", type=int, default=3, help="--all: concurrent runs")
    parser.add_argument("--out-dir", type=Path, help="--all: folder for the per-run JSON/logs")
    parser.add_argument("--max-infection-s", type=float, default=DEFAULT_THRESHOLDS["max_infection_s"])
    parser.add_argument("--max-infection-runner-s", type=float, default=DEFAULT_THRESHOLDS["max_infection_runner_s"])
    parser.add_argument("--max-stuck", type=int, default=DEFAULT_THRESHOLDS["max_stuck_zombies"])
    parser.add_argument("--max-pile-mean", type=float, default=DEFAULT_THRESHOLDS["max_pile_mean"])
    parser.add_argument("--max-idle-share", type=float, default=DEFAULT_THRESHOLDS["max_idle_share"])
    args = parser.parse_args()

    if args.all:
        return run_all(args)
    if args.real_server:
        result = run_real_server(args)
        if result is None:
            print("no summary from the live server", file=sys.stderr)
            return 2
    else:
        result = asyncio.run(run_in_process(args))
    text = json.dumps(result, indent=1, default=str)
    if not args.quiet:
        print(text)
    print("\n".join(summary_lines(result)))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(text, encoding="utf-8")
    return 0 if result.get("verdict", {}).get("all") else 1


if __name__ == "__main__":
    raise SystemExit(main())
