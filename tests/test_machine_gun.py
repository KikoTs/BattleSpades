import asyncio
import struct

import shared.constants as C
from protocol.packet_handler import PacketHandler
from server.config import ServerConfig
from server.entities.machine_gun import MachineGunBehavior
from server.entities.registry import EntityContext
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import ChangeEntity, CreateEntity, PlaceMG, UseCommand


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
    player = Player(0, "Gunner", TEAM1, C.MG_TOOL, connection)
    connection.player = player
    player.loadout = [int(C.MG_TOOL)]
    player.spawn(100.5, 100.5, 59.75)
    player.set_tool(C.MG_TOOL, raw=True)
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[TEAM1].add_player(player)
    return server, player, connection


def _place(server, player, x=101.0, y=100.0, z=62.0, yaw=35.0):
    raw = bytes([87]) + struct.pack(
        "<IBHHHH",
        10,
        99,  # must never be trusted by the server
        int(x),
        int(y),
        int(z),
        int(round(float(yaw) * 64.0)),
    )
    asyncio.run(PacketHandler(server).handle(player, raw))


def test_place_mg_wire_layout_roundtrips_all_fixed_fields():
    packet = PlaceMG()
    packet.loop_count = 0x01020304
    packet.player_id = 7
    packet.x, packet.y, packet.z = 12.5, 13.25, 14.75
    packet.yaw = -35.5

    raw = bytes(packet.generate())
    assert len(raw) == 14
    assert raw[0] == 87
    parsed = PlaceMG(ByteReader(raw[1:]))
    assert parsed.loop_count == 0x01020304
    assert parsed.player_id == 7
    assert (parsed.x, parsed.y, parsed.z) == (12.5, 13.25, 14.75)
    assert abs(parsed.yaw - (-35.5)) <= (1.0 / 64.0)


def test_place_mg_creates_join_safe_yawed_destructible_entity():
    server, player, connection = _server_player()
    _place(server, player)

    guns = [e for e in server.entity_registry.all() if e.type == C.MACHINE_GUN]
    assert len(guns) == 1
    gun = guns[0]
    assert gun.kind == "machine_gun"
    assert gun.yaw == 35.0
    assert gun.player_id == 0xFF
    assert isinstance(gun.behavior, MachineGunBehavior)
    assert gun.behavior.health == float(C.MG_HEALTH)
    assert gun.behavior.ammo == int(C.MG_AMMO)

    created = next(data for data in connection.sent if data[0] == 21)
    wire = CreateEntity(ByteReader(created[1:])).entity
    assert wire.entity_id == gun.entity_id
    assert wire.type == C.MACHINE_GUN
    assert wire.yaw == 35.0
    assert wire.player_id == 0xFF
    assert gun in server.entity_registry.static_entities()

    # MG_AMMO=999 is the client's local infinite-ammo sentinel. ChangeEntity
    # carries a signed fixed short (maximum 511.984375), so the server must not
    # corrupt it by trying to serialize 999 as a SET_AMMO property.
    assert not any(data[0] == 16 for data in connection.sent)


def test_place_mg_rejects_wrong_tool_far_or_duplicate_placement():
    server, player, _ = _server_player()
    player.set_tool(C.SMG_TOOL, raw=True)
    _place(server, player)
    assert not server.entity_registry.all()

    player.set_tool(C.MG_TOOL, raw=True)
    _place(server, player, x=107.0)
    assert not server.entity_registry.all()

    _place(server, player)
    _place(server, player, x=102.0)
    assert len(server.entity_registry.all()) == 1


def test_use_command_mounts_and_unmounts_nearest_machine_gun():
    server, player, connection = _server_player()
    _place(server, player)
    gun = server.entity_registry.all()[0]
    connection.sent.clear()

    use = UseCommand()
    asyncio.run(PacketHandler(server).handle(player, bytes(use.generate())))
    assert gun.behavior.carrier_id == player.id
    assert gun.player_id == player.id
    assert player.mounted_entity_id == gun.entity_id

    mounted = ChangeEntity(ByteReader(connection.sent[-1][1:]))
    assert mounted.action == C.SET_PLAYER
    assert mounted.entity_id == gun.entity_id
    assert mounted.player_id == player.id

    asyncio.run(PacketHandler(server).handle(player, bytes(use.generate())))
    assert gun.behavior.carrier_id is None
    assert gun.player_id == 0xFF
    assert player.mounted_entity_id is None

    unmounted = ChangeEntity(ByteReader(connection.sent[-1][1:]))
    assert unmounted.action == C.SET_PLAYER
    assert unmounted.player_id == 0xFF


def test_deployed_mg_uses_recovered_tenth_second_fire_interval(monkeypatch):
    server, player, _ = _server_player()
    player.input.is_weapon_deployed = True
    observed = []

    def consume(_now=None, fire_interval=None):
        observed.append(fire_interval)
        return False

    monkeypatch.setattr(player, "consume_shot", consume)
    packet = type("Shot", (), {
        "loop_count": 1,
        "x": player.eye[0], "y": player.eye[1], "z": player.eye[2],
        "ori_x": 1.0, "ori_y": 0.0, "ori_z": 0.0,
        "seed": 0, "shot_on_world_update": 0,
    })()

    server.combat = None
    from server.combat_runtime import get_combat_system
    get_combat_system(server).handle_shot(player, packet)

    assert observed == [float(C.MG_DEPLOYED_SHOOT_INTERVAL)]


def test_machine_gun_health_destruction_uses_recovered_blast_and_despawns():
    server, player, _ = _server_player()
    _place(server, player)
    gun = server.entity_registry.all()[0]
    blasts = []
    destroyed = []
    server._apply_blast = lambda *args, **kwargs: blasts.append((args, kwargs))
    ctx = EntityContext(
        dt=1 / 60.0, now=1000.0, players=[], server=server,
        destroy=destroyed.append,
    )

    gun.behavior.on_damage(gun, C.MG_HEALTH, player, ctx)

    assert server.entity_registry.get(gun.entity_id) is None
    assert destroyed == [gun.entity_id]
    args, kwargs = blasts[0]
    assert args[3:6] == (
        float(C.MG_EXPLOSION_DAMAGE),
        float(C.MG_EXPLOSION_BLOCK_DAMAGE),
        int(C.ENTITY_KILL),
    )
    assert kwargs["blast_radius"] == float(C.MG_EXPLOSION_RADIUS)
    assert kwargs["knockback_min"] == float(C.MG_EXPLOSION_KNOCKBACK_MIN)
    assert kwargs["knockback_max"] == float(C.MG_EXPLOSION_KNOCKBACK_MAX)
