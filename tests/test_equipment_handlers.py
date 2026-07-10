import asyncio

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from shared.packet import DetonateC4, DisguisePacket, PlaceC4, PlaceRadarStation


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(data)


def _server_player(tool, loadout):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(0, "EquipmentTest", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    player.loadout = list(loadout)
    player.spawn(100.5, 100.5, 59.75)
    player.set_tool(tool, raw=True)
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[TEAM1].add_player(player)
    return server, player, connection


def test_c4_packet_places_oriented_entity_and_remote_detonates_it():
    server, player, _ = _server_player(C.C4_TOOL, [C.C4_TOOL])
    place = PlaceC4()
    place.loop_count = 10
    place.x, place.y, place.z = 101.0, 100.0, 62.0
    place.face = 2

    asyncio.run(PacketHandler(server).handle(player, bytes(place.generate())))

    charges = [entity for entity in server.entity_registry.all()
               if entity.type == C.C4_ENTITY]
    assert len(charges) == 1
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
    place = PlaceRadarStation()
    place.loop_count = 10
    place.player_id = player.id
    place.x, place.y, place.z = 101.0, 100.0, 62.0

    asyncio.run(PacketHandler(server).handle(player, bytes(place.generate())))

    radars = [entity for entity in server.entity_registry.all()
              if entity.type == C.RADAR_STATION_ENTITY]
    assert len(radars) == 1
    assert server._radar_station_counts[TEAM1] == 1
    assert any(packet[0] == 83 for packet in connection.sent)
    assert any(packet[0] == 21 for packet in connection.sent)


def test_disguise_packet_sets_authoritative_worldupdate_state_bit():
    server, player, _ = _server_player(C.DISGUISE_TOOL, [C.DISGUISE_TOOL])
    packet = DisguisePacket()
    packet.loop_count = 10
    packet.active = 1

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    assert player.disguised is True
    assert player.pack_state_flags() & 0x02
