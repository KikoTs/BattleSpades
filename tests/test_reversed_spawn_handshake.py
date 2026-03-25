import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *args, **kwargs: {}))

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import (
    ClientInMenu,
    ClockSync,
    CreatePlayer,
    ExistingPlayer,
    NewPlayerConnection,
    SetClassLoadout,
    SetHP,
)

from protocol.packet_handler import PacketHandler
from server.connection import (
    Connection,
    DEFAULT_WIRE_TEAM,
    internal_team_to_wire,
    wire_team_to_internal,
)
from server.config import ServerConfig
from server.game_constants import PLAYER_HEIGHT, TEAM_NEUTRAL, TEAM_SPECTATOR, TEAM1, TEAM2
from server.player import Player
from server.team import Team
from server.world_manager import WorldManager


class DummyPeer:
    def __init__(self):
        self.address = ("127.0.0.1", 32887)
        self.disconnected_reason = None

    def disconnect(self, reason=0):
        self.disconnected_reason = reason


class DummyWorldManager:
    def __init__(self, spawn=(303.5, 62.5, 170.0)):
        self.map_name = "CityOfChicago"
        self.spawn = spawn
        self.spawn_calls = []

    def get_spawn_point(self, team):
        self.spawn_calls.append(team)
        return self.spawn


class DummyServer:
    def __init__(self):
        self.loop_count = 42
        self.players = {}
        self.connections = {}
        self.teams = {
            TEAM1: Team(TEAM1, "TEAM1_COLOR", (44, 117, 179)),
            TEAM2: Team(TEAM2, "TEAM2_COLOR", (137, 179, 44)),
        }
        self.world_manager = DummyWorldManager()
        self.mode = None
        self.broadcast_packets = []
        self.config = SimpleNamespace(
            max_players=32,
            log_suppress_packets=set(),
            respawn_time=5.0,
        )

    def get_next_player_id(self):
        for player_id in range(self.config.max_players):
            if player_id not in self.players:
                return player_id
        return -1

    def broadcast(self, data, exclude=None):
        self.broadcast_packets.append(data)


def make_connection(server):
    return Connection(DummyPeer(), server)


def test_team_mapping_helpers():
    assert wire_team_to_internal(TEAM1) == TEAM1
    assert wire_team_to_internal(TEAM2) == TEAM2
    assert wire_team_to_internal(TEAM_SPECTATOR) is None
    assert wire_team_to_internal(TEAM_NEUTRAL) is None
    assert wire_team_to_internal(99) is None
    assert internal_team_to_wire(TEAM1) == TEAM1
    assert internal_team_to_wire(TEAM2) == TEAM2
    assert internal_team_to_wire(99) == DEFAULT_WIRE_TEAM


def test_pre_join_loadout_is_cached():
    server = DummyServer()
    connection = make_connection(server)

    packet = SetClassLoadout()
    packet.player_id = 255
    packet.class_id = 0
    packet.instant = 0
    packet.loadout = [5, 1, 8, 13]
    packet.prefabs = ["prefab_ultrabarrier", "prefab_superbarrier"]
    packet.ugc_tools = [23, 30]

    asyncio.run(connection.handle_pre_join_packet(bytes(packet.generate())))

    assert connection.pending_class_id == 0
    assert connection.pending_loadout == [5, 1, 8, 13]
    assert connection.pending_prefabs == [
        "prefab_ultrabarrier",
        "prefab_superbarrier",
    ]
    assert connection.pending_ugc_tools == [23, 30]


def test_world_manager_height_scans_surface_from_solids():
    world_manager = WorldManager(ServerConfig())

    class FakeMap:
        def get_solid(self, x, y, z):
            return x == 64 and y == 64 and z in {120, 121, 122}

    world_manager.map = FakeMap()
    world_manager.world = None

    assert world_manager.get_height(64, 64) == 120


def test_world_manager_spawn_ignores_stale_random_pos_z():
    world_manager = WorldManager(ServerConfig())

    class FakeMap:
        def get_random_pos(self, x1, y1, x2, y2):
            return (100, 200, 122)

        def get_solid(self, x, y, z):
            return x == 100 and y == 200 and z in {120, 121, 122}

    world_manager.map = FakeMap()
    world_manager.world = None

    assert world_manager.get_spawn_point(TEAM_NEUTRAL) == (100.5, 200.5, 120.0 - PLAYER_HEIGHT)


def test_pre_join_clock_sync_and_menu_state_are_handled():
    server = DummyServer()
    connection = make_connection(server)
    sent_packets = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append(data)

    menu_packet = ClientInMenu()
    menu_packet.in_menu = 1
    asyncio.run(connection.handle_pre_join_packet(bytes(menu_packet.generate())))

    clock_packet = ClockSync()
    clock_packet.client_time = 5678
    clock_packet.server_loop_count = 0
    asyncio.run(connection.handle_pre_join_packet(bytes(clock_packet.generate())))

    assert connection.in_menu is True

    response = ClockSync(ByteReader(sent_packets[0][1:]))
    assert response.client_time == 5678
    assert response.server_loop_count == server.loop_count


def test_spawn_uses_cached_loadout_and_sends_hp():
    server = DummyServer()
    connection = make_connection(server)
    sent_packets = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append(data)

    connection.pending_class_id = 0
    connection.pending_loadout = [5, 1, 8, 13]
    connection.pending_prefabs = ["prefab_ultrabarrier", "prefab_superbarrier"]

    join_packet = NewPlayerConnection()
    join_packet.team = TEAM2
    join_packet.class_id = 2
    join_packet.forced_team = 0
    join_packet.local_language = 0
    join_packet.name = "KikoTs"

    asyncio.run(connection._on_new_player(join_packet))

    assert connection.player is not None
    assert connection.player.team == TEAM2
    assert connection.player.alive is True
    assert server.world_manager.spawn_calls == [TEAM2]
    assert connection.player in server.teams[TEAM2].players

    create_player = CreatePlayer(ByteReader(server.broadcast_packets[0][1:]))
    assert create_player.team == TEAM2
    assert create_player.dead == 0
    assert create_player.class_id == 0
    assert (create_player.x, create_player.y, create_player.z) == connection.player.position
    assert create_player.loadout == [5, 1, 8, 13]
    assert create_player.prefabs == [
        "prefab_ultrabarrier",
        "prefab_superbarrier",
    ]

    set_hp = SetHP(ByteReader(sent_packets[0][1:]))
    assert set_hp.hp == 100
    assert set_hp.damage_type == 2
    assert (set_hp.source_x, set_hp.source_y, set_hp.source_z) == (0.0, 0.0, 0.0)


def test_existing_player_packets_use_wire_team_ids():
    server = DummyServer()
    connection = make_connection(server)
    sent_packets = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append(data)

    existing_player = Player(7, "Other", TEAM2, C.RIFLE_TOOL, None)
    existing_player.class_id = 2
    existing_player.alive = True
    existing_player.kills = 4
    existing_player.block_color = 0x123456
    server.players[existing_player.id] = existing_player

    asyncio.run(connection.send_existing_players())

    packet = ExistingPlayer(ByteReader(sent_packets[0][1:]))
    assert packet.player_id == 7
    assert packet.team == TEAM2


def test_joined_clock_sync_replies_with_server_loop_count():
    server = DummyServer()
    connection = make_connection(server)
    sent_packets = []
    connection.send = lambda data, reliable=True, prefix=0x30: sent_packets.append(data)

    player = Player(0, "KikoTs", TEAM1, C.RIFLE_TOOL, connection)

    packet = ClockSync()
    packet.client_time = 1234
    packet.server_loop_count = 0

    asyncio.run(PacketHandler(server).handle(player, bytes(packet.generate())))

    response = ClockSync(ByteReader(sent_packets[0][1:]))
    assert response.client_time == 1234
    assert response.server_loop_count == server.loop_count
