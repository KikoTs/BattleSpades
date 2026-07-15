# distutils: language = c++
# cython: language_level=3
"""Small Cython owner for the vendored Recast/Detour navigation core."""

from libcpp.vector cimport vector


cdef extern from "recast_bridge.hpp" namespace "battlespades":
    cdef cppclass RecastNavigation:
        RecastNavigation() except +
        bint ready() const
        bint build_tile(
            int tile_x,
            int tile_y,
            const vector[float]& vertices,
            const vector[int]& triangles,
            const vector[float]& bounds_min,
            const vector[float]& bounds_max,
        ) except +
        bint remove_tile(int tile_x, int tile_y) except +
        vector[float] find_path(
            const vector[float]& start,
            const vector[float]& end,
        ) const
        vector[float] crowd_steer(
            int agent_id,
            const vector[float]& start,
            const vector[float]& end,
            const vector[float]& velocity,
            float max_speed,
            float max_acceleration,
            float delta_time,
        ) except +
        void remove_crowd_agent(int agent_id)
        int tile_count() const


cdef vector[float] _float_vector(object values):
    cdef vector[float] result
    for value in values:
        result.push_back(float(value))
    return result


cdef vector[int] _int_vector(object values):
    cdef vector[int] result
    for value in values:
        result.push_back(int(value))
    return result


cdef class RecastNavigator:
    """Tiled Recast builder/query object owned by one AI worker."""

    cdef RecastNavigation* _navigation

    def __cinit__(self):
        self._navigation = new RecastNavigation()

    def __dealloc__(self):
        del self._navigation

    @property
    def ready(self):
        return bool(self._navigation.ready())

    @property
    def tile_count(self):
        return int(self._navigation.tile_count())

    def build_tile(self, int tile_x, int tile_y, vertices, triangles,
                   bounds_min, bounds_max):
        """Rasterize one 32x32 tile from a flat triangle soup."""

        if len(vertices) % 3 or len(triangles) % 3:
            raise ValueError("vertices and triangles must contain xyz/index triples")
        if len(bounds_min) != 3 or len(bounds_max) != 3:
            raise ValueError("bounds must contain three values")
        cdef vector[float] native_vertices = _float_vector(vertices)
        cdef vector[int] native_triangles = _int_vector(triangles)
        cdef vector[float] native_min = _float_vector(bounds_min)
        cdef vector[float] native_max = _float_vector(bounds_max)
        return bool(self._navigation.build_tile(
            tile_x,
            tile_y,
            native_vertices,
            native_triangles,
            native_min,
            native_max,
        ))

    def find_path(self, start, end):
        """Return flattened Recast-space straight-path vertices."""

        if len(start) != 3 or len(end) != 3:
            raise ValueError("start and end must contain three values")
        cdef vector[float] native_start = _float_vector(start)
        cdef vector[float] native_end = _float_vector(end)
        cdef vector[float] path = self._navigation.find_path(
            native_start, native_end
        )
        return [path[index] for index in range(path.size())]

    def remove_tile(self, int tile_x, int tile_y):
        """Remove a tile that became fully non-walkable."""

        return bool(self._navigation.remove_tile(tile_x, tile_y))

    def crowd_steer(self, int agent_id, start, end, velocity=(0.0, 0.0, 0.0),
                    float max_speed=4.0, float max_acceleration=12.0,
                    float delta_time=0.2):
        """Return DetourCrowd's bounded local-avoidance velocity."""

        if len(start) != 3 or len(end) != 3 or len(velocity) != 3:
            raise ValueError("start, end, and velocity must contain three values")
        cdef vector[float] native_start = _float_vector(start)
        cdef vector[float] native_end = _float_vector(end)
        cdef vector[float] native_velocity = _float_vector(velocity)
        cdef vector[float] steering = self._navigation.crowd_steer(
            agent_id,
            native_start,
            native_end,
            native_velocity,
            max_speed,
            max_acceleration,
            delta_time,
        )
        return [steering[index] for index in range(steering.size())]

    def remove_crowd_agent(self, int agent_id):
        """Remove one persistent worker-side crowd proxy."""

        self._navigation.remove_crowd_agent(agent_id)
