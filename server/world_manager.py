"""
World Manager - handles map state and operations.
"""

import logging
import math
import os
import random
import struct
import zlib
from typing import Optional, Tuple

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
from server.runtime_vxl import ServerVXL as VXL

logger = logging.getLogger(__name__)

MAP_X = int(C.MAP_X)
MAP_Y = int(C.MAP_Y)
MAP_Z = int(C.MAP_Z)


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
        # Columns modified since map load — what a matched-CRC client is
        # missing relative to its local file. Sent as the MapSync delta.
        self.dirty_columns: set[tuple[int, int]] = set()
        # Blue/green spawn-zone marker columns the map author placed (the real
        # team base/spawn locations). Recorded + erased at load to mirror the
        # client's find_marker_points. {TEAM1: [(x,y,z), ...], TEAM2: [...]}.
        self.spawn_markers: dict[int, list[tuple[int, int, int]]] = {
            TEAM1: [],
            TEAM2: [],
        }
    
    def load_map(self, name: str) -> bool:
        """Load a VXL map file."""
        # Try with and without extension
        map_path = os.path.join(self.maps_path, name)
        if not map_path.endswith('.vxl'):
            map_path += '.vxl'
        
        if not os.path.exists(map_path):
            logger.warning(f"Map not found: {map_path}")
            logger.info("Generating flat map...")
            self.generate_flat_map()
            self.map_name = "flat"
            return True
        
        try:
            self.map = VXL(1, map_path, 3)
            self.map_name = name.replace('.vxl', '')
            with open(map_path, 'rb') as handle:
                raw = handle.read()
            self.map_raw_bytes = raw
            self._full_sync_chunks = None
            self.map_file_crc = zlib.crc32(raw) & 0xFFFFFFFF
            self.dirty_columns = set()
            self._refresh_world()
            self._scan_spawn_markers()
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
        
        # Set ground layer at Z=62 (near bottom, leaving water at 63)
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
        self.map_file_crc = zlib.crc32(raw) & 0xFFFFFFFF
        self.dirty_columns = set()
        self._refresh_world()
        logger.info("Generated flat map")

    def _refresh_world(self):
        if self.map is None:
            self.world = None
            return
        self.world = World(self.map)
    
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
    
    def set_block(self, x: int, y: int, z: int, solid: bool, color: int = 0):
        """Set block at position."""
        if self.map is None:
            return
        if solid:
            self.map.set_point(x, y, z, color)
        else:
            self.map.remove_point(x, y, z)
        self.dirty_columns.add((x, y))
        self.clear_block_damage(x, y, z)

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

        for z in range(MAP_Z):
            if self.map.get_solid(x, y, z):
                return z
        return MAP_Z - 1

    def is_water_column(self, x: int, y: int) -> bool:
        """A column whose topmost solid is the forced waterbed (z >= 239) is
        open water; land columns surface at or above the waterplane (z<=238)."""
        return self._get_surface_z(x, y) > MAP_Z - 2

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
    
    def _scan_spawn_markers(self):
        """Locate, record and ERASE the blue/green spawn-zone markers the map
        author placed (ugc_spawnblue*/ugc_spawngreen*), mirroring the client's
        find_marker_points which runs on every load (including the MapSync
        build path). A marker is a surface block whose RGB high nibbles are pure
        blue (0x0000F0) -> TEAM1 or pure green (0x00F0_00) -> TEAM2; the live
        engine deletes them, so the server must too or players collide with
        invisible blocks at the bases. These columns are the true team
        base/spawn locations (the old hardcoded rectangles spawned over open
        ocean on edge-sea maps like ArcticBase)."""
        self.spawn_markers = {TEAM1: [], TEAM2: []}
        m = self.map
        if m is None:
            return
        get_z = m.get_z
        get_ct = m.get_color_tuple
        erase: list[tuple[int, int, int]] = []
        for x in range(MAP_X):
            for y in range(MAP_Y):
                z = get_z(x, y)
                if z > MAP_Z - 2:        # ocean bed / empty column
                    continue
                r, g, b, _a = get_ct(x, y, z)
                if (r & 0xF0) == 0 and (g & 0xF0) == 0 and (b & 0xF0) == 0xF0:
                    self.spawn_markers[TEAM1].append((x, y, z))
                    erase.append((x, y, z))
                elif (r & 0xF0) == 0 and (g & 0xF0) == 0xF0 and (b & 0xF0) == 0:
                    self.spawn_markers[TEAM2].append((x, y, z))
                    erase.append((x, y, z))

        # Safety guard: a real map has a few dozen markers. A flood means our
        # colour predicate is matching ordinary terrain — abort rather than
        # gut the map.
        if len(erase) > 500:
            logger.warning(
                "Spawn-marker scan matched %d blocks (too many) — ignoring",
                len(erase),
            )
            self.spawn_markers = {TEAM1: [], TEAM2: []}
            return

        for (x, y, z) in erase:
            self.map.remove_point(x, y, z)
        if erase:
            logger.info(
                "Spawn markers: erased %d (TEAM1=%d, TEAM2=%d)",
                len(erase),
                len(self.spawn_markers[TEAM1]),
                len(self.spawn_markers[TEAM2]),
            )

    def team_base_anchor(self, team: int) -> Tuple[float, float, float]:
        """Feet-anchor for a team's base/intel: the dry ground at the centroid
        of its spawn markers, or a sensible fallback when the map has none."""
        markers = self.spawn_markers.get(team) or []
        if markers:
            cx = sum(mx for mx, _, _ in markers) / len(markers)
            cy = sum(my for _, my, _ in markers) / len(markers)
            return self.dry_ground_anchor(cx, cy)
        nominal = (64.0, 256.0) if team == TEAM1 else (448.0, 256.0)
        return self.dry_ground_anchor(*nominal)

    def get_spawn_point(self, team: int) -> Tuple[float, float, float]:
        """Get a spawn point for the given team."""
        if self.map is None:
            return (256.0, 256.0, 62.0 - PLAYER_STANDING_POS_ABOVE_GROUND)

        # Prefer the map author's spawn-zone markers (the real base) over the
        # hardcoded rectangles, which on edge-sea maps overlap open ocean.
        markers = self.spawn_markers.get(team) or []
        if markers:
            # Try the marker columns themselves (dry ones first).
            order = list(markers)
            random.shuffle(order)
            for mx, my, _mz in order:
                surface_z = self._get_surface_z(mx, my)
                if surface_z <= MAP_Z - 2:
                    return (
                        float(mx) + 0.5,
                        float(my) + 0.5,
                        float(surface_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
                    )
            # The markers are floating blocks erased over water, so every marker
            # column reads as sea. Anchor on the nearest DRY ground to the
            # team's marker CENTROID instead of dropping into the (wrong-area)
            # rectangle below — keeps each team on its own side of the map.
            cx = sum(m[0] for m in markers) / len(markers)
            cy = sum(m[1] for m in markers) / len(markers)
            ax, ay, az = self.dry_ground_anchor(cx, cy)
            return (ax, ay, az - 0.5)

        # Team-based spawn areas (only for marker-less maps)
        if team == TEAM1:
            x1, y1, x2, y2 = 64, 128, 192, 384
        elif team == TEAM2:
            x1, y1, x2, y2 = 320, 128, 448, 384
        else:
            x1, y1, x2, y2 = 192, 192, 320, 320

        pos = self.map.get_random_pos(x1, y1, x2, y2)
        surface_z = self._get_surface_z(int(pos[0]), int(pos[1]))

        # Spawn slightly ABOVE standing height and let the player drop in.
        # Spawning with feet exactly on the block boundary (surface - 2.25)
        # is a degenerate equilibrium: the engine micro-bobs on it, and the
        # client/server sims bob out of phase, producing constant
        # reconciliation jitter while walking. A natural landing settles
        # ~0.0004 above the boundary, which is stable.
        return (
            float(pos[0]) + 0.5,
            float(pos[1]) + 0.5,
            float(surface_z) - PLAYER_STANDING_POS_ABOVE_GROUND - 0.5,
        )
    
    def block_line(self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int):
        """Get all blocks along a line."""
        if self.map is None:
            return []
        return self.map.block_line(x1, y1, z1, x2, y2, z2)

    def _valid_block_position(self, x: int, y: int, z: int) -> bool:
        return 0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z

    def clear_block_damage(self, x: int, y: int, z: int):
        self.block_damage.pop((x, y, z), None)

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
                continue

            self.map.destroy_point(x, y, z)
            self.dirty_columns.add((x, y))
            self.clear_block_damage(x, y, z)
            destroyed.append(pos)
        return destroyed

    # Chunks bigger than this stay standing (perf guard; matches the classic
    # behavior where huge terrain sections never collapse).
    COLLAPSE_MAX_CHUNK = 512

    def find_unsupported_chunks(self, removed_positions):
        """Classic AoS floating-structure detection: after removing cells,
        flood-fill each solid neighbor's connected component; a component
        that never reaches the indestructible base plane (z >= MAP_Z-2) is
        unsupported and should collapse. Returns a list of cell-lists."""
        if self.map is None or not removed_positions:
            return []

        neighbors = ((1, 0, 0), (-1, 0, 0), (0, 1, 0),
                     (0, -1, 0), (0, 0, 1), (0, 0, -1))
        chunks = []
        visited = set()
        for (sx, sy, sz) in removed_positions:
            for dx, dy, dz in neighbors:
                start = (sx + dx, sy + dy, sz + dz)
                if start in visited or not self.get_solid(*start):
                    continue
                comp = []
                stack = [start]
                comp_seen = {start}
                grounded = False
                while stack:
                    cx, cy, cz = stack.pop()
                    if cz >= MAP_Z - 2:
                        grounded = True
                        break
                    comp.append((cx, cy, cz))
                    if len(comp) > self.COLLAPSE_MAX_CHUNK:
                        grounded = True
                        break
                    for ddx, ddy, ddz in neighbors:
                        nxt = (cx + ddx, cy + ddy, cz + ddz)
                        if nxt not in comp_seen and self.get_solid(*nxt):
                            comp_seen.add(nxt)
                            stack.append(nxt)
                visited |= comp_seen
                if not grounded and comp:
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
        """Accumulate damage on a block and destroy it when the threshold is reached."""
        if self.map is None or damage <= 0.0:
            return 0.0, False
        if not self._valid_block_position(x, y, z) or not self.get_solid(x, y, z):
            self.clear_block_damage(x, y, z)
            return 0.0, False

        pos = (x, y, z)
        total = self.block_damage.get(pos, 0.0) + damage
        if total >= threshold:
            self.destroy_blocks([pos])
            return total, True

        self.block_damage[pos] = total
        return total, False
    
    def get_chunker(self):
        """Get map chunker for network transmission."""
        if self.map is None:
            return None
        return self.map.get_chunker()

    def iter_full_sync_chunks(self):
        """Full-map MapSync payload as MAP_PACKET_SIZE (1024-byte) chunks.

        The client's stream-builder consumes ``(u32 x, u32 y, column-spans)``
        records (the same layout ``VXL.get_chunk`` emits). We build those
        records by wrapping the RAW .vxl file's own column spans — which use
        the native *implicit-underground* encoding (a final span with
        span_words==0 means "solid to the map floor", so only the visible
        surface colours are on the wire).

        This is the crux of the 2026-07-09 Steam-client join crash: our
        in-memory grid fills the underground solid for collision, and
        ``get_chunk`` re-serialises every one of those filled voxels
        *explicitly* — 36 MB uncompressed, which the strict client rejects
        mid-build. Re-wrapping the raw spans instead keeps the exact record
        format the client wants at ~5 MB (the client refills the underground
        itself, so the world is still solid and correctly coloured).

        Cached after first build (the raw file never changes at runtime).
        Returns a list of byte chunks, or None if no raw bytes are available
        or the file can't be walked cleanly (caller falls back to
        get_chunker()).
        """
        if self._full_sync_chunks is not None:
            return self._full_sync_chunks
        raw = self.map_raw_bytes
        if not raw:
            return None
        MAP_SIZE = 512
        MAP_PACKET_SIZE = 1024  # matches aoslib/vxl.pyx DEF MAP_PACKET_SIZE
        n = len(raw)
        out = bytearray()
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
                out += struct.pack("<II", x, y)
                out += raw[start:pos]
        if pos != n:
            logger.warning("Map walker consumed %d/%d bytes of %s — falling back "
                           "to chunker", pos, n, self.map_name)
            return None
        compressed = zlib.compress(bytes(out), 6)
        self._full_sync_chunks = [
            compressed[i:i + MAP_PACKET_SIZE]
            for i in range(0, len(compressed), MAP_PACKET_SIZE)
        ]
        logger.info("Built full-sync stream for %s: %d records, %d B raw, %d B "
                    "compressed, %d chunks", self.map_name, MAP_SIZE * MAP_SIZE,
                    len(out), len(compressed), len(self._full_sync_chunks))
        return self._full_sync_chunks

    def serialize_dirty_columns_compressed(self) -> bytes:
        """Serialize the columns changed since map load, zlib-compressed in
        the same stream format the full-map chunker produces (the client
        applies (x, y, column-spans) records onto its world base)."""
        if self.map is None or not self.dirty_columns:
            return b""
        raw = self.map.serialize_columns(sorted(self.dirty_columns))
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
