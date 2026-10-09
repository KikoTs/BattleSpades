# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
"""
VXL module compatibility layer.

This module restores the original Python-facing VXL surface closely enough to
match the original `aosdump/aoslib/vxl.pyd` in the local reverse-engineering
environment while keeping the implementation readable in Cython.
"""

import os as _os
import random as _random
import struct as _struct
import zlib as _zlib
from libc.math cimport sqrt
from libc.stdlib cimport malloc, calloc, realloc, free
from libc.string cimport memset


DEF MAP_SIZE = 512
DEF MAP_HEIGHT = 240
DEF MAP_AREA = MAP_SIZE * MAP_SIZE
DEF VOXEL_COUNT = MAP_AREA * MAP_HEIGHT
DEF VOXEL_BITS = (VOXEL_COUNT + 7) // 8
DEF EMPTY_TOP_START = 240
DEF EMPTY_TOP_END = 239
DEF MAP_SEND_ROWS = 4
DEF MAP_PACKET_SIZE = 1024


cdef list _ground_colors = []
cdef int _max_modifiable_z = 238

cdef unsigned int _FREE_SLOT = 0xFFFFFFFF
cdef unsigned int _DEAD_SLOT = 0xFFFFFFFE


cdef class _ColorTable:
    """Voxel index -> packed colour in two flat uint32 arrays.

    A Python dict spends about 100 bytes on each coloured voxel: 235 MB for one
    retail map, more than a 512 MB host has left, and the two Frankfurt servers
    answered nothing while they swapped. This open-addressing table spends 8
    bytes a slot, about 35 MB for the same map. Voxel indexes are below 2**26,
    so the two largest uint32 values are free to mark empty and deleted slots.
    Only the dict operations the map uses are provided.
    """

    cdef unsigned int* _keys
    cdef unsigned int* _values
    cdef Py_ssize_t _capacity
    cdef Py_ssize_t _live
    cdef Py_ssize_t _occupied

    def __cinit__(self):
        self._keys = NULL
        self._values = NULL
        self._capacity = 0
        self._live = 0
        self._occupied = 0
        self._resize(1 << 12)

    def __dealloc__(self):
        free(self._keys)
        free(self._values)

    cdef int _resize(self, Py_ssize_t capacity) except -1:
        cdef unsigned int* old_keys = self._keys
        cdef unsigned int* old_values = self._values
        cdef Py_ssize_t old_capacity = self._capacity
        cdef Py_ssize_t index
        cdef unsigned int* keys = <unsigned int*>malloc(capacity * sizeof(unsigned int))
        cdef unsigned int* values = <unsigned int*>malloc(capacity * sizeof(unsigned int))
        if keys == NULL or values == NULL:
            free(keys)
            free(values)
            raise MemoryError()
        with nogil:
            memset(keys, 0xFF, capacity * sizeof(unsigned int))
        self._keys = keys
        self._values = values
        self._capacity = capacity
        self._live = 0
        self._occupied = 0
        for index in range(old_capacity):
            if old_keys[index] < _DEAD_SLOT:
                self._insert(old_keys[index], old_values[index])
        free(old_keys)
        free(old_values)
        return 0

    cdef inline Py_ssize_t _home(self, unsigned int key) noexcept nogil:
        cdef unsigned int mixed = key
        mixed ^= mixed >> 16
        mixed *= <unsigned int>0x7FEB352D
        mixed ^= mixed >> 15
        mixed *= <unsigned int>0x846CA68B
        mixed ^= mixed >> 16
        return <Py_ssize_t>mixed & (self._capacity - 1)

    cdef Py_ssize_t _find(self, unsigned int key) noexcept nogil:
        cdef Py_ssize_t index = self._home(key)
        while self._keys[index] != _FREE_SLOT:
            if self._keys[index] == key:
                return index
            index = (index + 1) & (self._capacity - 1)
        return -1

    cdef void _insert(self, unsigned int key, unsigned int value) noexcept nogil:
        """Store into a table known to have room."""
        cdef Py_ssize_t index = self._home(key)
        cdef Py_ssize_t reusable = -1
        while self._keys[index] != _FREE_SLOT:
            if self._keys[index] == key:
                self._values[index] = value
                return
            if self._keys[index] == _DEAD_SLOT and reusable < 0:
                reusable = index
            index = (index + 1) & (self._capacity - 1)
        if reusable >= 0:
            index = reusable
        else:
            self._occupied += 1
        self._keys[index] = key
        self._values[index] = value
        self._live += 1

    cdef int _store(self, unsigned int key, unsigned int value) except -1:
        if key >= _DEAD_SLOT:
            raise KeyError(key)
        if (self._occupied + 1) * 5 > self._capacity * 3:
            # Grow only when live entries need it; otherwise just drop tombstones.
            self._resize(self._capacity * 2 if self._live * 5 > self._capacity * 2
                         else self._capacity)
        self._insert(key, value)
        return 0

    cdef int _reserve(self, Py_ssize_t count) except -1:
        """Size an empty table exactly as ``count`` distinct ``_store`` calls
        would grow it, so a GIL-free loader can ``_insert`` without resizing."""
        cdef Py_ssize_t capacity = self._capacity
        while count * 5 > capacity * 3:
            capacity *= 2
        if capacity != self._capacity:
            self._resize(capacity)
        return 0

    cdef bint _discard(self, unsigned int key) noexcept:
        cdef Py_ssize_t index = self._find(key)
        if index < 0:
            return False
        self._keys[index] = _DEAD_SLOT
        self._live -= 1
        return True

    cdef inline unsigned int _value(self, unsigned int key) noexcept:
        """Stored colour, or 0 for a voxel without one."""
        cdef Py_ssize_t index = self._find(key)
        return 0 if index < 0 else self._values[index]

    def __setitem__(self, key, value):
        self._store(<unsigned int>key, <unsigned int>value)

    def __delitem__(self, key):
        if not self._discard(<unsigned int>key):
            raise KeyError(key)

    def __contains__(self, key):
        return self._find(<unsigned int>key) >= 0

    def __len__(self):
        return self._live

    def __sizeof__(self):
        return self._capacity * 2 * sizeof(unsigned int)

    def get(self, key, default=0):
        cdef Py_ssize_t index = self._find(<unsigned int>key)
        if index < 0:
            return default
        return self._values[index]

cdef bytes _EMPTY_COLUMN = b"\x00\xF0\xEF\x00"
cdef bytes _BLANK_VXL = _EMPTY_COLUMN * MAP_AREA


cdef inline int _column_index(int x, int y) noexcept nogil:
    return x + (y << 9)


cdef inline int _voxel_index(int x, int y, int z) noexcept nogil:
    return x + (y << 9) + (z << 18)


cdef inline unsigned int _read_u32_le(bytes data, Py_ssize_t pos):
    return (
        data[pos]
        | (data[pos + 1] << 8)
        | (data[pos + 2] << 16)
        | (data[pos + 3] << 24)
    )


cdef inline unsigned int _read_u32_le_ptr(const unsigned char* data, Py_ssize_t pos) noexcept nogil:
    return (
        data[pos]
        | (<unsigned int>data[pos + 1] << 8)
        | (<unsigned int>data[pos + 2] << 16)
        | (<unsigned int>data[pos + 3] << 24)
    )


cdef inline tuple _color_tuple(unsigned int color):
    cdef int b = color & 0xFF
    cdef int g = (color >> 8) & 0xFF
    cdef int r = (color >> 16) & 0xFF
    cdef int alpha_byte = (color >> 24) & 0xFF
    cdef int a

    if alpha_byte == 0:
        a = 0
    else:
        a = alpha_byte * 2 - 1
    return (r, g, b, a)


cdef inline unsigned int _pack_color_tuple(tuple color_tuple):
    cdef int r
    cdef int g
    cdef int b
    cdef int a
    cdef int alpha_byte

    if len(color_tuple) == 3:
        r = int(color_tuple[0])
        g = int(color_tuple[1])
        b = int(color_tuple[2])
        a = 255
    elif len(color_tuple) == 4:
        r = int(color_tuple[0])
        g = int(color_tuple[1])
        b = int(color_tuple[2])
        a = int(color_tuple[3])
    else:
        raise TypeError("expected a 3-tuple or 4-tuple color")

    if a <= 0:
        alpha_byte = 0
    else:
        alpha_byte = (a + 1) // 2

    return (
        ((alpha_byte & 0xFF) << 24)
        | ((r & 0xFF) << 16)
        | ((g & 0xFF) << 8)
        | (b & 0xFF)
    )


cdef object _coerce_raw_bytes(object source):
    if isinstance(source, bytes):
        return source
    if isinstance(source, bytearray):
        return bytes(source)
    if isinstance(source, str):
        return source.encode("latin1", "ignore")
    return b""


cdef bint _scan_vxl_size(
    const unsigned char* data,
    Py_ssize_t limit,
    Py_ssize_t* columns_out,
    int* max_ref_out,
    Py_ssize_t* colour_words_out,
) noexcept nogil:
    """Validate the column/span walk and count columns, max z and colours.

    Pure C over the raw buffer so map loads can run without the GIL.
    ``colour_words_out`` is the number of explicit colour words the loader
    will store (every span word except headers)."""
    cdef Py_ssize_t pos = 0
    cdef Py_ssize_t columns = 0
    cdef Py_ssize_t colour_words = 0
    cdef int max_ref = 0
    cdef int span_words
    cdef int v1
    cdef int v2
    cdef int v3
    cdef int top_len
    cdef int bottom_len
    cdef Py_ssize_t next_header

    columns_out[0] = 0
    max_ref_out[0] = 0
    colour_words_out[0] = 0
    while pos < limit:
        if pos + 4 > limit:
            return False

        span_words = data[pos]
        v1 = data[pos + 1]
        v2 = data[pos + 2]
        v3 = data[pos + 3]
        if v1 > max_ref:
            max_ref = v1
        if v2 > max_ref:
            max_ref = v2
        if v3 > max_ref:
            max_ref = v3

        while span_words:
            top_len = v2 - v1 + 1 if v2 >= v1 else 0
            bottom_len = span_words - top_len - 1
            next_header = pos + 4 * span_words
            if (v1 > MAP_HEIGHT or v2 >= MAP_HEIGHT or v3 > MAP_HEIGHT
                    or v1 > v2 + 1 or bottom_len < 0 or next_header + 4 > limit):
                return False
            # The following header anchors this span's bottom colour run.
            # Validate before reserving the colour table or reading colours.
            if data[next_header + 3] - bottom_len < v2 + 1:
                return False
            if (data[next_header + 1] < data[next_header + 3]
                    or data[next_header + 1] <= v1):
                return False
            colour_words += span_words - 1
            pos = next_header
            if pos + 4 > limit:
                return False
            span_words = data[pos]
            v1 = data[pos + 1]
            v2 = data[pos + 2]
            v3 = data[pos + 3]
            if v1 > max_ref:
                max_ref = v1
            if v2 > max_ref:
                max_ref = v2
            if v3 > max_ref:
                max_ref = v3

        if v1 > MAP_HEIGHT or v2 >= MAP_HEIGHT or v3 > MAP_HEIGHT or v1 > v2 + 1:
            return False
        if v2 >= v1:
            colour_words += v2 - v1 + 1
            pos += 8 + 4 * (v2 - v1)
        else:
            pos += 4
        columns += 1
        if columns > MAP_AREA:
            return False

    if pos != limit:
        return False
    columns_out[0] = columns
    max_ref_out[0] = max_ref
    colour_words_out[0] = colour_words
    return True


cdef bint _has_classic_height(const unsigned char* data, Py_ssize_t limit) noexcept nogil:
    """Distinguish a 64 sentinel from an explicitly coloured retail z=64.

    The caller has already validated all span offsets and maximum z <= 64.
    """
    cdef Py_ssize_t pos = 0
    cdef int words
    cdef int top_len
    while pos < limit:
        if data[pos + 2] >= 64:
            return False
        words = data[pos]
        top_len = data[pos + 2] - data[pos + 1] + 1
        pos += words * 4 if words else 4 + max(0, top_len) * 4
    return True


cdef tuple _get_vxl_size(bytes data):
    cdef const unsigned char* buf = data
    cdef Py_ssize_t limit = len(data)
    cdef Py_ssize_t columns = 0
    cdef Py_ssize_t colour_words = 0
    cdef int max_ref = 0
    cdef bint ok
    with nogil:
        ok = _scan_vxl_size(buf, limit, &columns, &max_ref, &colour_words)
    if not ok:
        return (0, 0)
    return (columns, max_ref)


cpdef tuple raw_vxl_size(object data):
    """``(columns, max_z_reference)`` of a raw VXL, ``(0, 0)`` if malformed.

    Runs without the GIL and validates span bounds before allocating colours."""
    return _get_vxl_size(_coerce_raw_bytes(data))


cdef inline bint _is_marker_colour(unsigned int color) noexcept nogil:
    cdef unsigned int masked = color & 0x00F0F0F0
    return masked == 0x0000F000 or masked == 0x000000F0


cdef Py_ssize_t _scan_marker_words(
    const unsigned char* data,
    Py_ssize_t limit,
    unsigned int** out_ptr,
) noexcept nogil:
    """Collect (x, y, source_z, colour) quads of chroma-marker words.

    Mirrors ``server.runtime_vxl._iter_explicit_voxels`` exactly (including
    stopping silently at the first malformed span) but keeps only words in
    the native blue/green marker families. Returns the quad count, or -1
    when out of memory. ``out_ptr`` receives a malloc'd buffer to free."""
    cdef Py_ssize_t columns = 0
    cdef Py_ssize_t colour_words = 0
    cdef int max_ref = 0
    cdef int edge
    cdef int offset
    cdef Py_ssize_t pos = 0
    cdef Py_ssize_t span_start
    cdef Py_ssize_t next_header
    cdef Py_ssize_t count = 0
    cdef Py_ssize_t capacity = 0
    cdef unsigned int* out = NULL
    cdef unsigned int* grown
    cdef unsigned int color
    cdef int source_x
    cdef int source_y
    cdef int x
    cdef int y
    cdef int span_words
    cdef int top_start
    cdef int top_end
    cdef int top_len
    cdef int bottom_len
    cdef int bottom_start
    cdef int base_z
    cdef int index
    cdef int run
    cdef int run_len
    cdef Py_ssize_t run_pos

    out_ptr[0] = NULL
    if not _scan_vxl_size(data, limit, &columns, &max_ref, &colour_words):
        return 0
    edge = <int>sqrt(<double>columns) if columns > 0 else 0
    while (edge + 1) * (edge + 1) <= columns:
        edge += 1
    while edge > 0 and edge * edge > columns:
        edge -= 1
    if edge <= 0 or edge * edge != columns or edge > MAP_SIZE:
        return 0
    offset = (MAP_SIZE - edge) // 2
    for source_y in range(edge):
        y = source_y + offset
        for source_x in range(edge):
            x = source_x + offset
            while True:
                if pos + 4 > limit:
                    out_ptr[0] = out
                    return count
                span_start = pos
                span_words = data[pos]
                top_start = data[pos + 1]
                top_end = data[pos + 2]
                top_len = top_end - top_start + 1 if top_end >= top_start else 0
                pos += 4
                if pos + top_len * 4 > limit:
                    out_ptr[0] = out
                    return count
                bottom_len = 0
                bottom_start = 0
                if span_words != 0:
                    bottom_len = span_words - top_len - 1
                    next_header = span_start + span_words * 4
                    if (
                        bottom_len < 0
                        or next_header + 4 > limit
                        or pos + top_len * 4 + bottom_len * 4 != next_header
                    ):
                        # Python yields the top run before this check fails.
                        bottom_len = -1
                    else:
                        bottom_start = data[next_header + 3] - bottom_len
                        if bottom_start < top_end + 1:
                            bottom_len = -1
                for run in range(2):
                    if run == 0:
                        run_len = top_len
                        run_pos = pos
                        base_z = top_start
                    else:
                        if span_words == 0 or bottom_len <= 0:
                            break
                        run_len = bottom_len
                        run_pos = pos + top_len * 4
                        base_z = bottom_start
                    for index in range(run_len):
                        color = _read_u32_le_ptr(data, run_pos + index * 4)
                        if not _is_marker_colour(color):
                            continue
                        if count == capacity:
                            capacity = 64 if capacity == 0 else capacity * 2
                            grown = <unsigned int*>realloc(
                                out, capacity * 4 * sizeof(unsigned int)
                            )
                            if grown == NULL:
                                free(out)
                                return -1
                            out = grown
                        out[count * 4] = x
                        out[count * 4 + 1] = y
                        out[count * 4 + 2] = base_z + index
                        out[count * 4 + 3] = color
                        count += 1
                if span_words == 0:
                    pos += top_len * 4
                    break
                if bottom_len < 0:
                    out_ptr[0] = out
                    return count
                pos = span_start + span_words * 4
    out_ptr[0] = out
    return count


cpdef list find_marker_voxels(object data):
    """``[(x, y, source_z, colour)]`` for explicit chroma-marker words.

    The C twin of filtering ``_iter_explicit_voxels`` by marker colour; the
    walk runs without the GIL so a background map load does not stall the
    main thread."""
    cdef bytes raw = _coerce_raw_bytes(data)
    cdef const unsigned char* buf = raw
    cdef Py_ssize_t limit = len(raw)
    cdef unsigned int* quads = NULL
    cdef Py_ssize_t count
    cdef Py_ssize_t index
    cdef list result = []
    with nogil:
        count = _scan_marker_words(buf, limit, &quads)
    if count < 0:
        raise MemoryError()
    try:
        for index in range(count):
            result.append((
                <int>quads[index * 4],
                <int>quads[index * 4 + 1],
                <int>quads[index * 4 + 2],
                quads[index * 4 + 3],
            ))
    finally:
        free(quads)
    return result


cpdef object A2(object arg):
    return arg


cpdef object add_ground_color(int r, int g, int b, int a=255):
    _ground_colors.append((r, g, b, a))
    return None


cpdef object clamp(object value, object min_val=0, object max_val=1):
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value


cpdef object create_shadow_vbo():
    return None


cpdef object delete_shadow_vbo():
    return None


cpdef object generate_ground_color_table():
    return None


cpdef tuple get_color_tuple(unsigned int color, int include_alpha=0):
    return _color_tuple(color)


cpdef object reset_ground_colors():
    global _ground_colors
    _ground_colors = []
    return None


cpdef bint sphere_in_frustum(float x, float y, float z, float radius):
    return True


cpdef object parse_constant_overrides(object arg):
    return arg


class MapSerializer:
    def __init__(self, vxl_map, delta_mode=True):
        self.vxl_map = vxl_map
        self.delta_mode = delta_mode

    def iter(self):
        cdef int start_row
        for start_row in range(0, MAP_SIZE, MAP_SEND_ROWS):
            yield self.vxl_map.get_chunk(start_row, MAP_SEND_ROWS)


class MapPacker:
    def __init__(self, vxl_map):
        self.vxl_map = vxl_map
        self.serializer = MapSerializer(vxl_map)
        self.crc32 = _zlib.crc32(b"")

    def iter(self):
        compressor = _zlib.compressobj(
            level=6,
            method=_zlib.DEFLATED,
            wbits=15,
            memLevel=8,
            strategy=_zlib.Z_DEFAULT_STRATEGY,
        )
        raw_rows = b""
        compressed = b""
        for raw_rows in self.serializer.iter():
            self.crc32 = _zlib.crc32(raw_rows, self.crc32)
            compressed = compressor.compress(raw_rows)
            if compressed:
                yield compressed

        compressed = compressor.flush(_zlib.Z_FINISH)
        if compressed:
            yield compressed


class MapSyncChunker:
    def __init__(self, vxl_map):
        self.vxl_map = vxl_map
        self.packer = MapPacker(vxl_map)
        self.crc32 = _zlib.crc32(b"")

    def iter(self):
        pending = b""
        total_compressed = 0
        compressed = b""
        for compressed in self.packer.iter():
            self.crc32 = self.packer.crc32
            total_compressed += len(compressed)
            pending += compressed
            while len(pending) >= MAP_PACKET_SIZE:
                yield pending[:MAP_PACKET_SIZE]
                pending = pending[MAP_PACKET_SIZE:]

        if pending:
            yield pending

        self.vxl_map.estimated_size = total_compressed


cdef class CChunk:
    cdef int _x1, _y1, _z1
    cdef int _x2, _y2, _z2

    def __cinit__(self):
        self._x1 = 0
        self._y1 = 0
        self._z1 = 0
        self._x2 = 0
        self._y2 = 0
        self._z2 = 0

    @property
    def x1(self):
        return self._x1

    @property
    def y1(self):
        return self._y1

    @property
    def z1(self):
        return self._z1

    @property
    def x2(self):
        return self._x2

    @property
    def y2(self):
        return self._y2

    @property
    def z2(self):
        return self._z2

    cpdef void delete(self):
        return

    cpdef void draw(self):
        return

    cpdef list get_colors(self):
        return []

    cpdef list to_block_list(self):
        return []


cdef struct _CellMap:
    # Open-addressing voxel index -> component id (bit 31 = grounded/safe).
    unsigned int* keys
    unsigned int* values
    Py_ssize_t capacity
    Py_ssize_t used


cdef int _cellmap_init(_CellMap* table, Py_ssize_t capacity) noexcept nogil:
    table.keys = <unsigned int*>malloc(capacity * sizeof(unsigned int))
    table.values = <unsigned int*>malloc(capacity * sizeof(unsigned int))
    table.capacity = capacity
    table.used = 0
    if table.keys == NULL or table.values == NULL:
        free(table.keys)
        free(table.values)
        table.keys = NULL
        table.values = NULL
        return -1
    memset(table.keys, 0xFF, capacity * sizeof(unsigned int))
    return 0


cdef void _cellmap_free(_CellMap* table) noexcept nogil:
    free(table.keys)
    free(table.values)
    table.keys = NULL
    table.values = NULL


cdef inline Py_ssize_t _cellmap_slot(_CellMap* table, unsigned int key) noexcept nogil:
    cdef unsigned int mixed = key
    cdef Py_ssize_t mask = table.capacity - 1
    cdef Py_ssize_t index
    mixed ^= mixed >> 16
    mixed *= <unsigned int>0x7FEB352D
    mixed ^= mixed >> 15
    mixed *= <unsigned int>0x846CA68B
    mixed ^= mixed >> 16
    index = <Py_ssize_t>mixed & mask
    while table.keys[index] != _FREE_SLOT and table.keys[index] != key:
        index = (index + 1) & mask
    return index


cdef int _cellmap_put(_CellMap* table, unsigned int key, unsigned int value) noexcept nogil:
    cdef Py_ssize_t index
    cdef _CellMap grown
    cdef Py_ssize_t old
    if (table.used + 1) * 2 > table.capacity:
        if _cellmap_init(&grown, table.capacity * 2) < 0:
            return -1
        for old in range(table.capacity):
            if table.keys[old] != _FREE_SLOT:
                index = _cellmap_slot(&grown, table.keys[old])
                grown.keys[index] = table.keys[old]
                grown.values[index] = table.values[old]
                grown.used += 1
        _cellmap_free(table)
        table[0] = grown
    index = _cellmap_slot(table, key)
    if table.keys[index] == _FREE_SLOT:
        table.keys[index] = key
        table.used += 1
    table.values[index] = value
    return 0


cdef struct _CellList:
    unsigned int* items
    Py_ssize_t count
    Py_ssize_t capacity


cdef inline int _celllist_push(_CellList* items, unsigned int value) noexcept nogil:
    cdef unsigned int* grown
    cdef Py_ssize_t capacity
    if items.count == items.capacity:
        capacity = 256 if items.capacity == 0 else items.capacity * 2
        grown = <unsigned int*>realloc(items.items, capacity * sizeof(unsigned int))
        if grown == NULL:
            return -1
        items.items = grown
        items.capacity = capacity
    items.items[items.count] = value
    items.count += 1
    return 0


cdef unsigned int _SAFE_BIT = 0x80000000


cdef void _reset_column_bounds(short* top, short* bottom) noexcept nogil:
    cdef Py_ssize_t col
    for col in range(MAP_AREA):
        top[col] = MAP_HEIGHT
        bottom[col] = -1


cdef class VXL:
    cdef public object minimap_texture
    cdef public int estimated_size
    cdef public bint ready
    cdef int _detail_level
    cdef int _source_size
    cdef int _source_max_z
    cdef int _source_offset
    cdef int _z_shift
    cdef readonly object source_format
    cdef bint _dirty
    cdef bint _overview_dirty
    cdef bytes _raw_data
    cdef bytes _overview_opaque
    cdef bytes _overview_transparent
    cdef _ColorTable _colors
    # Plain C buffers (not bytearray/list) so the loader can fill them with
    # the GIL released: a map load on the transition thread used to freeze
    # the main thread for ~0.4 s.
    cdef unsigned char* _solid_bits
    cdef short* _top_z
    cdef short* _bottom_z
    # One fill colour per column for the implicit solid interior (the VXL
    # format never stores those voxels). Explicit surface runs still live in
    # ``_colors``; everything else that is solid takes the column fill.
    cdef unsigned int* _column_fill

    def __cinit__(self):
        self._column_fill = <unsigned int*>calloc(MAP_AREA, sizeof(unsigned int))
        self._solid_bits = <unsigned char*>calloc(VOXEL_BITS, 1)
        self._top_z = <short*>malloc(MAP_AREA * sizeof(short))
        self._bottom_z = <short*>malloc(MAP_AREA * sizeof(short))
        if (
            self._column_fill == NULL
            or self._solid_bits == NULL
            or self._top_z == NULL
            or self._bottom_z == NULL
        ):
            raise MemoryError()
        _reset_column_bounds(self._top_z, self._bottom_z)
        self.minimap_texture = None
        self.estimated_size = 0
        self.ready = True
        self._detail_level = 2
        self._source_size = MAP_SIZE
        self._source_max_z = EMPTY_TOP_END
        self._source_offset = 0
        self._z_shift = 0
        self.source_format = "retail"
        self._dirty = False
        self._overview_dirty = True
        self._raw_data = _BLANK_VXL
        self._overview_opaque = b""
        self._overview_transparent = b""
        self._colors = _ColorTable()

    def __dealloc__(self):
        free(self._column_fill)
        self._column_fill = NULL
        free(self._solid_bits)
        self._solid_bits = NULL
        free(self._top_z)
        self._top_z = NULL
        free(self._bottom_z)
        self._bottom_z = NULL

    def __init__(self, object state, object source, int size_or_detail, int detail_level=2,
                 source_format="retail"):
        cdef object data = b""
        cdef bint loaded = False

        self._reset_blank()
        if source_format not in ("auto", "retail", "classic64"):
            raise ValueError("VXL source_format must be auto, retail or classic64")

        if isinstance(source, str) and _os.path.exists(source):
            if detail_level == 2 and 0 <= size_or_detail <= 32:
                self._detail_level = size_or_detail
            else:
                self._detail_level = detail_level
            with open(source, "rb") as handle:
                data = handle.read()
        else:
            self._detail_level = detail_level
            data = _coerce_raw_bytes(source)
            if size_or_detail > 0 and len(data) > size_or_detail:
                data = data[:size_or_detail]

        if data:
            loaded = self._load_source(data, source_format)
            if loaded:
                self._raw_data = data
                self._dirty = False
                self.ready = True
            else:
                self._reset_blank()
                self.ready = False

        # The compiled client force-fills the z=239 bed on every load (file and
        # MapSync paths); mirror it so the server collision world matches.
        with nogil:
            self._fill_floor()

    cdef void _reset_blank(self):
        self._colors = _ColorTable()
        self._clear_voxels()
        self._source_size = MAP_SIZE
        self._source_max_z = EMPTY_TOP_END
        self._source_offset = 0
        self._z_shift = 0
        self.source_format = "retail"
        self._raw_data = _BLANK_VXL
        self.estimated_size = 0
        self.ready = True
        self._dirty = False
        self._overview_dirty = True
        self._overview_opaque = b""
        self._overview_transparent = b""

    cdef void _clear_voxels(self) noexcept:
        """All-air grid, empty column bounds and fills (colours untouched)."""
        cdef unsigned char* solid = self._solid_bits
        cdef short* top = self._top_z
        cdef short* bottom = self._bottom_z
        cdef unsigned int* fill = self._column_fill
        with nogil:
            memset(solid, 0, VOXEL_BITS)
            memset(fill, 0, MAP_AREA * sizeof(unsigned int))
            _reset_column_bounds(top, bottom)

    cdef inline bint _in_bounds(self, int x, int y, int z) noexcept nogil:
        return 0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE and 0 <= z < MAP_HEIGHT

    cdef inline bint _solid_at(self, int x, int y, int z) noexcept nogil:
        cdef int index
        cdef int byte_index
        cdef int shift
        if not self._in_bounds(x, y, z):
            return False
        index = _voxel_index(x, y, z)
        byte_index = index >> 3
        shift = index & 7
        return ((self._solid_bits[byte_index] >> shift) & 1) != 0

    cdef inline void _set_solid(self, int x, int y, int z, bint value) noexcept nogil:
        cdef int index
        cdef int byte_index
        cdef int shift
        cdef int mask
        cdef int current

        if not self._in_bounds(x, y, z):
            return

        index = _voxel_index(x, y, z)
        byte_index = index >> 3
        shift = index & 7
        mask = 1 << shift
        current = self._solid_bits[byte_index]
        if value:
            self._solid_bits[byte_index] = current | mask
        else:
            self._solid_bits[byte_index] = current & (~mask & 0xFF)

    cdef inline void _update_column_bounds(self, int x, int y, int z) noexcept nogil:
        cdef int col = _column_index(x, y)
        if z < self._top_z[col]:
            self._top_z[col] = z
        if z > self._bottom_z[col]:
            self._bottom_z[col] = z

    cdef void _recompute_column_bounds(self, int x, int y) noexcept nogil:
        cdef int col = _column_index(x, y)
        cdef int z

        self._top_z[col] = MAP_HEIGHT
        self._bottom_z[col] = -1
        for z in range(MAP_HEIGHT):
            if self._solid_at(x, y, z):
                self._top_z[col] = z
                break
        if self._top_z[col] == MAP_HEIGHT:
            return
        for z in range(MAP_HEIGHT - 1, -1, -1):
            if self._solid_at(x, y, z):
                self._bottom_z[col] = z
                break

    cdef inline void _store_block(self, int x, int y, int z, unsigned int color):
        if not self._in_bounds(x, y, z):
            return
        self._set_solid(x, y, z, True)
        self._update_column_bounds(x, y, z)
        if color:
            self._colors._store(<unsigned int>_voxel_index(x, y, z), color)

    cdef inline void _store_explicit(self, int x, int y, int z, unsigned int color):
        """Store an authored surface colour, keeping colour 0 explicit.

        A file may carry 0 inside a colour run; dropping it would split the
        run on re-serialization and lose the colours behind it."""
        if not self._in_bounds(x, y, z):
            return
        self._set_solid(x, y, z, True)
        self._update_column_bounds(x, y, z)
        self._colors._store(<unsigned int>_voxel_index(x, y, z), color)

    cdef inline void _store_fill(self, int x, int y, int z) noexcept nogil:
        """Mark an implicit interior voxel solid without a colour entry.

        The colour of such a voxel is the column fill (see ``_color_at``);
        storing it per voxel cost 8 bytes each and 286 MB for MayanJungle.
        """
        if not self._in_bounds(x, y, z):
            return
        self._set_solid(x, y, z, True)
        self._update_column_bounds(x, y, z)

    cdef inline unsigned int _color_at(self, int x, int y, int z):
        """Explicit colour, else the column fill for a solid voxel, else 0.

        Never 0 for a solid voxel with a filled column: a colour-0 solid is
        invisible to the client mesher yet blocks bullets."""
        cdef Py_ssize_t index
        if not self._in_bounds(x, y, z):
            return 0
        index = self._colors._find(<unsigned int>_voxel_index(x, y, z))
        if index >= 0:
            return self._colors._values[index]
        if self._solid_at(x, y, z):
            return self._column_fill[_column_index(x, y)]
        return 0

    cdef void _fill_floor(self) noexcept nogil:
        """Force-fill the bottom row (z=239) solid for every column, mirroring
        the compiled engine's post_load_map_setup @0xd140 / initialise_floor.

        The client ALWAYS lays this bed (also on the MapSync network-build
        path), so without it the server world is hollow under open water and a
        0.45-radius box that the client rests on the bed falls through on the
        server -> seabed collision desync. Uses _store_block (NOT set_point) so
        _dirty stays clear and generate_vxl() keeps returning the byte-faithful
        original file; the MapSync stream serializes in-memory columns, so the
        client still receives the bed. Also makes get_z==239 a reliable
        water-column classifier for spawning."""
        cdef int x, y
        cdef int z = MAP_HEIGHT - 1
        for y in range(MAP_SIZE):
            for x in range(MAP_SIZE):
                if not self._solid_at(x, y, z):
                    # _store_block with colour 0 == _store_fill (no entry).
                    self._store_fill(x, y, z)

    cdef bint _load_source(self, bytes data, object source_format):
        cdef const unsigned char* buf = data
        cdef Py_ssize_t limit = len(data)
        cdef Py_ssize_t columns = 0
        cdef Py_ssize_t colour_words = 0
        cdef int max_z = 0
        cdef int edge
        cdef int offset
        cdef int z_shift
        cdef int source_height
        cdef bint ok
        cdef _ColorTable colors

        # Every heavy loop below runs without the GIL: the transition service
        # loads the next map on a worker thread while the live match ticks.
        with nogil:
            ok = _scan_vxl_size(buf, limit, &columns, &max_z, &colour_words)
        if not ok or columns <= 0:
            return False

        edge = int(sqrt(columns))
        if edge * edge != columns or edge > MAP_SIZE or max_z >= 241:
            return False
        if source_format == "auto":
            with nogil:
                ok = edge == MAP_SIZE and max_z <= 64 and _has_classic_height(buf, limit)
            source_format = "classic64" if ok else "retail"
        if source_format == "classic64" and (edge != MAP_SIZE or max_z > 64):
            return False

        colors = _ColorTable()
        # Same final capacity the incremental _store path reached.
        colors._reserve(colour_words)
        self._colors = colors
        self._clear_voxels()
        self._overview_dirty = True
        self._overview_opaque = b""
        self._overview_transparent = b""

        offset = (MAP_SIZE - edge) // 2
        # Retail Battle Builder normalizes legacy/short VXL maps into its
        # 240-high world. The deepest referenced source z becomes the fixed
        # z=239 bed, placing dry terrain beside the z=238 waterplane.
        # Classic's coordinate system is always 64 high, including a map
        # with no explicit floor colour or an empty (64, 63) water column.
        z_shift = 176 if source_format == "classic64" else max(0, EMPTY_TOP_END - max_z)
        source_height = 64 if source_format == "classic64" else MAP_HEIGHT
        self.source_format = source_format

        self._source_size = edge
        self._source_max_z = max_z
        self._source_offset = offset
        self._z_shift = z_shift

        with nogil:
            ok = self._parse_columns(buf, limit, edge, offset, z_shift, colors,
                                     source_height)
        return ok

    cdef inline void _load_explicit(
        self, _ColorTable colors, int x, int y, int z, unsigned int color
    ) noexcept nogil:
        """``_store_explicit`` into a table ``_reserve``d for the whole file."""
        if not self._in_bounds(x, y, z):
            return
        self._set_solid(x, y, z, True)
        self._update_column_bounds(x, y, z)
        colors._insert(<unsigned int>_voxel_index(x, y, z), color)

    cdef bint _parse_columns(
        self,
        const unsigned char* data,
        Py_ssize_t limit,
        int edge,
        int offset,
        int z_shift,
        _ColorTable colors,
        int source_height,
    ) noexcept nogil:
        cdef Py_ssize_t pos = 0
        cdef int src_x
        cdef int src_y
        cdef int x
        cdef int y
        cdef int span_words
        cdef int top_start
        cdef int top_end
        cdef int top_len
        cdef int bottom_len
        cdef int next_air_start
        cdef int bottom_start
        cdef int z
        cdef int i
        cdef int has_surface
        cdef unsigned int color = 0
        cdef unsigned int surface_color

        for src_y in range(edge):
            y = src_y + offset
            for src_x in range(edge):
                x = src_x + offset
                has_surface = 0
                surface_color = 0
                while True:
                    if pos + 4 > limit:
                        return False

                    span_words = data[pos]
                    top_start = data[pos + 1]
                    top_end = data[pos + 2]
                    if (top_start > source_height or top_end >= source_height
                            or data[pos + 3] > source_height):
                        return False
                    if source_height == 64 and top_start > top_end + 1:
                        return False
                    pos += 4

                    if top_end >= top_start:
                        top_len = top_end - top_start + 1
                        if pos + (top_len * 4) > limit:
                            return False
                        for i in range(top_len):
                            color = _read_u32_le_ptr(data, pos + (i * 4))
                            self._load_explicit(colors, x, y, top_start + z_shift + i, color)
                        pos += top_len * 4
                        has_surface = 1
                        # Remember the deepest surface color so the underground
                        # fill below can inherit it (see the span_words==0 branch).
                        surface_color = color
                    else:
                        top_len = 0

                    if span_words == 0:
                        # LAST span of the column: in the AoS/VXL format
                        # everything below the final surface run is SOLID
                        # underground, down to the map floor. Without this fill
                        # the loader (which starts all-air and only adds explicit
                        # solids) leaves the column a thin surface shell over a
                        # lone bedrock voxel -> the whole map renders FLOATING
                        # client-side, and one dug block cascade-collapses it.
                        # Measured 2026-07-09: CityOfChicago col (256,128) went
                        # from [188,239] (2 solids) to [188..239] (52, grounded).
                        #
                        # BUT only for columns that HAVE a land surface. Open
                        # water is stored as an EMPTY column (no top run anywhere,
                        # raw surface None) whose only solid is the z=239 waterbed
                        # laid by _fill_floor; the client renders water wherever a
                        # column is empty above that bed. Filling those solid
                        # (CityOfChicago col (0,0) -> [200..239]) DELETES the water.
                        # has_surface gates the fill so water columns stay water.
                        #
                        # Fill with the SURFACE color, NOT 0: a color-0 solid
                        # block is INVISIBLE to the client mesher (it renders
                        # nothing) yet still blocks bullets -> "invisible blocks
                        # that only appear when you shoot them" where the fill is
                        # exposed (cliffs, land/water edges). Inheriting the
                        # surface color makes exposed underground render as
                        # terrain. Measured 2026-07-09: fill was color 0 at z>=176.
                        if has_surface:
                            self._column_fill[_column_index(x, y)] = surface_color
                            for z in range(top_end + 1, MAP_HEIGHT - z_shift):
                                self._store_fill(x, y, z + z_shift)
                        break

                    bottom_len = span_words - top_len - 1
                    if bottom_len < 0:
                        return False
                    if pos + (bottom_len * 4) + 4 > limit:
                        return False

                    next_air_start = data[pos + (bottom_len * 4) + 3]
                    if source_height == 64 and next_air_start > data[pos + (bottom_len * 4) + 1]:
                        return False
                    bottom_start = next_air_start - bottom_len
                    if bottom_start < top_end + 1:
                        return False

                    # Solid interior between the top surface run and the bottom
                    # cave-ceiling run. Use the surface color (NOT 0): a color-0
                    # solid block is invisible to the mesher yet blocks bullets,
                    # so exposed interior (cave mouths, cliffs) reads as
                    # "invisible blocks you can only see after shooting them".
                    self._column_fill[_column_index(x, y)] = surface_color
                    for z in range(top_end + 1, bottom_start):
                        self._store_fill(x, y, z + z_shift)

                    for i in range(bottom_len):
                        color = _read_u32_le_ptr(data, pos + (i * 4))
                        self._load_explicit(colors, x, y, bottom_start + z_shift + i, color)
                    pos += bottom_len * 4

        if pos != limit:
            return False
        return True

    cdef void _ensure_overview(self):
        cdef bytearray opaque
        cdef bytearray transparent
        cdef int col
        cdef int out_pos
        cdef int top_z
        cdef int x
        cdef int y
        cdef int alpha
        cdef unsigned int color
        cdef tuple color_tuple

        if not self._overview_dirty and self._overview_opaque and self._overview_transparent:
            return

        opaque = bytearray(MAP_AREA * 4)
        transparent = bytearray(MAP_AREA * 4)

        for col in range(MAP_AREA):
            out_pos = col << 2
            top_z = self._top_z[col]
            if top_z >= MAP_HEIGHT:
                opaque[out_pos + 3] = 255
                continue

            x = col & 511
            y = col >> 9
            color = self._color_at(x, y, top_z)
            color_tuple = _color_tuple(color)
            opaque[out_pos] = color_tuple[0]
            opaque[out_pos + 1] = color_tuple[1]
            opaque[out_pos + 2] = color_tuple[2]
            opaque[out_pos + 3] = 255

            transparent[out_pos] = color_tuple[0]
            transparent[out_pos + 1] = color_tuple[1]
            transparent[out_pos + 2] = color_tuple[2]
            alpha = color_tuple[3]
            if alpha < 0:
                alpha = 0
            elif alpha > 255:
                alpha = 255
            transparent[out_pos + 3] = alpha

        self._overview_opaque = bytes(opaque)
        self._overview_transparent = bytes(transparent)
        self._overview_dirty = False

    cdef tuple _column_runs_source(self, int map_x, int map_y):
        cdef list runs = []
        cdef int col = _column_index(map_x, map_y)
        cdef int top_z = self._top_z[col]
        cdef int bottom_z = self._bottom_z[col]
        cdef int source_z
        cdef int run_start = -1

        if top_z >= MAP_HEIGHT or bottom_z < top_z:
            return ()

        top_z -= self._z_shift
        bottom_z -= self._z_shift

        if top_z < 0:
            top_z = 0
        if bottom_z > EMPTY_TOP_END:
            bottom_z = EMPTY_TOP_END
        if bottom_z < top_z:
            return ()

        for source_z in range(top_z, bottom_z + 1):
            if self._solid_at(map_x, map_y, source_z + self._z_shift):
                if run_start < 0:
                    run_start = source_z
            elif run_start >= 0:
                runs.append((run_start, source_z - 1))
                run_start = -1

        if run_start >= 0:
            runs.append((run_start, bottom_z))
        return tuple(runs)

    cdef unsigned int _surface_color_source(self, int map_x, int map_y, int source_z):
        return self._color_at(map_x, map_y, source_z + self._z_shift)

    cdef tuple _column_runs_world(self, int map_x, int map_y):
        cdef list runs = []
        cdef int col = _column_index(map_x, map_y)
        cdef int top_z = self._top_z[col]
        cdef int bottom_z = self._bottom_z[col]
        cdef int z
        cdef int run_start = -1
        if top_z >= MAP_HEIGHT or bottom_z < top_z:
            return ()
        for z in range(max(0, top_z), min(EMPTY_TOP_END, bottom_z) + 1):
            if self._solid_at(map_x, map_y, z):
                if run_start < 0:
                    run_start = z
            elif run_start >= 0:
                runs.append((run_start, z - 1))
                run_start = -1
        if run_start >= 0:
            runs.append((run_start, min(EMPTY_TOP_END, bottom_z)))
        return tuple(runs)

    cdef unsigned int _surface_color_world(self, int map_x, int map_y, int z):
        return self._color_at(map_x, map_y, z)

    cdef bytes _serialize_column(self, int map_x, int map_y):
        cdef bytearray out = bytearray()
        cdef tuple runs
        cdef int run_index
        cdef int run_count
        cdef tuple run
        cdef int run_start
        cdef int run_end
        cdef int top_end
        cdef int bottom_start
        cdef int z
        cdef int prev_air_start
        cdef int span_words
        cdef list top_colors
        cdef list bottom_colors
        cdef unsigned int color

        runs = self._column_runs_world(map_x, map_y)
        if not runs:
            out.extend((0, EMPTY_TOP_START, EMPTY_TOP_END, 0))
            return bytes(out)

        prev_air_start = 0
        run_count = len(runs)
        for run_index in range(run_count):
            run = runs[run_index]
            run_start = int(run[0])
            run_end = int(run[1])

            if run_index == run_count - 1:
                # Last span: explicit colours only for the surface run; the
                # solid interior below it is implicit in the VXL format and
                # the client fills it exactly as it does for the map file.
                top_end = run_start
                for z in range(run_end, run_start, -1):
                    if _voxel_index(map_x, map_y, z) in self._colors:
                        top_end = z
                        break
                out.extend((0, run_start, top_end, prev_air_start))
                for z in range(run_start, top_end + 1):
                    color = self._surface_color_world(map_x, map_y, z)
                    out.extend((
                        color & 0xFF,
                        (color >> 8) & 0xFF,
                        (color >> 16) & 0xFF,
                        (color >> 24) & 0xFF,
                    ))
                continue

            bottom_start = run_end
            while bottom_start > run_start + 1:
                if _voxel_index(map_x, map_y, bottom_start - 1) not in self._colors:
                    break
                bottom_start -= 1
            # The top run covers every authored colour above the bottom
            # run (implicit voxels in between are sent with the column
            # fill); only the trailing implicit stretch stays implicit.
            top_end = run_start
            for z in range(bottom_start - 1, run_start, -1):
                if _voxel_index(map_x, map_y, z) in self._colors:
                    top_end = z
                    break

            top_colors = []
            for z in range(run_start, top_end + 1):
                top_colors.append(self._surface_color_world(map_x, map_y, z))

            # A fully explicit run is already covered by ``top_colors``.
            # Starting the bottom run at ``run_end`` unconditionally used to
            # serialize that last voxel twice.  For a one-voxel island this
            # produced ``span_words=3`` with top_start == top_end, which the
            # retail span decoder rejects because the inferred bottom range
            # overlaps the top range.  A rejected dirty column leaves the
            # client with stale/blank geometry while server collision has
            # already advanced.
            bottom_colors = []
            if bottom_start > top_end:
                for z in range(bottom_start, run_end + 1):
                    bottom_colors.append(
                        self._surface_color_world(map_x, map_y, z)
                    )

            span_words = 1 + len(top_colors) + len(bottom_colors)
            out.extend((span_words, run_start, top_end, prev_air_start))

            for color in top_colors:
                out.extend((
                    color & 0xFF,
                    (color >> 8) & 0xFF,
                    (color >> 16) & 0xFF,
                    (color >> 24) & 0xFF,
                ))

            for color in bottom_colors:
                out.extend((
                    color & 0xFF,
                    (color >> 8) & 0xFF,
                    (color >> 16) & 0xFF,
                    (color >> 24) & 0xFF,
                ))

            prev_air_start = run_end + 1

        return bytes(out)

    cdef bytes _serialize_dirty(self):
        cdef bytearray out = bytearray()
        cdef int src_x
        cdef int src_y
        cdef int map_x
        cdef int map_y
        cdef tuple runs
        cdef int run_index
        cdef int run_count
        cdef tuple run
        cdef int run_start
        cdef int run_end
        cdef int top_end
        cdef int bottom_start
        cdef int z
        cdef int prev_air_start
        cdef int span_words
        cdef list top_colors
        cdef list bottom_colors
        cdef unsigned int color

        for src_y in range(self._source_size):
            map_y = self._source_offset + src_y
            for src_x in range(self._source_size):
                map_x = self._source_offset + src_x
                runs = self._column_runs_source(map_x, map_y)
                if not runs:
                    out.extend((0, EMPTY_TOP_START, EMPTY_TOP_END, 0))
                    continue

                prev_air_start = 0
                run_count = len(runs)
                for run_index in range(run_count):
                    run = runs[run_index]
                    run_start = int(run[0])
                    run_end = int(run[1])

                    if run_index == run_count - 1:
                        top_end = run_start
                        for z in range(run_end, run_start, -1):
                            if _voxel_index(map_x, map_y, z + self._z_shift) in self._colors:
                                top_end = z
                                break
                        out.extend((0, run_start, top_end, prev_air_start))
                        for z in range(run_start, top_end + 1):
                            color = self._surface_color_source(map_x, map_y, z)
                            out.extend((
                                color & 0xFF,
                                (color >> 8) & 0xFF,
                                (color >> 16) & 0xFF,
                                (color >> 24) & 0xFF,
                            ))
                        continue

                    bottom_start = run_end
                    while bottom_start > run_start + 1:
                        if _voxel_index(map_x, map_y, bottom_start - 1 + self._z_shift) not in self._colors:
                            break
                        bottom_start -= 1
                    top_end = run_start
                    for z in range(bottom_start - 1, run_start, -1):
                        if _voxel_index(map_x, map_y, z + self._z_shift) in self._colors:
                            top_end = z
                            break

                    top_colors = []
                    for z in range(run_start, top_end + 1):
                        top_colors.append(self._surface_color_source(map_x, map_y, z))

                    bottom_colors = []
                    if bottom_start > top_end:
                        for z in range(bottom_start, run_end + 1):
                            bottom_colors.append(
                                self._surface_color_source(map_x, map_y, z)
                            )

                    span_words = 1 + len(top_colors) + len(bottom_colors)
                    out.extend((span_words, run_start, top_end, prev_air_start))

                    for color in top_colors:
                        out.extend((
                            color & 0xFF,
                            (color >> 8) & 0xFF,
                            (color >> 16) & 0xFF,
                            (color >> 24) & 0xFF,
                        ))

                    for color in bottom_colors:
                        out.extend((
                            color & 0xFF,
                            (color >> 8) & 0xFF,
                            (color >> 16) & 0xFF,
                            (color >> 24) & 0xFF,
                        ))

                    prev_air_start = run_end + 1

        return bytes(out)

    cpdef object add_point(self, object x, object y, object z, tuple color_tuple):
        return self.set_point(x, y, z, color_tuple)

    cpdef object set_point(self, object x, object y, object z, object color, object maybe_color=None):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        cdef unsigned int packed
        if not self._in_bounds(xi, yi, zi):
            return None

        if maybe_color is not None or isinstance(color, bool):
            if not bool(color):
                return self.remove_point(xi, yi, zi)
            color = 0 if maybe_color is None else maybe_color

        if isinstance(color, tuple):
            packed = _pack_color_tuple(color)
        else:
            packed = <unsigned int>int(color)

        self._store_block(xi, yi, zi, packed)
        if not packed and _voxel_index(xi, yi, zi) in self._colors:
            del self._colors[_voxel_index(xi, yi, zi)]
        self._dirty = True
        self._overview_dirty = True
        return None

    cpdef object remove_point(self, object x, object y, object z):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        cdef int key

        if not self._in_bounds(xi, yi, zi):
            return None

        key = _voxel_index(xi, yi, zi)
        self._set_solid(xi, yi, zi, False)
        if key in self._colors:
            del self._colors[key]
        self._recompute_column_bounds(xi, yi)
        self._dirty = True
        self._overview_dirty = True
        return None

    cpdef object remove_point_nochecks(self, object x, object y, object z):
        return self.remove_point(x, y, z)

    cpdef object destroy_point(self, object x, object y, object z):
        return self.remove_point(x, y, z)

    cpdef object color_block(self, object x, object y, object z, object color=0xFFFFFF):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        cdef unsigned int packed

        if isinstance(color, tuple):
            packed = _pack_color_tuple(color)
        else:
            packed = <unsigned int>int(color)

        if not self._in_bounds(xi, yi, zi):
            return None

        self._store_block(xi, yi, zi, packed)
        self._dirty = True
        self._overview_dirty = True
        return None

    cpdef object check_only(self, object x, object y, object z):
        return None

    cpdef void clear_checked_geometry(self):
        return

    cpdef bint can_build(self, object x, object y, object z):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        if not self._in_bounds(xi, yi, zi):
            return False
        if zi > _max_modifiable_z:
            return False
        return not self._solid_at(xi, yi, zi)

    cpdef bint get_solid(self, object x, object y, object z):
        return self._solid_at(int(x), int(y), int(z))

    cpdef tuple get_point(self, object x, object y, object z):
        cdef bint solid = self.get_solid(x, y, z)
        return (solid, self.get_color_tuple(x, y, z))

    cpdef bint has_explicit_color(self, object x, object y, object z):
        """True when the voxel carries an authored/placed colour entry.

        Solid voxels without one are implicit interior: their colour is
        client-owned and never transmitted."""
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        if not self._in_bounds(xi, yi, zi):
            return False
        return self._colors._find(<unsigned int>_voxel_index(xi, yi, zi)) >= 0

    cpdef Py_ssize_t color_entries(self):
        """Number of voxels with an explicit colour (memory diagnostics)."""
        return len(self._colors)

    cpdef unsigned int column_fill_color(self, object x, object y):
        """Fill colour used for a column's implicit solid interior."""
        cdef int xi = int(x)
        cdef int yi = int(y)
        if not (0 <= xi < MAP_SIZE and 0 <= yi < MAP_SIZE):
            return 0
        return self._column_fill[_column_index(xi, yi)]

    cpdef unsigned int get_color(self, object x, object y, object z):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        return self._color_at(xi, yi, zi)

    cpdef int get_z(self, object x, object y, object start=0):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi
        cdef int first = int(start)
        cdef int col
        if not (0 <= xi < MAP_SIZE and 0 <= yi < MAP_SIZE):
            return MAP_HEIGHT - 1
        col = _column_index(xi, yi)
        if self._top_z[col] >= MAP_HEIGHT:
            return MAP_HEIGHT - 1
        if first <= 0:
            return self._top_z[col]
        if first >= MAP_HEIGHT:
            return MAP_HEIGHT - 1
        for zi in range(first, MAP_HEIGHT):
            if self._solid_at(xi, yi, zi):
                return zi
        return self._top_z[col]

    cpdef int surface_z(self, int x, int y):
        """Topmost solid z by a direct column scan, else ``MAP_HEIGHT - 1``.

        Same answer as probing ``get_solid`` from z=0 downwards (the spawn
        code's deliberate get_z-independent scan), at C speed."""
        cdef int z
        if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
            return MAP_HEIGHT - 1
        for z in range(MAP_HEIGHT):
            if self._solid_at(x, y, z):
                return z
        return MAP_HEIGHT - 1

    cdef int _collapse_walk(
        self,
        _CellMap* seen,
        _CellList* stack,
        _CellList* comp,
        _CellList* comp_seen,
        unsigned int start_key,
        unsigned int comp_id,
        const int* ndx,
        const int* ndy,
        const int* ndz,
        int neighbor_count,
        long long work_budget,
    ) noexcept nogil:
        """One ``find_unsupported_chunks`` flood from ``start_key``.

        Returns 1 grounded, 2 budget exhausted, 0 floating (``comp`` then
        holds the component in pop order) or -1 out of memory. "Seen by this
        flood" is ``seen[key] == comp_id``; bit 31 marks ``safe``.
        """
        cdef unsigned int key
        cdef unsigned int next_key
        cdef Py_ssize_t slot
        cdef long long work = 0
        cdef int j
        cdef int cx
        cdef int cy
        cdef int cz
        cdef int nx
        cdef int ny
        cdef int nz

        comp.count = 0
        comp_seen.count = 0
        stack.count = 0
        if (
            _cellmap_put(seen, start_key, comp_id) < 0
            or _celllist_push(comp_seen, start_key) < 0
            or _celllist_push(stack, start_key) < 0
        ):
            return -1
        while stack.count > 0:
            stack.count -= 1
            key = stack.items[stack.count]
            cx = key & 511
            cy = (key >> 9) & 511
            cz = key >> 18
            if cz > 238:
                return 1
            if _celllist_push(comp, key) < 0:
                return -1
            for j in range(neighbor_count):
                work += 1
                if work > work_budget:
                    return 2
                nx = cx + ndx[j]
                ny = cy + ndy[j]
                nz = cz + ndz[j]
                if not self._in_bounds(nx, ny, nz):
                    # Out of bounds: never safe, never solid.
                    continue
                next_key = <unsigned int>_voxel_index(nx, ny, nz)
                slot = _cellmap_slot(seen, next_key)
                if seen.keys[slot] == next_key:
                    if seen.values[slot] & _SAFE_BIT:
                        return 1
                    if seen.values[slot] == comp_id:
                        continue
                if self._solid_at(nx, ny, nz):
                    if (
                        _cellmap_put(seen, next_key, comp_id) < 0
                        or _celllist_push(comp_seen, next_key) < 0
                        or _celllist_push(stack, next_key) < 0
                    ):
                        return -1
        return 0

    cpdef list find_unsupported_chunks(
        self, object removed_positions, object neighbors, long long work_budget
    ):
        """Same result as ``WorldManager.find_unsupported_chunks``, in C.

        The Python flood returns only the floating components: each one is
        a whole 18-connected component that never reaches z > 238, listed in
        its DFS pop order from the first start (in ``removed_positions`` x
        ``neighbors`` order) that lies in it, and omitted when its flood runs
        past ``work_budget`` probes. A complete flood probes every popped
        cell 18 times, so that exhaustion depends only on the component's
        size, never on the start or on what earlier floods marked ``safe``;
        and grounded components never produce output however they are
        proven grounded. So each new start is first probed with a
        down-first order that reaches the base plane in ~one column instead
        of wandering the terrain (1.6 ms/call in Python; ~0.2 ms with the
        original order in C), and only a component that probe finds floating
        is re-walked in the exact original order to produce its list.
        Bounds and solidity match ``WorldManager.get_solid``.
        """
        cdef int neighbor_count = len(neighbors)
        cdef int* ndx = NULL
        cdef int* ndy = NULL
        cdef int* ndz = NULL
        cdef int* pdx = NULL
        cdef int* pdy = NULL
        cdef int* pdz = NULL
        cdef _CellMap seen
        cdef _CellList stack
        cdef _CellList comp
        cdef _CellList comp_seen
        cdef list chunks = []
        cdef list chunk
        cdef list probe_order
        cdef unsigned int comp_id = 0
        cdef unsigned int key
        cdef Py_ssize_t slot
        cdef Py_ssize_t index
        cdef int i
        cdef int sx
        cdef int sy
        cdef int sz
        cdef int cx
        cdef int cy
        cdef int cz
        cdef int result
        cdef object position

        if neighbor_count <= 0:
            return chunks
        stack.items = NULL
        stack.count = 0
        stack.capacity = 0
        comp.items = NULL
        comp.count = 0
        comp.capacity = 0
        comp_seen.items = NULL
        comp_seen.count = 0
        comp_seen.capacity = 0
        seen.keys = NULL
        seen.values = NULL
        ndx = <int*>malloc(neighbor_count * sizeof(int))
        ndy = <int*>malloc(neighbor_count * sizeof(int))
        ndz = <int*>malloc(neighbor_count * sizeof(int))
        pdx = <int*>malloc(neighbor_count * sizeof(int))
        pdy = <int*>malloc(neighbor_count * sizeof(int))
        pdz = <int*>malloc(neighbor_count * sizeof(int))
        try:
            if (
                ndx == NULL or ndy == NULL or ndz == NULL
                or pdx == NULL or pdy == NULL or pdz == NULL
            ):
                raise MemoryError()
            for i in range(neighbor_count):
                ndx[i] = int(neighbors[i][0])
                ndy[i] = int(neighbors[i][1])
                ndz[i] = int(neighbors[i][2])
            # Probe order: the stack pops the last push first, so push by
            # ascending dz with straight-down (0, 0, +1) last.
            probe_order = sorted([
                (ndz[i], ndx[i] == 0 and ndy[i] == 0, i)
                for i in range(neighbor_count)
            ])
            for i in range(neighbor_count):
                pdx[i] = ndx[<int>probe_order[i][2]]
                pdy[i] = ndy[<int>probe_order[i][2]]
                pdz[i] = ndz[<int>probe_order[i][2]]
            if _cellmap_init(&seen, 1 << 12) < 0:
                raise MemoryError()

            for position in removed_positions:
                sx = int(position[0])
                sy = int(position[1])
                sz = int(position[2])
                for i in range(neighbor_count):
                    cx = sx + ndx[i]
                    cy = sy + ndy[i]
                    cz = sz + ndz[i]
                    if not self._solid_at(cx, cy, cz):
                        continue
                    key = <unsigned int>_voxel_index(cx, cy, cz)
                    slot = _cellmap_slot(&seen, key)
                    if seen.keys[slot] != _FREE_SLOT:
                        # Already in a probed component: grounded/exhausted
                        # ones are safe, floating ones were emitted in full.
                        continue
                    comp_id += 1
                    # Keep the GIL: each walk is microseconds (the
                    # down-first probe reaches the base plane in about
                    # one column). Releasing it per start handed the
                    # interpreter to the busy bot-AI thread for a whole
                    # switch interval (~15.6 ms on Windows timers) dozens
                    # of times per explosion: the 30-50 ms projectile
                    # tick spikes.
                    result = self._collapse_walk(
                        &seen, &stack, &comp, &comp_seen, key, comp_id,
                        pdx, pdy, pdz, neighbor_count, work_budget,
                    )
                    if result < 0:
                        raise MemoryError()
                    if result == 0:
                        # Floating: re-walk from the same start in the
                        # original order under a fresh id for the exact list.
                        comp_id += 1
                        result = self._collapse_walk(
                            &seen, &stack, &comp, &comp_seen, key, comp_id,
                            ndx, ndy, ndz, neighbor_count, work_budget,
                        )
                        if result < 0:
                            raise MemoryError()
                    if result == 0:
                        chunk = []
                        for index in range(comp.count):
                            key = comp.items[index]
                            chunk.append((
                                <int>(key & 511),
                                <int>((key >> 9) & 511),
                                <int>(key >> 18),
                            ))
                        chunks.append(chunk)
                    else:
                        for index in range(comp_seen.count):
                            slot = _cellmap_slot(&seen, comp_seen.items[index])
                            seen.values[slot] = comp_id | _SAFE_BIT
        finally:
            free(ndx)
            free(ndy)
            free(ndz)
            free(pdx)
            free(pdy)
            free(pdz)
            free(stack.items)
            free(comp.items)
            free(comp_seen.items)
            _cellmap_free(&seen)
        return chunks

    cpdef tuple get_color_tuple(self, object x, object y, object z):
        return _color_tuple(self.get_color(x, y, z))

    cpdef tuple get_random_pos(self, int x1, int y1, int x2, int y2):
        cdef int min_x = max(0, min(x1, x2))
        cdef int max_x = min(MAP_SIZE - 1, max(x1, x2) - 1)
        cdef int min_y = max(0, min(y1, y2))
        cdef int max_y = min(MAP_SIZE - 1, max(y1, y2) - 1)
        cdef int xi
        cdef int yi
        cdef int attempts

        if max_x < min_x:
            max_x = min_x
        if max_y < min_y:
            max_y = min_y

        cdef int zi

        # A column is DRY land iff its topmost solid is above the waterplane
        # (get_z <= MAP_HEIGHT-2 = 238). Sea/lake columns and empty columns
        # report the forced bed at z=239 and are rejected — the old test
        # (_bottom_z >= 0, "any solid anywhere") accepted every sea column on
        # the first draw, dropping spawns into the water at the shoreline.
        for attempts in range(64):
            xi = _random.randint(min_x, max_x)
            yi = _random.randint(min_y, max_y)
            zi = self.get_z(xi, yi)
            if zi <= MAP_HEIGHT - 2:
                return (xi, yi, zi)

        # No dry column drawn in 64 tries: scan deterministically for the first
        # one so an almost-fully-wet rect still lands on land where possible.
        for yi in range(min_y, max_y + 1):
            for xi in range(min_x, max_x + 1):
                zi = self.get_z(xi, yi)
                if zi <= MAP_HEIGHT - 2:
                    return (xi, yi, zi)

        xi = min_x
        yi = min_y
        return (xi, yi, self.get_z(xi, yi))

    cpdef bint has_neighbors(self, object x, object y, object z, object check_water):
        cdef int xi = int(x)
        cdef int yi = int(y)
        cdef int zi = int(z)
        return (
            self._solid_at(xi + 1, yi, zi)
            or self._solid_at(xi - 1, yi, zi)
            or self._solid_at(xi, yi + 1, zi)
            or self._solid_at(xi, yi - 1, zi)
            or self._solid_at(xi, yi, zi + 1)
            or self._solid_at(xi, yi, zi - 1)
        )

    cpdef list block_line(self, int x1, int y1, int z1, int x2, int y2, int z2):
        cdef int steps = max(abs(x2 - x1), abs(y2 - y1), abs(z2 - z1))
        cdef int index
        cdef int xi
        cdef int yi
        cdef int zi
        cdef list result = []
        if steps <= 0:
            return [(x1, y1, z1)]
        for index in range(steps + 1):
            xi = int(round(x1 + ((x2 - x1) * index) / float(steps)))
            yi = int(round(y1 + ((y2 - y1) * index) / float(steps)))
            zi = int(round(z1 + ((z2 - z1) * index) / float(steps)))
            # wraparound=False turns a negative typed-list index into an
            # unchecked PyList_GET_ITEM(-1), not Python's last-item lookup.
            if not result or result[len(result) - 1] != (xi, yi, zi):
                result.append((xi, yi, zi))
        return result

    cpdef bint is_space_to_add_blocks(self):
        return True

    cpdef void add_static_light(self, int x, int y, int z, int r, int g, int b, float intensity=1.0):
        return

    cpdef void update_static_light_colour(self, int x, int y, int z, int r, int g, int b):
        return

    cpdef void remove_static_light(self, int x, int y, int z):
        return

    cpdef void create_spot_shadows(self, object positions):
        return

    cpdef void set_shadow_char_height(self, int height):
        return

    cpdef void draw_spot_shadows(self):
        return

    cpdef void draw(self, object x=None, object y=None, object z=None, object draw_distance=None):
        return

    cpdef void draw_sea(self):
        return

    cpdef void post_load_draw_setup(self, object arg=None):
        self._ensure_overview()
        return

    def get_overview(self, transparent=None):
        if transparent is None:
            self._ensure_overview()
            return self._overview_opaque
        if not isinstance(transparent, int):
            raise TypeError("an integer is required")
        self._ensure_overview()
        return self._overview_transparent

    cpdef bint get_prefab_touches_world(self, object kv6, int x, int y, int z, int rx=0, int ry=0, int rz=0, int scale=1):
        return False

    cpdef void place_prefab_in_world(self, object kv6, int x, int y, int z, int rx=0, int ry=0, int rz=0, int scale=1, int flags=0, float tolerance=0.0):
        return

    cpdef void erase_prefab_from_world(self, object kv6, int x, int y, int z, int rx=0, int ry=0, int rz=0, int scale=1, int flags=0, float tolerance=0.0):
        return

    cpdef list get_ground_colors(self):
        return _ground_colors

    cpdef void refresh_ground_colors(self):
        self._overview_dirty = True
        return

    cpdef void set_max_modifiable_z(self, int z):
        global _max_modifiable_z
        _max_modifiable_z = z

    cpdef int get_max_modifiable_z(self):
        return _max_modifiable_z

    cpdef bint done_processing(self):
        return False

    cpdef void change_thread_state(self, int mode, object data=None, int data_size=0):
        return

    cpdef list chunk_to_pointlist(self, object chunk):
        if isinstance(chunk, CChunk):
            return (<CChunk>chunk).to_block_list()
        return []

    cpdef bytes get_chunk(self, int start_row, int num_rows):
        cdef bytearray chunk = bytearray()
        cdef int y
        cdef int x
        cdef int end_row = min(start_row + num_rows, MAP_SIZE)

        if start_row < 0:
            start_row = 0
        if end_row < start_row:
            end_row = start_row

        for y in range(start_row, end_row):
            for x in range(MAP_SIZE):
                chunk.extend(_struct.pack("<II", x, y))
                chunk.extend(self._serialize_column(x, y))

        return bytes(chunk)

    cpdef bytes serialize_columns(self, object columns):
        """Serialize specific (x, y) columns in the map-sync record format
        (u32 x, u32 y, column spans) — the delta complement of get_chunk."""
        cdef bytearray out = bytearray()
        cdef int x
        cdef int y
        for item in columns:
            x = int(item[0])
            y = int(item[1])
            if not (0 <= x < MAP_SIZE and 0 <= y < MAP_SIZE):
                continue
            out.extend(_struct.pack("<II", x, y))
            out.extend(self._serialize_column(x, y))
        return bytes(out)

    cpdef object get_chunker(self):
        return MapSyncChunker(self)

    cpdef bytes get_bytes(self):
        return self.generate_vxl(False)

    cpdef bytes generate_vxl(self, bint compress=True):
        if not self._dirty and self._raw_data:
            return self._raw_data
        self._raw_data = self._serialize_dirty()
        self._dirty = False
        return self._raw_data

    cpdef void destroy(self):
        self._reset_blank()
        return

    cpdef void cleanup(self):
        return

    def __bytes__(self):
        return self.get_bytes()


cdef class array:
    cdef public tuple shape
    cdef public int itemsize
    cdef public str format
    cdef public str mode
    cdef public bint allocate_buffer
    cdef public object data

    def __init__(self, tuple shape, str typestr="f", int itemsize=4, str format="f", str mode="c", bint allocate_buffer=True):
        cdef int total
        cdef int dim
        self.shape = shape
        self.itemsize = itemsize
        self.format = format
        self.mode = mode
        self.allocate_buffer = allocate_buffer
        self.data = None

        if allocate_buffer:
            total = itemsize
            for dim in shape:
                total *= dim
            self.data = bytearray(total)

    @property
    def memview(self):
        return memoryview(self.data)

    def __len__(self):
        if self.shape:
            return self.shape[0]
        return 0


cdef class memoryview:
    cdef public object obj
    cdef object _base
    cdef public bint dtype_is_object
    cdef tuple _shape
    cdef tuple _strides
    cdef int _itemsize
    cdef int _ndim

    def __init__(self, object obj, int flags=0, bint dtype_is_object=False):
        self.obj = obj
        self._base = obj
        self.dtype_is_object = dtype_is_object
        self._shape = ()
        self._strides = ()
        self._itemsize = 1
        self._ndim = 0
        if isinstance(obj, (bytes, bytearray)):
            self._shape = (len(obj),)
            self._strides = (1,)
            self._ndim = 1

    def __len__(self):
        if self._shape:
            return self._shape[0]
        return 0

    def __repr__(self):
        return "<memoryview object at %#x>" % id(self)

    @property
    def T(self):
        return self

    @property
    def base(self):
        return self._base

    @property
    def shape(self):
        return self._shape

    @property
    def strides(self):
        return self._strides

    @property
    def suboffsets(self):
        return ()

    @property
    def ndim(self):
        return self._ndim

    @property
    def itemsize(self):
        return self._itemsize

    @property
    def nbytes(self):
        cdef int total = self._itemsize
        cdef int dim
        for dim in self._shape:
            total *= dim
        return total

    @property
    def size(self):
        cdef int total = 1
        cdef int dim
        for dim in self._shape:
            total *= dim
        return total

    cpdef memoryview copy(self):
        return memoryview(self.obj, dtype_is_object=self.dtype_is_object)
