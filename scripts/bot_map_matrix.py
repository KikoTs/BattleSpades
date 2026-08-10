"""Accelerated production-bot navigation matrix for every shipped VXL.

Unlike the older policy-only city soak, this harness creates the real server,
mode, bot players, production brain/motor, mutation stream, and native player
physics.  Synthetic monotonic time only removes wall-clock sleeping; movement
and collisions still execute at the server's authoritative 60 Hz.

Examples::

    py -3.12 scripts/bot_map_matrix.py
    py -3.12 scripts/bot_map_matrix.py --map MayanJungle --seed 0 --seconds 40
    py -3.12 scripts/bot_map_matrix.py --seed 0 --seed 7 --json report.json
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import random
import sys
import time
from types import SimpleNamespace
from typing import Iterable
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import shared.constants as C

from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotActionKind,
    MovementAffordance,
    PerceptionFrame,
    VoxelChange,
    WorldDelta,
)
from server.bot_ai.simple_navigation import SimpleVoxelWorld
from server.bot_ai.simple_worker import SimpleBotBrain
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer


SIMULATION_HZ = 60
DECISION_INTERVAL_TICKS = 8
MOTOR_PHASES = 6
TEAM_CONGESTION_RADIUS = 7.0
TEAM_CONGESTION_BOTS = 4
TEAM_CONGESTION_FAR_GOALS = 3
FAR_GOAL_DISTANCE = 40.0
TEAM_PROGRESS_WINDOW_TICKS = 8 * SIMULATION_HZ
# Three blocks in eight seconds distinguishes an actual stationary pile from
# a squad slowly negotiating an authored jump/ledge choke. GreatWall's bots
# each advanced 3.2-5.3 blocks through its central vertical transition while
# the reported Mayan/Bran collapses remained within roughly 0-2.8 blocks.
TEAM_PROGRESS_DISTANCE = 3.0
RECENT_TERRAIN_PROGRESS_TICKS = 2 * SIMULATION_HZ
INDIVIDUAL_PROGRESS_WINDOW_TICKS = 10 * SIMULATION_HZ
INDIVIDUAL_PROGRESS_DISTANCE = 3.0
INDIVIDUAL_TRAP_FAILURE_SECONDS = 8.0


@dataclass(slots=True)
class WorstBotState:
    """Diagnostic snapshot retained at one bot's worst observed condition."""

    bot_id: int = -1
    team: int = -1
    tick: int = 0
    seconds: float = 0.0
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    role: str = "idle"
    affordance: str = "walk"
    direction: tuple[float, float, float] = (0.0, 0.0, 0.0)
    goal: tuple[float, float, float] | None = None
    path: tuple[tuple[float, float, float], ...] = ()
    grounded: bool = False
    wade: bool = False
    body_clear: bool = True
    action_kind: str = "none"
    action_position: tuple[float, float, float] | None = None
    feedback_kind: str = ""
    feedback_accepted: bool = True
    feedback_position: tuple[float, float, float] | None = None
    local_solids: tuple[tuple[int, int, int], ...] = ()


@dataclass(slots=True)
class TeamResult:
    """Progress and congestion measurements for one team."""

    team: int
    bot_ids: tuple[int, ...]
    max_congestion_seconds: float
    congestion_bot_ids: tuple[int, ...]
    congestion_positions: dict[int, tuple[float, float, float]]
    congestion_roles: dict[int, str]
    congestion_displacement: dict[int, float]
    congestion_states: dict[int, WorstBotState]
    max_radius_by_bot: dict[int, float]
    path_distance_by_bot: dict[int, float]


@dataclass(slots=True)
class MapResult:
    """Serializable outcome of one map/seed production simulation."""

    map_name: str
    seed: int
    requested_seconds: float
    simulated_seconds: float = 0.0
    wall_seconds: float = 0.0
    bot_count: int = 0
    unsafe_spawn_ids: tuple[int, ...] = ()
    max_stall_seconds: float = 0.0
    max_water_seconds: float = 0.0
    max_navigation_trap_seconds: float = 0.0
    max_embedded_seconds: float = 0.0
    worst_stall: WorstBotState = field(default_factory=WorstBotState)
    worst_water: WorstBotState = field(default_factory=WorstBotState)
    worst_navigation_trap: WorstBotState = field(default_factory=WorstBotState)
    worst_embedded: WorstBotState = field(default_factory=WorstBotState)
    water_seconds_by_bot: dict[int, float] = field(default_factory=dict)
    water_entries_by_bot: dict[int, int] = field(default_factory=dict)
    traversal_style_by_bot: dict[int, str] = field(default_factory=dict)
    tactical_swim_jump_decisions_by_bot: dict[int, int] = field(
        default_factory=dict
    )
    tactical_swim_jump_roles_by_bot: dict[int, tuple[str, ...]] = field(
        default_factory=dict
    )
    bridge_line_requests_by_bot: dict[int, int] = field(default_factory=dict)
    teams: tuple[TeamResult, ...] = ()
    decision_calls: int = 0
    decision_wall_seconds: float = 0.0
    slowest_decision_ms: float = 0.0
    topology_version: int = 0
    terrain_changes: tuple[tuple[int, int, int, bool], ...] = ()
    trace: tuple[dict[str, object], ...] = ()
    failures: tuple[str, ...] = ()
    error: str = ""

    @property
    def passed(self) -> bool:
        """Return whether the simulation satisfied every invariant."""

        return not self.error and not self.failures


def shipped_maps() -> tuple[str, ...]:
    """Return every checked-in VXL stem, including legacy and Training maps."""

    return tuple(path.stem for path in sorted((ROOT / "maps").glob("*.vxl")))


def _round_position(position: Iterable[float]) -> tuple[float, float, float]:
    values = tuple(round(float(value), 3) for value in position)
    if len(values) != 3:
        raise ValueError(f"expected three position values, got {values!r}")
    return values


def _bot_state(
    *,
    player: object,
    runtime: object,
    worker_world: SimpleVoxelWorld,
    tick: int,
    seconds: float,
    body_clear: bool,
) -> WorstBotState:
    """Capture enough local geometry to reproduce a navigation wedge."""

    intent = getattr(runtime, "intent", None)
    position = tuple(float(value) for value in player.position)
    cell_x = int(math.floor(position[0]))
    cell_y = int(math.floor(position[1]))
    cell_z = int(math.floor(position[2] + 2.25))
    direction = (
        tuple(float(value) for value in intent.movement.direction)
        if intent is not None
        else (0.0, 0.0, 0.0)
    )
    goal = getattr(intent, "debug_goal", None) if intent is not None else None
    return WorstBotState(
        bot_id=int(player.id),
        team=int(player.team),
        tick=int(tick),
        seconds=round(float(seconds), 3),
        position=_round_position(position),
        role=str(getattr(intent, "debug_role", "idle") or "idle"),
        affordance=(
            str(intent.movement.affordance.value)
            if intent is not None
            else "walk"
        ),
        direction=_round_position(direction),
        goal=_round_position(goal) if goal is not None else None,
        path=tuple(
            _round_position(point)
            for point in tuple(getattr(intent, "debug_path", ()) or ())[:4]
        ),
        grounded=bool(getattr(player, "grounded", False)),
        wade=bool(getattr(player, "wade", False)),
        body_clear=bool(body_clear),
        action_kind=(
            str(intent.action.kind.value) if intent is not None else "none"
        ),
        action_position=(
            _round_position(intent.action.position)
            if intent is not None and intent.action.position is not None
            else None
        ),
        feedback_kind=str(getattr(runtime, "feedback_action_kind", "")),
        feedback_accepted=bool(
            getattr(runtime, "feedback_action_accepted", True)
        ),
        feedback_position=(
            _round_position(runtime.feedback_action_position)
            if getattr(runtime, "feedback_action_position", None) is not None
            else None
        ),
        local_solids=tuple(
            (dx, dy, dz)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-2, -1, 0, 1, 2)
            if worker_world.solid(cell_x + dx, cell_y + dy, cell_z + dz)
        ),
    )


def _largest_dense_cohort(players: Iterable[object]) -> tuple[object, ...]:
    """Find the largest same-team horizontal cohort deterministically."""

    candidates = tuple(players)
    largest: tuple[object, ...] = ()
    for anchor in candidates:
        cohort = tuple(
            player
            for player in candidates
            if math.hypot(player.x - anchor.x, player.y - anchor.y)
            <= TEAM_CONGESTION_RADIUS
        )
        if len(cohort) > len(largest):
            largest = cohort
    return largest


async def simulate_map(
    map_name: str,
    *,
    seed: int = 0,
    seconds: float = 40.0,
    bots: int = 12,
    warmup_seconds: float = 12.0,
    trace_bot: int | None = None,
) -> MapResult:
    """Run one deterministic map through production AI and native physics."""

    result = MapResult(
        map_name=str(map_name),
        seed=int(seed),
        requested_seconds=float(seconds),
    )
    started = time.perf_counter()
    random_state = random.getstate()
    random.seed(seed)
    subscription: int | None = None
    server: BattleSpadesServer | None = None
    monotonic_patcher = None
    try:
        config = ServerConfig(
            default_map=map_name,
            default_mode="tdm",
            maps_path=str(ROOT / "maps"),
        )
        config.bots.seed = int(seed)
        config.bots.max_bots = int(bots)
        server = BattleSpadesServer(config)
        if not server.world_manager.load_map(map_name):
            raise RuntimeError(f"could not load map {map_name!r}")
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=SimpleNamespace())
        for bot_index in range(int(bots)):
            bot = await director.add_bot(
                team=TEAM1 if bot_index % 2 == 0 else TEAM2,
                name=f"Matrix{map_name[:12]}{bot_index}",
                class_id=int(C.CLASS_SOLDIER),
            )
            if bot is None:
                raise RuntimeError(f"bot {bot_index} could not spawn")

        players = tuple(director.bots)
        result.bot_count = len(players)
        starts = {int(player.id): tuple(player.position) for player in players}
        result.unsafe_spawn_ids = tuple(
            int(player.id)
            for player in players
            if not server.world_manager.spawn_position_is_safe(player.position)
        )

        worker_world = SimpleVoxelWorld()
        worker_world.load(director._make_map_snapshot(current=False))
        if not worker_world.ready:
            raise RuntimeError("production navigation world did not load")
        brain = SimpleBotBrain(worker_world, decision_hz=8.0)
        pending_deltas: dict[int, list[VoxelChange]] = defaultdict(list)
        terrain_events: deque[tuple[int, int, int, int]] = deque()
        all_terrain_changes: list[tuple[int, int, int, bool]] = []
        trace_rows: list[dict[str, object]] = []

        def remember_delta(x, y, z, solid, color, version) -> None:
            pending_deltas[int(version)].append(
                VoxelChange(int(x), int(y), int(z), bool(solid), int(color))
            )

        subscription = server.world_manager.subscribe_mutations(remember_delta)
        previous = dict(starts)
        max_radius = {int(player.id): 0.0 for player in players}
        path_distance = {int(player.id): 0.0 for player in players}
        position_history = {
            int(player.id): deque(
                (tuple(player.position),),
                maxlen=max(
                    TEAM_PROGRESS_WINDOW_TICKS,
                    INDIVIDUAL_PROGRESS_WINDOW_TICKS,
                ) + 1,
            )
            for player in players
        }
        stall_ticks = {int(player.id): 0 for player in players}
        water_ticks = {int(player.id): 0 for player in players}
        total_water_ticks = {int(player.id): 0 for player in players}
        water_entries = {int(player.id): 0 for player in players}
        traversal_styles: dict[int, str] = {}
        tactical_swim_jumps = {int(player.id): 0 for player in players}
        tactical_swim_jump_roles: dict[int, set[str]] = defaultdict(set)
        bridge_line_requests = {int(player.id): 0 for player in players}
        was_wading = {int(player.id): False for player in players}
        water_origins = {
            int(player.id): tuple(player.position) for player in players
        }
        navigation_trap_ticks = {int(player.id): 0 for player in players}
        embedded_ticks = {int(player.id): 0 for player in players}
        team_congestion_ticks = {TEAM1: 0, TEAM2: 0}
        team_max_congestion = {TEAM1: 0, TEAM2: 0}
        team_congestion_ids: dict[int, tuple[int, ...]] = {TEAM1: (), TEAM2: ()}
        team_congestion_positions: dict[
            int, dict[int, tuple[float, float, float]]
        ] = {TEAM1: {}, TEAM2: {}}
        team_congestion_roles: dict[int, dict[int, str]] = {
            TEAM1: {}, TEAM2: {}
        }
        team_congestion_displacement: dict[int, dict[int, float]] = {
            TEAM1: {}, TEAM2: {}
        }
        team_congestion_states: dict[int, dict[int, WorstBotState]] = {
            TEAM1: {}, TEAM2: {}
        }
        base = time.monotonic() + 1.0
        simulated_clock = [base]
        # All production cooldowns must observe the same accelerated clock as
        # the bot frames. Previously only ``created_at`` advanced at 60 Hz,
        # while combat/spade authority read real monotonic time; fast maps then
        # rejected valid digs and contaminated later matrix outcomes.
        monotonic_patcher = patch.object(
            time,
            "monotonic",
            side_effect=lambda: simulated_clock[0],
        )
        monotonic_patcher.start()
        frame_id = 0
        total_ticks = max(1, int(round(float(seconds) * SIMULATION_HZ)))
        warmup_ticks = max(0, int(round(float(warmup_seconds) * SIMULATION_HZ)))

        for tick in range(total_ticks):
            now = base + tick / SIMULATION_HZ
            simulated_clock[0] = now
            if tick % DECISION_INTERVAL_TICKS == 0:
                snapshots = director._snapshot_players()
                entities = director._snapshot_entities()
                objectives = director._snapshot_objectives()
                decision_started = time.perf_counter()
                for player in players:
                    runtime = director._runtime[int(player.id)]
                    frame_id += 1
                    call_started = time.perf_counter()
                    frame = PerceptionFrame(
                            frame_id=frame_id,
                            map_epoch=0,
                            mode_epoch=0,
                            topology_version=server.world_manager.topology_version,
                            observer_id=int(player.id),
                            observer_generation=runtime.generation,
                            created_at=now,
                            mode_id="tdm",
                            players=snapshots,
                            profile=runtime.profile,
                            entities=entities,
                            objectives=objectives,
                            mode_phase=director._mode_phase(),
                        )
                    intent = brain.decide(frame)
                    traversal_styles[int(player.id)] = (
                        brain._traversal_personality(
                            frame,
                            next(
                                snapshot
                                for snapshot in snapshots
                                if int(snapshot.player_id) == int(player.id)
                            ),
                        ).style.value
                    )
                    call_seconds = time.perf_counter() - call_started
                    result.slowest_decision_ms = max(
                        result.slowest_decision_ms,
                        call_seconds * 1000.0,
                    )
                    result.decision_calls += 1
                    if intent is not None:
                        runtime.intent = intent
                        if (
                            intent.movement.affordance
                            is MovementAffordance.SWIM
                            and intent.movement.jump
                        ):
                            tactical_swim_jumps[int(player.id)] += 1
                            tactical_swim_jump_roles[int(player.id)].add(
                                str(intent.debug_role)
                            )
                        if intent.action.kind is BotActionKind.BUILD_LINE:
                            bridge_line_requests[int(player.id)] += 1
                result.decision_wall_seconds += time.perf_counter() - decision_started

            server.loop_count += 1
            motor_phase = server.loop_count % MOTOR_PHASES
            for player in players:
                if int(player.id) % MOTOR_PHASES == motor_phase:
                    director._apply_motor(
                        director._runtime[int(player.id)],
                        now,
                        MOTOR_PHASES / SIMULATION_HZ,
                    )
            await server.simulation_runtime._simulate_players()
            server.world_mutations.commit_ready()
            server.prefab_actions.tick()
            if trace_bot is not None and tick % 15 == 0:
                traced = next(
                    (
                        player for player in players
                        if int(player.id) == int(trace_bot)
                    ),
                    None,
                )
                if traced is not None:
                    runtime = director._runtime[int(traced.id)]
                    intent = runtime.intent
                    state = brain._states.get(
                        (int(traced.id), int(runtime.generation))
                    )
                    route_step = (
                        state.route[state.route_index]
                        if state is not None
                        and state.route_index < len(state.route)
                        else None
                    )
                    trace_rows.append({
                        "tick": int(tick),
                        "time": round(float(now - base), 3),
                        "position": _round_position(traced.position),
                        "velocity": _round_position((
                            float(getattr(traced, "vx", 0.0)),
                            float(getattr(traced, "vy", 0.0)),
                            float(getattr(traced, "vz", 0.0)),
                        )),
                        "wade": bool(traced.wade),
                        "grounded": bool(traced.grounded),
                        "role": str(
                            getattr(intent, "debug_role", "idle") or "idle"
                        ),
                        "affordance": (
                            str(intent.movement.affordance.value)
                            if intent is not None else "walk"
                        ),
                        "direction": (
                            _round_position(intent.movement.direction)
                            if intent is not None else (0.0, 0.0, 0.0)
                        ),
                        "route_index": (
                            int(state.route_index) if state is not None else -1
                        ),
                        "route_step": (
                            {
                                "waypoint": _round_position(route_step.waypoint),
                                "affordance": route_step.affordance.value,
                                "breach_target": (
                                    tuple(route_step.breach.target_cell)
                                    if route_step.breach is not None else None
                                ),
                            }
                            if route_step is not None else None
                        ),
                        "waypoint_stalled_for": (
                            round(float(now - state.waypoint_progress_at), 3)
                            if state is not None else 0.0
                        ),
                        "blocked_edges": (
                            tuple(
                                (tuple(source), tuple(target))
                                for source, target in state.blocked_edges
                            )
                            if state is not None else ()
                        ),
                        "topology": int(
                            server.world_manager.topology_version
                        ),
                    })
            for version, changes in sorted(pending_deltas.items()):
                worker_world.apply(
                    WorldDelta(
                        map_epoch=0,
                        topology_version=int(version),
                        changed_cells=tuple(changes),
                    )
                )
                terrain_events.extend(
                    (tick, int(change.x), int(change.y), int(change.z))
                    for change in changes
                )
                all_terrain_changes.extend(
                    (
                        int(change.x),
                        int(change.y),
                        int(change.z),
                        bool(change.solid),
                    )
                    for change in changes
                )
            pending_deltas.clear()
            while (
                terrain_events
                and terrain_events[0][0]
                < tick - RECENT_TERRAIN_PROGRESS_TICKS
            ):
                terrain_events.popleft()

            for player in players:
                bot_id = int(player.id)
                runtime = director._runtime[bot_id]
                intent = runtime.intent
                alive = bool(player.alive and player.spawned)
                requested = bool(
                    alive
                    and intent is not None
                    and intent.expires_at > now
                    and math.hypot(
                        intent.movement.direction[0],
                        intent.movement.direction[1],
                    )
                    > 0.1
                )
                moved = math.dist(previous[bot_id], player.position)
                moved_xy = math.hypot(
                    float(player.x) - float(previous[bot_id][0]),
                    float(player.y) - float(previous[bot_id][1]),
                )
                path_distance[bot_id] += moved
                max_radius[bot_id] = max(
                    max_radius[bot_id], math.dist(starts[bot_id], player.position)
                )
                nearby_water_terrain_progress = any(
                    math.hypot(
                        float(change_x) - float(player.x),
                        float(change_y) - float(player.y),
                    ) <= 2.5
                    for _change_tick, change_x, change_y, _change_z
                    in terrain_events
                )
                stall_ticks[bot_id] = (
                    stall_ticks[bot_id] + 1
                    if requested and moved_xy < 1e-5
                    else 0
                )
                if alive and bool(player.wade):
                    total_water_ticks[bot_id] += 1
                    if not was_wading[bot_id]:
                        water_entries[bot_id] += 1
                    if water_ticks[bot_id] == 0:
                        water_origins[bot_id] = tuple(player.position)
                    water_ticks[bot_id] += 1
                    # Long swims are valid on CastleWars and other sea maps.
                    # Measure a trap as failure to gain four blocks, then move
                    # the progress origin forward for the next segment.
                    if (
                        math.hypot(
                            float(player.x) - float(water_origins[bot_id][0]),
                            float(player.y) - float(water_origins[bot_id][1]),
                        ) >= 4.0
                        or nearby_water_terrain_progress
                    ):
                        # A swimmer carving/building its bank is making
                        # concrete world progress even before its body moves
                        # four cells. Keep the trap gate focused on inert
                        # bobbing/cycling; the individual navigation gate still
                        # requires movement once terrain activity stops.
                        water_ticks[bot_id] = 0
                        water_origins[bot_id] = tuple(player.position)
                else:
                    water_ticks[bot_id] = 0
                was_wading[bot_id] = bool(alive and player.wade)
                body_clear = bool(
                    not alive
                    or server.world_manager._player_body_is_clear(
                        float(player.x), float(player.y), float(player.z)
                    )
                )
                embedded_ticks[bot_id] = (
                    embedded_ticks[bot_id] + 1 if alive and not body_clear else 0
                )
                if stall_ticks[bot_id] / SIMULATION_HZ > result.max_stall_seconds:
                    result.max_stall_seconds = stall_ticks[bot_id] / SIMULATION_HZ
                    result.worst_stall = _bot_state(
                        player=player,
                        runtime=runtime,
                        worker_world=worker_world,
                        tick=tick,
                        seconds=result.max_stall_seconds,
                        body_clear=body_clear,
                    )
                if water_ticks[bot_id] / SIMULATION_HZ > result.max_water_seconds:
                    result.max_water_seconds = water_ticks[bot_id] / SIMULATION_HZ
                    result.worst_water = _bot_state(
                        player=player,
                        runtime=runtime,
                        worker_world=worker_world,
                        tick=tick,
                        seconds=result.max_water_seconds,
                        body_clear=body_clear,
                    )
                if embedded_ticks[bot_id] / SIMULATION_HZ > result.max_embedded_seconds:
                    result.max_embedded_seconds = embedded_ticks[bot_id] / SIMULATION_HZ
                    result.worst_embedded = _bot_state(
                        player=player,
                        runtime=runtime,
                        worker_world=worker_world,
                        tick=tick,
                        seconds=result.max_embedded_seconds,
                        body_clear=body_clear,
                    )
                previous[bot_id] = tuple(player.position)
                position_history[bot_id].append(tuple(player.position))

                role = str(getattr(intent, "debug_role", "") or "")
                action_kind = (
                    intent.action.kind
                    if intent is not None
                    else BotActionKind.NONE
                )
                rolling_displacement = (
                    math.hypot(
                        float(player.x)
                        - float(position_history[bot_id][0][0]),
                        float(player.y)
                        - float(position_history[bot_id][0][1]),
                    )
                    if len(position_history[bot_id])
                    >= INDIVIDUAL_PROGRESS_WINDOW_TICKS
                    else math.inf
                )
                nearby_individual_terrain_progress = any(
                    math.hypot(
                        float(change_x) - float(player.x),
                        float(change_y) - float(player.y),
                    ) <= TEAM_CONGESTION_RADIUS + 2.0
                    for _change_tick, change_x, change_y, _change_z
                    in terrain_events
                )
                navigation_owned = bool(
                    alive
                    and intent is not None
                    and (
                        requested
                        or bool(player.wade)
                        or role == "water_no_route"
                    )
                    and action_kind
                    not in {BotActionKind.FIRE, BotActionKind.ORIENTED}
                    and not role.startswith("combat_visible")
                    and not role.startswith("combat_oriented")
                )
                individually_trapped = bool(
                    navigation_owned
                    and rolling_displacement <= INDIVIDUAL_PROGRESS_DISTANCE
                    and not nearby_individual_terrain_progress
                )
                navigation_trap_ticks[bot_id] = (
                    navigation_trap_ticks[bot_id] + 1
                    if individually_trapped
                    else 0
                )
                trap_seconds = navigation_trap_ticks[bot_id] / SIMULATION_HZ
                if trap_seconds > result.max_navigation_trap_seconds:
                    result.max_navigation_trap_seconds = trap_seconds
                    result.worst_navigation_trap = _bot_state(
                        player=player,
                        runtime=runtime,
                        worker_world=worker_world,
                        tick=tick,
                        seconds=trap_seconds,
                        body_clear=body_clear,
                    )

            if tick >= warmup_ticks:
                for team in (TEAM1, TEAM2):
                    team_players = tuple(
                        player
                        for player in players
                        if int(player.team) == team
                        and bool(player.alive and player.spawned)
                    )
                    dense = _largest_dense_cohort(team_players)
                    far = tuple(
                        player
                        for player in dense
                        if (
                            director._runtime[int(player.id)].intent is not None
                            and director._runtime[int(player.id)].intent.debug_goal
                            is not None
                            and math.hypot(
                                director._runtime[int(player.id)].intent.debug_goal[0]
                                - player.x,
                                director._runtime[int(player.id)].intent.debug_goal[1]
                                - player.y,
                            )
                            >= FAR_GOAL_DISTANCE
                        )
                    )
                    stagnant = tuple(
                        player
                        for player in far
                        if (
                            len(position_history[int(player.id)])
                            >= TEAM_PROGRESS_WINDOW_TICKS
                            and math.hypot(
                                float(player.x)
                                - float(position_history[int(player.id)][0][0]),
                                float(player.y)
                                - float(position_history[int(player.id)][0][1]),
                            )
                            <= TEAM_PROGRESS_DISTANCE
                        )
                    )
                    nearby_terrain_progress = any(
                        any(
                            math.hypot(
                                float(change_x) - float(player.x),
                                float(change_y) - float(player.y),
                            )
                            <= TEAM_CONGESTION_RADIUS + 2.0
                            for player in dense
                        )
                        for _change_tick, change_x, change_y, _change_z
                        in terrain_events
                    )
                    congested = bool(
                        len(dense) >= TEAM_CONGESTION_BOTS
                        and len(far) >= TEAM_CONGESTION_FAR_GOALS
                        and len(stagnant) >= TEAM_CONGESTION_FAR_GOALS
                        and not nearby_terrain_progress
                    )
                    team_congestion_ticks[team] = (
                        team_congestion_ticks[team] + 1 if congested else 0
                    )
                    if team_congestion_ticks[team] > team_max_congestion[team]:
                        team_max_congestion[team] = team_congestion_ticks[team]
                        team_congestion_ids[team] = tuple(
                            int(player.id) for player in dense
                        )
                        team_congestion_positions[team] = {
                            int(player.id): _round_position(player.position)
                            for player in dense
                        }
                        team_congestion_roles[team] = {
                            int(player.id): str(
                                getattr(
                                    director._runtime[int(player.id)].intent,
                                    "debug_role",
                                    "idle",
                                )
                                or "idle"
                            )
                            for player in dense
                        }
                        team_congestion_displacement[team] = {
                            int(player.id): round(
                                math.hypot(
                                    float(player.x)
                                    - float(
                                        position_history[int(player.id)][0][0]
                                    ),
                                    float(player.y)
                                    - float(
                                        position_history[int(player.id)][0][1]
                                    ),
                                ),
                                3,
                            )
                            for player in dense
                        }
                        team_congestion_states[team] = {
                            int(player.id): _bot_state(
                                player=player,
                                runtime=director._runtime[int(player.id)],
                                worker_world=worker_world,
                                tick=tick,
                                seconds=team_max_congestion[team]
                                / SIMULATION_HZ,
                                body_clear=server.world_manager._player_body_is_clear(
                                    float(player.x),
                                    float(player.y),
                                    float(player.z),
                                ),
                            )
                            for player in dense
                        }

        result.simulated_seconds = total_ticks / SIMULATION_HZ
        result.topology_version = int(server.world_manager.topology_version)
        result.terrain_changes = tuple(all_terrain_changes)
        result.trace = tuple(trace_rows)
        result.teams = tuple(
            TeamResult(
                team=team,
                bot_ids=tuple(
                    int(player.id) for player in players if int(player.team) == team
                ),
                max_congestion_seconds=team_max_congestion[team] / SIMULATION_HZ,
                congestion_bot_ids=team_congestion_ids[team],
                congestion_positions=team_congestion_positions[team],
                congestion_roles=team_congestion_roles[team],
                congestion_displacement=team_congestion_displacement[team],
                congestion_states=team_congestion_states[team],
                max_radius_by_bot={
                    int(player.id): round(max_radius[int(player.id)], 3)
                    for player in players
                    if int(player.team) == team
                },
                path_distance_by_bot={
                    int(player.id): round(path_distance[int(player.id)], 3)
                    for player in players
                    if int(player.team) == team
                },
            )
            for team in (TEAM1, TEAM2)
        )
        result.water_seconds_by_bot = {
            bot_id: round(ticks / SIMULATION_HZ, 3)
            for bot_id, ticks in total_water_ticks.items()
        }
        result.water_entries_by_bot = dict(water_entries)
        result.traversal_style_by_bot = dict(traversal_styles)
        result.tactical_swim_jump_decisions_by_bot = dict(
            tactical_swim_jumps
        )
        result.tactical_swim_jump_roles_by_bot = {
            bot_id: tuple(sorted(roles))
            for bot_id, roles in tactical_swim_jump_roles.items()
        }
        result.bridge_line_requests_by_bot = dict(bridge_line_requests)

        failures: list[str] = []
        if result.unsafe_spawn_ids:
            failures.append(f"unsafe spawns: {result.unsafe_spawn_ids}")
        if result.max_stall_seconds >= 10.0:
            failures.append(
                f"requested-motion stall {result.max_stall_seconds:.2f}s "
                f"(bot {result.worst_stall.bot_id})"
            )
        if result.max_water_seconds >= 10.0:
            failures.append(
                f"water trap {result.max_water_seconds:.2f}s "
                f"(bot {result.worst_water.bot_id})"
            )
        tactical_swim_jumpers = tuple(
            bot_id
            for bot_id, decisions in tactical_swim_jumps.items()
            if decisions > 0
        )
        if tactical_swim_jumpers:
            failures.append(
                "tactical jump requested during SWIM: "
                f"{tactical_swim_jumpers} "
                f"roles={result.tactical_swim_jump_roles_by_bot}"
            )
        if (
            result.max_navigation_trap_seconds
            >= INDIVIDUAL_TRAP_FAILURE_SECONDS
        ):
            failures.append(
                "navigation trap "
                f"{result.max_navigation_trap_seconds:.2f}s "
                f"(bot {result.worst_navigation_trap.bot_id})"
            )
        # ``_player_body_is_clear`` is deliberately more conservative than
        # native crouch/tunnel collision and therefore remains diagnostic.
        # Initial life anchors use the exact production spawn-safety gate
        # above; persistent inability to move is independently rejected by
        # the requested-motion stall oracle.
        for team in result.teams:
            if team.max_congestion_seconds >= 8.0:
                failures.append(
                    f"team {team.team} congestion "
                    f"{team.max_congestion_seconds:.2f}s {team.congestion_bot_ids}"
                )
            moving_bots = sum(
                distance >= 20.0 for distance in team.max_radius_by_bot.values()
            )
            if result.simulated_seconds >= 30.0 and moving_bots < 4:
                failures.append(
                    f"team {team.team} insufficient progress: "
                    f"{moving_bots}/{len(team.bot_ids)} reached 20 blocks"
                )
        result.failures = tuple(failures)
    except (AssertionError, OSError, RuntimeError, TypeError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if monotonic_patcher is not None:
            monotonic_patcher.stop()
        if server is not None and subscription is not None:
            server.world_manager.unsubscribe_mutations(subscription)
        random.setstate(random_state)
        result.wall_seconds = round(time.perf_counter() - started, 3)
        result.decision_wall_seconds = round(result.decision_wall_seconds, 6)
        result.slowest_decision_ms = round(result.slowest_decision_ms, 3)
    return result


async def run_matrix(
    maps: Iterable[str],
    *,
    seeds: Iterable[int],
    seconds: float,
    bots: int,
    trace_bot: int | None = None,
) -> tuple[MapResult, ...]:
    """Run map/seed cases sequentially to avoid cross-server global state."""

    results: list[MapResult] = []
    for map_name in maps:
        for seed in seeds:
            result = await simulate_map(
                map_name,
                seed=int(seed),
                seconds=seconds,
                bots=bots,
                trace_bot=trace_bot,
            )
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            team_summary = ", ".join(
                f"t{team.team}:crowd={team.max_congestion_seconds:.1f}s"
                for team in result.teams
            )
            print(
                f"{status} map={result.map_name} seed={result.seed} "
                f"sim={result.simulated_seconds:.1f}s wall={result.wall_seconds:.1f}s "
                f"stall={result.max_stall_seconds:.1f}s "
                f"water={result.max_water_seconds:.1f}s "
                f"embedded={result.max_embedded_seconds:.1f}s {team_summary} "
                f"failures={result.failures or result.error or '-'}",
                flush=True,
            )
    return tuple(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map",
        action="append",
        dest="maps",
        help="map stem to run; repeat for several (default: every shipped VXL)",
    )
    parser.add_argument(
        "--seed",
        action="append",
        type=int,
        dest="seeds",
        help="deterministic seed; repeat for several (default: 0)",
    )
    parser.add_argument("--seconds", type=float, default=40.0)
    parser.add_argument("--bots", type=int, default=12)
    parser.add_argument("--trace-bot", type=int)
    parser.add_argument("--json", type=Path, help="write the full diagnostic report")
    args = parser.parse_args()

    available = shipped_maps()
    maps = tuple(args.maps or available)
    unknown = tuple(map_name for map_name in maps if map_name not in available)
    if unknown:
        parser.error(f"unknown shipped map(s): {', '.join(unknown)}")
    if args.seconds <= 0.0:
        parser.error("--seconds must be positive")
    if not 2 <= args.bots <= 32:
        parser.error("--bots must be between 2 and 32")

    results = asyncio.run(
        run_matrix(
            maps,
            seeds=tuple(args.seeds or (0,)),
            seconds=float(args.seconds),
            bots=int(args.bots),
            trace_bot=args.trace_bot,
        )
    )
    document = {
        "schema": 1,
        "maps": len(maps),
        "cases": len(results),
        "passed": sum(result.passed for result in results),
        "failed": sum(not result.passed for result in results),
        "results": [asdict(result) for result in results],
    }
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(document, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(
        "SUMMARY " + json.dumps({key: document[key] for key in document if key != "results"}),
        flush=True,
    )
    return 1 if document["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
