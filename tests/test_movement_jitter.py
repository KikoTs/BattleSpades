"""Regression coverage for retail loop-label skips and client frame hitches.

The retail ``loop_count`` is a clock label, not proof that the client emitted
one ClientData packet (or one movement-history record) for every intervening
integer.  Captured retail frames regularly jump by two at an ordinary ~17 ms
frame, and a 50 ms hitch jumped by three.  A WorldUpdate must therefore never
acknowledge a loop label that was synthesized only by the server: the client's
exact history lookup cannot find that label and immediately snaps its player.
"""

import asyncio
from types import SimpleNamespace

import pytest

from server.config import ServerConfig
from server.replication import ReplicationService
from server.simulation_runtime import SimulationRuntime
from tests.test_reversed_world_update import make_player


TICK_DT = 1.0 / 60.0
FORWARD = (True, False, False, False, False, False, False, False)
ORIENTATION = (1.0, 0.0, 0.0)


def test_production_defaults_keep_self_anchor_without_clock_bias() -> None:
    config = ServerConfig()

    assert config.worldupdate_include_self is True
    assert config.clock_sync_loop_bias == 0


class _BufferedPlayer:
    """Small runtime double that consumes only already-observed labels."""

    def __init__(self, player_id: int, labels: range) -> None:
        self.id = player_id
        self.is_bot = False
        self.input_history = {label: object() for label in labels}
        self.last_applied_input_loop = None
        self.applied: list[int] = []

    async def simulate_tick(self, _dt: float) -> None:
        if not self.input_history:
            return
        label = min(self.input_history)
        del self.input_history[label]
        self.last_applied_input_loop = label
        self.applied.append(label)


def _runtime_for_input(players: list[_BufferedPlayer]) -> SimulationRuntime:
    server = SimpleNamespace(
        players={player.id: player for player in players},
        tick_interval=TICK_DT,
        config=ServerConfig(),
    )
    return SimulationRuntime(server)


def test_runtime_consumes_one_observed_input_per_player_per_tick() -> None:
    """Never replay queued input across an unacknowledged client state edge.

    Live retail capture ``current-mutationstall650-bound2`` proved that even a
    two-row batch can cross a terrain transition the client has not received:
    it produced a hard SNAP and a 4.36-block teleport.  Engineer activation
    similarly regressed under ordinary service.  Until transition watermarks
    exist, one authoritative row per owner per server tick is the safe wire
    contract regardless of queue depth.
    """
    player = _BufferedPlayer(0, range(100, 110))
    runtime = _runtime_for_input([player])

    asyncio.run(runtime._simulate_players())

    assert player.applied == [100]
    assert sorted(player.input_history) == list(range(101, 110))


def test_runtime_advances_each_player_once_without_queue_size_bias() -> None:
    players = [
        _BufferedPlayer(player_id, range(100, 110))
        for player_id in range(3)
    ]
    runtime = _runtime_for_input(players)

    asyncio.run(runtime._simulate_players())

    assert [player.applied for player in players] == [[100], [100], [100]]


def test_runtime_skips_network_players_outside_the_live_scene() -> None:
    """Joining and retiring clients must not simulate against the active VXL."""

    joining = _BufferedPlayer(0, range(100, 102))
    joining.connection = SimpleNamespace(in_game=False)
    active = _BufferedPlayer(1, range(100, 102))
    active.connection = SimpleNamespace(in_game=True)
    bot = _BufferedPlayer(2, range(100, 102))
    bot.connection = None
    runtime = _runtime_for_input([joining, active, bot])

    asyncio.run(runtime._simulate_players())

    assert joining.applied == []
    assert active.applied == [100]
    assert bot.applied == [100]


def _consume(player) -> list[float]:
    """Run one authoritative tick and return the physics deltas it used."""
    deltas: list[float] = []

    async def record_update(dt: float) -> None:
        deltas.append(dt)

    player.update = record_update
    asyncio.run(player.simulate_tick(TICK_DT))
    return deltas


def test_clock_label_skip_acks_only_the_observed_client_frame() -> None:
    """A 100 -> 102 clock jump must not invent acknowledgement 101.

    Retail evidence: capture ``53a2149823e9`` contains 2303 -> 2305 with
    ``dt=17.5615 ms``.  That is one ordinary physics frame carrying label 2305,
    not two frames that the server may replay as 2304 and 2305.
    """
    player, _ = make_player()
    player.record_input_frame(100, FORWARD, ORIENTATION)
    assert _consume(player) == pytest.approx([TICK_DT])

    player.record_input_frame(102, FORWARD, ORIENTATION)
    deltas = _consume(player)

    assert player.last_applied_input_loop == 102
    assert deltas == pytest.approx([TICK_DT])
    assert player.input_history == {}


def test_input_telemetry_separates_stale_duplicates_from_capacity_loss() -> None:
    player, _ = make_player()
    player.last_applied_input_loop = 100

    player.record_input_frame(100, FORWARD, ORIENTATION)

    assert player.input_frames_stale == 1
    assert player.input_frames_overflow == 0
    assert player.input_frames_dropped == 1


def test_buffered_duplicate_keeps_first_frame_and_receive_tick() -> None:
    """A duplicate loop cannot move its source-frame delivery boundary."""
    player, _ = make_player()
    player.record_input_frame(
        100,
        FORWARD,
        ORIENTATION,
        received_server_tick=500,
    )
    replacement = (False, True, False, False, False, False, False, False)

    player.record_input_frame(
        100,
        replacement,
        ORIENTATION,
        received_server_tick=900,
    )

    frame = player.input_history[100]
    assert frame.movement_flags == FORWARD
    assert frame.received_server_tick == 500
    assert player.input_frames_stale == 1


def test_dead_class_change_frames_do_not_fill_next_life_history() -> None:
    player, _ = make_player()
    player.alive = False
    player.spawned = False

    for loop in range(100, 500):
        player.record_input_frame(loop, FORWARD, ORIENTATION)

    assert player.input_history == {}
    assert player.input_frames_overflow == 0
    assert player.input_frames_dropped == 0


def test_starvation_and_a_label_skip_still_advance_one_observed_frame() -> None:
    """Transport starvation must not turn a clock-label skip into ``dt*3``.

    Retail captures contain ordinary single frames labelled 1698 -> 1700
    (16.93 ms), 1424 -> 1426 (17 ms), and 2387 -> 2389 (18 ms).  ClientData
    carries no rendered-frame duration, so a consumed packet is exactly one
    authoritative fixed step regardless of coincident server starvation.
    """
    player, _ = make_player()
    player.record_input_frame(100, FORWARD, ORIENTATION)
    assert _consume(player) == pytest.approx([TICK_DT])

    assert _consume(player) == []
    assert _consume(player) == []
    assert player.input_starved_ticks == 2

    player.record_input_frame(103, FORWARD, ORIENTATION)
    deltas = _consume(player)

    assert player.last_applied_input_loop == 103
    assert deltas == pytest.approx([TICK_DT])
    assert player.input_history == {}


def test_long_stall_resumes_with_one_fixed_observed_frame() -> None:
    """A delayed packet never injects a large nonlinear physics step."""
    player, _ = make_player()
    player.record_input_frame(100, FORWARD, ORIENTATION)
    assert _consume(player) == pytest.approx([TICK_DT])

    for _ in range(12):
        assert _consume(player) == []

    player.record_input_frame(113, FORWARD, ORIENTATION)
    deltas = _consume(player)

    assert player.last_applied_input_loop == 113
    assert deltas == pytest.approx([TICK_DT])
    assert player.input_history == {}


def test_transport_burst_does_not_turn_packet_delay_into_client_hitch_dt() -> None:
    """Buffered consecutive labels remain ordinary one-tick client frames.

    Starvation can mean either a client render hitch or mere transport delay.
    When the eventual burst contains every consecutive label, the packets prove
    that the client did render those frames separately.  Applying the whole
    starvation interval to frame 101 would overshoot its movement-history entry.
    """
    player, _ = make_player()
    player.record_input_frame(100, FORWARD, ORIENTATION)
    assert _consume(player) == pytest.approx([TICK_DT])

    assert _consume(player) == []
    assert _consume(player) == []
    for loop in (101, 102, 103):
        player.record_input_frame(loop, FORWARD, ORIENTATION)

    deltas = _consume(player)

    assert player.last_applied_input_loop == 101
    assert deltas == pytest.approx([TICK_DT])
    assert sorted(player.input_history) == [102, 103]


def test_jump_and_locomotion_share_the_observed_frame_latch() -> None:
    """A packet button transition becomes input on the next observed frame."""
    player, _ = make_player()
    simulated: list[tuple[bool, bool, bool, int | None, int | None]] = []

    async def record_update(_dt: float) -> None:
        simulated.append(
            (
                player.input.up,
                player.input.sprint,
                player.input.jump,
                player._applied_input_source_loop,
                player._applied_input_source_server_tick,
            )
        )

    player.update = record_update
    walking = (True, False, False, False, False, False, False, False)
    jump_sprinting = (
        True, False, False, False, True, False, False, True
    )
    player.record_input_frame(
        100,
        walking,
        ORIENTATION,
        received_server_tick=500,
    )
    asyncio.run(player.simulate_tick(TICK_DT))

    player.record_input_frame(
        101,
        jump_sprinting,
        ORIENTATION,
        received_server_tick=502,
    )
    asyncio.run(player.simulate_tick(TICK_DT))
    player.record_input_frame(
        102,
        jump_sprinting,
        ORIENTATION,
        received_server_tick=503,
    )
    asyncio.run(player.simulate_tick(TICK_DT))
    assert player.last_applied_input_loop == 102
    assert simulated == [
        (False, False, False, None, None),
        (True, False, False, 100, 500),
        (True, True, True, 101, 502),
    ]


def test_crouch_uses_current_frame_while_locomotion_stays_latched() -> None:
    """Crouch mutates the retail history anchor before native physics.

    GameScene calls ``Character.set_crouch`` before Character records
    ``movement_history[L]``.  Unlike ordinary buttons, packet L's crouch bit
    therefore belongs to history L, while movement/jump still use L-1.
    """
    player, _ = make_player()
    simulated: list[tuple[bool, bool, bool, bool]] = []

    async def record_update(_dt: float) -> None:
        simulated.append((
            player.input.up,
            player.input.sprint,
            player.input.jump,
            player.input.crouch,
        ))

    player.update = record_update
    walking = (True, False, False, False, False, False, False, False)
    crouch_jump_sprint = (
        True, False, False, False, True, True, False, True
    )
    released_crouch = (
        True, False, False, False, False, False, False, True
    )

    player.record_input_frame(100, walking, ORIENTATION)
    asyncio.run(player.simulate_tick(TICK_DT))
    player.record_input_frame(101, crouch_jump_sprint, ORIENTATION)
    asyncio.run(player.simulate_tick(TICK_DT))
    player.record_input_frame(102, released_crouch, ORIENTATION)
    asyncio.run(player.simulate_tick(TICK_DT))

    assert simulated == [
        (False, False, False, False),
        (True, False, False, True),
        (True, True, True, False),
    ]


def test_crouch_entry_shifts_the_current_ack_anchor_by_point_nine() -> None:
    """The authoritative row for a crouch packet includes its immediate Z."""
    standing, _ = make_player()
    crouching, _ = make_player()
    idle = (False,) * 8
    crouched = (False, False, False, False, False, True, False, False)

    for player in (standing, crouching):
        player.record_input_frame(100, idle, ORIENTATION)
        asyncio.run(player.simulate_tick(TICK_DT))

    # Mirror the handler exposing packet 101 immediately; simulate_tick must
    # still compose the current crouch bit with the latched locomotion bits.
    standing.record_input_frame(101, idle, ORIENTATION)
    standing.update_input(*idle)
    asyncio.run(standing.simulate_tick(TICK_DT))
    crouching.record_input_frame(101, crouched, ORIENTATION)
    crouching.update_input(*crouched)
    asyncio.run(crouching.simulate_tick(TICK_DT))

    assert crouching.last_applied_input_loop == 101
    assert crouching.input.crouch is True
    # Compare against the identical standing tick so spawn-settle gravity is
    # cancelled; the remaining difference is the retail eye-height mutation.
    assert crouching.z - standing.z == pytest.approx(0.9, abs=2e-5)


def test_real_jump_physics_is_invariant_to_starvation_and_label_skips() -> None:
    """Transport gaps must not stretch or lose a one-frame jump pulse.

    The two players consume the same three observed ClientData states.  The
    second stream uses non-contiguous retail loop labels, has two empty server
    ticks, and then receives both transitions in one burst.  Empty ticks freeze
    the acknowledged state, so both native world objects must finish
    bit-for-bit-equivalent physics despite the different packet cadence.
    """
    baseline, _ = make_player()
    delayed, _ = make_player()
    # The float32 stock movebox takes four frames to settle the exact spawn
    # contact plane; start the transport comparison from the stable ground
    # state so the pulse exercises jump rather than the spawn settle.
    for _ in range(4):
        asyncio.run(baseline.update(TICK_DT))
        asyncio.run(delayed.update(TICK_DT))
    idle = (False,) * 8
    jump_pulse = (
        False, False, False, False, True, False, False, False
    )

    def receive(player, loop: int, flags: tuple[bool, ...]) -> None:
        # Mirror handle_client_data(): expose the newest buttons immediately,
        # while movement consumes the buffered frame through its calibrated
        # latch.  This catches a pulse being overwritten by a later packet.
        player.record_input_frame(loop, flags, ORIENTATION)
        player.update_input(*flags)

    receive(baseline, 100, idle)
    asyncio.run(baseline.simulate_tick(TICK_DT))
    receive(baseline, 101, jump_pulse)
    asyncio.run(baseline.simulate_tick(TICK_DT))
    receive(baseline, 102, idle)
    asyncio.run(baseline.simulate_tick(TICK_DT))

    receive(delayed, 100, idle)
    asyncio.run(delayed.simulate_tick(TICK_DT))
    asyncio.run(delayed.simulate_tick(TICK_DT))
    asyncio.run(delayed.simulate_tick(TICK_DT))
    # Both transitions arrive in one drain.  The handler-visible state is now
    # idle again, but the buffered loop 102 must retain the jump pulse.
    receive(delayed, 102, jump_pulse)
    receive(delayed, 104, idle)
    asyncio.run(delayed.simulate_tick(TICK_DT))
    asyncio.run(delayed.simulate_tick(TICK_DT))

    assert baseline.last_applied_input_loop == 102
    assert delayed.last_applied_input_loop == 104
    assert delayed.position == pytest.approx(baseline.position, abs=1e-6)
    assert delayed.velocity == pytest.approx(baseline.velocity, abs=1e-6)
    assert delayed.airborne is baseline.airborne is True
    assert delayed._applied_input_flags == baseline._applied_input_flags
    assert delayed._pending_packet_flags == baseline._pending_packet_flags


def test_orientation_transition_uses_current_observed_frame() -> None:
    player, _ = make_player()
    simulated = []

    async def record_update(_dt: float) -> None:
        simulated.append((player.o_x, player.o_y, player.o_z))

    player.update = record_update
    first = (1.0, 0.0, 0.0)
    turned = (0.0, 1.0, 0.0)
    player.record_input_frame(100, FORWARD, first)
    asyncio.run(player.simulate_tick(TICK_DT))
    player.record_input_frame(101, FORWARD, turned)
    asyncio.run(player.simulate_tick(TICK_DT))
    player.record_input_frame(102, FORWARD, turned)
    asyncio.run(player.simulate_tick(TICK_DT))

    assert simulated == [first, turned, turned]


def test_buffered_action_state_cannot_leak_from_a_future_drained_frame() -> None:
    """A future hover bit must not activate jetpack on an older physics loop."""

    player, _ = make_player()
    simulated: list[bool] = []

    async def record_update(_dt: float) -> None:
        simulated.append(bool(player.input.hover))

    player.update = record_update
    action_idle = (False,) * 9
    action_hover = (False,) * 7 + (True, False)
    player.record_input_frame(
        100,
        FORWARD,
        ORIENTATION,
        action_flags=action_idle,
    )
    player.record_input_frame(
        101,
        FORWARD,
        ORIENTATION,
        action_flags=action_hover,
    )

    # Mirrors tick-start drain having already exposed the newest packet.
    player.input.hover = True
    asyncio.run(player.simulate_tick(TICK_DT))
    asyncio.run(player.simulate_tick(TICK_DT))

    assert simulated == [False, True]


def test_input_arrival_interval_does_not_drive_physics_dt() -> None:
    """ENet dequeue cadence is not the retail client's rendered-frame dt.

    Live A/B evidence made previously bit-exact straight sprint drift by 0.26
    blocks when a 20 ms network interarrival was passed into physics. Arrival
    timestamps are transport telemetry only; observed frames retain fixed dt.
    """
    player, _ = make_player()
    player.record_input_frame(
        100, FORWARD, ORIENTATION, received_at=10.000
    )
    assert _consume(player) == pytest.approx([TICK_DT])

    player.record_input_frame(
        101, FORWARD, ORIENTATION, received_at=10.020
    )

    assert _consume(player) == pytest.approx([TICK_DT])
    assert player.last_applied_input_loop == 101


def test_batched_arrivals_fall_back_to_fixed_dt_including_burst_head() -> None:
    """Transport bunching cannot become one long frame plus near-zero frames."""
    player, _ = make_player()
    player.record_input_frame(
        100, FORWARD, ORIENTATION, received_at=20.000
    )
    assert _consume(player) == pytest.approx([TICK_DT])

    # The first packet looks 60 ms late in isolation, but the next timestamp
    # proves these are separately rendered frames delivered as one burst.
    player.record_input_frame(
        101, FORWARD, ORIENTATION, received_at=20.0600
    )
    player.record_input_frame(
        102, FORWARD, ORIENTATION, received_at=20.0601
    )

    assert _consume(player) == pytest.approx([TICK_DT])
    assert player.last_applied_input_loop == 101
    assert _consume(player) == pytest.approx([TICK_DT])
    assert player.last_applied_input_loop == 102


def test_long_input_arrival_delay_without_starvation_uses_fixed_dt() -> None:
    player, _ = make_player()
    player.record_input_frame(
        100, FORWARD, ORIENTATION, received_at=30.000
    )
    assert _consume(player) == pytest.approx([TICK_DT])

    player.record_input_frame(
        105, FORWARD, ORIENTATION, received_at=31.000
    )

    assert _consume(player) == pytest.approx([TICK_DT])
    assert player.last_applied_input_loop == 105


def test_invalid_or_regressing_input_arrival_time_uses_fixed_dt() -> None:
    player, _ = make_player()
    player.record_input_frame(
        100, FORWARD, ORIENTATION, received_at=40.000
    )
    assert _consume(player) == pytest.approx([TICK_DT])

    player.record_input_frame(
        101, FORWARD, ORIENTATION, received_at=39.000
    )

    assert _consume(player) == pytest.approx([TICK_DT])
    assert player.last_applied_input_loop == 101


def test_replication_catchup_crossing_cadence_boundary_sends_latest_once() -> None:
    """A catch-up batch ending on an odd loop must not lose its 30 Hz send.

    ``SimulationRuntime`` broadcasts only after the entire catch-up batch.  A
    modulo-only gate misses loop 4 when a batch advances 3 -> 5, stretching the
    WorldUpdate interval to three ticks.  Repeated calls in bucket ``5 // 2``
    must still be deduplicated.
    """
    sent: list[tuple[bytes, bool]] = []
    player = SimpleNamespace(
        id=0,
        last_applied_input_loop=100,
        wu_ack_loop=0,
        is_block_tool=lambda: False,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append((data, reliable)),
    )
    config = SimpleNamespace(
        broadcast_world_updates=True,
        worldupdate_self_row_interval=2,
        worldupdate_loop_offset=0,
        worldupdate_include_self=True,
        debug_selfrow=False,
    )
    metrics = SimpleNamespace(record_world_packet=lambda *_args: None)
    server = SimpleNamespace(
        config=config,
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        metrics=metrics,
        build_world_update_data=lambda **_kwargs: b"wu",
    )
    replication = ReplicationService(server)

    replication.broadcast_world_updates()
    assert sent == [(b"wu", False)]

    # The scheduler caught up across loop 4 but invokes replication at loop 5.
    server.loop_count = 5
    player.last_applied_input_loop = 103
    replication.broadcast_world_updates()
    assert sent == [(b"wu", False), (b"wu", False)]

    # Same cadence bucket: no duplicate snapshot.
    replication.broadcast_world_updates()
    assert sent == [(b"wu", False), (b"wu", False)]


def test_no_self_rows_exclude_only_each_recipient_from_its_snapshot() -> None:
    """Owner prediction must not remove the owner from observer snapshots."""
    sent: dict[int, list[bytes]] = {0: [], 1: []}
    players = {
        player_id: SimpleNamespace(
            id=player_id,
            last_applied_input_loop=100,
            wu_ack_loop=0,
            is_block_tool=lambda: False,
        )
        for player_id in sent
    }
    connections = {
        player_id: SimpleNamespace(
            in_game=True,
            player=players[player_id],
            send=lambda data, reliable=False, player_id=player_id: sent[
                player_id
            ].append(data),
        )
        for player_id in sent
    }
    build_calls: list[int | None] = []

    def build_world_update_data(*, exclude_player_id=None, **_kwargs) -> bytes:
        build_calls.append(exclude_player_id)
        return f"exclude:{exclude_player_id}".encode()

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_self_row_interval=2,
            worldupdate_loop_offset=0,
            worldupdate_include_self=False,
            debug_selfrow=False,
        ),
        connections=connections,
        players=players,
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )

    ReplicationService(server).broadcast_world_updates()

    assert build_calls == [0, 1]
    assert sent[0] == [b"exclude:0"]
    assert sent[1] == [b"exclude:1"]


def test_self_rows_refresh_anchor_at_worldupdate_cadence() -> None:
    """The owner anchor must stay fresh enough for jump correction."""
    sent: list[bytes] = []
    player = SimpleNamespace(
        id=0,
        last_applied_input_loop=100,
        wu_ack_loop=0,
        is_block_tool=lambda: False,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append(data),
    )
    calls: list[tuple[int | None, int | None, int | None]] = []

    def build_world_update_data(
        *, exclude_player_id=None, loop_count_override=None,
        local_player_id=None,
    ) -> bytes:
        calls.append(
            (exclude_player_id, loop_count_override, local_player_id)
        )
        return (
            f"{exclude_player_id}:{loop_count_override}:{local_player_id}"
        ).encode()

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            debug_selfrow=False,
        ),
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )
    replication = ReplicationService(server)

    replication.broadcast_world_updates()
    server.loop_count = 4
    player.last_applied_input_loop = 102
    replication.broadcast_world_updates()
    server.loop_count = 6
    player.last_applied_input_loop = 104
    replication.broadcast_world_updates()

    # The WorldUpdate header is the global server clock.  Owner prediction is
    # paired by that player's row pong (player.wu_ack_loop), not this header.
    assert calls == [
        (None, 2, None),
        (None, 4, None),
        (None, 6, None),
    ]
    assert sent == [
        b"None:2:None",
        b"None:4:None",
        b"None:6:None",
    ]


def test_owner_anchor_changes_only_after_the_snapshot_is_queued() -> None:
    """A merely built row must not become a possible retail launch anchor."""
    observed_during_send: list[tuple[float, float, float]] = []
    recorded: list[
        tuple[
            int,
            tuple[float, float, float],
            tuple[float, float, float],
            int,
        ]
    ] = []
    player = SimpleNamespace(
        id=0,
        last_applied_input_loop=100,
        wu_ack_loop=0,
        last_advertised_owner_position=(10.0, 20.0, 30.0),
        world_update_snapshot=lambda: (
            (11.0, 22.0, 33.0),
            (1.0, 0.0, 0.0),
            (0.5, 0.25, -0.125),
        ),
    )

    def record_owner_anchor(
        stamp, position, velocity, *, queued_server_tick
    ) -> None:
        recorded.append((stamp, position, velocity, queued_server_tick))
        player.last_advertised_owner_position = position

    player.record_owner_anchor = record_owner_anchor

    def send(_data, reliable=False) -> None:
        observed_during_send.append(player.last_advertised_owner_position)

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            debug_selfrow=False,
        ),
        connections={
            object(): SimpleNamespace(
                in_game=True,
                player=player,
                send=send,
            )
        },
        players={0: player},
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=lambda **_kwargs: b"wu",
    )

    ReplicationService(server).broadcast_world_updates()

    assert observed_during_send == [(10.0, 20.0, 30.0)]
    assert player.last_advertised_owner_position == (11.0, 22.0, 33.0)
    assert recorded == [
        (100, (11.0, 22.0, 33.0), (0.5, 0.25, -0.125), 2)
    ]


def test_jetpack_transition_metadata_records_the_post_send_boundary() -> None:
    """Transition evidence must point at the newly queued owner anchor.

    Inputs accepted before the reliable WorldUpdate cannot prove that retail
    consumed its jetpack bit.  Pin both sides of that causal boundary while
    keeping ordinary owner rows silent in the transition-only diagnostic.
    """
    player, connection = make_player()
    connection.server.loop_count = 77
    player.record_input_frame(100, FORWARD, ORIENTATION)
    player.record_input_frame(101, FORWARD, ORIENTATION)
    latest_input_owner_sequence = max(
        frame.received_owner_sequence
        for frame in player.input_history.values()
    )
    replication = ReplicationService(connection.server)

    replication._record_owner_row(player, 100)
    assert player.last_jetpack_transition_debug == {}

    player.jetpack_active = True
    replication._record_owner_row(player, 101, transition=True)

    metadata = player.last_jetpack_transition_debug
    assert metadata == {
        "active": True,
        "stamp": 101,
        "sent_input_receive_sequence": 2,
        "sent_owner_sequence": player._owner_anchor_history[-1].queued_owner_sequence,
        "buffered_input_count": 2,
        "server_loop": 77,
    }
    assert metadata["sent_owner_sequence"] > latest_input_owner_sequence


def test_jetpack_transition_metadata_persists_exact_physics_start() -> None:
    """The 60 Hz handoff event must survive the 10 Hz parity sampler."""
    player, connection = make_player()
    connection.server.loop_count = 81
    player.record_owner_anchor(
        stamp=500,
        position=player.position,
        queued_owner_sequence=100,
    )
    player.note_jetpack_transition_sent(True, stamp=500)
    player._current_input_receive_sequence = 44
    player._current_input_owner_sequence = 107
    player._applied_input_source_loop = 498

    player._note_jetpack_physics_started()
    player._current_input_receive_sequence = 45
    player._note_jetpack_physics_started()

    assert player.last_jetpack_transition_debug == {
        "active": True,
        "stamp": 500,
        "sent_input_receive_sequence": 0,
        "sent_owner_sequence": 100,
        "buffered_input_count": 0,
        "server_loop": 81,
        "physics_started_input_receive_sequence": 44,
        "physics_started_owner_sequence": 107,
        "physics_started_source_loop": 498,
        "physics_started_server_loop": 81,
    }


def test_replication_forgets_cadence_and_transition_state_before_id_reuse():
    """A reconnect reusing an id must receive its first owner row promptly."""
    server = SimpleNamespace()
    replication = ReplicationService(server)
    replication._last_self_row_loop[3] = 900
    replication._last_advertised_jetpack_active[3] = True
    replication._jetpack_owner_handoff_deadline[3] = 912
    replication._jetpack_owner_handoff_target[3] = True
    replication._jetpack_owner_release_settle_deadline[3] = 906

    replication.forget_player(3)

    assert 3 not in replication._last_self_row_loop
    assert 3 not in replication._last_advertised_jetpack_active
    assert 3 not in replication._jetpack_owner_handoff_deadline
    assert 3 not in replication._jetpack_owner_handoff_target
    assert 3 not in replication._jetpack_owner_release_settle_deadline


def test_spawn_resets_owner_replication_state_for_the_new_life():
    player, connection = make_player()
    forgotten: list[int] = []
    connection.server.replication = SimpleNamespace(
        forget_player=forgotten.append
    )

    player.spawn(*player.position)

    assert forgotten == [player.id]


def test_airborne_self_rows_use_bounded_reduced_cadence() -> None:
    """Airborne smoothing is throttled, never disabled indefinitely."""

    sent: list[bytes] = []
    player = SimpleNamespace(
        id=0,
        airborne=True,
        last_applied_input_loop=100,
        wu_ack_loop=0,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append(data),
    )
    calls: list[int | None] = []

    def build_world_update_data(
        *, exclude_player_id=None, loop_count_override=None,
        local_player_id=None,
    ) -> bytes:
        calls.append(loop_count_override)
        return b"wu"

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_airborne_self_row_interval=6,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            debug_selfrow=False,
        ),
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )
    replication = ReplicationService(server)

    for loop_count in (2, 4, 6, 8):
        server.loop_count = loop_count
        player.last_applied_input_loop = 98 + loop_count
        replication.broadcast_world_updates()

    # Observer snapshots still transmit every 30 Hz bucket; only the owner's
    # local row is absent from the two middle packets.
    assert calls == [2, 4, 6, 8]
    assert sent == [b"wu", b"wu", b"wu", b"wu"]


def test_jetpack_activation_sends_an_immediate_owner_row_off_cadence() -> None:
    """The owner must learn about thrust before another predicted frame.

    Retail does not report a separate jetpack-active bit in ClientData.  The
    authoritative transition is carried back in WorldUpdate action bit 0x04.
    Waiting for the next 30 Hz/airborne row lets the server apply thrust while
    the owner still applies gravity, which produces a vertical ADJUST.
    """
    sent: list[tuple[bytes, bool]] = []
    flushes: list[str] = []
    player = SimpleNamespace(
        id=0,
        airborne=True,
        jetpack_active=False,
        last_applied_input_loop=100,
        wu_ack_loop=0,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append((data, reliable)),
    )
    calls: list[tuple[int | None, int | None]] = []

    def build_world_update_data(
        *, exclude_player_id=None, loop_count_override=None,
        local_player_id=None,
    ) -> bytes:
        calls.append((loop_count_override, local_player_id))
        return f"{loop_count_override}:{local_player_id}".encode()

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_airborne_self_row_interval=6,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            debug_selfrow=False,
        ),
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        host=SimpleNamespace(flush=lambda: flushes.append("flush")),
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )
    replication = ReplicationService(server)

    # Establish the last advertised inactive state on an ordinary cadence row.
    replication.broadcast_world_updates()
    assert sent == [(b"2:None", False)]

    # Activation occurs on loop 3, where an ordinary WorldUpdate is not due.
    player.jetpack_active = True
    player.last_applied_input_loop = 101
    server.loop_count = 3
    replication.broadcast_world_updates()

    assert sent == [(b"2:None", False), (b"3:0", True)]
    assert calls == [(2, None), (3, 0)]
    assert player.wu_ack_loop == 101
    assert flushes == ["flush"]

    # Repeated service calls in the same state must not duplicate the packet.
    replication.broadcast_world_updates()
    assert sent == [(b"2:None", False), (b"3:0", True)]


def test_jetpack_handoff_excludes_only_owner_row_during_active_thrust() -> None:
    """Independent native clocks must not reconcile in the middle of flight."""
    sent: list[tuple[bytes, bool]] = []
    player = SimpleNamespace(
        id=0,
        airborne=True,
        jetpack_active=False,
        last_applied_input_loop=100,
        wu_ack_loop=0,
        _input_receive_sequence=10,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append((data, reliable)),
    )

    def build_world_update_data(
        *, exclude_player_id=None, loop_count_override=None,
        local_player_id=None,
    ) -> bytes:
        return (
            f"exclude={exclude_player_id}:local={local_player_id}:"
            f"loop={loop_count_override}"
        ).encode()

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_airborne_self_row_interval=2,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            jetpack_owner_handoff_input_frames=2,
            debug_selfrow=False,
        ),
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )
    replication = ReplicationService(server)

    replication.broadcast_world_updates()
    player.jetpack_active = True
    player.last_applied_input_loop = 101
    server.loop_count = 3
    replication.broadcast_world_updates()

    # One later accepted input is still inside the two-frame handoff. The
    # owner is excluded, while this full server snapshot remains available to
    # every observer group.
    player._input_receive_sequence = 11
    player.last_applied_input_loop = 102
    server.loop_count = 4
    replication.broadcast_world_updates()

    # Accepted input 12 reaches the fallback deadline, but the pack remains
    # active. The owner row stays excluded; observer snapshots are unchanged.
    player._input_receive_sequence = 12
    player.last_applied_input_loop = 104
    server.loop_count = 6
    replication.broadcast_world_updates()

    assert sent == [
        (b"exclude=None:local=None:loop=2", False),
        (b"exclude=None:local=0:loop=3", True),
        (b"exclude=0:local=None:loop=4", False),
        (b"exclude=0:local=None:loop=6", False),
    ]


def test_active_jetpack_handoff_clears_after_pack_becomes_inactive() -> None:
    server = SimpleNamespace(
        config=SimpleNamespace(jetpack_owner_handoff_input_frames=2)
    )
    replication = ReplicationService(server)
    player = SimpleNamespace(
        id=7,
        jetpack_active=True,
        _input_receive_sequence=10,
    )

    replication._begin_jetpack_owner_handoff(player)
    player._input_receive_sequence = 100
    assert replication._jetpack_owner_handoff_active(player) is True

    player.jetpack_active = False
    assert replication._jetpack_owner_handoff_active(player) is False


def test_exhaustion_handoff_waits_for_release_and_settled_ground() -> None:
    """Held SPACE may not resume owner correction near fuel-zero landing."""
    server = SimpleNamespace(
        config=SimpleNamespace(
            jetpack_owner_handoff_input_frames=2,
            jetpack_owner_release_handoff_input_frames=20,
        )
    )
    replication = ReplicationService(server)
    player = SimpleNamespace(
        id=4,
        jetpack_id=68,
        jetpack_active=False,
        airborne=True,
        input=SimpleNamespace(jump=True, hover=False),
        _input_receive_sequence=30,
    )

    replication._begin_jetpack_owner_handoff(player)
    player._input_receive_sequence = 35
    assert replication._jetpack_owner_handoff_active(player) is True

    player.airborne = False
    assert replication._jetpack_owner_handoff_active(player) is True

    player.input.jump = False
    assert replication._jetpack_owner_handoff_active(player) is True
    player._input_receive_sequence = 36
    assert replication._jetpack_owner_handoff_active(player) is True
    player._input_receive_sequence = 37
    assert replication._jetpack_owner_handoff_active(player) is False

    player.airborne = True
    player._input_receive_sequence = 40
    replication._begin_jetpack_owner_handoff(player)
    player.input.jump = False
    assert replication._jetpack_owner_handoff_active(player) is True
    player._input_receive_sequence = 42
    assert replication._jetpack_owner_handoff_active(player) is True
    player.airborne = False
    assert replication._jetpack_owner_handoff_active(player) is False


def test_jetpack_release_also_sends_one_immediate_owner_row() -> None:
    """Clearing action bit 0x04 is transition-critical for owner gravity."""
    sent: list[tuple[bytes, bool]] = []
    player = SimpleNamespace(
        id=0,
        airborne=True,
        jetpack_active=True,
        last_applied_input_loop=200,
        wu_ack_loop=0,
    )
    connection = SimpleNamespace(
        in_game=True,
        player=player,
        send=lambda data, reliable=False: sent.append((data, reliable)),
    )

    def build_world_update_data(
        *, exclude_player_id=None, loop_count_override=None,
        local_player_id=None,
    ) -> bytes:
        return f"{loop_count_override}:{local_player_id}".encode()

    server = SimpleNamespace(
        config=SimpleNamespace(
            broadcast_world_updates=True,
            worldupdate_broadcast_interval=2,
            worldupdate_self_row_interval=2,
            worldupdate_airborne_self_row_interval=6,
            worldupdate_loop_offset=0,
            worldupdate_include_self=True,
            debug_selfrow=False,
        ),
        connections={object(): connection},
        players={0: player},
        loop_count=2,
        metrics=SimpleNamespace(record_world_packet=lambda *_args: None),
        build_world_update_data=build_world_update_data,
    )
    replication = ReplicationService(server)

    replication.broadcast_world_updates()
    assert sent == [(b"2:None", True)]

    player.jetpack_active = False
    player.last_applied_input_loop = 201
    server.loop_count = 3
    replication.broadcast_world_updates()
    replication.broadcast_world_updates()

    assert sent == [(b"2:None", True), (b"3:0", True)]


def test_retail_jetpack_release_waits_for_key_up_and_ground_settle() -> None:
    """The inactive owner row may not reconcile a mid-air fuel transition."""
    player = SimpleNamespace(
        id=4,
        airborne=True,
        jetpack_id=66,
        jetpack_active=False,
        last_applied_input_loop=200,
        input=SimpleNamespace(jump=True, hover=False),
        _input_receive_sequence=30,
    )
    connection = SimpleNamespace(in_game=True, player=player)
    server = SimpleNamespace(
        config=SimpleNamespace(
            worldupdate_include_self=True,
            jetpack_owner_handoff_input_frames=2,
        )
    )
    replication = ReplicationService(server)
    replication._last_advertised_jetpack_active[player.id] = True
    replication._jetpack_owner_handoff_deadline[player.id] = 32
    replication._jetpack_owner_handoff_target[player.id] = True

    assert replication._jetpack_transition_connections((connection,)) == []
    assert replication._jetpack_owner_handoff_active(player) is True

    # Landing alone is insufficient while the physical activation key remains
    # held; this is the fuel-zero auto-jump edge from the retail report.
    player.airborne = False
    player._input_receive_sequence = 40
    assert replication._jetpack_transition_connections((connection,)) == []

    # Key-up starts a short accepted-input settle window.
    player.input.jump = False
    assert replication._jetpack_transition_connections((connection,)) == []
    player._input_receive_sequence = 41
    assert replication._jetpack_transition_connections((connection,)) == []
    player._input_receive_sequence = 42
    assert replication._jetpack_transition_connections((connection,)) == [
        connection
    ]
