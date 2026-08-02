"""Small deterministic bot worker used by the production supervisor.

This replaces the legacy behavior-tree worker with a single ownership loop:

``select goal -> plan segment -> execute edge -> verify progress``.

There are no nested recovery modes, construction side quests, resource
targets, persistent crowd agents, or competing locomotion owners.  A failed
edge is temporarily excluded and the same planner searches again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from .policies import ModeBotDecision, objective_decision_for
from .simple_navigation import RouteStep, SimpleVoxelWorld
from .snapshot_transport import MapSnapshotAssembler, SnapshotTransportError


logger = logging.getLogger(__name__)

_VISUAL_RANGE = 160.0
_CONTACT_SECONDS = 4.0
_INTENT_TTL_SECONDS = 0.4
_WAYPOINT_RADIUS = 0.9
_WAYPOINT_STALL_SECONDS = 1.75
_GOAL_STALL_SECONDS = 8.0
_BLOCKED_EDGE_SECONDS = 6.0
_MAX_BLOCKED_EDGES = 8
_TEAM_ORIENTED_SPACING_SECONDS = 1.0
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
            # A visible enemy across a river is not a traversable waypoint.
            # Survival owns locomotion until the authoritative body leaves
            # water; combat reacquires immediately from the first dry frame.
            water_step = self.world.water_step(observer.position)
            return self._water_intent(
                frame,
                observer,
                water_step,
                now,
            )

        visible_target = self._visible_target(
            frame,
            observer,
            state,
        )
        if visible_target is not None:
            return self._combat_intent(
                frame,
                observer,
                visible_target,
                state,
                profile,
                now,
            )

        support = self._medic_support_intent(
            frame,
            observer,
            state,
            now,
        )
        if support is not None:
            return support

        deployable = self._strategic_deploy_intent(
            frame,
            observer,
            state,
            now,
        )
        if deployable is not None:
            return deployable

        goal = self._select_goal(frame, observer, state, now)
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
    ) -> _Goal | None:
        """Choose exactly one non-combat movement owner."""

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

        decision = objective_decision_for(frame, observer)
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
    ) -> BotIntent:
        """Chase or fight one visible enemy; no other goal may move the bot."""

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
    ) -> BotIntent | None:
        """Place durable gadgets only where they support an active route."""

        if now < state.next_deploy_at or not observer.grounded:
            return None
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
    ) -> BotIntent:
        """Plan/follow one route and invalidate a concrete failed edge."""

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

        goal_distance = math.hypot(
            float(active_goal.position[0]) - float(observer.position[0]),
            float(active_goal.position[1]) - float(observer.position[1]),
        )
        if goal_distance <= active_goal.arrival_radius:
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
            self._reset_breach(state, now)
            plan = self.world.plan(
                observer.position,
                active_goal.position,
                abilities=_movement_abilities(observer),
                dig_profile=_dig_profile(observer),
                blocked_edges=frozenset(state.blocked_edges),
            )
            state.route = plan.steps
            state.route_index = 0
            state.route_topology_version = int(frame.topology_version)
            state.waypoint_best_distance = math.inf
            state.waypoint_progress_at = now

        while state.route_index < len(state.route):
            step = state.route[state.route_index]
            if step.affordance is MovementAffordance.BREACH:
                # A bot pressed against the face may be within the ordinary
                # waypoint radius while the body column is still solid.
                break
            distance = math.hypot(
                float(step.waypoint[0]) - float(observer.position[0]),
                float(step.waypoint[1]) - float(observer.position[1]),
            )
            if distance > _WAYPOINT_RADIUS:
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
            return self._breach_intent(
                frame,
                observer,
                state,
                active_goal,
                step,
                now,
            )
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
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=direction,
                jump=affordance is MovementAffordance.JUMP,
                crouch=affordance is MovementAffordance.CROUCH,
                sprint=active_goal.sprint
                and affordance is MovementAffordance.WALK,
                affordance=affordance,
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
        self._set_goal(state, None, observer.position, now)
        if step is None:
            return self._intent(
                frame,
                movement=MovementIntent(jump=True),
                look=None,
                tool_id=_weapon_tool(observer),
                priority=BotIntentPriority.SURVIVAL,
                debug_role="water_no_route",
            )
        direction = _normalized_xy(
            float(step.waypoint[0]) - float(observer.position[0]),
            float(step.waypoint[1]) - float(observer.position[1]),
        )
        return self._intent(
            frame,
            movement=MovementIntent(
                direction=direction,
                jump=True,
                sprint=True,
                affordance=step.affordance,
            ),
            look=LookIntent(step.waypoint, visible=False),
            tool_id=_weapon_tool(observer),
            priority=BotIntentPriority.SURVIVAL,
            debug_goal=step.waypoint,
            debug_path=(observer.position, step.waypoint),
            debug_role="water_exit",
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
        # Failed edges belong to the concrete goal that encountered them.
        state.blocked_edges.clear()
        SimpleBotBrain._reset_breach(state, now)

    def _invalidate_current_edge(
        self,
        state: _BotState,
        position: Vector3,
        now: float,
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
            )
            return
        current = self.world.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=8,
        )
        if current is None:
            return
        waypoint = state.route[state.route_index].waypoint
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
        )
        if neighbor is None:
            return
        edge = (
            (current.x, current.y, current.support_z),
            (neighbor.x, neighbor.y, neighbor.support_z),
        )
        self._remember_blocked_edge(state, edge, now)

    @staticmethod
    def _remember_blocked_edge(
        state: _BotState,
        edge: EdgeKey,
        now: float,
    ) -> None:
        """Bound and time-limit one concrete failed navigation edge."""

        if len(state.blocked_edges) >= _MAX_BLOCKED_EDGES:
            oldest = min(
                state.blocked_edges,
                key=state.blocked_edges.__getitem__,
            )
            state.blocked_edges.pop(oldest, None)
        state.blocked_edges[edge] = float(now) + _BLOCKED_EDGE_SECONDS

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

    @staticmethod
    def _intent(
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
    ) -> BotIntent:
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
    # JUMP here reconnects that proven motor to the simple A* graph. Crouch,
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
