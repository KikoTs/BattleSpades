from types import SimpleNamespace

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import ChangeEntity

from server.rocket_turret import (
    ROCKET_TURRET_AMMO,
    ROCKET_TURRET_INITIAL_STOCK,
    ROCKET_TURRET_ROCKET_SPEC,
    RocketTurretController,
)
from protocol.packet_handler import _deploy_pos


class Player:
    def __init__(self, pid, team, xyz):
        self.id = pid
        self.team = team
        self.x, self.y, self.z = xyz
        self.alive = True
        self.spawned = True
        self.class_id = int(C.CLASS_ENGINEER)
        self.tool = int(C.ROCKET_TURRET_TOOL)
        self.loadout = [int(C.SMG_TOOL), int(C.ROCKET_TURRET_TOOL)]
        self.rocket_turret_stock = ROCKET_TURRET_INITIAL_STOCK


class Registry:
    def __init__(self):
        self.next_id = 20

    def place(self, type, x, y, z, **kw):
        result = SimpleNamespace(entity_id=self.next_id, type=type, x=x, y=y, z=z, **kw)
        self.next_id += 1
        return result


class Projectiles:
    def __init__(self):
        self.spawned = []

    def spawn_spec(self, spec, pos, vel, thrower_id, now=None):
        projectile = SimpleNamespace(spec=spec, entity_id=0)
        self.spawned.append((spec, pos, vel, thrower_id, now, projectile))
        return projectile


class Server:
    def __init__(self):
        self.entity_registry = Registry()
        self.projectile_engine = Projectiles()
        self.players = {}
        self.rocket_turrets = {}
        self.created = []
        self.changed = []

    def broadcast_create_entity(self, ent):
        self.created.append(ent)

    def broadcast_turret_properties(self, turret):
        self.changed.append((turret.entity_id, turret.ammo, turret.target_id))

    def spawn_projectile_entity(self, projectile, owner, pos, vel):
        projectile.entity_id = 99


def test_engineer_places_visible_turret_and_consumes_stock():
    server = Server()
    owner = Player(1, 2, (10.0, 10.0, 10.0))
    server.players[owner.id] = owner
    controller = RocketTurretController(server)

    turret = controller.place(owner, (12.0, 10.0, 10.0), yaw=45.0, now=0.0)

    assert turret is not None
    assert owner.rocket_turret_stock == ROCKET_TURRET_INITIAL_STOCK - 1
    assert turret.ammo == ROCKET_TURRET_AMMO
    assert turret.entity_id in server.rocket_turrets
    assert server.created[0].type == int(C.ROCKET_TURRET_ENTITY)


def test_turret_acquires_enemy_and_fires_authoritative_rocket():
    server = Server()
    owner = Player(1, 2, (10.0, 10.0, 10.0))
    enemy = Player(2, 3, (20.0, 10.0, 10.0))
    server.players = {1: owner, 2: enemy}
    controller = RocketTurretController(server)
    turret = controller.place(owner, (10.0, 10.0, 10.0), yaw=0.0, now=0.0)

    controller.update(1.0, now=2.0)

    assert turret.target_id == enemy.id
    assert turret.ammo == ROCKET_TURRET_AMMO - 1
    assert len(server.projectile_engine.spawned) == 1
    spec, _pos, vel, thrower_id, _now, projectile = server.projectile_engine.spawned[0]
    assert spec is ROCKET_TURRET_ROCKET_SPEC
    assert spec.damage == 50 and spec.block_damage == 10
    assert thrower_id == owner.id
    assert vel[0] > 0.0
    assert projectile.entity_id == 99


def test_turret_ignores_teammates_and_out_of_detection_range():
    server = Server()
    owner = Player(1, 2, (10.0, 10.0, 10.0))
    teammate = Player(2, 2, (12.0, 10.0, 10.0))
    far_enemy = Player(3, 3, (100.0, 10.0, 10.0))
    server.players = {1: owner, 2: teammate, 3: far_enemy}
    controller = RocketTurretController(server)
    turret = controller.place(owner, (10.0, 10.0, 10.0), yaw=0.0, now=0.0)

    controller.update(1.0, now=2.0)

    assert turret.target_id is None
    assert turret.ammo == ROCKET_TURRET_AMMO
    assert server.projectile_engine.spawned == []


def test_change_entity_target_and_ammo_use_stock_action_layout():
    target = ChangeEntity()
    target.entity_id = 42
    target.action = int(C.SET_TARGET)
    target.target_id = -1
    target_raw = bytes(target.generate())
    assert target_raw == bytes((16, 42, 0, C.SET_TARGET, 0xFF))
    parsed_target = ChangeEntity(ByteReader(target_raw[1:]))
    assert parsed_target.entity_id == 42 and parsed_target.target_id == -1

    ammo = ChangeEntity()
    ammo.entity_id = 42
    ammo.action = int(C.SET_AMMO)
    ammo.ammo = 9.0
    ammo_raw = bytes(ammo.generate())
    assert ammo_raw[:4] == bytes((16, 42, 0, C.SET_AMMO))
    parsed_ammo = ChangeEntity(ByteReader(ammo_raw[1:]))
    assert parsed_ammo.entity_id == 42 and parsed_ammo.ammo == 9.0


def test_turret_placement_uses_stock_ten_block_limit():
    player = SimpleNamespace(x=0.0, y=0.0, z=0.0)

    assert _deploy_pos(
        player, SimpleNamespace(x=10.0, y=0.0, z=0.0), max_distance=10.0
    ) == (10.0, 0.0, 0.0)
    assert _deploy_pos(
        player, SimpleNamespace(x=10.01, y=0.0, z=0.0), max_distance=10.0
    ) is None
