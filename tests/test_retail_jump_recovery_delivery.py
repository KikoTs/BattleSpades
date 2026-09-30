"""Retail jump replay spacing, decoded through both delivery modes."""
import pytest

from server.replication import ReplicationService
from tests.test_world_update_delivery import _decoded, _player, _rows_of, _server


def setup(delivery='split', rtt=8, backlog=0):
    retail = _player(0, applied=1000, airborne=True,
                     last_retail_jump_loop=1000, input_history={1000 + i: None
                         for i in range(1, backlog + 1)}, parachute_id=72)
    native = _player(1, applied=1000, airborne=True, last_retail_jump_loop=1000)
    server = _server([retail, native], delivery=delivery)
    server.config.worldupdate_retail_airborne_self_row_interval = 2
    for player, capable in ((retail, False), (native, True)):
        player.connection = server.connections[player.id]
        player.connection.flight_profile_capable = capable
        player.connection.peer.roundTripTime = rtt
    replication = ReplicationService(server)
    build = server.build_world_update_data

    def stamped_snapshot(**kwargs):
        packet = _decoded(build(**kwargs))
        for player_id, row in list(packet.player_updates.items()):
            fields = list(row)
            fields[4] = server.players[player_id].wu_ack_loop
            packet[player_id] = tuple(fields)
        return bytes(packet.generate())

    server.build_world_update_data = stamped_snapshot
    return server, replication, retail, native


def owner_rows(connection):
    return [payload for payload, _ in connection.sent
            if connection.player.id in _rows_of(payload)]


@pytest.mark.parametrize('delivery', ['split', 'sequenced'])
@pytest.mark.parametrize('rtt,backlog', [(8, 0), (80, 3)])
def test_one_jump_gap_then_retail_30hz_native_six_ticks(delivery, rtt, backlog):
    server, replication, retail, native = setup(delivery, rtt, backlog)
    observed = {0: [], 1: []}
    for delta in range(24):
        server.loop_count = 500 + delta
        for player in (retail, native):
            player.last_applied_input_loop = 1000 + delta
            player.connection.sent.clear()
        replication.broadcast_world_updates()
        for player in (retail, native):
            if owner_rows(player.connection):
                observed[player.id].append(delta)
                for payload in owner_rows(player.connection):
                    row = _decoded(payload).player_updates[player.id]
                    assert row[4] == player.last_applied_input_loop
    # The first jump anchor is sent immediately. The next waits past the
    # client's estimated replay window, then resumes normal 2-tick cadence.
    through = 1000 + backlog + (1 if rtt == 8 else 5) + 3
    resume = next(d for d in range(2, 24, 2) if 1000 + d > through)
    assert observed[0] == [0] + list(range(resume, 24, 2))
    assert observed[1] == [0, 6, 12, 18]


@pytest.mark.parametrize('delivery', ['split', 'sequenced'])
def test_observers_and_urgent_canopy_transition_are_not_delayed(delivery):
    server, replication, retail, native = setup(delivery)
    replication.broadcast_world_updates()
    for player in (retail, native):
        player.connection.sent.clear()
        player.last_applied_input_loop += 2
    server.loop_count += 2
    replication.broadcast_world_updates()
    assert not owner_rows(retail.connection)
    assert any(0 in _rows_of(data) for data, _ in native.connection.sent)
    # Canopy opening must bypass and clear a pending jump recovery gap.
    retail.parachute_active = True
    retail.connection.sent.clear()
    server.loop_count += 1
    retail.last_applied_input_loop += 1
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
    assert any(mode == 'reliable' for _, mode in retail.connection.sent)
    assert retail.id not in replication._retail_jump_recovery


def test_synthesized_rows_stay_excluded_and_unchanging_input_times_out():
    server, replication, retail, _ = setup()
    replication.broadcast_world_updates()
    through = replication._retail_jump_recovery[retail.id][1]
    retail.last_applied_input_loop = through + 1
    retail.last_applied_input_synthesized = True
    server.loop_count += 10
    assert not replication._owner_row_due(retail, set(), 2)
    retail.last_applied_input_synthesized = False
    retail.last_applied_input_loop = 1000
    server.loop_count = replication._retail_jump_recovery[retail.id][2]
    assert replication._owner_row_due(retail, set(), 2)


def test_reused_player_and_native_capability_clear_recovery_state():
    server, replication, retail, _ = setup()
    replication.broadcast_world_updates()
    assert retail.id in replication._retail_jump_recovery
    replication.forget_player(retail.id)
    assert retail.id not in replication._retail_jump_recovery
    retail.connection.flight_profile_capable = True
    replication._record_owner_row(retail, 1000)
    assert retail.id not in replication._retail_jump_recovery


def test_unsent_jump_does_not_start_a_gap():
    server, replication, retail, _ = setup()
    retail.last_applied_input_synthesized = True
    replication.broadcast_world_updates()
    assert retail.id not in replication._retail_jump_recovery


@pytest.mark.parametrize('delivery', ['split', 'sequenced'])
@pytest.mark.parametrize('pack', [66, 67, 68, 69])
def test_inactive_pack_ordinary_jump_gets_replay_spacing(delivery, pack):
    server, replication, retail, _ = setup(delivery)
    retail.jetpack_id = pack
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
    retail.connection.sent.clear()
    retail.last_applied_input_loop += 2
    server.loop_count += 2
    replication.broadcast_world_updates()
    assert not owner_rows(retail.connection)
    # Actual activation is still an urgent reliable update during this gap.
    retail.jetpack_active = True
    server.loop_count += 1
    retail.last_applied_input_loop += 1
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
    assert any(mode == 'reliable' for _, mode in retail.connection.sent)
    assert retail.id not in replication._retail_jump_recovery


@pytest.mark.parametrize('field', ['jetpack_active', '_jetpack_physics_active',
                                 '_jetpack_activation_defer_remaining',
                                 '_jetpack_exhaustion_tail_remaining'])
def test_flight_handoff_never_enters_jump_spacing(field):
    server, replication, retail, _ = setup()
    retail.jetpack_id = 68
    setattr(retail, field, 1)
    replication.broadcast_world_updates()
    assert retail.id not in replication._retail_jump_recovery


@pytest.mark.parametrize('delivery', ['split', 'sequenced'])
def test_grounded_climb_attempts_keep_owner_anchors_fresh(delivery):
    server, replication, retail, _ = setup(delivery)
    retail.airborne = False
    for delta in range(0, 12, 2):
        server.loop_count = 500 + delta
        retail.last_applied_input_loop = 1000 + delta
        # The native mover can flag every held-Space climb attempt while
        # remaining grounded. Stock Character still restores its owner cache.
        retail.last_retail_jump_loop = 1000 + delta
        retail.connection.sent.clear()
        replication.broadcast_world_updates()
        assert owner_rows(retail.connection)
    # Walking off the ledge without a new attempt must not replay an old gap.
    retail.airborne = True
    retail.last_applied_input_loop += 2
    server.loop_count += 2
    retail.connection.sent.clear()
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
    retail.last_applied_input_loop += 2
    server.loop_count += 2
    retail.connection.sent.clear()
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)


@pytest.mark.parametrize('delivery', ['split', 'sequenced'])
def test_landing_closes_pending_jump_gap_immediately(delivery):
    server, replication, retail, _ = setup(delivery)
    replication.broadcast_world_updates()
    retail.airborne = False
    retail.last_applied_input_loop += 2
    server.loop_count += 2
    retail.connection.sent.clear()
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
    # A short ground contact may immediately precede another fall.
    retail.airborne = True
    retail.last_applied_input_loop += 2
    server.loop_count += 2
    retail.connection.sent.clear()
    replication.broadcast_world_updates()
    assert owner_rows(retail.connection)
