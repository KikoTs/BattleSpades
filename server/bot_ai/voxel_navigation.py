"""Local voxel semantics and action planning for off-thread bot navigation.

This module owns the coordinate rules that distinguish a body-clear land
surface from the universal waterbed.  It operates only on a supplied immutable
or worker-owned solid query; it never mutates gameplay state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import math

import shared.constants as C

from .messages import MovementAffordance, Vector3, VoxelCoordinate


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


@dataclass(frozen=True, slots=True)
class VoxelActionStep:
    """Immediate local action plus the stable goal that selected it."""

    direction: Vector3
    waypoint: Vector3
    goal: Vector3
    affordance: MovementAffordance
    cells: tuple[VoxelCoordinate, ...] = ()


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


class VoxelActionPlanner:
    """Bounded local search for survival and topology-changing actions."""

    def __init__(self, terrain: VoxelTerrain) -> None:
        self.terrain = terrain

    def water_exit(
        self,
        position: Vector3,
        *,
        search_radius: int = 24,
    ) -> VoxelActionStep | None:
        """Return the first recovery step toward the nearest dry surface.

        Water nodes are legal only inside this recovery search.  The search is
        bounded to a square radius and accepts at most a two-voxel shore climb,
        matching the base jump affordance.  A blocked or taller shore returns
        no step so the worker can escalate to breach or construction.
        """

        start = self.terrain.classify(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            allow_water=True,
            vertical_span=4,
        )
        if start is None or not start.water:
            return None

        start_key = start.x, start.y
        frontier: deque[tuple[int, int]] = deque((start_key,))
        came_from: dict[tuple[int, int], tuple[int, int] | None] = {
            start_key: None
        }
        samples: dict[tuple[int, int], SurfaceSample] = {start_key: start}
        radius = max(1, min(64, int(search_radius)))

        while frontier:
            current_key = frontier.popleft()
            current = samples[current_key]
            if not current.water:
                first_key = current_key
                while came_from[first_key] not in (None, start_key):
                    previous = came_from[first_key]
                    if previous is None:
                        break
                    first_key = previous
                first = samples[first_key]
                dx = float(first.x - start.x)
                dy = float(first.y - start.y)
                length = math.hypot(dx, dy)
                if length <= 1e-6:
                    return None
                return VoxelActionStep(
                    direction=(dx / length, dy / length, 0.0),
                    waypoint=first.player_position,
                    goal=current.player_position,
                    affordance=MovementAffordance.JUMP,
                    cells=(
                        (first.x, first.y, first.support_z),
                        (current.x, current.y, current.support_z),
                    ),
                )

            for offset_x, offset_y in (
                (1, 0),
                (-1, 0),
                (0, 1),
                (0, -1),
            ):
                neighbor_key = (
                    current.x + offset_x,
                    current.y + offset_y,
                )
                if neighbor_key in came_from:
                    continue
                if (
                    abs(neighbor_key[0] - start.x) > radius
                    or abs(neighbor_key[1] - start.y) > radius
                ):
                    continue
                neighbor = self.terrain.classify(
                    neighbor_key[0],
                    neighbor_key[1],
                    current.player_position[2],
                    allow_water=True,
                    vertical_span=3,
                )
                if neighbor is None:
                    continue
                if abs(neighbor.support_z - current.support_z) > 2:
                    continue
                came_from[neighbor_key] = current_key
                samples[neighbor_key] = neighbor
                frontier.append(neighbor_key)
        return None
