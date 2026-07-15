"""Local voxel semantics and action planning for off-thread bot navigation.

This module owns the coordinate rules that distinguish a body-clear land
surface from the universal waterbed.  It operates only on a supplied immutable
or worker-owned solid query; it never mutates gameplay state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import heapq
import math
from typing import Iterable

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
    reached_goal: bool = False
    topology_version: int = -1


class VoxelTerrain:
    """Classify bounded local surfaces through a fail-closed solid query."""

    def __init__(self, solid: SolidQuery) -> None:
        self._solid = solid

    def solid(self, x: int, y: int, z: int) -> bool:
        """Return occupancy and fail closed for bounds or query errors."""

        if not (0 <= int(x) < MAP_X and 0 <= int(y) < MAP_Y and 0 <= int(z) < MAP_Z):
            return True
        try:
            return bool(self._solid(int(x), int(y), int(z)))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return True

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
        for support_z in sorted(
            candidates, key=lambda value: abs(value - expected_support)
        ):
            water = support_z >= WATERBED_SUPPORT_Z
            if water and not allow_water:
                continue
            if not self.solid(x, y, support_z):
                continue
            head_clear = all(
                not self.solid(x, y, support_z - offset)
                for offset in range(1, clearance + 1)
            )
            if head_clear:
                return SurfaceSample(x, y, support_z, water, True)
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

    def plan_local(
        self,
        start_position: Vector3,
        goal_position: Vector3,
        *,
        abilities: frozenset[MovementAffordance],
        topology_version: int,
        search_radius: int = 24,
        max_expansions: int = 4096,
    ) -> VoxelActionStep | None:
        """Plan the first timed movement action through nearby voxel floors."""

        start = self.terrain.classify(
            int(math.floor(start_position[0])),
            int(math.floor(start_position[1])),
            float(start_position[2]),
            vertical_span=4,
        )
        if start is None:
            return None
        goal_x = int(math.floor(goal_position[0]))
        goal_y = int(math.floor(goal_position[1]))
        goal_support = int(round(float(goal_position[2]) + PLAYER_STANDING_OFFSET))
        radius = max(1, min(64, int(search_radius)))
        expansion_limit = max(16, min(16384, int(max_expansions)))
        start_key = start.x, start.y, start.support_z
        frontier: list[tuple[float, int, tuple[int, int, int]]] = []
        sequence = 0
        heapq.heappush(frontier, (0.0, sequence, start_key))
        came_from: dict[
            tuple[int, int, int], tuple[int, int, int] | None
        ] = {start_key: None}
        came_affordance: dict[tuple[int, int, int], MovementAffordance] = {
            start_key: MovementAffordance.WALK
        }
        costs = {start_key: 0.0}
        samples = {start_key: start}
        best = start_key
        best_score = self._goal_score(start_key, goal_x, goal_y, goal_support)
        reached = False

        while frontier and len(came_from) <= expansion_limit:
            _priority, _sequence, current_key = heapq.heappop(frontier)
            current = samples[current_key]
            score = self._goal_score(current_key, goal_x, goal_y, goal_support)
            if score < best_score:
                best, best_score = current_key, score
            if (
                current.x == goal_x
                and current.y == goal_y
                and abs(current.support_z - goal_support) <= 1
            ):
                best = current_key
                reached = True
                break
            for neighbor, affordance, edge_cost in self._local_neighbors(
                current,
                abilities=abilities,
            ):
                if (
                    abs(neighbor.x - start.x) > radius
                    or abs(neighbor.y - start.y) > radius
                ):
                    continue
                neighbor_key = neighbor.x, neighbor.y, neighbor.support_z
                new_cost = costs[current_key] + edge_cost
                if new_cost >= costs.get(neighbor_key, math.inf):
                    continue
                costs[neighbor_key] = new_cost
                samples[neighbor_key] = neighbor
                came_from[neighbor_key] = current_key
                came_affordance[neighbor_key] = affordance
                sequence += 1
                heuristic = self._goal_score(
                    neighbor_key, goal_x, goal_y, goal_support
                )
                heapq.heappush(
                    frontier,
                    (new_cost + heuristic, sequence, neighbor_key),
                )

        if best == start_key:
            return None
        path: list[tuple[int, int, int]] = []
        cursor: tuple[int, int, int] | None = best
        while cursor is not None:
            path.append(cursor)
            cursor = came_from[cursor]
        path.reverse()
        if len(path) < 2:
            return None
        first_key = path[1]
        first = samples[first_key]
        dx = float(first.x - start.x)
        dy = float(first.y - start.y)
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return None
        return VoxelActionStep(
            direction=(dx / length, dy / length, 0.0),
            waypoint=first.player_position,
            goal=goal_position,
            affordance=came_affordance[first_key],
            cells=tuple(path[: min(4, len(path))]),
            reached_goal=reached,
            topology_version=int(topology_version),
        )

    @staticmethod
    def _goal_score(
        node: tuple[int, int, int],
        goal_x: int,
        goal_y: int,
        goal_support: int,
    ) -> float:
        """Estimate remaining travel while retaining vertical awareness."""

        return math.hypot(node[0] - goal_x, node[1] - goal_y) + (
            abs(node[2] - goal_support) * 0.35
        )

    def _local_neighbors(
        self,
        current: SurfaceSample,
        *,
        abilities: frozenset[MovementAffordance],
    ) -> Iterable[tuple[SurfaceSample, MovementAffordance, float]]:
        """Yield walk, jump, drop, and one-cell-gap actions from a surface."""

        for offset_x, offset_y in (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        ):
            neighbor = self.terrain.classify(
                current.x + offset_x,
                current.y + offset_y,
                current.player_position[2],
                vertical_span=8,
            )
            if neighbor is not None:
                delta_z = neighbor.support_z - current.support_z
                if abs(delta_z) <= 1:
                    yield (
                        neighbor,
                        MovementAffordance.WALK,
                        1.0 + abs(delta_z) * 0.25,
                    )
                    continue
                if (
                    delta_z < 0
                    and -delta_z <= 2
                    and MovementAffordance.JUMP in abilities
                    and self._jump_arc_clear(current, neighbor)
                ):
                    yield (
                        neighbor,
                        MovementAffordance.JUMP,
                        1.8 + -delta_z * 0.35,
                    )
                    continue
                if (
                    1 < delta_z <= 4
                    and MovementAffordance.DROP in abilities
                ):
                    yield (
                        neighbor,
                        MovementAffordance.DROP,
                        1.25 + delta_z * 0.2,
                    )
                continue

            if MovementAffordance.JUMP not in abilities:
                continue
            landing = self.terrain.classify(
                current.x + offset_x * 2,
                current.y + offset_y * 2,
                current.player_position[2],
                vertical_span=4,
            )
            if (
                landing is not None
                and abs(landing.support_z - current.support_z) <= 2
                and self._jump_arc_clear(current, landing)
            ):
                yield landing, MovementAffordance.JUMP, 2.6

    def _jump_arc_clear(
        self,
        start: SurfaceSample,
        landing: SurfaceSample,
    ) -> bool:
        """Validate a conservative standing-body arc between two surfaces."""

        start_position = start.player_position
        landing_position = landing.player_position
        for index in range(1, 8):
            fraction = index / 8.0
            x = start_position[0] + (
                landing_position[0] - start_position[0]
            ) * fraction
            y = start_position[1] + (
                landing_position[1] - start_position[1]
            ) * fraction
            linear_z = start_position[2] + (
                landing_position[2] - start_position[2]
            ) * fraction
            player_z = linear_z - math.sin(math.pi * fraction) * 1.25
            cell_x, cell_y = int(math.floor(x)), int(math.floor(y))
            if any(
                self.terrain.solid(
                    cell_x,
                    cell_y,
                    int(math.floor(player_z + body_offset)),
                )
                for body_offset in (0.0, 1.0)
            ):
                return False
        return True
