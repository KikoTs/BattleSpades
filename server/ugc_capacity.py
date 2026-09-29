"""Retail Map Creator voxel-capacity model (``BLOCK_PLACE_UGC_CAPACITY``).

Recovered from the stock client (headless idalib, 2026-09-29):

* ``aoslib.vxl.VXL.is_space_to_add_blocks`` (vxl.pyd 0x10019c00) returns
  false only when the solid counter at ``+0x3EB34CC`` is at least 2,800,000
  **and** the chunk counter at ``+0x3EB34D0`` is at least 3,200.
* The recount (0x10005600) counts every non-zero byte of the 512x512x240
  solid grid (implicit interior included) and every non-empty 16x16x16 chunk
  of the 15,360 (``sub_10003C80``).  ``set_point`` (0x10029da0) keeps both
  current through ``sub_10003D10``.
* gameScene.pyd 0x10075490 consults it only when ``is_in_ugc_mode()``, so the
  gate exists in the Map Creator alone (Alcatraz holds 16.8 M solids).

The server mirrors the two counters for the isolated UGC runtime.  One
240-bit integer per column is enough to learn a cell's previous state, which
the canonical mutation feed does not carry (a recolour republishes
``solid=True``).
"""

from __future__ import annotations

from array import array

WORLD_XY = 512
GRID_Z = 240
CHUNK = 16
CHUNKS_Z = GRID_Z // CHUNK
CHUNK_COUNT = (WORLD_XY // CHUNK) * (WORLD_XY // CHUNK) * CHUNKS_Z  # 15,360
SOLID_LIMIT = 2_800_000
CHUNK_LIMIT = 3_200
_LAYER_MASK = (1 << CHUNK) - 1
_COLUMN_MASK = (1 << GRID_Z) - 1


def chunk_index(x: int, y: int, z: int) -> int:
    """Return the retail chunk index ``(x>>4) + 32*((y>>4) + 32*(z>>4))``."""

    return (x >> 4) + 32 * ((y >> 4) + 32 * (z >> 4))


def parse_column_masks(raw: bytes) -> list[int]:
    """Decode an AoS VXL into one solid bit-mask per column (y-major order).

    A column's solid run starts at a span's top colour ``S`` and continues
    down to the next span's air start ``A`` (or the grid floor for the last
    span); implicit interior voxels are solid exactly as in the client grid.
    """

    data = memoryview(raw)
    columns: list[int] = []
    position = 0
    limit = len(data)
    max_ref = 0
    wide = (1 << 256) - 1
    while position < limit:
        mask = 0
        has_surface = False
        while True:
            if position + 4 > limit:
                raise ValueError("truncated VXL column data")
            length = data[position]
            top = data[position + 1]
            end = data[position + 2]
            max_ref = max(max_ref, top, end, data[position + 3])
            has_surface = has_surface or end >= top
            if length == 0:
                # Last span: the ground below the final surface run is solid
                # to the floor, but only for a column that has a land surface
                # (open water is an empty column above the bed).
                if end >= top:
                    position += 4 * (end - top + 2)
                else:
                    position += 4
                if has_surface:
                    start = top if end >= top else end + 1
                    mask |= wide & ~((1 << start) - 1)
                break
            next_position = position + length * 4
            if next_position + 4 > limit:
                raise ValueError("truncated VXL span")
            air_start = data[next_position + 3]
            if air_start > top:
                mask |= ((1 << (air_start - top)) - 1) << top
            position = next_position
        columns.append(mask)
    edge = int(round(len(columns) ** 0.5))
    if edge * edge != len(columns) or edge > WORLD_XY or max_ref >= 241:
        raise ValueError("unsupported VXL dimensions")
    # Same normalisation as aoslib.vxl: short legacy maps are centred and the
    # deepest referenced z becomes the fixed z=239 bed.
    offset = (WORLD_XY - edge) // 2
    shift = max(0, (GRID_Z - 1) - max_ref)
    masks: list[int] = [0] * (WORLD_XY * WORLD_XY)
    for index, mask in enumerate(columns):
        x = index % edge + offset
        y = index // edge + offset
        masks[x + y * WORLD_XY] = (mask << shift) & _COLUMN_MASK
    return masks


class UGCCapacity:
    """Solid and chunk counters with O(1) updates from canonical mutations."""

    __slots__ = ("_masks", "_chunks", "solid_count", "chunk_count")

    def __init__(self, masks: list[int]) -> None:
        if len(masks) != WORLD_XY * WORLD_XY:
            raise ValueError("capacity model needs 512x512 columns")
        self._masks = masks
        chunks = array("H", bytes(2 * CHUNK_COUNT))
        solid = 0
        for column, mask in enumerate(masks):
            if not mask:
                continue
            solid += mask.bit_count()
            x = column % WORLD_XY
            y = column // WORLD_XY
            base = (x >> 4) + 32 * (y >> 4)
            for layer in range(CHUNKS_Z):
                bits = (mask >> (layer * CHUNK)) & _LAYER_MASK
                if bits:
                    chunks[base + 1024 * layer] += bits.bit_count()
        self._chunks = chunks
        self.solid_count = solid
        self.chunk_count = sum(1 for value in chunks if value)

    @classmethod
    def from_vxl(cls, raw: bytes) -> "UGCCapacity":
        return cls(parse_column_masks(raw))

    def is_solid(self, x: int, y: int, z: int) -> bool:
        if not (0 <= x < WORLD_XY and 0 <= y < WORLD_XY and 0 <= z < GRID_Z):
            return False
        return bool((self._masks[x + y * WORLD_XY] >> z) & 1)

    def apply(self, x: int, y: int, z: int, solid: bool) -> None:
        """Record one committed cell state (idempotent for repeats/recolours)."""

        if not (0 <= x < WORLD_XY and 0 <= y < WORLD_XY and 0 <= z < GRID_Z):
            return
        column = x + y * WORLD_XY
        bit = 1 << z
        mask = self._masks[column]
        was = bool(mask & bit)
        if was == bool(solid):
            return
        index = chunk_index(x, y, z)
        if solid:
            self._masks[column] = mask | bit
            self.solid_count += 1
            if self._chunks[index] == 0:
                self.chunk_count += 1
            self._chunks[index] += 1
        else:
            self._masks[column] = mask & ~bit
            self.solid_count -= 1
            self._chunks[index] -= 1
            if self._chunks[index] == 0:
                self.chunk_count -= 1

    def has_space(self) -> bool:
        """Retail ``is_space_to_add_blocks``: full only when BOTH limits hit."""

        return not (
            self.solid_count >= SOLID_LIMIT and self.chunk_count >= CHUNK_LIMIT
        )


__all__ = [
    "CHUNK_COUNT",
    "CHUNK_LIMIT",
    "SOLID_LIMIT",
    "UGCCapacity",
    "chunk_index",
    "parse_column_masks",
    "ugc_capacity_full",
]


def ugc_capacity_full(server) -> bool:
    """True only in the Map Creator runtime while the retail cap is reached."""

    config = getattr(server, "config", None)
    if not bool(getattr(config, "ugc_runtime", False)):
        return False
    has_space = getattr(getattr(server, "mode", None), "has_block_space", None)
    return callable(has_space) and not bool(has_space())
