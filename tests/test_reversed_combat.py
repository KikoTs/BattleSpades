import asyncio
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
import server.combat_runtime as combat_runtime
from aoslib.vxl import VXL
from shared.bytes import ByteReader
from shared.packet import BlockBuild, BlockLiberate, BlockLine, BlockOccupy, KillAction, SetHP, ShootPacket, WeaponReload

from protocol.packet_handler import PacketHandler
from server.combat_runtime import get_combat_system
from server.config import ServerConfig
from server.game_constants import (
    BLOCK_ACTION_BUILD,
    BLOCK_ACTION_DESTROY,
    DEFAULT_BLOCK_HEALTH,
    KILL_HEADSHOT,
    TEAM1,
    TEAM2,
)
from server.player import Player
from server.world_manager import WorldManager


TEST_MAP_PATH = Path("maps/ArcticBase.vxl")
TEST_MAP_BYTES = TEST_MAP_PATH.read_bytes() if TEST_MAP_PATH.exists() else None
TEST_COLOR = 0x7F00FF00


class DummyConnection:
    def __init__(self, server=None, player=None):
        self.server = server
        self.player = player
        self.sent_packets = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent_packets.append(data)


class DummyServer:
    def __init__(self):
        self.config = ServerConfig()
        self.config.log_suppress_packets = set()
        self.loop_count = 7
        self.players = {}
        self.connections = {}
        self.broadcast_packets = []
        self.world_manager = WorldManager(self.config)
        if TEST_MAP_BYTES is not None:
            self.world_manager.map = VXL(-1, TEST_MAP_BYTES, len(TEST_MAP_BYTES), 2)
            self.world_manager.map_name = TEST_MAP_PATH.stem
            self.world_manager._refresh_world()
        else:
            self.world_manager.generate_flat_map()
        flatten_patch(self.world_manager, 100, 100)

    def broadcast(self, data, exclude=None):
        self.broadcast_packets.append(data)


def make_player(server, player_id, name, team, weapon, position):
    connection = DummyConnection(server)
    player = Player(player_id, name, team, weapon, connection)
    connection.player = player
    player.spawn(*position)
    server.players[player_id] = player
    server.connections[player_id] = connection
    return player, connection


def flatten_patch(world_manager, cell_x=100, cell_y=100, radius=6):
    if world_manager.map is None:
        return

    ground_top = world_manager.map.get_z(cell_x, cell_y)
    for x in range(cell_x - radius, cell_x + radius + 1):
        for y in range(cell_y - radius, cell_y + radius + 1):
            for z in range(0, ground_top):
                world_manager.map.set_point(x, y, z, False, 0)
            world_manager.map.set_point(x, y, ground_top, True, TEST_COLOR)


def normalize(vector):
    magnitude = math.sqrt(sum(component * component for component in vector))
    return tuple(component / magnitude for component in vector)


def aim_at(player, point):
    direction = (
        point[0] - player.eye_x,
        point[1] - player.eye_y,
        point[2] - player.eye_z,
    )
    player.set_orientation_vector(*normalize(direction))


def make_shoot_packet(player, origin=None, orientation=None, seed=1):
    packet = ShootPacket()
    packet.loop_count = 1
    packet.shooter_id = player.id
    packet.shot_on_world_update = 1
    packet.x, packet.y, packet.z = origin or player.eye
    packet.ori_x, packet.ori_y, packet.ori_z = orientation or player.orientation
    packet.damage = 0
    packet.penetration = 0
    packet.secondary = 0
    packet.seed = seed
    return packet


def test_shoot_packet_splits_affect_shooter_and_secondary_flag_bits():
    for flags in (0x01, 0x02, 0x03):
        packet = ShootPacket()
        packet.loop_count = 7
        packet.shooter_id = 2
        packet.shot_on_world_update = 0
        packet.x = packet.y = packet.z = 0.0
        packet.ori_x, packet.ori_y, packet.ori_z = 1.0, 0.0, 0.0
        packet.damage = 20
        packet.penetration = 1
        packet.affect_shooter = flags & 0x01
        packet.secondary = (flags >> 1) & 0x01
        packet.seed = 37

        raw = bytes(packet.generate())
        parsed = ShootPacket(ByteReader(raw[1:]))

        assert raw[38] == flags
        assert parsed.affect_shooter == (flags & 0x01)
        assert parsed.secondary == ((flags >> 1) & 0x01)


def test_shotgun_resolves_and_relays_each_client_pellet_but_consumes_once():
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.SHOTGUN_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.SHOTGUN_TOOL)
    profile = attacker.get_weapon_profile()
    before_clip = attacker.ammo_clip
    directions = []
    combat = get_combat_system(server)

    def record_ray(_attacker, direction, _origin=None):
        directions.append(direction)
        return False

    combat._resolve_hitscan = record_ray
    expected = []
    for pellet_index in range(profile.pellet_count):
        direction = normalize((1.0, (pellet_index - 4.5) * 0.01, 0.0))
        expected.append(direction)
        packet = make_shoot_packet(
            attacker,
            orientation=direction,
            seed=37,
        )
        packet.loop_count = 900
        combat.handle_shot(attacker, packet)

    assert attacker.ammo_clip == before_clip - 1
    assert len(server.broadcast_packets) == profile.pellet_count
    assert len(directions) == profile.pellet_count
    for actual, wanted in zip(directions, expected):
        assert all(
            math.isclose(component, expected_component, abs_tol=1e-6)
            for component, expected_component in zip(actual, wanted)
        )


def test_shotgun_rejects_duplicate_pellet_ray_within_trigger_group():
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.SHOTGUN_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.SHOTGUN_TOOL)
    before_clip = attacker.ammo_clip
    combat = get_combat_system(server)
    directions = []
    combat._resolve_hitscan = lambda _attacker, direction, _origin=None: directions.append(direction) or False
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.01, 0.0), seed=37)
    packet.loop_count = 900

    for _ in range(attacker.get_weapon_profile().pellet_count):
        combat.handle_shot(attacker, packet)

    assert attacker.ammo_clip == before_clip - 1
    assert len(server.broadcast_packets) == 1
    assert len(directions) == 1


def test_fire_rate_grace_does_not_compound_into_a_faster_schedule():
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.SMG_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.SMG_TOOL)
    interval = attacker.get_weapon_profile().fire_interval
    early = interval - (1.0 / 60.0) + 1e-5

    assert attacker.consume_shot(10.0)
    assert attacker.consume_shot(10.0 + early)
    assert not attacker.consume_shot(10.0 + 2.0 * early)
    assert attacker.consume_shot(10.0 + 2.0 * interval)


def test_assault_rifle_accepts_three_round_burst_but_not_early_fourth(monkeypatch):
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.ASSAULT_RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.ASSAULT_RIFLE_TOOL)
    before_clip = attacker.ammo_clip
    times = iter((10.0, 10.1, 10.2, 10.3, 10.5))
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: next(times))
    combat = get_combat_system(server)

    for loop_count in (100, 106, 112, 118, 130):
        packet = make_shoot_packet(attacker, seed=loop_count)
        packet.loop_count = loop_count
        combat.handle_shot(attacker, packet)

    assert len(server.broadcast_packets) == 4
    assert attacker.ammo_clip == before_clip - 4


def test_assault_rifle_rejects_same_tick_fake_burst(monkeypatch):
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.ASSAULT_RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.ASSAULT_RIFLE_TOOL)
    before_clip = attacker.ammo_clip
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: 10.0)
    combat = get_combat_system(server)

    for loop_count in (100, 101, 102):
        packet = make_shoot_packet(attacker, seed=loop_count)
        packet.loop_count = loop_count
        combat.handle_shot(attacker, packet)

    assert len(server.broadcast_packets) == 1
    assert attacker.ammo_clip == before_clip - 1


def test_minigun_accepts_stock_cadence_ramp(monkeypatch):
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.MINIGUN_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.MINIGUN_TOOL)
    before_clip = attacker.ammo_clip
    times = iter((20.0, 20.3, 20.555, 20.78))
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: next(times))
    combat = get_combat_system(server)

    for loop_count in range(200, 204):
        packet = make_shoot_packet(attacker, seed=loop_count)
        packet.loop_count = loop_count
        combat.handle_shot(attacker, packet)

    assert len(server.broadcast_packets) == 4
    assert attacker.ammo_clip == before_clip - 4


def test_block_occupy_round_trips_with_reference_layout():
    packet = BlockOccupy()
    packet.loop_count = 11
    packet.player_id = 3
    packet.x = 101
    packet.y = 102
    packet.z = 61

    raw = bytes(packet.generate())
    parsed = BlockOccupy(ByteReader(raw[1:]))

    assert len(raw) == 12
    assert parsed.loop_count == 11
    assert parsed.player_id == 3
    assert (parsed.x, parsed.y, parsed.z) == (101, 102, 61)


def test_rifle_body_hit_sends_hp_and_broadcasts_shot():
    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    target, target_connection = make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))

    attacker.set_tool(C.RIFLE_TOOL)
    aim_at(attacker, target.position)

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker).generate())))

    # RIFLE torso damage = 70 (real client value); soldier damage_multiplier 1.0.
    assert target.health == 30
    assert server.broadcast_packets[0][0] == 6
    hp_packet = SetHP(ByteReader(target_connection.sent_packets[0][1:]))
    assert hp_packet.hp == 30
    assert hp_packet.damage_type == 1


def test_rifle_headshot_kills_and_broadcasts_kill_action():
    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    target, target_connection = make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    target.health = 40

    attacker.set_tool(C.RIFLE_TOOL)
    aim_at(attacker, target.eye)

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker).generate())))

    assert target.alive is False
    hp_packet = SetHP(ByteReader(target_connection.sent_packets[0][1:]))
    assert hp_packet.hp == 0
    kill_packet = KillAction(ByteReader(server.broadcast_packets[-1][1:]))
    assert kill_packet.player_id == target.id
    assert kill_packet.killer_id == attacker.id
    assert kill_packet.kill_type == KILL_HEADSHOT


def test_stock_oriented_hitboxes_include_each_leg_and_preserve_the_gap():
    server = DummyServer()
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    target.set_orientation_vector(0.0, 1.0, 0.0)
    combat = get_combat_system(server)

    leg_height = target.z + 2.0
    left_leg_hit = combat._ray_hits_target(
        (target.x - 5.0, target.y, leg_height), (1.0, 0.0, 0.0), 10.0, target)
    between_legs = combat._ray_hits_target(
        (target.x, target.y - 5.0, leg_height), (0.0, 1.0, 0.0), 10.0, target)

    assert left_leg_hit is not None
    assert left_leg_hit[2] is False
    assert between_legs is None


def test_stock_hitboxes_rotate_with_player_yaw():
    server = DummyServer()
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    target.set_orientation_vector(1.0, 0.0, 0.0)
    combat = get_combat_system(server)

    hit = combat._ray_hits_target(
        (target.x - 5.0, target.y + 0.25, target.z + 2.0),
        (1.0, 0.0, 0.0),
        10.0,
        target,
    )
    assert hit is not None
    assert hit[2] is False


def test_crouch_uses_two_lowered_leg_models_not_one_center_box():
    server = DummyServer()
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    target.set_orientation_vector(0.0, 1.0, 0.0)
    target.input.crouch = True
    combat = get_combat_system(server)

    leg_height = target.z + 1.2
    leg_hit = combat._ray_hits_target(
        (target.x - 5.0, target.y + 0.3, leg_height),
        (1.0, 0.0, 0.0),
        10.0,
        target,
    )
    center_gap = combat._ray_hits_target(
        (target.x, target.y - 5.0, leg_height),
        (0.0, 1.0, 0.0),
        10.0,
        target,
    )
    assert leg_hit is not None
    assert center_gap is None


def test_same_team_shots_do_not_damage_with_friendly_fire_disabled():
    server = DummyServer()
    server.config.friendly_fire = False
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    target, target_connection = make_player(server, 1, "Target", TEAM1, C.RIFLE_TOOL, (106.5, 100.5, 60.0))

    attacker.set_tool(C.RIFLE_TOOL)
    aim_at(attacker, target.position)

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker).generate())))

    assert target.health == 100
    assert target_connection.sent_packets == []


def test_invalid_shot_origin_is_rejected():
    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))
    attacker.set_tool(C.RIFLE_TOOL)

    packet = make_shoot_packet(attacker, origin=(attacker.eye_x + 20.0, attacker.eye_y, attacker.eye_z))
    before_clip = attacker.ammo_clip

    asyncio.run(PacketHandler(server).handle(attacker, bytes(packet.generate())))

    assert attacker.ammo_clip == before_clip
    assert server.broadcast_packets == []


def test_weapon_block_damage_accumulates_and_breaks_wall_before_hitting_player():
    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    target, target_connection = make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (108.5, 100.5, 60.0))
    attacker.set_tool(C.RIFLE_TOOL)

    wall = (104, 100, 60)
    server.world_manager.set_block(*wall, solid=True, color=TEST_COLOR)
    aim_at(attacker, target.position)

    for shot_index in range(3):
        asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker, seed=shot_index + 1).generate())))
        attacker.last_shot_time -= attacker.get_weapon_profile().fire_interval
        attacker.next_shot_time -= attacker.get_weapon_profile().fire_interval

    assert target.health == 100
    assert target_connection.sent_packets == []
    assert wall not in server.world_manager.block_damage
    assert server.world_manager.get_solid(*wall) is False
    # Block damage/removal both ride Damage(37) — the ONLY packet this
    # client mutates world geometry from (decompiled gameScene contract).
    # Two hit-damage broadcasts, then the destroying shot sends kill-damage.
    assert [packet[0] for packet in server.broadcast_packets[:6]] == [6, 37, 6, 37, 6, 37]
    from shared.packet import Damage
    last_hit = Damage(ByteReader(server.broadcast_packets[3][1:]))
    assert last_hit.chunk_check == 1
    assert (int(last_hit.position[0]), int(last_hit.position[1]), int(last_hit.position[2])) == wall
    destroy_packet = Damage(ByteReader(server.broadcast_packets[5][1:]))
    assert destroy_packet.chunk_check == 1
    assert destroy_packet.damage >= 31.0  # kill-damage guarantees removal
    assert (int(destroy_packet.position[0]), int(destroy_packet.position[1]), int(destroy_packet.position[2])) == wall

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker, seed=4).generate())))

    # One rifle body hit after the wall breaks: 100 - 70 = 30.
    assert target.health == 30


def test_server_mirrors_native_collapse_without_per_fallen_voxel_flood():
    server = DummyServer()
    player, _ = make_player(
        server,
        0,
        "Digger",
        TEAM1,
        C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    falling = [(120, 120, 100), (121, 120, 100)]
    for position in falling:
        server.world_manager.set_block(*position, solid=True, color=TEST_COLOR)

    calls = 0

    def find_once(_frontier):
        nonlocal calls
        calls += 1
        return [falling] if calls == 1 else []

    server.world_manager.find_unsupported_chunks = find_once
    combat = get_combat_system(server)
    before = list(server.broadcast_packets)

    combat._collapse_unsupported(player, [(119, 120, 100)])

    assert all(not server.world_manager.get_solid(*position) for position in falling)
    assert server.broadcast_packets == before


def test_build_damage_flag_disables_weapon_block_damage():
    server = DummyServer()
    server.config.build_damage = False
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (108.5, 100.5, 60.0))
    attacker.set_tool(C.RIFLE_TOOL)

    wall = (104, 100, 60)
    server.world_manager.set_block(*wall, solid=True, color=TEST_COLOR)
    aim_at(attacker, (110.5, 100.5, 60.0))

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker).generate())))

    assert server.world_manager.get_solid(*wall) is True
    assert server.world_manager.block_damage == {}
    assert [packet[0] for packet in server.broadcast_packets] == [6]


def test_direct_block_destroy_refunds_one_block():
    server = DummyServer()
    builder, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    builder.set_tool(C.BLOCK_TOOL)
    builder.blocks = 10

    block = (101, 100, 60)
    server.world_manager.set_block(*block, solid=True, color=TEST_COLOR)

    packet = BlockLiberate()
    packet.loop_count = 1
    packet.player_id = builder.id
    packet.x, packet.y, packet.z = block

    asyncio.run(PacketHandler(server).handle(builder, bytes(packet.generate())))

    assert builder.blocks == 11
    assert server.world_manager.get_solid(*block) is False
    # Removal rides Damage(37) with kill-damage (BlockBuild is add-only).
    assert server.broadcast_packets[-1][0] == 37
    from shared.packet import Damage
    destroy_packet = Damage(ByteReader(server.broadcast_packets[-1][1:]))
    assert destroy_packet.damage >= 31.0
    assert (int(destroy_packet.position[0]), int(destroy_packet.position[1]), int(destroy_packet.position[2])) == block


def test_spade_destroy_breaks_vertical_three_block_column():
    server = DummyServer()
    player, _ = make_player(server, 0, "Digger", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.SPADE_TOOL)

    center = (101, 100, 60)
    for z in (59, 60, 61):
        server.world_manager.set_block(center[0], center[1], z, solid=True, color=TEST_COLOR)

    packet = BlockLiberate()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = center

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert server.world_manager.get_solid(center[0], center[1], 59) is False
    assert server.world_manager.get_solid(center[0], center[1], 60) is False
    assert server.world_manager.get_solid(center[0], center[1], 61) is False
    assert server.broadcast_packets
    # Every removal is a kill-damage Damage(37) — the only client destroy path.
    from shared.packet import Damage
    assert all(packet_bytes[0] == 37 for packet_bytes in server.broadcast_packets)
    for packet_bytes in server.broadcast_packets:
        destroy_packet = Damage(ByteReader(packet_bytes[1:]))
        assert destroy_packet.damage >= 31.0


def test_block_build_consumes_inventory_and_clears_old_damage():
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5

    block = (101, 100, 60)
    # The client only places blocks that FACE-touch an existing solid
    # (map.has_neighbors(...,1)); give this cell ground to rest on (z+1 is
    # BELOW, z grows downward) so the server's parity gate accepts it.
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)
    server.world_manager.block_damage[block] = DEFAULT_BLOCK_HEALTH - 1.0

    packet = BlockBuild()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = block
    packet.block_type = BLOCK_ACTION_BUILD

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 4
    assert server.world_manager.get_solid(*block) is True
    assert block not in server.world_manager.block_damage
    broadcast_packet = BlockBuild(ByteReader(server.broadcast_packets[-1][1:]))
    assert broadcast_packet.block_type == BLOCK_ACTION_BUILD
    assert (broadcast_packet.x, broadcast_packet.y, broadcast_packet.z) == block


def test_block_line_replicates_as_one_native_block_line_packet():
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    # Ground under the line (z+1 is BELOW): the client only renders blocks that
    # face-touch a solid, so the server must only accept supported cells.
    for x in range(101, 104):
        server.world_manager.set_block(x, 100, 61, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = (101, 100, 60)
    packet.x2, packet.y2, packet.z2 = (103, 100, 60)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 2
    assert all(server.world_manager.get_solid(x, 100, 60) for x in range(101, 104))
    assert len(server.broadcast_packets) == 1
    assert server.broadcast_packets[0][0] == BlockLine.id
    replicated = BlockLine(ByteReader(server.broadcast_packets[0][1:]))
    assert replicated.loop_count == server.loop_count
    assert replicated.player_id == player.id
    assert (replicated.x1, replicated.y1, replicated.z1) == (101, 100, 60)
    assert (replicated.x2, replicated.y2, replicated.z2) == (103, 100, 60)


def test_block_line_uses_stock_face_connected_cube_traversal():
    server = DummyServer()
    combat = get_combat_system(server)

    assert combat._block_line_cells(
        (100, 100, 60),
        (102, 101, 60),
    ) == [
        (100, 100, 60),
        (101, 100, 60),
        (101, 101, 60),
        (102, 101, 60),
    ]


def test_block_line_rejects_unsupported_floating_cells():
    """The client's gate is map.has_neighbors(x,y,z,1) — a block touching
    nothing is silently dropped. If the server accepts such a placement it
    keeps blocks NO client has: the build never appears, the builder loses
    inventory, and the server carries collision where every client sees air
    (a server-side "invisible wall"). Measured live 2026-07-10.
    """
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    # No ground anywhere near z=60 -> the whole line floats.

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = (101, 100, 60)
    packet.x2, packet.y2, packet.z2 = (103, 100, 60)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 5
    assert all(server.world_manager.get_solid(x, 100, 60) is False for x in range(101, 104))
    assert server.broadcast_packets == []


def test_block_line_supports_cells_on_earlier_cells_of_the_same_line():
    """A line may extend outward from the ground: each cell rests on the one
    placed before it, matching the client's in-order add_block walk."""
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    # Ground ONLY under the first cell; 102 and 103 float unless 101/102 support them.
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = (101, 100, 60)
    packet.x2, packet.y2, packet.z2 = (103, 100, 60)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 2
    assert all(server.world_manager.get_solid(x, 100, 60) for x in range(101, 104))
    assert len(server.broadcast_packets) == 1
    assert server.broadcast_packets[0][0] == BlockLine.id


def test_block_line_is_atomic_when_any_cell_cannot_be_built():
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    server.world_manager.set_block(102, 100, 60, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = (101, 100, 60)
    packet.x2, packet.y2, packet.z2 = (103, 100, 60)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 5
    assert server.world_manager.get_solid(101, 100, 60) is False
    assert server.world_manager.get_solid(102, 100, 60) is True
    assert server.world_manager.get_solid(103, 100, 60) is False
    assert server.broadcast_packets == []


def test_block_line_is_atomic_when_inventory_cannot_cover_it():
    server = DummyServer()
    player, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 2

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = (101, 100, 60)
    packet.x2, packet.y2, packet.z2 = (103, 100, 60)

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 2
    assert all(server.world_manager.get_solid(x, 100, 60) is False for x in range(101, 104))
    assert server.broadcast_packets == []


def test_reload_is_server_validated_and_completes_in_update():
    server = DummyServer()
    player, _ = make_player(server, 0, "Reloader", TEAM1, C.SHOTGUN_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.SHOTGUN_TOOL)
    player.ammo_clip = 1
    player.ammo_reserve = 10

    packet = WeaponReload()
    packet.player_id = player.id
    packet.tool_id = player.tool
    packet.is_done = 0

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.reloading is True
    assert server.broadcast_packets[-1][0] == 76

    player.reload_end_time = time.monotonic() - 0.01
    asyncio.run(player.update(1.0 / 60.0))

    assert player.reloading is False
    assert player.ammo_clip == player.get_weapon_profile().clip_size
    assert server.broadcast_packets[-1][0] == 76


def test_raw_reversed_minigun_tool_id_is_treated_as_weapon():
    server = DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    target, _ = make_player(server, 1, "Target", TEAM2, C.RIFLE_TOOL, (106.5, 100.5, 60.0))

    attacker.set_tool(C.MINIGUN_TOOL)
    attacker.ammo_clip = 30
    attacker.ammo_reserve = 120
    aim_at(attacker, target.position)

    asyncio.run(PacketHandler(server).handle(attacker, bytes(make_shoot_packet(attacker).generate())))

    assert target.health < 100


def test_raw_reversed_block_tool_id_can_destroy_blocks():
    server = DummyServer()
    builder, _ = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    builder.set_tool(C.BLOCK_TOOL)
    builder.blocks = 10

    block = (101, 100, 60)
    server.world_manager.set_block(*block, solid=True, color=TEST_COLOR)

    packet = BlockLiberate()
    packet.loop_count = 1
    packet.player_id = builder.id
    packet.x, packet.y, packet.z = block

    asyncio.run(PacketHandler(server).handle(builder, bytes(packet.generate())))

    assert server.world_manager.get_solid(*block) is False
    # Removal broadcast = Damage(37), the only client destroy path.
    assert server.broadcast_packets[-1][0] == 37
