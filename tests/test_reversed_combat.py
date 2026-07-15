import asyncio
import math
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
import server.combat_runtime as combat_runtime
from aoslib.vxl import VXL
from shared.bytes import ByteReader
from shared.packet import BlockBuild, BlockBuildColored, BlockLiberate, BlockLine, BlockOccupy, Damage, DestroyEntity, HitEntity, KillAction, PaintBlockPacket, SetHP, ShootFeedbackPacket, ShootPacket, ShootResponse, WeaponReload

from protocol.packet_handler import PacketHandler
from server.combat_runtime import get_combat_system
from server.bot_ai.director import _BotConnection
from server.config import ServerConfig
from server.entities.behaviors import MedpackBehavior, RadarStationBehavior
from server.entities.registry import EntityContext, EntityRegistry
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


def test_disconnect_clears_combat_cadence_before_player_id_reuse():
    combat = combat_runtime.CombatSystem(SimpleNamespace())
    combat._pellet_spread[7] = object()
    combat._assault_bursts[7] = object()
    combat._minigun_runs[7] = object()

    combat.forget_player(7)

    assert 7 not in combat._pellet_spread
    assert 7 not in combat._assault_bursts
    assert 7 not in combat._minigun_runs


def test_kill_action_count_resets_when_the_killer_dies():
    """Packet 46 carries the current life streak, not scoreboard kills."""

    server = DummyServer()
    killer, _ = make_player(
        server, 1, "Killer", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    first, _ = make_player(
        server, 2, "First", TEAM2, C.RIFLE_TOOL, (102.5, 100.5, 60.0)
    )
    second, _ = make_player(
        server, 3, "Second", TEAM2, C.RIFLE_TOOL, (103.5, 100.5, 60.0)
    )

    first.die(killer=killer, kill_type=0)
    first_packet = KillAction(ByteReader(server.broadcast_packets[-1][1:]))
    assert killer.kills == 1
    assert killer.kill_streak == 1
    assert first_packet.kill_count == 1

    killer.die(killer=second, kill_type=0)
    assert killer.kill_streak == 0

    killer.spawn(100.5, 100.5, 60.0)
    second.spawn(103.5, 100.5, 60.0)
    second.die(killer=killer, kill_type=0)
    second_packet = KillAction(ByteReader(server.broadcast_packets[-1][1:]))
    assert killer.kills == 2
    assert killer.kill_streak == 1
    assert second_packet.kill_count == 1


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
        self.broadcast_excludes = []
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
        self.broadcast_excludes.append(exclude)


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


def attach_entity_runtime(server):
    server.entity_registry = EntityRegistry()
    server.destroyed_entity_ids = []

    def destroy(entity_id):
        server.destroyed_entity_ids.append(int(entity_id))
        packet = DestroyEntity()
        packet.entity_id = int(entity_id)
        server.broadcast(bytes(packet.generate()))

    server.broadcast_destroy_entity = destroy
    server._build_entity_ctx = lambda: EntityContext(
        dt=1 / 60,
        now=time.time(),
        players=list(server.players.values()),
        world=server.world_manager,
        server=server,
        destroy=destroy,
    )


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


def test_hitscan_routes_nearest_damageable_entity_and_broadcasts_hit_effect():
    server = DummyServer()
    attach_entity_runtime(server)
    attacker, _ = make_player(
        server, 0, "Attacker", TEAM1, C.PISTOL_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.PISTOL_TOOL)
    center = (104.5, 100.5, attacker.eye_z)
    entity = server.entity_registry.place(
        C.MEDPACK_ENTITY,
        center[0], center[1], center[2] - 0.5,
        behavior=MedpackBehavior(team=TEAM2, health=1.0),
    )
    aim_at(attacker, center)

    hit = get_combat_system(server)._resolve_hitscan(
        attacker, attacker.orientation, attacker.eye
    )

    assert hit is True
    assert server.entity_registry.get(entity.entity_id) is None
    assert server.destroyed_entity_ids == [entity.entity_id]
    hit_packets = [packet for packet in server.broadcast_packets if packet[0] == 20]
    assert len(hit_packets) == 1
    parsed = HitEntity(ByteReader(hit_packets[0][1:]))
    assert parsed.entity_id == entity.entity_id
    assert parsed.type == C.PART_ENTITY1


def test_hitscan_radar_damage_accumulates_at_recovered_45_health():
    server = DummyServer()
    attach_entity_runtime(server)
    server._radar_station_removed = lambda team: None
    attacker, _ = make_player(
        server, 0, "Attacker", TEAM1, C.PISTOL_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.PISTOL_TOOL)
    center = (104.5, 100.5, attacker.eye_z)
    entity = server.entity_registry.place(
        C.RADAR_STATION_ENTITY,
        center[0] - 0.5, center[1] - 0.5, center[2] - 0.55,
        player_id=99,
        behavior=RadarStationBehavior(team=TEAM2, health=45.0),
    )
    aim_at(attacker, center)
    profile_damage = get_combat_system(server)._calculate_damage(
        attacker, attacker.get_weapon_profile(), False
    )

    assert get_combat_system(server)._resolve_hitscan(
        attacker, attacker.orientation, attacker.eye
    ) is True
    assert entity.behavior.health == 45.0 - profile_damage
    assert entity.alive is True


def test_shotgun_expands_one_seeded_packet_and_consumes_one_round():
    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.AUTO_SHOTGUN_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.AUTO_SHOTGUN_TOOL)
    profile = attacker.get_weapon_profile()
    before_clip = attacker.ammo_clip
    directions = []
    combat = get_combat_system(server)

    def record_ray(_attacker, direction, _origin=None):
        directions.append(direction)
        return False

    combat._resolve_hitscan = record_ray
    packet = make_shoot_packet(
        attacker,
        orientation=(1.0, 0.0, 0.0),
        seed=37,
    )
    packet.loop_count = 900
    combat.handle_shot(attacker, packet)

    rng = random.Random(37)
    expected = [
        normalize((
            1.0 + (rng.random() * 4.0 - 2.0) * profile.spread,
            (rng.random() * 4.0 - 2.0) * profile.spread,
            (rng.random() * 4.0 - 2.0) * profile.spread,
        ))
        for _ in range(profile.pellet_count)
    ]

    assert attacker.ammo_clip == before_clip - 1
    assert len(server.broadcast_packets) == 1
    assert len(directions) == profile.pellet_count
    for actual, wanted in zip(directions, expected):
        assert all(
            math.isclose(component, expected_component, abs_tol=1e-6)
            for component, expected_component in zip(actual, wanted)
        )


def test_shotgun_rejects_duplicate_trigger_during_fire_interval():
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
    assert len(directions) == attacker.get_weapon_profile().pellet_count


def test_relayed_shot_uses_native_feedback_and_excludes_predicted_owner():
    server = DummyServer()
    attacker, _ = make_player(
        server, 0, "Specialist", TEAM1, C.AUTO_SHOTGUN_TOOL,
        (100.5, 100.5, 60.0),
    )
    attacker.set_tool(C.AUTO_SHOTGUN_TOOL)
    combat = get_combat_system(server)
    combat._resolve_hitscan = lambda *_args, **_kwargs: False

    combat.handle_shot(attacker, make_shoot_packet(attacker, seed=127))

    relayed = ShootFeedbackPacket(ByteReader(server.broadcast_packets[0][1:]))
    assert relayed.shooter_id == attacker.id
    assert relayed.tool_id == C.AUTO_SHOTGUN_TOOL
    assert relayed.seed == 127
    assert relayed.loop_count == server.loop_count
    assert server.broadcast_excludes[0] is attacker


def test_machete_accumulates_two_damage_on_two_vertical_voxels(monkeypatch):
    server = DummyServer()
    player, _ = make_player(
        server, 0, "Specialist", TEAM1, C.MACHETE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player.set_tool(C.MACHETE_TOOL)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    block = (103, 100, 60)
    footprint = (block, (block[0], block[1], block[2] + 1))
    for cell in footprint:
        server.world_manager.set_block(*cell, True, TEST_COLOR)
    server.world_manager.raycast = lambda *_args: block
    times = iter((10.0, 10.8, 11.6))
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: next(times))
    starting_blocks = player.blocks

    combat = get_combat_system(server)
    for strike in range(3):
        assert combat.handle_shot(
            player,
            make_shoot_packet(player, orientation=(1.0, 0.0, 0.0)),
        )
        if strike < 2:
            assert all(server.world_manager.get_solid(*cell) for cell in footprint)
            assert all(
                server.world_manager.block_damage[cell] == (strike + 1) * 2.0
                for cell in footprint
            )

    assert all(not server.world_manager.get_solid(*cell) for cell in footprint)
    assert player.blocks == starting_blocks + 2

    relayed = [
        raw for raw in server.broadcast_packets
        if raw[0] == ShootFeedbackPacket.id
    ]
    damage_packets = [
        Damage(ByteReader(raw[1:]))
        for raw in server.broadcast_packets
        if raw[0] == 37
    ]
    # Packet 8 calls Character.shoot and crashes on MacheteTool/SpadeTool,
    # which deliberately expose use_primary instead of shoot. Remote melee
    # presentation is the WorldUpdate primary-action bit.
    assert relayed == []
    assert len(damage_packets) == 3
    assert all(packet.type == C.MACHETE_DAMAGE for packet in damage_packets)
    assert all(packet.damage == 2.0 for packet in damage_packets)
    assert all(packet.position == tuple(float(v) for v in block) for packet in damage_packets)


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
    assert server.broadcast_packets[0][0] == ShootFeedbackPacket.id
    hp_packet = SetHP(ByteReader(target_connection.sent_packets[0][1:]))
    assert hp_packet.hp == 30
    assert hp_packet.damage_type == 1


def test_peerless_bot_takes_normal_hitscan_damage_and_dies() -> None:
    """Bots are authoritative Player targets even without an ENet peer."""

    server = DummyServer()
    attacker, _ = make_player(
        server,
        0,
        "Attacker",
        TEAM1,
        C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    bot_connection = _BotConnection(server)
    bot = Player(1, "TargetBot", TEAM2, C.RIFLE_TOOL, bot_connection)
    bot_connection.player = bot
    bot.is_bot = True
    bot.spawn(106.5, 100.5, 60.0)
    server.players[bot.id] = bot

    attacker.set_tool(C.RIFLE_TOOL)
    aim_at(attacker, bot.position)
    combat = get_combat_system(server)
    assert combat.handle_shot(attacker, make_shoot_packet(attacker)) is True
    assert bot.health == 30
    assert bot.alive is True

    responses = [
        ShootResponse(ByteReader(data[1:]))
        for data in server.broadcast_packets
        if data[0] == ShootResponse.id
    ]
    assert len(responses) == 1
    assert responses[0].damage_by == attacker.id
    assert responses[0].damaged == 1
    assert responses[0].blood == 1
    assert attacker.x < responses[0].position_x <= bot.x
    assert math.isclose(responses[0].position_y, bot.y, abs_tol=1 / 32)

    attacker.next_shot_time = 0.0
    assert combat.handle_shot(attacker, make_shoot_packet(attacker, seed=2)) is True
    assert bot.health == 0
    assert bot.alive is False
    assert any(data[0] == KillAction.id for data in server.broadcast_packets)


def test_peerless_bot_fire_emits_remote_weapon_feedback() -> None:
    """A bot has no local client, so observers need packet 8 for its audio."""

    server = DummyServer()
    bot_connection = _BotConnection(server)
    bot = Player(7, "ShooterBot", TEAM1, C.RIFLE_TOOL, bot_connection)
    bot_connection.player = bot
    bot.is_bot = True
    bot.spawn(100.5, 100.5, 60.0)
    bot.set_tool(C.RIFLE_TOOL)
    server.players[bot.id] = bot

    get_combat_system(server).handle_shot(
        bot,
        make_shoot_packet(bot, orientation=(1.0, 0.0, 0.0), seed=73),
    )

    feedback = ShootFeedbackPacket(ByteReader(server.broadcast_packets[0][1:]))
    assert feedback.shooter_id == bot.id
    assert feedback.tool_id == C.RIFLE_TOOL
    assert feedback.seed == 73
    assert server.broadcast_excludes[0] is bot


def test_held_riot_shield_absorbs_half_of_frontal_direct_damage():
    server = DummyServer()
    attacker, _ = make_player(
        server, 0, "Attacker", TEAM1, C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    target, _ = make_player(
        server, 1, "Shield", TEAM2, C.RIOTSHIELD_TOOL,
        (106.5, 100.5, 60.0),
    )
    attacker.set_tool(C.RIFLE_TOOL)
    target.set_tool(C.RIOTSHIELD_TOOL, raw=True)
    target.input.can_display_weapon = True
    target.set_orientation_vector(-1.0, 0.0, 0.0)
    aim_at(attacker, target.position)

    get_combat_system(server)._resolve_hitscan(
        attacker, attacker.orientation, attacker.eye
    )

    assert target.health == 65  # rifle 70, retail shield absorbs 50%


def test_riot_shield_does_not_absorb_rear_or_hidden_weapon_hit():
    for facing, displayed in (((1.0, 0.0, 0.0), True),
                              ((-1.0, 0.0, 0.0), False)):
        server = DummyServer()
        attacker, _ = make_player(
            server, 0, "Attacker", TEAM1, C.RIFLE_TOOL,
            (100.5, 100.5, 60.0),
        )
        target, _ = make_player(
            server, 1, "Shield", TEAM2, C.RIOTSHIELD_TOOL,
            (106.5, 100.5, 60.0),
        )
        attacker.set_tool(C.RIFLE_TOOL)
        target.set_tool(C.RIOTSHIELD_TOOL, raw=True)
        target.input.can_display_weapon = displayed
        target.set_orientation_vector(*facing)
        aim_at(attacker, target.position)

        get_combat_system(server)._resolve_hitscan(
            attacker, attacker.orientation, attacker.eye
        )

        assert target.health == 30


def test_riot_shield_bash_uses_two_damage_and_half_unit_knockback():
    server = DummyServer()
    attacker, _ = make_player(
        server, 0, "Medic", TEAM1, C.RIOTSHIELD_TOOL,
        (100.5, 100.5, 60.0),
    )
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL,
        (102.5, 100.5, 60.0),
    )
    attacker.set_tool(C.RIOTSHIELD_TOOL, raw=True)
    aim_at(attacker, target.position)

    assert get_combat_system(server)._resolve_melee_hit(
        attacker, attacker.eye, attacker.orientation
    ) is True

    assert target.health == 98
    assert math.isclose(target.velocity[0], C.RIOTSHIELD_KNOCKBACK, abs_tol=1e-6)
    assert math.isclose(target.velocity[1], 0.0, abs_tol=1e-6)
    assert math.isclose(target.velocity[2], 0.0, abs_tol=1e-6)


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
    kill_data = next(
        data for data in server.broadcast_packets if data[0] == KillAction.id
    )
    kill_packet = KillAction(ByteReader(kill_data[1:]))
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
    assert [packet[0] for packet in server.broadcast_packets[:6]] == [8, 37, 8, 37, 8, 37]
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
    assert [packet[0] for packet in server.broadcast_packets] == [8]


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
    damage_packets = [raw for raw in server.broadcast_packets if raw[0] == 37]
    assert damage_packets
    assert any(raw[0] == 23 for raw in server.broadcast_packets)
    from shared.packet import Damage
    destroy_packet = Damage(ByteReader(damage_packets[-1][1:]))
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
    damage_packets = [raw for raw in server.broadcast_packets if raw[0] == 37]
    assert damage_packets
    assert any(raw[0] == 23 for raw in server.broadcast_packets)
    for packet_bytes in damage_packets:
        destroy_packet = Damage(ByteReader(packet_bytes[1:]))
        assert destroy_packet.damage >= 31.0


def test_retail_melee_footprints_keep_column_cube_and_single_cell_distinct():
    center = (101, 102, 60)

    assert combat_runtime._melee_dig_positions(
        center, combat_runtime.DIG_SINGLE
    ) == [center]
    assert combat_runtime._melee_dig_positions(
        center, combat_runtime.DIG_COLUMN
    ) == [(101, 102, 59), center, (101, 102, 61)]
    assert set(combat_runtime._melee_dig_positions(
        center, combat_runtime.DIG_CUBE
    )) == {
        (x, y, z)
        for x in range(100, 103)
        for y in range(101, 104)
        for z in range(59, 62)
    }


def test_zombie_hand_uses_the_native_cube_damage_handler() -> None:
    """IDA shows Zombie damage and Super Spade share the 3x3x3 handler."""

    assert combat_runtime.MELEE_DIG_PROFILES[int(C.ZOMBIEHAND_TOOL)] == (
        int(C.ZOMBIE_DAMAGE),
        float(C.ZOMBIEHAND_DAMAGE_AMOUNT),
        combat_runtime.DIG_CUBE,
    )


def test_zombie_hand_hits_players_through_shared_combat_authority() -> None:
    """Bot claws use normal LOS, damage, HP feedback, and player death rules."""

    server = DummyServer()
    zombie, _ = make_player(
        server, 0, "Zombie", TEAM2, C.ZOMBIEHAND_TOOL,
        (100.5, 100.5, 60.0),
    )
    survivor, survivor_connection = make_player(
        server, 1, "Survivor", TEAM1, C.RIFLE_TOOL,
        (103.0, 100.5, 60.0),
    )
    zombie.class_id = int(C.CLASS_ZOMBIE)
    zombie.loadout = [int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)]
    zombie.set_tool(C.ZOMBIEHAND_TOOL, raw=True)
    aim_at(zombie, survivor.position)

    accepted = get_combat_system(server).handle_shot(
        zombie,
        make_shoot_packet(zombie, orientation=zombie.orientation),
    )

    assert accepted is True
    stock_damage = round(
        float(C.ZOMBIEHAND_HITPLAYER_DAMAGE_AMOUNT)
        * float(C.ZOMBIE_DAMAGE_MULTIPLIER)
    )
    assert survivor.health == 100 - stock_damage
    hp = SetHP(ByteReader(survivor_connection.sent_packets[-1][1:]))
    assert hp.hp == survivor.health


def test_zombie_hand_removes_one_centered_cube_and_replicates_one_area_packet():
    """One claw swing commits the same 3x3x3 footprint the client predicts."""

    server = DummyServer()
    zombie, _ = make_player(
        server, 0, "Zombie", TEAM2, C.ZOMBIEHAND_TOOL,
        (100.5, 100.5, 60.0),
    )
    zombie.class_id = int(C.CLASS_ZOMBIE)
    zombie.loadout = [int(C.ZOMBIEHAND_TOOL), int(C.ZOMBIE_PREFAB_TOOL)]
    zombie.set_tool(C.ZOMBIEHAND_TOOL, raw=True)
    zombie.blocks = 0
    center = (103, 100, 60)
    footprint = {
        (x, y, z)
        for x in range(center[0] - 1, center[0] + 2)
        for y in range(center[1] - 1, center[1] + 2)
        for z in range(center[2] - 1, center[2] + 2)
    }
    for position in footprint:
        server.world_manager.set_block(*position, True, TEST_COLOR)
    server.world_manager.raycast = lambda *_args: center
    server.world_manager.find_unsupported_chunks = lambda _frontier: []

    accepted = get_combat_system(server).handle_shot(
        zombie,
        make_shoot_packet(zombie, orientation=(1.0, 0.0, 0.0)),
    )

    assert accepted is True
    assert all(not server.world_manager.get_solid(*pos) for pos in footprint)
    assert zombie.blocks == len(footprint)
    damage_packets = [data for data in server.broadcast_packets if data[0] == Damage.id]
    assert len(damage_packets) == 1
    damage = Damage(ByteReader(damage_packets[0][1:]))
    assert damage.type == int(C.ZOMBIE_DAMAGE)
    assert tuple(int(value) for value in damage.position) == center


def test_miner_superspade_shoot_removes_centered_3x3x3_atomically():
    server = DummyServer()
    player, _ = make_player(
        server, 0, "Miner", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.SUPERSPADE_TOOL)
    player.blocks = 0
    center = (103, 100, 60)
    footprint = {
        (x, y, z)
        for x in range(center[0] - 1, center[0] + 2)
        for y in range(center[1] - 1, center[1] + 2)
        for z in range(center[2] - 1, center[2] + 2)
    }
    outside = (center[0] + 2, center[1], center[2])
    for position in footprint | {outside}:
        server.world_manager.set_block(*position, True, TEST_COLOR)
    server.world_manager.raycast = lambda *_args: center
    server.world_manager.find_unsupported_chunks = lambda _frontier: []

    resolved = get_combat_system(server)._resolve_spade_dig(
        player, player.eye, player.orientation, SimpleNamespace()
    )

    assert resolved is True
    assert all(not server.world_manager.get_solid(*pos) for pos in footprint)
    assert server.world_manager.get_solid(*outside) is True
    assert player.blocks == 27
    # One native type-3 packet expands to the same cube on every client. A
    # per-cell type-3 flood would make each cell expand a second time.
    from shared.packet import Damage
    damage_packets = [data for data in server.broadcast_packets if data[0] == 37]
    assert len(damage_packets) == 1
    damage = Damage(ByteReader(damage_packets[0][1:]))
    assert damage.type == C.SUPERSPADE_DAMAGE
    assert damage.damage >= 31.0
    assert tuple(int(value) for value in damage.position) == center


def test_spade_mining_never_emits_crash_unsafe_shoot_feedback():
    """Spades animate through WorldUpdate; packet 8 calls a missing method."""

    server = DummyServer()
    player, _ = make_player(
        server, 0, "Miner", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.SPADE_TOOL)
    player.set_orientation_vector(1.0, 0.0, 0.0)
    block = (103, 100, 60)
    server.world_manager.set_block(*block, True, TEST_COLOR)
    server.world_manager.raycast = lambda *_args: block
    server.world_manager.find_unsupported_chunks = lambda _frontier: []

    get_combat_system(server).handle_shot(
        player,
        make_shoot_packet(player, orientation=(1.0, 0.0, 0.0), seed=29),
    )

    assert [raw[0] for raw in server.broadcast_packets] == [37, 23]


def test_legacy_superspade_liberate_uses_same_cube_and_one_native_damage():
    server = DummyServer()
    player, _ = make_player(
        server, 0, "Miner", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.SUPERSPADE_TOOL)
    player.blocks = 0
    center = (103, 100, 60)
    footprint = set(combat_runtime._melee_dig_positions(
        center, combat_runtime.DIG_CUBE
    ))
    for position in footprint:
        server.world_manager.set_block(*position, True, TEST_COLOR)
    server.world_manager.find_unsupported_chunks = lambda _frontier: []
    packet = BlockLiberate()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = center

    asyncio.run(PacketHandler(server).handle(
        player, bytes(packet.generate())
    ))

    assert all(not server.world_manager.get_solid(*pos) for pos in footprint)
    assert player.blocks == 27
    from shared.packet import Damage
    damage_packets = [raw for raw in server.broadcast_packets if raw[0] == 37]
    assert len(damage_packets) == 1
    assert any(raw[0] == 23 for raw in server.broadcast_packets)
    damage = Damage(ByteReader(damage_packets[0][1:]))
    assert damage.type == C.SUPERSPADE_DAMAGE
    assert tuple(int(value) for value in damage.position) == center


def test_block_build_consumes_inventory_and_clears_old_damage():
    server = DummyServer()
    player, connection = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
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


def test_rejected_predicted_build_is_queued_for_canonical_repair():
    server = DummyServer()
    attempted = []
    server.terrain_repair = SimpleNamespace(
        record_cells=lambda cells: attempted.extend(tuple(cells))
    )
    player, _connection = make_player(
        server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    # Valid coordinates but no face-connected support, so canonical VXL
    # rejects the client's attempted/predicted placement.
    position = (100, 100, 10)
    packet = BlockBuild()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x, packet.y, packet.z = position
    packet.block_type = BLOCK_ACTION_BUILD

    assert get_combat_system(server).handle_block_build(player, packet) is False
    assert attempted == [position]
    assert player.blocks == 5


def test_block_build_waits_for_its_originating_movement_loop():
    from server.metrics import RuntimeMetrics
    from server.world_mutations import WorldMutationService

    server = DummyServer()
    server.loop_count = 500
    server.metrics = RuntimeMetrics()
    server.world_mutations = WorldMutationService(server)
    player, _ = make_player(
        server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    player.last_applied_input_loop = 100
    block = (101, 100, 60)
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockBuild()
    packet.loop_count = 103
    packet.player_id = player.id
    packet.x, packet.y, packet.z = block
    packet.block_type = BLOCK_ACTION_BUILD
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 4
    assert server.world_manager.get_solid(*block) is False
    player.last_applied_input_loop = 103
    assert server.world_mutations.commit_ready() == 1
    assert server.world_manager.get_solid(*block) is True
    echoed = BlockBuild(ByteReader(server.broadcast_packets[-1][1:]))
    assert echoed.loop_count == 103


def test_pending_block_build_repairs_only_after_resolution():
    """Canonical air cannot race a valid but future-loop build mutation."""
    from server.metrics import RuntimeMetrics
    from server.world_mutations import WorldMutationService

    server = DummyServer()
    server.loop_count = 500
    server.config.world_mutation_timeout_ticks = 180
    server.metrics = RuntimeMetrics()
    server.world_mutations = WorldMutationService(server)
    attempted = []
    server.terrain_repair = SimpleNamespace(
        record_cells=lambda cells: attempted.extend(tuple(cells))
    )
    player, _ = make_player(
        server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    player.last_applied_input_loop = 100
    block = (101, 100, 60)
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockBuild()
    packet.loop_count = 10_000
    packet.player_id = player.id
    packet.x, packet.y, packet.z = block
    packet.block_type = BLOCK_ACTION_BUILD

    assert get_combat_system(server).handle_block_build(player, packet) is True
    assert attempted == []
    server.loop_count = 620
    assert server.world_mutations.commit_ready() == 0
    assert attempted == []
    server.loop_count = 680
    assert server.world_mutations.commit_ready() == 0
    assert attempted == [block]
    assert player.blocks == 5


def test_block_tool_destroy_waits_for_its_originating_movement_loop():
    from server.metrics import RuntimeMetrics
    from server.world_mutations import WorldMutationService

    server = DummyServer()
    server.loop_count = 500
    server.metrics = RuntimeMetrics()
    server.world_mutations = WorldMutationService(server)
    player, _ = make_player(
        server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0)
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 4
    player.last_applied_input_loop = 100
    block = (101, 100, 60)
    server.world_manager.set_block(*block, True, TEST_COLOR)

    packet = BlockLiberate()
    packet.loop_count = 103
    packet.player_id = player.id
    packet.x, packet.y, packet.z = block
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.blocks == 4
    assert server.world_manager.get_solid(*block) is True
    player.last_applied_input_loop = 103
    assert server.world_mutations.commit_ready() == 1
    assert server.world_manager.get_solid(*block) is False
    assert player.blocks == 5


def test_block_line_replicates_as_explicit_colored_cells():
    server = DummyServer()
    player, connection = make_player(server, 0, "Builder", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
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
    assert [data[0] for data in server.broadcast_packets] == [
        BlockBuildColored.id,
        BlockBuildColored.id,
        BlockBuildColored.id,
    ]
    replicated = [
        BlockBuildColored(ByteReader(data[1:]))
        for data in server.broadcast_packets
    ]
    assert [(p.x, p.y, p.z) for p in replicated] == [
        (101, 100, 60),
        (102, 100, 60),
        (103, 100, 60),
    ]
    assert all(p.player_id == player.id for p in replicated)
    assert all(p.color == player.block_color for p in replicated)
    assert len(connection.sent_packets) == 1
    own_echo = BlockLine(ByteReader(connection.sent_packets[0][1:]))
    assert own_echo.loop_count == packet.loop_count
    assert (own_echo.x1, own_echo.y1, own_echo.z1) == (101, 100, 60)
    assert (own_echo.x2, own_echo.y2, own_echo.z2) == (103, 100, 60)


def test_block_line_rejects_visually_identical_flare_tool():
    """Packet 40 is ordinary tool 5 only; flare tool 22 uses packet 104."""
    server = DummyServer()
    player, connection = make_player(
        server,
        0,
        "Builder",
        TEAM1,
        C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player.loadout = [int(C.BLOCK_TOOL), int(C.FLAREBLOCK_TOOL)]
    player.set_tool(C.FLAREBLOCK_TOOL, raw=True)
    player.blocks = 50
    block = (101, 100, 60)
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 1
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = block
    packet.x2, packet.y2, packet.z2 = block
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert server.world_manager.get_solid(*block) is False
    assert player.blocks == 50
    assert connection.sent_packets == []
    assert server.broadcast_packets == []


def test_block_line_waits_for_originating_movement_loop_before_collision_commit():
    """Tick-start packet drain cannot change an older frame's collision map."""

    from server.metrics import RuntimeMetrics
    from server.world_mutations import WorldMutationService

    server = DummyServer()
    server.loop_count = 500
    server.config.world_mutation_queue_limit = 64
    server.config.world_mutation_batch_limit = 16
    server.config.world_mutation_cell_budget = 64
    server.config.world_mutation_timeout_ticks = 180
    server.metrics = RuntimeMetrics()
    server.world_mutations = WorldMutationService(server)
    player, connection = make_player(
        server,
        0,
        "Builder",
        TEAM1,
        C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    player.last_applied_input_loop = 100
    block = (101, 100, 60)
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 103
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = block
    packet.x2, packet.y2, packet.z2 = block
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    # Inventory is reserved, but loops 100-102 still see the pre-build map.
    assert player.blocks == 4
    assert server.world_mutations.pending_count == 1
    assert server.world_manager.get_solid(*block) is False
    assert connection.sent_packets == []
    for loop_count in (100, 101, 102):
        player.last_applied_input_loop = loop_count
        assert server.world_mutations.commit_ready() == 0
        assert server.world_manager.get_solid(*block) is False

    player.last_applied_input_loop = 103
    assert server.world_mutations.commit_ready() == 1
    assert server.world_manager.get_solid(*block) is True
    assert server.world_mutations.pending_count == 0
    echoed = BlockLine(ByteReader(connection.sent_packets[0][1:]))
    assert echoed.loop_count == 103


def test_delayed_block_line_keeps_color_selected_when_action_was_sent():
    """A later palette click cannot recolour an already queued placement."""

    from server.metrics import RuntimeMetrics
    from server.world_mutations import WorldMutationService

    server = DummyServer()
    server.loop_count = 500
    server.config.world_mutation_queue_limit = 64
    server.config.world_mutation_batch_limit = 16
    server.config.world_mutation_cell_budget = 64
    server.config.world_mutation_timeout_ticks = 180
    server.metrics = RuntimeMetrics()
    server.world_mutations = WorldMutationService(server)
    player, _connection = make_player(
        server,
        0,
        "Builder",
        TEAM1,
        C.RIFLE_TOOL,
        (100.5, 100.5, 60.0),
    )
    player.set_tool(C.BLOCK_TOOL)
    player.blocks = 5
    player.last_applied_input_loop = 100
    action_color = 0x123456
    player.set_color(action_color)
    block = (101, 100, 60)
    server.world_manager.set_block(101, 100, 61, True, TEST_COLOR)

    packet = BlockLine()
    packet.loop_count = 103
    packet.player_id = player.id
    packet.x1, packet.y1, packet.z1 = block
    packet.x2, packet.y2, packet.z2 = block
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    # SetColor can arrive before the post-physics commit. The placed voxel and
    # observer packet still belong to the colour selected for packet 40.
    player.set_color(0xABCDEF)
    player.last_applied_input_loop = 103
    assert server.world_mutations.commit_ready() == 1

    replicated = BlockBuildColored(ByteReader(server.broadcast_packets[0][1:]))
    assert replicated.color == action_color
    assert server.world_manager.map.get_color_tuple(*block) == (
        0x12,
        0x34,
        0x56,
        0xFF,
    )



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
    assert [data[0] for data in server.broadcast_packets] == [
        BlockBuildColored.id,
        BlockBuildColored.id,
        BlockBuildColored.id,
    ]


def test_block_line_skips_existing_cells_and_builds_the_remaining_line():
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

    assert player.blocks == 3
    assert server.world_manager.get_solid(101, 100, 60) is True
    assert server.world_manager.get_solid(102, 100, 60) is True
    assert server.world_manager.get_solid(103, 100, 60) is True
    assert [data[0] for data in server.broadcast_packets] == [
        BlockBuildColored.id,
        BlockBuildColored.id,
    ]


def test_paint_block_updates_authoritative_color_and_replicates():
    server = DummyServer()
    player, _ = make_player(server, 0, "Painter", TEAM1, C.RIFLE_TOOL, (100.5, 100.5, 60.0))
    player.set_tool(C.BLOCK_TOOL)
    target = (101, 100, 61)
    server.world_manager.set_block(*target, True, TEST_COLOR)
    server.world_manager.dirty_columns.clear()

    packet = PaintBlockPacket()
    packet.loop_count = 12
    packet.x, packet.y, packet.z = target
    packet.color = (0x12, 0x34, 0x56)
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert server.world_manager.map.get_color_tuple(*target) == (0x12, 0x34, 0x56, 0xFF)
    assert target[:2] in server.world_manager.dirty_columns
    assert server.broadcast_packets[-1][0] == PaintBlockPacket.id
    echoed = PaintBlockPacket(ByteReader(server.broadcast_packets[-1][1:]))
    assert (echoed.x, echoed.y, echoed.z) == target
    assert echoed.color == (0x12, 0x34, 0x56)


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
    assert any(raw[0] == 37 for raw in server.broadcast_packets)
    assert any(raw[0] == 23 for raw in server.broadcast_packets)
