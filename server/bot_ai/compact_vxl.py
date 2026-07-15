"""Memory-bounded collision-only decoder for worker navigation.

The authoritative server VXL stores colors, overview textures, bounds, and a
full 7.5 MiB bit field.  A spawned worker needs only solid/air state.  This
decoder represents each 240-cell column as one Python integer bitset, reducing
the worker map footprint while preserving the exact implicit-underground and
vertical-normalization rules used by :class:`server.runtime_vxl.ServerVXL`.
"""

from __future__ import annotations

import math


MAP_SIZE = 512
MAP_HEIGHT = 240
MAP_AREA = MAP_SIZE * MAP_SIZE
FLOOR_BIT = 1 << (MAP_HEIGHT - 1)


def _raw_vxl_size(data: bytes) -> tuple[int, int]:
    position = 0
    columns = 0
    maximum_reference = 0
    limit = len(data)
    while position < limit:
        if position + 4 > limit:
            return 0, 0
        span_words = data[position]
        top_start = data[position + 1]
        top_end = data[position + 2]
        air_start = data[position + 3]
        maximum_reference = max(
            maximum_reference, top_start, top_end, air_start
        )
        while span_words:
            position += span_words * 4
            if position + 4 > limit:
                return 0, 0
            span_words = data[position]
            top_start = data[position + 1]
            top_end = data[position + 2]
            air_start = data[position + 3]
            maximum_reference = max(
                maximum_reference, top_start, top_end, air_start
            )
        top_length = top_end - top_start + 1 if top_end >= top_start else 0
        position += 4 + top_length * 4
        columns += 1
    if position != limit:
        return 0, 0
    return columns, maximum_reference


def _range_mask(start: int, end_exclusive: int) -> int:
    start = max(0, int(start))
    end_exclusive = min(MAP_HEIGHT, int(end_exclusive))
    if end_exclusive <= start:
        return 0
    return ((1 << (end_exclusive - start)) - 1) << start


def _marker_color(color: int) -> bool:
    return int(color) & 0x00F0F0F0 in (0x000000F0, 0x0000F000)


class CompactVoxelMap:
    """Collision-only VXL supporting worker LOS, tile builds, and deltas."""

    __slots__ = ("_columns", "source_z_shift")

    def __init__(self, raw_data: bytes) -> None:
        columns, maximum_reference = _raw_vxl_size(raw_data)
        edge = math.isqrt(columns) if columns > 0 else 0
        if edge <= 0 or edge * edge != columns or edge > MAP_SIZE:
            raise ValueError("invalid VXL column stream")
        self.source_z_shift = max(0, (MAP_HEIGHT - 1) - maximum_reference)
        self._columns = [FLOOR_BIT] * MAP_AREA
        offset = (MAP_SIZE - edge) // 2
        position = 0
        limit = len(raw_data)
        markers: list[tuple[int, int, int]] = []

        for source_y in range(edge):
            y = source_y + offset
            for source_x in range(edge):
                x = source_x + offset
                mask = FLOOR_BIT
                has_surface = False
                while True:
                    if position + 4 > limit:
                        raise ValueError("truncated VXL span header")
                    span_start = position
                    span_words = raw_data[position]
                    top_start = raw_data[position + 1]
                    top_end = raw_data[position + 2]
                    position += 4
                    top_length = (
                        top_end - top_start + 1 if top_end >= top_start else 0
                    )
                    if position + top_length * 4 > limit:
                        raise ValueError("truncated VXL top colors")
                    shifted_top = top_start + self.source_z_shift
                    mask |= _range_mask(shifted_top, shifted_top + top_length)
                    for index in range(top_length):
                        color_position = position + index * 4
                        color = int.from_bytes(
                            raw_data[color_position:color_position + 4], "little"
                        )
                        if _marker_color(color):
                            markers.append((x, y, shifted_top + index))
                    position += top_length * 4
                    has_surface = has_surface or top_length > 0

                    if span_words == 0:
                        if has_surface:
                            mask |= _range_mask(
                                top_end + 1 + self.source_z_shift,
                                MAP_HEIGHT,
                            )
                        break

                    bottom_length = span_words - top_length - 1
                    next_header = span_start + span_words * 4
                    if (
                        bottom_length < 0
                        or next_header + 4 > limit
                        or position + bottom_length * 4 != next_header
                    ):
                        raise ValueError("invalid VXL bottom span")
                    next_air_start = raw_data[next_header + 3]
                    bottom_start = next_air_start - bottom_length
                    if bottom_start < top_end + 1:
                        raise ValueError("overlapping VXL spans")
                    mask |= _range_mask(
                        top_end + 1 + self.source_z_shift,
                        bottom_start + bottom_length + self.source_z_shift,
                    )
                    for index in range(bottom_length):
                        color_position = position + index * 4
                        color = int.from_bytes(
                            raw_data[color_position:color_position + 4], "little"
                        )
                        if _marker_color(color):
                            markers.append(
                                (
                                    x,
                                    y,
                                    bottom_start + self.source_z_shift + index,
                                )
                            )
                    position += bottom_length * 4
                self._columns[y * MAP_SIZE + x] = mask

        if position != limit:
            raise ValueError("trailing VXL data")
        # Mirror the native exposed chroma-key marker cleanup. Snapshotting all
        # candidates before removal prevents one deletion exposing another.
        removable = [
            (x, y, z)
            for x, y, z in markers
            if self.get_solid(x, y, z)
            and not self.get_solid(x, y, z - 1)
            and not self.get_solid(x, y, z - 2)
        ]
        for x, y, z in removable:
            self.set_solid(x, y, z, False)

    def get_solid(self, x: int, y: int, z: int) -> bool:
        """Return collision occupancy for one in-bounds voxel."""

        x, y, z = int(x), int(y), int(z)
        if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE and 0 <= z < MAP_HEIGHT):
            return False
        return bool((self._columns[y * MAP_SIZE + x] >> z) & 1)

    def surface_z(self, x: int, y: int) -> int:
        """Topmost solid z of one column (z-down: the lowest set bit).

        O(1) via the two's-complement lowest-bit trick.  An empty column
        returns MAP_HEIGHT so callers treat it as the lowest possible ground.
        """

        x, y = int(x), int(y)
        if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
            return MAP_HEIGHT
        mask = self._columns[y * MAP_SIZE + x]
        if mask == 0:
            return MAP_HEIGHT
        return (mask & -mask).bit_length() - 1

    def set_solid(self, x: int, y: int, z: int, value: bool) -> None:
        """Apply one canonical live terrain delta."""

        x, y, z = int(x), int(y), int(z)
        if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE and 0 <= z < MAP_HEIGHT):
            return
        index = y * MAP_SIZE + x
        bit = 1 << z
        if value:
            self._columns[index] |= bit
        else:
            self._columns[index] &= ~bit

    def set_point(self, x: int, y: int, z: int, _color: int = 0) -> None:
        """ServerVXL-compatible solid mutation used by worker deltas."""

        self.set_solid(x, y, z, True)

    def remove_point_nochecks(self, x: int, y: int, z: int) -> None:
        """ServerVXL-compatible air mutation used by worker deltas."""

        self.set_solid(x, y, z, False)
