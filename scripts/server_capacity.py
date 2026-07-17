"""Run the real server loop under a 50-player simulation/network workload."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402
from server.logging_runtime import configure_logging  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402
from server.util import lzf_compress  # noqa: E402


class SyntheticPeer:
    def disconnect(self):
        return None


class SyntheticConnection:
    def __init__(self, player_id: int, stamp: int):
        self.player = SimpleNamespace(
            id=player_id,
            last_applied_input_loop=stamp,
            wu_ack_loop=stamp,
            is_block_tool=lambda: False,
        )
        self.in_game = True
        self.packets = 0
        self.bytes = 0

    def send(self, data: bytes, reliable=True, prefix=0x30):
        import enet

        wire = bytes([prefix]) + lzf_compress(data)
        flags = enet.PACKET_FLAG_RELIABLE if reliable else 0
        enet.Packet(wire, flags)
        self.packets += 1
        self.bytes += len(wire)


async def run_capacity(
    players: int,
    seconds: float,
    port: int,
    *,
    mode: str = "tdm",
    map_name: str | None = None,
) -> dict:
    import psutil

    config = load_config(ROOT / "config.toml")
    config.port = port
    config.max_players = max(players, 50)
    config.max_connections = max(config.max_players + 8, 64)
    config.bot_count = 0
    config.bots.configured = True
    config.bots.enabled = True
    config.bots.population_mode = "fixed"
    config.bots.fill_target = players
    config.bots.max_bots = players
    config.bots.reserve_human_slots = 0
    config.default_mode = str(mode).strip().lower()
    if map_name:
        config.default_map = str(map_name).strip()
    config.debug_parity = False
    config.debug_selfrow = False
    config.movement_debug_capture = False
    config.packet_trace = False
    config.log_level = "WARNING"
    config.log_console = False

    logging_runtime = configure_logging(config, ROOT / "logs" / "capacity")
    server = BattleSpadesServer(config)
    server_task = asyncio.create_task(server.start())
    try:
        deadline = time.monotonic() + 60.0
        while not server.running:
            if server_task.done():
                await server_task
            if time.monotonic() >= deadline:
                raise TimeoutError("server did not become ready")
            await asyncio.sleep(0.05)

        spawned = len(server.players)
        worker_deadline = time.monotonic() + 10.0
        while (
            server.bots is not None
            and not server.bots.status().running
            and time.monotonic() < worker_deadline
        ):
            await asyncio.sleep(0.02)
        worker_status = server.bots.status() if server.bots is not None else None
        worker_process = (
            psutil.Process(worker_status.process_id)
            if worker_status is not None and worker_status.process_id is not None
            else None
        )
        worker_cpu_start = (
            sum(worker_process.cpu_times()[:2]) if worker_process is not None else 0.0
        )
        worker_peak_rss = (
            worker_process.memory_info().rss if worker_process is not None else 0
        )
        stamp = server.loop_count
        synthetic = []
        for player_id in range(players):
            connection = SyntheticConnection(player_id, stamp)
            synthetic.append(connection)
            server.connections[SyntheticPeer()] = connection

        start_loop = server.loop_count
        start_cpu = time.process_time()
        start_wall = time.perf_counter()
        process = psutil.Process(os.getpid())
        start_rss = process.memory_info().rss
        peak_rss = start_rss
        peak_pending_packets = len(server._pending_ingame_packets)
        peak_active_block_fires = len(server.fire_controller.block_fires)
        deadline = start_wall + seconds
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0.0:
                break
            await asyncio.sleep(min(1.0, remaining))
            peak_rss = max(peak_rss, process.memory_info().rss)
            peak_pending_packets = max(
                peak_pending_packets, len(server._pending_ingame_packets)
            )
            peak_active_block_fires = max(
                peak_active_block_fires,
                len(server.fire_controller.block_fires),
            )
            if worker_process is not None and worker_process.is_running():
                worker_peak_rss = max(
                    worker_peak_rss, worker_process.memory_info().rss
                )
        elapsed = time.perf_counter() - start_wall
        cpu_seconds = time.process_time() - start_cpu
        end_rss = process.memory_info().rss
        ticks = server.loop_count - start_loop
        metrics = server.metrics.snapshot()
        total_packets = sum(connection.packets for connection in synthetic)
        total_bytes = sum(connection.bytes for connection in synthetic)
        worker_cpu_seconds = 0.0
        worker_end_rss = 0
        if worker_process is not None and worker_process.is_running():
            worker_cpu_seconds = max(
                0.0, sum(worker_process.cpu_times()[:2]) - worker_cpu_start
            )
            worker_end_rss = worker_process.memory_info().rss

        result = {
            "mode": config.default_mode,
            "map": config.default_map,
            "requested_players": players,
            "spawned_players": spawned,
            "seconds": round(elapsed, 3),
            "ticks": ticks,
            "achieved_tick_hz": round(ticks / elapsed, 3),
            "process_cpu_cores": round(cpu_seconds / elapsed, 3),
            "worker_cpu_cores": round(worker_cpu_seconds / elapsed, 3),
            "worker_memory_end_mib": round(worker_end_rss / (1024 * 1024), 3),
            "worker_memory_peak_mib": round(worker_peak_rss / (1024 * 1024), 3),
            "outbound_packets": total_packets,
            "memory_start_mib": round(start_rss / (1024 * 1024), 3),
            "memory_end_mib": round(end_rss / (1024 * 1024), 3),
            "memory_peak_mib": round(peak_rss / (1024 * 1024), 3),
            "memory_growth_mib": round(
                (end_rss - start_rss) / (1024 * 1024), 3
            ),
            "peak_pending_packets": peak_pending_packets,
            "peak_active_block_fires": peak_active_block_fires,
            "active_block_fires_end": len(server.fire_controller.block_fires),
            "team_scores": {
                str(team_id): int(team.score)
                for team_id, team in server.teams.items()
            },
            "player_captures": sum(
                int(getattr(player, "captures", 0))
                for player in server.players.values()
            ),
            "outbound_mib_per_second": round(
                total_bytes / elapsed / (1024 * 1024), 3
            ),
            **metrics,
            "logging_dropped_records": logging_runtime.dropped_records,
        }
        failures = []
        if spawned < players:
            failures.append("not all players spawned")
        if result["achieved_tick_hz"] < 58.0:
            failures.append("tick rate below 58 Hz")
        if result["tick_p99_ms"] > 12.0:
            failures.append("tick p99 above 12 ms")
        if result.get("subsystem_bots_p99_ms", 0.0) > config.bots.main_thread_budget_ms:
            failures.append("bot main-thread p99 above configured budget")
        if result["worker_cpu_cores"] > 1.0:
            failures.append("AI worker above one average CPU core")
        if result["worker_memory_peak_mib"] > 256.0:
            failures.append("AI worker above 256 MiB")
        if result["dropped_ingame_packets"]:
            failures.append("gameplay packet drops")
        if result["dropped_mode_events"]:
            failures.append("mode event drops")
        if result["rejected_world_mutations"]:
            failures.append("world mutation rejection")
        if result["expired_world_mutations"]:
            failures.append("world mutation expiry")
        if result["map_mutation_overflows"]:
            failures.append("map mutation journal overflow")
        if result["dropped_terrain_repairs"]:
            failures.append("terrain repair drops")
        if result["failed_terrain_repair_sends"]:
            failures.append("terrain repair send failures")
        if result["skipped_entity_ticks"]:
            failures.append("entity tick deferrals")
        if result["memory_growth_mib"] > 128.0:
            failures.append("memory growth above 128 MiB")
        if peak_pending_packets >= config.max_pending_packets:
            failures.append("gameplay packet queue saturated")
        result["failures"] = failures
        result["passed"] = not failures
        return result
    finally:
        server.connections.clear()
        await server.stop()
        await server_task
        logging_runtime.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--players", type=int, default=50)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--port", type=int, default=27016)
    parser.add_argument("--mode", default="tdm")
    parser.add_argument("--map", dest="map_name", default=None)
    args = parser.parse_args(argv)
    result = asyncio.run(run_capacity(
        args.players,
        args.seconds,
        args.port,
        mode=args.mode,
        map_name=args.map_name,
    ))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
