import asyncio
import struct

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.class_selection import active_tool_authorized
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind
from server.game_constants import TEAM1
from server.game_constants import TEAM2, TEAM_SPECTATOR
from server.main import BattleSpadesServer
from server.player import Player
from server.connection import internal_team_to_wire
from server.entities.behaviors import (
    MedpackBehavior,
    ProximityMineBehavior,
    RadarStationBehavior,
    RemoteChargeBehavior,
    TimedExplosiveBehavior,
)
from server.entities.machine_gun import MachineGunBehavior
from shared.bytes import ByteReader
from shared.packet import (
    BlockSuckerPacket,
    ChangeTeam,
    CreateEntity,
    CreatePlayer,
    Damage,
    DetonateC4,
    DisguisePacket,
)


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(data)

    def on_disconnect(self):
        pass


def _server_player(tool, loadout):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(0, "EquipmentTest", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    player.class_id = {
        int(C.C4_TOOL): int(C.CLASS_MINER),
        int(C.DYNAMITE_TOOL): int(C.CLASS_MINER),
        int(C.BLOCK_SUCKER_TOOL): int(C.CLASS_MINER),
        int(C.ROCKET_TURRET_TOOL): int(C.CLASS_ENGINEER),
        int(C.MEDPACK_TOOL): int(C.CLASS_MEDIC),
        int(C.RADAR_STATION_TOOL): int(C.CLASS_SCOUT),
        int(C.DISGUISE_TOOL): int(C.CLASS_ENGINEER),
    }.get(int(tool), int(C.CLASS_SOLDIER))
    player.loadout = list(loadout)
    player.spawn(100.5, 100.5, 59.75)
    player.set_tool(tool, raw=True)
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[TEAM1].add_player(player)
    return server, player, connection


def test_c4_packet_places_oriented_entity_and_remote_detonates_it():
    server, player, _ = _server_player(C.C4_TOOL, [C.C4_TOOL])
    place = bytes([92]) + struct.pack("<IHHHB", 10, 101, 100, 62, 2)

    asyncio.run(PacketHandler(server).handle(player, place))

    charges = [entity for entity in server.entity_registry.all()
               if entity.type == C.C4_ENTITY]
    assert len(charges) == 1
    assert charges[0].type == 38  # retail GameScene.ENTITIES wire index
    assert charges[0].face == 2

    detonate = DetonateC4()
    detonate.loop_count = 11
    asyncio.run(PacketHandler(server).handle(player, bytes(detonate.generate())))

    assert server.entity_registry.get(charges[0].entity_id) is None
    assert player._c4_entity_ids == []


def test_dynamite_packet_preserves_attachment_face_for_render_and_blast():
    server, player, _ = _server_player(C.DYNAMITE_TOOL, [C.DYNAMITE_TOOL])
    place = bytes([1]) + struct.pack("<IHHHB", 10, 101, 100, 62, 3)

    asyncio.run(PacketHandler(server).handle(player, place))

    charges = [
        entity for entity in server.entity_registry.all()
        if entity.type == C.DYNAMITE_ENTITY
    ]
    assert len(charges) == 1
    assert charges[0].face == 3
    assert charges[0].behavior.get_explosion_center(charges[0]) == (
        101.5,
        101.0,
        62.5,
    )


def test_dynamite_detonation_sends_one_native_radius_packet_to_known_peers():
    server, player, connection = _server_player(
        C.DYNAMITE_TOOL,
        [C.DYNAMITE_TOOL],
    )
    place = bytes([1]) + struct.pack("<IHHHB", 10, 101, 100, 62, 4)
    asyncio.run(PacketHandler(server).handle(player, place))
    charge = next(
        entity for entity in server.entity_registry.all()
        if entity.type == C.DYNAMITE_ENTITY
    )

    missed_connection = _Connection(server)
    missed_connection.known_entity_ids = set()
    server.connections[object()] = missed_connection
    connection.sent.clear()
    server.world_manager.find_unsupported_chunks = lambda _positions: []
    charge.behavior._detonate_at = 0.0
    charge.behavior.on_tick(
        charge,
        1.0 / 60.0,
        server._build_entity_ctx(),
    )

    live_damage = [
        Damage(ByteReader(raw[1:]))
        for raw in connection.sent
        if raw[0] == Damage.id
    ]
    assert len(live_damage) == 1
    assert live_damage[0].type == int(C.DYNAMITE_DAMAGE)
    assert live_damage[0].causer_id == charge.entity_id
    assert not [
        raw for raw in missed_connection.sent
        if raw[0] in (Damage.id, 19)
    ]


def test_human_placed_rocket_turret_acquires_and_fires_at_an_enemy():
    server, player, _ = _server_player(
        C.ROCKET_TURRET_TOOL,
        [C.ROCKET_TURRET_TOOL],
    )
    enemy_connection = _Connection(server)
    enemy = Player(1, "Enemy", TEAM2, C.RIFLE_TOOL, enemy_connection)
    enemy_connection.player = enemy
    enemy.spawn(112.5, 100.5, 59.75)
    server.players[enemy.id] = enemy
    server.connections[enemy.id] = enemy_connection
    server.teams[TEAM2].add_player(enemy)

    # Retail packet 88 uses raw voxel shorts for xyz and fixed-point for yaw.
    packet = bytes([88]) + struct.pack(
        "<IBHHHh",
        10,
        player.id,
        102,
        100,
        60,
        int(90.0 * 64),
    )
    asyncio.run(
        PacketHandler(server).handle(player, packet)
    )
    turret = next(iter(server.rocket_turrets.values()))

    server.rocket_turret_controller.update(
        1.0,
        now=turret.next_shot_at,
    )

    assert turret.target_id == enemy.id
    assert turret.ammo == int(C.ROCKET_TURRET_AMMO) - 1
    assert len(server.projectile_engine.projectiles) == 1


def test_team_change_retires_owner_turret_before_allegiance_changes():
    server, player, _ = _server_player(
        C.ROCKET_TURRET_TOOL,
        [C.ROCKET_TURRET_TOOL],
    )
    turret = server.rocket_turret_controller.place(
        player,
        (102.0, 100.0, 60.0),
        yaw=0.0,
        now=0.0,
    )
    packet = ChangeTeam()
    packet.team = internal_team_to_wire(TEAM2)

    asyncio.run(
        PacketHandler(server).handle(player, bytes(packet.generate()))
    )

    assert player.team == TEAM2
    assert turret.entity_id not in server.rocket_turrets
    assert server.entity_registry.get(turret.entity_id) is None


def test_team_change_to_spectator_does_not_schedule_blue_respawn():
    server, player, connection = _server_player(
        C.RIFLE_TOOL, [C.RIFLE_TOOL]
    )
    packet = ChangeTeam()
    packet.team = TEAM_SPECTATOR

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.team == TEAM_SPECTATOR
    assert player.alive is False
    assert player.spawned is False
    assert player.death_time == 0.0
    create_packets = [
        CreatePlayer(ByteReader(data[1:]))
        for data in connection.sent
        if data[0] == CreatePlayer.id
    ]
    assert create_packets
    assert create_packets[-1].team == TEAM_SPECTATOR


def test_radar_packet_creates_real_entity_and_enables_team_visibility():
    server, player, connection = _server_player(
        C.RADAR_STATION_TOOL, [C.RADAR_STATION_TOOL]
    )
    place = bytes([91]) + struct.pack("<IBHHH", 10, player.id, 101, 100, 62)

    asyncio.run(PacketHandler(server).handle(player, place))

    radars = [entity for entity in server.entity_registry.all()
              if entity.type == C.RADAR_STATION_ENTITY]
    assert len(radars) == 1
    assert radars[0].type == 36  # retail GameScene.ENTITIES wire index
    assert radars[0].fuse == 35.0
    assert radars[0].behavior.lifetime == 35.0
    assert server._radar_station_counts[TEAM1] == 1
    assert any(packet[0] == 83 for packet in connection.sent)
    creates = [
        CreateEntity(ByteReader(packet[1:]))
        for packet in connection.sent
        if packet[0] == CreateEntity.id
    ]
    assert creates
    assert creates[-1].entity.fuse == 35.0


def test_medpack_packet_creates_retail_type_30_entity():
    server, player, connection = _server_player(
        C.MEDPACK_TOOL, [C.MEDPACK_TOOL]
    )
    place = bytes([90]) + struct.pack(
        "<IBHHHB", 10, player.id, 101, 100, 62, 4
    )

    asyncio.run(PacketHandler(server).handle(player, place))

    medpacks = [
        entity for entity in server.entity_registry.all()
        if entity.type == C.MEDPACK_ENTITY
    ]
    assert len(medpacks) == 1
    assert medpacks[0].type == 30  # retail GameScene.ENTITIES wire index
    assert any(packet[0] == 21 for packet in connection.sent)


def test_bot_gateway_and_packet_handler_share_deployable_service():
    """A bot cannot bypass the Medic loadout/class/entity replication path."""

    server, player, connection = _server_player(
        C.MEDPACK_TOOL, [C.MEDPACK_TOOL]
    )
    player.is_bot = True

    accepted = BotActionGateway(server).execute(
        player,
        BotAction(
            BotActionKind.DEPLOY,
            tool_id=C.MEDPACK_TOOL,
            position=(101.0, 100.0, 62.0),
            face=4,
        ),
    )

    assert accepted is True
    medpacks = [
        entity for entity in server.entity_registry.all()
        if entity.type == C.MEDPACK_ENTITY
    ]
    assert len(medpacks) == 1
    assert any(packet[0] == 21 for packet in connection.sent)

    player.class_id = int(C.CLASS_MINER)
    assert BotActionGateway(server).execute(
        player,
        BotAction(
            BotActionKind.DEPLOY,
            tool_id=C.MEDPACK_TOOL,
            position=(102.0, 100.0, 62.0),
            face=4,
        ),
    ) is False
    assert len(list(server.entity_registry.all())) == 1


def test_block_sucker_uses_client_block_granting_damage_type():
    server, player, connection = _server_player(
        C.BLOCK_SUCKER_TOOL,
        [C.BLOCK_SUCKER_TOOL],
    )
    player.blocks = 0
    hit = (101, 100, 60)
    server.world_manager.set_block(*hit, True, (80, 90, 100))
    server.world_manager.raycast = lambda *_args: hit
    packet = BlockSuckerPacket()
    packet.loop_count = 10
    packet.shooter_id = player.id
    packet.state = int(C.BLOCK_SUCKER_STATE_FULL_POWER)
    packet.shot = 1

    asyncio.run(
        PacketHandler(server).handle(player, bytes(packet.generate()))
    )

    assert player.blocks == 1
    damage_packets = [
        Damage(ByteReader(raw[1:]))
        for raw in connection.sent
        if raw[0] == Damage.id
    ]
    assert damage_packets
    assert damage_packets[-1].type == int(C.BLOCK_SUCKER_DAMAGE)


def test_disguise_packet_sets_authoritative_worldupdate_state_bit():
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    packet = DisguisePacket()
    packet.loop_count = 10
    packet.active = 1

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.disguised is True
    assert player.pack_state_flags() & 0x02


def test_disguise_uses_two_stock_charges_and_duplicate_activation_is_inert():
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    packet = DisguisePacket()
    packet.loop_count = 10
    packet.active = 1

    assert player.disguise_stock == C.DISGUISE_INITIAL_STOCK == 2
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    assert player.disguised is True
    assert player.disguise_stock == 1

    # Retransmitting the activation cannot consume another disguise.
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    assert player.disguise_stock == 1

    packet.active = 0
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    assert player.disguised is False

    player._disguise_next_use = 0.0
    packet.active = 1
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    assert player.disguised is True
    assert player.disguise_stock == 0

    packet.active = 0
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    player._disguise_next_use = 0.0
    packet.active = 1
    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))
    assert player.disguised is False
    assert player.disguise_stock == 0


def test_cross_class_deployable_packet_is_rejected():
    server, player, _ = _server_player(C.MEDPACK_TOOL, [C.MEDPACK_TOOL])
    player.class_id = int(C.CLASS_MINER)
    packet = bytes([90]) + struct.pack("<IBHHHB", 10, player.id, 101, 100, 62, 4)

    asyncio.run(PacketHandler(server).handle(player, packet))

    assert not [
        entity for entity in server.entity_registry.all()
        if entity.type == C.MEDPACK_ENTITY
    ]


def test_active_tool_gate_rejects_packet_tool_not_held_or_loaded():
    _server, player, _connection = _server_player(C.RPG_TOOL, [C.RPG_TOOL])

    assert active_tool_authorized(player, C.RPG_TOOL)
    assert not active_tool_authorized(player, C.SNOWBLOWER_TOOL)

    player.loadout = [C.SNOWBLOWER_TOOL]
    assert not active_tool_authorized(player, C.RPG_TOOL)


def test_disconnect_removes_owned_deployables_but_keeps_building_entities():
    """Owner-bound producers cannot survive until a numeric id is reused."""
    server, player, _connection = _server_player(C.C4_TOOL, [C.C4_TOOL])
    peer = next(key for key, value in server.connections.items()
                if value.player is player)
    owned = [
        server.entity_registry.place(
            C.DYNAMITE_ENTITY, 1, 1, 1, kind="deployable",
            player_id=player.id,
            behavior=TimedExplosiveBehavior(player.id, 7, 300, 7, 2, 15),
        ),
        server.entity_registry.place(
            C.LANDMINE_ENTITY, 2, 1, 1, kind="deployable",
            player_id=player.id,
            behavior=ProximityMineBehavior(player.id, player.team, 100, 15, 1, 14),
        ),
        server.entity_registry.place(
            C.C4_ENTITY, 3, 1, 1, kind="deployable",
            player_id=player.id,
            behavior=RemoteChargeBehavior(player.id),
        ),
        server.entity_registry.place(
            C.RADAR_STATION_ENTITY, 4, 1, 1, kind="deployable",
            player_id=player.id,
            behavior=RadarStationBehavior(player.team),
        ),
        server.entity_registry.place(
            C.MEDPACK_ENTITY, 5, 1, 1, kind="medpack",
            player_id=player.id,
            behavior=MedpackBehavior(player.team),
        ),
        server.entity_registry.place(
            C.MACHINE_GUN, 6, 1, 1, kind="machine_gun", player_id=0xFF,
            behavior=MachineGunBehavior(player.id, player.team),
        ),
    ]
    player._c4_entity_ids = [owned[2].entity_id]
    player._radar_entity_id = owned[3].entity_id
    server._radar_station_counts[player.team] = 1
    construction = server.entity_registry.place(
        C.FLARE_BLOCK, 7, 1, 1, kind="flare_block", player_id=player.id,
    )
    foreign_machine_gun = server.entity_registry.place(
        C.MACHINE_GUN, 8, 1, 1, kind="machine_gun", player_id=0xFF,
        behavior=MachineGunBehavior(99, player.team),
    )
    assert foreign_machine_gun.behavior.mount(
        foreign_machine_gun, player, server
    ) is True

    server._on_disconnect_sync(peer)

    assert all(server.entity_registry.get(entity.entity_id) is None for entity in owned)
    assert server.entity_registry.get(construction.entity_id) is construction
    assert server.entity_registry.get(
        foreign_machine_gun.entity_id
    ) is foreign_machine_gun
    assert foreign_machine_gun.behavior.carrier_id is None
    assert foreign_machine_gun.player_id == 0xFF
    assert server._radar_station_counts[player.team] == 0
