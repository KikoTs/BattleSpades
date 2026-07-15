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
    assert reader.read_short() == 0
    assert reader.read_short() == 0
    assert reader.read_short() == 0
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
        73.5,
        0.0,
        0.0,
    )

    parsed = WorldUpdate(ByteReader(bytes(packet.generate())[1:]))
    update = parsed.player_updates[1]
    (position, orientation, velocity, movement, ping, health, input_flags,
     action_flags, state_flags, tool, jetpack_fuel, spawn_protection_timer,
     weapon_deployment_yaw) = update

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
    assert jetpack_fuel == 73.5
    assert spawn_protection_timer == 0.0
    assert weapon_deployment_yaw == 0.0


def test_engineer_has_one_fuel_short_before_spawn_and_deployment_fields():
    """The second HUD cylinder is artwork, not another wire resource."""

    player, _connection = make_player()
    player.class_id = int(C.CLASS_ENGINEER)
    player.jetpack_id = int(C.JETPACK_ENGINEER)
    player.jetpack_fuel = 64.25

    packet = WorldUpdate()
    packet.loop_count = 91
    packet[player.id] = player.world_update_snapshot()
    raw = bytes(packet.generate())

    # One 56-byte player row, followed immediately by the two zero entity
    # counts. Adding a second fuel value would shift and corrupt this trailer.
    assert len(raw) == 67
    parsed = WorldUpdate(ByteReader(raw[1:]))
    update = parsed.player_updates[player.id]
    assert update[10] == 64.25
    assert update[11] == 0.0  # spawn protection timer
    assert update[12] == 0.0  # weapon deployment yaw
    assert parsed.updated_entities == []
    assert parsed.rocket_turrets == []


def test_riot_shield_remote_state_uses_existing_tool_and_action_fields():
    player, _ = make_player()
    player.set_tool(C.RIOTSHIELD_TOOL, raw=True)
    player.input.can_display_weapon = True
    player.input.primary_fire = True

    packet = WorldUpdate()
    packet.loop_count = 9
    packet[player.id] = player.world_update_snapshot()
    parsed = WorldUpdate(ByteReader(bytes(packet.generate())[1:]))
    update = parsed.player_updates[player.id]

    assert update[9] == C.RIOTSHIELD_TOOL
    assert update[7] & 0x10  # existing can_display_weapon bit: held model
    assert update[7] & 0x01  # existing primary bit: bash animation
    assert parsed.updated_entities == []  # no synthetic shield entity/packet


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


def test_world_update_rocket_turret_wire_is_id_yaw_pitch_only():
    """Stock packet.pyd reads uint16 id + two fixed floats per turret.

    gameScene.pyx then applies tuple[0] as yaw and tuple[1] as pitch.  The
    previous four-short id/x/y/z layout shifted every packet that contained a
    turret and made the client parse past the end of WorldUpdate.
    """
    packet = WorldUpdate()
    packet.loop_count = 5
    packet.rocket_turrets = [(42, 91.25, 12.5)]

    raw = bytes(packet.generate())
    parsed = WorldUpdate(ByteReader(raw[1:]))

    entity_id, yaw, pitch = parsed.rocket_turrets[0]
    assert entity_id == 42
    assert abs(yaw - 91.25) < 1.0 / 64.0
    assert abs(pitch - 12.5) <= 1.0 / 64.0
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
        loop_count=321,
    )
    player, _ = make_player(server)

    packet = ClientData()
    packet.loop_count = 1
    packet.player_id = 0
    packet.tool_id = C.MINIGUN_TOOL
    packet.o_x = 1.0
    packet.o_y = 0.0
    packet.o_z = 0.0
    packet.ooo = 0x9F
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
    assert player.input_history[1].received_server_tick == 321
    assert player.input_history[1].wire_unknown_byte == 0x9F
    assert player.pack_input_flags() == 0x15
    # WorldUpdate action byte uses the client's DISPLAY layout, which remaps
    # zoom/hover/weapon_deployed vs the ClientData SEND layout. ClientData
    # 0x95 (primary|zoom|can_display_weapon|hover) packs to display 0x55
    # (primary 0x01 | can_display 0x10 | zoom 0x40 | hover→jetpack 0x04).
    assert player.pack_action_flags() == 0x51


def test_client_data_never_treats_fire_state_as_a_jetpack_ack():
    """The fire bit remains gameplay state and cannot be a hidden handshake."""
    server = SimpleNamespace(
        config=SimpleNamespace(log_suppress_packets=set()),
        world_manager=make_world_manager(),
        players={},
    )
    player, _ = make_player(server)
    observed = []
    player._jetpack_activation_marker_pending = True
    player.note_jetpack_activation_echo = (
        lambda loop, *, marker_value: observed.append(
            (int(loop), bool(marker_value))
        )
    )

    packet = ClientData()
    packet.loop_count = 321
    packet.player_id = player.id
    packet.tool_id = C.RIFLE_TOOL
    packet.o_x = 1.0
    packet.o_y = 0.0
    packet.o_z = 0.0
    packet.ooo = 0
    packet.is_on_fire = True
    packet.weapon_deployment_yaw = 0.0

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert observed == []


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

    assert snapshot[-6] == 0x0A
    assert snapshot[-5] == player.tool


def test_world_update_snapshot_serializes_authoritative_jetpack_fuel():
    """The first fixed short after pickup is Character.jetpack_fuel.

    Stock gameScene assigns this value every WorldUpdate, so leaving the six
    post-pickup bytes zeroed erases locally initialized fuel on every frame.
    """
    player, _ = make_player()
    player.jetpack_fuel = 73.5

    packet = WorldUpdate()
    packet[player.id] = player.world_update_snapshot()
    raw = bytes(packet.generate())

    row_start = 1 + 4 + 2
    fuel_offset = row_start + 50
    assert raw[fuel_offset:fuel_offset + 2] == struct.pack("<h", int(73.5 * 64 + 0.5))


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
    # Retail restores the complete cached network position on its launch
    # frame.  Airborne/jump_this_frame, rather than a position delta, proves
    # that the held request was consumed by this direct-update fixture.
    assert math.isclose(player.z, start_z, abs_tol=1e-6)
    assert player._world_object.jump_this_frame is True


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
    # The float32 native ground probe settles by less than one thousandth of
    # a block without producing meaningful locomotion.
    assert all(
        math.isclose(actual, expected, abs_tol=1e-3)
        for actual, expected in zip(idle_player.position, start)
    )


def test_player_update_builds_collision_snapshot_once_without_debug_capture():
    player, _ = make_player(SimpleNamespace(world_manager=make_world_manager()))
    player.connection.server.config = SimpleNamespace(movement_debug_capture=False)
    original = player._build_player_collision_positions
    calls = []

    def counted():
        calls.append(1)
        return original()

    player._build_player_collision_positions = counted
    player._capture_native_debug_state = lambda *_args, **_kwargs: (
        (_ for _ in ()).throw(AssertionError("debug capture ran in production tick"))
    )

    asyncio.run(player.update(1.0 / 60.0))

    assert len(calls) == 1


def test_player_collision_snapshot_matches_disabled_same_team_client_rule():
    """InitialInfo=0 means allies must not affect authoritative movement."""

    server = SimpleNamespace(
        world_manager=make_world_manager(),
        players={},
        config=SimpleNamespace(same_team_collision=False),
    )
    player, _ = make_player(server, cell_x=100, cell_y=100)
    ally_connection = DummyConnection(server)
    ally = Player(1, "Ally", player.team, C.RIFLE_TOOL, ally_connection)
    ally_connection.player = ally
    ally.spawn(player.x + 0.25, player.y, player.z)
    server.players = {player.id: player, ally.id: ally}

    assert player._build_player_collision_positions() == []


def test_player_collision_snapshot_keeps_enemy_collision():
    """The wire rule only disables same-team collision, not enemy contact."""

    server = SimpleNamespace(
        world_manager=make_world_manager(),
        players={},
        config=SimpleNamespace(same_team_collision=False),
    )
    player, _ = make_player(server, cell_x=100, cell_y=100)
    enemy_connection = DummyConnection(server)
    enemy = Player(1, "Enemy", 3, C.RIFLE_TOOL, enemy_connection)
    enemy_connection.player = enemy
    enemy.spawn(player.x + 0.25, player.y, player.z)
    server.players = {player.id: player, enemy.id: enemy}

    assert player._build_player_collision_positions() == [
        (enemy.x, enemy.y, enemy.z, enemy._current_height())
    ]


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


def test_input_label_gap_consumes_only_observed_client_frames():
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

    # Retail loop_count is a clock label and can skip without producing a
    # movement-history entry. ACKing invented label 101 would make the native
    # client's exact lookup fail and hard-snap its prediction history.
    assert applied == [100, 102]
    assert player.last_applied_input_loop == 102
    assert player.input_history == {}


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


def test_latched_buttons_retain_the_client_loop_that_authored_them():
    """A delayed jump must still identify the frame where retail launched.

    ClientData 101 is simulated while packet 102 is consumed because button
    state has a one-observed-frame latch.  Reconciliation anchors are stamped
    in the client's clock, so remembering only the consumed packet loop loses
    the distinction and can select a WorldUpdate row that retail received
    *after* its launch.
    """
    player, _ = make_player()
    observed_sources = []

    async def record_update(_dt):
        observed_sources.append(player._applied_input_source_loop)

    player.update = record_update
    walking = (True, False, False, False, False, False, False, True)
    jumping = (True, False, False, False, True, False, False, True)
    orientation = (1.0, 0.0, 0.0)

    player.record_input_frame(100, walking, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    player.record_input_frame(101, jumping, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    player.record_input_frame(102, jumping, orientation)
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert observed_sources == [None, 100, 101]


def test_validation_can_apply_packet_buttons_on_the_current_observed_loop():
    player, _ = make_player()
    player.connection.server.config = SimpleNamespace(
        movement_input_latch_frames=0
    )
    simulated_sprint = []

    async def record_update(_dt):
        simulated_sprint.append(player.input.sprint)

    player.update = record_update
    sprinting = (True, False, False, False, False, False, False, True)
    player.record_input_frame(100, sprinting, (1.0, 0.0, 0.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert simulated_sprint == [True]


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

    # Packet 102 introduces sprint. Actual frame 102 uses the prior walking
    # state, then actual frame 104 uses sprint. Missing integer labels are not
    # frames and therefore neither simulate nor advance the input latch.
    player.record_input_frame(102, sprinting, orientation)
    player.record_input_frame(104, crouching, orientation)
    player.update_input(*crouching)
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))
    asyncio.run(player.simulate_tick(1.0 / 60.0))

    assert player.last_applied_input_loop == 104
    assert simulated == [
        (False, False),
        (False, False),
        # Crouch geometry is current-frame in retail even though locomotion
        # remains one observed ClientData frame latched.
        (True, True),
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
    (update_position, _, _, _, _, _, update_input_flags, _, update_state,
     update_tool, update_fuel, _, _) = update
    assert update_tool == player_one.tool
    assert update_state == player_one.pack_state_flags()
    assert update_fuel == player_one.jetpack_fuel
    assert update_position[0] > player_two.position[0] - 20.0
    assert update_position[0] > player_one.last_reported_position[0] - 0.01
    assert update_input_flags == player_one.pack_input_flags()


def test_self_world_update_keeps_anchor_but_uses_noop_tool_sentinel():
    """A local row must not replay a delayed equipped-tool transition.

    Retail applies the row's network position before calling
    ``Player.set_tool(tool, True)``.  Tool id 0xFF is rejected as an invalid
    selectable tool, preserving the anchor while observer rows still receive
    the authoritative real tool.
    """
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()
    owner, _owner_connection = make_player(server, cell_x=100, cell_y=100)
    observer, _observer_connection = make_player(
        server, cell_x=110, cell_y=110
    )
    observer.id = 1
    owner.set_tool(C.BLOCK_TOOL)
    observer.set_tool(C.RIFLE_TOOL)
    server.players = {owner.id: owner, observer.id: observer}

    owner_data = server.build_world_update_data(
        loop_count_override=123,
        local_player_id=owner.id,
    )
    owner_packet = WorldUpdate(ByteReader(owner_data[1:]))

    assert owner_packet.player_updates[owner.id][9] == 0xFF
    assert owner_packet.player_updates[observer.id][9] == observer.tool


def test_world_header_clock_is_independent_from_each_player_row_pong():
    """Retail reconciles an owner from row pong, not WorldUpdate.loop_count."""
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()
    owner, _connection = make_player(server, cell_x=100, cell_y=100)
    server.players = {owner.id: owner}
    server.loop_count = 900
    owner.wu_ack_loop = 812

    data = server.build_world_update_data(local_player_id=owner.id)
    packet = WorldUpdate(ByteReader(data[1:]))

    assert packet.loop_count == 900
    assert packet.player_updates[owner.id][4] == 812


def test_peerless_bot_rows_advance_their_remote_snapshot_stamp():
    """Retail must not deduplicate every post-spawn bot WorldUpdate.

    A peerless bot has no ClientData loop to acknowledge.  Reusing the
    default pong value of zero caused each observer to accept one bot row,
    extrapolate it independently, and finally see a large teleport on bot
    respawn.  The global server loop is the bot's remote-only snapshot clock.
    """
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()
    observer, observer_connection = make_player(
        server, cell_x=100, cell_y=100
    )
    bot, _bot_connection = make_player(server, cell_x=110, cell_y=110)
    bot.id = 1
    bot.is_bot = True
    bot.last_applied_input_loop = None
    bot.wu_ack_loop = 0
    server.players = {observer.id: observer, bot.id: bot}
    server.connections = {"observer": observer_connection}

    server.loop_count = 120
    server._broadcast_world_updates()
    first = WorldUpdate(ByteReader(observer_connection.sent_packets[-1][1:]))

    bot.set_position(bot.x + 1.0, bot.y, bot.z)
    server.loop_count = 122
    server._broadcast_world_updates()
    second = WorldUpdate(ByteReader(observer_connection.sent_packets[-1][1:]))

    assert first.player_updates[bot.id][4] == 120
    assert second.player_updates[bot.id][4] == 122
    assert second.player_updates[bot.id][0][0] == first.player_updates[bot.id][0][0] + 1.0


def test_block_tool_keeps_a_fresh_self_world_update_anchor():
    """Building must not disable the local reconciliation anchor.

    A retail stress run with the old exclusion accumulated 1,781 loops of
    anchor lag and rolled the player back 62.76 blocks while jumping with tool
    5 equipped. Placement completion still uses the reliable BlockLine echo.
    """
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = make_world_manager()
    player, _connection = make_player(server, cell_x=100, cell_y=100)

    player.set_tool(C.BLOCK_TOOL)
    assert server._self_world_update_is_safe(player) is True


    player.set_tool(C.RIFLE_TOOL)
    assert server._self_world_update_is_safe(player) is True



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

