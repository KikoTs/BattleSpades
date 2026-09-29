"""Class deployables against the retail tables (docs/CRATES_CLASSES_RETAIL.md)."""

from __future__ import annotations

from types import SimpleNamespace

import shared.constants as C

from server.entities.behaviors import (
    DamageableEntityBehavior,
    MedpackBehavior,
    ProximityMineBehavior,
    RadarStationBehavior,
    RemoteChargeBehavior,
    TimedExplosiveBehavior,
)
from server.entities.registry import EntityContext, EntityRegistry


class _Server:
    def __init__(self):
        self.players = {}
        self.blasts = []
        self.entity_registry = None

    def _apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower,
                     **kwargs):
        self.blasts.append(((gx, gy, gz), damage, kwargs))


def _ctx(server, now=1000.0, players=()):
    destroyed = []
    ctx = EntityContext(
        dt=1 / 60, now=now, players=list(players), server=server,
        destroy=destroyed.append,
    )
    ctx._destroyed = destroyed
    return ctx


def _dynamite(registry, face=4):
    behavior = TimedExplosiveBehavior(
        thrower_id=3,
        fuse=float(C.DYNAMITE_EXPLOSION_FUSE),
        damage=float(C.DYNAMITE_EXPLOSION_DAMAGE),
        block_damage=float(C.DYNAMITE_EXPLOSION_BLOCK_DAMAGE),
        crater_radius=2,
        kill_type=15,
        blast_radius=float(C.DYNAMITE_EXPLOSION_RADIUS),
    )
    return registry.place(
        int(C.DYNAMITE_ENTITY), 40.0, 50.0, 60.0, face=face, behavior=behavior,
    )


def test_dynamite_has_the_retail_one_point_shell():
    registry = EntityRegistry()
    stick = _dynamite(registry)

    assert isinstance(stick.behavior, DamageableEntityBehavior)
    assert stick.behavior.takes_damage is True
    assert stick.behavior.health == float(C.DYNAMITE_HEALTH) == 1.0
    assert stick.behavior.hit_radius > 0.0
    # Hitscan aims at the rendered stick on its face, not inside the voxel.
    assert stick.behavior.get_hit_center(stick) == (40.5, 50.5, 60.0)


def test_shooting_dynamite_sets_it_off_through_the_normal_blast():
    registry = EntityRegistry()
    server = _Server()
    server.entity_registry = registry
    stick = _dynamite(registry)
    shooter = SimpleNamespace(id=9, team=1)
    ctx = _ctx(server)

    registry.damage_entity(stick.entity_id, 12.0, shooter, ctx)

    assert stick.alive is False
    assert registry.get(stick.entity_id) is None
    assert ctx._destroyed == [stick.entity_id]
    assert len(server.blasts) == 1
    center, damage, kwargs = server.blasts[0]
    assert center == (40.5, 50.5, 60.0)
    assert damage == float(C.DYNAMITE_EXPLOSION_DAMAGE)
    assert kwargs["blast_radius"] == float(C.DYNAMITE_EXPLOSION_RADIUS)
    assert stick.behavior.triggered_by is shooter


def test_dynamite_fuse_still_runs_untouched():
    registry = EntityRegistry()
    server = _Server()
    server.entity_registry = registry
    stick = _dynamite(registry)

    registry.tick(_ctx(server, now=1000.0))
    registry.tick(_ctx(server, now=1000.0 + float(C.DYNAMITE_EXPLOSION_FUSE) - 0.1))
    assert stick.alive and server.blasts == []
    registry.tick(_ctx(server, now=1000.0 + float(C.DYNAMITE_EXPLOSION_FUSE) + 0.01))
    assert not stick.alive and len(server.blasts) == 1


def test_retail_deployable_health_values():
    assert ProximityMineBehavior(1, 0, 100, 15, 1, 14).health == float(C.LANDMINE_HEALTH)
    assert RemoteChargeBehavior(1).health == float(C.C4_HEALTH)
    medpack = MedpackBehavior(
        0, heal_amount=int(C.MEDPACK_HEAL_AMOUNT), uses=int(C.MEDPACK_USES),
        health=float(C.MEDPACK_HEALTH),
    )
    assert (medpack.heal_amount, medpack.uses, medpack.health) == (25, 3, 1.0)
    assert RadarStationBehavior(0, lifetime=45.0, health=45.0).health == 45.0


def test_medpack_heals_teammates_only_and_spends_uses():
    registry = EntityRegistry()
    behavior = MedpackBehavior(0, heal_amount=25, uses=3)
    pack = registry.place(int(C.MEDPACK_ENTITY), 1.0, 1.0, 1.0, behavior=behavior)
    healed = []

    def player(team, health):
        return SimpleNamespace(
            team=team, health=health, max_health=100,
            heal=lambda amount: healed.append((team, amount)),
        )

    ctx = _ctx(None)
    assert behavior.on_touch(pack, player(1, 10), ctx) is False
    assert behavior.on_touch(pack, player(0, 100), ctx) is False
    for _ in range(3):
        assert behavior.on_touch(pack, player(0, 10), ctx) is True
    assert healed == [(0, 25)] * 3
    assert pack.alive is False
