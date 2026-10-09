"""
World Manager - handles map state and operations.
"""

import asyncio
import logging
import math
import os
import random
import struct
import threading
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from time import perf_counter as _scan_clock, sleep as _scan_yield
from typing import Callable, Optional, Tuple

import shared.constants as C
from aoslib.world import World
from server.game_constants import (
    DEFAULT_BLOCK_HEALTH,
    PLAYER_HEIGHT,
    PLAYER_STANDING_POS_ABOVE_GROUND,
    TEAM1,
    TEAM2,
    WATER_LEVEL,
)
from server.block_damage_model import MAX_DAMAGEABLE_Z, dim_rgb
from server.map_metadata import MapMetadata, MapZone, load_map_metadata
from server.runtime_vxl import ServerVXL as VXL

logger = logging.getLogger(__name__)

MAP_X = int(C.MAP_X)
MAP_Y = int(C.MAP_Y)
MAP_Z = int(C.MAP_Z)


def _spawn_scan_columns(
    x_start: int, x_stop: int, y_start: int, y_stop: int, step: int = 1,
) -> Iterator[tuple[int, int]]:
    """Keep background spawn discovery from starving the live event loop.

    Repeated short Python/native probes can reacquire the GIL for tens of
    milliseconds on macOS despite the parser releasing it. Yield explicitly
    every two milliseconds on a worker, preserving coordinate order. Startup
    and any live main-thread caller retain the ordinary synchronous scan.
    """
    background = threading.current_thread() is not threading.main_thread()
    deadline = _scan_clock() + 0.002 if background else 0.0
    for x in range(x_start, x_stop, step):
        for y in range(y_start, y_stop, step):
            yield x, y
            if background and _scan_clock() >= deadline:
                _scan_yield(0.001)
                deadline = _scan_clock() + 0.002


# Authored spawn areas whose retail spawns stood in the sea. Retail dropped
# SpookyMansion's zombies into the z=239 water ring around the island; every
# other spawn keeps rejecting open water.
WATER_SPAWN_ITEMS = frozenset(("zombie_spawn_area",))


def _shift_vxl_spans(data: bytes, z_shift: int) -> bytes:
    """Translate one column's compact VXL span headers into client-world Z."""
    if not z_shift:
        return bytes(data)
    out = bytearray(data)
    pos = 0
    limit = len(out)
    while pos < limit:
        if pos + 4 > limit:
            raise ValueError("truncated VXL span header")
        span_words = out[pos]
        top_start = out[pos + 1]
        top_end = out[pos + 2]
        previous_air = out[pos + 3]
        shifted_start = top_start + z_shift
        shifted_end = top_end + z_shift
        if shifted_start > 255 or shifted_end > 255:
            raise ValueError("shifted VXL coordinate exceeds byte range")
        out[pos + 1] = shifted_start
        out[pos + 2] = shifted_end
        if previous_air:
            shifted_air = previous_air + z_shift
            if shifted_air > 255:
                raise ValueError("shifted VXL air coordinate exceeds byte range")
            out[pos + 3] = shifted_air
        top_len = top_end - top_start + 1 if top_end >= top_start else 0
        if span_words == 0:
            pos += 4 + top_len * 4
            break
        pos += span_words * 4
    if pos != limit:
        raise ValueError("invalid VXL span length")
    return bytes(out)


def _shift_sync_records(data: bytes, z_shift: int) -> bytes:
    """Translate `(u32 x,u32 y,column)` records without changing their size."""
    if not z_shift:
        return bytes(data)
    out = bytearray()
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError("truncated map-sync coordinate")
        out.extend(data[pos:pos + 8])
        pos += 8
        start = pos
        while True:
            if pos + 4 > len(data):
                raise ValueError("truncated map-sync span")
            span_words = data[pos]
            top_start, top_end = data[pos + 1], data[pos + 2]
            top_len = top_end - top_start + 1 if top_end >= top_start else 0
            if span_words == 0:
                pos += 4 + top_len * 4
                break
            pos += span_words * 4
        out.extend(_shift_vxl_spans(data[start:pos], z_shift))
    return bytes(out)


@dataclass(frozen=True)
class MapSyncSnapshot:
    """Immutable map-transfer work; native VXL access happens before capture."""

    raw: bytes | None
    overlay_data: bytes
    z_shift: int
    map_name: str
    revision: int
    full: bool
    columns: frozenset[tuple[int, int]]
    cached_chunks: tuple[bytes, ...] | None = None

    def build_chunks(self) -> list[bytes] | None:
        """Wrap/compress bytes only, safe to call from the map-sync worker."""
        if self.cached_chunks is not None:
            return list(self.cached_chunks)
        if not self.full:
            if not self.overlay_data:
                return []
            compressed = zlib.compress(self.overlay_data, 6)
            return [compressed[i:i + 1024] for i in range(0, len(compressed), 1024)]
        raw = self.raw
        if not raw:
            return None
        MAP_SIZE = 512
        MAP_PACKET_SIZE = 1024  # matches aoslib/vxl.pyx DEF MAP_PACKET_SIZE
        n = len(raw)
        out = bytearray()
        overlays = {}
        z_shift = self.z_shift
        if self.overlay_data:
            overlay_data = self.overlay_data
            overlay_pos = 0
            while overlay_pos < len(overlay_data):
                record_start = overlay_pos
                if overlay_pos + 8 > len(overlay_data):
                    logger.warning("Truncated dirty-column coordinate record")
                    return None
                ox, oy = struct.unpack_from("<II", overlay_data, overlay_pos)
                overlay_pos += 8
                while True:
                    if overlay_pos + 4 > len(overlay_data):
                        logger.warning("Truncated dirty-column span record")
                        return None
                    span_words = overlay_data[overlay_pos]
                    top_start = overlay_data[overlay_pos + 1]
                    top_end = overlay_data[overlay_pos + 2]
                    top_len = (
                        top_end - top_start + 1
                        if top_end >= top_start else 0
                    )
                    if span_words == 0:
                        record_size = 4 + top_len * 4
                        if overlay_pos + record_size > len(overlay_data):
                            logger.warning("Truncated dirty-column final span")
                            return None
                        overlay_pos += record_size
                        break
                    record_size = span_words * 4
                    if overlay_pos + record_size > len(overlay_data):
                        logger.warning("Truncated dirty-column span payload")
                        return None
                    overlay_pos += record_size
                overlays[(ox, oy)] = overlay_data[record_start:overlay_pos]
        pos = 0
        for y in range(MAP_SIZE):
            for x in range(MAP_SIZE):
                start = pos
                # Walk this column's span list to its terminating span.
                while True:
                    if pos + 4 > n:
                        logger.warning("Map walker overran %s at col (%d,%d) — "
                                       "falling back to chunker", self.map_name, x, y)
                        return None
                    span_words = raw[pos]
                    top_start = raw[pos + 1]
                    top_end = raw[pos + 2]
                    top_len = (top_end - top_start + 1) if top_end >= top_start else 0
                    if span_words == 0:
                        pos += 4 + top_len * 4   # header + top-run colours; last span
                        break
                    pos += span_words * 4        # whole span is span_words 4-byte words
                current = overlays.pop((x, y), None)
                if current is not None:
                    out += current
                else:
                    out += struct.pack("<II", x, y)
                    out += _shift_vxl_spans(raw[start:pos], z_shift)
        if pos != n:
            logger.warning("Map walker consumed %d/%d bytes of %s — falling back "
                           "to chunker", pos, n, self.map_name)
            return None
        if overlays:
            logger.warning(
                "Dirty-column overlay contained out-of-map coordinates: %s",
                sorted(overlays)[:8],
            )
            return None
        compressed = zlib.compress(bytes(out), 6)
        return [
            compressed[i:i + MAP_PACKET_SIZE]
            for i in range(0, len(compressed), MAP_PACKET_SIZE)
        ]


class WorldManager:
    """
    Manages the game world (map) state.
    Provides interface to aoslib.vxl.VXL and aoslib.world.World.
    """
    
    def __init__(self, config):
        self.config = config
        self.map: Optional[VXL] = None
        self.world: Optional[World] = None
        self.map_name = ""
        self.maps_path = config.maps_path if hasattr(config, 'maps_path') else "maps"
        self.block_damage: dict[tuple[int, int, int], float] = {}
        # Initial health of cells that do NOT use the map default
        # (DEFAULT_BLOCK_HEALTH).  The retail client stores these in
        # BlockManager.user_blocks: a BuildPrefabAction(30) cell is added with
        # DEFAULT_PREFAB_HEALTH (9), measured live 2026-09-26.  Absent cells
        # break at the caller's threshold.  Cleared whenever the cell changes
        # topology; a recolour of an existing solid keeps it.
        self.block_health: dict[tuple[int, int, int], float] = {}
        # Retail FlareBlockEntity restores of stripped marker cells -> RGB.
        self.static_light_cells: dict[tuple[int, int, int], int] = {}
        # What live clients DISPLAY for a damaged cell.  The stock
        # ``BlockManager.add_damage`` darkens the voxel's current colour with
        # ``shared.common.dim`` on every hit, so the shade compounds per hit,
        # while a BlockManagerState(38) damaged row darkens its original
        # colour once by the total damage (IDA, 2026-09-26).  Late joiners
        # therefore need the per-hit result explicitly:
        #
        # * ``block_shade``: explicit-colour cells -> packed
        #   ``(original_rgb << 24) | shown_rgb``; ``original`` is the colour
        #   the client's DamagedBlock recorded at the first hit, ``shown`` the
        #   compounded (or painted-over) colour.
        # * ``block_hits``: cells whose colour the CLIENT owns at the first
        #   hit (implicit interior voxels, and pure black voxels, which
        #   ``add_damage`` re-colours through the native ``map.color_block``)
        #   -> the per-hit amounts in quarter units, replayed to joiners as
        #   the same single-cell Damage packets.  ``None`` = history lost
        #   (too long / off-grid amount), falling back to a health-only row.
        #
        # Both only exist while the cell has an entry in ``block_damage``.
        self.block_shade: dict[tuple[int, int, int], int] = {}
        self.block_hits: dict[tuple[int, int, int], Optional[bytes]] = {}
        # CRC32 of the raw .vxl bytes this world was loaded from. The client
        # compares InitialInfo.checksum / our MapDataValidation reply against
        # the CRC of its local copy of `filename` to decide whether its local
        # file is a valid world base (measured: London.vxl crc32 == 592649088,
        # the value the original server declared for London).
        self.map_file_crc: int = 0
        # Raw bytes of the .vxl this world was loaded from. Streamed verbatim
        # for the full MapSync so the client rebuilds the map in its native
        # implicit-underground encoding (identical to its own local copy) —
        # re-serializing our in-memory grid instead writes every filled
        # underground voxel explicitly, bloating a 3 MB map into a 36 MB
        # stream the strict client rejects (Steam-client join crash, 2026-07-09).
        self.map_raw_bytes: bytes | None = None
        # Cached full-sync chunk list (raw column spans wrapped as (x,y,spans)
        # records, zlib-compressed, sliced into 1 KB packets). Built lazily on
        # first join, invalidated when a new map loads.
        self._full_sync_chunks: list[bytes] | None = None
        # One immutable transfer job at a time bounds compression CPU/memory.
        # Keep only the last topology revision, in addition to the pristine map.
        self.map_sync_lock = asyncio.Lock()
        self._prepared_sync_key: tuple[object, ...] | None = None
        self._prepared_sync_chunks: tuple[bytes, ...] | None = None
        # Columns modified since map load — what a matched-CRC client is
        # missing relative to its local file. Sent as the MapSync delta.
        self.dirty_columns: set[tuple[int, int]] = set()
        # Exact air overrides accumulated since map load, packed as one
        # 240-bit mask per (x, y) column. MapSync remains the primary bulk
        # transfer, but the retail VXL worker can occasionally retain stale
        # native collision/mesh state after a heavily drilled column merge.
        # A late joiner replays these cells after entering GameScene. Bitmasks
        # avoid retaining one Python tuple per destroyed voxel.
        self._air_override_masks: dict[tuple[int, int], int] = {}
        self.map_metadata = MapMetadata()
        self._surface_cache: dict[tuple[int, int], int] = {}
        # (x, y) top-surface columns and, inside authored boxes only,
        # (x, y, floor_z) storeys/sea spawns -- see ``_zone_candidates``.
        self._spawn_candidates: dict[int, list[tuple[int, ...]]] = {
            TEAM1: [], TEAM2: []
        }
        # Team anchors describe authored/fallback map locations, not moving
        # gameplay state. Bot perception reads them many times per second, so
        # resolving large authored zones on every frame would put a full spawn
        # scan back on the authoritative 60 Hz thread.
        self._team_base_anchors: dict[int, tuple[float, float, float]] = {}
        # Canonical terrain-version stream consumed by off-thread navigation.
        # Listeners receive only primitive values and must remain non-blocking.
        self.topology_version: int = 0
        self._mutation_listeners: dict[
            int, Callable[[int, int, int, bool, int, int], None]
        ] = {}
        self._next_mutation_listener_id: int = 1
    def load_map(self, name: str) -> bool:
        """Load a VXL map file."""
        # Try with and without extension
        map_path = os.path.join(self.maps_path, name)
        if not map_path.lower().endswith('.vxl'):
            map_path += '.vxl'
        
        if not os.path.exists(map_path):
            logger.warning(f"Map not found: {map_path}")
            logger.info("Generating flat map...")
            self.generate_flat_map()
            self.map_name = "flat"
            return True
        
        try:
            with open(map_path, 'rb') as handle:
                raw = handle.read()
            if not raw:
                raise ValueError("Empty VXL map file")
            metadata = load_map_metadata(
                map_path, str(getattr(self.config, "game_mode", "nor"))
            )
            # Validate the new map completely before replacing the live one.
            loaded_map = VXL(1, raw, len(raw), 3, source_format=metadata.vxl_format)
            if not loaded_map.ready:
                raise ValueError("Invalid VXL column stream")
            self.map = loaded_map
            self.map_name = name[:-4] if name.lower().endswith('.vxl') else name
            self.map_raw_bytes = raw
            self._full_sync_chunks = None
            self._prepared_sync_key = None
            self._prepared_sync_chunks = None
            self.map_file_crc = zlib.crc32(raw) & 0xFFFFFFFF
            self.dirty_columns = set()
            self._air_override_masks = {}
            self.block_damage = {}
            self.block_health = {}
            self.static_light_cells = {}
            self.block_shade = {}
            self.block_hits = {}
            self.topology_version = 0
            self._surface_cache.clear()
            self._spawn_candidates = {TEAM1: [], TEAM2: []}
            self._team_base_anchors.clear()
            self.map_metadata = metadata
            self._refresh_world()
            # Candidate discovery performs thousands of terrain probes on
            # voxel-only maps. Do it while the map is loading (startup or the
            # transition service's worker thread), never on the first live
            # respawn tick. Zombie Patient Zero is often the first TEAM2 body
            # and previously paid a visible ~268 ms lazy-cache hitch.
            self.prewarm_spawn_candidates()
            logger.info(
                f"Loaded map: {self.map_name} (file crc32={self.map_file_crc})"
            )
            return True

        except Exception as e:
            logger.error(f"Error loading map {name}: {e}", exc_info=True)
            return False
    
    def generate_flat_map(self):
        """Generate a simple flat map."""
        self.map = VXL(-1, b"", 0, 2)
        
        # A deliberately high, dry debug plateau.  The retail waterplane is
        # z=238; z=62 is therefore far above water, not adjacent to it as the
        # old 64-high-world comment incorrectly claimed.
        ground_z = 62
        
        for x in range(MAP_X):
            for y in range(MAP_Y):
                color = 0x7F008F00  # Green grass
                self.map.set_point(x, y, ground_z, color)

        # No file backs a generated map; the CRC (and the full-sync bytes) come
        # from its byte-faithful serialized form.
        raw = self.map.generate_vxl()
        self.map_raw_bytes = raw
        self._full_sync_chunks = None
        self._prepared_sync_key = None
        self._prepared_sync_chunks = None
        self.map_file_crc = zlib.crc32(raw) & 0xFFFFFFFF
        self.dirty_columns = set()
        self._air_override_masks = {}
        self.block_damage = {}
        self.block_health = {}
        self.static_light_cells = {}
        self.block_shade = {}
        self.block_hits = {}
        self.topology_version = 0
        self._surface_cache.clear()
        self._spawn_candidates = {TEAM1: [], TEAM2: []}
        self._team_base_anchors.clear()
        self.map_metadata = MapMetadata()
        self._refresh_world()
        self.prewarm_spawn_candidates()
        logger.info("Generated flat map")

    def _refresh_world(self):
        if self.map is None:
            self.world = None
            return
        self.world = World(self.map)
        # StateData and authoritative physics must consume one canonical map
        # scalar.  This matters on LunarBase and for every jetpack recurrence,
        # whose thrust formula includes the global gravity value.
        self.world.set_gravity(float(self.map_metadata.gravity))
    
    def get_solid(self, x: int, y: int, z: int) -> bool:
        """Check if block at position is solid."""
        if self.map is None:
            return False
        if not (0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z):
            return False
        return bool(self.map.get_solid(x, y, z))
    
    def get_color(self, x: int, y: int, z: int) -> int:
        """Get color at position."""
        if self.map is None:
            return 0
        return self.map.get_color(x, y, z)
    
    def can_build(self, x: int, y: int, z: int) -> bool:
        """Check if building is allowed at position."""
        if self.map is None:
            return False
        return self.map.can_build(x, y, z)
    
    @staticmethod
    def _canonical_vxl_color(color) -> int:
        """Pack runtime RGB as an opaque Battle Builder VXL colour."""
        if isinstance(color, (tuple, list)):
            r, g, b = (int(component) & 0xFF for component in color[:3])
            rgb = (r << 16) | (g << 8) | b
        else:
            rgb = int(color) & 0xFFFFFF
        # Dynamic blocks use the codec's RGBA alpha encoding: byte 0x80
        # decodes to client alpha 255. This is also what tuple-coloured
        # prefabs already use, so live and rejoined blocks shade identically.
        return 0x80000000 | rgb

    def user_block_health(self, health: float) -> float | None:
        """Apply retail ``BlockManager.add_user_block``'s mode rules.

        Stock gameScene ``add_user_block`` (IDA 0x10070530): in the UGC Map
        Creator the cell is popped from ``user_blocks`` (untracked, so it
        breaks at the map default); in Classic every user block is stored at
        DEFAULT_BLOCK_HEALTH (5) whatever the caller asked for (build 9,
        prefab 9, BlockBuildColored 3).  ``None`` = no user-block entry.
        """

        config = self.config
        if bool(getattr(config, "ugc_runtime", False)):
            return None
        try:
            from server import mode_data

            classic = bool(mode_data.get(config.game_mode).classic)
        except (AttributeError, TypeError, ValueError):
            classic = False
        if classic:
            return float(DEFAULT_BLOCK_HEALTH)
        return float(health)

    def set_block(
        self,
        x: int,
        y: int,
        z: int,
        solid: bool,
        color: int = 0,
        health: float | None = None,
    ) -> bool:
        """Set one block and publish the committed canonical mutation.

        ``health`` records a non-default initial block health (retail
        ``add_user_block`` health, e.g. DEFAULT_PREFAB_HEALTH for prefab
        cells).  Without it, a newly solid cell uses the damage caller's
        default threshold, while re-colouring an already solid cell (paint)
        keeps its recorded health like the client's ``color_block``.
        """
        x, y, z = int(x), int(y), int(z)
        if self.map is None or not self._valid_block_position(x, y, z):
            return False
        position = (x, y, z)
        user_block = health is not None
        if user_block:
            health = self.user_block_health(health)
        # A recolour of an existing solid (paint) keeps the cell's health AND
        # its accumulated damage, exactly like the client's ``color_block``
        # (live 2026-09-26: PaintBlock on a damaged cell left
        # DamagedBlock.health unchanged).  Anything that creates or replaces a
        # voxel starts it undamaged.
        recolour = bool(solid) and not user_block and self.get_solid(x, y, z)
        if health is not None and float(health) > 0.0:
            self.block_health[position] = float(health)
        elif not recolour:
            self.block_health.pop(position, None)
        if recolour and position in self.block_damage:
            self._repaint_damaged(
                position, self._canonical_vxl_color(color) & 0xFFFFFF
            )
        if solid:
            self.map.set_point(x, y, z, self._canonical_vxl_color(color))
            self._set_air_override(x, y, z, False)
        else:
            self.map.remove_point(x, y, z)
            self._set_air_override(x, y, z, True)
            self._materialize_exposed_surfaces(((x, y, z),))
        self._surface_cache.pop((x, y), None)
        self.dirty_columns.add((x, y))
        if not recolour:
            self.clear_block_damage(x, y, z)
        published_color = self._canonical_vxl_color(color) & 0xFFFFFF
        self._publish_mutations(((x, y, z, bool(solid), published_color),))
        return True

    def _materialize_exposed_surfaces(self, removed_positions) -> None:
        """Keep newly visible solid faces in the retail MapSync color table.

        The retail remote loader fills implicit span interiors with solidity
        only. Its finalizer shades existing color entries, rather than
        discovering newly exposed interiors. Live destruction creates those
        entries on the client; a late join needs them explicitly in its VXL
        spans, including in columns adjacent to the excavated column.
        """
        world_map = self.map
        has_color = getattr(world_map, "has_explicit_color", None)
        if has_color is None:
            return  # Lightweight/alternate map implementations have no sparse colors.
        for x, y, z in removed_positions:
            for dx, dy, dz in (
                (-1, 0, 0), (1, 0, 0), (0, -1, 0),
                (0, 1, 0), (0, 0, -1), (0, 0, 1),
            ):
                nx, ny, nz = x + dx, y + dy, z + dz
                if not (0 <= nx < MAP_X and 0 <= ny < MAP_Y and 0 <= nz < MAP_Z):
                    continue
                if not world_map.get_solid(nx, ny, nz) or has_color(nx, ny, nz):
                    continue
                # Preserve the canonical interior fill color; authored,
                # painted, and player-built entries above were left intact.
                color = self._canonical_vxl_color(world_map.get_color(nx, ny, nz))
                world_map.color_block(nx, ny, nz, color)
                self.dirty_columns.add((nx, ny))

    def restore_static_light_block(
        self,
        x: int,
        y: int,
        z: int,
        color: tuple[int, int, int],
    ) -> bool:
        """Mirror the voxel created by a retail ``FlareBlockEntity``.

        Native ``vxl.pyd`` first removes exposed chroma-key marker voxels.
        ``FlareBlockEntity.post_initialize`` then adds the cell back with the
        resolved RGB while registering its point light.  The authoritative
        collision map must perform the same second half or clients collide
        with a block through which server movement can pass.

        This is load-derived map state, not a player mutation: the raw VXL
        still carries the marker and packet 21 supplies its final colour, so
        no dirty-column, reconnect, navigation-delta, or mutation-journal
        record is emitted here.  It runs on the gameplay thread during map
        resource construction.
        """
        x, y, z = int(x), int(y), int(z)
        if self.map is None or not self._valid_block_position(x, y, z):
            return False
        packed = self._canonical_vxl_color(color)
        self.map.set_point(x, y, z, packed)
        self._surface_cache.pop((x, y), None)
        # Bot workers load the raw VXL (markers stripped, no restore): tell
        # them about the restored cell so their collision matches (not a
        # player mutation: no journal / dirty column / reconnect record).
        cells = getattr(self, "static_light_cells", None)
        if not isinstance(cells, dict):
            cells = {}
            self.static_light_cells = cells
        cells[(x, y, z)] = int(packed) & 0xFFFFFF
        for listener in tuple(getattr(self, "static_light_listeners", ()) or ()):
            try:
                listener(x, y, z, int(packed) & 0xFFFFFF)
            except Exception:
                logger.debug("static light listener failed", exc_info=True)
        self.clear_block_damage(x, y, z)
        self.block_health.pop((x, y, z), None)
        return True

    def _set_air_override(self, x: int, y: int, z: int, air: bool) -> None:
        """Update the compact reconnect repair index for one committed cell.

        The index intentionally records any cell made air, including a
        player-built block that was later destroyed. Replaying an exact
        removal against pristine air is harmless, while omitting it can leave
        native collision behind when the retail map worker rejected a prior
        column update.
        """

        key = (int(x), int(y))
        bit = 1 << int(z)
        mask = int(self._air_override_masks.get(key, 0))
        if air:
            self._air_override_masks[key] = mask | bit
            return
        mask &= ~bit
        if mask:
            self._air_override_masks[key] = mask
        else:
            self._air_override_masks.pop(key, None)

    def snapshot_air_overrides(self) -> tuple[tuple[int, int, int], ...]:
        """Return immutable per-column air masks for one MapSync boundary.

        Called only while preparing a join snapshot on the gameplay thread.
        No voxel scan occurs: cost is proportional to columns that currently
        retain a destroyed cell, and each column occupies one tuple.
        """

        return tuple(
            (x, y, int(mask))
            for (x, y), mask in sorted(self._air_override_masks.items())
            if mask
        )

    def subscribe_mutations(
        self,
        callback: Callable[[int, int, int, bool, int, int], None],
    ) -> int:
        """Register a non-blocking canonical terrain listener.

        Returns a numeric token used by :meth:`unsubscribe_mutations`. Listener
        exceptions are isolated so navigation/telemetry can never roll back an
        already-committed gameplay mutation.
        """

        if not callable(callback):
            raise TypeError("mutation callback must be callable")
        token = self._next_mutation_listener_id
        self._next_mutation_listener_id += 1
        self._mutation_listeners[token] = callback
        return token

    def unsubscribe_mutations(self, token: int) -> None:
        """Remove a listener previously returned by ``subscribe_mutations``."""

        self._mutation_listeners.pop(int(token), None)

    def _publish_mutations(
        self, changes: tuple[tuple[int, int, int, bool, int], ...]
    ) -> None:
        """Advance topology once and notify listeners after a commit."""

        if not changes:
            return
        self.topology_version += 1
        version = self.topology_version
        for callback in tuple(self._mutation_listeners.values()):
            for x, y, z, solid, color in changes:
                try:
                    callback(x, y, z, solid, color, version)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    logger.exception("Terrain mutation listener failed")

    def destroy_block(self, x: int, y: int, z: int):
        """Destroy block at position."""
        destroyed = self.destroy_blocks([(x, y, z)])
        return bool(destroyed)
    
    def get_height(self, x: int, y: int) -> int:
        """Get the Z of topmost solid block at (x, y)."""
        return self._get_surface_z(x, y)

    def _get_surface_z(self, x: int, y: int) -> int:
        """Scan the column directly so spawn height does not depend on get_z()."""
        if self.map is None:
            return MAP_Z - 1
        if not (0 <= x < MAP_X and 0 <= y < MAP_Y):
            return MAP_Z - 1

        cached = self._surface_cache.get((x, y))
        if cached is not None:
            return cached
        # Spawn prewarm runs this ~70k times while a map loads on the
        # transition worker thread; the C column scan (same z=0 downward
        # get_solid probe) keeps that from competing with the live tick.
        scan = getattr(self.map, "surface_z", None)
        if scan is not None:
            z = int(scan(x, y))
            self._surface_cache[(x, y)] = z
            return z
        for z in range(MAP_Z):
            if self.map.get_solid(x, y, z):
                self._surface_cache[(x, y)] = z
                return z
        self._surface_cache[(x, y)] = MAP_Z - 1
        return MAP_Z - 1

    def is_water_column(self, x: int, y: int) -> bool:
        """A column whose topmost solid is the forced waterbed (z >= 239) is
        open water; land columns surface at or above the waterplane (z<=238)."""
        return self._get_surface_z(x, y) > MAP_Z - 2

    def spawn_position_is_safe(
        self,
        position: Tuple[float, float, float],
        *,
        team: int | None = None,
    ) -> bool:
        """Return whether a mode-authored player position is usable as-is.

        Mode spawns may be inside authored buildings, so this deliberately
        validates the body position instead of replacing it with the column's
        topmost surface. Open water, non-finite/out-of-world coordinates, and
        positions embedded in terrain are never valid life anchors -- except
        that a ``team`` whose authored spawn area is a retail sea spawn
        (SpookyMansion's ``zombie_spawn_area`` ring) may stand in that water,
        exactly as retail dropped zombies there. Without ``team`` (bot
        landing checks, generic callers) water stays rejected.
        """
        try:
            x, y, z = (float(position[index]) for index in range(3))
        except (IndexError, TypeError, ValueError):
            return False
        if not all(math.isfinite(value) for value in (x, y, z)):
            return False
        if not (0.0 <= x < MAP_X and 0.0 <= y < MAP_Y):
            return False
        if not (-float(MAP_Z) <= z < float(MAP_Z)):
            return False
        cell_x, cell_y = int(math.floor(x)), int(math.floor(y))
        if self.map is None:
            return False
        if self.is_water_column(cell_x, cell_y):
            if team is None or self._water_spawn_zone(int(team), cell_x, cell_y) is None:
                return False
            # Wading: the waterbed carries the body (clipbox folds z=239 into
            # the air above it, so the terrain support probe cannot see it).
            # Require the retail sea-spawn height, not a body hanging in air.
            return (
                self._player_body_is_clear(x, y, z)
                and z + PLAYER_STANDING_POS_ABOVE_GROUND >= float(MAP_Z - 2) - 0.75
            )
        return self._player_body_is_clear(x, y, z) and self._player_has_support(
            x, y, z
        )

    def _player_has_support(self, x: float, y: float, z: float) -> bool:
        """Return whether the standing capsule has terrain under either foot.

        Body clearance alone accepts arbitrary air pockets and coordinates
        above the visible VXL. That made a stale map-transition position look
        valid until native gravity exposed the bot underneath the new map.
        Spawn points may begin half a block above equilibrium, so probe the
        complete initial-settle band rather than one exact floating value.
        """

        radius = float(getattr(C, "PLAYER_RADIUS", 0.45)) * 0.75
        probes = (
            (x, y),
            (x - radius, y),
            (x + radius, y),
            (x, y - radius),
            (x, y + radius),
        )
        foot_z = float(z) + PLAYER_STANDING_POS_ABOVE_GROUND
        return any(
            self.clipbox(probe_x, probe_y, foot_z + settle)
            for probe_x, probe_y in probes
            for settle in (0.0, 0.25, 0.5, 0.75)
        )

    def _player_body_is_clear(self, x: float, y: float, z: float) -> bool:
        """Validate the full standing capsule footprint, not only its center."""

        radius = float(getattr(C, "PLAYER_RADIUS", 0.45))
        probes = (
            (x, y),
            (x - radius, y),
            (x + radius, y),
            (x, y - radius),
            (x, y + radius),
            (x - radius, y - radius),
            (x - radius, y + radius),
            (x + radius, y - radius),
            (x + radius, y + radius),
        )
        return all(
            not self.clipbox(probe_x, probe_y, z + height)
            for probe_x, probe_y in probes
            for height in (0.0, 1.0, 2.0)
        )

    def _nearest_safe_spawn_point(
        self, x: float, y: float, *, search: int
    ) -> Tuple[float, float, float] | None:
        """Find the closest ordinary terrain spawn around an invalid anchor."""
        center_x, center_y = int(math.floor(x)), int(math.floor(y))
        for radius in range(max(0, int(search)) + 1):
            ring: list[tuple[float, int, int]] = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    cell_x, cell_y = center_x + dx, center_y + dy
                    if not (1 <= cell_x < MAP_X - 1 and 1 <= cell_y < MAP_Y - 1):
                        continue
                    ring.append(
                        (
                            (float(cell_x) + 0.5 - x) ** 2
                            + (float(cell_y) + 0.5 - y) ** 2,
                            cell_x,
                            cell_y,
                        )
                    )
            for _distance, cell_x, cell_y in sorted(ring):
                if not self._safe_spawn_column(cell_x, cell_y):
                    continue
                surface_z = self._get_surface_z(cell_x, cell_y)
                return (
                    float(cell_x) + 0.5,
                    float(cell_y) + 0.5,
                    float(surface_z)
                    - PLAYER_STANDING_POS_ABOVE_GROUND
                    - 0.5,
                )
        return None

    def sanitize_spawn_point(
        self,
        position: Tuple[float, float, float],
        team: int,
        *,
        local_search: int = 64,
    ) -> Tuple[float, float, float]:
        """Return a dry final life anchor for a mode-proposed spawn.

        Every life-creation path calls this after its mode resolver. Valid
        authored interiors remain untouched. Invalid or water positions move
        to nearby safe terrain when possible, then fall back to the team's
        prewarmed production spawn pool.
        """
        if self.spawn_position_is_safe(position, team=team):
            return tuple(float(position[index]) for index in range(3))

        try:
            x, y = float(position[0]), float(position[1])
        except (IndexError, TypeError, ValueError):
            x = y = math.nan
        resolved = (
            self._nearest_safe_spawn_point(x, y, search=local_search)
            if math.isfinite(x) and math.isfinite(y)
            else None
        )
        if resolved is None:
            resolved = self.get_spawn_point(int(team))
        logger.warning(
            "Relocated invalid spawn on %s for team %s: %r -> %r",
            self.map_name or "<unloaded>",
            team,
            position,
            resolved,
        )
        return tuple(float(value) for value in resolved)

    def dry_ground_anchor(
        self, x: float, y: float, search: int = 24
    ) -> Tuple[float, float, float]:
        """Return a feet-anchor (x+0.5, y+0.5, surface - standing offset) on the
        nearest DRY column to (x, y), spiralling outward up to `search` blocks.
        Keeps CTF bases / intel out of the water when their nominal column is
        sea. Falls back to the requested column if nothing dry is in range."""
        bx, by = int(x), int(y)
        for r in range(search + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    cx, cy = bx + dx, by + dy
                    if not (0 <= cx < MAP_X and 0 <= cy < MAP_Y):
                        continue
                    sz = self._get_surface_z(cx, cy)
                    if sz <= MAP_Z - 2:
                        return (
                            float(cx) + 0.5,
                            float(cy) + 0.5,
                            float(sz) - PLAYER_STANDING_POS_ABOVE_GROUND,
                        )
        sz = self._get_surface_z(bx, by)
        return (float(x), float(y), float(sz) - PLAYER_STANDING_POS_ABOVE_GROUND)

    def dry_surface_anchor(
        self, x: float, y: float, search: int = 24
    ) -> Tuple[float, float, float]:
        """Return an entity anchor on the nearest dry voxel surface.

        Player positions are 2.25 blocks above the supporting voxel, whereas
        map entities use the surface coordinate itself.  Keeping these two
        coordinate spaces separate prevents crates/bases from floating at a
        player's head height or being placed on the ocean bed.
        """
        bx, by = int(x), int(y)
        for r in range(search + 1):
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    if max(abs(dx), abs(dy)) != r:
                        continue
                    cx, cy = bx + dx, by + dy
                    if not (0 <= cx < MAP_X and 0 <= cy < MAP_Y):
                        continue
                    surface_z = self._get_surface_z(cx, cy)
                    if surface_z <= int(C.Z_ABOVE_WATERPLANE):
                        return float(cx) + 0.5, float(cy) + 0.5, float(surface_z)
        surface_z = self._get_surface_z(bx, by)
        return float(x), float(y), float(surface_z)

    def clipbox(self, x: float, y: float, z: float) -> bool:
        """Reference-style player collision probe."""
        if x < 0 or x >= MAP_X or y < 0 or y >= MAP_Y:
            return True
        if z < 0:
            return False

        solid_z = int(math.floor(z))
        if solid_z == MAP_Z - 1:
            solid_z -= 1
        elif solid_z >= MAP_Z:
            return True
        return self.get_solid(int(math.floor(x)), int(math.floor(y)), solid_z)

    def clipworld(self, x: int, y: int, z: int) -> bool:
        """Reference-style solid query used by movement/world objects."""
        if x < 0 or x >= MAP_X or y < 0 or y >= MAP_Y:
            return False
        if z < 0:
            return False

        solid_z = z
        if solid_z == WATER_LEVEL + 1:
            solid_z = WATER_LEVEL
        elif solid_z >= WATER_LEVEL + 1:
            return True
        elif solid_z < 0:
            return False
        return self.get_solid(x, y, solid_z)
    
    def _spawn_region(self, team: int) -> tuple[int, int, int, int]:
        region = self.map_metadata.fallback_spawn_regions.get(int(team))
        if region is not None:
            return region
        return 192, 192, 320, 320

    def _safe_spawn_column(
        self,
        x: int,
        y: int,
        *,
        authored_zone: MapZone | None = None,
        reject_roofs: bool = True,
    ) -> bool:
        """Validate dry, level ground and reject raised building roofs."""
        if not (1 <= x < MAP_X - 1 and 1 <= y < MAP_Y - 1):
            return False
        surface_z = self._get_surface_z(x, y)
        if surface_z > int(C.Z_ABOVE_WATERPLANE):
            return False
        if authored_zone is not None and not authored_zone.contains_surface_z(surface_z):
            return False

        local = [
            self._get_surface_z(x + dx, y + dy)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        ]
        if any(z > int(C.Z_ABOVE_WATERPLANE) for z in local):
            return False
        if max(local) - min(local) > 2:
            return False

        # Never treat a roof, bridge, or player platform as terrain. VXL land
        # is solid beneath its surface; air shortly below the chosen surface
        # proves that this is an elevated structure/cave ceiling.
        support_end = min(MAP_Z, surface_z + 9)
        if any(not self.get_solid(x, y, z) for z in range(surface_z + 1, support_end)):
            return False

        # VXL's z axis points downward.  A roof is therefore significantly
        # smaller than the ordinary ground sampled around it.
        if reject_roofs:
            ring = []
            for radius in (8, 16):
                for dx, dy in (
                    (-radius, 0), (radius, 0), (0, -radius), (0, radius),
                    (-radius, -radius), (-radius, radius),
                    (radius, -radius), (radius, radius),
                ):
                    rx, ry = x + dx, y + dy
                    if 0 <= rx < MAP_X and 0 <= ry < MAP_Y:
                        rz = self._get_surface_z(rx, ry)
                        if rz <= int(C.Z_ABOVE_WATERPLANE):
                            ring.append(rz)
            if ring:
                ring.sort()
                ground_reference = ring[(len(ring) * 3) // 4]
                if ground_reference - surface_z > 4:
                    return False
        spawn_z = (
            float(surface_z)
            - PLAYER_STANDING_POS_ABOVE_GROUND
            - 0.5
        )
        if not self._player_body_is_clear(
            float(x) + 0.5,
            float(y) + 0.5,
            spawn_z,
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # Authored spawn volumes.
    #
    # A spawn candidate is either ``(x, y)`` -- stand on the column's topmost
    # surface (the historical form; every unauthored/fallback candidate) -- or
    # ``(x, y, floor_z)`` -- stand on an explicit floor voxel inside an
    # authored box: a storey under a roof (MayanJungle's temple, the
    # SpookyMansion mansion floors) or the waterbed of a retail sea spawn.
    # Retail dropped a player into the box volume; they landed on the first
    # solid below the drop point, which is what these floors enumerate.
    # ------------------------------------------------------------------

    @staticmethod
    def _zone_allows_water(zone: MapZone | None) -> bool:
        return zone is not None and zone.item in WATER_SPAWN_ITEMS

    @staticmethod
    def _zone_drop_range(zone: MapZone) -> tuple[int, int]:
        """Vertical (top, bottom) voxel range a retail drop may start in.

        Retail ``*_spawn_area`` boxes are centred; the metadata loader extends
        their floor to the map bottom so a box hovering over terrain still
        admits the ground below it. The authored bottom is the mirror of the
        top, which keeps basements and caves far beneath a box out of it.
        """
        _x0, _x1, _y0, _y1, z0, z1 = zone.extents
        top = int(math.floor(zone.z + z0))
        bottom = zone.z + z1
        if zone.item.endswith("spawn_area") and z1 > -z0:
            bottom = zone.z - z0
        return top, min(MAP_Z - 1, int(math.ceil(bottom)))

    def _zone_floor_levels(self, x: int, y: int, zone: MapZone) -> list[int]:
        """Floors a player dropped anywhere in ``zone``'s box at (x, y) lands on."""
        top, bottom = self._zone_drop_range(zone)
        floors: list[int] = []
        run_start: int | None = None
        for z in range(max(0, top), MAP_Z):
            if not self.get_solid(x, y, z):
                if run_start is None:
                    run_start = z
                continue
            if run_start is not None and run_start <= bottom:
                floors.append(z)
            run_start = None
            if z >= bottom:
                break
        return floors

    def _safe_spawn_floor(
        self, x: int, y: int, floor_z: int, zone: MapZone
    ) -> bool:
        """Validate one authored-box floor (indoor storey or retail sea spawn).

        Same guarantees as ``_safe_spawn_column`` for the top surface: the
        floor is reachable from the box, the standing body is clear (not
        embedded), it is dry unless the box is a retail water spawn, and the
        spot is not a pillar edge over a pit.
        """
        if not (1 <= x < MAP_X - 1 and 1 <= y < MAP_Y - 1):
            return False
        if not (0 < floor_z < MAP_Z) or not self.get_solid(x, y, floor_z):
            return False
        if floor_z > int(C.Z_ABOVE_WATERPLANE) and not self._zone_allows_water(zone):
            return False
        top, bottom = self._zone_drop_range(zone)
        # The air run above the floor must intersect the box: that is where
        # a retail drop that lands on this floor started.
        z = floor_z - 1
        if z < top or self.get_solid(x, y, z):
            return False
        while z - 1 >= top and not self.get_solid(x, y, z - 1):
            z -= 1
        if z > bottom:
            return False
        # A standing body needs three air voxels above the floor.
        if any(self.get_solid(x, y, floor_z - h) for h in (1, 2, 3)):
            return False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (dx or dy) and not any(
                    self.get_solid(x + dx, y + dy, level)
                    for level in range(floor_z - 3, min(MAP_Z, floor_z + 3))
                ):
                    return False
        if not self._player_body_is_clear(
            float(x) + 0.5,
            float(y) + 0.5,
            float(floor_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
        ):
            return False
        return floor_z == self._get_surface_z(x, y) or not self._sealed_air_pocket(
            x, y, floor_z - 1
        )

    def _sealed_air_pocket(self, x: int, y: int, z: int, limit: int = 64) -> bool:
        """True when (x, y, z) is in a closed air pocket under ``limit`` voxels.

        Trenches' spawn band holds 11-voxel voids under the trench boards; a
        floor inside one would trap the player. A bounded flood fill that
        escapes past ``limit`` cells is treated as open.
        """
        start = (x, y, z)
        seen = {start}
        frontier = [start]
        while frontier:
            cx, cy, cz = frontier.pop()
            for nx, ny, nz in (
                (cx + 1, cy, cz), (cx - 1, cy, cz), (cx, cy + 1, cz),
                (cx, cy - 1, cz), (cx, cy, cz - 1), (cx, cy, cz + 1),
            ):
                if (nx, ny, nz) in seen:
                    continue
                if not (0 <= nx < MAP_X and 0 <= ny < MAP_Y and 0 <= nz < MAP_Z):
                    continue
                if self.get_solid(nx, ny, nz):
                    continue
                seen.add((nx, ny, nz))
                if len(seen) > limit:
                    return False
                frontier.append((nx, ny, nz))
        return True

    def _water_spawn_zone(self, team: int, x: int, y: int) -> MapZone | None:
        """The team's authored retail sea-spawn zone covering (x, y), if any."""
        for zone in self.map_metadata.spawn_zones.get(team, []):
            if not self._zone_allows_water(zone):
                continue
            x0, x1, y0, y1 = zone.xy_bounds()
            if x0 <= x <= x1 and y0 <= y <= y1:
                return zone
        return None

    def _zone_candidates(self, zone: MapZone) -> list[tuple[int, ...]]:
        candidates: list[tuple[int, ...]] = []
        x0, x1, y0, y1 = zone.xy_bounds()
        for x, y in _spawn_scan_columns(
            max(1, x0), min(MAP_X - 2, x1) + 1,
            max(1, y0), min(MAP_Y - 2, y1) + 1,
        ):
            surface_ok = self._safe_spawn_column(
                x, y, authored_zone=zone, reject_roofs=False
            )
            if surface_ok:
                candidates.append((x, y))
            surface_z = self._get_surface_z(x, y)
            for floor_z in self._zone_floor_levels(x, y, zone):
                if floor_z == surface_z and surface_ok:
                    # Already a top-surface candidate. A top surface that
                    # failed the terrain-only rules (a jetty or bridge
                    # deck with air beneath it, open water) is still
                    # where a retail drop into this box landed, so it is
                    # judged by the floor rules below instead.
                    continue
                if self._safe_spawn_floor(x, y, floor_z, zone):
                    candidates.append((x, y, floor_z))
        return candidates

    def _zone_spawn_candidates(self, team: int) -> list[tuple[int, ...]]:
        zones = self.map_metadata.spawn_zones.get(team, [])
        water_zones = [zone for zone in zones if self._zone_allows_water(zone)]
        if water_zones:
            # Retail zombies rose only out of their ``zombie_spawn_area``
            # boxes; the metadata appends the team-one shore purely as a dry
            # complement for when those boxes yield nothing.
            candidates = [c for zone in water_zones for c in self._zone_candidates(zone)]
            if candidates:
                return candidates
        return [c for zone in zones for c in self._zone_candidates(zone)]

    def _zones_containing(
        self, zones, x: int, y: int
    ) -> list[MapZone]:
        result = []
        for zone in zones or ():
            x0, x1, y0, y1 = zone.xy_bounds()
            if x0 <= x <= x1 and y0 <= y <= y1:
                result.append(zone)
        return result

    def spawn_candidate_position(
        self, candidate, authored_zones=None
    ) -> Tuple[float, float, float] | None:
        """Revalidate one spawn candidate against the live world.

        Returns the standing position (half a block above equilibrium, as
        every spawn does) or None when the candidate is no longer safe. A
        top-surface ``(x, y)`` candidate inside an authored zone skips roof
        rejection (authored boxes may be on raised ground); outside one it
        gets the full terrain checks. A ``(x, y, floor_z)`` candidate is only
        valid inside an authored zone that admits that floor.
        """
        x, y = int(candidate[0]), int(candidate[1])
        if len(candidate) >= 3:
            floor_z = int(candidate[2])
            if not any(
                self._safe_spawn_floor(x, y, floor_z, zone)
                for zone in self._zones_containing(authored_zones, x, y)
            ):
                return None
        else:
            authored_zone = (
                self._zone_at(authored_zones, x, y) if authored_zones else None
            )
            if not self._safe_spawn_column(
                x, y,
                authored_zone=authored_zone,
                reject_roofs=authored_zone is None,
            ):
                return None
            floor_z = self._get_surface_z(x, y)
        return (
            float(x) + 0.5,
            float(y) + 0.5,
            float(floor_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
        )

    def _fallback_spawn_candidates(self, team: int) -> list[tuple[int, int]]:
        x0, y0, x1, y1 = self._spawn_region(team)
        strict = [
            (x, y)
            for x, y in _spawn_scan_columns(x0, x1 + 1, y0, y1 + 1, 4)
            if self._safe_spawn_column(x, y)
        ]
        if strict:
            return strict
        # Steep maps may have no 3x3-flat cells.  A dry team-region fallback
        # is still safer than the native random surface (which selects roofs).
        return [
            (x, y)
            for x, y in _spawn_scan_columns(x0, x1 + 1, y0, y1 + 1, 4)
            if self._get_surface_z(x, y) <= int(C.Z_ABOVE_WATERPLANE)
        ]

    def _get_spawn_candidates(self, team: int) -> list[tuple[int, ...]]:
        cached = self._spawn_candidates.get(team)
        if cached:
            return cached
        candidates = self._zone_spawn_candidates(team)
        source = "authored metadata"
        if not candidates:
            candidates = self._fallback_spawn_candidates(team)
            source = "safe terrain fallback"
        self._spawn_candidates[team] = candidates
        logger.info("TEAM%d has %d spawn columns from %s", team, len(candidates), source)
        return candidates

    def prewarm_spawn_candidates(self) -> None:
        """Build both team spawn caches outside the gameplay tick.

        This method is synchronous by design. ``load_map`` runs before the
        startup loops begin and map transitions call it in their existing
        background preflight worker. Later ``get_spawn_point`` calls still
        revalidate the selected cell against live block edits.
        """
        for team in (TEAM1, TEAM2):
            self._get_spawn_candidates(team)
            self.team_base_anchor(team)

    @staticmethod
    def _zone_at(zones: list[MapZone], x: int, y: int) -> MapZone | None:
        for zone in zones:
            x0, x1, y0, y1 = zone.xy_bounds()
            if x0 <= x <= x1 and y0 <= y <= y1:
                return zone
        return None

    def _fallback_base_candidate(
        self, team: int, candidates: list[tuple[int, int]]
    ) -> tuple[int, int] | None:
        """Choose one stable base centre for a voxel-only stock map."""
        if not candidates:
            return None
        x0, y0, x1, y1 = self._spawn_region(team)
        center_x = (x0 + x1) / 2.0
        center_y = (y0 + y1) / 2.0
        return min(
            candidates,
            key=lambda pos: (
                (pos[0] - center_x) ** 2 + (pos[1] - center_y) ** 2,
                pos[0],
                pos[1],
            ),
        )

    def team_base_anchor(self, team: int) -> Tuple[float, float, float]:
        """Player-coordinate anchor for a team's authored or fallback base."""
        team = int(team)
        cached = self._team_base_anchors.get(team)
        if cached is not None:
            return cached

        anchor = self._resolve_team_base_anchor(team)
        self._team_base_anchors[team] = anchor
        return anchor

    def _resolve_team_base_anchor(self, team: int) -> Tuple[float, float, float]:
        """Resolve one map-stable team anchor outside the gameplay tick."""
        base_zones = self.map_metadata.base_zones.get(team, [])
        for zone in base_zones:
            x0, x1, y0, y1 = zone.xy_bounds()
            candidates = [
                (x, y)
                for x, y in _spawn_scan_columns(
                    max(1, x0), min(MAP_X - 2, x1) + 1,
                    max(1, y0), min(MAP_Y - 2, y1) + 1,
                )
                if self._safe_spawn_column(
                    x, y, authored_zone=zone, reject_roofs=False
                )
            ]
            if candidates:
                x, y = min(
                    candidates,
                    key=lambda pos: (pos[0] - zone.x) ** 2 + (pos[1] - zone.y) ** 2,
                )
                return self.dry_ground_anchor(x, y)

        spawn_zones = self.map_metadata.spawn_zones.get(team, [])
        if spawn_zones:
            zone = spawn_zones[0]
            candidates = self._zone_spawn_candidates(team)
            # A base anchor is a dry place to regroup: prefer top-surface
            # columns; otherwise the nearest floor slot (a sea spawn is
            # snapped to the nearest shore by dry_ground_anchor).
            pool = [c for c in candidates if len(c) == 2] or candidates
            if pool:
                best = min(
                    pool,
                    key=lambda pos: (pos[0] - zone.x) ** 2 + (pos[1] - zone.y) ** 2,
                )
                if len(best) >= 3 and best[2] <= int(C.Z_ABOVE_WATERPLANE):
                    return (
                        float(best[0]) + 0.5,
                        float(best[1]) + 0.5,
                        float(best[2]) - PLAYER_STANDING_POS_ABOVE_GROUND,
                    )
                return self.dry_ground_anchor(best[0], best[1])
        candidates = self._get_spawn_candidates(team)
        if candidates:
            x, y = self._fallback_base_candidate(team, candidates)
            return self.dry_ground_anchor(x, y)
        nominal = (64.0, 256.0) if team == TEAM1 else (448.0, 256.0)
        return self.dry_ground_anchor(*nominal)

    def get_spawn_point(self, team: int) -> Tuple[float, float, float]:
        """Return a dry, level player spawn from authored zones or terrain."""
        if self.map is None:
            surface_z = float(C.Z_ABOVE_WATERPLANE) - 1.0
            return (256.0, 256.0, surface_z - PLAYER_STANDING_POS_ABOVE_GROUND)

        if team not in (TEAM1, TEAM2):
            x0, y0, x1, y1 = self._spawn_region(team)
            x, y, _native_z = self.map.get_random_pos(x0, y0, x1, y1)
            surface_z = self._get_surface_z(int(x), int(y))
            return (
                float(x) + 0.5,
                float(y) + 0.5,
                float(surface_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
            )

        candidates = self._get_spawn_candidates(team)
        if candidates:
            authored_zones = self.map_metadata.spawn_zones.get(team, [])
            choices = list(candidates)
            if not authored_zones:
                base = self._fallback_base_candidate(team, choices)
                if base is not None:
                    # Official stock-map coordinates are unavailable, but a
                    # team still needs one coherent fallback base rather than
                    # spawning anywhere in a 128x256 region. Start with a
                    # compact perimeter, then expand only enough to avoid maps
                    # such as Double Dragon collapsing a whole team onto one
                    # isolated safe column.
                    bx, by = base
                    target_count = min(8, len(choices))
                    cluster_radius = 24
                    clustered: list[tuple[int, int]] = []
                    while cluster_radius <= 64:
                        clustered = [
                            (x, y)
                            for x, y in choices
                            if (x - bx) ** 2 + (y - by) ** 2
                            <= cluster_radius ** 2
                        ]
                        if len(clustered) >= target_count:
                            break
                        cluster_radius += 4
                    if clustered:
                        choices = clustered
            random.shuffle(choices)
            for candidate in choices:
                # Spawn slightly above equilibrium and let physics settle.
                position = self.spawn_candidate_position(candidate, authored_zones)
                if position is not None:
                    return position
                if candidate in candidates:
                    candidates.remove(candidate)

        x0, y0, x1, y1 = self._spawn_region(team)
        center_x, center_y = (x0 + x1) // 2, (y0 + y1) // 2
        max_radius = max(x1 - x0, y1 - y0)
        for radius in range(max_radius + 1):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    x, y = center_x + dx, center_y + dy
                    if x0 <= x <= x1 and y0 <= y <= y1 and self._safe_spawn_column(x, y):
                        surface_z = self._get_surface_z(x, y)
                        return (
                            float(x) + 0.5,
                            float(y) + 0.5,
                            float(surface_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
                        )
        # A completely invalid team region should be exceptional; retain a
        # bounded dry anchor as a final availability fallback.
        return self.dry_ground_anchor(center_x, center_y)

    def block_line(self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int):
        """Get all blocks along a line."""
        if self.map is None:
            return []
        return self.map.block_line(x1, y1, z1, x2, y2, z2)

    def _valid_block_position(self, x: int, y: int, z: int) -> bool:
        return 0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z

    def clear_block_damage(self, x: int, y: int, z: int):
        position = (x, y, z)
        self.block_damage.pop(position, None)
        self.block_shade.pop(position, None)
        self.block_hits.pop(position, None)

    # Longest per-cell hit history kept for a client-coloured cell.  A map
    # voxel (5.0) takes at most 20 quarter-point hits; longer histories (big
    # RULE_BLOCK_HEALTH) fall back to a health-only row.
    MAX_REPLAY_HITS = 64

    def _client_coloured(self, position, rgb: int) -> bool:
        """True when the stock client, not the VXL, owns the colour a hit
        darkens: implicit interior voxels, and black voxels, which
        ``add_damage`` first re-colours through the native
        ``map.color_block`` (IDA, 2026-09-26)."""

        explicit = getattr(self.map, "has_explicit_color", None)
        if callable(explicit) and not explicit(*position):
            return True
        return (int(rgb) & 0xFFFFFF) == 0

    def _record_hit_shade(self, position, damage: float, first: bool) -> None:
        """Advance the displayed shade exactly like ``add_damage`` does.

        Called for a hit the cell survives (the client darkens only then).
        """

        if first:
            self.block_shade.pop(position, None)
            self.block_hits.pop(position, None)
            rgb = int(self.get_color(*position)) & 0xFFFFFF
            if self._client_coloured(position, rgb):
                self.block_hits[position] = b""
            else:
                self.block_shade[position] = (rgb << 24) | rgb
        if position in self.block_hits:
            history = self.block_hits[position]
            if history is None:
                return  # lost history stays lost until the cell is replaced
            quarters = int(round(float(damage) * 4.0))
            if (
                0 < quarters <= 255
                and abs(quarters / 4.0 - float(damage)) < 1e-9
                and len(history) < self.MAX_REPLAY_HITS
            ):
                self.block_hits[position] = history + bytes((quarters,))
            else:
                self.block_hits[position] = None
            return
        packed = self.block_shade.get(position)
        if packed is None:
            # Damage recorded before shade tracking (or set directly):
            # best effort from the canonical colour.
            rgb = int(self.get_color(*position)) & 0xFFFFFF
            packed = (rgb << 24) | rgb
        original = packed >> 24
        shown = packed & 0xFFFFFF
        self.block_shade[position] = (original << 24) | dim_rgb(shown, damage)

    def _repaint_damaged(self, position, rgb: int) -> None:
        """PaintBlock on a damaged cell: the client's ``color_block`` sets the
        new colour undarkened and leaves its DamagedBlock (health and
        original colour) alone; later hits darken the paint colour."""

        packed = self.block_shade.get(position)
        if packed is not None:
            original = packed >> 24
        else:
            # Client-coloured history: the painted colour is now explicit.
            # The DamagedBlock original colour is the client's own (only
            # restored transiently while the cell is being removed).
            original = int(self.get_color(*position)) & 0xFFFFFF
            self.block_hits.pop(position, None)
        self.block_shade[position] = (int(original) << 24) | (int(rgb) & 0xFFFFFF)

    def _hit_history(self, position) -> Optional[bytes]:
        """Replayable hit history for a client-coloured cell, or ``None``."""

        history = self.block_hits.get(position)
        if not history:
            return None
        total = float(self.block_damage.get(position, 0.0))
        if abs(sum(history) / 4.0 - total) > 1e-6:
            return None
        return history

    def initial_block_health(
        self, x: int, y: int, z: int, default: float = DEFAULT_BLOCK_HEALTH
    ) -> float:
        """Return a cell's undamaged health (recorded override or default)."""

        return float(self.block_health.get((int(x), int(y), int(z)), default))

    def iter_block_health_state(self):
        """Yield ``(x, y, z, remaining_health)`` for every live override.

        Remaining health is the recorded initial health minus the damage
        accumulated so far, expressed in the same unscaled units the retail
        ``BlockManagerState(38)`` user-block table carries.  Stale entries
        for cells that are no longer solid are dropped on the way.
        """

        from server.game_rules import get_rules

        try:
            scale = float(get_rules(self.config).get("RULE_BLOCK_HEALTH"))
        except (AttributeError, KeyError, TypeError, ValueError):
            scale = 1.0
        if not scale > 0.0:
            scale = 1.0
        for position, health in tuple(self.block_health.items()):
            if not self.get_solid(*position):
                self.block_health.pop(position, None)
                continue
            damage = float(self.block_damage.get(position, 0.0)) / scale
            remaining = float(health) - damage
            if remaining > 0.0:
                yield position[0], position[1], position[2], remaining

    def _health_scale(self) -> float:
        from server.game_rules import get_rules

        try:
            scale = float(get_rules(self.config).get("RULE_BLOCK_HEALTH"))
        except (AttributeError, KeyError, TypeError, ValueError):
            scale = 1.0
        return scale if scale > 0.0 else 1.0

    def block_manager_rows(self, cells=None, *, replay_hits: bool = False):
        """Return the retail BlockManagerState(38) rows for live cells.

        ``(user_rows, damaged_rows)``:

        * user rows ``(x, y, z, initial_health)`` in the client's UNSCALED
          ``user_blocks`` units -- every cell with a recorded non-default
          health (player builds 9, prefabs 9, block-cannon 3);
        * damaged rows ``(x, y, z, remaining_health, (r, g, b))`` -- every
          partially damaged cell.  The client stores these as
          ``DamagedBlock(health, original_color)`` where health is the SCALED
          remaining health (``initial * RULE_BLOCK_HEALTH - damage``) and
          darkens the voxel from ``original_color`` (live 2026-09-26).

        ``cells`` limits the rows to those coordinates; for those, cells
        without an override get an explicit default-health user row so a
        client that re-created the voxel through BlockBuildColored(33) (which
        the stock client stores at 3.0) converges to the server's health.
        Stale entries for cells that are no longer solid are dropped.

        A damaged row's colour is the ORIGINAL colour live clients recorded
        at the first hit; the per-hit shade they display differs from the
        row's one-shot darkening and is sent separately
        (:meth:`block_shade_rows`).  With ``replay_hits`` a client-coloured
        cell with a replayable hit history (:meth:`block_hit_replays`) gets
        a user row at its INITIAL health instead of a health-only row, for a
        fresh joiner that then receives the hits themselves.
        """

        scale = self._health_scale()
        user_rows = []
        damaged_rows = []
        if cells is None:
            health_cells = tuple(self.block_health.items())
            damage_cells = tuple(self.block_damage.items())
        else:
            wanted = [tuple(int(v) for v in cell) for cell in cells]
            health_cells = tuple(
                (cell, self.block_health.get(cell, DEFAULT_BLOCK_HEALTH))
                for cell in wanted
            )
            damage_cells = tuple(
                (cell, self.block_damage[cell])
                for cell in wanted if cell in self.block_damage
            )
        explicit = getattr(self.map, "has_explicit_color", None)
        user_index = {}
        for position, health in health_cells:
            if not self.get_solid(*position):
                self.block_health.pop(position, None)
                continue
            user_index[position] = len(user_rows)
            user_rows.append((position[0], position[1], position[2],
                              float(health)))
        for position, damage in damage_cells:
            if not self.get_solid(*position):
                self.clear_block_damage(*position)
                continue
            unscaled = float(
                self.block_health.get(position, DEFAULT_BLOCK_HEALTH)
            )
            initial = unscaled * scale
            remaining = initial - float(damage)
            if remaining <= 0.0:
                continue
            if replay_hits and self._hit_history(position) is not None:
                # The joiner re-applies the hits itself (exact health and
                # the client-owned colour's per-hit darkening).
                row = (position[0], position[1], position[2], unscaled)
                if position in user_index:
                    user_rows[user_index[position]] = row
                else:
                    user_rows.append(row)
                continue
            if (
                callable(explicit) and not explicit(*position)
            ) or position in self.block_hits:
                # Implicit interior (and black, see _client_coloured) colours
                # are client-owned; a damaged row
                # would repaint the voxel with the server's column fill.  An
                # equivalent user row (remaining health, unscaled) keeps the
                # hits-to-break identical without touching the colour.
                row = (position[0], position[1], position[2],
                       remaining / scale)
                if position in user_index:
                    user_rows[user_index[position]] = row
                else:
                    user_rows.append(row)
                continue
            packed = self.block_shade.get(position)
            if packed is not None:
                rgb = (packed >> 24) & 0xFFFFFF
            else:
                rgb = int(self.get_color(*position)) & 0xFFFFFF
            damaged_rows.append((
                position[0], position[1], position[2], remaining,
                ((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF),
            ))
        return user_rows, damaged_rows

    def _client_initial_health(self, position, scale: float) -> float:
        """``get_initial_health`` a client holds once the user rows landed:
        the recorded health on the 0.25 user-row grid (rounded up), else
        the map default, scaled by RULE_BLOCK_HEALTH."""

        health = self.block_health.get(position)
        if health is None:
            return float(DEFAULT_BLOCK_HEALTH) * scale
        quarters = max(1, min(255, int(math.ceil(float(health) * 4.0 - 1e-9))))
        return quarters / 4.0 * scale

    def block_shade_rows(self, cells=None):
        """Return ``(x, y, z, (r, g, b))`` for damaged cells whose displayed
        shade a BlockManagerState(38) damaged row would get wrong.

        The row darkens its original colour ONCE by ``get_initial_health -
        remaining`` while live clients compounded ``dim`` hit by hit (and a
        paint after damage shows the paint colour undarkened), so a cell hit
        more than once, or painted, needs its live shade restated after the
        row.  PaintBlock(7) does that exactly: ``color_block`` only sets the
        voxel colour and leaves the DamagedBlock alone.  Cells whose one-shot
        shade already matches (every single-hit cell) are omitted.
        """

        scale = self._health_scale()
        if cells is None:
            wanted = tuple(self.block_shade.items())
        else:
            wanted = tuple(
                (cell, self.block_shade[cell])
                for cell in (tuple(int(v) for v in raw) for raw in cells)
                if cell in self.block_shade
            )
        rows = []
        for position, packed in wanted:
            damage = self.block_damage.get(position)
            if damage is None or not self.get_solid(*position):
                self.block_shade.pop(position, None)
                continue
            remaining = (
                float(self.block_health.get(position, DEFAULT_BLOCK_HEALTH))
                * scale - float(damage)
            )
            if remaining <= 0.0:
                continue
            row_health = max(
                1, min(255, int(math.floor(remaining * 4.0 + 0.5)))
            ) / 4.0
            original = (packed >> 24) & 0xFFFFFF
            shown = packed & 0xFFFFFF
            predicted = dim_rgb(
                original,
                self._client_initial_health(position, scale) - row_health,
            )
            if predicted == shown:
                continue
            rows.append((
                position[0], position[1], position[2],
                ((shown >> 16) & 0xFF, (shown >> 8) & 0xFF, shown & 0xFF),
            ))
        return rows

    def block_hit_replays(self, cells=None):
        """Return ``(x, y, z, (amount, ...))`` for client-coloured cells.

        The client owns an implicit (or black) voxel's colour, so the server
        cannot state its shade.  Replaying the recorded per-hit amounts as
        single-cell Damage packets, after a user row at the cell's initial
        health (``block_manager_rows(replay_hits=True)``), makes a fresh
        joiner's ``add_damage`` produce the live health AND darkening.
        Only for a client that holds no damage for these cells yet.
        """

        if cells is None:
            positions = tuple(self.block_hits)
        else:
            positions = tuple(
                cell
                for cell in (tuple(int(v) for v in raw) for raw in cells)
                if cell in self.block_hits
            )
        rows = []
        for position in positions:
            if not self.get_solid(*position):
                self.clear_block_damage(*position)
                continue
            history = self._hit_history(position)
            if history is None:
                continue
            rows.append((
                position[0], position[1], position[2],
                tuple(quarters / 4.0 for quarters in history),
            ))
        return rows

    def destroy_blocks(self, positions: list[tuple[int, int, int]]):
        """Destroy a set of solid blocks and return the positions actually removed."""
        if self.map is None:
            return []

        destroyed = []
        seen = set()
        for x, y, z in positions:
            pos = (x, y, z)
            if pos in seen or not self._valid_block_position(x, y, z):
                continue
            seen.add(pos)
            if not self.get_solid(x, y, z):
                self.clear_block_damage(x, y, z)
                self.block_health.pop(pos, None)
                continue

            self.map.remove_point_nochecks(x, y, z)
            self._set_air_override(x, y, z, True)
            self._surface_cache.pop((x, y), None)
            self.dirty_columns.add((x, y))
            self.clear_block_damage(x, y, z)
            self.block_health.pop(pos, None)
            destroyed.append(pos)
        self._materialize_exposed_surfaces(destroyed)
        self._publish_mutations(
            tuple((x, y, z, False, 0) for x, y, z in destroyed)
        )
        return destroyed

    # Stock flood fill walks face + edge adjacency (18 neighbors, excluding
    # three-axis corners) and limits work, not component size. The 8000-block
    # constant elsewhere in the client only samples falling visual particles.
    COLLAPSE_NEIGHBORS = tuple(
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if 1 <= abs(dx) + abs(dy) + abs(dz) <= 2
    )
    # Retail vxl.pyd flood (sub_10036470) gives up once its visited-node
    # counter exceeds 0x989680 (10,000,000 NODES) and treats the chunk as
    # supported.  Our floods (this one and the C twin in aoslib.vxl) count
    # neighbour PROBES, and every popped node probes all 18 neighbours, so
    # the probe budget is the retail node budget x 18: exhaustion happens on
    # the first probe of node 10,000,001, as in retail.
    COLLAPSE_NODE_BUDGET = 10_000_000
    COLLAPSE_WORK_BUDGET = COLLAPSE_NODE_BUDGET * len(COLLAPSE_NEIGHBORS)

    def find_unsupported_chunks(self, removed_positions):
        """Classic AoS floating-structure detection: after removing cells,
        flood-fill each solid neighbor's connected component; a component
        that never reaches the indestructible base plane (z > 238) is
        unsupported and should collapse. Returns a list of cell-lists."""
        if self.map is None or not removed_positions:
            return []

        # Same traversal in C (aoslib.vxl VXL.find_unsupported_chunks): this
        # runs once per destroyed cell, and a grave's 3x3x3 crater paid
        # ~1.6 ms per call walking grounded terrain down to the base plane.
        fast = getattr(self.map, "find_unsupported_chunks", None)
        if (
            fast is not None
            and type(self).get_solid is WorldManager.get_solid
            and "get_solid" not in vars(self)
        ):
            return fast(
                removed_positions,
                self.COLLAPSE_NEIGHBORS,
                int(self.COLLAPSE_WORK_BUDGET),
            )

        neighbors = self.COLLAPSE_NEIGHBORS
        chunks = []
        visited = set()
        # Grounded searches intentionally stop as soon as they reach the base
        # plane.  Remember the discovered branch so another boundary cell can
        # prove support by touching it instead of walking to the base again.
        # Budget-exhausted branches are protected too: collapse detection is
        # fail-safe and must never return a partial component on a later start.
        safe = set()
        for (sx, sy, sz) in removed_positions:
            for dx, dy, dz in neighbors:
                start = (sx + dx, sy + dy, sz + dz)
                if start in visited or not self.get_solid(*start):
                    continue
                comp = []
                stack = [start]
                comp_seen = {start}
                grounded = False
                exhausted = False
                work = 0
                while stack:
                    cx, cy, cz = stack.pop()
                    if (cx, cy, cz) in safe or cz > 238:
                        grounded = True
                        break
                    comp.append((cx, cy, cz))
                    for ddx, ddy, ddz in neighbors:
                        work += 1
                        if work > self.COLLAPSE_WORK_BUDGET:
                            exhausted = True
                            stack.clear()
                            break
                        nxt = (cx + ddx, cy + ddy, cz + ddz)
                        if nxt in safe:
                            grounded = True
                            stack.clear()
                            break
                        if nxt not in comp_seen and self.get_solid(*nxt):
                            comp_seen.add(nxt)
                            stack.append(nxt)
                    if grounded:
                        break
                visited |= comp_seen
                if grounded or exhausted:
                    safe |= comp_seen
                elif comp:
                    chunks.append(comp)
        return chunks

    def apply_block_damage(
        self,
        x: int,
        y: int,
        z: int,
        damage: float,
        threshold: float = DEFAULT_BLOCK_HEALTH,
    ) -> tuple[float, bool]:
        """Accumulate damage on a block and destroy it when the threshold is reached.

        A cell with a recorded initial health (:attr:`block_health`, e.g. a
        prefab cell at DEFAULT_PREFAB_HEALTH) breaks at that health instead
        of the caller's default, matching the retail client's
        ``BlockManager.get_initial_health`` ledger for the same cell.
        """
        if self.map is None or damage <= 0.0:
            return 0.0, False
        if z > MAX_DAMAGEABLE_Z and self._valid_block_position(x, y, z):
            # Retail BlockManager.valid_to_damage: z > max_modifiable_z (238)
            # is the indestructible base layer on every client.
            return 0.0, False
        if not self._valid_block_position(x, y, z) or not self.get_solid(x, y, z):
            self.clear_block_damage(x, y, z)
            self.block_health.pop((x, y, z), None)
            return 0.0, False

        pos = (x, y, z)
        threshold = float(self.block_health.get(pos, threshold)) * (
            self._health_scale()
        )
        previous = self.block_damage.get(pos)
        total = (previous or 0.0) + damage
        if total >= threshold:
            self.destroy_blocks([pos])
            return total, True

        self.block_damage[pos] = total
        self._record_hit_shade(pos, float(damage), previous is None)
        return total, False
    
    def get_chunker(self):
        """Get map chunker for network transmission."""
        if self.map is None:
            return None
        return self.map.get_chunker()

    def capture_map_sync(
        self, snapshot_columns, *, full: bool
    ) -> MapSyncSnapshot:
        """Freeze VXL-dependent bytes on the gameplay thread before yielding.

        The caller must capture its mutation watermark in the same event-loop
        turn, then can wrap/compress this snapshot without touching the VXL.
        """
        columns = frozenset(snapshot_columns or ())
        # MapSync describes the finalized collision world. Raw spans are
        # byte-faithful for ordinary columns, but authored chroma markers
        # were removed (or restored with a palette colour) during load.
        # Send these columns for a full sync and a CRC-matched local raw base.
        columns = columns.union(
            (x, y) for x, y, _z in getattr(self.map, "retail_marker_positions", ())
        )
        revision = self.topology_version
        cache_key = (id(self.map_raw_bytes), revision, bool(full), columns)
        cached = (
            self._prepared_sync_chunks
            if self._prepared_sync_key == cache_key else None
        )
        if full and not columns and self._full_sync_chunks is not None:
            cached = tuple(self._full_sync_chunks)
        overlay = (
            bytes(self.map.serialize_columns(sorted(columns)))
            if cached is None and columns and self.map is not None else b""
        )
        return MapSyncSnapshot(
            raw=self.map_raw_bytes,
            overlay_data=overlay,
            z_shift=int(getattr(self.map, "source_z_shift", 0)),
            map_name=self.map_name,
            revision=revision,
            full=bool(full),
            columns=columns,
            cached_chunks=cached,
        )

    def cache_map_sync(
        self, snapshot: MapSyncSnapshot, chunks: list[bytes]
    ) -> None:
        """Keep only one completed revision; never publish into a new map."""
        if snapshot.raw is not self.map_raw_bytes:
            return
        if snapshot.full and not snapshot.columns:
            self._full_sync_chunks = list(chunks)
        if snapshot.revision == self.topology_version:
            self._prepared_sync_key = (
                id(self.map_raw_bytes), snapshot.revision, snapshot.full,
                snapshot.columns,
            )
            self._prepared_sync_chunks = tuple(chunks)

    def iter_full_sync_chunks(self, snapshot_columns=None):
        """Synchronous compatibility API; the live connection uses a worker."""
        snapshot = self.capture_map_sync(snapshot_columns, full=True)
        chunks = snapshot.build_chunks()
        if chunks is not None:
            self.cache_map_sync(snapshot, chunks)
        return chunks

    def serialize_dirty_columns_compressed(self, snapshot_columns=None) -> bytes:
        """Serialize the columns changed since map load, zlib-compressed in
        the same stream format the full-map chunker produces (the client
        applies (x, y, column-spans) records onto its world base)."""
        columns = (
            set(self.dirty_columns)
            if snapshot_columns is None else set(snapshot_columns)
        )
        if self.map is None or not columns:
            return b""
        raw = bytes(self.map.serialize_columns(sorted(columns)))
        if not raw:
            return b""
        return zlib.compress(raw, 6)
    
    def raycast(self, x: float, y: float, z: float, 
                dx: float, dy: float, dz: float,
                max_dist: float = 128.0) -> Optional[Tuple[int, int, int]]:
        """
        Cast a ray and return first solid block hit.
        Returns (x, y, z) of hit block or None.
        """
        if self.map is None:
            return None

        if self.world is not None:
            hit = self.world.hitscan_accurate((x, y, z), (dx, dy, dz), max_dist, False)
            if hit is not None:
                block = hit[1]
                return (int(block.x), int(block.y), int(block.z))

        step_size = 0.1
        steps = int(max_dist / step_size)
        for i in range(steps):
            cx = int(x + dx * step_size * i)
            cy = int(y + dy * step_size * i)
            cz = int(z + dz * step_size * i)
            if not (0 <= cx < MAP_X and 0 <= cy < MAP_Y and 0 <= cz < MAP_Z):
                return None
            if self.get_solid(cx, cy, cz):
                return (cx, cy, cz)
        return None
