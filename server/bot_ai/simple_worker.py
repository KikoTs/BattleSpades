"""Small deterministic bot worker used by the production supervisor.

This replaces the legacy behavior-tree worker with a single ownership loop:

``select goal -> plan segment -> execute edge -> verify progress``.

Local edge failures are excluded temporarily. A bounded, incremental surface
search supplies longer detours when local segments stop advancing the goal.
Construction remains part of traversal and uses ordinary server actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import logging
import math
import queue
import time
from typing import Iterable

import shared.constants as C
from server.dig_profiles import DigProfile, best_navigation_dig_profile
from server.game_constants import (
    CAT_SNIPER,
    SPADE_TOOL_IDS,
    WEAPON_PROFILES,
)
from server.projectiles import BASE_GRAVITY, PROJECTILE_SPECS

from .combat_profiles import envelope_for
from .combat_tactics import (
    Engagement,
    mix as tactical_mix,
    aim_offset as _visible_aim_offset,
    find_cover,
    fire_blocked,
    hazard_escape,
    relocation_heading,
    stationary_relocation,
)
from .locomotion import CombatFootwork
from .path_following import (
    SAFE_DROP,
    edge_axis,
    gaze_waypoint,
    held_index,
    takeoff_alignment,
    lookahead,
    passed_index,
    remaining_distance,
    run_end,
    runs_off,
    straight_walkable,
)
from .cooperative_behavior import CooperativeBehavior
from .team_tasks import TacticalOrder
from .messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    BotIntentPriority,
    BotProfile,
    LookIntent,
    MapSnapshot,
    MovementAffordance,
    MovementIntent,
    PerceptionFrame,
    PlayerSnapshot,
    Vector3,
    WorkerHeartbeat,
    WorkerShutdown,
    WorldDelta,
)
from .policies import (
    ModeBotDecision,
    ModePolicyMemory,
    ModeBotPosture,
    mode_decision_allows_combat,
    mode_objective_committed,
)
from .simple_navigation import RoutePlan, RouteStep, SimpleVoxelWorld
from .surface_corridor import SurfaceCorridorSearch
from .prefab_policy import bot_prefab_block_count, bot_prefab_is_suitable
from .snapshot_transport import MapSnapshotAssembler, SnapshotTransportError


logger = logging.getLogger(__name__)

_VISUAL_RANGE = 160.0
_CONTACT_SECONDS = 4.0
_CONTACT_GAZE_SECONDS = 2.0
_CORRIDOR_WINDOW = 16
_INTENT_TTL_SECONDS = 0.4
_WAYPOINT_RADIUS = 0.9
_WAYPOINT_STALL_SECONDS = 1.75
_GOAL_STALL_SECONDS = 6.0
_STUCK_REPLAN_SECONDS = 3.0
_NAVIGATION_PROGRESS_SECONDS = 2.5
_NAVIGATION_PROGRESS_DISTANCE = 0.5
_NAVIGATION_WINDOW_SECONDS = 5.0
_NAVIGATION_WINDOW_DISTANCE = 4.0
_NAVIGATION_REVISIT_SECONDS = 8.0
_NAVIGATION_VISITED_CELLS = 128
_WATER_ESCAPE_PROGRESS_SECONDS = 4.5
_WATER_ESCAPE_PROGRESS_DISTANCE = 4.0
_BLOCKED_EDGE_SECONDS = 60.0
_JUMP_BLOCKED_EDGE_SECONDS = 12.0
_WATER_BLOCKED_EDGE_SECONDS = 20.0
_WATER_GOAL_RELEASE_RADIUS = 4.0
_DRY_BANK_RELEASE_VERTICAL = 1.5
_MAX_BLOCKED_EDGES = 8
_TEAM_ORIENTED_SPACING_SECONDS = 1.0
_COMBAT_STRAFE_STALL_SECONDS = 1.0
_COMBAT_PROGRESS_DISTANCE = 0.4
_CROWD_PERSONAL_SPACE = 1.5
_CROWD_VERTICAL_TOLERANCE = 2.25
_CROWD_REPULSION_WEIGHT = 3.0
_CROWD_DETOUR_RADIUS = 7.0
_CROWD_DETOUR_BOTS = 4
_CROWD_DETOUR_SECONDS = 3.0
_CROWD_DETOUR_PROGRESS = 1.5
_CROWD_DETOUR_MIN_GOAL_DISTANCE = 40.0
_CROWD_BLOCKED_EDGE_SECONDS = 20.0
_CROWD_DETOUR_ROUTE_SECONDS = 20.0
_BREACH_RESERVATION_RADIUS = 2.25
_BREACH_QUEUE_SPACING = 1.15
_BREACH_YIELD_REPLAN_SECONDS = 1.25
_TEAM_LANE_SPACING = 8.0
_TEAM_LANE_MAX_OFFSET = 20.0
_TEAM_LANE_MIN_GOAL_DISTANCE = 96.0
_TEAM_LANE_SEGMENT_DISTANCE = 56.0
_DRY_DETOUR_DISTANCE = 36.0
_DRY_DETOUR_SECONDS = 12.0
_DRY_DETOURS_BEFORE_SWIM = 3
_BRIDGE_BUILD_INTERVAL = 0.8
_ZOMBIE_CLASSES = frozenset(
    {
        int(C.CLASS_ZOMBIE),
        int(C.CLASS_FAST_ZOMBIE),
        int(C.CLASS_JUMP_ZOMBIE),
    }
)
_HUNT_ROLES = frozenset({"chase_last_seen", "investigate_sound"})
_CASUAL_ERRANDS = _HUNT_ROLES | {
    "team_assault_enemy_side", "team_assault_search", "tdm_flank_approach",
    "tdm_squad_support", "tdm_overwatch_lane", "squad_advance"}
_ERRAND_COMMITMENT_SECONDS = 3.0
_ROCKET_TOOLS = frozenset((int(C.RPG_TOOL), int(C.RPG2_TOOL)))
_ORIENTED_ATTACK_TOOLS = frozenset(int(tool) for tool in PROJECTILE_SPECS) - {
    int(C.DYNAMITE_TOOL),
    int(C.LANDMINE_TOOL),
    int(C.C4_TOOL),
}
_ORIENTED_SPEEDS = {
    int(C.GRENADE_TOOL): float(getattr(C, "GRENADE_THROW_SPEED", 50.0)),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): float(
        getattr(C, "CLASSIC_GRENADE_THROW_SPEED", 35.0)
    ),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): float(
        getattr(C, "ANTIPERSONNEL_GRENADE_THROW_SPEED", 50.0)
    ),
    int(getattr(C, "MOLOTOV_TOOL", 33)): float(
        getattr(C, "MOLOTOV_THROW_SPEED", 40.0)
    ),
    int(C.RPG_TOOL): float(getattr(C, "ROCKET_SPEED", 75.0)),
    int(C.RPG2_TOOL): float(getattr(C, "ROCKET2_SPEED", 150.0)),
    int(C.DRILLGUN_TOOL): float(getattr(C, "DRILL_FLYING_SPEED", 40.0)),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): float(
        getattr(C, "SNOWBALL_SPEED", 50.0)
    ),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): 40.0,
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): float(
        getattr(C, "GRENADE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): 50.0,
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): float(
        getattr(C, "MINE_LAUNCHER_PROJECTILE_SPEED", 75.0)
    ),
}

NodeKey = tuple[int, int, int]
EdgeKey = tuple[NodeKey, NodeKey]


@dataclass(frozen=True, slots=True)
class _Goal:
    """One exclusive movement owner."""

    key: tuple[object, ...]
    position: Vector3
    role: str
    arrival_radius: float
    sprint: bool
    watch_position: Vector3 | None = None


class _TraversalStyle(str, Enum):
    """Stable route temperament assigned across one team roster."""

    DRY = "dry"
    SWIM = "swim"
    BRIDGE = "bridge"


@dataclass(frozen=True, slots=True)
class _TraversalPersonality:
    """Identity-stable navigation preferences for one bot."""

    style: _TraversalStyle
    detour_sign: int
    forward_bias: float


@dataclass(slots=True)
class _BotState:
    """All persistent state for one concrete bot life."""

    map_epoch: int
    mode_epoch: int
    life_id: int
    next_decision_at: float = 0.0
    goal: _Goal | None = None
    route: tuple[RouteStep, ...] = ()
    route_index: int = 0
    route_topology_version: int = -1
    waypoint_best_distance: float = math.inf
    waypoint_progress_at: float = 0.0
    goal_best_distance: float = math.inf
    goal_progress_at: float = 0.0
    blocked_edges: dict[EdgeKey, float] = field(default_factory=dict)
    blocked_edge_since: dict[EdgeKey, float] = field(default_factory=dict)
    contact_id: int = -1
    contact_generation: int = -1
    contact_position: Vector3 | None = None
    contact_until: float = 0.0
    acquired_at: float = 0.0
    flight_step: RouteStep | None = None
    flight_source: Vector3 | None = None
    flight_started_at: float = 0.0
    flight_departed: bool = False
    flight_watch: Vector3 | None = None
    next_flight_at: float = 0.0
    next_combat_hop_at: float = 0.0
    escape_attempts: int = 0
    escape_search: tuple[tuple[object, ...], tuple[tuple[object, ...], ...], int] | None = None
    planning_context: tuple[object, ...] | None = None
    planning_results: dict[tuple[object, ...], RoutePlan] = field(default_factory=dict)
    planning_wait_at: float | None = None
    next_oriented_at: float = 0.0
    next_support_at: float = 0.0
    next_deploy_at: float = 0.0
    next_cover_at: float = 0.0
    next_hop_at: float = 0.0
    last_hop_damage_at: float = 0.0
    breach_key: tuple[object, ...] | None = None
    breach_started_at: float = 0.0
    next_breach_at: float = 0.0
    yielded_breach_edge: EdgeKey | None = None
    yielded_breach_started_at: float = 0.0
    next_water_build_at: float = 0.0
    water_step_key: tuple[int, int, int, str] | None = None
    water_landing_step: RouteStep | None = None
    water_breach_target: tuple[int, int, int] | None = None
    water_best_distance: float = math.inf
    water_progress_at: float = 0.0
    water_recovery: bool = False
    water_committed: bool = False
    water_goal_reached: bool = False
    water_escape_position: Vector3 | None = None
    water_escape_at: float = 0.0
    water_search_heading: Vector3 | None = None
    water_search_until: float = 0.0
    water_search_origin: Vector3 | None = None
    water_failed_shores: list[tuple[float, frozenset[EdgeKey]]] = field(default_factory=list)
    water_last_bank: tuple[float, Vector3] | None = None
    combat_progress_position: Vector3 | None = None
    combat_progress_at: float = 0.0
    combat_last_at: float = 0.0
    combat_stall_stage: int = 0
    combat_wants_movement: bool = True
    footwork: CombatFootwork = field(default_factory=CombatFootwork)
    engagement: Engagement = field(default_factory=Engagement)
    carried_entity_id: int = -1
    can_shoot: bool = True
    crowd_anchor: Vector3 | None = None
    crowd_progress_at: float = 0.0
    crowd_detour_goal: Vector3 | None = None
    crowd_detour_until: float = 0.0
    dry_detour_goal: Vector3 | None = None
    dry_detour_until: float = 0.0
    dry_route_failures: int = 0
    navigation_progress_position: Vector3 | None = None
    navigation_progress_at: float = 0.0
    navigation_window_position: Vector3 | None = None
    navigation_window_at: float = 0.0
    navigation_previous_position: Vector3 | None = None
    navigation_visited: dict[NodeKey, float] = field(default_factory=dict)
    navigation_coverage_at: float = 0.0
    navigation_coverage_last_at: float = 0.0
    navigation_coverage_goal: tuple[object, ...] | None = None
    navigation_coverage_target: Vector3 | None = None
    navigation_coverage_best: float = math.inf
    escape_goal: Vector3 | None = None
    escape_allow_water: bool = False
    escape_until: float = 0.0
    escape_retry_at: float = 0.0
    corridor_search: SurfaceCorridorSearch | None = None
    corridor: tuple[Vector3, ...] = ()
    corridor_index: int = 0
    corridor_reach: float = 8.0  # blocks of corridor one detailed plan is sent along
    corridor_join_index: int = 0
    corridor_yield_local: bool = False
    corridor_retry_at: float = 0.0
    corridor_rejected_goal: Vector3 | None = None
    corridor_rejected_until: float = 0.0
    corridor_failed_goal: Vector3 | None = None
    support_retry_at: float = 0.0
    support_rejected_goal: Vector3 | None = None
    support_progress_anchor: Vector3 | None = None
    support_best_distance: float = math.inf
    support_no_progress_time: float = 0.0
    support_last_decision_at: float | None = None
    support_failed_goal: Vector3 | None = None
    support_failure_at: float = 0.0
    support_escape_attempted: bool = False
    patrol_index: int = 0
    patrol_position: Vector3 | None = None
    patrol_arrived_at: float | None = None
    patrol_started_at: float = 0.0
    guard_retry_at: float = 0.0
    guard_target: Vector3 | None = None
    guard_best_distance: float = math.inf
    guard_progress_at: float = 0.0
    goal_since: float = 0.0
    dead_end: bool = False
    short_plans: int = 0
    short_plan_at: float = 0.0
    dead_end_retry_at: float = 0.0
    lookahead_active: bool = False
    lookahead_off_until: float = 0.0
    travel_gaze: Vector3 | None = None
    travel_gaze_at: float = 0.0
    step_note: str = ""  # diagnostics: how the current route step is being taken
    lookahead_target: Vector3 | None = None
    travel_heading: Vector3 | None = None
    next_extension_at: float = 0.0
    flank_decided: bool = False
    flank_point: Vector3 | None = None
    flank_started: bool = False
    flank_until: float = 0.0


class SimpleBotBrain:
    """One-goal bot controller with bounded navigation and fair combat LOS."""

    def __init__(
        self,
        world: SimpleVoxelWorld,
        *,
        decision_hz: float = 8.0,
    ) -> None:
        self.world = world
        self._decision_interval = 1.0 / max(1.0, float(decision_hz))
        self._states: dict[tuple[int, int], _BotState] = {}
        self._team_oriented_ready_at: dict[int, float] = {}
        self._map_epoch = -1
        self.cooperative = CooperativeBehavior(world)
        self.mode_policy = ModePolicyMemory()

    def reset_for_map(self, map_epoch: int) -> None:
        """Discard every controller and route from the previous map."""

        normalized = int(map_epoch)
        if normalized == self._map_epoch:
            return
        self._map_epoch = normalized
        self._states.clear()
        self._team_oriented_ready_at.clear()
        self.cooperative.reset()
        self.mode_policy.reset()

    def reset_bot(self, player_id: int, generation: int) -> None:
        """Discard a failed controller without interrupting healthy teammates."""

        self._states.pop((int(player_id), int(generation)), None)
        self.cooperative.forget(int(player_id), int(generation))
        self.mode_policy.forget(int(player_id), int(generation))

    def decide(self, frame: PerceptionFrame) -> BotIntent | None:
        """Return the newest bounded intention for one observer."""

        intent = self._decide(frame)
        if (intent is None or intent.action.kind is not BotActionKind.NONE
                or intent.priority >= BotIntentPriority.SURVIVAL
                or (intent.look is not None and intent.look.visible)):
            return intent
        observer = next((player for player in frame.players
                         if player.player_id == frame.observer_id
                         and player.generation == frame.observer_generation), None)
        if observer is None or observer.reloading or intent.tool_id not in WEAPON_PROFILES:
            return intent
        clip, reserve = _weapon_wallet(observer, intent.tool_id)
        size = int(WEAPON_PROFILES[intent.tool_id].clip_size)
        if (reserve > 0 and clip < size and clip <= max(0, int(size * 0.4))
                and int(observer.weapon_tool) == intent.tool_id
                and frame.created_at - observer.last_damage_at > 1.5):
            # Top up between fights like anyone who has ever lost a duel to
            # a half-empty magazine. The gun stays in hand; movement goes on.
            return replace(intent, action=BotAction(BotActionKind.RELOAD, tool_id=intent.tool_id))
        return intent

    def _decide(self, frame: PerceptionFrame) -> BotIntent | None:
        observer = next(
            (
                player
                for player in frame.players
                if int(player.player_id) == int(frame.observer_id)
                and int(player.generation)
                == int(frame.observer_generation)
            ),
            None,
        )
        if observer is None or not observer.alive or not observer.spawned:
            return None
        if int(frame.map_epoch) != self._map_epoch:
            self.reset_for_map(frame.map_epoch)

        key = int(observer.player_id), int(observer.generation)
        state = self._states.get(key)
        if (
            state is None
            or int(state.map_epoch) != int(frame.map_epoch)
            or int(state.mode_epoch) != int(frame.mode_epoch)
            or int(state.life_id) != int(observer.life_id)
            or state.carried_entity_id != observer.carried_entity_id
            or state.can_shoot != observer.can_shoot
        ):
            # Pickup/drop changes both the winning goal and legal traversal.
            # Escape continuations are stronger than ordinary goal changes;
            # none may carry a pre-pickup dig route into an unarmed flag run.
            state = _BotState(
                map_epoch=int(frame.map_epoch),
                mode_epoch=int(frame.mode_epoch),
                life_id=int(observer.life_id),
                carried_entity_id=observer.carried_entity_id,
                can_shoot=observer.can_shoot,
            )
            self._states[key] = state

        now = float(frame.created_at)
        if now + 1e-9 < state.next_decision_at:
            return None
        state.next_decision_at = now + self._decision_interval
        self._prune_state(frame)
        self._prune_blocked_edges(state, now)

        profile = frame.profile or _fallback_profile(observer.player_id)
        if state.flight_step is not None:
            flight = self._flight_intent(frame, observer, state, now)
            if flight is not None:
                return flight
        water_contact = self._water_contact(observer)
        if water_contact:
            state.water_committed = True
        elif state.water_committed and self._landed_on_dry_surface(observer):
            state.water_committed = False
            state.dry_detour_goal = None
            state.dry_detour_until = 0.0
            state.dry_route_failures = 0

        if state.water_committed:
            if (state.water_breach_target is not None
                    and not self.world.solid(*state.water_breach_target)):
                # Excavating the actual exit is progress even while the body
                # stays in water. A slow tool must finish its finite ledge
                # instead of retreating just as the final face disappears.
                # Unchanged geometry and accepted swings alone never renew it.
                state.water_breach_target = None
                state.water_escape_at = now
            if state.goal is not None and state.goal.role == "tdm_squad_support":
                # This is a moving formation point projected ahead of a
                # teammate, not a crossing objective. Find a real bank before
                # resuming support so two swimmers cannot chase its moving
                # offset around the same patch of water indefinitely.
                state.water_recovery = True
            # Keep the progress watchdog through the airborne bank phase;
            # a false wade bit alone does not mean the shore was reached.
            state.navigation_progress_position = None
            state.navigation_progress_at = now
            force_water_edge = False
            if state.water_escape_position is None:
                state.water_escape_position = observer.position
                state.water_escape_at = now
            elif math.hypot(
                float(observer.position[0])
                - float(state.water_escape_position[0]),
                float(observer.position[1])
                - float(state.water_escape_position[1]),
            ) >= _WATER_ESCAPE_PROGRESS_DISTANCE:
                state.water_escape_position = observer.position
                state.water_escape_at = now
            elif (
                now - float(state.water_escape_at)
                >= _WATER_ESCAPE_PROGRESS_SECONDS
            ):
                # A route can alternate several individually valid swim
                # steps inside one small basin. Per-step timers reset on
                # every alternation, so blacklist the currently selected
                # edge when the body fails the map-level four-block swim
                # contract across the whole window.
                state.water_escape_position = observer.position
                state.water_escape_at = now
                state.water_recovery = True
                self._clear_route(state, now)
                force_water_edge = True
            landing = state.water_landing_step
            if (landing is not None and not force_water_edge and not observer.grounded
                    and now - state.water_progress_at < 1.2
                    and math.dist(observer.position[:2], landing.waypoint[:2]) <= 1.75
                    and observer.position[2] < landing.waypoint[2] - 0.25):
                # Finish an airborne shore landing when its dry column no
                # longer has a water-flow step. Both physical progress clocks
                # remain authoritative; this cannot renew a failed swim.
                return self._water_intent(frame, observer, landing, now)
            water_edge_blocked = any(
                int(source[2]) >= int(C.Z_ABOVE_WATERPLANE) + 1
                for source, _target in self._water_exclusions(state, now)
            )
            water_goal_reached = bool(
                state.goal is not None
                and math.hypot(
                    float(state.goal.position[0])
                    - float(observer.position[0]),
                    float(state.goal.position[1])
                    - float(observer.position[1]),
                )
                <= max(
                    _WATER_GOAL_RELEASE_RADIUS,
                    float(state.goal.arrival_radius),
                )
            )
            if water_goal_reached:
                state.water_goal_reached = True
                state.water_recovery = True
            elif water_edge_blocked:
                state.water_recovery = True
            # Keep following a strategic cross-water route when one already
            # owns locomotion. The former code erased this goal on contact with
            # water and sent the bot back to the nearest shore, making river
            # and island objectives permanently unreachable. Once native
            # physics rejects a shore edge, temporarily prefer the map-wide
            # water flow with that edge excluded; strategic A* otherwise
            # circles around the same goal-facing cells in CastleWars.
            if state.goal is not None and not state.water_recovery:
                strategic = self._navigation_intent(
                    frame,
                    observer,
                    state,
                    state.goal,
                    now,
                    water_context=True,
                )
                if (
                    strategic.action.kind is not BotActionKind.NONE
                    or math.hypot(
                        float(strategic.movement.direction[0]),
                        float(strategic.movement.direction[1]),
                    ) > 1e-6
                ):
                    return strategic
                # Segment completion, a rejected edge, or an empty strategic
                # plan hands locomotion to the map-wide shore flow for the
                # rest of this swim. Re-entering strategic A* on the next
                # frame made DragonIsland bots oscillate between two owners.
                state.water_recovery = True
            water_step = self.world.water_step(
                observer.position,
                preferred_goal=(
                    state.goal.position
                    if (
                        state.goal is not None
                        and not state.water_goal_reached
                        and not state.water_recovery
                    )
                    else None
                ),
                blocked_edges=self._water_exclusions(state, now),
            )
            return self._water_intent(
                frame,
                observer,
                water_step,
                now,
                force_block_edge=force_water_edge,
            )

        state.water_step_key = None
        state.water_landing_step = None
        state.water_breach_target = None
        state.water_best_distance = math.inf
        state.water_progress_at = now
        state.water_recovery = False
        state.water_goal_reached = False
        state.water_escape_position = None
        state.water_escape_at = now
        state.water_search_heading = None
        state.water_search_until = 0.0
        state.water_search_origin = None
        # A one-block foothold can briefly clear wade before the body falls
        # back in. Keep shoreline failure memory through that landing; its
        # ordinary TTL and the next life/map reset still bound it.

        if observer.grounded:
            escape = hazard_escape(self.world, frame, observer, profile,
                                   state.engagement, now)
            if escape is not None:
                # A noticed grenade outranks every task; the old route is
                # replanned from wherever the sprint ends.
                self._clear_route(state, now)
                return self._intent(
                    frame,
                    movement=MovementIntent(direction=escape, sprint=True),
                    look=None,
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.SURVIVAL,
                    debug_role="evade_explosive",
                    crowd_adjust=False,
                )

        mode_decision = self.mode_policy.decide(frame, observer)
        if mode_decision is not None and mode_decision.role == "diamond_guard_dropoff":
            if now >= state.guard_retry_at:
                distance = math.dist(observer.position, mode_decision.position)
                if (state.guard_target is None
                        or math.dist(state.guard_target, mode_decision.position) > 8.0):
                    state.guard_target = mode_decision.position
                    state.guard_best_distance = distance
                    state.guard_progress_at = now
                elif distance + 1.0 < state.guard_best_distance:
                    state.guard_best_distance = distance
                    state.guard_progress_at = now
            if (now >= state.guard_retry_at and state.guard_target is not None
                    and now - state.guard_progress_at >= 10.0
                    and distance > mode_decision.arrival_radius):
                # Guard duty is optional. A generated drop-off may sit above
                # a sheer structure with no usable approach from this side.
                # Contribute by mining before reconsidering that assignment;
                # do not spend the entire match repeating local detours.
                state.guard_retry_at = now + 35.0 + observer.player_id % 7
                state.guard_target = None
                self._set_goal(state, None, observer.position, now)
            if now < state.guard_retry_at:
                mode_decision = replace(
                    mode_decision, position=observer.position,
                    role="diamond_mine_blocks", directive="mine",
                    posture=ModeBotPosture.MINE, arrival_radius=0.5,
                )
        else:
            state.guard_target = None
        visible_target = self._visible_target(
            frame,
            observer,
            state,
            mode_decision,
        )
        engagement = state.engagement
        if (visible_target is None and observer.reloading and observer.grounded
                and now < engagement.cover_until
                and (engagement.cover_duck or engagement.cover is not None)
                and now - observer.last_damage_at > 0.6):
            # Breaking the sight line hides the enemy from perception too.
            # Finish the reload behind that cover instead of standing straight
            # back up into the same fire with an empty gun.
            contact = state.contact_position
            return self._intent(
                frame,
                movement=MovementIntent(crouch=True),
                look=(LookIntent((contact[0], contact[1], observer.eye[2]))
                      if contact is not None else None),
                # Keep the gun being reloaded in hand: a tool change cancels it.
                tool_id=(int(observer.weapon_tool) if int(observer.weapon_tool) in WEAPON_PROFILES
                         else _weapon_tool(observer)),
                priority=BotIntentPriority.COMBAT,
                debug_goal=contact,
                debug_role="cover_reload",
            )
        if frame.behavior_version == "cooperative":
            order = self.cooperative.decide(frame, observer, visible_target, mode_decision)
            if order is not None:
                return self._cooperative_intent(frame, observer, state, profile,
                                                visible_target, order, now)
        if visible_target is not None:
            return self._combat_intent(
                frame,
                observer,
                visible_target,
                state,
                profile,
                now,
                mode_decision,
            )

        if (
            mode_decision is not None
            and mode_decision.directive == "mine"
        ):
            mining = self._objective_mine_intent(
                frame,
                observer,
                state,
                now,
            )
            if mining is not None:
                return mining

        support = (
            self._medic_support_intent(
                frame,
                observer,
                state,
                now,
            )
            if frame.behavior_version != "cooperative" and (mode_decision is None
            or mode_decision.objective_priority < 0.9
            or mode_decision.posture
            in {ModeBotPosture.DEFEND, ModeBotPosture.ESCORT})
            else None
        )
        if support is not None:
            return support

        deployable = (
            self._strategic_deploy_intent(
                frame,
                observer,
                state,
                now,
                decision=mode_decision,
            )
            if frame.behavior_version != "cooperative" and (mode_decision is None
            or mode_decision.directive == "fortify"
            or mode_decision.objective_priority < 0.85)
            else None
        )
        if deployable is not None:
            return deployable

        goal = self._select_goal(
            frame,
            observer,
            state,
            now,
            decision=mode_decision,
        )
        if goal is None:
            self._set_goal(state, None, observer.position, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.ROUTINE,
                debug_role="idle_no_goal",
            )
        return self._navigation_intent(
            frame,
            observer,
            state,
            goal,
            now,
        )

    def _cooperative_intent(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        profile: BotProfile, visible: PlayerSnapshot | None, order: TacticalOrder,
        now: float,
    ) -> BotIntent:
        """Resolve the single movement/tool owner selected by a team task."""
        if order.action.kind is not BotActionKind.NONE:
            return self._intent(frame, movement=MovementIntent(),
                look=LookIntent(order.look) if order.look is not None else None,
                tool_id=order.action.tool_id, action=order.action,
                priority=BotIntentPriority.SURVIVAL if order.urgent else BotIntentPriority.TRAVERSAL,
                debug_role=order.role, debug_goal=order.goal)
        if order.tool_id >= 0:
            return self._intent(frame, movement=MovementIntent(),
                look=LookIntent(order.look) if order.look is not None else None,
                tool_id=order.tool_id, debug_role=order.role, debug_goal=order.goal)
        if visible is not None and order.hold:
            decision = ModeBotDecision(order.goal, order.role, sprint=False,
                posture=ModeBotPosture.DEFEND, arrival_radius=order.arrival_radius)
            intent = self._combat_intent(frame, observer, visible, state, profile, now, decision)
            # Retain normal weapon/reload/aim cadence, while staying at the
            # validated firing point. Urgent close threats cancel the project.
            if intent.action.kind in {BotActionKind.NONE, BotActionKind.FIRE, BotActionKind.RELOAD}:
                return replace(intent, movement=MovementIntent(crouch=intent.movement.crouch),
                               debug_role=order.role, debug_goal=order.goal)
            return intent
        if order.hold:
            return self._intent(frame, movement=MovementIntent(),
                look=LookIntent(order.look) if order.look is not None else None,
                tool_id=_weapon_tool(observer), debug_role=order.role, debug_goal=order.goal)
        goal = _Goal(("cooperative", order.task_id, order.role), order.goal,
                     order.role, order.arrival_radius, False)
        # Navigation owns aim/tool when it must dig, build, swim or use a pack.
        return self._navigation_intent(frame, observer, state, goal, now)

    def _visible_target(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        mode_decision: ModeBotDecision | None = None,
    ) -> PlayerSnapshot | None:
        """Select a stable enemy that is alive, in range, and unobscured."""

        if not observer.can_shoot:
            return None
        candidates: list[PlayerSnapshot] = []
        for player in frame.players:
            if (
                int(player.team) == int(observer.team)
                or not player.alive
                or not player.spawned
            ):
                continue
            distance = math.dist(observer.eye, player.eye)
            if distance > _VISUAL_RANGE:
                continue
            if not mode_decision_allows_combat(
                mode_decision,
                observer,
                player,
                now=float(frame.created_at),
            ):
                continue
            dx = float(player.eye[0]) - float(observer.eye[0])
            dy = float(player.eye[1]) - float(observer.eye[1])
            horizontal = math.hypot(dx, dy)
            if horizontal > 1e-6 and distance > 20.0:
                facing = (
                    float(observer.orientation[0]) * dx
                    + float(observer.orientation[1]) * dy
                ) / horizontal
                recently_hit_by_target = (
                    int(observer.last_damage_source_id)
                    == int(player.player_id)
                    and float(observer.last_damage_at) > 0.0
                    and float(frame.created_at)
                    - float(observer.last_damage_at)
                    <= 2.0
                )
                already_tracking = (
                    int(state.contact_id) == int(player.player_id)
                    and int(state.contact_generation)
                    == int(player.generation)
                )
                if facing < -0.2 and not (
                    recently_hit_by_target or already_tracking
                ):
                    continue
            if not self.world.has_line_of_sight(observer.eye, player.eye):
                continue
            candidates.append(player)

        if not candidates:
            return None
        current = next(
            (
                player
                for player in candidates
                if int(player.player_id) == int(state.contact_id)
                and int(player.generation)
                == int(state.contact_generation)
            ),
            None,
        )
        # Public carrier/crown markers rank only enemies already admitted by
        # range, field of view and LOS above. A nearby threat still gets urgent
        # self-defence; a visible VIP should not be ignored for an old decoy.
        objective_ids = {
            item.carrier_id for item in frame.objectives
            if item.carrier_id >= 0 and (
                frame.mode_id == "vip" and item.kind == "vip" and item.team != observer.team
                or frame.mode_id == "ctf" and item.kind == "ctf_intel" and item.team == observer.team)
        }
        marked = min((player for player in candidates if player.player_id in objective_ids),
                     key=lambda player: math.dist(observer.position, player.position), default=None)
        urgent = [player for player in candidates if (
            math.dist(observer.position, player.position) <= 4.5
            or observer.last_damage_source_id == player.player_id
            and observer.last_damage_at > 0
            and 0 <= frame.created_at - observer.last_damage_at <= 1.0)]
        # Preserve a tracked urgent opponent, but do not overlook a newly
        # arrived close threat simply because the old target has a crown.
        urgent_target = (current if current in urgent else min(
            urgent, key=lambda player: math.dist(observer.position, player.position),
            default=None))
        target = urgent_target or marked or current or min(
            candidates,
            key=lambda player: (
                math.dist(observer.position, player.position),
                int(player.player_id),
            ),
        )
        target_changed = (
            int(state.contact_id) != int(target.player_id)
            or int(state.contact_generation) != int(target.generation)
        )
        state.contact_id = int(target.player_id)
        state.contact_generation = int(target.generation)
        state.contact_position = tuple(float(value) for value in target.position)
        state.contact_until = float(frame.created_at) + _CONTACT_SECONDS
        if target_changed:
            state.acquired_at = float(frame.created_at)
        return target

    def _select_goal(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        now: float,
        *,
        decision: ModeBotDecision | None = None,
    ) -> _Goal | None:
        """Choose exactly one non-combat movement owner."""

        if decision is None:
            decision = self.mode_policy.decide(frame, observer)
        if decision is not None and (mode_objective_committed(decision)
                                     or decision.objective_priority >= 0.7):
            if decision.role in {"team_assault_enemy_side", "arena_elimination_push"}:
                return (self._flank_approach_goal(frame, observer, state, now)
                        or self._assault_search_goal(observer, state, decision, now))
            if decision.role in {"ctf_attack_intel", "classic_ctf_attack_intel"}:
                # The same wide approach lets some raiders arrive off the
                # defended centre line instead of feeding the midfield fight.
                flank = self._flank_approach_goal(frame, observer, state, now)
                if flank is not None:
                    return flank
            return self._goal_from_mode_decision(decision)

        if state.contact_position is not None and now < state.contact_until:
            return _Goal(
                (
                    "contact",
                    int(state.contact_id),
                    int(state.contact_generation),
                ),
                state.contact_position,
                "chase_last_seen",
                1.75,
                True,
            )

        if decision is None:
            return None
        decision = self._optional_support_decision(frame, observer, state, decision, now)
        if decision.role in {"team_assault_enemy_side", "arena_elimination_push"}:
            return (self._flank_approach_goal(frame, observer, state, now)
                    or self._assault_search_goal(observer, state, decision, now))
        return self._goal_from_mode_decision(decision)

    def _flank_approach_goal(
        self, frame: PerceptionFrame, observer: PlayerSnapshot,
        state: _BotState, now: float,
    ) -> _Goal | None:
        """Give some lives a wide approach instead of the shared centre line.

        The choice is made once per life from identity, life number and
        temperament, so the same bot arrives from different sides over a match
        while never changing its mind mid-route. Only public team anchors and
        the worker's own terrain copy are used.
        """

        if state.flank_decided and state.flank_point is None:
            return None
        own = next((item for item in frame.objectives
                    if item.kind == "team_anchor" and item.team == observer.team), None)
        enemy = next((item for item in frame.objectives
                      if item.kind == "team_anchor" and item.team != observer.team), None)
        if own is None or enemy is None:
            return None
        ax, ay = enemy.position[0] - own.position[0], enemy.position[1] - own.position[1]
        length = math.hypot(ax, ay)
        if length < 140.0:
            state.flank_decided = True
            return None
        ux, uy = ax / length, ay / length
        along = ((observer.position[0] - own.position[0]) * ux
                 + (observer.position[1] - own.position[1]) * uy) / length
        if not state.flank_decided:
            state.flank_decided = True
            profile = frame.profile or _fallback_profile(observer.player_id)
            seed = (observer.player_id, observer.life_id)
            appetite = (0.30 + 0.35 * profile.creativity + 0.20 * profile.caution
                        - 0.25 * profile.aggression)
            if along > 0.3 or tactical_mix(*seed, 41) > appetite:
                return None
            side = 1.0 if tactical_mix(*seed, 43) < 0.5 else -1.0
            for attempt in range(3):
                lateral = side * (34.0 + 46.0 * tactical_mix(*seed, 47 + attempt))
                stage = 0.42 + 0.16 * tactical_mix(*seed, 53 + attempt)
                x = own.position[0] + ax * stage - uy * lateral
                y = own.position[1] + ay * stage + ux * lateral
                if not (16.0 <= x <= 495.0 and 16.0 <= y <= 495.0):
                    side = -side
                    continue
                surface = self.world.surface(int(x), int(y), observer.position[2],
                                             vertical_span=48, allow_water=False)
                if surface is not None:
                    state.flank_point = surface.position
                    state.flank_until = now + 75.0
                    break
                side = -side
        point = state.flank_point
        if point is None:
            return None
        if not state.flank_started and along < 0.10:
            # Leave the base by the ordinary, all-map-validated route first.
            # Peeling off inside a spawn structure walked a TokyoNeon bot into
            # a stairwell dead end it never meets on the centre line.
            state.flank_until = now + 75.0
            return None
        # Latched: a staging point that lies slightly rearward must not drag
        # the bot back across the line and re-arm the guard. ToTheBridge held
        # one bot swapping flank/assault goals on that boundary for 17 s.
        state.flank_started = True
        if (along >= 0.55 or now >= state.flank_until
                or (state.contact_position is not None and now < state.contact_until)
                or math.hypot(point[0] - observer.position[0],
                              point[1] - observer.position[1]) <= 10.0):
            # Staging reached, a fight found, or patience exhausted: push in.
            state.flank_point = None
            return None
        return _Goal(("flank", observer.life_id), point, "tdm_flank_approach", 10.0, True)

    @staticmethod
    def _optional_support_decision(
        frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        decision: ModeBotDecision, now: float,
    ) -> ModeBotDecision:
        """Stop retrying one inaccessible formation point; keep useful team pressure."""
        if decision.role != "tdm_squad_support":
            return decision
        if (not observer.wade
                and math.dist(observer.position[:2], decision.position[:2]) <= decision.arrival_radius
                and abs(observer.position[2] - decision.position[2]) <= 3.0):
            # Arrival bypasses route-progress accounting. Old failed approach
            # evidence must not abandon a formation point now actually held.
            state.corridor_failed_goal = None
            state.support_retry_at = 0.0
            state.support_rejected_goal = None
            state.support_progress_anchor = None
            state.support_no_progress_time = 0.0
            state.support_last_decision_at = None
            state.support_failed_goal = None
            state.support_escape_attempted = False
            return decision
        if (state.support_rejected_goal is not None
                and math.dist(decision.position, state.support_rejected_goal) >= 8.0):
            state.support_retry_at = 0.0
            state.support_rejected_goal = None
        if now >= state.support_retry_at:
            anchor = state.support_progress_anchor
            if anchor is None or math.dist(anchor, decision.position) >= 16.0:
                state.support_progress_anchor = anchor = decision.position
                state.support_best_distance = math.dist(observer.position, anchor)
                state.support_no_progress_time = 0.0
                state.support_failed_goal = None
                state.support_escape_attempted = False
            distance = math.dist(observer.position, anchor)
            if distance + 4.0 <= state.support_best_distance:
                # Measure the actor against a fixed commitment, not a moving
                # teammate. Target jitter fabricated progress on London.
                state.support_best_distance = distance
                state.support_no_progress_time = 0.0
                state.support_failed_goal = None
                state.support_escape_attempted = False
            elif state.support_last_decision_at is not None:
                elapsed = now - state.support_last_decision_at
                step = (state.route[state.route_index]
                        if state.route_index < len(state.route) else None)
                if (0.0 <= elapsed <= 1.0
                        and (step is None or step.affordance is not MovementAffordance.BREACH)):
                    # Combat/task gaps and committed excavation have their
                    # own progress contracts; they do not consume this one.
                    state.support_no_progress_time += elapsed
        state.support_last_decision_at = now
        failed = (state.support_no_progress_time >= 15.0
                  and state.support_escape_attempted
                  and state.support_failed_goal is not None
                  and now - state.support_failure_at <= 30.0
                  and math.dist(decision.position, state.support_failed_goal) <= 3.0)
        if not failed and now >= state.support_retry_at:
            return decision
        enemy_anchor = next((objective for objective in frame.objectives
                             if objective.kind == "team_anchor"
                             and objective.team != observer.team), None)
        if enemy_anchor is None:
            return decision
        if failed and now >= state.support_retry_at:
            state.support_rejected_goal = decision.position
            state.support_retry_at = now + 30.0
            state.support_progress_anchor = None
            state.support_no_progress_time = 0.0
            state.support_failed_goal = None
            state.support_escape_attempted = False
        # This is an optional formation offset, not a carrier, flag or other
        # winning objective. The authored enemy-side anchor is already legal
        # policy information; combat/contact ownership remains above this.
        return ModeBotDecision(enemy_anchor.position, "team_assault_enemy_side",
                               sprint=True, arrival_radius=5.0,
                               posture=ModeBotPosture.ASSAULT,
                               objective_priority=0.6)

    def _assault_search_goal(
        self,
        observer: PlayerSnapshot,
        state: _BotState,
        decision: ModeBotDecision,
        now: float,
    ) -> _Goal:
        """Search authored enemy-side ground after arrival, using no hidden intel."""

        position = state.patrol_position or decision.position
        arrived = (math.hypot(position[0] - observer.position[0],
                              position[1] - observer.position[1]) <= decision.arrival_radius
                   and abs(position[2] - observer.position[2]) <= 3.0)
        if not arrived:
            state.patrol_arrived_at = None
        elif state.patrol_arrived_at is None:
            state.patrol_arrived_at = now
        finished_wait = (state.patrol_arrived_at is not None
                         and now - state.patrol_arrived_at >= 3.0)
        expired_search = (state.patrol_position is not None
                          and now - state.patrol_started_at >= 45.0)
        if finished_wait or expired_search:
            # A stable sequence gives different bots different search points;
            # the target remains fixed throughout each route attempt.
            state.patrol_index += 1
            state.patrol_started_at = now
            state.patrol_arrived_at = None
            for offset in range(8):
                angle = (state.patrol_index + observer.player_id * 3 + offset) * math.pi / 4.0
                radius = 20.0 + (observer.player_id % 3) * 6.0
                x = min(510, max(1, int(decision.position[0] + math.cos(angle) * radius)))
                y = min(510, max(1, int(decision.position[1] + math.sin(angle) * radius)))
                surface = self.world.surface(x, y, decision.position[2],
                                             vertical_span=12, clearance=3, allow_water=False)
                if surface is not None:
                    state.patrol_position = surface.position
                    break
        if state.patrol_position is None:
            return self._goal_from_mode_decision(decision)
        return _Goal(("assault_search", state.patrol_index), state.patrol_position,
                     "team_assault_search", decision.arrival_radius, True)

    @staticmethod
    def _goal_from_mode_decision(decision: ModeBotDecision) -> _Goal:
        """Normalize a mode policy into the controller's one goal type."""

        position = tuple(float(value) for value in decision.position)
        return _Goal(
            (
                "objective",
                str(decision.role),
            ),
            position,
            str(decision.role),
            max(0.75, float(decision.arrival_radius)),
            bool(decision.sprint),
            decision.watch_position,
        )

    def _combat_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BotState,
        profile: BotProfile,
        now: float,
        mode_decision: ModeBotDecision | None = None,
    ) -> BotIntent:
        """Fight a visible enemy while retaining a committed mode route."""

        self._update_combat_progress(state, observer.position, now)

        dx = float(target.position[0]) - float(observer.position[0])
        dy = float(target.position[1]) - float(observer.position[1])
        distance = math.hypot(dx, dy)
        direction_to_target = _normalized_xy(dx, dy)
        weapon_tool = _weapon_tool(observer, distance)
        clip, reserve = _weapon_wallet(observer, weapon_tool)
        # Reload state belongs to the gun in hand; a fresh draw is ready.
        reloading = observer.reloading and int(observer.weapon_tool) == weapon_tool
        melee_tool = _melee_tool(observer)
        zombie = (
            int(observer.class_id) in _ZOMBIE_CLASSES
            and melee_tool is not None
        )
        posture = (
            mode_decision.posture
            if mode_decision is not None
            else ModeBotPosture.BALANCED
        )
        protected_objective = (
            not zombie and mode_decision is not None
            and posture in {ModeBotPosture.EVASIVE, ModeBotPosture.SURVIVE,
                            ModeBotPosture.DEFEND, ModeBotPosture.ESCORT,
                            ModeBotPosture.BUILD, ModeBotPosture.MINE}
            and mode_decision.objective_priority >= 0.7
        )
        if (not zombie and mode_objective_committed(mode_decision)
                and mode_decision.posture in {
                    ModeBotPosture.ASSAULT, ModeBotPosture.BALANCED}):
            objective_distance = math.dist(observer.position, mode_decision.position)
            target_offset = math.dist(target.position, mode_decision.position)
            # Return fire without letting a distant incidental opponent pull
            # a capture/intercept order off its route. Combat in the objective
            # area keeps normal range selection and footwork.
            protected_objective = (
                objective_distance > max(12.0, mode_decision.arrival_radius * 2)
                and target_offset > max(16.0, envelope_for(weapon_tool).ideal_max * .8))
            if (mode_decision.role.endswith("ctf_attack_intel")
                    and objective_distance <= 18.0):
                # Touching the intel IS the fight. A raider three blocks from
                # the flag used to stop and duel its guard until it died; a
                # player dives on the pickup and shoots on the way.
                protected_objective = True

        action = BotAction()
        movement = direction_to_target
        affordance = MovementAffordance.WALK
        crouch = False
        sprint = True
        selected_tool = weapon_tool
        oriented_aim_offset: float | None = None
        priority = BotIntentPriority.COMBAT
        pursuit: BotIntent | None = None
        pursuit_goal = _Goal(
            (
                "enemy",
                int(target.player_id),
                int(target.generation),
            ),
            target.position,
            "combat_pursuit",
            1.5 if zombie else 3.0,
            True,
        )
        if protected_objective:
            pursuit_goal = self._goal_from_mode_decision(mode_decision)

        if zombie:
            selected_tool = int(melee_tool)
            if distance <= 2.35:
                self._set_goal(state, None, observer.position, now)
                movement = (0.0, 0.0, 0.0)
                action = BotAction(
                    BotActionKind.MELEE,
                    tool_id=int(melee_tool),
                )
            else:
                pursuit = self._navigation_intent(
                    frame,
                    observer,
                    state,
                    pursuit_goal,
                    now,
                )
                movement = pursuit.movement.direction
                affordance = pursuit.movement.affordance
        else:
            envelope = envelope_for(weapon_tool)
            spacing = state.footwork.spacing(
                distance, float(envelope.ideal_min), float(envelope.ideal_max))
            if (clip <= 0 and melee_tool is not None
                    and math.dist(observer.position, target.position) <= 3.2):
                # Nobody finishes a reload with an enemy at arm's length:
                # the spade is already in reach.
                selected_tool = int(melee_tool)
                movement = (0.0, 0.0, 0.0)
                sprint = False
                reloading = False
                action = BotAction(BotActionKind.MELEE, tool_id=int(melee_tool))
            elif reloading:
                action = BotAction()
            elif clip <= 0 and reserve > 0:
                action = BotAction(
                    BotActionKind.RELOAD,
                    tool_id=weapon_tool,
                )
            elif (
                clip <= 0
                and reserve <= 0
                and melee_tool is not None
            ):
                selected_tool = int(melee_tool)
                if distance <= 3.0:
                    if not protected_objective:
                        self._set_goal(state, None, observer.position, now)
                    movement = (0.0, 0.0, 0.0)
                    action = BotAction(
                        BotActionKind.MELEE,
                        tool_id=int(melee_tool),
                    )
                else:
                    pursuit = self._navigation_intent(
                        frame,
                        observer,
                        state,
                        pursuit_goal,
                        now,
                    )
                    movement = pursuit.movement.direction
                    affordance = pursuit.movement.affordance
            else:
                if spacing == "approach":
                    pursuit = self._navigation_intent(
                        frame,
                        observer,
                        state,
                        pursuit_goal,
                        now,
                    )
                    movement = pursuit.movement.direction
                    affordance = pursuit.movement.affordance
                    sprint = True
                elif spacing == "retreat":
                    if not protected_objective:
                        self._set_goal(state, None, observer.position, now)
                    movement = _normalized_xy(-dx, -dy)
                    sprint = False
                elif envelope.prefers_stationary:
                    if not protected_objective:
                        self._set_goal(state, None, observer.position, now)
                    # Planted weapons still displace: after a spell in one
                    # spot, or at once when the position starts taking hits.
                    shift = stationary_relocation(
                        self.world, observer, target, profile, state.engagement, now)
                    movement = shift or (0.0, 0.0, 0.0)
                    crouch = shift is None
                    sprint = False
                else:
                    if not protected_objective:
                        self._set_goal(state, None, observer.position, now)
                    movement = state.footwork.direction(
                        observer, target, profile, now,
                        blocked_stage=state.combat_stall_stage)
                    sprint = False

                oriented = self._oriented_attack_choice(
                    frame,
                    observer,
                    target,
                    state,
                    profile,
                    now,
                )
                if oriented is not None:
                    selected_tool, oriented_aim_offset = oriented
                    action = BotAction(
                        BotActionKind.ORIENTED,
                        tool_id=selected_tool,
                        end_position=(
                            target.eye
                            if selected_tool in _ROCKET_TOOLS
                            else None
                        ),
                    )
                elif (
                    clip > 0
                    and distance <= float(envelope.hard_max)
                    and now - float(state.acquired_at)
                    >= float(profile.reaction_time)
                ):
                    low, high = envelope.burst_shots
                    burst = max(
                        int(low),
                        min(
                            int(high),
                            int(
                                round(
                                    float(low)
                                    + (float(high) - float(low))
                                    * float(profile.burst_discipline)
                                )
                            ),
                        ),
                    )
                    pause_low, pause_high = envelope.burst_pause
                    pause = (
                        float(pause_high)
                        - (float(pause_high) - float(pause_low))
                        * float(profile.burst_discipline)
                    )
                    action = BotAction(
                        BotActionKind.FIRE,
                        tool_id=weapon_tool,
                        burst=burst,
                        burst_pause=pause,
                    )

        engagement = state.engagement
        engagement.retarget(target)
        if not zombie and not protected_objective and (
                reloading or action.kind is BotActionKind.RELOAD):
            self._set_goal(state, None, observer.position, now)
            # An empty gun is the moment to break the sight line: duck behind
            # a lip, or step behind nearby terrain, before the old open-ground
            # backpedal.
            cover, duck = find_cover(self.world, observer, target.eye, engagement, now)
            if duck:
                pursuit = None
                movement = (0.0, 0.0, 0.0)
                crouch = True
                sprint = False
            elif cover is not None:
                pursuit = None
                gap = math.hypot(cover[0] - observer.position[0],
                                 cover[1] - observer.position[1])
                movement = ((0.0, 0.0, 0.0) if gap <= 0.6 else _normalized_xy(
                    cover[0] - observer.position[0], cover[1] - observer.position[1]))
                crouch = gap <= 0.6
                sprint = gap > 2.0
            else:
                movement = state.footwork.direction(
                    observer if reloading else replace(observer, reloading=True),
                    target, profile, now, blocked_stage=state.combat_stall_stage)
                sprint = False
        elif not zombie and not protected_objective and fire_blocked(
                engagement, observer, action.kind is BotActionKind.FIRE, now,
                cadence=float(action.burst_pause)) and pursuit is None and (
                    state.combat_stall_stage == 0):
            # (Physically blocked footwork has its own recovery ladder.)
            # Wanted shots are not leaving the barrel (a grazing edge the
            # authoritative ray rejects). Close in along a real route instead
            # of holding a silent standoff.
            pursuit = self._navigation_intent(frame, observer, state, pursuit_goal, now)
            movement = pursuit.movement.direction
            affordance = pursuit.movement.affordance
            crouch = False

        if protected_objective:
            # One route owner survives both quiet and combat frames. Clearing
            # it for a strafe before recreating it reset progress every tick;
            # nearby enemies also used to pull escorts out of formation.
            if pursuit is None:
                pursuit = self._navigation_intent(
                    frame, observer, state, pursuit_goal, now)
            movement = pursuit.movement.direction
            affordance = pursuit.movement.affordance
            sprint = bool(mode_decision.sprint)
            crouch = False

        if pursuit is None and math.hypot(movement[0], movement[1]) > 1e-6:
            # A visible target can keep combat ownership indefinitely.  In a
            # one-column tunnel the preferred perpendicular strafe may be
            # terrain-blocked on both sides, so navigation's waypoint timeout
            # never gets a chance to invalidate it.  Cycle through the other
            # human choices after measured non-progress: opposite strafe,
            # advance along the clear sight line, then retreat along it.
            stage = int(state.combat_stall_stage) % 4
            if int(state.combat_stall_stage) >= 4:
                # Four terrain-blind human movement choices have all failed.
                # Keep combat ownership, aim and firing, but let the ordinary
                # voxel planner own locomotion until real progress resumes.
                # This is London's waterline/low-wall combat wedge: a visible
                # enemy across the obstacle kept resetting strategic routing.
                pursuit = self._navigation_intent(
                    frame,
                    observer,
                    state,
                    pursuit_goal,
                    now,
                )
                movement = pursuit.movement.direction
                affordance = pursuit.movement.affordance
            elif stage == 1:
                movement = (-movement[0], -movement[1], 0.0)
            elif stage == 2:
                movement = direction_to_target
            elif stage == 3:
                movement = (
                    -direction_to_target[0],
                    -direction_to_target[1],
                    0.0,
                )

        aim_offset = (
            float(oriented_aim_offset)
            if oriented_aim_offset is not None
            else _visible_aim_offset(self.world, observer, target, profile, engagement, now)
        )
        look = LookIntent(
            (
                float(target.eye[0]),
                float(target.eye[1]),
                float(target.eye[2]) + aim_offset,
            ),
            visible=True,
            target_player_id=int(target.player_id),
            target_generation=int(target.generation),
            aim_offset_z=aim_offset,
        )
        scoped = (
            not zombie
            and selected_tool in WEAPON_PROFILES
            and WEAPON_PROFILES[selected_tool].category == CAT_SNIPER
            and distance >= 18.0
        )
        if pursuit is not None and (
            pursuit.action.kind not in {BotActionKind.NONE, BotActionKind.FIRE}
            or pursuit.movement.affordance in {
                MovementAffordance.BREACH, MovementAffordance.BUILD_STEP,
                MovementAffordance.BUILD_BRIDGE, MovementAffordance.PLACE_PREFAB,
                MovementAffordance.JETPACK,
            }
        ):
            # Seeing an enemy across a wall/river does not make that edge
            # traversable. Keep the dig/build aim, tool and action together.
            return replace(pursuit, priority=priority,
                           debug_role="combat_pursuit:" + pursuit.debug_role)
        if pursuit is None and not zombie and not protected_objective and not scoped:
            leap = self._combat_jetpack_hop(frame, observer, target, state, profile, now)
            if leap is not None:
                return leap
        locomotion = (pursuit.movement if pursuit is not None else MovementIntent(
            direction=movement, jump=affordance is MovementAffordance.JUMP,
            crouch=crouch, sprint=sprint, affordance=affordance))
        state.combat_wants_movement = math.hypot(*locomotion.direction[:2]) > 0.1
        if pursuit is None and not scoped:
            locomotion = replace(locomotion, jump=self._safe_tactical_hop(
                frame, observer, state, locomotion, now))
        if (pursuit is None and not zombie and observer.health < 60
                and now - observer.last_damage_at < 3.0 and observer.reloading):
            cover = self._prefab_cover_intent(frame, observer, state, target.position, now)
            if cover is not None:
                return cover
        return self._intent(
            frame,
            movement=locomotion,
            look=look,
            tool_id=selected_tool,
            action=action,
            priority=priority,
            secondary_fire=scoped,
            zoom=scoped,
            debug_goal=(pursuit.debug_goal if protected_objective and pursuit is not None
                        else target.position),
            debug_path=(
                pursuit.debug_path if pursuit is not None else ()
            ),
            debug_role=(
                "combat_objective:" + mode_decision.role
                if protected_objective else "combat_pursuit"
                if pursuit is not None
                else (
                    "combat_melee"
                    if zombie
                    else (
                        "combat_oriented"
                        if action.kind is BotActionKind.ORIENTED
                        else "combat_visible"
                    )
                )
            ),
        )

    @staticmethod
    def _update_combat_progress(
        state: _BotState,
        position: Vector3,
        now: float,
    ) -> None:
        """Advance collision recovery only after intended movement fails."""
        if not state.combat_wants_movement:
            # Planting the feet for a burst/holding an objective is intentional,
            # not terrain failure. Resume the blocked-input clock on departure.
            state.combat_progress_position = position
            state.combat_progress_at = now
            state.combat_last_at = now
            state.combat_stall_stage = 0
            return
        interrupted = (
            state.combat_progress_position is None
            or float(now) - float(state.combat_last_at) > 0.75
        )
        if interrupted:
            state.combat_progress_position = position
            state.combat_progress_at = float(now)
            state.combat_stall_stage = 0
        else:
            anchor = state.combat_progress_position
            moved = math.hypot(
                float(position[0]) - float(anchor[0]),
                float(position[1]) - float(anchor[1]),
            )
            if moved >= _COMBAT_PROGRESS_DISTANCE:
                state.combat_progress_position = position
                state.combat_progress_at = float(now)
                state.combat_stall_stage = 0
            elif (
                float(now) - float(state.combat_progress_at)
                >= _COMBAT_STRAFE_STALL_SECONDS
            ):
                state.combat_progress_position = position
                state.combat_progress_at = float(now)
                state.combat_stall_stage = min(
                    4,
                    int(state.combat_stall_stage) + 1,
                )
        state.combat_last_at = float(now)

    def _oriented_attack_choice(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: PlayerSnapshot,
        state: _BotState,
        profile: BotProfile,
        now: float,
    ) -> tuple[int, float] | None:
        """Choose one safe stocked projectile for a visible engagement.

        This is intentionally a pure tactical choice with a cooldown, not a
        persistent equipment state. If the authoritative gateway rejects the
        launch because the live lane changed, the next decision draws the
        firearm instead of standing forever with the gadget selected.
        """

        if (
            observer.reloading
            or now < state.next_oriented_at
            or now
            < self._team_oriented_ready_at.get(int(observer.team), -math.inf)
            or now - float(state.acquired_at) < float(profile.reaction_time)
        ):
            return None
        stock = {int(tool): int(count) for tool, count in observer.oriented_stock}
        distance = math.dist(observer.eye, target.eye)
        cluster = sum(
            1
            for player in frame.players
            if int(player.team) != int(observer.team)
            and player.alive
            and player.spawned
            and _distance_squared(player.position, target.position) <= 8.0 ** 2
        )
        choices: list[tuple[float, int, float]] = []
        for raw_tool in observer.loadout:
            tool = int(raw_tool)
            if (
                tool not in _ORIENTED_ATTACK_TOOLS
                or stock.get(tool, 0) <= 0
            ):
                continue
            spec = PROJECTILE_SPECS.get(tool)
            if spec is None:
                continue
            radius = max(0.0, float(spec.blast_radius))
            behavior = str(spec.behavior)
            minimum = max(radius + 4.0, 10.0)
            maximum = {
                "bounce": 42.0,
                "stick": 48.0,
                "deploy": 52.0,
                "contact": 92.0,
            }.get(behavior, 60.0)
            if tool == int(C.DRILLGUN_TOOL):
                minimum, maximum = 9.0, 42.0
            elif tool in _ROCKET_TOOLS:
                # A rocket is never a speculative breach shot. The target
                # must be directly visible at decision time, and the director
                # rechecks the full live ray again immediately before launch.
                minimum = max(radius + 5.0, 12.0)
                maximum = 100.0
                if not self.world.has_line_of_sight(observer.eye, target.eye):
                    continue
            if not minimum <= distance <= maximum:
                continue
            if not self._explosive_target_safe(
                frame,
                observer,
                target.position,
                tool,
                ignore_observer=False,
            ):
                continue
            if not self._friendly_launch_lane_clear(
                frame,
                observer,
                target.eye,
            ):
                continue
            if cluster <= 1 and int(target.health) <= 22:
                continue
            damage = min(250.0, max(0.0, float(spec.damage)))
            score = (
                damage / 250.0
                + radius / 10.0
                + max(0, cluster - 1) * 0.8
            )
            if tool in _ROCKET_TOOLS:
                score += 0.35
            aim_offset = self._projectile_aim_offset(tool, distance)
            choices.append((score, tool, aim_offset))
        if not choices:
            return None

        _score, selected, aim_offset = max(
            choices,
            key=lambda choice: (choice[0], -choice[1]),
        )
        state.next_oriented_at = (
            float(now)
            + 5.5
            + (1.0 - float(profile.creativity)) * 4.0
            + (int(observer.player_id) % 3) * 0.6
        )
        self._team_oriented_ready_at[int(observer.team)] = (
            float(now) + _TEAM_ORIENTED_SPACING_SECONDS
        )
        return int(selected), float(aim_offset)

    @staticmethod
    def _projectile_aim_offset(tool: int, distance: float) -> float:
        """Return a conservative upward z offset for projectile drop."""

        spec = PROJECTILE_SPECS.get(int(tool))
        speed = float(_ORIENTED_SPEEDS.get(int(tool), 0.0))
        if spec is None or speed <= 1e-6:
            return 0.0
        travel_time = max(0.0, float(distance)) / speed
        drop = (
            0.5
            * BASE_GRAVITY
            * max(0.0, float(spec.gravity_mult))
            * travel_time
            * travel_time
        )
        cap = 9.0 if str(spec.behavior) in {"bounce", "stick"} else 6.0
        # AoS world z grows downward, so a negative offset aims upward.
        return -min(cap, drop)

    @staticmethod
    def _friendly_launch_lane_clear(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: Vector3,
    ) -> bool:
        """Reject a projectile when a teammate crosses the firing lane."""

        start = observer.eye
        delta = tuple(float(target[index]) - float(start[index]) for index in range(3))
        length_squared = sum(value * value for value in delta)
        if length_squared <= 1e-6:
            return False
        for player in frame.players:
            if (
                int(player.player_id) == int(observer.player_id)
                or int(player.team) != int(observer.team)
                or not player.alive
                or not player.spawned
            ):
                continue
            relative = tuple(
                float(player.eye[index]) - float(start[index])
                for index in range(3)
            )
            fraction = sum(
                relative[index] * delta[index] for index in range(3)
            ) / length_squared
            if not 0.03 < fraction < 0.97:
                continue
            closest = tuple(
                float(start[index]) + delta[index] * fraction
                for index in range(3)
            )
            if _distance_squared(player.eye, closest) <= 2.25 ** 2:
                return False
        return True

    @staticmethod
    def _explosive_target_safe(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        position: Vector3,
        tool: int,
        *,
        ignore_observer: bool,
    ) -> bool:
        """Keep teammates and active blast volumes outside a new explosion."""

        spec = PROJECTILE_SPECS.get(int(tool))
        if spec is None:
            return False
        radius = max(0.0, float(spec.blast_radius))
        if radius <= 0.0:
            return False
        if (
            not ignore_observer
            and _distance_squared(observer.position, position)
            <= (radius + 3.0) ** 2
        ):
            return False
        for player in frame.players:
            if (
                int(player.player_id) == int(observer.player_id)
                or int(player.team) != int(observer.team)
                or not player.alive
                or not player.spawned
            ):
                continue
            if _distance_squared(player.position, position) <= (radius + 1.5) ** 2:
                return False
        for entity in frame.entities:
            if not entity.alive or not entity.hazardous:
                continue
            other_radius = max(0.0, float(entity.blast_radius))
            if _distance_squared(entity.position, position) <= (
                radius + other_radius + 1.0
            ) ** 2:
                return False
        return True

    def _medic_support_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        now: float,
    ) -> BotIntent | None:
        """Move into real placement range of the most injured nearby ally."""

        if (
            int(C.MEDPACK_TOOL) not in observer.loadout
            or now < state.next_support_at
        ):
            return None
        wounded = tuple(
            player
            for player in frame.players
            if int(player.team) == int(observer.team)
            and int(player.player_id) != int(observer.player_id)
            and player.alive
            and player.spawned
            and int(player.health) <= 70
            and _distance_squared(observer.position, player.position) <= 30.0 ** 2
        )
        if not wounded:
            return None
        patient = min(
            wounded,
            key=lambda player: (
                int(player.health),
                _distance_squared(observer.position, player.position),
            ),
        )
        if self._deployable_near(
            frame,
            int(C.MEDPACK_TOOL),
            patient.position,
            radius=7.0,
        ):
            state.next_support_at = float(now) + 3.0
            return None
        distance = math.dist(observer.position, patient.position)
        if distance <= 4.25 and observer.grounded:
            self._set_goal(state, None, observer.position, now)
            state.next_support_at = float(now) + 10.0
            return self._intent(
                frame,
                movement=MovementIntent(crouch=True),
                look=LookIntent(patient.position, visible=False),
                tool_id=int(C.MEDPACK_TOOL),
                action=BotAction(
                    BotActionKind.DEPLOY,
                    tool_id=int(C.MEDPACK_TOOL),
                    position=patient.position,
                    face=4,
                ),
                priority=BotIntentPriority.ROUTINE,
                debug_goal=patient.position,
                debug_role="medic_place_medpack",
            )
        goal = _Goal(
            ("support", int(patient.player_id), int(patient.generation)),
            patient.position,
            "medic_support",
            3.5,
            True,
        )
        return self._navigation_intent(
            frame,
            observer,
            state,
            goal,
            now,
        )

    def _combat_jetpack_hop(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, target: PlayerSnapshot,
        state: _BotState, profile: BotProfile, now: float,
    ) -> BotIntent | None:
        """Let a pack owner leap to a new angle mid-fight, as Rocketeers do.

        The hop reuses the validated traversal flight: a fuel reserve, a known
        dry landing, bounded thrust and a thrust-free native descent. It fires
        when the bot is being hit, or now and then for bolder temperaments.
        """

        if (now < state.next_combat_hop_at or now < state.next_flight_at
                or MovementAffordance.JETPACK not in _movement_abilities(observer)
                or math.dist(observer.position, target.position) < 9.0):
            return None
        pressured = 0.0 <= now - observer.last_damage_at <= 1.0
        whim = tactical_mix(observer.player_id, observer.life_id, int(now / 2.5))
        if not pressured and whim > 0.10 + 0.25 * profile.creativity:
            return None
        state.next_combat_hop_at = now + 2.5
        heading = relocation_heading(self.world, observer, target, profile, state.engagement)
        if heading is None:
            return None
        state.engagement.relocations += 1
        for reach in (7.0, 5.5):
            x = observer.position[0] + heading[0] * reach
            y = observer.position[1] + heading[1] * reach
            surface = self.world.surface(int(math.floor(x)), int(math.floor(y)),
                                         observer.position[2], vertical_span=3,
                                         allow_water=False)
            if surface is None or abs(surface.position[2] - observer.position[2]) > 2.5:
                continue
            apex = (observer.position[0], observer.position[1], observer.position[2] - 3.0)
            if not (self.world.has_line_of_sight(observer.eye, apex)
                    and self.world.has_line_of_sight(apex, surface.position)):
                continue  # a roof or wall is in the way of the arc
            state.next_combat_hop_at = now + 9.0 + 6.0 * profile.caution
            state.flight_step = RouteStep(surface.position, MovementAffordance.JETPACK)
            state.flight_source = observer.position
            state.flight_started_at = now
            state.flight_departed = False
            state.flight_watch = target.eye
            self._set_goal(state, None, observer.position, now)
            return self._flight_intent(frame, observer, state, now)
        return None

    def _safe_tactical_hop(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        movement: MovementIntent, now: float,
    ) -> bool:
        """Vary grounded combat movement only over a clear, supported runway."""
        profile = frame.profile or _fallback_profile(observer.player_id)
        if (not observer.grounded or observer.wade or movement.crouch
                or movement.affordance is not MovementAffordance.WALK
                or profile.aggression < 0.4 or now < state.next_hop_at
                or observer.reloading or observer.health > 75
                or observer.last_damage_at <= state.last_hop_damage_at
                or not 0.0 <= now - observer.last_damage_at <= 0.8
                or math.hypot(*movement.direction[:2]) < 0.5):
            return False
        solid = getattr(self.world, "solid", None)
        if not callable(solid):
            return False
        for distance in (0.0, 1.0, 2.0, 3.0, 4.0):
            x = observer.position[0] + movement.direction[0] * distance
            y = observer.position[1] + movement.direction[1] * distance
            surface = self.world.surface(int(x), int(y), observer.position[2],
                                         vertical_span=1, allow_water=False)
            if surface is None or abs(surface.position[2] - observer.position[2]) > 0.3:
                return False
            for dx in (-0.45, 0.45):
                for dy in (-0.45, 0.45):
                    if any(solid(int(x + dx), int(y + dy), z) for z in
                           range(surface.support_z - 5, surface.support_z - 1)):
                        return False
        # Identity and temperament vary the cadence, never new randomness each
        # frame. The native motor still owns jump rearming and normal physics.
        state.last_hop_damage_at = observer.last_damage_at
        state.next_hop_at = now + 3.5 + profile.caution * 2.0 + (observer.player_id * 0.37) % 0.7
        return True

    def _prefab_cover_intent(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        towards: Vector3, now: float,
    ) -> BotIntent | None:
        """Build affordable cover on open ground without sealing an approach."""
        if (now < state.next_cover_at or not observer.grounded or observer.wade
                or int(C.PREFAB_TOOL) not in observer.loadout
                or int(observer.class_id) in _ZOMBIE_CLASSES):
            return None
        names = sorted(name for name in observer.prefabs
            if "wall" in name and bot_prefab_is_suitable(name, "cover")
            and (bot_prefab_block_count(name) or 9999) <= observer.blocks)
        if not names:
            return None
        direction = _normalized_xy(towards[0] - observer.position[0], towards[1] - observer.position[1])
        if math.hypot(*direction[:2]) < 0.1:
            angle = (observer.player_id * 2.39996) % (2 * math.pi)
            direction = (math.cos(angle), math.sin(angle), 0.0)
        target = (observer.position[0] + direction[0] * 3.0,
                  observer.position[1] + direction[1] * 3.0, observer.position[2])
        surface = self.world.surface(int(target[0]), int(target[1]), target[2],
                                     vertical_span=1, allow_water=False)
        if surface is None or abs(surface.position[2] - target[2]) > 0.5:
            return None
        # Require open lateral exits and leave objectives and teammates clear.
        for sign in (-1, 1):
            exit_x, exit_y = target[0] - direction[1] * 3 * sign, target[1] + direction[0] * 3 * sign
            if self.world.surface(int(exit_x), int(exit_y), target[2],
                                  vertical_span=1, allow_water=False) is None:
                return None
        if any(p.player_id != observer.player_id and p.alive
               and math.dist(p.position, target) < 3.0 for p in frame.players):
            return None
        if any(o.kind != "team_anchor" and math.dist(o.position, target) < 4.0 for o in frame.objectives):
            return None
        name = names[observer.player_id % len(names)]
        state.next_cover_at = now + 18.0 + (observer.player_id % 5) * 2.0
        return self._intent(frame, movement=MovementIntent(crouch=True),
            look=LookIntent(target, visible=False), tool_id=int(C.PREFAB_TOOL),
            action=BotAction(BotActionKind.PLACE_PREFAB, tool_id=int(C.PREFAB_TOOL),
                             position=target, argument=name, yaw=math.atan2(direction[1], direction[0])),
            priority=BotIntentPriority.TRAVERSAL, debug_role="fortify_prefab_cover")

    def _strategic_deploy_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        now: float,
        *,
        decision: ModeBotDecision | None = None,
    ) -> BotIntent | None:
        """Place durable gadgets only where they support an active route."""

        if now < state.next_deploy_at or not observer.grounded:
            return None
        if decision is None:
            decision = self.mode_policy.decide(frame, observer)
        if (decision is not None and decision.directive == "fortify"
                and math.dist(observer.position, decision.position) <= 14.0):
            cover = self._prefab_cover_intent(frame, observer, state, decision.position, now)
            if cover is not None:
                return cover
        if state.contact_position is not None and now < state.contact_until:
            strategic_position = state.contact_position
            strategic_role = "recent_contact"
        elif decision is not None:
            strategic_position = tuple(float(value) for value in decision.position)
            strategic_role = str(decision.role)
        else:
            return None
        distance = math.dist(observer.position, strategic_position)
        placement = tuple(float(value) for value in observer.position)
        owned_loadout = {int(tool) for tool in observer.loadout}
        selected = -1
        max_distance = 0.0
        cooldown = 20.0

        if int(C.ROCKET_TURRET_TOOL) in owned_loadout:
            selected = int(C.ROCKET_TURRET_TOOL)
            max_distance = 18.0
            cooldown = 24.0
        elif int(C.RADAR_STATION_TOOL) in owned_loadout:
            selected = int(C.RADAR_STATION_TOOL)
            max_distance = 22.0
            cooldown = 35.0
        elif int(C.LANDMINE_TOOL) in owned_loadout:
            selected = int(C.LANDMINE_TOOL)
            max_distance = 10.0 if strategic_role != "recent_contact" else 20.0
            cooldown = 14.0
        elif (
            int(C.DISGUISE_TOOL) in owned_loadout
            and any(
                int(objective.team) >= 0
                and int(objective.team) != int(observer.team)
                for objective in frame.objectives
            )
        ):
            selected = int(C.DISGUISE_TOOL)
            max_distance = 45.0
            cooldown = 45.0
        if selected < 0 or distance > max_distance:
            return None

        owned_count = sum(
            1
            for entity in frame.entities
            if entity.alive
            and int(entity.tool_id) == selected
            and int(entity.owner_id) == int(observer.player_id)
        )
        limits = {
            int(C.ROCKET_TURRET_TOOL): 2,
            int(C.RADAR_STATION_TOOL): 1,
            int(C.LANDMINE_TOOL): 3,
        }
        if owned_count >= limits.get(selected, 1):
            state.next_deploy_at = float(now) + 8.0
            return None
        spacing = {
            int(C.ROCKET_TURRET_TOOL): 12.0,
            int(C.RADAR_STATION_TOOL): 18.0,
            int(C.LANDMINE_TOOL): 7.0,
        }.get(selected, 0.0)
        if spacing > 0.0 and self._deployable_near(
            frame,
            selected,
            placement,
            radius=spacing,
        ):
            state.next_deploy_at = float(now) + 6.0
            return None
        if selected == int(C.LANDMINE_TOOL) and not self._explosive_target_safe(
            frame,
            observer,
            placement,
            selected,
            ignore_observer=True,
        ):
            state.next_deploy_at = float(now) + 4.0
            return None

        state.next_deploy_at = float(now) + cooldown
        yaw = math.atan2(
            float(strategic_position[1]) - float(observer.position[1]),
            float(strategic_position[0]) - float(observer.position[0]),
        )
        position = None if selected == int(C.DISGUISE_TOOL) else placement
        return self._intent(
            frame,
            movement=MovementIntent(crouch=True),
            look=LookIntent(strategic_position, visible=False),
            tool_id=selected,
            action=BotAction(
                BotActionKind.DEPLOY,
                tool_id=selected,
                position=position,
                yaw=yaw,
            ),
            priority=BotIntentPriority.ROUTINE,
            debug_goal=strategic_position,
            debug_role=f"deploy_{selected}_{strategic_role}",
        )

    def _objective_mine_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        now: float,
    ) -> BotIntent | None:
        """Mine a safe nearby surface ring for Diamond Mine discovery rolls."""

        if now + 1e-9 < state.next_breach_at:
            return None
        melee = _melee_tool(observer)
        if melee is None:
            return None
        base_x = int(math.floor(observer.position[0]))
        base_y = int(math.floor(observer.position[1]))
        surface_z = int(round(observer.position[2] + 2.25))
        phase = (int(observer.player_id) + int(now * 2.0)) & 7
        offsets = (
            (2, 0),
            (2, 1),
            (0, 2),
            (-1, 2),
            (-2, 0),
            (-2, -1),
            (0, -2),
            (1, -2),
        )
        cell = next(
            (
                (base_x + dx, base_y + dy, surface_z)
                for index in range(len(offsets))
                for dx, dy in (offsets[(phase + index) % len(offsets)],)
                if self.world.solid(base_x + dx, base_y + dy, surface_z)
            ),
            None,
        )
        if cell is None:
            return None
        state.next_breach_at = float(now) + max(
            0.35,
            float(getattr(C, "PICKAXE_SHOOT_INTERVAL", 0.4)),
        )
        target = tuple(float(value) + 0.5 for value in cell)
        return self._intent(
            frame,
            movement=MovementIntent(
                crouch=True,
                affordance=MovementAffordance.BREACH,
            ),
            look=LookIntent(target, visible=False),
            tool_id=int(melee),
            action=BotAction(
                BotActionKind.MELEE,
                tool_id=int(melee),
                position=target,
            ),
            priority=BotIntentPriority.ROUTINE,
            debug_goal=target,
            debug_role="diamond_mine_blocks",
        )

    @staticmethod
    def _deployable_near(
        frame: PerceptionFrame,
        tool: int,
        position: Vector3,
        *,
        radius: float,
    ) -> bool:
        return any(
            entity.alive
            and int(entity.tool_id) == int(tool)
            and _distance_squared(entity.position, position) <= float(radius) ** 2
            for entity in frame.entities
        )

    def _navigation_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        goal: _Goal,
        now: float,
        *,
        water_context: bool | None = None,
    ) -> BotIntent:
        """Plan/follow one route and invalidate a concrete failed edge."""

        if state.flight_step is not None:
            flight = self._flight_intent(frame, observer, state, now)
            if flight is not None:
                return flight
        effective_wading = (
            bool(observer.wade)
            if water_context is None
            else bool(water_context)
        )

        held = state.goal
        if (held is not None and held.key != goal.key
                and held.role in _CASUAL_ERRANDS and goal.role in _CASUAL_ERRANDS
                and now - state.goal_since < _ERRAND_COMMITMENT_SECONDS
                and math.hypot(held.position[0] - observer.position[0],
                               held.position[1] - observer.position[1])
                > held.arrival_radius + 2.0):
            # Pushing on, checking a noise and following a last sighting are
            # all "go and look over there". Swapping between them once a second
            # discarded the route each time and could turn the bot round in
            # its tracks. Finish a few seconds of the errand in hand; a
            # visible enemy, an objective or an order never waits for this.
            goal = held
        elif held is None or held.key != goal.key:
            state.goal_since = now
        self._set_goal(state, goal, observer.position, now)
        if state.planning_wait_at is not None:
            # Pause only scheduling time; denial is not physical progress and
            # must not postpone strategic corridor guidance indefinitely.
            delay = max(0.0, now - state.planning_wait_at)
            state.waypoint_progress_at += delay
            state.navigation_progress_at += delay
            state.navigation_window_at += delay
            state.navigation_coverage_at += delay
            if state.breach_key is not None:
                state.breach_started_at += delay
            state.planning_wait_at = None
        if state.escape_goal is not None and (
            now >= state.escape_until
            or math.dist(observer.position, state.escape_goal) <= 1.25
        ):
            state.escape_goal = None
            self._clear_route(state, now)
        previous_position = state.navigation_previous_position
        state.navigation_previous_position = observer.position
        active_goal = state.goal
        if active_goal is None:
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                debug_role="idle_goal_reset",
            )

        if state.navigation_progress_position is None:
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = float(now)
        elif math.hypot(
            float(observer.position[0])
            - float(state.navigation_progress_position[0]),
            float(observer.position[1])
            - float(state.navigation_progress_position[1]),
        ) >= _NAVIGATION_PROGRESS_DISTANCE:
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = float(now)
        if state.navigation_window_position is None:
            state.navigation_window_position = observer.position
            state.navigation_window_at = float(now)
        elif math.hypot(
            float(observer.position[0])
            - float(state.navigation_window_position[0]),
            float(observer.position[1])
            - float(state.navigation_window_position[1]),
        ) >= _NAVIGATION_WINDOW_DISTANCE:
            state.navigation_window_position = observer.position
            state.navigation_window_at = float(now)

        goal_distance = math.hypot(
            float(active_goal.position[0]) - float(observer.position[0]),
            float(active_goal.position[1]) - float(observer.position[1]),
        )
        if (goal_distance <= active_goal.arrival_radius and not effective_wading
                and abs(float(active_goal.position[2]) - float(observer.position[2])) <= 3.0):
            if active_goal.role == "chase_last_seen":
                state.contact_until = 0.0
                state.contact_position = None
            state.route = ()
            state.route_index = 0
            watch = active_goal.watch_position
            watch_look = None
            if watch is not None:
                # Watch the public approach from eye height, not the nearby
                # protected actor's feet or a distant map layer through walls.
                heading = _normalized_xy(watch[0] - observer.position[0],
                                         watch[1] - observer.position[1])
                if math.hypot(*heading[:2]) > 0.1:
                    watch_look = LookIntent(self._navigation_look_target(observer, heading))
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=watch_look,
                tool_id=_weapon_tool(observer),
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:arrived",
            )

        repeated_coverage = self._navigation_revisits(state, observer.position, active_goal, now)
        current_step = (state.route[state.route_index]
                        if state.route_index < len(state.route) else None)
        if current_step is not None and current_step.affordance not in {
                MovementAffordance.WALK, MovementAffordance.CROUCH,
                MovementAffordance.JUMP, MovementAffordance.DROP}:
            # Digging/traversal has its own authoritative progress contract.
            state.navigation_coverage_at = now
        elif repeated_coverage and state.escape_search is None:
            # Valid movement can still repeat the same closed loop forever.
            # Exclude this approach briefly, then use the bounded recovery
            # planner. Retain visited cells through that escape and goal swaps.
            self._invalidate_current_edge(state, observer.position, now, lifetime=12.0)
            self._clear_route(state, now)
            state.escape_goal = None
            if state.corridor_search is None or state.corridor_search.done:
                state.corridor_search = None
                state.corridor_join_index = 0
                state.corridor_yield_local = False
            # _invalidate_current_edge already teaches a pending search this
            # exclusion. Preserve its frontier: repeated local recovery must
            # not restart the long detour that can actually leave this shelf.
            state.corridor_retry_at = now
            state.navigation_coverage_at = now
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = now
            state.navigation_window_position = observer.position
            state.navigation_window_at = now
            self._escape_empty_route(frame, observer, state, now)
            return self._intent(frame, movement=MovementIntent(), look=None,
                tool_id=_weapon_tool(observer), priority=BotIntentPriority.TRAVERSAL,
                debug_goal=active_goal.position, debug_role=active_goal.role + ":route_cycle")

        if state.escape_search is not None:
            # Resume the next recovery candidate before ordinary routing can
            # spend this observer's only grant again.
            self._escape_empty_route(frame, observer, state, now)
            if state.escape_search is not None:
                return self._planning_wait_intent(frame, observer, state, active_goal, now)

        if (
            state.route_index < len(state.route)
            and state.route[state.route_index].affordance
            is not MovementAffordance.BREACH
            and float(now) - float(state.navigation_window_at)
            >= _NAVIGATION_WINDOW_SECONDS
        ):
            # Queueing behind a digger and crowd detours are evaluated before
            # ordinary route execution. They must not postpone the hard
            # physical-progress contract indefinitely: macOS ARM reproduced
            # a live WALK edge that remained trapped for nine seconds because
            # those early branches kept bypassing the later timeout.
            self._invalidate_current_edge(state, observer.position, now)
            self._clear_route(state, now)
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = float(now)
            state.navigation_window_position = observer.position
            state.navigation_window_at = float(now)
            self._escape_empty_route(frame, observer, state, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:physical_edge_blocked",
            )

        crowd_detour = self._crowd_detour_intent(
            frame,
            observer,
            state,
            active_goal,
            goal_distance,
            now,
        )
        if crowd_detour is not None:
            return crowd_detour

        active_digger = self._active_breach_digger(
            frame,
            observer,
            now,
        )
        if active_digger is not None:
            return self._breach_assist_queue_intent(
                frame,
                observer,
                active_goal,
                active_digger,
            )

        corridor_goal = self._corridor_segment_goal(state, observer, active_goal, now)
        if goal_distance + 1.0 < state.goal_best_distance:
            state.goal_best_distance = goal_distance
            state.goal_progress_at = now
        # A legitimate route around a wall can move away from its destination
        # for much longer than six seconds. Lack of straight-line progress
        # requests map-wide guidance; only failed physical edges are excluded.

        topology_changed = (
            bool(state.route)
            and int(state.route_topology_version)
            != int(frame.topology_version)
        )
        if topology_changed:
            local_check = getattr(self.world, "route_needs_replan", None)
            # A body that is getting somewhere keeps its route through edits
            # elsewhere on the map. One that has not physically moved on for
            # a few seconds takes any edit as a reason to think again.
            # (Straight-line goal distance is no measure: every honest detour
            # stalls it.)
            stuck = now - state.navigation_window_at >= _STUCK_REPLAN_SECONDS
            if (callable(local_check)
                    and not local_check(state.route_topology_version, observer.position,
                                        state.route[state.route_index:], stuck=stuck)):
                # A teammate editing distant terrain does not invalidate this
                # unchanged walking corridor or spend another planning grant.
                state.route_topology_version = int(frame.topology_version)
                topology_changed = False
        if (topology_changed and state.route_index < len(state.route)
                and state.route[state.route_index].affordance in {
                    MovementAffordance.WALK, MovementAffordance.CROUCH}
                and state.escape_goal is None):
            available = getattr(self.world, "planning_available", None)
            if callable(available) and not available():
                # Terrain changed near the route but the planner is busy. A
                # player keeps walking and adapts; stopping dead for a queue
                # is what made whole squads freeze and restart in firefights.
                # The stale version makes the next decision ask again, and the
                # motor's live probes still guard the actual ground.
                topology_changed = False
        if topology_changed and state.breach_key is not None:
            # Removing a planned wall cell is real strategic progress even
            # though the bot has not moved closer to the goal yet.
            state.goal_progress_at = now

        if (
            not state.route
            or state.route_index >= len(state.route)
            or topology_changed
        ):
            previous_step = (
                state.route[state.route_index]
                if topology_changed
                and state.route_index < len(state.route)
                else None
            )
            previous_waypoint_best_distance = state.waypoint_best_distance
            previous_waypoint_progress_at = state.waypoint_progress_at
            previous_breach_key = state.breach_key
            previous_breach_started_at = state.breach_started_at
            previous_next_breach_at = state.next_breach_at
            personality = self._traversal_personality(frame, observer)
            if state.dry_detour_goal is not None and (
                float(now) >= float(state.dry_detour_until)
                or math.hypot(
                    float(state.dry_detour_goal[0])
                    - float(observer.position[0]),
                    float(state.dry_detour_goal[1])
                    - float(observer.position[1]),
                )
                <= 2.5
            ):
                state.dry_detour_goal = None
                state.dry_detour_until = 0.0
            crowd_detour_active = bool(
                state.crowd_detour_goal is not None
                and float(now) < float(state.crowd_detour_until)
            )
            planning_goal = state.escape_goal
            if planning_goal is None:
                planning_goal = (
                    state.crowd_detour_goal if crowd_detour_active
                    else state.dry_detour_goal or corridor_goal
                )
            if planning_goal is None:
                planning_goal = self._team_lane_segment_goal(
                    frame, observer, active_goal.position,
                )
            assert planning_goal is not None
            plan_arguments = {
                "abilities": (_movement_abilities(observer) if now >= state.next_flight_at
                              else _movement_abilities(observer) - {MovementAffordance.JETPACK}),
                "dig_profile": _dig_profile(observer),
                "blocked_edges": frozenset(state.blocked_edges),
            }
            dry_state = (state.dry_detour_goal, state.dry_detour_until,
                         state.dry_route_failures)
            context = (observer.position, frame.topology_version,
                       active_goal.position, planning_goal, state.escape_goal,
                       dry_state, effective_wading, personality.style,
                       frozenset(plan_arguments["abilities"]),
                       plan_arguments["dig_profile"], plan_arguments["blocked_edges"])
            if context != state.planning_context:
                state.planning_context = context
                state.planning_results.clear()

            def request_plan(start, destination, **arguments):
                # Preserve the completed first query until its fallback receives
                # a grant. Otherwise every new decision repeats that first query
                # and permanently starves its dry/wet alternative.
                if getattr(self.world, "planning_budget", None) is None:
                    state.corridor_yield_local = False
                    return self.world.plan(start, destination, **arguments)
                key = (start, destination, tuple(sorted(
                    (name, frozenset(value) if isinstance(value, set) else value)
                    for name, value in arguments.items())))
                if key not in state.planning_results:
                    result = self.world.plan(start, destination, **arguments)
                    if not result.deferred:
                        state.corridor_yield_local = False
                    if not result.deferred and len(state.planning_results) < 4:
                        state.planning_results[key] = result
                    return result
                return state.planning_results[key]

            def wait_for_plan():
                # Failure counts and detours describe tested geometry, not a
                # second query still waiting for worker time.
                (state.dry_detour_goal, state.dry_detour_until,
                 state.dry_route_failures) = dry_state
                return self._planning_wait_intent(frame, observer, state, active_goal, now)

            if state.escape_goal is not None:
                # Terrain changes replan the active escape with the same
                # movement contract. Falling back to ordinary routing here
                # silently removed DROP/dig/swim on the very next block edit.
                plan_arguments["abilities"] |= {MovementAffordance.DROP}
                plan = request_plan(
                    observer.position, planning_goal,
                    allow_water=effective_wading or state.escape_allow_water,
                    _prefer_existing_path=False,
                    **plan_arguments,
                )
            elif effective_wading:
                plan = request_plan(
                    observer.position,
                    planning_goal,
                    allow_water=True,
                    **plan_arguments,
                )
            elif personality.style is _TraversalStyle.SWIM:
                # This identity accepts a genuinely shorter water crossing.
                # Water edges remain 2.75x walking cost in the voxel planner,
                # so authored roads still win unless the detour is material.
                state.dry_detour_goal = None
                state.dry_detour_until = 0.0
                state.dry_route_failures = 0
                plan = request_plan(
                    observer.position,
                    planning_goal,
                    allow_water=True,
                    **plan_arguments,
                )
                if plan.deferred:
                    return wait_for_plan()
                if not _plan_can_advance(plan, observer.position):
                    # A swimmer's preference must not disable excavation.
                    # Near the water plane, wet and dry searches can select
                    # different floor layers; a failed wet graph does not
                    # prove the dry pocket has no diggable exit.
                    plan = request_plan(
                        observer.position, planning_goal, allow_water=False,
                        **plan_arguments,
                    )
            else:
                # Dry-route and builder identities search the shoreline
                # before conceding to a swim. A bounded A* can reach the local
                # minimum at the water's edge without proving there is no
                # surface route around it, so an empty segment first creates
                # a stable lateral dry target instead of entering immediately.
                dry_plan = request_plan(
                    observer.position,
                    planning_goal,
                    allow_water=False,
                    **plan_arguments,
                )
                if dry_plan.deferred:
                    return wait_for_plan()
                if _plan_can_advance(dry_plan, observer.position):
                    plan = dry_plan
                    if (
                        state.dry_detour_goal is None
                        and dry_plan.reached_segment_goal
                    ):
                        state.dry_route_failures = 0
                else:
                    bridge = (
                        self._water_bridge_intent(
                            frame,
                            observer,
                            state,
                            active_goal,
                            now,
                        )
                        if (
                            personality.style is _TraversalStyle.BRIDGE
                            and not crowd_detour_active
                        )
                        else None
                    )
                    if bridge is not None:
                        state.route = ()
                        state.route_index = 0
                        return bridge

                    detour_plan = RoutePlan((), False, 0)
                    if (
                        not crowd_detour_active
                        and state.dry_route_failures
                        < _DRY_DETOURS_BEFORE_SWIM
                    ):
                        state.dry_route_failures += 1
                        state.dry_detour_goal = self._dry_detour_segment_goal(
                            observer,
                            active_goal.position,
                            personality,
                            state.dry_route_failures,
                        )
                        state.dry_detour_until = (
                            float(now) + _DRY_DETOUR_SECONDS
                        )
                        detour_plan = request_plan(
                            observer.position,
                            state.dry_detour_goal,
                            allow_water=False,
                            **plan_arguments,
                        )
                        if detour_plan.deferred:
                            return wait_for_plan()
                    plan = (
                        detour_plan
                        if _plan_can_advance(detour_plan, observer.position)
                        else request_plan(
                            observer.position,
                            planning_goal,
                            allow_water=True,
                            **plan_arguments,
                        )
                    )
                    if not _plan_can_advance(detour_plan, observer.position):
                        state.dry_detour_goal = None
                        state.dry_detour_until = 0.0
            if plan.deferred:
                return wait_for_plan()
            state.planning_context = None
            state.planning_results.clear()
            self._reset_breach(state, now)
            if state.escape_goal is not None and not _plan_can_advance(plan, observer.position):
                # A terrain replan can reduce an escape to the occupied cell.
                # Retaining that destination repeatedly "completes" a zero
                # length segment without ever attempting a different exit.
                state.escape_goal = None
                self._clear_route(state, now)
                if now >= state.escape_retry_at:
                    self._escape_empty_route(frame, observer, state, now)
                plan = replace(plan, steps=state.route)
            state.route = plan.steps
            # A fresh bounded segment needs no continuation in the very same
            # decision; ask again once the body is actually using it up.
            state.next_extension_at = now + 0.5
            # A bounded search that cannot reach its target offers its best
            # guess. When that guess is only a few blocks long, twice running,
            # the body is poking at a local dead end: a platform edge over a
            # goal far below, or a pyramid between it and a goal on the far
            # side, where bots climbed a terrace, met a lip, came down and went
            # up again. Stop poking and ask for the map-wide route at once
            # instead of after six seconds without progress.
            reach = remaining_distance(plan.steps, 0, observer.position)
            if (state.escape_goal is None and not plan.reached_segment_goal
                    and goal_distance > 12.0 and plan.steps and reach <= 5.0
                    and not any(step.breach is not None for step in plan.steps)):
                state.short_plans = (state.short_plans + 1
                                     if now - state.short_plan_at <= 6.0 else 1)
                state.short_plan_at = now
            elif plan.reached_segment_goal or reach > 8.0:
                state.short_plans = 0
            state.dead_end = state.short_plans >= 2
            if corridor_goal is not None and planning_goal == corridor_goal:
                # A stretch the detailed planner could not finish is retried
                # corner by corner; the first one it completes restores trust.
                state.corridor_reach = 8.0 if plan.reached_segment_goal else 0.0
            # Plans commonly begin with the current/nearby surface. Skip
            # those already-reached placeholders before comparing a topology
            # replan with the previous actionable edge. Comparing route[0]
            # reset the stall clock on every unrelated teammate dig and made
            # blocked Atlantis/GreatWall edges immortal.
            state.route_index = 0
            while state.route_index < len(state.route):
                candidate = state.route[state.route_index]
                if not _route_step_reached(
                    candidate,
                    observer.position,
                    wading=effective_wading,
                    previous_position=previous_position,
                    final_step=state.route_index == len(state.route) - 1,
                    require_current_cell=_requires_traversal_entry(state.route, state.route_index),
                ):
                    break
                state.route_index += 1
            state.route_topology_version = int(frame.topology_version)
            next_step = (
                state.route[state.route_index]
                if state.route_index < len(state.route)
                else None
            )
            if _same_traversal_step(previous_step, next_step):
                if (previous_step is not None and next_step is not None
                        and next_step.affordance is MovementAffordance.JUMP
                        and previous_step.entry_edge is not None):
                    # Unrelated terrain edits can replan while this jump is
                    # airborne. Preserve its committed takeoff with its clock.
                    state.route = (
                        *state.route[:state.route_index],
                        replace(next_step, entry_edge=previous_step.entry_edge),
                        *state.route[state.route_index + 1:],
                    )
                # An unrelated bot can mutate terrain anywhere on the map.
                # Replanning the exact same edge must not erase its progress
                # timer, or continuous distant excavation makes a genuinely
                # blocked edge immortal (reproduced at Mayan x=126/y=210).
                state.waypoint_best_distance = (
                    previous_waypoint_best_distance
                )
                state.waypoint_progress_at = previous_waypoint_progress_at
                state.breach_key = previous_breach_key
                state.breach_started_at = previous_breach_started_at
                state.next_breach_at = previous_next_breach_at
            else:
                state.waypoint_best_distance = math.inf
                state.waypoint_progress_at = now

        self._catch_up_walk_route(state, observer.position)
        while state.route_index < len(state.route):
            step = state.route[state.route_index]
            if not _route_step_reached(
                step,
                observer.position,
                wading=effective_wading,
                previous_position=previous_position,
                final_step=state.route_index == len(state.route) - 1,
                require_current_cell=_requires_traversal_entry(state.route, state.route_index),
            ):
                break
            state.route_index += 1
            state.waypoint_best_distance = math.inf
            state.waypoint_progress_at = now

        if not effective_wading:
            self._extend_route(frame, observer, state, active_goal, now)

        if state.route_index >= len(state.route):
            if (now - state.navigation_progress_at >= _NAVIGATION_PROGRESS_SECONDS
                    and now >= state.escape_retry_at):
                self._escape_empty_route(frame, observer, state, now)

        if state.route_index >= len(state.route):
            # A bounded segment ended before the strategic destination.
            # Replan from the new position on the next frame.
            state.route = ()
            return self._intent(
                frame,
                movement=self._coast(observer, state, active_goal, now),
                look=None,
                tool_id=_weapon_tool(observer),
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:segment_complete",
            )

        step = state.route[state.route_index]
        if (observer.grounded and not effective_wading
                and step.affordance is MovementAffordance.DROP
                and observer.position[2] - step.waypoint[2] > 1.6):
            # The plan was made a level higher: this drop's landing is now overhead.
            # Climbing back up to take a drop again is the opposite of getting
            # on with it. Plan from where the body really is.
            self._clear_route(state, now)
            return self._intent(
                frame, movement=self._coast(observer, state, active_goal, now), look=None,
                tool_id=_weapon_tool(observer), debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:fell_past_step")
        if step.affordance is MovementAffordance.BREACH:
            if effective_wading:
                # A strategic route may expose a valid but very long dig
                # through a waterfront structure. Once swimming, hand that
                # ownership to the dedicated bank recovery below; it limits
                # excavation to the adjacent body-height exit instead of
                # pinning the bot in water while it tunnels toward the goal.
                self._clear_route(state, now)
                state.water_recovery = True
                return self._intent(
                    frame,
                    movement=MovementIntent(),
                    look=None,
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.SURVIVAL,
                    debug_goal=active_goal.position,
                    debug_role=f"{active_goal.role}:water_breach_handoff",
                )
            return self._breach_intent(
                frame,
                observer,
                state,
                active_goal,
                step,
                now,
            )
        if (
            float(now) - float(state.navigation_progress_at)
            >= _NAVIGATION_PROGRESS_SECONDS
            or float(now) - float(state.navigation_window_at)
            >= _NAVIGATION_WINDOW_SECONDS
        ):
            self._invalidate_current_edge(state, observer.position, now)
            self._clear_route(state, now)
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = float(now)
            state.navigation_window_position = observer.position
            state.navigation_window_at = float(now)
            self._escape_empty_route(frame, observer, state, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:physical_edge_blocked",
            )
        state.yielded_breach_edge = None
        state.yielded_breach_started_at = float(now)
        self._reset_breach(state, now)
        waypoint_distance = math.hypot(
            float(step.waypoint[0]) - float(observer.position[0]),
            float(step.waypoint[1]) - float(observer.position[1]),
        )
        if waypoint_distance + 0.35 < state.waypoint_best_distance:
            state.waypoint_best_distance = waypoint_distance
            state.waypoint_progress_at = now
        elif now - state.waypoint_progress_at >= _WAYPOINT_STALL_SECONDS:
            if state.lookahead_active:
                # The straight walk met something the voxel check missed (a
                # teammate, a fresh block). Take this stretch cell by cell.
                state.lookahead_off_until = now + 8.0
            self._invalidate_current_edge(state, observer.position, now)
            self._clear_route(state, now)
            # Publish an explicit stop before replanning. Replacing one failed
            # edge with another movement intention in the same decision made
            # the authoritative motor look continuously active while a bot
            # cycled around an unreachable ledge.
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:edge_blocked",
            )

        direction = _normalized_xy(
            float(step.waypoint[0]) - float(observer.position[0]),
            float(step.waypoint[1]) - float(observer.position[1]),
        )
        affordance = step.affordance
        if affordance is MovementAffordance.JETPACK:
            state.flight_step = step
            state.flight_source = observer.position
            state.flight_started_at = now
            state.flight_departed = False
            state.flight_watch = None
            return self._flight_intent(frame, observer, state, now)
        motor_affordance = (
            MovementAffordance.SWIM
            if (
                effective_wading
                and affordance
                not in {
                    MovementAffordance.JUMP,
                    MovementAffordance.BUILD_STEP,
                    MovementAffordance.BREACH,
                }
            )
            else affordance
        )
        if (motor_affordance is MovementAffordance.WALK
                and 1.5 < step.waypoint[2] - observer.position[2] <= 4.0):
            # The live body may still be above the planner's source floor
            # after a jump or a neighbouring lip. Validate the actual descent
            # instead of rejecting every WALK until it somehow lands first.
            motor_affordance = MovementAffordance.DROP
        steer = step.waypoint
        state.lookahead_active = False
        state.step_note = (f"exact:{motor_affordance.value}" if motor_affordance not in {
            MovementAffordance.WALK, MovementAffordance.DROP} else
            "airborne" if not observer.grounded else
            "wading" if effective_wading else
            "off" if now < state.lookahead_off_until else
            f"blocked:{run_end(state.route, state.route_index) - state.route_index}:" + (
                state.route[state.route_index + 1].affordance.value
                if state.route_index + 1 < len(state.route) else "end"))
        sprint_allowed = (
            motor_affordance is MovementAffordance.SWIM
            or (motor_affordance is MovementAffordance.WALK
                and not step.waypoint[2] < observer.position[2] - 0.25
                and self._route_allows_sprint(state, observer)))
        walk_drop = 1
        if (motor_affordance in {MovementAffordance.WALK, MovementAffordance.DROP}
                and not effective_wading and now >= state.lookahead_off_until):
            # Steer at the far end of the straight stretch ahead, as a player
            # looks where they are going, instead of stopping at every cell.
            if observer.grounded:
                target_index = lookahead(self.world, state.route, state.route_index,
                                         observer.position, keep=state.lookahead_target)
            else:
                # Every step down a terrace is a moment in the air. The line
                # was validated from the ground a moment ago: keep running at
                # it rather than re-aiming at the cell underfoot mid-stride.
                target_index = held_index(state.route, state.route_index,
                                          state.lookahead_target)
            state.lookahead_target = (state.route[target_index].waypoint
                                      if target_index > state.route_index else None)
            if target_index > state.route_index:
                advanced = passed_index(state.route, state.route_index, target_index,
                                        observer.position)
                if advanced != state.route_index:
                    state.route_index = advanced
                    state.waypoint_best_distance = math.inf
                    state.waypoint_progress_at = now
                steer = state.route[target_index].waypoint
                state.lookahead_active = True
                state.step_note = "far"
                # Running off a ledge of a few blocks is ordinary movement
                # (falls only hurt from ten). The motor's walking gate is told
                # so only when this validated line really goes over one.
                levels = [observer.position[2], *(
                    item.waypoint[2] for item in state.route[state.route_index:target_index + 1])]
                if any(after - before > 1.05 for before, after in zip(levels, levels[1:])):
                    walk_drop = int(SAFE_DROP)
                motor_affordance = MovementAffordance.WALK
                direction = _normalized_xy(steer[0] - observer.position[0],
                                           steer[1] - observer.position[1])
                # Brake only for what needs an exact takeoff: a jump, drop,
                # dig or build right after this run. Hills are the native
                # movement model's business, not a reason to stroll.
                last = run_end(state.route, state.route_index)
                exact_step_next = last + 1 < len(state.route)
                to_run_end = math.hypot(
                    state.route[last].waypoint[0] - observer.position[0],
                    state.route[last].waypoint[1] - observer.position[1])
                sprint_allowed = not exact_step_next or to_run_end >= 4.5
            elif motor_affordance is MovementAffordance.DROP and runs_off(
                    self.world, step, observer.position):
                # A lone ledge is walked off like any other; no shuffle first.
                state.lookahead_active = True
                state.step_note = "run_off"
                walk_drop = int(SAFE_DROP)
                motor_affordance = MovementAffordance.WALK
        if (motor_affordance in {MovementAffordance.JUMP, MovementAffordance.DROP}
                and not effective_wading and observer.grounded):
            lineup = takeoff_alignment(step, observer.position)
            if lineup is not None:
                # Sidestep onto the edge's own line before committing to it.
                return self._intent(
                    frame,
                    movement=MovementIntent(
                        direction=_normalized_xy(lineup[0] - observer.position[0],
                                                 lineup[1] - observer.position[1]),
                        travel_source=observer.position, travel_waypoint=lineup,
                        # Eyes stay on the way ahead; the keys do the shuffle.
                        gaze_waypoint=(self._travel_gaze(state, observer, now)
                                       or step.waypoint)),
                    look=LookIntent(self._navigation_look_target(observer, direction),
                                    visible=False),
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_goal=active_goal.position,
                    debug_role=f"{active_goal.role}:line_up",
                )
            direction = edge_axis(step) or direction
        gaze = None
        if motor_affordance is MovementAffordance.WALK:
            state.travel_heading = direction
        if (not effective_wading
                and motor_affordance in {MovementAffordance.WALK, MovementAffordance.CROUCH,
                                         MovementAffordance.JUMP, MovementAffordance.DROP}):
            gaze = self._travel_gaze(state, observer, now)
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=direction,
                # Native swimming already moves horizontally at the water
                # plane. Holding jump on every SWIM tick produces the visible
                # London bob/stutter and slows the crossing. Pulse it only for
                # the concrete bank/ledge edge selected by the planner.
                jump=affordance is MovementAffordance.JUMP,
                crouch=affordance is MovementAffordance.CROUCH,
                sprint=active_goal.sprint and sprint_allowed,
                affordance=motor_affordance,
                travel_source=(observer.position if motor_affordance in {
                    MovementAffordance.WALK, MovementAffordance.CROUCH,
                    MovementAffordance.JUMP, MovementAffordance.DROP} else None),
                travel_waypoint=(steer if motor_affordance in {
                    MovementAffordance.WALK, MovementAffordance.CROUCH,
                    MovementAffordance.JUMP, MovementAffordance.DROP} else None),
                gaze_waypoint=gaze,
                walk_drop=walk_drop,
            ),
            look=LookIntent(self._navigation_look_target(observer, direction), visible=False),
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.TRAVERSAL,
            debug_goal=active_goal.position,
            debug_path=(
                observer.position,
                *(
                    route_step.waypoint
                    for route_step in state.route[
                        state.route_index:state.route_index + 8
                    ]
                ),
            ),
            debug_role=active_goal.role,
        )

    def _extend_route(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                      state: _BotState, goal: _Goal, now: float) -> None:
        """Plan the next stretch before this one runs out.

        Bounded segments used to end in a full stop while the next plan was
        requested, several times a minute per bot. Appending the continuation
        from the segment's last cell keeps the body moving. Only ordinary dry
        travel is extended; escapes, detours, corridors and special edges
        keep their own owners, and a denied or failed query changes nothing.
        """

        route = state.route
        if (not route or state.route_index >= len(route) or now < state.next_extension_at
                or state.escape_goal is not None or state.corridor
                or state.corridor_search is not None or state.dry_detour_goal is not None
                or state.crowd_detour_goal is not None
                or route[-1].affordance is not MovementAffordance.WALK
                or int(state.route_topology_version) != int(frame.topology_version)):
            return
        last = route[-1].waypoint
        if (math.hypot(goal.position[0] - last[0], goal.position[1] - last[1])
                <= goal.arrival_radius + 2.0
                or remaining_distance(route, state.route_index, observer.position) > 12.0):
            return
        state.next_extension_at = now + 0.35
        target = self._team_lane_segment_goal(frame, replace(observer, position=last),
                                              goal.position)
        plan = self.world.plan(
            last, target,
            abilities=_movement_abilities(observer) - {MovementAffordance.JETPACK},
            dig_profile=_dig_profile(observer), allow_water=False,
            blocked_edges=frozenset(state.blocked_edges))
        if plan.deferred:
            return
        if not _plan_can_advance(plan, last):
            state.next_extension_at = now + 3.0
            return
        state.route = (*route, *plan.steps)

    @staticmethod
    def _catch_up_walk_route(state: _BotState, position: Vector3) -> None:
        """A body already on a later ordinary landing must not orbit an old one.

        Native stepping and momentum can carry the body past a one-cell
        terrace between worker snapshots. Match an actually occupied later
        landing rather than relaxing the missed landing's height tolerance.
        Never skip an unexecuted jump, drop, excavation or other special edge.
        """
        current = state.route_index
        for index in range(current, min(len(state.route), current + 8)):
            step = state.route[index]
            if step.affordance not in {MovementAffordance.WALK, MovementAffordance.CROUCH}:
                break
            if index > current and _route_step_reached(
                    step, position, wading=False, require_current_cell=True):
                state.route_index = index

    @staticmethod
    def _route_allows_sprint(state: _BotState, observer: PlayerSnapshot) -> bool:
        """Reserve native braking distance before turns, steps and landings."""
        # The live motor uses this same velocity scale: 0.35 needs about four
        # blocks to brake. A short approach needs walking even from rest, or
        # sprint acceleration creates the AncientEgypt missed-waypoint orbit.
        distance_needed = min(5.0, max(3.0, 0.65 + math.hypot(*observer.velocity[:2]) * 10.0))
        previous = observer.position
        direction = None
        distance = 0.0
        for step in state.route[state.route_index:state.route_index + 8]:
            if (step.affordance is not MovementAffordance.WALK
                    or abs(step.waypoint[2] - previous[2]) > 0.25):
                return False
            dx, dy = step.waypoint[0] - previous[0], step.waypoint[1] - previous[1]
            length = math.hypot(dx, dy)
            if length > 1e-6:
                heading = (dx / length, dy / length)
                if direction is None:
                    direction = heading
                elif direction[0] * heading[0] + direction[1] * heading[1] < 0.94:
                    return False
                distance += length
                if distance >= distance_needed:
                    return True
            previous = step.waypoint
        return False

    @staticmethod
    def _navigation_look_target(observer: PlayerSnapshot, direction: Vector3) -> Vector3:
        """Travel gaze stays ahead at eye height; footstep coordinates steer the body.

        A near waypoint can pass behind or below the eye between worker frames.
        It remains the movement target but must not make an idle gun snap down
        at the bot's own feet. Combat, excavation and flight own separate aim.
        """
        return (observer.eye[0] + direction[0] * 6.0,
                observer.eye[1] + direction[1] * 6.0, observer.eye[2])

    @staticmethod
    def _navigation_revisits(state: _BotState, position: Vector3,
                             goal: _Goal, now: float) -> bool:
        """Reject repeated local coverage, not valid detours away from a goal."""
        if now - state.navigation_coverage_last_at > 1.0:
            # Combat/idle interruptions do not age into a navigation failure.
            state.navigation_coverage_at = now
        state.navigation_coverage_last_at = now
        cell = tuple(int(math.floor(value / 2.0)) for value in position)
        new_cell = cell not in state.navigation_visited
        state.navigation_visited.pop(cell, None)
        state.navigation_visited[cell] = now
        if len(state.navigation_visited) > _NAVIGATION_VISITED_CELLS:
            del state.navigation_visited[next(iter(state.navigation_visited))]
        distance = math.dist(position, goal.position)
        target_changed = (state.navigation_coverage_goal != goal.key
            or state.navigation_coverage_target is None
            or math.dist(state.navigation_coverage_target, goal.position) >= 3.0)
        if target_changed:
            state.navigation_coverage_goal = goal.key
            state.navigation_coverage_target = goal.position
            state.navigation_coverage_best = distance
        improved = distance + _NAVIGATION_WINDOW_DISTANCE < state.navigation_coverage_best
        if improved:
            state.navigation_coverage_best = distance
        if new_cell or improved:
            state.navigation_coverage_at = now
            if state.escape_goal is None and improved:
                state.escape_attempts = 0
        return now - state.navigation_coverage_at >= _NAVIGATION_REVISIT_SECONDS

    def _flight_intent(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        now: float,
    ) -> BotIntent | None:
        """Own takeoff, a short crossing, then a thrust-free native landing."""
        step, source = state.flight_step, state.flight_source
        if step is None or source is None:
            return None
        elapsed = now - state.flight_started_at
        distance = math.hypot(step.waypoint[0] - observer.position[0],
                              step.waypoint[1] - observer.position[1])
        state.flight_departed |= not observer.grounded
        landed = (state.flight_departed and observer.grounded and elapsed > 0.35)
        if landed or observer.wade or elapsed >= 3.5:
            if distance > 1.0 or observer.wade:
                edge = (tuple(int(math.floor(v)) for v in (*source[:2], source[2] + 2.25)),
                        tuple(int(math.floor(v)) for v in (*step.waypoint[:2], step.waypoint[2] + 2.25)))
                state.blocked_edges[edge] = now + _BLOCKED_EDGE_SECONDS
                state.blocked_edge_since[edge] = now
            state.flight_step = None
            state.flight_source = None
            state.flight_watch = None
            state.next_flight_at = now + 4.0
            self._clear_route(state, now)
            return None
        altitude = source[2] - observer.position[2]
        direction = _normalized_xy(step.waypoint[0] - observer.position[0],
                                   step.waypoint[1] - observer.position[1])
        # Rise above the bank before entering the gap, then brake over its
        # known landing. Never hold thrust until fuel exhaustion.
        if (altitude < 0.8 and elapsed < 0.6) or distance < 0.4:
            direction = (0.0, 0.0, 0.0)
        ceiling = 5.0 if observer.jetpack_id == int(C.JETPACK_ENGINEER) else 2.5
        thrust = (elapsed < 0.5 or (altitude < ceiling and distance > 0.6 and elapsed < 2.3))
        return self._intent(frame,
            movement=MovementIntent(direction=direction, affordance=MovementAffordance.JETPACK,
                                    jetpack_thrust=thrust),
            look=LookIntent(state.flight_watch or step.waypoint, visible=False),
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.TRAVERSAL, debug_goal=step.waypoint,
            debug_role=("combat_" if state.flight_watch is not None else "")
            + ("jetpack_takeoff" if thrust else "jetpack_landing"))

    def _planning_wait_intent(self, frame: PerceptionFrame, observer: PlayerSnapshot,
                              state: _BotState, goal: _Goal, now: float) -> BotIntent:
        """Scheduling delay must not age into a physical/geometry failure."""
        if state.planning_wait_at is None:
            state.planning_wait_at = now
        return self._intent(frame, movement=self._coast(observer, state, goal, now), look=None,
            tool_id=_weapon_tool(observer), debug_goal=goal.position,
            debug_role=goal.role + ":planning_wait")

    def _coast(self, observer: PlayerSnapshot, state: _BotState, goal: _Goal,
               now: float) -> MovementIntent:
        """Keep walking the way we were going while the next route is computed.

        Nobody halts mid-field to think. The stretch ahead must be plain,
        body-wide, walkable ground; the motor's live probes still guard it.
        Anything else (water, ledges, walls, flight), or a body that has not
        physically got anywhere for a few seconds, keeps the old full stop.
        """

        heading = state.travel_heading or _normalized_xy(
            goal.position[0] - observer.position[0], goal.position[1] - observer.position[1])
        if (not observer.grounded or observer.wade or math.hypot(*heading[:2]) < 0.5
                or state.dead_end
                or now - state.navigation_window_at >= _STUCK_REPLAN_SECONDS
                or not callable(getattr(self.world, "surface", None))):
            return MovementIntent()
        ahead = (observer.position[0] + heading[0] * 4.0,
                 observer.position[1] + heading[1] * 4.0, observer.position[2])
        if not straight_walkable(self.world, observer.position, ahead):
            return MovementIntent()
        return MovementIntent(direction=heading, travel_source=observer.position,
                              travel_waypoint=ahead)

    def _escape_empty_route(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
        now: float,
    ) -> None:
        """Leave a local minimum instead of idling until failed edges expire."""

        state.escape_retry_at = now + 1.0
        goal = state.goal
        if goal is None:
            state.escape_search = None
            return
        escape_abilities = _movement_abilities(observer) | {MovementAffordance.DROP}
        dig_profile = _dig_profile(observer)
        context = (observer.position, goal.key, goal.position,
                   frame.topology_version, frozenset(escape_abilities),
                   dig_profile, observer.wade, frozenset(state.blocked_edges))
        previous = state.escape_search[0] if state.escape_search is not None else None
        resume = (previous is not None and previous[1] == goal.key
                  and math.dist(previous[0], observer.position) < 1.0
                  and math.dist(previous[2], goal.position) < 16.0
                  and previous[4:7] == context[4:7])
        if not resume:
            bearing = math.atan2(goal.position[1] - observer.position[1],
                                 goal.position[0] - observer.position[0])
            sign = 1.0 if observer.player_id % 2 else -1.0
            restrictions = [frozenset(state.blocked_edges)]
            recent = frozenset(
                edge for edge, expiry in state.blocked_edges.items()
                if now - state.blocked_edge_since.get(edge, expiry - _BLOCKED_EDGE_SECONDS) < 4.0
            )
            if recent != restrictions[0]:
                restrictions.append(recent)
            offsets = (math.pi / 2, -math.pi / 2, math.pi,
                       math.pi / 4, -math.pi / 4, 3 * math.pi / 4,
                       -3 * math.pi / 4, 0.0)
            attempt = state.escape_attempts
            rotation = (attempt * 3) % len(offsets)
            offsets = offsets[rotation:] + offsets[:rotation]
            radius = 8.0 + min(attempt, 4) * 2.0
            candidates = tuple(
                (observer.position[0] + radius * math.cos(bearing + offset * sign),
                 observer.position[1] + radius * math.sin(bearing + offset * sign),
                 observer.position[2]) for offset in offsets)
            water_options = (True,) if observer.wade else (False, True)
            # At most 51 exact candidates, resumed rather than restarting the
            # first dry direction every time the per-observer grant is spent.
            queries = [(candidate, water, None, blocked)
                       for water in water_options for blocked in restrictions
                       for candidate in candidates]
            queries.append((goal.position, True, None, recent))
            if dig_profile is not None:
                queries.extend((candidate, water, dig_profile, recent)
                               for water in water_options
                               for candidate in (goal.position, *candidates))
            state.escape_search = (context, tuple(queries), 0)
        context, queries, index = state.escape_search
        chosen = None
        chosen_restrictions = frozenset()
        while index < len(queries):
            candidate, allow_water, profile, blocked = queries[index]
            # This continuation contains only candidate coordinates and
            # failures, never cached successful geometry. Resume on live
            # terrain, honoring newly rejected edges; expired ones can retry.
            current_blocked = frozenset(state.blocked_edges)
            recent_blocked = frozenset(
                edge for edge, expiry in state.blocked_edges.items()
                if now - state.blocked_edge_since.get(edge, expiry - _BLOCKED_EDGE_SECONDS) < 4.0
            )
            blocked = ((blocked & current_blocked) | (current_blocked - context[7])
                       | recent_blocked)
            arguments = dict(abilities=escape_abilities, dig_profile=profile,
                             allow_water=allow_water, blocked_edges=blocked)
            if profile is not None:
                # Recovery must inspect the excavation instead of accepting
                # an ordinary dead-end walking prefix beside the same wall.
                arguments["_prefer_existing_path"] = False
            plan = self.world.plan(observer.position, candidate, **arguments)
            if plan.deferred:
                state.escape_search = (context, queries, index)
                state.escape_retry_at = now
                return
            index += 1
            useful = (any(step.affordance is MovementAffordance.BREACH for step in plan.steps)
                      and _plan_can_advance(plan, observer.position)) if profile is not None else (
                          plan.steps and math.dist(observer.position, plan.steps[-1].waypoint) >= 6.0)
            if useful:
                chosen, chosen_restrictions = plan, blocked
                break
        state.escape_search = None
        state.escape_attempts += 1
        if goal.role == "tdm_squad_support":
            state.support_escape_attempted = True
        if chosen is None:
            return
        revalidated = _route_edges(observer.position, chosen.steps)
        for edge in tuple(state.blocked_edges):
            if edge not in chosen_restrictions and edge in revalidated:
                del state.blocked_edges[edge]
                state.blocked_edge_since.pop(edge, None)
        state.escape_goal = chosen.steps[-1].waypoint
        state.escape_allow_water = observer.wade or any(
            step.affordance is MovementAffordance.SWIM for step in chosen.steps
        )
        state.escape_until = now + 8.0
        state.route = chosen.steps
        state.route_index = 0
        state.route_topology_version = int(frame.topology_version)
        state.waypoint_best_distance = math.inf
        state.waypoint_progress_at = now
        state.navigation_progress_position = observer.position
        state.navigation_progress_at = now
        state.navigation_window_position = observer.position
        state.navigation_window_at = now

    @staticmethod
    def _corridor_is_absurd(corridor: tuple[Vector3, ...], position: Vector3,
                            goal: Vector3) -> bool:
        """Reject guidance far longer than any player would walk."""

        direct = math.hypot(goal[0] - position[0], goal[1] - position[1])
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                     for a, b in zip(corridor, corridor[1:]))
        # Both a ratio and an absolute floor: a 150-block walk around a long
        # wall to a goal 14 blocks away is a real detour, not an absurd one.
        return length > max(2.2 * direct + 60.0, 280.0)

    def _corridor_segment_goal(
        self,
        state: _BotState,
        observer: PlayerSnapshot,
        goal: _Goal,
        now: float,
    ) -> Vector3 | None:
        """Remember a full detour while the local planner executes its corners."""

        if observer.wade or state.water_committed:
            return None
        if (not state.corridor and state.corridor_search is None
                and (state.dead_end and now >= state.dead_end_retry_at
                     or now >= state.corridor_retry_at
                     and now - state.goal_progress_at >= _GOAL_STALL_SECONDS)
                and not (now < state.corridor_rejected_until
                         and state.corridor_rejected_goal is not None
                         and math.dist(goal.position, state.corridor_rejected_goal) <= 24.0)):
            reader = getattr(self.world, "begin_corridor", None)
            if callable(reader):
                state.corridor_search = reader(
                    observer.position, goal.position,
                    blocked_edges=frozenset(state.blocked_edges),
                )
                state.corridor_failed_goal = (goal.position
                                             if state.corridor_search is None else None)
                if state.corridor_search is None and goal.role == "tdm_squad_support":
                    state.support_failed_goal = goal.position
                    state.support_failure_at = now
            state.corridor_retry_at = now + 15.0
            state.dead_end_retry_at = now + 5.0
        search = state.corridor_search
        if search is not None:
            if (not search.done and state.corridor_yield_local
                    and (state.route_index >= len(state.route)
                         or state.route_topology_version != getattr(
                             self.world, "topology_version", state.route_topology_version))):
                # Coarse guidance shares this observer's one admitted job
                # with local routing. After one coarse slice, an actor with
                # no executable route owns the next grant until its detailed
                # query completes. Otherwise a long search consumes every
                # grant first and leaves it motionless for seconds.
                return None
            search.advance()
            if getattr(search, "deferred", False):
                return None
            state.corridor_yield_local = True
            if search.done:
                if search.path:
                    # The bot may have moved during incremental search. Join
                    # a nearby corner only after proving a short dry connection;
                    # proximity alone can pick the far side of a U-shaped wall.
                    entry = None
                    candidates = sorted(range(len(search.path)), key=lambda index:
                                        math.dist(observer.position, search.path[index]))
                    for offset, index in enumerate(candidates[:8]):
                        if offset < state.corridor_join_index:
                            continue
                        point = search.path[index]
                        distance = math.dist(observer.position, point)
                        if distance > 12.0:
                            break
                        if distance <= 0.75:
                            entry = index
                            break
                        join = self.world.plan(observer.position, point,
                            abilities=_movement_abilities(observer) - {MovementAffordance.BREACH},
                            dig_profile=None, allow_water=False,
                            blocked_edges=frozenset(state.blocked_edges))
                        if join.deferred:
                            state.corridor_join_index = offset
                            return None
                        state.corridor_join_index = offset + 1
                        if join.reached_segment_goal and (not join.steps or
                                math.dist(join.steps[-1].waypoint, point) <= 1.25):
                            entry = index
                            break
                    if entry is not None and self._corridor_is_absurd(
                            search.path[entry:], observer.position, goal.position):
                        # The coarse atlas models walks and two-block drops
                        # only. Where the real route uses a bigger drop, dig
                        # or swim, its "only" corridor can circle the whole
                        # map; AncientEgypt sent a bot 700 blocks along the
                        # border for a 230-block trip. Keep local planning.
                        entry = None
                        state.corridor_rejected_goal = goal.position
                        state.corridor_rejected_until = now + 45.0
                    if entry is not None:
                        state.corridor = search.path
                        state.corridor_index = entry
                        state.dry_detour_goal = None
                        self._clear_route(state, now)
                state.corridor_search = None
                state.corridor_join_index = 0
                state.corridor_yield_local = False
        return self._corridor_point_ahead(state, observer.position)

    @staticmethod
    def _corridor_point_ahead(state: _BotState, position: Vector3) -> Vector3 | None:
        """The corridor point a stretch ahead of the body, not its very next corner.

        A corridor keeps every corner and every change of height, so on
        terraces its points are one block apart. Planning to the next one made
        routes of one to three steps: the bot stopped at each, waited for the
        next plan and swung its head to a new bearing, for the whole detour.
        Detailed planning is trusted with the eight cells the corridor allows
        a straight section, so that is how far ahead it is sent.
        """

        corridor, index = state.corridor, state.corridor_index
        reach = corridor[index:index + _CORRIDOR_WINDOW]
        if reach:
            # Whatever part of the corridor the body has come alongside is done.
            nearest = min(range(len(reach)), key=lambda item: math.dist(position, reach[item]))
            if math.dist(position, reach[nearest]) <= 1.25:
                index += nearest + 1
        state.corridor_index = index
        if index >= len(corridor):
            state.corridor = ()
            return None
        chosen, travelled = corridor[index], 0.0
        for before, point in zip(corridor[index:], corridor[index + 1:index + _CORRIDOR_WINDOW]):
            travelled += math.dist(before, point)
            if travelled > state.corridor_reach:
                break
            chosen = point
        return chosen

    def _crowd_detour_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        goal: _Goal,
        goal_distance: float,
        now: float,
    ) -> BotIntent | None:
        """Make stagnant followers exclude a shared choke and replan.

        Short-range repulsion separates moving bodies, but it cannot solve a
        group whose planners all selected the same valid narrow edge. Track
        physical progress only while at least four friendly bots remain in a
        far-goal cohort. After a bounded wait, every bot except the stable
        lowest-id leader blacklists its own directed edge. This keeps one
        digger/traverser working while followers ask the VXL planner for
        genuinely different geometry instead of forming a permanent queue.
        """

        if state.crowd_detour_goal is not None:
            detour_distance = math.hypot(
                float(state.crowd_detour_goal[0])
                - float(observer.position[0]),
                float(state.crowd_detour_goal[1])
                - float(observer.position[1]),
            )
            if (
                float(now) < float(state.crowd_detour_until)
                and detour_distance > 1.25
            ):
                return None
            state.crowd_detour_goal = None
            state.crowd_detour_until = float(now)

        nearby = tuple(
            player
            for player in frame.players
            if (
                player.is_bot
                and player.alive
                and player.spawned
                and int(player.team) == int(observer.team)
                and abs(
                    float(player.position[2])
                    - float(observer.position[2])
                )
                <= _CROWD_VERTICAL_TOLERANCE
                and math.hypot(
                    float(player.position[0])
                    - float(observer.position[0]),
                    float(player.position[1])
                    - float(observer.position[1]),
                )
                <= _CROWD_DETOUR_RADIUS
            )
        )
        if (
            float(goal_distance) < _CROWD_DETOUR_MIN_GOAL_DISTANCE
            or len(nearby) < _CROWD_DETOUR_BOTS
        ):
            state.crowd_anchor = None
            state.crowd_progress_at = float(now)
            return None

        position = tuple(float(value) for value in observer.position)
        if state.crowd_anchor is None:
            state.crowd_anchor = position
            state.crowd_progress_at = float(now)
            return None
        if math.hypot(
            position[0] - float(state.crowd_anchor[0]),
            position[1] - float(state.crowd_anchor[1]),
        ) >= _CROWD_DETOUR_PROGRESS:
            state.crowd_anchor = position
            state.crowd_progress_at = float(now)
            return None
        if float(now) - float(state.crowd_progress_at) < _CROWD_DETOUR_SECONDS:
            return None

        state.crowd_anchor = position
        state.crowd_progress_at = float(now)
        leader_id = min(int(player.player_id) for player in nearby)
        if int(observer.player_id) == leader_id:
            return None

        team_ids = sorted(
            int(player.player_id)
            for player in frame.players
            if (
                player.is_bot
                and player.alive
                and player.spawned
                and int(player.team) == int(observer.team)
            )
        )
        rank = team_ids.index(int(observer.player_id))
        centered_rank = float(rank) - (float(len(team_ids)) - 1.0) * 0.5
        lateral_sign = -1.0 if centered_rank < 0.0 else 1.0
        lateral_distance = 8.0 + abs(centered_rank) * 2.0
        goal_x = float(goal.position[0]) - float(observer.position[0])
        goal_y = float(goal.position[1]) - float(observer.position[1])
        goal_length = math.hypot(goal_x, goal_y)
        if goal_length <= 1e-6:
            unit_x, unit_y = 1.0, 0.0
        else:
            unit_x, unit_y = goal_x / goal_length, goal_y / goal_length
        state.crowd_detour_goal = (
            min(
                510.0,
                max(
                    1.0,
                    position[0]
                    - unit_y * lateral_sign * lateral_distance
                    - unit_x * 12.0,
                ),
            ),
            min(
                510.0,
                max(
                    1.0,
                    position[1]
                    + unit_x * lateral_sign * lateral_distance
                    - unit_y * 12.0,
                ),
            ),
            position[2],
        )
        state.crowd_detour_until = float(now) + _CROWD_DETOUR_ROUTE_SECONDS

        self._invalidate_current_edge(
            state,
            observer.position,
            now,
            lifetime=_CROWD_BLOCKED_EDGE_SECONDS,
        )
        self._clear_route(state, now)
        return self._intent(
            frame,
            movement=MovementIntent(),
            look=None,
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.TRAVERSAL,
            debug_goal=goal.position,
            debug_path=(observer.position, state.crowd_detour_goal),
            debug_role=f"{goal.role}:crowd_detour",
            crowd_adjust=False,
        )

    @staticmethod
    def _active_breach_digger(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        now: float,
    ) -> PlayerSnapshot | None:
        """Return a nearby teammate whose accepted melee is terrain-owned."""

        candidates = (
            player
            for player in frame.players
            if (
                player.is_bot
                and player.alive
                and player.spawned
                and int(player.team) == int(observer.team)
                and int(player.player_id) != int(observer.player_id)
                and str(player.last_action_kind)
                == BotActionKind.MELEE.value
                and bool(player.last_action_accepted)
                and player.last_action_position is not None
                and 0.0
                <= float(now) - float(player.last_action_at)
                <= 0.8
                and math.hypot(
                    float(player.last_action_position[0])
                    - float(observer.position[0]),
                    float(player.last_action_position[1])
                    - float(observer.position[1]),
                )
                <= 5.0
            )
        )
        return min(candidates, key=lambda player: int(player.player_id), default=None)

    def _breach_assist_queue_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        goal: _Goal,
        digger: PlayerSnapshot,
    ) -> BotIntent:
        """Back followers into stable slots while one teammate excavates."""

        target = digger.last_action_position
        if target is None:
            raise ValueError("active breach digger has no terrain target")
        follower_ids = sorted(
            int(player.player_id)
            for player in frame.players
            if (
                player.is_bot
                and player.alive
                and player.spawned
                and int(player.team) == int(observer.team)
                and int(player.player_id) != int(digger.player_id)
                and math.hypot(
                    float(player.position[0]) - float(target[0]),
                    float(player.position[1]) - float(target[1]),
                )
                <= 6.0
            )
        )
        try:
            rank = follower_ids.index(int(observer.player_id))
        except ValueError:
            rank = 0
        dx = float(observer.position[0]) - float(target[0])
        dy = float(observer.position[1]) - float(target[1])
        distance = math.hypot(dx, dy)
        if distance <= 1e-6:
            dx, dy = _deterministic_pair_axis(
                int(observer.player_id),
                int(digger.player_id),
            )
            distance = 1.0
        desired_distance = 2.0 + min(3, rank) * _BREACH_QUEUE_SPACING
        direction = (
            (dx / distance, dy / distance, 0.0)
            if distance + 0.2 < desired_distance
            else (0.0, 0.0, 0.0)
        )
        queue_goal = (
            float(target[0]) + dx / distance * desired_distance,
            float(target[1]) + dy / distance * desired_distance,
            float(observer.position[2]),
        )
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=direction,
                affordance=MovementAffordance.WALK,
            ),
            look=LookIntent(target, visible=False),
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.TRAVERSAL,
            debug_goal=goal.position,
            debug_path=(observer.position, queue_goal, target),
            debug_role=f"{goal.role}:breach_assist_queue",
            crowd_adjust=False,
        )

    def _breach_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        goal: _Goal,
        step: RouteStep,
        now: float,
    ) -> BotIntent:
        """Execute one planner-selected wall cell and await its terrain delta."""

        breach = step.breach
        if (not observer.can_shoot or breach is None
                or not self.world.solid(*breach.target_cell)):
            self._clear_route(state, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=goal.position,
                debug_role=f"{goal.role}:breach_replan",
            )

        nearby = self._breach_queue(frame, observer, breach.target)
        owner = nearby[0] if nearby else observer
        if int(owner.player_id) != int(observer.player_id):
            # Only one body may own a narrow excavation face.  Every worker
            # sees the same immutable roster and derives the same distance
            # bucket/id order, so this is a reservation without shared mutable
            # state or gameplay-thread coordination.  Followers back into
            # ordered slots instead of occupying the digger's body, starting
            # their own timeout, and eventually blacklisting the same edge.
            self._reset_breach(state, now)
            edge = (breach.source, breach.destination)
            if state.yielded_breach_edge != edge:
                state.yielded_breach_edge = edge
                state.yielded_breach_started_at = float(now)
            elif (
                float(now) - float(state.yielded_breach_started_at)
                >= _BREACH_YIELD_REPLAN_SECONDS
            ):
                # Waiting forever preserves the exact shared-hole deadlock:
                # every follower asks A* for the same cheapest breach again.
                # Exclude that concrete edge and let the authoritative VXL
                # search choose another wall cell or a dry route while the
                # elected owner continues excavating this one.
                self._remember_blocked_edge(state, edge, now)
                self._clear_route(state, now)
                state.yielded_breach_edge = None
                return self._intent(
                    frame,
                    movement=MovementIntent(),
                    look=None,
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.TRAVERSAL,
                    debug_goal=goal.position,
                    debug_role=f"{goal.role}:breach_detour",
                    crowd_adjust=False,
                )
            rank = next(
                index
                for index, player in enumerate(nearby)
                if int(player.player_id) == int(observer.player_id)
            )
            dx = float(observer.position[0]) - float(breach.target[0])
            dy = float(observer.position[1]) - float(breach.target[1])
            distance = math.hypot(dx, dy)
            if distance <= 1e-6:
                dx, dy = _deterministic_pair_axis(
                    int(observer.player_id), int(owner.player_id)
                )
                distance = 1.0
            direction = (dx / distance, dy / distance, 0.0)
            desired_distance = (
                1.0 + min(3, int(rank)) * _BREACH_QUEUE_SPACING
            )
            if distance + 0.2 >= desired_distance:
                direction = (0.0, 0.0, 0.0)
            queue_goal = (
                float(breach.target[0])
                + float(direction[0]) * desired_distance,
                float(breach.target[1])
                + float(direction[1]) * desired_distance,
                float(observer.position[2]),
            )
            return self._intent(
                frame,
                movement=MovementIntent(
                    direction=direction,
                    affordance=MovementAffordance.WALK,
                ),
                look=LookIntent(breach.target, visible=False),
                tool_id=int(breach.tool_id),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=goal.position,
                debug_path=(observer.position, queue_goal, breach.target),
                debug_role=f"{goal.role}:breach_yield",
                crowd_adjust=False,
            )

        state.yielded_breach_edge = None
        state.yielded_breach_started_at = float(now)

        breach_key = (
            breach.target_cell,
            int(breach.tool_id),
            bool(breach.secondary),
        )
        if state.breach_key != breach_key:
            same_tool = (state.breach_key is not None
                         and state.breach_key[1:] == breach_key[1:])
            state.breach_key = breach_key
            state.breach_started_at = float(now)
            # Clearing one face does not reset the owned tool's cadence.
            # Otherwise the next cell immediately produces a cooldown
            # rejection, incorrectly excluding a partially excavated bank.
            state.next_breach_at = (max(state.next_breach_at, float(now))
                                    if same_tool else float(now))

        timeout = max(
            3.0,
            float(breach.estimated_swings)
            * float(breach.fire_interval)
            * 3.0
            + 1.5,
        )
        if now - state.breach_started_at > timeout:
            self._remember_blocked_edge(
                state,
                (breach.source, breach.destination),
                now,
            )
            self._clear_route(state, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=goal.position,
                debug_role=f"{goal.role}:breach_failed",
            )

        target = breach.target
        action = BotAction()
        if now + 1e-9 >= state.next_breach_at:
            action = BotAction(
                BotActionKind.MELEE,
                tool_id=int(breach.tool_id),
                position=target,
            )
            state.next_breach_at = (
                float(now) + max(0.05, float(breach.fire_interval))
            )
        return self._intent(
            frame,
            movement=MovementIntent(
                jump=False,
                crouch=target[2] >= observer.position[2],
                affordance=MovementAffordance.BREACH,
            ),
            look=LookIntent(target, visible=False),
            tool_id=int(breach.tool_id),
            action=action,
            priority=BotIntentPriority.TRAVERSAL,
            secondary_fire=bool(breach.secondary),
            debug_goal=goal.position,
            debug_path=(observer.position, target, step.waypoint),
            debug_role=f"{goal.role}:route_breach",
        )

    @staticmethod
    def _breach_queue(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        target: Vector3,
    ) -> tuple[PlayerSnapshot, ...]:
        """Return active friendly diggers sharing one excavation face."""

        candidates = [
            player
            for player in frame.players
            if (
                player.is_bot
                and player.alive
                and player.spawned
                and int(player.team) == int(observer.team)
                and (
                    int(player.player_id) == int(observer.player_id)
                    or (
                        str(player.last_action_kind)
                        == BotActionKind.MELEE.value
                        and player.last_action_position is not None
                        and 0.0
                        <= float(frame.created_at)
                        - float(player.last_action_at)
                        <= 0.8
                        and math.hypot(
                            float(player.last_action_position[0])
                            - float(target[0]),
                            float(player.last_action_position[1])
                            - float(target[1]),
                        )
                        <= 3.25
                        and abs(
                            float(player.last_action_position[2])
                            - float(target[2])
                        )
                        <= 2.0
                    )
                )
                and abs(
                    float(player.position[2]) - float(observer.position[2])
                ) <= _CROWD_VERTICAL_TOLERANCE
                and math.hypot(
                    float(player.position[0]) - float(observer.position[0]),
                    float(player.position[1]) - float(observer.position[1]),
                ) <= _BREACH_RESERVATION_RADIUS
            )
        ]
        if not any(
            int(player.player_id) == int(observer.player_id)
            for player in candidates
        ):
            candidates.append(observer)

        # Passive nearby bodies must not reserve an excavation they are not
        # executing. That made GreatWall's actual digger yield forever to a
        # lower-id teammate whose route was a different jump. Once authority
        # observes simultaneous hits on this face, the shared roster elects
        # the same lowest-id owner and all other workers form the queue.
        return tuple(sorted(candidates, key=lambda player: int(player.player_id)))

    def _water_bridge_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        goal: _Goal,
        now: float,
    ) -> BotIntent | None:
        """Extend a dry route with one short authoritative BlockLine."""

        block_tool = int(C.BLOCK_TOOL)
        if (
            block_tool not in observer.loadout
            or int(observer.blocks) < 1
            or not observer.grounded
        ):
            return None
        direction = _normalized_xy(
            float(goal.position[0]) - float(observer.position[0]),
            float(goal.position[1]) - float(observer.position[1]),
        )
        line_reader = getattr(self.world, "water_bridge_line", None)
        line = (
            line_reader(
                observer.position,
                direction,
                max_cells=min(6, int(observer.blocks)),
                require_landing_within=int(observer.blocks) + 1,
            )
            if callable(line_reader)
            else None
        )
        line_role = "bridge_builder"
        shoulder_reader = getattr(
            self.world,
            "narrow_bridge_shoulder_line",
            None,
        )
        if line is None and callable(shoulder_reader):
            line = shoulder_reader(
                observer.position,
                direction,
                max_cells=min(6, int(observer.blocks)),
            )
            if line is not None:
                line_role = "bridge_widener"
        if line is None:
            return None
        start, end = line
        if (observer.last_action_kind == BotActionKind.BUILD_LINE.value
                and not observer.last_action_accepted
                and observer.last_action_position is not None
                and tuple(int(round(value)) for value in observer.last_action_position) == start
                and now - float(observer.last_action_at) < 10.0):
            # A reservation/protected cell rejection is not terrain progress.
            # Yield to route/swim fallback instead of issuing it every 0.8 s.
            return None
        cost = (
            max(
                abs(int(end[index]) - int(start[index]))
                for index in range(3)
            )
            + 1
        )
        if int(observer.blocks) < cost:
            return None
        if float(now) < float(state.next_water_build_at):
            return self._intent(
                frame,
                movement=MovementIntent(
                    crouch=True,
                    affordance=MovementAffordance.BUILD_BRIDGE,
                ),
                look=LookIntent(tuple(float(value) for value in end), visible=False),
                tool_id=block_tool,
                priority=BotIntentPriority.TRAVERSAL,
                debug_goal=goal.position,
                debug_role=f"{goal.role}:{line_role}_wait",
            )
        state.next_water_build_at = float(now) + _BRIDGE_BUILD_INTERVAL
        return self._intent(
            frame,
            movement=MovementIntent(
                crouch=True,
                affordance=MovementAffordance.BUILD_BRIDGE,
            ),
            look=LookIntent(tuple(float(value) for value in end), visible=False),
            tool_id=block_tool,
            action=BotAction(
                BotActionKind.BUILD_LINE,
                tool_id=block_tool,
                position=tuple(float(value) for value in start),
                end_position=tuple(float(value) for value in end),
            ),
            priority=BotIntentPriority.TRAVERSAL,
            debug_goal=goal.position,
            debug_path=(
                observer.position,
                tuple(float(value) for value in start),
                tuple(float(value) for value in end),
            ),
            debug_role=f"{goal.role}:{line_role}",
        )

    def _water_intent(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        step: RouteStep | None,
        now: float,
        *,
        force_block_edge: bool = False,
    ) -> BotIntent:
        """Give wading survival sole ownership of locomotion."""

        state = self._states[
            (int(observer.player_id), int(observer.generation))
        ]
        if (state.water_search_origin is not None
                and math.dist(observer.position[:2], state.water_search_origin[:2]) >= 6.0):
            state.water_search_until = 0.0
            state.water_search_origin = None
        failed_search_heading = (
            state.water_search_heading
            if force_block_edge and now < state.water_search_until else None
        )
        if now < state.water_search_until and not force_block_edge:
            search = self._water_search_intent(frame, observer, state)
            if search is not None:
                return search
        if force_block_edge:
            # Commit to leaving this pocket before considering its attractive
            # but failed bank again. Per-edge exclusions alone can alternate
            # neighbouring exits without ever escaping the same small basin.
            # Finish a physical retreat before the nearest-bank flow can
            # reclaim movement. A three-second timer often expired halfway
            # out, causing the swimmer to turn back into the same pocket.
            state.water_search_until = now + 8.0
            state.water_search_origin = observer.position
            state.water_search_heading = (
                _normalized_xy(observer.position[0] - step.waypoint[0],
                               observer.position[1] - step.waypoint[1])
                if step is not None else None
            )
            if (step is not None and step.affordance in {
                    MovementAffordance.WALK, MovementAffordance.SWIM}):
                # In open water, reversing the current swim bearing creates
                # a six-block out-and-back loop when shore flow resumes.
                # Search across that failed bearing to reach a different
                # approach. A concrete bank still calls for backing away.
                dx, dy, _ = state.water_search_heading
                side = 1.0 if (observer.player_id + observer.generation) % 2 else -1.0
                state.water_search_heading = (-dy * side, dx * side, 0.0)
            if failed_search_heading is not None:
                # The whole-swim watchdog also owns an active retreat. A
                # failed search must change direction, rather than consume
                # another four-second window behind its eight-second lease.
                state.water_search_heading = (
                    -failed_search_heading[1], failed_search_heading[0], 0.0,
                )
            reader = getattr(self.world, "water_shore_edges", None)
            if callable(reader):
                # A recovery may already have swum several cells away from
                # the bank. Remember the attempted exit, not merely the open
                # water occupied when the progress timer expires.
                failed_bank = (state.water_last_bank[1]
                               if state.water_last_bank is not None
                               and now - state.water_last_bank[0] < 30.0
                               else observer.position)
                state.water_failed_shores.append((now + 30.0, reader(failed_bank)))
                state.water_failed_shores = state.water_failed_shores[-4:]
        if step is None:
            state.water_landing_step = None
            state.water_step_key = None
            state.water_best_distance = math.inf
            state.water_progress_at = now
            preferred_goal = (
                state.goal.position
                if (
                    state.goal is not None
                    and not state.water_goal_reached
                    and not state.water_recovery
                )
                else None
            )
            bank = self.world.assisted_water_step(
                observer.position,
                preferred_goal=preferred_goal,
                # Resolve the failing bank before applying the new region
                # exclusion so its direction can seed the retreat.
                blocked_edges=(frozenset(state.blocked_edges) if force_block_edge
                               else self._water_exclusions(state, now)),
            )
            if bank is not None:
                state.water_last_bank = (now, bank.waypoint)
            if force_block_edge and bank is not None:
                if failed_search_heading is None:
                    state.water_search_heading = _normalized_xy(
                        observer.position[0] - bank.waypoint[0],
                        observer.position[1] - bank.waypoint[1],
                    )
                # The map-wide four-block swim timer owns this recovery even
                # when the normal water flow has no direct step and falls back
                # to an assisted build/breach bank. Ignoring the flag on this
                # branch let a valid but slow shoreline excavation hold a
                # swimmer beyond the ten-second hard limit.
                self._block_water_step(
                    state,
                    observer.position,
                    bank,
                    now,
                )
                return self._intent(
                    frame,
                    movement=MovementIntent(),
                    look=None,
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.SURVIVAL,
                    debug_goal=bank.waypoint,
                    debug_role="water_exit:cycle_blocked",
                )
            block_tool = int(C.BLOCK_TOOL)
            build_cell = (
                self.world.jump_build_cell(observer.position)
                if (
                    bank is not None
                    and block_tool in observer.loadout
                    and int(observer.blocks) > 0
                )
                else None
            )
            if build_cell is not None and now >= state.next_water_build_at:
                state.next_water_build_at = now + 0.8
                target = tuple(float(value) for value in build_cell)
                direction = _normalized_xy(
                    float(bank.waypoint[0]) - float(observer.position[0]),
                    float(bank.waypoint[1]) - float(observer.position[1]),
                )
                return self._intent(
                    frame,
                    movement=MovementIntent(
                        direction=direction,
                        jump=True,
                        affordance=MovementAffordance.BUILD_STEP,
                    ),
                    look=LookIntent(target, visible=False),
                    tool_id=block_tool,
                    action=BotAction(
                        BotActionKind.BUILD,
                        tool_id=block_tool,
                        position=target,
                    ),
                    priority=BotIntentPriority.SURVIVAL,
                    debug_goal=bank.waypoint,
                    debug_role="water_bank_build_step",
                )
            profile = _dig_profile(observer)
            breach = (
                self.world.water_bank_breach(
                    observer.position,
                    profile,
                    preferred_goal=preferred_goal,
                    blocked_edges=self._water_exclusions(state, now),
                )
                if bank is not None and profile is not None
                else None
            )
            if breach is not None:
                goal = state.goal or _Goal(
                    ("water_bank",),
                    bank.waypoint,
                    "water_bank",
                    1.0,
                    False,
                )
                plan = breach.breach
                rejected_target = observer.last_action_position
                if (
                    plan is not None
                    and str(observer.last_action_kind)
                    == BotActionKind.MELEE.value
                    and not bool(observer.last_action_accepted)
                    and rejected_target is not None
                    and 0.0
                    <= float(now) - float(observer.last_action_at)
                    <= 0.8
                    and math.dist(plan.target, rejected_target) <= 0.25
                ):
                    # The gameplay gateway has already proved that this exact
                    # bank face cannot accept the planned swing. Retrying it
                    # until the normal (potentially long) excavation timeout
                    # pins a swimmer against one shoreline forever. Exclude
                    # only that directed edge and let the water flow choose a
                    # different bank; ordinary dry breaches keep their normal
                    # transient-cooldown tolerance.
                    self._remember_blocked_edge(
                        state,
                        (plan.source, plan.destination),
                        now,
                        lifetime=_WATER_BLOCKED_EDGE_SECONDS,
                    )
                    self._clear_route(state, now)
                    state.water_step_key = None
                    state.water_best_distance = math.inf
                    state.water_progress_at = now
                    state.water_recovery = True
                    return self._intent(
                        frame,
                        movement=MovementIntent(),
                        look=None,
                        tool_id=_weapon_tool(observer),
                        priority=BotIntentPriority.SURVIVAL,
                        debug_goal=bank.waypoint,
                        debug_role=f"{goal.role}:water_breach_rejected",
                    )
                state.water_breach_target = plan.target_cell if plan is not None else None
                return self._breach_intent(
                    frame,
                    observer,
                    state,
                    goal,
                    breach,
                    now,
                )
            if bank is not None:
                # This concrete bank has neither a supported build cell nor a
                # safe solid melee target (for example, a one-block lip where
                # an ordinary spade would also remove the waterbed). Exclude
                # it before asking the shore flow again; returning a zero
                # vector here forever pinned London swimmers to the same
                # already-cleared air target.
                self._block_water_step(
                    state,
                    observer.position,
                    bank,
                    now,
                )
                return self._intent(
                    frame,
                    movement=MovementIntent(),
                    look=None,
                    tool_id=_weapon_tool(observer),
                    priority=BotIntentPriority.SURVIVAL,
                    debug_goal=bank.waypoint,
                    debug_role="water_bank:unusable_edge",
                )
            search = self._water_search_intent(frame, observer, state)
            if search is not None:
                state.water_search_until = now + 8.0
                state.water_search_origin = observer.position
                return search
            return self._intent(
                frame,
                movement=MovementIntent(
                    direction=(
                        _normalized_xy(
                            float(preferred_goal[0])
                            - float(observer.position[0]),
                            float(preferred_goal[1])
                            - float(observer.position[1]),
                        )
                        if preferred_goal is not None
                        else (0.0, 0.0, 0.0)
                    ),
                    jump=False,
                    sprint=True,
                    affordance=MovementAffordance.SWIM,
                ),
                look=(
                    LookIntent(preferred_goal, visible=False)
                    if preferred_goal is not None
                    else None
                ),
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.SURVIVAL,
                debug_goal=preferred_goal,
                debug_role="water_no_route",
            )
        step_key = (
            int(math.floor(step.waypoint[0])),
            int(math.floor(step.waypoint[1])),
            int(round(step.waypoint[2] + 2.25)),
            step.affordance.value,
        )
        distance = math.hypot(
            float(step.waypoint[0]) - float(observer.position[0]),
            float(step.waypoint[1]) - float(observer.position[1]),
        )
        if force_block_edge:
            self._block_water_step(state, observer.position, step, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.SURVIVAL,
                debug_goal=step.waypoint,
                debug_role="water_exit:cycle_blocked",
            )
        if state.water_step_key != step_key:
            state.water_step_key = step_key
            state.water_best_distance = distance
            state.water_progress_at = now
        elif distance + 0.35 < state.water_best_distance:
            state.water_best_distance = distance
            state.water_progress_at = now
        elif now - state.water_progress_at >= _WAYPOINT_STALL_SECONDS:
            self._block_water_step(state, observer.position, step, now)
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.SURVIVAL,
                debug_goal=step.waypoint,
                debug_role="water_exit:edge_blocked",
            )

        direction = _normalized_xy(
            float(step.waypoint[0]) - float(observer.position[0]),
            float(step.waypoint[1]) - float(observer.position[1]),
        )
        state.water_landing_step = step if step.affordance is MovementAffordance.JUMP else None
        if state.water_landing_step is not None:
            state.water_last_bank = (now, step.waypoint)
        motor_affordance = (
            MovementAffordance.JUMP
            if step.affordance is MovementAffordance.JUMP
            else MovementAffordance.SWIM
        )
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=direction,
                jump=step.affordance is MovementAffordance.JUMP,
                sprint=True,
                affordance=motor_affordance,
            ),
            look=LookIntent(step.waypoint, visible=False),
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.SURVIVAL,
            debug_goal=step.waypoint,
            debug_path=(observer.position, step.waypoint),
            debug_role="water_exit",
        )

    @staticmethod
    def _water_exclusions(state: _BotState, now: float) -> frozenset[EdgeKey]:
        """Keep failed bank regions separate from the small local-edge cache."""
        state.water_failed_shores[:] = [
            item for item in state.water_failed_shores if item[0] > now
        ]
        return frozenset(state.blocked_edges).union(
            *(edges for _expires, edges in state.water_failed_shores)
        )

    def _water_search_intent(
        self, frame: PerceptionFrame, observer: PlayerSnapshot, state: _BotState,
    ) -> BotIntent | None:
        """Follow live open water while retaining a stable escape heading."""

        reader = getattr(self.world, "water_roam_step", None)
        step = (
            reader(observer.position,
                   heading=state.water_search_heading or observer.orientation,
                   blocked_edges=frozenset(state.blocked_edges))
            if callable(reader) else None
        )
        if step is None:
            return None
        state.water_search_heading = _normalized_xy(
            step.waypoint[0] - observer.position[0],
            step.waypoint[1] - observer.position[1],
        )
        return self._intent(
            frame,
            movement=MovementIntent(direction=state.water_search_heading,
                                    sprint=True, affordance=MovementAffordance.SWIM),
            look=LookIntent(step.waypoint, visible=False),
            tool_id=_weapon_tool(observer), priority=BotIntentPriority.SURVIVAL,
            debug_goal=step.waypoint, debug_role="water_search_shore",
        )

    def _block_water_step(
        self,
        state: _BotState,
        position: Vector3,
        step: RouteStep,
        now: float,
    ) -> None:
        """Blacklist one failed swim/shore edge and reset its local timer."""

        current = self.world.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=8,
            allow_water=True,
        )
        target = self.world.surface(
            int(math.floor(step.waypoint[0])),
            int(math.floor(step.waypoint[1])),
            float(step.waypoint[2]),
            vertical_span=8,
            allow_water=True,
        )
        edge = step.entry_edge
        if edge is None and current is not None and target is not None:
            edge = (
                (current.x, current.y, current.support_z),
                (target.x, target.y, target.support_z),
            )
        if edge is not None:
            self._remember_blocked_edge(
                state,
                edge,
                now,
                lifetime=_WATER_BLOCKED_EDGE_SECONDS,
            )
        state.water_step_key = None
        state.water_landing_step = None
        state.water_best_distance = math.inf
        state.water_progress_at = now

    def _landed_on_dry_surface(self, observer: PlayerSnapshot) -> bool:
        """Return whether a former swimmer has a stable dry foothold.

        The native wade bit can clear for individual airborne bob frames at a
        bank. Releasing water ownership on that bit alone made London bots
        alternate SWIM/JUMP against the same wall forever.
        """

        # This method is reached only after native and live-VXL water-contact
        # checks are both clear. At that point the authoritative grounded bit
        # is sufficient evidence of a dry landing even when the capsule is
        # supported by a neighboring column that the point surface probe
        # below cannot see (reproduced on London's irregular banks).
        if observer.grounded:
            return True

        surface = self.world.surface(
            int(math.floor(observer.position[0])),
            int(math.floor(observer.position[1])),
            float(observer.position[2]),
            vertical_span=3,
            allow_water=False,
        )
        return bool(
            surface is not None
            and int(surface.support_z) < int(C.Z_ABOVE_WATERPLANE) + 1
            # Native collision can keep ``grounded`` false while jump remains
            # held at the lip. A live dry support directly under the capsule
            # is sufficient to return locomotion to ordinary navigation; an
            # airborne bob over water still has no dry support in its column.
            and abs(
                float(surface.position[2]) - float(observer.position[2])
            ) <= _DRY_BANK_RELEASE_VERTICAL
        )

    def _water_contact(self, observer: PlayerSnapshot) -> bool:
        """Return whether native state or live VXL geometry owns water motion.

        Native physics updates ``wade`` at 60 Hz while bot decisions are
        sampled at a lower cadence. On ARM runners a body balanced at the
        London bank could report a dry bit at every decision phase, then
        become wading again after the same tick's physics step. That alias
        left a zero-motion breach intent in control forever. The body-clear
        water support directly under the capsule is a stable second signal;
        the tight vertical bound prevents distant water or roofs from
        claiming an ordinary dry player.
        """

        if observer.wade:
            return True
        surface = self.world.surface(
            int(math.floor(observer.position[0])),
            int(math.floor(observer.position[1])),
            float(observer.position[2]),
            vertical_span=3,
            allow_water=True,
        )
        return bool(
            surface is not None
            and int(surface.support_z)
            >= int(C.Z_ABOVE_WATERPLANE) + 1
            and abs(
                float(surface.position[2]) - float(observer.position[2])
            ) <= _DRY_BANK_RELEASE_VERTICAL
        )

    @staticmethod
    def _traversal_personality(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
    ) -> _TraversalPersonality:
        """Distribute stable route styles while retaining profile character.

        Team rank guarantees that a normal six-bot side has dry-route,
        swimmer, and bridge-builder identities instead of every worker
        independently converging on the same shortest path. Profile traits
        then vary how far ahead each identity plans without making the team
        composition random or seed-fragile.
        """

        team_ids = sorted(
            int(player.player_id)
            for player in frame.players
            if player.is_bot and int(player.team) == int(observer.team)
        )
        try:
            rank = team_ids.index(int(observer.player_id))
        except ValueError:
            rank = abs(int(observer.player_id))
        style = (
            _TraversalStyle.DRY,
            _TraversalStyle.SWIM,
            _TraversalStyle.BRIDGE,
        )[rank % 3]
        centered_rank = float(rank) - (float(len(team_ids)) - 1.0) * 0.5
        detour_sign = -1 if centered_rank < 0.0 else 1
        if abs(centered_rank) < 0.5:
            detour_sign = -1 if rank % 2 else 1
        profile = frame.profile or _fallback_profile(observer.player_id)
        forward_bias = max(
            -6.0,
            min(
                6.0,
                (float(profile.creativity) - 0.5) * 8.0
                + (float(profile.caution) - float(profile.aggression)) * 4.0,
            ),
        )
        return _TraversalPersonality(
            style=style,
            detour_sign=detour_sign,
            forward_bias=forward_bias,
        )

    @staticmethod
    def _dry_detour_segment_goal(
        observer: PlayerSnapshot,
        strategic_goal: Vector3,
        personality: _TraversalPersonality,
        attempt: int,
    ) -> Vector3:
        """Return a bounded shoreline-search target without entering water."""

        dx = float(strategic_goal[0]) - float(observer.position[0])
        dy = float(strategic_goal[1]) - float(observer.position[1])
        distance = math.hypot(dx, dy)
        if distance <= 1e-6:
            return strategic_goal
        unit_x, unit_y = dx / distance, dy / distance
        sign = int(personality.detour_sign)
        if int(attempt) % 2 == 0:
            sign *= -1
        lateral = _DRY_DETOUR_DISTANCE + min(2, max(0, int(attempt) - 1)) * 12.0
        forward = 8.0
        return (
            min(
                510.0,
                max(
                    1.0,
                    float(observer.position[0])
                    + unit_x * forward
                    - unit_y * lateral * sign,
                ),
            ),
            min(
                510.0,
                max(
                    1.0,
                    float(observer.position[1])
                    + unit_y * forward
                    + unit_x * lateral * sign,
                ),
            ),
            float(observer.position[2]),
        )

    @staticmethod
    def _team_lane_segment_goal(
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        strategic_goal: Vector3,
    ) -> Vector3:
        """Return a stable, bounded corridor target for a distant team goal.

        A final formation point alone does not diversify a 250-cell route:
        bounded A* repeatedly selects the same cheapest tunnel long before
        those small final offsets matter. Give every team bot a stable lateral
        lane for each long segment, fading back to the exact strategic target
        for the final approach.
        """

        dx = float(strategic_goal[0]) - float(observer.position[0])
        dy = float(strategic_goal[1]) - float(observer.position[1])
        distance = math.hypot(dx, dy)
        if distance < _TEAM_LANE_MIN_GOAL_DISTANCE:
            return strategic_goal

        team_ids = sorted(
            int(player.player_id)
            for player in frame.players
            if player.is_bot and int(player.team) == int(observer.team)
        )
        try:
            rank = team_ids.index(int(observer.player_id))
        except ValueError:
            return strategic_goal
        personality = SimpleBotBrain._traversal_personality(frame, observer)
        centered_rank = float(rank) - (float(len(team_ids)) - 1.0) * 0.5
        raw_lateral = centered_rank * _TEAM_LANE_SPACING
        lateral = max(
            -_TEAM_LANE_MAX_OFFSET,
            min(
                _TEAM_LANE_MAX_OFFSET,
                raw_lateral,
            ),
        )
        # More than six bots used to collapse onto the same clamped outer
        # coordinates. Stagger their forward segment lengths by the overflow;
        # the lateral corridor stays bounded but no four workers receive one
        # identical local A* target.
        overflow = max(0.0, abs(raw_lateral) - _TEAM_LANE_MAX_OFFSET)
        unit_x, unit_y = dx / distance, dy / distance
        forward = min(
            max(
                36.0,
                _TEAM_LANE_SEGMENT_DISTANCE
                - overflow * 0.5
                + float(personality.forward_bias),
            ),
            distance,
        )
        return (
            min(
                510.0,
                max(
                    1.0,
                    float(observer.position[0])
                    + unit_x * forward
                    - unit_y * lateral,
                ),
            ),
            min(
                510.0,
                max(
                    1.0,
                    float(observer.position[1])
                    + unit_y * forward
                    + unit_x * lateral,
                ),
            ),
            float(observer.position[2]),
        )

    @staticmethod
    def _set_goal(
        state: _BotState,
        goal: _Goal | None,
        position: Vector3,
        now: float,
    ) -> None:
        """Atomically replace the only movement owner."""

        old = state.goal
        same_key = old is not None and goal is not None and old.key == goal.key
        if (not same_key and old is not None and goal is not None
                and old.role in _HUNT_ROLES and goal.role in _HUNT_ROLES
                and math.dist(old.position, goal.position) < 8.0):
            # The worker's last-seen chase and the team layer's investigation
            # of the same evidence are one errand. Swapping their labels must
            # not throw away the route each time and stutter the approach.
            same_key = True
        moved = (
            old is not None
            and goal is not None
            and math.hypot(
                float(old.position[0]) - float(goal.position[0]),
                float(old.position[1]) - float(goal.position[1]),
            )
            >= 3.0
        )
        if same_key and not moved:
            state.goal = goal
            return
        if (same_key and goal is not None and old is not None
                and math.dist(old.position, goal.position) < 16.0
                and (state.corridor or state.corridor_search is not None
                     or state.route_index < len(state.route)
                     or state.escape_search is not None)):
            # Moving squad/escort targets must not reset a blocked edge's
            # deadline each time they cross a quantized objective cell.
            # Finish or reject the committed local edge before steering on.
            state.goal = goal
            return
        if old is None and goal is None:
            return
        state.goal = goal
        state.navigation_previous_position = None
        state.planning_context = None
        state.planning_results.clear()
        state.planning_wait_at = None
        state.escape_search = None
        state.route = ()
        state.route_index = 0
        state.route_topology_version = -1
        state.waypoint_best_distance = math.inf
        state.waypoint_progress_at = float(now)
        state.goal_best_distance = (
            math.hypot(
                float(goal.position[0]) - float(position[0]),
                float(goal.position[1]) - float(position[1]),
            )
            if goal is not None
            else math.inf
        )
        state.goal_progress_at = float(now)
        # Directed failed edges describe live geometry, not the short-lived
        # target that encountered it. Keep their bounded TTL across combat,
        # last-seen, and strategic goal switches so those roles cannot
        # resurrect the same rejected jump every frame.
        state.yielded_breach_edge = None
        state.yielded_breach_started_at = float(now)
        state.dry_detour_goal = None
        state.dry_detour_until = 0.0
        state.dry_route_failures = 0
        SimpleBotBrain._reset_breach(state, now)
        state.corridor_search = None
        state.corridor = ()
        state.corridor_index = 0
        state.corridor_join_index = 0
        state.corridor_yield_local = False
        state.corridor_failed_goal = None
        state.corridor_retry_at = float(now)

    def _invalidate_current_edge(
        self,
        state: _BotState,
        position: Vector3,
        now: float,
        *,
        lifetime: float | None = None,
    ) -> None:
        """Exclude the adjacent voxel edge currently failing to make progress."""

        if state.route_index >= len(state.route):
            return
        route_step = state.route[state.route_index]
        if (route_step.affordance is MovementAffordance.JUMP
                and route_step.entry_edge is not None):
            # A failed jump can already be over the gap or inside the landing
            # voxel. Reconstructing its source from that position records a
            # different edge (or none), so A* keeps selecting the failed jump.
            self._remember_blocked_edge(
                state, route_step.entry_edge, now,
                lifetime=(float(lifetime) if lifetime is not None else
                          _WATER_BLOCKED_EDGE_SECONDS
                          if route_step.entry_edge[0][2] >= int(C.Z_ABOVE_WATERPLANE) + 1
                          else _JUMP_BLOCKED_EDGE_SECONDS),
            )
            return
        if route_step.breach is not None:
            breach = route_step.breach
            self._remember_blocked_edge(
                state,
                (breach.source, breach.destination),
                now,
                lifetime=(
                    _BLOCKED_EDGE_SECONDS
                    if lifetime is None
                    else float(lifetime)
                ),
            )
            return
        current = self.world.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=8,
            allow_water=True,
        )
        if current is None:
            return
        waypoint = state.route[state.route_index].waypoint
        target = self.world.surface(
            int(math.floor(waypoint[0])),
            int(math.floor(waypoint[1])),
            float(waypoint[2]),
            vertical_span=8,
            allow_water=True,
        )
        if (
            target is not None
            and route_step.affordance is MovementAffordance.JUMP
        ):
            # A route edge can span a one-cell gap. Looking only at the
            # adjacent column returned ``None`` over the void, so failed gap
            # jumps were never blacklisted and Invasion bots bobbed forever.
            self._remember_blocked_edge(
                state,
                (
                    (current.x, current.y, current.support_z),
                    (target.x, target.y, target.support_z),
                ),
                now,
                lifetime=(
                    float(lifetime)
                    if lifetime is not None
                    else (
                        _WATER_BLOCKED_EDGE_SECONDS
                        if int(current.support_z)
                        >= int(C.Z_ABOVE_WATERPLANE) + 1
                        else _JUMP_BLOCKED_EDGE_SECONDS
                    )
                ),
            )
            return
        dx = float(waypoint[0]) - float(position[0])
        dy = float(waypoint[1]) - float(position[1])
        step_x, step_y = (
            (1 if dx > 0.0 else -1, 0)
            if abs(dx) >= abs(dy)
            else (0, 1 if dy > 0.0 else -1)
        )
        neighbor = self.world.surface(
            current.x + step_x,
            current.y + step_y,
            current.position[2],
            vertical_span=8,
            allow_water=(
                int(current.support_z)
                >= int(C.Z_ABOVE_WATERPLANE) + 1
                or route_step.affordance is MovementAffordance.SWIM
            ),
        )
        if neighbor is None:
            return
        edge = (
            (current.x, current.y, current.support_z),
            (neighbor.x, neighbor.y, neighbor.support_z),
        )
        self._remember_blocked_edge(
            state,
            edge,
            now,
            lifetime=(
                float(lifetime)
                if lifetime is not None
                else (
                    _WATER_BLOCKED_EDGE_SECONDS
                    if int(current.support_z)
                    >= int(C.Z_ABOVE_WATERPLANE) + 1
                    else _BLOCKED_EDGE_SECONDS
                )
            ),
        )

    @staticmethod
    def _remember_blocked_edge(
        state: _BotState,
        edge: EdgeKey,
        now: float,
        *,
        lifetime: float = _BLOCKED_EDGE_SECONDS,
    ) -> None:
        """Bound and time-limit one concrete failed navigation edge."""

        if edge not in state.blocked_edges and len(state.blocked_edges) >= _MAX_BLOCKED_EDGES:
            oldest = min(
                state.blocked_edges,
                key=state.blocked_edges.__getitem__,
            )
            state.blocked_edges.pop(oldest, None)
            state.blocked_edge_since.pop(oldest, None)
        state.blocked_edges[edge] = float(now) + max(0.1, float(lifetime))
        state.blocked_edge_since[edge] = float(now)
        if state.corridor_search is not None:
            state.corridor_search.exclude_edge(*edge)
        # A failed approach can start beside the coarse corridor, so testing
        # membership in its centreline misses bad joins and pins that corner.
        # Completed guidance must be rebuilt; only pending search work survives.
        state.corridor = ()
        state.corridor_index = 0
        state.corridor_retry_at = float(now) + 1.0

    @staticmethod
    def _reset_breach(state: _BotState, now: float) -> None:
        state.breach_key = None
        state.breach_started_at = float(now)
        state.next_breach_at = float(now)

    @staticmethod
    def _clear_route(state: _BotState, now: float) -> None:
        state.route = ()
        state.route_index = 0
        state.route_topology_version = -1
        state.waypoint_best_distance = math.inf
        state.waypoint_progress_at = float(now)
        SimpleBotBrain._reset_breach(state, now)

    @staticmethod
    def _prune_blocked_edges(state: _BotState, now: float) -> None:
        expired = [
            edge
            for edge, expires_at in state.blocked_edges.items()
            if float(expires_at) <= float(now)
        ]
        for edge in expired:
            state.blocked_edges.pop(edge, None)
            state.blocked_edge_since.pop(edge, None)

    def _prune_state(self, frame: PerceptionFrame) -> None:
        # Each frame contains a bounded subset, not the complete bot roster.
        # Missing from someone else's frame must not erase a live task/route.
        observed = {(p.player_id, p.generation): p for p in frame.players}
        retired = [key for key, state in self._states.items()
                   if (key in observed and (not observed[key].alive
                       or state.life_id != observed[key].life_id))
                   or frame.created_at - state.next_decision_at > 15]
        for key in retired:
            self._states.pop(key, None)
            self.mode_policy.forget(*key)
        while len(self._states) > 128:
            self._states.pop(min(self._states, key=lambda key: self._states[key].next_decision_at))

    def _intent(
        self,
        frame: PerceptionFrame,
        *,
        movement: MovementIntent,
        look: LookIntent | None,
        tool_id: int,
        action: BotAction = BotAction(),
        priority: BotIntentPriority = BotIntentPriority.ROUTINE,
        secondary_fire: bool = False,
        zoom: bool = False,
        debug_goal: Vector3 | None = None,
        debug_path: tuple[Vector3, ...] = (),
        debug_role: str,
        crowd_adjust: bool = True,
    ) -> BotIntent:
        if crowd_adjust:
            movement = self._crowd_adjusted_movement(
                frame,
                movement,
                action=action,
            )
        emitted_at = max(time.monotonic(), float(frame.created_at))
        navigation = self._states.get((int(frame.observer_id), int(frame.observer_generation)))
        return BotIntent(
            bot_id=int(frame.observer_id),
            bot_generation=int(frame.observer_generation),
            frame_id=int(frame.frame_id),
            map_epoch=int(frame.map_epoch),
            mode_epoch=int(frame.mode_epoch),
            topology_version=int(frame.topology_version),
            created_at=emitted_at,
            expires_at=emitted_at + _INTENT_TTL_SECONDS,
            movement=movement,
            look=look,
            tool_id=int(tool_id),
            action=action,
            priority=priority,
            secondary_fire=bool(secondary_fire),
            zoom=bool(zoom),
            debug_goal=debug_goal,
            debug_path=debug_path,
            debug_role=str(debug_role),
            life_id=next((p.life_id for p in frame.players if p.player_id == frame.observer_id
                          and p.generation == frame.observer_generation), -1),
            debug_navigation=(self._navigation_diagnostics(navigation, frame.created_at)
                              if navigation is not None else ()),
        )

    @staticmethod
    def _travel_gaze(state: _BotState, observer: PlayerSnapshot, now: float) -> Vector3 | None:
        """Where the eyes rest while the keys run the route.

        The body goes exactly where it is steered whatever the view, so the
        view is free to do what a player's does: stay for a moment on the spot
        an enemy was last seen, and otherwise look down the route, through the
        next corner, instead of at whichever cell is being stepped on.
        """

        contact = state.contact_position
        if (contact is not None
                and 0.0 <= now - (state.contact_until - _CONTACT_SECONDS) <= _CONTACT_GAZE_SECONDS
                and math.hypot(contact[0] - observer.position[0],
                               contact[1] - observer.position[1]) > 3.0):
            return contact
        rest = gaze_waypoint(state.route, state.route_index, observer.position)
        if rest is not None:
            state.travel_gaze, state.travel_gaze_at = rest, now
        elif (state.travel_gaze is not None and now - state.travel_gaze_at <= 1.5
              and math.hypot(state.travel_gaze[0] - observer.position[0],
                             state.travel_gaze[1] - observer.position[1]) > 3.0):
            # A route about to run out says nothing about where to look. The
            # next one will; until then the eyes stay where they were, not on
            # the last cell underfoot.
            return state.travel_gaze
        return rest

    @staticmethod
    def _navigation_diagnostics(state: _BotState, now: float) -> tuple:
        """Bounded scalar snapshot; never expose mutable search/path state."""
        search = state.corridor_search
        return (
            ("corridor_expansions", int(search.expansions) if search is not None else -1),
            ("corridor_done", bool(search.done) if search is not None else None),
            ("corridor_remaining", max(0, len(state.corridor) - state.corridor_index)),
            ("corridor_yield_local", state.corridor_yield_local),
            ("corridor_endpoint_failure", state.corridor_failed_goal is not None),
            ("escape_goal", state.escape_goal),
            ("escape_query_index", state.escape_search[2] if state.escape_search else -1),
            ("escape_attempts", state.escape_attempts),
            ("step_note", state.step_note),
            ("goal_progress_age", round(max(0.0, now - state.goal_progress_at), 3)),
            ("coverage_age", round(max(0.0, now - state.navigation_coverage_at), 3)),
            ("route_index", state.route_index),
            ("route_remaining", max(0, len(state.route) - state.route_index)),
            ("planning_results", len(state.planning_results)),
            ("planning_wait", state.planning_wait_at is not None),
            ("blocked_edges", len(state.blocked_edges)),
            ("support_no_progress_time", round(state.support_no_progress_time, 3)),
            ("support_progress_anchor", state.support_progress_anchor),
            ("support_failed_goal", state.support_failed_goal),
            ("support_failure_age", round(max(0.0, now - state.support_failure_at), 3)
             if state.support_failed_goal is not None else None),
            ("support_escape_attempted", state.support_escape_attempted),
        )

    @staticmethod
    def _crowd_adjusted_movement(
        frame: PerceptionFrame,
        movement: MovementIntent,
        *,
        action: BotAction,
    ) -> MovementIntent:
        """Keep friendly bot bodies apart without persistent crowd state."""

        if movement.affordance not in {
            MovementAffordance.WALK,
            MovementAffordance.CROUCH,
            MovementAffordance.SWIM,
        } or action.kind in {
            BotActionKind.BUILD,
            BotActionKind.BUILD_LINE,
            BotActionKind.MINE,
            BotActionKind.PLACE_PREFAB,
            BotActionKind.DEPLOY,
        }:
            return movement
        observer = next(
            (
                player
                for player in frame.players
                if int(player.player_id) == int(frame.observer_id)
                and int(player.generation)
                == int(frame.observer_generation)
            ),
            None,
        )
        if observer is None:
            return movement

        desired_x = float(movement.direction[0])
        desired_y = float(movement.direction[1])
        desired_length = math.hypot(desired_x, desired_y)
        if desired_length <= 1e-6:
            # Separation may shape requested locomotion, but it must never
            # turn an arrived/segment-complete/idle intent into a new goal.
            # GreatWall followers otherwise pressed into the breach owner
            # while their planner was explicitly yielding a stop frame.
            return movement
        desired_x /= desired_length
        desired_y /= desired_length

        repel_x = 0.0
        repel_y = 0.0
        neighbors = 0
        for player in frame.players:
            if (
                not player.is_bot
                or not player.alive
                or not player.spawned
                or int(player.player_id) == int(observer.player_id)
                or int(player.team) != int(observer.team)
                or abs(
                    float(player.position[2])
                    - float(observer.position[2])
                ) > _CROWD_VERTICAL_TOLERANCE
            ):
                continue
            dx = float(observer.position[0]) - float(player.position[0])
            dy = float(observer.position[1]) - float(player.position[1])
            distance = math.hypot(dx, dy)
            if distance >= _CROWD_PERSONAL_SPACE:
                continue
            if distance <= 1e-4:
                if desired_length > 1e-6:
                    sign = (
                        1.0
                        if int(observer.player_id) < int(player.player_id)
                        else -1.0
                    )
                    # Exact overlaps need opposite lateral shoulders. Sending
                    # one bot backward along the route made the next frame's
                    # planner and crowd owner fight each other in chokepoints.
                    unit_x = -desired_y * sign
                    unit_y = desired_x * sign
                else:
                    unit_x, unit_y = _deterministic_pair_axis(
                        int(observer.player_id), int(player.player_id)
                    )
            else:
                unit_x, unit_y = dx / distance, dy / distance
            strength = 1.0 - distance / _CROWD_PERSONAL_SPACE
            repel_x += unit_x * strength
            repel_y += unit_y * strength
            neighbors += 1

        if neighbors <= 0:
            return movement
        combined_x = desired_x + repel_x * _CROWD_REPULSION_WEIGHT
        combined_y = desired_y + repel_y * _CROWD_REPULSION_WEIGHT
        combined_length = math.hypot(combined_x, combined_y)
        if combined_length <= 1e-6:
            return movement
        # Crowd separation may bend a route, never reverse or nearly cancel
        # it. Precise voxel waypoints (and especially shore flow) otherwise
        # become unreachable even though each subsystem is individually
        # correct. Preserve full requested speed with at least 35% forward
        # projection and use repulsion only for the remaining lateral part.
        unit_x = combined_x / combined_length
        unit_y = combined_y / combined_length
        forward = unit_x * desired_x + unit_y * desired_y
        if forward < 0.35:
            lateral_x = unit_x - forward * desired_x
            lateral_y = unit_y - forward * desired_y
            lateral_length = math.hypot(lateral_x, lateral_y)
            if lateral_length > 1e-6:
                lateral_x /= lateral_length
                lateral_y /= lateral_length
                lateral_scale = math.sqrt(1.0 - 0.35 * 0.35)
                unit_x = desired_x * 0.35 + lateral_x * lateral_scale
                unit_y = desired_y * 0.35 + lateral_y * lateral_scale
            else:
                unit_x, unit_y = desired_x, desired_y
        scale = min(1.0, desired_length)
        return replace(
            movement,
            direction=(unit_x * scale, unit_y * scale, 0.0),
        )


def _weapon_tool(observer: PlayerSnapshot, distance: float | None = None) -> int:
    """Choose the firearm a player would hold: loaded first, sidearm up close."""

    owned = {int(tool) for tool in observer.loadout}
    firearms = [int(tool) for tool in observer.loadout if int(tool) in WEAPON_PROFILES]
    wallets = {int(tool): (int(clip), int(reserve))
               for tool, clip, reserve in observer.weapon_ammo}
    live = [tool for tool in firearms if sum(wallets.get(tool, (0, 0))) > 0]
    if live:
        primary = firearms[0]
        choice = primary if primary in live else live[0]
        sidearm = next((tool for tool in live if tool != primary), None)
        if distance is not None and sidearm is not None and choice == primary:
            holding_sidearm = int(observer.weapon_tool) == sidearm
            sidearm_loaded = wallets[sidearm][0] > 0
            # Drawing a pistol beats reloading in someone's face, and a scope
            # is the wrong tool inside a room. Separate draw/holster distances
            # keep one opponent at the boundary from flipping the hands.
            if sidearm_loaded and wallets[primary][0] <= 0 and distance < 28.0:
                choice = sidearm
            elif (WEAPON_PROFILES[primary].category == CAT_SNIPER
                  and distance < (22.0 if holding_sidearm else 13.0)):
                choice = sidearm
        return choice
    candidate = int(observer.weapon_tool)
    if candidate in owned and candidate in WEAPON_PROFILES:
        return candidate
    candidate = int(observer.tool)
    if candidate in owned and candidate in WEAPON_PROFILES:
        return candidate
    return next(
        (
            int(tool)
            for tool in observer.loadout
            if int(tool) in WEAPON_PROFILES
        ),
        next(
            (int(tool) for tool in observer.loadout),
            int(observer.tool),
        ),
    )


def _weapon_wallet(observer: PlayerSnapshot, tool: int) -> tuple[int, int]:
    """Clip and reserve of one owned firearm, held or stowed."""

    for owned, clip, reserve in observer.weapon_ammo:
        if int(owned) == int(tool):
            return int(clip), int(reserve)
    return int(observer.ammo_clip), int(observer.ammo_reserve)


def _melee_tool(observer: PlayerSnapshot) -> int | None:
    if not observer.can_shoot:
        return None
    return next(
        (
            int(tool)
            for tool in observer.loadout
            if int(tool) in SPADE_TOOL_IDS
        ),
        None,
    )


def _dig_profile(observer: PlayerSnapshot) -> DigProfile | None:
    """Return the fastest owned tool under the authoritative dig model."""

    if not observer.can_shoot:
        return None
    return best_navigation_dig_profile(
        int(tool) for tool in getattr(observer, "loadout", ())
    )


def _movement_abilities(
    observer: PlayerSnapshot,
) -> frozenset[MovementAffordance]:
    # The director owns a bounded/rearmed native jump pulse and its live VXL
    # gate validates both two-block climbs and two-column landings. Exposing
    # JUMP here reconnects that proven motor to the simple A* graph. Native
    # crouch does not reliably traverse authored two-cell-high openings, so
    # production bots clear the exact overhead voxel through BREACH instead.
    # Ordinary routing stays conservative around descents. Escape recovery
    # explicitly enables the native motor's validated two-to-four-block DROP.
    # BREACH is exposed only with an owned,
    # positive-damage melee profile and is executed as a stationary action.
    abilities = {MovementAffordance.JUMP}
    pack = int(getattr(observer, "jetpack_id", 0))
    properties = C.JETPACK_PROPERTIES.get(pack)
    if properties is not None and pack in {int(C.JETPACK2), int(C.JETPACK_ENGINEER)}:
        reserve = (properties[C.JETPACK_FUEL_ACTIVATION_COST]
                   + properties[C.JETPACK_FUEL_FLYING_CONSUMPTION] * 2.3 + 5.0)
        if observer.grounded and not observer.wade and observer.jetpack_fuel >= reserve:
            abilities.add(MovementAffordance.JETPACK)
    if _dig_profile(observer) is not None:
        abilities.add(MovementAffordance.BREACH)
    return frozenset(abilities)


def _normalized_xy(dx: float, dy: float) -> Vector3:
    length = math.hypot(float(dx), float(dy))
    if length <= 1e-6:
        return 0.0, 0.0, 0.0
    return float(dx) / length, float(dy) / length, 0.0


def _same_traversal_step(
    previous: RouteStep | None,
    current: RouteStep | None,
) -> bool:
    """Return whether a topology replan still owns the same concrete edge."""

    if previous is None or current is None:
        return False
    if previous.affordance is not current.affordance:
        return False
    if math.dist(previous.waypoint, current.waypoint) > 0.25:
        return False
    previous_target = (
        previous.breach.target_cell if previous.breach is not None else None
    )
    current_target = (
        current.breach.target_cell if current.breach is not None else None
    )
    return previous_target == current_target


def _route_edges(start: Vector3, steps: tuple[RouteStep, ...]) -> set[EdgeKey]:
    """Recover concrete edges hidden inside compacted straight waypoints.

    A validated escape can reconsider an old failure. Its first cell may be
    absent from the compacted waypoints, so checking only waypoint endpoints
    leaves that failure active and makes the very next replan empty again.
    """
    result: set[EdgeKey] = set()
    previous = start
    for step in steps:
        if step.breach is not None:
            result.add((step.breach.source, step.breach.destination))
        elif step.entry_edge is not None:
            result.add(step.entry_edge)
        else:
            count = max(1, int(math.ceil(math.dist(previous, step.waypoint) * 4.0)))
            node = (math.floor(previous[0]), math.floor(previous[1]), round(previous[2] + 2.25))
            for index in range(1, count + 1):
                point = tuple(a + (b - a) * index / count for a, b in zip(previous, step.waypoint))
                next_node = (math.floor(point[0]), math.floor(point[1]), round(point[2] + 2.25))
                if next_node != node:
                    result.add((node, next_node))
                    node = next_node
        previous = step.waypoint
    return result


def _plan_can_advance(plan: RoutePlan, position: Vector3) -> bool:
    """A path containing only the occupied cell is not an executable escape."""
    return any(step.affordance is MovementAffordance.BREACH
               or not _route_step_reached(step, position, wading=False)
               for step in plan.steps)


def _requires_traversal_entry(route: tuple[RouteStep, ...], index: int) -> bool:
    """Occupy a special movement's approach column before executing its edge."""
    return (index + 1 < len(route)
            and route[index + 1].affordance is not MovementAffordance.WALK)


def _route_step_reached(
    step: RouteStep,
    position: Vector3,
    *,
    wading: bool,
    previous_position: Vector3 | None = None,
    final_step: bool = True,
    require_current_cell: bool = False,
) -> bool:
    """Return whether the native body actually occupies a route landing."""

    vertical_tolerance = 0.75
    if require_current_cell and (
        int(math.floor(step.waypoint[0])) != int(math.floor(position[0]))
        or int(math.floor(step.waypoint[1])) != int(math.floor(position[1]))
    ):
        # Passing a corner between decisions completes ordinary walking,
        # but it cannot authorize a jump/drop from a different takeoff cell.
        return False
    if step.affordance is MovementAffordance.BREACH:
        # A body pressed against a face is horizontally close to the breach
        # waypoint while the blocking column is still solid.
        return False
    if (step.affordance is MovementAffordance.WALK
            and previous_position is not None
            and math.dist(previous_position, position) <= 4.0
            and abs(step.waypoint[2] - position[2]) <= vertical_tolerance
            and abs(step.waypoint[2] - previous_position[2]) <= vertical_tolerance):
        dx = position[0] - previous_position[0]
        dy = position[1] - previous_position[1]
        length_squared = dx * dx + dy * dy
        if length_squared > 1e-6:
            projection = ((step.waypoint[0] - previous_position[0]) * dx
                          + (step.waypoint[1] - previous_position[1]) * dy) / length_squared
            if 0.0 < projection < 1.0 and math.hypot(
                previous_position[0] + dx * projection - step.waypoint[0],
                previous_position[1] + dy * projection - step.waypoint[1],
            ) <= 0.45:
                # A sprint can pass a one-cell corner between decisions.
                return True
    if math.hypot(
        float(step.waypoint[0]) - float(position[0]),
        float(step.waypoint[1]) - float(position[1]),
    ) > _WAYPOINT_RADIUS:
        return False
    if step.affordance is MovementAffordance.JUMP and bool(wading):
        # Water bob can cross the shore waypoint's z without mounting the
        # bank. The authoritative wade flag is the actual exit contract.
        return False
    if (step.affordance is MovementAffordance.SWIM and not wading
            or final_step and step.affordance is MovementAffordance.WALK) and (
        int(math.floor(step.waypoint[0])) != int(math.floor(position[0]))
        or int(math.floor(step.waypoint[1])) != int(math.floor(position[1]))
    ):
        # A neighbour's centre can be inside the 0.9 radius before the body
        # enters its voxel. A dry approach to water needs its intermediate
        # cells too: skipping one can cut the following corner through the
        # bank before the body has entered the water column.
        return False
    # XY-only completion skipped GreatWall's two-block landing while the body
    # was still below it, then issued WALK for the upper corridor forever.
    return abs(float(step.waypoint[2]) - float(position[2])) <= vertical_tolerance


def _deterministic_pair_axis(left_id: int, right_id: int) -> tuple[float, float]:
    """Return opposite stable separation axes for one overlapping pair."""

    low, high = sorted((int(left_id), int(right_id)))
    mixed = (low * 73856093) ^ (high * 19349663) ^ 0x9E3779B9
    angle = float(mixed % 360) * math.pi / 180.0
    sign = 1.0 if int(left_id) == low else -1.0
    return math.cos(angle) * sign, math.sin(angle) * sign


def _distance_squared(left: Vector3, right: Vector3) -> float:
    return sum(
        (float(left[index]) - float(right[index])) ** 2
        for index in range(3)
    )


def _fallback_profile(player_id: int) -> BotProfile:
    return BotProfile(
        name=f"Bot{int(player_id)}",
        difficulty="normal",
        skill=0.55,
        aggression=0.55,
        caution=0.50,
        teamwork=0.55,
        creativity=0.50,
        reaction_time=0.32,
        tracking_delay=0.12,
        turn_speed=3.8,
        turn_acceleration=13.0,
        recoil_control=0.60,
        burst_discipline=0.60,
        preferred_range=24.0,
        aim_noise=0.055,
    )


def behavior_metrics_snapshot(brain: SimpleBotBrain) -> dict[str, object]:
    """Copy bounded coordinator diagnostics on the worker's owner thread."""
    cooperative = getattr(brain, "cooperative", None)
    if cooperative is None:
        return {}
    teams = cooperative.teams
    kinds: dict[str, int] = {}
    for project in teams.projects.values():
        kinds[project.kind] = kinds.get(project.kind, 0) + 1
    return {"counters": dict(teams.metrics),
            "events": tuple(dict(event) for event in teams.events),
            "active_projects": len(teams.projects),
            "project_kinds": kinds,
            "active_lives": len(cooperative.lives)}


def refresh_planning_observers(world: SimpleVoxelWorld, frames: Iterable[PerceptionFrame]) -> None:
    """Keep live FIFO waiters across slow coalesced batches, excluding dead lives."""
    budget = world.planning_budget
    if budget is None:
        return
    frames = tuple(frames)
    if not frames:
        return
    observers = {
        (int(frame.observer_id), int(frame.observer_generation))
        for frame in frames
        if any(player.player_id == frame.observer_id
               and player.generation == frame.observer_generation
               and player.alive and player.spawned for player in frame.players)
    }
    roster = max((sum(1 for player in frame.players if player.is_bot) for frame in frames),
                 default=0)
    budget.scale_for(max(roster, len(observers)))
    budget.refresh_waiters(observers, max(float(frame.created_at) for frame in frames))


def decide_current_frame(
    world: SimpleVoxelWorld,
    brain: SimpleBotBrain,
    frame: PerceptionFrame,
) -> BotIntent | None:
    """Use current terrain and isolate one bad observer from the fleet."""

    if (frame.map_epoch != world.map_epoch
            or frame.topology_version > world.topology_version):
        return None
    if frame.topology_version != world.topology_version:
        frame = replace(frame, topology_version=world.topology_version)
    world.begin_planning(
        (int(frame.observer_id), int(frame.observer_generation)),
        float(frame.created_at),
    )
    cancel_unused = False
    try:
        intent = brain.decide(frame)
        cancel_unused = intent is not None
        return intent
    except Exception:
        cancel_unused = True
        logger.exception(
            "Bot %d/%d decision failed; resetting its controller",
            frame.observer_id, frame.observer_generation,
        )
        brain.reset_bot(frame.observer_id, frame.observer_generation)
        return None
    finally:
        world.end_planning(cancel_unused=cancel_unused)


def _process_worker_batch(
    world: SimpleVoxelWorld,
    brain: SimpleBotBrain,
    messages: Iterable[object],
) -> tuple[bool, list[BotIntent]]:
    """Apply map messages and decide only the newest frame per bot life."""

    frames: dict[tuple[int, int], PerceptionFrame] = {}
    for message in messages:
        if isinstance(message, WorkerShutdown):
            return True, []
        if isinstance(message, MapSnapshot):
            world.load(message)
            brain.reset_for_map(message.map_epoch)
            frames.clear()
            continue
        if isinstance(message, WorldDelta):
            world.apply(message)
            continue
        if isinstance(message, PerceptionFrame):
            key = int(message.observer_id), int(message.observer_generation)
            previous = frames.get(key)
            if previous is None or int(message.frame_id) > int(previous.frame_id):
                frames[key] = message

    refresh_planning_observers(world, frames.values())
    intents: list[BotIntent] = []
    for frame in sorted(frames.values(), key=lambda item: int(item.frame_id)):
        intent = decide_current_frame(world, brain, frame)
        if intent is not None:
            intents.append(intent)
    return False, intents


def run_worker(
    input_queue,
    output_queue,
    seed: int = 0,
    decision_hz: float = 8.0,
    path_requests_per_second: float = 24.0,
) -> None:
    """Child entry point with a bounded, coalescing message loop."""

    from .planning_budget import PlanningBudget

    del seed
    world = SimpleVoxelWorld(planning_budget=PlanningBudget(
        path_requests_per_second, decision_hz=decision_hz))
    brain = SimpleBotBrain(world, decision_hz=decision_hz)
    snapshot_assembler = MapSnapshotAssembler()
    batch_id = 0
    next_metrics_at = time.monotonic() + 10.0
    while True:
        try:
            first = input_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        messages = [first]
        for _ in range(63):
            try:
                messages.append(input_queue.get_nowait())
            except queue.Empty:
                break
        try:
            decoded = snapshot_assembler.consume(messages)
        except SnapshotTransportError:
            logger.exception("Simple AI worker rejected map snapshot transport")
            raise

        processed_frame_id = max(
            (
                int(message.frame_id)
                for message in decoded
                if isinstance(message, PerceptionFrame)
            ),
            default=-1,
        )
        shutdown, intents = _process_worker_batch(world, brain, decoded)
        if shutdown:
            return
        if time.monotonic() >= next_metrics_at:
            logger.info("AI planning budget: %s", world.planning_budget.snapshot())
            logger.info("AI behavior: %s", behavior_metrics_snapshot(brain))
            next_metrics_at = time.monotonic() + 10.0
        batch_id += 1
        heartbeat = WorkerHeartbeat(
            batch_id=batch_id,
            processed_frame_id=processed_frame_id,
            map_epoch=int(world.map_epoch),
            topology_version=int(world.topology_version),
            snapshot_transfer_id=(
                snapshot_assembler.last_completed_transfer_id
            ),
        )
        try:
            output_queue.put(heartbeat, timeout=0.05)
        except queue.Full:
            pass
        for intent in intents:
            try:
                output_queue.put_nowait(intent)
            except queue.Full:
                break


__all__ = [
    "SimpleBotBrain",
    "run_worker",
]
