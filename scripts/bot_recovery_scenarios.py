"""Offline recovery gates: water/pit climbing and gap bridging.

Runs the production brain, director motor, action gateway, mutation commits,
inventory and native 60 Hz physics on an accelerated clock. Only the initial
body positions and the strategic destination are controlled; no position is
assigned after the drop.

Scenarios
---------
``water``  DragonIsland: bots are dropped into the sea next to cliffs whose
           top is the map's main walkable ground. Success = standing dry on
           main ground (the island top), not merely touching a beach.
``pit``    Flat debug plateau with a 6-deep, 3x3 shaft dug under each bot.
``gap``    Flat debug plateau split by a bottomless trench (water far below)
           that has no walking detour. Success = standing on the far side.

Run::

    py -3.12 scripts/bot_recovery_scenarios.py --scenario water --bots 8 --seconds 90 --json tmp/recovery/water.json
    py -3.12 scripts/bot_recovery_scenarios.py --scenario gap --gap-width 8 --bots 4 --seconds 40
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
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

import shared.constants as C  # noqa: E402
from modes.tdm import TDMMode  # noqa: E402
from server.bot_ai.director import BotDirector  # noqa: E402
from server.bot_ai.messages import PerceptionFrame, VoxelChange, WorldDelta  # noqa: E402
from server.bot_ai.policies import ModeBotDecision  # noqa: E402
from server.bot_ai.simple_navigation import SimpleVoxelWorld  # noqa: E402
from server.bot_ai.simple_worker import SimpleBotBrain  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.game_constants import TEAM1  # noqa: E402
from server.main import BattleSpadesServer  # noqa: E402

SIMULATION_HZ = 60
DECISION_INTERVAL_TICKS = 8
MOTOR_PHASES = 6
WATER_SUPPORT = int(C.Z_ABOVE_WATERPLANE) + 1

DEFAULT_CLASSES = (
    int(C.CLASS_MINER),      # Super Spade, no blocks: dig a staircase
    int(C.CLASS_SOLDIER),    # knife (weak digger), 200 blocks: pillar
    int(C.CLASS_ENGINEER),   # pickaxe, many blocks
    int(C.CLASS_SCOUT),      # knife, blocks
    int(C.CLASS_ROCKETEER),  # pickaxe
    int(C.CLASS_MEDIC),      # pickaxe
    int(C.CLASS_SPECIALIST),  # machete
    int(C.CLASS_MINER),
)


@dataclass
class BotOutcome:
    bot_id: int
    class_id: int
    start: tuple[float, float, float]
    recovered_at: float | None = None
    left_water_at: float | None = None
    final: tuple[float, float, float] = (0.0, 0.0, 0.0)
    deaths: int = 0
    blocks_used: int = 0
    digs_requested: int = 0
    builds_requested: int = 0
    lines_requested: int = 0
    roles: dict[str, int] = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: str
    map_name: str
    seed: int
    seconds: float
    simulated_seconds: float = 0.0
    wall_seconds: float = 0.0
    bots: list[BotOutcome] = field(default_factory=list)
    recovered: int = 0
    success_rate: float = 0.0
    median_recovery_seconds: float | None = None
    mean_recovery_seconds: float | None = None
    max_recovery_seconds: float | None = None
    slowest_decision_ms: float = 0.0
    terrain_changes: int = 0
    skill_metrics: dict[str, int] = field(default_factory=dict)
    skill_events: list = field(default_factory=list)


def _pit_sites(world, atlas, count: int, depth: int, seed: int):
    """Flat, deep-solid main-ground spots for test shafts, spread apart."""

    rng = random.Random(seed)
    sites = []
    main = atlas.main_region_id
    candidates = []
    for y in range(150, 420, 3):
        for x in range(150, 320, 3):
            index = y * 512 + x
            if int(atlas.regions[index]) != main or atlas.layer_count[index] != 1:
                continue
            top = int(atlas.primary_support[index])
            if top > 230:
                continue
            ok = True
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    if int(atlas.primary_support[(y + dy) * 512 + x + dx]) != top:
                        ok = False
                        break
                    if not all(world.get_solid(x + dx, y + dy, z)
                               for z in range(top, top + depth + 2)):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                candidates.append((x, y, top))
    rng.shuffle(candidates)
    for candidate in candidates:
        if all(math.hypot(candidate[0] - s[0], candidate[1] - s[1]) > 10 for s in sites):
            sites.append(candidate)
        if len(sites) >= count:
            break
    if len(sites) < count:
        raise RuntimeError("not enough pit sites")
    return sites


class _NoSkills:
    """Stand-in driver that never engages a skill (baseline runs)."""

    metrics: dict = {}
    events: list = []

    def __getattr__(self, name):
        if name in ("active",):
            return lambda *args, **kwargs: False
        if name in ("describe",):
            return lambda *args, **kwargs: ""
        return lambda *args, **kwargs: None


def _support(position) -> int:
    return int(round(float(position[2]) + 2.25))


def _water_drop_points(atlas, worker: SimpleVoxelWorld, count: int, seed: int):
    """Sea cells beside main-ground cliffs, spread around the island."""

    main = atlas.main_region_id
    candidates = []
    for y in range(2, 510):
        for x in range(2, 510):
            index = y * 512 + x
            if not atlas.flags[index] & 1:  # WATER
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = (y + dy) * 512 + x + dx
                if not atlas.flags[neighbor] & 2:  # DRY
                    continue
                if int(atlas.regions[neighbor]) != main:
                    continue
                rise = WATER_SUPPORT - int(atlas.primary_support[neighbor])
                if rise >= 6:
                    candidates.append((x, y, rise))
                    break
    rng = random.Random(seed)
    rng.shuffle(candidates)
    chosen = []
    if not candidates:
        return chosen
    chosen.append(candidates[0])
    while len(chosen) < count and len(chosen) < len(candidates):
        best = max(candidates, key=lambda c: min(
            math.hypot(c[0] - o[0], c[1] - o[1]) for o in chosen))
        chosen.append(best)
    return chosen


async def run_scenario(
    scenario: str,
    *,
    map_name: str = "DragonIsland",
    bots: int = 8,
    seconds: float = 90.0,
    seed: int = 7,
    classes: tuple[int, ...] = DEFAULT_CLASSES,
    gap_width: int = 8,
    pit_depth: int = 6,
    trace_every: float = 0.0,
    trace_bot: int | None = None,
    disable_skills: bool = False,
) -> ScenarioResult:
    if scenario not in {"water", "pit", "gap", "pillar", "staircase"}:
        raise ValueError(scenario)
    started = time.perf_counter()
    result = ScenarioResult(scenario, map_name if scenario in ("water", "pit", "staircase") else "flat",
                            seed, seconds)
    random_state = random.getstate()
    random.seed(seed)
    subscription = None
    server = None
    patcher = None
    try:
        config = ServerConfig(default_mode="tdm", maps_path=str(ROOT / "maps"),
                              default_map=map_name)
        config.bots.max_bots = bots
        config.bots.seed = seed
        config.max_players = max(int(config.max_players), bots)
        server = BattleSpadesServer(config)
        world = server.world_manager
        if scenario in ("water", "pit", "staircase"):
            if not world.load_map(map_name):
                raise RuntimeError(f"could not load {map_name}")
        else:
            world.generate_flat_map()
            if scenario == "gap":
                # A full-width chasm down to the sea: no detour exists.
                for x in range(101, 101 + gap_width):
                    for y in range(0, 512):
                        for z in range(62, 239):
                            if world.map.get_solid(x, y, z):
                                world.map.remove_point(x, y, z)
            world.map_raw_bytes = world.map.generate_vxl()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        for index in range(bots):
            bot = await director.add_bot(team=TEAM1, name=f"Rec{index}",
                                         class_id=int(classes[index % len(classes)]))
            if bot is None:
                raise RuntimeError("could not create bot")
        players = tuple(director.bots)
        worker = SimpleVoxelWorld()
        worker.load(director._make_map_snapshot(current=False))
        atlas = worker._atlas
        brain = SimpleBotBrain(worker, decision_hz=8.0)
        if disable_skills:
            # "Before" baseline: the production brain without locomotion skills.
            brain.skills = _NoSkills()

        goals: dict[int, tuple[float, float, float]] = {}
        pit_centres: dict[int, tuple[int, int, int]] = {}
        if scenario == "water":
            points = _water_drop_points(atlas, worker, len(players), seed)
            if len(points) < len(players):
                raise RuntimeError("not enough drop points")
            spawn = (226.5, 189.5)
            goal_support = int(atlas.primary_support[189 * 512 + 226])
            for player, (x, y, _rise) in zip(players, points):
                player.set_position(x + 0.5, y + 0.5, WATER_SUPPORT - 2.25)
                player._world_object.set_velocity(0.0, 0.0, 0.0)
                goals[player.id] = (spawn[0], spawn[1], goal_support - 2.25)
        elif scenario in ("pit", "staircase"):
            # Real, grounded island terrain: a 3x3 shaft ``pit_depth`` deep
            # dug into solid main ground near the team spawn. ``staircase``
            # orders the bot (ModeBotDecision.directive) to dig a staircase
            # east out of it, up to the rim.
            pits = _pit_sites(world, atlas, len(players), pit_depth, seed)
            for player, (cx, cy, top) in zip(players, pits):
                for x in range(cx - 1, cx + 2):
                    for y in range(cy - 1, cy + 2):
                        for z in range(top - 2, top + pit_depth):
                            if world.map.get_solid(x, y, z):
                                world.map.remove_point(x, y, z)
                player.set_position(cx + 0.5, cy + 0.5, top + pit_depth - 2.25)
                goals[player.id] = ((cx + pit_depth + 1.5, cy + 0.5, top - 2.25)
                                    if scenario == "staircase"
                                    else (cx + 0.5, cy + 0.5 + 12, top - 2.25))
                pit_centres[player.id] = (cx, cy, top)
            world.map_raw_bytes = world.map.generate_vxl()
            worker.load(director._make_map_snapshot(current=False))
        elif scenario == "pillar":
            # Strategy-layer primitive through ModeBotDecision.directive:
            # pillar up ``pit_depth`` levels on open ground.
            for index, player in enumerate(players):
                y = 90 + index * 8
                player.spawn(98.5, y + 0.5, 59.75)
                goals[player.id] = (98.5, y + 0.5, 62 - pit_depth - 2.25)
        else:
            for index, player in enumerate(players):
                y = 90 + index * 8
                player.spawn(96.5, y + 0.5, 59.75)
                goals[player.id] = (101 + gap_width + 8.5, y + 0.5, 59.75)
        for player in players:
            player._world_object.set_velocity(0.0, 0.0, 0.0)
        outcomes = {
            p.id: BotOutcome(p.id, int(p.class_id), tuple(round(v, 2) for v in p.position))
            for p in players
        }
        blocks_before = {p.id: int(p.blocks) for p in players}
        pending: dict[int, list[VoxelChange]] = defaultdict(list)

        def remember(x, y, z, solid, color, version):
            pending[int(version)].append(VoxelChange(int(x), int(y), int(z), bool(solid), int(color)))

        subscription = world.subscribe_mutations(remember)
        base = math.ceil(time.monotonic() + 2.0)
        clock = [base]
        patcher = patch.object(time, "monotonic", side_effect=lambda: clock[0])
        patcher.start()
        frame_id = 0
        alive_before = {p.id: True for p in players}
        roles: dict[int, Counter] = defaultdict(Counter)

        directive = {"pillar": "pillar_up", "staircase": "dig_staircase_up"}.get(scenario, "")

        def decide(frame, observer):
            goal = goals.get(int(observer.player_id))
            if goal is None:
                return None
            return ModeBotDecision(goal, "recovery_goal", arrival_radius=2.0,
                                   directive=directive)

        with patch.object(brain.mode_policy, "decide", side_effect=decide):
            total_ticks = int(round(seconds * SIMULATION_HZ))
            for tick in range(total_ticks):
                now = base + tick / SIMULATION_HZ
                clock[0] = now
                if tick % DECISION_INTERVAL_TICKS == 0:
                    snapshots = director._snapshot_players()
                    for player in players:
                        if not player.alive:
                            continue
                        runtime = director._runtime[player.id]
                        frame_id += 1
                        frame = PerceptionFrame(
                            frame_id, 0, 0, world.topology_version, player.id,
                            runtime.generation, now, "tdm", snapshots,
                            profile=runtime.profile, mode_phase=director._mode_phase(),
                        )
                        decision_started = time.perf_counter()
                        intent = brain.decide(frame)
                        result.slowest_decision_ms = max(
                            result.slowest_decision_ms,
                            (time.perf_counter() - decision_started) * 1000.0)
                        if intent is not None:
                            runtime.intent = intent
                            roles[player.id][intent.debug_role.split(":")[-1]] += 1
                            kind = intent.action.kind.value
                            outcome = outcomes[player.id]
                            if kind == "melee":
                                outcome.digs_requested += 1
                            elif kind == "build":
                                outcome.builds_requested += 1
                            elif kind == "build_line":
                                outcome.lines_requested += 1
                server.loop_count += 1
                phase = server.loop_count % MOTOR_PHASES
                for player in players:
                    if player.id % MOTOR_PHASES == phase:
                        director._apply_motor(director._runtime[player.id], now,
                                              MOTOR_PHASES / SIMULATION_HZ)
                await server.simulation_runtime._simulate_players()
                server.world_mutations.commit_ready()
                server.prefab_actions.tick()
                await asyncio.sleep(0)
                for version, changes in sorted(pending.items()):
                    worker.apply(WorldDelta(0, version, tuple(changes)))
                    result.terrain_changes += len(changes)
                pending.clear()
                elapsed = (tick + 1) / SIMULATION_HZ
                for player in players:
                    outcome = outcomes[player.id]
                    if alive_before[player.id] and not player.alive:
                        outcome.deaths += 1
                    alive_before[player.id] = bool(player.alive)
                    if not player.alive or outcome.recovered_at is not None:
                        continue
                    grounded = bool(player.grounded) and not bool(player.wade)
                    if grounded and outcome.left_water_at is None:
                        outcome.left_water_at = round(elapsed, 3)
                    if not grounded:
                        continue
                    x, y = int(math.floor(player.x)), int(math.floor(player.y))
                    support = _support(player.position)
                    if scenario == "water":
                        context = atlas.context(x, y, support)
                        primary = context.primary_support_z if context else None
                        # On the top of a main-ground column (a walkable
                        # beach of the island counts), or inside one of its
                        # buildings well above the sea.
                        if (context is not None and context.main_ground
                                and primary is not None
                                and (abs(support - primary) <= 2
                                     or support <= WATER_SUPPORT - 8)):
                            outcome.recovered_at = round(elapsed, 3)
                    elif scenario in ("pit", "staircase"):
                        cx, cy, top = pit_centres[player.id]
                        if support <= top + 1 and (abs(x - cx) > 1 or abs(y - cy) > 1):
                            outcome.recovered_at = round(elapsed, 3)
                    elif scenario == "pillar":
                        if support <= 62 - pit_depth:
                            outcome.recovered_at = round(elapsed, 3)
                    else:
                        if x >= 101 + gap_width and support == 62:
                            outcome.recovered_at = round(elapsed, 3)
                if trace_every > 0 and tick % int(trace_every * SIMULATION_HZ) == 0:
                    for player in players:
                        if trace_bot is not None and player.id != trace_bot:
                            continue
                        intent = director._runtime[player.id].intent
                        runtime = director._runtime[player.id]
                        snapshot = next((s for s in director._snapshot_players()
                                         if s.player_id == player.id), None)
                        skill = brain.skills.describe(snapshot) if snapshot else ""
                        action = getattr(getattr(intent, "action", None), "kind", None)
                        print(f"   skill={skill} action={getattr(action, 'value', '')} "
                              f"fb={runtime.feedback_action_kind}:{runtime.feedback_action_accepted}:"
                              f"{runtime.feedback_reason}", flush=True)
                        print(f"t={elapsed:6.2f} bot={player.id} cls={player.class_id} "
                              f"pos=({player.x:.1f},{player.y:.1f},{player.z:.2f}) "
                              f"wade={int(player.wade)} gr={int(player.grounded)} "
                              f"blk={player.blocks} role={getattr(intent, 'debug_role', '')}",
                              flush=True)
                result.simulated_seconds = elapsed
                if all(o.recovered_at is not None for o in outcomes.values()):
                    break
        for player in players:
            outcome = outcomes[player.id]
            outcome.final = tuple(round(v, 2) for v in player.position)
            outcome.blocks_used = blocks_before[player.id] - int(player.blocks)
            outcome.roles = dict(roles[player.id].most_common(8))
        result.bots = list(outcomes.values())
        result.skill_metrics = dict(sorted(getattr(getattr(brain, "skills", None),
                                                   "metrics", {}).items()))
        result.skill_events = list(getattr(getattr(brain, "skills", None), "events", []))
        times = sorted(o.recovered_at for o in result.bots if o.recovered_at is not None)
        result.recovered = len(times)
        result.success_rate = round(len(times) / max(1, len(result.bots)), 3)
        if times:
            result.median_recovery_seconds = times[len(times) // 2]
            result.mean_recovery_seconds = round(sum(times) / len(times), 2)
            result.max_recovery_seconds = times[-1]
    finally:
        if patcher is not None:
            patcher.stop()
        if subscription is not None and server is not None:
            try:
                server.world_manager.unsubscribe_mutations(subscription)
            except Exception:  # noqa: BLE001 - teardown only
                pass
        random.setstate(random_state)
        result.wall_seconds = round(time.perf_counter() - started, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("water", "pit", "gap", "pillar", "staircase"),
                        default="water")
    parser.add_argument("--map", default="DragonIsland")
    parser.add_argument("--bots", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--gap-width", type=int, default=8)
    parser.add_argument("--pit-depth", type=int, default=6)
    parser.add_argument("--classes", default="",
                        help="comma-separated class ids (default: a mixed roster)")
    parser.add_argument("--trace-every", type=float, default=0.0)
    parser.add_argument("--trace-bot", type=int, default=None)
    parser.add_argument("--disable-skills", action="store_true",
                        help="baseline: run without the locomotion skill driver")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    classes = (tuple(int(value) for value in args.classes.split(",") if value)
               or DEFAULT_CLASSES)
    result = asyncio.run(run_scenario(
        args.scenario, map_name=args.map, bots=args.bots, seconds=args.seconds,
        seed=args.seed, classes=classes, gap_width=args.gap_width,
        pit_depth=args.pit_depth, trace_every=args.trace_every,
        trace_bot=args.trace_bot, disable_skills=args.disable_skills,
    ))
    for outcome in result.bots:
        print(f"bot {outcome.bot_id} class={outcome.class_id} start={outcome.start} "
              f"recovered={outcome.recovered_at} left_water={outcome.left_water_at} "
              f"final={outcome.final} deaths={outcome.deaths} blocks={outcome.blocks_used} "
              f"digs={outcome.digs_requested} builds={outcome.builds_requested} "
              f"lines={outcome.lines_requested} roles={outcome.roles}")
    print(f"skill metrics: {result.skill_metrics}")
    for event in result.skill_events:
        print("  skill failure:", event)
    print(f"scenario={result.scenario} recovered={result.recovered}/{len(result.bots)} "
          f"median={result.median_recovery_seconds} mean={result.mean_recovery_seconds} "
          f"max={result.max_recovery_seconds} simulated={result.simulated_seconds:.1f}s "
          f"wall={result.wall_seconds}s slowest_decision={result.slowest_decision_ms:.1f}ms")
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
