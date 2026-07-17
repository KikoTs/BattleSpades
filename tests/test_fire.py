"""Molotov block-fire replication and authoritative burn-state regressions."""
from types import SimpleNamespace

import shared.constants as C

from server.entities.registry import EntityRegistry
from server.fire import FireController
from server.player import Player


class _World:
    def __init__(self, solids=()):
        self.solids = set(solids)

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids


class _Player:
    def __init__(self, player_id=1, team=0, position=(10.0, 10.0, 9.0)):
        self.id = player_id
        self.team = team
        self.x, self.y, self.z = position
        self.alive = True
        self.spawned = True
        self.wade = False
        self.on_fire = False
        self.health = 100
        self.damage_events = []

    def damage(self, amount, source=None, kill_type=0):
        self.health -= amount
        self.damage_events.append((amount, source, kill_type))
        if self.health <= 0:
            self.alive = False
        return True


class _Server:
    def __init__(self, world, players):
        self.config = SimpleNamespace(build_damage=False)
        self.world_manager = world
        self.players = {player.id: player for player in players}
        self.entity_registry = EntityRegistry()
        self.created = []
        self.destroyed = []

    def broadcast_create_entity(self, ent):
        self.created.append(ent)

    def broadcast_destroy_entity(self, entity_id):
        self.destroyed.append(entity_id)


class _AlwaysSpread:
    """Deterministic RNG that exercises the worst-case spread branch."""

    @staticmethod
    def random():
        return 0.0

    @staticmethod
    def choice(values):
        return values[0]


def test_blockfire_entity_carries_retail_fuse_and_deduplicates_cells():
    owner = _Player()
    server = _Server(_World({(10, 10, 10)}), [owner])
    fire = FireController(server)

    entity_id = fire.ignite_block((10, 10, 10), owner, now=5.0)

    assert entity_id is not None
    assert fire.ignite_block((10, 10, 10), owner, now=5.0) is None
    ent = server.entity_registry.get(entity_id)
    assert ent.type == C.BLOCKFIRE
    assert ent.fuse == C.BLOCKFIRE_MAX_LIFESPAN
    assert ent.to_wire_entity().fuse == C.BLOCKFIRE_MAX_LIFESPAN
    assert ent.to_wire_entity().face == C.FACE_TOP


def test_molotov_impact_lights_exposed_nearby_blocks():
    owner = _Player()
    solids = {(x, 10, 10) for x in range(8, 13)}
    server = _Server(_World(solids), [owner])
    fire = FireController(server)

    ids = fire.ignite_impact(10.0, 10.0, 9.5, owner, now=0.0)

    assert 1 <= len(ids) <= C.BLOCKFIRE_SPREAD_COUNT


def test_molotov_wall_impact_creates_side_anchored_fire():
    owner = _Player()
    # A vertical wall has a solid voxel directly above every impact voxel.
    # The former top-only exposure check produced no fire here.
    wall = {(10, 10, z) for z in range(7, 13)}
    server = _Server(_World(wall), [owner])
    fire = FireController(server)

    ids = fire.ignite_impact(9.9, 10.5, 9.5, owner, now=0.0)

    assert ids
    entities = [server.entity_registry.get(entity_id) for entity_id in ids]
    assert all(entity.type == C.BLOCKFIRE for entity in entities)
    # The surface anchor may sit on a wall, but BlockFire is particle-only.
    # FACE_TOP=4 is the native base class's sole no-rotation branch; every
    # other face reaches rotate() and expects a model that fire does not own.
    assert all(entity.face == C.FACE_TOP for entity in entities)
    assert any(abs((entity.x % 1.0) - 0.5) > 0.1 for entity in entities)


def test_blockfire_ignites_player_and_fractional_damage_totals_exactly():
    owner = _Player(player_id=1, team=0, position=(10.0, 10.0, 9.0))
    target = _Player(player_id=2, team=1, position=(11.0, 10.0, 9.0))
    server = _Server(_World({(10, 10, 10)}), [owner, target])
    fire = FireController(server)
    fire.ignite_block((10, 10, 10), owner, now=0.0)

    fire.update(now=0.0)
    assert target.on_fire is True
    fire.update(now=0.3)
    fire.update(now=0.6)

    assert target.health == 95
    assert [event[0] for event in target.damage_events] == [2, 3]
    assert all(event[2] == C.KILL.BLOCKFIRE_KILL for event in target.damage_events)


def test_water_extinguishes_player_and_clears_world_update_state():
    owner = _Player(player_id=1)
    target = _Player(player_id=2)
    server = _Server(_World(), [owner, target])
    fire = FireController(server)
    fire.ignite_player(target, owner.id, now=0.0)
    target.wade = True

    fire.update(now=0.1)

    assert target.on_fire is False
    assert target.id not in fire.burning_players


def test_blockfire_expires_and_removes_replicated_entity():
    owner = _Player()
    server = _Server(_World({(10, 10, 10)}), [owner])
    fire = FireController(server)
    entity_id = fire.ignite_block((10, 10, 10), owner, now=0.0)

    fire.update(now=C.BLOCKFIRE_MAX_LIFESPAN)

    assert server.entity_registry.get(entity_id) is None
    assert server.destroyed == [entity_id]


def test_world_update_fire_bit_is_authoritative_not_client_spoofed():
    player = Player(7, "burn-test", 0, C.SMG_TOOL)
    player.input.is_on_fire = True
    assert player.pack_action_flags() & 0x20 == 0

    player.on_fire = True
    assert player.pack_action_flags() & 0x20 == 0x20


def test_blockfire_applies_retail_block_damage_on_timer(monkeypatch):
    owner = _Player()
    server = _Server(_World({(10, 10, 10)}), [owner])
    fire = FireController(server)
    entity_id = fire.ignite_block((10, 10, 10), owner, now=0.0)
    calls = []

    combat = SimpleNamespace(
        _apply_block_damage=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        "server.combat_runtime.get_combat_system", lambda _server: combat
    )

    fire.update(now=C.BLOCKFIRE_BLOCK_DAMAGE_TIMER - 0.01)
    assert calls == []
    fire.update(now=C.BLOCKFIRE_BLOCK_DAMAGE_TIMER)

    assert len(calls) == 1
    assert calls[0][0][1:] == ((10, 10, 10), C.BLOCKFIRE_BLOCK_DAMAGE)
    assert calls[0][1] == {
        "damage_type": C.BLOCKFIRE_DAMAGE,
        "causer_id": entity_id,
    }


def test_disconnect_forgets_owned_fire_and_burn_before_id_reuse():
    owner = _Player(player_id=1, team=0)
    target = _Player(player_id=2, team=1)
    server = _Server(_World({(10, 10, 10)}), [owner, target])
    fire = FireController(server)
    entity_id = fire.ignite_block((10, 10, 10), owner, now=0.0)
    fire.ignite_player(target, owner.id, now=0.0)

    fire.forget_player(owner.id)

    assert entity_id not in fire.block_fires
    assert server.entity_registry.get(entity_id) is None
    assert server.destroyed == [entity_id]
    assert target.id not in fire.burning_players
    assert target.on_fire is False


def test_one_molotov_uses_one_shared_spread_budget(monkeypatch):
    """Descendant fires must not receive a fresh recursive budget."""

    owner = _Player(position=(10.0, 10.0, 8.0))
    solids = {(x, y, 10) for x in range(4, 17) for y in range(4, 17)}
    server = _Server(_World(solids), [owner])
    fire = FireController(server, rng=_AlwaysSpread())
    monkeypatch.setattr(
        "server.combat_runtime.get_combat_system",
        lambda _server: SimpleNamespace(_apply_block_damage=lambda *a, **k: None),
    )

    initial = fire.ignite_impact(10.0, 10.0, 9.5, owner, now=0.0)
    maximum = len(initial)
    for step in range(1, 8):
        fire.update(now=step * C.BLOCKFIRE_SPREAD_TIMER)
        maximum = max(maximum, len(fire.block_fires))

    assert len(initial) == C.BLOCKFIRE_SPREAD_COUNT
    assert maximum <= len(initial) + C.BLOCKFIRE_SPREAD_COUNT


def test_blockfire_global_cap_replaces_oldest_emitters():
    owner = _Player(position=(0.0, 0.0, 0.0))
    count = FireController.MAX_ACTIVE_BLOCK_FIRES + 7
    solids = {(index, 0, 10) for index in range(count)}
    server = _Server(_World(solids), [owner])
    fire = FireController(server)

    for index in range(count):
        assert fire.ignite_block((index, 0, 10), owner, now=float(index)) is not None

    assert len(fire.block_fires) == FireController.MAX_ACTIVE_BLOCK_FIRES
    assert len(server.destroyed) == count - FireController.MAX_ACTIVE_BLOCK_FIRES
    assert len(fire._clusters) == FireController.MAX_ACTIVE_BLOCK_FIRES
