"""Headless end-to-end smoke for bot lifecycle, worker, and native physics."""

from __future__ import annotations

import asyncio
import argparse
import math
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modes import get_mode_class
from server.bot_ai import BotDirector
from server.config import load_config
from server.main import BattleSpadesServer


async def _run(
    *,
    seconds: float = 4.0,
    bot_count: int = 2,
    mode_name: str = "tdm",
    map_name: str | None = None,
    water_spawn_bots: int = 0,
    restart_worker_at: float | None = None,
) -> None:
    config = load_config(ROOT / "config.toml")
    config.default_mode = str(mode_name).lower()
    if map_name is not None:
        config.default_map = str(map_name)
    config.bots.population_mode = "admin"
    config.bots.max_bots = max(1, int(bot_count))
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
    requested_stall_ticks = {bot.id: 0 for bot in director.bots}
    max_requested_stall_ticks = {bot.id: 0 for bot in director.bots}
    water_exit_seconds: dict[int, float] = {}
    worker_deadline = asyncio.get_running_loop().time() + 10.0
    original_pid = director.status().process_id
    while original_pid is None and asyncio.get_running_loop().time() < worker_deadline:
        await asyncio.sleep(0.02)
        original_pid = director.status().process_id
    if original_pid is None:
        raise RuntimeError("worker did not publish a process id")
    restart_requested = False
    restart_observed = False
    try:
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
                max_requested_stall_ticks[bot.id] = max(
                    max_requested_stall_ticks[bot.id],
                    requested_stall_ticks[bot.id],
                )
                previous_positions[bot.id] = bot.position
                if (
                    bot.id in water_started
                    and bot.id not in water_exit_seconds
                    and not server.world_manager.is_water_column(
                        int(bot.x), int(bot.y)
                    )
                ):
                    water_exit_seconds[bot.id] = elapsed
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
            await asyncio.sleep(server.tick_interval)
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
            raise RuntimeError(
                f"requested bot movement stalled for >=5s: {excessive_stalls}"
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
        print(
            "runtime_ok",
            f"mode={config.default_mode}",
            f"map={config.default_map}",
            f"bots={len(director.bots)}",
            f"pid={status.process_id}",
            f"restarts={status.restarts}",
            f"world_mutations={server.metrics.committed_world_mutations}",
            f"moved={moved}",
            f"water_started={water_started}",
            f"water_exit_seconds={water_exit_seconds}",
            f"max_requested_stall_ticks={max_requested_stall_ticks}",
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
    args = parser.parse_args()
    asyncio.run(
        _run(
            seconds=args.seconds,
            bot_count=args.bots,
            mode_name=args.mode,
            map_name=args.map,
            water_spawn_bots=args.water_spawn_bots,
            restart_worker_at=args.restart_worker_at,
        )
    )
