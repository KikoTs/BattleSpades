"""Objective airstrike shells must not claim a real player slot."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C

from modes.airstrike import NO_OWNER_PLAYER_ID, trigger_airstrike
from server.entities.registry import EntityRegistry
from server.main import BattleSpadesServer
from shared.bytes import ByteReader
from shared.packet import CreateEntity


class _Engine:
    def __init__(self):
        self.spawned = []

    def spawn_spec(self, spec, pos, vel, thrower_id):
        projectile = SimpleNamespace(spec=spec, pos=pos, vel=vel, thrower_id=thrower_id)
        self.spawned.append(projectile)
        return projectile


class _Server:
    def __init__(self):
        self.projectile_engine = _Engine()
        self.entity_registry = EntityRegistry()
        self.created = []

    def broadcast_create_entity(self, entity):
        self.created.append(entity)

    def spawn_projectile_entity(self, projectile, owner, pos, vel):
        BattleSpadesServer.spawn_projectile_entity(self, projectile, owner, pos, vel)


def test_airstrike_shells_carry_no_owner_player_id() -> None:
    server = _Server()
    assert trigger_airstrike(server, (100.0, 100.0, 60.0)) == 5
    assert len(server.created) == 5
    assert all(p.thrower_id == -1 for p in server.projectile_engine.spawned)
    for entity in server.created:
        assert entity.type == int(C.AIRSTRIKE_ENTITY)
        assert entity.player_id == NO_OWNER_PLAYER_ID != 0
        packet = CreateEntity()
        packet.entity = entity.to_wire_entity()
        data = bytes(packet.generate())
        decoded = CreateEntity(ByteReader(data[1:]))
        assert decoded.entity.player_id & 0xFF == 0xFF
