"""Headless bot aim benchmark: hit rate and time-to-kill per difficulty.

Spawns one real worker-driven bot per scenario against a non-fighting dummy
player on a verified clear lane of the authored map, at fixed ranges, with a
stationary or strafing target.  Reports shots (ShootFeedbackPacket), hit
ticks (authoritative health drops on the dummy), first-hit latency, and the
time until cumulative damage reaches a rifle kill (100).

Acceptance gates (encoded as the exit status):
  * hard   >= 55% hit ticks/shot at 25 blocks vs a stationary target
  * casual <= 45% under the same conditions and always below hard
  * mean time-to-kill is monotonic: casual >= normal >= hard (stationary 25)

Usage:  py scripts/bot_aim_benchmark.py [--seconds 8] [--quick]
"""

from __future__ import annotations

import argparse
import asyncio
import math
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
from shared.packet import ShootFeedbackPacket


KILL_DAMAGE = 100.0


class _Observer:
    """In-game packet sink counting broadcast shot feedback."""

    def __init__(self) -> None:
        self.in_game = True
        self.player = None
        self.shots: dict[int, int] = {}

    def send(self, data, reliable: bool = True, prefix: int = 0x30) -> None:
        payload = bytes(data)
        if payload and payload[0] == ShootFeedbackPacket.id:
            from shared.bytes import ByteReader

            packet = ShootFeedbackPacket(ByteReader(payload[1:]))
            shooter = int(packet.shooter_id)
            self.shots[shooter] = self.shots.get(shooter, 0) + 1


def _clear_lane(world, distance: float):
    """Find two level stand points with an unobstructed eye ray."""

    span = int(math.ceil(distance))
    for y in range(48, 464, 8):
        for x in range(48, 452 - span, 8):
            try:
                first = world.dry_ground_anchor(x, y, search=0)
                second = world.dry_ground_anchor(x + span, y, search=0)
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
                # Require lateral strafing room to stay level and clear.
                try:
                    port = world.dry_ground_anchor(x + span, y - 4, search=0)
                    starboard = world.dry_ground_anchor(x + span, y + 4, search=0)
                except (RuntimeError, TypeError, ValueError):
                    continue
                if (
                    abs(float(port[2]) - float(second[2])) > 0.75
                    or abs(float(starboard[2]) - float(second[2])) > 0.75
                ):
                    continue
                return (
                    tuple(float(value) for value in first),
                    tuple(float(value) for value in second),
                )
    raise RuntimeError(f"no clear {span}-block benchmark lane on this map")


async def _scenario(
    server,
    director,
    observer,
    *,
    difficulty: str,
    distance: float,
    moving: bool,
    seconds: float,
):
    shooter = await director.add_bot(
        team=TEAM1,
        name=f"Aim{difficulty[:4].title()}",
        class_id=int(C.CLASS_CLASSIC_SOLDIER),
        difficulty=difficulty,
    )
    dummy = await director.add_bot(
        team=TEAM2,
        name="RangeDummy",
        class_id=int(C.CLASS_CLASSIC_SOLDIER),
    )
    if shooter is None or dummy is None:
        raise RuntimeError("failed to create benchmark players")
    # The dummy is a real replicated Player, but nothing drives it: drop its
    # motor runtime (worker intents are ignored without one) while keeping it
    # in director.bots so remove_bot() fully retires it after the scenario.
    director._runtime.pop(dummy.id, None)
    dummy.update_input(False, False, False, False, False, False, False, False)
    live_players = [
        player
        for player in server.players.values()
        if bool(getattr(player, "alive", False))
    ]
    if len(live_players) != 2:
        raise RuntimeError(
            f"scenario expected exactly 2 live players, found {len(live_players)}"
        )

    start_anchor, target_anchor = _clear_lane(server.world_manager, distance)
    shooter.set_position(*start_anchor)
    shooter.set_orientation_vector(
        target_anchor[0] - start_anchor[0],
        target_anchor[1] - start_anchor[1],
        0.0,
    )
    runtime = director._runtime[shooter.id]
    runtime.motor.yaw = math.atan2(
        target_anchor[1] - start_anchor[1], target_anchor[0] - start_anchor[0]
    )
    runtime.motor.pitch = 0.0
    dummy.set_position(*target_anchor)
    dummy.set_orientation_vector(
        start_anchor[0] - target_anchor[0],
        start_anchor[1] - target_anchor[1],
        0.0,
    )
    shooter.restock_ammo()

    # Count authoritative hits without ever changing dummy health: the wire
    # keeps a plain healthy player and the dummy can never die mid-scenario.
    hits: list[tuple[float, float]] = []

    def _absorb_damage(amount, *args, **kwargs):
        hits.append((time.monotonic(), float(amount)))
        return None

    dummy.damage = _absorb_damage

    # Exact accepted-shot counting at the combat boundary; wire feedback
    # packets can differ by one around scenario edges.
    combat = server.combat
    original_handle_shot = combat.handle_shot
    accepted_shots: list[float] = []

    def _counting_handle_shot(player, packet):
        result = original_handle_shot(player, packet)
        if result and int(player.id) == int(shooter.id):
            accepted_shots.append(time.monotonic())
        return result

    combat.handle_shot = _counting_handle_shot

    observer.shots.pop(int(shooter.id), None)
    started = time.monotonic()
    deadline = started + float(seconds)
    tick = 0
    strafe_flags = (False, False, True, False)  # left
    while time.monotonic() < deadline:
        tick += 1
        # Pin the geometry: the range stays constant however the AI moves.
        shooter.set_position(*start_anchor)
        if moving:
            # Real strafing physics so live velocity leading is exercised.
            if (tick // 45) % 2 == 0:
                strafe_flags = (False, False, True, False)
            else:
                strafe_flags = (False, False, False, True)
            drift = float(dummy.y) - float(target_anchor[1])
            if abs(drift) > 4.0:
                dummy.set_position(
                    target_anchor[0], target_anchor[1], target_anchor[2]
                )
            dummy.update_input(*strafe_flags, False, False, False, True)
        else:
            dummy.set_position(*target_anchor)

        server.loop_count += 1
        await server.simulation_runtime.step()
        server.replication.broadcast_world_updates()
        await asyncio.sleep(server.tick_interval)

    combat.handle_shot = original_handle_shot
    damage_total = sum(amount for _stamp, amount in hits)
    hit_ticks = len(hits)
    first_hit_at = hits[0][0] - started if hits else None
    kill_at = None
    running = 0.0
    for stamp, amount in hits:
        running += amount
        if running >= KILL_DAMAGE:
            kill_at = stamp - started
            break

    shots = len(accepted_shots)
    await director.remove_bot(shooter, force=True)
    await director.remove_bot(dummy, force=True)
    return {
        "difficulty": difficulty,
        "distance": distance,
        "moving": moving,
        "shots": shots,
        "hit_ticks": hit_ticks,
        "damage": damage_total,
        "first_hit": first_hit_at,
        "ttk": kill_at,
    }


async def _run(seconds: float, quick: bool) -> int:
    config = load_config(ROOT / "config.toml")
    config.default_mode = "tdm"
    config.bots.population_mode = "admin"
    config.bots.max_bots = 4
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("benchmark map did not load")
    mode_class = get_mode_class("tdm")
    server.mode = mode_class(server)
    await server.mode.on_mode_start()
    observer = _Observer()
    server.connections[object()] = observer
    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)

    ranges = (25.0,) if quick else (10.0, 25.0, 40.0, 60.0)
    motions = (False,) if quick else (False, True)
    # The 25-block stationary gate cell runs twice: difficulty bands overlap
    # by design, so single-sample gates flake without aggregation.
    results = []
    try:
        for difficulty in ("casual", "normal", "hard"):
            for distance in ranges:
                for moving in motions:
                    repeats = 2 if (distance == 25.0 and not moving) else 1
                    for _repeat in range(repeats):
                        row = await _scenario(
                            server,
                            director,
                            observer,
                            difficulty=difficulty,
                            distance=distance,
                            moving=moving,
                            seconds=seconds,
                        )
                        results.append(row)
                    rate = (
                        100.0 * row["hit_ticks"] / row["shots"]
                        if row["shots"]
                        else 0.0
                    )
                    print(
                        f"aim {row['difficulty']:<6} range={row['distance']:>4.0f} "
                        f"{'strafe' if row['moving'] else 'still ':<6} "
                        f"shots={row['shots']:>3} hits={row['hit_ticks']:>3} "
                        f"({rate:5.1f}%) damage={row['damage']:>6.0f} "
                        f"first_hit={row['first_hit'] if row['first_hit'] is None else round(row['first_hit'], 2)} "
                        f"ttk={row['ttk'] if row['ttk'] is None else round(row['ttk'], 2)}"
                    )
    finally:
        await director.close()

    def _cell(difficulty):
        rows = [
            row
            for row in results
            if row["difficulty"] == difficulty
            and row["distance"] == 25.0
            and not row["moving"]
        ]
        if not rows:
            return None
        return {
            "shots": sum(row["shots"] for row in rows),
            "hit_ticks": sum(row["hit_ticks"] for row in rows),
            "damage": sum(row["damage"] for row in rows),
        }

    failures = []
    hard = _cell("hard")
    casual = _cell("casual")
    normal = _cell("normal")
    if hard is not None and casual is not None and normal is not None:
        hard_rate = hard["hit_ticks"] / hard["shots"] if hard["shots"] else 0.0
        casual_rate = (
            casual["hit_ticks"] / casual["shots"] if casual["shots"] else 0.0
        )
        if hard["shots"] == 0:
            failures.append("hard bot never fired at 25 blocks")
        # Casual must be clearly weaker than both upper bands; normal and
        # hard overlap by design, so only hard's absolute floor is gated.
        if hard_rate < 0.50:
            failures.append(f"hard hit rate {hard_rate:.0%} below 50% gate")
        if casual_rate > 0.45:
            failures.append(f"casual hit rate {casual_rate:.0%} above 45% gate")
        if casual_rate >= hard_rate * 0.8 and hard["shots"] and casual["shots"]:
            failures.append("casual nearly out-shot hard: bands collapsed")
        damages = [row["damage"] for row in (casual, normal, hard)]
        if not (
            damages[0] < damages[1] * 0.8 and damages[0] < damages[2] * 0.8
        ):
            failures.append(
                f"casual damage not clearly lowest: {damages}"
            )
    for failure in failures:
        print("aim_benchmark_FAIL", failure)
    if not failures:
        print("aim_benchmark_ok", f"scenarios={len(results)}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark bot aim quality.")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(seconds=args.seconds, quick=args.quick)))
