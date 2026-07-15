"""Tickable entity system (Phase-2) — behavior + registry.tick tests.

Covers the crate pickup/respawn migration, touch/damage routing, and the
wire-safety regression guard (a behavior must not change the serialized bytes).
"""
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants as C  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import CreateEntity  # noqa: E402
from server.entities.registry import EntityRegistry, EntityContext  # noqa: E402
from server.entities.behaviors import (  # noqa: E402
    EntityBehavior, PickupCrateBehavior, GraveBehavior,
)


class FakePlayer:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z
        self.alive = True
        self.spawned = True
        self.restocked = 0
        self.healed = 0

    def restock_ammo(self):
        self.restocked += 1

    def heal(self, amount):
        self.healed += 1


def ctx(players, now=1000.0, dt=1 / 60):
    created, destroyed = [], []
    c = EntityContext(dt=dt, now=now, players=players,
                      create=created.append, destroy=destroyed.append)
    c._created = created
    c._destroyed = destroyed
    return c


def ammo_crate(reg, x, y, z, delay=15.0):
    b = PickupCrateBehavior(lambda p: p.restock_ammo(), respawn_delay=delay)
    return reg.place(C.AMMO_CRATE, x, y, z, behavior=b)


# --- touch -----------------------------------------------------------------

def test_touch_refills_despawns_and_schedules_respawn():
    reg = EntityRegistry()
    e = ammo_crate(reg, 100.0, 100.0, 60.0)
    p = FakePlayer(100.5, 100.0, 60.0)
    c = ctx([p], now=1000.0)
    reg.tick(c)
    assert p.restocked == 1
    assert e.alive is False
    assert e.respawn_at == 1015.0
    assert c._destroyed == [e.entity_id]


def test_touch_not_fired_outside_radius():
    reg = EntityRegistry()
    e = ammo_crate(reg, 100.0, 100.0, 60.0)
    p = FakePlayer(110.0, 100.0, 60.0)   # 10 blocks away > 3
    c = ctx([p])
    reg.tick(c)
    assert p.restocked == 0
    assert e.alive is True
    assert c._destroyed == []


def test_touch_not_fired_for_dead_entity():
    reg = EntityRegistry()
    e = ammo_crate(reg, 100.0, 100.0, 60.0)
    e.alive = False
    p = FakePlayer(100.0, 100.0, 60.0)
    c = ctx([p])
    reg.tick(c)
    assert p.restocked == 0


def test_crate_consumed_once_even_with_two_players_same_tick():
    reg = EntityRegistry()
    e = ammo_crate(reg, 100.0, 100.0, 60.0)
    p1 = FakePlayer(100.1, 100.0, 60.0)
    p2 = FakePlayer(100.2, 100.0, 60.0)
    c = ctx([p1, p2])
    reg.tick(c)
    # exactly one player consumes it; the other sees it already dead
    assert p1.restocked + p2.restocked == 1
    assert c._destroyed == [e.entity_id]


# --- respawn ---------------------------------------------------------------

def test_respawn_recreates_after_timer():
    reg = EntityRegistry()
    e = ammo_crate(reg, 100.0, 100.0, 60.0, delay=15.0)
    reg.tick(ctx([FakePlayer(100.0, 100.0, 60.0)], now=1000.0))  # consume
    assert e.alive is False

    # not yet due
    c_early = ctx([], now=1010.0)
    reg.tick(c_early)
    assert e.alive is False
    assert c_early._created == []

    # due
    c_due = ctx([], now=1016.0)
    reg.tick(c_due)
    assert e.alive is True
    assert e.respawn_at == 0.0
    assert c_due._created == [e]


# --- damage routing --------------------------------------------------------

def test_damage_entity_noop_when_not_damageable():
    reg = EntityRegistry()
    e = ammo_crate(reg, 1, 2, 3)                 # PickupCrateBehavior: takes_damage=False
    reg.damage_entity(e.entity_id, 50, None, ctx([]))  # must not raise / do anything
    assert e.alive is True


def test_damage_entity_routes_when_damageable():
    hits = []

    class Destructible(EntityBehavior):
        takes_damage = True
        def on_damage(self, ent, amount, source, c):
            hits.append(amount)

    reg = EntityRegistry()
    e = reg.place(C.AMMO_CRATE, 1, 2, 3, behavior=Destructible())
    reg.damage_entity(e.entity_id, 42, None, ctx([]))
    assert hits == [42]


# --- on_tick ---------------------------------------------------------------

def test_on_tick_called_only_for_overriding_behavior():
    ticks = []

    class Ticker(EntityBehavior):
        def on_tick(self, ent, dt, c):
            ticks.append(ent.entity_id)

    reg = EntityRegistry()
    ticker = reg.place(C.AMMO_CRATE, 1, 2, 3, behavior=Ticker())
    inert = reg.place(C.HEALTH_CRATE, 4, 5, 6, behavior=EntityBehavior())
    reg.tick(ctx([]))
    assert ticks == [ticker.entity_id]
    assert inert.alive is True


def test_on_tick_batch_cap_round_robins_overloaded_behaviors():
    ticks = []

    class Ticker(EntityBehavior):
        def on_tick(self, ent, dt, c):
            ticks.append(ent.entity_id)

    reg = EntityRegistry()
    entities = [
        reg.place(C.AMMO_CRATE, index, 0, 0, behavior=Ticker())
        for index in range(3)
    ]

    assert reg.tick(ctx([]), max_on_tick=2) == 1
    assert ticks == [entities[0].entity_id, entities[1].entity_id]

    assert reg.tick(ctx([]), max_on_tick=2) == 1
    assert ticks[-2:] == [entities[2].entity_id, entities[0].entity_id]


def test_touch_spatial_index_excludes_distant_entity_buckets():
    """Large deployable counts must not become players × entities work."""
    reg = EntityRegistry()
    nearby = reg.place(C.HEALTH_CRATE, 4, 4, 4, behavior=EntityBehavior())
    distant = reg.place(C.HEALTH_CRATE, 400, 400, 4, behavior=EntityBehavior())
    nearby.behavior.touch_radius = 3.0
    distant.behavior.touch_radius = 3.0

    buckets = reg._bucket_touchers([nearby, distant])
    candidates = list(reg._nearby_touchers(buckets, 4, 4, 1))

    assert candidates == [nearby]


# --- grave lifecycle -------------------------------------------------------

def test_grave_detonates_after_stock_fuse():
    reg = EntityRegistry()
    srv = FakeServer()
    srv.entity_registry = reg
    g = reg.place(
        C.GRAVE_ENTITY, 10, 20, 30, kind="grave",
        behavior=GraveBehavior(thrower_id=7),
    )
    reg.tick(deploy_ctx(srv, [], now=1000.0))
    assert g.alive is True
    reg.tick(deploy_ctx(srv, [], now=1006.9))
    assert g.alive is True
    c = deploy_ctx(srv, [], now=1007.0)
    reg.tick(c)
    assert g.alive is False
    assert reg.get(g.entity_id) is None
    assert srv.blasts == [(10.0, 20.0, 30.0, 25.0, 3.0, 13, 1, False, 3.0)]
    assert c._destroyed == [g.entity_id]


# --- crate separation + block crate ------------------------------------------

def test_pickup_radius_matches_client_crate_distance():
    # CRATE_DISTANCE = 2.5 in the client; a wider radius plus close crates let
    # one walk-through consume ammo AND health at once.
    assert PickupCrateBehavior(lambda p: None).touch_radius == 2.5


def test_adjacent_crates_no_longer_double_trigger():
    """Two crates 8 blocks apart: a player at one must NOT trigger the other."""
    reg = EntityRegistry()
    a = ammo_crate(reg, 100.0, 100.0, 60.0)
    b = ammo_crate(reg, 108.0, 100.0, 60.0)
    p = FakePlayer(100.0, 100.0, 60.0)
    reg.tick(ctx([p]))
    assert p.restocked == 1          # only the near crate
    assert a.alive is False
    assert b.alive is True


def test_block_crate_refills_blocks_only():
    class BlockPlayer(FakePlayer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.blocks = 10
            self.movement_profile = SimpleNamespace(max_blocks=1000)

        def add_blocks(self, n):
            self.blocks = min(self.movement_profile.max_blocks, self.blocks + n)

    reg = EntityRegistry()
    behavior = PickupCrateBehavior(
        lambda p: p.add_blocks(p.movement_profile.max_blocks), respawn_delay=15.0)
    reg.place(int(getattr(C, "BLOCK_CRATE", 5)), 100.0, 100.0, 60.0, behavior=behavior)
    p = BlockPlayer(100.0, 100.0, 60.0)
    reg.tick(ctx([p]))
    assert p.blocks == 1000          # blocks topped up
    assert p.restocked == 0          # ammo untouched
    assert p.healed == 0             # health untouched


# --- deployables: dynamite (timed) + landmine (proximity) --------------------

class FakeServer:
    def __init__(self):
        self.players = {}
        self.blasts = []

    def _apply_blast(self, gx, gy, gz, damage, block_damage, kill_type, thrower,
                     crater_radius=1, force_destroy=True, blast_radius=16.0,
                     **kwargs):
        self.blasts.append((gx, gy, gz, damage, block_damage, kill_type,
                            crater_radius, force_destroy, blast_radius))


def deploy_ctx(server, players, now):
    created, destroyed = [], []
    c = EntityContext(dt=1 / 60, now=now, players=players, server=server,
                      create=created.append, destroy=destroyed.append)
    c._destroyed = destroyed
    return c


def test_dynamite_detonates_after_fuse():
    from server.entities.behaviors import TimedExplosiveBehavior
    reg = EntityRegistry()
    srv = FakeServer()
    b = TimedExplosiveBehavior(thrower_id=7, fuse=7.0, damage=300.0,
                               block_damage=5.0, crater_radius=2, kill_type=15)
    e = reg.place(C.DYNAMITE_ENTITY, 100.0, 100.0, 60.0, behavior=b)

    reg.tick(deploy_ctx(srv, [], now=1000.0))     # first tick arms the fuse (t0)
    assert e.alive and srv.blasts == []
    reg.tick(deploy_ctx(srv, [], now=1005.0))     # 5s < 7s fuse
    assert e.alive and srv.blasts == []
    c = deploy_ctx(srv, [], now=1008.0)           # 8s >= 7s
    reg.tick(c)
    assert not e.alive
    assert len(srv.blasts) == 1
    assert srv.blasts[0][3] == 300.0              # damage
    assert srv.blasts[0][6] == 2                  # crater_radius
    assert c._destroyed == [e.entity_id]


def test_c4_waits_for_remote_detonation_and_uses_stock_blast():
    from server.entities.behaviors import RemoteChargeBehavior
    reg = EntityRegistry()
    srv = FakeServer()
    srv.entity_registry = reg
    behavior = RemoteChargeBehavior(thrower_id=7)
    ent = reg.place(C.C4_ENTITY, 10, 20, 30, behavior=behavior)
    c = deploy_ctx(srv, [], now=1000.0)

    reg.tick(c)
    assert ent.alive and srv.blasts == []
    behavior.detonate(ent, c)

    assert not ent.alive
    assert reg.get(ent.entity_id) is None
    assert srv.blasts == [(10.0, 20.0, 30.0, 300.0, 7.0, 36, 2, True, 8.0)]
    assert c._destroyed == [ent.entity_id]


def test_radar_station_expires_and_releases_team_visibility():
    from server.entities.behaviors import RadarStationBehavior

    class RadarServer(FakeServer):
        def __init__(self):
            super().__init__()
            self.removed = []

        def _radar_station_removed(self, team):
            self.removed.append(team)

    reg = EntityRegistry()
    srv = RadarServer()
    srv.entity_registry = reg
    ent = reg.place(
        C.RADAR_STATION_ENTITY, 10, 20, 30,
        behavior=RadarStationBehavior(team=2, lifetime=250.0),
    )
    reg.tick(deploy_ctx(srv, [], now=1000.0))
    reg.tick(deploy_ctx(srv, [], now=1249.9))
    assert ent.alive
    c = deploy_ctx(srv, [], now=1250.0)
    reg.tick(c)
    assert not ent.alive
    assert reg.get(ent.entity_id) is None
    assert srv.removed == [2]
    assert c._destroyed == [ent.entity_id]


def test_medpack_health_is_server_authoritative_and_one_hit_destroys_it():
    from server.entities.behaviors import MedpackBehavior

    reg = EntityRegistry()
    srv = FakeServer()
    srv.entity_registry = reg
    ent = reg.place(
        C.MEDPACK_ENTITY, 10, 20, 30, player_id=7,
        behavior=MedpackBehavior(team=2, heal_amount=25, uses=3, health=1.0),
    )
    c = deploy_ctx(srv, [], now=1000.0)

    reg.damage_entity(ent.entity_id, 1.0, None, c)

    assert not ent.alive
    assert reg.get(ent.entity_id) is None
    assert c._destroyed == [ent.entity_id]


def test_radar_station_uses_recovered_45_health_and_releases_visibility_once():
    from server.entities.behaviors import RadarStationBehavior

    class RadarServer(FakeServer):
        def __init__(self):
            super().__init__()
            self.removed = []

        def _radar_station_removed(self, team):
            self.removed.append(team)

    reg = EntityRegistry()
    srv = RadarServer()
    srv.entity_registry = reg
    ent = reg.place(
        C.RADAR_STATION_ENTITY, 10, 20, 30, player_id=7,
        behavior=RadarStationBehavior(team=2, lifetime=250.0, health=45.0),
    )
    c = deploy_ctx(srv, [], now=1000.0)

    reg.damage_entity(ent.entity_id, 44.0, None, c)
    assert ent.alive
    assert ent.behavior.health == 1.0
    reg.damage_entity(ent.entity_id, 1.0, None, c)

    assert not ent.alive
    assert reg.get(ent.entity_id) is None
    assert srv.removed == [2]
    assert c._destroyed == [ent.entity_id]


def test_shooting_c4_removes_charge_without_remote_detonation():
    from server.entities.behaviors import RemoteChargeBehavior

    reg = EntityRegistry()
    srv = FakeServer()
    owner = SimpleNamespace(_c4_entity_ids=[])
    srv.players[7] = owner
    srv.entity_registry = reg
    ent = reg.place(
        C.C4_ENTITY, 10, 20, 30, player_id=7, face=4,
        behavior=RemoteChargeBehavior(thrower_id=7, health=1.0),
    )
    owner._c4_entity_ids = [ent.entity_id]
    c = deploy_ctx(srv, [], now=1000.0)

    reg.damage_entity(ent.entity_id, 1.0, None, c)

    assert reg.get(ent.entity_id) is None
    assert owner._c4_entity_ids == []
    assert srv.blasts == []
    assert c._destroyed == [ent.entity_id]


def test_explosion_damage_routes_through_entity_health_and_los():
    from server.main import BattleSpadesServer
    from server.entities.behaviors import MedpackBehavior

    reg = EntityRegistry()
    destroyed = []
    server = SimpleNamespace(
        config=SimpleNamespace(build_damage=False),
        players={},
        entity_registry=reg,
        world_manager=None,
        _blocked_los=lambda *args: False,
    )
    server._build_entity_ctx = lambda: EntityContext(
        dt=1 / 60, now=1000.0, players=[], server=server,
        destroy=destroyed.append,
    )
    entity = reg.place(
        C.MEDPACK_ENTITY, 10.0, 10.0, 10.0,
        behavior=MedpackBehavior(team=2, health=1.0),
    )

    BattleSpadesServer._apply_blast(
        server, 10.0, 10.0, 10.5,
        damage=25.0, block_damage=0.0, kill_type=3, thrower=None,
        blast_radius=3.0,
    )

    assert reg.get(entity.entity_id) is None
    assert destroyed == [entity.entity_id]


def test_landmine_triggers_on_enemy_not_teammate():
    from server.entities.behaviors import ProximityMineBehavior
    reg = EntityRegistry()
    srv = FakeServer()
    b = ProximityMineBehavior(thrower_id=7, team=2, damage=100.0,
                              block_damage=3.0, crater_radius=1, kill_type=14,
                              trigger_radius=2.5, arm_delay=1.0)
    e = reg.place(C.LANDMINE_ENTITY, 100.0, 100.0, 60.0, behavior=b)

    teammate = FakePlayer(100.5, 100.0, 60.0)
    teammate.team = 2
    enemy = FakePlayer(101.0, 100.0, 60.0)
    enemy.team = 3

    reg.tick(deploy_ctx(srv, [teammate, enemy], now=1000.0))   # arms (t0)
    assert e.alive and srv.blasts == []
    reg.tick(deploy_ctx(srv, [teammate], now=1000.5))          # not yet armed
    assert e.alive and srv.blasts == []
    reg.tick(deploy_ctx(srv, [teammate], now=1002.0))          # armed, only teammate near
    assert e.alive and srv.blasts == []                        # teammate never trips it
    reg.tick(deploy_ctx(srv, [teammate, enemy], now=1002.5))   # enemy in range
    assert not e.alive
    assert len(srv.blasts) == 1
    assert srv.blasts[0][3] == 100.0


def test_landmine_detects_enemy_reburied_two_layers_deep():
    from server.entities.behaviors import ProximityMineBehavior
    reg = EntityRegistry()
    srv = FakeServer()
    b = ProximityMineBehavior(
        thrower_id=7, team=2, damage=100.0, block_damage=15.0,
        crater_radius=1, kill_type=14, trigger_radius=2.5,
        arm_delay=4.0, blast_radius=3.0, detection_layers=3,
    )
    # Mine at z=60. A standing player's position is 2.25 above the ground;
    # after two blocks are placed over it their feet are at z=58.
    e = reg.place(C.LANDMINE_ENTITY, 100.0, 100.0, 60.0, behavior=b)
    enemy = FakePlayer(100.5, 100.0, 55.75)
    enemy.team = 3
    enemy.input = SimpleNamespace(crouch=False)

    reg.tick(deploy_ctx(srv, [enemy], now=1000.0))
    reg.tick(deploy_ctx(srv, [enemy], now=1004.1))

    assert not e.alive
    assert len(srv.blasts) == 1


# --- wire-safety regression guard ------------------------------------------

def test_behavior_does_not_change_wire_bytes():
    reg = EntityRegistry()
    plain = reg.place(C.AMMO_CRATE, 100.5, 200.25, 60.0)
    withb = reg.place(C.AMMO_CRATE, 100.5, 200.25, 60.0,
                      behavior=PickupCrateBehavior(lambda p: None))
    # equalize the only field that legitimately differs (the unique id)
    withb.entity_id = plain.entity_id

    def wire(ent):
        pkt = CreateEntity()
        pkt.set_entity(ent.to_wire_entity())
        return bytes(pkt.generate())

    assert wire(plain) == wire(withb)
