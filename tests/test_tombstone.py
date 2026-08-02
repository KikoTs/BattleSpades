"""Normal corpse-to-grave retail lifecycle regressions."""

import shared.constants as C
from server.class_selection import normalize_class_selection
from server.config import ServerConfig
from server.game_constants import TEAM1
from server.main import BattleSpadesServer
from server.player import Player
from shared.bytes import ByteReader
from shared.packet import ExplodeCorpse, KillAction, WorldUpdate


class _Connection:
    def __init__(self, server):
        self.server = server
        self.sent = []

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(data)


def _player(
    server,
    *,
    player_id=0,
    name="GraveTest",
    weapon=C.RIFLE_TOOL,
):
    connection = _Connection(server)
    player = Player(player_id, name, TEAM1, weapon, connection)
    player.spawn(100.5, 100.5, 59.75)
    server.players[player.id] = player
    server.teams[TEAM1].add_player(player)
    return player


def _packet(data: bytes, packet_type):
    assert data[0] == packet_type.id
    return packet_type(ByteReader(data[1:]))


def test_normal_death_explodes_corpse_then_spawns_falling_team_grave(
    monkeypatch,
):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    player = _player(server)
    player.velocity = (0.25, -0.5, 0.125)
    broadcasts: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )
    monkeypatch.setattr(server, "_apply_blast", lambda *args, **kwargs: None)

    player.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    assert [data[0] for data in broadcasts] == [
        KillAction.id,
        ExplodeCorpse.id,
    ]
    explode = _packet(broadcasts[1], ExplodeCorpse)
    assert explode.player_id == player.id
    assert explode.show_explosion_effect == 1

    grave = server.entity_registry.get(player._grave_entity_id)
    assert grave is not None
    assert grave.type == C.GRAVE_ENTITY
    assert grave.state == TEAM1
    assert grave.color == tuple(server.teams[TEAM1].color)
    # GraveEntity owns gravity/bounce client-side. The server must preserve
    # the airborne corpse transform instead of snapping packet 21 to terrain.
    assert (grave.x, grave.y, grave.z) == (100.5, 100.5, 59.75)
    assert grave.vel == (0.25, -0.5, 0.125)
    assert grave.behavior.get_explosion_center(grave) == (
        server.world_manager.dry_surface_anchor(100.5, 100.5, search=0)
    )
    assert grave.behavior.fuse == C.GRAVE_EXPLOSION_FUSE
    assert grave.behavior.damage == C.GRAVE_EXPLOSION_DAMAGE
    assert grave.behavior.blast_radius == C.GRAVE_EXPLOSION_RADIUS


def test_jetpack_death_flies_for_retail_fuse_before_explosion_and_grave(
    monkeypatch,
):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    player = _player(
        server,
        name="RocketGrave",
        weapon=C.SMG_TOOL,
    )
    player.apply_class_selection(normalize_class_selection(C.CLASS_ROCKETEER))
    player.spawn(100.5, 100.5, 59.75)
    assert player.jetpack_id == int(C.JETPACK2)
    assert player._ensure_world_object().jetpack == int(C.JETPACK2)
    broadcasts: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )
    monkeypatch.setattr(server, "_apply_blast", lambda *args, **kwargs: None)

    player.die(killer=None, kill_type=int(C.KILL.FALL_KILL))

    assert [data[0] for data in broadcasts] == [KillAction.id]
    assert player._grave_entity_id is None

    dt = 1.0 / 60.0
    for _ in range(30):
        server.corpse_lifecycle.tick(dt)

    # KillAction leaves the retail Character alive as a visible corpse.  Its
    # native receiver accepts WorldUpdate movement while dead, so the server
    # must retain this row for the bounded flight instead of filtering it with
    # ordinary dead players.
    update = WorldUpdate(ByteReader(server.build_world_update_data()[1:]))
    assert player.id in update.player_updates
    assert update.player_updates[player.id][0][2] < 59.75
    # Dead players intentionally fail live-tool authorization. The no-op tool
    # sentinel lets retail consume the corpse transform without constructing a
    # new weapon while its Character is already dead.
    assert update.player_updates[player.id][9] == 0xFF

    owner_update = WorldUpdate(
        ByteReader(
            server.build_world_update_data(local_player_id=player.id)[1:]
        )
    )
    assert owner_update.player_updates[player.id][9] == 0xFF

    for _ in range(29):
        server.corpse_lifecycle.tick(dt)
    assert player._grave_entity_id is None
    assert [data[0] for data in broadcasts] == [KillAction.id]

    server.corpse_lifecycle.tick(dt)

    assert [data[0] for data in broadcasts] == [
        KillAction.id,
        ExplodeCorpse.id,
    ]
    grave = server.entity_registry.get(player._grave_entity_id)
    assert grave is not None
    # AoS uses +Z down. The recovered dead-jetpack branch accelerates upward,
    # so both the corpse handoff and its initial grave velocity must be above.
    assert grave.z < 59.75
    assert grave.vel[2] < 0.0
    assert grave.behavior.get_explosion_center(grave)[2] > grave.z
    completed_update = WorldUpdate(
        ByteReader(server.build_world_update_data()[1:])
    )
    assert player.id not in completed_update.player_updates


def test_pre_fuse_respawn_silently_retires_old_jetpack_corpse(monkeypatch):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    player = _player(server, name="FastRespawn")
    player.jetpack_id = int(C.JETPACK_ENGINEER)
    player._ensure_world_object().jetpack = int(C.JETPACK_ENGINEER)
    broadcasts: list[bytes] = []
    monkeypatch.setattr(
        server,
        "broadcast",
        lambda data, *args, **kwargs: broadcasts.append(bytes(data)),
    )

    player.die(killer=None, kill_type=int(C.KILL.FALL_KILL))
    server.corpse_lifecycle.tick(0.25)
    server.corpse_lifecycle.before_player_spawn(player)

    assert player._grave_entity_id is None
    assert [data[0] for data in broadcasts] == [
        KillAction.id,
        ExplodeCorpse.id,
    ]
    assert _packet(broadcasts[1], ExplodeCorpse).show_explosion_effect == 0
