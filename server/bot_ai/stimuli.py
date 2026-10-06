"""Bounded fair sensory-event feed for the isolated bot worker."""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass

import shared.constants as C

from .messages import Stimulus, StimulusKind, Vector3

# The retail client drops every positioned sound farther than this from the
# listener before it is played (MediaManager.play; constants_audio). A shot,
# a blast, a spade on stone and a footstep all share the one distance.
HEARING_DISTANCE = float(getattr(C, "HEARING_DISTANCE", 50.0))
# Retail footstep cadence. A step is heard from a character walking on the
# ground; crouching and sneaking are silent, which is why players do them.
FOOTSTEP_INTERVAL_WALK = 0.512
FOOTSTEP_INTERVAL_SPRINT = 0.386
_FOOTSTEP_SPEED = 1.5
_FOOTSTEP_LIFETIME = 0.45
_SOUNDS = frozenset({
    StimulusKind.SHOT, StimulusKind.EXPLOSION, StimulusKind.BLOCK_DESTROYED,
    StimulusKind.FOOTSTEP, StimulusKind.DEPLOYABLE,
})


@dataclass(frozen=True, slots=True)
class _SoundEvent:
    kind: StimulusKind
    position: Vector3
    created_at: float
    expires_at: float
    radius: float
    source_id: int
    team: int


class BotStimulusBus:
    """Retain a small time window of sounds without exposing exact positions.

    Publishing is O(1) on the gameplay thread. Perception is sampled only for
    a bot whose staggered 10 Hz frame is already due. Returned locations have
    deterministic distance-dependent error and therefore cannot be used as a
    hidden-position oracle.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._events: deque[_SoundEvent] = deque(maxlen=max(32, int(capacity)))
        self._last_publish: dict[tuple[StimulusKind, int], float] = {}
        self._next_step: dict[int, float] = {}

    def publish(
        self,
        kind: StimulusKind,
        position: Vector3,
        *,
        source_id: int = -1,
        team: int = -1,
        radius: float = 64.0,
        lifetime: float = 1.5,
        now: float | None = None,
    ) -> bool:
        """Publish one rate-limited finite event; malformed values fail closed."""

        current = time.monotonic() if now is None else float(now)
        key = kind, int(source_id)
        if current - self._last_publish.get(key, -math.inf) < 0.05:
            return False
        try:
            normalized = tuple(float(value) for value in position)
        except (TypeError, ValueError):
            return False
        if len(normalized) != 3 or not all(math.isfinite(value) for value in normalized):
            return False
        radius = max(0.0, min(256.0, float(radius)))
        if kind in _SOUNDS:
            # Nobody hears farther than the game plays the sound.
            radius = min(radius, HEARING_DISTANCE)
        lifetime = max(0.05, min(10.0, float(lifetime)))
        self._last_publish[key] = current
        self._events.append(
            _SoundEvent(
                kind,
                normalized,
                current,
                current + lifetime,
                radius,
                int(source_id),
                int(team),
            )
        )
        return True

    def note_footsteps(self, players, now: float) -> int:
        """Publish the footsteps the roster is making; returns how many.

        Called once per perception refresh with the live players. Each walker
        gets one step per retail cadence interval, so the cost is one pass
        over the roster and the feed holds about one step per moving player.
        """

        published = 0
        live = set()
        for player in players:
            player_id = int(getattr(player, "id", -1))
            live.add(player_id)
            if not (getattr(player, "alive", False) and getattr(player, "spawned", False)
                    and getattr(player, "grounded", False)):
                continue
            controls = getattr(player, "input", None)
            if getattr(controls, "crouch", False) or getattr(controls, "sneak", False):
                continue
            speed = math.hypot(float(getattr(player, "vx", 0.0)), float(getattr(player, "vy", 0.0)))
            if speed < _FOOTSTEP_SPEED or now < self._next_step.get(player_id, 0.0):
                continue
            self._next_step[player_id] = now + (
                FOOTSTEP_INTERVAL_SPRINT if getattr(controls, "sprint", False)
                else FOOTSTEP_INTERVAL_WALK)
            published += self.publish(
                StimulusKind.FOOTSTEP, tuple(player.position), source_id=player_id,
                team=int(getattr(player, "team", -1)), radius=HEARING_DISTANCE,
                lifetime=_FOOTSTEP_LIFETIME, now=now)
        if len(self._next_step) > len(live) + 32:
            self._next_step = {key: value for key, value in self._next_step.items()
                               if key in live}
        return published

    def perceive(
        self,
        observer_position: Vector3,
        *,
        now: float,
        rng: random.Random,
        limit: int = 24,
        observer_id: int = -1,
        team: int | None = None,
    ) -> tuple[Stimulus, ...]:
        """Return nearby active events with non-zero positional uncertainty.

        With ``team`` given, the listener's own noise and its side's
        footsteps are left out: a squad on the move would otherwise fill the
        whole list with itself and push the enemy's shots off the end.
        """

        current = float(now)
        while self._events and self._events[0].expires_at <= current:
            self._events.popleft()
        perceived: list[tuple[float, Stimulus]] = []
        for event in self._events:
            if event.expires_at <= current:
                continue  # a short-lived step queued behind a longer blast
            if team is not None and (event.source_id == observer_id or (
                    event.kind is StimulusKind.FOOTSTEP and event.team == team)):
                continue
            dx = event.position[0] - observer_position[0]
            dy = event.position[1] - observer_position[1]
            dz = event.position[2] - observer_position[2]
            distance = math.sqrt(dx * dx + dy * dy + dz * dz)
            if distance > event.radius:
                continue
            uncertainty = max(0.75, distance * 0.045)
            angle = rng.uniform(-math.pi, math.pi)
            error = uncertainty * math.sqrt(rng.random())
            approximate = (
                event.position[0] + math.cos(angle) * error,
                event.position[1] + math.sin(angle) * error,
                event.position[2] + rng.uniform(-0.25, 0.25) * uncertainty,
            )
            perceived.append(
                (
                    distance,
                    Stimulus(
                        kind=event.kind,
                        position=approximate,
                        created_at=event.created_at,
                        expires_at=event.expires_at,
                        source_id=event.source_id,
                        team=event.team,
                        uncertainty=uncertainty,
                    ),
                )
            )
        perceived.sort(key=lambda item: item[0])
        return tuple(item[1] for item in perceived[: max(0, int(limit))])
