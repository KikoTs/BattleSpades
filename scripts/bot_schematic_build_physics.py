"""Offline native gate: bots build schematics together with block lines.

Production server composition (no network listener): the real brain and
cooperative coordinator, schematic planner, director motor and aim model,
action gateway, CombatSystem BlockLine validation, inventory, world
mutation stream and 60 Hz native player physics. Only spawn positions, the
strategic decision (a fixed mode role) and the accelerated clock are
controlled; no position is assigned after spawning.

Scenarios:

* ``vip_shelter`` - VIP mode roles: the VIP (``vip_rally``) boxes himself in
  with ``vip_shelter`` while two escorts (``vip_guard_formation``) help.
* ``watchtower`` - strategy request API: two bots build a tower whose
  platform is reached only by climbing the stair they build.
* ``small_hut`` - strategy request API with three builders.
* ``defend`` - a defender holding its post (DEFEND posture) digs in with a
  defensive schematic of its own choosing.

Run::

    py -3.12 scripts/bot_schematic_build_physics.py --json tmp/schematic-physics/report.json
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
from server.bot_ai.messages import (
    MapSnapshot, ObjectiveSnapshot, PerceptionFrame, VoxelChange, WorldDelta,
)
from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
from server.bot_ai.schematics.sites import interior_columns
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer

GROUND = 62
SCENARIOS = ("vip_shelter", "watchtower", "small_hut", "defend")


@dataclass
class SchematicPhysicsResult:
    scenario: str
    schematic: str = ""
    simulated_seconds: float = 0.0
    wall_seconds: float = 0.0
    completion_seconds: float = 0.0
    plan_steps: int = 0
    plan_cells: int = 0
    plan_estimate_seconds: float = 0.0
    lines_built: int = 0
    singles_built: int = 0
    line_cells: int = 0
    single_cells: int = 0
    cells_by_builder: dict[int, int] = field(default_factory=dict)
    blocks_spent: dict[int, int] = field(default_factory=dict)
    authoritative_cells_present: int = 0
    rejected_actions: dict[str, int] = field(default_factory=dict)
    accepted_actions: dict[str, int] = field(default_factory=dict)
    distance_walked: dict[int, float] = field(default_factory=dict)
    highest_support: dict[int, int] = field(default_factory=dict)
    final_positions: dict[int, tuple[float, ...]] = field(default_factory=dict)
    roles: dict[str, int] = field(default_factory=dict)
    site_status: dict[str, object] = field(default_factory=dict)
    metrics: dict[str, int] = field(default_factory=dict)
    colours: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _decision_for(scenario: str, observer, vip_id: int, posts: dict[int, tuple]) -> ModeBotDecision:
    if scenario == "vip_shelter":
        if observer.player_id == vip_id:
            return ModeBotDecision(observer.position, "vip_rally", sprint=False, arrival_radius=6.0,
                                   posture=ModeBotPosture.EVASIVE, objective_priority=1.0)
        return ModeBotDecision(posts[vip_id], "vip_guard_formation", arrival_radius=6.0,
                               posture=ModeBotPosture.ESCORT, objective_priority=.94)
    if scenario == "defend":
        return ModeBotDecision(posts[observer.player_id], "ctf_defend_intel", sprint=False,
                               arrival_radius=4.0, posture=ModeBotPosture.DEFEND,
                               objective_priority=.95, watch_position=(140.5, 100.5, 59.75))
    # Request scenarios: an idle, non-committed role near the spawn.
    return ModeBotDecision(posts[observer.player_id], "fixture_idle", sprint=False,
                           arrival_radius=3.0, objective_priority=.3)


async def simulate(scenario: str, *, seconds: float = 90.0) -> SchematicPhysicsResult:
    if scenario not in SCENARIOS:
        raise ValueError(scenario)
    started = time.perf_counter()
    result = SchematicPhysicsResult(scenario)
    rng = random.getstate()
    subscription = None
    server = None
    try:
        random.seed(31)
        config = ServerConfig(default_mode="tdm", maps_path=str(ROOT / "maps"))
        config.bots.max_bots = 4
        config.bots.seed = 31
        server = BattleSpadesServer(config)
        world = server.world_manager
        world.generate_flat_map()
        world.map_raw_bytes = world.map.generate_vxl()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        server.bots = director
        spawns = {
            "vip_shelter": [(100.5, 100.5), (94.5, 96.5), (106.5, 104.5)],
            "watchtower": [(96.5, 97.5), (96.5, 103.5)],
            "small_hut": [(95.5, 96.5), (95.5, 104.5), (106.5, 100.5)],
            "defend": [(100.5, 100.5)],
        }[scenario]
        players = []
        for index, (x, y) in enumerate(spawns):
            bot = await director.add_bot(team=TEAM1, name=f"Builder{index}", class_id=int(C.CLASS_SOLDIER))
            if bot is None:
                raise RuntimeError("could not create offline production bot")
            players.append(bot)
        for bot, (x, y) in zip(players, spawns):
            bot.loadout = [int(C.BLOCK_TOOL), int(C.RIFLE_TOOL)]
            bot.prefabs = []
            bot.spawn(x, y, GROUND - 2.25)
            bot.set_tool(int(C.RIFLE_TOOL))
            bot.blocks = 200
            runtime = director._runtime[bot.id]
            runtime.profile = replace(runtime.profile, teamwork=.9, creativity=.8)
        vip_id = players[0].id
        posts = {bot.id: tuple(bot.position) for bot in players}
        worker = SimpleVoxelWorld()
        worker.load(MapSnapshot(0, 0, world.map_raw_bytes, "tdm"))
        brain = SimpleBotBrain(worker, decision_hz=8)
        pending: dict[int, list[VoxelChange]] = defaultdict(list)

        def delta(x, y, z, solid, color, version):
            pending[version].append(VoxelChange(x, y, z, solid, color))

        subscription = world.subscribe_mutations(delta)
        construction_reasons: Counter[str] = Counter()
        construction = getattr(server, "construction", None)
        if construction is not None:
            original_reserve = construction.reserve_construction

            def reserve(owner_id, team, cells, **kwargs):
                token, reason = original_reserve(owner_id, team, cells, **kwargs)
                construction_reasons[reason or "ok"] += 1
                return token, reason

            construction.reserve_construction = reserve
        previous = {p.id: tuple(p.position) for p in players}
        result.distance_walked = {p.id: 0.0 for p in players}
        result.highest_support = {p.id: GROUND for p in players}
        blocks_before = {p.id: p.blocks for p in players}
        roles: Counter[str] = Counter()
        accepted: Counter[str] = Counter()
        rejected: Counter[str] = Counter()
        seen_feedback: dict[int, tuple] = {}
        base = time.monotonic() + 1
        clock = [base]
        frame_id = 0
        mode_id = "vip" if scenario == "vip_shelter" else "tdm"
        site = None
        if scenario in {"watchtower", "small_hut"}:
            site = brain.cooperative.request_schematic(
                TEAM1, scenario, (101.5, 100.5, GROUND - 2.25), base, facing=(1.0, 0.0),
                builders=tuple(p.id for p in players), requester="physics_gate", ttl=120.0)
            if site is None:
                raise RuntimeError("request API found no feasible plan")

        def strategic(frame, observer, *args, **kwargs):
            return _decision_for(scenario, observer, vip_id, posts)

        with patch.object(time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(brain.mode_policy, "decide", side_effect=strategic):
            for tick in range(math.ceil(seconds * 60)):
                clock[0] = now = base + tick / 60
                if tick % 8 == 0:
                    snapshots = director._snapshot_players()
                    objectives = ()
                    if scenario == "vip_shelter":
                        vip = server.players[vip_id]
                        objectives = (ObjectiveSnapshot("vip", TEAM1, tuple(vip.position), carrier_id=vip_id),
                                      ObjectiveSnapshot("team_anchor", TEAM2, (150.5, 100.5, GROUND - 2.25)),
                                      ObjectiveSnapshot("team_anchor", TEAM1, (60.5, 100.5, GROUND - 2.25)))
                    for player in players:
                        frame_id += 1
                        runtime = director._runtime[player.id]
                        frame = PerceptionFrame(frame_id, 0, 0, world.topology_version, player.id,
                                                runtime.generation, now, mode_id, snapshots,
                                                profile=runtime.profile, objectives=objectives,
                                                mode_phase="ACTIVE", behavior_version="cooperative",
                                                friendly_mischief=False)
                        intent = brain.decide(frame)
                        if intent:
                            runtime.intent = intent
                            roles[intent.debug_role] += 1
                if site is None:
                    site = next((s for s in brain.cooperative.sites.sites.values()), None)
                server.loop_count += 1
                for player in players:
                    if player.id % 6 == server.loop_count % 6:
                        director._apply_motor(director._runtime[player.id], now, .1)
                await server.simulation_runtime._simulate_players()
                server.world_mutations.commit_ready()
                await asyncio.sleep(0)
                for version, changes in sorted(pending.items()):
                    worker.apply(WorldDelta(0, version, tuple(changes)))
                pending.clear()
                for player in players:
                    runtime = director._runtime[player.id]
                    feedback = (runtime.feedback_action_kind, runtime.feedback_action_accepted,
                                runtime.feedback_reason, getattr(runtime, "feedback_request_id", None),
                                getattr(runtime, "feedback_at", None))
                    if feedback != seen_feedback.get(player.id) and runtime.feedback_action_kind in {"build", "build_line"}:
                        seen_feedback[player.id] = feedback
                        (accepted if runtime.feedback_action_accepted else rejected)[
                            f"{runtime.feedback_action_kind}:{runtime.feedback_reason or 'ok'}"] += 1
                    result.distance_walked[player.id] += math.dist(previous[player.id], player.position)
                    previous[player.id] = tuple(player.position)
                    if player.alive and not player.airborne:
                        support = round(player.z + 2.25)
                        result.highest_support[player.id] = min(result.highest_support[player.id], support)
                    if not player.alive or player.wade:
                        result.failures.append(f"bot {player.id} died or entered water at tick {tick}")
                if tick % 30 == 0:
                    result.trace.append({"t": round(tick / 60, 2), "bots": [{
                        "id": p.id, "pos": [round(v, 2) for v in p.position],
                        "role": getattr(director._runtime[p.id].intent, "debug_role", ""),
                        "tool": int(p.tool), "airborne": bool(p.airborne),
                    } for p in players]})
                result.simulated_seconds = (tick + 1) / 60
                if site is not None and not site.active:
                    # A finished overwatch post is then occupied: keep running
                    # until a builder stands on the platform it climbed to.
                    occupant = site.plan.placement.occupant
                    climbed = occupant is not None and any(
                        math.dist(tuple(p.position), occupant) < 1.0 for p in players)
                    if (scenario != "watchtower" or climbed or site.abandoned
                            or now - site.completed_at > 20):
                        break
                if result.failures:
                    break
        if site is None:
            result.failures.append("no schematic site was started")
            return result
        placement = site.plan.placement
        result.schematic = site.name
        result.plan_steps = len(site.plan.steps)
        result.plan_cells = site.plan.cost
        result.plan_estimate_seconds = site.plan.estimated_seconds
        result.site_status = site.status(worker.solid)
        result.lines_built, result.singles_built = site.lines_built, site.singles_built
        result.line_cells, result.single_cells = site.line_cells, site.single_cells
        result.cells_by_builder = dict(site.cells_by_builder)
        result.completion_seconds = round(site.completed_at - site.created_at, 2) if site.completed_at else 0.0
        result.authoritative_cells_present = sum(1 for cell in placement.cells if world.get_solid(*cell))
        result.blocks_spent = {p.id: blocks_before[p.id] - p.blocks for p in players}
        result.final_positions = {p.id: tuple(round(v, 3) for v in p.position) for p in players}
        result.roles = dict(roles.most_common(16))
        result.accepted_actions, result.rejected_actions = dict(accepted), dict(rejected)
        result.metrics = {k: v for k, v in brain.cooperative.sites.metrics.items()}
        result.events = [{**e, "at": round(float(e["at"]) - base, 2)} for e in brain.cooperative.teams.events
                         if e["kind"] in {"tasks_started", "tasks_failed", "tasks_completed",
                                          "schematic_steps_failed"}]
        result.metrics.update({f"construction:{k}": v for k, v in construction_reasons.items()})
        colours: Counter[str] = Counter()
        for cell in placement.cells:
            colours[f"{world.get_color(*cell) & 0xFFFFFF:06x}"] += 1
        result.colours = dict(colours)
        if not site.completed_at:
            result.failures.append(f"site not completed: {site.status(worker.solid)}")
        if result.authoritative_cells_present != len(placement.cells):
            result.failures.append("authoritative map is missing planned cells")
        if sum(result.blocks_spent.values()) != result.authoritative_cells_present:
            result.failures.append("blocks spent differ from cells committed")
        if any(cell for cell in placement.clear if world.get_solid(*cell)):
            result.failures.append("a keep-clear (door/slit/interior) cell was filled")
        if scenario in {"vip_shelter", "small_hut", "watchtower"} and len(site.cells_by_builder) < 2:
            result.failures.append("only one bot contributed to a cooperative build")
        if scenario == "vip_shelter":
            vip = server.players[vip_id]
            if (math.floor(vip.x), math.floor(vip.y)) not in interior_columns(placement):
                result.failures.append("VIP is not inside its shelter")
        if scenario == "watchtower":
            occupant = placement.occupant
            if not any(math.dist(tuple(p.position), occupant) < 1.0 for p in players):
                result.failures.append("no builder climbed onto the tower platform it built")
        return result
    finally:
        if server is not None and subscription is not None:
            server.world_manager.unsubscribe_mutations(subscription)
        random.setstate(rng)
        result.wall_seconds = round(time.perf_counter() - started, 2)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    results = [await simulate(name, seconds=args.seconds) for name in args.scenario or SCENARIOS]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2, default=str) + "\n",
                             encoding="utf-8")
    for r in results:
        print(f"{r.scenario}: {'FAIL' if r.failures else 'PASS'} {r.schematic} "
              f"done_in={r.completion_seconds}s (plan est {r.plan_estimate_seconds}s) "
              f"sim={r.simulated_seconds:.1f}s wall={r.wall_seconds}s cells={r.authoritative_cells_present}/{r.plan_cells} "
              f"lines={r.lines_built}({r.line_cells} cells) singles={r.singles_built}({r.single_cells}) "
              f"builders={r.cells_by_builder} rejected={r.rejected_actions} failures={r.failures}")
    return int(any(r.failures for r in results))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
