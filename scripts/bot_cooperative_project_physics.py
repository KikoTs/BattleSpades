"""Two offline cooperative construction gates using authoritative native physics.

Only the public strategic destination and accelerated clock are controlled.
The production brain, coordinator, navigation, motor, action gateway, resource
checks, world mutation stream and 60 Hz player collisions run unchanged. No
network listener is opened and no position is assigned after initial spawn.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
import json
import math
from pathlib import Path
import random
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C
from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import MapSnapshot, PerceptionFrame, VoxelChange, WorldDelta
from server.bot_ai.policies import ModeBotDecision
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer


@dataclass
class ProjectPhysicsResult:
    scenario: str
    simulated_seconds: float = 0
    wall_seconds: float = 0
    initial_positions: dict[int, tuple[float, ...]] = field(default_factory=dict)
    final_positions: dict[int, tuple[float, ...]] = field(default_factory=dict)
    distance_walked: dict[int, float] = field(default_factory=dict)
    blocks_before: int = 0
    blocks_after: int = 0
    project_cells: tuple[tuple[int, int, int], ...] = ()
    committed_cells: list[tuple[int, int, int, bool]] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, object]] = field(default_factory=list)
    roles: dict[str, int] = field(default_factory=dict)
    actions: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, object]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


async def simulate_project(scenario: str, *, seconds: float = 24) -> ProjectPhysicsResult:
    """Require a Miner project and partner uptake in at most 24 simulated seconds."""
    if scenario not in {"bridge", "breach"}:
        raise ValueError(scenario)
    if not 0 < seconds <= 30:
        raise ValueError("the local gate is bounded to thirty simulated seconds")
    started = time.perf_counter()
    result = ProjectPhysicsResult(scenario)
    rng = random.getstate()
    subscription = None
    server = None
    try:
        random.seed(23)
        config = ServerConfig(default_mode="tdm", maps_path=str(ROOT / "maps"))
        config.bots.max_bots = 2
        config.bots.seed = 23
        server = BattleSpadesServer(config)
        world = server.world_manager
        world.generate_flat_map()
        # The debug plateau floats: anchor both banks so real structural
        # destruction cannot turn a single tunnel swing into a map collapse.
        for x in (90, 115):
            for z in range(63, 240):
                world.map.set_point(x, 100, z, 0x7F808080)
        if scenario == "bridge":
            for x in range(101, 105):
                for y in range(70, 131):
                    world.map.remove_point(x, y, 62)
        else:
            # A two-block-deep wall, too wide for a local detour. Its roof and
            # walking floor survive the recovered Super Spade dig footprint.
            for x in range(102, 104):
                for y in range(70, 131):
                    for z in range(55, 62):
                        world.map.set_point(x, y, z, 0x7F808080)
        world.map_raw_bytes = world.map.generate_vxl()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        for name, class_id in (("ProjectMiner", C.CLASS_MINER), ("ProjectPartner", C.CLASS_SOLDIER)):
            bot = await director.add_bot(team=TEAM1, name=name, class_id=int(class_id))
            if bot is None:
                raise RuntimeError("could not create offline production bot")
        miner, partner = tuple(director.bots)
        miner.loadout = [int(C.SUPERSPADE_TOOL), int(C.BLOCK_TOOL)]
        partner.loadout = [int(C.RIFLE_TOOL)]
        miner.prefabs = partner.prefabs = []
        miner.spawn(100.5, 100.5, 59.75)
        partner.spawn(96.5, 103.5, 59.75)
        miner.set_tool(int(C.SUPERSPADE_TOOL))
        partner.set_tool(int(C.RIFLE_TOOL))
        miner.blocks = 20
        partner.blocks = 0
        players = (miner, partner)
        for player in players:
            runtime = director._runtime[player.id]
            runtime.profile = replace(runtime.profile, teamwork=.9, creativity=.8)
        worker = SimpleVoxelWorld()
        worker.load(MapSnapshot(0, 0, world.map_raw_bytes, "tdm"))
        brain = SimpleBotBrain(worker, decision_hz=8)
        pending: dict[int, list[VoxelChange]] = defaultdict(list)

        def delta(x, y, z, solid, color, version):
            pending[version].append(VoxelChange(x, y, z, solid, color))

        subscription = world.subscribe_mutations(delta)
        result.initial_positions = {p.id: tuple(p.position) for p in players}
        previous = dict(result.initial_positions)
        result.distance_walked = {p.id: 0.0 for p in players}
        result.blocks_before = miner.blocks
        roles: Counter[str] = Counter()
        actions: Counter[str] = Counter()
        base = time.monotonic() + 1
        clock = [base]
        frame_id = 0
        goal = ModeBotDecision((120.5, 100.5, 59.75), "fixture_known_lane", arrival_radius=1)
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(brain.mode_policy, "decide", return_value=goal):
            for tick in range(math.ceil(seconds * 60)):
                clock[0] = now = base + tick / 60
                if tick % 8 == 0:
                    snapshots = director._snapshot_players()
                    for player in players:
                        frame_id += 1
                        runtime = director._runtime[player.id]
                        frame = PerceptionFrame(frame_id, 0, 0, world.topology_version,
                            player.id, runtime.generation, now, "tdm", snapshots,
                            profile=runtime.profile, behavior_version="cooperative", friendly_mischief=False)
                        intent = brain.decide(frame)
                        if intent:
                            runtime.intent = intent
                            roles[intent.debug_role] += 1
                            actions[intent.action.kind.value] += 1
                    for project in brain.cooperative.teams.projects.values():
                        if project.owner[0] == miner.id and project.kind == scenario:
                            result.project_cells = project.cells
                server.loop_count += 1
                for player in players:
                    if player.id % 6 == server.loop_count % 6:
                        director._apply_motor(director._runtime[player.id], now, .1)
                await server.simulation_runtime._simulate_players()
                server.world_mutations.commit_ready()
                server.prefab_actions.tick()
                await asyncio.sleep(0)
                for version, changes in sorted(pending.items()):
                    worker.apply(WorldDelta(0, version, tuple(changes)))
                    result.committed_cells.extend((c.x, c.y, c.z, c.solid) for c in changes)
                pending.clear()
                for player in players:
                    result.distance_walked[player.id] += math.dist(previous[player.id], player.position)
                    previous[player.id] = tuple(player.position)
                    if not player.alive or player.wade or player.z > 61:
                        result.failures.append(f"bot {player.id} left the dry walking surface at tick {tick}")
                if tick % 30 == 0:
                    result.trace.append({"seconds": round(tick / 60, 3), "bots": [{
                        "id": p.id, "position": [round(v, 3) for v in p.position],
                        "role": getattr(director._runtime[p.id].intent, "debug_role", ""),
                        "action": director._runtime[p.id].feedback_action_kind,
                        "accepted": director._runtime[p.id].feedback_action_accepted,
                        "reason": director._runtime[p.id].feedback_reason,
                    } for p in players]})
                result.simulated_seconds = (tick + 1) / 60
                # Both native bodies must reach the far bank; metrics alone
                # cannot pass this gate while still approaching the obstacle.
                # Conversely, crossing can happen between worker frames: let
                # the Miner observe its landing before ending the simulation.
                # The partner may already have confirmed uptake by that time.
                far_x = 105 if scenario == "bridge" else 104
                if (brain.cooperative.teams.metrics["tasks_completed"]
                        and brain.cooperative.teams.metrics["routes_used_by_partner"]
                        and all(p.x >= far_x for p in players)
                        and all(distance > 7 for distance in result.distance_walked.values())):
                    break
                if result.failures:
                    break
        result.final_positions = {p.id: tuple(p.position) for p in players}
        result.blocks_after = miner.blocks
        result.metrics = dict(brain.cooperative.teams.metrics)
        result.events = [{**e, "at": round(float(e["at"]) - base, 3)}
                         for e in brain.cooperative.teams.events]
        result.roles, result.actions = dict(roles), dict(actions)
        if not result.project_cells:
            result.failures.append("Miner did not reserve the expected project")
        if not result.metrics.get("tasks_completed"):
            result.failures.append("Miner did not complete and use the project")
        if not result.metrics.get("routes_used_by_partner"):
            result.failures.append("partner did not adopt and walk the completed route")
        far_x = 105 if scenario == "bridge" else 104
        if not all(p.x >= far_x for p in players):
            result.failures.append("both native bodies did not reach the far side")
        if scenario == "bridge":
            if result.blocks_before - result.blocks_after != len(result.project_cells):
                result.failures.append("bridge did not pay exactly its committed block count")
            if not all(world.get_solid(*cell) for cell in result.project_cells):
                result.failures.append("bridge cells are missing from authoritative geometry")
        elif any(world.get_solid(*cell) for cell in result.project_cells):
            result.failures.append("planned tunnel blockers remain in authoritative geometry")
        if scenario == "breach" and not all(world.get_solid(x, y, 58)
                for x in (102, 103) for y in (99, 100, 101)):
            result.failures.append("tunnel excavation removed its protective ceiling")
        if not all(world.get_solid(x, 100, 62) for x in range(100, 108)):
            result.failures.append("the completed route has missing footing")
        return result
    finally:
        if server is not None and subscription is not None:
            server.world_manager.unsubscribe_mutations(subscription)
        random.setstate(rng)
        result.wall_seconds = time.perf_counter() - started


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("bridge", "breach"), action="append")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    results = [await simulate_project(name) for name in args.scenario or ("bridge", "breach")]
    payload = [asdict(result) for result in results]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for result in results:
        print(f"{result.scenario}: {'FAIL' if result.failures else 'PASS'} "
              f"sim={result.simulated_seconds:.2f}s wall={result.wall_seconds:.2f}s "
              f"positions={result.final_positions} cells={len(result.committed_cells)} "
              f"failures={result.failures}")
    return int(any(result.failures for result in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
