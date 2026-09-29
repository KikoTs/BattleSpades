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
from server.game_constants import WEAPON_CATALOG


DIG_SINGLE = "single"
DIG_COLUMN = "column"
DIG_CUBE = "cube"
DIG_MACHETE = "machete_vertical_pair"

VoxelCoordinate = tuple[int, int, int]

# Undamaged health of an authored map voxel and of a player-built voxel
# (BlockBuild/BlockLine/prefab cells are ``add_user_block``-ed with
# DEFAULT_PREFAB_HEALTH on the retail client and in CombatRuntime).
MAP_BLOCK_HEALTH = float(getattr(C, "DEFAULT_BLOCK_HEALTH", 5.0))
BUILT_BLOCK_HEALTH = float(getattr(C, "DEFAULT_PREFAB_HEALTH", 9.0))


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
    def random_extra(self) -> float:
        """Per-cell random bonus of a cube footprint (``amount + extra*r``)."""

        from server.block_damage_model import CUBE, footprint_kind

        entry = footprint_kind(self.damage_type)
        if entry is None or entry[0] != CUBE or self.pattern != DIG_CUBE:
            return 0.0
        return max(0.0, float(entry[1]))

    def swings_for_health(self, health: float, *, worst_case: bool = False) -> int:
        """Hits this tool needs to break one cell of ``health``.

        Every footprint cell accumulates damage until it reaches its health
        (server/block_damage_model.py): map voxels have 5, player-built ones
        9, so a spade clears a map column in one swing but a built wall in
        two, and zombie hands (2 + up to 8 random per cell) average two hits
        on a built block and may need five. ``worst_case`` ignores the random
        bonus; the default uses its mean, the planning estimate.
        """

        if self.block_damage <= 0.0:
            return 0
        per_swing = float(self.block_damage)
        if not worst_case:
            per_swing += 0.5 * self.random_extra
        return max(1, int(math.ceil(float(health) / per_swing - 1e-9)))

    @property
    def swings_per_block(self) -> int:
        """Return expected hits needed on an undamaged authored map voxel."""

        return self.swings_for_health(MAP_BLOCK_HEALTH)


def _value(name: str, default: int | float) -> int | float:
    return getattr(C, name, default)


def _profile(
    tool_name: str,
    tool_default: int,
    damage_name: str,
    damage_default: int,
    pattern: str,
) -> DigProfile:
    """Build one dig profile from the stock weapon catalog.

    Block damage and swing cadence come from ``WEAPON_CATALOG`` (the stock
    Steam ``DiggingTool`` class values, docs/WEAPONS_RETAIL.md) -- the SAME
    row ``Player.consume_shot`` gates melee with. Reading them from
    ``shared.constants`` picked up the nonsteam decompile's later modded
    block (spade 0.4 s, pickaxe 0.4 s, knife 0.25 s, crowbar 0.6 s), so bots
    planned swings faster than the server accepts.
    """

    tool_id = int(_value(tool_name, tool_default))
    stock = WEAPON_CATALOG[tool_id]
    return DigProfile(
        tool_id=tool_id,
        damage_type=int(_value(damage_name, damage_default)),
        block_damage=float(stock.block_damage),
        pattern=str(pattern),
        fire_interval=float(stock.fire_interval),
    )


PRIMARY_DIG_PROFILES = {
    profile.tool_id: profile
    for profile in (
        _profile("SPADE_TOOL", 2, "SPADE_DAMAGE", 2, DIG_COLUMN),
        _profile("CLASSIC_SPADE_TOOL", 4, "SPADE_DAMAGE", 2, DIG_COLUMN),
        _profile("SUPERSPADE_TOOL", 3, "SUPERSPADE_DAMAGE", 3, DIG_CUBE),
        _profile("ZOMBIEHAND_TOOL", 24, "ZOMBIE_DAMAGE", 17, DIG_CUBE),
        _profile("PICKAXE_TOOL", 0, "PICKAXE_DAMAGE", 0, DIG_SINGLE),
        _profile("KNIFE_TOOL", 1, "KNIFE_DAMAGE", 1, DIG_SINGLE),
        _profile("CROWBAR_TOOL", 34, "CROWBAR_DAMAGE", 26, DIG_SINGLE),
        _profile("MACHETE_TOOL", 50, "MACHETE_DAMAGE", 35, DIG_MACHETE),
        # A Medic who chose the riot stick still needs terrain access.
        _profile("RIOTSTICK_TOOL", 49, "RIOTSTICK_DAMAGE", 34, DIG_SINGLE),
        # Stock RiotShieldTool ``block_damage`` is 2 (A1882). The client
        # expands RIOTSHIELD_DAMAGE (36) as a z-1..z+1 column (fitted live,
        # server/block_damage_model.py), so the server footprint matches.
        _profile("RIOTSHIELD_TOOL", 52, "RIOTSHIELD_DAMAGE", 36, DIG_COLUMN),
        _profile("UGC_PICKAXE_TOOL", 44, "UGC_PICKAXE_DAMAGE", 28, DIG_SINGLE),
        _profile(
            "UGC_SUPERSPADE_TOOL", 45, "UGC_SUPERSPADE_DAMAGE", 29, DIG_SINGLE,
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
    "BUILT_BLOCK_HEALTH",
    "MAP_BLOCK_HEALTH",
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
