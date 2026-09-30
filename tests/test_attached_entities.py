"""Stuck sticky grenades (entity 35) and the riot-shield anchor (entity 39).

The retail client has one class for the flying sticky (34) and another for
the stuck one (35, AttachedStickyGrenadeEntity), and plays shield impacts
through HitEntity on a RiotShieldEntity (39). These tests pin the packets the
server sends for both; the contract is in docs/PROTOCOL.md.
"""
from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import ChangeEntity, CreateEntity, DestroyEntity, HitEntity

from server.config import ServerConfig
from server.entities import attachments
from server.main import BattleSpadesServer

DT = 1.0 / 60.0
STICKY = int(C.STICKY_GRENADE_TOOL)
FLYING = int(C.STICKY_GRENADE_ENTITY)
STUCK = int(C.ATTACHED_STICKY_GRENADE_ENTITY)
SHIELD = int(C.RIOT_SHIELD_ENTITY)


class RecordingConnection:
    def __init__(self, player=None, in_game=True, known_players=None):
        self.player = player
        self.in_game = in_game
        self.sent = []
        self.known_entity_ids = set()
        if known_players is not None:
            self.known_player_lives = {pid: (0, 0) for pid in known_players}

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


class WallWorld:
    def __init__(self, wall_x):
        self.wall_x = wall_x

    def get_solid(self, x, y, z):
        return x >= self.wall_x


class OpenWorld:
    def get_solid(self, x, y, z):
        return False


def _decode(data):
    reader = ByteReader(data[1:])
    if data[0] == CreateEntity.id:
        entity = CreateEntity(reader).entity
        return ("create", entity.entity_id, entity.type, entity)
    if data[0] == DestroyEntity.id:
        return ("destroy", DestroyEntity(reader).entity_id)
    if data[0] == ChangeEntity.id:
        packet = ChangeEntity(reader)
        return ("change", packet.entity_id, packet.action, packet)
    if data[0] == HitEntity.id:
        packet = HitEntity(reader)
        return ("hit", packet.entity_id, packet.type, packet)
    return ("other", data[0])


def _close(wire, exact):
    """Wire positions are 1/64 fixed point."""
    return all(abs(a - b) <= 1.0 / 64.0 for a, b in zip(wire, exact))


def _entity_packets(connection):
    return [
        item for item in (_decode(data) for data in connection.sent)
        if item[0] != "other"
    ]


def _player(player_id, team=0, **position):
    values = {"x": 100.0, "y": 100.0, "z": 29.0}
    values.update(position)
    return SimpleNamespace(
        id=player_id, name=f"p{player_id}", team=team, alive=True,
        spawned=True, input=SimpleNamespace(crouch=False), **values,
    )


def _server(world, *players):
    server = BattleSpadesServer(ServerConfig())
    server.world_manager = world
    server.players = {player.id: player for player in players}
    server.connections = {
        player.id: RecordingConnection(player) for player in players
    }
    server._apply_blast = lambda *args, **kwargs: None
    return server


def _throw(server, thrower):
    packet = SimpleNamespace(
        tool=STICKY, position=(100.0, 100.0, 30.0),
        velocity=(40.0, 0.0, 0.0), value=0.0,
    )
    assert server.spawn_grenade(thrower, packet) is True
    return server.projectile_engine.projectiles[0]


def _fly_until_stuck(server, projectile, ticks=120):
    for _ in range(ticks):
        server._update_grenades(DT)
        if projectile.stuck:
            return
    raise AssertionError("the sticky never stuck")


def test_sticky_on_terrain_swaps_the_flying_entity_for_the_stuck_one():
    thrower, observer = _player(1), _player(2, team=1, y=140.0)
    server = _server(WallWorld(105), thrower, observer)
    projectile = _throw(server, thrower)
    flying_id = projectile.entity_id

    _fly_until_stuck(server, projectile)

    stuck_id = projectile.entity_id
    assert stuck_id != flying_id
    for connection in server.connections.values():
        packets = _entity_packets(connection)
        assert [item[:3] for item in packets] == [
            ("create", flying_id, FLYING),
            ("destroy", flying_id),
            ("create", stuck_id, STUCK),
        ]
        stuck = packets[2][3]
        assert _close(
            (stuck.pos_x, stuck.pos_y, stuck.pos_z),
            (projectile.x, projectile.y, projectile.z),
        )
        assert stuck.player_id == thrower.id
        assert stuck.state == packets[0][3].state
        assert 4.5 < stuck.fuse <= float(C.STICKY_GRENADE_STICK_FUSE)
    assert [entity.type for entity in server.entity_registry.all()] == [STUCK]
    # A joining client's snapshot holds no five-second projectile.
    assert server.entity_registry.static_entities() == []


def test_stuck_sticky_explodes_by_destroying_the_stuck_entity():
    thrower = _player(1)
    server = _server(WallWorld(105), thrower)
    projectile = _throw(server, thrower)
    _fly_until_stuck(server, projectile)
    stuck_id = projectile.entity_id
    blasts = []
    server._apply_blast = lambda *args, **kwargs: blasts.append(args)

    projectile.explode_at = 0.0
    server._update_grenades(DT)

    assert _entity_packets(server.connections[1])[-1] == ("destroy", stuck_id)
    assert server.entity_registry.all() == []
    assert len(blasts) == 1


def test_sticky_on_a_player_names_the_target_after_the_create():
    thrower = _player(1)
    victim = _player(2, team=1, x=105.0)
    server = _server(OpenWorld(), thrower, victim)
    projectile = _throw(server, thrower)
    flying_id = projectile.entity_id

    _fly_until_stuck(server, projectile)

    assert projectile.attached_player_id == victim.id
    stuck_id = projectile.entity_id
    packets = _entity_packets(server.connections[2])
    assert [item[:3] for item in packets] == [
        ("create", flying_id, FLYING),
        ("destroy", flying_id),
        ("create", stuck_id, STUCK),
        ("change", stuck_id, int(C.SET_TARGET)),
    ]
    assert packets[3][3].target_id == victim.id
    # The create carries the contact point; the client derives its follow
    # offsets from it, so it must not already be the carrier's own origin.
    stuck = packets[2][3]
    assert stuck.pos_x < victim.x


def test_a_peer_that_never_saw_the_victim_gets_no_target():
    thrower = _player(1)
    victim = _player(2, team=1, x=105.0)
    server = _server(OpenWorld(), thrower, victim)
    server.connections[1] = RecordingConnection(thrower, known_players=[1])
    projectile = _throw(server, thrower)

    _fly_until_stuck(server, projectile)

    kinds = [item[0] for item in _entity_packets(server.connections[1])]
    assert kinds == ["create", "destroy", "create"]


def test_sticky_stays_where_its_carrier_died():
    thrower = _player(1)
    victim = _player(2, team=1, x=105.0)
    server = _server(OpenWorld(), thrower, victim)
    projectile = _throw(server, thrower)
    _fly_until_stuck(server, projectile)
    victim.x = 120.0
    server._update_grenades(DT)
    resting = (projectile.x, projectile.y, projectile.z)
    assert resting[0] == 120.0
    before = len(_entity_packets(server.connections[1]))

    victim.alive = False
    server._update_grenades(DT)

    packets = _entity_packets(server.connections[1])[before:]
    assert [item[:3] for item in packets] == [
        ("change", projectile.entity_id, int(C.SET_TARGET)),
        ("change", projectile.entity_id, int(C.SET_POSITION)),
    ]
    assert packets[0][3].target_id == -1
    moved = packets[1][3]
    assert _close((moved.pos_x, moved.pos_y, moved.pos_z), resting)

    # The respawn must not pull the grenade to the new life.
    victim.alive = True
    victim.x = 300.0
    server._update_grenades(DT)
    assert (projectile.x, projectile.y, projectile.z) == resting
    assert len(_entity_packets(server.connections[1])) == before + 2


def test_a_leaving_carrier_releases_its_sticky_before_the_id_is_freed():
    thrower = _player(1)
    victim = _player(2, team=1, x=105.0)
    server = _server(OpenWorld(), thrower, victim)
    projectile = _throw(server, thrower)
    _fly_until_stuck(server, projectile)
    before = len(_entity_packets(server.connections[1]))

    attachments.forget_player(server, victim)

    assert projectile.attached_player_id is None
    packets = _entity_packets(server.connections[1])[before:]
    assert [item[2] for item in packets] == [
        int(C.SET_TARGET), int(C.SET_POSITION),
    ]
    assert packets[0][3].target_id == -1
    # Nothing is queued twice on the next tick.
    server.players.pop(victim.id)
    server._update_grenades(DT)
    assert len(_entity_packets(server.connections[1])) == before + 2


def test_instant_respawn_detaches_before_the_next_projectile_tick():
    from server.player import Player

    thrower = _player(1)
    server = _server(OpenWorld(), thrower)
    connection = RecordingConnection()
    connection.server = server
    victim = Player(2, "victim", 3, int(C.RIFLE_TOOL), connection)
    connection.player = victim
    server.players[victim.id] = victim
    server.connections[victim.id] = connection
    victim.spawn(105.0, 100.0, 29.0)
    projectile = _throw(server, thrower)
    _fly_until_stuck(server, projectile)
    resting = (projectile.x, projectile.y, projectile.z)

    # No engine update sees the brief dead state (VIP promotion and zero-
    # delay mode respawns can retire and create a body in one server tick).
    victim.alive = False
    victim.spawn(300.0, 100.0, 29.0)
    assert projectile.attached_player_id is None
    server._update_grenades(DT)

    assert (projectile.x, projectile.y, projectile.z) == resting
    packets = _entity_packets(server.connections[1])
    assert packets[-2][3].target_id == -1
    assert packets[-1][2] == int(C.SET_POSITION)


def test_forgetting_attachments_on_a_lightweight_server_needs_no_registry():
    server = SimpleNamespace()

    attachments.forget_player(server, _player(1))

    assert not hasattr(server, "_riot_shield_entities")


def test_carrier_leaving_in_the_tick_of_the_stick_is_never_named():
    thrower = _player(1)
    victim = _player(2, team=1, x=105.0)
    server = _server(OpenWorld(), thrower, victim)
    projectile = _throw(server, thrower)
    # The engine sticks it; the leave is handled before the packets go out.
    for _ in range(120):
        server.projectile_engine.update(
            DT, server.world_manager, players=(thrower, victim)
        )
        if projectile.stuck:
            break
    assert projectile.attached_player_id == victim.id

    attachments.forget_player(server, victim)
    server.players.pop(victim.id)
    server._update_grenades(DT)

    packets = _entity_packets(server.connections[1])
    assert [item[0] for item in packets] == ["create", "destroy", "create"]
    assert packets[-1][2] == STUCK


def test_a_leaving_thrower_takes_the_stuck_sticky_with_it():
    thrower = _player(1)
    observer = _player(2, team=1, y=140.0)
    server = _server(WallWorld(105), thrower, observer)
    projectile = _throw(server, thrower)
    _fly_until_stuck(server, projectile)
    stuck_id = projectile.entity_id

    server.round_lifecycle.forget_player(thrower)

    assert server.projectile_engine.projectiles == []
    assert _entity_packets(server.connections[2])[-1] == ("destroy", stuck_id)


def test_stale_sticky_event_after_a_round_reset_is_ignored():
    thrower = _player(1)
    server = _server(WallWorld(105), thrower)
    projectile = _throw(server, thrower)
    server.projectile_engine.update(
        DT * 10, server.world_manager, players=(thrower,)
    )
    assert projectile.stuck
    server.entity_registry.clear()
    server.projectile_engine.projectiles.clear()
    sent = len(server.connections[1].sent)

    server._update_grenades(DT)

    assert len(server.connections[1].sent) == sent
    assert server.entity_registry.all() == []


def test_stale_sticky_event_cannot_replace_a_new_round_entity_with_reused_id():
    thrower = _player(1)
    server = _server(WallWorld(105), thrower)
    projectile = _throw(server, thrower)
    server.projectile_engine.update(DT * 10, server.world_manager, players=(thrower,))
    assert projectile.stuck
    old_id = projectile.entity_id
    server.entity_registry.clear()
    server.projectile_engine.projectiles.clear()
    replacement = server.entity_registry.place(FLYING, 50.0, 60.0, 70.0)
    assert replacement.entity_id == old_id
    sent = len(server.connections[1].sent)

    server._update_grenades(DT)

    assert server.entity_registry.get(old_id) is replacement
    assert len(server.connections[1].sent) == sent
    assert server.projectile_engine.attachment_events == []


# -- riot shield --------------------------------------------------------


def test_first_shield_hit_creates_the_anchor_then_only_hits_follow():
    bearer, shooter = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), bearer, shooter)

    assert attachments.riot_shield_hit(
        server, bearer, (100.5, 100.0, 29.5), melee=False
    )
    attachments.riot_shield_hit(server, bearer, (100.5, 100.0, 29.5), melee=True)

    packets = _entity_packets(server.connections[2])
    shield_id = packets[0][1]
    assert [item[:3] for item in packets] == [
        ("create", shield_id, SHIELD),
        ("change", shield_id, int(C.SET_TARGET)),
        ("hit", shield_id, int(C.WEAPON_KILL)),
        ("hit", shield_id, int(C.MELEE_KILL)),
    ]
    assert packets[0][3].player_id == bearer.id
    assert packets[1][3].target_id == bearer.id
    impact = packets[2][3]
    assert _close((impact.x, impact.y, impact.z), (100.5, 100.0, 29.5))
    assert server.entity_registry.static_entities() == []


def test_late_peer_gets_the_anchor_before_its_first_shield_hit():
    bearer, shooter = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), bearer, shooter)
    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)
    late = RecordingConnection(_player(3))
    server.connections[3] = late

    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)

    assert [item[0] for item in _entity_packets(late)] == [
        "create", "change", "hit",
    ]
    assert [item[0] for item in _entity_packets(server.connections[2])][-2:] == [
        "hit", "hit",
    ]


def test_shield_anchor_is_destroyed_when_the_bearer_dies():
    bearer, shooter = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), bearer, shooter)
    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)
    shield_id = server.entity_registry.all()[0].entity_id

    server._update_grenades(DT)
    assert server.entity_registry.get(shield_id) is not None

    bearer.alive = False
    server._update_grenades(DT)

    assert server.entity_registry.get(shield_id) is None
    assert _entity_packets(server.connections[2])[-1] == ("destroy", shield_id)
    # The next life gets a fresh anchor.
    bearer.alive = True
    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)
    assert [entity.type for entity in server.entity_registry.all()] == [SHIELD]


def test_shield_anchor_is_released_when_the_bearer_leaves():
    bearer, shooter = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), bearer, shooter)
    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)
    shield_id = server.entity_registry.all()[0].entity_id

    attachments.forget_player(server, bearer)

    assert server.entity_registry.all() == []
    assert _entity_packets(server.connections[2])[-1] == ("destroy", shield_id)


def test_shield_anchor_survives_a_round_reset_of_the_registry():
    bearer, shooter = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), bearer, shooter)
    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=False)
    server.entity_registry.clear()
    for connection in server.connections.values():
        connection.known_entity_ids.clear()
        connection.sent.clear()

    attachments.riot_shield_hit(server, bearer, (100.0, 100.0, 29.0), melee=True)

    assert [item[0] for item in _entity_packets(server.connections[2])] == [
        "create", "change", "hit",
    ]


def test_old_shield_ledger_cannot_delete_a_new_bearers_reused_entity_id():
    old_bearer, new_bearer = _player(1), _player(2, team=1)
    server = _server(OpenWorld(), old_bearer, new_bearer)
    attachments.riot_shield_hit(server, old_bearer, (100.0, 100.0, 29.0), melee=False)
    old_id = server.entity_registry.all()[0].entity_id
    server.entity_registry.clear()
    for connection in server.connections.values():
        connection.known_entity_ids.clear()
    attachments.riot_shield_hit(server, new_bearer, (100.0, 100.0, 29.0), melee=False)
    replacement = server.entity_registry.get(old_id)
    assert replacement.player_id == new_bearer.id

    attachments.forget_player(server, old_bearer)

    assert server.entity_registry.get(old_id) is replacement
    assert attachments._live_shield(server, new_bearer) is replacement
