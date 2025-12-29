# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: cdivision=True
"""
VXL Map Format Handler for Ace of Spades Battle Builders.
Based on the original AoS VXL format.

Coordinate System:
- X: 0-511 (West to East)
- Y: 0-511 (South to North)
- Z: 0-254 (0 = sky, higher values = deeper underground)

Note: Z=0 is at the TOP (sky), Z increases downward.
"""

import zlib
import struct
cimport cython
from libc.stdint cimport uint8_t, uint32_t, int32_t
from libc.stdlib cimport malloc, free
from libc.string cimport memcpy, memset

# Map dimensions
DEF MAP_X = 512
DEF MAP_Y = 512
DEF MAP_Z = 255
DEF DEFAULT_COLOR = 0xFF674028  # Brown dirt with alpha

# Export constants
VXL_MAP_X = MAP_X
VXL_MAP_Y = MAP_Y
VXL_MAP_Z = MAP_Z
VXL_DEFAULT_COLOR = DEFAULT_COLOR

# Map chunking constants
DEF MAP_SEND_ROWS = 4
DEF MAP_PACKET_SIZE = 1024


cpdef inline uint32_t block_color(int r, int g, int b):
    """Create a block color from RGB values."""
    return (0x7F << 24) | (r << 16) | (g << 8) | b


cdef inline size_t get_pos(int x, int y, int z) nogil:
    """Convert 3D coordinates to flat array index."""
    return x + (y * MAP_Y) + (z * MAP_X * MAP_Y)


cdef inline bint is_valid_pos(int x, int y, int z) nogil:
    """Check if coordinates are within valid bounds."""
    return 0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z


class MapSyncChunker:
    """Chunks map data for network transmission with CRC32."""
    
    def __init__(self, vxl_map):
        self.packer = MapPacker(vxl_map)
        self.crc32 = zlib.crc32(b"")
    
    def iter(self):
        s = b""
        for ins in self.packer.iter():
            self.crc32 = self.packer.crc32
            s += ins
            while len(s) >= MAP_PACKET_SIZE:
                yield s[:MAP_PACKET_SIZE]
                s = s[MAP_PACKET_SIZE:]
        if s:
            yield s


class MapPacker:
    """Compresses map data with zlib."""
    
    def __init__(self, data):
        self.serializer = MapSerializer(data)
        self.crc32 = zlib.crc32(b"")
    
    def iter(self):
        # Use same zlib settings as original (level 6, 15 window bits)
        compressor = zlib.compressobj(
            level=6,
            method=zlib.DEFLATED,
            wbits=15,
            memLevel=8,
            strategy=zlib.Z_DEFAULT_STRATEGY
        )
        for s in self.serializer.iter():
            # Update CRC with raw serialized data BEFORE compression
            self.crc32 = zlib.crc32(s, self.crc32)
            compressed = compressor.compress(s)
            if compressed:
                yield compressed
        # Final flush must use Z_FINISH to match original
        final = compressor.flush(zlib.Z_FINISH)
        if final:
            yield final


class MapSerializer:
    """Serializes map data in VXL format."""
    
    def __init__(self, data, delta_mode=True):
        self.data = data
        self.delta_mode = delta_mode
    
    def iter(self):
        for i in range(0, 512, MAP_SEND_ROWS):
            s = b""
            for j in range(MAP_SEND_ROWS):
                y = i + j
                if y >= 512:
                    break
                row = self.data.columns[y]
                if self.delta_mode:
                    s += b"".join(
                        struct.pack("<II", x, y) + row[x]
                        for x in range(512)
                    )
            yield s


cdef class AceMap:
    """
    Ace of Spades VXL map container.
    
    The map uses a Z-down coordinate system where:
    - X: 0-511 (West to East)
    - Y: 0-511 (South to North)
    - Z: 0-254 (Sky to Ground, 0 = top)
    
    Geometry is stored as a bitfield, colors as uint32 RGBA.
    """
    
    cdef public int size_x
    cdef public int size_y
    cdef public int size_z
    cdef public bint ready
    cdef public str fname
    cdef public dict map_info
    cdef public int estimated_size
    
    # Geometry: True = solid, False = air
    cdef bint *geometry
    
    # Color data: RGBA format
    cdef uint32_t *colors
    
    # Column data for network serialization
    cdef public list columns
    
    def __cinit__(self):
        self.size_x = MAP_X
        self.size_y = MAP_Y
        self.size_z = MAP_Z
        self.ready = False
        self.fname = ""
        self.map_info = {}
        self.estimated_size = 0
        
        # Allocate geometry array
        cdef size_t total_size = MAP_X * MAP_Y * MAP_Z
        self.geometry = <bint *>malloc(total_size * sizeof(bint))
        self.colors = <uint32_t *>malloc(total_size * sizeof(uint32_t))
        
        if self.geometry == NULL or self.colors == NULL:
            raise MemoryError("Failed to allocate map memory")
        
        # Initialize to solid with default color
        cdef size_t i
        for i in range(total_size):
            self.geometry[i] = True
            self.colors[i] = DEFAULT_COLOR
        
        # Initialize column storage
        self.columns = [[b"" for _ in range(512)] for _ in range(512)]
    
    def __dealloc__(self):
        if self.geometry != NULL:
            free(self.geometry)
        if self.colors != NULL:
            free(self.colors)
    
    def __init__(self, bytes data=None, dict map_info=None):
        if map_info is not None:
            self.map_info = map_info
        if data is not None:
            self.load_vxl(data)
    
    cpdef bint is_valid_position(self, int x, int y, int z):
        """Check if coordinates are within map bounds."""
        return is_valid_pos(x, y, z)
    
    cpdef bint is_surface(self, int x, int y, int z):
        """Check if a solid block is exposed to air on any face."""
        if not self.get_solid(x, y, z):
            return False
        if x > 0 and not self.get_solid(x - 1, y, z):
            return True
        if x + 1 < MAP_X and not self.get_solid(x + 1, y, z):
            return True
        if y > 0 and not self.get_solid(x, y - 1, z):
            return True
        if y + 1 < MAP_Y and not self.get_solid(x, y + 1, z):
            return True
        if z > 0 and not self.get_solid(x, y, z - 1):
            return True
        if z + 1 < MAP_Z and not self.get_solid(x, y, z + 1):
            return True
        return False
    
    cpdef bint get_solid(self, int x, int y, int z, bint wrapped=False):
        """Check if a block is solid at the given position."""
        if wrapped:
            x = x & (MAP_X - 1)
            y = y & (MAP_Y - 1)
        if not is_valid_pos(x, y, z):
            return False
        return self.geometry[get_pos(x, y, z)]
    
    cpdef uint32_t get_color(self, int x, int y, int z, bint wrapped=False):
        """Get the color at the given position (RGBA format)."""
        if wrapped:
            x = x & (MAP_X - 1)
            y = y & (MAP_Y - 1)
        if not is_valid_pos(x, y, z):
            return 0
        return self.colors[get_pos(x, y, z)]
    
    cpdef bint set_point(self, int x, int y, int z, bint solid, uint32_t color=0):
        """Set whether a block is solid and its color."""
        if not is_valid_pos(x, y, z):
            return False
        cdef size_t pos = get_pos(x, y, z)
        self.geometry[pos] = solid
        self.colors[pos] = color if solid else DEFAULT_COLOR
        return True
    
    cpdef bint can_build(self, int x, int y, int z):
        """Check if a block can be placed at this position."""
        return 0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z - 2
    
    cpdef bint build_point(self, int x, int y, int z, tuple color):
        """Build a block at position with given color."""
        if not self.can_build(x, y, z):
            return False
        
        # Check if there's a neighboring solid block
        if not self._has_neighbor(x, y, z):
            return False
        
        return self.set_point(x, y, z, True, block_color(color[0], color[1], color[2]))
    
    cpdef bint destroy_point(self, int x, int y, int z):
        """Destroy a block at position."""
        if not self.can_build(x, y, z):
            return False
        
        cdef bint ok = self.set_point(x, y, z, False, 0)
        
        # Check neighbors for falling blocks
        cdef list neighbors = [(x-1, y, z), (x+1, y, z), (x, y-1, z), 
                               (x, y+1, z), (x, y, z-1), (x, y, z+1)]
        for nx, ny, nz in neighbors:
            if self.can_build(nx, ny, nz) and self.get_solid(nx, ny, nz):
                self.check_node(nx, ny, nz, True)
        
        return ok
    
    cdef bint _has_neighbor(self, int x, int y, int z):
        """Check if position has at least one solid neighbor."""
        if x > 0 and self.get_solid(x - 1, y, z):
            return True
        if x + 1 < MAP_X and self.get_solid(x + 1, y, z):
            return True
        if y > 0 and self.get_solid(x, y - 1, z):
            return True
        if y + 1 < MAP_Y and self.get_solid(x, y + 1, z):
            return True
        if z > 0 and self.get_solid(x, y, z - 1):
            return True
        if z + 1 < MAP_Z and self.get_solid(x, y, z + 1):
            return True
        return False
    
    cpdef int get_z(self, int x, int y, int start=0):
        """
        Get the Z coordinate of the topmost solid block at (x, y).
        Returns MAP_Z if no solid block found.
        """
        cdef int z
        for z in range(start, MAP_Z):
            if self.get_solid(x, y, z):
                return z
        return MAP_Z
    
    cpdef tuple get_random_pos(self, int x1, int y1, int x2, int y2):
        """
        Get a random spawn point in the given area.
        Returns (x, y, z) tuple.
        """
        import random
        
        cdef int center_x = MAP_X // 2
        cdef int center_y = MAP_Y // 2
        cdef int max_radius = 300
        cdef int radius_step = 50
        cdef int rx, ry, rz
        cdef bint valid_point_found = False
        
        # Try different radii, starting from max and decreasing
        for radius in range(max_radius, radius_step - 1, -radius_step):
            for attempts in range(15):
                import math
                angle = random.random() * 2.0 * math.pi
                
                offset_x = int(math.cos(angle) * radius)
                offset_y = int(math.sin(angle) * radius)
                
                temp_x = center_x + offset_x
                temp_y = center_y + offset_y
                
                if not is_valid_pos(temp_x, temp_y, 0):
                    continue
                
                temp_z = self.get_z(temp_x, temp_y)
                
                # Check valid spawn (solid below, air above)
                if (temp_z < MAP_Z - 2 and 
                    self.get_solid(temp_x, temp_y, temp_z) and
                    not self.get_solid(temp_x, temp_y, temp_z - 1) and
                    not self.get_solid(temp_x, temp_y, temp_z - 2)):
                    
                    return (temp_x, temp_y, temp_z)
            
            if valid_point_found:
                break
        
        # Fallback to center
        rz = self.get_z(center_x, center_y)
        return (center_x, center_y, rz)
    
    cpdef list block_line(self, int x1, int y1, int z1, int x2, int y2, int z2):
        """
        Trace a line through voxels using 3D DDA algorithm.
        Returns list of (x, y, z) tuples for each voxel the line passes through.
        """
        cdef list ret = []
        cdef int x, y, z
        cdef int dx, dy, dz
        cdef int ixi, iyi, izi
        cdef long dxi, dyi, dzi
        cdef long ddx, ddy, ddz
        
        x, y, z = x1, y1, z1
        dx = x2 - x1
        dy = y2 - y1
        dz = z2 - z1
        
        ixi = 1 if dx >= 0 else -1
        iyi = 1 if dy >= 0 else -1
        izi = 1 if dz >= 0 else -1
        
        dx = abs(dx)
        dy = abs(dy)
        dz = abs(dz)
        
        if dx >= dy and dx >= dz:
            dxi = 1024
            ddx = 512
            dyi = 0x3FFFFFFF // MAP_X if dy == 0 else (dx * 1024) // dy
            ddy = dyi // 2
            dzi = 0x3FFFFFFF // MAP_X if dz == 0 else (dx * 1024) // dz
            ddz = dzi // 2
        elif dy >= dz:
            dyi = 1024
            ddy = 512
            dxi = 0x3FFFFFFF // MAP_X if dx == 0 else (dy * 1024) // dx
            ddx = dxi // 2
            dzi = 0x3FFFFFFF // MAP_X if dz == 0 else (dy * 1024) // dz
            ddz = dzi // 2
        else:
            dzi = 1024
            ddz = 512
            dxi = 0x3FFFFFFF // MAP_X if dx == 0 else (dz * 1024) // dx
            ddx = dxi // 2
            dyi = 0x3FFFFFFF // MAP_X if dy == 0 else (dz * 1024) // dy
            ddy = dyi // 2
        
        if ixi >= 0:
            ddx = dxi - ddx
        if iyi >= 0:
            ddy = dyi - ddy
        if izi >= 0:
            ddz = dzi - ddz
        
        while True:
            ret.append((x, y, z))
            
            if x == x2 and y == y2 and z == z2:
                break
            
            if ddz <= ddx and ddz <= ddy:
                z += izi
                if z < 0 or z >= MAP_Z:
                    break
                ddz += dzi
            elif ddx < ddy:
                x += ixi
                if x < 0 or x >= MAP_X:
                    break
                ddx += dxi
            else:
                y += iyi
                if y < 0 or y >= MAP_Y:
                    break
                ddy += dyi
        
        return ret
    
    cpdef bint check_node(self, int x, int y, int z, bint destroy=True):
        """
        Check if block at position is connected to ground.
        If destroy=True and disconnected, remove the floating blocks.
        """
        cdef set marked = set()
        cdef list nodes = [(x, y, z)]
        cdef int nx, ny, nz
        
        while nodes:
            nx, ny, nz = nodes.pop()
            
            # Connected to bottom of map = supported
            if nz >= MAP_Z - 2:
                return True
            
            pos = get_pos(nx, ny, nz)
            if pos in marked:
                continue
            marked.add(pos)
            
            # Add solid neighbors to check
            for dx, dy, dz in [(-1,0,0), (1,0,0), (0,-1,0), (0,1,0), (0,0,-1), (0,0,1)]:
                new_x, new_y, new_z = nx + dx, ny + dy, nz + dz
                if is_valid_pos(new_x, new_y, new_z) and self.get_solid(new_x, new_y, new_z):
                    nodes.append((new_x, new_y, new_z))
        
        # Not connected, destroy if requested
        if destroy:
            for pos in marked:
                self.geometry[pos] = False
                self.colors[pos] = DEFAULT_COLOR
        
        return True
    
    cpdef bint load_vxl(self, bytes data):
        """
        Load VXL data in the original AoS format.
        Row-major order: for y in [0..511], for x in [0..511]
        """
        cdef int x, y, z
        cdef int pos = 0
        cdef Py_ssize_t length = len(data)
        cdef int number_4byte_chunks, top_color_start, top_color_end
        cdef int len_bottom, len_top, bottom_color_start, bottom_color_end
        cdef bytes ns
        cdef int lowest_point = 0
        
        try:
            for y in range(MAP_Y):
                for x in range(MAP_X):
                    # Reset column to solid
                    for z in range(MAP_Z):
                        self.geometry[get_pos(x, y, z)] = True
                        self.colors[get_pos(x, y, z)] = DEFAULT_COLOR
                    
                    z = 0
                    col_data = bytearray()
                    
                    while True:
                        if pos + 4 > length:
                            break
                        
                        ns = data[pos:pos+4]
                        pos += 4
                        col_data += ns
                        
                        number_4byte_chunks = ns[0]
                        top_color_start = ns[1]
                        top_color_end = ns[2]
                        
                        # Track lowest point
                        if top_color_end > lowest_point:
                            lowest_point = top_color_end
                        
                        # Mark air from z to top_color_start
                        for i in range(z, top_color_start):
                            self.geometry[get_pos(x, y, i)] = False
                        
                        # Read top surface colors
                        len_bottom = top_color_end - top_color_start + 1
                        for i in range(len_bottom):
                            if pos + 4 > length:
                                break
                            color = struct.unpack('<I', data[pos:pos+4])[0]
                            self.colors[get_pos(x, y, top_color_start + i)] = color
                            pos += 4
                            col_data += data[pos-4:pos]
                        
                        # Check for end of data marker
                        if number_4byte_chunks == 0:
                            break
                        
                        # Calculate bottom colors
                        len_top = (number_4byte_chunks - 1) - len_bottom
                        
                        # Skip to next span header
                        bottom_color_end = ns[3]  # air start
                        bottom_color_start = bottom_color_end - len_top
                        
                        # Read bottom colors
                        for i in range(len_top):
                            if pos + 4 > length:
                                break
                            color = struct.unpack('<I', data[pos:pos+4])[0]
                            self.colors[get_pos(x, y, bottom_color_start + i)] = color
                            pos += 4
                            col_data += data[pos-4:pos]
                        
                        z = bottom_color_end
                    
                    self.columns[y][x] = bytes(col_data)
            
            self.ready = True
            return True
            
        except Exception as e:
            print(f"Map load error: {e}")
            self.ready = False
            return False
    
    cpdef bytes get_bytes(self):
        """Serialize the map to VXL format bytes."""
        cdef list result = []
        cdef int x, y, z
        cdef int air_start, top_colors_start, top_colors_end
        cdef int bottom_colors_start, bottom_colors_end
        cdef int top_colors_len, bottom_colors_len, colors_count
        
        for y in range(MAP_Y):
            for x in range(MAP_X):
                z = 0
                while z < MAP_Z:
                    # Find air region
                    air_start = z
                    while z < MAP_Z and not self.get_solid(x, y, z):
                        z += 1
                    
                    # Find top surface colors
                    top_colors_start = z
                    while z < MAP_Z and self.is_surface(x, y, z):
                        z += 1
                    top_colors_end = z
                    
                    # Skip internal solid
                    while z < MAP_Z and self.get_solid(x, y, z) and not self.is_surface(x, y, z):
                        z += 1
                    
                    # Find bottom surface colors
                    bottom_colors_start = z
                    i = z
                    while i < MAP_Z and self.is_surface(x, y, i):
                        i += 1
                    
                    if i != MAP_Z:
                        while self.is_surface(x, y, z):
                            z += 1
                    bottom_colors_end = z
                    
                    # Write span
                    top_colors_len = top_colors_end - top_colors_start
                    bottom_colors_len = bottom_colors_end - bottom_colors_start
                    colors_count = top_colors_len + bottom_colors_len
                    
                    if z == MAP_Z:
                        result.append(struct.pack('B', 0))
                    else:
                        result.append(struct.pack('B', colors_count + 1))
                    
                    result.append(struct.pack('B', top_colors_start))
                    result.append(struct.pack('B', top_colors_end - 1))
                    result.append(struct.pack('B', air_start))
                    
                    for i in range(top_colors_len):
                        result.append(struct.pack('<I', self.colors[get_pos(x, y, top_colors_start + i)]))
                    
                    for i in range(bottom_colors_len):
                        result.append(struct.pack('<I', self.colors[get_pos(x, y, bottom_colors_start + i)]))
        
        return b''.join(result)
    
    cpdef bytes get_chunk(self, int start_row, int num_rows):
        """Get a chunk of the map data for network transmission."""
        if not self.ready:
            return b""
        
        cdef list chunk = []
        cdef int end_row = min(start_row + num_rows, MAP_Y)
        
        for y in range(start_row, end_row):
            chunk.extend(self.columns[y])
        
        return b"".join(chunk)
    
    def get_chunker(self):
        """Get a MapSyncChunker for network transmission."""
        return MapSyncChunker(self)
    
    def width(self):
        return MAP_X
    
    def length(self):
        return MAP_Y
    
    def depth(self):
        return MAP_Z
    
    def to_grid(self, double x, double y):
        """Convert coordinates to grid reference (e.g., "A1")."""
        letter = chr(ord('A') + int(x // 64))
        number = str(int(y // 64) + 1)
        return letter + number
    
    def from_grid(self, str grid):
        """Convert grid reference to coordinates."""
        letter = grid[0].lower()
        number = int(grid[1])
        x = max(0, min(self.width() - 1, 32 + (64 * (ord(letter) - ord('a')))))
        y = max(0, min(self.length() - 1, 32 + (64 * (number - 1))))
        return x, y
    
    @property
    def name(self):
        return self.map_info.get("name", "Unknown")
    
    def __bytes__(self):
        return self.get_bytes()
    
    @staticmethod
    def load_from_file(str filepath):
        """Load a VXL map from file."""
        with open(filepath, 'rb') as f:
            data = f.read()
        return AceMap(data)
