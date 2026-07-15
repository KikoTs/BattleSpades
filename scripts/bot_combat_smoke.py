"""Headless retail-wire acceptance for bot player parity.

The test starts the real worker process, spawns opposing peerless Players on a
flat authoritative VXL, and observes them through the same packet broadcast
boundary as a retail client.  Passing requires visible equipped tools, normal
hitscan damage/death, and a lifecycle respawn; movement alone is insufficient.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C
from modes import get_mode_class
from server.bot_ai import BotDirector
from server.config import load_config
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import (
    CreatePlayer,
    KillAction,
    ShootFeedbackPacket,
    WeaponReload,
    WorldUpdate,
)


class _Observer:
    """In-game packet sink representing an already settled retail observer."""

    def __init__(self) -> None:
        self.in_game = True
        self.player = None
        self.sent: list[bytes] = []

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        self.sent.append(bytes(data))


def _clear_combat_lane(world) -> tuple[tuple[float, float, float], ...]:
    """Find two level stand points with an unobstructed ten-block eye ray."""

    for y in range(48, 464, 8):
        for x in range(48, 452, 8):
            try:
                first = world.dry_ground_anchor(x, y, search=0)
                second = world.dry_ground_anchor(x + 10, y, search=0)
            except (RuntimeError, TypeError, ValueError):
                continue
            if abs(float(first[2]) - float(second[2])) > 0.75:
                continue
            direction = (
                float(second[0]) - float(first[0]),
                float(second[1]) - float(first[1]),
                float(second[2]) - float(first[2]),
            )
            length = sum(value * value for value in direction) ** 0.5
            if length <= 1e-6:
                continue
            unit = tuple(value / length for value in direction)
            if world.raycast(*first, *unit, length - 0.75) is None:
                return tuple(float(value) for value in first), tuple(
                    float(value) for value in second
                )
    raise RuntimeError("authored map has no clear ten-block combat lane")


async def _run(seconds: float = 20.0) -> None:
    config = load_config(ROOT / "config.toml")
    config.default_mode = "tdm"
    config.respawn_time = 0.25
    config.bots.population_mode = "admin"
    config.bots.max_bots = 2
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("combat smoke map did not load")
    mode_class = get_mode_class("tdm")
    if mode_class is None:
        raise RuntimeError("TDM mode unavailable")
    server.mode = mode_class(server)
    await server.mode.on_mode_start()

    observer = _Observer()
    server.connections[object()] = observer
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)
    try:
        first = await director.add_bot(
            team=TEAM1,
            name="ParityOne",
            class_id=int(C.CLASS_CLASSIC_SOLDIER),
        )
        second = await director.add_bot(
            team=TEAM2,
            name="ParityTwo",
            class_id=int(C.CLASS_CLASSIC_SOLDIER),
        )
        if first is None or second is None:
            raise RuntimeError("failed to create opposing bots")

        # Keep the combat fixture deterministic and unobstructed while using
        # the same authored VXL bytes loaded by the production worker.
        first_position, second_position = _clear_combat_lane(
            server.world_manager
        )
        first.set_position(*first_position)
        second.set_position(*second_position)
        first.set_orientation_vector(1.0, 0.0, 0.0)
        second.set_orientation_vector(-1.0, 0.0, 0.0)
        director._runtime[first.id].motor.yaw = 0.0
        director._runtime[second.id].motor.yaw = 3.141592653589793

        initial_lives = {
            first.id: first.replication_generation,
            second.id: second.replication_generation,
        }
        initial_tools = {first.id: int(first.tool), second.id: int(second.tool)}
        # Start both rifles one shot from empty. This turns the smoke into a
        # regression for the historical bot stall: an empty clip used to pick
        # a grenade before RELOAD, selecting that tool cancelled the reload,
        # and a bot with no reserve then dry-fired forever.
        for bot in (first, second):
            bot.ammo_clip = 1
            bot.ammo_reserve = 12
            bot.reloading = False
            bot.reload_end_time = 0.0

        deadline = time.monotonic() + max(2.0, float(seconds))
        saw_death = False
        saw_respawn = False
        reload_started: set[int] = set()
        reload_completed: set[int] = set()
        post_reload_shots: set[int] = set()
        observer_cursor = 0
        while time.monotonic() < deadline:
            server.loop_count += 1
            await server.simulation_runtime.step()
            server.replication.broadcast_world_updates()

            for data in observer.sent[observer_cursor:]:
                if not data:
                    continue
                if data[0] == WeaponReload.id:
                    packet = WeaponReload(ByteReader(data[1:]))
                    if packet.is_done:
                        reload_completed.add(int(packet.player_id))
                    else:
                        reload_started.add(int(packet.player_id))
                elif data[0] == ShootFeedbackPacket.id:
                    packet = ShootFeedbackPacket(ByteReader(data[1:]))
                    shooter_id = int(packet.shooter_id)
                    if shooter_id in reload_completed:
                        post_reload_shots.add(shooter_id)
            observer_cursor = len(observer.sent)

            if first.deaths or second.deaths:
                saw_death = True
            if saw_death and any(
                player.replication_generation > initial_lives[player.id]
                for player in (first, second)
            ):
                saw_respawn = True
            if saw_respawn and post_reload_shots:
                break
            await asyncio.sleep(server.tick_interval)

        updates = [data for data in observer.sent if data and data[0] == WorldUpdate.id]
        if not updates:
            raise RuntimeError("observer received no WorldUpdate")
        # A combat death intentionally removes that player from the snapshot
        # until RoundLifecycle respawns it.  Inspecting only the final packet is
        # therefore timing-dependent; accumulate each concrete row observed
        # during the run and require the visible initial weapon state.
        observed_rows = {first.id: [], second.id: []}
        for update in updates:
            parsed = WorldUpdate(ByteReader(update[1:]))
            for bot_id in observed_rows:
                row = parsed.player_updates.get(bot_id)
                if row is not None:
                    observed_rows[bot_id].append(row)
        for bot in (first, second):
            rows = observed_rows[bot.id]
            if not rows:
                raise RuntimeError(
                    f"bot {bot.id} absent from every observer WorldUpdate"
                )
            if any(not (int(row[7]) & 0x10) for row in rows):
                raise RuntimeError(f"bot {bot.id} weapon display bit missing")
            if not any(
                int(row[9]) == initial_tools[bot.id]
                for row in rows
            ):
                raise RuntimeError(
                    f"bot {bot.id} initial tool {initial_tools[bot.id]} "
                    "never appeared on the wire"
                )

        packet_ids = [data[0] for data in observer.sent if data]
        if ShootFeedbackPacket.id not in packet_ids:
            raise RuntimeError(
                "bots never emitted native remote-shot feedback"
            )
        if not reload_started or not reload_completed:
            raise RuntimeError(
                "low-ammo bots did not complete a reload sequence "
                f"started={sorted(reload_started)} "
                f"completed={sorted(reload_completed)}"
            )
        if not post_reload_shots:
            raise RuntimeError(
                "bots stopped fighting after reload instead of firing again"
            )
        if not saw_death or KillAction.id not in packet_ids:
            raise RuntimeError(
                f"bots never completed authoritative combat deaths="
                f"{(first.deaths, second.deaths)}"
            )
        if not saw_respawn:
            raise RuntimeError("dead bot did not return through RoundLifecycle")
        if packet_ids.count(CreatePlayer.id) < 3:
            raise RuntimeError("observer did not receive bot spawn/respawn creation")

        print(
            "bot_combat_ok",
            f"deaths={(first.deaths, second.deaths)}",
            f"kills={(first.kills, second.kills)}",
            f"lives={(first.replication_generation, second.replication_generation)}",
            f"shots={packet_ids.count(ShootFeedbackPacket.id)}",
            f"reloads={sorted(reload_completed)}",
            f"post_reload_shots={sorted(post_reload_shots)}",
            f"world_updates={len(updates)}",
        )
    finally:
        await director.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run authoritative bot combat and reload acceptance."
    )
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    asyncio.run(_run(seconds=args.seconds))
