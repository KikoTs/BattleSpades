"""Headless end-to-end smoke for bot lifecycle, worker, and native physics."""

from __future__ import annotations

import asyncio
import argparse
from collections import Counter
import math
import json
import hashlib
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modes import get_mode_class
from server.bot_ai import BotDirector
from server.config import load_config
from server.main import BattleSpadesServer

if TYPE_CHECKING:
    from server.player import Player


async def _run(
    *,
    seconds: float = 4.0,
    bot_count: int = 2,
    mode_name: str = "tdm",
    map_name: str | None = None,
    water_spawn_bots: int = 0,
    restart_worker_at: float | None = None,
    progress_every: float = 0.0,
    trace_state: bool = False,
    seed: int | None = None,
    detect_team_congestion: bool = False,
    worker_backend: str | None = None,
    full_runtime: bool = False,
    report_path: Path | None = None,
    behavior_version: str | None = None,
    config_path: Path | None = None,
    trace_path: Path | None = None,
    detect_travel_loops: bool = False,
    detect_objective_abandonment: bool = False,
) -> None:
    if detect_travel_loops and trace_path is None:
        raise ValueError("--detect-travel-loops requires --trace-jsonl")
    if detect_objective_abandonment and (trace_path is None or str(mode_name).lower() not in {"vip", "ctf", "cctf"}):
        raise ValueError("--detect-objective-abandonment requires VIP/CTF and --trace-jsonl")
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    def source_hashes():
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((ROOT / "server/bot_ai").glob("*.py"))}
    initial_source_hashes = source_hashes()
    if seed is not None:
        # WorldManager deliberately uses the module RNG when shuffling authored
        # spawn candidates. Seed both that path and the bot profile factory so
        # a field report can be reduced to one replayable match.
        random.seed(int(seed))
    config = load_config(config_path or ROOT / "config.toml")
    config.default_mode = str(mode_name).lower()
    if map_name is not None:
        config.default_map = str(map_name)
    config.bots.population_mode = "admin"
    if behavior_version is not None:
        config.bots.behavior_version = behavior_version
    config.bots.max_bots = max(1, int(bot_count))
    config.max_players = max(config.max_players, config.bots.max_bots)
    if seed is not None:
        config.bots.seed = int(seed)
    if worker_backend is not None:
        config.bots.worker = worker_backend
    elif restart_worker_at is not None:
        # Killing a child is specifically a process-backend acceptance.
        config.bots.worker = "process"
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("smoke map did not load")
    mode_class = get_mode_class(config.default_mode)
    if mode_class is None:
        raise ValueError(f"unsupported mode: {config.default_mode}")
    server.mode = mode_class(server)
    await server.mode.on_mode_start()
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=config.bots.max_bots)
    if len(director.bots) != config.bots.max_bots:
        await director.close()
        raise RuntimeError("smoke could not create the requested bot population")
    starts = {bot.id: bot.position for bot in director.bots}
    unsafe_spawns = {
        bot.id: bot.position
        for bot in director.bots
        if not server.world_manager.spawn_position_is_safe(bot.position)
    }
    if unsafe_spawns:
        raise RuntimeError(f"unsafe production bot spawns: {unsafe_spawns}")

    def nearest_water_anchor(position, search: int = 96):
        center_x, center_y = int(position[0]), int(position[1])
        for radius in range(1, int(search) + 1):
            candidates = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    x, y = center_x + dx, center_y + dy
                    if not (0 <= x < 512 and 0 <= y < 512):
                        continue
                    if server.world_manager.is_water_column(x, y):
                        candidates.append(
                            (
                                (x + 0.5 - position[0]) ** 2
                                + (y + 0.5 - position[1]) ** 2,
                                x,
                                y,
                            )
                        )
            if candidates:
                _distance, x, y = min(candidates)
                surface = server.world_manager.get_height(x, y)
                return x + 0.5, y + 0.5, float(surface) - 2.25
        return None

    water_started: dict[int, tuple[float, float, float]] = {}
    for bot in director.bots[:max(0, int(water_spawn_bots))]:
        anchor = nearest_water_anchor(bot.position)
        if anchor is None:
            raise RuntimeError(
                f"no real water column near production spawn {bot.position}"
            )
        bot.set_position(*anchor)
        bot._world_object.set_velocity(0.0, 0.0, 0.0)
        water_started[bot.id] = anchor

    previous_positions = {bot.id: bot.position for bot in director.bots}
    missing_intent_since: dict[int, float] = {}
    max_intent_gap = {bot.id: 0.0 for bot in director.bots}
    was_alive = {bot.id: bot.alive for bot in director.bots}
    respawns = {bot.id: 0 for bot in director.bots}
    progress_positions = dict(previous_positions)
    requested_stall_ticks = {bot.id: 0 for bot in director.bots}
    max_requested_stall_ticks = {bot.id: 0 for bot in director.bots}
    max_requested_stall_details: dict[int, dict[str, object]] = {}
    congestion_ticks = 0
    max_congestion_ticks = 0
    congestion_details: dict[str, object] = {}
    position_history: dict[int, list[tuple[float, tuple[float, float, float]]]] = {
        bot.id: [] for bot in director.bots
    }
    water_exit_seconds: dict[int, float] = {}
    worker_deadline = asyncio.get_running_loop().time() + 10.0
    status = director.status()
    original_pid = status.process_id
    while not status.running and asyncio.get_running_loop().time() < worker_deadline:
        await asyncio.sleep(0.02)
        status = director.status()
        original_pid = status.process_id
    if not status.running:
        raise RuntimeError("worker did not become ready")
    restart_requested = False
    restart_observed = False
    loop = asyncio.get_running_loop()
    next_tick_at = loop.time()
    tick_costs: list[float] = []
    role_samples: Counter[str] = Counter()
    action_results: Counter[str] = Counter()
    last_feedback: dict[int, int] = {}
    cpu_started = time.process_time()
    wall_started = time.perf_counter()
    trace_stream = None
    try:
        if trace_path is not None:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_stream = trace_path.open("w", encoding="utf-8")
        trace_steps = max(1, round(.1 / server.tick_interval))
        progress_steps = (
            max(1, int(float(progress_every) / server.tick_interval))
            if progress_every > 0.0
            else 0
        )
        for step in range(max(1, int(float(seconds) / server.tick_interval))):
            elapsed = step * server.tick_interval
            if (
                restart_worker_at is not None
                and not restart_requested
                and elapsed >= float(restart_worker_at)
            ):
                if original_pid is None:
                    director.supervisor.request_restart()
                else:
                    # This PID came from our director; only kill our child.
                    os.kill(original_pid, signal.SIGTERM)
                restart_requested = True
            server.loop_count += 1
            tick_started = time.perf_counter()
            if full_runtime:
                await server.simulation_runtime.step()
            else:
                await director.update(server.tick_interval)
                director.drain_actions(limit=1)
                await server.simulation_runtime._simulate_players()
            tick_costs.append((time.perf_counter() - tick_started) * 1000)
            now = asyncio.get_running_loop().time()
            trace_objectives = ()
            if trace_stream is not None and step % trace_steps == 0:
                trace_objectives = tuple({"kind": objective.kind, "team": objective.team,
                    "position": objective.position, "carrier_id": objective.carrier_id,
                    "state": objective.state} for objective in director._snapshot_objectives()
                    if objective.kind in {"vip", "team_anchor", "ctf_base", "ctf_intel"})[:16]
            for bot in director.bots:
                runtime = director._runtime.get(bot.id)
                intent = runtime.intent if runtime is not None else None
                if intent is not None:
                    role_samples[intent.debug_role.split(":")[0]] += 1
                if runtime is not None and runtime.feedback_action_frame != last_feedback.get(bot.id):
                    last_feedback[bot.id] = runtime.feedback_action_frame
                    if runtime.feedback_action_kind:
                        action_results[runtime.feedback_action_kind + (":accepted" if runtime.feedback_action_accepted else ":rejected")] += 1
                if bot.alive and not was_alive[bot.id]:
                    respawns[bot.id] += 1
                was_alive[bot.id] = bot.alive
                if bot.alive and bot.spawned and (intent is None or intent.expires_at <= now):
                    since = missing_intent_since.setdefault(bot.id, now)
                    max_intent_gap[bot.id] = max(max_intent_gap[bot.id], now - since)
                else:
                    missing_intent_since.pop(bot.id, None)
                requested = (
                    intent is not None
                    and intent.expires_at > now
                    and math.hypot(
                        intent.movement.direction[0],
                        intent.movement.direction[1],
                    ) > 0.1
                )
                previous = previous_positions.get(bot.id, bot.position)
                planar_delta = math.hypot(
                    bot.x - previous[0],
                    bot.y - previous[1],
                )
                if requested and planar_delta < 1e-5:
                    requested_stall_ticks[bot.id] += 1
                else:
                    requested_stall_ticks[bot.id] = 0
                if (
                    requested_stall_ticks[bot.id]
                    > max_requested_stall_ticks[bot.id]
                ):
                    max_requested_stall_ticks[bot.id] = (
                        requested_stall_ticks[bot.id]
                    )
                    max_requested_stall_details[bot.id] = {
                        "ticks": requested_stall_ticks[bot.id],
                        "position": bot.position,
                        "velocity": (
                            float(getattr(bot, "vx", 0.0)),
                            float(getattr(bot, "vy", 0.0)),
                            float(getattr(bot, "vz", 0.0)),
                        ),
                        "role": (
                            intent.debug_role if intent is not None else None
                        ),
                        "goal": (
                            intent.debug_goal if intent is not None else None
                        ),
                        "affordance": (
                            intent.movement.affordance.value
                            if intent is not None
                            else None
                        ),
                        "direction": (
                            intent.movement.direction
                            if intent is not None
                            else None
                        ),
                    }
                previous_positions[bot.id] = bot.position
                if trace_stream is not None and step % trace_steps == 0:
                    look = intent.look if intent is not None else None
                    terrain_target = (intent.action.position if intent else None)
                    if terrain_target is None and runtime and runtime.pending_action:
                        terrain_target = runtime.pending_action.position
                    terrain_cell = (tuple(math.floor(value) for value in terrain_target)
                                    if terrain_target is not None else None)
                    record = {
                        "t": round(elapsed, 4), "id": bot.id,
                        "wall_t": round(time.perf_counter() - wall_started, 6),
                        "life": bot.replication_generation,
                        "team": bot.team, "class_id": bot.class_id,
                        "score": bot.score,
                        "carried_entity_id": int(bot.pickup_id if bot.pickup_id is not None else -1),
                        "pickup_burdensome": bool(bot.pickup_burdensome),
                        "shoot_with_intel": bool(getattr(server.mode, "shoot_with_intel", False)),
                        "mode_state": {
                            "mode": config.default_mode,
                            "phase": getattr(getattr(server.mode, "phase", None), "name", None),
                            "vips": {str(team): player.id if player is not None else None
                                     for team, player in getattr(server.mode, "vips", {}).items()},
                            "vip_alive": getattr(server.mode, "vip_alive", {}),
                            "respawn_enabled": getattr(server.mode, "respawn_enabled", {}),
                            "team_scores": {str(team): value.score for team, value in server.teams.items()},
                            "objectives": trace_objectives,
                        },
                        "alive": bot.alive, "spawned": bot.spawned, "health": bot.health,
                        "kills": int(getattr(bot, "kills", 0)), "deaths": int(getattr(bot, "deaths", 0)),
                        "ammo": (int(getattr(bot, "ammo_clip", 0)), int(getattr(bot, "ammo_reserve", 0))),
                        "reloading": bool(getattr(bot, "reloading", False)),
                        "jetpack": (int(getattr(bot, "jetpack_id", 0) or 0),
                                    round(float(getattr(bot, "jetpack_fuel", 0.0) or 0.0), 1)),
                        "blocks": int(getattr(bot, "blocks", 0)),
                        "crouch": bool(getattr(getattr(bot, "input", None), "crouch", False)),
                        "position": bot.position,
                        "topology_version": server.world_manager.topology_version,
                        "terrain_target": ({
                            "cell": terrain_cell,
                            "solid": server.world_manager.get_solid(*terrain_cell),
                            "damage": server.world_manager.block_damage.get(terrain_cell, 0.0),
                        } if terrain_cell is not None else None),
                        "velocity": bot.velocity,
                        "eye": (bot.eye_x, bot.eye_y, bot.eye_z),
                        "orientation": (bot.o_x, bot.o_y, bot.o_z),
                        "grounded": bot.grounded, "wade": bot.wade, "tool": bot.tool,
                        "dry_safe": bool(bot.grounded and not bot.wade
                                         and server.world_manager.spawn_position_is_safe(bot.position)),
                        "intent_fresh": bool(intent and intent.expires_at > now),
                        "intent_age_seconds": round(now - intent.created_at, 6) if intent else None,
                        "role": intent.debug_role if intent else None,
                        "goal": intent.debug_goal if intent else None,
                        "path": intent.debug_path if intent else None,
                        "navigation": dict(intent.debug_navigation) if intent else None,
                        "movement": intent.movement.direction if intent else None,
                        "sprint": intent.movement.sprint if intent else None,
                        "travel_source": intent.movement.travel_source if intent else None,
                        "travel_waypoint": intent.movement.travel_waypoint if intent else None,
                        "gaze_waypoint": intent.movement.gaze_waypoint if intent else None,
                        "gaze_purpose": runtime.motor.gaze_purpose if runtime else None,
                        "affordance": intent.movement.affordance.value if intent else None,
                        "look": look.target if look else None,
                        "visible": look.visible if look else None,
                        "target_id": getattr(look, "target_player_id", -1),
                        "action": intent.action.kind.value if intent else None,
                        "action_position": intent.action.position if intent else None,
                        "feedback": (runtime.feedback_action_kind,
                                     runtime.feedback_action_accepted,
                                     runtime.feedback_action_frame) if runtime else None,
                        "feedback_reason": runtime.feedback_reason if runtime else None,
                        "pending": runtime.pending_action.kind.value if runtime and runtime.pending_action else None,
                        "pending_position": runtime.pending_action.position if runtime and runtime.pending_action else None,
                        "motor": (runtime.motor.yaw, runtime.motor.pitch,
                                  runtime.motor.yaw_velocity, runtime.motor.pitch_velocity) if runtime else None,
                    }
                    trace_stream.write(json.dumps(record) + "\n")
                history = position_history[bot.id]
                history.append((elapsed, bot.position))
                cutoff = elapsed - 8.0
                while len(history) > 1 and history[1][0] <= cutoff:
                    del history[0]
                if (
                    bot.id in water_started
                    and bot.id not in water_exit_seconds
                    and bot.alive and bot.grounded and not bot.wade
                    and server.world_manager.spawn_position_is_safe(bot.position)
                ):
                    water_exit_seconds[bot.id] = elapsed

            # Reproduce the field failure, not merely a stationary individual:
            # four or more green bots converge on the same narrow excavation
            # lane, keep requesting a distant goal, and shuffle enough that a
            # per-tick "did it move?" check incorrectly passes.  Ignore match
            # opening and require a persistent eight-second rolling collapse.
            green_bots = [
                bot for bot in director.bots
                if int(bot.team) == 3 and bot.alive and bot.spawned
            ]
            dense_green: list[Player] = []
            if detect_team_congestion and elapsed >= 15.0 and len(green_bots) >= 4:
                for anchor in green_bots:
                    cohort = [
                        bot for bot in green_bots
                        if math.hypot(bot.x - anchor.x, bot.y - anchor.y) <= 7.0
                    ]
                    if len(cohort) > len(dense_green):
                        dense_green = cohort
                stalled = []
                far_goal = []
                for bot in dense_green:
                    history = position_history[int(bot.id)]
                    displacement = (
                        math.hypot(
                            bot.x - history[0][1][0],
                            bot.y - history[0][1][1],
                        )
                        if history
                        else 0.0
                    )
                    if displacement < 5.0:
                        stalled.append((int(bot.id), round(displacement, 2)))
                    runtime = director._runtime.get(int(bot.id))
                    intent = runtime.intent if runtime is not None else None
                    if intent is not None and intent.debug_goal is not None:
                        goal_distance = math.hypot(
                            float(intent.debug_goal[0]) - bot.x,
                            float(intent.debug_goal[1]) - bot.y,
                        )
                        if goal_distance >= 40.0:
                            far_goal.append((int(bot.id), round(goal_distance, 1)))
                # Do not require literal immobility here. The Mayan failure
                # consists of four bots shuffling around the same entrance;
                # that movement is precisely how the old single-bot stall
                # assertion missed it. Persistent density plus distant active
                # goals is the team-level invariant we care about.
                collapsed = len(dense_green) >= 4 and len(far_goal) >= 3
                if collapsed:
                    congestion_ticks += 1
                    if congestion_ticks > max_congestion_ticks:
                        max_congestion_ticks = congestion_ticks
                        congestion_details = {
                            "elapsed": round(elapsed, 2),
                            "cohort": [int(bot.id) for bot in dense_green],
                            "positions": {
                                int(bot.id): tuple(round(value, 2) for value in bot.position)
                                for bot in dense_green
                            },
                            "rolling_displacement": stalled,
                            "goal_distance": far_goal,
                            "roles": {
                                int(bot.id): (
                                    director._runtime[int(bot.id)].intent.debug_role
                                    if director._runtime.get(int(bot.id)) is not None
                                    and director._runtime[int(bot.id)].intent is not None
                                    else "idle"
                                )
                                for bot in dense_green
                            },
                        }
                else:
                    congestion_ticks = 0
            else:
                congestion_ticks = 0
            # Match the production ordering boundary: bot action suggestions
            # arrive before physics; their shared terrain mutations commit
            # only after that tick's native Player simulation.
            if not full_runtime:
                server.world_mutations.commit_ready()
                server.prefab_actions.tick()
            status = director.status()
            if (
                restart_requested
                and status.running
                and status.restarts >= 1
                and (original_pid is None or status.process_id != original_pid)
            ):
                restart_observed = True
            if progress_steps and step > 0 and step % progress_steps == 0:
                roles = Counter()
                cells: dict[tuple[int, int, int], list[int]] = {}
                interval_movement: dict[int, float] = {}
                for bot in director.bots:
                    runtime = director._runtime.get(bot.id)
                    intent = runtime.intent if runtime is not None else None
                    roles[
                        intent.debug_role if intent is not None else "idle"
                    ] += 1
                    cell = (
                        int(math.floor(bot.x)),
                        int(math.floor(bot.y)),
                        int(math.floor(bot.z)),
                    )
                    cells.setdefault(cell, []).append(int(bot.id))
                    interval_movement[int(bot.id)] = math.dist(
                        progress_positions.get(bot.id, bot.position),
                        bot.position,
                    )
                    progress_positions[bot.id] = bot.position
                overlaps = {
                    cell: ids
                    for cell, ids in cells.items()
                    if len(ids) > 1
                }
                breaches = []
                for bot in director.bots:
                    runtime = director._runtime.get(bot.id)
                    intent = runtime.intent if runtime is not None else None
                    if (
                        intent is None
                        or intent.movement.affordance.value != "breach"
                    ):
                        continue
                    breaches.append(
                        {
                            "id": int(bot.id),
                            "position": tuple(
                                round(value, 2) for value in bot.position
                            ),
                            "path": intent.debug_path,
                            "action": intent.action.kind.value,
                            "action_position": intent.action.position,
                            "feedback": (
                                runtime.feedback_action_kind,
                                runtime.feedback_action_accepted,
                                runtime.feedback_action_position,
                                runtime.feedback_action_frame,
                            ),
                            "pending": (
                                runtime.pending_action.kind.value
                                if runtime.pending_action is not None
                                else "none"
                            ),
                        }
                    )
                print(
                    "runtime_progress",
                    f"simulated_seconds={elapsed:.1f}",
                    f"restarts={status.restarts}",
                    "max_requested_stall_ticks="
                    f"{max(max_requested_stall_ticks.values(), default=0)}",
                    f"world_mutations={server.metrics.committed_world_mutations}",
                    f"roles={dict(roles)}",
                    f"overlaps={overlaps}",
                    f"breaches={breaches}",
                    "interval_movement="
                    f"{interval_movement if trace_state else min(interval_movement.values(), default=0.0):}",
                    flush=True,
                )
                if trace_state:
                    rows = []
                    for bot in director.bots:
                        runtime = director._runtime.get(bot.id)
                        intent = runtime.intent if runtime is not None else None
                        rows.append(
                            {
                                "id": int(bot.id),
                                "team": int(bot.team),
                                "position": tuple(round(value, 2) for value in bot.position),
                                "role": intent.debug_role if intent is not None else "idle",
                                "goal": intent.debug_goal if intent is not None else None,
                                "affordance": (
                                    intent.movement.affordance.value
                                    if intent is not None
                                    else "walk"
                                ),
                                "action": (
                                    intent.action.kind.value
                                    if intent is not None
                                    else "none"
                                ),
                                "action_position": (
                                    intent.action.position
                                    if intent is not None
                                    else None
                                ),
                            }
                        )
                    print("runtime_bot_state", rows, flush=True)
            next_tick_at += server.tick_interval
            if trace_stream is not None and step % 60 == 0:
                trace_stream.flush()
            await asyncio.sleep(max(0.0, next_tick_at - loop.time()))
        moved = {
            bot.id: math.dist(starts[bot.id], bot.position)
            for bot in director.bots
        }
        status = director.status()
        try:
            import psutil
        except ImportError:
            process_memory = None
        else:
            memory = psutil.Process().memory_info()
            process_memory = {"rss_bytes": memory.rss,
                              "peak_rss_bytes": getattr(memory, "peak_wset", memory.rss)}
        travel_review = None
        objective_review = None
        objective_reviewer = None
        if trace_stream is not None:
            trace_stream.flush()
            from scripts.bot_trace_review import review
            trace_rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            travel_review = review(trace_rows)
            if config.default_mode == "vip":
                from scripts.bot_objective_review import review as review_objectives
                objective_review = review_objectives(trace_rows)
                objective_reviewer = "scripts/bot_objective_review.py"
            elif config.default_mode in {"ctf", "cctf"}:
                from scripts.bot_ctf_review import review as review_objectives
                objective_review = review_objectives(trace_rows)
                objective_reviewer = "scripts/bot_ctf_review.py"
        report = {
            "map": config.default_map, "mode": config.default_mode,
            "seconds": seconds, "bots": len(director.bots),
            "worker": config.bots.worker, "full_runtime": full_runtime,
            "restarts": status.restarts, "restart_observed": restart_observed,
            "max_intent_gap_seconds": max_intent_gap, "respawns": respawns,
            "deaths": {bot.id: bot.deaths for bot in director.bots},
            "water_started": water_started,
            "water_exit_seconds": water_exit_seconds,
            "max_requested_stall_seconds": {
                key: ticks * server.tick_interval for key, ticks in max_requested_stall_ticks.items()
            },
            "world_mutations": server.metrics.committed_world_mutations,
            "behavior_version": config.bots.behavior_version,
            "config_path": str(config_path) if config_path else None,
            "trace_path": str(trace_path) if trace_path else None,
            "seed": seed,
            "effective_bot_seed": config.bots.seed,
            "source_sha256": initial_source_hashes,
            "source_unchanged_during_run": initial_source_hashes == source_hashes(),
            "smoke_runner_sha256": runner_sha256,
            "smoke_runner_unchanged_during_run": runner_sha256 == hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest(),
            "trace_reviewer_sha256": hashlib.sha256(
                (ROOT / "scripts/bot_trace_review.py").read_bytes()).hexdigest() if trace_stream else None,
            "travel_review": travel_review,
            "objective_review": objective_review,
            "objective_reviewer_sha256": hashlib.sha256(
                (ROOT / objective_reviewer).read_bytes()).hexdigest() if objective_reviewer else None,
            "runtime_metrics": server.metrics.snapshot(),
            "planning_metrics": getattr(director.supervisor, "planning_metrics", lambda: {})(),
            "behavior_metrics": getattr(director.supervisor, "behavior_metrics", lambda: {})(),
            "role_samples": dict(role_samples), "action_results": dict(action_results),
            "tick_ms": {"p95": sorted(tick_costs)[min(len(tick_costs) - 1, int(len(tick_costs) * .95))],
                        "p99": sorted(tick_costs)[min(len(tick_costs) - 1, int(len(tick_costs) * .99))],
                        "max": max(tick_costs, default=0)},
            "host_process_cpu_seconds": time.process_time() - cpu_started,
            "host_process_memory": process_memory,
            "elapsed_wall_seconds": time.perf_counter() - wall_started,
        }
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if detect_travel_loops and travel_review["suspected_travel_loops"]:
            raise RuntimeError(f"repeated native travel loops: {travel_review['suspected_travel_loops']}")
        if detect_objective_abandonment:
            if not objective_review["objective_state_recorded"]:
                raise RuntimeError("objective acceptance lacks authoritative objective telemetry")
            if objective_review["findings"]:
                raise RuntimeError(f"native objective abandonment: {objective_review['findings']}")
        if max(max_intent_gap.values(), default=0.0) >= 5.0:
            raise RuntimeError(f"bots lost fresh intents for >=5s: {max_intent_gap}")
        if not status.running:
            raise RuntimeError(f"worker unavailable after smoke: {status}")
        if restart_worker_at is not None and not restart_observed:
            raise RuntimeError(f"worker restart not observed: {status}")
        if not any(distance > 0.1 for distance in moved.values()):
            raise RuntimeError(f"bot physics did not move: {moved}")
        excessive_stalls = {
            bot_id: ticks
            for bot_id, ticks in max_requested_stall_ticks.items()
            if ticks >= int(5.0 / server.tick_interval)
        }
        if excessive_stalls:
            details = {}
            for bot_id, ticks in excessive_stalls.items():
                historical = max_requested_stall_details.get(bot_id)
                if historical is not None:
                    details[bot_id] = historical
                    continue
                bot = next(
                    (candidate for candidate in director.bots if candidate.id == bot_id),
                    None,
                )
                runtime = director._runtime.get(bot_id)
                intent = runtime.intent if runtime is not None else None
                details[bot_id] = {
                    "ticks": ticks,
                    "position": bot.position if bot is not None else None,
                    "velocity": (
                        (
                            float(getattr(bot, "vx", 0.0)),
                            float(getattr(bot, "vy", 0.0)),
                            float(getattr(bot, "vz", 0.0)),
                        )
                        if bot is not None
                        else None
                    ),
                    "role": intent.debug_role if intent is not None else None,
                    "goal": intent.debug_goal if intent is not None else None,
                    "affordance": (
                        intent.movement.affordance.value
                        if intent is not None
                        else None
                    ),
                    "direction": (
                        intent.movement.direction
                        if intent is not None
                        else None
                    ),
                }
            raise RuntimeError(
                f"requested bot movement stalled for >=5s: {details}"
            )
        congestion_limit = max(1, int(8.0 / server.tick_interval))
        if detect_team_congestion and max_congestion_ticks >= congestion_limit:
            raise RuntimeError(
                "green team persistently converged in one excavation lane: "
                f"seed={seed} ticks={max_congestion_ticks} "
                f"details={congestion_details}"
            )
        water_remaining = {
            bot.id: bot.position
            for bot in director.bots
            if bot.id in water_started
            and bot.id not in water_exit_seconds
        }
        if water_remaining:
            raise RuntimeError(
                f"fault-injected water bots did not reach land: {water_remaining}"
            )
        if server.world_mutations.pending_count:
            raise RuntimeError(
                f"bot world mutations did not commit: "
                f"{server.world_mutations.pending_count} pending"
            )
        if server.metrics.expired_world_mutations:
            raise RuntimeError(
                f"bot world mutations expired: "
                f"{server.metrics.expired_world_mutations}"
            )
        bot_metrics = {
            key: value
            for key, value in server.metrics.snapshot().items()
            if key.startswith("subsystem_bots_")
        }
        print(
            "runtime_ok",
            f"mode={config.default_mode}",
            f"map={config.default_map}",
            f"bots={len(director.bots)}",
            f"worker={status.process_id or 'thread'}",
            f"restarts={status.restarts}",
            f"world_mutations={server.metrics.committed_world_mutations}",
            f"moved={moved}",
            f"water_started={water_started}",
            f"water_exit_seconds={water_exit_seconds}",
            f"max_requested_stall_ticks={max_requested_stall_ticks}",
            f"max_congestion_ticks={max_congestion_ticks}",
            f"bot_metrics={bot_metrics}",
            f"entities={[(entity.type, entity.player_id) for entity in server.entity_registry.all()]}",
        )
    finally:
        if trace_stream is not None:
            trace_stream.close()
        await director.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--bots", type=int, default=2)
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--map", default=None)
    parser.add_argument("--worker", choices=("thread", "process"), default=None)
    parser.add_argument("--behavior", choices=("classic", "cooperative"), default=None)
    parser.add_argument("--full-runtime", action="store_true",
                        help="include mode events, projectiles, respawns and entities")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None,
                        help="use a specific match configuration instead of repository defaults")
    parser.add_argument("--trace-jsonl", type=Path, default=None,
                        help="record native positions, aim, task and action ownership at 10 Hz")
    parser.add_argument("--detect-travel-loops", action="store_true",
                        help="fail suspected 20/30-second travel loops (requires --trace-jsonl)")
    parser.add_argument("--detect-objective-abandonment", action="store_true",
                        help="fail sustained VIP/CTF objective abandonment (requires --trace-jsonl)")
    parser.add_argument(
        "--water-spawn-bots",
        type=int,
        default=0,
        help="move N bots from production spawns to their nearest real water column",
    )
    parser.add_argument(
        "--restart-worker-at",
        type=float,
        default=None,
        help="terminate this match's owned AI child after N seconds",
    )
    parser.add_argument(
        "--progress-every",
        type=float,
        default=0.0,
        help="print a flushed progress line every N simulated seconds",
    )
    parser.add_argument(
        "--trace-state",
        action="store_true",
        help="include per-bot goals, actions, and positions in progress output",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed authored spawn shuffling and bot profiles for replay",
    )
    parser.add_argument(
        "--detect-team-congestion",
        action="store_true",
        help="fail if green bots persistently collapse into one excavation lane",
    )
    args = parser.parse_args()
    asyncio.run(
        _run(
            seconds=args.seconds,
            bot_count=args.bots,
            mode_name=args.mode,
            map_name=args.map,
            water_spawn_bots=args.water_spawn_bots,
            restart_worker_at=args.restart_worker_at,
            progress_every=args.progress_every,
            trace_state=args.trace_state,
            seed=args.seed,
            detect_team_congestion=args.detect_team_congestion,
            worker_backend=args.worker,
            full_runtime=args.full_runtime,
            report_path=args.json,
            behavior_version=args.behavior,
            config_path=args.config,
            trace_path=args.trace_jsonl,
            detect_travel_loops=args.detect_travel_loops,
            detect_objective_abandonment=args.detect_objective_abandonment,
        )
    )
