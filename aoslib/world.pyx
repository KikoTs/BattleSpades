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
# Contact offsets drive grounding, landing, and anchor semantics.
# Derived body heights are retained for overlap math and debug compatibility.
_PLAYER_STANDING_CONTACT_OFFSET = 2.25
_PLAYER_CROUCHING_CONTACT_OFFSET = 1.35
_PLAYER_STANDING_BODY_HEIGHT = _PLAYER_STANDING_CONTACT_OFFSET + _PLAYER_RADIUS
_PLAYER_CROUCHING_BODY_HEIGHT = _PLAYER_CROUCHING_CONTACT_OFFSET + _PLAYER_RADIUS

_PLAYER_STANDING_POS_ABOVE_GROUND = _PLAYER_STANDING_CONTACT_OFFSET
_PLAYER_CROUCHING_POS_ABOVE_GROUND = _PLAYER_CROUCHING_CONTACT_OFFSET
_PLAYER_HEIGHT = _PLAYER_STANDING_BODY_HEIGHT
_PLAYER_CROUCH_HEIGHT = _PLAYER_CROUCHING_BODY_HEIGHT
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
_WADE_SURFACE_Z = Z_ABOVE_WATERPLANE + 1.0

_DEBUG_MOVEMENT_DEFAULTS = {
    "standing_pos_above_ground": _PLAYER_STANDING_POS_ABOVE_GROUND,
    "crouching_pos_above_ground": _PLAYER_CROUCHING_POS_ABOVE_GROUND,
    "crouch_shift": _PLAYER_CROUCH_SHIFT,
    # Oracle-calibrated (live game probes via scripts/oracle_experiments.py):
    # ground friction divisor is 1+4*dt, air is 1+2*dt — measured, not the
    # other way around as the aoslib-reversed reimplementation claims.
    # Jump impulse measured live: vz after the jump frame is exactly
    # (-0.36 * jump_multiplier) / (1 + dt) — base impulse -0.36, and it
    # REPLACES the gravity step on the jump frame.
    "jump_impulse": -0.36,
    "ground_friction": 4.0,
    "air_friction": 2.0,
    "water_friction_scale": 1.0,
    "accel_multiplier_scale": 1.0,
    "sprint_multiplier_scale": 1.0,
    "crouch_sneak_multiplier_scale": 1.0,
    "climb_step_height": 1.0,
    "climb_shift": _CLIMB_SHIFT,
    "fall_slow_down": _FALL_SLOW_DOWN,
}
_DEBUG_MOVEMENT_OVERRIDES = dict(_DEBUG_MOVEMENT_DEFAULTS)


def A2():
    return None


def parse_constant_overrides():
    return None


def _movement_override(name):
    return float(_DEBUG_MOVEMENT_OVERRIDES.get(name, _DEBUG_MOVEMENT_DEFAULTS[name]))


def _debug_movement_state():
    overrides = dict(_DEBUG_MOVEMENT_OVERRIDES)
    overrides["standing_height"] = overrides["standing_pos_above_ground"] + _PLAYER_RADIUS
    overrides["crouch_height"] = overrides["crouching_pos_above_ground"] + _PLAYER_RADIUS
    return overrides


def get_debug_movement_override_names():
    return list(sorted(_DEBUG_MOVEMENT_DEFAULTS.keys()))


def get_debug_movement_overrides():
    return _debug_movement_state()


def set_debug_movement_override(name, value):
    name = str(name)
    if name not in _DEBUG_MOVEMENT_DEFAULTS:
        raise KeyError(name)
    _DEBUG_MOVEMENT_OVERRIDES[name] = float(value)
    return float(_DEBUG_MOVEMENT_OVERRIDES[name])


def reset_debug_movement_override(name):
    name = str(name)
    if name not in _DEBUG_MOVEMENT_DEFAULTS:
        raise KeyError(name)
    _DEBUG_MOVEMENT_OVERRIDES[name] = float(_DEBUG_MOVEMENT_DEFAULTS[name])
    return float(_DEBUG_MOVEMENT_OVERRIDES[name])


def reset_debug_movement_overrides():
    _DEBUG_MOVEMENT_OVERRIDES.clear()
    _DEBUG_MOVEMENT_OVERRIDES.update(_DEBUG_MOVEMENT_DEFAULTS)
    return _debug_movement_state()


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


def _player_body_height(crouch, wade):
    return _player_contact_offset(crouch, wade) + _PLAYER_RADIUS


def _player_contact_offset(crouch, wade):
    if crouch and not wade:
        return _movement_override("crouching_pos_above_ground")
    return _movement_override("standing_pos_above_ground")


def _is_wading(position_z, crouch):
    body_bottom_z = float(position_z) + _player_body_height(crouch, False)
    return body_bottom_z > (_WADE_SURFACE_Z + 1e-4)


def _wade_zone(position_z, crouch):
    """Oracle-calibrated wade check: feet (anchor + contact offset) at or
    below the water surface. Only evaluated while grounded — the live
    engine holds the previous wade value while airborne.

    Threshold pinned by live data: grounded feet at 238.99 is dry (the
    real client says wade=False there) while 239.99 is wading, so the
    boundary is Z_ABOVE_WATERPLANE + 1.0 — the same value as the original
    _WADE_SURFACE_Z constant."""
    feet = float(position_z) + _player_contact_offset(crouch, False)
    return feet >= float(Z_ABOVE_WATERPLANE) + 1.0


def _aabb_collides(map_obj, x, y, z, radius, contact_offset):
    min_x = int(_math.floor(x - radius))
    max_x = int(_math.floor(x + radius))
    min_y = int(_math.floor(y - radius))
    max_y = int(_math.floor(y + radius))
    min_z = int(_math.floor((z - radius) + 1e-6))
    max_z = int(_math.floor(z + contact_offset - 1e-6))

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


# Ground-contact probe tolerance, measured from the live engine by binary
# search (oracle grounded with feet up to ~0.00875 above the surface).
_GROUNDED_PROBE_EPSILON = 0.00875


def _grounded(map_obj, position, crouch, wade):
    feet = position.z + _player_contact_offset(crouch, wade)
    sample_z = int(_math.floor(feet + _GROUNDED_PROBE_EPSILON))
    for bx in (int(_math.floor(position.x - _PLAYER_RADIUS)), int(_math.floor(position.x + _PLAYER_RADIUS))):
        for by in (int(_math.floor(position.y - _PLAYER_RADIUS)), int(_math.floor(position.y + _PLAYER_RADIUS))):
            if _is_solid(map_obj, bx, by, sample_z):
                return True
    return feet >= MAP_Z


cdef bint _clip(map_obj, double x, double y, double z):
    """Faithful port of the compiled engine's clipbox @0x3c00 with
    allow_below_water = False (boxclipmove always passes 0) and no entity
    list / lock-box bounds (the infantry path).

    Truncation semantics matter at block boundaries: a probe exactly on an
    integer face belongs to the HIGHER block (<int> of a guaranteed-positive
    coordinate == floor); z in (-1, 0) is EMPTY (only z<0 is open air);
    x/y outside [0, 511] read SOLID; the open-water row 239 samples the bed
    at 238, and z>239 is the solid world floor."""
    cdef int ix, iy, iz, v10
    if x < 0.0:
        return True
    ix = <int>x
    if ix > 511:
        return True
    if y < 0.0:
        return True
    iy = <int>y
    if iy > 511:
        return True
    iz = <int>z
    if iz < 0:
        return False
    v10 = 238
    if iz != 239:
        v10 = iz
        if iz > 238:
            return True
    if map_obj is None:
        return False
    if 0 <= v10 <= 239 and 0 <= iy <= 511 and 0 <= ix <= 511:
        return bool(map_obj.get_solid(ix, iy, v10))
    return False


cdef bint _clip_corners(map_obj, double cx, double cy, double z):
    """The four AABB corners at radius 0.45 — the only horizontal probe set
    the live engine uses (a single z level, the head block is never probed by
    a lateral move)."""
    return (_clip(map_obj, cx - 0.45, cy - 0.45, z)
            or _clip(map_obj, cx - 0.45, cy + 0.45, z)
            or _clip(map_obj, cx + 0.45, cy - 0.45, z)
            or _clip(map_obj, cx + 0.45, cy + 0.45, z))


cdef bint _solid_int(map_obj, int x, int y, int z):
    if map_obj is None:
        return False
    return bool(map_obj.get_solid(x, y, z))


def _move_box(position, velocity, dt, map_obj, crouch, hover, sprint,
              can_uphill, airborne, wade):
    """Faithful port of the compiled engine's boxclipmove @0x3e90.

    ONE pass per frame (no substepping): X section -> X glide pass -> Y
    section (using the X-glided z) -> Y glide pass -> finalize. Verified
    byte-for-byte against logs/oracle/movebox_probes.json (scripts/
    replay_movebox.py, all probes within 1e-4 blocks of the live client).

    Key behaviours the old sweep got wrong:
      * a blocked vertical move keeps the FRAME-START z exactly (landing does
        not partially advance), the fix for the 0.57-block hard-landing error;
      * each horizontal axis runs a glide pass that lifts the box out of a
        penetrated step by (4*v_axis^2 + 0.05)*dt*32 (both axes fire for a
        straight climb -> the oracle-measured +0.1), with a head-revert that
        zeroes the axis velocity only when the head is blocked;
      * climb is gated by not-crouch / not-hover / (not-sprint or
        can_sprint_uphill) — there is NO orientation.z and NO wade gate;
      * velocity is zeroed only on a one-block-up climb-probe hit or a glide
        head-revert, never unconditionally on a blocked wall.

    Mutates position/velocity in place. Returns
    (climbed, landed, airborne, wade_or_None, crouch); wade_or_None is the new
    wade value only on a landing frame (else None -> caller keeps its value)."""
    cdef double px = position.x, py = position.y, pz = position.z
    cdef double vx = velocity.x, vy = velocity.y, vz = velocity.z
    cdef bint cr = bool(crouch), hv = bool(hover), sp = bool(sprint)
    cdef bint up = bool(can_uphill), air = bool(airborne), wd = bool(wade)
    cdef double v35, v6, v32, m, rad, v31, v36, v40, v33, wx, wy
    cdef double v34, v15, v37, v45, v41, v46, v49, v54, v22, v57
    cdef double v26, v27, v28
    cdef int lp, v10, v12, i14, i17, i18, ii
    cdef bint climbed = False, advanced, hit, overlap

    v35 = dt * _PHYSICS_SCALE
    v6 = vx * v35 + px                 # candidate new x
    v32 = vy * v35 + py               # candidate new y
    if cr and not hv:
        lp = 2; m = 0.89999998; rad = 0.44999999
    else:
        lp = 3; m = 1.3499999; rad = 0.89999998
    v31 = rad
    v36 = pz + rad
    v40 = m + v36                      # feet z
    v33 = v6
    wx = px
    wy = py

    # ---- X section ----
    v10 = 0
    advanced = False
    while True:
        if _clip_corners(map_obj, v6, py, v40 - v10):
            break
        v10 += 1
        if v10 >= lp:
            wx = v33
            advanced = True
            break
    if not advanced and not cr and not hv and ((not sp) or up):
        v12 = 0
        hit = False
        while v12 < lp:
            if _clip_corners(map_obj, v6, py, (v40 - v12) - 1.0):
                vx = 0.0
                hit = True
                break
            v12 += 1
        if not hit:
            v49 = (v36 - m) - 1.0
            if not _clip_corners(map_obj, v6, wy, v49):
                wx = v33
                climbed = True

    # ---- X glide pass ----
    v34 = 0.0
    v15 = v36
    i14 = 0
    overlap = True
    while True:
        if _clip_corners(map_obj, wx, wy, v40 - i14):
            break
        i14 += 1
        if i14 >= lp:
            overlap = False
            break
    if overlap:
        v34 = ((vx * vx * 4.0) + 0.050000001) * v35
        v41 = _math.floor(v36 - m)
        if _clip_corners(map_obj, wx, wy, v41):
            wx = px
            vx = 0.0
        else:
            v15 = v36 - v34
            vz = 0.0
    v37 = v15
    v45 = m + v15

    # ---- Y section (at the X-advanced x, with the glided z) ----
    i17 = 0
    advanced = False
    while True:
        if _clip_corners(map_obj, wx, v32, v45 - i17):
            break
        i17 += 1
        if i17 >= lp:
            wy = v32
            advanced = True
            break
    if not advanced and not cr and not hv and ((not sp) or up):
        ii = 0
        hit = False
        while ii < lp:
            if _clip_corners(map_obj, wx, v32, (v45 - ii) - 1.0):
                vy = 0.0
                hit = True
                break
            ii += 1
        if not hit:
            v54 = (v37 - m) - 1.0
            if not _clip_corners(map_obj, wx, v32, v54):
                wy = v32
                climbed = True

    # ---- Y glide pass ----
    i18 = 0
    overlap = True
    while True:
        if _clip_corners(map_obj, wx, wy, v45 - i18):
            break
        i18 += 1
        if i18 >= lp:
            overlap = False
            break
    if overlap:
        v34 = ((vy * vy * 4.0) + 0.050000001) * v35
        v46 = _math.floor(v37 - m)
        if _clip_corners(map_obj, wx, wy, v46):
            wy = py
            vy = 0.0
        else:
            v37 = v37 - v34
            vz = 0.0

    # ---- finalize ----
    position.x = wx
    position.y = wy
    if climbed:
        velocity.x = vx
        velocity.y = vy
        velocity.z = vz
        position.z = v37 - v31
        return (True, False, False, None, cr)
    if v34 != 0.0:
        # Glide frame: the vertical move is skipped entirely (z frozen at the
        # glided value, airborne untouched). Auto-crouch into a 1.x-block gap.
        if not cr:
            v26 = _math.floor(m + v37)
            if _clip(map_obj, wx, wy, v26):
                v27 = _math.floor(v37)
                if not _clip(map_obj, wx, wy, v27):
                    v28 = _math.floor(v37 - m)
                    if not _clip(map_obj, wx, wy, v28):
                        cr = True
        velocity.x = vx
        velocity.y = vy
        velocity.z = vz
        position.z = v37 - v31
        return (False, False, air, None, cr)

    # normal vertical move
    v22 = m if vz >= 0.0 else -m
    v37 = v37 + v35 * vz
    air = True
    v57 = _math.floor(v22 + v37)
    if not _clip_corners(map_obj, wx, wy, v57):
        velocity.x = vx
        velocity.y = vy
        velocity.z = vz
        position.z = v37 - v31
        return (False, False, True, None, cr)
    # vertical blocked -> keep the exact frame-start z (no partial advance)
    position.z = pz
    velocity.x = vx
    velocity.y = vy
    velocity.z = 0.0
    if vz >= 0.0:                      # landing (downward blocked)
        return (False, True, False, (pz > 237.0), cr)
    return (False, False, air, None, cr)   # ceiling bump: stays airborne


def _check_for_ground_holes(position, velocity, dt, map_obj, crouch, hover,
                            up, down, left, right):
    """Faithful port of check_for_ground_holes @0x2290.

    When the player is grounded and pressing NO movement key, but the block at
    floor(feet + 1.0) under its own column is empty (it is standing on a ledge
    lip), the live engine OVERWRITES horizontal velocity to drift toward the
    hole centre at offset*dt*5 — provided the chosen axis offset is <= 0.2 and
    a straight (or one resolved diagonal) neighbour is solid. The server used to
    stand perfectly still here, diverging from the client until reconciliation
    yanked the player into the cliff face."""
    cdef double x = position.x, y = position.y, z = position.z
    cdef double v2, v7, v8, v12, v13
    cdef int v4, v5, v6, v9, v14
    cdef bint v10, v11, v15, v16, v17, v19, v20

    if crouch and not hover:
        v2 = z + 1.3499999
    else:
        v2 = z + 2.25
    if up or down or left or right:
        return
    v4 = <int>_math.floor(x)
    v5 = <int>_math.floor(y)
    v6 = <int>_math.floor(v2 + 1.0)
    if not (v6 > 239 or v6 < 0 or v5 > 511 or v5 < 0 or v4 > 511
            or not _solid_int(map_obj, v4, v5, v6)):
        return

    v7 = (<double>v4 + 0.5) - x
    v8 = _math.fabs(v7)
    if v8 != 0.0:
        v9 = v4 - <int>(v7 / v8)
    else:
        v9 = -(1 << 30)
    v11 = True
    if 0 <= v6 <= 239 and 0 <= v5 <= 511 and 0 <= v9 <= 511:
        v11 = not _solid_int(map_obj, v9, v5, v6)

    v12 = (<double>v5 + 0.5) - y
    v13 = _math.fabs(v12)
    if v13 != 0.0:
        v14 = v5 - <int>(v12 / v13)
    else:
        v14 = -(1 << 30)
    v10 = True
    if 0 <= v6 <= 239 and 0 <= v14 <= 511 and 0 <= v4 <= 511:
        v10 = not _solid_int(map_obj, v4, v14, v6)

    v15 = not v11                      # push_x (x neighbour solid)
    v16 = v10 and v11                  # both straight neighbours empty
    v17 = not v10                      # push_y (y neighbour solid)
    if (v16 and 0 <= v6 <= 239 and 0 <= v14 <= 511 and 0 <= v9 <= 511
            and _solid_int(map_obj, v9, v14, v6)):
        # both straight neighbours empty but the diagonal is solid: pick one
        # axis by signed dy <= dx -> push y, else push x.
        if v12 <= v7:
            v17 = True
        else:
            v15 = True

    v19 = v15
    if (not v19) or v8 <= 0.2:
        v20 = v17
        if (not v20) or v13 <= 0.2:
            if v19:
                velocity.x = (v7 * dt) * 5.0
            if v20:
                velocity.y = (v12 * dt) * 5.0


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
    own_body_height = _player_body_height(player.crouch, player.wade)
    own_center_z = position.z + ((own_body_height - 0.45) - (0.5 * own_body_height))
    scale = max(dt * 32.0, 1e-6)
    collisions = 0

    for item in positions:
        try:
            ox, oy, oz = item[:3]
            other_body_height = float(item[3]) if len(item) >= 4 else _PLAYER_STANDING_BODY_HEIGHT
        except Exception:
            continue

        dx = (position.x + (velocity.x * scale)) - float(ox)
        dy = (position.y + (velocity.y * scale)) - float(oy)
        dist_sq = (dx * dx) + (dy * dy)
        push = max(0.0, 0.9 - _math.sqrt(dist_sq))
        if push <= 0.0:
            continue

        other_center_z = float(oz) + ((other_body_height - 0.45) - (0.5 * other_body_height))
        vertical_overlap = max(
            0.0,
            ((0.5 * other_body_height) + (0.5 * own_body_height)) - abs(own_center_z - other_center_z),
        )
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
        crouch_shift = _movement_override("crouch_shift")
        if target == self._crouch:
            return None
        if target:
            if not self._airborne:
                self._position.z += crouch_shift
            self._crouch = True
            return None
        if self._parent is None or self._parent.map is None or not _aabb_collides(
            self._parent.map,
            self._position.x,
            self._position.y,
            self._position.z - crouch_shift,
            _PLAYER_RADIUS,
            _player_contact_offset(False, self._wade),
        ):
            self._position.z -= crouch_shift
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
        # wade is NOT recomputed here: the live engine holds the previous
        # value while airborne and only re-evaluates it on ground contact
        # (after the move, below). Oracle-calibrated.
        self._jump_this_frame = bool(self._jump)
        jumped_this_frame = False
        if self._jump:
            self._jump = False
            self._velocity.z = _movement_override("jump_impulse") * self._jump_multiplier
            self._airborne = True
            jumped_this_frame = True

        # Oracle-calibrated: accel is the selected class multiplier (the
        # crouch/sneak and sprint multipliers REPLACE the base multiplier,
        # they do not stack) scaled by dt — no 3.0 base factor.
        if (self._crouch and not self._wade) or self._sneak:
            accel = self._crouch_sneak_multiplier * _movement_override("crouch_sneak_multiplier_scale")
        elif self._sprint and not self._burdened:
            accel = self._sprint_multiplier * _movement_override("sprint_multiplier_scale")
        else:
            accel = self._accel_multiplier * _movement_override("accel_multiplier_scale")
        if self._airborne:
            accel *= 0.5
        accel *= dt

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
        if self._parachute_active and self._parachute:
            gravity_step *= 0.75
        elif self._jetpack_active:
            gravity_step *= 0.05
        # Oracle-calibrated: gravity applies normally while wading (the
        # airborne bounce frames during a water-floor settle show the full
        # gravity step with wade=1). Only the crouch buoyancy differs.
        # On the jump frame the impulse REPLACES the gravity step entirely
        # (measured: post-frame vz == impulse / (1 + dt) exactly).
        if jumped_this_frame:
            pass
        elif self._wade and self._crouch:
            self._velocity.z += ((_GLOBAL_GRAVITY + 1.0) * 0.025) * 0.5
        else:
            self._velocity.z += gravity_step
        self._velocity.z /= divisor

        if self._wade or self._hover or self._jetpack_active or self._parachute_active:
            horizontal_divisor = (dt * (self._water_friction * _movement_override("water_friction_scale"))) + 1.0
        elif not self._airborne:
            horizontal_divisor = (dt * _movement_override("ground_friction")) + 1.0
        else:
            horizontal_divisor = (dt * _movement_override("air_friction")) + 1.0
        self._velocity.x /= horizontal_divisor
        self._velocity.y /= horizontal_divisor

        _collide_with_players(self, positions, dt)
        landing_speed = self._velocity.z
        # Single-pass boxclipmove port (aoslib.world.so @0x3e90). It owns the
        # airborne/wade flags exactly as the compiled engine does: airborne is
        # set inside the move (cleared on a downward landing or a climb, held
        # through a glide frame), and wade is written only on a landing frame.
        climbed, collided_down, airborne, wade_out, new_crouch = _move_box(
            self._position,
            self._velocity,
            dt,
            map_obj,
            self._crouch,
            self._hover,
            self._sprint,
            self._can_sprint_uphill,
            self._airborne,
            self._wade,
        )
        self._crouch = new_crouch

        if self._lock_box is not None and len(self._lock_box) == 6:
            x1, y1, z1, x2, y2, z2 = self._lock_box
            self._position.x = min(max(self._position.x, x1), x2)
            self._position.y = min(max(self._position.y, y1), y2)
            self._position.z = min(max(self._position.z, z1), z2)

        if climbed:
            self._climb_timer = 0.1
        else:
            self._climb_timer = max(0.0, self._climb_timer - dt)

        if jumped_this_frame:
            self._airborne = True
        else:
            self._airborne = bool(airborne)
        if wade_out is not None:
            self._wade = bool(wade_out)

        # Defensive world-bottom clamp (the z=239 floor fill normally makes
        # this unreachable; mirrors move_player's p.z>240 -> 239 guard).
        if self._position.z > MAP_Z:
            self._position.z = float(MAP_Z)
            self._velocity.z = 0.0
            collided_down = True

        # Idle ledge-lip nudge: when grounded and not pressing a movement key,
        # the live engine creeps toward a hole centre at the lip. Without this
        # the server stands still where the client drifts -> the reconciliation
        # anchor diverges and yanks the player into the cliff ("randomly stuck
        # on cliffs and edges").
        if not self._airborne:
            _check_for_ground_holes(
                self._position, self._velocity, dt, map_obj,
                self._crouch, self._hover,
                self._up, self._down, self._left, self._right,
            )

        if collided_down and landing_speed > _movement_override("fall_slow_down"):
            self._velocity.x *= 0.7
            self._velocity.y *= 0.7
            effective_landing_speed = landing_speed
            if self._parachute_active and self._parachute:
                effective_landing_speed *= 0.75
            if effective_landing_speed > _FALL_DAMAGE_VELOCITY:
                damage = effective_landing_speed - _FALL_DAMAGE_VELOCITY
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
