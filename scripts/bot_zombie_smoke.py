"""Headless acceptance for Zombie chase, claw combat, and retail replication.

This launches the real AI worker on an authored VXL.  One idle, server-owned
human body is placed ten blocks from a Zombie bot.  Passing requires the bot
to close the gap, expose its native Zombie hand in WorldUpdate, publish a
primary swing, and damage the survivor through the ordinary CombatSystem.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import math
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C
from modes import get_mode_class
from modes.zombie import ZombiePhase
from server.bot_ai import BotDirector
from server.bot_ai.messages import WorldDelta
from server.bot_ai.worker import BotBrain, WorkerVoxelWorld
from server.class_selection import normalize_class_selection
from server.config import load_config
from server.game_constants import DEFAULT_WEAPON_TOOL, TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import CreatePlayer, SetHP, WorldUpdate


class _RetailObserver:
    """Minimal settled connection which records unframed gameplay packets."""

    def __init__(self, server: BattleSpadesServer) -> None:
        self.server = server
        self.in_game = True
        self.player = None
        self.sent: list[bytes] = []

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        self.sent.append(bytes(data))


class _InlineSmokeSupervisor:
    """Smoke-only worker adapter for sandboxes which forbid Windows pipes.

    Production never selects this adapter.  It preserves the exact immutable
    perception/intent boundary while evaluating ``BotBrain`` synchronously so
    the rest of the real director, motor, physics, gateway, combat, and packet
    pipeline can still be exercised in a restricted test runner.
    """

    def __init__(self) -> None:
        self.world = WorkerVoxelWorld()
        self.brain = BotBrain(self.world, seed=0)
        self.intents = deque()

    @property
    def snapshot_required(self) -> bool:
        return False

    def start(self, snapshot) -> None:
        self.world.load(snapshot)

    def close(self, timeout: float = 0.0) -> None:
        self.intents.clear()

    def publish_map(self, snapshot) -> None:
        self.world.load(snapshot)

    def publish_world_change(self, change, *, map_epoch, topology_version) -> None:
        self.world.apply(
            WorldDelta(
                map_epoch=int(map_epoch),
                topology_version=int(topology_version),
                changed_cells=(change,),
            )
        )

    def submit_frame(self, frame) -> bool:
        intent = self.brain.decide(frame)
        if intent is not None:
            self.intents.append(intent)
        return True

    def drain_intents(self, limit: int = 12):
        return [
            self.intents.popleft()
            for _ in range(min(max(0, int(limit)), len(self.intents)))
        ]

    def status(self):
        return SimpleNamespace(
            running=True,
            process_id=None,
            restarts=0,
            queued_frames=0,
            queued_intents=len(self.intents),
            pending_terrain_cells=0,
            dropped_frames=0,
            dropped_intents=0,
            snapshot_required=False,
        )


def _clear_lane(world) -> tuple[tuple[float, float, float], ...]:
    """Find two level stand points with a clear ten-block eye ray."""

    for y in range(48, 464, 8):
        for x in range(48, 452, 8):
            try:
                first = world.dry_ground_anchor(x, y, search=0)
                second = world.dry_ground_anchor(x + 10, y, search=0)
            except (RuntimeError, TypeError, ValueError):
                continue
            if abs(float(first[2]) - float(second[2])) > 0.75:
                continue
            direction = tuple(
                float(second[index]) - float(first[index]) for index in range(3)
            )
            length = math.sqrt(sum(value * value for value in direction))
            if length <= 1e-6:
                continue
            unit = tuple(value / length for value in direction)
            if world.raycast(*first, *unit, length - 0.75) is None:
                return tuple(float(value) for value in first), tuple(
                    float(value) for value in second
                )
    raise RuntimeError("authored map has no clear ten-block Zombie lane")


def _spawn_idle_survivor(
    server: BattleSpadesServer,
    connection: _RetailObserver,
    position: tuple[float, float, float],
) -> Player:
    """Create one real Player which supplies no input and remains a fair target."""

    player_id = server.get_next_player_id()
    if player_id < 0:
        raise RuntimeError("no player id available for survivor fixture")
    player = Player(
        player_id,
        "SmokeSurvivor",
        TEAM1,
        DEFAULT_WEAPON_TOOL,
        connection,
    )
    connection.player = player
    player.apply_class_selection(
        normalize_class_selection(int(C.CLASS_SOLDIER))
    )
    player.spawn(*position)
    server.players[player.id] = player
    server.teams[TEAM1].add_player(player)
    server.connections[object()] = connection
    server._broadcast_create_player(player, position)
    return player


async def _run(seconds: float = 15.0, *, inline_worker: bool = False) -> None:
    config = load_config(ROOT / "config.toml")
    config.default_mode = "zom"
    config.respawn_time = 30.0
    config.bots.population_mode = "admin"
    config.bots.max_bots = 1
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("Zombie smoke map did not load")
    mode_class = get_mode_class("zom")
    if mode_class is None:
        raise RuntimeError("Zombie mode unavailable")
    server.mode = mode_class(server)
    await server.mode.on_mode_start()

    survivor_position, zombie_position = _clear_lane(server.world_manager)
    observer = _RetailObserver(server)
    survivor = _spawn_idle_survivor(server, observer, survivor_position)
    await server.mode.on_player_join(survivor)
    server.mode.phase = ZombiePhase.ACTIVE

    director = BotDirector(
        server,
        supervisor=_InlineSmokeSupervisor() if inline_worker else None,
    )
    server.bots = director
    await director.start(initial_count=0)
    try:
        zombie = await director.add_bot(
            team=TEAM2,
            name="SmokeZombie",
            difficulty="hard",
        )
        if zombie is None:
            raise RuntimeError("failed to create Zombie bot")
        zombie.set_position(*zombie_position)
        zombie.set_orientation_vector(-1.0, 0.0, 0.0)
        director._runtime[zombie.id].motor.yaw = math.pi

        start_distance = math.dist(zombie.position, survivor.position)
        minimum_distance = start_distance
        initial_health = survivor.health
        saw_hand = False
        saw_swing = False
        observed_rows = []
        cursor = 0
        hit_at: float | None = None
        deadline = time.monotonic() + max(4.0, float(seconds))
        while time.monotonic() < deadline:
            server.loop_count += 1
            await server.simulation_runtime.step()
            server.replication.broadcast_world_updates()
            if zombie.alive and survivor.alive:
                minimum_distance = min(
                    minimum_distance,
                    math.dist(zombie.position, survivor.position),
                )
            for data in observer.sent[cursor:]:
                if not data or data[0] != WorldUpdate.id:
                    continue
                update = WorldUpdate(ByteReader(data[1:]))
                row = update.player_updates.get(zombie.id)
                if row is None:
                    continue
                observed_rows.append(row)
                saw_hand |= int(row[9]) == int(C.ZOMBIEHAND_TOOL)
                saw_swing |= bool(int(row[7]) & 0x01)
            cursor = len(observer.sent)
            now = time.monotonic()
            if survivor.health < initial_health and hit_at is None:
                hit_at = now
            # The hit can land on the 60 Hz tick between two 30 Hz snapshots.
            # Keep one bounded replication window so the primary pulse is
            # actually observed instead of stopping on the damage frame.
            if hit_at is not None and saw_hand and saw_swing:
                break
            if hit_at is not None and now - hit_at >= 0.25:
                break
            await asyncio.sleep(server.tick_interval)

        create_packets = [
            CreatePlayer(ByteReader(data[1:]))
            for data in observer.sent
            if data and data[0] == CreatePlayer.id
        ]
        zombie_create = next(
            (packet for packet in create_packets if int(packet.player_id) == zombie.id),
            None,
        )
        hp_packets = [
            SetHP(ByteReader(data[1:]))
            for data in observer.sent
            if data and data[0] == SetHP.id
        ]
        if zombie_create is None:
            raise RuntimeError("observer never received Zombie CreatePlayer")
        if int(zombie_create.class_id) != int(zombie.class_id):
            raise RuntimeError("Zombie wire class differs from authoritative class")
        if int(C.ZOMBIEHAND_TOOL) not in zombie_create.loadout:
            raise RuntimeError("Zombie hand absent from CreatePlayer loadout")
        if int(C.ZOMBIE_PREFAB_TOOL) not in zombie_create.loadout:
            raise RuntimeError("Zombie prefab tool absent from CreatePlayer loadout")
        if minimum_distance >= start_distance - 1.0:
            raise RuntimeError(
                f"Zombie did not pursue survivor: {start_distance=:.2f} "
                f"{minimum_distance=:.2f}"
            )
        if survivor.health >= initial_health:
            raise RuntimeError(
                f"Zombie reached no authoritative hit: {minimum_distance=:.2f}"
            )
        if not saw_hand or not saw_swing:
            raise RuntimeError(
                f"Zombie action missing on retail wire: {saw_hand=} {saw_swing=} "
                f"rows={len(observed_rows)} samples={observed_rows[-3:]}"
            )
        if not hp_packets or hp_packets[-1].hp != survivor.health:
            raise RuntimeError("survivor HP feedback missing or stale")

        print(
            "bot_zombie_ok",
            f"class={zombie.class_id}",
            f"distance={start_distance:.2f}->{minimum_distance:.2f}",
            f"survivor_hp={initial_health}->{survivor.health}",
            f"hand_visible={saw_hand}",
            f"swing_visible={saw_swing}",
            f"worker_pid={director.status().process_id}",
        )
    finally:
        await director.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run authoritative Zombie chase/combat acceptance."
    )
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument(
        "--inline-worker",
        action="store_true",
        help="use only in a sandbox which forbids multiprocessing pipes",
    )
    args = parser.parse_args()
    asyncio.run(_run(seconds=args.seconds, inline_worker=args.inline_worker))
