# aoslib/glm.pyx
# GLM-style vector types for Battle Builders

import cython
from libc.math cimport sqrt

cdef class Vector3:
    """3D float vector."""
    
    def __cinit__(self, float x=0.0, float y=0.0, float z=0.0):
        self.x = x
        self.y = y
        self.z = z
    
    def __repr__(self):
        return f"Vector3({self.x}, {self.y}, {self.z})"
    
    def __str__(self):
        return f"({self.x}, {self.y}, {self.z})"
    
    def __add__(Vector3 self, Vector3 other):
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)
    
    def __sub__(Vector3 self, Vector3 other):
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)
    
    def __mul__(self, other):
        if isinstance(other, (int, float)):
            return Vector3(self.x * other, self.y * other, self.z * other)
        elif isinstance(other, Vector3):
            return Vector3(self.x * other.x, self.y * other.y, self.z * other.z)
        return NotImplemented
    
    def __truediv__(self, other):
        if isinstance(other, (int, float)):
            return Vector3(self.x / other, self.y / other, self.z / other)
        return NotImplemented
    
    def __neg__(self):
        return Vector3(-self.x, -self.y, -self.z)
    
    def __eq__(self, other):
        if isinstance(other, Vector3):
            return self.x == other.x and self.y == other.y and self.z == other.z
        return False
    
    cpdef float length(self):
        return sqrt(self.x * self.x + self.y * self.y + self.z * self.z)
    
    cpdef float length_squared(self):
        return self.x * self.x + self.y * self.y + self.z * self.z
    
    cpdef Vector3 normalize(self):
        cdef float l = self.length()
        if l == 0:
            return Vector3(0, 0, 0)
        return Vector3(self.x / l, self.y / l, self.z / l)
    
    cpdef float dot(self, Vector3 other):
        return self.x * other.x + self.y * other.y + self.z * other.z
    
    cpdef Vector3 cross(self, Vector3 other):
        return Vector3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x
        )
    
    cpdef tuple to_tuple(self):
        return (self.x, self.y, self.z)
    
    @staticmethod
    def from_tuple(tuple t):
        return Vector3(t[0], t[1], t[2])


cdef class IntVector3:
    """3D integer vector."""
    
    def __cinit__(self, int x=0, int y=0, int z=0):
        self.x = x
        self.y = y
        self.z = z
    
    def __repr__(self):
        return f"IntVector3({self.x}, {self.y}, {self.z})"
    
    def __str__(self):
        return f"({self.x}, {self.y}, {self.z})"
    
    def __add__(IntVector3 self, IntVector3 other):
        return IntVector3(self.x + other.x, self.y + other.y, self.z + other.z)
    
    def __sub__(IntVector3 self, IntVector3 other):
        return IntVector3(self.x - other.x, self.y - other.y, self.z - other.z)
    
    def __mul__(self, other):
        if isinstance(other, int):
            return IntVector3(self.x * other, self.y * other, self.z * other)
        elif isinstance(other, IntVector3):
            return IntVector3(self.x * other.x, self.y * other.y, self.z * other.z)
        return NotImplemented
    
    def __neg__(self):
        return IntVector3(-self.x, -self.y, -self.z)
    
    def __eq__(self, other):
        if isinstance(other, IntVector3):
            return self.x == other.x and self.y == other.y and self.z == other.z
        return False
    
    cpdef tuple to_tuple(self):
        return (self.x, self.y, self.z)
    
    cpdef Vector3 to_float(self):
        return Vector3(<float>self.x, <float>self.y, <float>self.z)
    
    @staticmethod
    def from_tuple(tuple t):
        return IntVector3(t[0], t[1], t[2])
