"""Headless end-to-end smoke for bot lifecycle, worker, and native physics."""

from __future__ import annotations

import asyncio
import argparse
from collections import Counter
import math
import os
import random
import signal
import sys
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
) -> None:
    if seed is not None:
        # WorldManager deliberately uses the module RNG when shuffling authored
        # spawn candidates. Seed both that path and the bot profile factory so
        # a field report can be reduced to one replayable match.
        random.seed(int(seed))
    config = load_config(ROOT / "config.toml")
    config.default_mode = str(mode_name).lower()
    if map_name is not None:
        config.default_map = str(map_name)
    config.bots.population_mode = "admin"
    config.bots.max_bots = max(1, int(bot_count))
    if seed is not None:
        config.bots.seed = int(seed)
    if restart_worker_at is not None:
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
    try:
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
                    raise RuntimeError("worker has no process id")
                # This PID came from our director; never enumerate or kill an
                # unrelated Python process during the recovery acceptance.
                os.kill(original_pid, signal.SIGTERM)
                restart_requested = True
            server.loop_count += 1
            await director.update(server.tick_interval)
            director.drain_actions(limit=1)
            await server.simulation_runtime._simulate_players()
            now = asyncio.get_running_loop().time()
            for bot in director.bots:
                runtime = director._runtime.get(bot.id)
                intent = runtime.intent if runtime is not None else None
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
                history = position_history[bot.id]
                history.append((elapsed, bot.position))
                cutoff = elapsed - 8.0
                while len(history) > 1 and history[1][0] <= cutoff:
                    del history[0]
                if (
                    bot.id in water_started
                    and bot.id not in water_exit_seconds
                    and not server.world_manager.is_water_column(
                        int(bot.x), int(bot.y)
                    )
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
            server.world_mutations.commit_ready()
            server.prefab_actions.tick()
            status = director.status()
            if (
                restart_requested
                and status.running
                and status.restarts >= 1
                and status.process_id is not None
                and status.process_id != original_pid
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
            await asyncio.sleep(max(0.0, next_tick_at - loop.time()))
        moved = {
            bot.id: math.dist(starts[bot.id], bot.position)
            for bot in director.bots
        }
        status = director.status()
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
            and server.world_manager.is_water_column(int(bot.x), int(bot.y))
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
        await director.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--bots", type=int, default=2)
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--map", default=None)
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
        )
    )
