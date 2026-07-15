import asyncio
import math
from types import SimpleNamespace

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.pickups import broadcast_pickup
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import DropPickup, PickPickup, Restock, WorldUpdate


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


def _server_player():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    connection = _Connection(server)
    player = Player(0, "Carrier", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    player.spawn(100.5, 100.5, 59.75)
    server.players[player.id] = player
    server.connections[player.id] = connection
    return server, player, connection


def test_pick_and_drop_packet_wire_layouts_are_exact():
    pick = PickPickup()
    pick.player_id, pick.pickup_id, pick.burdensome = 7, C.INTEL_PICKUP, 1
    assert bytes(pick.generate()) == bytes((70, 7, C.INTEL_PICKUP, 1))

    drop = DropPickup()
    drop.loop_count = 12
    drop.player_id, drop.pickup_id = 7, C.INTEL_PICKUP
    drop.position = (10.5, 20.25, 30.75)
    drop.velocity = (15.0, 0.0, 0.0)
    raw = bytes(drop.generate())
    assert len(raw) == 19 and raw[0] == 71
    parsed = DropPickup(ByteReader(raw[1:]))
    assert parsed.player_id == 7 and parsed.pickup_id == C.INTEL_PICKUP
    assert parsed.position == drop.position and parsed.velocity == drop.velocity


def test_world_update_carries_only_valid_objective_pickup_id():
    server, player, _ = _server_player()
    assert broadcast_pickup(
        server, player, C.INTEL_PICKUP, burdensome=True, state=TEAM2
    )
    packet = WorldUpdate()
    packet[player.id] = player.world_update_snapshot()
    raw = bytes(packet.generate())
    row_start = 1 + 4 + 2
    assert raw[row_start + 49] == C.INTEL_PICKUP
    assert player._world_object.burdened is True


def test_late_join_reveal_announces_carrier_when_entity_wire_is_disabled():
    server, player, connection = _server_player()
    broadcast_pickup(server, player, C.INTEL_PICKUP, burdensome=True, state=TEAM2)
    server.config.entities_wire_ready = False
    revealed = []
    server.mode = SimpleNamespace(reveal_to=lambda target: revealed.append(target))
    connection.sent.clear()

    server.reveal_world_to(connection)

    picks = [data for data in connection.sent if data and data[0] == PickPickup.id]
    assert len(picks) == 1
    packet = PickPickup(ByteReader(picks[0][1:]))
    assert packet.player_id == player.id
    assert packet.pickup_id == C.INTEL_PICKUP
    assert packet.burdensome == 1
    assert revealed == [connection]


def test_drop_ignores_spoofed_identity_caps_speed_and_persists_entity():
    server, player, connection = _server_player()
    broadcast_pickup(server, player, C.INTEL_PICKUP, burdensome=True, state=TEAM2)
    connection.sent.clear()

    spoof = DropPickup()
    spoof.loop_count = 1
    spoof.player_id = 99
    spoof.pickup_id = C.DIAMOND_PICKUP
    spoof.position = player.position
    spoof.velocity = (999.0, 0.0, 0.0)
    asyncio.run(PacketHandler(server).handle(player, bytes(spoof.generate())))
    assert player.pickup_id == C.INTEL_PICKUP

    spoof.pickup_id = C.INTEL_PICKUP
    asyncio.run(PacketHandler(server).handle(player, bytes(spoof.generate())))
    assert player.pickup_id is None
    dropped = [e for e in server.entity_registry.all() if e.kind == "pickup"]
    assert len(dropped) == 1 and dropped[0].type == C.INTEL_PICKUP
    assert math.isclose(math.sqrt(sum(v * v for v in dropped[0].vel)), C.INTEL_THROW_SPEED)
    relayed = DropPickup(ByteReader(connection.sent[-1][1:]))
    assert relayed.player_id == player.id
    assert relayed.pickup_id == C.INTEL_PICKUP


def test_jetpack_crate_restock_refills_fuel_and_uses_type_six():
    server, player, connection = _server_player()
    player.jetpack_id = int(C.JETPACK_ENGINEER)
    player.jetpack_fuel = 3.0
    player.restock_jetpack()
    assert player.jetpack_fuel == 100.0
    packet = Restock(ByteReader(connection.sent[-1][1:]))
    assert packet.player_id == player.id and packet.type == C.JETPACK_CRATE


def test_physical_ammo_crate_uses_type_three_without_server_health_refill():
    _server, player, connection = _server_player()
    player.health = 41
    connection.sent.clear()

    player.restock_ammo(int(C.AMMO_CRATE))

    packet = Restock(ByteReader(connection.sent[-1][1:]))
    assert packet.player_id == player.id
    assert packet.type == C.AMMO_CRATE
    assert player.health == 41
