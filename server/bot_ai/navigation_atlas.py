"""Persistent semantic navigation metadata derived from one VXL snapshot.

The atlas complements Recast rather than replacing it. Recast owns detailed
ground corridors and crowd steering; this compact map-wide layer answers the
questions that a local mesh cannot answer cheaply:

* is this column water, ordinary ground, or vertically layered terrain;
* which primary ground component does it belong to;
* is it a narrow main-region passage that construction should preserve; and
* which monotonic water edge leads to the nearest body-clear shore.

The serialized format contains only fixed-width integer arrays, is validated
against a BLAKE2 digest of the source VXL, and never executes cached code.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass
from enum import IntFlag
import hashlib
from pathlib import Path
import struct
import sys
import tempfile
import zlib


MAP_HEIGHT = 240
DEFAULT_MAP_SIZE = 512
DEFAULT_WATER_SUPPORT = 239
NO_SUPPORT = 255
NO_INDEX = -1
_CACHE_MAGIC = b"BSNAV01\0"
_CACHE_VERSION = 2
_CACHE_HEADER = struct.Struct("<8sHHHH16sII")
_CACHE_TRAILER = struct.Struct("<II")
_HEIGHT_MASK = (1 << MAP_HEIGHT) - 1
_LOW_UNSTANDABLE_MASK = (1 << 2) - 1
_BYTES_PER_COLUMN = 18
_MAX_CACHE_PAYLOAD_BYTES = DEFAULT_MAP_SIZE ** 2 * _BYTES_PER_COLUMN
_MAX_CACHE_FILE_BYTES = _MAX_CACHE_PAYLOAD_BYTES + 64 * 1024
_MAX_NATIVE_WATER_BANK_RISE = 1


class ColumnFlag(IntFlag):
    """Map-stable semantic labels for one x/y VXL column."""

    NONE = 0
    WATER = 1 << 0
    DRY = 1 << 1
    LAYERED = 1 << 2
    MAIN_GROUND = 1 << 3
    SHORE = 1 << 4
    CHOKEPOINT = 1 << 5


@dataclass(frozen=True, slots=True)
class NavigationContext:
    """Semantic terrain context at one player support."""

    support_z: int
    primary_support_z: int | None
    layer_count: int
    region_id: int
    clearance: int
    water: bool
    underground: bool
    main_ground: bool
    shore: bool
    chokepoint: bool


@dataclass(frozen=True, slots=True)
class WaterRouteStep:
    """One monotonic step in the map-wide reverse shore flow."""

    next_x: int
    next_y: int
    goal_x: int
    goal_y: int
    goal_support_z: int
    distance: int
    climbable: bool


@dataclass(frozen=True, slots=True)
class NavigationAtlasStats:
    """Summary emitted by offline cache and map-matrix validation."""

    width: int
    height: int
    dry_columns: int
    water_columns: int
    reachable_water_columns: int
    assisted_water_columns: int
    unreachable_water_columns: int
    layered_columns: int
    regions: int
    main_region_columns: int
    chokepoints: int
    maximum_water_distance: int


def source_digest(raw_vxl: bytes) -> bytes:
    """Return the stable 128-bit identity embedded in an atlas cache."""

    return hashlib.blake2s(bytes(raw_vxl), digest_size=16).digest()


def cache_path(map_directory: str | Path, map_name: str) -> Path:
    """Return the portable cache path for one configured VXL map."""

    stem = Path(str(map_name)).name
    if stem.lower().endswith(".vxl"):
        stem = stem[:-4]
    return Path(map_directory) / f"{stem}.botnav"


class NavigationAtlas:
    """Compact map-wide surface, region, passage, and water-flow metadata."""

    __slots__ = (
        "width",
        "height",
        "water_support",
        "primary_support",
        "layer_count",
        "flags",
        "clearance",
        "regions",
        "water_next",
        "water_goal",
        "water_distance",
        "main_region_id",
        "region_count",
    )

    def __init__(
        self,
        width: int,
        height: int,
        water_support: int = DEFAULT_WATER_SUPPORT,
    ) -> None:
        self.width = int(width)
        self.height = int(height)
        self.water_support = int(water_support)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("navigation atlas dimensions must be positive")
        if not 2 <= self.water_support < MAP_HEIGHT:
            raise ValueError("navigation atlas water support is invalid")
        area = self.width * self.height
        self.primary_support = bytearray([NO_SUPPORT]) * area
        self.layer_count = bytearray(area)
        self.flags = bytearray(area)
        self.clearance = bytearray(area)
        self.regions = array("I", [0]) * area
        self.water_next = array("i", [NO_INDEX]) * area
        self.water_goal = array("i", [NO_INDEX]) * area
        self.water_distance = array("H", [0]) * area
        self.main_region_id = 0
        self.region_count = 0

    @classmethod
    def from_vxl(cls, vxl) -> "NavigationAtlas":
        """Build an atlas from a compact worker VXL collision map."""

        masks = getattr(vxl, "iter_column_masks", None)
        if not callable(masks):
            raise TypeError("navigation atlas requires compact VXL column masks")
        return cls.from_masks(
            masks(),
            width=DEFAULT_MAP_SIZE,
            height=DEFAULT_MAP_SIZE,
        )

    @classmethod
    def from_masks(
        cls,
        masks,
        *,
        width: int,
        height: int,
        water_support: int = DEFAULT_WATER_SUPPORT,
    ) -> "NavigationAtlas":
        """Build semantic arrays from row-major 240-bit collision columns."""

        atlas = cls(width, height, water_support)
        values = iter(masks)
        area = atlas.width * atlas.height
        water_bit = 1 << atlas.water_support
        for index in range(area):
            try:
                mask = int(next(values)) & _HEIGHT_MASK
            except StopIteration as exc:
                raise ValueError("navigation atlas received too few columns") from exc
            # A support is standable when it is solid and both cells above it
            # are air. Shifting lower-z occupancy upward aligns those two tests
            # with each potential support bit.
            exposed = (
                mask
                & ~(mask << 1)
                & ~(mask << 2)
                & _HEIGHT_MASK
                & ~_LOW_UNSTANDABLE_MASK
            )
            dry = exposed & ~water_bit
            if dry:
                primary = (dry & -dry).bit_length() - 1
                layers = min(255, int(dry.bit_count()))
                atlas.primary_support[index] = primary
                atlas.layer_count[index] = layers
                atlas.flags[index] = int(
                    ColumnFlag.DRY
                    | (ColumnFlag.LAYERED if layers > 1 else ColumnFlag.NONE)
                )
            elif mask & water_bit:
                atlas.flags[index] = int(ColumnFlag.WATER)
        try:
            next(values)
        except StopIteration:
            pass
        else:
            raise ValueError("navigation atlas received too many columns")

        atlas._build_regions()
        atlas._build_clearance_and_passages()
        atlas._build_water_flow()
        return atlas

    @property
    def area(self) -> int:
        return self.width * self.height

    def _index(self, x: int, y: int) -> int:
        return int(y) * self.width + int(x)

    def _coordinates(self, index: int) -> tuple[int, int]:
        return int(index) % self.width, int(index) // self.width

    def _neighbor_indices(self, index: int):
        x, y = self._coordinates(index)
        if x + 1 < self.width:
            yield index + 1
        if x > 0:
            yield index - 1
        if y + 1 < self.height:
            yield index + self.width
        if y > 0:
            yield index - self.width

    def _dry_connected(self, left: int, right: int) -> bool:
        if not (
            self.flags[left] & int(ColumnFlag.DRY)
            and self.flags[right] & int(ColumnFlag.DRY)
        ):
            return False
        return abs(
            int(self.primary_support[left]) - int(self.primary_support[right])
        ) <= 2

    def _build_regions(self) -> None:
        """Label connected primary ground components using legal step height."""

        region_sizes: list[int] = [0]
        for start in range(self.area):
            if (
                not self.flags[start] & int(ColumnFlag.DRY)
                or self.regions[start] != 0
            ):
                continue
            region_id = len(region_sizes)
            region_size = 0
            frontier = deque((start,))
            self.regions[start] = region_id
            while frontier:
                current = frontier.popleft()
                region_size += 1
                for neighbor in self._neighbor_indices(current):
                    if self.regions[neighbor] != 0:
                        continue
                    if not self._dry_connected(current, neighbor):
                        continue
                    self.regions[neighbor] = region_id
                    frontier.append(neighbor)
            region_sizes.append(region_size)

        self.region_count = len(region_sizes) - 1
        if self.region_count <= 0:
            return
        self.main_region_id = max(
            range(1, len(region_sizes)),
            key=lambda region_id: region_sizes[region_id],
        )
        main_flag = int(ColumnFlag.MAIN_GROUND)
        for index, region_id in enumerate(self.regions):
            if int(region_id) == self.main_region_id:
                self.flags[index] |= main_flag

    def _build_clearance_and_passages(self) -> None:
        """Measure obstacle clearance and label narrow main-region corridors."""

        frontier: deque[int] = deque()
        for index in range(self.area):
            region_id = int(self.regions[index])
            if region_id <= 0:
                continue
            boundary = False
            neighbor_count = 0
            for neighbor in self._neighbor_indices(index):
                if (
                    int(self.regions[neighbor]) == region_id
                    and self._dry_connected(index, neighbor)
                ):
                    neighbor_count += 1
                else:
                    boundary = True
            x, y = self._coordinates(index)
            if x in (0, self.width - 1) or y in (0, self.height - 1):
                boundary = True
            if boundary or neighbor_count < 4:
                self.clearance[index] = 1
                frontier.append(index)

        while frontier:
            current = frontier.popleft()
            next_clearance = min(255, int(self.clearance[current]) + 1)
            region_id = int(self.regions[current])
            for neighbor in self._neighbor_indices(current):
                if (
                    self.clearance[neighbor] != 0
                    or int(self.regions[neighbor]) != region_id
                    or not self._dry_connected(current, neighbor)
                ):
                    continue
                self.clearance[neighbor] = next_clearance
                frontier.append(neighbor)

        choke_flag = int(ColumnFlag.CHOKEPOINT)
        for index in range(self.area):
            if not self.flags[index] & int(ColumnFlag.MAIN_GROUND):
                continue
            degree = sum(
                1
                for neighbor in self._neighbor_indices(index)
                if self._dry_connected(index, neighbor)
                and self.regions[neighbor] == self.regions[index]
            )
            if self.clearance[index] <= 2 and degree <= 2:
                self.flags[index] |= choke_flag

    def _build_water_flow(self) -> None:
        """Create acyclic reverse flows to direct or build-assisted shores.

        A component with a native-swimmable one-voxel exit always prefers it.
        Two-voxel banks are valid ground jumps but are not reliable when the
        native body reaches them after swimming, so they remain assisted exits.
        Components
        enclosed by taller authored banks flow to a bank within the worker's
        bounded climb/build probe where one exists, then to any dry bank as a
        final deterministic hint. The action planner stops at an unwalkable
        final edge so ordinary stuck recovery can build upward; it never
        pretends the cliff itself is a valid jump.
        """

        water_flag = int(ColumnFlag.WATER)
        dry_flag = int(ColumnFlag.DRY)
        shore_flag = int(ColumnFlag.SHORE)

        def seed_and_expand(max_bank_rise: int) -> None:
            frontier: deque[int] = deque()
            for water_index in range(self.area):
                if (
                    not self.flags[water_index] & water_flag
                    or self.water_distance[water_index] != 0
                ):
                    continue
                best_goal = NO_INDEX
                best_delta = MAP_HEIGHT
                for neighbor in self._neighbor_indices(water_index):
                    if not self.flags[neighbor] & dry_flag:
                        continue
                    goal_x, goal_y = self._coordinates(neighbor)
                    # Never teach an interior swimmer to jump outward onto the
                    # clipping boundary. Boundary swimmers may route inward.
                    if goal_x in (0, self.width - 1) or goal_y in (
                        0, self.height - 1
                    ):
                        continue
                    delta = abs(
                        int(self.primary_support[neighbor])
                        - self.water_support
                    )
                    if delta <= max_bank_rise and delta < best_delta:
                        best_goal = neighbor
                        best_delta = delta
                if best_goal == NO_INDEX:
                    continue
                self.water_next[water_index] = best_goal
                self.water_goal[water_index] = best_goal
                self.water_distance[water_index] = 1
                self.flags[best_goal] |= shore_flag
                frontier.append(water_index)

            while frontier:
                current = frontier.popleft()
                distance = int(self.water_distance[current])
                if distance >= 65535:
                    continue
                goal = int(self.water_goal[current])
                for neighbor in self._neighbor_indices(current):
                    if (
                        not self.flags[neighbor] & water_flag
                        or self.water_distance[neighbor] != 0
                    ):
                        continue
                    self.water_next[neighbor] = current
                    self.water_goal[neighbor] = goal
                    self.water_distance[neighbor] = distance + 1
                    frontier.append(neighbor)

        # Direct exits dominate their entire connected water component.
        seed_and_expand(_MAX_NATIVE_WATER_BANK_RISE)
        # A 16-voxel vertical probe is the worker's bounded tower/breach range.
        seed_and_expand(16)
        # Fully enclosed authored basins still receive a deterministic bank
        # instead of accumulating aimless/cyclic movement forever.
        seed_and_expand(MAP_HEIGHT)

    def context(
        self,
        x: int,
        y: int,
        support_z: int,
    ) -> NavigationContext | None:
        """Return semantic context for one current player support."""

        x, y, support_z = int(x), int(y), int(support_z)
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        index = self._index(x, y)
        flags = ColumnFlag(self.flags[index])
        primary_raw = int(self.primary_support[index])
        primary = None if primary_raw == NO_SUPPORT else primary_raw
        layers = int(self.layer_count[index])
        underground = bool(
            layers > 1
            and primary is not None
            and support_z > primary + 1
        )
        return NavigationContext(
            support_z=support_z,
            primary_support_z=primary,
            layer_count=layers,
            region_id=int(self.regions[index]),
            clearance=int(self.clearance[index]),
            water=bool(flags & ColumnFlag.WATER),
            underground=underground,
            main_ground=bool(flags & ColumnFlag.MAIN_GROUND),
            shore=bool(flags & ColumnFlag.SHORE),
            chokepoint=bool(
                flags & ColumnFlag.CHOKEPOINT
                and primary is not None
                and abs(support_z - primary) <= 1
            ),
        )

    def water_route(self, x: int, y: int) -> WaterRouteStep | None:
        """Return the next strictly decreasing water-distance edge."""

        x, y = int(x), int(y)
        if not (0 <= x < self.width and 0 <= y < self.height):
            return None
        index = self._index(x, y)
        distance = int(self.water_distance[index])
        next_index = int(self.water_next[index])
        goal_index = int(self.water_goal[index])
        if distance <= 0 or next_index < 0 or goal_index < 0:
            return None
        next_x, next_y = self._coordinates(next_index)
        goal_x, goal_y = self._coordinates(goal_index)
        goal_support = int(self.primary_support[goal_index])
        if goal_support == NO_SUPPORT:
            return None
        return WaterRouteStep(
            next_x=next_x,
            next_y=next_y,
            goal_x=goal_x,
            goal_y=goal_y,
            goal_support_z=goal_support,
            distance=distance,
            climbable=(
                abs(goal_support - self.water_support)
                <= _MAX_NATIVE_WATER_BANK_RISE
            ),
        )

    def validate(self) -> None:
        """Raise when any cached water edge is cyclic or semantically invalid."""

        water_flag = int(ColumnFlag.WATER)
        dry_flag = int(ColumnFlag.DRY)
        known_flags = sum(int(flag) for flag in ColumnFlag)
        arrays = (
            self.primary_support,
            self.layer_count,
            self.flags,
            self.clearance,
            self.regions,
            self.water_next,
            self.water_goal,
            self.water_distance,
        )
        if any(len(values) != self.area for values in arrays):
            raise ValueError("navigation atlas array length is invalid")
        if not 0 <= self.main_region_id <= self.region_count:
            raise ValueError("navigation atlas main region is invalid")
        for index in range(self.area):
            flags = int(self.flags[index])
            support = int(self.primary_support[index])
            region = int(self.regions[index])
            next_index = int(self.water_next[index])
            goal_index = int(self.water_goal[index])
            distance = int(self.water_distance[index])
            if flags & ~known_flags:
                raise ValueError("navigation atlas contains unknown flags")
            if flags & water_flag and flags & dry_flag:
                raise ValueError("navigation column is both water and dry")
            if support != NO_SUPPORT and not 2 <= support < MAP_HEIGHT:
                raise ValueError("navigation support is out of bounds")
            if not 0 <= region <= self.region_count:
                raise ValueError("navigation region is out of bounds")
            if flags & dry_flag:
                if support == NO_SUPPORT or region <= 0:
                    raise ValueError("dry navigation column has no surface region")
            elif region != 0:
                raise ValueError("non-dry navigation column owns a region")
            if distance <= 0:
                if next_index != NO_INDEX or goal_index != NO_INDEX:
                    raise ValueError("unrouted water metadata is not empty")
                continue
            if not flags & water_flag:
                raise ValueError("non-water column owns a water route")
            if not (0 <= next_index < self.area and 0 <= goal_index < self.area):
                raise ValueError("water route index is out of bounds")
            x, y = self._coordinates(index)
            next_x, next_y = self._coordinates(next_index)
            if abs(next_x - x) + abs(next_y - y) != 1:
                raise ValueError("water route edge is not adjacent")
            if not self.flags[goal_index] & dry_flag:
                raise ValueError("water route goal is not dry ground")
            if distance == 1:
                if next_index != goal_index:
                    raise ValueError("shore water route does not enter its goal")
            elif (
                not self.flags[next_index] & water_flag
                or int(self.water_distance[next_index]) != distance - 1
                or int(self.water_goal[next_index]) != goal_index
            ):
                raise ValueError("water route distance is not strictly decreasing")

    def stats(self) -> NavigationAtlasStats:
        """Return bounded diagnostics without exposing internal arrays."""

        water_flag = int(ColumnFlag.WATER)
        dry_flag = int(ColumnFlag.DRY)
        layered_flag = int(ColumnFlag.LAYERED)
        main_flag = int(ColumnFlag.MAIN_GROUND)
        choke_flag = int(ColumnFlag.CHOKEPOINT)
        water_columns = sum(bool(value & water_flag) for value in self.flags)
        reachable = sum(
            bool(self.flags[index] & water_flag)
            and self.water_distance[index] > 0
            for index in range(self.area)
        )
        assisted = sum(
            bool(self.flags[index] & water_flag)
            and self.water_distance[index] > 0
            and abs(
                int(self.primary_support[int(self.water_goal[index])])
                - self.water_support
            )
            > 2
            for index in range(self.area)
            if int(self.water_goal[index]) >= 0
        )
        return NavigationAtlasStats(
            width=self.width,
            height=self.height,
            dry_columns=sum(bool(value & dry_flag) for value in self.flags),
            water_columns=water_columns,
            reachable_water_columns=reachable,
            assisted_water_columns=assisted,
            unreachable_water_columns=water_columns - reachable,
            layered_columns=sum(
                bool(value & layered_flag) for value in self.flags
            ),
            regions=int(self.region_count),
            main_region_columns=sum(
                bool(value & main_flag) for value in self.flags
            ),
            chokepoints=sum(
                bool(value & choke_flag) for value in self.flags
            ),
            maximum_water_distance=max(self.water_distance, default=0),
        )

    def to_cache_bytes(self, digest: bytes) -> bytes:
        """Serialize the atlas into the validated, compressed cache format."""

        digest = bytes(digest)
        if len(digest) != 16:
            raise ValueError("navigation cache digest must contain 16 bytes")
        self.validate()
        payload = b"".join(
            (
                bytes(self.primary_support),
                bytes(self.layer_count),
                bytes(self.flags),
                bytes(self.clearance),
                self._array_bytes(self.regions),
                self._array_bytes(self.water_next),
                self._array_bytes(self.water_goal),
                self._array_bytes(self.water_distance),
                _CACHE_TRAILER.pack(
                    int(self.main_region_id),
                    int(self.region_count),
                ),
            )
        )
        compressed = zlib.compress(payload, level=6)
        header = _CACHE_HEADER.pack(
            _CACHE_MAGIC,
            _CACHE_VERSION,
            self.width,
            self.height,
            self.water_support,
            digest,
            len(payload),
            len(compressed),
        )
        return header + compressed

    @classmethod
    def from_cache_bytes(
        cls,
        data: bytes,
        *,
        expected_digest: bytes,
    ) -> "NavigationAtlas":
        """Load an atlas after strict magic, size, digest, and flow checks."""

        if (
            len(data) < _CACHE_HEADER.size
            or len(data) > _MAX_CACHE_FILE_BYTES
        ):
            raise ValueError("navigation cache header is truncated")
        (
            magic,
            version,
            width,
            height,
            water_support,
            digest,
            raw_size,
            compressed_size,
        ) = _CACHE_HEADER.unpack_from(data)
        if magic != _CACHE_MAGIC or version != _CACHE_VERSION:
            raise ValueError("navigation cache format is unsupported")
        if digest != bytes(expected_digest):
            raise ValueError("navigation cache does not match the source VXL")
        compressed = data[_CACHE_HEADER.size:]
        if len(compressed) != compressed_size:
            raise ValueError("navigation cache payload size is invalid")
        area = int(width) * int(height)
        expected_size = area * _BYTES_PER_COLUMN + _CACHE_TRAILER.size
        if (
            width <= 0
            or height <= 0
            or width > DEFAULT_MAP_SIZE
            or height > DEFAULT_MAP_SIZE
            or raw_size != expected_size
            or raw_size > _MAX_CACHE_PAYLOAD_BYTES + _CACHE_TRAILER.size
            or compressed_size > _MAX_CACHE_FILE_BYTES - _CACHE_HEADER.size
        ):
            raise ValueError("navigation cache decoded size is invalid")
        try:
            decompressor = zlib.decompressobj()
            payload = decompressor.decompress(compressed, raw_size + 1)
        except zlib.error as exc:
            raise ValueError("navigation cache payload is corrupt") from exc
        if (
            len(payload) != raw_size
            or not decompressor.eof
            or decompressor.unconsumed_tail
            or decompressor.unused_data
        ):
            raise ValueError("navigation cache decoded length is invalid")

        atlas = cls(width, height, water_support)
        offset = 0

        def take(length: int) -> bytes:
            nonlocal offset
            result = payload[offset:offset + length]
            if len(result) != length:
                raise ValueError("navigation cache array is truncated")
            offset += length
            return result

        atlas.primary_support[:] = take(area)
        atlas.layer_count[:] = take(area)
        atlas.flags[:] = take(area)
        atlas.clearance[:] = take(area)
        atlas.regions = cls._array_from_bytes("I", take(area * 4))
        atlas.water_next = cls._array_from_bytes("i", take(area * 4))
        atlas.water_goal = cls._array_from_bytes("i", take(area * 4))
        atlas.water_distance = cls._array_from_bytes("H", take(area * 2))
        atlas.main_region_id, atlas.region_count = _CACHE_TRAILER.unpack(
            take(_CACHE_TRAILER.size)
        )
        if offset != len(payload):
            raise ValueError("navigation cache has trailing data")
        atlas.validate()
        return atlas

    def write_cache(self, path: str | Path, digest: bytes) -> Path:
        """Atomically write one atlas cache and return its resolved path."""

        destination = Path(path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = self.to_cache_bytes(digest)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                stream.write(encoded)
                stream.flush()
                temporary_path = Path(stream.name)
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return destination

    @staticmethod
    def _array_bytes(values: array) -> bytes:
        copy = array(values.typecode, values)
        if sys.byteorder != "little":
            copy.byteswap()
        return copy.tobytes()

    @staticmethod
    def _array_from_bytes(typecode: str, data: bytes) -> array:
        values = array(typecode)
        values.frombytes(data)
        if sys.byteorder != "little":
            values.byteswap()
        return values


def load_or_build_atlas(
    vxl,
    raw_vxl: bytes,
    *,
    map_name: str = "",
    map_directory: str = "",
) -> tuple[NavigationAtlas, bool]:
    """Load a matching disk cache or build an atlas in memory.

    Returns:
        A tuple ``(atlas, cache_hit)``. Cache read failures are deliberately
        non-fatal; callers retain a complete in-memory navigation system.
    """

    digest = source_digest(raw_vxl)
    if map_name and map_directory:
        candidate = cache_path(map_directory, map_name)
        try:
            if candidate.stat().st_size > _MAX_CACHE_FILE_BYTES:
                raise ValueError("navigation cache file is oversized")
            atlas = NavigationAtlas.from_cache_bytes(
                candidate.read_bytes(),
                expected_digest=digest,
            )
        except (OSError, ValueError):
            pass
        else:
            return atlas, True
    return NavigationAtlas.from_vxl(vxl), False
