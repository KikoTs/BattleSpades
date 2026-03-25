# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
"""
Native-shaped restoration of aoslib.world.
"""

import json
import sys
import time
import math as _math
import random as _random

from shared.constants import *
from shared import glm as _glm
from aoslib import vxl as _vxl


_GLOBAL_GRAVITY = 1.0
_CUBE_SQ_DISTANCE = 0.0
_RAY_DEFAULT_LENGTH = 128.0
_PLAYER_RADIUS = 0.45
_PLAYER_HEIGHT = 2.7
_PLAYER_CROUCH_HEIGHT = 1.8
_PLAYER_CROUCH_SHIFT = 0.9
_FALL_SLOW_DOWN = 0.24
_FALL_DAMAGE_VELOCITY = 0.58
_FALL_DAMAGE_SCALAR = 4096.0
_PLAYER_CENTER_OFFSET_STANDING = 0.9
_PLAYER_CENTER_OFFSET_CROUCH = 0.45
_COLLISION_PROBE_STANDING = 1.3
_COLLISION_SIDE_PROBE = 1.0
_COLLISION_PROBE_CROUCH = 0.9
_COLLISION_BOTTOM_THRESHOLD = -1.36
_COLLISION_CLIMB_THRESHOLD = -2.36
_COLLISION_STEP = 1.0
_CLIMB_CHECK_START = 0.3
_CLIMB_SHIFT = -1.35
_PHYSICS_SCALE = 32.0


def A2():
    return None


def parse_constant_overrides():
    return None


def floor(value):
    return float(_math.floor(value))


def get_random_vector():
    z = (_random.random() * 2.0) - 1.0
    theta = _random.random() * (_math.pi * 2.0)
    radius = _math.sqrt(max(0.0, 1.0 - (z * z)))
    return _glm.Vector3(_math.cos(theta) * radius, _math.sin(theta) * radius, z)


def get_next_cube(position, face):
    cube = _as_intvector3(position)
    face = int(face)
    if face == 0:
        cube.x -= 1
    elif face == 1:
        cube.x += 1
    elif face == 2:
        cube.y -= 1
    elif face == 3:
        cube.y += 1
    elif face == 4:
        cube.z -= 1
    elif face == 5:
        cube.z += 1
    return cube


def cube_line(x1, y1, z1, x2, y2, z2):
    result = []
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    dz = abs(z2 - z1)
    steps = 1 + dx + dy + dz
    sx = 1 if x2 > x1 else (0 if x2 == x1 else -1)
    sy = 1 if y2 > y1 else (0 if y2 == y1 else -1)
    sz = 1 if z2 > z1 else (0 if z2 == z1 else -1)
    x = x1
    y = y1
    z = z1
    x_err = dx
    y_err = dy
    z_err = dz

    for _ in range(steps):
        result.append((x, y, z))
        if x_err > y_err and x_err > z_err:
            x += sx
            x_err -= steps
        elif y_err > x_err and y_err > z_err:
            y += sy
            y_err -= steps
        elif z_err > x_err and z_err > y_err:
            z += sz
            z_err -= steps
        else:
            if dz >= dy and dz >= dx:
                if z_err >= y_err and z_err >= x_err:
                    z += sz
                    z_err -= steps
                elif y_err >= x_err:
                    y += sy
                    y_err -= steps
                else:
                    x += sx
                    x_err -= steps
            elif dy >= dx:
                if y_err >= x_err and y_err >= z_err:
                    y += sy
                    y_err -= steps
                elif x_err >= z_err:
                    x += sx
                    x_err -= steps
                else:
                    z += sz
                    z_err -= steps
            else:
                if x_err >= y_err and x_err >= z_err:
                    x += sx
                    x_err -= steps
                elif y_err >= z_err:
                    y += sy
                    y_err -= steps
                else:
                    z += sz
                    z_err -= steps
        x_err += dx
        y_err += dy
        z_err += dz
    return result


def is_centered(x, y, z, o_x, o_y, o_z, x2, y2, z2, tolerance):
    side_len = _math.sqrt((o_x * o_x) + (o_y * o_y))
    if side_len > 0.0:
        side_x = -o_y / side_len
        side_y = o_x / side_len
    else:
        side_x = 0.0
        side_y = 0.0

    dx = x2 - x
    dy = y2 - y
    dz = z2 - z
    denom = (o_z * dz) + (o_y * dy) + (o_x * dx)
    if denom == 0.0:
        return False

    along = ((side_y * dy) + (side_x * dx)) / denom
    limit = tolerance / denom
    if not (along - limit < 0.0 < along + limit):
        return False

    across = (
        (dz * ((o_x * side_y) - (o_y * side_x)))
        + (dy * (o_z * side_x))
        - (dx * (o_z * side_y))
    ) / denom
    return across - limit < 0.0 < across + limit


def _type_error(name, expected, value):
    raise TypeError(
        "Argument '%s' has incorrect type (expected %s, got %s)"
        % (name, expected, type(value).__name__)
    )


def _as_vector3(value, name="value"):
    if isinstance(value, _glm.Vector3):
        return _glm.Vector3(value.x, value.y, value.z)
    if value is None:
        _type_error(name, "Vector3", value)
    try:
        x = value.x
        y = value.y
        z = value.z
    except AttributeError:
        try:
            x, y, z = value
        except Exception:
            _type_error(name, "Vector3", value)
    return _glm.Vector3(float(x), float(y), float(z))


def _as_intvector3(value, name="value"):
    if isinstance(value, _glm.IntVector3):
        return _glm.IntVector3(value.x, value.y, value.z)
    if value is None:
        _type_error(name, "IntVector3", value)
    try:
        x = value.x
        y = value.y
        z = value.z
    except AttributeError:
        try:
            x, y, z = value
        except Exception:
            _type_error(name, "IntVector3", value)
    return _glm.IntVector3(int(x), int(y), int(z))


def _vector_set(target, source):
    target.x = source.x
    target.y = source.y
    target.z = source.z
    return target


def _normalize_xy(vector):
    mag = _math.sqrt((vector.x * vector.x) + (vector.y * vector.y))
    if mag <= 0.0:
        return 0.0, 0.0
    return vector.x / mag, vector.y / mag


def _within_bounds(x, y):
    return 0 <= x < MAP_X and 0 <= y < MAP_Y


def _is_solid(map_obj, x, y, z):
    if not _within_bounds(x, y):
        return True
    if z < 0 or z >= MAP_Z or map_obj is None:
        return False
    return bool(map_obj.get_solid(int(x), int(y), int(z)))


def _water_solid(z):
    return int(z) >= int(Z_ABOVE_WATERPLANE)


def _raycast(world_map, position, direction, length, accurate, water_is_solid):
    if world_map is None:
        return None

    start = _as_vector3(position, "position")
    direction = _as_vector3(direction, "direction")
    mag = _math.sqrt((direction.x * direction.x) + (direction.y * direction.y) + (direction.z * direction.z))
    if mag <= 0.0:
        return None

    dx = direction.x / mag
    dy = direction.y / mag
    dz = direction.z / mag
    x = int(_math.floor(start.x))
    y = int(_math.floor(start.y))
    z = int(_math.floor(start.z))

    step_x = 1 if dx > 0.0 else -1 if dx < 0.0 else 0
    step_y = 1 if dy > 0.0 else -1 if dy < 0.0 else 0
    step_z = 1 if dz > 0.0 else -1 if dz < 0.0 else 0

    next_x = ((x + (step_x > 0)) - start.x) / dx if step_x else float("inf")
    next_y = ((y + (step_y > 0)) - start.y) / dy if step_y else float("inf")
    next_z = ((z + (step_z > 0)) - start.z) / dz if step_z else float("inf")
    delta_x = abs(1.0 / dx) if step_x else float("inf")
    delta_y = abs(1.0 / dy) if step_y else float("inf")
    delta_z = abs(1.0 / dz) if step_z else float("inf")

    last_face = 0
    travelled = 0.0
    while travelled <= length:
        solid = False
        if _within_bounds(x, y) and 0 <= z < MAP_Z:
            solid = bool(world_map.get_solid(x, y, z))
            if not solid and water_is_solid and _water_solid(z):
                solid = True
        if solid:
            block = _glm.IntVector3(x, y, z)
            if accurate:
                hit = _glm.Vector3(start.x + (dx * travelled), start.y + (dy * travelled), start.z + (dz * travelled))
                return hit, block, last_face
            return block, last_face

        if next_x <= next_y and next_x <= next_z:
            travelled = next_x
            next_x += delta_x
            x += step_x
            last_face = 0 if step_x > 0 else 1
        elif next_y <= next_x and next_y <= next_z:
            travelled = next_y
            next_y += delta_y
            y += step_y
            last_face = 2 if step_y > 0 else 3
        else:
            travelled = next_z
            next_z += delta_z
            z += step_z
            last_face = 4 if step_z > 0 else 5

    return None


cdef class World:
    cdef object _map
    cdef double _timer
    cdef list _objects

    def __init__(self, map):
        global _GLOBAL_GRAVITY
        if map is not None and not isinstance(map, _vxl.VXL):
            _type_error("map", "aoslib.vxl.VXL", map)
        _GLOBAL_GRAVITY = 1.0
        self._map = map
        self._timer = 0.0
        self._objects = []

    property map:
        def __get__(self):
            return self._map
        def __set__(self, value):
            if value is not None and not isinstance(value, _vxl.VXL):
                _type_error("map", "aoslib.vxl.VXL", value)
            self._map = value

    property timer:
        def __get__(self):
            return self._timer
        def __set__(self, value):
            self._timer = float(value)

    def set_gravity(self, gravity):
        global _GLOBAL_GRAVITY
        _GLOBAL_GRAVITY = float(gravity)

    def get_gravity(self):
        return float(_GLOBAL_GRAVITY)

    def create_object(self, cls, *args, **kwargs):
        obj = cls(self, *args, **kwargs)
        self._objects.append(obj)
        return obj

    def update(self, dt):
        self._timer += float(dt)
        return None

    def hitscan(self, position, direction):
        return _raycast(self._map, position, direction, _RAY_DEFAULT_LENGTH, False, False)

    def hitscan_accurate(self, position, direction, length=_RAY_DEFAULT_LENGTH, water_is_solid=False):
        return _raycast(self._map, position, direction, float(length), True, bool(water_is_solid))

    def get_block_face_center_position(self, position, face):
        cube = _as_intvector3(position)
        face = int(face)
        x = cube.x + 0.5
        y = cube.y + 0.5
        z = cube.z + 0.5
        if face == 0:
            x = cube.x
        elif face == 1:
            x = cube.x + 1.0
        elif face == 2:
            y = cube.y
        elif face == 3:
            y = cube.y + 1.0
        elif face == 4:
            z = cube.z
        elif face == 5:
            z = cube.z + 1.0
        return _glm.Vector3(x, y, z)


cdef class Object:
    cdef object _parent
    cdef object _name
    cdef object _position
    cdef bint _deleted

    def __init__(self, parent, *args, **kwargs):
        if parent is not None and not isinstance(parent, World):
            _type_error("parent", "aoslib.world.World", parent)
        self._parent = parent
        self._deleted = False
        self._name = None
        self._position = _glm.Vector3(0.0, 0.0, 0.0)
        self.initialize(*args, **kwargs)

    property name:
        def __get__(self):
            return self._name
        def __set__(self, value):
            self._name = value

    property position:
        def __get__(self):
            return self._position
        def __set__(self, value):
            _vector_set(self._position, _as_vector3(value, "position"))

    property deleted:
        def __get__(self):
            return bool(self._deleted)
        def __set__(self, value):
            self._deleted = bool(value)

    def initialize(self, *args, **kwargs):
        return None

    def check_valid_position(self, position):
        pos = _as_vector3(position, "position")
        if pos.x < 0.0 or pos.x >= MAP_X or pos.y < 0.0 or pos.y >= MAP_Y:
            return False
        if pos.z < 0.0 or pos.z >= MAP_Z:
            return True
        if self._parent is None or self._parent.map is None:
            return True
        return not bool(self._parent.map.get_solid(int(pos.x), int(pos.y), int(pos.z)))

    def delete(self):
        self._deleted = True
        self._parent = None
        return None

    def update(self, *args, **kwargs):
        return None


def _player_height(crouch, wade):
    if crouch and not wade:
        return _PLAYER_CROUCH_HEIGHT
    return _PLAYER_HEIGHT


def _aabb_collides(map_obj, x, y, z, radius, height):
    min_x = int(_math.floor(x - radius))
    max_x = int(_math.floor(x + radius))
    min_y = int(_math.floor(y - radius))
    max_y = int(_math.floor(y + radius))
    min_z = int(_math.floor(z))
    max_z = int(_math.floor(z + height - 1e-6))

    if max_x < 0 or max_y < 0 or min_x >= MAP_X or min_y >= MAP_Y:
        return True

    for bx in range(min_x, max_x + 1):
        for by in range(min_y, max_y + 1):
            for bz in range(min_z, max_z + 1):
                if _is_solid(map_obj, bx, by, bz):
                    return True
    return False


def _clipbox(map_obj, x, y, z):
    if x < 0.0 or x >= MAP_X or y < 0.0 or y >= MAP_Y:
        return True
    if z < 0.0:
        return False

    sample_z = int(z)
    if sample_z == MAP_Z - 1:
        sample_z -= 1
    elif sample_z >= MAP_Z:
        return True
    return _is_solid(map_obj, int(x), int(y), sample_z)


def _grounded(map_obj, position, crouch, wade):
    height = _player_height(crouch, wade)
    feet = position.z + height
    sample_z = int(_math.floor(feet + 1e-4))
    for bx in (int(_math.floor(position.x - _PLAYER_RADIUS)), int(_math.floor(position.x + _PLAYER_RADIUS))):
        for by in (int(_math.floor(position.y - _PLAYER_RADIUS)), int(_math.floor(position.y + _PLAYER_RADIUS))):
            if _is_solid(map_obj, bx, by, sample_z):
                return True
    return feet >= MAP_Z


def _move_box(position, velocity, dt, map_obj, crouch, wade, can_climb):
    height = _player_height(crouch, wade)
    dx = velocity.x * dt * _PHYSICS_SCALE
    dy = velocity.y * dt * _PHYSICS_SCALE
    dz = velocity.z * dt * _PHYSICS_SCALE
    steps = int(max(1.0, _math.ceil(max(abs(dx), abs(dy), abs(dz)) / 0.25)))
    sx = dx / steps
    sy = dy / steps
    sz = dz / steps
    collided_down = False
    climbed = False

    for _ in range(steps):
        nx = position.x + sx
        if not _aabb_collides(map_obj, nx, position.y, position.z, _PLAYER_RADIUS, height):
            position.x = nx
        elif can_climb and not _aabb_collides(map_obj, nx, position.y, position.z - 1.0, _PLAYER_RADIUS, height):
            position.x = nx
            position.z -= 1.0
            velocity.x *= 0.5
            velocity.y *= 0.5
            climbed = True
        else:
            velocity.x = 0.0

        ny = position.y + sy
        if not _aabb_collides(map_obj, position.x, ny, position.z, _PLAYER_RADIUS, height):
            position.y = ny
        elif can_climb and not _aabb_collides(map_obj, position.x, ny, position.z - 1.0, _PLAYER_RADIUS, height):
            position.y = ny
            position.z -= 1.0
            velocity.x *= 0.5
            velocity.y *= 0.5
            climbed = True
        else:
            velocity.y = 0.0

        nz = position.z + sz
        if not _aabb_collides(map_obj, position.x, position.y, nz, _PLAYER_RADIUS, height):
            position.z = nz
        else:
            if sz > 0.0:
                collided_down = True
            velocity.z = 0.0

    if position.z > MAP_Z:
        position.z = float(MAP_Z)
        velocity.z = 0.0
        collided_down = True

    airborne = not (collided_down or _grounded(map_obj, position, crouch, wade))
    return collided_down, climbed, airborne


def _sign(value):
    if value < 0.0:
        return -1.0
    if value > 0.0:
        return 1.0
    return 0.0


def _collide_with_players(player, positions, dt):
    if not positions:
        return 0

    position = player.position
    velocity = player.velocity
    own_height = _player_height(player.crouch, player.wade)
    own_center_z = position.z + ((own_height - 0.45) - (0.5 * own_height))
    scale = max(dt * 32.0, 1e-6)
    collisions = 0

    for item in positions:
        try:
            ox, oy, oz = item[:3]
            other_height = float(item[3]) if len(item) >= 4 else _PLAYER_HEIGHT
        except Exception:
            continue

        dx = (position.x + (velocity.x * scale)) - float(ox)
        dy = (position.y + (velocity.y * scale)) - float(oy)
        dist_sq = (dx * dx) + (dy * dy)
        push = max(0.0, 0.9 - _math.sqrt(dist_sq))
        if push <= 0.0:
            continue

        other_center_z = float(oz) + ((other_height - 0.45) - (0.5 * other_height))
        vertical_overlap = max(0.0, ((0.5 * other_height) + (0.5 * own_height)) - abs(own_center_z - other_center_z))
        if vertical_overlap <= 0.0:
            continue

        if vertical_overlap <= push:
            velocity.z += (_sign(own_center_z - other_center_z) * (vertical_overlap / scale))
        else:
            length = _math.sqrt(dist_sq)
            if length <= 0.0:
                nx, ny = 1.0, 0.0
            else:
                nx = dx / length
                ny = dy / length
            velocity.x += nx * (push / scale)
            velocity.y += ny * (push / scale)
        collisions += 1

    return collisions


def _default_class_value(table, fallback):
    try:
        return table[CLASS_SOLDIER]
    except Exception:
        return fallback


cdef class Player(Object):
    cdef object _velocity
    cdef object _orientation
    cdef object _s
    cdef object _lock_box
    cdef bint _alive
    cdef bint _exploded
    cdef bint _airborne
    cdef bint _burdened
    cdef bint _crouch
    cdef bint _down
    cdef bint _fall
    cdef bint _hover
    cdef bint _jetpack
    cdef bint _jetpack_active
    cdef bint _jetpack_passive
    cdef bint _jump
    cdef bint _jump_this_frame
    cdef bint _left
    cdef bint _parachute
    cdef bint _parachute_active
    cdef bint _right
    cdef bint _sneak
    cdef bint _sprint
    cdef bint _up
    cdef bint _wade
    cdef double _fall_distance
    cdef double _climb_timer
    cdef double _accel_multiplier
    cdef double _sprint_multiplier
    cdef double _crouch_sneak_multiplier
    cdef double _jump_multiplier
    cdef double _water_friction
    cdef double _fall_min_distance
    cdef double _fall_max_distance
    cdef double _fall_max_damage
    cdef double _fall_on_water_multiplier
    cdef double _climb_slowdown
    cdef bint _can_sprint_uphill

    def initialize(self):
        self._name = "player"
        self._position = _glm.Vector3(0.0, 0.0, 0.0)
        self._velocity = _glm.Vector3(0.0, 0.0, 0.0)
        self._orientation = _glm.Vector3(1.0, 0.0, 0.0)
        self._s = _glm.Vector3(0.0, 1.0, 0.0)
        self._lock_box = None
        self._alive = True
        self._exploded = False
        self._airborne = False
        self._burdened = False
        self._crouch = False
        self._down = False
        self._fall = False
        self._hover = False
        self._jetpack = False
        self._jetpack_active = False
        self._jetpack_passive = False
        self._jump = False
        self._jump_this_frame = False
        self._left = False
        self._parachute = False
        self._parachute_active = False
        self._right = False
        self._sneak = False
        self._sprint = False
        self._up = False
        self._wade = False
        self._fall_distance = 0.0
        self._climb_timer = 0.0
        self._accel_multiplier = _default_class_value(CLASS_ACCEL_MULTIPLIER, 1.0)
        self._sprint_multiplier = _default_class_value(CLASS_SPRINT_MULTIPLIER, 1.0)
        self._crouch_sneak_multiplier = _default_class_value(CLASS_CROUCH_SNEAK_MULTIPLIER, 1.0)
        self._jump_multiplier = _default_class_value(CLASS_JUMP_MULTIPLIER, 1.0)
        self._water_friction = _default_class_value(CLASS_WATER_FRICTION, 2.0)
        self._fall_min_distance = _default_class_value(CLASS_FALLING_DAMAGE_MIN_DISTANCE, 3.0)
        self._fall_max_distance = _default_class_value(CLASS_FALLING_DAMAGE_MAX_DISTANCE, 12.0)
        self._fall_max_damage = _default_class_value(CLASS_FALLING_DAMAGE_MAX_DAMAGE, 100.0)
        self._fall_on_water_multiplier = _default_class_value(CLASS_FALL_ON_WATER_DAMAGE_MULTIPLIER, 1.0)
        self._climb_slowdown = 1.0
        self._can_sprint_uphill = bool(_default_class_value(CLASS_CAN_SPRINT_UPHILL, True))

    property airborne:
        def __get__(self):
            return bool(self._airborne)

    property burdened:
        def __get__(self):
            return bool(self._burdened)
        def __set__(self, value):
            self._burdened = bool(value)

    property crouch:
        def __get__(self):
            return bool(self._crouch)

    property down:
        def __get__(self):
            return bool(self._down)

    property fall:
        def __get__(self):
            return bool(self._fall)

    property hover:
        def __get__(self):
            return bool(self._hover)
        def __set__(self, value):
            self._hover = bool(value)

    property is_locked_to_box:
        def __get__(self):
            return self._lock_box is not None
        def __set__(self, value):
            self._lock_box = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0) if value else None

    property jetpack:
        def __get__(self):
            return bool(self._jetpack)
        def __set__(self, value):
            self._jetpack = bool(value)

    property jetpack_active:
        def __get__(self):
            return bool(self._jetpack_active)
        def __set__(self, value):
            self._jetpack_active = bool(value)

    property jetpack_passive:
        def __get__(self):
            return bool(self._jetpack_passive)
        def __set__(self, value):
            self._jetpack_passive = bool(value)

    property jump:
        def __get__(self):
            return bool(self._jump)
        def __set__(self, value):
            self._jump = bool(value)

    property jump_this_frame:
        def __get__(self):
            return bool(self._jump_this_frame)
        def __set__(self, value):
            self._jump_this_frame = bool(value)

    property left:
        def __get__(self):
            return bool(self._left)

    property orientation:
        def __get__(self):
            return self._orientation
        def __set__(self, value):
            value = _as_vector3(value, "orientation")
            self._orientation = value
            ox, oy = _normalize_xy(value)
            if ox == 0.0 and oy == 0.0:
                self._s = _glm.Vector3(0.0, 1.0, 0.0)
            else:
                self._s = _glm.Vector3(-oy, ox, 0.0)

    property parachute:
        def __get__(self):
            return bool(self._parachute)
        def __set__(self, value):
            self._parachute = bool(value)

    property parachute_active:
        def __get__(self):
            return bool(self._parachute_active)
        def __set__(self, value):
            self._parachute_active = bool(value)

    property right:
        def __get__(self):
            return bool(self._right)

    property s:
        def __get__(self):
            return self._s
        def __set__(self, value):
            self._s = _as_vector3(value, "s")

    property sneak:
        def __get__(self):
            return bool(self._sneak)
        def __set__(self, value):
            self._sneak = bool(value)

    property sprint:
        def __get__(self):
            return bool(self._sprint)
        def __set__(self, value):
            self._sprint = bool(value)

    property up:
        def __get__(self):
            return bool(self._up)

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            _vector_set(self._velocity, _as_vector3(value, "velocity"))

    property wade:
        def __get__(self):
            return bool(self._wade)

    def set_position(self, x, y, z):
        self._position = _glm.Vector3(float(x), float(y), float(z))

    def set_velocity(self, x, y, z):
        self._velocity = _glm.Vector3(float(x), float(y), float(z))

    def set_orientation(self, orientation):
        self.orientation = orientation

    def set_walk(self, up=None, down=None, left=None, right=None):
        if up is not None:
            self._up = bool(up)
        if down is not None:
            self._down = bool(down)
        if left is not None:
            self._left = bool(left)
        if right is not None:
            self._right = bool(right)
        return None

    def set_crouch(self, crouch, players, noof_players):
        target = bool(crouch)
        if target == self._crouch:
            return None
        if target:
            if not self._airborne:
                self._position.z += _PLAYER_CROUCH_SHIFT
            self._crouch = True
            return None
        if self._parent is None or self._parent.map is None or not _aabb_collides(
            self._parent.map,
            self._position.x,
            self._position.y,
            self._position.z - _PLAYER_CROUCH_SHIFT,
            _PLAYER_RADIUS,
            _PLAYER_HEIGHT,
        ):
            self._position.z -= _PLAYER_CROUCH_SHIFT
            self._crouch = False
        return None

    def set_dead(self, dead):
        self._alive = not bool(dead)
        return None

    def set_exploded(self, exploded):
        self._exploded = bool(exploded)
        return None

    def set_locked_to_box(self, box):
        if box is None:
            self._lock_box = None
        else:
            self._lock_box = tuple(float(part) for part in box)
        return None

    def clear_locked_to_box(self):
        self._lock_box = None
        return None

    def set_class_accel_multiplier(self, multiplier):
        self._accel_multiplier = float(multiplier)

    def set_class_can_sprint_uphill(self, can_sprint):
        self._can_sprint_uphill = bool(can_sprint)

    def set_class_crouch_sneak_multiplier(self, multiplier):
        self._crouch_sneak_multiplier = float(multiplier)

    def set_class_fall_on_water_damage_multiplier(self, multiplier):
        self._fall_on_water_multiplier = float(multiplier)

    def set_class_falling_damage_max_damage(self, damage):
        self._fall_max_damage = float(damage)

    def set_class_falling_damage_max_distance(self, distance):
        self._fall_max_distance = float(distance)

    def set_class_falling_damage_min_distance(self, distance):
        self._fall_min_distance = float(distance)

    def set_class_jump_multiplier(self, multiplier):
        self._jump_multiplier = float(multiplier)

    def set_class_sprint_multiplier(self, multiplier):
        self._sprint_multiplier = float(multiplier)

    def set_class_water_friction(self, friction):
        self._water_friction = float(friction)

    def set_climb_slowdown(self, slowdown):
        self._climb_slowdown = float(slowdown)

    def check_cube_placement(self, position, safe_radius):
        global _CUBE_SQ_DISTANCE
        cube = _as_intvector3(position, "position")
        safe_radius = float(safe_radius)
        if self._parent is None or self._parent.map is None:
            max_z = int(A2215)
        else:
            max_z = int(self._parent.map.get_max_modifiable_z())

        if 0 <= cube.x < MAP_X and 0 <= cube.y < MAP_Y and cube.z <= max_z:
            dx = self._position.x - (cube.x + 0.5)
            dy = self._position.y - (cube.y + 0.5)
            dz = self._position.z - (cube.z + 0.5)
            _CUBE_SQ_DISTANCE = (dx * dx) + (dy * dy) + (dz * dz)
            return (safe_radius * safe_radius) > _CUBE_SQ_DISTANCE

        _CUBE_SQ_DISTANCE = safe_radius * safe_radius
        return False

    def get_cube_sq_distance(self):
        return float(_CUBE_SQ_DISTANCE)

    def update(self, dt, positions):
        if not self._alive:
            return None

        dt = float(dt)
        map_obj = self._parent.map if self._parent is not None else None
        self._wade = (self._position.z + _player_height(self._crouch, False)) >= Z_ABOVE_WATERPLANE
        self._jump_this_frame = bool(self._jump)
        if self._jump:
            self._jump = False
            self._velocity.z = -0.46 * self._jump_multiplier
            self._airborne = True

        accel = dt * 3.0 * self._accel_multiplier
        if self._airborne:
            accel *= self._jump_multiplier
        elif (self._crouch and not self._wade) or self._sneak:
            accel *= self._crouch_sneak_multiplier
        elif self._sprint and not self._burdened:
            accel *= self._sprint_multiplier

        if (self._up or self._down) and (self._left or self._right):
            accel *= _math.sqrt(0.5)

        ox, oy = _normalize_xy(self._orientation)
        sx, sy = self._s.x, self._s.y
        if self._up:
            self._velocity.x += ox * accel
            self._velocity.y += oy * accel
        elif self._down:
            self._velocity.x -= ox * accel
            self._velocity.y -= oy * accel
        if self._left:
            self._velocity.x -= sx * accel
            self._velocity.y -= sy * accel
        elif self._right:
            self._velocity.x += sx * accel
            self._velocity.y += sy * accel

        divisor = dt + 1.0
        gravity_step = dt * _GLOBAL_GRAVITY
        if self._hover:
            gravity_step *= 0.75
        if self._jetpack_active:
            gravity_step *= 0.05
        if not self._wade:
            self._velocity.z += gravity_step
        elif self._crouch:
            self._velocity.z += ((_GLOBAL_GRAVITY + 1.0) * 0.025) * 0.5
        self._velocity.z /= divisor

        if self._wade:
            horizontal_divisor = (dt * self._water_friction) + 1.0
        elif not self._airborne:
            horizontal_divisor = (dt * 4.0) + 1.0
        else:
            horizontal_divisor = divisor
        self._velocity.x /= horizontal_divisor
        self._velocity.y /= horizontal_divisor

        _collide_with_players(self, positions, dt)
        landing_speed = self._velocity.z
        collided_down, climbed, airborne = _move_box(
            self._position,
            self._velocity,
            dt,
            map_obj,
            self._crouch,
            self._wade,
            not self._crouch,
        )

        if self._lock_box is not None and len(self._lock_box) == 6:
            x1, y1, z1, x2, y2, z2 = self._lock_box
            self._position.x = min(max(self._position.x, x1), x2)
            self._position.y = min(max(self._position.y, y1), y2)
            self._position.z = min(max(self._position.z, z1), z2)

        if climbed:
            self._climb_timer = 0.25
        else:
            self._climb_timer = max(0.0, self._climb_timer - dt)

        self._wade = (self._position.z + _player_height(self._crouch, False)) >= Z_ABOVE_WATERPLANE
        self._airborne = airborne and not _grounded(map_obj, self._position, self._crouch, self._wade)

        if collided_down and landing_speed > _FALL_SLOW_DOWN:
            self._velocity.x *= 0.7
            self._velocity.y *= 0.7
            if landing_speed > _FALL_DAMAGE_VELOCITY:
                damage = landing_speed - _FALL_DAMAGE_VELOCITY
                damage = damage * damage * _FALL_DAMAGE_SCALAR
                if self._wade:
                    damage *= self._fall_on_water_multiplier
                return damage
            return -1

        return 0


cdef class PlayerMovementHistory:
    cdef public int loop_count
    cdef object _position
    cdef object _velocity

    def __init__(self, player, loop_count):
        self.loop_count = int(loop_count)
        self._position = _glm.Vector3(0.0, 0.0, 0.0)
        self._velocity = _glm.Vector3(0.0, 0.0, 0.0)
        self.set_all_data(player)

    property position:
        def __get__(self):
            return self._position
        def __set__(self, value):
            self._position = _as_vector3(value, "position")

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            self._velocity = _as_vector3(value, "velocity")

    def set_all_data(self, player):
        if hasattr(player, "position"):
            self._position = _as_vector3(player.position, "player")
        if hasattr(player, "velocity"):
            self._velocity = _as_vector3(player.velocity, "player")
        return None

    def get_client_data(self, player):
        return None


cdef class GenericMovement(Object):
    cdef object _velocity
    cdef object _last_hit_collision_block
    cdef object _last_hit_normal
    cdef bint _allow_burying
    cdef bint _allow_floating
    cdef bint _bouncing
    cdef double _gravity_multiplier
    cdef double _max_speed
    cdef bint _stop_on_collision
    cdef bint _stop_on_face

    def initialize(self, position, velocity=None):
        self._position = _as_vector3(position, "position")
        self._velocity = _as_vector3(velocity if velocity is not None else _glm.Vector3(0.0, 0.0, 0.0), "velocity")
        self._last_hit_collision_block = _glm.IntVector3(0, 0, 0)
        self._last_hit_normal = _glm.IntVector3(0, 0, 0)
        self._allow_burying = False
        self._allow_floating = False
        self._bouncing = False
        self._gravity_multiplier = 1.0
        self._max_speed = 0.0
        self._stop_on_collision = False
        self._stop_on_face = False

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            _vector_set(self._velocity, _as_vector3(value, "velocity"))

    property last_hit_collision_block:
        def __get__(self):
            return self._last_hit_collision_block
        def __set__(self, value):
            self._last_hit_collision_block = _as_intvector3(value, "last_hit_collision_block")

    property last_hit_normal:
        def __get__(self):
            return self._last_hit_normal
        def __set__(self, value):
            self._last_hit_normal = _as_intvector3(value, "last_hit_normal")

    def set_bouncing(self, bouncing):
        self._bouncing = bool(bouncing)

    def set_stop_on_collision(self, stop):
        self._stop_on_collision = bool(stop)

    def set_stop_on_face(self, stop):
        self._stop_on_face = bool(stop)

    def set_allow_burying(self, allow):
        self._allow_burying = bool(allow)

    def set_allow_floating(self, allow):
        self._allow_floating = bool(allow)

    def set_gravity_multiplier(self, multiplier):
        self._gravity_multiplier = float(multiplier)

    def set_max_speed(self, speed):
        self._max_speed = float(speed)

    def set_position(self, position):
        self._position = _as_vector3(position, "position")

    def set_velocity(self, velocity):
        self._velocity = _as_vector3(velocity, "velocity")

    def update(self, dt, players):
        dt = float(dt)
        if not self._allow_floating:
            self._velocity.z += _GLOBAL_GRAVITY * self._gravity_multiplier * dt
        return 0


cdef class ControlledGenericMovement(GenericMovement):
    cdef object _forward_vector
    cdef bint _input_back
    cdef bint _input_forward
    cdef bint _input_left
    cdef bint _input_right
    cdef double _speed_back
    cdef double _speed_forward
    cdef double _speed_left
    cdef double _speed_right
    cdef bint _strafing

    def initialize(self, position, velocity=None, forward_vector=None):
        GenericMovement.initialize(self, position, velocity)
        self._forward_vector = _as_vector3(
            forward_vector if forward_vector is not None else _glm.Vector3(0.0, 1.0, 0.0),
            "forward_vector",
        )
        self._last_hit_collision_block = None
        self._last_hit_normal = None
        self._input_back = False
        self._input_forward = False
        self._input_left = False
        self._input_right = False
        self._speed_back = 0.0
        self._speed_forward = 0.0
        self._speed_left = 0.0
        self._speed_right = 0.0
        self._strafing = False

    property forward_vector:
        def __get__(self):
            return self._forward_vector
        def __set__(self, value):
            self._forward_vector = _as_vector3(value, "forward_vector")

    property input_back:
        def __get__(self):
            return bool(self._input_back)
        def __set__(self, value):
            self._input_back = bool(value)

    property input_forward:
        def __get__(self):
            return bool(self._input_forward)
        def __set__(self, value):
            self._input_forward = bool(value)

    property input_left:
        def __get__(self):
            return bool(self._input_left)
        def __set__(self, value):
            self._input_left = bool(value)

    property input_right:
        def __get__(self):
            return bool(self._input_right)
        def __set__(self, value):
            self._input_right = bool(value)

    property speed_back:
        def __get__(self):
            return self._speed_back
        def __set__(self, value):
            self._speed_back = float(value)

    property speed_forward:
        def __get__(self):
            return self._speed_forward
        def __set__(self, value):
            self._speed_forward = float(value)

    property speed_left:
        def __get__(self):
            return self._speed_left
        def __set__(self, value):
            self._speed_left = float(value)

    property speed_right:
        def __get__(self):
            return self._speed_right
        def __set__(self, value):
            self._speed_right = float(value)

    property strafing:
        def __get__(self):
            return bool(self._strafing)
        def __set__(self, value):
            self._strafing = bool(value)

    def set_forward_vector(self, vector):
        self.forward_vector = vector

    def update(self, dt, players):
        dt = float(dt)
        fx, fy = _normalize_xy(self._forward_vector)
        side = _glm.Vector3(-fy, fx, 0.0)
        if self._input_forward:
            self._velocity.x += fx * (self._speed_forward * dt)
            self._velocity.y += fy * (self._speed_forward * dt)
        if self._input_back:
            self._velocity.x -= fx * (self._speed_back * dt)
            self._velocity.y -= fy * (self._speed_back * dt)
        if self._input_left:
            self._velocity.x -= side.x * (self._speed_left * dt)
            self._velocity.y -= side.y * (self._speed_left * dt)
        if self._input_right:
            self._velocity.x += side.x * (self._speed_right * dt)
            self._velocity.y += side.y * (self._speed_right * dt)
        return GenericMovement.update(self, dt, players)


cdef class Grenade(Object):
    cdef object _velocity
    cdef double _fuse

    def initialize(self, position, velocity, fuse):
        self._name = "grenade"
        self._position = _as_vector3(position, "position")
        self._velocity = _as_vector3(velocity, "velocity")
        self._fuse = float(fuse)

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            _vector_set(self._velocity, _as_vector3(value, "velocity"))

    property fuse:
        def __get__(self):
            return self._fuse
        def __set__(self, value):
            self._fuse = float(value)

    def update(self, dt, players):
        self._fuse = max(0.0, self._fuse - float(dt))
        self._velocity.z += _GLOBAL_GRAVITY * float(dt)
        return 0 if self._fuse > 0.0 else 1


cdef class FallingBlocks(Object):
    cdef object _velocity
    cdef object _rotation

    def initialize(self, x, y, z):
        self._name = "blocks"
        self._position = _glm.Vector3(float(x), float(y), float(z))
        self._velocity = _glm.Vector3(0.0, 0.0, 0.0)
        self._rotation = get_random_vector()

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            self._velocity = _as_vector3(value, "velocity")

    property rotation:
        def __get__(self):
            return self._rotation
        def __set__(self, value):
            self._rotation = _as_vector3(value, "rotation")

    def update(self, dt, players):
        self._velocity.z += _GLOBAL_GRAVITY * float(dt)
        return 0


cdef class Debris(Object):
    cdef object _velocity
    cdef int _rotation
    cdef double _rotation_speed
    cdef bint _in_use

    def initialize(self, position, velocity, rotation, gravity_multiplier, rotation_speed):
        vel = _as_vector3(velocity, "velocity")
        self._name = "debris"
        self._position = _as_vector3(position, "position")
        self._velocity = _glm.Vector3(-vel.x, -vel.y, -vel.z)
        self._rotation = int(rotation)
        self._rotation_speed = float(rotation_speed)
        self._in_use = False

    property velocity:
        def __get__(self):
            return self._velocity
        def __set__(self, value):
            self._velocity = _as_vector3(value, "velocity")

    property rotation:
        def __get__(self):
            return self._rotation
        def __set__(self, value):
            self._rotation = int(value)

    property rotation_speed:
        def __get__(self):
            return self._rotation_speed
        def __set__(self, value):
            self._rotation_speed = float(value)

    property in_use:
        def __get__(self):
            return bool(self._in_use)
        def __set__(self, value):
            self._in_use = bool(value)

    def use(self):
        self._in_use = True
        return None

    def free(self):
        self._in_use = False
        return None

    def update(self, dt, players):
        self._velocity.z += _GLOBAL_GRAVITY * float(dt)
        return 0
