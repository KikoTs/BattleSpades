"""Invariant and progress monitoring for accelerated bot policy soaks.

The monitor is deliberately independent of the live server.  It consumes the
same immutable snapshots and intents that cross the worker boundary and turns
long simulations into small, deterministic counters.  It never changes bot
state and is therefore safe to use in tests or offline map diagnostics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math

import shared.constants as C

from .messages import BotActionKind, BotIntent, PlayerSnapshot, Vector3


_CONSTRUCTION_ACTIONS = frozenset(
    {
        BotActionKind.BUILD,
        BotActionKind.BUILD_LINE,
        BotActionKind.PLACE_PREFAB,
        BotActionKind.DEPLOY,
    }
)


@dataclass(slots=True)
class _BotState:
    """Progress history for one bot generation during a diagnostic run."""

    position: Vector3
    progress_at: float
    action_signature: tuple[object, ...] | None = None
    action_started_at: float = 0.0
    action_loop_reported: bool = False
    jump_started_at: float | None = None
    jump_loop_reported: bool = False
    water_started_at: float | None = None
    navigation_started_at: float | None = None
    navigation_stall_reported: bool = False


class BotSoakMonitor:
    """Detect priority inversions and repeated non-progressing bot actions.

    ``observe`` runs once per simulated decision frame.  The monitor accepts
    immutable worker messages, performs bounded O(players) checks, and records
    only counters plus one compact state row per bot.  A stationary bot doing
    no action is valid (for example a defender holding cover); repeated active
    construction or jumping without displacement is treated as a loop.
    """

    def __init__(
        self,
        *,
        loop_seconds: float = 3.0,
        jump_loop_seconds: float = 2.0,
        threat_distance: float = 8.0,
        progress_distance: float = 0.25,
    ) -> None:
        self.loop_seconds = max(0.1, float(loop_seconds))
        self.jump_loop_seconds = max(0.1, float(jump_loop_seconds))
        self.threat_distance = max(0.1, float(threat_distance))
        self.progress_distance = max(0.01, float(progress_distance))
        self._states: dict[tuple[int, int], _BotState] = {}
        self._samples = 0
        self._priority_violations = 0
        self._action_loops = 0
        self._jump_loops = 0
        self._navigation_stalls = 0
        self._invalid_looks = 0
        self._steep_looks = 0
        self._water_samples = 0
        self._max_stationary_seconds = 0.0
        self._max_water_seconds = 0.0
        self._roles: Counter[str] = Counter()
        self._actions: Counter[str] = Counter()

    def observe(
        self,
        now: float,
        observer: PlayerSnapshot,
        intent: BotIntent,
        players: tuple[PlayerSnapshot, ...],
    ) -> None:
        """Record one decision without mutating the simulated world."""

        timestamp = float(now)
        self._samples += 1
        self._roles[str(intent.debug_role or "idle")] += 1
        self._actions[intent.action.kind.value] += 1

        key = (int(observer.player_id), int(observer.generation))
        state = self._states.get(key)
        if state is None:
            state = _BotState(observer.position, timestamp)
            self._states[key] = state

        if self._distance(state.position, observer.position) >= self.progress_distance:
            state.position = observer.position
            state.progress_at = timestamp
            state.action_signature = None
            state.action_loop_reported = False
            state.jump_started_at = None
            state.jump_loop_reported = False

        stationary_for = max(0.0, timestamp - state.progress_at)
        self._max_stationary_seconds = max(
            self._max_stationary_seconds, stationary_for
        )

        signature = self._action_signature(intent)
        if signature is None:
            state.action_signature = None
            state.action_loop_reported = False
        elif signature != state.action_signature:
            state.action_signature = signature
            state.action_started_at = timestamp
            state.action_loop_reported = False
        elif (
            not state.action_loop_reported
            and timestamp - state.action_started_at >= self.loop_seconds
            and stationary_for >= self.loop_seconds
        ):
            self._action_loops += 1
            state.action_loop_reported = True

        if intent.movement.jump:
            if state.jump_started_at is None:
                state.jump_started_at = timestamp
                state.jump_loop_reported = False
            elif (
                not state.jump_loop_reported
                and timestamp - state.jump_started_at >= self.jump_loop_seconds
                and stationary_for >= self.jump_loop_seconds
            ):
                self._jump_loops += 1
                state.jump_loop_reported = True
        else:
            state.jump_started_at = None
            state.jump_loop_reported = False

        travel_role = self._is_travel_role(intent.debug_role)
        moving = math.hypot(
            intent.movement.direction[0], intent.movement.direction[1]
        ) > 0.1
        if (
            travel_role
            and not moving
            and intent.action.kind is BotActionKind.NONE
        ):
            if state.navigation_started_at is None:
                state.navigation_started_at = timestamp
                state.navigation_stall_reported = False
            elif (
                not state.navigation_stall_reported
                and timestamp - state.navigation_started_at >= self.loop_seconds
                and stationary_for >= self.loop_seconds
            ):
                self._navigation_stalls += 1
                state.navigation_stall_reported = True
        else:
            state.navigation_started_at = None
            state.navigation_stall_reported = False

        # Position alone cannot distinguish the lowest legal dry surface from
        # the water plane across stock maps.  Production physics already
        # publishes the authoritative wade bit, so diagnostics must trust it.
        in_water = bool(observer.wade)
        if in_water:
            self._water_samples += 1
            if state.water_started_at is None:
                state.water_started_at = timestamp
            self._max_water_seconds = max(
                self._max_water_seconds, timestamp - state.water_started_at
            )
        else:
            state.water_started_at = None

        if intent.look is not None:
            look = intent.look.target
            if (
                not all(math.isfinite(component) for component in look)
                or not (0.0 <= look[0] < 512.0)
                or not (0.0 <= look[1] < 512.0)
                or not (-16.0 <= look[2] < 240.0)
            ):
                self._invalid_looks += 1
            horizontal = math.hypot(
                look[0] - observer.eye[0], look[1] - observer.eye[1]
            )
            vertical = abs(look[2] - observer.eye[2])
            if vertical > 8.0 and vertical > horizontal * 2.75:
                self._steep_looks += 1

        if intent.action.kind in _CONSTRUCTION_ACTIONS:
            enemy = self._nearest_living_enemy(observer, players)
            if enemy is not None:
                distance = self._distance(observer.position, enemy.position)
                if distance <= self.threat_distance and not self._is_zombie_climb(
                    observer, enemy, intent
                ):
                    self._priority_violations += 1

    def summary(self) -> dict[str, object]:
        """Return a JSON-serializable aggregate for CI and human inspection."""

        return {
            "samples": self._samples,
            "bots_observed": len(self._states),
            "priority_violations": self._priority_violations,
            "action_loops": self._action_loops,
            "jump_loops": self._jump_loops,
            "navigation_stalls": self._navigation_stalls,
            "invalid_looks": self._invalid_looks,
            "steep_looks": self._steep_looks,
            "water_samples": self._water_samples,
            "max_stationary_seconds": round(self._max_stationary_seconds, 3),
            "max_water_seconds": round(self._max_water_seconds, 3),
            "roles": dict(self._roles.most_common()),
            "actions": dict(self._actions.most_common()),
        }

    @staticmethod
    def _distance(left: Vector3, right: Vector3) -> float:
        return math.dist(left, right)

    @staticmethod
    def _action_signature(intent: BotIntent) -> tuple[object, ...] | None:
        action = intent.action
        if action.kind is BotActionKind.NONE:
            return None

        def rounded(position: Vector3 | None) -> tuple[float, ...] | None:
            if position is None:
                return None
            return tuple(round(component, 1) for component in position)

        return (
            action.kind.value,
            int(action.tool_id),
            rounded(action.position),
            rounded(action.end_position),
            str(action.argument),
            str(intent.debug_role),
        )

    @staticmethod
    def _nearest_living_enemy(
        observer: PlayerSnapshot,
        players: tuple[PlayerSnapshot, ...],
    ) -> PlayerSnapshot | None:
        enemies = (
            player
            for player in players
            if player.player_id != observer.player_id
            and player.alive
            and player.spawned
            and player.team != observer.team
        )
        return min(
            enemies,
            key=lambda player: math.dist(observer.position, player.position),
            default=None,
        )

    @staticmethod
    def _is_zombie_climb(
        observer: PlayerSnapshot,
        enemy: PlayerSnapshot,
        intent: BotIntent,
    ) -> bool:
        """Permit a Zombie's deliberate build-up toward an elevated victim."""

        return (
            int(observer.class_id) == int(C.CLASS_ZOMBIE)
            and intent.debug_role in {"zombie_build_climb", "zombie_hunt_breach"}
            and enemy.position[2] < observer.position[2] - 2.5
        )

    @staticmethod
    def _is_travel_role(role: str) -> bool:
        normalized = str(role).lower()
        return normalized == "resource" or any(
            token in normalized
            for token in (
                "assault",
                "hunt",
                "escort",
                "recover",
                "return",
                "regroup",
                "intercept",
                "medic_support",
            )
        )
