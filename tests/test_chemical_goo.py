"""Specialist Chemical Bomb goo: retail entity, dissolve and contact burn."""
from types import SimpleNamespace

import shared.constants as C

from server.chemical_goo import (
    CHEMICAL_BOMB_BLOCK_DAMAGE_TYPE,
    GOO_HP_DAMAGE_TYPE,
    ChemicalGooController,
)
from server.entities.registry import EntityRegistry
from server.player import Player


class _World:
    def __init__(self, solids=()):
        self.solids = set(solids)

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids


class _Player:
    def __init__(self, player_id=1, team=0, position=(50.0, 50.0, 0.0)):
        self.id = player_id
        self.team = team
        self.x, self.y, self.z = position
        self.alive = True
        self.spawned = True
        self.touching_goo = False
        self.health = 100
        self.damage_events = []

    def _current_contact_offset(self):
        return 2.25

    def damage(self, amount, source=None, kill_type=0, hp_damage_type=None):
        self.health -= amount
        self.damage_events.append((amount, source, kill_type, hp_damage_type))
        if self.health <= 0:
            self.alive = False
        return True


class _Server:
    def __init__(self, world, players):
        self.config = SimpleNamespace(build_damage=False, friendly_fire=False)
        self.world_manager = world
        self.players = {player.id: player for player in players}
        self.entity_registry = EntityRegistry()
        self.created = []
        self.destroyed = []

    def broadcast_create_entity(self, ent):
        self.created.append(ent)

    def broadcast_destroy_entity(self, entity_id):
        self.destroyed.append(entity_id)


def _floor(z=10, size=12):
    return {(x, y, z) for x in range(size) for y in range(size)}


def _standing_on(block, player_id=2, team=1):
    x, y, z = block
    # Eye is 2.25 above the feet; feet rest on the voxel's top face (z).
    return _Player(player_id, team, (x + 0.5, y + 0.5, z - 2.25))


def test_block_goo_is_retail_entity_31_with_block_fire_lifespan():
    assert C.BLOCK_GOO_ENTITY == 31
    owner = _Player()
    server = _Server(_World(_floor()), [owner])
    goo = ChemicalGooController(server)

    ids = goo.splash(6.0, 6.0, 9.5, owner, now=0.0)

    assert ids
    for entity_id in ids:
        entity = server.entity_registry.get(entity_id)
        assert entity.type == C.BLOCK_GOO_ENTITY
        assert entity.fuse == C.BLOCKFIRE_MAX_LIFESPAN
        # Modelless surface patch: FACE_TOP is the only non-rotating face.
        assert entity.face == C.FACE_TOP
    assert [e.entity_id for e in server.created] == ids


def test_splash_covers_the_radius_three_retail_footprint_only():
    owner = _Player()
    server = _Server(_World(_floor(size=20)), [owner])
    goo = ChemicalGooController(server)

    goo.splash(10.0, 10.0, 9.5, owner, now=0.0)

    blocks = {state.block for state in goo.goo.values()}
    assert (10, 10, 10) in blocks
    # CHEMICALBOMB_EXPLOSION_RADIUS (A1666) = 3: the native radius list
    # tests voxel centres against radius + 0.5.
    assert all(abs(x - 10) <= 3 and abs(y - 10) <= 3 for x, y, _z in blocks)
    assert (14, 10, 10) not in blocks
    assert len(blocks) <= ChemicalGooController.MAX_GOO_PER_BOMB


def test_goo_dissolves_its_block_with_chemical_damage_type(monkeypatch):
    owner = _Player()
    server = _Server(_World({(10, 10, 10)}), [owner])
    goo = ChemicalGooController(server)
    entity_id = goo.coat_block((10, 10, 10), owner, now=0.0)
    calls = []
    monkeypatch.setattr(
        "server.combat_runtime.get_combat_system",
        lambda _server: SimpleNamespace(
            _apply_block_damage=lambda *a, **k: calls.append((a, k))
        ),
    )

    goo.update(now=C.BLOCKFIRE_BLOCK_DAMAGE_TIMER - 0.01)
    assert calls == []
    goo.update(now=C.BLOCKFIRE_BLOCK_DAMAGE_TIMER)

    assert len(calls) == 1
    assert calls[0][0][1:] == ((10, 10, 10), C.BLOCKFIRE_BLOCK_DAMAGE)
    # Damage type 43 = BlockManager.handle_chemical_bomb_damage (A417).
    assert calls[0][1] == {
        "damage_type": CHEMICAL_BOMB_BLOCK_DAMAGE_TYPE,
        "causer_id": entity_id,
    }
    assert CHEMICAL_BOMB_BLOCK_DAMAGE_TYPE == 43


def test_touching_goo_burns_with_chemical_kill_and_sets_state_bit():
    owner = _Player(1, 0)
    victim = _standing_on((10, 10, 10))
    server = _Server(_World({(10, 10, 10)}), [owner, victim])
    goo = ChemicalGooController(server)
    goo.coat_block((10, 10, 10), owner, now=0.0)

    goo.update(now=0.0)
    goo.update(now=0.3)

    assert victim.touching_goo is True
    assert [event[0] for event in victim.damage_events] == [2, 3]
    assert all(event[1] is owner for event in victim.damage_events)
    assert all(event[2] == C.KILL.CHEMICALBOMB_KILL for event in victim.damage_events)
    assert all(event[3] == GOO_HP_DAMAGE_TYPE for event in victim.damage_events)


def test_goo_hurts_only_while_touched_no_afterburn():
    owner = _Player(1, 0)
    victim = _standing_on((10, 10, 10))
    server = _Server(_World({(10, 10, 10)}), [owner, victim])
    goo = ChemicalGooController(server)
    goo.coat_block((10, 10, 10), owner, now=0.0)
    goo.update(now=0.0)

    victim.x += 5.0
    goo.update(now=0.3)
    goo.update(now=0.6)

    assert victim.touching_goo is False
    assert len(victim.damage_events) == 1


def test_friendly_fire_off_spares_teammates_but_not_the_thrower():
    owner = _standing_on((10, 10, 10), player_id=1, team=0)
    mate = _standing_on((11, 10, 10), player_id=2, team=0)
    server = _Server(_World({(10, 10, 10), (11, 10, 10)}), [owner, mate])
    goo = ChemicalGooController(server)
    goo.coat_block((10, 10, 10), owner, now=0.0)
    goo.coat_block((11, 10, 10), owner, now=0.0)

    goo.update(now=0.0)

    assert mate.damage_events == [] and mate.touching_goo is False
    assert owner.damage_events and owner.touching_goo is True


def test_goo_expires_and_clears_contact():
    owner = _Player(1, 0)
    victim = _standing_on((10, 10, 10))
    server = _Server(_World({(10, 10, 10)}), [owner, victim])
    goo = ChemicalGooController(server)
    entity_id = goo.coat_block((10, 10, 10), owner, now=0.0)
    goo.update(now=0.0)

    goo.update(now=C.BLOCKFIRE_MAX_LIFESPAN)

    assert server.destroyed == [entity_id]
    assert victim.touching_goo is False
    assert goo.goo == {}


def test_goo_eats_down_into_the_voxel_below():
    owner = _Player()
    world = _World({(10, 10, 10), (10, 10, 11)})
    server = _Server(world, [owner])
    goo = ChemicalGooController(server)
    first = goo.coat_block((10, 10, 10), owner, now=0.0)

    world.solids.discard((10, 10, 10))
    goo.update(now=1.0)

    assert server.destroyed == [first]
    (moved,) = goo.goo.values()
    assert moved.block == (10, 10, 11)
    assert moved.expires_at == C.BLOCKFIRE_MAX_LIFESPAN


def test_disconnect_forgets_owned_goo():
    owner = _Player(1, 0)
    victim = _standing_on((10, 10, 10))
    server = _Server(_World({(10, 10, 10)}), [owner, victim])
    goo = ChemicalGooController(server)
    entity_id = goo.coat_block((10, 10, 10), owner, now=0.0)
    goo.update(now=0.0)

    goo.forget_player(owner.id)

    assert server.destroyed == [entity_id]
    assert victim.touching_goo is False


def test_world_update_goo_bit_follows_contact_state():
    player = Player(7, "goo-test", 0, C.SMG_TOOL)
    assert player.pack_state_flags() & 0x08 == 0
    player.touching_goo = True
    assert player.pack_state_flags() & 0x08 == 0x08


def test_chemical_bomb_impact_is_goo_not_a_blast():
    """The stock ExplosionDamageManager has no chemical-bomb handler."""

    from server.main import BattleSpadesServer
    from server.projectiles import Explosion, PROJECTILE_SPECS

    spec = PROJECTILE_SPECS[int(C.CHEMICALBOMB_TOOL)]
    splashes, blasts = [], []
    fake = SimpleNamespace(
        players={},
        entity_registry=EntityRegistry(),
        goo_controller=SimpleNamespace(
            splash=lambda *a, **k: splashes.append(a)
        ),
        _apply_blast=lambda *a, **k: blasts.append(a),
        broadcast_destroy_entity=lambda *_: None,
    )
    projectile = SimpleNamespace(
        x=10.0, y=11.0, z=9.5, thrower_id=1, spec=spec, entity_id=None,
        contact_block=(10, 11, 10), block_color=None, source_loop=None,
    )

    BattleSpadesServer._explode_projectile(fake, Explosion(projectile))

    assert splashes == [(10.0, 11.0, 9.5, None)]
    assert blasts == []


def test_chemical_bomb_launch_cap_uses_stock_throw_speed():
    from server.oriented_actions import max_launch_speed

    # A1663 = 50 full-charge throw speed.
    assert max_launch_speed(int(C.CHEMICALBOMB_TOOL)) >= 50.0 * 1.5
