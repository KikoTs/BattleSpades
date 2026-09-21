"""Small sensory and outcome memories; no authoritative world access."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math

from .messages import PerceptionFrame, PlayerSnapshot, StimulusKind, Vector3


@dataclass(slots=True)
class Contact:
    position: Vector3
    expires_at: float
    confidence: float
    uncertainty: float


@dataclass(slots=True)
class BehaviorMemory:
    """Per-life memory with fixed capacity and temporary site aversion."""

    contacts: dict[tuple[int, int, str], Contact] = field(default_factory=dict)
    outcomes: deque[tuple[str, Vector3, float, bool]] = field(
        default_factory=lambda: deque(maxlen=16)
    )
    pressure: float = 0.0
    updated_at: float = 0.0

    def observe(self, frame: PerceptionFrame, player: PlayerSnapshot,
                visible: PlayerSnapshot | None) -> None:
        now = float(frame.created_at)
        self.pressure *= math.exp(-max(0.0, now - self.updated_at) / 4.0)
        self.updated_at = now
        self.contacts = {key: value for key, value in self.contacts.items()
                         if value.expires_at > now}
        for event in frame.stimuli[:24]:
            if (event.expires_at <= now or event.created_at > now
                    or event.team == player.team
                    or math.dist(player.position, event.position) > 80.0
                    or event.kind not in {StimulusKind.SHOT, StimulusKind.EXPLOSION,
                                          StimulusKind.BLOCK_DESTROYED,
                                          StimulusKind.TEAM_SIGHTING}):
                continue
            key = (int(event.position[0] // 8), int(event.position[1] // 8),
                   event.kind.value)
            self.contacts[key] = Contact(event.position,
                min(now + 6.0, event.expires_at), 0.45, max(2.0, event.uncertainty))
        if visible is not None:
            key = (visible.player_id, visible.generation, "seen")
            self.contacts[key] = Contact(visible.position, now + 6.0, 1.0, 0.75)
        if 0.0 <= now - player.last_damage_at < 1.0 and player.last_damage_at > 0:
            self.pressure = min(1.0, self.pressure + 0.25)
        while len(self.contacts) > 24:
            self.contacts.pop(next(iter(self.contacts)))

    def contact(self, position: Vector3, now: float) -> Contact | None:
        """Choose remembered evidence, without updating it from hidden actors."""
        return max((c for c in self.contacts.values() if c.expires_at > now),
                   key=lambda c: c.confidence / (1.0 + math.dist(position, c.position) / 30),
                   default=None)

    def penalty(self, kind: str, position: Vector3, now: float) -> float:
        return min(1.0, sum((0.3 if success else 0.7)
                           for previous, site, at, success in self.outcomes
                           if previous == kind and now - at < (20 if success else 45)
                           and math.dist(site, position) < 10))

    def record(self, kind: str, position: Vector3, now: float, success: bool) -> None:
        self.outcomes.append((kind, position, now, success))
