"""Bounded navigation directly over the BattleSpades voxel field.

The legacy worker converted VXL columns to triangle soup, asked Recast to
rasterize those triangles back into a heightfield, and then layered a second
voxel planner and several recovery state machines on top.  This module keeps
one representation and one rule: every returned edge is a body-clear move in
the current collision snapshot.

Long routes are deliberately segmented.  A query can inspect only a fixed
number of nodes and a fixed radius, so malformed maps or unreachable goals
cannot wedge every bot in the worker.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import heapq
import math
from typing import Iterable

import shared.constants as C
from server.dig_profiles import (
    DIG_CUBE,
    DIG_MACHETE,
    DIG_SINGLE,
    DigProfile,
    melee_dig_positions,
)

from .compact_vxl import CompactVoxelMap
from .messages import (
    MapSnapshot,
    MovementAffordance,
    Vector3,
    WorldDelta,
    map_snapshot_vxl_bytes,
)
from .navigation_atlas import (
    _MAX_NATIVE_WATER_BANK_RISE,
    ColumnFlag,
    NO_SUPPORT,
    NavigationAtlas,
    load_or_build_atlas,
)


MAP_SIZE = 512
MAP_HEIGHT = 240
PLAYER_SUPPORT_OFFSET = 2.25
WATER_SUPPORT_Z = int(C.Z_ABOVE_WATERPLANE) + 1
_CARDINAL_EDGES = ((1, 0), (-1, 0), (0, 1), (0, -1))
_MAX_ROUTE_RADIUS = 64
_MAX_ROUTE_EXPANSIONS = 4096
_MAX_WATER_EXPANSIONS = 8192
_WALK_SECONDS_PER_CELL = 0.25
_BREACH_SETUP_COST = 1.5


@dataclass(frozen=True, slots=True)
class SurfaceNode:
    """One body-clear standing position supported by a solid voxel."""

    x: int
    y: int
    support_z: int

    @property
    def position(self) -> Vector3:
        return (
            float(self.x) + 0.5,
            float(self.y) + 0.5,
            float(self.support_z) - PLAYER_SUPPORT_OFFSET,
        )


@dataclass(frozen=True, slots=True)
class BreachPlan:
    """One costed excavation edge through an occupied body column."""

    source: tuple[int, int, int]
    destination: tuple[int, int, int]
    target_cell: tuple[int, int, int]
    blocking_cells: tuple[tuple[int, int, int], ...]
    tool_id: int
    secondary: bool
    fire_interval: float
    estimated_swings: int

    @property
    def target(self) -> Vector3:
        """Return the exact voxel centre the authoritative ray should hit."""

        return tuple(float(value) + 0.5 for value in self.target_cell)


@dataclass(frozen=True, slots=True)
class RouteStep:
    """One executable edge in a segmented route."""

    waypoint: Vector3
    affordance: MovementAffordance
    breach: BreachPlan | None = None


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """A bounded route segment and whether it reached its local target."""

    steps: tuple[RouteStep, ...]
    reached_segment_goal: bool
    expansions: int


class SimpleVoxelWorld:
    """Worker-owned collision map with bounded LOS and surface A* queries."""

    __slots__ = (
        "map_epoch",
        "topology_version",
        "_vxl",
        "_atlas",
        "_dirty_columns",
    )

    def __init__(self) -> None:
        self.map_epoch = -1
        self.topology_version = -1
        self._vxl: CompactVoxelMap | None = None
        self._atlas: NavigationAtlas | None = None
        self._dirty_columns: set[tuple[int, int]] = set()

    @property
    def ready(self) -> bool:
        return self._vxl is not None

    def load(self, snapshot: MapSnapshot) -> None:
        """Replace all map-scoped state with one verified VXL snapshot."""

        self.map_epoch = int(snapshot.map_epoch)
        self.topology_version = int(snapshot.topology_version)
        self._vxl = None
        self._atlas = None
        self._dirty_columns.clear()
        raw_vxl = map_snapshot_vxl_bytes(snapshot)
        if not raw_vxl:
            return
        vxl = CompactVoxelMap(raw_vxl)
        for change in snapshot.changed_cells:
            vxl.set_solid(change.x, change.y, change.z, change.solid)
            self._dirty_columns.add((int(change.x), int(change.y)))
        self._vxl = vxl
        try:
            self._atlas, _cache_hit = load_or_build_atlas(
                vxl,
                raw_vxl,
                map_name=(
                    snapshot.map_name if not snapshot.changed_cells else ""
                ),
                map_directory=(
                    snapshot.map_directory if not snapshot.changed_cells else ""
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            # Ordinary dry navigation does not depend on the semantic atlas.
            # Only map-wide water recovery loses its precomputed fast path.
            self._atlas = None

    def apply(self, delta: WorldDelta) -> None:
        """Apply one monotonic terrain batch and invalidate touched columns."""

        if int(delta.map_epoch) != self.map_epoch:
            return
        if int(delta.topology_version) < self.topology_version:
            return
        if self._vxl is not None:
            for change in delta.changed_cells:
                self._vxl.set_solid(
                    int(change.x),
                    int(change.y),
                    int(change.z),
                    bool(change.solid),
                )
                self._dirty_columns.add((int(change.x), int(change.y)))
        self.topology_version = int(delta.topology_version)

    def solid(self, x: int, y: int, z: int) -> bool:
        """Return collision occupancy, failing closed outside the map."""

        if (
            self._vxl is None
            or not 0 <= int(x) < MAP_SIZE
            or not 0 <= int(y) < MAP_SIZE
            or not 0 <= int(z) < MAP_HEIGHT
        ):
            return True
        return bool(self._vxl.get_solid(int(x), int(y), int(z)))

    def has_line_of_sight(self, origin: Vector3, target: Vector3) -> bool:
        """Return whether a half-voxel DDA ray stays in air."""

        if self._vxl is None:
            return False
        dx = float(target[0]) - float(origin[0])
        dy = float(target[1]) - float(origin[1])
        dz = float(target[2]) - float(origin[2])
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        if distance <= 1e-6:
            return True
        steps = max(1, min(384, int(math.ceil(distance * 2.0))))
        for index in range(1, steps):
            fraction = float(index) / float(steps)
            if self.solid(
                int(math.floor(float(origin[0]) + dx * fraction)),
                int(math.floor(float(origin[1]) + dy * fraction)),
                int(math.floor(float(origin[2]) + dz * fraction)),
            ):
                return False
        return True

    def surface(
        self,
        x: int,
        y: int,
        player_z: float,
        *,
        vertical_span: int = 4,
        clearance: int = 2,
        allow_water: bool = False,
    ) -> SurfaceNode | None:
        """Return the nearest body-clear support around ``player_z``."""

        x, y = int(x), int(y)
        if not 0 <= x < MAP_SIZE or not 0 <= y < MAP_SIZE:
            return None
        expected = int(round(float(player_z) + PLAYER_SUPPORT_OFFSET))

        atlas = self._atlas
        if (
            atlas is not None
            and (x, y) not in self._dirty_columns
            and clearance == 2
        ):
            index = y * atlas.width + x
            support = int(atlas.primary_support[index])
            flags = int(atlas.flags[index])
            water = bool(flags & int(ColumnFlag.WATER))
            if (
                support != NO_SUPPORT
                and int(atlas.layer_count[index]) == 1
                and abs(support - expected) <= max(0, int(vertical_span))
                and (allow_water or not water)
            ):
                return SurfaceNode(x, y, support)

        low = max(clearance, expected - max(0, int(vertical_span)))
        high = min(
            MAP_HEIGHT,
            expected + max(0, int(vertical_span)) + 1,
        )
        candidates = sorted(range(low, high), key=lambda z: abs(z - expected))
        for support_z in candidates:
            if support_z >= WATER_SUPPORT_Z and not allow_water:
                continue
            if not self.solid(x, y, support_z):
                continue
            if all(
                not self.solid(x, y, support_z - offset)
                for offset in range(1, clearance + 1)
            ):
                return SurfaceNode(x, y, support_z)
        return None

    def plan(
        self,
        start: Vector3,
        goal: Vector3,
        *,
        abilities: frozenset[MovementAffordance],
        dig_profile: DigProfile | None = None,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RoutePlan:
        """Return one bounded dry route segment toward ``goal``."""

        start_node = self.surface(
            int(math.floor(start[0])),
            int(math.floor(start[1])),
            float(start[2]),
            vertical_span=8,
        )
        if start_node is None:
            return RoutePlan((), False, 0)

        target_x = max(
            start_node.x - _MAX_ROUTE_RADIUS,
            min(start_node.x + _MAX_ROUTE_RADIUS, int(math.floor(goal[0]))),
        )
        target_y = max(
            start_node.y - _MAX_ROUTE_RADIUS,
            min(start_node.y + _MAX_ROUTE_RADIUS, int(math.floor(goal[1]))),
        )
        local_goal_reachable = (
            target_x == int(math.floor(goal[0]))
            and target_y == int(math.floor(goal[1]))
        )

        start_key = (
            int(start_node.x),
            int(start_node.y),
            int(start_node.support_z),
        )
        frontier: list[tuple[float, int, tuple[int, int, int]]] = [
            (0.0, 0, start_key)
        ]
        came_from: dict[
            tuple[int, int, int], tuple[int, int, int] | None
        ] = {start_key: None}
        came_by: dict[
            tuple[int, int, int], MovementAffordance
        ] = {start_key: MovementAffordance.WALK}
        came_breach: dict[
            tuple[int, int, int], BreachPlan | None
        ] = {start_key: None}
        costs: dict[tuple[int, int, int], float] = {start_key: 0.0}
        sequence = 0
        best = start_key
        best_distance = math.hypot(
            float(start_node.x - target_x),
            float(start_node.y - target_y),
        )
        reached = False
        expansions = 0

        while frontier and expansions < _MAX_ROUTE_EXPANSIONS:
            _priority, _sequence, current = heapq.heappop(frontier)
            expansions += 1
            distance = math.hypot(
                float(current[0] - target_x),
                float(current[1] - target_y),
            )
            if distance < best_distance:
                best = current
                best_distance = distance
            if current[0] == target_x and current[1] == target_y:
                best = current
                reached = True
                break

            for neighbor, affordance, edge_cost, breach in self._neighbors(
                current,
                abilities=abilities,
                dig_profile=dig_profile,
            ):
                edge = (current, neighbor)
                if edge in blocked_edges:
                    continue
                new_cost = costs[current] + edge_cost
                if new_cost >= costs.get(neighbor, math.inf):
                    continue
                costs[neighbor] = new_cost
                came_from[neighbor] = current
                came_by[neighbor] = affordance
                came_breach[neighbor] = breach
                sequence += 1
                heuristic = math.hypot(
                    float(neighbor[0] - target_x),
                    float(neighbor[1] - target_y),
                )
                heapq.heappush(
                    frontier,
                    (new_cost + heuristic, sequence, neighbor),
                )

        if best == start_key:
            return RoutePlan((), False, expansions)

        nodes: list[tuple[int, int, int]] = []
        cursor: tuple[int, int, int] | None = best
        while cursor is not None:
            nodes.append(cursor)
            cursor = came_from[cursor]
        nodes.reverse()

        steps = tuple(
            RouteStep(
                (
                    float(node[0]) + 0.5,
                    float(node[1]) + 0.5,
                    float(node[2]) - PLAYER_SUPPORT_OFFSET,
                ),
                came_by[node],
                came_breach[node],
            )
            for node in nodes[1:]
        )
        return RoutePlan(
            self._compact_straight_steps(steps, start),
            bool(reached and local_goal_reachable),
            expansions,
        )

    def water_step(self, position: Vector3) -> RouteStep | None:
        """Return one monotonic edge toward dry ground for a wading bot."""

        current = self.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=6,
            allow_water=True,
        )
        if current is None or current.support_z < WATER_SUPPORT_Z:
            return None

        atlas = self._atlas
        key = current.x, current.y
        if atlas is not None and key not in self._dirty_columns:
            route = atlas.water_route(current.x, current.y)
            if route is not None:
                next_key = route.next_x, route.next_y
                goal_key = route.goal_x, route.goal_y
                if (
                    next_key not in self._dirty_columns
                    and goal_key not in self._dirty_columns
                    and not (route.distance == 1 and not route.climbable)
                ):
                    sample = self.surface(
                        route.next_x,
                        route.next_y,
                        float(position[2]),
                        vertical_span=5,
                        allow_water=True,
                    )
                    if sample is not None:
                        return RouteStep(
                            sample.position,
                            (
                                MovementAffordance.JUMP
                                if route.distance == 1
                                else MovementAffordance.WALK
                            ),
                        )

        return self._bounded_water_search(current)

    def _bounded_water_search(self, start: SurfaceNode) -> RouteStep | None:
        """Find a nearby live shore without any persistent recovery state."""

        start_key = start.x, start.y
        frontier: deque[tuple[int, int]] = deque([start_key])
        came_from: dict[
            tuple[int, int], tuple[int, int] | None
        ] = {start_key: None}
        samples: dict[tuple[int, int], SurfaceNode] = {start_key: start}
        dry_goal: tuple[int, int] | None = None
        expansions = 0
        while frontier and expansions < _MAX_WATER_EXPANSIONS:
            current_key = frontier.popleft()
            current = samples[current_key]
            expansions += 1
            for dx, dy in _CARDINAL_EDGES:
                neighbor_key = current.x + dx, current.y + dy
                if neighbor_key in came_from:
                    continue
                sample = self.surface(
                    neighbor_key[0],
                    neighbor_key[1],
                    current.position[2],
                    vertical_span=4,
                    allow_water=True,
                )
                if sample is None:
                    continue
                support_delta = abs(
                    int(sample.support_z) - int(current.support_z)
                )
                if (
                    sample.support_z < WATER_SUPPORT_Z
                    and support_delta > _MAX_NATIVE_WATER_BANK_RISE
                ):
                    # A two-block rise is a valid dry-ground jump, but the
                    # native body cannot reliably initiate it after a swim.
                    # Keep searching this water component for a lower bank.
                    continue
                if support_delta > 2:
                    continue
                came_from[neighbor_key] = current_key
                samples[neighbor_key] = sample
                if sample.support_z < WATER_SUPPORT_Z:
                    dry_goal = neighbor_key
                    frontier.clear()
                    break
                frontier.append(neighbor_key)

        if dry_goal is None:
            return None
        cursor = dry_goal
        while came_from[cursor] not in (None, start_key):
            parent = came_from[cursor]
            if parent is None:
                break
            cursor = parent
        sample = samples[cursor]
        return RouteStep(
            sample.position,
            (
                MovementAffordance.JUMP
                if sample.support_z < WATER_SUPPORT_Z
                else MovementAffordance.WALK
            ),
        )

    def _neighbors(
        self,
        node: tuple[int, int, int],
        *,
        abilities: frozenset[MovementAffordance],
        dig_profile: DigProfile | None,
    ) -> Iterable[
        tuple[
            tuple[int, int, int],
            MovementAffordance,
            float,
            BreachPlan | None,
        ]
    ]:
        x, y, support_z = node
        player_z = float(support_z) - PLAYER_SUPPORT_OFFSET
        for dx, dy in _CARDINAL_EDGES:
            nx, ny = x + dx, y + dy
            sample = self.surface(
                nx,
                ny,
                player_z,
                vertical_span=8,
                clearance=2,
            )
            if sample is not None:
                delta = int(sample.support_z) - int(support_z)
                neighbor = sample.x, sample.y, sample.support_z
                if abs(delta) <= 1:
                    yield (
                        neighbor,
                        MovementAffordance.WALK,
                        1.0 + abs(delta) * 0.25,
                        None,
                    )
                    continue
                if (
                    -2 <= delta < -1
                    and MovementAffordance.JUMP in abilities
                ):
                    yield (
                        neighbor,
                        MovementAffordance.JUMP,
                        1.8 + abs(delta) * 0.35,
                        None,
                    )
                    continue
                if (
                    1 < delta <= 4
                    and MovementAffordance.DROP in abilities
                ):
                    yield (
                        neighbor,
                        MovementAffordance.DROP,
                        1.2 + delta * 0.15,
                        None,
                    )
                    continue
                if (
                    abs(delta) <= 8
                    and MovementAffordance.JETPACK in abilities
                ):
                    yield (
                        neighbor,
                        MovementAffordance.JETPACK,
                        3.0 + abs(delta) * 0.2,
                        None,
                    )
                    continue

            if MovementAffordance.CROUCH in abilities:
                crouched = self.surface(
                    nx,
                    ny,
                    player_z,
                    vertical_span=1,
                    clearance=1,
                )
                if (
                    crouched is not None
                    and abs(crouched.support_z - support_z) <= 1
                ):
                    yield (
                        (crouched.x, crouched.y, crouched.support_z),
                        MovementAffordance.CROUCH,
                        1.4,
                        None,
                    )
                    continue

            if (
                dig_profile is not None
                and MovementAffordance.BREACH in abilities
            ):
                breach = self._breach_plan(
                    node,
                    nx,
                    ny,
                    dig_profile,
                )
                if breach is not None:
                    excavation_seconds = (
                        float(breach.estimated_swings)
                        * max(0.05, float(breach.fire_interval))
                    )
                    yield (
                        breach.destination,
                        MovementAffordance.BREACH,
                        1.0
                        + _BREACH_SETUP_COST
                        + excavation_seconds / _WALK_SECONDS_PER_CELL,
                        breach,
                    )
                    continue

            max_gap = (
                4
                if MovementAffordance.JETPACK in abilities
                else 2
            )
            for distance in range(2, max_gap + 1):
                landing = self.surface(
                    x + dx * distance,
                    y + dy * distance,
                    player_z,
                    vertical_span=8,
                    clearance=2,
                )
                if landing is None:
                    continue
                delta = int(landing.support_z) - int(support_z)
                target = landing.x, landing.y, landing.support_z
                if (
                    distance == 2
                    and abs(delta) <= 2
                    and MovementAffordance.JUMP in abilities
                ):
                    yield (
                        target,
                        MovementAffordance.JUMP,
                        2.6 + abs(delta) * 0.3,
                        None,
                    )
                    break
                if (
                    abs(delta) <= 8
                    and MovementAffordance.JETPACK in abilities
                ):
                    yield (
                        target,
                        MovementAffordance.JETPACK,
                        3.5 + distance * 0.5,
                        None,
                    )
                    break

    def _breach_plan(
        self,
        source: tuple[int, int, int],
        x: int,
        y: int,
        profile: DigProfile,
    ) -> BreachPlan | None:
        """Describe a safe body-height tunnel edge without mutating VXL."""

        source_support = int(source[2])
        # Preserve normal one-layer floor variation while tunnelling. The
        # support cell itself is never included in a planned dig footprint.
        for support_z in (
            source_support,
            source_support - 1,
            source_support + 1,
        ):
            if not 2 <= support_z < WATER_SUPPORT_Z:
                continue
            if not self.solid(x, y, support_z):
                continue
            blockers = tuple(
                (int(x), int(y), support_z - offset)
                for offset in (2, 1)
                if self.solid(int(x), int(y), support_z - offset)
            )
            if not blockers:
                continue
            target_cell, estimated_swings = self._clearance_target(
                blockers,
                support_z,
                profile,
            )
            if target_cell is None or estimated_swings <= 0:
                continue
            destination = int(x), int(y), int(support_z)
            return BreachPlan(
                source=source,
                destination=destination,
                target_cell=target_cell,
                blocking_cells=blockers,
                tool_id=int(profile.tool_id),
                secondary=bool(profile.secondary),
                fire_interval=max(0.05, float(profile.fire_interval)),
                estimated_swings=int(estimated_swings),
            )
        return None

    @staticmethod
    def _clearance_target(
        blockers: tuple[tuple[int, int, int], ...],
        support_z: int,
        profile: DigProfile,
    ) -> tuple[tuple[int, int, int] | None, int]:
        """Aim where this tool clears the most body voxels without the floor."""

        if not blockers or profile.swings_per_block <= 0:
            return None, 0
        x, y, _z = blockers[0]
        if profile.pattern == DIG_SINGLE:
            # Clear the head-height voxel first so the next swing remains
            # reachable even when the bot is pressed against the wall.
            target = min(blockers, key=lambda cell: cell[2])
            return target, len(blockers) * profile.swings_per_block

        target = (int(x), int(y), int(support_z) - 2)
        footprint = set(melee_dig_positions(target, profile.pattern))
        if (int(x), int(y), int(support_z)) in footprint:
            return None, 0
        covered = sum(cell in footprint for cell in blockers)
        if covered <= 0:
            return None, 0
        if profile.pattern in {DIG_CUBE}:
            return target, 1
        if profile.pattern == DIG_MACHETE:
            return target, profile.swings_per_block
        # Ordinary/classic spades use the recovered vertical-column handler.
        return target, 1

    @staticmethod
    def _compact_straight_steps(
        steps: tuple[RouteStep, ...],
        start: Vector3,
    ) -> tuple[RouteStep, ...]:
        """Keep only corners of same-affordance cardinal runs."""

        if len(steps) < 3:
            return steps
        result: list[RouteStep] = []
        previous = start
        previous_direction: tuple[int, int] | None = None
        for index, step in enumerate(steps):
            direction = (
                int(math.copysign(1, step.waypoint[0] - previous[0]))
                if abs(step.waypoint[0] - previous[0]) > 0.25
                else 0,
                int(math.copysign(1, step.waypoint[1] - previous[1]))
                if abs(step.waypoint[1] - previous[1]) > 0.25
                else 0,
            )
            next_step = steps[index + 1] if index + 1 < len(steps) else None
            next_direction = None
            if next_step is not None:
                next_direction = (
                    int(math.copysign(1, next_step.waypoint[0] - step.waypoint[0]))
                    if abs(next_step.waypoint[0] - step.waypoint[0]) > 0.25
                    else 0,
                    int(math.copysign(1, next_step.waypoint[1] - step.waypoint[1]))
                    if abs(next_step.waypoint[1] - step.waypoint[1]) > 0.25
                    else 0,
                )
            if (
                next_step is None
                or next_direction != direction
                or next_step.affordance is not step.affordance
                or step.affordance is not MovementAffordance.WALK
                or previous_direction not in (None, direction)
            ):
                result.append(step)
            previous = step.waypoint
            previous_direction = direction
        return tuple(result)


__all__ = [
    "BreachPlan",
    "RoutePlan",
    "RouteStep",
    "SimpleVoxelWorld",
    "SurfaceNode",
]
