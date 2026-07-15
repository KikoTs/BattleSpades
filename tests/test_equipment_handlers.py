import asyncio
import struct

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.class_selection import active_tool_authorized
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotAction, BotActionKind
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from server.entities.behaviors import (
    MedpackBehavior,
    ProximityMineBehavior,
    RadarStationBehavior,
    RemoteChargeBehavior,
    TimedExplosiveBehavior,
)
from server.entities.machine_gun import MachineGunBehavior
from shared.packet import DetonateC4, DisguisePacket


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
    assert server._radar_station_counts[TEAM1] == 1
    assert any(packet[0] == 83 for packet in connection.sent)
    assert any(packet[0] == 21 for packet in connection.sent)


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
