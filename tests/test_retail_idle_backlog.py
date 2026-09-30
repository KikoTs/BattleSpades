"""Recover received idle frames without speeding up native or active players."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_retail_jump_launch import captured_player, DT


IDLE = (False,) * 8
# Captured stock Soldier packet: pickup/display/palette capabilities are true
# even when no action button is pressed.
RETAIL_IDLE_ACTIONS = (False, False, False, True, True, False, False, False, True)


def queued_player(actions=RETAIL_IDLE_ACTIONS):
    player = captured_player()
    server = player.connection.server
    server.loop_count = 100
    server.world_manager.topology_version = 1
    player.parachute_id = 72  # Equipping a dormant canopy must not block recovery.
    player.last_applied_input_loop = 100
    player._pending_packet_flags = IDLE
    player._applied_action_flags = actions
    for loop in range(101, 111):
        player.record_input_frame(loop, IDLE, (1., 0., 0.), action_flags=actions,
                                  received_server_tick=server.loop_count)
    return player, server


@pytest.mark.parametrize('actions', [IDLE, RETAIL_IDLE_ACTIONS])
def test_received_idle_backlog_drains_without_dropping_labels_or_moving(actions):
    player, server = queued_player(actions)
    before = player.position

    async def simulate():
        for _ in range(4):
            server.loop_count += 1
            await player.simulate_tick(DT)
    asyncio.run(simulate())
    assert player.last_applied_input_loop == 108
    assert player.input_frames_catchup == 4
    assert player.input_frames_backlog_dropped == 0
    assert player.position == pytest.approx(before, abs=.01)
    assert len(player.input_history) == 2
    assert not player._input_backlog_policy(server)


@pytest.mark.parametrize('index', [0, 1, 2, 5, 6, 7, 9])
def test_idle_catchup_rejects_active_and_unknown_action_bits(index):
    actions = list(RETAIL_IDLE_ACTIONS) + [False]
    actions[index] = True
    player, server = queued_player(tuple(actions))
    assert not player._input_backlog_policy(server)


@pytest.mark.parametrize('hazard', [
    'native', 'unknown', 'bot', 'walking', 'coasting', 'water', 'airborne',
    'pending_jump', 'next_jump', 'action', 'terrain', 'mutation', 'impulse',
    'near_player', 'jetpack', 'parachute',
    'latched_jump', 'latched_action',
])
def test_idle_catchup_rejects_active_or_uncertain_frames(hazard):
    player, server = queued_player()
    if hazard == 'native':
        player.connection.flight_profile_capable = True
    elif hazard == 'unknown':
        del player.connection.flight_profile_capable
    elif hazard == 'bot':
        player.is_bot = True
    elif hazard == 'walking':
        player.input.up = True
    elif hazard == 'coasting':
        player.vx = .1
    elif hazard == 'water':
        player.wade = True
    elif hazard == 'airborne':
        player.grounded = False
    elif hazard == 'pending_jump':
        player._pending_packet_flags = (False,) * 4 + (True,) + (False,) * 3
    elif hazard == 'next_jump':
        player.input_history[102] = replace(player.input_history[102],
            movement_flags=(False,) * 4 + (True,) + (False,) * 3)
    elif hazard == 'action':
        player.input_history[101] = replace(player.input_history[101],
            action_flags=(True,) + (False,) * 7)
    elif hazard == 'terrain':
        server.world_manager.topology_version += 1
    elif hazard == 'mutation':
        server.world_mutations = SimpleNamespace(pending_count=1)
    elif hazard == 'impulse':
        player._pending_velocity_impulses.append((101, (1., 0., 0.)))
    elif hazard == 'near_player':
        player._build_player_collision_positions = lambda: [(player.x + 1, player.y, player.z, 2.25)]
    elif hazard == 'jetpack':
        player.jetpack_id = 66
    elif hazard == 'parachute':
        player.parachute_active = True
    elif hazard == 'latched_jump':
        player._press_latch_flags = (False,) * 4 + (True,) + (False,) * 3
    elif hazard == 'latched_action':
        player._press_latch_actions = (True,) + (False,) * 8
    assert not player._input_backlog_policy(server)
