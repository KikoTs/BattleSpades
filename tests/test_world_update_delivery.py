"""WorldUpdate transport: nothing may hold it, nothing may apply it stale.

Measured on this ENet build (loopback, one datagram with a reliable command
dropped): a sequenced unreliable packet waits for the retransmission, an
unsequenced one does not, and any unreliable packet above ``mtu - 28`` wire
bytes leaves as RELIABLE fragments unless it is flagged otherwise.

The stock client (gameScene.pyd process_packet_world_update, character.pyd
set_network_position_and_velocity) and the native client apply every
WorldUpdate they receive; neither compares loop counts. Ordering is therefore
the server's job.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from server.config import ServerConfig, load_config  # noqa: E402
from server.connection import (  # noqa: E402
    Connection,
    ENET_DEFAULT_MTU,
    ENET_FRAGMENT_OVERHEAD,
    SNAPSHOT_HOLD_TIMEOUT_TICKS,
    framed_wire_size,
    max_unframed_payload,
)
from server.metrics import RuntimeMetrics  # noqa: E402
from server.replication import (  # noqa: E402
    REORDER_GUARD_MAX_INTERVAL,
    ReplicationService,
)
from server.util import lzf_compress  # noqa: E402
from shared.packet import CreatePlayer, WorldUpdate  # noqa: E402


RELIABLE, UNSEQUENCED, UNRELIABLE_FRAGMENT = 1, 2, 8
ROW = 56


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(entity_id, *, ints=(), floats=()):
    from shared.packet import Entity

    entity = Entity()
    entity.entity_id = entity_id
    entity.type = 12
    entity.state = 0
    entity.player_id = 1
    entity.pos_x, entity.pos_y, entity.pos_z = 100.0, 110.0, 50.0
    entity.vel_x, entity.vel_y, entity.vel_z = 1.0, 0.0, -0.5
    entity.int_properties = list(ints)
    entity.float_properties = list(floats)
    return entity


def _snapshot(player_ids, loop=500, turrets=0, entities=()) -> bytes:
    packet = WorldUpdate()
    packet.loop_count = loop
    packet.updated_entities = list(entities)
    for player_id in player_ids:
        packet[player_id] = (
            (100.0 + player_id, 200.0, 50.0),
            (1.0, 0.0, 0.0),
            (0.25, 0.0, 0.0),
            40,
            1000 + player_id,
            100,
            0x01,
            0x10,
            0x00,
            7,
        )
    packet.rocket_turrets = [(index + 1, 0.5, 0.25) for index in range(turrets)]
    return bytes(packet.generate())


def _rows_of(payload: bytes) -> list[int]:
    count = int.from_bytes(payload[5:7], "little")
    return [payload[7 + index * ROW] for index in range(count)]


def _decoded(payload: bytes) -> WorldUpdate:
    from shared.bytes import ByteReader

    assert payload[0] == WorldUpdate.id
    packet = WorldUpdate()
    packet.read(ByteReader(payload[1:]))
    return packet


class _Peer:
    def __init__(self, mtu=ENET_DEFAULT_MTU, in_transit=0):
        self.mtu = mtu
        self.reliableDataInTransit = in_transit


class _Recipient:
    """Connection double with the real Connection's snapshot surface."""

    def __init__(self, player, mtu=ENET_DEFAULT_MTU):
        self.player = player
        self.in_game = True
        self.peer = _Peer(mtu)
        self.sent: list[tuple[bytes, str]] = []
        self.held: frozenset = frozenset()

    def send(self, data, reliable=True, prefix=0x30, *, unsequenced=False):
        if unsequenced:
            mode = "unsequenced"
        else:
            mode = "reliable" if reliable else "sequenced"
        self.sent.append((bytes(data), mode))

    def send_snapshot(self, data, *, unsequenced=True):
        self.send(data, reliable=False, unsequenced=unsequenced)

    def snapshot_payload_limit(self):
        return max_unframed_payload(self.peer.mtu - ENET_FRAGMENT_OVERHEAD)

    def held_snapshot_rows(self):
        return self.held


def _player(player_id, *, spread=0, applied=1000, **extra):
    fields = dict(
        id=player_id,
        last_applied_input_loop=applied,
        last_applied_input_synthesized=False,
        wu_ack_loop=0,
        airborne=False,
        jetpack_active=False,
        parachute_active=False,
        is_bot=False,
        is_block_tool=lambda: False,
        input_reorder_spread_frames=lambda _now: spread,
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


def _server(players, *, delivery="split", guard=True, loop=500, turrets=0,
            snapshot_ids=None, entities=()):
    config = SimpleNamespace(
        broadcast_world_updates=True,
        worldupdate_broadcast_interval=2,
        worldupdate_self_row_interval=2,
        worldupdate_airborne_self_row_interval=6,
        worldupdate_loop_offset=0,
        worldupdate_include_self=True,
        worldupdate_delivery=delivery,
        worldupdate_reorder_guard=guard,
        debug_selfrow=False,
    )
    connections = {player.id: _Recipient(player) for player in players}
    server = SimpleNamespace(
        config=config,
        connections=connections,
        players={player.id: player for player in players},
        loop_count=loop,
        metrics=RuntimeMetrics(),
        host=None,
    )
    builds = []

    def build_world_update_data(
        exclude_player_id=None, loop_count_override=None, local_player_id=None,
    ):
        builds.append((exclude_player_id, loop_count_override, local_player_id))
        ids = snapshot_ids if snapshot_ids is not None else sorted(
            server.players
        )
        return _snapshot(
            [pid for pid in ids if pid != exclude_player_id],
            loop=loop_count_override,
            turrets=turrets,
            entities=entities,
        )

    server.build_world_update_data = build_world_update_data
    server.builds = builds
    return server


# ---------------------------------------------------------------------------
# Connection.send: flags
# ---------------------------------------------------------------------------


def _fake_enet(monkeypatch, allocated, with_fragment_flag=True):
    class Packet:
        def __init__(self, data, flags):
            self.data = data
            self.flags = flags
            self.free_callback = None
            allocated.append(self)

        def set_free_callback(self, callback):
            self.free_callback = callback

    fields = dict(
        Packet=Packet,
        PACKET_FLAG_RELIABLE=RELIABLE,
        PACKET_FLAG_UNSEQUENCED=UNSEQUENCED,
    )
    if with_fragment_flag:
        fields["PACKET_FLAG_UNRELIABLE_FRAGMENT"] = UNRELIABLE_FRAGMENT
    monkeypatch.setitem(sys.modules, "enet", SimpleNamespace(**fields))


def _connection(mtu=ENET_DEFAULT_MTU, loop=0):
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=[], packet_trace=False),
        loop_count=loop,
    )
    peer = SimpleNamespace(
        address="127.0.0.1:5000", mtu=mtu, send=lambda channel, packet: None,
    )
    return Connection(peer, server), server


def test_wire_size_matches_the_real_framing():
    for length in (0, 1, 31, 32, 33, 64, 1329, 1330, 4000):
        assert framed_wire_size(length) == 1 + len(lzf_compress(bytes(length)))


def test_payload_limit_is_the_largest_body_in_one_enet_command():
    limit = ENET_DEFAULT_MTU - ENET_FRAGMENT_OVERHEAD
    assert limit == 1372
    body = max_unframed_payload(limit)
    assert framed_wire_size(body) <= limit < framed_wire_size(body + 1)
    connection, _ = _connection()
    assert connection.unfragmented_wire_limit() == limit
    assert connection.snapshot_payload_limit() == body
    small, _ = _connection(mtu=576)
    assert small.unfragmented_wire_limit() == 548


@pytest.mark.parametrize("unsequenced", [False, True])
def test_oversize_snapshot_part_is_never_promoted_to_reliable(
    monkeypatch, unsequenced
):
    """ENet fragments flag 0 and UNSEQUENCED reliably above mtu - 28."""
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, _ = _connection()
    body = connection.snapshot_payload_limit()

    connection.send_snapshot(
        bytes([2]) + bytes(body - 1), unsequenced=unsequenced
    )
    assert allocated[-1].flags == (UNSEQUENCED if unsequenced else 0)

    connection.send_snapshot(
        bytes([2]) + bytes(body), unsequenced=unsequenced
    )
    assert allocated[-1].flags == UNRELIABLE_FRAGMENT
    assert connection.oversize_snapshot_sends == 1


def test_every_other_send_keeps_the_flags_it_always_had(monkeypatch):
    """Only snapshot parts are reflagged (tests/test_server_capacity.py
    pins flag 0 for the ordinary unreliable send of any size)."""
    allocated = []
    _fake_enet(monkeypatch, allocated, with_fragment_flag=False)
    connection, _ = _connection()

    connection.send(bytes([2]) + bytes(4000), reliable=True)
    assert allocated[-1].flags == RELIABLE
    connection.send(bytes([2]) + bytes(4000), reliable=False)
    assert allocated[-1].flags == 0
    connection.send(bytes([0]) + bytes(8), reliable=False, unsequenced=True)
    assert allocated[-1].flags == UNSEQUENCED
    connection.send_snapshot(bytes([2]) + bytes(60))
    assert allocated[-1].flags == UNSEQUENCED
    # A binding without the constant still gets ENet's flag value.
    connection.send_snapshot(bytes([2]) + bytes(4000))
    assert allocated[-1].flags == UNRELIABLE_FRAGMENT


# ---------------------------------------------------------------------------
# Connection: rows held behind CreatePlayer
# ---------------------------------------------------------------------------


def _create_player(player_id) -> bytes:
    packet = CreatePlayer()
    packet.player_id = player_id
    packet.name = "Row%d" % player_id
    packet.loadout = [7]
    packet.prefabs = []
    return bytes(packet.generate())


def test_rows_are_held_until_create_player_is_acknowledged(monkeypatch):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, _ = _connection(loop=100)

    connection.send(_create_player(5), reliable=True)
    assert connection.held_snapshot_rows() == frozenset({5})

    allocated[-1].free_callback()  # ENet acknowledged the packet
    assert connection.held_snapshot_rows() == frozenset()


def test_two_lives_in_flight_release_on_the_second_acknowledgement(monkeypatch):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, _ = _connection(loop=100)

    connection.send(_create_player(5), reliable=True)
    connection.send(_create_player(5), reliable=True)
    allocated[0].free_callback()
    assert connection.held_snapshot_rows() == frozenset({5})
    allocated[1].free_callback()
    assert connection.held_snapshot_rows() == frozenset()
    # A packet's callback releases its own hold only, however often it runs.
    connection.send(_create_player(5), reliable=True)
    allocated[1].free_callback()
    allocated[1].free_callback()
    assert connection.held_snapshot_rows() == frozenset({5})
    allocated[2].free_callback()
    assert connection.held_snapshot_rows() == frozenset()


def test_hold_expires_when_no_acknowledgement_is_ever_reported(monkeypatch):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, server = _connection(loop=100)

    connection.send(_create_player(9), reliable=True)
    server.loop_count = 100 + SNAPSHOT_HOLD_TIMEOUT_TICKS
    assert connection.held_snapshot_rows() == frozenset({9})
    server.loop_count += 1
    assert connection.held_snapshot_rows() == frozenset()


def test_reliable_packets_are_numbered_until_acknowledged(monkeypatch):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, server = _connection(loop=100)
    assert connection.reliable_unacked_through(10) is False

    for _ in range(3):
        connection.send(bytes([5, 7, 0, 0]), reliable=True)
    connection.send(bytes([5, 7, 0, 0]), reliable=False)  # not numbered
    assert connection.reliable_send_index == 3

    assert connection.reliable_unacked_through(1) is True
    allocated[0].free_callback()
    assert connection.reliable_unacked_through(1) is False
    assert connection.reliable_unacked_through(3) is True
    allocated[2].free_callback()
    allocated[1].free_callback()
    assert connection.reliable_unacked_through(3) is False
    # An acknowledgement that is never reported stops counting after 10 s.
    connection.send(bytes([5, 7, 0, 0]), reliable=True)
    server.loop_count = 100 + 601
    assert connection.reliable_unacked_through(4) is False


def test_entity_row_is_held_until_create_entity_is_acknowledged(monkeypatch):
    from shared.packet import CreateEntity

    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, _ = _connection(loop=100)
    for entity_id in (7, 40000):  # 40000 is -25536 on the wire
        packet = CreateEntity()
        packet.set_entity(_entity(
            ((entity_id + 0x8000) & 0xFFFF) - 0x8000
        ))
        connection.send(bytes(packet.generate()), reliable=True)

    assert connection.held_snapshot_rows() == frozenset({
        ("entity", 7), ("entity", -25536),
    })
    allocated[0].free_callback()
    assert connection.held_snapshot_rows() == frozenset({("entity", -25536)})
    allocated[1].free_callback()
    assert connection.held_snapshot_rows() == frozenset()


def test_other_reliable_packets_and_bindings_without_callback_hold_nothing(
    monkeypatch,
):
    allocated = []
    _fake_enet(monkeypatch, allocated)
    connection, _ = _connection()
    connection.send(bytes([5, 7, 0, 0]), reliable=True)  # SetHP, not a life
    assert connection.held_snapshot_rows() == frozenset()

    monkeypatch.setitem(sys.modules, "enet", SimpleNamespace(
        Packet=lambda data, flags: data,
        PACKET_FLAG_RELIABLE=RELIABLE,
        PACKET_FLAG_UNSEQUENCED=UNSEQUENCED,
    ))
    connection.send(_create_player(5), reliable=True)
    assert connection.held_snapshot_rows() == frozenset()


# ---------------------------------------------------------------------------
# Replication: two streams
# ---------------------------------------------------------------------------


def test_observer_rows_leave_unsequenced_and_the_own_row_stays_ordered():
    players = [_player(0), _player(1), _player(2)]
    server = _server(players, turrets=2)

    ReplicationService(server).broadcast_world_updates()

    # One serialization for every recipient.
    assert server.builds == [(None, 500, None)]
    for player in players:
        sent = server.connections[player.id].sent
        assert [mode for _data, mode in sent] == ["unsequenced", "sequenced"]
        observer, owner = sent[0][0], sent[1][0]
        assert sorted(_rows_of(observer)) == sorted(
            other.id for other in players if other.id != player.id
        )
        assert len(_decoded(observer).rocket_turrets) == 2
        assert _rows_of(owner) == [player.id]
        own = _decoded(owner)
        assert own.loop_count == 500
        assert own.player_updates[player.id][9] == 0xFF  # tool sentinel
        assert own.player_updates[player.id][4] == 1000 + player.id  # pong
        assert own.updated_entities == [] and own.rocket_turrets == []
        # Observers still see the real tool.
        for row in _decoded(observer).player_updates.values():
            assert row[9] == 7


def test_metrics_count_one_serialization_and_every_packet():
    players = [_player(0), _player(1)]
    server = _server(players)

    ReplicationService(server).broadcast_world_updates()

    sent = [data for c in server.connections.values() for data, _m in c.sent]
    assert server.metrics.world_serializations == 1
    assert server.metrics.world_sends == len(sent) == 4
    assert server.metrics.world_bytes == sum(len(data) for data in sent)


def test_a_full_server_snapshot_is_split_below_the_fragment_limit():
    """24 rows are 1355 bytes: one packet would leave as reliable fragments."""
    players = [_player(player_id) for player_id in range(24)]
    server = _server(players, turrets=3)
    whole = _snapshot(range(24), turrets=3)
    limit = ENET_DEFAULT_MTU - ENET_FRAGMENT_OVERHEAD
    assert framed_wire_size(len(whole)) > limit

    ReplicationService(server).broadcast_world_updates()

    recipient = server.connections[0]
    observers = [d for d, mode in recipient.sent if mode == "unsequenced"]
    assert len(observers) >= 1
    seen: list[int] = []
    turrets = 0
    for payload in observers:
        assert framed_wire_size(len(payload)) <= limit
        seen.extend(_rows_of(payload))
        turrets += len(_decoded(payload).rocket_turrets)
    assert sorted(seen) == list(range(1, 24))  # every row exactly once
    assert turrets == 3  # the tail travels once


def test_small_mtu_peer_gets_more_smaller_parts():
    players = [_player(player_id) for player_id in range(12)]
    server = _server(players)
    server.connections[0].peer.mtu = 576

    ReplicationService(server).broadcast_world_updates()

    observers = [
        d for d, mode in server.connections[0].sent if mode == "unsequenced"
    ]
    assert len(observers) == 2
    assert all(framed_wire_size(len(p)) <= 548 for p in observers)
    assert sorted(
        row for payload in observers for row in _rows_of(payload)
    ) == list(range(1, 12))


def test_row_of_an_unacknowledged_life_is_left_out_for_that_peer_only():
    players = [_player(0), _player(1), _player(2)]
    server = _server(players)
    server.connections[0].held = frozenset({2})

    ReplicationService(server).broadcast_world_updates()

    assert _rows_of(server.connections[0].sent[0][0]) == [1]
    assert sorted(_rows_of(server.connections[1].sent[0][0])) == [0, 2]


def test_entity_and_turret_rows_wait_for_their_create_entity():
    entities = [
        _entity(7, ints=(3, 4), floats=(0.5,)),
        _entity(8),
        _entity(9, floats=(1.0, 2.0, 3.0)),
    ]
    server = _server(
        [_player(0), _player(1)], entities=entities, turrets=3,
    )
    # Turret rows carry ids 1, 2, 3; entity 8 and turret 2 are unacknowledged.
    server.connections[0].held = frozenset({("entity", 8), ("entity", 2)})

    ReplicationService(server).broadcast_world_updates()

    held = _decoded(server.connections[0].sent[0][0])
    assert [e.entity_id for e in held.updated_entities] == [7, 9]
    assert held.updated_entities[0].int_properties == [3, 4]
    assert len(held.updated_entities[1].float_properties) == 3
    assert [row[0] for row in held.rocket_turrets] == [1, 3]
    assert _rows_of(server.connections[0].sent[0][0]) == [1]
    # The other recipient acknowledged everything.
    full = _decoded(server.connections[1].sent[0][0])
    assert [e.entity_id for e in full.updated_entities] == [7, 8, 9]
    assert [row[0] for row in full.rocket_turrets] == [1, 2, 3]


def test_unparseable_tail_is_withheld_rather_than_sent_unfiltered():
    tail = _snapshot([], entities=[_entity(7)])[7:]

    assert ReplicationService._tail_without(tail, {99}) == tail
    assert ReplicationService._tail_without(tail[:-1], {7}) == bytes(4)
    assert ReplicationService._tail_without(tail[:20], {7}) == bytes(4)


def test_dead_owner_gets_observer_rows_and_no_own_packet():
    players = [_player(0), _player(1)]
    server = _server(players, snapshot_ids=[1])  # player 0 has no live body

    ReplicationService(server).broadcast_world_updates()

    sent = server.connections[0].sent
    assert [mode for _d, mode in sent] == ["unsequenced"]
    assert _rows_of(sent[0][0]) == [1]


def test_refilled_label_never_stamps_an_own_row_in_split_mode():
    players = [_player(0, last_applied_input_synthesized=True), _player(1)]
    server = _server(players)

    ReplicationService(server).broadcast_world_updates()

    assert [mode for _d, mode in server.connections[0].sent] == ["unsequenced"]
    assert [mode for _d, mode in server.connections[1].sent] == [
        "unsequenced", "sequenced",
    ]


def test_flight_transition_is_reliable_and_carries_the_owner_alone():
    """A retransmitted transition must not re-apply old rows of others."""
    flyer = _player(0, jetpack_active=True)
    players = [flyer, _player(1), _player(2)]
    server = _server(players, loop=501)  # between two cadence ticks
    replication = ReplicationService(server)
    replication._last_broadcast_bucket = 250

    replication.broadcast_world_updates()

    sent = server.connections[0].sent
    assert [mode for _d, mode in sent] == ["reliable"]
    assert _rows_of(sent[0][0]) == [0]
    assert _decoded(sent[0][0]).player_updates[0][9] == 0xFF
    assert server.connections[1].sent == []
    # Settled. The transition refreshed the anchor at 501, so the own-row
    # cadence (two ticks) skips 502 and resumes at 504, as it always did.
    server.loop_count = 502
    replication.broadcast_world_updates()
    assert [mode for _d, mode in server.connections[0].sent[1:]] == [
        "unsequenced",
    ]
    server.loop_count = 504
    replication.broadcast_world_updates()
    assert [mode for _d, mode in server.connections[0].sent[2:]] == [
        "unsequenced", "sequenced",
    ]


def test_transition_on_a_cadence_tick_keeps_observers_unsequenced():
    flyer = _player(0, jetpack_active=True)
    server = _server([flyer, _player(1)])

    ReplicationService(server).broadcast_world_updates()

    assert [mode for _d, mode in server.connections[0].sent] == [
        "unsequenced", "reliable",
    ]
    assert _rows_of(server.connections[0].sent[1][0]) == [0]


def test_sequenced_mode_is_the_previous_single_ordered_packet():
    players = [_player(0), _player(1)]
    server = _server(players, delivery="sequenced")

    ReplicationService(server).broadcast_world_updates()

    for player in players:
        sent = server.connections[player.id].sent
        assert [mode for _d, mode in sent] == ["sequenced"]
        assert sorted(_rows_of(sent[0][0])) == [0, 1]


def test_opaque_payload_from_a_tooling_seam_falls_back_to_one_packet():
    server = _server([_player(0)])
    server.build_world_update_data = lambda **_kwargs: b"opaque"

    ReplicationService(server).broadcast_world_updates()

    assert server.connections[0].sent == [(b"opaque", "sequenced")]


def test_shipped_defaults_select_split_delivery(tmp_path):
    assert ServerConfig().worldupdate_delivery == "split"
    assert ServerConfig().worldupdate_reorder_guard is True
    path = tmp_path / "c.toml"
    path.write_text(
        "[network]\nworldupdate_delivery = \"sequenced\"\n"
        "worldupdate_reorder_guard = false\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.worldupdate_delivery == "sequenced"
    assert config.worldupdate_reorder_guard is False
    path.write_text(
        "[network]\nworldupdate_delivery = \"reliable\"\n", encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(path)


# ---------------------------------------------------------------------------
# Replication: reorder guard
# ---------------------------------------------------------------------------


def _observer_loops(server, replication, recipient, loops):
    out = []
    for loop in loops:
        server.loop_count = loop
        before = len(recipient.sent)
        replication.broadcast_world_updates()
        modes = [mode for _d, mode in recipient.sent[before:]]
        if "unsequenced" in modes:
            out.append((loop, "unsequenced"))
        elif modes.count("sequenced") == 2:
            out.append((loop, "sequenced"))
    return out


def test_clean_link_keeps_full_cadence():
    server = _server([_player(0, spread=1), _player(1)], loop=500)
    replication = ReplicationService(server)

    sent = _observer_loops(
        server, replication, server.connections[0], range(500, 512, 2)
    )

    assert sent == [(loop, "unsequenced") for loop in range(500, 512, 2)]


@pytest.mark.parametrize("spread,spacing", [(2, 4), (3, 4), (4, 6), (5, 6)])
def test_snapshots_are_spaced_wider_than_the_measured_reordering(
    spread, spacing
):
    """Displacement d bounds the delay spread below d + 1 frames."""
    server = _server([_player(0, spread=spread), _player(1)], loop=500)
    replication = ReplicationService(server)

    sent = _observer_loops(
        server, replication, server.connections[0], range(500, 524, 2)
    )

    loops = [loop for loop, _mode in sent]
    assert all(mode == "unsequenced" for _loop, mode in sent)
    gaps = {b - a for a, b in zip(loops, loops[1:])}
    assert gaps == {spacing}
    assert spacing >= spread + 1  # two snapshots cannot cross
    # The other player's link is clean and keeps 30 Hz.
    assert len([
        1 for _d, mode in server.connections[1].sent if mode == "unsequenced"
    ]) == 12


def test_own_row_cadence_is_untouched_by_the_guard():
    server = _server([_player(0, spread=3), _player(1)], loop=500)
    replication = ReplicationService(server)
    for loop in range(500, 512, 2):
        server.loop_count = loop
        replication.broadcast_world_updates()

    own = [
        data for data, mode in server.connections[0].sent
        if mode == "sequenced"
    ]
    assert len(own) == 6 and all(_rows_of(data) == [0] for data in own)


def test_own_row_header_names_the_newest_observer_snapshot_sent():
    """ShootPacket.shot_on_world_update must date the bodies the shooter saw.

    The client stores every WorldUpdate's header loop; while the observer
    stream is paced, own-row packets still leave every two ticks and would
    otherwise claim a snapshot the client was never sent.
    """
    server = _server([_player(0, spread=3), _player(1)], loop=500)
    replication = ReplicationService(server)
    recipient = server.connections[0]
    newest_observer = None
    for loop in range(500, 512, 2):
        server.loop_count = loop
        before = len(recipient.sent)
        replication.broadcast_world_updates()
        for data, mode in recipient.sent[before:]:
            header = _decoded(data).loop_count
            if mode == "unsequenced":
                newest_observer = header
                assert header == loop
            else:
                assert header == newest_observer
    # Spacing four ticks: observer snapshots at 500, 504, 508.
    assert newest_observer == 508
    # The other recipient's link is clean: both streams carry the tick.
    for data, _mode in server.connections[1].sent[-2:]:
        assert _decoded(data).loop_count == 510


def test_link_beyond_the_widest_spacing_gets_ordered_delivery():
    spread = REORDER_GUARD_MAX_INTERVAL  # needs spacing 7 > 6
    player = _player(0, spread=0)
    server = _server([player, _player(1)], loop=500)
    replication = ReplicationService(server)
    recipient = server.connections[0]
    server.loop_count = 500
    replication.broadcast_world_updates()
    assert recipient.sent[0][1] == "unsequenced"

    player.input_reorder_spread_frames = lambda _now: spread
    sent = _observer_loops(server, replication, recipient, range(502, 520, 2))

    # The first ordered snapshot waits until the last unsequenced one cannot
    # arrive after it, then ordered delivery runs at full cadence.
    assert sent[0] == (508, "sequenced")
    assert [loop for loop, _m in sent] == [508, 510, 512, 514, 516, 518]
    assert all(mode == "sequenced" for _loop, mode in sent)


def test_return_to_unsequenced_waits_until_no_ordered_snapshot_can_be_held():
    """A held ordered snapshot would be released after a newer one.

    Sending another ordered one only moves that risk along, so the observer
    stream pauses until every reliable packet sent before the last ordered
    snapshot is acknowledged. The own row keeps its cadence.
    """
    player = _player(0, spread=REORDER_GUARD_MAX_INTERVAL)
    server = _server([player, _player(1)], loop=500)
    replication = ReplicationService(server)
    recipient = server.connections[0]
    recipient.reliable_send_index = 41
    unacked = {40, 41}
    recipient.reliable_unacked_through = lambda index: any(
        number <= index for number in unacked
    )
    replication.broadcast_world_updates()
    assert recipient.sent[0][1] == "sequenced"
    assert recipient._wu_ordered_barrier == 41

    player.input_reorder_spread_frames = lambda _now: 0  # evidence expired
    # Reliable packets sent after the last ordered snapshot do not matter.
    recipient.reliable_send_index = 43
    unacked.update({42, 43})
    before = len(recipient.sent)
    assert _observer_loops(server, replication, recipient, [502, 504]) == []
    assert [mode for _d, mode in recipient.sent[before:]] == [
        "sequenced", "sequenced",
    ]
    assert all(
        _rows_of(data) == [0] for data, _m in recipient.sent[before:]
    )
    unacked.difference_update({40, 41})
    assert _observer_loops(server, replication, recipient, [506, 508]) == [
        (506, "unsequenced"), (508, "unsequenced"),
    ]


def test_binding_without_packet_numbers_waits_for_an_empty_reliable_stream():
    player = _player(0, spread=REORDER_GUARD_MAX_INTERVAL)
    server = _server([player, _player(1)], loop=500)
    replication = ReplicationService(server)
    recipient = server.connections[0]
    replication.broadcast_world_updates()

    player.input_reorder_spread_frames = lambda _now: 0
    recipient.peer.reliableDataInTransit = 300
    assert _observer_loops(server, replication, recipient, [502, 504]) == []
    recipient.peer.reliableDataInTransit = 0
    assert _observer_loops(server, replication, recipient, [506]) == [
        (506, "unsequenced"),
    ]


def test_guard_can_be_switched_off():
    server = _server([_player(0, spread=5), _player(1)], guard=False)
    replication = ReplicationService(server)

    sent = _observer_loops(
        server, replication, server.connections[0], range(500, 508, 2)
    )

    assert sent == [(loop, "unsequenced") for loop in range(500, 508, 2)]


@pytest.mark.parametrize("delivery", ["split", "sequenced"])
@pytest.mark.parametrize("from_sample", [False, True])
def test_default_airborne_owner_receives_each_snapshot_bucket(delivery, from_sample):
    """Jumping must not make retail's cached self position three times older.

    The stock Character restores its received self position when a jump
    launches. Test decoded packet inclusion, including an airborne period and
    landing, rather than merely asserting the configured interval value.
    """
    config = (
        load_config(Path(__file__).resolve().parents[1] / "config.toml")
        if from_sample else ServerConfig()
    )
    config.worldupdate_delivery = delivery
    player = _player(0)
    server = _server([player, _player(1)], loop=500)
    server.config = config
    recipient = server.connections[0]
    replication = ReplicationService(server)

    for index, airborne in enumerate((False, True, True, True, True, False)):
        player.airborne = airborne
        server.loop_count = 500 + 2 * index
        player.last_applied_input_loop = 1000 + 2 * index
        recipient.sent.clear()
        replication.broadcast_world_updates()
        owner_packets = [
            _decoded(data) for data, _mode in recipient.sent if 0 in _rows_of(data)
        ]
        assert len(owner_packets) == 1, (server.loop_count, airborne)


@pytest.mark.parametrize("delivery", ["split", "sequenced"])
@pytest.mark.parametrize("from_sample", [False, True])
def test_retail_airborne_refresh_does_not_change_native_cadence(delivery, from_sample):
    """Mixed peers retain distinct owner cadence and full observer delivery."""
    config = (
        load_config(Path(__file__).resolve().parents[1] / "config.toml")
        if from_sample else ServerConfig()
    )
    config.worldupdate_delivery = delivery
    retail, native = _player(0, airborne=True), _player(1, airborne=True)
    server = _server([retail, native], loop=600)
    server.config = config
    for player, capable in ((retail, False), (native, True)):
        player.connection = server.connections[player.id]
        player.connection.flight_profile_capable = capable
    replication = ReplicationService(server)
    owners = {0: [], 1: []}

    for loop in range(600, 624, 2):
        server.loop_count = loop
        for player in (retail, native):
            player.last_applied_input_loop = 1000 + loop
            player.connection.sent.clear()
        replication.broadcast_world_updates()
        for player in (retail, native):
            received = [
                row_id
                for data, _mode in player.connection.sent
                for row_id in _rows_of(data)
            ]
            assert 1 - player.id in received, (loop, player.id)
            if player.id in received:
                owners[player.id].append(loop)
    assert owners[0] == list(range(600, 624, 2))
    assert owners[1] == list(range(600, 624, 6))

    # Landing restores the existing common 30 Hz cadence for both peers.
    for player in (retail, native):
        player.airborne = False
        player.connection.sent.clear()
    server.loop_count = 624
    replication.broadcast_world_updates()
    for player in (retail, native):
        assert any(player.id in _rows_of(data) for data, _ in player.connection.sent)


@pytest.mark.parametrize("delivery", ["split", "sequenced"])
@pytest.mark.parametrize(
    "broadcast,grounded,native,retail,native_spacing,retail_spacing",
    [
        (2, 2, 8, 4, 8, 4),
        # Odd intervals round up to the next available broadcast bucket.
        (2, 2, 5, 3, 6, 4),
        # Neither owner lane can outrun the common broadcast/ground floor.
        (4, 2, 6, 1, 8, 4),
        (2, 4, 6, 1, 6, 4),
        # Loading clamps nonpositive values independently to one tick.
        (2, 2, -5, 0, 2, 2),
    ],
)
def test_custom_airborne_config_reaches_each_peer_packet_schedule(
    tmp_path, delivery, broadcast, grounded, native, retail,
    native_spacing, retail_spacing,
):
    path = tmp_path / "cadence.toml"
    path.write_text(
        f'[network]\nworldupdate_delivery = "{delivery}"\n'
        f'[debug]\nworldupdate_broadcast_interval = {broadcast}\n'
        f'worldupdate_self_row_interval = {grounded}\n'
        f'worldupdate_airborne_self_row_interval = {native}\n'
        f'worldupdate_retail_airborne_self_row_interval = {retail}\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.worldupdate_airborne_self_row_interval == max(1, native)
    assert config.worldupdate_retail_airborne_self_row_interval == max(1, retail)
    retail_player, native_player = _player(0, airborne=True), _player(1, airborne=True)
    server = _server([retail_player, native_player], loop=600)
    server.config = config
    for player, capable in ((retail_player, False), (native_player, True)):
        player.connection = server.connections[player.id]
        player.connection.flight_profile_capable = capable
    replication = ReplicationService(server)
    owners = {0: [], 1: []}

    for loop in range(600, 648):
        server.loop_count = loop
        for player in (retail_player, native_player):
            player.last_applied_input_loop = 1000 + loop
            player.connection.sent.clear()
        replication.broadcast_world_updates()
        for player in (retail_player, native_player):
            received = [
                row_id for data, _mode in player.connection.sent
                for row_id in _rows_of(data)
            ]
            assert (1 - player.id in received) == (loop % broadcast == 0)
            if player.id in received:
                owners[player.id].append(loop)
    assert owners[0] == list(range(600, 648, retail_spacing))
    assert owners[1] == list(range(600, 648, native_spacing))


@pytest.mark.parametrize("delivery", ["split", "sequenced"])
def test_airborne_legacy_config_namespace_retains_existing_cadence(delivery):
    """An older injected config without the new field keeps its old interval."""
    retail, native = _player(0, airborne=True), _player(1, airborne=True)
    server = _server([retail, native], delivery=delivery, loop=600)
    server.config.worldupdate_airborne_self_row_interval = 4
    assert not hasattr(server.config, "worldupdate_retail_airborne_self_row_interval")
    # Missing capability/connection is a retail-shaped test fixture, not an
    # error or a reason to alter an old injected namespace's explicit value.
    native.connection = server.connections[1]
    native.connection.flight_profile_capable = True
    replication = ReplicationService(server)
    owners = {0: [], 1: []}
    for loop in range(600, 616, 2):
        server.loop_count = loop
        for connection in server.connections.values():
            connection.player.last_applied_input_loop = 1000 + loop
            connection.sent.clear()
        replication.broadcast_world_updates()
        for player_id, connection in server.connections.items():
            if any(player_id in _rows_of(data) for data, _ in connection.sent):
                owners[player_id].append(loop)
    assert owners == {0: [600, 604, 608, 612], 1: [600, 604, 608, 612]}
