"""Local voxel semantics and action planning for off-thread bot navigation.

This module owns the coordinate rules that distinguish a body-clear land
surface from the universal waterbed.  It operates only on a supplied immutable
or worker-owned solid query; it never mutates gameplay state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import shared.constants as C


SolidQuery = Callable[[int, int, int], bool]

MAP_X = 512
MAP_Y = 512
MAP_Z = 240
PLAYER_STANDING_OFFSET = 2.25
WATERBED_SUPPORT_Z = int(C.Z_ABOVE_WATERPLANE) + 1


@dataclass(frozen=True, slots=True)
class SurfaceSample:
    """One body-clear supporting surface in AoS z-down coordinates."""

    x: int
    y: int
    support_z: int
    water: bool
    head_clear: bool

    @property
    def player_position(self) -> tuple[float, float, float]:
        """Return the centered standing player anchor for this surface."""

        return (
            float(self.x) + 0.5,
            float(self.y) + 0.5,
            float(self.support_z) - PLAYER_STANDING_OFFSET,
        )


class VoxelTerrain:
    """Classify bounded local surfaces through a fail-closed solid query."""

    def __init__(self, solid: SolidQuery) -> None:
        self._solid = solid

    def classify(
        self,
        x: int,
        y: int,
        player_z: float,
        *,
        allow_water: bool = False,
        vertical_span: int = 3,
        clearance: int = 2,
    ) -> SurfaceSample | None:
        """Return the nearest valid support around ``player_z``.

        Ordinary traversal rejects the forced waterbed at z=239.  Recovery
        code must opt into that surface explicitly with ``allow_water=True``.
        Invalid bounds and failed collision reads reveal no traversable space.
        """

        x, y = int(x), int(y)
        if not (0 <= x < MAP_X and 0 <= y < MAP_Y):
            return None
        expected_support = int(round(float(player_z) + PLAYER_STANDING_OFFSET))
        candidates = range(
            max(clearance, expected_support - max(0, int(vertical_span))),
            min(MAP_Z, expected_support + max(0, int(vertical_span)) + 1),
        )
        try:
            for support_z in sorted(
                candidates, key=lambda value: abs(value - expected_support)
            ):
                water = support_z >= WATERBED_SUPPORT_Z
                if water and not allow_water:
                    continue
                if not self._solid(x, y, support_z):
                    continue
                head_clear = all(
                    not self._solid(x, y, support_z - offset)
                    for offset in range(1, clearance + 1)
                )
                if head_clear:
                    return SurfaceSample(x, y, support_z, water, True)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return None
        return None

    def standing_node(
        self,
        x: int,
        y: int,
        player_z: float,
        *,
        allow_water: bool = False,
        vertical_span: int = 3,
        clearance: int = 2,
    ) -> tuple[int, int, int] | None:
        """Return the planner node for a body-clear supporting surface."""

        sample = self.classify(
            x,
            y,
            player_z,
            allow_water=allow_water,
            vertical_span=vertical_span,
            clearance=clearance,
        )
        if sample is None:
            return None
        return sample.x, sample.y, sample.support_z
