"""Server-facing VXL with retail vertical-normalization metadata."""

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


def _is_retail_marker_color(color: int) -> bool:
    """Return whether a voxel belongs to a native chroma-key colour family.

    Native ``vxl.pyd`` cleanup ``sub_10029FD0`` masks the RGB word with
    ``0x00F0F0F0`` and recognises the blue and green families below. Colour is
    only the first half of the rule: the caller must also require two air
    voxels immediately above the candidate before removing it.
    """
    masked = int(color) & 0x00F0F0F0
    return masked in (0x000000F0, 0x0000F000)


def _iter_explicit_voxels(data: bytes):
    """Yield ``(x, y, z, color)`` for explicit VXL surface words.

    VXL columns can contain several spans. Non-final spans store a top colour
    run and a bottom colour run around an implicit solid interior; the next
    header's ``air_start`` locates that bottom run. This walker is used only at
    load time and validates every offset before yielding anything past it.
    """
    columns, _max_ref = _raw_vxl_size(data)
    edge = math.isqrt(columns) if columns > 0 else 0
    if edge <= 0 or edge * edge != columns or edge > MAP_SIZE:
        return
    offset = (MAP_SIZE - edge) // 2
    pos = 0
    limit = len(data)
    for source_y in range(edge):
        y = source_y + offset
        for source_x in range(edge):
            x = source_x + offset
            while True:
                if pos + 4 > limit:
                    return
                span_start = pos
                span_words = data[pos]
                top_start = data[pos + 1]
                top_end = data[pos + 2]
                top_len = top_end - top_start + 1 if top_end >= top_start else 0
                pos += 4
                if pos + top_len * 4 > limit:
                    return
                for index in range(top_len):
                    color = int.from_bytes(
                        data[pos + index * 4:pos + index * 4 + 4],
                        "little",
                    )
                    yield x, y, top_start + index, color
                pos += top_len * 4
                if span_words == 0:
                    break

                bottom_len = span_words - top_len - 1
                next_header = span_start + span_words * 4
                if (
                    bottom_len < 0
                    or next_header + 4 > limit
                    or pos + bottom_len * 4 != next_header
                ):
                    return
                next_air_start = data[next_header + 3]
                bottom_start = next_air_start - bottom_len
                if bottom_start < top_end + 1:
                    return
                for index in range(bottom_len):
                    color = int.from_bytes(
                        data[pos + index * 4:pos + index * 4 + 4],
                        "little",
                    )
                    yield x, y, bottom_start + index, color
                pos = next_header


def _walk_raw_columns(data: bytes):
    """Yield (x, y, surface_z_or_None) for every column of the raw file.

    A column is a SEQUENCE of span records terminated by span_words == 0;
    walking only the first record per column drifts out of sync on
    multi-span columns (overhangs/buildings) and attributes data to the
    wrong (x, y).
    """
    pos = 0
    limit = len(data)
    for y in range(MAP_SIZE):
        for x in range(MAP_SIZE):
            surface = None
            while True:
                if pos + 4 > limit:
                    return
                span_words = data[pos]
                top_start = data[pos + 1]
                top_end = data[pos + 2]
                if surface is None and top_end >= top_start:
                    surface = top_start
                if span_words == 0:
                    top_len = top_end - top_start + 1 if top_end >= top_start else 0
                    pos += 4 + top_len * 4
                    break
                pos += span_words * 4
            yield (x, y, surface)


def _raw_column_surface_z(data: bytes, target_x: int, target_y: int) -> Optional[int]:
    for x, y, surface in _walk_raw_columns(data):
        if x == target_x and y == target_y:
            return surface
    return None


def _find_first_surface_column(data: bytes) -> tuple[int, int, int] | None:
    for x, y, surface in _walk_raw_columns(data):
        if surface is not None:
            return (x, y, surface)
    return None


class ServerVXL(VXL):
    def __init__(self, state, source, size_or_detail, detail_level=2):
        raw_data = _read_source_bytes(source, size_or_detail)
        columns, max_ref = _raw_vxl_size(raw_data) if raw_data else (0, EMPTY_TOP_END)
        edge = math.isqrt(columns) if columns > 0 else 0
        self.source_z_shift = (
            max(0, EMPTY_TOP_END - max_ref)
            if edge * edge == columns and edge <= MAP_SIZE else 0
        )
        super().__init__(state, source, size_or_detail, detail_level)
        self.retail_marker_positions = ()
        # Battle Builder maps can embed blue/green chroma-key voxels as ordinary
        # VXL words. The compiled client deletes an eligible voxel only when it
        # and both immediately higher cells form an exposed marker. Retaining
        # those points server-side creates invisible one-block steps and
        # deterministic reconciliation corrections. Snapshot every candidate
        # before removal so a mutation cannot make the next point appear newly
        # exposed during this pass.
        if raw_data:
            markers = tuple(
                (x, y, z + self.source_z_shift)
                for x, y, z, color in _iter_explicit_voxels(raw_data)
                if _is_retail_marker_color(color)
                and self.get_solid(x, y, z + self.source_z_shift)
                and not self.get_solid(x, y, z + self.source_z_shift - 1)
                and not self.get_solid(x, y, z + self.source_z_shift - 2)
            )
            for x, y, z in markers:
                self.remove_point_nochecks(x, y, z)
            self.retail_marker_positions = markers
            if markers:
                logger.info(
                    "Removed %s retail marker voxels from server collision map",
                    len(markers),
                )
        if self.source_z_shift:
            logger.info(
                "Loaded VXL with retail z normalization +%s: %s",
                self.source_z_shift,
                source if isinstance(source, str) else "<bytes>",
            )

    def raw_surface_z(self, x: int, y: int, raw_data: bytes) -> Optional[int]:
        return _raw_column_surface_z(raw_data, x, y)
