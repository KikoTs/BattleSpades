"""Authoritative jump physics must agree with the retail fix and native client."""
import asyncio
import json
from pathlib import Path

import pytest

from tests.test_reversed_world_update import advance_player, make_player


DT = 1.0 / 60.0
CAPTURE = json.loads((Path(__file__).parent / 'fixtures' /
                      'retail_native_jump_arc.json').read_text())


def captured_player(capable=False, base_loop=100):
    player, connection = make_player()
    if capable is not None:
        connection.flight_profile_capable = capable
    terrain = connection.server.world_manager.map
    for x in range(96, 105):
        for y in range(96, 105):
            terrain.set_point(x, y, CAPTURE['ground_z'], True, 0x7F00FF00)
    player.set_position(100.5, 100.5, CAPTURE['frames'][0]['before_z'])
    player._world_object.set_velocity(0.0, 0.0, 0.0)
    player._sync_cached_vectors()
    assert not player._world_object.airborne
    for stamp in range(base_loop - 8, base_loop, 2):
        player.record_owner_anchor(stamp, player.position)
    return player


@pytest.mark.parametrize('capable', [False, True])
def test_server_matches_original_native_standing_jump_capture(capable):
    """45 original World PYD frames, including ascent, apex, descent and landing.

    The client package removes Character's extra reset and leaves these native
    results intact. The server must not reintroduce that reset for retail peers.
    """
    player = captured_player(capable, base_loop=CAPTURE['frames'][0]['loop'] + 1)

    async def replay():
        for frame in CAPTURE['frames']:
            player.last_applied_input_loop = frame['loop'] + 1
            player.update_input(*frame['buttons'])
            await player.update(DT)
            assert player.z == pytest.approx(frame['z'], abs=0.002), frame['loop']
            assert (player.vx, player.vy, player.vz) == pytest.approx(
                frame['velocity'], abs=0.0001), frame['loop']
            assert player.airborne == frame['airborne'], frame['loop']
    asyncio.run(replay())


@pytest.mark.parametrize('capable,bot,labelled', [
    (False, False, True), (True, False, True), (False, True, True),
    (None, False, True), (False, False, False),
])
def test_all_clients_keep_native_launch(capable, bot, labelled):
    player = captured_player(capable)
    player.is_bot = bot
    if labelled:
        player.last_applied_input_loop = 100
    before = player.z
    player.input.jump = True
    advance_player(player, DT)
    assert player.z == pytest.approx(before - 0.2178802490234375, abs=0.00001)
    assert player.vz == pytest.approx(-0.40852463245391846)
    assert player.airborne


def test_held_airborne_jump_still_moves_and_keeps_launch_velocity():
    player = captured_player()
    player.last_applied_input_loop = 100
    player.input.jump = True
    before = player.z
    advance_player(player, DT)
    assert player.z == pytest.approx(before - 0.2178802490234375, abs=0.00001)
    assert player.vz == pytest.approx(-0.40852463245391846)
    assert player.airborne
    advance_player(player, DT)
    assert player.z < before - 0.4
    assert not player._world_object.jump_this_frame


@pytest.mark.parametrize('pack', [66, 67, 68, 69])
@pytest.mark.parametrize('native', [False, True])
def test_ordinary_moving_jump_with_inactive_pack_tracks_retail_replay(pack, native):
    player = captured_player(capable=native)
    player.jetpack_id = pack
    player.last_applied_input_loop = 100
    player.input.jump = True
    player.input.up = True
    advance_player(player, DT)
    assert player._world_object.jump_this_frame
    assert player.airborne
    assert not player.jetpack_active
    assert not player._jetpack_physics_active
    assert player.last_retail_jump_loop == (None if native else 100)


@pytest.mark.parametrize('pack', [66, 67, 68])
def test_starting_thrust_retires_the_earlier_ordinary_jump(pack):
    player = captured_player()
    player.jetpack_id = pack
    player.input.up = player.input.jump = True
    player.last_applied_input_loop = 100
    advance_player(player, DT)
    assert player.last_retail_jump_loop == 100
    for label in range(101, 160):
        player.last_applied_input_loop = label
        advance_player(player, DT)
        if player.jetpack_active:
            break
    assert player.jetpack_active
    assert player.last_retail_jump_loop is None
    player.input.jump = False
    advance_player(player, DT)
    assert not player.jetpack_active
    assert player.last_retail_jump_loop is None


def test_retail_launch_never_uses_a_stale_or_future_owner_anchor():
    player = captured_player()
    player.last_applied_input_loop = 100
    player._owner_anchor_history.clear()
    player.record_owner_anchor(99, (20.0, 30.0, 40.0))
    player.record_owner_anchor(200, (200.0, 210.0, 220.0))
    before = (player.x, player.y, player.z)
    player.input.jump = True
    advance_player(player, DT)
    assert (player.x, player.y) == before[:2]
    assert player.z == pytest.approx(before[2] - 0.2178802490234375)
    assert player.last_applied_input_loop == 100
    assert player.airborne


def test_blocked_climb_jump_keeps_collision_resolution():
    retail = captured_player()
    native = captured_player(True)
    for player in (retail, native):
        terrain = player.connection.server.world_manager.map
        for x in range(101, 105):
            for y in range(98, 104):
                terrain.set_point(x, y, CAPTURE['ground_z'] - 1, True, 0x7F00FF00)
        player.set_position(100.5, 100.5, CAPTURE['ground_z'] - 2.3)
        player._world_object.set_velocity(0.3, 0.0, 0.0)
        player._sync_cached_vectors()
        player.last_applied_input_loop = 100
        player.input.jump = True
        advance_player(player, DT)
        assert player._world_object.jump_this_frame
        assert not player.airborne
        assert player.vz == 0.0
    assert (retail.x, retail.y, retail.z) == (native.x, native.y, native.z)


@pytest.mark.parametrize('case', ['walking', 'coasting', 'crouch', 'recent_landing',
                                 'no_owner_rows', 'high_ping', 'stale_owner_rows'])
def test_uncertain_launch_origin_keeps_native_motion(case):
    retail = captured_player()
    native = captured_player(True)
    for player in (retail, native):
        player.last_applied_input_loop = 100
        player.input.jump = True
        if case == 'walking':
            player.input.up = True
        elif case == 'coasting':
            player._world_object.set_velocity(0.2, 0.0, 0.0)
        elif case == 'crouch':
            player.input.crouch = True
        elif case == 'recent_landing':
            player._owner_anchor_history.clear()
            player.record_owner_anchor(94, (player.x, player.y, player.z - 0.3),
                                       (0.0, 0.0, 0.2))
            player.record_owner_anchor(98, player.position)
        elif case == 'no_owner_rows':
            player._owner_anchor_history.clear()
        elif case == 'stale_owner_rows':
            player.last_applied_input_loop = 200
        elif case == 'high_ping':
            from types import SimpleNamespace
            player.connection.peer = SimpleNamespace(roundTripTime=500)
        advance_player(player, DT)
    assert retail.position == native.position
    assert (retail.vx, retail.vy, retail.vz) == (native.vx, native.vy, native.vz)
