"""Small deterministic bot worker used by the production supervisor.

This replaces the legacy behavior-tree worker with a single ownership loop:

``select goal -> plan segment -> execute edge -> verify progress``.

There are no nested recovery modes, construction side quests, resource
targets, persistent crowd agents, or competing locomotion owners.  A failed
edge is temporarily excluded and the same planner searches again.
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
    ModeBotPosture,
    mode_decision_allows_combat,
    objective_decision_for,
)
from .simple_navigation import RoutePlan, RouteStep, SimpleVoxelWorld
from .snapshot_transport import MapSnapshotAssembler, SnapshotTransportError


logger = logging.getLogger(__name__)

_VISUAL_RANGE = 160.0
_CONTACT_SECONDS = 4.0
_INTENT_TTL_SECONDS = 0.4
_WAYPOINT_RADIUS = 0.9
_WAYPOINT_STALL_SECONDS = 1.75
_GOAL_STALL_SECONDS = 8.0
_NAVIGATION_PROGRESS_SECONDS = 2.5
_NAVIGATION_PROGRESS_DISTANCE = 0.5
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
    contact_id: int = -1
    contact_generation: int = -1
    contact_position: Vector3 | None = None
    contact_until: float = 0.0
    acquired_at: float = 0.0
    next_oriented_at: float = 0.0
    next_support_at: float = 0.0
    next_deploy_at: float = 0.0
    breach_key: tuple[object, ...] | None = None
    breach_started_at: float = 0.0
    next_breach_at: float = 0.0
    yielded_breach_edge: EdgeKey | None = None
    yielded_breach_started_at: float = 0.0
    next_water_build_at: float = 0.0
    water_step_key: tuple[int, int, int, str] | None = None
    water_best_distance: float = math.inf
    water_progress_at: float = 0.0
    water_recovery: bool = False
    water_committed: bool = False
    water_goal_reached: bool = False
    combat_progress_position: Vector3 | None = None
    combat_progress_at: float = 0.0
    combat_last_at: float = 0.0
    combat_stall_stage: int = 0
    crowd_anchor: Vector3 | None = None
    crowd_progress_at: float = 0.0
    crowd_detour_goal: Vector3 | None = None
    crowd_detour_until: float = 0.0
    dry_detour_goal: Vector3 | None = None
    dry_detour_until: float = 0.0
    dry_route_failures: int = 0
    navigation_progress_position: Vector3 | None = None
    navigation_progress_at: float = 0.0


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

    def reset_for_map(self, map_epoch: int) -> None:
        """Discard every controller and route from the previous map."""

        normalized = int(map_epoch)
        if normalized == self._map_epoch:
            return
        self._map_epoch = normalized
        self._states.clear()
        self._team_oriented_ready_at.clear()

    def decide(self, frame: PerceptionFrame) -> BotIntent | None:
        """Return the newest bounded intention for one observer."""

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
        ):
            state = _BotState(
                map_epoch=int(frame.map_epoch),
                mode_epoch=int(frame.mode_epoch),
                life_id=int(observer.life_id),
            )
            self._states[key] = state

        now = float(frame.created_at)
        if now + 1e-9 < state.next_decision_at:
            return None
        state.next_decision_at = now + self._decision_interval
        self._prune_state(frame)
        self._prune_blocked_edges(state, now)

        profile = frame.profile or _fallback_profile(observer.player_id)
        if observer.wade:
            state.water_committed = True
        elif state.water_committed and self._landed_on_dry_surface(observer):
            state.water_committed = False
            state.dry_detour_goal = None
            state.dry_detour_until = 0.0
            state.dry_route_failures = 0

        if state.water_committed:
            state.navigation_progress_position = None
            state.navigation_progress_at = now
            water_edge_blocked = any(
                int(source[2]) >= int(C.Z_ABOVE_WATERPLANE) + 1
                for source, _target in state.blocked_edges
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
                ) or strategic.debug_role.endswith(":segment_complete"):
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
                blocked_edges=frozenset(state.blocked_edges),
            )
            return self._water_intent(
                frame,
                observer,
                water_step,
                now,
            )

        state.water_step_key = None
        state.water_best_distance = math.inf
        state.water_progress_at = now
        state.water_recovery = False
        state.water_goal_reached = False

        mode_decision = objective_decision_for(frame, observer)
        visible_target = self._visible_target(
            frame,
            observer,
            state,
            mode_decision,
        )
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
            if mode_decision is None
            or mode_decision.objective_priority < 0.9
            or mode_decision.posture
            in {ModeBotPosture.DEFEND, ModeBotPosture.ESCORT}
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
            if mode_decision is None
            or mode_decision.directive == "fortify"
            or mode_decision.objective_priority < 0.85
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

    def _visible_target(
        self,
        frame: PerceptionFrame,
        observer: PlayerSnapshot,
        state: _BotState,
        mode_decision: ModeBotDecision | None = None,
    ) -> PlayerSnapshot | None:
        """Select a stable enemy that is alive, in range, and unobscured."""

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
        target = current or min(
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
            decision = objective_decision_for(frame, observer)
        if decision is not None and decision.objective_priority >= 0.7:
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
        return self._goal_from_mode_decision(decision)

    @staticmethod
    def _goal_from_mode_decision(decision: ModeBotDecision) -> _Goal:
        """Normalize a mode policy into the controller's one goal type."""

        position = tuple(float(value) for value in decision.position)
        return _Goal(
            (
                "objective",
                str(decision.role),
                int(round(position[0] / 4.0)),
                int(round(position[1] / 4.0)),
            ),
            position,
            str(decision.role),
            max(0.75, float(decision.arrival_radius)),
            bool(decision.sprint),
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
        """Chase or fight one visible enemy; no other goal may move the bot."""

        self._update_combat_progress(state, observer.position, now)

        dx = float(target.position[0]) - float(observer.position[0])
        dy = float(target.position[1]) - float(observer.position[1])
        distance = math.hypot(dx, dy)
        direction_to_target = _normalized_xy(dx, dy)
        weapon_tool = _weapon_tool(observer)
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
            if observer.reloading:
                action = BotAction()
            elif observer.ammo_clip <= 0 and observer.ammo_reserve > 0:
                action = BotAction(
                    BotActionKind.RELOAD,
                    tool_id=weapon_tool,
                )
            elif (
                observer.ammo_clip <= 0
                and observer.ammo_reserve <= 0
                and melee_tool is not None
            ):
                selected_tool = int(melee_tool)
                if distance <= 3.0:
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
                if distance > float(envelope.ideal_max):
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
                elif distance < max(2.0, float(envelope.ideal_min) * 0.65):
                    self._set_goal(state, None, observer.position, now)
                    movement = _normalized_xy(-dx, -dy)
                    sprint = False
                elif envelope.prefers_stationary:
                    self._set_goal(state, None, observer.position, now)
                    movement = (0.0, 0.0, 0.0)
                    crouch = True
                    sprint = False
                else:
                    self._set_goal(state, None, observer.position, now)
                    sign = -1.0 if int(observer.player_id) & 1 else 1.0
                    movement = _normalized_xy(-dy * sign, dx * sign)
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
                    observer.ammo_clip > 0
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

        if not zombie and mode_decision is not None:
            objective_goal = self._goal_from_mode_decision(mode_decision)
            if posture in {
                ModeBotPosture.EVASIVE,
                ModeBotPosture.SURVIVE,
            }:
                # Carriers and last survivors keep following the authoritative
                # objective route while returning fire at immediate threats.
                pursuit = self._navigation_intent(
                    frame,
                    observer,
                    state,
                    objective_goal,
                    now,
                )
                movement = pursuit.movement.direction
                affordance = pursuit.movement.affordance
                sprint = bool(mode_decision.sprint)
                crouch = False
            elif (
                posture
                in {
                    ModeBotPosture.DEFEND,
                    ModeBotPosture.ESCORT,
                    ModeBotPosture.BUILD,
                    ModeBotPosture.MINE,
                }
                and distance > float(envelope.ideal_max)
            ):
                # Guards fight from the protected area instead of chasing a
                # visible opponent until their objective is left undefended.
                pursuit = self._navigation_intent(
                    frame,
                    observer,
                    state,
                    objective_goal,
                    now,
                )
                movement = pursuit.movement.direction
                affordance = pursuit.movement.affordance
                sprint = False

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
            else (0.0 if float(profile.skill) >= 0.82 else 1.0)
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
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=movement,
                jump=affordance is MovementAffordance.JUMP,
                crouch=crouch,
                sprint=sprint,
                affordance=affordance,
            ),
            look=look,
            tool_id=selected_tool,
            action=action,
            priority=priority,
            secondary_fire=scoped,
            zoom=scoped,
            debug_goal=target.position,
            debug_path=(
                pursuit.debug_path if pursuit is not None else ()
            ),
            debug_role=(
                "combat_pursuit"
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
        """Advance the blocked-strafe phase only after measured non-progress."""

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
            decision = objective_decision_for(frame, observer)
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

        effective_wading = (
            bool(observer.wade)
            if water_context is None
            else bool(water_context)
        )

        self._set_goal(state, goal, observer.position, now)
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

        goal_distance = math.hypot(
            float(active_goal.position[0]) - float(observer.position[0]),
            float(active_goal.position[1]) - float(observer.position[1]),
        )
        if goal_distance <= active_goal.arrival_radius and not effective_wading:
            if active_goal.role == "chase_last_seen":
                state.contact_until = 0.0
                state.contact_position = None
            state.route = ()
            state.route_index = 0
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:arrived",
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

        if goal_distance + 1.0 < state.goal_best_distance:
            state.goal_best_distance = goal_distance
            state.goal_progress_at = now
        elif now - state.goal_progress_at >= _GOAL_STALL_SECONDS:
            self._invalidate_current_edge(state, observer.position, now)
            self._clear_route(state, now)
            state.goal_best_distance = goal_distance
            state.goal_progress_at = now

        topology_changed = (
            bool(state.route)
            and int(state.route_topology_version)
            != int(frame.topology_version)
        )
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
            self._reset_breach(state, now)
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
            planning_goal = (
                state.crowd_detour_goal
                if crowd_detour_active
                else (
                    state.dry_detour_goal
                    if state.dry_detour_goal is not None
                    else self._team_lane_segment_goal(
                        frame,
                        observer,
                        active_goal.position,
                    )
                )
            )
            assert planning_goal is not None
            plan_arguments = {
                "abilities": _movement_abilities(observer),
                "dig_profile": _dig_profile(observer),
                "blocked_edges": frozenset(state.blocked_edges),
            }
            if effective_wading:
                plan = self.world.plan(
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
                plan = self.world.plan(
                    observer.position,
                    planning_goal,
                    allow_water=True,
                    **plan_arguments,
                )
            else:
                # Dry-route and builder identities search the shoreline
                # before conceding to a swim. A bounded A* can reach the local
                # minimum at the water's edge without proving there is no
                # surface route around it, so an empty segment first creates
                # a stable lateral dry target instead of entering immediately.
                dry_plan = self.world.plan(
                    observer.position,
                    planning_goal,
                    allow_water=False,
                    **plan_arguments,
                )
                if dry_plan.steps:
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
                        detour_plan = self.world.plan(
                            observer.position,
                            state.dry_detour_goal,
                            allow_water=False,
                            **plan_arguments,
                        )
                    plan = (
                        detour_plan
                        if detour_plan.steps
                        else self.world.plan(
                            observer.position,
                            planning_goal,
                            allow_water=True,
                            **plan_arguments,
                        )
                    )
                    if not detour_plan.steps:
                        state.dry_detour_goal = None
                        state.dry_detour_until = 0.0
            state.route = plan.steps
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

        while state.route_index < len(state.route):
            step = state.route[state.route_index]
            if not _route_step_reached(
                step,
                observer.position,
                wading=effective_wading,
            ):
                break
            state.route_index += 1
            state.waypoint_best_distance = math.inf
            state.waypoint_progress_at = now

        if state.route_index >= len(state.route):
            # A bounded segment ended before the strategic destination.
            # Replan from the new position on the next frame.
            state.route = ()
            return self._intent(
                frame,
                movement=MovementIntent(),
                look=None,
                tool_id=_weapon_tool(observer),
                debug_goal=active_goal.position,
                debug_role=f"{active_goal.role}:segment_complete",
            )

        step = state.route[state.route_index]
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
        ):
            self._invalidate_current_edge(state, observer.position, now)
            self._clear_route(state, now)
            state.navigation_progress_position = observer.position
            state.navigation_progress_at = float(now)
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
                sprint=active_goal.sprint
                and motor_affordance
                in {MovementAffordance.WALK, MovementAffordance.SWIM},
                affordance=motor_affordance,
            ),
            look=LookIntent(step.waypoint, visible=False),
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
        if breach is None or not self.world.solid(*breach.target_cell):
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
            state.breach_key = breach_key
            state.breach_started_at = float(now)
            state.next_breach_at = float(now)

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
                crouch=True,
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
            or int(observer.blocks) < 2
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
    ) -> BotIntent:
        """Give wading survival sole ownership of locomotion."""

        state = self._states[
            (int(observer.player_id), int(observer.generation))
        ]
        if step is None:
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
                blocked_edges=frozenset(state.blocked_edges),
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
                    blocked_edges=frozenset(state.blocked_edges),
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
                return self._breach_intent(
                    frame,
                    observer,
                    state,
                    goal,
                    breach,
                    now,
                )
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
        if state.water_step_key != step_key:
            state.water_step_key = step_key
            state.water_best_distance = distance
            state.water_progress_at = now
        elif distance + 0.35 < state.water_best_distance:
            state.water_best_distance = distance
            state.water_progress_at = now
        elif now - state.water_progress_at >= _WAYPOINT_STALL_SECONDS:
            current = self.world.surface(
                int(math.floor(observer.position[0])),
                int(math.floor(observer.position[1])),
                float(observer.position[2]),
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
            if current is not None and target is not None:
                self._remember_blocked_edge(
                    state,
                    (
                        (current.x, current.y, current.support_z),
                        (target.x, target.y, target.support_z),
                    ),
                    now,
                    lifetime=_WATER_BLOCKED_EDGE_SECONDS,
                )
            state.water_step_key = None
            state.water_best_distance = math.inf
            state.water_progress_at = now
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

    def _landed_on_dry_surface(self, observer: PlayerSnapshot) -> bool:
        """Return whether a former swimmer has a stable dry foothold.

        The native wade bit can clear for individual airborne bob frames at a
        bank. Releasing water ownership on that bit alone made London bots
        alternate SWIM/JUMP against the same wall forever.
        """

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
        if old is None and goal is None:
            return
        state.goal = goal
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

        if len(state.blocked_edges) >= _MAX_BLOCKED_EDGES:
            oldest = min(
                state.blocked_edges,
                key=state.blocked_edges.__getitem__,
            )
            state.blocked_edges.pop(oldest, None)
        state.blocked_edges[edge] = float(now) + max(0.1, float(lifetime))

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

    def _prune_state(self, frame: PerceptionFrame) -> None:
        active = {
            (int(player.player_id), int(player.generation))
            for player in frame.players
            if player.is_bot
        }
        retired = [key for key in self._states if key not in active]
        for key in retired:
            self._states.pop(key, None)

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


def _weapon_tool(observer: PlayerSnapshot) -> int:
    owned = {int(tool) for tool in observer.loadout}
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


def _melee_tool(observer: PlayerSnapshot) -> int | None:
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
    # DROP and jetpack remain disabled until they have equally explicit
    # multi-tick execution contracts. BREACH is exposed only with an owned,
    # positive-damage melee profile and is executed as a stationary action.
    abilities = {MovementAffordance.JUMP}
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


def _route_step_reached(
    step: RouteStep,
    position: Vector3,
    *,
    wading: bool,
) -> bool:
    """Return whether the native body actually occupies a route landing."""

    if step.affordance is MovementAffordance.BREACH:
        # A body pressed against a face is horizontally close to the breach
        # waypoint while the blocking column is still solid.
        return False
    if math.hypot(
        float(step.waypoint[0]) - float(position[0]),
        float(step.waypoint[1]) - float(position[1]),
    ) > _WAYPOINT_RADIUS:
        return False
    if step.affordance is MovementAffordance.JUMP and bool(wading):
        # Water bob can cross the shore waypoint's z without mounting the
        # bank. The authoritative wade flag is the actual exit contract.
        return False
    # XY-only completion skipped GreatWall's two-block landing while the body
    # was still below it, then issued WALK for the upper corridor forever.
    return abs(float(step.waypoint[2]) - float(position[2])) <= 0.75


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

    intents: list[BotIntent] = []
    for frame in sorted(frames.values(), key=lambda item: int(item.frame_id)):
        if (
            int(frame.map_epoch) != int(world.map_epoch)
            or int(frame.topology_version) != int(world.topology_version)
        ):
            continue
        intent = brain.decide(frame)
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

    del seed, path_requests_per_second
    world = SimpleVoxelWorld()
    brain = SimpleBotBrain(world, decision_hz=decision_hz)
    snapshot_assembler = MapSnapshotAssembler()
    batch_id = 0
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
