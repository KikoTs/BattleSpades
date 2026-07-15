"""Deterministic human-like profile and callsign generation."""

from __future__ import annotations

import random
from dataclasses import dataclass

import shared.constants as C

from .messages import BotProfile


_CALLSIGNS = (
    "Atlas", "Bishop", "Bolt", "Comet", "Echo", "Flint", "Ghost",
    "Harbor", "Ibis", "Juno", "Kestrel", "Mako", "Nova", "Orbit",
    "Pixel", "Quartz", "Rook", "Sable", "Tango", "Vex", "Warden",
)
_ADJECTIVES = (
    "Blue", "Brisk", "Calm", "Copper", "Dusty", "Lucky", "Quiet",
    "Red", "Silver", "Swift", "Wild",
)
_NOUNS = (
    "Badger", "Crow", "Fox", "Hawk", "Mole", "Otter", "Raven",
    "Wolf", "Yak",
)
_CLASS_PREFERENCES = tuple(
    int(value)
    for value in getattr(C, "DEFAULT_TEAM_CLASSES", ())
)


@dataclass(frozen=True, slots=True)
class _DifficultyBand:
    skill: tuple[float, float]
    reaction: tuple[float, float]
    tracking: tuple[float, float]
    turn_speed: tuple[float, float]
    turn_acceleration: tuple[float, float]
    aim_noise: tuple[float, float]


_BANDS = {
    # aim_noise bands calibrated 2026-07-15 against scripts/bot_aim_benchmark
    # (25 blocks, stationary): casual lands roughly a third of its shots,
    # normal most, hard nearly all.  Live target tracking settles noise by a
    # skill-weighted factor (director._apply_motor), so raw values here are
    # deliberately higher than the pre-tracking era.
    "casual": _DifficultyBand(
        (0.20, 0.45), (0.38, 0.65), (0.18, 0.32),
        (2.0, 3.2), (7.0, 11.0), (0.20, 0.28),
    ),
    "normal": _DifficultyBand(
        (0.45, 0.72), (0.22, 0.45), (0.09, 0.20),
        (3.0, 4.8), (10.0, 17.0), (0.055, 0.105),
    ),
    "hard": _DifficultyBand(
        (0.70, 0.90), (0.18, 0.30), (0.05, 0.13),
        (4.0, 5.8), (14.0, 22.0), (0.022, 0.055),
    ),
}


class ProfileFactory:
    """Create reproducible profiles while guaranteeing unique wire names."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(int(seed))
        self._used_names: set[str] = set()

    def release_name(self, name: str) -> None:
        """Allow a retired profile's name to be reused later."""

        self._used_names.discard(str(name))

    def create(self, difficulty: str = "mixed") -> BotProfile:
        """Return one profile constrained to the configured difficulty mix."""

        selected = self._choose_difficulty(difficulty)
        band = _BANDS[selected]
        skill = self._uniform(band.skill)
        return BotProfile(
            name=self._unique_name(),
            difficulty=selected,
            skill=skill,
            aggression=self._rng.uniform(0.25, 0.90),
            caution=self._rng.uniform(0.20, 0.90),
            teamwork=self._rng.uniform(0.30, 0.95),
            creativity=self._rng.uniform(0.15, 0.85),
            reaction_time=self._uniform(band.reaction),
            tracking_delay=self._uniform(band.tracking),
            turn_speed=self._uniform(band.turn_speed),
            turn_acceleration=self._uniform(band.turn_acceleration),
            recoil_control=0.30 + skill * 0.65,
            burst_discipline=self._rng.uniform(0.35, 0.90),
            preferred_range=self._rng.uniform(14.0, 36.0),
            aim_noise=self._uniform(band.aim_noise),
            class_preferences=tuple(
                self._rng.sample(
                    _CLASS_PREFERENCES,
                    k=min(2, len(_CLASS_PREFERENCES)),
                )
            ),
        )

    def _choose_difficulty(self, requested: str) -> str:
        normalized = str(requested).lower()
        if normalized in _BANDS:
            return normalized
        # Approved mixed roster: 20% casual, 60% normal, 20% hard.
        roll = self._rng.random()
        if roll < 0.20:
            return "casual"
        if roll < 0.80:
            return "normal"
        return "hard"

    def _uniform(self, bounds: tuple[float, float]) -> float:
        return self._rng.uniform(bounds[0], bounds[1])

    def _unique_name(self) -> str:
        """Generate an ASCII name fitting the retail 3..15 byte field."""

        for _ in range(512):
            style = self._rng.randrange(4)
            if style == 0:
                candidate = self._rng.choice(_CALLSIGNS)
            elif style == 1:
                candidate = f"{self._rng.choice(_CALLSIGNS)}{self._rng.randrange(10, 100)}"
            elif style == 2:
                candidate = f"{self._rng.choice(_ADJECTIVES)}{self._rng.choice(_NOUNS)}"
            else:
                candidate = f"{self._rng.choice(_NOUNS)}{self._rng.randrange(2, 90)}"
            candidate = candidate[:15]
            if 3 <= len(candidate) <= 15 and candidate not in self._used_names:
                self._used_names.add(candidate)
                return candidate
        # Deterministic bounded fallback if a tiny catalog is exhausted.
        index = len(self._used_names)
        candidate = f"Bot{index:04d}"[:15]
        self._used_names.add(candidate)
        return candidate
