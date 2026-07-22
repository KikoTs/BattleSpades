"""Run an extended, repeatable movement/reconciliation stress scenario.

This scenario drives the retail client's real keyboard and character-facing
pipeline.  It deliberately keeps the tracer's synchronous frame capture off:
the TCP console is the instrument, while this process owns the timestamped
JSON artifact.  That avoids turning filesystem latency into apparent network
jitter.

The client's ``network_position_loop_count`` is the loop stamp echoed by the
server in the local player's WorldUpdate row.  Recording it beside the live
client loop provides a common clock without enabling invasive server parity
capture.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from game_console import ConsoleError, GameConsole  # noqa: E402
from parity_clients import (  # noqa: E402
    DEFAULT_CLIENT_DIR,
    ClientSpec,
    launch_client,
    stop_client,
)


PRESS_KEY = """from pyglet.window import key as K
manager.keyboard[K.{key}] = True
manager.window.dispatch_event('on_key_press', K.{key}, 0)
_ = 'down'"""

RELEASE_KEY = """from pyglet.window import key as K
manager.keyboard[K.{key}] = False
manager.window.dispatch_event('on_key_release', K.{key}, 0)
_ = 'up'"""

# Do not iterate native Vector3 values.  The stock client's glm wrapper does
# not raise IndexError past element 2 and unbounded iteration can crash it.
CLIENT_SAMPLE = """_c = manager.scene.player.character
_w = _c.world_object
_p = _w.position
_v = _w.velocity
_n = _c.network_position
_loop = int(_c.network_position_loop_count)
_old = _c.get_old_movement_data(_loop)
_matched_error = None
_matched_position = None
_matched_error_vector = None
if _old is not None:
    _hp = _old[0].position
    _matched_position = tuple(round(float(_hp[i]), 6) for i in range(3))
    _matched_error_vector = tuple(round(float(_n[i]-_hp[i]), 6) for i in range(3))
    _matched_error = ((_n[0]-_hp[0])**2 + (_n[1]-_hp[1])**2 + (_n[2]-_hp[2])**2)**0.5
_candidate_errors = {}
for _offset in range(-3, 4):
    _candidate = _c.get_old_movement_data(_loop + _offset)
    if _candidate is not None:
        _cp = _candidate[0].position
        _candidate_errors[_offset] = round(float(
            ((_n[0]-_cp[0])**2 + (_n[1]-_cp[1])**2 + (_n[2]-_cp[2])**2)**0.5
        ), 6)
_ = {'history_length': int(len(_c.movement_history)),
     'lerp_timer': round(float(_c.position_lerp_timer), 6),
     'reconciliation_snap_count': int(state.reconciliation_snap_count),
     'reconciliation_adjust_count': int(state.reconciliation_adjust_count),
     'network_loop': _loop,
     'client_loop': int(manager.scene.loop_count),
     'matched_loop_error': None if _matched_error is None else round(float(_matched_error), 6),
     'matched_history_position': _matched_position,
     'matched_error_vector': _matched_error_vector,
     'candidate_loop_errors': _candidate_errors,
     'position': tuple(round(float(_p[i]), 6) for i in range(3)),
     'network_position': tuple(round(float(_n[i]), 6) for i in range(3)),
     'velocity': tuple(round(float(_v[i]), 6) for i in range(3)),
     'orientation': tuple(round(float(_w.orientation[i]), 6) for i in range(3)),
     'yaw_degrees': round(float(_c.yaw), 6),
     'tool_id': int(manager.scene.player.tool_id),
     'block_count': int(_c.block_count),
     'jetpack_id': int(getattr(manager.scene.player, 'jetpack', 0) or 0),
     'jetpack_active': bool(getattr(manager.scene.player, 'jetpack_active', False)),
     'jetpack_fuel': round(float(getattr(_c, 'jetpack_fuel', 0.0)), 6),
     'parachute_active': bool(getattr(manager.scene.player, 'parachute_active', False)),
     'palette_active': bool(getattr(manager.scene.hud.palette, 'active', False)),
     'airborne': bool(_w.airborne),
     'wade': bool(_w.wade)}"""

START_YAW_RAMP = """import pyglet as _stress_pyglet
try:
    _stress_pyglet.clock.unschedule(_stress_yaw_tick)
except (NameError, AttributeError):
    pass
_stress_yaw_elapsed = 0.0
_stress_yaw_duration = {duration:.9f}
_stress_yaw_start = {start:.9f}
_stress_yaw_delta = {delta:.9f}
def _stress_yaw_tick(dt):
    global _stress_yaw_elapsed
    _scene = getattr(manager, 'scene', None)
    _player = getattr(_scene, 'player', None)
    if _player is None or getattr(_player, 'character', None) is None:
        # A disconnect/menu transition can race the controller's explicit
        # STOP request.  Never let a diagnostic callback take down the game.
        _stress_pyglet.clock.unschedule(_stress_yaw_tick)
        return
    _stress_yaw_elapsed += max(0.0, float(dt))
    _progress = min(1.0, _stress_yaw_elapsed / max(0.001, _stress_yaw_duration))
    _yaw = _stress_yaw_start + _stress_yaw_delta * _progress
    _player.character.yaw = _yaw
    _player.character.update_orientation()
    if _progress >= 1.0:
        _stress_pyglet.clock.unschedule(_stress_yaw_tick)
_stress_pyglet.clock.schedule(_stress_yaw_tick)
_ = 'yaw-ramp-started'"""

STOP_YAW_RAMP = """import pyglet as _stress_pyglet
try:
    _stress_pyglet.clock.unschedule(_stress_yaw_tick)
except (NameError, AttributeError):
    pass
_ = 'yaw-ramp-stopped'"""

START_KEY_PULSE = """import pyglet as _stress_pyglet
from pyglet.window import key as _StressKey
try:
    _stress_pyglet.clock.unschedule(_stress_pulse_tick)
except (NameError, AttributeError):
    pass
_stress_pulse_key = _StressKey.{key}
_stress_pulse_period = {period:.9f}
_stress_pulse_down_for = {down_for:.9f}
_stress_pulse_total = {total:.9f}
_stress_pulse_elapsed = 0.0
_stress_pulse_down = False
def _stress_set_pulse_key(_down):
    global _stress_pulse_down
    if bool(_down) == _stress_pulse_down:
        return
    _stress_pulse_down = bool(_down)
    manager.keyboard[_stress_pulse_key] = _stress_pulse_down
    if _stress_pulse_down:
        manager.window.dispatch_event('on_key_press', _stress_pulse_key, 0)
    else:
        manager.window.dispatch_event('on_key_release', _stress_pulse_key, 0)
def _stress_pulse_tick(dt):
    global _stress_pulse_elapsed
    _stress_pulse_elapsed += max(0.0, float(dt))
    _active = _stress_pulse_elapsed < _stress_pulse_total
    _phase = _stress_pulse_elapsed % max(0.001, _stress_pulse_period)
    _stress_set_pulse_key(_active and _phase < _stress_pulse_down_for)
    if not _active:
        _stress_set_pulse_key(False)
        _stress_pyglet.clock.unschedule(_stress_pulse_tick)
_stress_pyglet.clock.schedule(_stress_pulse_tick)
_ = 'key-pulse-started'"""

STOP_KEY_PULSE = """import pyglet as _stress_pyglet
try:
    _stress_pyglet.clock.unschedule(_stress_pulse_tick)
except (NameError, AttributeError):
    pass
try:
    _stress_set_pulse_key(False)
except NameError:
    pass
_ = 'key-pulse-stopped'"""

START_PRIMARY_PULSE = """import pyglet as _stress_pyglet
try:
    _stress_pyglet.clock.unschedule(_stress_primary_tick)
except (NameError, AttributeError):
    pass
_stress_primary_period = {period:.9f}
_stress_primary_down_for = {down_for:.9f}
_stress_primary_total = {total:.9f}
_stress_primary_elapsed = 0.0
_stress_primary_down = False
def _stress_set_primary(_down):
    global _stress_primary_down
    if bool(_down) == _stress_primary_down:
        return
    _stress_primary_down = bool(_down)
    manager.scene.player.character.set_primary_shoot(_stress_primary_down)
def _stress_primary_tick(dt):
    global _stress_primary_elapsed
    _stress_primary_elapsed += max(0.0, float(dt))
    _active = _stress_primary_elapsed < _stress_primary_total
    _phase = _stress_primary_elapsed % max(0.001, _stress_primary_period)
    _stress_set_primary(_active and _phase < _stress_primary_down_for)
    if not _active:
        _stress_set_primary(False)
        _stress_pyglet.clock.unschedule(_stress_primary_tick)
_stress_pyglet.clock.schedule(_stress_primary_tick)
_ = 'primary-pulse-started'"""

STOP_PRIMARY_PULSE = """import pyglet as _stress_pyglet
try:
    _stress_pyglet.clock.unschedule(_stress_primary_tick)
except (NameError, AttributeError):
    pass
try:
    _stress_set_primary(False)
except NameError:
    manager.scene.player.character.set_primary_shoot(False)
_ = 'primary-pulse-stopped'"""

# Select one deterministic, face-supported air cell in native-client reach.
# The probe and scripted sequence intentionally use the same scan.  A prior
# version merely aimed down from the end of a long route; once that route
# reached water it pulsed for twelve seconds without ever sending BlockLine.
BLOCK_TARGET_SCAN = """_stress_character = manager.scene.player.character
_stress_world_object = _stress_character.world_object
_stress_position = _stress_world_object.position
_stress_map = manager.scene.world.map
_stress_target = None
_stress_target_forward_projection = None
_stress_target_horizontal_distance = None
_stress_target_outside_route_hull = False
if not bool(_stress_character.dead):
    _stress_occupied = set()
    for _stress_block in _stress_character.get_blocks_occupied():
        _stress_occupied.add(tuple(int(_stress_block[i]) for i in range(3)))
    _stress_candidates = []
    _stress_px = int(_stress_position[0])
    _stress_py = int(_stress_position[1])
    _stress_pz = int(_stress_position[2])
    _stress_forward_x = float(_stress_world_object.orientation[0])
    _stress_forward_y = float(_stress_world_object.orientation[1])
    _stress_forward_length = max(
        0.000001,
        (_stress_forward_x ** 2 + _stress_forward_y ** 2) ** 0.5,
    )
    _stress_forward_x /= _stress_forward_length
    _stress_forward_y /= _stress_forward_length
    _stress_max_z = int(manager.scene.block_manager.max_modifiable_z)
    for _stress_x in range(max(0, _stress_px - 6), min(511, _stress_px + 6) + 1):
        for _stress_y in range(max(0, _stress_py - 6), min(511, _stress_py + 6) + 1):
            for _stress_z in range(max(0, _stress_pz - 6), min(_stress_max_z, _stress_pz + 6) + 1):
                _stress_cell = (_stress_x, _stress_y, _stress_z)
                if _stress_cell in _stress_occupied:
                    continue
                if _stress_map.get_solid(_stress_x, _stress_y, _stress_z):
                    continue
                # Server placement requires a six-axis face neighbour.  The
                # native has_neighbors(..., 1) helper also accepts diagonal
                # contact; selecting one of those made a syntactically valid
                # BlockLine that the authoritative server correctly rejected.
                _stress_face_supported = False
                for _stress_dx, _stress_dy, _stress_dz in (
                    (1, 0, 0), (-1, 0, 0),
                    (0, 1, 0), (0, -1, 0),
                    (0, 0, 1), (0, 0, -1),
                ):
                    if _stress_map.get_solid(
                        _stress_x + _stress_dx,
                        _stress_y + _stress_dy,
                        _stress_z + _stress_dz,
                    ):
                        _stress_face_supported = True
                        break
                if not _stress_face_supported:
                    continue
                if not manager.scene.block_manager.valid_to_add(_stress_x, _stress_y, _stress_z):
                    continue
                _stress_distance = (
                    (_stress_x + 0.5 - _stress_position[0]) ** 2
                    + (_stress_y + 0.5 - _stress_position[1]) ** 2
                    + (_stress_z + 0.5 - _stress_position[2]) ** 2
                )
                _stress_delta_x = _stress_x + 0.5 - _stress_position[0]
                _stress_delta_y = _stress_y + 0.5 - _stress_position[1]
                _stress_horizontal_distance = (
                    _stress_delta_x ** 2 + _stress_delta_y ** 2
                ) ** 0.5
                _stress_forward_projection = (
                    _stress_delta_x * _stress_forward_x
                    + _stress_delta_y * _stress_forward_y
                )
                _stress_outside_route_hull = (
                    _stress_forward_projection <= -1.5
                    and _stress_horizontal_distance >= 2.0
                )
                if _stress_distance <= 81.0 and _stress_outside_route_hull:
                    _stress_candidates.append(
                        (
                            _stress_distance,
                            _stress_x,
                            _stress_y,
                            _stress_z,
                            _stress_forward_projection,
                            _stress_horizontal_distance,
                        )
                    )
    _stress_candidates.sort()
    if _stress_candidates:
        _stress_target = tuple(_stress_candidates[0][1:4])
        _stress_target_forward_projection = float(_stress_candidates[0][4])
        _stress_target_horizontal_distance = float(_stress_candidates[0][5])
        _stress_target_outside_route_hull = True"""

BLOCK_TARGET_PROBE = BLOCK_TARGET_SCAN + """
_ = {
    'dead': bool(_stress_character.dead),
    'wade': bool(_stress_world_object.wade),
    'target': _stress_target,
    'target_forward_projection': _stress_target_forward_projection,
    'target_horizontal_distance': _stress_target_horizontal_distance,
    'target_outside_route_hull': bool(_stress_target_outside_route_hull),
}"""

REQUEST_RESPAWN = "manager.scene.send_chat('/kill');_='respawn-requested'"

# Exact retail reproduction for the reported failure: send one native
# BlockLine for a proven air cell, wait until both the wallet and that exact
# map cell acknowledge it, engage sprint on the next rendered frame, then jump
# one frame later.  The ring is memory-only and bounded.
START_BLOCK_SPRINT_JUMP = """import pyglet as _stress_pyglet
import time as _stress_time
from pyglet.window import key as _StressKey
try:
    _stress_pyglet.clock.unschedule(_stress_build_sprint_tick)
except (NameError, AttributeError):
    pass
_stress_client_clock_anchor = float(_stress_time.clock())
_stress_controller_monotonic_anchor_ns = float(__CONTROLLER_MONOTONIC_NS__)
_stress_frame_ring = []
_stress_sequence_events = []
_stress_sequence_phase = 'placing'
_stress_sequence_frame = 0
_stress_sequence_elapsed = 0.0
_stress_sequence_initial_blocks = int(manager.scene.player.character.block_count)
_stress_sequence_last_adjust = int(state.reconciliation_adjust_count)
_stress_sequence_last_snap = int(state.reconciliation_snap_count)
_stress_sequence_key_state = {}
_stress_sequence_last_client_loop = None
_stress_sequence_last_clock = None
""" + BLOCK_TARGET_SCAN + """
_stress_sequence_target = _stress_target
_stress_sequence_target_forward_projection = _stress_target_forward_projection
_stress_sequence_target_horizontal_distance = _stress_target_horizontal_distance
_stress_sequence_target_outside_route_hull = bool(_stress_target_outside_route_hull)
_stress_sequence_target_solid_before = (
    None if _stress_sequence_target is None
    else bool(_stress_map.get_solid(*_stress_sequence_target))
)
_stress_sequence_send_attempted = False
_stress_block_action_enabled = bool(__BLOCK_ACTION_ENABLED__)
_stress_control_delay_frames = int(__CONTROL_DELAY_FRAMES__)
def _stress_sequence_key(_key, _down):
    _down = bool(_down)
    if _stress_sequence_key_state.get(_key, False) == _down:
        return
    _stress_sequence_key_state[_key] = _down
    manager.keyboard[_key] = _down
    if _down:
        manager.window.dispatch_event('on_key_press', _key, 0)
    else:
        manager.window.dispatch_event('on_key_release', _key, 0)
def _stress_sequence_event(_name, **_extra):
    _event = {
        'name': str(_name),
        'client_loop': int(manager.scene.loop_count),
        'frame': int(_stress_sequence_frame),
        'blocks': int(manager.scene.player.character.block_count),
    }
    _event.update(_extra)
    _stress_sequence_events.append(_event)
def _stress_build_sprint_tick(dt):
    global _stress_sequence_phase, _stress_sequence_frame
    global _stress_sequence_elapsed, _stress_sequence_last_adjust
    global _stress_sequence_last_snap, _stress_sequence_send_attempted
    global _stress_sequence_last_client_loop, _stress_sequence_last_clock
    _client_loop_now = int(manager.scene.loop_count)
    if _client_loop_now == _stress_sequence_last_client_loop:
        return
    _stress_sequence_last_client_loop = _client_loop_now
    _clock_now = float(_stress_time.clock())
    _frame_clock_dt = (
        0.0 if _stress_sequence_last_clock is None
        else max(0.0, _clock_now - _stress_sequence_last_clock)
    )
    _stress_sequence_last_clock = _clock_now
    _stress_sequence_frame += 1
    # ``schedule`` can call us more than once between GameManager loops. The
    # early-returned callbacks fragment pyglet's callback ``dt`` even though
    # the rendered frame is healthy. Use the accepted loop's QPC delta so a
    # nominal 0.18-second key hold is actually 0.18 seconds.
    _stress_sequence_elapsed += _frame_clock_dt
    _c = manager.scene.player.character
    _w = _c.world_object
    _loop = int(_c.network_position_loop_count)
    _n = _c.network_position
    _old = _c.get_old_movement_data(_loop)
    _matched_error = None
    _matched_error_vector = None
    if _old is not None:
        _hp = _old[0].position
        _matched_error_vector = tuple(
            round(float(_n[i]-_hp[i]), 6) for i in range(3)
        )
        _matched_error = ((_n[0]-_hp[0])**2 + (_n[1]-_hp[1])**2 + (_n[2]-_hp[2])**2)**0.5
    _candidate_errors = {}
    for _offset in range(-3, 4):
        _candidate = _c.get_old_movement_data(_loop + _offset)
        if _candidate is not None:
            _cp = _candidate[0].position
            _candidate_errors[_offset] = round(float(
                ((_n[0]-_cp[0])**2 + (_n[1]-_cp[1])**2 + (_n[2]-_cp[2])**2)**0.5
            ), 6)
    _blocks = int(_c.block_count)
    _target_solid = (
        None if _stress_sequence_target is None
        else bool(manager.scene.world.map.get_solid(*_stress_sequence_target))
    )
    if _stress_sequence_phase == 'placing':
        if not _stress_block_action_enabled:
            if _stress_sequence_frame >= _stress_control_delay_frames:
                _stress_sequence_phase = 'sprint_next_frame'
                _stress_sequence_event(
                    'control_delay_complete',
                    delay_frames=_stress_control_delay_frames,
                )
        elif _stress_sequence_target is None:
            _stress_sequence_phase = 'no_target'
            _stress_sequence_event('block_target_missing')
        elif not _stress_sequence_send_attempted:
            _stress_sequence_send_attempted = True
            manager.scene.send_block_line(
                _stress_sequence_target[0],
                _stress_sequence_target[1],
                _stress_sequence_target[2],
                _stress_sequence_target[0],
                _stress_sequence_target[1],
                _stress_sequence_target[2],
            )
            _stress_sequence_event(
                'block_sent',
                target=_stress_sequence_target,
                solid_before=_stress_sequence_target_solid_before,
                blocks_before=_stress_sequence_initial_blocks,
                forward_projection=_stress_sequence_target_forward_projection,
                horizontal_distance=_stress_sequence_target_horizontal_distance,
                outside_route_hull=_stress_sequence_target_outside_route_hull,
            )
        elif (
            _blocks < _stress_sequence_initial_blocks
            and _target_solid is True
            and _stress_sequence_target_solid_before is False
        ):
            _stress_sequence_phase = 'sprint_next_frame'
            _stress_sequence_event(
                'block_committed',
                target=_stress_sequence_target,
                solid_before=False,
                solid_after=True,
                blocks_before=_stress_sequence_initial_blocks,
                blocks_after=_blocks,
                forward_projection=_stress_sequence_target_forward_projection,
                horizontal_distance=_stress_sequence_target_horizontal_distance,
                outside_route_hull=_stress_sequence_target_outside_route_hull,
            )
    elif _stress_sequence_phase == 'sprint_next_frame':
        _stress_sequence_key(_StressKey.W, True)
        _stress_sequence_key(_StressKey.LSHIFT, True)
        _stress_sequence_phase = 'jump_next_frame'
        _stress_sequence_event('sprint_started')
    elif _stress_sequence_phase == 'jump_next_frame':
        _stress_sequence_key(_StressKey.SPACE, True)
        _stress_sequence_phase = 'jump_held'
        _stress_sequence_elapsed = 0.0
        _stress_sequence_event('jump_started')
    elif _stress_sequence_phase == 'jump_held' and _stress_sequence_elapsed >= 0.18:
        _stress_sequence_key(_StressKey.SPACE, False)
        _stress_sequence_phase = 'sustained'
        _stress_sequence_elapsed = 0.0
        _stress_sequence_event('jump_released')
    elif _stress_sequence_phase == 'sustained':
        _jump_phase = _stress_sequence_elapsed % 1.25
        _stress_sequence_key(_StressKey.SPACE, _jump_phase < 0.16)
    _p = _w.position
    _v = _w.velocity
    _adjust = int(state.reconciliation_adjust_count)
    _snap = int(state.reconciliation_snap_count)
    _stress_frame_ring.append({
        'history_length': int(len(_c.movement_history)),
        'lerp_timer': round(float(_c.position_lerp_timer), 6),
        'reconciliation_snap_count': _snap,
        'reconciliation_adjust_count': _adjust,
        'network_loop': _loop,
        'client_loop': _client_loop_now,
        'matched_loop_error': None if _matched_error is None else round(float(_matched_error), 6),
        'matched_error_vector': _matched_error_vector,
        'candidate_loop_errors': _candidate_errors,
        'position': tuple(round(float(_p[i]), 6) for i in range(3)),
        'network_position': tuple(round(float(_n[i]), 6) for i in range(3)),
        'velocity': tuple(round(float(_v[i]), 6) for i in range(3)),
        'orientation': tuple(round(float(_w.orientation[i]), 6) for i in range(3)),
        'yaw_degrees': round(float(_c.yaw), 6),
        'tool_id': int(manager.scene.player.tool_id),
        'class_id': int(manager.scene.player.get_class_id()),
        'jetpack_id': int(getattr(manager.scene.player, 'jetpack', 0) or 0),
        'jetpack_active': bool(getattr(manager.scene.player, 'jetpack_active', False)),
        'jetpack_fuel': round(float(getattr(_c, 'jetpack_fuel', 0.0)), 6),
        'block_count': _blocks,
        'block_target': _stress_sequence_target,
        'block_target_solid_before': _stress_sequence_target_solid_before,
        'block_target_solid': _target_solid,
        'block_target_forward_projection': _stress_sequence_target_forward_projection,
        'block_target_horizontal_distance': _stress_sequence_target_horizontal_distance,
        'block_target_outside_route_hull': _stress_sequence_target_outside_route_hull,
        'block_send_attempted': bool(_stress_sequence_send_attempted),
        'block_action_enabled': bool(_stress_block_action_enabled),
        'airborne': bool(_w.airborne),
        'wade': bool(_w.wade),
        'sequence_phase': str(_stress_sequence_phase),
        'frame_dt': round(float(_frame_clock_dt), 9),
        # Python 2.7 on Windows exposes QueryPerformanceCounter as time.clock.
        # Keep this in its native monotonic domain; the controller translates
        # only the delta from the paired anchor above.
        'client_clock_seconds': float(_clock_now),
        'sample_duration_ms': 0.0,
    })
    if len(_stress_frame_ring) > 1200:
        del _stress_frame_ring[:-1200]
    _stress_sequence_last_adjust = _adjust
    _stress_sequence_last_snap = _snap
_stress_pyglet.clock.schedule(_stress_build_sprint_tick)
_ = 'build-sprint-jump-started'"""

STOP_BLOCK_SPRINT_JUMP = """import pyglet as _stress_pyglet
from pyglet.window import key as _StressKey
try:
    _stress_pyglet.clock.unschedule(_stress_build_sprint_tick)
except (NameError, AttributeError):
    pass
manager.scene.player.character.set_primary_shoot(False)
for _stress_release_key in (_StressKey.W, _StressKey.LSHIFT, _StressKey.SPACE):
    manager.keyboard[_stress_release_key] = False
    manager.window.dispatch_event('on_key_release', _stress_release_key, 0)
_ = {
    'frames': list(_stress_frame_ring),
    'events': list(_stress_sequence_events),
    'client_clock_anchor': float(_stress_client_clock_anchor),
    'controller_monotonic_anchor_ns': float(_stress_controller_monotonic_anchor_ns),
}"""

RESTORE_FOREGROUND = """try:
    import ctypes as _stress_ctypes
    _stress_hwnd = int(getattr(manager.window, '_hwnd', 0) or 0)
    if _stress_hwnd:
        _stress_ctypes.windll.user32.ShowWindow(_stress_hwnd, 9)
        # Keep the automated retail window above the controller during the
        # timing gate. Merely showing it is insufficient on Windows: the game
        # drops toward 30 FPS when another desktop window remains foreground.
        _stress_ctypes.windll.user32.SetWindowPos(
            _stress_hwnd, -1, 0, 0, 0, 0, 0x0043)
        _stress_ctypes.windll.user32.BringWindowToTop(_stress_hwnd)
        _stress_ctypes.windll.user32.SetForegroundWindow(_stress_hwnd)
        _ = 'foreground-restored'
    else:
        _ = 'foreground-no-hwnd'
except Exception:
    _ = 'foreground-unavailable'"""

ALL_KEYS = ("W", "A", "S", "D", "SPACE", "LCTRL", "LSHIFT")


@dataclass(frozen=True)
class StressSegment:
    """One sustained input phase executed through the normal client path."""

    name: str
    duration: float
    keys: tuple[str, ...] = ()
    turn_degrees: float = 0.0
    pulse_key: str | None = None
    pulse_period: float = 1.25
    pulse_duration: float = 0.16
    tool_id: int | None = None
    pitch_degrees: float | None = None
    primary_period: float | None = None
    primary_duration: float = 0.12
    scripted_sequence: str | None = None
    control_delay_frames: int = 4
    required_class_id: int | None = None
    required_loadout_tools: tuple[int, ...] = ()
    include_by_default: bool = True


DEFAULT_SEGMENTS = (
    StressSegment("settle", 2.0),
    StressSegment(
        "jump_in_place",
        8.0,
        pulse_key="SPACE",
        pulse_period=1.15,
    ),
    StressSegment("walk", 10.0, ("W",)),
    StressSegment("sprint", 12.0, ("W", "LSHIFT")),
    StressSegment("crouch_walk", 8.0, ("W", "LCTRL")),
    StressSegment("turn_left", 8.0, ("W",), turn_degrees=100.0),
    StressSegment("turn_right", 8.0, ("W",), turn_degrees=-200.0),
    StressSegment("slope_diagonal", 12.0, ("W", "D", "LSHIFT")),
    StressSegment(
        "jump_run",
        12.0,
        ("W", "LSHIFT"),
        pulse_key="SPACE",
        pulse_period=1.35,
    ),
    StressSegment(
        "block_build_jump",
        10.0,
        pulse_key="SPACE",
        pulse_period=1.25,
        tool_id=5,
        # Aim steeply at the floor so the retail preview has a valid adjacent
        # voxel even after earlier movement moved the player away from a wall.
        pitch_degrees=60.0,
        primary_period=1.0,
        primary_duration=0.30,
    ),
    StressSegment(
        "block_sprint_jump",
        12.0,
        tool_id=5,
        pitch_degrees=60.0,
        scripted_sequence="block_sprint_jump",
    ),
    # Engineer pack 68 needs a continuous hold: its stock 0.25-second start
    # delay means the short bunny-hop pulses above never exercise thrust.
    StressSegment(
        "engineer_jetpack_hold",
        4.0,
        ("SPACE",),
        required_class_id=12,
        # Engineer's equipment slot is pack 68 OR Disguise 64. A saved
        # Disguise choice is valid and must not be misreported as broken
        # flight; explicitly select the pack for this flight-only scenario.
        required_loadout_tools=(68,),
        include_by_default=False,
    ),
    StressSegment(
        "rocketeer_jump_pack_hold",
        4.0,
        ("SPACE",),
        required_class_id=2,
        # The retail loadout calls pack 66 the Jump Pack. It shares the class
        # slot with Glide Pack/Jetpack2 but has different thrust and fuel
        # constants, so both must have an independent reconciliation gate.
        required_loadout_tools=(66,),
        include_by_default=False,
    ),
    StressSegment(
        "rocketeer_jetpack2_hold",
        4.0,
        ("SPACE",),
        required_class_id=2,
        # Jetpack2 is the Rocketeer's first equipment choice, not a second
        # Engineer fuel/state channel.
        required_loadout_tools=(67,),
        include_by_default=False,
    ),
    StressSegment(
        "flying_entity_jump",
        10.0,
        ("W",),
        turn_degrees=120.0,
        pulse_key="SPACE",
        pulse_period=1.35,
        # Snowblower shots use the same server contact-flight/entity lifecycle
        # as rockets but deal only ten damage, so dense spawn geometry cannot
        # invalidate the movement sample by killing the shooter.
        tool_id=29,
        pitch_degrees=-35.0,
        primary_period=0.9,
        primary_duration=0.35,
        required_class_id=12,
        include_by_default=False,
    ),
    StressSegment(
        "specialist_machete_dig",
        3.0,
        tool_id=50,
        pitch_degrees=60.0,
        primary_period=0.75,
        primary_duration=0.18,
        required_class_id=16,
        required_loadout_tools=(50,),
        include_by_default=False,
    ),
    StressSegment(
        "fall_recovery",
        10.0,
        ("W",),
        turn_degrees=70.0,
        pulse_key="SPACE",
        pulse_period=2.2,
    ),
    StressSegment("reverse", 8.0, ("S",)),
    StressSegment("cooldown", 2.0),
)


@dataclass(frozen=True)
class StressThresholds:
    """Release gate for observable retail-client reconciliation jitter."""

    max_snaps: int = 0
    max_adjusts: int = 0
    max_visible_rollbacks: int = 0
    max_horizontal_step: float = 3.0
    max_backward_step: float = 0.75
    # Samples are intentionally slower than native Character.update calls.
    # Normalize vertical travel by client-loop advance so ordinary jump
    # ascent is not mistaken for a one-frame position restore.
    max_vertical_step_per_client_loop: float = 0.5
    max_matched_error: float = 0.1
    max_abs_loop_lag: int = 8
    max_network_regressions: int = 0
    max_sample_gap_factor: float = 4.0
    max_stalls: int = 2


@dataclass(frozen=True)
class SegmentAnalysis:
    name: str
    sample_count: int
    snap_count: int
    adjust_count: int
    max_matched_error: float
    max_abs_loop_lag: int
    airborne_samples: int
    position_z_span: float


@dataclass(frozen=True)
class StressAnalysis:
    sample_count: int
    duration_seconds: float
    snap_count: int
    adjust_count: int
    visible_rollback_count: int
    max_horizontal_step: float
    max_backward_step: float
    max_vertical_step: float
    unmatched_count: int
    network_loop_regressions: int
    max_matched_error: float
    p95_matched_error: float
    max_abs_loop_lag: int
    p95_abs_loop_lag: float
    max_sample_gap_seconds: float
    stall_count: int
    airborne_samples: int
    slope_covered: bool
    passed: bool
    failure_reasons: tuple[str, ...]


def _percentile(values: Sequence[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without external packages."""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * float(percentile)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _correction_counts(samples: Sequence[Mapping[str, object]]) -> tuple[int, int]:
    counter_keys = (
        "reconciliation_snap_count",
        "reconciliation_adjust_count",
    )
    if samples and all(
        all(key in sample for key in counter_keys) for sample in samples
    ):
        snaps = 0
        adjusts = 0
        previous_snap = int(samples[0][counter_keys[0]])
        previous_adjust = int(samples[0][counter_keys[1]])
        for sample in samples[1:]:
            current_snap = int(sample[counter_keys[0]])
            current_adjust = int(sample[counter_keys[1]])
            snaps += max(0, current_snap - previous_snap)
            adjusts += max(0, current_adjust - previous_adjust)
            previous_snap = current_snap
            previous_adjust = current_adjust
        return snaps, adjusts

    snaps = 0
    adjusts = 0
    previous_history: int | None = None
    previous_timer: float | None = None
    for sample in samples:
        history = int(sample["history_length"])
        timer = float(sample["lerp_timer"])
        if previous_history is not None and previous_history > 8 and history <= 1:
            snaps += 1
        if previous_timer is not None and timer > previous_timer + 1e-6:
            adjusts += 1
        previous_history = history
        previous_timer = timer
    return snaps, adjusts


def analyze_stress_samples(
    samples: Iterable[Mapping[str, object]],
    *,
    interval: float,
    thresholds: StressThresholds | None = None,
) -> tuple[StressAnalysis, list[SegmentAnalysis], list[dict]]:
    """Analyze client observations and return gate, per-phase data, and events.

    A SNAP is the stock client's hard reconciliation path clearing movement
    history.  An ADJUST is the softer path rearming its position lerp timer.
    Both are visible without patching the native reconciliation function.
    """
    thresholds = thresholds or StressThresholds()
    snapshots = list(samples)
    errors = [
        float(sample["matched_loop_error"])
        for sample in snapshots
        if sample.get("matched_loop_error") is not None
    ]
    loop_lags = [
        abs(int(sample["client_loop"]) - int(sample["network_loop"]))
        for sample in snapshots
        if int(sample.get("network_loop", 0)) > 0
    ]
    # Engineer activation/exhaustion intentionally withholds only the local
    # position row while GameScene crosses an unacknowledged WorldUpdate state
    # boundary. Global/observer snapshots continue at 30 Hz, but the tracer's
    # cached owner-row loop is expected to age during this segment. The native
    # SNAP/ADJUST and visible-rollback gates remain active.
    gated_loop_lags = [
        abs(int(sample["client_loop"]) - int(sample["network_loop"]))
        for sample in snapshots
        if int(sample.get("network_loop", 0)) > 0
        and str(sample.get("segment", "")) not in {
            "engineer_jetpack_hold",
            "rocketeer_jump_pack_hold",
            "rocketeer_jetpack2_hold",
        }
    ]
    gaps = [
        (float(current["monotonic_ns"]) - float(previous["monotonic_ns"]))
        / 1_000_000_000.0
        for previous, current in zip(snapshots, snapshots[1:])
        if (
            str(previous.get("segment", "")),
            int(previous.get("cycle", 0)),
        ) == (
            str(current.get("segment", "")),
            int(current.get("cycle", 0)),
        )
    ]
    stall_limit = max(0.01, float(interval) * thresholds.max_sample_gap_factor)
    stalls = [gap for gap in gaps if gap > stall_limit]

    regressions = 0
    events: list[dict] = []
    previous_history: int | None = None
    previous_timer: float | None = None
    previous_network_loop: int | None = None
    previous_snap_counter: int | None = None
    previous_adjust_counter: int | None = None
    previous_position: tuple[float, float, float] | None = None
    previous_client_loop: int | None = None
    previous_group: tuple[str, int] | None = None
    max_horizontal_step = 0.0
    max_backward_step = 0.0
    max_vertical_step = 0.0
    counter_mode = bool(snapshots) and all(
        "reconciliation_snap_count" in sample
        and "reconciliation_adjust_count" in sample
        for sample in snapshots
    )
    for index, sample in enumerate(snapshots):
        group = (
            str(sample.get("segment", "")),
            int(sample.get("cycle", 0)),
        )
        if previous_group is not None and group != previous_group:
            # Respawn/preflight time between repeated cycles is outside the
            # measured input stream.  Do not turn its position jump, counter
            # growth, or network-label discontinuity into gameplay jitter.
            previous_history = None
            previous_timer = None
            previous_network_loop = None
            previous_snap_counter = None
            previous_adjust_counter = None
            previous_position = None
            previous_client_loop = None
        history = int(sample["history_length"])
        timer = float(sample["lerp_timer"])
        network_loop = int(sample["network_loop"])
        common = {
            "sample_index": index,
            "segment": str(sample.get("segment", "")),
            "cycle": int(sample.get("cycle", 0)),
            "client_loop": int(sample["client_loop"]),
            "network_loop": network_loop,
            "matched_loop_error": sample.get("matched_loop_error"),
        }
        position = tuple(float(value) for value in sample["position"])  # type: ignore[index]
        if previous_position is not None:
            dx = position[0] - previous_position[0]
            dy = position[1] - previous_position[1]
            dz = position[2] - previous_position[2]
            horizontal_step = math.hypot(dx, dy)
            vertical_step = abs(dz)
            client_loop_delta = max(
                1,
                common["client_loop"] - int(previous_client_loop or 0),
            )
            vertical_step_per_loop = vertical_step / client_loop_delta
            max_horizontal_step = max(max_horizontal_step, horizontal_step)
            max_vertical_step = max(max_vertical_step, vertical_step)
            try:
                ox, oy, _oz = sample["orientation"]  # type: ignore[misc]
                backward_step = max(0.0, -(dx * float(ox) + dy * float(oy)))
            except (TypeError, ValueError):
                backward_step = 0.0
            max_backward_step = max(max_backward_step, backward_step)
            if horizontal_step > thresholds.max_horizontal_step:
                events.append({
                    "kind": "visible_teleport",
                    "count": 1,
                    "horizontal_step": round(horizontal_step, 6),
                    "vertical_step": round(vertical_step, 6),
                    **common,
                })
            non_forward_segments = {"reverse", "turn_left", "turn_right"}
            if (
                common["segment"] not in non_forward_segments
                and backward_step > thresholds.max_backward_step
            ):
                events.append({
                    "kind": "visible_rollback",
                    "count": 1,
                    "backward_step": round(backward_step, 6),
                    **common,
                })
            velocity = sample.get("velocity", (0.0, 0.0, 0.0))
            try:
                vertical_velocity = float(velocity[2])  # type: ignore[index]
            except (TypeError, ValueError, IndexError):
                vertical_velocity = 0.0
            # AoS uses increasing Z downward. A large positive step while the
            # player is airborne with positive vertical velocity is a real
            # fall (often after a Snowblower destroys the supporting voxel),
            # not a reconciliation rollback. Native SNAP counters still catch
            # actual history resets independently.
            explained_fall = (
                dz > 0.0
                and bool(sample.get("airborne", False))
                and vertical_velocity > 0.0
            )
            explained_jetpack_climb = (
                common["segment"] in {
                    "engineer_jetpack_hold",
                    "rocketeer_jump_pack_hold",
                    "rocketeer_jetpack2_hold",
                }
                and dz < 0.0
                and bool(sample.get("airborne", False))
                and vertical_velocity < 0.0
            )
            # Native collision resolution can climb one voxel in a single
            # rendered loop.  It is distinguishable from a stale owner-row
            # restore: the body is grounded with zero vertical velocity, the
            # step is at most one block, and the server row still matches the
            # client's history to within a tiny fraction of a block.  The
            # packaged release gate captured exactly this shape (0.564 block,
            # 0.002485 matched error, no reconciliation counter increment).
            try:
                matched_error = float(sample.get("matched_loop_error"))
            except (TypeError, ValueError):
                matched_error = float("inf")
            explained_ground_step = (
                not bool(sample.get("airborne", False))
                and abs(vertical_velocity) <= 1e-6
                and vertical_step <= 1.05
                and matched_error <= min(0.02, thresholds.max_matched_error * 0.2)
            )
            if (
                vertical_step_per_loop
                > thresholds.max_vertical_step_per_client_loop
                and not explained_fall
                and not explained_jetpack_climb
                and not explained_ground_step
            ):
                events.append({
                    "kind": "visible_vertical_snap",
                    "count": 1,
                    "vertical_step": round(vertical_step, 6),
                    "vertical_step_per_client_loop": round(
                        vertical_step_per_loop,
                        6,
                    ),
                    **common,
                })
        if counter_mode:
            snap_counter = int(sample["reconciliation_snap_count"])
            adjust_counter = int(sample["reconciliation_adjust_count"])
            snap_delta = (
                max(0, snap_counter - previous_snap_counter)
                if previous_snap_counter is not None else 0
            )
            adjust_delta = (
                max(0, adjust_counter - previous_adjust_counter)
                if previous_adjust_counter is not None else 0
            )
            if snap_delta:
                events.append({"kind": "snap", "count": snap_delta, **common})
            if adjust_delta:
                events.append({"kind": "adjust", "count": adjust_delta, **common})
            previous_snap_counter = snap_counter
            previous_adjust_counter = adjust_counter
        else:
            if previous_history is not None and previous_history > 8 and history <= 1:
                events.append({"kind": "snap", "count": 1, **common})
            if previous_timer is not None and timer > previous_timer + 1e-6:
                events.append({"kind": "adjust", "count": 1, **common})
        if (
            previous_network_loop is not None
            and network_loop > 0
            and previous_network_loop > 0
            and network_loop < previous_network_loop
        ):
            regressions += 1
            events.append({"kind": "network_loop_regression", **common})
        previous_history = history
        previous_timer = timer
        previous_network_loop = network_loop
        previous_position = position
        previous_client_loop = common["client_loop"]
        previous_group = group

    snaps = sum(
        int(event.get("count", 1)) for event in events
        if event["kind"] == "snap"
    )
    adjusts = sum(
        int(event.get("count", 1)) for event in events
        if event["kind"] == "adjust"
    )
    visible_rollbacks = sum(
        int(event.get("count", 1)) for event in events
        if event["kind"] in {
            "visible_rollback",
            "visible_teleport",
            "visible_vertical_snap",
        }
    )

    segment_results: list[SegmentAnalysis] = []
    segment_names = list(dict.fromkeys(str(row.get("segment", "")) for row in snapshots))
    for name in segment_names:
        rows = [row for row in snapshots if str(row.get("segment", "")) == name]
        segment_snaps = sum(
            int(event.get("count", 1)) for event in events
            if event["kind"] == "snap" and event["segment"] == name
        )
        segment_adjusts = sum(
            int(event.get("count", 1)) for event in events
            if event["kind"] == "adjust" and event["segment"] == name
        )
        segment_errors = [
            float(row["matched_loop_error"])
            for row in rows
            if row.get("matched_loop_error") is not None
        ]
        segment_lags = [
            abs(int(row["client_loop"]) - int(row["network_loop"]))
            for row in rows
            if int(row.get("network_loop", 0)) > 0
        ]
        z_values = [float(row["position"][2]) for row in rows]  # type: ignore[index]
        segment_results.append(
            SegmentAnalysis(
                name=name,
                sample_count=len(rows),
                snap_count=segment_snaps,
                adjust_count=segment_adjusts,
                max_matched_error=max(segment_errors, default=0.0),
                max_abs_loop_lag=max(segment_lags, default=0),
                airborne_samples=sum(bool(row.get("airborne")) for row in rows),
                position_z_span=(max(z_values) - min(z_values)) if z_values else 0.0,
            )
        )

    airborne_samples = sum(bool(row.get("airborne")) for row in snapshots)
    slope_result = next(
        (row for row in segment_results if row.name == "slope_diagonal"), None
    )
    slope_covered = slope_result is None or slope_result.position_z_span >= 0.15
    failures: list[str] = []
    if not snapshots:
        failures.append("no_samples")
    if snaps > thresholds.max_snaps:
        failures.append("hard_snap_limit")
    if adjusts > thresholds.max_adjusts:
        failures.append("soft_adjust_limit")
    if visible_rollbacks > thresholds.max_visible_rollbacks:
        failures.append("visible_rollback_limit")
    if max(errors, default=0.0) > thresholds.max_matched_error:
        failures.append("matched_error_limit")
    if max(gated_loop_lags, default=0) > thresholds.max_abs_loop_lag:
        failures.append("loop_lag_limit")
    if regressions > thresholds.max_network_regressions:
        failures.append("network_loop_regression")
    if len(stalls) > thresholds.max_stalls:
        failures.append("sample_stall_limit")
    if any(row.name in {"jump_run", "fall_recovery"} for row in segment_results):
        if airborne_samples == 0:
            failures.append("airborne_path_not_covered")
    if not slope_covered:
        failures.append("slope_path_not_covered")

    expected_feature_tools = {
        "block_build_jump": 5,
        "block_sprint_jump": 5,
        "flying_entity_jump": 29,
    }
    for segment_name, expected_tool in expected_feature_tools.items():
        rows = [
            row for row in snapshots
            if str(row.get("segment", "")) == segment_name
        ]
        if not rows:
            continue
        if not any(int(row.get("tool_id", -1)) == expected_tool for row in rows):
            failures.append(f"{segment_name}_tool_not_active")
        consumed = 0
        previous_by_cycle: dict[int, int] = {}
        for row in rows:
            cycle = int(row.get("cycle", 0))
            current = int(row.get("block_count", 0))
            previous = previous_by_cycle.get(cycle)
            if previous is not None:
                consumed += max(0, previous - current)
            previous_by_cycle[cycle] = current
        if consumed <= 0:
            failures.append(f"{segment_name}_action_not_exercised")
        else:
            events.append({
                "kind": "feature_action",
                "segment": segment_name,
                "count": consumed,
                "tool_id": expected_tool,
            })

    # Inventory alone is not proof of a successful block transition.  It can
    # decrease before an echo is rendered, while a rejected/desynced map cell
    # remains air.  Correlate the exact target selected as air with the native
    # client's later solid-map observation, once per repeat cycle.
    block_rows = [
        row for row in snapshots
        if str(row.get("segment", "")) == "block_sprint_jump"
    ]
    if block_rows:
        block_cycles = list(
            dict.fromkeys(int(row.get("cycle", 0)) for row in block_rows)
        )
        all_cycles_mutated = True
        all_cycles_isolated = True
        any_target = False
        for cycle in block_cycles:
            rows = [
                row for row in block_rows
                if int(row.get("cycle", 0)) == cycle
            ]
            targets = [
                tuple(int(value) for value in row["block_target"])  # type: ignore[arg-type]
                for row in rows
                if row.get("block_target") is not None
            ]
            target = targets[0] if targets else None
            if target is not None:
                any_target = True
            target_is_stable = bool(targets) and all(
                candidate == target for candidate in targets
            )
            solid_before = target_is_stable and any(
                row.get("block_target_solid_before") is False for row in rows
            )
            solid_after = target_is_stable and any(
                row.get("block_target_solid") is True for row in rows
            )
            outside_route_hull = target_is_stable and all(
                row.get("block_target_outside_route_hull") is True
                for row in rows
            )
            projections = [
                float(row["block_target_forward_projection"])
                for row in rows
                if row.get("block_target_forward_projection") is not None
            ]
            horizontal_distances = [
                float(row["block_target_horizontal_distance"])
                for row in rows
                if row.get("block_target_horizontal_distance") is not None
            ]
            inventory_before = int(rows[0].get("block_count", 0))
            inventory_after = min(
                int(row.get("block_count", inventory_before)) for row in rows
            )
            inventory_consumed = max(0, inventory_before - inventory_after)
            mutation_observed = bool(
                target is not None
                and solid_before
                and solid_after
                and inventory_consumed > 0
                and outside_route_hull
            )
            all_cycles_mutated = all_cycles_mutated and mutation_observed
            all_cycles_isolated = all_cycles_isolated and outside_route_hull
            if mutation_observed:
                events.append({
                    "kind": "block_mutation",
                    "segment": "block_sprint_jump",
                    "cycle": cycle,
                    "target": target,
                    "solid_before": False,
                    "solid_after": True,
                    "inventory_before": inventory_before,
                    "inventory_after": inventory_after,
                    "inventory_consumed": inventory_consumed,
                    "forward_projection": projections[0],
                    "horizontal_distance": horizontal_distances[0],
                    "outside_route_hull": True,
                })
        if not any_target:
            failures.append("block_sprint_jump_target_not_selected")
        if not all_cycles_isolated:
            failures.append("block_sprint_jump_target_intersects_route_hull")
        if not all_cycles_mutated:
            failures.append("block_sprint_jump_map_mutation_not_observed")

    for jetpack_segment in (
        "engineer_jetpack_hold",
        "rocketeer_jump_pack_hold",
        "rocketeer_jetpack2_hold",
    ):
        jetpack_rows = [
            row for row in snapshots
            if str(row.get("segment", "")) == jetpack_segment
        ]
        if not jetpack_rows:
            continue
        activated = any(bool(row.get("jetpack_active")) for row in jetpack_rows)
        fuels = [float(row.get("jetpack_fuel", 0.0)) for row in jetpack_rows]
        drained = bool(fuels) and (max(fuels) - min(fuels)) >= 1.0
        if not activated:
            failures.append(f"{jetpack_segment}_not_activated")
        if not drained:
            failures.append(f"{jetpack_segment}_fuel_not_drained")
        if activated and drained:
            events.append({
                "kind": "jetpack_activation",
                "segment": jetpack_segment,
                "fuel_start": fuels[0],
                "fuel_min": min(fuels),
            })

    duration = 0.0
    if len(snapshots) >= 2:
        duration = (
            float(snapshots[-1]["monotonic_ns"])
            - float(snapshots[0]["monotonic_ns"])
        ) / 1_000_000_000.0
    analysis = StressAnalysis(
        sample_count=len(snapshots),
        duration_seconds=duration,
        snap_count=snaps,
        adjust_count=adjusts,
        visible_rollback_count=visible_rollbacks,
        max_horizontal_step=max_horizontal_step,
        max_backward_step=max_backward_step,
        max_vertical_step=max_vertical_step,
        unmatched_count=len(snapshots) - len(errors),
        network_loop_regressions=regressions,
        max_matched_error=max(errors, default=0.0),
        p95_matched_error=_percentile(errors, 0.95),
        max_abs_loop_lag=max(loop_lags, default=0),
        p95_abs_loop_lag=_percentile(loop_lags, 0.95),
        max_sample_gap_seconds=max(gaps, default=0.0),
        stall_count=len(stalls),
        airborne_samples=airborne_samples,
        slope_covered=slope_covered,
        passed=not failures,
        failure_reasons=tuple(failures),
    )
    return analysis, segment_results, events


def _read_mapping(console: GameConsole) -> dict:
    result = ast.literal_eval(console.run(CLIENT_SAMPLE))
    if not isinstance(result, dict):
        raise TypeError(f"client sample was not a mapping: {result!r}")
    return result


def _read_block_target_probe(console: GameConsole) -> dict:
    """Read the retail client's dry-ground block-placement precondition."""

    result = ast.literal_eval(console.run(BLOCK_TARGET_PROBE))
    if not isinstance(result, dict):
        raise TypeError(f"block target probe was not a mapping: {result!r}")
    target = result.get("target")
    if target is not None:
        result["target"] = tuple(int(value) for value in target)
    return result


def _prepare_block_sequence(
    console: GameConsole,
    *,
    force_respawn: bool = False,
    timeout: float = 15.0,
    poll_interval: float = 0.1,
) -> dict:
    """Ensure the scripted block transition starts near a real build face.

    Long movement routes on ArcticBase end in water.  A downward click there
    has no solid face within retail build reach, so the old scenario silently
    measured twelve seconds of *no placement*.  If the native map probe cannot
    select a dry, supported air cell, request the server's normal `/kill`
    respawn and wait for a fresh dry spawn before starting measurement.

    The polling loop uses the controller's monotonic clock and performs no
    gameplay mutation beyond that explicit validation-only respawn.
    """

    if timeout <= 0 or poll_interval < 0:
        raise ValueError("block preflight timeout must be positive")
    state = _read_block_target_probe(console)
    ready = (
        not bool(state.get("dead"))
        and not bool(state.get("wade"))
        and state.get("target") is not None
    )
    if ready and not force_respawn:
        state["respawned"] = False
        return state

    saw_dead = bool(state.get("dead"))
    if not saw_dead:
        console.run(REQUEST_RESPAWN)
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        if poll_interval:
            time.sleep(poll_interval)
        state = _read_block_target_probe(console)
        if bool(state.get("dead")):
            saw_dead = True
            continue
        if saw_dead:
            ready = (
                not bool(state.get("wade"))
                and state.get("target") is not None
            )
            if ready:
                state["respawned"] = True
                return state
            # CreatePlayer can precede the native occupancy/map view by a
            # rendered frame.  Keep polling instead of classifying that
            # transient alive/no-target state as a bad spawn.
    raise TimeoutError("timed out waiting for a dry block-sequence respawn")


def _set_key(console: GameConsole, key: str, pressed: bool) -> None:
    console.run((PRESS_KEY if pressed else RELEASE_KEY).format(key=key))


def _release_all(console: GameConsole) -> None:
    """Best-effort cleanup so a failed run never leaves synthetic keys held."""
    for key in ALL_KEYS:
        try:
            _set_key(console, key, False)
        except ConsoleError:
            pass


def _start_yaw_ramp(
    console: GameConsole,
    initial_yaw: float,
    turn_degrees: float,
    duration: float,
) -> None:
    """Schedule the complete turn inside the retail client's own clock.

    A prior version sent one synchronous console command every sample.  Each
    round trip occupied the game thread for 15--32 ms, manufacturing the very
    frame hitches and reconciliation errors this scenario is meant to measure.
    The callback below runs once per client frame; this controller performs one
    start RPC and one cleanup RPC for the whole segment.
    """
    console.run(
        START_YAW_RAMP.format(
            duration=max(0.001, float(duration)),
            start=float(initial_yaw),
            delta=float(turn_degrees),
        )
    )


def _stop_yaw_ramp(console: GameConsole) -> None:
    """Unschedule a client-side yaw callback after success or failure."""
    console.run(STOP_YAW_RAMP)


def _start_key_pulse(
    console: GameConsole,
    key: str,
    period: float,
    down_for: float,
    total: float,
) -> None:
    """Schedule every pulse edge inside the retail client's frame clock."""
    if key not in ALL_KEYS:
        raise ValueError(f"unsupported stress key: {key}")
    console.run(
        START_KEY_PULSE.format(
            key=key,
            period=max(0.001, float(period)),
            down_for=max(0.0, float(down_for)),
            total=max(0.001, float(total)),
        )
    )


def _stop_key_pulse(console: GameConsole) -> None:
    """Unschedule and release a pulse key after success or failure."""
    console.run(STOP_KEY_PULSE)


def _start_primary_pulse(
    console: GameConsole,
    period: float,
    down_for: float,
    total: float,
) -> None:
    """Pulse native primary-fire from the retail client's frame clock."""
    console.run(
        START_PRIMARY_PULSE.format(
            period=max(0.001, float(period)),
            down_for=max(0.0, float(down_for)),
            total=max(0.001, float(total)),
        )
    )


def _stop_primary_pulse(console: GameConsole) -> None:
    """Unschedule primary-fire and guarantee the button is released."""
    console.run(STOP_PRIMARY_PULSE)


def _start_scripted_sequence(
    console: GameConsole,
    name: str,
    *,
    control_delay_frames: int = 4,
) -> None:
    """Start a named per-frame reproduction sequence on the retail clock."""

    if name not in {"block_sprint_jump", "no_block_sprint_jump"}:
        raise ValueError(f"unknown scripted movement sequence: {name}")
    if control_delay_frames < 1:
        raise ValueError("control delay must be at least one rendered frame")
    controller_anchor_ns = time.monotonic_ns()
    code = START_BLOCK_SPRINT_JUMP
    replacements = {
        "__CONTROLLER_MONOTONIC_NS__": str(controller_anchor_ns),
        "__BLOCK_ACTION_ENABLED__": (
            "1" if name == "block_sprint_jump" else "0"
        ),
        "__CONTROL_DELAY_FRAMES__": str(int(control_delay_frames)),
    }
    for marker, value in replacements.items():
        code = code.replace(marker, value)
    console.run(code)


def _stop_scripted_sequence(
    console: GameConsole,
    name: str,
    *,
    segment: str,
    repeat: int,
) -> list[dict]:
    """Stop a scripted sequence and fetch its bounded zero-I/O frame ring."""

    if name not in {"block_sprint_jump", "no_block_sprint_jump"}:
        raise ValueError(f"unknown scripted movement sequence: {name}")
    payload = ast.literal_eval(console.run(STOP_BLOCK_SPRINT_JUMP))
    if not isinstance(payload, dict):
        raise TypeError(f"scripted sequence payload was not a mapping: {payload!r}")
    frames = payload.get("frames", [])
    events = payload.get("events", [])
    client_anchor = float(payload["client_clock_anchor"])
    controller_anchor_ns = int(float(payload["controller_monotonic_anchor_ns"]))
    normalized: list[dict] = []
    for frame in frames:
        row = dict(frame)
        client_clock = float(row.pop("client_clock_seconds"))
        row["monotonic_ns"] = controller_anchor_ns + int(
            (client_clock - client_anchor) * 1_000_000_000.0
        )
        row["segment"] = segment
        row["cycle"] = repeat
        normalized.append(row)
    if normalized:
        normalized[-1]["sequence_events"] = events
    return normalized


def _restore_foreground(console: GameConsole) -> None:
    """Best-effort foreground restore before measuring retail frame pacing."""
    console.run(RESTORE_FOREGROUND)


def _set_pitch_with_mouse(console: GameConsole, target_degrees: float) -> float:
    """Aim through the native mouse path and return the reached pitch.

    Writing ``Character.pitch`` directly makes ``ClientData`` look correct but
    leaves the retail weapon's shot vector partially stale: ShootPacket then
    carries the pitch angle in its Y component.  Driving ``GameScene.mouse_move``
    updates both representations exactly as real mouse input does.
    """
    code = """_stress_pitch_target = float(%r)
_stress_pitch_character = manager.scene.player.character
for _stress_pitch_step in range(12):
    _stress_pitch_delta = _stress_pitch_target - float(_stress_pitch_character.pitch)
    if abs(_stress_pitch_delta) <= 0.05:
        break
    _stress_pitch_mouse_delta = min(50.0, max(0.25, abs(_stress_pitch_delta) * 2.3))
    if _stress_pitch_delta > 0.0:
        _stress_pitch_mouse_delta = -_stress_pitch_mouse_delta
    manager.scene.mouse_move(0, 0, 0, _stress_pitch_mouse_delta)
_ = repr(float(_stress_pitch_character.pitch))""" % float(target_degrees)
    return float(ast.literal_eval(console.run(code)))


def collect_segment(
    console: GameConsole,
    segment: StressSegment,
    *,
    interval: float,
    repeat: int,
) -> list[dict]:
    """Drive one segment and capture timestamped reconciliation observations."""
    marker = f"cycle-{repeat}:{segment.name}"
    console.run("repr(tag(%r))" % marker)
    if segment.scripted_sequence in {
        "block_sprint_jump",
        "no_block_sprint_jump",
    }:
        _prepare_block_sequence(console)
    initial = _read_mapping(console)
    initial_tool_id = int(initial.get("tool_id", 0))
    initial_pitch = float(
        ast.literal_eval(
            console.run("repr(float(manager.scene.player.character.pitch))")
        )
    )
    if segment.tool_id is not None:
        console.run(
            "manager.scene.player.set_tool(%d, True);_='tool-set'"
            % int(segment.tool_id)
        )
        # Let on_set/HUD and the next ClientData settle before measurement.
        time.sleep(0.25)
    if segment.pitch_degrees is not None:
        _set_pitch_with_mouse(console, float(segment.pitch_degrees))
    baseline = _read_mapping(console)
    baseline["segment"] = segment.name
    baseline["cycle"] = repeat
    baseline["monotonic_ns"] = time.monotonic_ns()
    baseline["sample_duration_ms"] = 0.0
    initial_yaw = float(baseline["yaw_degrees"])
    samples: list[dict] = [baseline]
    scripted_frames: list[dict] = []
    started = time.monotonic()
    deadline = started + segment.duration
    if segment.scripted_sequence is not None:
        _start_scripted_sequence(
            console,
            segment.scripted_sequence,
            control_delay_frames=segment.control_delay_frames,
        )
    for key in segment.keys:
        _set_key(console, key, True)
    if segment.turn_degrees:
        _start_yaw_ramp(
            console,
            initial_yaw,
            segment.turn_degrees,
            segment.duration,
        )
    if segment.pulse_key:
        _start_key_pulse(
            console,
            segment.pulse_key,
            segment.pulse_period,
            segment.pulse_duration,
            segment.duration,
        )
    if segment.primary_period is not None:
        _start_primary_pulse(
            console,
            segment.primary_period,
            segment.primary_duration,
            segment.duration,
        )
    try:
        while time.monotonic() < deadline:
            sample_started = time.monotonic_ns()
            sample = _read_mapping(console)
            sample["segment"] = segment.name
            sample["cycle"] = repeat
            sample["monotonic_ns"] = time.monotonic_ns()
            sample["sample_duration_ms"] = (
                sample["monotonic_ns"] - sample_started
            ) / 1_000_000.0
            samples.append(sample)
            remaining = interval - ((time.monotonic_ns() - sample_started) / 1e9)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        if segment.scripted_sequence is not None:
            try:
                scripted_frames = _stop_scripted_sequence(
                    console,
                    segment.scripted_sequence,
                    segment=segment.name,
                    repeat=repeat,
                )
            except ConsoleError:
                scripted_frames = []
        if segment.turn_degrees:
            try:
                _stop_yaw_ramp(console)
            except ConsoleError:
                pass
        if segment.pulse_key:
            try:
                _stop_key_pulse(console)
            except ConsoleError:
                pass
        if segment.primary_period is not None:
            try:
                _stop_primary_pulse(console)
            except ConsoleError:
                pass
        for key in reversed(segment.keys):
            _set_key(console, key, False)
        if segment.pitch_degrees is not None:
            _set_pitch_with_mouse(console, initial_pitch)
        if segment.tool_id is not None and initial_tool_id:
            console.run(
                "manager.scene.player.set_tool(%d, True);_='tool-restored'"
                % initial_tool_id
            )
        console.run("repr(tag(''))")
    return scripted_frames or samples


def run_scenario(
    console: GameConsole,
    *,
    segments: Sequence[StressSegment] = DEFAULT_SEGMENTS,
    repeats: int = 2,
    interval: float = 0.05,
    thresholds: StressThresholds | None = None,
) -> dict:
    """Execute all phases and return a JSON-serializable stress report."""
    _restore_foreground(console)
    scene = ast.literal_eval(console.run("manager.scene.__class__.__name__"))
    if scene != "GameScene":
        raise RuntimeError(f"client is not in GameScene: {scene!r}")
    thresholds = thresholds or StressThresholds()
    samples: list[dict] = []
    started_at = datetime.now(timezone.utc)
    _release_all(console)
    try:
        for cycle in range(1, repeats + 1):
            for segment in segments:
                samples.extend(
                    collect_segment(
                        console,
                        segment,
                        interval=interval,
                        repeat=cycle,
                    )
                )
    finally:
        _release_all(console)
        try:
            console.run("repr(tag(''))")
        except ConsoleError:
            pass

    analysis, segment_results, events = analyze_stress_samples(
        samples,
        interval=interval,
        thresholds=thresholds,
    )
    tracer = ast.literal_eval(
        console.run(
            "{'session_id': str(state.session_id), "
            "'capture_path': state.capture_path, "
            "'capture_on': bool(state.capture_on), "
            "'tick_count': int(state.tick_count), "
            "'pre_correction_events': list(getattr(state, "
            "'reconciliation_events', []))}"
        )
    )
    return {
        "schema_version": 1,
        "scenario": "movement_stress",
        "created_at": started_at.isoformat(),
        "configuration": {
            "interval_seconds": interval,
            "repeats": repeats,
            "segments": [asdict(segment) for segment in segments],
            "thresholds": asdict(thresholds),
        },
        "tracer": tracer,
        "analysis": asdict(analysis),
        "segment_analysis": [asdict(row) for row in segment_results],
        "correction_events": events,
        "feature_evidence": {
            "block_mutations": [
                dict(event)
                for event in events
                if event.get("kind") == "block_mutation"
            ],
        },
        "samples": samples,
    }


def write_report(report: Mapping[str, object], artifact_dir: Path) -> Path:
    """Atomically publish one machine-readable scenario result."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = artifact_dir / f"movement-stress-{stamp}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _auto_join(
    console_port: int,
    server: str,
    team: int,
    class_id: int,
    wait: float,
    loadout_tools: Sequence[int] = (),
) -> None:
    """Run the existing UI-equivalent join flow against the launched client."""
    command = [
            sys.executable,
            str(SCRIPTS_DIR / "auto_join.py"),
            "--server",
            server,
            "--team",
            str(team),
            "--class-id",
            str(class_id),
            "--console-port",
            str(console_port),
            "--wait",
            str(wait),
        ]
    for tool_id in loadout_tools:
        command.extend(("--loadout-tool", str(int(tool_id))))
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=wait + 240.0,
    )
    print(completed.stdout, end="")
    if completed.returncode:
        raise RuntimeError(f"auto_join failed with exit code {completed.returncode}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="127.0.0.1:27015")
    parser.add_argument("--console-port", type=int, default=32896)
    parser.add_argument("--tracer-port", type=int, default=32895)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--duration-scale", type=float, default=1.0)
    parser.add_argument("--segments", default="")
    parser.add_argument("--artifact-dir", type=Path, default=ROOT / "logs" / "movement")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--client-dir", type=Path, default=DEFAULT_CLIENT_DIR)
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--team", type=int, default=2)
    parser.add_argument("--class-id", type=int, default=0)
    parser.add_argument("--no-auto-join", action="store_true")
    parser.add_argument("--keep-client", action="store_true")
    parser.add_argument(
        "--minimized-client",
        action="store_true",
        help=(
            "launch minimized (diagnostic only; retail throttles background "
            "windows and the result does not represent foreground movement)"
        ),
    )
    parser.add_argument("--client-frame-capture", action="store_true")
    parser.add_argument("--max-loop-lag", type=int, default=8)
    parser.add_argument("--max-stalls", type=int, default=2)
    return parser.parse_args(argv)


def _selected_segments(args: argparse.Namespace) -> tuple[StressSegment, ...]:
    selected = set(filter(None, (part.strip() for part in args.segments.split(","))))
    unknown = selected.difference(segment.name for segment in DEFAULT_SEGMENTS)
    if unknown:
        raise ValueError(f"unknown movement segments: {sorted(unknown)}")
    segments = tuple(
        segment
        for segment in DEFAULT_SEGMENTS
        if (
            segment.name in selected
            if selected
            else segment.include_by_default
        )
    )
    required_classes = {
        int(segment.required_class_id)
        for segment in segments
        if segment.required_class_id is not None
    }
    if len(required_classes) > 1:
        raise ValueError(
            "selected movement segments require incompatible classes: "
            f"{sorted(required_classes)}"
        )
    if required_classes and int(args.class_id) not in required_classes:
        required = next(iter(required_classes))
        raise ValueError(
            f"selected segment requires --class-id {required}, "
            f"got {args.class_id}"
        )
    return tuple(
        replace(segment, duration=segment.duration * args.duration_scale)
        for segment in segments
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.interval <= 0 or args.repeats <= 0 or args.duration_scale <= 0:
        raise ValueError("interval, repeats, and duration-scale must be positive")
    segments = _selected_segments(args)
    thresholds = StressThresholds(
        max_abs_loop_lag=args.max_loop_lag,
        max_stalls=args.max_stalls,
    )
    process = None
    try:
        if args.launch:
            spec = ClientSpec(
                index=1,
                client_dir=args.client_dir,
                python_path=args.client_dir / "python" / "python.exe",
                connect_target=args.server,
                console_port=args.console_port,
                tracer_port=args.tracer_port,
                capture_dir=args.artifact_dir / "client-1",
                capture_enabled=args.client_frame_capture,
                stack_sampler_enabled=False,
                minimized=args.minimized_client,
            )
            process = launch_client(spec)
            if not args.no_auto_join:
                _auto_join(
                    args.console_port,
                    args.server,
                    args.team,
                    args.class_id,
                    args.wait,
                    tuple(dict.fromkeys([
                            int(segment.tool_id)
                            for segment in segments
                            if segment.tool_id is not None
                            and int(segment.tool_id) != 5
                        ] + [
                            int(tool_id)
                            for segment in segments
                            for tool_id in segment.required_loadout_tools
                        ])),
                )

        console = GameConsole(port=args.console_port, timeout=15.0)
        console.connect(wait_seconds=args.wait)
        try:
            report = run_scenario(
                console,
                segments=segments,
                repeats=args.repeats,
                interval=args.interval,
                thresholds=thresholds,
            )
        finally:
            console.close()
        path = write_report(report, args.artifact_dir)
        analysis = report["analysis"]
        print(f"artifact: {path}")
        print(json.dumps(analysis, indent=2, sort_keys=True))
        return 0 if analysis["passed"] else 1  # type: ignore[index]
    finally:
        if process is not None and not args.keep_client:
            stop_client(process)


if __name__ == "__main__":
    raise SystemExit(main())
