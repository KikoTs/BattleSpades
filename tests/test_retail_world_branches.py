"""Server movement regressions derived from original world.pyd instructions.

Oracle: SHA256 ae45ec007e312c8d650620bc2779169f7b7461c74192b7a7480342c21237c1a0.
Address-tagged evidence: tmp/retail-server-reversal-20260920/world-*.c.
These expected values follow original branches/stores, not native-client logs.
"""

import struct

import pytest

from aoslib import world
from aoslib.vxl import VXL
from shared import constants as C


DT = 1.0 / 60.0


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def make_player(*, airborne=True):
    simulation = world.World(VXL(-1, b"", 0, 2))
    player = world.Player(simulation)
    player.set_position(100.5, 100.5, 100.0)
    if airborne:
        player.update(DT, ())
        assert player.airborne
    player.set_velocity(0.0, 0.0, 0.0)
    return simulation, player


@pytest.mark.parametrize("pack,thrust", [(66, 0.045), (67, 0.0125), (68, 0.020), (69, 0.025)])
@pytest.mark.parametrize("gravity", [1.0, 26.0 / 64.0])
@pytest.mark.parametrize("passive", [False, True])
def test_flight_recurrence_uses_original_float_stores(pack, thrust, gravity, passive):
    """0x10012C4E..CB1, 0x10012EE6/0x10012F06, 0x10012F20."""
    simulation, player = make_player()
    simulation.set_gravity(gravity)
    player.jetpack = pack
    player.jetpack_active = True
    player.jetpack_passive = passive
    player.jump = True
    expected = 0.0
    step = f32(DT)
    divisor = f32(1.0 + step)
    for _ in range(120):
        expected = f32(expected - (gravity + 1.0) * f32(thrust) * 0.5)
        expected = f32(expected + step * gravity * (0.75 if passive else 1.0))
        expected = f32(expected / divisor)
        player.update(DT, ())
        assert player.velocity.z == expected
        assert player.jump  # 0x10012D36 skips clearing an equipped pack.


@pytest.mark.parametrize("pack,parachute,retained", [
    (C.NO_JETPACK, False, False), (C.NO_JETPACK, True, True),
    (66, False, True), (67, False, True), (68, False, True), (69, False, True),
])
def test_airborne_jump_request_is_only_cleared_for_plain_infantry(pack, parachute, retained):
    """0x10012D32..0x10012D48; no unconditional one-frame consumption."""
    _, player = make_player()
    player.jetpack = pack
    player.parachute = parachute
    player.jump = True
    player.update(DT, ())
    assert player.jump is retained
    assert not player.jump_this_frame


def test_opposing_movement_buttons_both_apply_in_original_order():
    """Independent up/down/left/right checks at0x10012E02..0x10012E79."""
    _, player = make_player()
    player.set_walk(True, True, True, True)
    player.update(DT, ())
    assert player.velocity.x == 0.0
    assert player.velocity.y == 0.0


@pytest.mark.parametrize("hover,factor", [(False, 0.1), (True, 0.5)])
def test_engineer_air_control_checks_hover_not_wade(hover, factor):
    """0x10012D9B reads+124(hover), not+156(wade)."""
    _, player = make_player()
    player.jetpack = 68
    player.jetpack_active = True
    player.hover = hover
    player.set_class_accel_multiplier(0.7)
    player.set_walk(True, False, False, False)
    player.update(DT, ())
    accel = f32(f32(0.7) * f32(DT))
    accel = f32(accel * f32(factor))
    assert player.velocity.x == f32(accel / f32(1.0 + f32(DT)))


def test_wading_crouch_has_ordinary_gravity_not_an_invented_buoyancy_impulse():
    """Gravity block0x10012EAD..0x10012F06 has no wade/crouch branch."""
    _, player = make_player()
    player.set_position(100.5, 100.5, 237.9)
    player.set_velocity(0.0, 0.0, 0.1)
    for _ in range(240):
        player.update(DT, ())
    assert player.wade
    player.set_crouch(True, (), 0)
    # Wade remains latched in air; place it away from floor collision.
    player.set_position(100.5, 100.5, 100.0)
    player.set_velocity(0.0, 0.0, 0.0)
    player.update(DT, ())
    assert player.velocity.z == f32(f32(DT) / f32(1.0 + f32(DT)))


def test_airborne_uncrouch_expands_downward_without_moving_eye_position():
    """sub_10013270 first checks p.z+2.25; free air keeps p.z unchanged."""
    _, player = make_player()
    start = player.position.z
    player.set_crouch(True, (), 0)
    assert player.position.z == start
    player.set_crouch(False, (), 0)
    assert not player.crouch
    assert player.position.z == start


def test_uncrouch_rejects_overlapping_player_clearance():
    """sub_10013270 checks sub_10012710(resolve=0) in both expansion paths."""
    _, player = make_player()
    player.set_crouch(True, (), 0)
    start = player.position.z
    peers = [(player.position.x, player.position.y, player.position.z)]
    player.set_crouch(False, peers, 1)
    assert player.crouch
    assert player.position.z == f32(f32(start - f32(0.9)) + f32(0.9))


@pytest.mark.parametrize("dead,exploded", [(True, False), (False, True)])
def test_peer_impulses_skip_dead_or_exploded_players(dead, exploded):
    """sub_10012710 entry checks+164(alive) and+168(exploded)."""
    _, player = make_player()
    player.set_dead(dead)
    player.set_exploded(exploded)
    peers = [(100.6, 100.6, player.position.z)]
    assert world._collide_with_players(player, peers, DT) == 0
    assert player.velocity.get() == (0.0, 0.0, 0.0)


def test_axis_aligned_peer_uses_original_positive_x_fallback():
    """0x1001299C..0x100129F2 defaults to(+1,0) if either delta is zero."""
    _, player = make_player()
    peers = [(player.position.x + 0.25, player.position.y, player.position.z)]
    assert world._collide_with_players(player, peers, DT) == 1
    assert player.velocity.x > 0.0
    assert player.velocity.y == 0.0


def test_two_peer_impulses_use_one_pre_loop_candidate():
    """Candidate XYZ is stored before the loop at0x10012791..0x100127BB."""
    _, player = make_player()
    peer = (player.position.x + 0.25, player.position.y + 0.25, player.position.z)
    assert world._collide_with_players(player, [peer], DT) == 1
    one = player.velocity.get()
    player.set_velocity(0.0, 0.0, 0.0)
    assert world._collide_with_players(player, [peer, peer], DT) == 2
    assert player.velocity.x == f32(one[0] + one[0])
    assert player.velocity.y == f32(one[1] + one[1])


def test_vertical_peer_impulse_preserves_original_normalization_rounding():
    """Unchanged x86 sub_10012710 returns this float, not sign(dz)*strength."""
    _, player = make_player()
    player.set_position(100.5, 100.5, 100.0)
    peer = (100.5, 100.5, 98.1996841430664)
    assert world._collide_with_players(player, [peer], DT) == 1
    assert player.velocity.z == 1.686907172203064


def test_non_resolving_peer_query_counts_all_overlaps_without_changing_velocity():
    """0x10012976 skips impulses but still increments/counts every overlap."""
    _, player = make_player()
    peer = (player.position.x + 0.25, player.position.y + 0.25, player.position.z)
    assert world._collide_with_players(player, [peer, peer, peer], 0.0, False) == 3
    assert player.velocity.get() == (0.0, 0.0, 0.0)


def test_world_bottom_guard_uses_original_238_position():
    """sub_10007820 writes238 at0x10007961 rather than240."""
    _, player = make_player()
    player.set_position(100.5, 100.5, 241.0)
    player.update(DT, ())
    assert player.position.z == 238.0


def step_player():
    simulation, player = make_player(airborne=False)
    for x in range(98, 105):
        for y in range(98, 104):
            simulation.map.set_point(x, y, 62, True, 0x7F00FF00)
            if x >= 101:
                simulation.map.set_point(x, y, 61, True, 0x7F00FF00)
    player.set_position(100.5, 100.5, 59.7)
    player.set_velocity(0.3, 0.0, 0.0)
    return simulation, player


def test_step_collision_owns_airborne_even_on_an_ordinary_jump_frame():
    """0x1000247C..0x1000249D clears airborne on the climb return path."""
    _, player = step_player()
    player.jump = True
    player.update(DT, ())
    assert player.jump_this_frame
    assert player.position.x > 100.5
    assert player.position.z < 59.7
    assert not player.airborne
    assert player.velocity.z == 0.0


def test_climb_slowdown_expires_with_original_same_frame_timer_decrement():
    """0x10012E93 multiplies XY;0x100078F3 decrements even on new climb."""
    _, player = step_player()
    player.set_climb_slowdown(0.5)
    player.update(DT, ())
    assert not player.airborne
    assert player.position.x > 100.5
    # Move away from the step so another climb cannot refresh the timer.
    player.set_position(100.5, 100.5, 100.0)
    timer = max(0.0, f32(f32(0.1) - f32(DT)))
    for frame in range(10):
        player.set_velocity(0.3, 0.0, 0.0)
        expected = f32(0.3)
        if timer > 0.0:
            expected = f32(expected * 0.5)
        divisor = f32(1.0 + f32(DT) * (4.0 if frame == 0 else 2.0))
        expected = f32(expected / divisor)
        player.update(DT, ())
        assert player.velocity.x == expected
        timer = max(0.0, f32(timer - f32(DT)))


@pytest.mark.parametrize('velocity', [(0.0, 0.0, 0.0), (0.3, 0.0, 0.1), (0.3, 0.0, -0.3377)])
def test_auto_crouch_clears_prior_fall_before_the_tunnel_landing(velocity):
    """Original complete core returns0/-1; missing old-Z write dealt23/26HP.

    The original binary's auto-crouch write at0x10002758 changes the old-Z
    baseline consumed by0x10007892, even when final position hardly moves.
    """
    simulation, player = make_player(airborne=False)
    player.set_position(100.5, 100.5, 20.0)
    player.set_class_falling_damage_min_distance(3.0)
    player.set_class_falling_damage_max_distance(40.0)
    player.set_class_falling_damage_max_damage(100.0)
    for _ in range(60):
        player.update(DT, ())
    assert player.airborne
    for x in range(97, 105):
        for y in range(97, 105):
            simulation.map.set_point(x, y, 62, True, 0x7F00FF00)
            if x >= 101:
                simulation.map.set_point(x, y, 59, True, 0x7F00FF00)
    player.set_position(100.55000305175781, 100.5, 59.75)
    player.set_velocity(*velocity)
    results = [player.update(DT, ()) for _ in range(30)]
    assert player.crouch
    assert all(result <= 0 for result in results)
