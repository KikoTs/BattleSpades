"""Accelerated, production-faithful bot audit harness.

Differences from scripts/bot_map_matrix.py:

* The real ``BotDirector.update`` runs every tick through
  ``SimulationRuntime.step`` (perception cadence, stimuli, cooperative
  behaviour version from config.toml, motor phases, action drain, mode ticks,
  projectiles, entities, respawns).
* The AI thread is replaced by an in-line supervisor that executes exactly the
  thread's batch body once per gameplay tick, so the run is independent of
  host scheduling.  ``time.monotonic`` and ``time.time`` are a simulated clock
  from before the server object exists.
* Everything a player would notice is logged: deaths with cause, damage,
  executed bot actions per tool, mode objective method calls, objective
  snapshots, and a 4 Hz per-bot trace.

Usage::

    py -3.12 scripts/bot_audit_harness.py --map Classic --mode ctf --seed 0 --seconds 300 --out runs/x.json.gz
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import replace
import gzip
import json
import logging
import math
import os
from pathlib import Path
import random
import sys
import time
import traceback

ROOT = Path(os.environ.get("BOT_AUDIT_ROOT", Path(__file__).resolve().parents[1]))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ORIGINAL_CWD = Path.cwd()
os.chdir(ROOT)

SIM_HZ = 60
SAMPLE_TICKS = 15          # 4 Hz trace
VIS_TICKS = 30             # 2 Hz enemy-visibility oracle
OBJ_TICKS = 30             # 2 Hz objective snapshot

_REAL_MONOTONIC = time.monotonic
_REAL_TIME = time.time


class SimClock:
    def __init__(self, base: float = 50_000.0, wall: float = 1_790_000_000.0):
        self.base = float(base)
        self.wall = float(wall)
        self.elapsed = 0.0

    def monotonic(self) -> float:
        return self.base + self.elapsed

    def time(self) -> float:
        return self.wall + self.elapsed


CLOCK = SimClock()


def install_clock() -> None:
    time.monotonic = CLOCK.monotonic
    time.time = CLOCK.time


def uninstall_clock() -> None:
    time.monotonic = _REAL_MONOTONIC
    time.time = _REAL_TIME


def _make_sync_supervisor(decision_hz: float, path_rps: float):
    from server.bot_ai.thread_supervisor import AIThreadSupervisor
    from server.bot_ai.simple_navigation import SimpleVoxelWorld
    from server.bot_ai.planning_budget import PlanningBudget
    from server.bot_ai.simple_worker import (
        SimpleBotBrain, behavior_metrics_snapshot, decide_current_frame,
        refresh_planning_observers,
    )
    from server.bot_ai.messages import WorldDelta

    class SyncSupervisor(AIThreadSupervisor):
        """AIThreadSupervisor whose batch body runs in-line via ``pump``."""

        def start(self, snapshot) -> None:  # noqa: D401
            with self._lock:
                self._latest_snapshot = snapshot
                self._snapshot_serial += 1
                self._terrain_map_epoch = int(snapshot.map_epoch)
                self._terrain_version = int(snapshot.topology_version)
                self._terrain_pending.clear()
                self._terrain_overlay = {c.coordinate: c for c in snapshot.changed_cells}
                self._frames.clear()
                self._intents.clear()
                self._running = True
                self._ready = False
                self._last_processed_at = time.monotonic()
            if os.environ.get("BOT_AUDIT_NO_BUDGET") == "1":
                self._world = SimpleVoxelWorld()
            else:
                self._world = SimpleVoxelWorld(planning_budget=PlanningBudget(
                    self.path_requests_per_second, decision_hz=self.decision_hz))
            self._brain = SimpleBotBrain(self._world, decision_hz=self.decision_hz)
            self._applied_serial = -1
            self.decision_ms_max = 0.0
            self.decision_errors = 0
            self.pump()

        def close(self, timeout: float = 3.0) -> None:
            with self._lock:
                self._running = False
                self._ready = False

        def recover_if_stopped(self) -> bool:
            return False

        def status(self):
            status = super().status()
            return replace(status, running=bool(self._running and self._ready))

        @property
        def brain(self):
            return self._brain

        @property
        def world(self):
            return self._world

        def pump(self) -> int:
            with self._lock:
                snapshot_serial = int(self._snapshot_serial)
                snapshot = None
                if (self._latest_snapshot is not None
                        and snapshot_serial != self._applied_serial):
                    snapshot = replace(
                        self._latest_snapshot,
                        topology_version=int(self._terrain_version),
                        changed_cells=tuple(self._terrain_overlay.values()),
                    )
                    self._terrain_pending.clear()
                changes = tuple(self._terrain_pending.values())
                self._terrain_pending.clear()
                map_epoch = int(self._terrain_map_epoch)
                topology_version = int(self._terrain_version)
                frames = tuple(sorted(self._frames.values(), key=lambda f: int(f.frame_id)))
                self._frames.clear()
            if snapshot is not None:
                self._world.load(snapshot)
                self._brain = SimpleBotBrain(self._world, decision_hz=self.decision_hz)
                self._brain.reset_for_map(snapshot.map_epoch)
                self._applied_serial = snapshot_serial
                with self._lock:
                    self._ready = True
            elif changes:
                self._world.apply(WorldDelta(map_epoch, topology_version, changes))
            intents = []
            processed = -1
            refresh_planning_observers(self._world, frames)
            for frame in frames:
                processed = max(processed, int(frame.frame_id))
                started = time.perf_counter()
                intent = decide_current_frame(self._world, self._brain, frame)
                self.decision_ms_max = max(self.decision_ms_max,
                                           (time.perf_counter() - started) * 1000.0)
                if intent is not None:
                    intents.append(intent)
            with self._lock:
                for intent in intents:
                    self._intents.append(intent)
                self._last_processed_at = time.monotonic()
                self._last_acknowledged_frame_id = max(
                    self._last_acknowledged_frame_id, processed)
                if (self._awaiting_frame_id is not None
                        and processed >= self._awaiting_frame_id):
                    self._awaiting_frame_id = None
            return len(frames)

    return SyncSupervisor(seed=0, decision_hz=decision_hz, path_requests_per_second=path_rps)


MODE_HOOKS = (
    # ctf / cctf
    "_pickup_intel", "_drop_intel", "_capture_intel", "_return_intel",
    # multi hill / tc
    "_announce_claim", "_activate_next", "_announce_capture", "_announce_team_entered",
    # diamond
    "_spawn_diamond", "_pickup_diamond", "_cash_in", "_cash_in_loose",
    "_drop_carried_diamond", "_rotate_dropoff",
    # occupation
    "_spawn_bomb", "_pickup_bomb", "_drop_bomb",
    # vip
    "_select_vips", "_promote_vip", "_kill_vip", "_finish_round", "_begin_round",
    "_reassign_vip",
    # zombie
    "_start_outbreak", "_infect", "_begin_next_round",
    # generic
    "on_mode_end", "_end_by_score", "_end_by_time", "_restart_round", "on_round_start",
    "on_round_end", "_run_end_sequence", "_focus_airstrike",
)


def _arg_summary(args, kwargs):
    out = []
    for value in list(args)[:3] + list(kwargs.values())[:2]:
        if hasattr(value, "id") and hasattr(value, "team"):
            out.append({"p": int(value.id), "team": int(value.team)})
        elif isinstance(value, (int, float, str, bool)) or value is None:
            out.append(value)
        elif hasattr(value, "position"):
            try:
                out.append({"pos": [round(float(v), 1) for v in value.position]})
            except Exception:  # noqa: BLE001
                out.append(type(value).__name__)
        else:
            out.append(type(value).__name__)
    return out


def instrument_mode(mode, events: list, now) -> None:
    for name in MODE_HOOKS:
        original = getattr(mode, name, None)
        if original is None or not callable(original):
            continue
        if asyncio.iscoroutinefunction(original):
            def make(original=original, name=name):
                async def wrapped(*args, **kwargs):
                    events.append([round(now(), 2), name, _arg_summary(args, kwargs)])
                    return await original(*args, **kwargs)
                return wrapped
        else:
            def make(original=original, name=name):
                def wrapped(*args, **kwargs):
                    events.append([round(now(), 2), name, _arg_summary(args, kwargs)])
                    return original(*args, **kwargs)
                return wrapped
        try:
            setattr(mode, name, make())
        except Exception:  # noqa: BLE001
            pass


def _r(value, digits=2):
    return round(float(value), digits)


async def run_case(*, map_name: str, mode_name: str, seed: int, seconds: float, bots: int,
                   out: Path | None, class_ids: tuple[int, ...] = (),
                   natural_limits: bool = True, behavior: str | None = None,
                   scenario=None, quiet: bool = True, max_bots: int = 0) -> dict:
    wall_started = time.perf_counter()
    install_clock()
    CLOCK.elapsed = 0.0
    random.seed(int(seed))
    if quiet:
        logging.disable(logging.WARNING)

    import shared.constants as C
    from modes import get_mode_class
    from server.bot_ai import BotDirector
    from server.config import load_config
    from server.main import BattleSpadesServer
    from server.player import Player

    config = load_config(ROOT / "config.toml")
    config.default_mode = str(mode_name).lower()
    config.default_map = str(map_name)
    config.maps_path = str(ROOT / "maps")
    config.bots.population_mode = "admin"
    config.bots.max_bots = max(int(bots), int(max_bots))
    config.bots.seed = int(seed)
    config.max_players = max(int(config.max_players), int(bots), int(max_bots) + 8)
    if behavior is not None:
        config.bots.behavior_version = behavior
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(map_name):
        raise RuntimeError(f"map {map_name} did not load")
    mode_class = get_mode_class(config.default_mode)
    if mode_class is None:
        raise ValueError(f"unknown mode {mode_name}")
    server.mode = mode_class(server)
    mode_events: list = []
    now_fn = lambda: CLOCK.elapsed  # noqa: E731
    await server.mode.on_mode_start()
    instrument_mode(server.mode, mode_events, now_fn)
    _mode = server.mode
    if hasattr(_mode, "_detonate_bomb"):
        _orig_detonate = _mode._detonate_bomb

        async def _detonate(bomb, *a, **k):
            inside = None
            try:
                inside = bool(_mode._bomb_inside_target(bomb.position))
            except Exception:  # noqa: BLE001
                pass
            mode_events.append([round(now_fn(), 2), "_detonate_bomb",
                                [{"inside": inside, "team": int(getattr(bomb, "team", -1)),
                                  "pos": [round(float(v), 1) for v in bomb.position]}]])
            return await _orig_detonate(bomb, *a, **k)

        _mode._detonate_bomb = _detonate
    if hasattr(_mode, "objective_cells"):
        def _dem_mutation(x, y, z, solid, color, version):
            if getattr(_mode, "phase", "") not in ("active", "airstrike"):
                return
            cell = (int(x), int(y), int(z))
            for team, cells in getattr(_mode, "objective_cells", {}).items():
                if cell in cells:
                    mode_events.append([round(now_fn(), 2),
                                        "dem_repaired" if solid else "dem_destroyed",
                                        [int(team)]])
        server.world_manager.subscribe_mutations(_dem_mutation)
    if not natural_limits:
        server.mode.score_limit = 1_000_000
        server.mode.time_limit = float(seconds) + 600.0

    supervisor = _make_sync_supervisor(float(config.bots.decision_hz),
                                       float(config.bots.path_requests_per_second))
    director = BotDirector(server, supervisor=supervisor)
    server.bots = director

    # ---- hooks -----------------------------------------------------------
    deaths: list = []
    damages: list = []
    actions: list = []
    original_die = Player.die
    original_damage = Player.damage

    def role_of(player) -> str:
        runtime = director._runtime.get(int(getattr(player, "id", -1)))
        intent = runtime.intent if runtime is not None else None
        return str(getattr(intent, "debug_role", "") or "")

    def die(self, killer=None, kill_type=0):
        if self.alive:
            deaths.append([
                round(CLOCK.elapsed, 2), int(self.id), int(getattr(self, "team", -1)),
                int(getattr(self, "class_id", -1)),
                int(killer.id) if killer is not None else -1,
                int(kill_type),
                [_r(self.x), _r(self.y), _r(self.z)],
                role_of(self),
                bool(getattr(self, "wade", False)),
                int(self.pickup_id) if getattr(self, "pickup_id", None) is not None else -1,
            ])
        return original_die(self, killer, kill_type)

    def damage(self, amount, source=None, kill_type=0, **kwargs):
        before = int(getattr(self, "health", 0))
        result = original_damage(self, amount, source, kill_type, **kwargs)
        after = int(getattr(self, "health", 0))
        if before != after or result:
            damages.append([
                round(CLOCK.elapsed, 2), int(self.id),
                int(source.id) if source is not None else -1,
                int(before - after), int(kill_type),
            ])
        return result

    Player.die = die
    Player.damage = damage

    original_execute = director.gateway.execute

    def execute(player, action):
        accepted = original_execute(player, action)
        actions.append([
            round(CLOCK.elapsed, 2), int(player.id), str(action.kind.value),
            int(getattr(action, "tool_id", -1)), bool(accepted),
            str(getattr(action, "argument", "") or "")[:24],
        ])
        return accepted

    director.gateway.execute = execute

    result: dict = {
        "map": map_name, "mode": mode_name, "seed": int(seed), "bots": int(bots),
        "requested_seconds": float(seconds), "behavior": config.bots.behavior_version,
        "natural_limits": bool(natural_limits),
    }
    trace: dict[int, list] = defaultdict(list)
    vis: dict[int, list] = defaultdict(list)
    objective_rows: list = []
    score_rows: list = []
    bot_meta: dict[int, dict] = {}
    error = ""
    no_intent_ticks: Counter = Counter()
    alive_ticks: Counter = Counter()
    jetpack_ticks: Counter = Counter()
    parachute_ticks: Counter = Counter()
    tool_ticks: dict[int, Counter] = defaultdict(Counter)
    tick_ms_max = 0.0
    try:
        await director.start(initial_count=int(bots))
        if class_ids:
            # Optional forced classes (after the normal join) for ability probes.
            pass
        on_tick = None
        scenario_state: dict = {}
        result["scenario"] = scenario_state
        if scenario is not None:
            on_tick = await scenario(server, director, CLOCK, scenario_state)
        total_ticks = max(1, int(round(float(seconds) * SIM_HZ)))
        world = server.world_manager
        for tick in range(total_ticks):
            CLOCK.elapsed = tick / SIM_HZ
            server.loop_count += 1
            if on_tick is not None:
                on_tick(tick, CLOCK.elapsed)
            started = time.perf_counter()
            await server.simulation_runtime.step()
            supervisor.pump()
            tick_ms_max = max(tick_ms_max, (time.perf_counter() - started) * 1000.0)
            await asyncio.sleep(0)
            now = time.monotonic()
            live_bots = [p for p in server.players.values() if getattr(p, "is_bot", False)]
            for bot in live_bots:
                bid = int(bot.id)
                runtime = director._runtime.get(bid)
                intent = runtime.intent if runtime is not None else None
                if bot.alive and bot.spawned:
                    alive_ticks[bid] += 1
                    if intent is None or intent.expires_at <= now:
                        no_intent_ticks[bid] += 1
                    if getattr(bot, "jetpack_active", False):
                        jetpack_ticks[bid] += 1
                    if getattr(bot, "parachute_active", False):
                        parachute_ticks[bid] += 1
                    tool_ticks[bid][int(getattr(bot, "tool", -1))] += 1
            if tick % SAMPLE_TICKS == 0:
                t = round(CLOCK.elapsed, 2)
                for bot in live_bots:
                    bid = int(bot.id)
                    runtime = director._runtime.get(bid)
                    intent = runtime.intent if runtime is not None else None
                    fresh = bool(intent is not None and intent.expires_at > now)
                    meta = bot_meta.setdefault(bid, {"names": [], "classes": [], "teams": []})
                    cls = int(getattr(bot, "class_id", -1))
                    if not meta["classes"] or meta["classes"][-1][1] != cls:
                        meta["classes"].append([t, cls])
                    team = int(getattr(bot, "team", -1))
                    if not meta["teams"] or meta["teams"][-1][1] != team:
                        meta["teams"].append([t, team])
                    goal = getattr(intent, "debug_goal", None) if intent is not None else None
                    trace[bid].append([
                        t,
                        1 if (bot.alive and bot.spawned) else 0,
                        _r(bot.x), _r(bot.y), _r(bot.z),
                        str(getattr(intent, "debug_role", "") or "") if fresh else ("STALE" if intent is not None else "NONE"),
                        str(intent.action.kind.value) if fresh else "",
                        1 if getattr(bot, "wade", False) else 0,
                        1 if getattr(bot, "grounded", False) else 0,
                        int(getattr(bot, "health", 0)),
                        int(getattr(bot, "tool", -1)),
                        int(bot.pickup_id) if getattr(bot, "pickup_id", None) is not None else -1,
                        [_r(goal[0], 1), _r(goal[1], 1), _r(goal[2], 1)] if goal is not None else None,
                        str(intent.movement.affordance.value) if fresh else "",
                        1 if (fresh and math.hypot(intent.movement.direction[0], intent.movement.direction[1]) > 0.1) else 0,
                        int(getattr(getattr(intent, "look", None), "target_player_id", -1)) if fresh and intent.look is not None else -1,
                        int(getattr(bot, "blocks", 0)),
                        _r(getattr(bot, "o_x", 0.0)), _r(getattr(bot, "o_y", 0.0)), _r(getattr(bot, "o_z", 0.0)),
                    ])
            if tick % VIS_TICKS == 0:
                t = round(CLOCK.elapsed, 2)
                alive = [p for p in server.players.values() if p.alive and p.spawned]
                for bot in alive:
                    if not getattr(bot, "is_bot", False):
                        continue
                    best = None
                    for other in alive:
                        if other is bot or int(other.team) == int(bot.team):
                            continue
                        d = math.dist((bot.eye_x, bot.eye_y, bot.eye_z),
                                      (other.eye_x, other.eye_y, other.eye_z))
                        if d > 48.0 or (best is not None and d >= best[1]):
                            continue
                        if server._blocked_los(bot.eye_x, bot.eye_y, bot.eye_z,
                                               other.eye_x, other.eye_y, other.eye_z):
                            continue
                        dx, dy = other.eye_x - bot.eye_x, other.eye_y - bot.eye_y
                        h = math.hypot(dx, dy)
                        facing = ((bot.o_x * dx + bot.o_y * dy) / h) if h > 1e-6 else 1.0
                        best = (int(other.id), d, facing)
                    if best is not None:
                        vis[int(bot.id)].append([t, best[0], _r(best[1], 1), _r(best[2])])
            if tick % OBJ_TICKS == 0:
                t = round(CLOCK.elapsed, 2)
                try:
                    objectives = director._snapshot_objectives()
                except Exception:  # noqa: BLE001
                    objectives = ()
                objective_rows.append([t, [
                    [o.kind, int(o.team), [_r(o.position[0], 1), _r(o.position[1], 1), _r(o.position[2], 1)],
                     int(o.carrier_id), int(o.state), _r(o.progress, 3), int(o.attacker)]
                    for o in objectives if o.kind not in ("zombie_order",)
                ][:40]])
                score_rows.append([t, {int(k): int(getattr(v, "score", 0)) for k, v in server.teams.items()},
                                   str(director._mode_phase())])
        result["simulated_seconds"] = (tick + 1) / SIM_HZ
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"
        result["simulated_seconds"] = CLOCK.elapsed
    finally:
        Player.die = original_die
        Player.damage = original_damage
        logging.disable(logging.NOTSET)

    players = {int(p.id): p for p in server.players.values()}
    result.update({
        "error": error,
        "wall_seconds": round(time.perf_counter() - wall_started, 1),
        "tick_ms_max": round(tick_ms_max, 1),
        "decision_ms_max": round(getattr(supervisor, "decision_ms_max", 0.0), 1),
        "team_scores": {int(k): int(getattr(v, "score", 0)) for k, v in server.teams.items()},
        "final_phase": str(director._mode_phase()),
        "topology_version": int(server.world_manager.topology_version),
        "terrain_recoveries": int(director.terrain_recoveries),
        "bot_meta": {str(k): v for k, v in bot_meta.items()},
        "final_bots": {
            str(pid): {
                "team": int(p.team), "class_id": int(getattr(p, "class_id", -1)),
                "kills": int(getattr(p, "kills", 0)), "deaths": int(getattr(p, "deaths", 0)),
                "score": int(getattr(p, "score", 0)), "is_bot": bool(getattr(p, "is_bot", False)),
                "name": str(getattr(p, "name", "")),
            } for pid, p in players.items()
        },
        "alive_seconds": {str(k): round(v / SIM_HZ, 1) for k, v in alive_ticks.items()},
        "no_intent_seconds": {str(k): round(v / SIM_HZ, 1) for k, v in no_intent_ticks.items()},
        "jetpack_seconds": {str(k): round(v / SIM_HZ, 1) for k, v in jetpack_ticks.items()},
        "parachute_seconds": {str(k): round(v / SIM_HZ, 1) for k, v in parachute_ticks.items()},
        "tool_seconds": {str(k): {str(t): round(n / SIM_HZ, 1) for t, n in c.items()}
                         for k, c in tool_ticks.items()},
        "mode_events": mode_events,
        "deaths": deaths,
        "damages": damages,
        "actions": actions,
        "objectives": objective_rows,
        "scores": score_rows,
        "trace": {str(k): v for k, v in trace.items()},
        "vis": {str(k): v for k, v in vis.items()},
        "planning": supervisor._world.planning_budget.snapshot() if getattr(supervisor, "_world", None) is not None and supervisor._world.planning_budget is not None else {},
        "no_budget": os.environ.get("BOT_AUDIT_NO_BUDGET") == "1",
    })
    try:
        from server.bot_ai.simple_worker import behavior_metrics_snapshot
        metrics = behavior_metrics_snapshot(supervisor._brain)
        result["behavior_metrics"] = {
            "counters": metrics.get("counters", {}),
            "project_kinds": metrics.get("project_kinds", {}),
            "active_projects": metrics.get("active_projects", 0),
        }
    except Exception:  # noqa: BLE001
        result["behavior_metrics"] = {}
    try:
        skills = supervisor._brain.skills
        result["skill_metrics"] = dict(getattr(skills, "metrics", {}) or {})
        result["skill_events"] = [list(map(lambda v: list(v) if isinstance(v, tuple) else v, e))
                                  for e in list(getattr(skills, "events", []))[-64:]]
    except Exception:  # noqa: BLE001
        result["skill_metrics"] = {}
        result["skill_events"] = []
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt", encoding="utf-8") as stream:
            json.dump(result, stream, separators=(",", ":"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True)
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--bots", type=int, default=12)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--behavior", choices=("classic", "cooperative"), default=None)
    parser.add_argument("--unlimited", action="store_true",
                        help="raise score/time limits so one round runs for the whole case")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.out is not None and not args.out.is_absolute():
        args.out = ORIGINAL_CWD / args.out
    result = asyncio.run(run_case(
        map_name=args.map, mode_name=args.mode, seed=args.seed, seconds=args.seconds,
        bots=args.bots, out=args.out, natural_limits=not args.unlimited,
        behavior=args.behavior, quiet=not args.verbose,
    ))
    uninstall_clock()
    kills = sum(1 for d in result["deaths"] if d[4] >= 0 and d[4] != d[1])
    print(
        f"{'ERR ' if result['error'] else 'OK  '}{args.map} {args.mode} s{args.seed} "
        f"sim={result['simulated_seconds']:.0f}s wall={result['wall_seconds']}s "
        f"deaths={len(result['deaths'])} kills={kills} scores={result['team_scores']} "
        f"events={len(result['mode_events'])} phase={result['final_phase']} "
        f"tickmax={result['tick_ms_max']}ms decmax={result['decision_ms_max']}ms",
        flush=True,
    )
    if result["error"]:
        print(result["error"], flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
