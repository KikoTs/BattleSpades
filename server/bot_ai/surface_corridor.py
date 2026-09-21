"""Incremental map-wide dry-route guidance for the local voxel planner.

The atlas supplies cheap primary-surface connectivity, not motor commands.
Each returned corner still goes through live voxel A*, including clearance,
excavation and native jump checks. Search work and retained nodes are bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math
from typing import Callable

from .messages import Vector3

_SLICE_EXPANSIONS = 512
_MAX_DISCOVERED = 32768
_MAX_EXPANSIONS = 32768


@dataclass(slots=True)
class SurfaceCorridorSearch:
    """Resume one weighted A* instead of forgetting a concave obstacle."""

    supports: bytes
    width: int
    height: int
    start: int
    target: int
    blocked: frozenset[tuple[int, int]] = frozenset()
    frontier: list[tuple[float, int, float]] = field(default_factory=list)
    costs: dict[int, float] = field(default_factory=dict)
    parents: dict[int, int] = field(default_factory=dict)
    closed: set[int] = field(default_factory=set)
    done: bool = False
    expansions: int = 0
    path: tuple[Vector3, ...] = ()
    surface_at: Callable[[int, int, int], int | None] | None = None
    start_height: int = 0
    target_height: int = 0

    def _column(self, node: int) -> int:
        return node % (self.width * self.height)

    def _level(self, node: int) -> int:
        return node // (self.width * self.height) if self.surface_at else self.supports[node]

    def _node(self, column: int, support: int) -> int:
        return column + support * self.width * self.height if self.surface_at else column

    def __post_init__(self) -> None:
        area = self.width * self.height
        if (self.width <= 0 or self.height <= 0 or len(self.supports) != area
                or not 0 <= self.start < area or not 0 <= self.target < area):
            raise ValueError("invalid surface corridor dimensions/endpoints")
        if self.surface_at:
            self.start = self._node(self.start, self.start_height)
            self.target = self._node(self.target, self.target_height)
        if self._level(self.start) >= 239 or self._level(self.target) >= 239:
            self.done = True
            return
        self.costs[self.start] = 0.0
        self.frontier.append((0.0, self.start, 0.0))

    def advance(self) -> None:
        """Spend at most one slice, keeping the frontier for the next frame."""

        if self.done:
            return
        target_column = self._column(self.target)
        target_x, target_y = target_column % self.width, target_column // self.width
        for _ in range(_SLICE_EXPANSIONS):
            if not self.frontier or self.expansions >= _MAX_EXPANSIONS:
                self._finish()
                return
            _, current, cost = heapq.heappop(self.frontier)
            self.expansions += 1
            if cost != self.costs[current] or current in self.closed:
                continue
            self.closed.add(current)
            if current == self.target:
                self._finish(reached=True)
                return
            column = self._column(current)
            x, y = column % self.width, column // self.width
            for nx, ny in ((x + 1, y), (x, y + 1), (x - 1, y), (x, y - 1)):
                if not (0 <= nx < self.width and 0 <= ny < self.height):
                    continue
                neighbor_column = ny * self.width + nx
                support = (self.surface_at(nx, ny, self._level(current))
                           if self.surface_at else self.supports[neighbor_column])
                if support is None:
                    continue
                neighbor = self._node(neighbor_column, support)
                if neighbor in self.closed:
                    continue
                rise = support - self._level(current)
                # Match production WALK/JUMP; large drops have no motor contract.
                if support >= 239 or not -2 <= rise <= 1:
                    continue
                if (current, neighbor) in self.blocked:
                    continue
                candidate = cost + 1.0 + abs(rise) * 0.4
                if candidate >= self.costs.get(neighbor, math.inf):
                    continue
                if neighbor not in self.costs and len(self.costs) >= _MAX_DISCOVERED:
                    self._finish()
                    return
                self.costs[neighbor] = candidate
                self.parents[neighbor] = current
                heuristic = (abs(nx - target_x) + abs(ny - target_y)
                             + abs(support - self._level(self.target)) * 0.4)
                heapq.heappush(self.frontier, (candidate + heuristic * 1.25, neighbor, candidate))

    def exclude_edge(self, source: tuple[int, int, int], target: tuple[int, int, int]) -> None:
        """Retain bounded search work while learning a new local failure."""
        left = self._node(source[1] * self.width + source[0], source[2])
        right = self._node(target[1] * self.width + target[0], target[2])
        self.blocked = self.blocked | {(left, right)}

    def _finish(self, *, reached: bool = False) -> None:
        if reached:
            nodes = [self.target]
            while nodes[-1] != self.start:
                nodes.append(self.parents[nodes[-1]])
            nodes.reverse()
            # An edge may have failed after it entered the search tree. Never
            # return a corridor using that stale parent chain; the next search
            # starts with current exclusions while unrelated work can finish.
            if any(edge in self.blocked for edge in zip(nodes, nodes[1:])):
                self._finish()
                return
            # Preserve corners and height transitions. Limit straight sections
            # to eight cells so detailed planning cannot shortcut a large bend.
            points: list[Vector3] = []
            last = 0
            for index, node in enumerate(nodes):
                if (index == 0 or index == len(nodes) - 1 or index - last >= 8
                        or node - nodes[index - 1] != nodes[index + 1] - node
                        or self._level(node) != self._level(nodes[index - 1])
                        or self._level(node) != self._level(nodes[index + 1])):
                    column = self._column(node)
                    points.append((column % self.width + 0.5, column // self.width + 0.5,
                                   self._level(node) - 2.25))
                    last = index
            self.path = tuple(points)
        self.done = True
        self.frontier.clear()
        self.costs.clear()
        self.parents.clear()
        self.closed.clear()
