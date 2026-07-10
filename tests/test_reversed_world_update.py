import asyncio
import math
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
from aoslib.vxl import VXL
from shared.bytes import ByteReader
from shared.packet import ClientData, PositionData, StateData, WorldUpdate

from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from server.world_manager import WorldManager


TEST_COLOR = 0x7F00FF00
GROUND_Z = 62


def tofixed_orientation(value: float) -> int:
    magnitude = min(int(round(abs(value) * 8192.0)), 0x7FFF)
    if value < 0.0:
        return magnitude | 0x8000
    return magnitude


def tofixed(value: float) -> int:
    scaled = int(value * 64.0 + 0.5)
    magnitude = scaled if scaled >= 0 else -scaled
    magnitude = min(magnitude, 0x7FFF)
    if scaled < 0:
        return magnitude | 0x8000
    return magnitude


def make_world_manager():
    world_manager = WorldManager(ServerConfig())
    world_manager.map = VXL(-1, b"", 0, 2)
    world_manager.map_name = "test_flat"
    world_manager._refresh_world()
    return world_manager


class DummyConnection:
    def __init__(self, server=None, player=None):
        self.server = server
        self.player = player
        self.sent_packets = []
        # Represents a fully-joined, in-game client (gameplay broadcasts are
        # gated on this; a connecting client gets none until its first
        # ClientData). See server.main.broadcast.
        self.in_game = True

    def send(self, data, reliable=True, prefix=0x30):
        self.sent_packets.append(data)


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


def make_player(server=None, cell_x=100, cell_y=100):
    if server is None:
        server = SimpleNamespace(world_manager=make_world_manager(), players={})
    elif not hasattr(server, "players"):
        server.players = {}
    flatten_patch(server.world_manager, cell_x, cell_y)
    connection = DummyConnection(server)
    player = Player(0, "KikoTs", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    server.players[player.id] = player
    player.spawn(*make_spawn(server.world_manager, cell_x, cell_y))
    return player, connection


def advance_player(player, seconds, step=1.0 / 60.0):
    ticks = max(1, int(seconds / step))
    for _ in range(ticks):
        asyncio.run(player.update(step))


def test_world_update_serializes_reference_layout():
    packet = WorldUpdate()
    packet.loop_count = 123
    packet[7] = (
        (10.0, 20.0, 30.0),
        (1.0, 0.0, 0.0),
        (0.5, 1.5, -2.0),
        11,
        22,
        95,
        0x31,
        0x42,
        0x0A,
        C.MINIGUN_TOOL,
    )

    data = bytes(packet.generate())
    assert len(data) == 67
    assert data[0] == 2
    row_start = 7
    assert data[row_start + 46] == 0x42  # action
    assert data[row_start + 47] == 0x0A  # disguise + touching-goo state
    assert data[row_start + 48] == C.MINIGUN_TOOL
    assert data[row_start + 49] == 0xFF  # no pickup

    reader = ByteReader(data[1:])
    assert reader.read_int() == 123
    assert reader.read_short() == 1
    assert reader.read_byte() == 7
    assert reader.read_float() == 10.0
    assert reader.read_float() == 20.0
    assert reader.read_float() == 30.0
    assert reader.read_float() == 1.0
    assert reader.read_float() == 0.0
    assert reader.read_float() == 0.0
    assert reader.read_float() == 0.5
    assert reader.read_float() == 1.5
    assert reader.read_float() == -2.0
    assert reader.read_short() == 11
    assert reader.read_int() == 22
    assert reader.read_short() == 95
    assert reader.read_byte() == 0x31
    assert reader.read_byte() == 0x42
    # The byte after the action byte is a per-player STATE bitfield the client
    # bit-splits (parachute/disguise/goo), NOT the tool id — writing the raw
    # tool id here made weapon switches toggle those states.
    assert reader.read_byte() == 0x0A
    assert reader.read_byte() == C.MINIGUN_TOOL
    # Pickup id byte: 0xFF (-1) = "no pickup". Sending 0 crashes the client
    # minimap (PICKUPS[0] KeyError) — measured on the stock Steam client.
    assert reader.read_byte() == 0xFF
    assert reader.read_byte() == 0
    assert reader.read_short() == 0
    assert reader.read_short() == 0
    assert reader.read_byte() == 0
    assert reader.read_short() == 0
    assert reader.read_short() == 0


def test_world_update_read_parses_player_updates_and_counts():
    packet = WorldUpdate()
    packet.loop_count = 77
    packet[1] = (
        (303.5, 62.5, 170.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        0,
        0,
        100,
        0x11,
        0x02,
        0x08,
        C.RIFLE_TOOL,
    )

    parsed = WorldUpdate(ByteReader(bytes(packet.generate())[1:]))
    update = parsed.player_updates[1]
    position, orientation, velocity, movement, ping, health, input_flags, action_flags, state_flags, tool = update

    assert parsed.loop_count == 77
    assert len(parsed.updated_entities) == 0
    assert len(parsed.rocket_turrets) == 0
    assert position == (303.5, 62.5, 170.0)
    assert orientation == (1.0, 0.0, 0.0)
    assert velocity == (0.0, 0.0, 0.0)
    assert movement == 0
    assert ping == 0
    assert health == 100
    assert input_flags == 0x11
    assert action_flags == 0x02
    assert state_flags == 0x08
    assert tool == C.RIFLE_TOOL


def test_world_update_round_trips_its_serialized_bytes():
    packet = WorldUpdate()
    packet.loop_count = 1
    packet[0] = (
        (62.5, 302.5, 170.0),
        (0.0, 0.0, 255.5),
        (0.0, 0.0, 0.0),
        0,
        0,
        100,
        0,
        0,
        0,
        C.MINIGUN_TOOL,
    )

    raw = bytes(packet.generate())
    parsed = WorldUpdate(ByteReader(raw[1:]))

    assert bytes(parsed.generate()) == raw


def test_position_data_round_trip():
    packet = PositionData()
    packet.set(12.5, 48.25, 99.0)

    parsed = PositionData(ByteReader(bytes(packet.generate())[1:]))
    assert (parsed.x, parsed.y, parsed.z) == (12.5, 48.25, 99.0)


def test_packet_handler_reads_live_float_position_data():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)
    start = player.position

    raw_packet = bytes([PositionData.id]) + struct.pack(
        "<fff",
        start[0] + 4.0,
        start[1] - 2.0,
        start[2] + 1.0,
    )

    asyncio.run(PacketHandler(server).handle(player, raw_packet))

    assert player.last_reported_position == (start[0] + 4.0, start[1] - 2.0, start[2] + 1.0)
    assert player.last_position_drift > 0.0
    assert player.position == start


def test_packet_handler_reads_fixed_position_data_fallback():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)

    raw_packet = bytes([PositionData.id]) + struct.pack(
        "<HHH",
        tofixed(player.x + 1.5),
        tofixed(player.y + 0.5),
        tofixed(player.z + 0.25),
    )

    asyncio.run(PacketHandler(server).handle(player, raw_packet))

    assert player.last_reported_position == (player.x + 1.5, player.y + 0.5, player.z + 0.25)


def test_client_data_updates_player_state_and_flags():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)

    packet = ClientData()
    packet.loop_count = 1
    packet.player_id = 0
    packet.tool_id = C.MINIGUN_TOOL
    packet.o_x = 1.0
    packet.o_y = 0.0
    packet.o_z = 0.0
    packet.ooo = 0
    packet.up = True
    packet.left = True
    packet.jump = True
    packet.primary = True
    packet.zoom = True
    packet.can_display_weapon = True
    packet.hover = True
    packet.weapon_deployment_yaw = 0.0

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.tool == C.MINIGUN_TOOL
    assert player.orientation[0] > 0.99
    assert player.input.jump is True
    assert player.pack_input_flags() == 0x15
    # WorldUpdate action byte uses the client's DISPLAY layout, which remaps
    # zoom/hover/weapon_deployed vs the ClientData SEND layout. ClientData
    # 0x95 (primary|zoom|can_display_weapon|hover) packs to display 0x55
    # (primary 0x01 | can_display 0x10 | zoom 0x40 | hover→jetpack 0x04).
    assert player.pack_action_flags() == 0x51


def test_packet_handler_reads_live_client_data_unsigned_orientation_and_float_yaw():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)

    raw_packet = bytes([ClientData.id]) + struct.pack(
        "<IBBHHHBBBf",
        1,
        0x80 | player.id,
        C.MINIGUN_TOOL,
        tofixed_orientation(0.3768),
        tofixed_orientation(-0.7424),
        tofixed_orientation(-0.6653),
        0,
        0x15,
        0x95,
        12.5,
    )

    asyncio.run(PacketHandler(server).handle(player, raw_packet))

    expected_magnitude = math.sqrt((0.3768 * 0.3768) + (-0.7424 * -0.7424) + (-0.6653 * -0.6653))
    assert math.isclose(player.orientation[0], 0.3768 / expected_magnitude, abs_tol=1e-3)
    assert math.isclose(player.orientation[1], -0.7424 / expected_magnitude, abs_tol=1e-3)
    assert math.isclose(player.orientation[2], -0.6653 / expected_magnitude, abs_tol=1e-3)
    assert player.input.palette_enabled is True
    assert player.input.jump is True
    assert player.pack_input_flags() == 0x15
    # See note above: 0x95 send-layout -> 0x55 display-layout in pack.
    assert player.pack_action_flags() == 0x51


def test_world_update_snapshot_packs_remote_disguise_and_water_state():
    player, _ = make_player()
    player.disguised = True
    player.wade = True

    snapshot = player.world_update_snapshot()

    assert snapshot[-2] == 0x0A
    assert snapshot[-1] == player.tool


def test_repeated_live_client_data_held_jump_launches_on_next_tick():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)
    start_z = player.z

    raw_packet = bytes([ClientData.id]) + struct.pack(
        "<IBBHHHBBBf",
        1,
        player.id,
        C.RIFLE_TOOL,
        tofixed_orientation(1.0),
        tofixed_orientation(0.0),
        tofixed_orientation(0.0),
        0,
        0x10,
        0x00,
        0.0,
    )

    handler = PacketHandler(server)
    asyncio.run(handler.handle(player, raw_packet))
    asyncio.run(handler.handle(player, raw_packet))

    assert player.input.jump is True

    advance_player(player, 1.0 / 60.0)

    assert player.airborne is True
    assert player.z < start_z


def test_client_data_round_trips_across_quadrants():
    vectors = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (-0.5, 0.75, -0.25),
        (-0.707106, -0.707106, 0.0),
    ]

    for vector in vectors:
        packet = ClientData()
        packet.loop_count = 1
        packet.player_id = 0
        packet.tool_id = C.MINIGUN_TOOL
        packet.o_x, packet.o_y, packet.o_z = vector
        packet.ooo = 0
        packet.up = True
        packet.right = True
        packet.weapon_deployment_yaw = 12.5

        parsed = ClientData(ByteReader(bytes(packet.generate())[1:]))

        assert math.isclose(parsed.o_x, vector[0], abs_tol=1e-3)
        assert math.isclose(parsed.o_y, vector[1], abs_tol=1e-3)
        assert math.isclose(parsed.o_z, vector[2], abs_tol=1e-3)
        assert parsed.up is True
        assert parsed.right is True
        assert math.isclose(parsed.weapon_deployment_yaw, 12.5, abs_tol=1e-4)
        assert max(abs(parsed.o_x), abs(parsed.o_y), abs(parsed.o_z)) <= 1.0


def test_client_data_reads_negative_orientation_without_blowup():
    packet = ClientData()
    packet.loop_count = 1
    packet.player_id = 0
    packet.tool_id = C.MINIGUN_TOOL
    packet.o_x = -0.5
    packet.o_y = 0.75
    packet.o_z = -0.25
    packet.ooo = 0
    packet.weapon_deployment_yaw = 12.5

    parsed = ClientData(ByteReader(bytes(packet.generate())[1:]))

    assert math.isclose(parsed.o_x, -0.5, abs_tol=1e-3)
    assert math.isclose(parsed.o_y, 0.75, abs_tol=1e-3)
    assert math.isclose(parsed.o_z, -0.25, abs_tol=1e-3)
    assert math.isclose(parsed.weapon_deployment_yaw, 12.5, abs_tol=1e-4)
    assert max(abs(parsed.o_x), abs(parsed.o_y), abs(parsed.o_z)) <= 1.0


def test_orientation_guard_normalizes_out_of_range_vectors():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))

    player.set_orientation_vector(-3.5, 0.75, -3.75)

    magnitude = (
        player.orientation[0] * player.orientation[0]
        + player.orientation[1] * player.orientation[1]
        + player.orientation[2] * player.orientation[2]
    ) ** 0.5
    assert math.isclose(magnitude, 1.0, abs_tol=1e-6)
    assert max(abs(component) for component in player.orientation) <= 1.0


def test_world_orientation_uses_live_vector_without_upward_clamp():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))

    player.set_orientation_vector(0.3768, -0.7424, -0.6653)

    native_orientation = tuple(player._world_object.orientation)
    for actual, expected in zip(native_orientation, player.orientation):
        assert math.isclose(actual, expected, abs_tol=1e-6)


def test_native_world_position_matches_wire_position_on_spawn():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))

    assert player._world_object is not None
    assert tuple(player._world_object.position) == player.position


def test_player_movement_is_normalized_and_lands_back_on_ground():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))
    start_x, start_y, start_z = player.position
    player.set_orientation_vector(1.0, 0.0, 0.0)

    player.update_input(True, False, False, False, False, False, False, False)
    advance_player(player, 0.5)
    assert player.x > start_x
    assert math.isclose(player.y, start_y, abs_tol=0.25)

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, True, False, False, False, False)
    advance_player(player, 0.5)
    assert player.x > start_x
    assert player.y > start_y

    player.spawn(start_x, start_y, start_z)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(False, False, False, False, True, False, False, False)
    advance_player(player, 0.1)
    assert player.z < start_z

    player.update_input(False, False, False, False, False, False, False, False)
    advance_player(player, 2.0)
    assert player.grounded is True
    assert math.isclose(player.z, start_z, abs_tol=0.25)

    idle_player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))
    start = idle_player.position
    advance_player(idle_player, 0.5)
    assert idle_player.position == start


def test_released_jump_settles_back_to_ground():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))
    start = player.position
    player.update_input(False, False, False, False, True, False, False, False)

    advance_player(player, 0.1)
    mid_z = player.z
    assert mid_z < start[2]

    # Release the key mid-air: the player completes the arc and stays down
    # (held jump would keep bunny-hopping — that's the client behavior,
    # pinned in test_reversed_movement_engine).
    player.update_input(False, False, False, False, False, False, False, False)
    advance_player(player, 2.0)
    assert math.isclose(player.z, start[2], abs_tol=0.25)
    assert player.grounded is True


def test_position_data_logs_drift_without_correcting_position():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)
    start = player.position

    packet = PositionData()
    packet.set(start[0] + 4.0, start[1] - 2.0, start[2] + 1.0)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert math.isclose(player.last_reported_position[0], packet.x, abs_tol=1e-6)
    assert math.isclose(player.last_reported_position[1], packet.y, abs_tol=1e-6)
    assert math.isclose(player.last_reported_position[2], packet.z, abs_tol=1.0 / 64.0)
    assert player.last_position_drift > 0.0
    assert player.position == start


def test_position_data_does_not_rollback_flat_ground_walking():
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    player.update_input(True, False, False, False, False, False, False, False)
    handler = PacketHandler(server)

    positions = [player.x]
    for _ in range(12):
        stale_position = (player.x - 0.4, player.y, player.z)
        raw_packet = bytes([PositionData.id]) + struct.pack("<fff", *stale_position)
        asyncio.run(handler.handle(player, raw_packet))
        advance_player(player, 1.0 / 60.0)
        positions.append(player.x)

    assert all(current > previous for previous, current in zip(positions, positions[1:]))
    assert player.last_position_drift > 0.0


def test_simulate_tick_consumes_only_one_buffered_frame_per_server_tick():
    player, _ = make_player()
    updates = []

    async def record_update(dt):
        updates.append(dt)

    player.update = record_update
    flags = (True, False, False, False, False, False, False, False)
    player.record_input_frame(100, flags, (1.0, 0.0, 0.0))
    player.record_input_frame(101, flags, (1.0, 0.0, 0.0))

    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert len(updates) == 1
    assert player.last_applied_input_loop == 100
    assert sorted(player.input_history) == [101]


def test_simulate_tick_never_reapplies_stale_or_duplicate_input():
    player, _ = make_player()
    applied = []

    async def record_update(dt):
        applied.append((player.last_applied_input_loop, player.input.up))

    player.update = record_update
    forward = (True, False, False, False, False, False, False, False)
    idle = (False, False, False, False, False, False, False, False)
    player.record_input_frame(200, forward, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    player.record_input_frame(199, idle, (1.0, 0.0, 0.0))
    player.record_input_frame(200, idle, (1.0, 0.0, 0.0))
    player.record_input_frame(201, forward, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert applied == [(200, False), (201, True)]
    assert player.last_applied_input_loop == 201
    assert player.input_history == {}


def test_starved_tick_never_changes_position_under_same_ack():
    player, _ = make_player()

    async def move(_dt):
        player.x += 1.0

    player.update = move
    flags = (True, False, False, False, False, False, False, False)
    player.record_input_frame(100, flags, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    ack_before = player.last_applied_input_loop
    position_before = player.position

    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert player.last_applied_input_loop == ack_before
    assert player.position == position_before


def test_bot_without_client_history_still_advances_physics():
    player, _ = make_player()
    player.is_bot = True
    updates = []

    async def record_update(dt):
        updates.append(dt)

    player.update = record_update

    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert updates == [1.0 / 60.0]
    assert player.last_applied_input_loop is None


def test_input_gap_synthesizes_only_the_expected_loop_before_future_frame():
    player, _ = make_player()
    applied = []

    async def record_update(_dt):
        applied.append(player.last_applied_input_loop)

    player.update = record_update
    flags = (True, False, False, False, False, False, False, False)
    player.record_input_frame(100, flags, (1.0, 0.0, 0.0))
    player.record_input_frame(102, flags, (1.0, 0.0, 0.0))

    asyncio.run(player.simulate_tick(1.0 / 60.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert applied == [100, 101]
    assert player.last_applied_input_loop == 101
    assert sorted(player.input_history) == [102]


def test_packet_transition_is_latched_for_the_next_simulated_loop():
    player, _ = make_player()
    simulated_sprint = []

    async def record_update(_dt):
        simulated_sprint.append(player.input.sprint)

    player.update = record_update
    walking = (True, False, False, False, False, False, False, False)
    sprinting = (True, False, False, False, False, False, False, True)
    orientation = (1.0, 0.0, 0.0)
    player.record_input_frame(100, walking, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    # The packet handler exposes the newest state immediately to combat/tool
    # systems before movement reaches this future frame.
    player.update_input(*sprinting)
    player.record_input_frame(101, sprinting, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    player.record_input_frame(102, sprinting, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert player.last_applied_input_loop == 102
    assert simulated_sprint == [False, False, True]
    assert player.input_history == {}


def test_first_post_spawn_packet_is_latched_after_idle_frame():
    player, _ = make_player()
    simulated_forward = []

    async def record_update(_dt):
        simulated_forward.append(player.input.up)

    player.update = record_update
    forward = (True, False, False, False, False, False, False, False)
    orientation = (1.0, 0.0, 0.0)
    player.record_input_frame(100, forward, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    player.record_input_frame(101, forward, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert simulated_forward == [False, True]


def test_large_input_gap_does_not_leak_newest_transition_backwards():
    player, _ = make_player()
    simulated = []

    async def record_update(_dt):
        simulated.append((player.input.sprint, player.input.crouch))

    player.update = record_update
    walking = (True, False, False, False, False, False, False, False)
    sprinting = (True, False, False, False, False, False, False, True)
    crouching = (True, False, False, False, False, True, False, False)
    orientation = (1.0, 0.0, 0.0)
    player.record_input_frame(100, walking, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    # Packet 102 introduces sprint. Both missing 101 and actual 102 still use
    # the previous packet state; synthetic loops never advance the latch.
    player.record_input_frame(102, sprinting, orientation)
    player.record_input_frame(104, crouching, orientation)
    player.update_input(*crouching)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert player.last_applied_input_loop == 103
    assert simulated == [
        (False, False),
        (False, False),
        (False, False),
        (True, False),
    ]


def test_server_broadcasts_world_update_to_connected_clients():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()

    player_one, connection_one = make_player(server, cell_x=100, cell_y=100)
    player_two, connection_two = make_player(server, cell_x=110, cell_y=110)
    player_two.id = 1
    connection_two.player = player_two

    player_one.update_input(True, False, False, False, False, False, False, False)
    advance_player(player_one, 0.5)

    server.players = {
        player_one.id: player_one,
        player_two.id: player_two,
    }
    server.connections = {
        "one": connection_one,
        "two": connection_two,
    }

    data = server.build_world_update_data()
    server.broadcast(data)

    assert connection_one.sent_packets[0][0] == 2
    assert connection_two.sent_packets[0][0] == 2

    parsed = WorldUpdate(ByteReader(connection_one.sent_packets[0][1:]))
    update = parsed.player_updates[player_one.id]
    update_position, _, _, _, _, _, update_input_flags, _, update_state, update_tool = update
    assert update_tool == player_one.tool
    assert update_state == player_one.pack_state_flags()
    assert update_position[0] > player_two.position[0] - 20.0
    assert update_position[0] > player_one.last_reported_position[0] - 0.01
    assert update_input_flags == player_one.pack_input_flags()


def test_round_restart_state_preserves_each_clients_player_id():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()
    player_one, connection_one = make_player(server, cell_x=100, cell_y=100)
    player_two, connection_two = make_player(server, cell_x=110, cell_y=110)
    player_two.id = 1
    connection_two.player = player_two
    server.players = {0: player_one, 1: player_two}
    server.connections = {"one": connection_one, "two": connection_two}

    server.broadcast_state_data()

    state_one = StateData(ByteReader(connection_one.sent_packets[-1][1:]))
    state_two = StateData(ByteReader(connection_two.sent_packets[-1][1:]))
    assert state_one.player_id == 0
    assert state_two.player_id == 1
    assert state_one.has_map_ended == 0
    assert state_two.has_map_ended == 0

