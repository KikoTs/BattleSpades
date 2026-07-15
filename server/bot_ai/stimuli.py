"""Bounded fair sensory-event feed for the isolated bot worker."""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass

from .messages import Stimulus, StimulusKind, Vector3


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

    def perceive(
        self,
        observer_position: Vector3,
        *,
        now: float,
        rng: random.Random,
        limit: int = 24,
    ) -> tuple[Stimulus, ...]:
        """Return nearby active events with non-zero positional uncertainty."""

        current = float(now)
        while self._events and self._events[0].expires_at <= current:
            self._events.popleft()
        perceived: list[tuple[float, Stimulus]] = []
        for event in self._events:
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

