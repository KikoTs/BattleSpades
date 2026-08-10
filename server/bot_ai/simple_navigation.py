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
# The worker evaluates every live bot serially.  A 4,096-node query took
# 300-450 ms on water/structure-heavy stock maps, so a twelve-bot batch could
# stop publishing intentions for several seconds.  Routes are segmented and
# replan from their endpoint; 256 expansions keep one batch within the 8 Hz
# decision window while still making monotonic terrain progress.
_MAX_ROUTE_EXPANSIONS = 256
_MAX_WATER_EXPANSIONS = 8192
_WATER_FLOW_LOOKAHEAD = 4
_WALK_SECONDS_PER_CELL = 0.25
_BREACH_SETUP_COST = 1.5
_WALL_CLEARANCE_BIAS = 0.18


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

        if (
            allow_water
            and expected >= WATER_SUPPORT_Z - 1
            and not self.solid(x, y, WATER_SUPPORT_Z - 1)
            and not self.solid(x, y, WATER_SUPPORT_Z - 2)
        ):
            # Water is an authoritative plane, not a VXL support voxel.  A
            # column can contain an overhead bridge/platform whose atlas
            # primary support is several cells above a player swimming below
            # it (WinterValley x=215/y=249). Prefer the body-clear water plane
            # near water height; otherwise A* plans on the unreachable roof.
            return SurfaceNode(x, y, WATER_SUPPORT_Z)

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
        allow_water: bool = False,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RoutePlan:
        """Return one bounded route segment toward ``goal``.

        Water remains opt-in and is deliberately more expensive than walking,
        so a short dry detour wins while an island objective can still be
        reached instead of producing an empty route at the shoreline.
        """

        start_node = self.surface(
            int(math.floor(start[0])),
            int(math.floor(start[1])),
            float(start[2]),
            vertical_span=8,
            allow_water=allow_water,
        )
        expected_start_support = int(
            round(float(start[2]) + PLAYER_SUPPORT_OFFSET)
        )
        if (
            start_node is None
            or abs(
                int(start_node.support_z) - expected_start_support
            )
            > 2
        ):
            # Native player collision is capsule-like: at a wall face the
            # authoritative centre can sit a fraction inside the wall's voxel
            # coordinate or over a diagonal corner while its feet remain on
            # the neighboring support. Mayan exposes the wall case; Arctic's
            # upper platform exposes the corner case. A vertically distant
            # floor in floor(x/y) is not the surface the native body owns.
            recovered = self._adjacent_start_surface(
                start,
                allow_water=allow_water,
            )
            if (
                recovered is not None
                and (
                    start_node is None
                    or abs(
                        int(recovered.support_z)
                        - expected_start_support
                    )
                    < abs(
                        int(start_node.support_z)
                        - expected_start_support
                    )
                )
            ):
                start_node = recovered
        if start_node is None:
            return RoutePlan((), False, 0)

        if (
            dig_profile is not None
            and MovementAffordance.BREACH in abilities
        ):
            # Dig edges are substantially more expensive than movement edges:
            # each solid frontier column can expose flat/up/down excavation
            # alternatives.  Search ordinary traversal first and return any
            # useful segment.  Only a bot already stopped at the closest
            # reachable point pays for a breach search on its next decision.
            traversal = self.plan(
                start,
                goal,
                abilities=abilities,
                dig_profile=None,
                allow_water=allow_water,
                blocked_edges=blocked_edges,
            )
            if traversal.steps:
                return traversal

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
        target_surface = self.surface(
            target_x,
            target_y,
            float(goal[2]),
            vertical_span=12,
            allow_water=allow_water,
        )
        target_support = (
            int(target_surface.support_z)
            if target_surface is not None
            else None
        )

        def target_distance(node: tuple[int, int, int]) -> float:
            vertical = (
                0.0
                if target_support is None
                else float(int(node[2]) - target_support)
            )
            return math.sqrt(
                float(int(node[0]) - target_x) ** 2
                + float(int(node[1]) - target_y) ** 2
                + vertical * vertical
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
        best_distance = target_distance(start_key)
        reached = False
        expansions = 0

        while frontier and expansions < _MAX_ROUTE_EXPANSIONS:
            _priority, _sequence, current = heapq.heappop(frontier)
            expansions += 1
            distance = target_distance(current)
            if distance < best_distance:
                best = current
                best_distance = distance
            if (
                current[0] == target_x
                and current[1] == target_y
                and (
                    target_support is None
                    or int(current[2]) == target_support
                )
            ):
                best = current
                reached = True
                break

            for candidate in self._neighbors(
                current,
                abilities=abilities,
                dig_profile=dig_profile,
                allow_water=allow_water,
            ):
                try:
                    neighbor, affordance, edge_cost, breach = candidate
                    if len(neighbor) != 3:
                        continue
                    neighbor = tuple(int(value) for value in neighbor)
                    edge_cost = float(edge_cost)
                except (OverflowError, TypeError, ValueError):
                    # A malformed edge must cost one bot decision, never the
                    # entire worker batch.  The fleet crash log captured a
                    # tuple in this scalar slot immediately before the native
                    # worker fault, so validate the generator boundary before
                    # arithmetic or heap mutation.
                    continue
                if not math.isfinite(edge_cost) or edge_cost < 0.0:
                    continue
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
                heuristic = target_distance(neighbor)
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
                    self._wall_biased_waypoint(
                        node,
                        previous=nodes[index - 1],
                    )
                    if came_by[node] is not MovementAffordance.BREACH
                    else (
                        float(node[0]) + 0.5,
                        float(node[1]) + 0.5,
                        float(node[2]) - PLAYER_SUPPORT_OFFSET,
                    )
                ),
                came_by[node],
                came_breach[node],
            )
            for index, node in enumerate(nodes[1:], start=1)
        )
        return RoutePlan(
            self._compact_straight_steps(steps, start),
            bool(reached and local_goal_reachable),
            expansions,
        )

    def _adjacent_start_surface(
        self,
        position: Vector3,
        *,
        allow_water: bool,
    ) -> SurfaceNode | None:
        """Recover the live support beside a wall-overlapping body centre."""

        origin_x = int(math.floor(float(position[0])))
        origin_y = int(math.floor(float(position[1])))
        candidates: list[tuple[float, int, int, SurfaceNode]] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                x, y = origin_x + dx, origin_y + dy
                sample = self.surface(
                    x,
                    y,
                    float(position[2]),
                    vertical_span=8,
                    allow_water=allow_water,
                )
                if sample is None:
                    continue
                horizontal = (
                    (float(sample.x) + 0.5 - float(position[0])) ** 2
                    + (float(sample.y) + 0.5 - float(position[1])) ** 2
                )
                vertical = abs(
                    float(sample.position[2]) - float(position[2])
                )
                candidates.append(
                    (horizontal + vertical * vertical, x, y, sample)
                )
        if not candidates:
            return None
        return min(candidates, key=lambda row: row[:3])[3]

    def _wall_biased_waypoint(
        self,
        node: tuple[int, int, int],
        *,
        previous: tuple[int, int, int],
    ) -> Vector3:
        """Bias a floor-cell centre away from an adjacent body-height wall.

        The native capsule rests around 0.04 cells inside Mayan wall faces.
        A long compacted waypoint parallel to that face contributes almost no
        separating velocity, so collision friction pins the player despite a
        valid surface route. A small in-cell shoulder bias preserves the same
        voxel edge while giving the motor enough normal velocity to detach.
        """

        x, y, support_z = (int(value) for value in node)

        def body_wall(cell_x: int, cell_y: int) -> bool:
            return any(
                self.solid(cell_x, cell_y, support_z - offset)
                for offset in (2, 1)
            )

        travel_x = int(node[0]) - int(previous[0])
        travel_y = int(node[1]) - int(previous[1])
        # Only detach from a wall parallel to travel. A wall directly ahead
        # is the intended breach/turn boundary; pushing away from it makes the
        # subsequent melee edge harder to reach and changes exact approach
        # points without helping tangential collision.
        bias_x = (
            float(body_wall(x - 1, y)) - float(body_wall(x + 1, y))
            if travel_y != 0
            else 0.0
        )
        bias_y = (
            float(body_wall(x, y - 1)) - float(body_wall(x, y + 1))
            if travel_x != 0
            else 0.0
        )
        return (
            float(x) + 0.5 + bias_x * _WALL_CLEARANCE_BIAS,
            float(y) + 0.5 + bias_y * _WALL_CLEARANCE_BIAS,
            float(support_z) - PLAYER_SUPPORT_OFFSET,
        )

    def water_step(
        self,
        position: Vector3,
        *,
        preferred_goal: Vector3 | None = None,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RouteStep | None:
        """Return one monotonic edge toward the intended opposite bank.

        A strategic crossing owns ``preferred_goal``.  Follow that bearing
        through open water before falling back to the map-wide nearest-shore
        flow, otherwise a bot halfway across a river can turn around toward
        the bank it just left.  An adjacent high bank deliberately returns no
        step so build/breach recovery can open that intended exit.
        """

        current = self.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=6,
            allow_water=True,
        )
        if current is None or current.support_z < WATER_SUPPORT_Z:
            return None

        if preferred_goal is not None:
            preferred_bank = self._preferred_water_bank(current, preferred_goal)
            if preferred_bank is not None:
                bank_surface = self._live_dry_bank(
                    current,
                    int(math.floor(preferred_bank.waypoint[0])),
                    int(math.floor(preferred_bank.waypoint[1])),
                )
                bank_edge = (
                    (current.x, current.y, current.support_z),
                    (
                        int(math.floor(preferred_bank.waypoint[0])),
                        int(math.floor(preferred_bank.waypoint[1])),
                        (
                            int(bank_surface.support_z)
                            if bank_surface is not None
                            else current.support_z
                        ),
                    ),
                )
                if (
                    bank_edge not in blocked_edges
                    and bank_surface is not None
                    and abs(
                        int(bank_surface.support_z) - int(current.support_z)
                    ) > _MAX_NATIVE_WATER_BANK_RISE
                ):
                    return None
            directed = self._goal_directed_water_step(
                current,
                preferred_goal,
                blocked_edges=blocked_edges,
            )
            if directed is not None:
                return directed

        atlas = self._atlas
        if atlas is not None:
            route = atlas.water_route(current.x, current.y)
            if route is not None:
                cursor = current
                furthest_water: SurfaceNode | None = None
                atlas_path_blocked = False
                for _index in range(
                    min(_WATER_FLOW_LOOKAHEAD, max(1, int(route.distance)))
                ):
                    route = atlas.water_route(cursor.x, cursor.y)
                    if route is None:
                        break
                    if route.distance == 1:
                        # The atlas identifies a shoreline coordinate, not an
                        # immutable edge. Bots can excavate or build that
                        # column after the atlas is loaded, so decide whether
                        # it is still dry and climbable from the live VXL.
                        bank = self._live_dry_bank(
                            cursor,
                            int(route.goal_x),
                            int(route.goal_y),
                        )
                        if bank is None:
                            atlas_path_blocked = True
                            break
                        bank_edge = (
                            (cursor.x, cursor.y, cursor.support_z),
                            (bank.x, bank.y, bank.support_z),
                        )
                        if bank_edge in blocked_edges:
                            atlas_path_blocked = True
                            break
                        if cursor is current:
                            if abs(
                                int(bank.support_z)
                                - int(cursor.support_z)
                            ) <= _MAX_NATIVE_WATER_BANK_RISE:
                                return RouteStep(
                                    bank.position,
                                    MovementAffordance.JUMP,
                                )
                            # A live high bank is a valid exit, but movement
                            # alone cannot mount it. Let assisted build/breach
                            # recovery own the adjacent edge.
                            return None
                        break
                    next_key = int(route.next_x), int(route.next_y)
                    if (
                        abs(next_key[0] - int(cursor.x))
                        + abs(next_key[1] - int(cursor.y))
                        != 1
                    ):
                        break
                    sample = self.surface(
                        next_key[0],
                        next_key[1],
                        float(position[2]),
                        vertical_span=5,
                        allow_water=True,
                    )
                    if (
                        sample is None
                        or abs(
                            int(sample.support_z) - int(cursor.support_z)
                        )
                        > 2
                    ):
                        break
                    # Dirty columns bypass cached height lookup inside
                    # ``surface`` but do not invalidate the atlas direction
                    # by themselves. Live-validate every lookahead edge while
                    # preserving the atlas's strictly decreasing flow.
                    edge = (
                        (cursor.x, cursor.y, cursor.support_z),
                        (sample.x, sample.y, sample.support_z),
                    )
                    if edge in blocked_edges:
                        atlas_path_blocked = True
                        break
                    if sample.support_z < WATER_SUPPORT_Z:
                        break
                    furthest_water = sample
                    cursor = sample
                if furthest_water is not None and not atlas_path_blocked:
                    return RouteStep(
                        furthest_water.position,
                        MovementAffordance.WALK,
                    )

        return self._bounded_water_search(
            current,
            blocked_edges=blocked_edges,
        )

    def assisted_water_step(
        self,
        position: Vector3,
        *,
        preferred_goal: Vector3 | None = None,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RouteStep | None:
        """Return the intended adjacent tall bank for build/breach recovery."""

        current = self.surface(
            int(math.floor(position[0])),
            int(math.floor(position[1])),
            float(position[2]),
            vertical_span=6,
            allow_water=True,
        )
        atlas = self._atlas
        if current is None or current.support_z < WATER_SUPPORT_Z:
            return None
        if preferred_goal is not None:
            preferred = self._preferred_water_bank(current, preferred_goal)
            if preferred is not None:
                preferred_surface = self._live_dry_bank(
                    current,
                    int(math.floor(preferred.waypoint[0])),
                    int(math.floor(preferred.waypoint[1])),
                )
                preferred_edge = (
                    (current.x, current.y, current.support_z),
                    (
                        int(math.floor(preferred.waypoint[0])),
                        int(math.floor(preferred.waypoint[1])),
                        (
                            int(preferred_surface.support_z)
                            if preferred_surface is not None
                            else current.support_z
                        ),
                    ),
                )
                if (
                    preferred_edge not in blocked_edges
                    and preferred_surface is not None
                    and abs(
                        int(preferred_surface.support_z)
                        - int(current.support_z)
                    ) > _MAX_NATIVE_WATER_BANK_RISE
                ):
                    return preferred
        if atlas is not None:
            route = atlas.water_route(current.x, current.y)
            if route is not None and route.distance == 1:
                goal = self._live_dry_bank(
                    current,
                    int(route.goal_x),
                    int(route.goal_y),
                )
                if goal is not None:
                    edge = (
                        (current.x, current.y, current.support_z),
                        (goal.x, goal.y, goal.support_z),
                    )
                    if (
                        edge not in blocked_edges
                        and abs(
                            int(goal.support_z) - int(current.support_z)
                        ) > _MAX_NATIVE_WATER_BANK_RISE
                    ):
                        return RouteStep(
                            goal.position,
                            MovementAffordance.BUILD_STEP,
                        )

        # A cached exit may have disappeared entirely. Once the live water
        # search reaches any other high shoreline, it still needs an assisted
        # hand-off even though the atlas does not describe that bank.
        return self._adjacent_high_water_bank(
            current,
            preferred_goal=preferred_goal,
            blocked_edges=blocked_edges,
        )

    def jump_build_cell(self, position: Vector3) -> tuple[int, int, int] | None:
        """Return the empty, waterbed-supported step directly under a swimmer."""

        x = int(math.floor(position[0]))
        y = int(math.floor(position[1]))
        support_z = WATER_SUPPORT_Z
        target = x, y, WATER_SUPPORT_Z - 1
        if (
            not self.solid(x, y, support_z)
            or self.solid(*target)
        ):
            return None
        # Placement commits after physics. While the swim bob is low, the
        # candidate block occupies the capsule's feet and freezes the native
        # body inside the new support (London x=297). Hold jump until the top
        # body probe is wholly above z=238, then place the fixed water step.
        if int(math.floor(float(position[2]) + 2.0)) >= int(target[2]):
            return None
        return target

    def water_bridge_line(
        self,
        position: Vector3,
        direction: Vector3,
        *,
        max_cells: int = 6,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        """Return a short face-supported floor line over immediate water.

        The first cell is supported by the dry shore under the builder and
        every following cell is supported by the previous line cell.  The
        authoritative BlockLine handler still validates reach, inventory,
        protected areas, bodies, and live terrain before committing it.
        """

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        step_x, step_y = (
            (1 if dx > 0.0 else -1, 0)
            if abs(dx) >= abs(dy)
            else (0, 1 if dy > 0.0 else -1)
        )
        start_x = int(math.floor(position[0]))
        start_y = int(math.floor(position[1]))
        support_z = int(round(position[2] + PLAYER_SUPPORT_OFFSET))
        if not self.solid(start_x, start_y, support_z):
            return None

        cells: list[tuple[int, int, int]] = []
        limit = max(1, min(8, int(max_cells)))
        for distance in range(1, limit + 1):
            x = start_x + step_x * distance
            y = start_y + step_y * distance
            if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
                break
            if self.solid(x, y, support_z):
                break
            if self.solid(x, y, support_z - 1) or self.solid(
                x, y, support_z - 2
            ):
                break
            cells.append((x, y, support_z))
        if len(cells) < 2:
            return None
        return cells[0], cells[-1]

    def narrow_bridge_shoulder_line(
        self,
        position: Vector3,
        direction: Vector3,
        *,
        max_cells: int = 6,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        """Return a missing shoulder beside a supported one-cell bridge."""

        dx, dy, _ = direction
        if math.hypot(dx, dy) <= 1e-6:
            return None
        step_x, step_y = (
            (1 if dx > 0.0 else -1, 0)
            if abs(dx) >= abs(dy)
            else (0, 1 if dy > 0.0 else -1)
        )
        side_x, side_y = -step_y, step_x
        start_x = int(math.floor(position[0]))
        start_y = int(math.floor(position[1]))
        support_z = int(round(position[2] + PLAYER_SUPPORT_OFFSET))
        if not self.solid(start_x, start_y, support_z):
            return None
        cross_track = (
            (float(position[0]) - (float(start_x) + 0.5)) * side_x
            + (float(position[1]) - (float(start_y) + 0.5)) * side_y
        )
        preferred_side = 1 if cross_track >= 0.0 else -1
        limit = max(1, min(8, int(max_cells)))
        for side_sign in (preferred_side, -preferred_side):
            cells: list[tuple[int, int, int]] = []
            for distance in range(0, limit + 1):
                center_x = start_x + step_x * distance
                center_y = start_y + step_y * distance
                if not self.solid(center_x, center_y, support_z):
                    break
                x = center_x + side_x * side_sign
                y = center_y + side_y * side_sign
                if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
                    break
                if self.solid(x, y, support_z):
                    if cells:
                        break
                    continue
                if self.solid(x, y, support_z - 1) or self.solid(
                    x, y, support_z - 2
                ):
                    if cells:
                        break
                    continue
                cells.append((x, y, support_z))
                if len(cells) >= limit:
                    break
            if cells:
                return cells[0], cells[-1]
        return None

    def water_bank_breach(
        self,
        position: Vector3,
        profile: DigProfile,
        *,
        preferred_goal: Vector3 | None = None,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RouteStep | None:
        """Carve a body-height entrance into the intended tall bank."""

        bank = self.assisted_water_step(
            position,
            preferred_goal=preferred_goal,
            blocked_edges=blocked_edges,
        )
        if bank is None:
            return None
        source_x = int(math.floor(position[0]))
        source_y = int(math.floor(position[1]))
        target_x = int(math.floor(bank.waypoint[0]))
        target_y = int(math.floor(bank.waypoint[1]))
        support_z = WATER_SUPPORT_Z
        blockers = tuple(
            (target_x, target_y, support_z - offset)
            for offset in (2, 1)
            if self.solid(target_x, target_y, support_z - offset)
        )
        target_cell, estimated_swings = self._clearance_target(
            blockers,
            support_z,
            profile,
        )
        if target_cell is None or estimated_swings <= 0:
            return None
        breach = BreachPlan(
            source=(source_x, source_y, support_z),
            destination=(target_x, target_y, support_z),
            target_cell=target_cell,
            blocking_cells=blockers,
            tool_id=int(profile.tool_id),
            secondary=bool(profile.secondary),
            fire_interval=max(0.05, float(profile.fire_interval)),
            estimated_swings=int(estimated_swings),
        )
        return RouteStep(
            (
                float(target_x) + 0.5,
                float(target_y) + 0.5,
                float(support_z) - PLAYER_SUPPORT_OFFSET,
            ),
            MovementAffordance.BREACH,
            breach,
        )

    def _preferred_water_bank(
        self,
        current: SurfaceNode,
        goal: Vector3,
    ) -> RouteStep | None:
        """Return the adjacent dry bank that most advances toward ``goal``."""

        current_distance = math.hypot(
            float(goal[0]) - (float(current.x) + 0.5),
            float(goal[1]) - (float(current.y) + 0.5),
        )
        candidates: list[tuple[float, int, int, SurfaceNode]] = []
        for dx, dy in _CARDINAL_EDGES:
            x, y = current.x + dx, current.y + dy
            distance = math.hypot(
                float(goal[0]) - (float(x) + 0.5),
                float(goal[1]) - (float(y) + 0.5),
            )
            if distance >= current_distance - 1e-6:
                continue
            sample = self.surface(
                x,
                y,
                current.position[2],
                vertical_span=12,
                allow_water=False,
            )
            if (
                sample is None
                or sample.support_z >= WATER_SUPPORT_Z
            ):
                continue
            candidates.append((distance, x, y, sample))
        if not candidates:
            return None
        sample = min(candidates, key=lambda row: row[:3])[3]
        return RouteStep(sample.position, MovementAffordance.BUILD_STEP)

    def _live_dry_bank(
        self,
        current: SurfaceNode,
        x: int,
        y: int,
    ) -> SurfaceNode | None:
        """Return the live dry support nearest a swimmer's waterline."""

        x, y = int(x), int(y)
        if not 0 <= x < MAP_SIZE or not 0 <= y < MAP_SIZE:
            return None
        # A dry support far above an open water column is a bridge/ceiling,
        # not a shoreline. A bank that blocks a swimmer has collision at body
        # or foot height around the water plane; use that cheap probe before
        # scanning tall London cliffs all the way to their top support.
        if not (
            self.solid(x, y, WATER_SUPPORT_Z - 1)
            or self.solid(x, y, WATER_SUPPORT_Z - 2)
        ):
            return None
        expected = int(round(current.position[2] + PLAYER_SUPPORT_OFFSET))
        low = 2
        high = min(WATER_SUPPORT_Z, expected + 13)
        for support_z in sorted(
            range(low, high),
            key=lambda candidate: abs(candidate - expected),
        ):
            if not self.solid(x, y, support_z):
                continue
            if all(
                not self.solid(x, y, support_z - offset)
                for offset in (1, 2)
            ):
                return SurfaceNode(x, y, support_z)
        return None

    def _adjacent_high_water_bank(
        self,
        current: SurfaceNode,
        *,
        preferred_goal: Vector3 | None,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ],
    ) -> RouteStep | None:
        """Find a live adjacent bank that requires building or excavation."""

        candidates: list[tuple[float, int, int, SurfaceNode]] = []
        for dx, dy in _CARDINAL_EDGES:
            bank = self._live_dry_bank(current, current.x + dx, current.y + dy)
            if bank is None:
                continue
            if (
                abs(int(bank.support_z) - int(current.support_z))
                <= _MAX_NATIVE_WATER_BANK_RISE
            ):
                continue
            edge = (
                (current.x, current.y, current.support_z),
                (bank.x, bank.y, bank.support_z),
            )
            if edge in blocked_edges:
                continue
            distance = (
                0.0
                if preferred_goal is None
                else math.hypot(
                    float(preferred_goal[0]) - (float(bank.x) + 0.5),
                    float(preferred_goal[1]) - (float(bank.y) + 0.5),
                )
            )
            candidates.append((distance, bank.x, bank.y, bank))
        if not candidates:
            return None
        bank = min(candidates, key=lambda row: row[:3])[3]
        return RouteStep(bank.position, MovementAffordance.BUILD_STEP)

    def _goal_directed_water_step(
        self,
        start: SurfaceNode,
        goal: Vector3,
        *,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ],
    ) -> RouteStep | None:
        """Look ahead along a stable shortest bearing across open water."""

        cursor = start
        visited = {(int(start.x), int(start.y))}
        furthest_water: SurfaceNode | None = None
        for _index in range(_WATER_FLOW_LOOKAHEAD):
            current_distance = math.hypot(
                float(goal[0]) - (float(cursor.x) + 0.5),
                float(goal[1]) - (float(cursor.y) + 0.5),
            )
            candidates: list[tuple[float, int, int, SurfaceNode]] = []
            for dx, dy in _CARDINAL_EDGES:
                x, y = cursor.x + dx, cursor.y + dy
                if (x, y) in visited:
                    continue
                sample = self.surface(
                    x,
                    y,
                    cursor.position[2],
                    vertical_span=5,
                    allow_water=True,
                )
                if sample is None:
                    continue
                edge = (
                    (cursor.x, cursor.y, cursor.support_z),
                    (sample.x, sample.y, sample.support_z),
                )
                if edge in blocked_edges:
                    continue
                distance = math.hypot(
                    float(goal[0]) - (float(sample.x) + 0.5),
                    float(goal[1]) - (float(sample.y) + 0.5),
                )
                if distance >= current_distance - 1e-6:
                    continue
                if abs(int(sample.support_z) - int(cursor.support_z)) > 2:
                    continue
                candidates.append((distance, x, y, sample))
            if not candidates:
                break
            sample = min(candidates, key=lambda row: row[:3])[3]
            if sample.support_z < WATER_SUPPORT_Z:
                if cursor is start:
                    return RouteStep(
                        sample.position,
                        MovementAffordance.JUMP,
                    )
                break
            furthest_water = sample
            cursor = sample
            visited.add((int(cursor.x), int(cursor.y)))
        if furthest_water is None:
            return None
        return RouteStep(
            furthest_water.position,
            MovementAffordance.SWIM,
        )

    def _bounded_water_search(
        self,
        start: SurfaceNode,
        *,
        blocked_edges: frozenset[
            tuple[tuple[int, int, int], tuple[int, int, int]]
        ] = frozenset(),
    ) -> RouteStep | None:
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
                dry_sample = self._live_dry_bank(
                    current,
                    neighbor_key[0],
                    neighbor_key[1],
                )
                if dry_sample is not None:
                    dry_edge = (
                        (current.x, current.y, current.support_z),
                        (
                            dry_sample.x,
                            dry_sample.y,
                            dry_sample.support_z,
                        ),
                    )
                    if dry_edge in blocked_edges:
                        continue
                    # A high bank is still a route: swim to it, then let the
                    # adjacent assisted-water path build or excavate the exit.
                    came_from[neighbor_key] = current_key
                    samples[neighbor_key] = dry_sample
                    dry_goal = neighbor_key
                    frontier.clear()
                    break
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
                if support_delta > 2:
                    continue
                edge = (
                    (current.x, current.y, current.support_z),
                    (sample.x, sample.y, sample.support_z),
                )
                if edge in blocked_edges:
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
        if (
            sample.support_z < WATER_SUPPORT_Z
            and abs(int(sample.support_z) - int(start.support_z))
            > _MAX_NATIVE_WATER_BANK_RISE
        ):
            # Already touching a high bank: movement cannot mount it. The
            # caller immediately asks ``assisted_water_step`` for build/breach.
            return None
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
        allow_water: bool,
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
                allow_water=allow_water,
            )
            if sample is not None:
                delta = int(sample.support_z) - int(support_z)
                neighbor = sample.x, sample.y, sample.support_z
                current_is_water = int(support_z) >= WATER_SUPPORT_Z
                neighbor_is_water = int(sample.support_z) >= WATER_SUPPORT_Z
                if allow_water and (current_is_water or neighbor_is_water):
                    if abs(delta) <= 2:
                        yield (
                            neighbor,
                            (
                                MovementAffordance.JUMP
                                if current_is_water and not neighbor_is_water
                                else MovementAffordance.SWIM
                            ),
                            2.75 + abs(delta) * 0.35,
                            None,
                        )
                    continue
                low_overhang = (
                    self.solid(
                        nx,
                        ny,
                        int(sample.support_z) - 3,
                    )
                    or (
                        delta > 0
                        and self.solid(
                            nx,
                            ny,
                            int(support_z) - 3,
                        )
                    )
                )
                if abs(delta) <= 1 and not low_overhang:
                    yield (
                        neighbor,
                        MovementAffordance.WALK,
                        1.0 + abs(delta) * 0.25,
                        None,
                    )
                    continue
                if (
                    abs(delta) <= 1
                    and low_overhang
                    and MovementAffordance.CROUCH in abilities
                ):
                    # The lower destination has enough standing air after the
                    # body drops, but its overhang intersects the native body
                    # at the source height. Walking cannot enter far enough to
                    # start gravity; crouching lowers the body through the
                    # transition. This is the carved Mayan stair at
                    # x=127/y=210 from the production regression.
                    yield (
                        neighbor,
                        MovementAffordance.CROUCH,
                        1.4 + abs(delta) * 0.25,
                        None,
                    )
                    continue
                if (
                    delta == 0
                    and low_overhang
                    and dig_profile is not None
                    and MovementAffordance.BREACH in abilities
                ):
                    # The native standing capsule cannot enter a two-cell-high
                    # authored opening reliably, even with crouch held.  Do
                    # not advertise CROUCH to production A*: it produced
                    # permanent wedges on ArcticBase, BranCastle, Frontier,
                    # GreatWall, LunarBase, MayanJungle, and WinterValley.
                    # Clear only a level passage's one-voxel overhang and
                    # retain the authored floor.  Sloped/step transitions keep
                    # their existing alternate routing: opening those here
                    # exposes a bad two-block lip in MayanJungle's carved
                    # x=126 stair. This gives the level Invasion/Trenches
                    # passages a concrete action instead of retrying WALK.
                    blockers = tuple(sorted({
                        (nx, ny, int(sample.support_z) - 3),
                        (nx, ny, int(support_z) - 3),
                    } & {
                        cell for cell in (
                            (nx, ny, int(sample.support_z) - 3),
                            (nx, ny, int(support_z) - 3),
                        ) if self.solid(*cell)
                    }))
                    target_cell, estimated_swings = self._clearance_target(
                        blockers,
                        int(sample.support_z),
                        dig_profile,
                    )
                    if target_cell is not None and estimated_swings > 0:
                        breach = BreachPlan(
                            source=node,
                            destination=neighbor,
                            target_cell=target_cell,
                            blocking_cells=blockers,
                            tool_id=int(dig_profile.tool_id),
                            secondary=bool(dig_profile.secondary),
                            fire_interval=max(
                                0.05,
                                float(dig_profile.fire_interval),
                            ),
                            estimated_swings=int(estimated_swings),
                        )
                        yield (
                            neighbor,
                            MovementAffordance.BREACH,
                            1.0
                            + _BREACH_SETUP_COST
                            + abs(delta) * 0.25
                            + (
                                float(estimated_swings)
                                * max(
                                    0.05,
                                    float(dig_profile.fire_interval),
                                )
                            ) / _WALK_SECONDS_PER_CELL,
                            breach,
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
                    if (
                        dig_profile is not None
                        and MovementAffordance.BREACH in abilities
                    ):
                        # A two-block rise is normally jumpable, so traversal
                        # keeps the cheaper JUMP. If authoritative physics has
                        # already marked that concrete edge blocked, expose a
                        # distinct excavation edge that lowers the ledge to
                        # source height. Previously the generator continued
                        # immediately after JUMP, making GreatWall bots retry
                        # the same failed lip every six seconds forever.
                        for breach in self._breach_plans(
                            node,
                            nx,
                            ny,
                            dig_profile,
                        ):
                            excavation_seconds = (
                                float(breach.estimated_swings)
                                * max(0.05, float(breach.fire_interval))
                            )
                            yield (
                                breach.destination,
                                MovementAffordance.BREACH,
                                1.0
                                + _BREACH_SETUP_COST
                                + abs(
                                    int(breach.destination[2])
                                    - int(node[2])
                                )
                                * 0.25
                                + excavation_seconds
                                / _WALK_SECONDS_PER_CELL,
                                breach,
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
                    allow_water=allow_water,
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
                breaches = self._breach_plans(
                    node, nx, ny, dig_profile
                )
                for breach in breaches:
                    excavation_seconds = (
                        float(breach.estimated_swings)
                        * max(0.05, float(breach.fire_interval))
                    )
                    yield (
                        breach.destination,
                        MovementAffordance.BREACH,
                        1.0
                        + _BREACH_SETUP_COST
                        + abs(
                            int(breach.destination[2]) - int(node[2])
                        )
                        * 0.25
                        + excavation_seconds / _WALK_SECONDS_PER_CELL,
                        breach,
                    )
                if breaches:
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
                    allow_water=allow_water,
                )
                if landing is None:
                    continue
                delta = int(landing.support_z) - int(support_z)
                target = landing.x, landing.y, landing.support_z
                if (
                    distance == 2
                    and abs(delta) <= 2
                    and MovementAffordance.JUMP in abilities
                    and self._jump_gap_is_clear(
                        x,
                        y,
                        dx,
                        dy,
                        distance,
                        support_z,
                        landing.support_z,
                    )
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

    def _jump_gap_is_clear(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        distance: int,
        source_support: int,
        landing_support: int,
    ) -> bool:
        """Reject a nominal gap jump whose intermediate body crosses terrain."""

        for offset in range(1, max(1, int(distance))):
            fraction = float(offset) / float(distance)
            body_support = int(round(
                float(source_support)
                + (float(landing_support) - float(source_support))
                * fraction
            ))
            cell_x = int(x) + int(dx) * offset
            cell_y = int(y) + int(dy) * offset
            if any(
                self.solid(cell_x, cell_y, body_support - body_offset)
                for body_offset in (2, 1)
            ):
                return False
        return True

    def _breach_plans(
        self,
        source: tuple[int, int, int],
        x: int,
        y: int,
        profile: DigProfile,
    ) -> tuple[BreachPlan, ...]:
        """Describe flat/up/down tunnel edges without mutating the VXL.

        Returning every safe one-block elevation is important on tall solid
        terrain.  A single flat edge lets the search reach the target's X/Y
        underneath it and then stop; the upward alternatives let A* carve a
        staircase whose support cells are never part of the dig footprint.
        """

        source_support = int(source[2])
        # Preserve normal one-layer floor variation while tunnelling. The
        # support cell itself is never included in a planned dig footprint.
        result: list[BreachPlan] = []
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
            result.append(
                BreachPlan(
                    source=source,
                    destination=destination,
                    target_cell=target_cell,
                    blocking_cells=blockers,
                    tool_id=int(profile.tool_id),
                    secondary=bool(profile.secondary),
                    fire_interval=max(0.05, float(profile.fire_interval)),
                    estimated_swings=int(estimated_swings),
                )
            )
        return tuple(result)

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
                or abs(
                    float(step.waypoint[2]) - float(previous[2])
                )
                > 0.25
                or (
                    next_step is not None
                    and abs(
                        float(next_step.waypoint[2])
                        - float(step.waypoint[2])
                    )
                    > 0.25
                )
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
