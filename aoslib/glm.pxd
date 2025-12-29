# aoslib/glm.pxd

cdef class Vector3:
    cdef public:
        float x
        float y
        float z
    
    cpdef float length(self)
    cpdef float length_squared(self)
    cpdef Vector3 normalize(self)
    cpdef float dot(self, Vector3 other)
    cpdef Vector3 cross(self, Vector3 other)
    cpdef tuple to_tuple(self)


cdef class IntVector3:
    cdef public:
        int x
        int y
        int z
    
    cpdef tuple to_tuple(self)
    cpdef Vector3 to_float(self)
