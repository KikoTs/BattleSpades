"""Incremental map-wide dry-route guidance for the local voxel planner.

The atlas supplies cheap primary-surface connectivity, not motor commands.
Each returned corner still goes through live voxel A*, including clearance,
excavation and native jump checks. Search work and retained nodes are bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
import math
from typing import Callable, Iterable

from .messages import Vector3

_SLICE_EXPANSIONS = 512
_MAX_DISCOVERED = 32768
_MAX_EXPANSIONS = 32768


@dataclass(slots=True)
class SurfaceCorridorSearch:
    """Resume one weighted A* instead of forgetting a concave obstacle.

    The search grows from both ends, always from the smaller frontier, and
    ends where the two meet. An end that is boxed in (a pocket, a tower top,
    a perch nothing leads to) has the small frontier and is settled first: a
    tower's stairs are followed down from the top instead of the whole map
    being flooded from below, and a goal with no walk to it is known after a
    few expansions, not after the cap.
    """

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
    # Every standable support of a column, for stepping backwards cheaply.
    layers_of: Callable[[int, int], Iterable[int]] | None = None
    frontier_back: list[tuple[float, int, float]] = field(default_factory=list)
    costs_back: dict[int, float] = field(default_factory=dict)
    children: dict[int, int] = field(default_factory=dict)
    closed_back: set[int] = field(default_factory=set)

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
        self.costs_back[self.target] = 0.0
        self.frontier_back.append((0.0, self.target, 0.0))

    def advance(self) -> None:
        """Spend at most one slice, keeping the frontiers for the next frame."""

        for _ in range(_SLICE_EXPANSIONS):
            if self.done:
                return
            if (not self.frontier or not self.frontier_back
                    or self.expansions >= _MAX_EXPANSIONS):
                self._finish()
                return
            if len(self.frontier) <= len(self.frontier_back):
                self._expand_forward()
            else:
                self._expand_backward()

    def _neighbors(self, node: int):
        column = self._column(node)
        x, y = column % self.width, column // self.width
        for nx, ny in ((x + 1, y), (x, y + 1), (x - 1, y), (x, y - 1)):
            if 0 <= nx < self.width and 0 <= ny < self.height:
                yield x, y, nx, ny

    def _estimate(self, x: int, y: int, level: int, end: int) -> float:
        column = self._column(end)
        return (abs(x - column % self.width) + abs(y - column // self.width)
                + abs(level - self._level(end)) * 0.4) * 1.25

    def _expand_forward(self) -> None:
        _, current, cost = heapq.heappop(self.frontier)
        self.expansions += 1
        if cost != self.costs[current] or current in self.closed:
            return
        self.closed.add(current)
        if current == self.target or current in self.closed_back:
            self._finish(meeting=current)
            return
        level = self._level(current)
        for _x, _y, nx, ny in self._neighbors(current):
            neighbor_column = ny * self.width + nx
            support = (self.surface_at(nx, ny, level)
                       if self.surface_at else self.supports[neighbor_column])
            if support is None:
                continue
            neighbor = self._node(neighbor_column, support)
            if neighbor in self.closed:
                continue
            rise = support - level
            # Match production WALK/JUMP; large drops have no motor contract.
            if support >= 239 or not -2 <= rise <= 1:
                continue
            if (current, neighbor) in self.blocked:
                continue
            candidate = cost + 1.0 + abs(rise) * 0.4
            if candidate >= self.costs.get(neighbor, math.inf):
                continue
            if neighbor not in self.costs and self._working_set_is_full():
                self._finish()
                return
            self.costs[neighbor] = candidate
            self.parents[neighbor] = current
            heapq.heappush(self.frontier, (
                candidate + self._estimate(nx, ny, support, self.target), neighbor, candidate))

    def _expand_backward(self) -> None:
        _, current, cost = heapq.heappop(self.frontier_back)
        self.expansions += 1
        if cost != self.costs_back[current] or current in self.closed_back:
            return
        self.closed_back.add(current)
        if current == self.start or current in self.closed:
            self._finish(meeting=current)
            return
        level = self._level(current)
        for x, y, nx, ny in self._neighbors(current):
            for previous in self._steps_onto(x, y, level, nx, ny):
                if previous in self.closed_back or (previous, current) in self.blocked:
                    continue
                before = self._level(previous)
                candidate = cost + 1.0 + abs(level - before) * 0.4
                if candidate >= self.costs_back.get(previous, math.inf):
                    continue
                if previous not in self.costs_back and self._working_set_is_full():
                    self._finish()
                    return
                self.costs_back[previous] = candidate
                self.children[previous] = current
                heapq.heappush(self.frontier_back, (
                    candidate + self._estimate(nx, ny, before, self.start), previous, candidate))

    def _steps_onto(self, x: int, y: int, level: int, nx: int, ny: int):
        """Nodes of column (nx, ny) whose forward step lands on (x, y, level)."""

        column = ny * self.width + nx
        if not self.surface_at:
            support = self.supports[column]
            if support < 239 and -2 <= level - support <= 1:
                yield column
            return
        known = self.layers_of is not None
        for support in (self.layers_of(nx, ny) if known else range(level - 1, level + 3)):
            if not level - 1 <= support <= min(level + 2, 238):
                continue
            if not known and self.surface_at(nx, ny, support) != support:
                continue
            # A step goes to the floor nearest the body's own height, which
            # from down there may be another one of this column.
            if self.surface_at(x, y, support) == level:
                yield self._node(column, support)

    def _working_set_is_full(self) -> bool:
        return len(self.costs) + len(self.costs_back) >= _MAX_DISCOVERED

    def exclude_edge(self, source: tuple[int, int, int], target: tuple[int, int, int]) -> None:
        """Retain bounded search work while learning a new local failure."""
        left = self._node(source[1] * self.width + source[0], source[2])
        right = self._node(target[1] * self.width + target[0], target[2])
        self.blocked = self.blocked | {(left, right)}

    def _finish(self, *, meeting: int | None = None) -> None:
        if meeting is not None:
            nodes = [meeting]
            while nodes[-1] != self.start:
                nodes.append(self.parents[nodes[-1]])
            nodes.reverse()
            while nodes[-1] != self.target:
                nodes.append(self.children[nodes[-1]])
            # An edge may have failed after it entered a search tree. Never
            # return a corridor using that stale chain; the next search
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
        for working in (self.frontier, self.costs, self.parents, self.closed,
                        self.frontier_back, self.costs_back, self.children, self.closed_back):
            working.clear()
