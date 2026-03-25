"""
Runtime VXL adapter for the currently built native module.

The fresh PYX loader keeps standard shipped maps in a shifted internal z-space.
Until the rebuilt native module is in place, the server can subclass the native
type and translate public z values back to the coordinate system the rest of the
runtime and client expect.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Optional

from aoslib.vxl import VXL


logger = logging.getLogger(__name__)

MAP_SIZE = 512
MAP_HEIGHT = 240
EMPTY_TOP_END = 239


def _read_source_bytes(source, size_or_detail: int) -> bytes:
    if isinstance(source, str) and os.path.exists(source):
        with open(source, "rb") as handle:
            return handle.read()

    if isinstance(source, bytes):
        data = source
    elif isinstance(source, bytearray):
        data = bytes(source)
    else:
        return b""

    if size_or_detail > 0 and len(data) > size_or_detail:
        return data[:size_or_detail]
    return data


def _raw_vxl_size(data: bytes) -> tuple[int, int]:
    pos = 0
    limit = len(data)
    columns = 0
    max_ref = 0

    while pos < limit:
        if pos + 4 > limit:
            return (0, 0)

        span_words = data[pos]
        v1 = data[pos + 1]
        v2 = data[pos + 2]
        v3 = data[pos + 3]
        max_ref = max(max_ref, v1, v2, v3)

        while span_words:
            pos += 4 * span_words
            if pos + 4 > limit:
                return (0, 0)
            span_words = data[pos]
            v1 = data[pos + 1]
            v2 = data[pos + 2]
            v3 = data[pos + 3]
            max_ref = max(max_ref, v1, v2, v3)

        if v2 >= v1:
            pos += 8 + 4 * (v2 - v1)
        else:
            pos += 4
        columns += 1

    if pos != limit:
        return (0, 0)
    return (columns, max_ref)


def _raw_column_surface_z(data: bytes, target_x: int, target_y: int) -> Optional[int]:
    pos = 0
    for y in range(MAP_SIZE):
        for x in range(MAP_SIZE):
            if pos + 4 > len(data):
                return None

            span_words = data[pos]
            top_start = data[pos + 1]
            top_end = data[pos + 2]
            pos += 4

            if x == target_x and y == target_y:
                if top_end >= top_start:
                    return top_start
                return None

            if span_words == 0:
                if top_end >= top_start:
                    pos += (top_end - top_start + 1) * 4
                continue

            pos += (span_words - 1) * 4

    return None


def _find_first_surface_column(data: bytes) -> tuple[int, int, int] | None:
    pos = 0
    for y in range(MAP_SIZE):
        for x in range(MAP_SIZE):
            if pos + 4 > len(data):
                return None

            span_words = data[pos]
            top_start = data[pos + 1]
            top_end = data[pos + 2]
            pos += 4

            if top_end >= top_start:
                return (x, y, top_start)

            if span_words == 0:
                continue

            pos += (span_words - 1) * 4

    return None


class ServerVXL(VXL):
    def __init__(self, state, source, size_or_detail, detail_level=2):
        raw_data = _read_source_bytes(source, size_or_detail)
        self._public_z_shift = 0
        super().__init__(state, source, size_or_detail, detail_level)
        self._public_z_shift = self._detect_public_z_shift(raw_data)

        if self._public_z_shift:
            logger.info(
                "Applying runtime VXL z correction of -%s to %s",
                self._public_z_shift,
                source if isinstance(source, str) else "<bytes>",
            )

    def _detect_public_z_shift(self, raw_data: bytes) -> int:
        if not raw_data:
            return 0

        columns, max_ref = _raw_vxl_size(raw_data)
        edge = math.isqrt(columns) if columns > 0 else 0
        if edge * edge != columns or edge > MAP_SIZE:
            return 0

        candidate = EMPTY_TOP_END - max_ref
        if candidate <= 0:
            return 0

        sample = _find_first_surface_column(raw_data)
        if sample is None:
            return 0

        x, y, surface_z = sample
        shifted_surface = super().get_z(x, y)
        if shifted_surface - surface_z == candidate:
            return candidate
        return 0

    def _to_internal_z(self, z):
        return z + self._public_z_shift

    def _to_public_z(self, z):
        return z - self._public_z_shift

    def get_z(self, x, y, start=0):
        internal_start = self._to_internal_z(start) if self._public_z_shift else start
        result = super().get_z(x, y, internal_start)
        if self._public_z_shift and super().get_solid(x, y, result):
            return self._to_public_z(result)
        return result

    def get_random_pos(self, x1, y1, x2, y2):
        x, y, z = super().get_random_pos(x1, y1, x2, y2)
        if self._public_z_shift and super().get_solid(x, y, z):
            z = self._to_public_z(z)
        return (x, y, z)

    def get_solid(self, x, y, z):
        return super().get_solid(x, y, self._to_internal_z(z))

    def get_color(self, x, y, z):
        return super().get_color(x, y, self._to_internal_z(z))

    def get_color_tuple(self, x, y, z):
        return super().get_color_tuple(x, y, self._to_internal_z(z))

    def get_point(self, x, y, z):
        return super().get_point(x, y, self._to_internal_z(z))

    def has_neighbors(self, x, y, z, check_water):
        return super().has_neighbors(x, y, self._to_internal_z(z), check_water)

    def can_build(self, x, y, z):
        return super().can_build(x, y, self._to_internal_z(z))

    def get_max_modifiable_z(self):
        return self._to_public_z(super().get_max_modifiable_z())

    def add_point(self, x, y, z, color_tuple):
        return super().add_point(x, y, self._to_internal_z(z), color_tuple)

    def set_point(self, x, y, z, color, maybe_color=None):
        return super().set_point(x, y, self._to_internal_z(z), color, maybe_color)

    def remove_point(self, x, y, z):
        return super().remove_point(x, y, self._to_internal_z(z))

    def remove_point_nochecks(self, x, y, z):
        return super().remove_point_nochecks(x, y, self._to_internal_z(z))

    def destroy_point(self, x, y, z):
        return super().destroy_point(x, y, self._to_internal_z(z))

    def color_block(self, x, y, z, color=0xFFFFFF):
        return super().color_block(x, y, self._to_internal_z(z), color)

    def block_line(self, x1, y1, z1, x2, y2, z2):
        points = super().block_line(
            x1,
            y1,
            self._to_internal_z(z1),
            x2,
            y2,
            self._to_internal_z(z2),
        )
        if not self._public_z_shift:
            return points
        return [(x, y, self._to_public_z(z)) for x, y, z in points]

    def raw_surface_z(self, x: int, y: int, raw_data: bytes) -> Optional[int]:
        return _raw_column_surface_z(raw_data, x, y)
