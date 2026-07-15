"""Per-weapon engagement envelopes derived from the authoritative catalog.

Pure data importable by both the gameplay thread and the worker process.
Ranges are in blocks; categories come from server.game_constants so a new
catalog weapon automatically inherits its category's fighting doctrine.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.game_constants import (
    CAT_MG,
    CAT_PISTOL,
    CAT_RIFLE,
    CAT_SHOTGUN,
    CAT_SMG,
    CAT_SNIPER,
    WEAPON_PROFILES,
)


@dataclass(frozen=True, slots=True)
class EngagementEnvelope:
    """How a weapon wants to be fought with."""

    ideal_min: float
    ideal_max: float
    hard_max: float
    prefers_stationary: bool
    burst_shots: tuple[int, int]
    burst_pause: tuple[float, float]


_BY_CATEGORY: dict[str, EngagementEnvelope] = {
    CAT_SNIPER: EngagementEnvelope(40.0, 120.0, 160.0, True, (1, 1), (0.9, 1.8)),
    CAT_RIFLE: EngagementEnvelope(25.0, 70.0, 120.0, False, (2, 4), (0.5, 1.1)),
    CAT_SMG: EngagementEnvelope(8.0, 30.0, 60.0, False, (4, 8), (0.35, 0.8)),
    CAT_SHOTGUN: EngagementEnvelope(4.0, 14.0, 30.0, False, (1, 2), (0.4, 0.9)),
    CAT_MG: EngagementEnvelope(15.0, 50.0, 90.0, True, (6, 12), (0.6, 1.2)),
    CAT_PISTOL: EngagementEnvelope(6.0, 25.0, 50.0, False, (2, 3), (0.4, 0.9)),
}
_DEFAULT = EngagementEnvelope(10.0, 35.0, 70.0, False, (2, 5), (0.4, 1.0))

# Muzzle climb per accepted shot, radians, before recoil_control mitigation.
# AoS z increases downward, so the director applies these as negative pitch.
RECOIL_KICKS: dict[str, float] = {
    CAT_SMG: 0.030,
    CAT_RIFLE: 0.055,
    CAT_SHOTGUN: 0.075,
    CAT_MG: 0.040,
    CAT_SNIPER: 0.090,
    CAT_PISTOL: 0.045,
}


def envelope_for(tool_id: int) -> EngagementEnvelope:
    """Return the engagement envelope for one catalog tool."""

    profile = WEAPON_PROFILES.get(int(tool_id))
    if profile is None:
        return _DEFAULT
    return _BY_CATEGORY.get(profile.category, _DEFAULT)


def recoil_kick_for(tool_id: int) -> float:
    """Return the per-shot muzzle climb for one catalog tool."""

    profile = WEAPON_PROFILES.get(int(tool_id))
    if profile is None:
        return 0.0
    return RECOIL_KICKS.get(profile.category, 0.0)
