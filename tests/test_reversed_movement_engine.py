import asyncio
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
from aoslib import world as native_world_module
from aoslib.world import Player as NativeWorldPlayer
from aoslib.vxl import VXL
from shared.glm import Vector3

from server.config import ServerConfig
from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1
from server.player import Player, _JETPACK_PROPERTIES
from server.world_manager import WorldManager


TEST_COLOR = 0x7F00FF00
GROUND_Z = 62


class DummyConnection:
    def __init__(self, server=None, player=None):
        self.server = server
        self.player = player
        self.sent_packets = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent_packets.append(data)


def make_world_manager():
    world_manager = WorldManager(ServerConfig())
    world_manager.map = VXL(-1, b"", 0, 2)
    world_manager.map_name = "test_flat"
    world_manager._refresh_world()
    return world_manager


def flatten_patch(world_manager, cell_x=100, cell_y=100, radius=4):
    if world_manager.map is None:
        return

    for x in range(cell_x - radius, cell_x + radius + 1):
        for y in range(cell_y - radius, cell_y + radius + 1):
            world_manager.map.set_point(x, y, GROUND_Z, True, TEST_COLOR)


def make_spawn(world_manager, cell_x=100, cell_y=100):
    return (
        float(cell_x) + 0.5,
        float(cell_y) + 0.5,
        float(GROUND_Z) - PLAYER_STANDING_POS_ABOVE_GROUND,
    )


def add_step(world_manager, base_x, base_y, top_z, extra_height, width=3):
    for x in range(base_x + 1, base_x + 1 + width):
        for y in range(base_y - 1, base_y + 2):
            for offset in range(1, extra_height + 1):
                world_manager.map.set_point(x, y, top_z - offset, True, TEST_COLOR)


def make_player(world_manager=None, cell_x=100, cell_y=100, flatten=True):
    if world_manager is None:
        world_manager = make_world_manager()
    if flatten:
        flatten_patch(world_manager, cell_x, cell_y)
    server = SimpleNamespace(world_manager=world_manager, players={})
    connection = DummyConnection(server)
    player = Player(0, "KikoTs", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    server.players[player.id] = player
    player.class_id = int(C.CLASS.SOLDIER)
    player.spawn(*make_spawn(world_manager, cell_x, cell_y))
    return player


def advance_player(player, ticks, step=1.0 / 60.0):
    for _ in range(ticks):
        asyncio.run(player.update(step))


def test_direction_mapping_does_not_skew_axes():
    player = make_player()
    start_x, start_y, start_z = player.position
    player.set_orientation_vector(1.0, 0.0, 0.0)

    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)
    assert player.x > start_x
    assert math.isclose(player.y, start_y, abs_tol=0.25)

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(False, False, True, False, False, False, False, False)
    advance_player(player, 30)
    assert math.isclose(player.x, start_x, abs_tol=0.25)
    assert player.y < start_y

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(0.0, 1.0, 0.0)
    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)
    assert math.isclose(player.x, start_x, abs_tol=0.25)
    assert player.y > start_y


def test_player_cannot_walk_through_wall_blocks():
    world_manager = make_world_manager()
    base_x = 100
    base_y = 100
    flatten_patch(world_manager, base_x, base_y)
    ground_top = world_manager.map.get_z(base_x, base_y)
    add_step(world_manager, base_x, base_y, ground_top, extra_height=3, width=1)

    player = make_player(world_manager, base_x, base_y, flatten=False)
    start_x, start_y, _ = player.position
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, False, False, False, False, False)

    advance_player(player, 60)

    assert player.x < start_x + 0.8
    assert math.isclose(player.y, start_y, abs_tol=0.25)


def test_one_block_step_climbs_but_two_block_wall_blocks():
    """Oracle-calibrated: the live engine glides up single-block steps while
    walking (gradual climb, no teleport); two-block walls block."""
    base_x = 100
    base_y = 100

    one_block_world = make_world_manager()
    flatten_patch(one_block_world, base_x, base_y)
    top_z = one_block_world.map.get_z(base_x, base_y)
    add_step(one_block_world, base_x, base_y, top_z, extra_height=1)

    player = make_player(one_block_world, base_x, base_y, flatten=False)
    start_z = player.z
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, False, False, False, False, False)
    # 35 ticks: enough for the faithful gradual glide to carry the player up
    # and onto the step and settle grounded (oracle C_step_full grounds by
    # ~f60), but not so far that it walks off the step's narrow (~3-block) far
    # edge. The old substep climb settled faster, so this used to read 45.
    advance_player(player, 35)
    assert player.x > base_x + 1.5      # walked onto the step
    assert player.z < start_z - 0.5     # actually rose onto the step
    assert player.grounded

    two_block_world = make_world_manager()
    flatten_patch(two_block_world, base_x, base_y)
    top_z = two_block_world.map.get_z(base_x, base_y)
    add_step(two_block_world, base_x, base_y, top_z, extra_height=2)

    blocker = make_player(two_block_world, base_x, base_y, flatten=False)
    block_start_x = blocker.x
    blocker.set_orientation_vector(1.0, 0.0, 0.0)
    blocker.update_input(True, False, False, False, False, False, False, False)
    advance_player(blocker, 120)
    assert blocker.x < block_start_x + 0.8


def test_class_profile_applies_blocks_and_movement_multipliers():
    soldier = make_player()
    scout = make_player()
    rocketeer = make_player()

    scout.class_id = int(C.CLASS.SCOUT)
    scout.spawn(*scout.position)

    rocketeer.class_id = int(C.CLASS.ROCKETEER)
    rocketeer.spawn(*rocketeer.position)

    assert scout.blocks == C.CLASS_BLOCKS[C.CLASS.SCOUT][0]
    assert rocketeer.blocks == C.CLASS_BLOCKS[C.CLASS.ROCKETEER][0]

    soldier.update_input(True, False, False, False, False, False, False, True)
    scout.update_input(True, False, False, False, False, False, False, True)
    soldier.set_orientation_vector(1.0, 0.0, 0.0)
    scout.set_orientation_vector(1.0, 0.0, 0.0)

    advance_player(soldier, 30)
    advance_player(scout, 30)

    assert scout.x > soldier.x


def test_reference_constant_tables_match_dumped_values():
    assert C.CLASS_BLOCKS[C.CLASS.MINER] == (0, 1000)
    assert C.CLASS_ACCEL_MULTIPLIER[C.CLASS.SOLDIER] == 0.7
    assert C.CLASS_ACCEL_MULTIPLIER[C.CLASS.SCOUT] == 0.7
    assert C.CLASS_ACCEL_MULTIPLIER[C.CLASS.ROCKETEER] == 0.7
    assert math.isclose(C.CLASS_SPRINT_MULTIPLIER[C.CLASS.SCOUT], 1.45, abs_tol=1e-9)


def test_crouch_toggle_shifts_height_once():
    player = make_player()
    # Exact CreatePlayer height takes the stock four-frame float32 contact
    # settle before it reaches the natural grounded anchor.
    advance_player(player, 4)
    start_z = player.z

    player.update_input(False, False, False, False, False, True, False, False)
    assert math.isclose(player.z, start_z, abs_tol=1e-6)
    assert player._world_object.crouch is False
    asyncio.run(player.update(1.0 / 60.0))
    assert math.isclose(player.z, start_z + 0.9, abs_tol=2e-6)
    assert player._world_object.crouch is True

    player.update_input(False, False, False, False, False, True, False, False)
    assert math.isclose(player.z, start_z + 0.9, abs_tol=2e-6)
    assert player._world_object.crouch is True

    player.update_input(False, False, False, False, False, False, False, False)
    assert math.isclose(player.z, start_z + 0.9, abs_tol=2e-6)
    assert player._world_object.crouch is True
    asyncio.run(player.update(1.0 / 60.0))
    assert math.isclose(player.z, start_z, abs_tol=1e-6)
    assert player._world_object.crouch is False


def test_forward_speed_stays_stable_at_extreme_pitch():
    player = make_player()
    start_x, start_y, start_z = player.position

    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)
    level_distance = math.hypot(player.x - start_x, player.y - start_y)

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.set_orientation_vector(0.001, 0.0, -0.9999995)
    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)
    steep_distance = math.hypot(player.x - start_x, player.y - start_y)

    assert abs(level_distance - steep_distance) <= 0.15


def test_vertical_look_uses_last_valid_horizontal_basis():
    player = make_player()
    start_x, start_y, _ = player.position

    player.set_orientation_vector(0.0, 1.0, 0.0)
    player.set_orientation_vector(0.0, 0.0, -1.0)
    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)

    assert player.y > start_y + 0.5
    assert math.isclose(player.x, start_x, abs_tol=0.25)


def test_forward_and_strafe_speed_match_at_extreme_pitch():
    player = make_player()
    start_x, start_y, start_z = player.position

    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.set_orientation_vector(0.001, 0.0, -0.9999995)
    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 30)
    forward_distance = math.hypot(player.x - start_x, player.y - start_y)

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.set_orientation_vector(0.001, 0.0, -0.9999995)
    player.update_input(False, False, False, True, False, False, False, False)
    advance_player(player, 30)
    strafe_distance = math.hypot(player.x - start_x, player.y - start_y)

    assert abs(forward_distance - strafe_distance) <= 0.15


def test_holding_jump_auto_repeats_like_the_client():
    """Measured client behavior: while the jump key is held, the Character
    re-triggers the jump every frame the player is grounded — holding jump
    bunny-hops. The server mirrors that (no edge detection, no queue)."""
    player = make_player()
    player.update_input(False, False, False, False, True, False, False, False)

    jump_launches = 0
    was_grounded = True
    for _ in range(240):
        asyncio.run(player.update(1.0 / 60.0))
        if was_grounded and player.airborne and player.vz < -0.2:
            jump_launches += 1
        was_grounded = player.grounded

    assert jump_launches >= 2  # kept hopping the whole time


def test_held_jump_fires_on_landing_release_does_not():
    """The 'buffered jump' emerges naturally: if the key is still held when
    the player lands, the next tick jumps again. If the key was released
    mid-air, landing stays grounded."""
    player = make_player()
    advance_player(player, 4)

    # Press and hold: first jump launches.
    player.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player.update(1.0 / 60.0))
    assert player.airborne

    # Keep holding through the whole arc: must launch again after landing.
    relaunched = False
    for _ in range(240):
        was_grounded = player.grounded
        asyncio.run(player.update(1.0 / 60.0))
        if was_grounded and player.airborne and player.vz < -0.2:
            relaunched = True
            break
    assert relaunched

    # Now release mid-air: landing must NOT relaunch.
    player2 = make_player()
    advance_player(player2, 4)
    player2.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player2.update(1.0 / 60.0))
    player2.update_input(False, False, False, False, False, False, False, False)
    for _ in range(240):
        asyncio.run(player2.update(1.0 / 60.0))
    assert player2.grounded
    assert abs(player2.vz) < 1e-4


def test_held_jump_uses_native_impulse():
    player = make_player()
    advance_player(player, 4)
    player.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player.update(1.0 / 60.0))

    impulse = (
        native_world_module.get_debug_movement_overrides()["jump_impulse"]
        * player.movement_profile.jump_multiplier
    )
    # Retail assigns the impulse, adds the ordinary gravity step in the same
    # frame, then applies vertical damping.
    dt = 1.0 / 60.0
    expected_vz = (impulse + dt) / (1.0 + dt)
    assert math.isclose(player.vz, expected_vz, abs_tol=1e-6)


def test_authoritative_jump_rejects_a_stale_owner_anchor_teleport():
    """A launch keeps native velocity without restoring a far-away WU row.

    Retail ``Character.update_alive`` first runs native physics, then its
    ``jump_this_frame`` branch calls ``world_object.set_position`` with all
    three coordinates from ``network_position`` (``character.pyd``
    ``0x100808E5`` -> ``0x100815AB``). A buffered re-jump can see a row from
    the prior airborne cadence and move more than one voxel in one frame. The
    maintained client and server retain the pre-physics position for that
    stale-row case while keeping the native launch velocity.
    """
    player = make_player()
    advance_player(player, 4)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    before = player.position
    advertised_anchor = (
        before[0] - 0.25,
        before[1] + 0.125,
        before[2],
    )
    player.last_advertised_owner_position = advertised_anchor
    player.update_input(
        True, False, False, False, True, False, False, True
    )

    asyncio.run(player.update(1.0 / 60.0))

    assert player.position == pytest.approx(before, abs=1e-6)
    assert player.vx > 0.0
    assert player.airborne is True
    assert player.vz == pytest.approx(-0.4085246, abs=1e-6)

    player.update_input(
        True, False, False, False, False, False, False, True
    )
    asyncio.run(player.update(1.0 / 60.0))
    assert player.x > before[0]
    assert player.z < before[2]


def test_authoritative_jump_keeps_small_retail_anchor_correction():
    """Sub-quarter-block owner anchors retain the stock reconciliation path."""
    player = make_player()
    advance_player(player, 4)
    before = player.position
    advertised_anchor = (before[0] - 0.10, before[1], before[2])
    player.last_advertised_owner_position = advertised_anchor
    player.update_input(
        True, False, False, False, True, False, False, True
    )

    asyncio.run(player.update(1.0 / 60.0))

    assert player.position == pytest.approx(advertised_anchor, abs=1e-6)
    assert player.airborne is True


def test_held_landing_relaunch_does_not_reuse_the_press_anchor():
    """One continuous SPACE hold consumes its cached anchor only once."""
    player = make_player()
    advance_player(player, 4)
    player.update_input(
        True, False, False, False, True, False, False, True
    )
    asyncio.run(player.update(1.0 / 60.0))
    assert player.airborne is True

    for _ in range(240):
        asyncio.run(player.update(1.0 / 60.0))
        if player.grounded:
            break
    else:
        pytest.fail("held jump never returned to the ground")

    before_relaunch = player.position
    stale_anchor = (
        before_relaunch[0] - 0.10,
        before_relaunch[1],
        before_relaunch[2],
    )
    player.last_advertised_owner_position = stale_anchor

    asyncio.run(player.update(1.0 / 60.0))

    assert player.last_trigger_jump is True
    assert player.airborne is True
    assert player.position != pytest.approx(stale_anchor, abs=1e-6)


def test_authoritative_jump_uses_owner_anchor_strictly_before_input_source():
    """A row stamped with the launch loop was queued too late for that launch.

    Live retail proof (2026-07-12): frame 3614 launched from the already cached
    row 3612 at X=160.500000.  The server then queued row 3614 at X=160.516403;
    its one-frame button latch applied that same frame's jump on the following
    packet and incorrectly used the newer row.  The 0.016403 phase error later
    became a 0.245728 correction at a voxel boundary.  Selection must therefore
    be strict ``stamp < input_source_loop``, not ``<=`` and not latest queued.
    """
    player = make_player()
    advance_player(player, 4)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    old_anchor = player.position
    equal_stamp_anchor = (
        old_anchor[0] + 0.016403,
        old_anchor[1],
        old_anchor[2],
    )
    player.record_owner_anchor(3612, old_anchor)
    player.record_owner_anchor(3614, equal_stamp_anchor)
    player._applied_input_source_loop = 3614
    # Keep the compatibility field at the value which caused the live race.
    player.last_advertised_owner_position = equal_stamp_anchor
    player.update_input(
        True, False, False, False, True, False, False, True
    )

    asyncio.run(player.update(1.0 / 60.0))

    assert player.position == pytest.approx(old_anchor, abs=1e-6)
    assert player.vx > 0.0
    assert player.airborne is True


def test_owner_anchor_history_is_bounded_and_keeps_every_duplicate_row():
    """Local retail rows are force-applied, including duplicate stamps."""
    player = make_player()
    for stamp in range(300):
        player.record_owner_anchor(
            stamp,
            (float(stamp), 2.0, 3.0),
            (0.1, 0.2, 0.3),
            queued_server_tick=stamp + 1000,
        )

    assert len(player._owner_anchor_history) == 128
    assert player._owner_anchor_history[0].stamp == 172
    player.record_owner_anchor(
        299,
        (999.0, 2.0, 3.0),
        (9.0, 8.0, 7.0),
        queued_server_tick=9999,
    )
    assert len(player._owner_anchor_history) == 128
    anchor = player._owner_anchor_history[-1]
    assert anchor.stamp == 299
    assert anchor.position == (999.0, 2.0, 3.0)
    assert anchor.velocity == (9.0, 8.0, 7.0)
    assert anchor.queued_server_tick == 9999


def test_owner_anchor_selection_uses_send_receive_causality():
    """A duplicate queued after ClientData arrived cannot be its launch cache.

    The owner path passes ``force_update=True`` to Character, so two rows with
    the same pong stamp both replace ``network_position``.  Server tick labels
    cannot order a send and receive that occur inside one tick; the gameplay-
    thread event sequence can.
    """
    player = make_player()
    player.record_owner_anchor(
        500,
        (10.0, 20.0, 30.0),
        (1.0, 2.0, 3.0),
        queued_server_tick=80,
        queued_owner_sequence=10,
    )
    player.record_owner_anchor(
        500,
        (11.0, 20.0, 30.0),
        (4.0, 5.0, 6.0),
        queued_server_tick=80,
        queued_owner_sequence=12,
    )

    before_second_send = player._owner_anchor_entry_before_input(
        502,
        source_received_server_tick=80,
        source_received_owner_sequence=11,
    )
    after_second_send = player._owner_anchor_entry_before_input(
        502,
        source_received_server_tick=80,
        source_received_owner_sequence=13,
    )

    assert before_second_send is not None
    assert before_second_send[1].position == (10.0, 20.0, 30.0)
    assert after_second_send is not None
    assert after_second_send[1].position == (11.0, 20.0, 30.0)


def test_native_world_player_jumps_from_ground_with_single_trigger():
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    start = make_spawn(world_manager)

    native = NativeWorldPlayer(world_manager.world)
    native.set_position(*start)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.500001))
    for _ in range(4):
        native.update(1.0 / 60.0, [])
    native.jump = True

    native.update(1.0 / 60.0, [])

    assert native.airborne is True
    assert native.velocity.z < 0.0
    assert native.position.z < start[2]


def test_jump_launch_uses_the_retail_stale_ground_horizontal_branch():
    """The launch frame becomes airborne only after horizontal integration.

    Retail ``world.pyd`` assigns the jump impulse first but leaves its
    ``airborne`` field unchanged until boxclipmove.  Consequently a grounded
    sprint-jump gets one final full sprint acceleration step and the ground
    friction divisor.  The stock-client block->sprint->jump capture measured
    ``vx 0.030762 -> 0.059601`` across this exact transition.
    """
    player = make_player()
    advance_player(player, 4)
    native = player._world_object
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.0))
    native.set_class_sprint_multiplier(1.96875)
    for _ in range(4):
        native.update(1.0 / 60.0, [])

    native.set_walk(True, False, False, False)
    native.sprint = True
    native.update(1.0 / 60.0, [])
    assert native.airborne is False
    assert native.velocity.x == pytest.approx(0.03076172, abs=1e-6)

    native.jump = True
    native.update(1.0 / 60.0, [])

    assert native.airborne is True
    assert native.velocity.x == pytest.approx(0.05960083, abs=1e-6)


def test_shallow_landing_preserves_retail_horizontal_velocity():
    """Ordinary jump landings do not apply a second horizontal slowdown.

    The air-friction step still divides ``vx`` by ``1 + 2*dt``.  Retail's
    landing-result threshold at 0.24 only controls the return/damage path;
    horizontal damping is reserved for a severe landing above 0.8.  Applying
    the old unconditional ``* 0.7`` here caused the exact run/jump rollback
    signature captured from the stock client.
    """
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 50.0)
    native.update(1.0 / 60.0, [])
    assert native.airborne is True
    native.set_position(100.5, 100.5, 59.70)
    native.set_velocity(0.4, 0.0, 0.45)
    native.set_orientation((1.0, 0.0, 0.0))

    result = native.update(1.0 / 60.0, [])

    expected_vx = 0.4 / (1.0 + (2.0 / 60.0))
    assert result == -1
    assert native.airborne is False
    assert native.velocity.x == pytest.approx(expected_vx, abs=1e-6)


def test_severe_landing_uses_retail_half_horizontal_velocity():
    """Only a landing above 0.8/gravity halves XY speed.

    Teleporting near the floor does not invent falling distance, so the stock
    mover returns the no-damage landing marker even with a high saved velocity.
    """
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 50.0)
    native.update(1.0 / 60.0, [])
    assert native.airborne is True
    native.set_position(100.5, 100.5, 59.70)
    native.set_velocity(0.4, 0.0, 0.90)
    native.set_orientation((1.0, 0.0, 0.0))

    result = native.update(1.0 / 60.0, [])

    expected_vx = (0.4 / (1.0 + (2.0 / 60.0))) * 0.5
    assert result == -1
    assert native.airborne is False
    assert native.velocity.x == pytest.approx(expected_vx, abs=1e-6)


def test_natural_long_fall_uses_accumulated_distance_for_damage():
    """Fall damage comes from travelled Z distance, not impact velocity."""
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 20.0)
    native.set_velocity(0.0, 0.0, 0.0)

    result = 0
    for _ in range(600):
        result = native.update(1.0 / 60.0, [])
        if result:
            break

    assert native.airborne is False
    assert result == 98


def test_native_world_ignores_repeated_jump_triggers_while_airborne():
    """Retail accepts jump only while grounded, even if SPACE stays held."""
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    start = make_spawn(world_manager)

    native = NativeWorldPlayer(world_manager.world)
    native.set_position(*start)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.0))
    for _ in range(4):
        native.update(1.0 / 60.0, [])

    velocities = []
    for _ in range(8):
        native.jump = True
        native.update(1.0 / 60.0, [])
        velocities.append(native.velocity.z)
        assert native.airborne is True

    # Gravity should move vz steadily back toward zero. Reassigning the jump
    # impulse in mid-air leaves every value at the first-frame launch speed.
    assert all(
        current > previous
        for previous, current in zip(velocities, velocities[1:])
    )


def test_native_world_preserves_the_concrete_jetpack_enum():
    """world.pyd stores the selected pack, not a boolean equipped flag."""
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)

    native.jetpack = int(C.JETPACK_ENGINEER)

    assert native.jetpack == int(C.JETPACK_ENGINEER)


def test_native_world_engineer_jetpack_applies_stock_sustained_thrust():
    """An active Engineer pack subtracts 0.020 vertical velocity per frame.

    Retail applies the per-pack thrust before ordinary gravity and vertical
    damping.  The former boolean-only port skipped this branch entirely, so an
    apparently active pack merely fell instead of climbing.
    """
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.0))

    # One free-fall frame establishes the retail airborne state without
    # coupling this regression to the ordinary jump impulse.
    native.update(1.0 / 60.0, [])
    native.set_velocity(0.0, 0.0, 0.0)
    native.jetpack = int(C.JETPACK_ENGINEER)
    native.jetpack_active = True
    native.jetpack_passive = False
    dt = 1.0 / 60.0
    expected = (
        -0.00327868736,
        -0.00650362577,
        -0.00967569742,
        -0.01279576588,
        -0.01586468518,
        -0.01888329536,
    )
    observed = []
    for _ in expected:
        # world.pyd consumes this request every update; Character rewrites the
        # held SPACE state on the following frame.
        native.jump = True
        native.update(dt, [])
        observed.append(native.velocity.z)

    assert observed == pytest.approx(expected, abs=1e-6)


def test_native_world_glide_jetpack_has_limited_vertical_thrust():
    """Jetpack2 is a long-range glider, not a renamed Engineer jetpack.

    The retail switch at world.pyd 0x10012C47 uses 0.0125 for pack 67.  At
    60 Hz that is slightly weaker than gravity, so it slows descent while the
    Engineer's 0.020 branch climbs.  This is the defining low-altitude glide.
    """
    world_manager = make_world_manager()
    dt = 1.0 / 60.0

    def one_airborne_frame(pack_id):
        native = NativeWorldPlayer(world_manager.world)
        native.set_position(100.5, 100.5, 100.0)
        native.set_velocity(0.0, 0.0, 0.0)
        native.update(dt, [])
        native.set_velocity(0.0, 0.0, 0.0)
        native.jetpack = int(pack_id)
        native.jetpack_active = True
        native.jump = True
        native.update(dt, [])
        return native.velocity.z

    glide_vz = one_airborne_frame(C.JETPACK2)
    engineer_vz = one_airborne_frame(C.JETPACK_ENGINEER)
    normal_vz = one_airborne_frame(C.JETPACK_NORMAL)

    # Positive Z falls in AoS coordinates. The glider descends gently while
    # the two true thrust packs gain altitude at different rates.
    assert glide_vz > 0.0
    assert engineer_vz < 0.0
    assert normal_vz < engineer_vz


def test_glide_jetpack_trades_height_for_long_fuel_endurance():
    usable_fuel = 100.0 - 10.0
    glide_seconds = usable_fuel / float(_JETPACK_PROPERTIES[C.JETPACK2][4])
    engineer_seconds = usable_fuel / float(
        _JETPACK_PROPERTIES[C.JETPACK_ENGINEER][4]
    )
    normal_seconds = usable_fuel / float(
        _JETPACK_PROPERTIES[C.JETPACK_NORMAL][4]
    )

    assert glide_seconds == pytest.approx(90.0 / 17.0)
    assert glide_seconds > engineer_seconds
    assert glide_seconds > normal_seconds * 4.0


def test_native_world_grounded_active_engineer_uses_thrust_not_jump_impulse():
    """Active pack thrust wins before the grounded ordinary-jump branch.

    Retail ``world.pyd`` checks ``jetpack_active`` at 0x10012C29 before it
    reads the airborne field.  A one-frame contact disagreement at a ledge
    must therefore produce Engineer's gradual thrust, not a full -0.36 jump.
    """
    player = make_player()
    advance_player(player, 4)
    native = player._world_object
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.0))
    assert native.airborne is False
    native.jetpack = int(C.JETPACK_ENGINEER)
    native.jetpack_active = True
    native.jetpack_passive = False
    native.jump = True

    dt = 1.0 / 60.0
    native.update(dt, [])

    thrust = ((native_world_module._GLOBAL_GRAVITY + 1.0) * 0.020) * 0.5
    expected_vz = (-thrust + dt * native_world_module._GLOBAL_GRAVITY) / (1.0 + dt)
    assert native.velocity.z == pytest.approx(expected_vz, abs=1e-6)
    assert native.velocity.z > -0.05
    assert native.jump_this_frame is True
    # jump_this_frame itself does not force this flag; the stock upward mover
    # owns the transition and leaves the unobstructed flat-floor case airborne.
    assert native.airborne is True


def test_native_world_active_jetpack_uses_stock_horizontal_drag():
    """Active flight divides horizontal velocity by ``1 + dt``.

    Retail ``Player.update`` keeps the vertical damping divisor for airborne
    active/passive jetpacks (world.pyd 0x10012F25-0x10012F7C).  Treating an
    active Engineer pack as wading instead applies class water friction
    (``1 + 8*dt``), so every owner snapshot pulls forward flight backward.
    """
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)
    native.set_velocity(0.0, 0.0, 0.0)

    dt = 1.0 / 60.0
    native.update(dt, [])
    native.set_velocity(0.1, 0.0, 0.0)
    native.jetpack = int(C.JETPACK_ENGINEER)
    native.jetpack_active = True
    native.jetpack_passive = False

    native.update(dt, [])

    assert native.velocity.x == pytest.approx(0.1 / (1.0 + dt), abs=1e-6)


def test_native_world_passive_jetpack_uses_stock_horizontal_drag():
    """Passive flight shares the active pack's ``1 + dt`` drag branch."""
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)

    dt = 1.0 / 60.0
    native.update(dt, [])
    native.set_velocity(0.1, 0.0, 0.0)
    native.jetpack = int(C.JETPACK_ENGINEER)
    native.jetpack_active = False
    native.jetpack_passive = True

    native.update(dt, [])

    assert native.velocity.x == pytest.approx(0.1 / (1.0 + dt), abs=1e-6)


def test_native_world_hover_skips_gravity_instead_of_scaling_it():
    """Hover (+124) skips gravity; passive jetpack (+180) owns 0.75 gravity."""
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)

    dt = 1.0 / 60.0
    native.update(dt, [])
    native.set_velocity(0.0, 0.0, 0.0)
    native.hover = True

    native.update(dt, [])

    assert native.velocity.z == pytest.approx(0.0, abs=1e-6)


def test_native_world_parachute_keeps_ordinary_air_drag():
    """The chute changes vertical gravity, not horizontal friction."""
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)

    dt = 1.0 / 60.0
    native.update(dt, [])
    native.set_velocity(0.1, 0.0, 0.0)
    native.parachute = int(C.A370)
    native.parachute_active = True

    native.update(dt, [])

    expected = 0.1 / (1.0 + 2.0 * dt)
    assert native.velocity.x == pytest.approx(expected, abs=1e-6)


def test_native_world_jetpack_passive_uses_retail_three_quarter_gravity():
    """The separate passive flag is a 0.75-gravity mode, not active thrust."""
    world_manager = make_world_manager()
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(100.5, 100.5, 100.0)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.0))
    native.update(1.0 / 60.0, [])
    native.set_velocity(0.0, 0.0, 0.0)
    native.jetpack = int(C.JETPACK_ENGINEER)
    native.jetpack_active = True
    native.jetpack_passive = True
    native.jump = True

    dt = 1.0 / 60.0
    native.update(dt, [])

    thrust = ((native_world_module._GLOBAL_GRAVITY + 1.0) * 0.020) * 0.5
    passive_gravity = dt * native_world_module._GLOBAL_GRAVITY * 0.75
    expected_vz = (-thrust + passive_gravity) / (1.0 + dt)
    assert math.isclose(native.velocity.z, expected_vz, abs_tol=1e-6)


def test_server_passes_held_jump_to_an_active_engineer_jetpack():
    """Airborne SPACE remains a thrust input after the ordinary jump edge."""
    player = make_player()
    player.class_id = int(C.CLASS_ENGINEER)
    player.loadout = [int(C.SMG_TOOL), int(C.JETPACK_ENGINEER)]
    player.spawn(*player.position)
    player.input.jump = True
    player.jetpack_active = True

    player._apply_input_state_to_world(trigger_jump=False, collisions=[])

    assert player._world_object.jump is True
    assert player._world_object.jetpack == int(C.JETPACK_ENGINEER)
    assert player._world_object.jetpack_passive is False


def test_grounded_launch_hold_does_not_freeze_engineer_sustained_thrust():
    """The reconciliation-only launch hold applies to one grounded tick.

    Retail keeps SPACE set while a jetpack is equipped, and every later
    airborne frame consumes it as thrust.  Holding the pre-physics Z position
    beyond the launch frame would make Engineer flight hover/stutter even
    though its native velocity and fuel state continued to change.
    """
    player = make_player()
    player.class_id = int(C.CLASS_ENGINEER)
    player.loadout = [int(C.SMG_TOOL), int(C.JETPACK_ENGINEER)]
    player.spawn(*player.position)
    advance_player(player, 4)
    # Model the ordinary grounded owner row that retail caches before SPACE.
    player.last_advertised_owner_position = player.position
    player.input.jump = True

    launch_z = player.z
    asyncio.run(player.update(1.0 / 60.0))
    assert player.airborne is True
    assert player.z == pytest.approx(launch_z, abs=1e-6)

    player.jetpack_active = True
    airborne_z = player.z
    fuel_before = player.jetpack_fuel
    asyncio.run(player.update(1.0 / 60.0))

    assert player.z < airborne_z
    assert player.vz < 0.0
    assert player.jetpack_fuel < fuel_before


def test_move_box_uses_retail_float32_contact_plane_at_exact_rest_height():
    """The retail feet probe rounds 225.99999988 to float32 226.0.

    Keeping this calculation in double precision probes voxel 225 instead,
    misses the floor, and takes the ordinary-airborne branch while the client
    takes its horizontal glide branch.  One frame later reconciliation is
    already visibly divergent.
    """
    world_manager = make_world_manager()
    for x in range(158, 164):
        for y in range(254, 259):
            world_manager.map.set_point(x, y, 226, True, TEST_COLOR)

    position = Vector3(160.5, 256.5, 223.75)
    velocity = Vector3(0.058377, 0.0, -0.315775)
    native_world_module._move_box(
        position,
        velocity,
        1.0 / 60.0,
        world_manager.map,
        False,
        False,
        True,
        True,
        True,
        False,
    )

    assert math.isclose(position.z, 223.716064453125, abs_tol=1e-5)
    assert math.isclose(velocity.z, 0.0, abs_tol=1e-8)


def test_arctic_slope_landing_matches_retail_collision_and_slowdown():
    """Pin the exact ledge frame that caused the block/jump rollback.

    Client and server maps agree at every collision probe around this point.
    The stock 32-bit ``world.pyd`` classifies this frame as a landing, returns
    ``-1``, and applies the severe-fall 0.5 horizontal slowdown.  Treating it
    as a climb keeps nearly double speed and is 0.68 blocks ahead by the next
    owner row.
    """
    world_manager = WorldManager(ServerConfig())
    assert world_manager.load_map("ArcticBase")
    native = NativeWorldPlayer(world_manager.world)
    native.set_position(318.519836, 223.5, 228.957184)
    native.set_velocity(0.490012, 0.0, 0.845539)
    native.set_orientation(Vector3(0.5, 0.0, 0.866025))
    # The isolated retail constructor defaults to sprint=2.0. Production
    # overwrites this from InitialInfo/class data; pinning it here isolates
    # collision/landing semantics from that separate initialization detail.
    native.set_class_sprint_multiplier(2.0)
    native.set_walk(True, False, False, False)
    native.sprint = True

    result = native.update(1.0 / 60.0, [])

    assert result == -1
    assert tuple(native.position) == pytest.approx(
        (318.781494140625, 223.5, 228.41697692871094), abs=1e-5
    )
    assert tuple(native.velocity) == pytest.approx(
        (0.24531811475753784, 0.0, 0.0), abs=1e-6
    )
    assert native.airborne is False


def test_native_world_wade_follows_ground_contact_in_water_zone():
    """Oracle-calibrated wade semantics: the flag only changes on ground
    contact (feet at/below the waterplane => wade), and is held unchanged
    while airborne."""
    world_manager = make_world_manager()
    # Underwater shelf: solid at z=240 grounds the player with feet ~239.99,
    # i.e. below the waterplane at Z_ABOVE_WATERPLANE (238).
    for x in range(98, 104):
        for y in range(98, 104):
            world_manager.map.set_point(x, y, 240, True, TEST_COLOR)

    native = NativeWorldPlayer(world_manager.world)
    native.set_orientation((1.0, 0.0, 0.0))
    native.set_position(100.5, 100.5, 235.0)
    native.set_velocity(0.0, 0.0, 0.0)
    assert native.wade is False
    for _ in range(240):
        native.update(1.0 / 60.0, [])
    assert native.airborne is False
    assert native.wade is True

    # Held while airborne, even far above the water zone.
    native.set_position(100.5, 100.5, 200.0)
    native.set_velocity(0.0, 0.0, 0.0)
    native.update(1.0 / 60.0, [])
    assert native.airborne is True
    assert native.wade is True

    # Grounding on dry land (GROUND_Z terrain is far above the waterplane)
    # clears it.
    flatten_patch(world_manager)
    native.set_position(100.5, 100.5, float(GROUND_Z) - 4.0)
    native.set_velocity(0.0, 0.0, 0.0)
    for _ in range(240):
        native.update(1.0 / 60.0, [])
    assert native.airborne is False
    assert native.wade is False


def test_wading_crouch_uses_crouch_acceleration_not_base_acceleration():
    """Crouch stays the acceleration selector while the player is wading.

    Retail ``world.pyd`` ``0x10012D65`` selects the crouch/sneak multiplier
    for ``crouch && !hover``; the second flag is hover, not wade.  The live
    stock-client capture on the water shelf therefore tends toward
    ``crouch_accel / water_friction``.  Treating that flag as wade instead
    makes the authoritative player run about 40% faster and triggers a soft
    correction every WorldUpdate.
    """
    world_manager = make_world_manager()
    for x in range(98, 104):
        for y in range(98, 104):
            world_manager.map.set_point(x, y, 240, True, TEST_COLOR)

    native = NativeWorldPlayer(world_manager.world)
    native.set_orientation((1.0, 0.0, 0.0))
    native.set_position(100.5, 100.5, 235.0)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_class_accel_multiplier(0.984375)
    native.set_class_crouch_sneak_multiplier(0.703125)
    native.set_class_water_friction(8.0)
    for _ in range(240):
        native.update(1.0 / 60.0, [])
    assert native.airborne is False
    assert native.wade is True

    native.set_velocity(0.0, 0.0, 0.0)
    native.set_crouch(True, [], 0)
    native.set_walk(True, False, False, False)
    native.update(1.0 / 60.0, [])

    dt = 1.0 / 60.0
    expected_vx = (0.703125 * dt) / (1.0 + 8.0 * dt)
    assert native.velocity.x == pytest.approx(expected_vx, abs=1e-7)


def test_soft_correction_ignores_small_drift():
    player = make_player()
    player.last_reported_position = (player.x + 0.1, player.y, player.z)
    player.last_position_update = time.time()
    before_x = player.x

    player._apply_soft_drift_correction()

    assert math.isclose(player.x, before_x, abs_tol=1e-6)


def test_soft_correction_moves_toward_client_without_snapping():
    player = make_player()
    player.last_reported_position = (player.x + 1.0, player.y, player.z)
    player.last_position_update = time.time()
    before_x = player.x

    player._apply_soft_drift_correction()

    assert math.isclose(player.x, before_x + 0.12, abs_tol=1e-6)


def test_soft_correction_skips_vertical_adjustment_while_airborne():
    player = make_player()
    player.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player.update(1.0 / 60.0))
    assert player.airborne

    before_z = player.z
    player.last_reported_position = (player.x, player.y, player.z + 0.5)
    player.last_position_update = time.time()
    player._apply_soft_drift_correction()

    assert math.isclose(player.z, before_z, abs_tol=1e-6)


def test_open_map_turning_keeps_velocity_bounded():
    world_manager = make_world_manager()
    player = make_player(world_manager=world_manager, cell_x=100, cell_y=100)
    start = player.position
    player.class_id = int(C.CLASS.SOLDIER)
    player.spawn(*start)
    player.set_orientation_vector(1.0, 0.0, 0.0)

    step = 1.0 / 60.0
    max_horizontal_speed = 0.0

    sequences = [
        ((True, False, False, False), (False, False, False, False), (1.0, 0.0, 0.0), 30),
        ((True, False, False, False), (False, False, False, True), (0.9238795, 0.3826834, 0.0), 20),
        ((False, False, True, False), (False, True, False, False), (0.9238795, 0.3826834, 0.0), 15),
        ((False, False, False, False), (True, False, False, False), (0.9238795, 0.3826834, 0.0), 20),
        ((False, False, False, False), (False, False, False, False), (0.9238795, 0.3826834, 0.0), 60),
    ]

    for walk, animation, orientation, ticks in sequences:
        player.set_orientation_vector(*orientation)
        player.update_input(
            walk[0],
            walk[1],
            walk[2],
            walk[3],
            animation[0],
            animation[1],
            animation[2],
            animation[3],
        )
        player.update_action_input(False, False)

        for _ in range(ticks):
            asyncio.run(player.update(step))
            max_horizontal_speed = max(max_horizontal_speed, math.hypot(player.vx, player.vy))

    assert player.x > start[0] + 5.0
    assert player.y > start[1] + 0.5
    assert max_horizontal_speed < 0.5
    # The real engine has no hard zero-clamp: velocity decays by /(1+4*dt)
    # per grounded frame (oracle-calibrated), so after 60 idle ticks a small
    # residual remains.
    assert math.hypot(player.vx, player.vy) < 0.02
