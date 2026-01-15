# distutils: sources = aoslib/vxl_c.cpp
import zlib
import struct
from libc.stdint cimport *
from libc.math cimport NAN, isnan
from libcpp cimport bool

VXL_MAP_X = MAP_X
VXL_MAP_Y = MAP_Y
VXL_MAP_Z = MAP_Z
VXL_DEFAULT_COLOR = DEFAULT_COLOR


cpdef inline block_color(int r, int g, int b):
    return 0x7F << 24 | r << 16 | g << 8 | b << 0

# Constants for map chunking
MAP_SEND_ROWS = 4
MAP_PACKET_SIZE = 1024

class MapSyncChunker:
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
    def __cinit__(self, uint8_t *buffer=NULL, dict map_info=None):
        self.columns = [[b"" for _ in range(512)] for _ in range(512)]
        self.ready = False
        self.fname = ""
        self.map_data = new CppAceMap(buffer)
        self.estimated_size = len(buffer)
        self.map_info = map_info or {}

    def __dealloc__(self):
        del self.map_data

    def __init__(self, bytes data=None, dict map_info=None):
        if map_info is not None:
            self.map_info = map_info
        if data is not None:
            self.load_vxl(data)

    def __iter__(self):
        cdef:
            int x = 0, y = 0, size
            vector[uint8_t] v
            int chunk_size = 1024 * 4  # Chunk size for better compression
        v.reserve(chunk_size)
        while True:
            # This 32 value is the important factor to get 1167 chunks
            size = self.map_data.write(v, &x, &y, 32)
            if not size:
                break
            yield v.data()[:size]
            v.clear()

    def iter_compressed(self, compressor):
        cdef int total = 0
        cdef bytes data

        for data in iter(self):
            data = compressor.compress(data)
            total += len(data)
            yield data
        data = compressor.flush()
        self.estimated_size = total + len(data)
        yield data

    cpdef bint can_build(self, int x, int y, int z):
        return  0 <= x < MAP_X and 0 <= y < MAP_Y and 0 <= z < MAP_Z - 2

    cpdef bint set_point(self, int x, int y, int z, bool solid, uint32_t color=0, bool destroy=True):
        cdef bint ok = self.map_data.set_point(x, y, z, solid, color)
        return ok

    cpdef bint build_point(self, int x, int y, int z, tuple color):
        if not self.can_build(x, y, z):
            return False

        cdef vector[Pos3] neighbors = self.map_data.get_neighbors(x, y, z)
        if neighbors.empty():
            return False

        return self.map_data.set_point(x, y, z, True, block_color(*color))

    cpdef bint destroy_point(self, int x, int y, int z):
        if not self.can_build(x, y, z):
            return False

        cdef:
            bint ok = self.map_data.set_point(x, y, z, False, 0)
            vector[Pos3] neighbors = self.map_data.get_neighbors(x, y, z)
            Pos3 node
        for node in neighbors:
            if self.can_build(node.x, node.y, node.z):
                self.map_data.check_node(node.x, node.y, node.z, True)
        return ok

    cpdef list block_line(self, int x1, int y1, int z1, int x2, int y2, int z2):
        cdef vector[Pos3] line = self.map_data.block_line(x1, y1, z1, x2, y2, z2)
        return [(p.x, p.y, p.z) for p in line]

    cpdef int get_z(self, int x, int y, int start = 0):
        return self.map_data.get_z(x, y, start)

    cpdef tuple get_random_pos(self, int x1, int y1, int x2, int y2):
        cdef int x, y, z
        self.map_data.get_random_point(&x, &y, &z, x1, y1, x2, y2)
        return x, y, z

    cpdef bytes get_bytes(self):
        cdef vector[uint8_t] x = self.map_data.write()
        return x.data()[:x.size()]

    def width(self):
        return MAP_X

    def length(self):
        return MAP_Y

    def depth(self):
        return MAP_Z

    def to_grid(self, x: double, y: double):
        letter = chr(ord('A') + <int>(x // 64))
        number = str(<int>(y // 64) + 1)
        return letter + number

    def from_grid(self, grid: str):
        letter = grid[0].lower()
        number = int(grid[1])
        x = max(0, min(self.width() - 1, 32 + (64 * (ord(letter) - ord('a')))))
        y = max(0, min(self.length() - 1, 32 + (64 * (number - 1))))
        return x, y

    def __bytes__(self):
        return self.get_bytes()





    cpdef bint load_vxl(self, bytes data) except *:
        """
        Load VXL data by mimicking Mari Kiri's original map.py logic:
          - row-major order: for y in [0..511], for x in [0..511]
          - read 4 bytes 'ns'
            * if ns[0] == 0, then read (ns[2] - ns[1] + 1)*4 color bytes and break
            * else read (ns[0]-1)*4 color bytes (no break)
          - track 'lowest_point' if needed (the original code uses
            cand_lowest = max(ns[2], ns[1]) etc.)
          - if we find lowest_point == 63 after loading => shift by 64
        """
        # 1) Declare all Cython variables here, outside any try / for / if block:
        cdef int x, y
        cdef int pos = 0
        cdef bytes ns
        cdef int lowest_point = 63
        cdef int highest_point = 255
        cdef int cand_lowest
        cdef Py_ssize_t length = len(data)
        cdef int finals, needed, block_size  # Declare all variables here

        try:
            # Go row-major, each row is y, each column is x
            for y in range(512):
                for x in range(512):
                    # If we've exhausted the file, store a 4-byte dummy column
                    if pos >= length:
                        self.columns[y][x] = b"\x00\x00\x00\x00"
                        continue

                    col_data = bytearray()

                    # Keep reading 4-byte blocks until we see ns[0] == 0
                    while True:
                        # If there's not even 4 bytes left, bail out
                        if pos + 4 > length:
                            break

                        ns = data[pos:pos+4]
                        pos += 4

                        # Mimic Mari Kiri's "lowest_point" logic
                        cand_lowest = max(ns[2], ns[1])
                        if cand_lowest > lowest_point:
                            lowest_point = cand_lowest
                        if ns[1] < highest_point:
                            highest_point = ns[1]

                        # Always append this 4-byte header to col_data
                        col_data += ns

                        if ns[0] == 0:
                            # If spans=0, read final color block => break
                            finals = ns[2] - ns[1] + 1
                            needed = 4 * finals
                            if pos + needed > length:
                                # Not enough data => partial read => break
                                break
                            col_data += data[pos:pos+needed]
                            pos += needed
                            break
                        else:
                            # If spans != 0, read (spans-1)*4 color bytes
                            block_size = (ns[0] - 1) * 4
                            if block_size < 0:
                                # Protect from negative
                                break
                            if pos + block_size > length:
                                break

                            col_data += data[pos:pos+block_size]
                            pos += block_size
                            # Then we loop again until ns[0] == 0

                    self.columns[y][x] = bytes(col_data)

            # After reading all columns, if lowest_point==63 => older 0.x map => shift up by 64
            if lowest_point == 63:
                self._shift_map_up(64)

            self.ready = True
            return True

        except Exception as e:
            print(f"Map load error: {e}")
            self.ready = False
            return False

    def _shift_map_up(self, int z_shift):
        """Deprecated: Handling for old 0.x maps. Currently a no-op/stub."""
        print(f"Warning: Map requires shift up by {z_shift} (legacy format). Skipping implementation.")
        return



    cpdef bytes get_chunk(self, int start_row, int num_rows):
        """Get a chunk of the map data for network transmission"""
        if not self.ready:
            return b""
            
        cdef:
            list chunk = []
            int end_row = min(start_row + num_rows, VXL_MAP_Y)
            
        for y in range(start_row, end_row):
            chunk.extend(self.columns[y])
            
        return b"".join(chunk)





    @property
    def name(self):
        return self.map_info["name"]
    cpdef get_chunker(self):
        return MapSyncChunker(self)

