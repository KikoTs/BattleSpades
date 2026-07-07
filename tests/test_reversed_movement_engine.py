import asyncio
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
from aoslib import world as native_world_module
from aoslib.world import Player as NativeWorldPlayer
from aoslib.vxl import VXL

from server.config import ServerConfig
from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1
from server.player import Player
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
    start_z = player.z

    player.update_input(False, False, False, False, False, True, False, False)
    assert math.isclose(player.z, start_z, abs_tol=1e-6)
    assert player._world_object.crouch is False
    asyncio.run(player.update(1.0 / 60.0))
    assert math.isclose(player.z, start_z + 0.9, abs_tol=1e-6)
    assert player._world_object.crouch is True

    player.update_input(False, False, False, False, False, True, False, False)
    assert math.isclose(player.z, start_z + 0.9, abs_tol=1e-6)
    assert player._world_object.crouch is True

    player.update_input(False, False, False, False, False, False, False, False)
    assert math.isclose(player.z, start_z + 0.9, abs_tol=1e-6)
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
    player2.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player2.update(1.0 / 60.0))
    player2.update_input(False, False, False, False, False, False, False, False)
    for _ in range(240):
        asyncio.run(player2.update(1.0 / 60.0))
    assert player2.grounded
    assert abs(player2.vz) < 1e-4


def test_held_jump_uses_native_impulse():
    player = make_player()
    player.update_input(False, False, False, False, True, False, False, False)
    asyncio.run(player.update(1.0 / 60.0))

    impulse = (
        native_world_module.get_debug_movement_overrides()["jump_impulse"]
        * player.movement_profile.jump_multiplier
    )
    # The impulse replaces the gravity step on the jump frame, then the
    # vertical damping divides by (1 + dt) — measured live.
    expected_vz = impulse / (1.0 + 1.0 / 60.0)
    assert math.isclose(player.vz, expected_vz, abs_tol=1e-6)


def test_native_world_player_jumps_from_ground_with_single_trigger():
    world_manager = make_world_manager()
    flatten_patch(world_manager)
    start = make_spawn(world_manager)

    native = NativeWorldPlayer(world_manager.world)
    native.set_position(*start)
    native.set_velocity(0.0, 0.0, 0.0)
    native.set_orientation((1.0, 0.0, 0.500001))
    native.jump = True

    native.update(1.0 / 60.0, [])

    assert native.airborne is True
    assert native.velocity.z < 0.0
    assert native.position.z < start[2]


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
