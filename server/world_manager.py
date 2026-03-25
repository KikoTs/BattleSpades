"""
World Manager - handles map state and operations.
"""

import logging
import math
import os
from typing import Optional, Tuple

import shared.constants as C
from aoslib.world import World
from server.game_constants import (
    DEFAULT_BLOCK_HEALTH,
    PLAYER_HEIGHT,
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
            self._refresh_world()
            logger.info(f"Loaded map: {self.map_name}")
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
    
    def get_spawn_point(self, team: int) -> Tuple[float, float, float]:
        """Get a spawn point for the given team."""
        if self.map is None:
            return (256.0, 256.0, 62.0 - PLAYER_HEIGHT)
        
        # Team-based spawn areas
        if team == TEAM1:
            x1, y1, x2, y2 = 64, 128, 192, 384
        elif team == TEAM2:
            x1, y1, x2, y2 = 320, 128, 448, 384
        else:
            x1, y1, x2, y2 = 192, 192, 320, 320
        
        pos = self.map.get_random_pos(x1, y1, x2, y2)
        surface_z = self._get_surface_z(int(pos[0]), int(pos[1]))
        
        # aoslib.world.Player treats position.z as the top of the player body.
        return (float(pos[0]) + 0.5, float(pos[1]) + 0.5, float(surface_z) - PLAYER_HEIGHT)
    
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
            self.clear_block_damage(x, y, z)
            destroyed.append(pos)
        return destroyed

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
