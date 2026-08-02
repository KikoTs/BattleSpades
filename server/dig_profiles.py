"""Retail melee terrain footprints shared by combat and bot navigation.

The gameplay authority and worker must agree about what one swing removes.
Keeping the recovered damage, footprint, cadence, and secondary-fire choice
here prevents path costs from drifting away from actual block damage.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Iterable

import shared.constants as C


DIG_SINGLE = "single"
DIG_COLUMN = "column"
DIG_CUBE = "cube"
DIG_MACHETE = "machete_vertical_pair"

VoxelCoordinate = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class DigProfile:
    """One exact melee-to-terrain action available to a player."""

    tool_id: int
    damage_type: int
    block_damage: float
    pattern: str
    fire_interval: float
    secondary: bool = False

    @property
    def swings_per_block(self) -> int:
        """Return authoritative hits needed by an accumulating footprint."""

        if self.pattern in {DIG_COLUMN, DIG_CUBE}:
            # Retail area packets remove the full footprint in one operation;
            # block_damage controls matching client feedback only.
            return 1
        health = float(getattr(C, "DEFAULT_BLOCK_HEALTH", 5.0))
        if self.block_damage <= 0.0:
            return 0
        return max(1, int(math.ceil(health / self.block_damage)))


def _value(name: str, default: int | float) -> int | float:
    return getattr(C, name, default)


def _profile(
    tool_name: str,
    tool_default: int,
    damage_name: str,
    damage_default: int,
    block_damage: float,
    pattern: str,
    interval_name: str,
    interval_default: float,
) -> DigProfile:
    return DigProfile(
        tool_id=int(_value(tool_name, tool_default)),
        damage_type=int(_value(damage_name, damage_default)),
        block_damage=float(block_damage),
        pattern=str(pattern),
        fire_interval=float(_value(interval_name, interval_default)),
    )


PRIMARY_DIG_PROFILES = {
    profile.tool_id: profile
    for profile in (
        _profile(
            "SPADE_TOOL", 2, "SPADE_DAMAGE", 2, 5.0, DIG_COLUMN,
            "SPADE_SHOOT_INTERVAL", 0.4,
        ),
        _profile(
            "CLASSIC_SPADE_TOOL", 4, "SPADE_DAMAGE", 2, 3.0,
            DIG_COLUMN, "CLASSIC_SPADE_SHOOT_INTERVAL", 0.3,
        ),
        _profile(
            "SUPERSPADE_TOOL", 3, "SUPERSPADE_DAMAGE", 3, 7.5,
            DIG_CUBE, "SUPERSPADE_SHOOT_INTERVAL", 0.6,
        ),
        _profile(
            "ZOMBIEHAND_TOOL", 24, "ZOMBIE_DAMAGE", 17,
            float(_value("ZOMBIEHAND_DAMAGE_AMOUNT", 2.0)), DIG_CUBE,
            "ZOMBIEHAND_SHOOT_INTERVAL", 0.4,
        ),
        _profile(
            "PICKAXE_TOOL", 0, "PICKAXE_DAMAGE", 0, 9.0, DIG_SINGLE,
            "PICKAXE_SHOOT_INTERVAL", 0.4,
        ),
        _profile(
            "KNIFE_TOOL", 1, "KNIFE_DAMAGE", 1, 1.0, DIG_SINGLE,
            "KNIFE_SHOOT_INTERVAL", 0.25,
        ),
        _profile(
            "CROWBAR_TOOL", 34, "CROWBAR_DAMAGE", 26, 5.0, DIG_SINGLE,
            "CROWBAR_SHOOT_INTERVAL", 0.6,
        ),
        _profile(
            "MACHETE_TOOL", 50, "MACHETE_DAMAGE", 35, 2.0, DIG_MACHETE,
            "MACHETE_SHOOT_INTERVAL", 0.7,
        ),
        _profile(
            "UGC_PICKAXE_TOOL", 44, "UGC_PICKAXE_DAMAGE", 28, 9.0,
            DIG_SINGLE, "UGC_PICKAXE_SHOOT_INTERVAL", 0.2,
        ),
        _profile(
            "UGC_SUPERSPADE_TOOL", 45, "UGC_SUPERSPADE_DAMAGE", 29,
            7.5, DIG_SINGLE, "UGC_SUPERSPADE_SHOOT_INTERVAL", 0.2,
        ),
    )
}

# Tuple compatibility used by CombatSystem and the reversed-behavior tests.
MELEE_DIG_PROFILES = {
    tool_id: (profile.damage_type, profile.block_damage, profile.pattern)
    for tool_id, profile in PRIMARY_DIG_PROFILES.items()
}
DEFAULT_MELEE_PROFILE = (
    int(_value("SPADE_DAMAGE", 2)),
    5.0,
    DIG_COLUMN,
)


def melee_dig_positions(
    block_pos: VoxelCoordinate,
    pattern: str,
) -> list[VoxelCoordinate]:
    """Return the exact retail voxel footprint centered on ``block_pos``."""

    x, y, z = (int(value) for value in block_pos)
    if pattern == DIG_CUBE:
        return [
            (x + dx, y + dy, z + dz)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
        ]
    if pattern == DIG_COLUMN:
        return [(x, y, z - 1), (x, y, z), (x, y, z + 1)]
    if pattern == DIG_MACHETE:
        return [(x, y, z), (x, y, z + 1)]
    return [(x, y, z)]


def navigation_dig_profile(tool_id: int) -> DigProfile | None:
    """Return the most efficient safe terrain action for one owned tool."""

    profile = PRIMARY_DIG_PROFILES.get(int(tool_id))
    if profile is None or profile.block_damage <= 0.0:
        return None
    if int(tool_id) == int(_value("UGC_SUPERSPADE_TOOL", 45)):
        # Retail UGC Super Spade RMB is the recovered 3x3x3 terrain action.
        return replace(
            profile,
            damage_type=int(_value("UGC_SUPERSPADE_SECONDARY_DAMAGE", 31)),
            block_damage=float(
                _value("UGC_SUPERSPADE_SECONDARY_DAMAGE_AMOUNT", 7.5)
            ),
            pattern=DIG_CUBE,
            secondary=True,
        )
    return profile


def best_navigation_dig_profile(
    tool_ids: Iterable[int],
) -> DigProfile | None:
    """Choose the owned digger with the best body-clearance throughput."""

    profiles = tuple(
        profile
        for tool_id in tool_ids
        if (profile := navigation_dig_profile(int(tool_id))) is not None
    )
    if not profiles:
        return None

    def score(profile: DigProfile) -> tuple[float, int, float, int]:
        vertical_yield = 2 if profile.pattern != DIG_SINGLE else 1
        work = max(
            0.05,
            float(profile.fire_interval) * float(profile.swings_per_block),
        )
        area_bonus = 1 if profile.pattern == DIG_CUBE else 0
        return vertical_yield / work, area_bonus, profile.block_damage, -profile.tool_id

    return max(profiles, key=score)


__all__ = [
    "DEFAULT_MELEE_PROFILE",
    "DIG_COLUMN",
    "DIG_CUBE",
    "DIG_MACHETE",
    "DIG_SINGLE",
    "DigProfile",
    "MELEE_DIG_PROFILES",
    "PRIMARY_DIG_PROFILES",
    "best_navigation_dig_profile",
    "melee_dig_positions",
    "navigation_dig_profile",
]
