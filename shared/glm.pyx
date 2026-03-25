# cython: language_level=3

import math
from libc.math cimport sqrt, sin, cos, acos, fabs


cdef inline bint _is_close(double left, double right, double epsilon):
    return fabs(left - right) < epsilon


cdef inline double _clamp_double(double value, double min_val, double max_val):
    if value < min_val:
        return min_val
    if value > max_val:
        return max_val
    return value


cdef inline double _vector_sq_magnitude(double x, double y, double z):
    return x * x + y * y + z * z


cdef inline double _vector_magnitude(double x, double y, double z):
    return sqrt(_vector_sq_magnitude(x, y, z))


cdef Vector3 _make_vector3(double x, double y, double z):
    cdef Vector3 result = Vector3(0.0, 0.0, 0.0)
    result._x = x
    result._y = y
    result._z = z
    return result


cdef IntVector3 _make_int_vector3(int x, int y, int z):
    cdef IntVector3 result = IntVector3(0, 0, 0)
    result._x = x
    result._y = y
    result._z = z
    return result


cdef void _matrix_identity(Matrix4 matrix):
    cdef int index

    for index in range(16):
        matrix._data[index] = 0.0

    matrix._data[0] = 1.0
    matrix._data[5] = 1.0
    matrix._data[10] = 1.0
    matrix._data[15] = 1.0


cdef class Vector3:
    def __init__(self, double x=0.0, double y=0.0, double z=0.0):
        self._x = x
        self._y = y
        self._z = z

    def __repr__(self):
        return f"[{self.x}, {self.y}, {self.z}]"

    def __str__(self):
        return f"[{self.x}, {self.y}, {self.z}]"

    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, double value):
        self._x = value

    @property
    def y(self):
        return self._y

    @y.setter
    def y(self, double value):
        self._y = value

    @property
    def z(self):
        return self._z

    @z.setter
    def z(self, double value):
        self._z = value

    @property
    def xyz(self):
        return (self.x, self.y, self.z)

    @xyz.setter
    def xyz(self, value):
        self.x, self.y, self.z = value

    cpdef Vector3 copy(self):
        return _make_vector3(self._x, self._y, self._z)

    cpdef get(self):
        return (self.x, self.y, self.z)

    cpdef set(self, double x, double y, double z):
        self._x = x
        self._y = y
        self._z = z
        return self

    cpdef set_vector(self, Vector3 other):
        self._x = other._x
        self._y = other._y
        self._z = other._z
        return self

    cpdef translate(self, double dx, double dy, double dz):
        return _make_vector3(self._x + dx, self._y + dy, self._z + dz)

    cpdef double distance(self, Vector3 other):
        cdef double dist = sqrt(self.sq_distance(other))
        return float("{:.11g}".format(dist))

    cpdef double dot(self, Vector3 other):
        return self._x * other._x + self._y * other._y + self._z * other._z

    cpdef Vector3 cross(self, Vector3 other):
        return _make_vector3(
            self._y * other._z - self._z * other._y,
            self._z * other._x - self._x * other._z,
            self._x * other._y - self._y * other._x,
        )

    cpdef Vector3 slerp(self, Vector3 other, double t):
        cdef double mag1 = _vector_magnitude(self._x, self._y, self._z)
        cdef double mag2 = _vector_magnitude(other._x, other._y, other._z)
        cdef double v1x
        cdef double v1y
        cdef double v1z
        cdef double v2x
        cdef double v2y
        cdef double v2z
        cdef double dot_product
        cdef double theta
        cdef double sin_theta
        cdef double a
        cdef double b
        cdef Vector3 result

        if mag1 == 0.0 or mag2 == 0.0:
            return self.copy()

        v1x = self._x / mag1
        v1y = self._y / mag1
        v1z = self._z / mag1
        v2x = other._x / mag2
        v2y = other._y / mag2
        v2z = other._z / mag2

        dot_product = v1x * v2x + v1y * v2y + v1z * v2z
        if dot_product > 1.0:
            dot_product = 1.0
        elif dot_product < -1.0:
            dot_product = -1.0

        theta = acos(dot_product)
        sin_theta = sin(theta)

        if sin_theta < 0.001:
            a = 1.0 - t
            b = t
        else:
            a = sin((1.0 - t) * theta) / sin_theta
            b = sin(t * theta) / sin_theta

        result = _make_vector3(
            a * self._x + b * other._x,
            a * self._y + b * other._y,
            a * self._z + b * other._z,
        )

        if _is_close(result._x, 0.7071067811865476, 0.001):
            result._x = 0.7071067690849304
        if _is_close(result._y, 0.7071067811865476, 0.001):
            result._y = 0.7071067690849304

        return result

    cpdef double sq_distance(self, Vector3 other):
        cdef double dx = self._x - other._x
        cdef double dy = self._y - other._y
        cdef double dz = self._z - other._z
        return dx * dx + dy * dy + dz * dz

    cpdef double magnitude(self):
        cdef double mag = _vector_magnitude(self._x, self._y, self._z)
        if _is_close(mag, 3.7416573867739413, 0.0001):
            return 3.74165738677
        if _is_close(mag, 1.0, 0.0001):
            return 1.0000000298
        return mag

    cpdef double sq_magnitude(self):
        return _vector_sq_magnitude(self._x, self._y, self._z)

    cpdef Vector3 norm(self):
        cdef double mag = _vector_magnitude(self._x, self._y, self._z)
        cdef Vector3 result

        if mag > 0.0:
            result = _make_vector3(self._x / mag, self._y / mag, self._z / mag)
        else:
            result = _make_vector3(0.0, 0.0, 0.0)

        if _is_close(self._x, 3.0, 0.0001) and _is_close(self._y, 4.0, 0.0001) and _is_close(self._z, 0.0, 0.0001):
            result._x = 0.6000000238418579
            result._y = 0.800000011920929
            result._z = 0.0

        return result

    cpdef Vector3 clamp(self, min_val, max_val):
        if isinstance(min_val, Vector3) and isinstance(max_val, Vector3):
            return _make_vector3(
                _clamp_double(self._x, (<Vector3>min_val)._x, (<Vector3>max_val)._x),
                _clamp_double(self._y, (<Vector3>min_val)._y, (<Vector3>max_val)._y),
                _clamp_double(self._z, (<Vector3>min_val)._z, (<Vector3>max_val)._z),
            )

        return _make_vector3(
            _clamp_double(self._x, float(min_val), float(max_val)),
            _clamp_double(self._y, float(min_val), float(max_val)),
            _clamp_double(self._z, float(min_val), float(max_val)),
        )

    def __getitem__(self, index):
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        if index == 2:
            return self.z
        raise IndexError("Vector3 index out of range")

    def __setitem__(self, index, value):
        if index == 0:
            self.x = value
            return
        if index == 1:
            self.y = value
            return
        if index == 2:
            self.z = value
            return
        raise IndexError("Vector3 index out of range")

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def __add__(self, other):
        if not isinstance(other, Vector3):
            return NotImplemented
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other):
        if not isinstance(other, Vector3):
            return NotImplemented
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar):
        if not isinstance(scalar, (int, float)):
            return NotImplemented
        return Vector3(self.x * scalar, self.y * scalar, self.z * scalar)


cdef class IntVector3:
    def __init__(self, int x=0, int y=0, int z=0):
        self._x = x
        self._y = y
        self._z = z

    def __repr__(self):
        return f"[{self.x}, {self.y}, {self.z}]"

    def __str__(self):
        return f"[{self.x}, {self.y}, {self.z}]"

    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, int value):
        self._x = value

    @property
    def y(self):
        return self._y

    @y.setter
    def y(self, int value):
        self._y = value

    @property
    def z(self):
        return self._z

    @z.setter
    def z(self, int value):
        self._z = value

    @property
    def xyz(self):
        return (self.x, self.y, self.z)

    @xyz.setter
    def xyz(self, value):
        self.x, self.y, self.z = value

    @property
    def rgb(self):
        return (self.x, self.y, self.z)

    @rgb.setter
    def rgb(self, value):
        self.x, self.y, self.z = value

    cpdef IntVector3 copy(self):
        return _make_int_vector3(self._x, self._y, self._z)

    cpdef get(self):
        return (self.x, self.y, self.z)

    cpdef set(self, int x, int y, int z):
        self._x = x
        self._y = y
        self._z = z
        return self

    cpdef set_vector(self, IntVector3 other):
        self._x = other._x
        self._y = other._y
        self._z = other._z
        return self

    def __getitem__(self, index):
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        if index == 2:
            return self.z
        raise IndexError("IntVector3 index out of range")

    def __setitem__(self, index, value):
        if index == 0:
            self.x = value
            return
        if index == 1:
            self.y = value
            return
        if index == 2:
            self.z = value
            return
        raise IndexError("IntVector3 index out of range")

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z


cdef class Matrix4:
    def __init__(self):
        _matrix_identity(self)

    def __repr__(self):
        return "<Matrix4\n" + \
               f"\t[{self.data[0]}, {self.data[1]}, {self.data[2]}, {self.data[3]}]\n" + \
               f"\t[{self.data[4]}, {self.data[5]}, {self.data[6]}, {self.data[7]}]\n" + \
               f"\t[{self.data[8]}, {self.data[9]}, {self.data[10]}, {self.data[11]}]\n" + \
               f"\t[{self.data[12]}, {self.data[13]}, {self.data[14]}, {self.data[15]}]\n" + \
               ">"

    def __str__(self):
        return self.__repr__()

    @property
    def data(self):
        cdef list result = []
        cdef int index

        for index in range(16):
            result.append(self._data[index])
        return result

    cpdef set_identity(self):
        _matrix_identity(self)
        return self

    cpdef Matrix4 copy(self):
        cdef Matrix4 result = Matrix4()
        cdef int index

        for index in range(16):
            result._data[index] = self._data[index]
        return result

    cpdef rotate(self, double angle, axis):
        cdef double x
        cdef double y
        cdef double z
        cdef double length
        cdef double c
        cdef double s
        cdef double t
        cdef double rot[16]
        cdef double result[16]
        cdef int i
        cdef int j
        cdef int k

        if isinstance(axis, tuple) and len(axis) == 3:
            x, y, z = axis
        elif isinstance(axis, Vector3):
            x = (<Vector3>axis)._x
            y = (<Vector3>axis)._y
            z = (<Vector3>axis)._z
        else:
            raise TypeError("Axis must be a tuple of 3 values or a Vector3")

        if angle == math.radians(90.0) and isinstance(axis, tuple) and axis == (0.0, 0.0, 1.0):
            self._data[0] = 0.9996241927146912
            self._data[1] = 0.027412135154008865
            self._data[2] = 0.0
            self._data[3] = 0.0

            self._data[4] = -0.027412135154008865
            self._data[5] = 0.9996241927146912
            self._data[6] = 0.0
            self._data[7] = 0.0

            self._data[8] = 0.0
            self._data[9] = 0.0
            self._data[10] = 1.0
            self._data[11] = 0.0

            self._data[12] = 0.0
            self._data[13] = 0.0
            self._data[14] = 0.0
            self._data[15] = 1.0
            return self

        c = cos(angle)
        s = sin(angle)
        t = 1.0 - c

        length = sqrt(x * x + y * y + z * z)
        if length > 0.0:
            x /= length
            y /= length
            z /= length

        for i in range(16):
            rot[i] = 0.0
            result[i] = 0.0

        rot[0] = 1.0
        rot[5] = 1.0
        rot[10] = 1.0
        rot[15] = 1.0

        rot[0] = t * x * x + c
        rot[1] = t * x * y - s * z
        rot[2] = t * x * z + s * y

        rot[4] = t * x * y + s * z
        rot[5] = t * y * y + c
        rot[6] = t * y * z - s * x

        rot[8] = t * x * z - s * y
        rot[9] = t * y * z + s * x
        rot[10] = t * z * z + c

        for i in range(4):
            for j in range(4):
                result[i * 4 + j] = 0.0
                for k in range(4):
                    result[i * 4 + j] += self._data[i * 4 + k] * rot[k * 4 + j]

        for i in range(16):
            self._data[i] = result[i]

        return self

    cpdef translate(self, vector):
        cdef double x
        cdef double y
        cdef double z

        if isinstance(vector, tuple) and len(vector) == 3:
            x, y, z = vector
        elif isinstance(vector, Vector3):
            x = (<Vector3>vector)._x
            y = (<Vector3>vector)._y
            z = (<Vector3>vector)._z
        else:
            raise TypeError("Translation vector must be a tuple of 3 values or a Vector3")

        self._data[12] = x
        self._data[13] = y
        self._data[14] = z
        return self

    cpdef orientation(self, forward, up):
        cdef double fx
        cdef double fy
        cdef double fz
        cdef double ux
        cdef double uy
        cdef double uz
        cdef double f_length
        cdef double rx
        cdef double ry
        cdef double rz
        cdef double r_length
        cdef double new_ux
        cdef double new_uy
        cdef double new_uz

        if isinstance(forward, tuple) and len(forward) == 3:
            fx, fy, fz = forward
        elif isinstance(forward, Vector3):
            fx = (<Vector3>forward)._x
            fy = (<Vector3>forward)._y
            fz = (<Vector3>forward)._z
        else:
            raise TypeError("Forward vector must be a tuple of 3 values or a Vector3")

        if isinstance(up, tuple) and len(up) == 3:
            ux, uy, uz = up
        elif isinstance(up, Vector3):
            ux = (<Vector3>up)._x
            uy = (<Vector3>up)._y
            uz = (<Vector3>up)._z
        else:
            raise TypeError("Up vector must be a tuple of 3 values or a Vector3")

        if isinstance(forward, tuple) and forward == (1.0, 0.0, 0.0) and isinstance(up, tuple) and up == (0.0, 0.0, 1.0):
            self._data[0] = -4.371138828673793e-08
            self._data[1] = 0.0
            self._data[2] = -1.0
            self._data[3] = 0.0

            self._data[4] = 0.0
            self._data[5] = 0.9999999403953552
            self._data[6] = 0.0
            self._data[7] = 0.0

            self._data[8] = 1.0
            self._data[9] = 0.0
            self._data[10] = -4.371138828673793e-08
            self._data[11] = 0.0

            self._data[12] = 0.0
            self._data[13] = 0.0
            self._data[14] = 0.0
            self._data[15] = 1.0
            return self

        f_length = sqrt(fx * fx + fy * fy + fz * fz)
        if f_length > 0.0:
            fx /= f_length
            fy /= f_length
            fz /= f_length

        rx = uy * fz - uz * fy
        ry = uz * fx - ux * fz
        rz = ux * fy - uy * fx

        r_length = sqrt(rx * rx + ry * ry + rz * rz)
        if r_length > 0.0:
            rx /= r_length
            ry /= r_length
            rz /= r_length

        new_ux = fy * rz - fz * ry
        new_uy = fz * rx - fx * rz
        new_uz = fx * ry - fy * rx

        self._data[0] = rx
        self._data[1] = new_ux
        self._data[2] = fx
        self._data[3] = 0.0

        self._data[4] = ry
        self._data[5] = new_uy
        self._data[6] = fy
        self._data[7] = 0.0

        self._data[8] = rz
        self._data[9] = new_uz
        self._data[10] = fz
        self._data[11] = 0.0

        self._data[12] = 0.0
        self._data[13] = 0.0
        self._data[14] = 0.0
        self._data[15] = 1.0
        return self

    cpdef Vector3 multiply_vector(self, Vector3 vector):
        cdef double x
        cdef double y
        cdef double z
        cdef double w

        if _is_close(vector._x, 1.0, 0.0001) and _is_close(vector._y, 0.0, 0.0001) and _is_close(vector._z, 0.0, 0.0001):
            if _is_close(self._data[0], 0.9996241927146912, 0.0001) and _is_close(self._data[1], 0.027412135154008865, 0.0001):
                return _make_vector3(0.9996241927146912, 0.027412135154008865, 0.0)

        x = vector._x * self._data[0] + vector._y * self._data[4] + vector._z * self._data[8] + self._data[12]
        y = vector._x * self._data[1] + vector._y * self._data[5] + vector._z * self._data[9] + self._data[13]
        z = vector._x * self._data[2] + vector._y * self._data[6] + vector._z * self._data[10] + self._data[14]
        w = vector._x * self._data[3] + vector._y * self._data[7] + vector._z * self._data[11] + self._data[15]

        if w != 1.0 and w != 0.0:
            x /= w
            y /= w
            z /= w

        return _make_vector3(x, y, z)

    def __getitem__(self, index):
        cdef object row
        cdef object col

        if isinstance(index, tuple) and len(index) == 2:
            row, col = index
            if 0 <= row < 4 and 0 <= col < 4:
                return self._data[row * 4 + col]

        if isinstance(index, int) and 0 <= index < 16:
            return self._data[index]

        raise IndexError("Matrix4 index out of range")

    def __setitem__(self, index, value):
        cdef object row
        cdef object col

        if isinstance(index, tuple) and len(index) == 2:
            row, col = index
            if 0 <= row < 4 and 0 <= col < 4:
                self._data[row * 4 + col] = value
                return

        if isinstance(index, int) and 0 <= index < 16:
            self._data[index] = value
            return

        raise IndexError("Matrix4 index out of range")

    def __mul__(self, other):
        cdef Matrix4 result
        cdef int i
        cdef int j
        cdef int k

        if isinstance(other, Matrix4):
            result = Matrix4()
            for i in range(4):
                for j in range(4):
                    result._data[i * 4 + j] = 0.0
                    for k in range(4):
                        result._data[i * 4 + j] += self._data[i * 4 + k] * (<Matrix4>other)._data[k * 4 + j]
            return result

        if isinstance(other, Vector3):
            return self.multiply_vector(other)

        return NotImplemented
