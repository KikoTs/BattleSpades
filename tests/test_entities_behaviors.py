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
    inert = reg.place(C.HEALTH_CRATE, 4, 5, 6, behavior=GraveBehavior())  # no on_tick override
    reg.tick(ctx([]))
    assert ticks == [ticker.entity_id]
    assert inert.alive is True


# --- grave lifecycle -------------------------------------------------------

def test_grave_is_inert_and_explicitly_removable():
    reg = EntityRegistry()
    g = reg.place(C.GRAVE_ENTITY, 10, 20, 30, kind="grave", behavior=GraveBehavior())
    reg.tick(ctx([FakePlayer(10, 20, 30)]))   # a player standing on it does nothing
    assert g.alive is True
    assert reg.remove(g.entity_id) is g
    assert reg.get(g.entity_id) is None


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
