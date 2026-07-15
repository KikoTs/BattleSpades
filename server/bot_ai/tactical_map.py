"""Worker-side coarse tactical terrain layer.

A 32x32 grid of 16x16-column cells summarizing the collision map: mean and
highest surface height plus roughness, computed from a 4x4 column subsample
per cell.  Everything is incremental and bounded: the worker rebuilds at most
a small budget of dirty cells between message batches, so the layer never
competes with perception or pathfinding for worker time, and it never runs on
the gameplay thread at all.

AoS z increases downward: a SMALLER surface z is HIGHER ground.
"""

from __future__ import annotations

import math

GRID = 32
CELL = 16
_SAMPLE_OFFSETS = (2, 6, 10, 14)


class TacticalMap:
    """Incremental elevation summary over a CompactVoxelMap."""

    __slots__ = ("_vxl", "_cells", "_dirty")

    def __init__(self) -> None:
        self._vxl = None
        self._cells: list[tuple[float, int, float] | None] = [None] * (
            GRID * GRID
        )
        self._dirty: set[int] = set()

    def attach(self, vxl) -> None:
        """Bind a collision map and schedule a full incremental rebuild."""

        self._vxl = vxl if hasattr(vxl, "surface_z") else None
        self._cells = [None] * (GRID * GRID)
        self._dirty = set(range(GRID * GRID)) if self._vxl is not None else set()

    def mark_dirty(self, x: int, y: int) -> None:
        """Schedule the cell containing one mutated column for rebuild."""

        if 0 <= x < GRID * CELL and 0 <= y < GRID * CELL:
            self._dirty.add((int(y) // CELL) * GRID + (int(x) // CELL))

    @property
    def pending_cells(self) -> int:
        return len(self._dirty)

    def rebuild(self, budget: int = 64) -> int:
        """Recompute up to ``budget`` dirty cells; returns how many ran."""

        if self._vxl is None or not self._dirty:
            return 0
        done = 0
        while self._dirty and done < budget:
            index = self._dirty.pop()
            self._cells[index] = self._compute(index)
            done += 1
        return done

    def _compute(self, index: int) -> tuple[float, int, float]:
        base_x = (index % GRID) * CELL
        base_y = (index // GRID) * CELL
        surface = self._vxl.surface_z
        samples = [
            int(surface(base_x + ox, base_y + oy))
            for ox in _SAMPLE_OFFSETS
            for oy in _SAMPLE_OFFSETS
        ]
        mean = sum(samples) / len(samples)
        highest = min(samples)
        variance = sum((value - mean) ** 2 for value in samples) / len(samples)
        return mean, highest, math.sqrt(variance)

    def cell_at(self, position) -> tuple[float, int, float] | None:
        """Return (mean_z, highest_z, roughness) for the cell under a point."""

        x, y = int(position[0]), int(position[1])
        if not (0 <= x < GRID * CELL and 0 <= y < GRID * CELL):
            return None
        return self._cells[(y // CELL) * GRID + (x // CELL)]

    def high_ground_near(
        self, position, radius_cells: int = 2
    ) -> tuple[float, float, float] | None:
        """Center of the highest ready cell near a point, distance-penalized.

        Returns player-space coordinates (z = surface - 2.25) or None when no
        neighborhood cell has been computed yet.
        """

        if self._vxl is None:
            return None
        cx = int(position[0]) // CELL
        cy = int(position[1]) // CELL
        best = None
        best_key = math.inf
        for gy in range(max(0, cy - radius_cells), min(GRID, cy + radius_cells + 1)):
            for gx in range(max(0, cx - radius_cells), min(GRID, cx + radius_cells + 1)):
                cell = self._cells[gy * GRID + gx]
                if cell is None:
                    continue
                distance = max(abs(gx - cx), abs(gy - cy))
                key = float(cell[1]) + 0.75 * distance
                if key < best_key:
                    best_key = key
                    best = (
                        gx * CELL + CELL / 2.0,
                        gy * CELL + CELL / 2.0,
                        float(cell[1]) - 2.25,
                    )
        return best
