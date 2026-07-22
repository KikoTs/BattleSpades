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


@dataclass(slots=True)
class _WaterSearchState:
    """Resumable breadth-first water escape search for one start column."""

    start: SurfaceSample
    radius: int
    frontier: deque[tuple[int, int]]
    came_from: dict[tuple[int, int], tuple[int, int] | None]
    samples: dict[tuple[int, int], SurfaceSample]


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

    def direction_is_traversable(
        self,
        start: Vector3,
        direction: Vector3,
        affordance: MovementAffordance,
        *,
        allow_water: bool = False,
    ) -> bool:
        """Validate an immediate step using the gameplay motor's body width."""

        dx, dy = float(direction[0]), float(direction[1])
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return True
        dx, dy = dx / length, dy / length
        shoulder_x, shoulder_y = -dy * 0.45, dx * 0.45
        expected_support = int(round(float(start[2]) + PLAYER_STANDING_OFFSET))
        climb, drop = {
            MovementAffordance.JUMP: (2, 3),
            MovementAffordance.DROP: (1, 4),
            MovementAffordance.JETPACK: (8, 8),
        }.get(affordance, (1, 1))

        def probes_at(distance: float) -> tuple[tuple[float, float], ...]:
            center_x = float(start[0]) + dx * distance
            center_y = float(start[1]) + dy * distance
            return (
                (center_x, center_y),
                (center_x + shoulder_x, center_y + shoulder_y),
                (center_x - shoulder_x, center_y - shoulder_y),
            )

        def supported(probes: tuple[tuple[float, float], ...]) -> bool:
            for probe_x, probe_y in probes:
                sample = self.classify(
                    int(math.floor(probe_x)),
                    int(math.floor(probe_y)),
                    float(start[2]),
                    allow_water=allow_water,
                    vertical_span=max(climb, drop),
                    clearance=2,
                )
                if sample is None:
                    return False
                delta = int(sample.support_z) - expected_support
                if not -climb <= delta <= drop:
                    return False
            return True

        immediate = probes_at(0.65)
        if supported(immediate):
            return True
        if affordance is not MovementAffordance.JUMP:
            return False
        body_z = int(math.floor(float(start[2])))
        if any(
            self.solid(int(math.floor(x)), int(math.floor(y)), z)
            for x, y in immediate
            for z in (body_z, body_z + 1)
        ):
            return False
        # A one-cell gap is allowed only when the full body width has a valid
        # two-cell landing beyond empty air.
        return supported(probes_at(2.05))

    def dry_corridor_is_traversable(
        self,
        start: Vector3,
        direction: Vector3,
        *,
        distance: float = 1.5,
    ) -> bool:
        """Reject a short walk corridor containing water or unsupported void.

        Local A* can legally choose a first 0.65-block step beside a cliff even
        when the next cell in the same escape direction is water. The live
        motor must account for braking distance, so worker escape selection
        uses this slightly longer, body-width dry lookahead as well.
        """
        dx, dy = float(direction[0]), float(direction[1])
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return True
        dx, dy = dx / length, dy / length
        shoulder_x, shoulder_y = -dy * 0.45, dx * 0.45
        limit = max(0.5, float(distance))
        sample_count = max(1, int(math.ceil(limit / 0.5)))
        for sample_index in range(1, sample_count + 1):
            sample_distance = min(limit, float(sample_index) * 0.5)
            center_x = float(start[0]) + dx * sample_distance
            center_y = float(start[1]) + dy * sample_distance
            for probe_x, probe_y in (
                (center_x, center_y),
                (center_x + shoulder_x, center_y + shoulder_y),
                (center_x - shoulder_x, center_y - shoulder_y),
            ):
                sample = self.classify(
                    int(math.floor(probe_x)),
                    int(math.floor(probe_y)),
                    float(start[2]),
                    allow_water=True,
                    vertical_span=8,
                    clearance=2,
                )
                if sample is None or sample.water:
                    return False
        return True


class VoxelActionPlanner:
    """Bounded local search for survival and topology-changing actions."""

    def __init__(self, terrain: VoxelTerrain) -> None:
        self.terrain = terrain
        # Water recovery is intentionally global, unlike ordinary local
        # action planning. Large authored seas (CastleWars reaches 132 cells
        # from shore) make a fixed 24/64-cell search strand valid players.
        # Successful routes are memoized as a flow toward one dry goal so a
        # swimming bot does not repeat the wide search every decision tick.
        self._water_next: dict[tuple[int, int], tuple[int, int]] = {}
        self._water_goal: dict[tuple[int, int], SurfaceSample] = {}
        self._water_dead_ends: set[tuple[int, int]] = set()
        self._water_route_columns: set[tuple[int, int]] = set()
        self._water_searches: dict[tuple[int, int], _WaterSearchState] = {}

    def invalidate_water_routes(
        self,
        changed_columns: (
            set[tuple[int, int]] | frozenset[tuple[int, int]] | None
        ) = None,
    ) -> None:
        """Drop escape flow affected by an authoritative terrain change.

        A remote bullet hole should not force a stranded bot to repeat a
        70,000-column sea search. Existing routes remain valid unless the
        delta touches one of their columns; failed searches are always retried
        because any newly built shore may make them recoverable.
        """

        self._water_dead_ends.clear()
        if changed_columns is None:
            self._water_searches.clear()
        else:
            # Preserve wide searches when an unrelated firefight changes a
            # distant column. A search only retains classified samples, so an
            # unvisited changed column is read canonically when reached.
            for start_key, search in tuple(self._water_searches.items()):
                if not search.came_from.keys().isdisjoint(changed_columns):
                    self._water_searches.pop(start_key, None)
        if (
            changed_columns is not None
            and self._water_route_columns.isdisjoint(changed_columns)
        ):
            return
        self._water_next.clear()
        self._water_goal.clear()
        self._water_route_columns.clear()

    def water_exit(
        self,
        position: Vector3,
        *,
        search_radius: int = MAP_X,
        max_nodes: int | None = None,
    ) -> VoxelActionStep | None:
        """Return the first recovery step toward the nearest dry surface.

        Water nodes are legal only inside this recovery search.  The search is
        bounded to the map and accepts at most a two-voxel shore climb,
        matching the base jump affordance. Successful paths seed a cached
        flow field for their water cells. ``max_nodes`` makes the wide search
        resumable across worker batches; ``None`` preserves the synchronous
        behavior used by focused offline navigation fixtures. A blocked or
        taller shore returns no step so the worker can escalate to breach or
        construction.
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
        cached_next = self._water_next.get(start_key)
        cached_goal = self._water_goal.get(start_key)
        if cached_next is not None and cached_goal is not None:
            cached_sample = self.terrain.classify(
                cached_next[0],
                cached_next[1],
                start.player_position[2],
                allow_water=True,
                vertical_span=3,
            )
            if cached_sample is not None:
                return self._water_step(
                    position, cached_sample, cached_goal
                )
            # A caller using a mutable terrain without explicit invalidation
            # still fails closed instead of steering into a newly blocked cell.
            self.invalidate_water_routes()
        if start_key in self._water_dead_ends:
            return None

        radius = max(1, min(MAP_X, int(search_radius)))
        search = self._water_searches.get(start_key)
        if search is None or search.radius != radius:
            if len(self._water_searches) >= 32:
                self._water_searches.pop(next(iter(self._water_searches)))
            search = _WaterSearchState(
                start=start,
                radius=radius,
                frontier=deque((start_key,)),
                came_from={start_key: None},
                samples={start_key: start},
            )
            self._water_searches[start_key] = search
        frontier = search.frontier
        came_from = search.came_from
        samples = search.samples
        node_budget = (
            None if max_nodes is None else max(1, int(max_nodes))
        )
        processed = 0

        while frontier and (node_budget is None or processed < node_budget):
            current_key = frontier.popleft()
            processed += 1
            current = samples[current_key]
            if not current.water:
                path = [current_key]
                while path[-1] != start_key:
                    previous = came_from[path[-1]]
                    if previous is None:
                        break
                    path.append(previous)
                path.reverse()
                if len(path) < 2:
                    return None
                for index, water_key in enumerate(path[:-1]):
                    self._water_next[water_key] = path[index + 1]
                    self._water_goal[water_key] = current
                    self._water_route_columns.add(water_key)
                    self._water_route_columns.add(path[index + 1])
                self._water_route_columns.add((current.x, current.y))
                self._water_searches.pop(start_key, None)
                return self._water_step(
                    position, samples[path[1]], current
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
        if frontier:
            # A healthy worker may emit no intention while this bounded slice
            # is pending; its processed-frame heartbeat keeps supervision live.
            return None
        self._water_searches.pop(start_key, None)
        self._water_dead_ends.add(start_key)
        return None

    @staticmethod
    def _water_step(
        position: Vector3,
        first: SurfaceSample,
        goal: SurfaceSample,
    ) -> VoxelActionStep | None:
        """Encode one adjacent swim step or the final jump onto dry land.

        Treating every water cell as a JUMP edge made the gameplay-thread
        safety gate fail closed on any imperfect probe. Ordinary water travel
        is steerable WALK locomotion with the swim key held by the worker;
        only the actual shore transition needs the non-rotatable JUMP edge.
        """

        # Correct cross-track drift on every worker sample. A cardinal vector
        # based only on cell indices preserves a tiny shoulder offset forever;
        # alongside a cliff that offset is enough for the live body-width probe
        # to reject an otherwise valid escape corridor.
        first_position = first.player_position
        dx = float(first_position[0] - position[0])
        dy = float(first_position[1] - position[1])
        length = math.hypot(dx, dy)
        if length <= 1e-6:
            return None
        return VoxelActionStep(
            direction=(dx / length, dy / length, 0.0),
            waypoint=first.player_position,
            goal=goal.player_position,
            affordance=(
                MovementAffordance.WALK
                if first.water
                else MovementAffordance.JUMP
            ),
            cells=(
                (first.x, first.y, first.support_z),
                (goal.x, goal.y, goal.support_z),
            ),
        )

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
                origin_position=(
                    start_position if current_key == start_key else None
                ),
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
        dx = float(first.player_position[0] - start_position[0])
        dy = float(first.player_position[1] - start_position[1])
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
        origin_position: Vector3 | None = None,
    ) -> Iterable[tuple[SurfaceSample, MovementAffordance, float]]:
        """Yield walk, jump, drop, and one-cell-gap actions from a surface."""

        origin = origin_position or current.player_position

        def fits_body(
            destination: SurfaceSample,
            affordance: MovementAffordance,
        ) -> bool:
            waypoint = destination.player_position
            return self.terrain.direction_is_traversable(
                origin,
                (
                    waypoint[0] - origin[0],
                    waypoint[1] - origin[1],
                    0.0,
                ),
                affordance,
            )

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
                    if fits_body(neighbor, MovementAffordance.WALK):
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
                    and fits_body(neighbor, MovementAffordance.JUMP)
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
                    and fits_body(neighbor, MovementAffordance.DROP)
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
