"""Entity registry + wire round-trip. A malformed Entity crashes the compiled
client natively, so pin the serialization here."""
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import CreateEntity, DestroyEntity  # noqa: E402
from server.entities.registry import EntityRegistry  # noqa: E402
from server.game_constants import TEAM_NEUTRAL  # noqa: E402


def test_late_battle_builder_entity_ids_match_retail_dispatch_table():
    """These values index the retail client's GameScene.ENTITIES table.

    They are wire ABI, not an implementation-defined class-registration order.
    A wrong value can render a different object or crash the native client.
    """
    assert C.MEDPACK_ENTITY == 30
    assert C.BLOCK_GOO_ENTITY == 31
    assert C.CHEMICAL_BOMB_ENTITY == 32
    assert C.GL_GRENADE_ENTITY == 33
    assert C.STICKY_GRENADE_ENTITY == 34
    assert C.ATTACHED_STICKY_GRENADE_ENTITY == 35
    assert C.RADAR_STATION_ENTITY == 36
    assert C.PROJECTILE_MINE_ENTITY == 37
    assert C.C4_ENTITY == 38
    assert C.RIOT_SHIELD_ENTITY == 39


def test_allocate_id_unique():
    reg = EntityRegistry()
    ids = {reg.place(C.AMMO_CRATE, 10, 20, 30).entity_id for _ in range(200)}
    assert len(ids) == 200
    assert all(0 <= i <= 0xFFFF for i in ids)


def test_create_entity_roundtrip():
    reg = EntityRegistry()
    e = reg.place(C.HEALTH_CRATE, 100.5, 200.25, 60.0,
                  state=TEAM_NEUTRAL, kind="health")
    pkt = CreateEntity()
    pkt.set_entity(e.to_wire_entity())
    data = bytes(pkt.generate())
    assert data[0] == 21  # CreateEntity packet id

    back = CreateEntity(ByteReader(data[1:]))
    ent = back.entity
    assert ent.type == C.HEALTH_CRATE
    assert ent.entity_id == e.entity_id
    assert ent.state == TEAM_NEUTRAL
    assert ent.player_id == 0
    assert ent.face == 4
    # fixed-point precision is 1/64 ~ 0.0156
    assert abs(ent.pos_x - 100.5) < 0.02
    assert abs(ent.pos_y - 200.25) < 0.02
    assert abs(ent.pos_z - 60.0) < 0.02


def test_create_entity_preserves_attachment_face():
    reg = EntityRegistry()
    e = reg.place(C.C4_ENTITY, 100.0, 200.0, 60.0, face=2)
    pkt = CreateEntity()
    pkt.set_entity(e.to_wire_entity())
    back = CreateEntity(ByteReader(bytes(pkt.generate())[1:]))
    assert back.entity.face == 2


def test_destroy_entity_roundtrip():
    reg = EntityRegistry()
    e = reg.place(C.AMMO_CRATE, 1, 2, 3)
    pkt = DestroyEntity()
    pkt.entity_id = e.entity_id
    data = bytes(pkt.generate())
    assert data[0] == 19
    back = DestroyEntity(ByteReader(data[1:]))
    assert back.entity_id == e.entity_id
    assert reg.remove(e.entity_id) is e
    assert reg.get(e.entity_id) is None


def test_static_entities_excludes_dead():
    reg = EntityRegistry()
    a = reg.place(C.AMMO_CRATE, 1, 2, 3)
    b = reg.place(C.HEALTH_CRATE, 4, 5, 6)
    b.alive = False
    statics = reg.static_entities()
    assert a in statics and b not in statics
