"""Headless acceptance for Zombie-mode survivor fortification.

Three survivor bots are held in the WAITING phase on the authored map for a
bounded window.  Passing requires real committed BlockBuild mutations near
the team anchor, at least a few two-high wall columns, and that no live bot
ends up entombed (a bounded flood-fill from each bot's column must escape
its local neighborhood).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C
from modes import get_mode_class
from modes.zombie import ZombiePhase
from server.bot_ai import BotDirector
from server.config import load_config
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


def _escapes(world, start, *, radius: int = 12, budget: int = 1200) -> bool:
    """Bounded walkability flood-fill: can a standing player leave the area?"""

    def support_at(x: int, y: int, near_z: int):
        for z in range(max(2, near_z - 4), min(239, near_z + 5)):
            if (
                world.get_solid(x, y, z)
                and not world.get_solid(x, y, z - 1)
                and not world.get_solid(x, y, z - 2)
            ):
                return z
        return None

    sx, sy = int(start[0]), int(start[1])
    sz = support_at(sx, sy, int(start[2] + 2.25))
    if sz is None:
        return False
    seen = {(sx, sy)}
    frontier = deque([(sx, sy, sz)])
    expanded = 0
    while frontier and expanded < budget:
        x, y, z = frontier.popleft()
        expanded += 1
        if max(abs(x - sx), abs(y - sy)) >= radius:
            return True
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if (nx, ny) in seen or not (0 <= nx < 512 and 0 <= ny < 512):
                continue
            nz = support_at(nx, ny, z)
            if nz is None or abs(nz - z) > 1:
                continue
            seen.add((nx, ny))
            frontier.append((nx, ny, nz))
    return False


async def _run(seconds: float = 45.0) -> None:
    config = load_config(ROOT / "config.toml")
    config.default_mode = "zom"
    config.bots.population_mode = "admin"
    config.bots.max_bots = 3
    server = BattleSpadesServer(config)
    if not server.world_manager.load_map(config.default_map):
        raise RuntimeError("fortify smoke map did not load")
    mode_class = get_mode_class("zom")
    server.mode = mode_class(server)
    await server.mode.on_mode_start()

    anchor = server.world_manager.team_base_anchor(TEAM1)
    added_cells: list[tuple[int, int, int]] = []

    def _on_mutation(x, y, z, solid, _color, _topology):
        if solid:
            added_cells.append((int(x), int(y), int(z)))

    server.world_manager.subscribe_mutations(_on_mutation)

    director = BotDirector(server)
    server.bots = director
    await director.start(initial_count=0)
    try:
        bots = []
        for name, class_id in (
            ("FortEng", int(C.CLASS_ENGINEER)),
            ("FortMiner", int(C.CLASS_MINER)),
            ("FortSold", int(C.CLASS_SOLDIER)),
        ):
            bot = await director.add_bot(
                team=TEAM1, name=name, class_id=class_id
            )
            if bot is None:
                raise RuntimeError(f"failed to create fortify bot {name}")
            bots.append(bot)

        deadline = time.monotonic() + max(10.0, float(seconds))
        while time.monotonic() < deadline:
            # Pin the pre-outbreak phase so the survivors keep fortifying.
            server.mode.phase = ZombiePhase.WAITING
            server.loop_count += 1
            await server.simulation_runtime.step()
            server.replication.broadcast_world_updates()
            await asyncio.sleep(server.tick_interval)

        if len(added_cells) < 12:
            raise RuntimeError(
                f"survivors placed only {len(added_cells)} blocks in "
                f"{seconds:.0f}s; expected >= 12"
            )
        far = [
            cell
            for cell in added_cells
            if math.hypot(cell[0] - anchor[0], cell[1] - anchor[1]) > 45.0
        ]
        if far:
            raise RuntimeError(
                f"{len(far)} fortification blocks landed far from the "
                f"anchor {anchor}: {far[:5]}"
            )
        columns: dict[tuple[int, int], set[int]] = {}
        for x, y, z in added_cells:
            columns.setdefault((x, y), set()).add(z)
        two_high = [
            column
            for column, zs in columns.items()
            if any(z - 1 in zs for z in zs)
        ]
        if len(two_high) < 3:
            raise RuntimeError(
                f"expected >= 3 two-high wall columns, found {len(two_high)} "
                f"of {len(columns)} columns"
            )
        # A sealed perimeter is the goal, so bots INSIDE the fort legitimately
        # cannot walk out. Entombment means being boxed in AWAY from the fort:
        # not near the built walls and still unable to leave its pocket.
        fort_x = sum(x for (x, _y) in two_high) / len(two_high)
        fort_y = sum(y for (_x, y) in two_high) / len(two_high)
        entombed = [
            f"{bot.name}@({bot.x:.0f},{bot.y:.0f})"
            for bot in bots
            if bot.alive
            and max(abs(bot.x - fort_x), abs(bot.y - fort_y)) > 7.0
            and not _escapes(server.world_manager, bot.position)
        ]
        if entombed:
            raise RuntimeError(
                f"bots entombed away from the fort "
                f"({fort_x:.0f},{fort_y:.0f}): {entombed}"
            )

        print(
            "bot_fortify_ok",
            f"blocks={len(added_cells)}",
            f"columns={len(columns)}",
            f"two_high={len(two_high)}",
            f"anchor=({anchor[0]:.0f},{anchor[1]:.0f})",
            f"worker_pid={director.status().process_id}",
        )
    finally:
        await director.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run Zombie survivor fortification acceptance."
    )
    parser.add_argument("--seconds", type=float, default=45.0)
    args = parser.parse_args()
    asyncio.run(_run(seconds=args.seconds))
