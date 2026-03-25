# cython: language_level=3

import random
from enum import Enum

from cython cimport view


MAGIC = b"Kvxl"
PALETTE_MAGIC = b"SPal"
array = view.array
memoryview = memoryview


class state(object):
    value = 0


cdef tuple _CARDINALS = (
    (-1, 0, 0),
    (1, 0, 0),
    (0, -1, 0),
    (0, 1, 0),
    (0, 0, -1),
    (0, 0, 1),
)

cdef tuple _EMPTY_BOUNDS = (None, None)

cdef float default_r = 0.0
cdef float default_g = 0.0
cdef float default_b = 0.0


def crc32(value):
    import zlib

    if isinstance(value, str):
        value = value.encode("latin-1")
    elif isinstance(value, bytearray):
        value = bytes(value)
    elif not isinstance(value, bytes):
        value = bytes(value)

    value = zlib.crc32(value)
    if value & 0x80000000:
        value -= 0x100000000
    return value


def set_kv6_default_color(r, g, b):
    global default_r, default_g, default_b

    default_r = float(r)
    default_g = float(g)
    default_b = float(b)


cdef object _make_memoryviewslice_type():
    cdef view.array arr = view.array(shape=(1, 1), itemsize=sizeof(short), format="h")
    cdef short[:, :] points = arr
    return type(points)


_memoryviewslice = _make_memoryviewslice_type()


cdef int _round_to_int(double value):
    if value >= 0:
        return <int>(value + 0.5)
    return <int>(value - 0.5)


cdef tuple _coerce_point(object row):
    if len(row) != 6:
        raise ValueError("point rows must contain 6 values")
    return (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        int(row[5]),
    )


cdef tuple _coerce_adjacent(object row):
    if len(row) != 3:
        raise ValueError("adjacent rows must contain 3 values")
    return (int(row[0]), int(row[1]), int(row[2]))


cdef bytes _read_kv6_source(str filename):
    import os

    cdef list candidates = [filename]
    cdef str normalized = filename.replace("\\", os.sep).replace("/", os.sep)

    if not os.path.isabs(normalized):
        candidates.append(os.path.join(os.getcwd(), normalized))
        candidates.append(os.path.join(os.getcwd(), "aosdump", normalized))
        candidates.append(os.path.join(os.path.dirname(__file__), "..", normalized))
        candidates.append(os.path.join(os.path.dirname(__file__), "..", "aosdump", normalized))

    for candidate in candidates:
        candidate = os.path.abspath(candidate)
        if os.path.exists(candidate):
            with open(candidate, "rb") as handle:
                return handle.read()

    raise IOError("No such file: %r" % filename)


cdef tuple _unpack_voxel(bytes data, Py_ssize_t offset):
    import struct

    cdef tuple voxel = struct.unpack_from("<4B H 2B", data, offset)
    return (
        int(voxel[2]),
        int(voxel[1]),
        int(voxel[0]),
        int(voxel[3]),
        int(voxel[4]),
        int(voxel[5]),
        int(voxel[6]),
    )


cdef tuple _parse_standard(bytes data):
    import struct

    cdef unsigned int xsize
    cdef unsigned int ysize
    cdef unsigned int zsize
    cdef float xpivot
    cdef float ypivot
    cdef float zpivot
    cdef unsigned int count
    cdef Py_ssize_t offset
    cdef list records
    cdef list voxels
    cdef tuple xlen
    cdef tuple ylen
    cdef unsigned int x
    cdef unsigned int y
    cdef unsigned int column_count
    cdef unsigned int index
    cdef tuple record
    cdef bytes palette_tail

    if len(data) < 32:
        raise ValueError("short standard header")

    xsize, ysize, zsize = struct.unpack_from("<III", data, 4)
    xpivot, ypivot, zpivot = struct.unpack_from("<fff", data, 16)
    count = struct.unpack_from("<I", data, 28)[0]
    offset = 32

    if len(data) < offset + count * 8 + xsize * 4 + xsize * ysize * 2:
        raise ValueError("truncated standard kv6")

    records = []
    for index in range(count):
        records.append(_unpack_voxel(data, offset + index * 8))
    offset += count * 8

    xlen = struct.unpack_from("<%dI" % xsize, data, offset)
    offset += xsize * 4
    ylen = struct.unpack_from("<%dH" % (xsize * ysize), data, offset)
    offset += xsize * ysize * 2
    palette_tail = data[offset:]

    voxels = []
    index = 0
    for x in range(xsize):
        for y in range(ysize):
            column_count = ylen[x * ysize + y]
            for _ in range(column_count):
                if index >= count:
                    raise ValueError("column spans exceed voxel count")
                record = records[index]
                voxels.append((x, y, record[4], record[0], record[1], record[2], record[3], record[5], record[6]))
                index += 1

    if index != count:
        raise ValueError("column spans do not match voxel count")

    return (
        int(xsize),
        int(ysize),
        int(zsize),
        float(xpivot),
        float(ypivot),
        float(zpivot),
        voxels,
        tuple(int(value) for value in xlen),
        tuple(int(value) for value in ylen),
        palette_tail,
        False,
    )


cdef tuple _parse_legacy(bytes data):
    import struct

    cdef unsigned int xsize
    cdef unsigned int ysize
    cdef float xpivot
    cdef float ypivot
    cdef float zpivot
    cdef unsigned int count
    cdef Py_ssize_t offset
    cdef list records
    cdef list voxels
    cdef tuple xlen
    cdef tuple ylen
    cdef unsigned int x
    cdef unsigned int y
    cdef unsigned int column_count
    cdef unsigned int index
    cdef int zsize
    cdef tuple record

    if len(data) < 28:
        raise ValueError("short legacy header")

    xsize, ysize = struct.unpack_from("<II", data, 4)
    xpivot, ypivot, zpivot = struct.unpack_from("<fff", data, 12)
    count = struct.unpack_from("<I", data, 24)[0]
    offset = 28

    if len(data) < offset + count * 8 + xsize * 4 + xsize * ysize * 2:
        raise ValueError("truncated legacy kv6")

    records = []
    for index in range(count):
        records.append(_unpack_voxel(data, offset + index * 8))
    offset += count * 8

    xlen = struct.unpack_from("<%dI" % xsize, data, offset)
    offset += xsize * 4
    ylen = struct.unpack_from("<%dH" % (xsize * ysize), data, offset)
    offset += xsize * ysize * 2

    voxels = []
    index = 0
    zsize = 0
    for x in range(xsize):
        for y in range(ysize):
            column_count = ylen[x * ysize + y]
            for _ in range(column_count):
                if index >= count:
                    raise ValueError("column spans exceed voxel count")
                record = records[index]
                voxels.append((x, y, record[4], record[0], record[1], record[2], record[3], record[5], record[6]))
                if record[4] + 1 > zsize:
                    zsize = record[4] + 1
                index += 1

    if index != count:
        raise ValueError("column spans do not match voxel count")

    return (
        int(xsize),
        int(ysize),
        int(zsize),
        float(xpivot),
        float(ypivot),
        float(zpivot),
        voxels,
        tuple(int(value) for value in xlen),
        tuple(int(value) for value in ylen),
        b"",
        True,
    )


cdef tuple _parse_kv6(bytes data):
    if data[:4] != MAGIC:
        raise ValueError("bad magic")

    try:
        return _parse_standard(data)
    except Exception:
        return _parse_legacy(data)


cdef list _scale_model(list voxels, int scale):
    cdef dict merged = {}
    cdef tuple voxel
    cdef tuple key
    cdef list scaled

    if scale <= 1:
        return voxels

    for voxel in voxels:
        key = (
            int(voxel[0]) // scale,
            int(voxel[1]) // scale,
            int(voxel[2]) // scale,
        )
        if key not in merged:
            merged[key] = (
                key[0],
                key[1],
                key[2],
                int(voxel[3]),
                int(voxel[4]),
                int(voxel[5]),
                int(voxel[6]),
                int(voxel[7]),
                int(voxel[8]),
            )

    scaled = sorted(merged.values(), key=lambda row: (row[0], row[1], row[2], row[3], row[4], row[5]))
    return scaled


cdef tuple _build_column_metadata(list voxels, int xsize, int ysize):
    cdef list xlen = [0] * xsize
    cdef list ylen = [0] * (xsize * ysize)
    cdef tuple voxel

    for voxel in voxels:
        if 0 <= voxel[0] < xsize and 0 <= voxel[1] < ysize:
            if not (
                voxel[3] == 0
                and voxel[4] == 0
                and voxel[5] == 0
                and voxel[6] == 0
                and voxel[7] == 0
                and voxel[8] == 0
            ):
                xlen[voxel[0]] += 1
            ylen[voxel[0] * ysize + voxel[1]] += 1

    return (tuple(xlen), tuple(ylen))


cdef class KV6:
    cdef int _xsize
    cdef int _ysize
    cdef int _zsize
    cdef float _xpivot
    cdef float _ypivot
    cdef float _zpivot
    cdef int _scale
    cdef int _detail_level
    cdef bint _billboards
    cdef bint _load_display
    cdef bint _has_data
    cdef list _voxels
    cdef dict _voxel_map
    cdef object _points_cache
    cdef object _adjacent_view
    cdef tuple _bounds_cache
    cdef object _display
    cdef tuple _xlen
    cdef tuple _ylen
    cdef bytes _palette_tail
    cdef bytes _source_bytes
    cdef int _crc

    def __init__(self, filename, billboards, offset=None, load_display=True, invscale=3, detail_level=0):
        if not isinstance(filename, str):
            raise TypeError("expected string or Unicode object, %s found" % type(filename).__name__)

        self._display = None
        self._points_cache = None
        self._adjacent_view = None
        self._bounds_cache = _EMPTY_BOUNDS
        self._palette_tail = b""
        self._source_bytes = b""
        self._voxels = []
        self._voxel_map = {}
        self._xlen = ()
        self._ylen = ()
        self._has_data = False

        self._billboards = bool(billboards)
        self._load_display = bool(load_display)
        self._scale = int(invscale)
        if self._scale <= 0:
            raise OverflowError("value too large to convert to int")
        self._detail_level = int(detail_level)

        try:
            self._load_from_filename(filename)
        except Exception:
            raise NotImplementedError("bad kv6 file: %r" % filename)

        if offset is not None:
            self.offset_pivots(*tuple(offset))

        if self._load_display:
            self._display = self._create_display()

    cdef void _load_from_filename(self, str filename) except *:
        cdef tuple parsed
        cdef int legacy
        cdef int computed_z
        cdef tuple metadata

        self._source_bytes = _read_kv6_source(filename)
        self._crc = crc32(self._source_bytes)
        parsed = _parse_kv6(self._source_bytes)

        self._xsize = parsed[0]
        self._ysize = parsed[1]
        self._zsize = parsed[2]
        self._xpivot = parsed[3]
        self._ypivot = parsed[4]
        self._zpivot = parsed[5]
        self._voxels = list(parsed[6])
        self._xlen = parsed[7]
        self._ylen = parsed[8]
        self._palette_tail = parsed[9]
        legacy = parsed[10]

        if self._scale > 1:
            self._voxels = list(_scale_model(self._voxels, self._scale))
            self._xpivot = self._xpivot / self._scale
            self._ypivot = self._ypivot / self._scale
            self._zpivot = self._zpivot / self._scale
            self._xsize = max([1] + [voxel[0] + 1 for voxel in self._voxels]) if self._voxels else max(1, _round_to_int(self._xsize / self._scale))
            self._ysize = max([1] + [voxel[1] + 1 for voxel in self._voxels]) if self._voxels else max(1, _round_to_int(self._ysize / self._scale))
            self._zsize = max([0] + [voxel[2] + 1 for voxel in self._voxels])
            metadata = _build_column_metadata(self._voxels, self._xsize, self._ysize)
            self._xlen = metadata[0]
            self._ylen = metadata[1]
            self._palette_tail = b""
        elif legacy:
            metadata = _build_column_metadata(self._voxels, self._xsize, self._ysize)
            self._xlen = metadata[0]
            self._ylen = metadata[1]

        if self._zsize < 0:
            self._zsize = 0

        self._rebuild_voxel_map()
        self._invalidate_caches()
        self._has_data = True

    cdef void _rebuild_voxel_map(self):
        cdef dict voxel_map = {}
        cdef Py_ssize_t index
        cdef tuple voxel

        for index in range(len(self._voxels)):
            voxel = self._voxels[index]
            voxel_map[(voxel[0], voxel[1], voxel[2])] = index

        self._voxel_map = voxel_map

    cdef void _invalidate_caches(self):
        self._points_cache = None
        self._adjacent_view = None
        self._bounds_cache = _EMPTY_BOUNDS

    cdef object _create_display(self):
        return {
            "billboards": bool(self._billboards),
            "detail_level": int(self._detail_level),
            "scale": int(self._scale),
            "default_color": (default_r, default_g, default_b),
        }

    cdef tuple _point_tuple(self, tuple voxel):
        return (
            _round_to_int((voxel[0] - self._xpivot) * self._scale),
            _round_to_int((voxel[1] - self._ypivot) * self._scale),
            _round_to_int((voxel[2] - self._zpivot) * self._scale),
            int(voxel[3]),
            int(voxel[4]),
            int(voxel[5]),
        )

    cdef tuple _current_bounds(self):
        cdef tuple point
        cdef int min_x
        cdef int min_y
        cdef int min_z
        cdef int max_x
        cdef int max_y
        cdef int max_z
        cdef bint first = True

        if self._bounds_cache != _EMPTY_BOUNDS:
            return self._bounds_cache

        if not self._voxels:
            self._bounds_cache = _EMPTY_BOUNDS
            return self._bounds_cache

        for voxel in self._voxels:
            point = self._point_tuple(voxel)
            if first:
                min_x = max_x = point[0]
                min_y = max_y = point[1]
                min_z = max_z = point[2]
                first = False
                continue
            if point[0] < min_x:
                min_x = point[0]
            if point[1] < min_y:
                min_y = point[1]
            if point[2] < min_z:
                min_z = point[2]
            if point[0] > max_x:
                max_x = point[0]
            if point[1] > max_y:
                max_y = point[1]
            if point[2] > max_z:
                max_z = point[2]

        self._bounds_cache = ((min_x, min_y, min_z), (max_x, max_y, max_z))
        return self._bounds_cache

    cdef bytes _serialize(self):
        import struct

        cdef list ordered_voxels
        cdef tuple metadata
        cdef bytes output
        cdef tuple voxel
        cdef int zsize

        ordered_voxels = sorted(self._voxels, key=lambda row: (row[0], row[1]))
        zsize = max([self._zsize] + [voxel[2] + 1 for voxel in ordered_voxels]) if ordered_voxels else self._zsize
        metadata = _build_column_metadata(ordered_voxels, self._xsize, self._ysize)
        self._xlen = metadata[0]
        self._ylen = metadata[1]

        output = struct.pack(
            "<4sIIIfffI",
            MAGIC,
            self._xsize,
            self._ysize,
            zsize,
            self._xpivot,
            self._ypivot,
            self._zpivot,
            len(ordered_voxels),
        )

        for voxel in ordered_voxels:
            output += struct.pack(
                "<4B H 2B",
                voxel[5],
                voxel[4],
                voxel[3],
                voxel[6],
                voxel[2],
                voxel[7],
                voxel[8],
            )

        if self._xlen:
            output += struct.pack("<%dI" % len(self._xlen), *self._xlen)
        if self._ylen:
            output += struct.pack("<%dH" % len(self._ylen), *self._ylen)

        return output

    def get_scale(self):
        return self._scale

    def get_crc(self):
        if not self._has_data:
            return 0
        return self._crc

    def add_points(self, points):
        cdef tuple point
        cdef int raw_x
        cdef int raw_y
        cdef int raw_z
        cdef tuple voxel
        cdef tuple key
        cdef tuple metadata

        for row in points:
            point = _coerce_point(row)
            raw_x = _round_to_int(point[0] / float(self._scale) + self._xpivot)
            raw_y = _round_to_int(point[1] / float(self._scale) + self._ypivot)
            raw_z = _round_to_int(point[2] / float(self._scale) + self._zpivot)
            key = (raw_x, raw_y, raw_z)
            voxel = (raw_x, raw_y, raw_z, point[3], point[4], point[5], 128, 63, 0)
            if key in self._voxel_map:
                self._voxels[self._voxel_map[key]] = voxel
            else:
                self._voxel_map[key] = len(self._voxels)
                self._voxels.append(voxel)

        self._xsize = max([self._xsize] + [voxel[0] + 1 for voxel in self._voxels]) if self._voxels else self._xsize
        self._ysize = max([self._ysize] + [voxel[1] + 1 for voxel in self._voxels]) if self._voxels else self._ysize
        self._zsize = max([self._zsize] + [voxel[2] + 1 for voxel in self._voxels]) if self._voxels else self._zsize
        metadata = _build_column_metadata(self._voxels, self._xsize, self._ysize)
        self._xlen = metadata[0]
        self._ylen = metadata[1]
        self._crc = crc32(self._serialize())
        self._invalidate_caches()

    def get_bounding_box_sizes(self):
        bounds = self._current_bounds()
        if bounds == _EMPTY_BOUNDS:
            return (0, 0, 0)
        return (
            bounds[1][0] - bounds[0][0] + 1,
            bounds[1][1] - bounds[0][1] + 1,
            bounds[1][2] - bounds[0][2] + 1,
        )

    def draw(self, display):
        return None

    def replace(self, display):
        cdef KV6 donor

        if display is not None and not isinstance(display, KV6):
            raise TypeError(
                "Argument '%s' has incorrect type (expected %s, got %s)"
                % ("display", self.__class__.__name__, display.__class__.__name__)
            )
        if display is None:
            self._display = None
            return

        donor = display
        self._display = donor._display
        donor._display = None

    def destroy_kv6(self):
        self._display = None
        self._voxels = []
        self._voxel_map = {}
        self._points_cache = None
        self._adjacent_view = None
        self._bounds_cache = _EMPTY_BOUNDS
        self._xlen = ()
        self._ylen = ()
        self._has_data = False

    def __del__(self):
        try:
            self.destroy_kv6()
        except Exception:
            pass

    def offset_pivots(self, x, y, z):
        self._xpivot += float(x) / self._scale
        self._ypivot += float(y) / self._scale
        self._zpivot += float(z) / self._scale
        self._invalidate_caches()

    def reset_prefab_pivots(self):
        if self._xpivot == 0.0 and self._ypivot == 0.0 and self._zpivot == 0.0:
            return None
        self._xpivot = 0.0
        self._ypivot = 0.0
        self._zpivot = 0.0
        self._invalidate_caches()
        if self._load_display:
            self._display = self._create_display()
        return None

    def get_pivots(self):
        return (self._xpivot, self._ypivot, self._zpivot)

    def get_sizes(self):
        return (self._xsize, self._ysize, self._zsize)

    def get_max_z_size(self):
        cdef tuple point
        cdef int max_z = 0

        if not self._voxels:
            return 0.0

        max_z = self._point_tuple(self._voxels[0])[2]
        for voxel in self._voxels[1:]:
            point = self._point_tuple(voxel)
            if point[2] > max_z:
                max_z = point[2]

        return float(max_z)

    def get_points(self):
        cdef view.array arr
        cdef short[:, :] points
        cdef Py_ssize_t index
        cdef tuple voxel
        cdef tuple point

        if self._points_cache is not None:
            return self._points_cache

        if not self._voxels:
            arr = view.array(shape=(1, 6), itemsize=sizeof(short), format="h")
            points = arr
            self._points_cache = points[:0, :]
            return self._points_cache

        arr = view.array(shape=(len(self._voxels), 6), itemsize=sizeof(short), format="h")
        points = arr
        for index in range(len(self._voxels)):
            voxel = self._voxels[index]
            point = self._point_tuple(voxel)
            points[index, 0] = point[0]
            points[index, 1] = point[1]
            points[index, 2] = point[2]
            points[index, 3] = point[3]
            points[index, 4] = point[4]
            points[index, 5] = point[5]

        self._points_cache = points
        return self._points_cache

    def get_bounds(self):
        return self._current_bounds()

    def save(self, filename):
        cdef bytes output

        if not isinstance(filename, str):
            raise TypeError("expected string or Unicode object, %s found" % type(filename).__name__)

        output = self._serialize()
        with open(filename, "wb") as handle:
            handle.write(output)
        self._source_bytes = output
        self._crc = crc32(output)

    def set_adjacent_points(self, adjacent_points):
        cdef view.array arr
        cdef short[:, :] points
        cdef Py_ssize_t index
        cdef tuple row
        cdef list rows = [_coerce_adjacent(value) for value in adjacent_points]

        if not rows:
            arr = view.array(shape=(1, 3), itemsize=sizeof(short), format="h")
            points = arr
            self._adjacent_view = points[:0, :]
            return

        arr = view.array(shape=(len(rows), 3), itemsize=sizeof(short), format="h")
        points = arr
        for index in range(len(rows)):
            row = rows[index]
            points[index, 0] = row[0]
            points[index, 1] = row[1]
            points[index, 2] = row[2]

        self._adjacent_view = points

    def get_adjacent_points(self):
        cdef short[:, :] points
        cdef Py_ssize_t index
        cdef list output
        cdef tuple point
        cdef tuple neighbor
        cdef tuple delta
        cdef set occupied
        cdef set seen

        if self._adjacent_view is not None:
            points = self._adjacent_view
            return [(points[index, 0], points[index, 1], points[index, 2]) for index in range(points.shape[0])]

        occupied = set()
        for voxel in self._voxels:
            point = self._point_tuple(voxel)
            occupied.add((point[0], point[1], point[2]))

        output = []
        seen = set()
        for voxel in self._voxels:
            point = self._point_tuple(voxel)
            for delta in _CARDINALS:
                neighbor = (point[0] + delta[0], point[1] + delta[1], point[2] + delta[2])
                if neighbor in occupied or neighbor in seen:
                    continue
                seen.add(neighbor)
                output.append(neighbor)

        self.set_adjacent_points(output)
        return output
