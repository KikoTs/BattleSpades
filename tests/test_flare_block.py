import asyncio
import struct

import shared.constants as C
from protocol.packet_handler import PacketHandler
from protocol.runtime_packets import (
    RuntimePlaceFlareBlock,
    decode_runtime_packet,
)
from server.config import ServerConfig
from server.entities.flare_block import FlareBlockBehavior
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import CreateEntity, DestroyEntity


class _Connection:
    def __init__(self, server, *, in_game=True):
        self.server = server
        self.player = None
        self.in_game = in_game
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


def _server_player():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(0, "Builder", TEAM1, C.FLAREBLOCK_TOOL, connection)
    connection.player = player
    player.loadout = [int(C.FLAREBLOCK_TOOL)]
    player.spawn(100.5, 100.5, 59.75)
    player.set_tool(C.FLAREBLOCK_TOOL, raw=True)
    player.blocks = 50
    player.block_color = 0x123456
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[TEAM1].add_player(player)
    return server, player, connection


def _place(server, player, x=104, y=100, z=61, loop_count=10):
    # Retail packet 104 is id + loop:u32 + three raw voxel u16s.
    data = bytes([104]) + struct.pack("<IHHH", loop_count, x, y, z)
    asyncio.run(PacketHandler(server).handle(player, data))


def test_packet_104_runtime_decoder_preserves_captured_raw_voxel_coordinates():
    # Captured from the stock client: loop=0x01f765, xyz=(212, 221, 238).
    payload = bytes.fromhex("65 F7 01 00 D4 00 DD 00 EE 00")
    packet = decode_runtime_packet(104, payload)
    assert isinstance(packet, RuntimePlaceFlareBlock)
    assert packet.loop_count == 0x01F765
    assert (packet.x, packet.y, packet.z) == (212, 221, 238)


def test_flare_placement_cost_color_team_wire_and_late_join_snapshot():
    server, player, connection = _server_player()
    _place(server, player)

    flares = [ent for ent in server.entity_registry.all() if ent.type == C.FLARE_BLOCK]
    assert len(flares) == 1
    flare = flares[0]
    assert (flare.x, flare.y, flare.z) == (104.0, 100.0, 61.0)
    assert flare.kind == "flare_block"
    assert flare.player_id == player.id
    assert flare.state == TEAM1
    assert flare.color == (0x12, 0x34, 0x56)
    assert isinstance(flare.behavior, FlareBlockBehavior)
    assert flare.behavior.health == float(C.DEFAULT_BLOCK_HEALTH)
    assert player.blocks == 50 - C.FLAREBLOCK_COST

    created = next(data for data in connection.sent if data[0] == 21)
    wire = CreateEntity(ByteReader(created[1:])).entity
    assert wire.type == C.FLARE_BLOCK
    assert wire.entity_id == flare.entity_id
    assert wire.state == TEAM1
    assert wire.player_id == player.id
    assert wire.color == (0x12, 0x34, 0x56)
    assert (wire.pos_x, wire.pos_y, wire.pos_z) == (104.0, 100.0, 61.0)

    # Flare entities are static registry state, so a client that joins after
    # placement receives the same CreateEntity during reveal_world_to.
    late = _Connection(server, in_game=False)
    server.reveal_world_to(late)
    late_creates = [CreateEntity(ByteReader(data[1:])).entity
                    for data in late.sent if data[0] == 21]
    assert any(entity.entity_id == flare.entity_id and entity.type == C.FLARE_BLOCK
               for entity in late_creates)


def test_flare_placement_rejects_wrong_tool_far_unsupported_duplicate_and_low_budget():
    server, player, _ = _server_player()
    player.set_tool(C.SMG_TOOL, raw=True)
    _place(server, player)
    assert not server.entity_registry.all()

    player.set_tool(C.FLAREBLOCK_TOOL, raw=True)
    _place(server, player, x=120)
    _place(server, player, x=104, z=50)  # unsupported floating light
    assert not server.entity_registry.all()

    player.blocks = C.FLAREBLOCK_COST - 1
    _place(server, player)
    assert not server.entity_registry.all()

    player.blocks = 50
    _place(server, player)
    _place(server, player)  # retransmit/duplicate must not double-charge
    assert len(server.entity_registry.all()) == 1
    assert player.blocks == 50 - C.FLAREBLOCK_COST


def test_packet_104_rejects_visually_identical_normal_block_tool():
    """Packet identity cannot silently turn tool 5 into flare tool 22."""
    server, player, connection = _server_player()
    player.loadout = [int(C.BLOCK_TOOL), int(C.FLAREBLOCK_TOOL)]
    player.set_tool(C.BLOCK_TOOL, raw=True)

    _place(server, player)

    assert server.entity_registry.all() == []
    assert player.blocks == 50
    assert connection.sent == []


def test_flare_block_is_allowed_on_retail_water_plane():
    server, player, _ = _server_player()
    player.set_position(104.5, 100.5, 235.5)
    assert not server.world_manager.get_solid(108, 100, C.Z_ABOVE_WATERPLANE)
    assert server.world_manager.get_solid(108, 100, C.Z_ABOVE_WATERPLANE + 1)

    _place(server, player, x=108, y=100, z=C.Z_ABOVE_WATERPLANE)

    flare = server.entity_registry.all()[0]
    assert flare.type == C.FLARE_BLOCK
    assert (flare.x, flare.y, flare.z) == (108.0, 100.0, 238.0)


def test_flare_destruction_removes_registry_entity_and_broadcasts_light_cleanup():
    server, player, connection = _server_player()
    _place(server, player)
    flare = server.entity_registry.all()[0]
    connection.sent.clear()

    flare.behavior.on_damage(
        flare, C.DEFAULT_BLOCK_HEALTH, player, server._build_entity_ctx()
    )

    assert server.entity_registry.get(flare.entity_id) is None
    destroyed_data = next(data for data in connection.sent if data[0] == 19)
    destroyed = DestroyEntity(ByteReader(destroyed_data[1:]))
    assert destroyed.entity_id == flare.entity_id


def test_flare_loses_support_and_uses_same_destroy_entity_cleanup_path():
    server, player, connection = _server_player()
    _place(server, player)
    flare = server.entity_registry.all()[0]
    connection.sent.clear()
    assert server.world_manager.set_block(104, 100, 62, False)

    server.entity_registry.tick(server._build_entity_ctx())

    assert server.entity_registry.get(flare.entity_id) is None
    assert any(data[0] == 19 for data in connection.sent)
