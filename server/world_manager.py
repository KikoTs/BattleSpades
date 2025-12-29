"""
World Manager - handles map state and operations.
"""

import logging
import os
from typing import Optional, Tuple

from aoslib.vxl import AceMap, VXL_MAP_X, VXL_MAP_Y, VXL_MAP_Z

logger = logging.getLogger(__name__)


class WorldManager:
    """
    Manages the game world (map) state.
    Provides interface to aoslib.vxl.AceMap.
    """
    
    def __init__(self, config):
        self.config = config
        self.map: Optional[AceMap] = None
        self.map_name = ""
        self.maps_path = config.maps_path if hasattr(config, 'maps_path') else "maps"
    
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
            with open(map_path, 'rb') as f:
                data = f.read()
            
            self.map = AceMap(data)
            self.map_name = name.replace('.vxl', '')
            
            if self.map.ready:
                logger.info(f"Loaded map: {self.map_name}")
                return True
            else:
                logger.error(f"Failed to parse map: {name}")
                return False
                
        except Exception as e:
            logger.error(f"Error loading map {name}: {e}", exc_info=True)
            return False
    
    def generate_flat_map(self):
        """Generate a simple flat map."""
        # Create empty map
        self.map = AceMap()
        
        # Set ground layer at Z=62 (near bottom, leaving water at 63)
        ground_z = 62
        
        for x in range(VXL_MAP_X):
            for y in range(VXL_MAP_Y):
                # Air above ground
                for z in range(ground_z):
                    self.map.set_point(x, y, z, False, 0)
                
                # Ground at z=62
                color = 0x7F008F00  # Green grass
                self.map.set_point(x, y, ground_z, True, color)
        
        self.map.ready = True
        logger.info("Generated flat map")
    
    def get_solid(self, x: int, y: int, z: int) -> bool:
        """Check if block at position is solid."""
        if self.map is None:
            return False
        return self.map.get_solid(x, y, z)
    
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
        self.map.set_point(x, y, z, solid, color)
    
    def destroy_block(self, x: int, y: int, z: int):
        """Destroy block at position."""
        if self.map is None:
            return
        self.map.destroy_point(x, y, z)
    
    def get_height(self, x: int, y: int) -> int:
        """Get the Z of topmost solid block at (x, y)."""
        if self.map is None:
            return VXL_MAP_Z - 1
        return self.map.get_z(x, y)
    
    def get_spawn_point(self, team: int) -> Tuple[float, float, float]:
        """Get a spawn point for the given team."""
        if self.map is None:
            return (256.0, 256.0, 60.0)
        
        # Team-based spawn areas
        if team == 0:
            x1, y1, x2, y2 = 64, 128, 192, 384
        elif team == 1:
            x1, y1, x2, y2 = 320, 128, 448, 384
        else:
            x1, y1, x2, y2 = 192, 192, 320, 320
        
        pos = self.map.get_random_pos(x1, y1, x2, y2)
        
        # Return with slight offset above ground for spawning
        return (float(pos[0]) + 0.5, float(pos[1]) + 0.5, float(pos[2]) - 2.0)
    
    def block_line(self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int):
        """Get all blocks along a line."""
        if self.map is None:
            return []
        return self.map.block_line(x1, y1, z1, x2, y2, z2)
    
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
        
        # Simple raymarching
        step_size = 0.1
        steps = int(max_dist / step_size)
        
        for i in range(steps):
            cx = int(x + dx * step_size * i)
            cy = int(y + dy * step_size * i)
            cz = int(z + dz * step_size * i)
            
            if not (0 <= cx < VXL_MAP_X and 0 <= cy < VXL_MAP_Y and 0 <= cz < VXL_MAP_Z):
                return None
            
            if self.map.get_solid(cx, cy, cz):
                return (cx, cy, cz)
        
        return None
