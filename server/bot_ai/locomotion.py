"""Bounded per-encounter footwork; the native motor still validates movement."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .messages import BotProfile, PlayerSnapshot, Vector3


@dataclass(slots=True)
class CombatFootwork:
    """Commit to short strides, then settle for a shot instead of orbiting.

    Decisions depend on actual displacement, weapon spacing and received
    pressure. Identity-stable variation changes each stride without consuming
    global randomness or changing direction on every perception frame.
    """

    target: tuple[int, int, int] | None = None
    phase: str = ""
    until: float = 0.0
    started_at: float = 0.0
    origin: Vector3 = (0.0, 0.0, 0.0)
    heading: Vector3 = (0.0, 0.0, 0.0)
    stride_length: float = 0.0
    sequence: int = 0
    side: float = 1.0
    range_mode: str = "hold"
    last_pressure_at: float = 0.0
    was_reloading: bool = False

    def spacing(self, distance: float, ideal_min: float, ideal_max: float) -> str:
        """Use separate entry/exit distances so a range boundary cannot flap."""
        retreat_at = max(2.0, ideal_min * 0.65)
        if self.range_mode == "retreat" and distance < max(3.0, ideal_min * 0.95):
            return "retreat"
        if distance < retreat_at:
            self.range_mode = "retreat"
        elif self.range_mode == "approach" and distance > ideal_max * 0.88:
            return "approach"
        elif distance > ideal_max:
            self.range_mode = "approach"
        else:
            self.range_mode = "hold"
        return self.range_mode

    def direction(
        self, observer: PlayerSnapshot, target: PlayerSnapshot,
        profile: BotProfile, now: float, *, blocked_stage: int = 0,
    ) -> Vector3:
        """Return a committed world-space stride or an intentional firing pause."""
        identity = (target.player_id, target.generation, target.life_id)
        dx = target.position[0] - observer.position[0]
        dy = target.position[1] - observer.position[1]
        distance = math.hypot(dx, dy)
        if distance < 1e-6:
            return (0.0, 0.0, 0.0)
        if self.target != identity:
            self.target = identity
            self.phase = ""
            self.sequence = 0
            self.side = -1.0 if observer.player_id & 1 else 1.0
        if observer.reloading and not self.was_reloading:
            self.phase = ""
        self.was_reloading = observer.reloading
        if blocked_stage:
            # The worker's measured collision recovery owns its opposite,
            # forward and backward probes. A new stride must not cancel them.
            self.phase = "recovery"
            return (-dy * self.side / distance, dx * self.side / distance, 0.0)

        new_pressure = (
            observer.last_damage_at > self.last_pressure_at
            and 0.0 <= now - observer.last_damage_at <= 0.8
            and now - self.started_at >= 0.6
        )
        if new_pressure:
            self.last_pressure_at = observer.last_damage_at
            self.phase = ""
        moved = math.dist(observer.position[:2], self.origin[:2])
        if self.phase == "stride" and (now >= self.until or moved >= self.stride_length):
            self.phase = "settle"
            # Disciplined/cautious bots plant their feet longer, while recent
            # damage and reloading favor repositioning over a long exposure.
            variation = self._variation(observer, profile)
            duration = 0.22 + 0.5 * profile.burst_discipline + 0.25 * variation
            if observer.reloading or now - observer.last_damage_at < 1.2:
                duration *= 0.45
            self.until = now + duration
            return (0.0, 0.0, 0.0)
        if self.phase == "settle" and now < self.until:
            return (0.0, 0.0, 0.0)
        if self.phase != "stride":
            self.sequence += 1
            variation = self._variation(observer, profile)
            # Changes of side occur at a decision boundary, not on a global
            # alternating timer shared by the entire roster.
            if self.sequence > 1 and variation < 0.3 + 0.2 * profile.caution:
                self.side = -self.side
            desired_range = max(3.0, profile.preferred_range)
            radial = max(-0.25, min(0.25, (distance - desired_range) / desired_range))
            if observer.reloading or new_pressure or observer.health < 45:
                radial = -0.45 - 0.2 * profile.caution
            ux, uy = dx / distance, dy / distance
            hx, hy = -uy * self.side + ux * radial, ux * self.side + uy * radial
            length = math.hypot(hx, hy)
            self.heading = (hx / length, hy / length, 0.0)
            self.origin = observer.position
            self.stride_length = 2.0 + 2.4 * profile.aggression + 1.4 * variation
            self.started_at = now
            self.until = now + 0.8 + 0.6 * profile.caution + 0.5 * variation
            self.phase = "stride"
        return self.heading

    def _variation(self, observer: PlayerSnapshot, profile: BotProfile) -> float:
        # Fixed integer mixing is repeatable across processes/Python versions.
        value = ((observer.player_id + 1) * 73856093
                 ^ (self.target[0] + 1 if self.target else 1) * 19349663
                 ^ self.sequence * 83492791
                 ^ int(profile.creativity * 65535)) & 0xFFFFFFFF
        value ^= value >> 13
        return (value & 0xFFFF) / 65535.0
